"""Storage single-interval state equation, portfolio mode and enforced
physical invariants (T13).

Scope (methodology V0.4 section 6):

- interval-end SOC state equation with float64 arithmetic:
  ``E_next = raw + eta_c*Pc*dt - Pd*dt/eta_d`` where
  ``raw = E_cur*(1-lambda*dt)`` is the STRICT raw self-discharge value
  (never clamped - clamping would invent energy with no source); the
  SOC bounds are judged on the FULL interval-end state, so a raw value
  below the floor is legal when charging lifts the interval-end SOC
  back into the band and blocks (stable code) only when the final
  state is below the floor; lambda*dt > 1 blocks with a stable code
  (the linear discrete equation is inapplicable);
- SOC bounds enforced by writing the ACTUAL charge/discharge power back
  from the bounds - a clipped SOC never reports impossible system-side
  power (S-05);
- power chain: requested -> constrained (power / availability / grid /
  hourly available power / reservations) -> actual (SOC bounds); all
  three are reported;
- units: MW (power), MWh (energy), hour (time), fractions in [0, 1];
- the availability-scaled rating and the hourly ABSOLUTE MW caps are
  INDEPENDENT candidate upper bounds taking the minimum - an absolute
  hourly value is never multiplied by availability again; reservations
  subtract last and BLOCK when exceeding the technical capability
  (never silently truncated); hourly ``available_energy_mwh`` lowers
  the effective energy band; fixed/hourly/stochastic are availability
  SOURCE modes - an adapter must never feed the same signal as both a
  fixed fraction and an hourly absolute capability (T13 covers only
  the single-interval physics; chronological selection belongs to
  T14);
- simultaneous charge+discharge of the SAME resource in the SAME
  interval is rejected with a stable code (never netted);
- portfolio mode from the raw pre-storage surplus/deficit: only charge
  in surplus hours, only discharge in deficit hours, full idle when
  balanced; cross-charging between storages is impossible by
  construction and re-checked by a defensive validator that raises the
  blocking STORAGE_CROSS_CHARGING_ATTEMPT;
- allocation priority implements methodology 6.5 with an EXPLICIT sort
  key (charge: higher charge efficiency first; discharge: higher
  discharge efficiency first; ties broken by storage_id ascending) -
  never input order, dict insertion order or set iteration order;
- diagnostics reuse the T12 registry codes ONLY
  (STORAGE_CROSS_CHARGING_BLOCKED info / STORAGE_CROSS_CHARGING_ATTEMPT
  blocking method error); no second storage diagnostic enum exists, and
  dynamic T13 diagnostics are not written into the search trace or the
  ScenarioBank object diagnostics (T16 wires them through the upstream
  channel).

T14 chronological logic lives in the SIBLING modules
(``storage_dispatch.py``: eue_greedy / risk_window /
perfect_foresight_eue request generation; ``storage_chronology.py``:
warm-up / burn-in / year boundaries) and always calls back into
``step_storage`` / ``step_storage_portfolio`` here - this module
remains the single source of the single-interval physics and exposes
``effective_soc_band`` as the single source of the effective SOC band.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from capacity_credit.contracts.elcc_diagnostic_specs import (
    ElccDiagnosticsError,
    diagnostic_spec,
)

# Floating-point comparison tolerance ONLY - it must never hide a real
# energy or power violation (property-tested).
_POWER_TOLERANCE_MW = 1e-9
_ENERGY_TOLERANCE_MWH = 1e-9


class StorageError(ValueError):
    """Stable storage-model error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _finite_number(value: Any, code: str, *, allow_negative: bool = True) -> float:
    """Strict numeric coercion: bools, strings and non-finite values are
    rejected - pseudo-numbers never bypass the model validation."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise StorageError(code, f"value must be a real number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise StorageError(code, f"value must be finite, got {value!r}")
    if not allow_negative and result < 0.0:
        raise StorageError(code, f"value must be non-negative, got {result!r}")
    return result


def _normalize_spec_fields(spec: StorageSpec) -> dict[str, Any]:
    """Validate and normalize every StorageSpec field (C901 split); the
    frozen dataclass __post_init__ only applies the normalized values."""
    normalized: dict[str, Any] = {}
    if not isinstance(spec.storage_id, str) or not spec.storage_id.strip():
        raise StorageError("STORAGE_INVALID_ID", "storage_id must be a non-empty string")
    normalized["storage_id"] = spec.storage_id.strip()
    charge = _finite_number(spec.charge_power_mw, "STORAGE_INVALID_POWER", allow_negative=False)
    discharge = _finite_number(
        spec.discharge_power_mw, "STORAGE_INVALID_POWER", allow_negative=False
    )
    energy = _finite_number(
        spec.energy_capacity_mwh, "STORAGE_INVALID_ENERGY_CAPACITY", allow_negative=False
    )
    if charge <= 0.0 or discharge <= 0.0:
        raise StorageError("STORAGE_INVALID_POWER", "power ratings must be positive")
    if energy <= 0.0:
        raise StorageError(
            "STORAGE_INVALID_ENERGY_CAPACITY", "energy_capacity_mwh must be positive"
        )
    normalized["charge_power_mw"] = charge
    normalized["discharge_power_mw"] = discharge
    normalized["energy_capacity_mwh"] = energy
    charge_efficiency = _finite_number(spec.charge_efficiency, "STORAGE_INVALID_EFFICIENCY")
    discharge_efficiency = _finite_number(spec.discharge_efficiency, "STORAGE_INVALID_EFFICIENCY")
    if not 0.0 < charge_efficiency <= 1.0 or not 0.0 < discharge_efficiency <= 1.0:
        raise StorageError("STORAGE_INVALID_EFFICIENCY", "efficiencies must lie in (0, 1]")
    normalized["charge_efficiency"] = charge_efficiency
    normalized["discharge_efficiency"] = discharge_efficiency
    soc_min = _finite_number(spec.soc_min_fraction, "STORAGE_INVALID_SOC_BOUNDS")
    soc_max = _finite_number(spec.soc_max_fraction, "STORAGE_INVALID_SOC_BOUNDS")
    if not (0.0 <= soc_min < soc_max <= 1.0):
        raise StorageError(
            "STORAGE_INVALID_SOC_BOUNDS",
            "soc fractions must satisfy 0 <= min < max <= 1",
        )
    normalized["soc_min_fraction"] = soc_min
    normalized["soc_max_fraction"] = soc_max
    self_discharge = _finite_number(spec.self_discharge_per_hour, "STORAGE_INVALID_SELF_DISCHARGE")
    if not 0.0 <= self_discharge < 1.0:
        raise StorageError(
            "STORAGE_INVALID_SELF_DISCHARGE",
            "self_discharge_per_hour must lie in [0, 1)",
        )
    normalized["self_discharge_per_hour"] = self_discharge
    availability = _finite_number(spec.availability_fraction, "STORAGE_INVALID_AVAILABILITY")
    if not 0.0 <= availability <= 1.0:
        raise StorageError(
            "STORAGE_INVALID_AVAILABILITY", "availability_fraction must lie in [0, 1]"
        )
    normalized["availability_fraction"] = availability
    grid = spec.grid_connection_limit_mw
    if grid is not None:
        grid = _finite_number(grid, "STORAGE_INVALID_GRID_LIMIT")
        if grid <= 0.0:
            raise StorageError(
                "STORAGE_INVALID_GRID_LIMIT", "grid_connection_limit_mw must be positive"
            )
    normalized["grid_connection_limit_mw"] = grid
    return normalized


@dataclass(frozen=True)
class StorageSpec:
    """Immutable technical storage parameters (data contract V0.3 §18)."""

    storage_id: str
    charge_power_mw: float
    discharge_power_mw: float
    energy_capacity_mwh: float
    charge_efficiency: float
    discharge_efficiency: float
    soc_min_fraction: float
    soc_max_fraction: float
    self_discharge_per_hour: float
    availability_fraction: float = 1.0
    grid_connection_limit_mw: float | None = None

    def __post_init__(self) -> None:
        for name, value in _normalize_spec_fields(self).items():
            object.__setattr__(self, name, value)

    @property
    def energy_min_mwh(self) -> float:
        return self.energy_capacity_mwh * self.soc_min_fraction

    @property
    def energy_max_mwh(self) -> float:
        return self.energy_capacity_mwh * self.soc_max_fraction

    @property
    def usable_energy_mwh(self) -> float:
        return self.energy_max_mwh - self.energy_min_mwh


@dataclass(frozen=True)
class StorageState:
    """Immutable interval-end SOC state (MWh)."""

    soc_mwh: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "soc_mwh",
            _finite_number(self.soc_mwh, "STORAGE_INVALID_STATE", allow_negative=False),
        )


@dataclass(frozen=True)
class StorageOperatingLimits:
    """Immutable per-interval operating limits (contract §18.4 / §19.1).

    Hourly absolute capabilities (all optional, units MW / MW / MWh,
    asymmetric charge/discharge allowed): ``available_charge_power_mw``,
    ``available_discharge_power_mw`` and ``available_energy_mwh``
    (mapped onto the effective energy band). bool / string
    pseudo-numbers / NaN / inf / negatives are rejected with a stable
    code."""

    availability_fraction: float = 1.0
    grid_connection_limit_mw: float | None = None
    reserved_charge_mw: float = 0.0
    reserved_discharge_mw: float = 0.0
    reserved_energy_mwh: float = 0.0
    available_charge_power_mw: float | None = None
    available_discharge_power_mw: float | None = None
    available_energy_mwh: float | None = None

    def __post_init__(self) -> None:
        availability = _finite_number(self.availability_fraction, "STORAGE_INVALID_LIMITS")
        if not 0.0 <= availability <= 1.0:
            raise StorageError("STORAGE_INVALID_LIMITS", "availability_fraction must lie in [0, 1]")
        object.__setattr__(self, "availability_fraction", availability)
        if self.grid_connection_limit_mw is not None:
            grid = _finite_number(self.grid_connection_limit_mw, "STORAGE_INVALID_LIMITS")
            if grid <= 0.0:
                raise StorageError(
                    "STORAGE_INVALID_LIMITS", "grid_connection_limit_mw must be positive"
                )
            object.__setattr__(self, "grid_connection_limit_mw", grid)
        for name in (
            "reserved_charge_mw",
            "reserved_discharge_mw",
            "reserved_energy_mwh",
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(getattr(self, name), "STORAGE_INVALID_LIMITS", allow_negative=False),
            )
        for name in (
            "available_charge_power_mw",
            "available_discharge_power_mw",
            "available_energy_mwh",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            object.__setattr__(
                self,
                name,
                _finite_number(value, "STORAGE_INVALID_LIMITS", allow_negative=False),
            )


def _effective_bounds(
    spec: StorageSpec, limits: StorageOperatingLimits
) -> tuple[float, float, float, float]:
    """Effective charge/discharge power caps and energy band (second-
    review B): the availability-scaled rating and the hourly ABSOLUTE
    MW caps are INDEPENDENT candidate upper bounds taking the minimum -
    an absolute hourly MW value is never multiplied by availability
    again. Reservations subtract LAST; a reservation exceeding the
    available technical capability BLOCKS (never silently truncated to
    0). ``available_energy_mwh`` lowers the effective upper bound of
    the energy band."""

    def _cap(
        rating: float,
        reserved: float,
        grid_limit: float | None,
        hourly_available: float | None,
    ) -> float:
        limits_grid = limits.grid_connection_limit_mw
        spec_grid = spec.grid_connection_limit_mw
        if grid_limit is None:
            grid_limit = limits_grid
        if spec_grid is not None:
            grid_limit = min(grid_limit, spec_grid) if grid_limit is not None else spec_grid
        fractional_cap = rating * spec.availability_fraction * limits.availability_fraction
        technical = rating
        if grid_limit is not None:
            technical = min(technical, grid_limit)
        technical = min(technical, fractional_cap)
        if hourly_available is not None:
            technical = min(technical, hourly_available)
        effective = technical - reserved
        if effective < -_POWER_TOLERANCE_MW:
            raise StorageError(
                "STORAGE_RESERVATION_EXCEEDS_CAPACITY",
                f"reservation {reserved} MW exceeds the available technical "
                f"capability {technical} MW",
            )
        return max(0.0, effective)

    effective_charge_cap = _cap(
        spec.charge_power_mw,
        limits.reserved_charge_mw,
        limits.grid_connection_limit_mw,
        limits.available_charge_power_mw,
    )
    effective_discharge_cap = _cap(
        spec.discharge_power_mw,
        limits.reserved_discharge_mw,
        limits.grid_connection_limit_mw,
        limits.available_discharge_power_mw,
    )
    e_min = spec.energy_min_mwh + limits.reserved_energy_mwh
    e_max = spec.energy_max_mwh
    if limits.available_energy_mwh is not None:
        e_max = min(e_max, limits.available_energy_mwh)
    if e_min > e_max + _ENERGY_TOLERANCE_MWH:
        if limits.available_energy_mwh is not None and (
            limits.available_energy_mwh < e_min - _ENERGY_TOLERANCE_MWH
        ):
            raise StorageError(
                "STORAGE_AVAILABLE_ENERGY_INFEASIBLE",
                f"hourly available energy {limits.available_energy_mwh} MWh is "
                f"below the effective SOC floor {e_min} MWh",
            )
        raise StorageError(
            "STORAGE_RESERVATION_EXCEEDS_CAPACITY",
            f"reserved energy {limits.reserved_energy_mwh} MWh exceeds the "
            f"usable band {spec.usable_energy_mwh} MWh",
        )
    return effective_charge_cap, effective_discharge_cap, e_min, e_max


def effective_soc_band(spec: StorageSpec, limits: StorageOperatingLimits) -> tuple[float, float]:
    """Public single-source effective SOC band (T14): the SAME bounds
    used inside ``step_storage`` - E_min + reserved_energy_mwh and
    min(E_max, available_energy_mwh). Higher layers (warm-up / burn-in
    / chronology) must consume THIS interface instead of re-deriving a
    band that could drift. Infeasible bands raise the same stable codes
    as ``step_storage``."""
    _charge_cap, _discharge_cap, e_min, e_max = _effective_bounds(spec, limits)
    return e_min, e_max


@dataclass(frozen=True)
class StorageStepResult:
    """One single-interval storage transition (immutable, float64).

    Field semantics:

    - requested_charge_mw / requested_discharge_mw: the caller's ask;
    - constrained_charge_mw / constrained_discharge_mw: after power /
      availability / grid / reservation caps;
    - actual_charge_mw / actual_discharge_mw: after SOC bounds
      (back-computed; never reports impossible system-side power);
    - soc_before_mwh: interval-start SOC;
    - soc_after_self_discharge_mwh: RAW self-discharge result
      (E x (1 - lambda*dt)), never clamped; the SOC lower bound is
      judged on the FULL interval-end state - a raw value below the
      floor is legal when charging lifts the interval-end SOC back
      into the band;
    - soc_after_mwh: interval-end SOC;
    - system_side_*_mwh: energy seen by the system (power x dt);
    - soc_side_*_mwh: energy actually stored/withdrawn (efficiency
      applied);
    - diagnostics: registry-validated diagnostic codes.
    """

    requested_charge_mw: float
    requested_discharge_mw: float
    constrained_charge_mw: float
    constrained_discharge_mw: float
    actual_charge_mw: float
    actual_discharge_mw: float
    soc_before_mwh: float
    soc_after_self_discharge_mwh: float
    soc_after_mwh: float
    system_side_charge_mwh: float
    soc_side_charge_mwh: float
    system_side_discharge_mwh: float
    soc_side_discharge_mwh: float
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for code in self.diagnostics:
            try:
                diagnostic_spec(code)
            except ElccDiagnosticsError as exc:
                raise StorageError(
                    "STORAGE_UNKNOWN_DIAGNOSTIC",
                    f"diagnostic {code!r} is not in the ELCC registry",
                ) from exc


def step_storage(
    spec: StorageSpec,
    state: StorageState,
    limits: StorageOperatingLimits,
    *,
    requested_charge_mw: float = 0.0,
    requested_discharge_mw: float = 0.0,
    delta_t_hours: float = 1.0,
) -> StorageStepResult:
    """One pure single-interval storage transition (interval-end SOC)."""
    charge_req = _finite_number(
        requested_charge_mw, "STORAGE_INVALID_REQUEST", allow_negative=False
    )
    discharge_req = _finite_number(
        requested_discharge_mw, "STORAGE_INVALID_REQUEST", allow_negative=False
    )
    delta_t = _finite_number(delta_t_hours, "STORAGE_INVALID_DELTA_T")
    if delta_t <= 0.0:
        raise StorageError("STORAGE_INVALID_DELTA_T", "delta_t_hours must be positive")
    if spec.self_discharge_per_hour * delta_t > 1.0:
        raise StorageError(
            "STORAGE_SELF_DISCHARGE_RATE_INVALID",
            "self_discharge_per_hour x delta_t_hours > 1 makes the linear "
            "discrete state equation inapplicable (negative raw energy); "
            "the step is blocked, never clamped",
        )
    if charge_req > _POWER_TOLERANCE_MW and discharge_req > _POWER_TOLERANCE_MW:
        raise StorageError(
            "STORAGE_SIMULTANEOUS_CHARGE_DISCHARGE",
            "the same resource must not charge and discharge in the same "
            "interval; the request is rejected, never netted",
        )
    charge_cap, discharge_cap, e_min, e_max = _effective_bounds(spec, limits)
    soc_before = state.soc_mwh
    if not e_min - _ENERGY_TOLERANCE_MWH <= soc_before <= e_max + _ENERGY_TOLERANCE_MWH:
        raise StorageError(
            "STORAGE_SOC_OUT_OF_BAND",
            f"interval-start SOC {soc_before} MWh is outside the effective "
            f"band [{e_min}, {e_max}] MWh",
        )

    # Second-review fix A: the RAW self-discharged SOC is strictly
    # computed (never clamped). The SOC lower bound is judged on the
    # FULL interval-end state AFTER the power-constrained charge and
    # discharge are applied - a raw value below the floor is still
    # legal when charging lifts the interval-end SOC back into the
    # band. No energy is ever invented; discharge is impossible when
    # the intermediate value is below the floor (its room is <= 0).
    raw_after_self_discharge = soc_before * (1.0 - spec.self_discharge_per_hour * delta_t)
    soc_after_self_discharge = raw_after_self_discharge

    constrained_charge = min(charge_req, charge_cap)
    charge_room = (e_max - soc_after_self_discharge) / (spec.charge_efficiency * delta_t)
    actual_charge = min(constrained_charge, max(0.0, charge_room))

    constrained_discharge = min(discharge_req, discharge_cap)
    discharge_room = (soc_after_self_discharge - e_min) * spec.discharge_efficiency / delta_t
    actual_discharge = min(constrained_discharge, max(0.0, discharge_room))

    soc_side_charge = spec.charge_efficiency * actual_charge * delta_t
    soc_side_discharge = actual_discharge * delta_t / spec.discharge_efficiency
    soc_after = soc_after_self_discharge + soc_side_charge - soc_side_discharge

    # FULL interval-end bound checks (the approved state equation):
    if soc_after < e_min - _ENERGY_TOLERANCE_MWH:
        raise StorageError(
            "STORAGE_SELF_DISCHARGE_BELOW_FLOOR",
            f"interval-end SOC {soc_after} MWh is below the effective SOC "
            f"floor {e_min} MWh; the full-interval state equation yields "
            "an infeasible state (no energy is invented to reach the floor)",
        )
    if soc_after > e_max + _ENERGY_TOLERANCE_MWH:
        raise StorageError(
            "STORAGE_BAND_VIOLATION",
            "interval-end SOC violates the effective band (internal invariant)",
        )
    # Float-remainder snap: charge/discharge round-trips (x/eff then
    # x*eff) can leave a ~1e-15 residual below the floor; snap values
    # inside the tolerance band onto the boundary (never hides a real
    # violation - those are rejected above).
    if abs(soc_after - e_min) <= _ENERGY_TOLERANCE_MWH:
        soc_after = e_min
    if abs(soc_after - e_max) <= _ENERGY_TOLERANCE_MWH:
        soc_after = e_max
    return StorageStepResult(
        requested_charge_mw=charge_req,
        requested_discharge_mw=discharge_req,
        constrained_charge_mw=constrained_charge,
        constrained_discharge_mw=constrained_discharge,
        actual_charge_mw=actual_charge,
        actual_discharge_mw=actual_discharge,
        soc_before_mwh=soc_before,
        soc_after_self_discharge_mwh=soc_after_self_discharge,
        soc_after_mwh=soc_after,
        system_side_charge_mwh=actual_charge * delta_t,
        soc_side_charge_mwh=soc_side_charge,
        system_side_discharge_mwh=actual_discharge * delta_t,
        soc_side_discharge_mwh=soc_side_discharge,
        diagnostics=(),
    )


@dataclass(frozen=True)
class PortfolioUnit:
    """One storage resource in a portfolio step: spec + state + limits +
    requested power (the dispatch strategy's ask)."""

    spec: StorageSpec
    state: StorageState
    limits: StorageOperatingLimits
    requested_charge_mw: float = 0.0
    requested_discharge_mw: float = 0.0


@dataclass(frozen=True)
class StorageUnitStepResult:
    """Per-unit outcome inside a portfolio step (units are reported in
    storage_id order, never input order)."""

    storage_id: str
    step: StorageStepResult


@dataclass(frozen=True)
class StoragePortfolioStepResult:
    """One portfolio interval: mode + totals + per-unit steps."""

    mode: str  # "charge" | "discharge" | "idle"
    surplus_mw: float
    deficit_mw: float
    total_charge_mw: float
    total_discharge_mw: float
    units: tuple[StorageUnitStepResult, ...]
    diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode not in ("charge", "discharge", "idle"):
            raise StorageError("STORAGE_INVALID_MODE", f"unknown portfolio mode {self.mode!r}")
        for code in self.diagnostics:
            try:
                diagnostic_spec(code)
            except ElccDiagnosticsError as exc:
                raise StorageError(
                    "STORAGE_UNKNOWN_DIAGNOSTIC",
                    f"diagnostic {code!r} is not in the ELCC registry",
                ) from exc


def _normalize_power_value(value: object, label: str, violations: list[str]) -> float | None:
    """THE single strict numeric gate (third-review): every balance /
    total / per-unit power field of a portfolio result passes through
    here. Accepts Python and NumPy real ints and floats; explicitly
    rejects bool, strings and other pseudo-numbers, NaN, +-inf and
    negatives. Returns a normalized float or None (violation recorded).
    No native exception can escape into arithmetic."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        violations.append(f"{label} must be a real number, got {value!r}")
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        violations.append(f"{label} must be a real number, got {value!r}")
        return None
    if not math.isfinite(numeric):
        violations.append(f"{label} must be finite, got {value!r}")
        return None
    if numeric < 0.0:
        violations.append(f"{label} must be non-negative, got {value!r}")
        return None
    return numeric


def _check_portfolio_mode(
    result: StoragePortfolioStepResult,
    result_surplus: float,
    result_deficit: float,
    violations: list[str],
) -> None:
    """Exact mode <-> raw balance pairing (second-review C): charge mode
    requires surplus > 0 AND deficit = 0; discharge the mirror; idle
    requires both zero. surplus and deficit must never both exceed the
    floating tolerance."""
    if result_surplus > _POWER_TOLERANCE_MW and result_deficit > _POWER_TOLERANCE_MW:
        violations.append("surplus and deficit must not both be positive")
        return
    if result.mode == "charge":
        if not (result_surplus > _POWER_TOLERANCE_MW and result_deficit <= _POWER_TOLERANCE_MW):
            violations.append("charge mode requires surplus > 0 and deficit = 0")
    elif result.mode == "discharge":
        if not (result_deficit > _POWER_TOLERANCE_MW and result_surplus <= _POWER_TOLERANCE_MW):
            violations.append("discharge mode requires deficit > 0 and surplus = 0")
    elif result_surplus > _POWER_TOLERANCE_MW or result_deficit > _POWER_TOLERANCE_MW:
        violations.append("idle mode requires both surplus and deficit to be zero")


def _check_portfolio_balance_agreement(
    param_surplus: float | None,
    param_deficit: float | None,
    result_surplus: float | None,
    result_deficit: float | None,
    result: StoragePortfolioStepResult,
    violations: list[str],
) -> None:
    """Surplus/deficit parameter vs result consistency (C901 split): all
    inputs are ALREADY normalized - agreement and mode pairing run only
    when every value passed the strict gate."""
    if (
        param_surplus is None
        or param_deficit is None
        or result_surplus is None
        or result_deficit is None
    ):
        return
    if abs(result_surplus - param_surplus) > _POWER_TOLERANCE_MW:
        violations.append(
            f"result surplus {result_surplus} disagrees with the check parameter {param_surplus}"
        )
    if abs(result_deficit - param_deficit) > _POWER_TOLERANCE_MW:
        violations.append(
            f"result deficit {result_deficit} disagrees with the check parameter {param_deficit}"
        )
    _check_portfolio_mode(result, result_surplus, result_deficit, violations)


def _normalize_portfolio_units(
    result: StoragePortfolioStepResult, violations: list[str]
) -> tuple[list[float] | None, list[float] | None]:
    """Strict per-unit normalization (C901 split): returns normalized
    per-unit lists only when EVERY value passed the gate; any invalid
    value records its violation and poisons the result so downstream
    sums and comparisons are skipped (never executed on raw fields)."""
    charges: list[float] = []
    discharges: list[float] = []
    all_valid = True
    for entry in result.units:
        charge = _normalize_power_value(
            entry.step.actual_charge_mw,
            f"actual_charge_mw({entry.storage_id})",
            violations,
        )
        discharge = _normalize_power_value(
            entry.step.actual_discharge_mw,
            f"actual_discharge_mw({entry.storage_id})",
            violations,
        )
        if charge is None or discharge is None:
            all_valid = False
            continue
        if charge > _POWER_TOLERANCE_MW and discharge > _POWER_TOLERANCE_MW:
            violations.append(
                f"resource {entry.storage_id!r} charges and discharges in the same interval"
            )
        charges.append(charge)
        discharges.append(discharge)
    if not all_valid:
        return None, None
    return charges, discharges


def _check_portfolio_totals(
    result: StoragePortfolioStepResult,
    param_surplus: float | None,
    param_deficit: float | None,
    unit_charges: list[float] | None,
    unit_discharges: list[float] | None,
    violations: list[str],
) -> None:
    """Totals recomputed from NORMALIZED per-unit steps (C901 split):
    caller-provided summary fields are never trusted, and every
    comparison uses only normalized values."""
    total_charge = _normalize_power_value(result.total_charge_mw, "total_charge_mw", violations)
    total_discharge = _normalize_power_value(
        result.total_discharge_mw, "total_discharge_mw", violations
    )
    if unit_charges is None or unit_discharges is None:
        return
    recomputed_charge = sum(unit_charges)
    recomputed_discharge = sum(unit_discharges)
    if total_charge is not None and abs(recomputed_charge - total_charge) > _POWER_TOLERANCE_MW:
        violations.append(
            f"total_charge_mw {total_charge} disagrees with the per-unit sum {recomputed_charge}"
        )
    if (
        total_discharge is not None
        and abs(recomputed_discharge - total_discharge) > _POWER_TOLERANCE_MW
    ):
        violations.append(
            f"total_discharge_mw {total_discharge} disagrees with "
            f"the per-unit sum {recomputed_discharge}"
        )
    if param_surplus is not None and recomputed_charge > param_surplus + _POWER_TOLERANCE_MW:
        violations.append(f"total charge {recomputed_charge} exceeds raw surplus {param_surplus}")
    if param_deficit is not None and recomputed_discharge > param_deficit + _POWER_TOLERANCE_MW:
        violations.append(
            f"total discharge {recomputed_discharge} exceeds raw deficit {param_deficit}"
        )


def _check_portfolio_directions(
    result: StoragePortfolioStepResult,
    unit_charges: list[float] | None,
    unit_discharges: list[float] | None,
    violations: list[str],
) -> None:
    """Mode-direction checks on NORMALIZED per-unit values (C901 split);
    skipped entirely when any per-unit value failed the gate."""
    if unit_charges is None or unit_discharges is None:
        return
    if result.mode == "charge":
        if any(discharge > _POWER_TOLERANCE_MW for discharge in unit_discharges):
            violations.append("discharge in a charge-only (surplus) interval")
    elif result.mode == "discharge":
        if any(charge > _POWER_TOLERANCE_MW for charge in unit_charges):
            violations.append("charge in a discharge-only (deficit) interval")
    elif any(
        charge > _POWER_TOLERANCE_MW or discharge > _POWER_TOLERANCE_MW
        for charge, discharge in zip(unit_charges, unit_discharges)
    ):
        violations.append("storage activity in a balanced (idle) interval")


def check_portfolio_step_invariants(
    result: StoragePortfolioStepResult,
    *,
    surplus_mw: float,
    deficit_mw: float,
) -> None:
    """Defensive validator over the FINAL allocation (third-review
    strict numeric defenses): the caller-provided summary fields are
    NEVER trusted - totals are recomputed from ``result.units`` and
    EVERY balance/total/per-unit power field passes ONE strict numeric
    gate (real ints/floats only; bool, strings and other pseudo-
    numbers, NaN, +-inf and negatives are violations). Comparisons,
    sums and mode checks use ONLY normalized values - an invalid field
    records its violation and skips every dependent check, so a
    corrupted result either passes legally or raises exactly one
    StorageError with the blocking code STORAGE_CROSS_CHARGING_ATTEMPT
    (method_error). Native TypeError / ValueError / OverflowError can
    never leak. Normal requests suppressed by the portfolio mode are
    constraint enforcement and must NEVER be reported as attempts."""
    violations: list[str] = []
    param_surplus = _normalize_power_value(surplus_mw, "surplus_mw", violations)
    param_deficit = _normalize_power_value(deficit_mw, "deficit_mw", violations)
    result_surplus = _normalize_power_value(result.surplus_mw, "result.surplus_mw", violations)
    result_deficit = _normalize_power_value(result.deficit_mw, "result.deficit_mw", violations)
    _check_portfolio_balance_agreement(
        param_surplus, param_deficit, result_surplus, result_deficit, result, violations
    )
    unit_charges, unit_discharges = _normalize_portfolio_units(result, violations)
    _check_portfolio_totals(
        result, param_surplus, param_deficit, unit_charges, unit_discharges, violations
    )
    _check_portfolio_directions(result, unit_charges, unit_discharges, violations)
    if violations:
        raise StorageError(
            "STORAGE_CROSS_CHARGING_ATTEMPT",
            "portfolio allocation violates the cross-charging / mode "
            "invariants: " + "; ".join(violations),
        )


def _charge_priority_key(unit: PortfolioUnit) -> tuple[float, str]:
    """Methodology 6.5 explicit sort key: per unit of system-side energy,
    a higher charge efficiency forms more usable SOC; ties broken by
    storage_id ascending (never input / insertion / set order)."""
    return (-unit.spec.charge_efficiency, unit.spec.storage_id)


def _discharge_priority_key(unit: PortfolioUnit) -> tuple[float, str]:
    """Methodology 6.5 explicit sort key: per unit of SOC drawn, a higher
    discharge efficiency delivers more system energy; ties broken by
    storage_id ascending."""
    return (-unit.spec.discharge_efficiency, unit.spec.storage_id)


def _validate_portfolio_units(units: list[PortfolioUnit]) -> None:
    """Portfolio input validation (C901 split): unique storage ids and
    strictly numeric requests. Per-unit requests in BOTH directions are
    legal strategy inputs here - the portfolio mode is the authority and
    suppresses the conflicting direction (BLOCKED). The single-resource
    step still rejects a simultaneous charge+discharge instruction with
    a stable code (S-07)."""
    seen: set[str] = set()
    for unit in units:
        if unit.spec.storage_id in seen:
            raise StorageError(
                "STORAGE_DUPLICATE_UNIT",
                f"duplicate storage_id {unit.spec.storage_id!r} in one portfolio step",
            )
        seen.add(unit.spec.storage_id)
        _finite_number(unit.requested_charge_mw, "STORAGE_INVALID_REQUEST", allow_negative=False)
        _finite_number(unit.requested_discharge_mw, "STORAGE_INVALID_REQUEST", allow_negative=False)


def _portfolio_unit_step(
    unit: PortfolioUnit,
    *,
    mode: str,
    budget: float,
    delta_t_hours: float,
    diagnostics: set[str],
    reserve_energy_mwh: float | None = None,
) -> tuple[StorageStepResult, float]:
    """One unit's allocation inside the enforced portfolio mode (C901
    split); returns the step and the remaining mode budget."""
    if mode == "charge":
        if unit.requested_discharge_mw > _POWER_TOLERANCE_MW:
            diagnostics.add("STORAGE_CROSS_CHARGING_BLOCKED")
        allowed = min(max(0.0, unit.requested_charge_mw), budget)
        step = step_storage(
            unit.spec,
            unit.state,
            unit.limits,
            requested_charge_mw=allowed,
            requested_discharge_mw=0.0,
            delta_t_hours=delta_t_hours,
        )
        return step, budget - step.actual_charge_mw
    if mode == "discharge":
        if unit.requested_charge_mw > _POWER_TOLERANCE_MW:
            diagnostics.add("STORAGE_CROSS_CHARGING_BLOCKED")
        allowed = min(max(0.0, unit.requested_discharge_mw), budget)
        # B6 (方法说明第三章(一) 周期末回充约束): a caller-declared
        # discharge-side floor clips the REQUEST so the SOC after the
        # step stays at/above the reserve line. Pure constraint - the
        # energy to hold the floor is never invented; the T13 physics
        # (efficiency, self-discharge, SOC band) still applies and may
        # clip further.
        if reserve_energy_mwh is not None and reserve_energy_mwh > unit.state.soc_mwh:
            allowed = 0.0
        elif reserve_energy_mwh is not None:
            headroom_mw = (
                (unit.state.soc_mwh - reserve_energy_mwh)
                * unit.spec.discharge_efficiency
                / delta_t_hours
            )
            allowed = min(allowed, max(0.0, headroom_mw))
        step = step_storage(
            unit.spec,
            unit.state,
            unit.limits,
            requested_charge_mw=0.0,
            requested_discharge_mw=allowed,
            delta_t_hours=delta_t_hours,
        )
        return step, budget - step.actual_discharge_mw
    if (
        unit.requested_charge_mw > _POWER_TOLERANCE_MW
        or unit.requested_discharge_mw > _POWER_TOLERANCE_MW
    ):
        diagnostics.add("STORAGE_CROSS_CHARGING_BLOCKED")
    step = step_storage(
        unit.spec,
        unit.state,
        unit.limits,
        requested_charge_mw=0.0,
        requested_discharge_mw=0.0,
        delta_t_hours=delta_t_hours,
    )
    return step, budget


def step_storage_portfolio(
    units: list[PortfolioUnit],
    *,
    pre_storage_supply_mw: float,
    load_mw: float,
    delta_t_hours: float = 1.0,
    discharge_reserve_mwh: Mapping[str, float] | None = None,
) -> StoragePortfolioStepResult:
    """One portfolio interval under the enforced portfolio mode.

    The mode is decided FIRST from the raw pre-storage surplus/deficit
    (methodology 6.1): charge-only, discharge-only or idle. Conflicting
    per-unit requests are suppressed (STORAGE_CROSS_CHARGING_BLOCKED,
    info). Same-direction allocation follows the explicit 6.5 sort key
    and the raw surplus/deficit budget; ordering only ever affects
    allocation BETWEEN same-direction resources.

    B6 (方法说明第三章(一) 周期末回充约束): ``discharge_reserve_mwh``
    maps storage_id -> an energy floor the unit must KEEP during this
    step (used by the caller inside the end-of-cycle recharge window).
    It only clips discharge requests - no charging is ever fabricated.
    Units not named in the mapping keep the legacy SOC-floor behavior."""
    supply = _finite_number(pre_storage_supply_mw, "STORAGE_INVALID_PORTFOLIO")
    load = _finite_number(load_mw, "STORAGE_INVALID_PORTFOLIO", allow_negative=False)
    surplus = max(0.0, supply - load)
    deficit = max(0.0, load - supply)
    if surplus > _POWER_TOLERANCE_MW:
        mode = "charge"
    elif deficit > _POWER_TOLERANCE_MW:
        mode = "discharge"
    else:
        mode = "idle"

    _validate_portfolio_units(units)

    key = _charge_priority_key if mode == "charge" else _discharge_priority_key
    ordered = sorted(units, key=key)
    diagnostics: set[str] = set()
    budget = surplus if mode == "charge" else deficit
    outcomes: dict[str, StorageStepResult] = {}

    for unit in ordered:
        reserve: float | None = None
        if discharge_reserve_mwh is not None and unit.spec.storage_id in discharge_reserve_mwh:
            raw = float(discharge_reserve_mwh[unit.spec.storage_id])
            # normalize into the physical band; below energy_min_mwh the
            # legacy SOC floor already dominates (degrade, never error)
            reserve = max(
                raw,
                unit.spec.energy_min_mwh,
            )
            reserve = min(reserve, unit.spec.energy_max_mwh)
        step, budget = _portfolio_unit_step(
            unit,
            mode=mode,
            budget=budget,
            delta_t_hours=delta_t_hours,
            diagnostics=diagnostics,
            reserve_energy_mwh=reserve,
        )
        outcomes[unit.spec.storage_id] = step

    unit_results = tuple(
        StorageUnitStepResult(storage_id=storage_id, step=outcomes[storage_id])
        for storage_id in sorted(outcomes)
    )
    total_charge = sum(entry.step.actual_charge_mw for entry in unit_results)
    total_discharge = sum(entry.step.actual_discharge_mw for entry in unit_results)
    result = StoragePortfolioStepResult(
        mode=mode,
        surplus_mw=surplus,
        deficit_mw=deficit,
        total_charge_mw=total_charge,
        total_discharge_mw=total_discharge,
        units=unit_results,
        diagnostics=tuple(sorted(diagnostics)),
    )
    check_portfolio_step_invariants(result, surplus_mw=surplus, deficit_mw=deficit)
    return result


__all__ = [
    "PortfolioUnit",
    "StorageError",
    "StorageOperatingLimits",
    "StoragePortfolioStepResult",
    "StorageSpec",
    "StorageState",
    "StorageStepResult",
    "StorageUnitStepResult",
    "check_portfolio_step_invariants",
    "effective_soc_band",
    "step_storage",
    "step_storage_portfolio",
]
