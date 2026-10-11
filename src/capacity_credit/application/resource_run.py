"""T16 P3 constrained-resource application layer.

``run_resource_pipeline`` turns standard tables + a frozen ScenarioBank
into one runnable per-sample resource-combination evidence set: thermal
(stochastic + DQ-27 derating decision), renewables, budget-constrained
hydro (per-resource budgets owned by hydro_id + scenario_id), validated
intertie local support aggregated per channel (candidate extra import
kept in a SEPARATE channel), and storage dispatch with warm-up /
burn-in / multi-year chronology. Risk metrics and A-11 deficit events
are computed through the SAME engine primitives as the adequacy engine
(balance/metrics/events), so a baseline or a storage-augmented
candidate composition runs end to end and the result is directly usable
as an ELCC/adequacy input.

First-review contract decisions (all tested):

- SIGN / UNITS: every hourly vector is system-side MW at the hour
  start; supply = thermal_frac*capacity*derating + renewable + hydro +
  intertie_local(import - export, ACCUMULATED per hour across channels)
  + storage_net(discharge - charge). Candidate extra import NEVER
  enters the baseline supply - reported separately, applied only via
  ``evaluate_candidate``.
- CHRONOLOGY: one sample dispatches ALL its years in ONE
  ``run_multi_year_dispatch`` call (continuous_multiyear carries SOC
  across years; independent years re-initialize per year). Never a
  per-year loop over multi-year dispatch.
- STRATEGY: eue_greedy is the production default; risk_window actually
  trains on baseline-only pre-storage and dispatches through the
  windowed rule; perfect_foresight actually computes the diagnostic
  upper bound, publishes STORAGE_PERFECT_FORESIGHT and keeps the
  greedy trajectory as the scored production result; unknown strategies
  block with a stable code.
- EXOGENOUS INVARIANTS: intertie plans, hydro budgets, planned outages
  and storage streams are computed ONCE per pipeline call; the load
  axis never regenerates them and never enters the resource-run
  identity.
- DQ-24 / DQ-27 / DQ-30 are consumed BEFORE any dispatch: blocking
  errors raise ``ResourceRunError`` (no silent continuation), warnings
  are published through the shared ELCC diagnostic registry.
- CONFIG-BANK BINDING: scenario_bank_id / weather_years / samples /
  timezone / root_seed / year_roles / year_weights / primary metric /
  delta_t are validated against the opened bank before any dispatch.
- EVENTS: renewable_output_mw carries wind/PV ONLY; hydro context is a
  separate controlled field; cross-year merge preserves the FIRST
  segment's pre-event SOC and duration-weights the mean context.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from capacity_credit.application.resource_run_inputs import (
    DEFAULT_DELTA_T_HOURS,
    ResourceRunConfig,
    ResourceRunDiagnostic,
    ResourceRunError,
    align_to_canonical_axis,
    canonical_axes_from_load,
    canonical_index_by_timestamp,
    check_snapshot_structure,
    merge_published_diagnostics,
    merge_resource_run_diagnostics,
    resolve_scenario,
    select_runtime_snapshot,
    validate_resource_run_config,
    validate_resource_run_inputs,
)
from capacity_credit.contracts.enums import (
    ChronologyMode,
    RiskMetric,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.storage_soc_config import StorageSocConfig
from capacity_credit.data.validation import validate_outage_derating_consistency
from capacity_credit.engine.balance import compute_margin
from capacity_credit.engine.events import (
    DeficitEvent,
    EventContext,
    EventContextSnapshot,
    detect_deficit_events,
)
from capacity_credit.engine.metrics import (
    RiskMetricsSample,
    compute_sample_metrics,
)
from capacity_credit.resources.hydro import (
    HydroEnergyBudget,
    HydroError,
    build_hydro_fixed_profile,
    check_hydro_constraints,
)
from capacity_credit.resources.intertie import (
    IntertieHourlyRecord,
    IntertieSpec,
    compute_intertie_plan_identity,
    compute_intertie_spec_identity,
    validate_intertie_plan_by_channel,
)
from capacity_credit.resources.renewable import build_renewable_series
from capacity_credit.resources.storage import (
    PortfolioUnit,
    StorageOperatingLimits,
    StorageSpec,
    StorageState,
)
from capacity_credit.resources.storage_chronology import run_multi_year_dispatch
from capacity_credit.resources.storage_dispatch import (
    DispatchHorizon,
    DispatchOutcome,
    PerfectForesightBound,
    RiskWindowRule,
    run_eue_greedy,
    run_perfect_foresight_eue,
    run_risk_window,
    train_risk_window,
)
from capacity_credit.scenarios.scenario_bank import ScenarioBank
from capacity_credit.scenarios.thermal_states import (
    ThermalStateError,
    derive_outage_derating,
)


@dataclass(frozen=True)
class ResourceRunIdentity:
    """Deterministic identity of the exogenous resource run.

    LOAD is deliberately excluded: a load trial must never regenerate
    or re-identify the exogenous resource plan."""

    fingerprint: str
    components: dict[str, str]


@dataclass(frozen=True)
class StorageSampleEvidence:
    """Per-(year, sample) storage evidence (scoring period only)."""

    year: int
    sample_id: int
    dispatch: DispatchOutcome
    initial_state_policy: str
    warmup_diagnostics: tuple[str, ...] = ()
    burnin_length_hours: int = 0


@dataclass(frozen=True)
class ResourceRunEvidence:
    """Full audited outcome of one resource run."""

    config: ResourceRunConfig
    scenario_id: str
    years: tuple[int, ...]
    load_by_year: dict[int, np.ndarray]
    local_dates_by_year: dict[int, np.ndarray]
    renewable_by_year: dict[int, np.ndarray]
    hydro_by_year: dict[int, np.ndarray]
    intertie_local_by_year: dict[int, np.ndarray]
    candidate_extra_import_by_year: dict[int, np.ndarray]
    post_storage_supply_by_sample: dict[tuple[int, int], np.ndarray]
    storage_net_by_year: dict[int, np.ndarray]
    derating_by_unit: dict[str, float]
    risk_metrics: dict[str, float]
    deficit_events: tuple[DeficitEvent, ...]
    storage_evidence: tuple[StorageSampleEvidence, ...]
    event_context_by_sample: dict[tuple[int, int], EventContext]
    perfect_foresight_bounds: dict[tuple[int, int], PerfectForesightBound]
    risk_window_rules: dict[int, RiskWindowRule]
    identity: ResourceRunIdentity
    diagnostics: tuple[ResourceRunDiagnostic, ...]
    # W1/MR-04: ordered emergency layer evidence (empty tuple = absent
    # table = the legacy no-relief behavior; means are cross-sample)
    emergency_programs: tuple[EmergencyProgramSpec, ...] = ()
    emergency_relief_mean_by_year: dict[int, np.ndarray] = field(default_factory=dict)
    emergency_served_mwh_mean_by_year: dict[int, float] = field(default_factory=dict)

    @property
    def standard_diagnostics(self) -> tuple[ResourceRunDiagnostic, ...]:
        """Derived view: standard-source diagnostics (never a second
        stored copy of the unified list)."""
        return tuple(d for d in self.diagnostics if d.source == "standard")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# per-resource builders
# ----------------------------------------------------------------------


def _renewable_by_year(
    tables: dict[str, pd.DataFrame],
    axes: dict[int, tuple[pd.Series, np.ndarray]],
    timezone: str,
    scenario_id: str,
) -> dict[int, np.ndarray]:
    capacities = tables["renewable_capacities"]
    frame = tables["renewable_hourly"]
    frame = frame[frame["scenario_id"].astype(str) == scenario_id]
    groups = sorted(capacities["resource_group_id"].astype(str).unique())
    by_year: dict[int, np.ndarray] = {}
    for year in axes:
        total = np.zeros(len(axes[year][0]), dtype=np.float64)
        for group in groups:
            stamps = pd.to_datetime(frame["timestamp"])
            group_frame = frame[
                (frame["resource_group_id"].astype(str) == group) & (stamps.dt.year == year)
            ]
            series = build_renewable_series(group_frame, timezone=timezone, load_axis=axes[year][0])
            total += np.asarray(series, dtype=np.float64)
        by_year[year] = total
    return by_year


def _hydro_budgets_for_unit(
    tables: dict[str, pd.DataFrame],
    hydro_id: str,
    scenario_id: str,
) -> tuple[HydroEnergyBudget, ...]:
    if "hydro_energy_budgets" not in tables:
        return ()
    frame = tables["hydro_energy_budgets"]
    owned = frame[
        (frame["hydro_id"].astype(str) == hydro_id)
        & (frame["scenario_id"].astype(str) == scenario_id)
    ]
    budgets: list[HydroEnergyBudget] = []
    for _, row in owned.iterrows():
        budgets.append(
            HydroEnergyBudget(
                period_start=pd.to_datetime(row["period_start"]).to_pydatetime(),
                period_end=pd.to_datetime(row["period_end"]).to_pydatetime(),
                energy_budget_mwh=float(row["energy_budget_mwh"]),
            )
        )
    return tuple(budgets)


def _hydro_by_year(
    tables: dict[str, pd.DataFrame],
    axes: dict[int, tuple[pd.Series, np.ndarray]],
    config: ResourceRunConfig,
) -> tuple[dict[int, np.ndarray], dict[str, str]]:
    by_year: dict[int, np.ndarray] = {
        year: np.zeros(len(axes[year][0]), dtype=np.float64) for year in axes
    }
    identities: dict[str, str] = {}
    if "hydro_hourly" not in tables or tables["hydro_hourly"].empty:
        return by_year, identities
    hourly = tables["hydro_hourly"]
    capacity_by_id: dict[str, float] = {}
    if "hydro_units" in tables:
        for _, row in tables["hydro_units"].iterrows():
            capacity_by_id[str(row["hydro_id"])] = float(row["capacity_mw"])
    scenario_id = config.scenario_id if config.scenario_id is not None else ""
    for hydro_id in sorted(hourly["hydro_id"].astype(str).unique()):
        group = hourly[hourly["hydro_id"].astype(str) == hydro_id]
        scenario_frame = group[group["scenario_id"].astype(str) == scenario_id]
        if scenario_frame.empty:
            continue
        budgets = _hydro_budgets_for_unit(tables, hydro_id, scenario_id)
        for year in axes:
            total, identity = _process_hydro_unit_year(
                scenario_frame=scenario_frame,
                hydro_id=hydro_id,
                scenario_id=scenario_id,
                capacity_mw=capacity_by_id.get(hydro_id),
                budgets=budgets,
                config=config,
                year=year,
                axis_index=canonical_index_by_timestamp(axes, year),
            )
            by_year[year] += total
            if identity is not None:
                identities[f"{hydro_id}:{scenario_id}"] = identity
    return by_year, identities


def _process_hydro_unit_year(
    *,
    scenario_frame: pd.DataFrame,
    hydro_id: str,
    scenario_id: str,
    capacity_mw: float | None,
    budgets: tuple[HydroEnergyBudget, ...],
    config: ResourceRunConfig,
    year: int,
    axis_index: dict[pd.Timestamp, int],
) -> tuple[np.ndarray, str | None]:
    """One (hydro_id, scenario_id, year) constrained profile (C901
    split). Aligns by TIMESTAMP to the standard hourly axis; fixed
    output can never exceed the declared capacity; budgets are owned by
    the (hydro_id, scenario_id) pair and enter the identity with their
    full values."""
    stamps = pd.to_datetime(scenario_frame["timestamp"])
    year_frame = scenario_frame[stamps.dt.year == year].copy()
    if year_frame.empty:
        return np.zeros(0, dtype=np.float64), None
    hours = len(axis_index)
    aligned = align_to_canonical_axis(
        year_frame["timestamp"],
        year_frame["fixed_output_mw"].astype(float).to_numpy(),
        axis_index,
        hours,
        f"hydro {hydro_id}",
    )
    fixed = tuple(float(value) for value in aligned)
    time_axis = tuple(
        pd.Timestamp(ts) for ts in sorted(pd.to_datetime(year_frame["timestamp"]), key=lambda t: t)
    )
    if capacity_mw is None:
        raise ResourceRunError(
            "RESOURCE_RUN_HYDRO_UNIT_MISSING",
            f"no hydro_units row for hydro_id={hydro_id}",
            [],
        )
    if any(value > capacity_mw + 1e-9 for value in fixed):
        raise ResourceRunError(
            "HYDRO_FIXED_EXCEEDS_CAPACITY",
            f"hydro {hydro_id} fixed output exceeds its declared "
            f"capacity {capacity_mw} MW (never silently raised)",
            [],
        )
    try:
        profile = build_hydro_fixed_profile(
            hydro_id=hydro_id,
            fixed_output_mw=fixed,
            time_axis=time_axis,
        )
        result = check_hydro_constraints(
            hydro_id=hydro_id,
            fixed_output_mw=profile.fixed_output_mw,
            max_output_mw=tuple(capacity_mw for _ in fixed),
            requested_adjustable_mw=tuple(config.hydro_adjustable_request_mw for _ in fixed),
            time_axis=time_axis,
            budgets=budgets,
            delta_t_hours=config.delta_t_hours,
        )
    except HydroError as error:
        raise ResourceRunError(
            error.code,
            f"hydro {hydro_id} constraint failed: {error.message}",
            [],
        ) from None
    budget_payload = "|".join(
        f"{b.period_start.isoformat()}|{b.period_end.isoformat()}|{b.energy_budget_mwh}"
        for b in sorted(budgets, key=lambda b: b.period_start)
    )
    identity = _sha256(
        f"{hydro_id}|{scenario_id}|{budget_payload}|"
        f"{result.budget_application_rule}|{tuple(fixed)}|"
        f"{capacity_mw}|{config.hydro_adjustable_request_mw}"
    )
    return np.asarray(result.total_output_mw, dtype=np.float64), identity


def _intertie_by_year(
    tables: dict[str, pd.DataFrame],
    axes: dict[int, tuple[pd.Series, np.ndarray]],
    config: ResourceRunConfig,
) -> tuple[
    dict[int, np.ndarray],
    dict[int, np.ndarray],
    str,
    str,
    str,
    tuple[str, ...],
]:
    specs: list[IntertieSpec] = []
    for _, row in tables["interties"].iterrows():
        specs.append(
            IntertieSpec(
                intertie_id=str(row["intertie_id"]),
                rated_capacity_mw=float(row["rated_capacity_mw"]),
                normal_import_limit_mw=float(row["normal_import_limit_mw"]),
                normal_export_limit_mw=float(row["normal_export_limit_mw"]),
            )
        )
    hourly = tables["intertie_hourly"]
    hourly = hourly[hourly["scenario_id"].astype(str) == config.scenario_id]
    records: list[IntertieHourlyRecord] = []
    for _, row in hourly.iterrows():
        support = row.get("external_support_available_mw")
        available_import = row.get("available_import_limit_mw")
        available_export = row.get("available_export_limit_mw")
        records.append(
            IntertieHourlyRecord(
                intertie_id=str(row["intertie_id"]),
                scenario_id=str(row.get("scenario_id", "BASE")),
                timestamp=pd.to_datetime(row["timestamp"]).to_pydatetime(),
                scheduled_import_mw=float(row.get("scheduled_import_mw", 0.0)),
                scheduled_export_mw=float(row.get("scheduled_export_mw", 0.0)),
                available_import_limit_mw=(
                    float(available_import)
                    if available_import is not None and pd.notna(available_import)
                    else None
                ),
                available_export_limit_mw=(
                    float(available_export)
                    if available_export is not None and pd.notna(available_export)
                    else None
                ),
                external_support_available_mw=(
                    float(support) if support is not None and pd.notna(support) else None
                ),
                source_id=str(row.get("source_id", "")),
            )
        )
    validated = validate_intertie_plan_by_channel(
        tuple(records),
        specs=tuple(specs),
        availability_policy=config.intertie_availability_policy,
        emergency_limit_source=config.emergency_limit_source,
    )
    local_by_year: dict[int, np.ndarray] = {}
    extra_by_year: dict[int, np.ndarray] = {}
    for year, (axis, _d) in axes.items():
        hours = len(axis)
        index_by_timestamp = {pd.Timestamp(ts): index for index, ts in enumerate(axis)}
        local = np.zeros(hours, dtype=np.float64)
        extra = np.zeros(hours, dtype=np.float64)
        seen_hours: set[tuple[str, int]] = set()
        for index, record in enumerate(records):
            if record.timestamp.year != year:
                continue
            hour = index_by_timestamp.get(pd.Timestamp(record.timestamp))
            if hour is None:
                raise ResourceRunError(
                    "RESOURCE_RUN_TIME_AXIS_INVALID",
                    "intertie timestamp not on the canonical hourly axis",
                    [],
                )
            if (record.intertie_id, hour) in seen_hours:
                raise ResourceRunError(
                    "RESOURCE_RUN_TIME_AXIS_INVALID",
                    "intertie carries duplicate canonical hours",
                    [],
                )
            seen_hours.add((record.intertie_id, hour))
            # ACCUMULATE per channel: several ties in the same hour must
            # sum their net local supply / candidate extra import
            local[hour] += validated.import_mw[index] - validated.export_mw[index]
            extra[hour] += validated.candidate_extra_import_mw[index]
        local_by_year[year] = local
        extra_by_year[year] = extra
    plan = compute_intertie_plan_identity(
        tuple(records), alignment_rule="hour_of_year", leap_day_rule=""
    )
    spec_identity = compute_intertie_spec_identity(tuple(specs))
    return (
        local_by_year,
        extra_by_year,
        plan.fingerprint,
        plan.support_fingerprint,
        spec_identity,
        tuple(validated.diagnostics),
    )


def _storage_specs_from_tables(tables: dict[str, pd.DataFrame]) -> list[StorageSpec]:
    specs: list[StorageSpec] = []
    if "storage_units" not in tables:
        return specs
    for _, row in tables["storage_units"].iterrows():
        grid_limit = row.get("grid_connection_limit_mw")
        specs.append(
            StorageSpec(
                storage_id=str(row["storage_id"]),
                charge_power_mw=float(row["charge_power_mw"]),
                discharge_power_mw=float(row["discharge_power_mw"]),
                energy_capacity_mwh=float(row["energy_capacity_mwh"]),
                charge_efficiency=float(row.get("charge_efficiency", 1.0)),
                discharge_efficiency=float(row.get("discharge_efficiency", 1.0)),
                soc_min_fraction=float(row.get("soc_min_fraction", 0.0)),
                soc_max_fraction=float(row.get("soc_max_fraction", 1.0)),
                self_discharge_per_hour=float(row.get("self_discharge_per_hour", 0.0)),
                availability_fraction=float(row.get("availability_fraction", 1.0)),
                grid_connection_limit_mw=(
                    float(grid_limit) if grid_limit is not None and pd.notna(grid_limit) else None
                ),
            )
        )
    return specs


def _derating_by_unit(
    tables: dict[str, pd.DataFrame],
    bank: ScenarioBank,
) -> tuple[dict[str, float], tuple[str, ...]]:
    classes = tables["thermal_reliability_classes"]
    mapping = tables["thermal_unit_reliability_map"]
    units = tables["thermal_units"]
    rate_by_class: dict[str, float | None] = {}
    for _, row in classes.iterrows():
        value = row.get("planned_outage_rate")
        rate_by_class[str(row["reliability_class_id"])] = (
            None if value is None or pd.isna(value) else float(value)
        )
    class_by_unit: dict[str, str] = {}
    for _, row in mapping.iterrows():
        class_by_unit.setdefault(str(row["unit_id"]), str(row["reliability_class_id"]))
    outage_by_unit: dict[str, tuple[str, ...]] = {}
    for obj in bank.index["objects"]:
        events = obj.get("planned_outages", ())
        statuses = tuple(
            str(event.get("status", "")) if isinstance(event, dict) else str(event)
            for event in events
        )
        outage_by_unit[obj["object_id"]] = statuses
    derating: dict[str, float] = {}
    diagnostics: list[str] = []
    for _, row in units.iterrows():
        unit_id = str(row["unit_id"])
        class_id = class_by_unit.get(unit_id)
        rate = rate_by_class.get(class_id) if class_id else None
        statuses = outage_by_unit.get(unit_id, ())
        has_rows = bool(statuses)
        auto = any(status == "modelled" for status in statuses)
        try:
            decision = derive_outage_derating(
                planned_outage_rate=rate,
                complete_planned_outages_present=has_rows,
                hourly_capacity_includes_outages=False,
                auto_scheduled_events_present=auto,
            )
        except ThermalStateError:
            issues = validate_outage_derating_consistency(
                planned_outage_rate=rate,
                explicit_outage_rows_present=has_rows,
                auto_scheduled_events_present=auto,
                unit_id=unit_id,
                reliability_class_id=class_id,
            )
            method_diags = merge_resource_run_diagnostics(
                [], sorted({issue.code for issue in issues})
            )
            raise ResourceRunError(
                "RESOURCE_RUN_BLOCKED",
                f"DQ-27 runtime gate failed for unit {unit_id}",
                method_diags,
            )
        if decision.apply_rate:
            derating[unit_id] = 1.0 - decision.rate
        else:
            derating[unit_id] = 1.0
            if rate is not None and has_rows:
                diagnostics.append("DQ27_RATE_CONSISTENCY_ONLY")
    return derating, tuple(sorted(set(diagnostics)))


# ----------------------------------------------------------------------
# identity
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class EmergencyProgramSpec:
    """W1/MR-04: one ordered emergency program parsed from the frozen
    台帐 (方法说明第二章(二)4). Row order in the table IS the dispatch
    order; storage deliberately never appears here (it dispatches first
    via the storage fleet)."""

    program_id: str
    kind: str
    capacity_mw: float
    max_duration_hours: int
    max_events_per_year: int
    energy_limit_mwh: float | None
    availability_fraction: float = 1.0


def parse_emergency_programs(
    tables: dict[str, pd.DataFrame],
) -> tuple[EmergencyProgramSpec, ...]:
    """Parse the frozen `emergency_programs` table (row order preserved).
    Absent or empty table -> an EMPTY tuple = no emergency layer = the
    legacy behavior bit-for-bit. Validation lives in the standard layer
    (DQ34); this parser trusts the frozen contract and refuses
    structurally unusable rows defensively."""
    frame = tables.get("emergency_programs")
    if frame is None or frame.empty:
        return ()
    programs: list[EmergencyProgramSpec] = []
    for _, row in frame.iterrows():
        energy_raw = row.get("energy_limit_mwh_per_year")
        energy = None if energy_raw is None or (isinstance(energy_raw, float) and math.isnan(float(energy_raw))) else float(energy_raw)
        availability = row.get("availability_fraction", 1.0)
        availability = 1.0 if availability is None else float(availability)
        programs.append(
            EmergencyProgramSpec(
                program_id=str(row["program_id"]),
                kind=str(row.get("kind", "")),
                capacity_mw=float(row["available_capacity_mw"]),
                max_duration_hours=int(row["max_duration_hours_per_event"]),
                max_events_per_year=int(row["max_events_per_year"]),
                energy_limit_mwh=energy,
                availability_fraction=availability,
            )
        )
    return tuple(programs)


def apply_emergency_programs(
    gap_mw: "np.ndarray",
    programs: tuple[EmergencyProgramSpec, ...],
) -> tuple["np.ndarray", float]:
    """W1/MR-04: the ordered emergency dispatch state machine (逐时序贯).

    For every deficit hour the programs fill the gap in TABLE ORDER,
    each constrained by its per-event duration (a continuous stretch of
    deployment), its annual event count and its optional annual energy
    cap; state advances HOURLY and a gap-free hour ends every open
    event. Returns (relief_mw, served_mwh): `relief_mw` is ADDED to the
    post-storage supply by the caller - the T06 metrics then run
    unchanged, and served + unserved == the pre-relief unserved energy
    (energy conservation; nothing is invented)."""
    relief = np.zeros(len(gap_mw), dtype=np.float64)
    served = 0.0
    hours_in_event = [0] * len(programs)
    events_used = [0] * len(programs)
    energy_used = [0.0] * len(programs)
    active = [False] * len(programs)
    for index in range(len(gap_mw)):
        remaining = float(gap_mw[index])
        if remaining <= 1e-9:
            for position in range(len(programs)):
                active[position] = False
                hours_in_event[position] = 0
            continue
        for position, program in enumerate(programs):
            if remaining <= 1e-9:
                break
            energy_left = (
                math.inf
                if program.energy_limit_mwh is None
                else program.energy_limit_mwh - energy_used[position]
            )
            if energy_left <= 1e-9:
                continue
            if active[position]:
                if hours_in_event[position] >= program.max_duration_hours:
                    active[position] = False
                    hours_in_event[position] = 0
                    continue
            else:
                if events_used[position] >= program.max_events_per_year:
                    continue
                events_used[position] += 1
                active[position] = True
                hours_in_event[position] = 0
            cap = program.capacity_mw * program.availability_fraction
            take = min(cap, remaining, energy_left)
            relief[index] += take
            remaining -= take
            energy_used[position] += take
            hours_in_event[position] += 1
            served += take
    return relief, served


def _data_identity(tables: dict[str, pd.DataFrame]) -> str:
    parts: list[str] = []
    for name in sorted(tables):
        if name == "load_hourly":
            continue  # load is a trial input, never part of the plan
        frame = tables[name]
        header = "|".join(str(c) for c in frame.columns)
        rows = sorted(str(row) for row in frame.astype(str).values.tolist())
        parts.append(f"{name}:{_sha256(header + '#' + '\n'.join(rows))}")
    return _sha256("|".join(parts))


def build_resource_run_identity(
    *,
    bank: ScenarioBank,
    tables: dict[str, pd.DataFrame],
    config: ResourceRunConfig,
    intertie_plan_fingerprint: str,
    intertie_support_fingerprint: str,
    intertie_spec_identity: str,
    hydro_identities: dict[str, str],
    storage_specs: list[StorageSpec],
    risk_window_rules: dict[int, RiskWindowRule],
) -> ResourceRunIdentity:
    storage_payload = "|".join(
        f"{spec.storage_id}|{spec.charge_power_mw}|{spec.discharge_power_mw}|"
        f"{spec.energy_capacity_mwh}|{spec.charge_efficiency}|"
        f"{spec.discharge_efficiency}|{spec.soc_min_fraction}|"
        f"{spec.soc_max_fraction}|{spec.self_discharge_per_hour}|"
        f"{spec.availability_fraction}|{spec.grid_connection_limit_mw}"
        for spec in sorted(storage_specs, key=lambda s: s.storage_id)
    )
    soc_payload = config.storage_soc.model_dump_json() if config.storage_soc else "none"
    method_payload = (
        f"{config.primary_risk_metric}|{config.declared_grade}|"
        f"{config.dispatch_strategy}|{config.risk_window_length_hours}|"
        f"{config.risk_threshold}|{config.reserve_soc_fraction}|"
        f"{config.intertie_availability_policy}|{config.emergency_limit_source}|"
        f"{config.hydro_adjustable_request_mw}|{config.perfect_capacity_mw}|"
        f"{config.auto_schedule_planned_outages}|"
        f"{config.merge_events_across_years}|{config.delta_t_hours}|"
        f"{config.chronology_mode.value}|{config.soc_year_boundary_mode.value}|"
        f"{config.root_seed}|{config.emergency_layer}"
    )
    hydro_payload = "|".join(f"{key}:{fp}" for key, fp in sorted(hydro_identities.items()))
    risk_rule_version_payload = "|".join(
        f"{year}:{rule.window_version}" for year, rule in sorted(risk_window_rules.items())
    )
    components = {
        "data_resources": _data_identity(tables),
        "scenario_bank": bank.bank_id,
        "scenario_id": config.scenario_id or "",
        "weather_years": ",".join(str(y) for y in config.weather_years),
        "year_roles": ",".join(f"{y}={r}" for y, r in sorted(config.year_roles.items())),
        "year_weights": ",".join(
            f"{y}={w}" for y, w in sorted((config.year_weights or {}).items())
        ),
        "samples": str(config.samples),
        "risk_window_rule": risk_rule_version_payload if risk_window_rules else "not_applied",
        "intertie_plan": intertie_plan_fingerprint,
        "intertie_support": intertie_support_fingerprint,
        "intertie_spec": intertie_spec_identity,
        "hydro": hydro_payload,
        "storage": _sha256(storage_payload),
        "storage_soc_config": _sha256(soc_payload),
        "dispatch_strategy": config.dispatch_strategy,
        "method_config": _sha256(method_payload),
        "random_seed": str(config.root_seed),
    }
    fingerprint = _sha256("|".join(f"{key}={value}" for key, value in sorted(components.items())))
    return ResourceRunIdentity(fingerprint=fingerprint, components=dict(components))


# ----------------------------------------------------------------------
# storage dispatch routing
# ----------------------------------------------------------------------


def _make_dispatcher(
    config: ResourceRunConfig,
    holder: list[PerfectForesightBound],
    risk_rules: dict[int, RiskWindowRule],
) -> (
    Callable[
        [list[PortfolioUnit], DispatchHorizon, dict[str, StorageState], int],
        DispatchOutcome,
    ]
    | None
):
    """Strategy routing (first review): eue_greedy stays the default
    production path; risk_window actually trains on baseline-only
    pre-storage and dispatches through the windowed rule;
    perfect_foresight actually computes the diagnostic upper bound,
    records it in ``holder`` and still returns the greedy production
    trajectory."""
    if config.dispatch_strategy == "risk_window":

        def _risk_window_dispatcher(
            units: list[PortfolioUnit],
            horizon: DispatchHorizon,
            initial_states: dict[str, StorageState],
            year: int,
        ) -> DispatchOutcome:
            rule = risk_rules[year]  # frozen BEFORE candidate evaluation
            return run_risk_window(units, horizon, rule, initial_states=initial_states)

        return _risk_window_dispatcher

    if config.dispatch_strategy == "perfect_foresight":

        def _foresight_dispatcher(
            units: list[PortfolioUnit],
            horizon: DispatchHorizon,
            initial_states: dict[str, StorageState],
            year: int,
        ) -> DispatchOutcome:
            bound = run_perfect_foresight_eue(units, horizon, initial_states=initial_states)
            holder.append(bound)
            return run_eue_greedy(units, horizon, initial_states=initial_states)

        return _foresight_dispatcher

    return None  # eue_greedy: the chronology default path


# ----------------------------------------------------------------------
# per-sample processing (one multi-year chronology call per sample)
# ----------------------------------------------------------------------


def _apply_perfect_capacity(
    supply: np.ndarray,
    load: np.ndarray,
    perfect_capacity_mw: float,
) -> np.ndarray:
    """Trial-only perfect capacity (methodology §9.2): the perfect
    capacity responds ONLY to the remaining deficit after the ordinary
    supply and never creates NEW surplus, while the ordinary supply's
    ORIGINAL surplus is fully preserved.

    Per hour: residual_deficit = max(load - ordinary_supply, 0);
    perfect_output = min(F, residual_deficit);
    supply_before_storage = ordinary_supply + perfect_output.

    Consequences (vectorized, applied exactly once per (year, scenario,
    sample, hour)):

    - the ordinary supply's existing surplus is never truncated to the
      load level - a storage charging source that exists at F=0 stays
      available at F>0;
    - the perfect capacity never adds charging energy (it never creates
      surplus), and never removes the ordinary surplus;
    - no outages / SOC / duration limits; reuses the existing
      balance/metrics kernel - no second reliability calculator.
    """
    if perfect_capacity_mw <= 0.0:
        return supply
    deficit = np.maximum(load - supply, 0.0)
    perfect_output = np.minimum(float(perfect_capacity_mw), deficit)
    return supply + perfect_output  # type: ignore[no-any-return]


def _build_pre_storage_supply(
    *,
    data: dict[str, Any],
    year: int,
    hours: int,
    capacity_map: dict[str, float],
    derating: dict[str, float],
    renewable: dict[int, np.ndarray],
    hydro: dict[int, np.ndarray],
    intertie_local: dict[int, np.ndarray],
) -> np.ndarray:
    """The SINGLE pre-storage supply constructor shared by the
    risk-window training pass and the scoring pass (never two diverging
    copies of the supply formula)."""
    supply = np.zeros(hours, dtype=np.float64)
    for object_id, capacity in capacity_map.items():
        frac = data["data"][object_id][year][0]
        supply += np.asarray(frac, dtype=float) * capacity * derating.get(object_id, 1.0)
    supply += renewable[year] + hydro[year] + intertie_local[year]
    return supply


@dataclass(frozen=True)
class _SampleYearResult:
    storage_net: np.ndarray
    final_supply: np.ndarray
    metrics: RiskMetricsSample
    context: EventContext
    events: list[DeficitEvent]
    storage_evidence: list[StorageSampleEvidence]
    # W1/MR-04: the ordered emergency relief ALREADY merged into
    # final_supply (recorded so the evidence can report it without
    # re-deriving it)
    emergency_relief: np.ndarray | None = None
    emergency_served_mwh: float = 0.0


def _process_sample(
    *,
    bank: ScenarioBank,
    tables: dict[str, pd.DataFrame],
    config: ResourceRunConfig,
    axes: dict[int, tuple[pd.Series, np.ndarray]],
    load_by_year: dict[int, np.ndarray],
    renewable: dict[int, np.ndarray],
    hydro: dict[int, np.ndarray],
    intertie_local: dict[int, np.ndarray],
    derating: dict[str, float],
    capacity_map: dict[str, float],
    sample: int,
    run_id: str,
    foresight_holder: list[PerfectForesightBound],
    risk_rules: dict[int, RiskWindowRule],
    emergency_programs: tuple[EmergencyProgramSpec, ...] = (),
) -> dict[int, _SampleYearResult]:
    """One SAMPLE across ALL its years: build every year's pre-storage
    horizon, then ONE ``run_multi_year_dispatch`` call so continuous
    multiyear carries SOC across years and independent years
    re-initialize - never a per-year loop over multi-year dispatch."""
    data = bank.read_batch({"sample_indices": [sample]})
    storage_specs = _storage_specs_from_tables(tables)
    horizons_by_year: dict[int, DispatchHorizon] = {}
    pre_storage_by_year: dict[int, np.ndarray] = {}
    # trial-only perfect capacity is applied BEFORE storage dispatch
    # (methodology §9.2): residual_deficit = max(load - ordinary_supply,
    # 0); perfect_output = min(F, residual_deficit);
    # supply_before_storage = ordinary_supply + perfect_output. The
    # perfect capacity responds FIRST to the remaining deficit after the
    # ordinary supply and never creates NEW surplus; the ordinary
    # supply's ORIGINAL surplus stays fully available for storage
    # charging (never truncated to the load level). STORAGE then
    # dispatches on the remaining net load - infinite perfect capacity
    # takes priority over finite-energy storage. F is applied exactly
    # ONCE per (year, scenario, sample, hour).
    supply_before_storage_by_year: dict[int, np.ndarray] = {}
    for year, (axis, _dates) in axes.items():
        hours = len(axis)
        pre_storage = _build_pre_storage_supply(
            data=data,
            year=year,
            hours=hours,
            capacity_map=capacity_map,
            derating=derating,
            renewable=renewable,
            hydro=hydro,
            intertie_local=intertie_local,
        )
        pre_storage_by_year[year] = pre_storage
        supply_before_storage = _apply_perfect_capacity(
            pre_storage, load_by_year[year], config.perfect_capacity_mw
        )
        supply_before_storage_by_year[year] = supply_before_storage
        horizons_by_year[year] = DispatchHorizon(
            pre_storage_supply_mw=tuple(float(v) for v in supply_before_storage),
            load_mw=tuple(float(v) for v in load_by_year[year]),
        )
    results: dict[int, _SampleYearResult] = {}
    if not storage_specs:
        # no storage: skip chronology entirely
        for year in axes:
            final_supply = supply_before_storage_by_year[year]
            relief_array = None
            served = 0.0
            if emergency_programs:
                gap = np.maximum(load_by_year[year] - final_supply, 0.0)
                relief_array, served = apply_emergency_programs(gap, emergency_programs)
                final_supply = final_supply + relief_array
            results[year] = _finalize_year(
                final_supply=final_supply,
                storage_net=np.zeros(len(final_supply), dtype=np.float64),
                soc_before=np.zeros(len(final_supply), dtype=np.float64),
                year=year,
                sample=sample,
                axes=axes,
                load_by_year=load_by_year,
                renewable=renewable,
                hydro=hydro,
                intertie_local=intertie_local,
                derating=derating,
                capacity_map=capacity_map,
                data=data,
                run_id=run_id,
                storage_evidence=[],
                emergency_relief=relief_array,
                emergency_served_mwh=served,
            )
        return results
    units = [
        PortfolioUnit(
            spec=spec,
            state=StorageState(soc_mwh=0.0),
            limits=StorageOperatingLimits(),
            requested_charge_mw=0.0,
            requested_discharge_mw=0.0,
        )
        for spec in storage_specs
    ]
    stream = f"{bank.bank_id}|{config.dispatch_strategy}|s{sample}"

    def _burnin_factory(
        _year: int, _sample: int, _fixed_horizon: DispatchHorizon | None = None
    ) -> DispatchHorizon:
        return horizons_by_year[int(_year)]

    dispatcher = _make_dispatcher(config, foresight_holder, risk_rules)
    # W2/B6-2: the configured end-of-cycle recharge window (disabled by
    # default; the strategy floor only binds in the eue_greedy path)
    end_of_cycle_recharge = None
    if (
        config.storage_soc is not None
        and config.storage_soc.end_of_cycle_recharge is not None
        and config.storage_soc.end_of_cycle_recharge.enabled
    ):
        from capacity_credit.resources.storage_dispatch import EndOfCycleRecharge

        eoc = config.storage_soc.end_of_cycle_recharge
        end_of_cycle_recharge = EndOfCycleRecharge(
            window_hours=eoc.window_hours,
            target_soc_fraction=eoc.target_soc_fraction,
        )
    outcome = run_multi_year_dispatch(
        units,
        horizons_by_year=horizons_by_year,
        chronology_mode=config.chronology_mode,
        soc_year_boundary_mode=config.soc_year_boundary_mode,
        soc_config=config.storage_soc or _default_soc_config(),
        random_stream_identity=stream,
        burnin_horizon_factory=_burnin_factory,
        burnin_sample_count=1,
        dispatcher=dispatcher,
        end_of_cycle_recharge=end_of_cycle_recharge,
    )
    for year, record in zip(sorted(horizons_by_year), outcome.years):
        dispatch = record.dispatch
        hours = len(pre_storage_by_year[year])
        storage_net = _storage_net_from_outcome(dispatch, hours)
        soc_before = _soc_before_from_outcome(dispatch, hours)
        initialization = record.initialization
        year_index = sorted(horizons_by_year).index(year)
        if config.chronology_mode is ChronologyMode.CONTINUOUS_MULTIYEAR and year_index > 0:
            init_policy = "carry_over"
        elif (
            config.storage_soc is not None
            and config.storage_soc.explicit_initial_soc_mwh is not None
        ):
            init_policy = "explicit_initial_soc"
        elif initialization is not None:
            init_policy = initialization.__class__.__name__
        else:
            init_policy = "carry_over"
        storage_evidence = [
            StorageSampleEvidence(
                year=year,
                sample_id=sample,
                dispatch=dispatch,
                initial_state_policy=init_policy,
                warmup_diagnostics=(
                    tuple(initialization.diagnostics)
                    if initialization is not None and hasattr(initialization, "diagnostics")
                    else ()
                ),
                burnin_length_hours=(
                    int(initialization.burnin_hours)
                    if initialization is not None and hasattr(initialization, "burnin_hours")
                    else 0
                ),
            )
        ]
        final_supply = supply_before_storage_by_year[year] + storage_net
        relief_array = None
        served = 0.0
        if emergency_programs:
            gap = np.maximum(load_by_year[year] - final_supply, 0.0)
            relief_array, served = apply_emergency_programs(gap, emergency_programs)
            final_supply = final_supply + relief_array
        results[year] = _finalize_year(
            final_supply=final_supply,
            storage_net=storage_net,
            soc_before=soc_before,
            year=year,
            sample=sample,
            axes=axes,
            load_by_year=load_by_year,
            renewable=renewable,
            hydro=hydro,
            intertie_local=intertie_local,
            derating=derating,
            capacity_map=capacity_map,
            data=data,
            run_id=run_id,
            storage_evidence=storage_evidence,
            emergency_relief=relief_array,
            emergency_served_mwh=served,
        )
    return results


def _finalize_year(
    *,
    final_supply: np.ndarray,
    storage_net: np.ndarray,
    soc_before: np.ndarray,
    emergency_relief: np.ndarray | None = None,
    emergency_served_mwh: float = 0.0,
    year: int,
    sample: int,
    axes: dict[int, tuple[pd.Series, np.ndarray]],
    load_by_year: dict[int, np.ndarray],
    renewable: dict[int, np.ndarray],
    hydro: dict[int, np.ndarray],
    intertie_local: dict[int, np.ndarray],
    derating: dict[str, float],
    capacity_map: dict[str, float],
    data: dict[str, Any],
    run_id: str,
    storage_evidence: list[StorageSampleEvidence],
) -> _SampleYearResult:
    """Balance + metrics + A-11 event context for one (year, sample)."""
    axis, local_dates = axes[year]
    load = load_by_year[year]
    balance = compute_margin(final_supply, load)
    metrics = compute_sample_metrics(balance, local_dates=local_dates)
    hours = len(axis)
    outage_capacity = np.zeros(hours, dtype=np.float64)
    for object_id, capacity in capacity_map.items():
        frac = data["data"][object_id][year][0]
        outage_capacity += np.asarray(frac, dtype=float) * capacity * (
            1.0 - derating.get(object_id, 1.0)
        ) + capacity * (1.0 - np.asarray(frac, dtype=float))
    context = EventContext(
        pre_event_soc_mwh=soc_before,
        renewable_output_mw=renewable[year],  # wind/PV ONLY - never hydro
        hydro_output_mw=hydro[year],
        outage_capacity_mw=outage_capacity,
        intertie_import_mw=intertie_local[year],
    )
    events = detect_deficit_events(
        balance,
        local_dates,
        axis=axis,
        sample_id=f"s{sample}",
        run_id=run_id,
        merge_across_years=False,
        context=context,
    )
    return _SampleYearResult(
        storage_net=storage_net,
        final_supply=final_supply,
        metrics=metrics,
        context=context,
        events=events,
        storage_evidence=storage_evidence,
        emergency_relief=emergency_relief,
        emergency_served_mwh=emergency_served_mwh,
    )


# ----------------------------------------------------------------------
# event merging (A-11)
# ----------------------------------------------------------------------


def _merge_cross_year_events(
    events: list[DeficitEvent],
) -> list[DeficitEvent]:
    """A-11 cross-year merge: per-sample adjacent deficit events whose
    intervals are EXACTLY consecutive are merged. The merged event
    keeps the FIRST segment's pre-event SOC and duration-weights the
    mean context over the whole merged window - everything stays
    recomputable from the hourly input."""
    merged: list[DeficitEvent] = []
    by_sample: dict[str, list[DeficitEvent]] = {}
    for event in events:
        by_sample.setdefault(event.sample_id, []).append(event)
    for sample_id in sorted(by_sample):
        ordered = sorted(by_sample[sample_id], key=lambda e: e.start_time)
        for event in ordered:
            if (
                merged
                and merged[-1].sample_id == sample_id
                and merged[-1].end_time == event.start_time
            ):
                previous = merged[-1]
                total_duration = previous.duration_hours + event.duration_hours

                def _weighted_mean(
                    left: EventContextSnapshot | None,
                    right: EventContextSnapshot | None,
                    left_hours: int,
                    right_hours: int,
                    attribute: str,
                    _total: int = total_duration,
                ) -> float | None:
                    left_value = getattr(left, attribute) if left else None
                    right_value = getattr(right, attribute) if right else None
                    if left_value is None and right_value is None:
                        return None
                    left_value = left_value if left_value is not None else 0.0
                    right_value = right_value if right_value is not None else 0.0
                    return (left_value * left_hours + right_value * right_hours) / _total

                merged_context: EventContextSnapshot | None = None
                if previous.context_values is not None or event.context_values is not None:
                    merged_context = EventContextSnapshot(
                        pre_event_soc_mwh=(
                            previous.context_values.pre_event_soc_mwh
                            if previous.context_values is not None
                            else event.context_values.pre_event_soc_mwh
                            if event.context_values is not None
                            else None
                        ),
                        mean_renewable_output_mw=_weighted_mean(
                            previous.context_values,
                            event.context_values,
                            previous.duration_hours,
                            event.duration_hours,
                            "mean_renewable_output_mw",
                        ),
                        mean_outage_capacity_mw=_weighted_mean(
                            previous.context_values,
                            event.context_values,
                            previous.duration_hours,
                            event.duration_hours,
                            "mean_outage_capacity_mw",
                        ),
                        mean_intertie_import_mw=_weighted_mean(
                            previous.context_values,
                            event.context_values,
                            previous.duration_hours,
                            event.duration_hours,
                            "mean_intertie_import_mw",
                        ),
                        mean_hydro_output_mw=_weighted_mean(
                            previous.context_values,
                            event.context_values,
                            previous.duration_hours,
                            event.duration_hours,
                            "mean_hydro_output_mw",
                        ),
                    )
                combined = DeficitEvent(
                    start_time=previous.start_time,
                    end_time=event.end_time,
                    duration_hours=total_duration,
                    event_eue_mwh=previous.event_eue_mwh + event.event_eue_mwh,
                    max_unserved_power_mw=max(
                        previous.max_unserved_power_mw, event.max_unserved_power_mw
                    ),
                    weather_year=previous.weather_year,
                    year_range=(previous.year_range[0], event.year_range[1]),
                    crosses_year_boundary=(previous.year_range[0] != event.year_range[1]),
                    sample_id=sample_id,
                    run_id=previous.run_id,
                    context_values=merged_context,
                )
                merged[-1] = combined
            else:
                merged.append(event)
    return merged


# ----------------------------------------------------------------------
# aggregation helpers
# ----------------------------------------------------------------------


def _weighted_aggregation(
    stats: dict[int, dict[str, list[float]]],
    probability_years: list[int],
    weights: dict[int, float],
    primary_risk_metric: str,
) -> dict[str, float]:
    names = (
        "lole_days_per_year",
        "lolh_hours_per_year",
        "eue_mwh_per_year",
        "neue_ppm",
        "demand_energy_mwh",
        "primary",
    )
    aggregated: dict[str, float] = {}
    for name in names:
        aggregated[name] = float(
            sum(weights[year] * float(np.mean(stats[year][name])) for year in probability_years)
        )
    if primary_risk_metric == RiskMetric.NEUE_PPM.value:
        demand = aggregated["demand_energy_mwh"]
        if demand <= 0:
            raise ResourceRunError(
                "RESOURCE_RUN_INVALID_DEMAND",
                "weighted demand energy must be positive for NEUE",
                [],
            )
        aggregated["primary"] = 1_000_000.0 * aggregated["eue_mwh_per_year"] / demand
    # NEUE is ALWAYS recomputed from weighted EUE and weighted demand
    # energy - never a plain average of per-sample/annual NEUE
    demand = aggregated["demand_energy_mwh"]
    if demand > 0:
        aggregated["neue_ppm"] = 1_000_000.0 * aggregated["eue_mwh_per_year"] / demand
    return aggregated


# ----------------------------------------------------------------------
# pipeline
# ----------------------------------------------------------------------


def _default_soc_config() -> StorageSocConfig:
    return StorageSocConfig(initialization_mode=StorageSocInitializationMode.WEATHER_CYCLE_WARMUP)


def _storage_net_from_outcome(outcome: DispatchOutcome, hours: int) -> np.ndarray:
    net = np.zeros(hours, dtype=np.float64)
    for record in outcome.hours:
        net[record.hour_index] = (
            record.portfolio.total_discharge_mw - record.portfolio.total_charge_mw
        )
    return net


def _soc_before_from_outcome(outcome: DispatchOutcome, hours: int) -> np.ndarray:
    soc = np.zeros(hours, dtype=np.float64)
    for record in outcome.hours:
        soc[record.hour_index] = sum(unit.step.soc_before_mwh for unit in record.portfolio.units)
    return soc


def run_resource_pipeline(
    *,
    bank: ScenarioBank,
    tables: dict[str, pd.DataFrame],
    config: ResourceRunConfig,
    run_id: str = "resource_run",
    observe: Any = None,
) -> ResourceRunEvidence:
    """Run the full constrained-resource combination and return the
    audited evidence (risk metrics + A-11 events + storage traces).

    ``observe`` (V1-T3-B) is an OPTIONAL read-only progress callback
    invoked at the EXISTING per-sample boundary with
    ``(completed_samples, total_samples)`` AFTER a sample is fully
    processed: it can never change the random draws, the dispatch, the
    batching or the convergence, and a raising observer is isolated so
    normalized values and the run identity stay identical to the
    observer-less run."""
    import dataclasses

    validate_resource_run_config(config, bank)
    # (1) structure preflight over the snapshot-selection fields (no
    #     KeyError / DateParseError / AttributeError from the boundary)
    check_snapshot_structure(tables)
    # (2) explicit scenario resolution: missing / ambiguous / cross-table
    #     inconsistent selections raise a stable ResourceRunError
    resolved_scenario = resolve_scenario(config, tables)
    config = dataclasses.replace(config, scenario_id=resolved_scenario)
    # (3)+(4) ONE runtime snapshot: scenario filter + project-timezone
    #     weather-year filter. Validation, every builder, the intertie
    #     plan / hydro identity / data identity and _process_sample all
    #     consume the SAME snapshot.
    working = select_runtime_snapshot(
        tables, resolved_scenario, config.weather_years, config.timezone
    )
    # (5) selected-snapshot standard validation (split publication)
    standard_warnings = validate_resource_run_inputs(working, config)
    # (6) canonical axes from the selected frozen load, cross-checked
    #     with the ScenarioBank metadata (shared build_hour_axis)
    axes = canonical_axes_from_load(working, bank, config.weather_years, config.timezone)
    years = tuple(sorted(config.weather_years))
    load_by_year: dict[int, np.ndarray] = {}
    local_dates_by_year: dict[int, np.ndarray] = {}
    stamps_all = pd.to_datetime(working["load_hourly"]["timestamp"])
    for year in years:
        _axis_unused, local_dates = axes[year]
        load_frame = working["load_hourly"][stamps_all.dt.year == year]
        load_by_year[year] = align_to_canonical_axis(
            load_frame["timestamp"],
            load_frame["load_mw"].astype(float).to_numpy(),
            canonical_index_by_timestamp(axes, year),
            len(axes[year][0]),
            "load_hourly",
        )
        local_dates_by_year[year] = local_dates
    renewable = _renewable_by_year(working, axes, config.timezone, resolved_scenario)
    hydro, hydro_identities = _hydro_by_year(working, axes, config)
    (
        intertie_local,
        intertie_extra,
        plan_fp,
        support_fp,
        spec_id,
        intertie_diagnostics,
    ) = _intertie_by_year(working, axes, config)
    derating, derating_diagnostics = _derating_by_unit(working, bank)
    storage_specs = _storage_specs_from_tables(working)
    capacity_map = {obj["object_id"]: float(obj["capacity_mw"]) for obj in bank.index["objects"]}

    # risk_window baseline training: TWO passes over the samples are
    # REQUIRED by the methodology - the baseline rule must be trained
    # and FROZEN before any candidate / scoring evaluation (no
    # look-ahead into scored samples). The training pass reuses the SAME
    # pre-storage supply constructor as the scoring pass and accumulates
    # STREAMING statistics (deficit_count / supply_sum) - it never
    # materializes a sample x year x hour matrix.
    risk_window_rules: dict[int, RiskWindowRule] = {}
    if config.dispatch_strategy == "risk_window" and storage_specs:
        deficit_count: dict[int, np.ndarray] = {
            year: np.zeros(len(axes[year][0]), dtype=np.float64) for year in years
        }
        supply_sum: dict[int, np.ndarray] = {
            year: np.zeros(len(axes[year][0]), dtype=np.float64) for year in years
        }
        for sample in range(config.samples):
            data = bank.read_batch({"sample_indices": [sample]})
            for year in years:
                hours = len(axes[year][0])
                supply = _build_pre_storage_supply(
                    data=data,
                    year=year,
                    hours=hours,
                    capacity_map=capacity_map,
                    derating=derating,
                    renewable=renewable,
                    hydro=hydro,
                    intertie_local=intertie_local,
                )
                deficit_count[year] += (supply < load_by_year[year]).astype(np.float64)
                supply_sum[year] += supply
        sample_count = float(config.samples)
        for year in years:
            risk_probability = deficit_count[year] / sample_count
            baseline_mean = supply_sum[year] / sample_count
            training_horizon = DispatchHorizon(
                pre_storage_supply_mw=tuple(float(v) for v in baseline_mean),
                load_mw=tuple(float(v) for v in load_by_year[year]),
            )
            rule = train_risk_window(
                training_horizon,
                baseline_identity=f"{bank.bank_id}|{resolved_scenario}",
                risk_probability_by_hour=tuple(float(v) for v in risk_probability),
                window_length_hours=config.risk_window_length_hours,
                risk_threshold=config.risk_threshold,
                reserve_soc_fraction=config.reserve_soc_fraction,
                training_source="cross_sample_baseline_deficit_probability",
            )
            risk_window_rules[year] = rule

    identity = build_resource_run_identity(
        bank=bank,
        tables=working,
        config=config,
        intertie_plan_fingerprint=plan_fp,
        intertie_support_fingerprint=support_fp,
        intertie_spec_identity=spec_id,
        hydro_identities=hydro_identities,
        storage_specs=storage_specs,
        risk_window_rules=risk_window_rules,
    )

    probability_years = [
        y for y, role in config.year_roles.items() if role == "probability_baseline"
    ]
    weights = config.year_weights or {y: 1.0 / len(probability_years) for y in probability_years}
    stats: dict[int, dict[str, list[float]]] = {
        year: {
            name: []
            for name in (
                "lole_days_per_year",
                "lolh_hours_per_year",
                "eue_mwh_per_year",
                "neue_ppm",
                "demand_energy_mwh",
                "primary",
            )
        }
        for year in years
    }
    all_events: list[DeficitEvent] = []
    storage_evidence: list[StorageSampleEvidence] = []
    event_context_by_sample: dict[tuple[int, int], EventContext] = {}
    post_storage_by_sample: dict[tuple[int, int], np.ndarray] = {}
    storage_net_by_year: dict[int, np.ndarray] = {
        year: np.zeros(len(axes[year][0]), dtype=np.float64) for year in years
    }
    foresight_holder: list[PerfectForesightBound] = []
    # W1/MR-04: the ordered emergency layer parses ONCE from the frozen
    # tables (row order = dispatch order); empty table = legacy behavior
    if config.emergency_layer not in ("auto", "disabled"):
        raise ResourceRunError(
            "RESOURCE_RUN_EMERGENCY_LAYER_INVALID",
            f'emergency_layer must be "auto" or "disabled", got {config.emergency_layer!r}',
            [],
        )
    emergency_programs = (
        ()
        if config.emergency_layer == "disabled"
        else parse_emergency_programs(working)
    )
    for sample in range(config.samples):
        sample_result = _process_sample(
            bank=bank,
            tables=working,
            config=config,
            emergency_programs=emergency_programs,
            axes=axes,
            load_by_year=load_by_year,
            renewable=renewable,
            hydro=hydro,
            intertie_local=intertie_local,
            derating=derating,
            capacity_map=capacity_map,
            sample=sample,
            run_id=run_id,
            foresight_holder=foresight_holder,
            risk_rules=risk_window_rules,
        )
        if observe is not None:
            try:
                observe(sample + 1, config.samples)
            except Exception:  # noqa: BLE001, S110 - a faulty observer is
                pass  # isolated; the computation never changes (V1-T3-B)
        for year in years:
            result = sample_result[year]
            all_events.extend(result.events)
            storage_evidence.extend(result.storage_evidence)
            event_context_by_sample[(year, sample)] = result.context
            post_storage_by_sample[(year, sample)] = result.final_supply
            storage_net_by_year[year] += result.storage_net / max(config.samples, 1)
            for name in (
                "lole_days_per_year",
                "lolh_hours_per_year",
                "eue_mwh_per_year",
                "neue_ppm",
                "demand_energy_mwh",
            ):
                stats[year][name].append(getattr(result.metrics, name))
            stats[year]["primary"].append(getattr(result.metrics, config.primary_risk_metric))

    aggregated = _weighted_aggregation(
        stats, probability_years, weights, config.primary_risk_metric
    )
    risk_metrics: dict[str, float] = {
        "lole_days_per_year": aggregated["lole_days_per_year"],
        "lolh_hours_per_year": aggregated["lolh_hours_per_year"],
        "eue_mwh_per_year": aggregated["eue_mwh_per_year"],
        "neue_ppm": aggregated["neue_ppm"],
        "primary": aggregated["primary"],
    }
    # split publication: standard DQ diagnostics are converted DIRECTLY
    # (never through the ELCC registry) and every distinct position
    # (code + table + locator + message) is preserved; ELCC method
    # diagnostics go through the registry and are deduplicated by code.
    # Standard entries win on code collisions without dropping the other
    # standard positions; output is deterministically sorted.
    method_codes = sorted(set(intertie_diagnostics) | set(derating_diagnostics))
    if foresight_holder:
        method_codes.append("STORAGE_PERFECT_FORESIGHT")
    published = merge_published_diagnostics(standard_warnings, method_codes)
    if config.merge_events_across_years:
        all_events = _merge_cross_year_events(all_events)
    foresight_bounds: dict[tuple[int, int], PerfectForesightBound] = {}
    if foresight_holder:
        positions = [(year, sample) for sample in range(config.samples) for year in years]
        for position, bound in zip(positions, foresight_holder):
            foresight_bounds[position] = bound
    return ResourceRunEvidence(
        config=config,
        scenario_id=resolved_scenario,
        years=years,
        load_by_year=load_by_year,
        local_dates_by_year=local_dates_by_year,
        renewable_by_year=renewable,
        hydro_by_year=hydro,
        intertie_local_by_year=intertie_local,
        candidate_extra_import_by_year=intertie_extra,
        post_storage_supply_by_sample=post_storage_by_sample,
        storage_net_by_year=storage_net_by_year,
        derating_by_unit=derating,
        risk_metrics=risk_metrics,
        deficit_events=tuple(all_events),
        storage_evidence=tuple(storage_evidence),
        event_context_by_sample=event_context_by_sample,
        perfect_foresight_bounds=foresight_bounds,
        risk_window_rules=risk_window_rules,
        identity=identity,
        diagnostics=tuple(published),
        emergency_programs=emergency_programs,
        emergency_relief_mean_by_year={
            year: np.mean(
                [
                    sample_result[year].emergency_relief
                    if sample_result[year].emergency_relief is not None
                    else np.zeros(len(axes[year][0]), dtype=np.float64)
                    for sample in range(config.samples)
                ],
                axis=0,
            )
            for year in years
        },
        emergency_served_mwh_mean_by_year={
            year: float(
                np.mean(
                    [sample_result[year].emergency_served_mwh for sample in range(config.samples)]
                )
            )
            for year in years
        },
    )


def evaluate_candidate(
    evidence: ResourceRunEvidence,
    *,
    extra_import_mw: float = 0.0,
) -> dict[str, float]:
    """Apply the SEPARATE candidate extra-import channel on top of the
    post-storage supply. Per-year sample means first, then year
    weights; NEUE recomputed from weighted EUE and weighted demand;
    extra_import_mw=0 exactly reproduces the baseline metrics."""
    if extra_import_mw < 0.0:
        raise ResourceRunError(
            "RESOURCE_RUN_INVALID_CANDIDATE",
            "extra_import_mw must be non-negative",
            [],
        )
    probability_years = [
        y for y, role in evidence.config.year_roles.items() if role == "probability_baseline"
    ]
    weights = evidence.config.year_weights or {
        y: 1.0 / len(probability_years) for y in probability_years
    }
    names = (
        "lole_days_per_year",
        "lolh_hours_per_year",
        "eue_mwh_per_year",
        "neue_ppm",
        "demand_energy_mwh",
    )
    year_means: dict[str, dict[int, float]] = {name: {} for name in names}
    for year in probability_years:
        local_dates = evidence.local_dates_by_year[year]
        series: dict[str, list[float]] = {name: [] for name in names}
        for sample in range(evidence.config.samples):
            supply = evidence.post_storage_supply_by_sample[(year, sample)] + extra_import_mw
            balance = compute_margin(supply, evidence.load_by_year[year])
            metrics = compute_sample_metrics(balance, local_dates=local_dates)
            for name in names:
                series[name].append(getattr(metrics, name))
        for name in names:
            year_means[name][year] = float(np.mean(series[name]))
    candidate: dict[str, float] = {}
    for name in names:
        candidate[name] = float(
            sum(weights[year] * year_means[name][year] for year in probability_years)
        )
    demand = candidate["demand_energy_mwh"]
    if demand <= 0:
        raise ResourceRunError(
            "RESOURCE_RUN_INVALID_DEMAND",
            "weighted demand energy must be positive",
            [],
        )
    candidate["neue_ppm"] = 1_000_000.0 * candidate["eue_mwh_per_year"] / demand
    if evidence.config.primary_risk_metric == RiskMetric.NEUE_PPM.value:
        candidate["primary"] = candidate["neue_ppm"]
    else:
        candidate["primary"] = candidate[evidence.config.primary_risk_metric]
    return candidate


__all__ = [
    "DEFAULT_DELTA_T_HOURS",
    "ResourceRunConfig",
    "ResourceRunDiagnostic",
    "ResourceRunError",
    "ResourceRunEvidence",
    "ResourceRunIdentity",
    "StorageSampleEvidence",
    "build_resource_run_identity",
    "evaluate_candidate",
    "run_resource_pipeline",
    "validate_resource_run_config",
    "validate_resource_run_inputs",
]
