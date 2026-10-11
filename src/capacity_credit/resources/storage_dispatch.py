"""T14 storage dispatch strategies.

Dispatch strategies turn chronological raw supply/load signals into
per-hour charge/discharge REQUESTS; every SOC transition goes through
the T13 ``step_storage_portfolio`` physics (the T13 state equation,
portfolio mode, efficiency priorities and invariants are never
re-implemented here).

Strategies (methodology V0.4 §6.4):

- ``eue_greedy`` (default for storage_eue_credit / rmri_eue): charge
  only in surplus hours (never beyond the raw pre-storage surplus),
  discharge only in deficit hours (never beyond the deficit); no
  price/arbitrage signals; not presented as real market dispatch.
- ``risk_window`` (baseline rules only): the window is trained from the
  BASELINE system without the candidate storage (a candidate result can
  never retrain it); SOC is reserved before the window and allocated
  inside the window by deficit and remaining risk hours.
- ``perfect_foresight_eue`` (upper-bound interface): a two-pass
  full-information EUE upper bound; always emits
  STORAGE_PERFECT_FORESIGHT; never replaces eue_greedy and never trains
  risk windows.

All diagnostics reuse the T12 registry codes (no second diagnostic
dictionary); duplicates collapse (stable dedup by code).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from capacity_credit.resources.storage import (
    PortfolioUnit,
    StorageError,
    StoragePortfolioStepResult,
    StorageState,
    effective_soc_band,
    step_storage_portfolio,
)

_POWER_TOLERANCE_MW = 1e-9


def _strict_power(value: object, label: str) -> float:
    """Strict numeric gate for dispatch inputs (bool / strings / NaN /
    +-inf rejected; non-negative)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            f"{label} must be a real number, got {value!r}",
        )
    number = float(value)
    if not math.isfinite(number):
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT", f"{label} must be finite, got {value!r}"
        )
    if number < 0.0:
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            f"{label} must be non-negative, got {value!r}",
        )
    return number


def _strict_delta_t(value: object) -> float:
    """Strict finite POSITIVE gate for delta_t_hours (0 / negative /
    bool / NaN / +-inf blocked - never a ZeroDivisionError leak)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            f"delta_t_hours must be a real number, got {value!r}",
        )
    number = float(value)
    if not math.isfinite(number):
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            f"delta_t_hours must be finite, got {value!r}",
        )
    if number <= 0.0:
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            f"delta_t_hours must be positive, got {value!r}",
        )
    return number


@dataclass(frozen=True)
class DispatchHorizon:
    """One chronological sequence: raw pre-storage supply and load (MW)
    for every hour. Strictly numeric, equal length, non-negative."""

    pre_storage_supply_mw: tuple[float, ...]
    load_mw: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.pre_storage_supply_mw, tuple) or not isinstance(self.load_mw, tuple):
            raise StorageError(
                "STORAGE_DISPATCH_INVALID_INPUT",
                "horizon series must be tuples",
            )
        if len(self.pre_storage_supply_mw) != len(self.load_mw):
            raise StorageError(
                "STORAGE_DISPATCH_INVALID_INPUT",
                "supply and load series must have equal length",
            )
        if not self.pre_storage_supply_mw:
            raise StorageError("STORAGE_DISPATCH_INVALID_INPUT", "horizon must not be empty")
        object.__setattr__(
            self,
            "pre_storage_supply_mw",
            tuple(
                _strict_power(value, "pre_storage_supply_mw")
                for value in self.pre_storage_supply_mw
            ),
        )
        object.__setattr__(
            self,
            "load_mw",
            tuple(_strict_power(value, "load_mw") for value in self.load_mw),
        )

    @property
    def hours(self) -> int:
        return len(self.load_mw)


@dataclass(frozen=True)
class DispatchHourRecord:
    """Audit evidence for one dispatched hour."""

    hour_index: int
    surplus_mw: float
    deficit_mw: float
    portfolio: StoragePortfolioStepResult


@dataclass(frozen=True)
class DispatchOutcome:
    """Full dispatch outcome (scored risk comes ONLY from these hours -
    warm-up / burn-in passes are never scored into it)."""

    hours: tuple[DispatchHourRecord, ...]
    final_states: dict[str, StorageState]
    total_charge_mwh: float
    total_discharge_mwh: float
    eue_reduction_mwh: float
    diagnostics: tuple[str, ...]


def _check_units_and_states(
    units: list[PortfolioUnit], initial_states: dict[str, StorageState]
) -> dict[str, PortfolioUnit]:
    by_id: dict[str, PortfolioUnit] = {}
    for unit in units:
        if unit.spec.storage_id in by_id:
            raise StorageError(
                "STORAGE_DISPATCH_DUPLICATE_UNIT",
                f"duplicate storage_id {unit.spec.storage_id!r} in dispatch",
            )
        by_id[unit.spec.storage_id] = unit
    for storage_id in by_id:
        if storage_id not in initial_states:
            raise StorageError(
                "STORAGE_DISPATCH_MISSING_STATE",
                f"no initial SOC for storage {storage_id!r}",
            )
    for storage_id in initial_states:
        if storage_id not in by_id:
            raise StorageError(
                "STORAGE_DISPATCH_UNKNOWN_UNIT",
                f"initial SOC for unknown storage {storage_id!r}",
            )
    _validate_initial_states(by_id, initial_states)
    return by_id


def _validate_initial_states(
    unit_map: dict[str, PortfolioUnit],
    initial_states: dict[str, StorageState],
) -> None:
    """Initial SOC band validation shared by ALL dispatch entries: a
    state below the effective floor or above the effective cap blocks
    with the SAME stable code as ``step_storage`` (never silently
    truncated)."""
    for storage_id, unit in unit_map.items():
        e_min, e_max = effective_soc_band(unit.spec, unit.limits)
        soc_mwh = initial_states[storage_id].soc_mwh
        if not e_min - 1e-9 <= soc_mwh <= e_max + 1e-9:
            raise StorageError(
                "STORAGE_SOC_OUT_OF_BAND",
                f"interval-start SOC {soc_mwh} MWh for {storage_id!r} is "
                f"outside the effective band [{e_min}, {e_max}] MWh",
            )


def _units_with_states(
    units: dict[str, PortfolioUnit], states: dict[str, StorageState]
) -> list[PortfolioUnit]:
    return [
        PortfolioUnit(
            spec=units[storage_id].spec,
            state=states[storage_id],
            limits=units[storage_id].limits,
            requested_charge_mw=0.0,
            requested_discharge_mw=0.0,
        )
        for storage_id in sorted(units)
    ]


def _build_outcome(
    units: dict[str, PortfolioUnit],
    records: list[DispatchHourRecord],
    final_states: dict[str, StorageState],
    diagnostics: set[str],
) -> DispatchOutcome:
    """Energy totals summed from every unit's StorageStepResult
    system_side_charge_mwh / system_side_discharge_mwh (NEVER by
    accumulating MW fields - the time-step length belongs to the
    per-interval energy fields)."""
    total_charge = sum(
        entry.step.system_side_charge_mwh for record in records for entry in record.portfolio.units
    )
    total_discharge = sum(
        entry.step.system_side_discharge_mwh
        for record in records
        for entry in record.portfolio.units
    )
    eue_reduction = total_discharge
    return DispatchOutcome(
        hours=tuple(records),
        final_states=dict(final_states),
        total_charge_mwh=total_charge,
        total_discharge_mwh=total_discharge,
        eue_reduction_mwh=eue_reduction,
        diagnostics=tuple(sorted(diagnostics)),
    )


@dataclass(frozen=True)
class EndOfCycleRecharge:
    """W2/B6-2: the end-of-cycle recharge window (方法说明第三章(一)
    周期末回充约束, provisional). Inside the FINAL ``window_hours`` of a
    horizon, every unit's discharge is floored at
    ``target_soc_fraction x energy_capacity`` so the energy hands over to
    the next cycle. Pure constraint: no charging is ever invented.
    Implemented ONLY in the default eue_greedy path (risk_window /
    perfect-foresight diagnostic routes ignore it, per the work-order
    scope)."""

    window_hours: int
    target_soc_fraction: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.window_hours, bool)
            or not isinstance(self.window_hours, int)
            or self.window_hours <= 0
        ):
            raise StorageError(
                "STORAGE_DISPATCH_INVALID_INPUT",
                f"window_hours must be a positive integer, got {self.window_hours!r}",
            )
        number = float(self.target_soc_fraction)
        if not math.isfinite(number) or not 0.0 < number <= 1.0:
            raise StorageError(
                "STORAGE_DISPATCH_INVALID_INPUT",
                f"target_soc_fraction must be in (0, 1], got {self.target_soc_fraction!r}",
            )


def run_eue_greedy(
    units: list[PortfolioUnit],
    horizon: DispatchHorizon,
    *,
    initial_states: dict[str, StorageState],
    delta_t_hours: float = 1.0,
    end_of_cycle_recharge: EndOfCycleRecharge | None = None,
) -> DispatchOutcome:
    """eue_greedy (methodology §6.4): hourly portfolio dispatch over the
    raw pre-storage surplus/deficit. Every hour requests only the mode-
    legal direction (full rated capability in that direction); the T13
    portfolio mode, budget and efficiency priorities do the rest. EUE
    reduction equals the system-side discharge energy (never above it).

    ``end_of_cycle_recharge`` (W2/B6-2, optional): the discharge-side
    window floor described in :class:`EndOfCycleRecharge`."""
    _strict_delta_t(delta_t_hours)
    unit_map = _check_units_and_states(units, initial_states)
    states = dict(initial_states)
    records: list[DispatchHourRecord] = []
    diagnostics: set[str] = set()
    for hour_index in range(horizon.hours):
        supply = horizon.pre_storage_supply_mw[hour_index]
        load = horizon.load_mw[hour_index]
        surplus = max(0.0, supply - load)
        deficit = max(0.0, load - supply)
        mode = (
            "charge"
            if surplus > _POWER_TOLERANCE_MW
            else ("discharge" if deficit > _POWER_TOLERANCE_MW else "idle")
        )
        in_recharge_window = (
            end_of_cycle_recharge is not None
            and hour_index >= horizon.hours - end_of_cycle_recharge.window_hours
        )
        step_units = []
        for storage_id in sorted(unit_map):
            unit = unit_map[storage_id]
            charge_req = unit.spec.charge_power_mw if mode == "charge" else 0.0
            discharge_req = unit.spec.discharge_power_mw if mode == "discharge" else 0.0
            step_units.append(
                PortfolioUnit(
                    spec=unit.spec,
                    state=states[storage_id],
                    limits=unit.limits,
                    requested_charge_mw=charge_req,
                    requested_discharge_mw=discharge_req,
                )
            )
        discharge_reserve: dict[str, float] | None = None
        if in_recharge_window:
            discharge_reserve = {
                u.spec.storage_id: u.spec.energy_capacity_mwh
                * end_of_cycle_recharge.target_soc_fraction
                for u in unit_map.values()
            }
        portfolio = step_storage_portfolio(
            step_units,
            pre_storage_supply_mw=supply,
            load_mw=load,
            delta_t_hours=delta_t_hours,
            discharge_reserve_mwh=discharge_reserve,
        )
        for entry in portfolio.units:
            states[entry.storage_id] = StorageState(soc_mwh=entry.step.soc_after_mwh)
        diagnostics.update(portfolio.diagnostics)
        records.append(
            DispatchHourRecord(
                hour_index=hour_index,
                surplus_mw=surplus,
                deficit_mw=deficit,
                portfolio=portfolio,
            )
        )
    return _build_outcome(unit_map, records, states, diagnostics)


@dataclass(frozen=True)
class RiskWindowRule:
    """Baseline-trained risk window (methodology §6.4): immutable after
    training; a candidate run can never change it."""

    window_version: str
    baseline_identity: str
    training_source: str
    window_hours: frozenset[int]
    window_length_hours: int
    risk_threshold: float
    reserve_soc_fraction: float


def train_risk_window(
    horizon: DispatchHorizon,
    *,
    baseline_identity: str,
    risk_probability_by_hour: tuple[float, ...],
    window_length_hours: int,
    risk_threshold: float,
    reserve_soc_fraction: float,
    training_source: str,
) -> RiskWindowRule:
    """Train the risk window from the BASELINE system only (the
    candidate storage is absent from the training inputs)."""
    if len(risk_probability_by_hour) != horizon.hours:
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            "risk_probability_by_hour must match the horizon length",
        )
    if (
        isinstance(window_length_hours, bool)
        or not isinstance(window_length_hours, int)
        or window_length_hours <= 0
    ):
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            "window_length_hours must be a positive integer",
        )
    if window_length_hours > horizon.hours:
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            "window_length_hours must not exceed the horizon length",
        )
    risk_threshold = _strict_power(risk_threshold, "risk_threshold")
    if not 0.0 < risk_threshold < 1.0:
        raise StorageError("STORAGE_DISPATCH_INVALID_INPUT", "risk_threshold must lie in (0, 1)")
    reserve_soc_fraction = _strict_power(reserve_soc_fraction, "reserve_soc_fraction")
    if not 0.0 <= reserve_soc_fraction <= 1.0:
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            "reserve_soc_fraction must lie in [0, 1]",
        )
    if not isinstance(baseline_identity, str) or not baseline_identity.strip():
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            "baseline_identity must be a non-empty string",
        )
    if not isinstance(training_source, str) or not training_source.strip():
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            "training_source must be a non-empty string",
        )
    probabilities = tuple(
        _strict_power(value, "risk_probability_by_hour") for value in risk_probability_by_hour
    )
    if any(probability > 1.0 for probability in probabilities):
        raise StorageError(
            "STORAGE_DISPATCH_INVALID_INPUT",
            "risk probabilities must lie in [0, 1]",
        )
    # FIRST filter by the threshold (probability >= threshold), THEN
    # take the top window_length_hours by (probability desc, hour asc);
    # no hour qualifies -> an EMPTY window (full greedy fallback)
    qualifying = [
        index for index, probability in enumerate(probabilities) if probability >= risk_threshold
    ]
    ranked = sorted(qualifying, key=lambda index: (-probabilities[index], index))
    window_hours = frozenset(ranked[:window_length_hours])
    payload = (
        f"{baseline_identity}|{training_source}|{sorted(window_hours)}|"
        f"{window_length_hours}|{risk_threshold}|{reserve_soc_fraction}"
    )
    version = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return RiskWindowRule(
        window_version=version,
        baseline_identity=baseline_identity,
        training_source=training_source,
        window_hours=window_hours,
        window_length_hours=window_length_hours,
        risk_threshold=risk_threshold,
        reserve_soc_fraction=reserve_soc_fraction,
    )


def run_risk_window(
    units: list[PortfolioUnit],
    horizon: DispatchHorizon,
    rule: RiskWindowRule,
    *,
    initial_states: dict[str, StorageState],
    delta_t_hours: float = 1.0,
) -> DispatchOutcome:
    """Baseline risk-window dispatch: before the window charge up to the
    per-unit reserve target (above the reserve SOC the unit may still
    serve deficits); inside the window discharge by deficit and the
    remaining risk hours (energy spread over the remaining window
    hours); after the window fall back to greedy. The rule is consumed
    read-only - a candidate run never retrains it.

    Three phases (second remediation): (1) future risk window hours
    remain -> the RESERVE rule (charge toward the reserve target, serve
    deficits only above it); (2) the current hour IS a window member ->
    allocate energy by deficit and the REMAINING ACTUAL risk hours;
    (3) every risk hour has passed -> PURE eue_greedy (no SOC kept for
    a window that is already over). The reserve target and the
    in-window available energy use the T13 ``effective_soc_band``
    (reserved energy / hourly available energy respected - never the
    nominal usable band)."""
    _strict_delta_t(delta_t_hours)
    unit_map = _check_units_and_states(units, initial_states)
    states = dict(initial_states)
    records: list[DispatchHourRecord] = []
    diagnostics: set[str] = set()
    last_window_hour = max(rule.window_hours) if rule.window_hours else -1
    for hour_index in range(horizon.hours):
        supply = horizon.pre_storage_supply_mw[hour_index]
        load = horizon.load_mw[hour_index]
        surplus = max(0.0, supply - load)
        deficit = max(0.0, load - supply)
        mode = (
            "charge"
            if surplus > _POWER_TOLERANCE_MW
            else ("discharge" if deficit > _POWER_TOLERANCE_MW else "idle")
        )
        # membership is EXACT: ordinary hours between sparse window
        # members are NOT window members (never min/max span)
        in_window = hour_index in rule.window_hours
        # remaining risk hours = ACTUAL window members at or after this
        # hour (not the min/max arithmetic)
        remaining_window_hours = (
            sum(1 for member in rule.window_hours if member >= hour_index) if in_window else 0
        )
        # future risk hours still exist -> the reserve phase applies to
        # ordinary hours; once the last risk hour passed -> pure greedy
        future_window_remaining = last_window_hour >= hour_index
        step_units = []
        for storage_id in sorted(unit_map):
            unit = unit_map[storage_id]
            charge_req = 0.0
            discharge_req = 0.0
            e_min_eff, e_max_eff = effective_soc_band(unit.spec, unit.limits)
            if in_window and mode == "discharge":
                # allocate the remaining SOC-side energy over the
                # remaining risk hours (the deficit itself is capped by
                # the T13 portfolio budget)
                soc_side_available = max(0.0, states[storage_id].soc_mwh - e_min_eff)
                per_hour_soc_side = soc_side_available / remaining_window_hours
                discharge_req = per_hour_soc_side * unit.spec.discharge_efficiency / delta_t_hours
            elif future_window_remaining and not in_window and mode == "charge":
                # charge toward the reserve TARGET only (effective band):
                # the request is capped by the distance to the target
                reserve_target = e_min_eff + rule.reserve_soc_fraction * (e_max_eff - e_min_eff)
                soc_gap = reserve_target - states[storage_id].soc_mwh
                if soc_gap > 1e-9:
                    charge_req = min(
                        unit.spec.charge_power_mw,
                        soc_gap / (unit.spec.charge_efficiency * delta_t_hours),
                    )
            elif future_window_remaining and not in_window and mode == "discharge":
                # only SOC above the reserve target may serve pre-window
                # deficits
                reserve_target = e_min_eff + rule.reserve_soc_fraction * (e_max_eff - e_min_eff)
                soc_above_reserve = max(0.0, states[storage_id].soc_mwh - reserve_target)
                discharge_req = min(
                    unit.spec.discharge_power_mw,
                    soc_above_reserve * unit.spec.discharge_efficiency / delta_t_hours,
                )
            else:
                # phase 3 (or in-window non-deficit hour): pure greedy
                if mode == "charge":
                    charge_req = unit.spec.charge_power_mw
                elif mode == "discharge":
                    discharge_req = unit.spec.discharge_power_mw
            step_units.append(
                PortfolioUnit(
                    spec=unit.spec,
                    state=states[storage_id],
                    limits=unit.limits,
                    requested_charge_mw=charge_req,
                    requested_discharge_mw=discharge_req,
                )
            )
        portfolio = step_storage_portfolio(
            step_units,
            pre_storage_supply_mw=supply,
            load_mw=load,
            delta_t_hours=delta_t_hours,
        )
        for entry in portfolio.units:
            states[entry.storage_id] = StorageState(soc_mwh=entry.step.soc_after_mwh)
        diagnostics.update(portfolio.diagnostics)
        records.append(
            DispatchHourRecord(
                hour_index=hour_index,
                surplus_mw=surplus,
                deficit_mw=deficit,
                portfolio=portfolio,
            )
        )
    return _build_outcome(unit_map, records, states, diagnostics)


@dataclass(frozen=True)
class PerfectForesightBound:
    """Theoretical EUE upper bound (methodology §6.4): computed with
    FULL horizon information as the exact max-flow on the time-expanded
    storage network of the LOSSLESS portfolio aggregate (a relaxation
    of the real lossy multi-unit system, hence a VALID upper bound -
    never below the achievable optimum). Always emits
    STORAGE_PERFECT_FORESIGHT; never the default strategy."""

    eue_reduction_mwh: float
    total_discharge_mwh: float
    total_charge_mwh: float
    deficit_demand_mwh: float
    initial_energy_side_mwh: float
    charge_capacity_mw: float
    discharge_capacity_mw: float
    energy_band_mwh: float
    diagnostics: tuple[str, ...]


def _shortest_residual_path(
    capacities: dict[tuple[int, int], float],
    costs: dict[tuple[int, int], float],
) -> tuple[dict[int, int], bool]:
    """Bellman-Ford shortest source(-1) -> sink(-2) path over the
    residual graph (C901 split); returns the parent map and whether a
    path exists."""
    nodes = {node for edge in capacities for node in edge}
    distance: dict[int, float] = {node: float("inf") for node in nodes}
    parent: dict[int, int] = {}
    distance[-1] = 0.0
    for _ in range(len(nodes)):
        changed = False
        for (source, target), capacity in capacities.items():
            if capacity <= 1e-12:
                continue
            candidate = distance[source] + costs[(source, target)]
            if candidate < distance[target] - 1e-12:
                distance[target] = candidate
                parent[target] = source
                changed = True
        if not changed:
            break
    return parent, -2 in parent


def _min_cost_max_flow(
    capacities: dict[tuple[int, int], float],
    costs: dict[tuple[int, int], float],
) -> float:
    """Successive-shortest-path min-cost MAX flow (C901 split) on the
    time-expanded network: every augmentation follows the shortest
    residual path, so the final flow maximizes the discharge AND
    minimizes the charge cost among maximum flows (the deterministic
    secondary objective). Node -1 is the source, -2 the sink.
    ``capacities`` becomes the RESIDUAL graph; edge flows are recovered
    by the caller as original - residual."""
    flow = 0.0
    while True:
        parent, reachable = _shortest_residual_path(capacities, costs)
        if not reachable:
            break
        bottleneck = float("inf")
        node = -2
        while node != -1:
            previous = parent[node]
            bottleneck = min(bottleneck, capacities[(previous, node)])
            node = previous
        node = -2
        while node != -1:
            previous = parent[node]
            capacities[(previous, node)] -= bottleneck
            reverse = (node, previous)
            capacities[reverse] = capacities.get(reverse, 0.0) + bottleneck
            costs[reverse] = -costs[(previous, node)]
            node = previous
        flow += bottleneck
    return flow


def run_perfect_foresight_eue(
    units: list[PortfolioUnit],
    horizon: DispatchHorizon,
    *,
    initial_states: dict[str, StorageState],
    delta_t_hours: float = 1.0,
) -> PerfectForesightBound:
    """Perfect-foresight EUE ideal upper bound (methodology §6.4).

    Exact for the time-expanded single-portfolio structure: the
    lossless (efficiency = 1) portfolio aggregate is a path max-flow
    network with STATE-NODE SPLITTING - every time node has an IN and an
    OUT side connected by the pooled usable energy band, so the initial
    energy and every hourly charge must pass THROUGH the state capacity
    (a state can never exceed the band); discharges draw from the OUT
    side (energy stored at the START of the hour) and charges arrive at
    the IN side of the NEXT hour. The flow is computed with a
    successive-shortest-path min-cost MAX flow: the deterministic
    secondary objective minimizes unnecessary charging, so the audited
    net charge / discharge flows come from a REALIZABLE optimal
    trajectory (fixed initial SOC respected, energy conservation and
    every hourly direction/power constraint satisfied) - never a
    post-hoc truncation. With real (lossy) units the true optimum is
    <= this bound."""
    delta_t_hours = _strict_delta_t(delta_t_hours)
    unit_map = _check_units_and_states(units, initial_states)
    diagnostics = {"STORAGE_PERFECT_FORESIGHT"}
    charge_capacity = sum(unit.spec.charge_power_mw for unit in unit_map.values())
    discharge_capacity = sum(unit.spec.discharge_power_mw for unit in unit_map.values())
    # pooled usable band and pooled initial SOC-side energy (valid
    # RELAXATION for an upper bound - energy is allowed to move between
    # units)
    energy_band = sum(unit.spec.usable_energy_mwh for unit in unit_map.values())
    initial_side = sum(
        max(
            0.0,
            initial_states[unit.spec.storage_id].soc_mwh - unit.spec.energy_min_mwh,
        )
        for unit in unit_map.values()
    )

    def state_in(hour_index: int) -> int:
        return 2 * hour_index

    def state_out(hour_index: int) -> int:
        return 2 * hour_index + 1

    # time-expanded path flow with state splitting. All capacity in
    # SOC-side MWh (lossless); charge edges cost 1 (minimized as the
    # secondary objective), everything else costs 0.
    capacities: dict[tuple[int, int], float] = {}
    costs: dict[tuple[int, int], float] = {}
    for hour_index in range(horizon.hours + 1):
        edge = (state_in(hour_index), state_out(hour_index))
        capacities[edge] = energy_band
        costs[edge] = 0.0
    capacities[(-1, state_in(0))] = initial_side
    costs[(-1, state_in(0))] = 0.0
    charge_edges: list[tuple[int, int]] = []
    discharge_edges: list[tuple[int, int]] = []
    for hour_index in range(horizon.hours):
        surplus = max(
            0.0,
            horizon.pre_storage_supply_mw[hour_index] - horizon.load_mw[hour_index],
        )
        deficit = max(
            0.0,
            horizon.load_mw[hour_index] - horizon.pre_storage_supply_mw[hour_index],
        )
        if surplus > _POWER_TOLERANCE_MW:
            # charging during hour i arrives at the IN side of hour i+1
            edge = (-1, state_in(hour_index + 1))
            capacities[edge] = min(surplus, charge_capacity) * delta_t_hours
            costs[edge] = 1.0
            charge_edges.append(edge)
        if deficit > _POWER_TOLERANCE_MW:
            # discharging during hour i draws from the OUT side of hour i
            edge = (state_out(hour_index), -2)
            capacities[edge] = min(deficit, discharge_capacity) * delta_t_hours
            costs[edge] = 0.0
            discharge_edges.append(edge)
        # unbounded carry between consecutive hour states
        carry = (state_out(hour_index), state_in(hour_index + 1))
        capacities[carry] = float("inf")
        costs[carry] = 0.0

    original = dict(capacities)
    flow = _min_cost_max_flow(capacities, costs)
    net_charge = sum(max(0.0, original[edge] - capacities[edge]) for edge in charge_edges)
    net_discharge = sum(max(0.0, original[edge] - capacities[edge]) for edge in discharge_edges)

    deficit_demand = (
        sum(
            max(
                0.0,
                horizon.load_mw[hour_index] - horizon.pre_storage_supply_mw[hour_index],
            )
            for hour_index in range(horizon.hours)
        )
        * delta_t_hours
    )
    return PerfectForesightBound(
        eue_reduction_mwh=flow,
        total_discharge_mwh=net_discharge,
        total_charge_mwh=net_charge,
        deficit_demand_mwh=deficit_demand,
        initial_energy_side_mwh=initial_side,
        charge_capacity_mw=charge_capacity,
        discharge_capacity_mw=discharge_capacity,
        energy_band_mwh=energy_band,
        diagnostics=tuple(sorted(diagnostics)),
    )


__all__ = [
    "DispatchHorizon",
    "DispatchHourRecord",
    "DispatchOutcome",
    "PerfectForesightBound",
    "RiskWindowRule",
    "run_eue_greedy",
    "run_perfect_foresight_eue",
    "run_risk_window",
    "train_risk_window",
]
