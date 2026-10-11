"""T12 ELCC end-to-end application workflow.

Thin orchestration between the CLI and the existing services for the
first ELCC chain:

    ScenarioBank -> AdequacyEngine -> ScenarioService.run_risk_series
    -> paired evaluation -> equal-risk search -> ElccResult
    -> MethodResult (elcc branch) + search trace artifact
    -> ArtifactManifest (result + trace) + result link

The workflow reuses the P1 frozen gates, the teaching-project table
reading and the adequacy engine exactly as the adequacy pipeline does;
it never re-implements risk formulas. The candidate system is the base
system plus the SELECTED frozen renewable supply (deterministic supply),
with the load grown by the search variable; ``load_growth_mode`` selects
fixed_mw / scaling / specified (methodology V0.4 §8.2).

Round-2 remediation guarantees:

- the FULL normalized ElccApplicationConfig is validated BEFORE any run
  side effect (invalid config -> P1_ELCC_INVALID_CONFIG, no run dir /
  request file / lock file), then frozen into the run directory;
- the candidate resource selection is resolved against the frozen
  renewable data BEFORE create_run with FULL cross-year / cross-scenario
  completeness: every selected group must have a capacity entry, hourly
  rows and the same installed capacity in EVERY run weather year under
  the effective scenario; no ``get(..., 0.0)`` fallback, no
  ``max(per_year)``; the resolved constant capacity is written into the
  FROZEN EFFECTIVE config (scheme A: capacity_credit_fraction is always
  computable from the resolved reference capacity);
- the effective config lineage: ``method_config_hash`` is the SHA-256 of
  the normalized method config ONLY; ``application_config_hash`` is the
  SHA-256 of the full normalized application config; RunService
  recomputes/verifies the method hash from the validated config; the
  workflow verifies the application hash against the frozen
  elcc_config.json on replay; run_config_hash / request idempotency
  params complete the cross-check set;
- the primary risk metric comes ONLY from RunConfig.primary_risk_metric
  and flows consistently into the engine series, the equal-risk search,
  the trace and the MethodResult (LOLE / LOLH / EUE / NEUE);
- ``search.confidence_level`` (equal-risk significance tests) and
  ``convergence.confidence_level`` (risk-estimate convergence CI) are
  distinct parameters with distinct consumers (see the config contract);
- risk roles: candidate risk = R(S+X, L) at 0 MW (real evaluation);
  matched risk = R(S+X, L+G(C*)) at C* (real re-evaluation, jointly
  converged with all four criteria and within tolerance); risk
  difference = matched - base (T10 sign);
- the search trace artifact is strict and complete (final summary,
  points, bound decisions, FULL monotonicity ledger, diagnostics,
  sample-set/generation identity) with a typed read-back validation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import ValidationError

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    safe_filename_component,
)
from capacity_credit.application.import_service import ImportService
from capacity_credit.application.p1_workflow import (
    P1WorkflowError,
    _load_by_year,
    _read_tables,
    _renewable_supply_by_year,
    _require_frozen_gates,
    _validate_weight_years,
    _write_result_link,
    run_projection,
)
from capacity_credit.application.result_service import (
    build_elcc_method_result,
    read_method_result,
    reserve_publish_ids,
    validate_manifest_before_publish,
    write_artifact_manifest,
    write_method_result,
)
from capacity_credit.application.run_service import RunService
from capacity_credit.application.scenario_service import (
    RiskSeriesOutcome,
    run_risk_series,
)
from capacity_credit.contracts.elcc_config import (
    ElccApplicationConfig,
)
from capacity_credit.contracts.elcc_method_config import (
    ElccMethodCandidateConfig,
    ElccMethodConvergenceConfig,
    ElccMethodLoadGrowthConfig,
    ElccMethodSearchConfig,
)
from capacity_credit.contracts.enums import (
    DataGrade,
    LifecycleStatus,
    ResultStatus,
    RiskConvergence,
)
from capacity_credit.contracts.risk_convergence import convergence_config_identity
from capacity_credit.contracts.search_trace import (
    build_search_trace,
    write_search_trace,
)
from capacity_credit.domain.portfolios import (
    GrowthError,
    LoadGrowthProfile,
)
from capacity_credit.engine.convergence import ConvergenceConfig
from capacity_credit.scenarios.scenario_bank import open_scenario_bank
from capacity_credit.solvers.elcc import (
    ElccSolverConfig,
    ElccTrialOutcome,
    growth_for_capacity,
    run_elcc,
)
from capacity_credit.solvers.equal_risk import EqualRiskSearchConfig
from capacity_credit.solvers.paired_evaluation import (
    RiskSampleSeries,
    evaluate_paired_differences,
)

SEARCH_TRACE_FILE = "search_trace.json"
ELCC_CONFIG_FILE = "elcc_config.json"
P1_ELCC_INVALID_CONFIG = "P1_ELCC_INVALID_CONFIG"
P1_ELCC_CANDIDATE_UNKNOWN = "P1_ELCC_CANDIDATE_UNKNOWN"
P1_ELCC_CANDIDATE_HOURLY_MISSING = "P1_ELCC_CANDIDATE_HOURLY_MISSING"
P1_ELCC_CANDIDATE_HOURLY_DUPLICATE = "P1_ELCC_CANDIDATE_HOURLY_DUPLICATE"
P1_ELCC_CANDIDATE_HOURLY_EXTRA = "P1_ELCC_CANDIDATE_HOURLY_EXTRA"
P1_ELCC_CANDIDATE_HOURLY_INVALID = "P1_ELCC_CANDIDATE_HOURLY_INVALID"
P1_ELCC_CAPACITY_MISMATCH = "P1_ELCC_CAPACITY_MISMATCH"
P1_ELCC_CAPACITY_CROSS_YEAR = "P1_ELCC_CAPACITY_CROSS_YEAR"
P1_ELCC_CAPACITY_BASIS_MISMATCH = "P1_ELCC_CAPACITY_BASIS_MISMATCH"
P1_ELCC_UPPER_BOUND_MISMATCH = "P1_ELCC_UPPER_BOUND_MISMATCH"
P1_ELCC_PROFILE_INVALID = "P1_ELCC_PROFILE_INVALID"


def _emergency_summary_payload(
    tables: dict[str, Any],
) -> list[dict[str, Any]]:
    """F2: the ACTUAL enabled emergency-program list (empty = the table
    is absent = the layer is off) for the assumption register - parsed
    from the SAME frozen tables the run consumed (single fact source)."""
    from capacity_credit.application.resource_run import (
        parse_emergency_programs,
    )

    return [
        {
            "program_id": p.program_id,
            "kind": p.kind,
            "capacity_mw": p.capacity_mw,
            "max_duration_hours": p.max_duration_hours,
            "max_events_per_year": p.max_events_per_year,
            "energy_limit_mwh": p.energy_limit_mwh,
            "availability_fraction": p.availability_fraction,
        }
        for p in parse_emergency_programs(tables)
    ]


def _search_config(model: ElccMethodSearchConfig) -> EqualRiskSearchConfig:
    return EqualRiskSearchConfig(
        lower_bound_mw=model.lower_bound_mw,
        physical_upper_bound_mw=model.physical_upper_bound_mw,
        capacity_tolerance_mw=model.capacity_tolerance_mw,
        max_iterations=model.max_iterations,
        monotonic_direction=model.monotonic_direction,
        allow_upper_bound_expansion=model.allow_upper_bound_expansion,
        max_expanded_upper_bound_mw=model.max_expanded_upper_bound_mw,
        confidence_level=model.confidence_level,
    )


def _convergence_config(
    model: ElccMethodConvergenceConfig,
    primary_risk_metric: str,
) -> ConvergenceConfig:
    """Typed convergence parameters (versioned contract; the teaching
    defaults are explicit and marked provisional, never hidden). The
    primary risk metric comes ONLY from RunConfig.primary_risk_metric -
    it is never hardcoded here."""
    return ConvergenceConfig(
        batch_size=model.batch_size,
        min_samples=model.min_samples,
        max_samples=model.max_samples,
        confidence_level=model.confidence_level,
        relative_half_width_tolerance=model.relative_half_width_tolerance,
        absolute_half_width_tolerance=model.absolute_half_width_tolerance,
        near_zero_threshold=model.near_zero_threshold,
        min_nonzero_samples=model.min_nonzero_samples,
        recent_batch_window=model.recent_batch_window,
        recent_batch_stability_tolerance=model.recent_batch_stability_tolerance,
        use_student_t=model.use_student_t,
        normal_interval_min_samples=model.normal_interval_min_samples,
        primary_risk_metric=primary_risk_metric,
    )


def _canonical_run_time_axis(
    tables: dict[str, Any],
    timezone: str,
) -> dict[int, pd.DatetimeIndex]:
    """The single canonical ELCC run time axis: the frozen load_hourly
    timestamps converted to the SCENARIO-BANK timezone NAME (never
    UTC-only absolute instants, never fixed-offset names, never the
    profile axis). Unparsable / naive load timestamps block with a
    stable code (the frozen data passed import validation, so this is
    defensive)."""
    load = tables["load_hourly"]
    canonical: dict[int, pd.DatetimeIndex] = {}
    for year, group in load.groupby(load["weather_year"].astype(int)):
        try:
            stamps = pd.to_datetime(group.sort_values("timestamp")["timestamp"])
        except (TypeError, ValueError) as exc:
            raise P1WorkflowError(
                P1_ELCC_INVALID_CONFIG,
                f"unparsable load timestamp axis for year {year}: {exc}",
            ) from exc
        if stamps.dt.tz is None:
            raise P1WorkflowError(
                P1_ELCC_INVALID_CONFIG,
                f"load timestamps for year {year} must be timezone-aware",
            )
        if stamps.isna().any():
            raise P1WorkflowError(
                P1_ELCC_INVALID_CONFIG,
                f"load timestamps for year {year} must not contain NaT",
            )
        canonical[int(year)] = pd.DatetimeIndex(stamps.dt.tz_convert(timezone).to_numpy())
    if not canonical:
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "frozen load data carries no weather years",
        )
    return canonical


def _growth_profile(
    model: ElccMethodLoadGrowthConfig,
    base_load: dict[int, np.ndarray],
    run_time_axis: dict[int, pd.DatetimeIndex],
) -> LoadGrowthProfile | None:
    """STRUCTURAL + time-axis validation of the specified growth profile
    against the REAL frozen run axis BEFORE create_run - deliberately
    SCALE-FREE (round 5): the profile's ORIGINAL scale is never used
    here because the solver recomputes scale = capacity / peak_term at
    every trial. A zero-scale copy exercises the identical axis
    comparisons (year sets, per-year length, timezone NAME, hour-by-hour
    timestamps, duplicates / missing / extra / shifted / leap-year)
    while keeping the grown load trivially non-negative. The ACTUAL
    re-scaled load is validated separately by
    ``_validate_growth_at_max_capacity`` with the solver's exact
    semantics. GrowthError maps to P1_ELCC_PROFILE_INVALID."""
    if model.mode != "specified":
        return None
    assert model.specified_profile is not None
    profile = model.specified_profile.to_profile()
    from capacity_credit.domain.portfolios import GrowthError, apply_specified_growth

    try:
        zero_scale = LoadGrowthProfile(
            load_growth_profile_id=profile.load_growth_profile_id,
            source_id=profile.source_id,
            data_status=profile.data_status,
            basis=profile.basis,
            scale=0.0,
            shape_by_year=profile.shape_by_year,
            timestamps_by_year=profile.timestamps_by_year,
        )
        apply_specified_growth(base_load, run_time_axis, zero_scale)
    except GrowthError as exc:
        raise P1WorkflowError(
            P1_ELCC_PROFILE_INVALID,
            f"specified growth profile does not match the frozen run time axis: {exc}",
        ) from exc
    return profile


def _validate_growth_at_max_capacity(
    *,
    effective: ElccApplicationConfig,
    profile: LoadGrowthProfile | None,
    base_load: dict[int, np.ndarray],
    time_axis_by_year: dict[int, pd.DatetimeIndex],
) -> None:
    """SOLVER-SEMANTICS preflight check (round 5): run the exact
    growth_for_capacity the search would run, at the MAXIMUM searchable
    capacity - physical_upper_bound_mw, or max_expanded_upper_bound_mw
    when expansion is enabled. The ELCC search lower bound is fixed at
    0 and the rescaling is linear in capacity (scale = capacity /
    peak_term), so the maximum capacity is the most adverse state for
    load non-negativity: if the max-capacity grown load is
    non-negative, every smaller trial capacity is too. GrowthError /
    ElccConfigError map to P1_ELCC_PROFILE_INVALID BEFORE create_run -
    never a FAILED run."""
    portfolio_id = effective.candidate.resource_or_portfolio_id
    solver_config = _solver_config(effective, profile, portfolio_id, time_axis_by_year)
    if effective.search.allow_upper_bound_expansion:
        max_capacity = effective.search.max_expanded_upper_bound_mw
        assert max_capacity is not None
    else:
        max_capacity = effective.search.physical_upper_bound_mw
    from capacity_credit.solvers.elcc import ElccConfigError, growth_for_capacity

    try:
        growth_for_capacity(solver_config, float(max_capacity), base_load)
    except (GrowthError, ElccConfigError) as exc:
        code = getattr(exc, "code", "")
        raise P1WorkflowError(
            P1_ELCC_PROFILE_INVALID,
            f"specified growth invalid at the maximum search capacity "
            f"{max_capacity} MW ({code}): {exc}",
        ) from exc


def _parse_capacity_table(tables: dict[str, Any]) -> tuple[dict[tuple[str, int], float], set[str]]:
    """Parse the frozen renewable-capacity table into
    {(group, year): installed_mw} and the known group set. Every entry
    is a strict finite number; unparsable years / capacities block."""
    installed: dict[tuple[str, int], float] = {}
    known: set[str] = set()
    for _, row in tables["renewable_capacities"].iterrows():
        try:
            year = int(float(row["year"]))
        except (TypeError, ValueError) as exc:
            raise P1WorkflowError(
                "P1_RENEWABLE_CAPACITY_YEAR",
                f"unparsable capacity year {row.get('year')!r}",
            ) from exc
        group = str(row["resource_group_id"])
        raw = row["installed_capacity_mw"]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not np.isfinite(float(raw))
        ):
            raise P1WorkflowError(
                "P1_RENEWABLE_CAPACITY_VALUE",
                f"invalid installed_capacity_mw {raw!r} for group {group!r}",
            )
        capacity = float(raw)
        if capacity < 0.0:
            raise P1WorkflowError(
                "P1_RENEWABLE_CAPACITY_VALUE",
                f"negative installed_capacity_mw {capacity} for group {group!r}",
            )
        known.add(group)
        installed[(group, year)] = capacity
    return installed, known


def _check_cross_year_capacity(
    installed: dict[tuple[str, int], float],
    selected: list[str],
    years: list[int],
) -> dict[str, float]:
    """Every selected group must have a capacity entry for EVERY run
    weather year, and the installed capacity must be IDENTICAL across
    those years (weather trajectories; year-varying capacity would
    require an explicit versioned rule - there is none, so strict
    consistency is enforced). Returns {group: constant_capacity}."""
    per_group: dict[str, set[float]] = {group: set() for group in selected}
    for year in years:
        for group in selected:
            key = (group, year)
            if key not in installed:
                raise P1WorkflowError(
                    P1_ELCC_CANDIDATE_UNKNOWN,
                    f"candidate resource group {group!r} has no installed "
                    f"capacity for run weather year {year}",
                )
            per_group[group].add(installed[key])
    constant: dict[str, float] = {}
    for group in selected:
        capacities = per_group[group]
        if len(capacities) != 1:
            raise P1WorkflowError(
                P1_ELCC_CAPACITY_CROSS_YEAR,
                f"candidate resource group {group!r} has DIFFERENT "
                f"installed capacities across run weather years "
                f"{sorted(capacities)}; year-varying capacity requires an "
                "explicit versioned rule (none exists)",
            )
        constant[group] = next(iter(capacities))
    return constant


def _effective_scenario_for_year(
    renewable: Any,
    year: int,
    scenario_id: str | None,
) -> str | None:
    """Resolve the effective renewable scenario for one weather year
    (explicit scenario_id or the single scenario; ambiguity blocks)."""
    year_frame = renewable[renewable["weather_year"].astype(int) == year]
    scenarios = sorted(year_frame["scenario_id"].astype(str).unique())
    if scenario_id is not None:
        if scenario_id not in scenarios:
            raise P1WorkflowError(
                "P1_RENEWABLE_SCENARIO_MISSING",
                f"scenario {scenario_id!r} has no renewable data in year {year}",
            )
        return scenario_id
    if len(scenarios) > 1:
        raise P1WorkflowError(
            "P1_RENEWABLE_SCENARIO_AMBIGUOUS",
            f"year {year} has multiple scenarios {scenarios}; an explicit scenario_id is required",
        )
    return scenarios[0] if scenarios else None


def _check_group_hourly_axis(
    group_frame: Any,
    group: str,
    year: int,
    effective_scenario: str,
    load_utc: Any,
) -> None:
    """Strict timestamp-set equality of ONE candidate group-year against
    the frozen time axis, with DETERMINISTIC error priority (round 4):

    1. INVALID   - unparsable / naive / NaT timestamps (naive is NEVER
                   silently interpreted as UTC);
    2. DUPLICATE - duplicate timestamps (even when the row count equals
                   the axis length, i.e. duplicate + missing);
    3. MISSING   - hours of the frozen axis absent from the data
                   (reported before EXTRA when both are present);
    4. EXTRA     - hours beyond the frozen axis.
    Every message carries group / year / scenario."""
    try:
        # explicit mixed-ISO parsing: deterministic error behaviour and
        # no pandas format-inference warning for malformed entries
        stamps = pd.to_datetime(group_frame["timestamp"], format="mixed")
    except (TypeError, ValueError) as exc:
        raise P1WorkflowError(
            P1_ELCC_CANDIDATE_HOURLY_INVALID,
            f"candidate resource group {group!r} year {year} scenario "
            f"{effective_scenario!r}: unparsable timestamp: {exc}",
        ) from exc
    if stamps.dt.tz is None:
        raise P1WorkflowError(
            P1_ELCC_CANDIDATE_HOURLY_INVALID,
            f"candidate resource group {group!r} year {year} scenario "
            f"{effective_scenario!r}: naive timestamps are not allowed "
            "(never silently interpreted as UTC)",
        )
    if stamps.isna().any():
        raise P1WorkflowError(
            P1_ELCC_CANDIDATE_HOURLY_INVALID,
            f"candidate resource group {group!r} year {year} scenario "
            f"{effective_scenario!r}: timestamps must not contain NaT",
        )
    timestamps = pd.DatetimeIndex(stamps.dt.tz_convert("UTC"))
    if len(set(timestamps)) != len(timestamps):
        raise P1WorkflowError(
            P1_ELCC_CANDIDATE_HOURLY_DUPLICATE,
            f"candidate resource group {group!r} year {year} scenario "
            f"{effective_scenario!r}: DUPLICATE hourly timestamps",
        )
    ts_set = set(timestamps)
    axis_set = set(load_utc)
    missing = axis_set - ts_set
    extra = ts_set - axis_set
    if missing:
        raise P1WorkflowError(
            P1_ELCC_CANDIDATE_HOURLY_MISSING,
            f"candidate resource group {group!r} year {year} scenario "
            f"{effective_scenario!r}: MISSES {len(missing)} hours of the "
            f"frozen time axis (axis {len(axis_set)} hours, data "
            f"{len(ts_set)})",
        )
    if extra:
        raise P1WorkflowError(
            P1_ELCC_CANDIDATE_HOURLY_EXTRA,
            f"candidate resource group {group!r} year {year} scenario "
            f"{effective_scenario!r}: has {len(extra)} EXTRA hours beyond "
            f"the frozen time axis",
        )


def _check_hourly_axis_completeness(
    renewable: Any,
    selected: list[str],
    scenario_id: str | None,
    years: list[int],
    time_axis_by_year: dict[int, Any],
    installed: dict[tuple[str, int], float],
    timezone: str,
) -> None:
    """STRICT candidate hourly completeness against the frozen time
    axis (round 3): for every selected group, effective scenario and
    run weather year, the candidate timestamp set must EXACTLY equal
    the frozen time axis (no missing first/last/middle hours, no
    duplicate, no extra hours), and the hourly VALUES must pass the
    single domain authority ``build_renewable_series`` (NaN/Inf,
    negative, capacity-factor range, weather-year vs local-year
    mismatch, naive timestamps). RenewableError is mapped to stable
    P1_ELCC preflight codes."""
    from capacity_credit.resources.renewable import (
        RenewableError,
        build_renewable_series,
    )

    for year in years:
        axis = time_axis_by_year[year]
        load_utc = pd.DatetimeIndex(pd.to_datetime(pd.Series(axis), utc=True))
        effective_scenario = _effective_scenario_for_year(renewable, year, scenario_id)
        year_frame = renewable[renewable["weather_year"].astype(int) == year]
        for group in selected:
            if effective_scenario is None:
                raise P1WorkflowError(
                    P1_ELCC_CANDIDATE_HOURLY_MISSING,
                    f"no renewable hourly data at all for year {year}",
                )
            group_frame = year_frame[
                (year_frame["resource_group_id"].astype(str) == group)
                & (year_frame["scenario_id"].astype(str) == effective_scenario)
            ]
            if group_frame.empty:
                raise P1WorkflowError(
                    P1_ELCC_CANDIDATE_HOURLY_MISSING,
                    f"candidate resource group {group!r} has no hourly "
                    f"output rows for year {year} scenario "
                    f"{effective_scenario!r}",
                )
            _check_group_hourly_axis(group_frame, group, year, effective_scenario, load_utc)
            try:
                build_renewable_series(
                    group_frame.assign(installed_capacity_mw=installed[(group, year)]),
                    timezone=timezone,
                    load_axis=pd.Series(axis),
                )
            except RenewableError as exc:
                code = (
                    P1_ELCC_CAPACITY_BASIS_MISMATCH
                    if exc.code in ("RENEWABLE_CF_MISMATCH", "RENEWABLE_CF_RANGE")
                    else P1_ELCC_CANDIDATE_HOURLY_INVALID
                )
                raise P1WorkflowError(
                    code,
                    f"candidate resource group {group!r} hourly data "
                    f"invalid in year {year} scenario {effective_scenario!r}: "
                    f"{exc}",
                ) from exc


def _resolve_candidate_resources(
    tables: dict[str, Any],
    time_axis_by_year: dict[int, Any],
    candidate: ElccMethodCandidateConfig,
    scenario_id: str | None,
    bank_years: list[int],
    timezone: str,
) -> tuple[list[str], float]:
    """Resolve the candidate resource selection against the frozen
    renewable data BEFORE create_run, with FULL cross-year and
    cross-scenario completeness (see the helpers). Returns
    (selected group ids, candidate nominal capacity MW); the capacity
    is the CONSTANT per-group capacity summed over the selection (never
    max(per_year), never a missing-value fallback)."""
    selected = candidate.selected_group_ids()
    installed, known = _parse_capacity_table(tables)
    for group in selected:
        if group not in known:
            raise P1WorkflowError(
                P1_ELCC_CANDIDATE_UNKNOWN,
                f"candidate resource group {group!r} is not in the frozen renewable capacities",
            )
    years = sorted({int(y) for y in bank_years})
    constant = _check_cross_year_capacity(installed, selected, years)
    _check_hourly_axis_completeness(
        tables["renewable_hourly"],
        selected,
        scenario_id,
        years,
        time_axis_by_year,
        installed,
        timezone,
    )
    candidate_capacity = sum(constant[group] for group in selected)
    if candidate_capacity <= 0.0:
        raise P1WorkflowError(
            P1_ELCC_CANDIDATE_UNKNOWN,
            "candidate selection has zero installed capacity in the frozen renewable capacities",
        )
    return selected, candidate_capacity


def _effective_config(
    app_config: ElccApplicationConfig,
    candidate_capacity: float,
) -> ElccApplicationConfig:
    """Frozen EFFECTIVE config (scheme A): the resolved candidate
    nominal capacity from the frozen data is written into the frozen
    config; a configured reference capacity must agree with it. Every
    downstream consumer (solver, MethodResult, trace) reads from this
    effective config, so capacity_credit_fraction is always computable
    from a real frozen capacity."""
    configured = app_config.candidate.reference_capacity_mw
    if configured is not None and abs(configured - candidate_capacity) > 1e-6:
        raise P1WorkflowError(
            P1_ELCC_CAPACITY_MISMATCH,
            f"configured reference capacity {configured} MW does not "
            f"match the frozen candidate capacity {candidate_capacity} MW",
        )
    if configured == candidate_capacity:
        return app_config
    candidate = app_config.candidate.model_copy(
        update={"reference_capacity_mw": candidate_capacity}
    )
    return app_config.model_copy(update={"candidate": candidate})


def _validate_and_freeze_config(
    app_config: ElccApplicationConfig,
    *,
    project: Path,
    import_batch_id: str,
    data_version_id: str,
    bank: Any,
) -> tuple[
    dict[str, Any],
    dict[int, Any],
    ElccApplicationConfig,
    str,
    str,
    list[str],
]:
    """Full-config pre-flight BEFORE create_run (no run lifecycle side
    effect): profile structure, candidate completeness (cross-year /
    cross-scenario / hourly / constant capacity), search upper bound
    consistency, then the normalized EFFECTIVE config payload + its
    stable hashes.

    Returns (tables, time axis by year, effective config,
    application_config_payload, application_config_hash, selected)."""
    import_service = _require_frozen_gates(project, import_batch_id, data_version_id)
    tables = _read_tables(import_service.frozen_files(import_batch_id))
    timezone = str(bank.index["timezone"])
    time_axis_by_year = _canonical_run_time_axis(tables, timezone)
    base_load = _load_by_year(tables)
    _growth_profile(app_config.load_growth, base_load, time_axis_by_year)
    bank_years = [int(y) for y in bank.index["weather_years"]]
    selected, candidate_capacity = _resolve_candidate_resources(
        tables,
        time_axis_by_year,
        app_config.candidate,
        app_config.run_config.scenario_id,
        bank_years,
        timezone,
    )
    effective = _effective_config(app_config, candidate_capacity)
    # F4/C5: a FORMAL run must declare real convergence intent - the
    # untouched teaching defaults are refused (zero side effects);
    # teaching/research chains keep them
    _run_mode_value = getattr(
        effective.run_config.run_mode, "value", str(effective.run_config.run_mode)
    )
    if _run_mode_value == "formal":
        from capacity_credit.contracts.elcc_method_config import (
            is_teaching_default_convergence,
        )

        if is_teaching_default_convergence(effective.convergence):
            raise P1WorkflowError(
                "P1_CONVERGENCE_PROFILE_REQUIRED",
                "正式运行不得携带未声明的教学级收敛默认值：请在 convergence "
                "节显式给出生产档案（PRODUCTION_CONVERGENCE_PROFILE，provisional）"
                "或真实自定义值（如 min_samples/max_samples/容差），拒绝创建",
            )
    # MR-02 (A2 方案一): a provided calibrated_baseline_ref must bind to
    # THIS run's frozen data version and carry a sane criterion triple -
    # anything else refuses BEFORE create_run (no run side effect). The
    # batch-manifest side (the round actually converged inside the band)
    # is validated by calibration_service.validate_calibrated_baseline_ref
    # when the reference is assembled from the batch.
    ref = effective.calibrated_baseline_ref
    if ref is not None:
        frozen_state = import_service.current_state(import_batch_id)
        if str(ref.data_version_hash) != str(frozen_state.data_version_hash):
            raise P1WorkflowError(
                "P1_CALIBRATION_REF_MISMATCH",
                "calibrated_baseline_ref.data_version_hash 与本运行冻结数据"
                f"版本 {frozen_state.data_version_hash!r} 不一致：标定闭环只对"
                "同一冻结世界有效",
            )
        if ref.load_scale_percent < -50.0 or ref.load_scale_percent > 100.0:
            raise P1WorkflowError(
                "P1_CALIBRATION_REF_MISMATCH",
                f"calibrated_baseline_ref.load_scale_percent={ref.load_scale_percent!r} "
                "超出可信标定范围 [-50%, +100%]",
            )
        if (
            float(ref.criterion_target_value) <= 0.0
            or not 0.0 < float(ref.tolerance_percent) < 50.0
        ):
            raise P1WorkflowError(
                "P1_CALIBRATION_REF_MISMATCH",
                "calibrated_baseline_ref 判据三元组非法：目标值必须为正、"
                "容差须在 0—50% 之间",
            )
    if abs(effective.search.physical_upper_bound_mw - candidate_capacity) > 1e-6:
        raise P1WorkflowError(
            P1_ELCC_UPPER_BOUND_MISMATCH,
            f"search physical upper bound "
            f"{effective.search.physical_upper_bound_mw} MW must equal "
            f"the candidate nominal capacity {candidate_capacity} MW",
        )
    profile = _growth_profile(app_config.load_growth, base_load, time_axis_by_year)
    _validate_growth_at_max_capacity(
        effective=effective,
        profile=profile,
        base_load=base_load,
        time_axis_by_year=time_axis_by_year,
    )
    normalized = effective.model_dump(mode="json")
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    app_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return tables, time_axis_by_year, effective, payload, app_hash, selected


def _parse_app_config(app_config: ElccApplicationConfig | dict[str, Any]) -> ElccApplicationConfig:
    """Pipeline-entry config normalization with stable error mapping
    (round 5): EVERY input - dict OR already-constructed model instance -
    is normalized through model_dump and RE-VALIDATED through
    model_validate. A constructed instance may have been mutated after
    construction (plain field assignment does not re-run pydantic
    validators), bypassing ElccRunSettings / confidence / specified
    profile rules; the re-validation also REBUILDS the private domain
    profile of LoadGrowthProfileConfig so it can never disagree with
    the fields. All entry-config errors map to P1_ELCC_INVALID_CONFIG."""
    try:
        if isinstance(app_config, ElccApplicationConfig):
            payload = app_config.model_dump(mode="json")
        elif isinstance(app_config, dict):
            payload = app_config
        else:
            raise TypeError("app_config must be an ElccApplicationConfig or a dict")
        return ElccApplicationConfig.model_validate(payload)
    except Exception as exc:
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            f"invalid ELCC config: {exc}",
        ) from exc


def _open_bank(project: Path, config: Any) -> Any:
    """Open and verify the scenario bank; the seed must match the
    frozen run config."""
    scenario_bank_id = config.scenario_bank_id
    if not scenario_bank_id:
        raise P1WorkflowError(
            "P1_ELCC_BANK_REQUIRED",
            "run_config.scenario_bank_id is required for ELCC",
        )
    bank = open_scenario_bank(project / "artifacts" / "scenario_banks" / scenario_bank_id)
    if bank.bank_id != scenario_bank_id:
        raise P1WorkflowError(
            "P1_SCENARIO_MISMATCH",
            "scenario bank id does not match the reference",
        )
    if int(bank.index["root_seed"]) != config.random_seed:
        raise P1WorkflowError(
            "P1_SEED_MISMATCH",
            f"random_seed {config.random_seed} does not match the "
            f"scenario bank seed {bank.index['root_seed']}",
        )
    return bank


def _prepare_run(
    *,
    project: Path,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: ElccApplicationConfig,
    attach_run: Any = None,
) -> tuple[
    dict[str, Any],
    dict[int, Any],
    ElccApplicationConfig,
    str,
    str,
    list[str],
    Any,
    str,
    Path,
]:
    """Pre-flight + create_run + frozen-config persistence (idempotent
    replay: the same request_id reuses the same run directory and the
    frozen config must be byte-identical). Returns the run artifacts
    needed by the computation stage."""
    bank = _open_bank(project, app_config.run_config)
    tables, time_axis_by_year, effective, payload, app_hash, selected = _validate_and_freeze_config(
        app_config,
        project=project,
        import_batch_id=import_batch_id,
        data_version_id=data_version_id,
        bank=bank,
    )
    run_service = RunService(project, ImportService(project))
    try:
        # effective.to_run_config() sits INSIDE the stable exception
        # mapping boundary (round 5): no bare ValidationError escapes
        full_run_config = effective.to_run_config().model_dump(mode="json")
        created = run_service.create_run(
            request_id=request_id,
            run_config=full_run_config,
            import_batch_id=import_batch_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            scenario_bank_id=app_config.run_config.scenario_bank_id,
            teaching_override_reason="teaching fixture synthetic project (P2)",
            method_config_hash=_method_config_hash(effective),
            application_config_hash=app_hash,
        )
    except ValidationError as exc:
        # residual contract mismatch at the freeze boundary must never
        # leak as a bare pydantic error
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            f"invalid ELCC config: {exc}",
        ) from exc
    resolved_run_id = str(created["run_id"])
    # V1-T3-D seam 3: the formal identity exists HERE - surface it to
    # the caller before any long computation starts (mirrors the
    # resource chain's post-create attach)
    if attach_run is not None:
        attach_run(resolved_run_id)
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / ELCC_CONFIG_FILE
    if config_path.exists():
        existing = config_path.read_bytes().decode("utf-8").strip()
        if existing != payload:
            raise P1WorkflowError(
                P1_ELCC_INVALID_CONFIG,
                "existing frozen elcc_config.json differs from the current request payload",
            )
    else:
        atomic_write_bytes(
            config_path,
            (payload + "\n").encode("utf-8"),
            immutable=True,
        )
    return (
        tables,
        time_axis_by_year,
        effective,
        payload,
        app_hash,
        selected,
        bank,
        resolved_run_id,
        run_dir,
    )


def _method_config_hash(app_config: ElccApplicationConfig) -> str:
    """SHA-256 of the normalized METHOD config ONLY (search +
    load_growth + candidate + convergence), never the full application
    config."""
    method = app_config.to_run_config().method_config
    assert method is not None
    payload = json.dumps(method.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _engine_series(
    *,
    bank: Any,
    import_service: ImportService,
    import_batch_id: str,
    scenario_id: str | None,
    run_id: str,
    data_version_hash: str,
    config_hash: str,
    software_version: str,
    load_by_year: dict[int, np.ndarray],
    convergence: ConvergenceConfig,
    year_weights: dict[int, float],
    deterministic_supply_by_year: dict[int, np.ndarray] | None,
    portfolio_id: str,
    shared_object_ids: frozenset[str],
    candidate_object_ids: frozenset[str],
    sample_limit: int | None,
) -> RiskSeriesOutcome:
    """Run the real engine chain over a fixed sample set (ScenarioBank
    -> AdequacyEngine -> ScenarioService)."""
    return run_risk_series(
        bank=bank,
        load_by_year=load_by_year,
        run_id=run_id,
        data_version_hash=data_version_hash,
        config_hash=config_hash,
        software_version=software_version,
        config=convergence,
        year_weights=year_weights,
        deterministic_supply_by_year=deterministic_supply_by_year,
        portfolio_id=portfolio_id,
        shared_object_ids=shared_object_ids,
        candidate_object_ids=candidate_object_ids,
        sample_limit=sample_limit,
    )


# R2-B4: the preflight growth validation never touches the engine, so it
# carries an inert label - honestly named as such, never a fake digest.
# The REAL search config is built from the base series' actual
# exogenous fingerprint (exogenous_invariant_digest, C-29).
PREFLIGHT_EXOGENOUS_FINGERPRINT = "ex0:preflight_no_series"


def _solver_config(
    app_config: ElccApplicationConfig,
    profile: LoadGrowthProfile | None,
    portfolio_id: str,
    time_axis_by_year: dict[int, Any],
    exogenous_fingerprint: str = PREFLIGHT_EXOGENOUS_FINGERPRINT,
) -> ElccSolverConfig:
    """Shared ELCC solver config (used by the trial provider and the
    search entry). The REAL canonical run time axis is carried as a
    runtime-only field (never persisted into the method config)."""
    return ElccSolverConfig(
        search=_search_config(app_config.search),
        load_growth_mode=app_config.load_growth.mode,
        specified_profile=profile,
        reference_capacity_mw=app_config.candidate.reference_capacity_mw,
        exogenous_invariant_fingerprint=exogenous_fingerprint,
        portfolio_id=portfolio_id,
        run_time_axis_by_year=time_axis_by_year,
    )


def _make_trial_provider(
    *,
    bank: Any,
    project: Path,
    import_batch_id: str,
    scenario_id: str | None,
    run_id: str,
    data_version_hash: str,
    run_config_hash: str,
    software_version: str,
    base_load: dict[int, np.ndarray],
    renewable: dict[int, np.ndarray],
    convergence: ConvergenceConfig,
    year_weights: dict[int, float],
    reference: RiskSampleSeries,
    solver_config: ElccSolverConfig,
    shared_ids: frozenset[str],
    candidate_ids: frozenset[str],
    sample_limit: int | None,
) -> Any:
    """Build the candidate-system trial provider (real engine chain per
    capacity)."""

    def trial_provider(capacity_mw: float) -> ElccTrialOutcome:
        growth = growth_for_capacity(
            solver_config,
            capacity_mw,
            base_load,
        )
        trial_outcome = _engine_series(
            bank=bank,
            import_service=ImportService(project),
            import_batch_id=import_batch_id,
            scenario_id=scenario_id,
            run_id=run_id,
            data_version_hash=data_version_hash,
            config_hash=run_config_hash,
            software_version=software_version,
            load_by_year=dict(growth.grown_load_by_year),
            convergence=convergence,
            year_weights=year_weights,
            deterministic_supply_by_year=renewable,
            portfolio_id=solver_config.portfolio_id,
            shared_object_ids=shared_ids,
            candidate_object_ids=candidate_ids,
            sample_limit=sample_limit,
        )
        # R2-B4 (C-29): the trial's fingerprint is recomputed from ITS
        # actual exogenous inputs - a drift against the base anchor
        # aborts the search honestly instead of passing a label check
        if (
            trial_outcome.exogenous_fingerprint
            != solver_config.exogenous_invariant_fingerprint
        ):
            raise P1WorkflowError(
                "P1_EXOGENOUS_DRIFT",
                "试算序列的外生不变量指纹与基准序列不一致（C-29 控制变量纪律，"
                "方法说明第四章(一)）：搜索中止，不在漂移的外生世界上出数",
            )
        return ElccTrialOutcome(
            paired_result=evaluate_paired_differences(
                reference=reference,
                candidate=trial_outcome.risk_series,
            ),
            risk_metrics=dict(trial_outcome.risk_metrics or {}),
            risk_convergence=RiskConvergence(trial_outcome.risk_convergence),
            convergence_criteria=dict(trial_outcome.criteria),
            should_stop=trial_outcome.should_stop,
            recent_batch_evidence=tuple(trial_outcome.recent_batch_evidence),
            sample_set_identity=trial_outcome.sample_set_identity,
            config_identity=convergence_config_identity(convergence.to_snapshot()),
        )

    return trial_provider


def _run_computation(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    import_batch_id: str,
    tables: dict[str, Any],
    time_axis_by_year: dict[int, Any],
    effective: ElccApplicationConfig,
    selected: list[str],
    bank: Any,
    run_config_hash: str,
    observe: Any = None,
) -> dict[str, Any]:
    """Base series + equal-risk search + publication (the compute
    stage; failure marks the run FAILED). ``observe`` (optional) is a
    (phase, subtask, observed, total) callback invoked ONLY at real
    boundaries that already exist (base series completion, each search
    trial); failures are isolated and never touch the search."""
    def _safe_observe(
        subtask: str, observed: float | None, total: float | None
    ) -> None:
        if observe is None:
            return
        try:
            observe("elcc", subtask, observed, total)
        except Exception:  # noqa: BLE001 - progress isolation only
            return

    state = ImportService(project).current_state(import_batch_id)
    effective_grade = ImportService(project).effective_data_grade(import_batch_id)
    base_load = _load_by_year(tables)
    timezone = str(bank.index["timezone"])
    renewable = _renewable_supply_by_year(
        tables,
        time_axis_by_year,
        timezone=timezone,
        scenario_id=effective.run_config.scenario_id,
        resource_group_ids=selected,
    )
    year_weights = _year_weights_from_tables(tables, effective.run_config)
    _validate_weight_years(year_weights, list(bank.index["weather_years"]))
    convergence = _convergence_config(
        effective.convergence,
        effective.run_config.primary_risk_metric.value,
    )
    sample_limit = min(
        int(bank.index["sample_count"]),
        convergence.max_samples,
    )
    portfolio_id = effective.candidate.resource_or_portfolio_id
    shared_ids = frozenset({str(o["object_id"]) for o in bank.index["objects"]})
    candidate_ids = frozenset(selected)
    profile = _growth_profile(effective.load_growth, base_load, time_axis_by_year)

    _safe_observe("base_series", 0, 1)
    base_outcome = _engine_series(
        bank=bank,
        import_service=ImportService(project),
        import_batch_id=import_batch_id,
        scenario_id=effective.run_config.scenario_id,
        run_id=resolved_run_id,
        data_version_hash=str(state.data_version_hash),
        config_hash=run_config_hash,
        software_version=software_version,
        load_by_year=base_load,
        convergence=convergence,
        year_weights=year_weights,
        deterministic_supply_by_year=None,
        portfolio_id=portfolio_id,
        shared_object_ids=shared_ids,
        candidate_object_ids=candidate_ids,
        sample_limit=sample_limit,
    )
    _safe_observe("base_series", 1, 1)
    # R2-B4 (C-29): the search anchors on the base series' REAL
    # exogenous digest; every trial recomputes its own fingerprint from
    # its actual inputs and a drift fails the search instead of being
    # masked by one shared hardcoded constant.
    solver_config = _solver_config(
        effective,
        profile,
        portfolio_id,
        time_axis_by_year,
        exogenous_fingerprint=base_outcome.exogenous_fingerprint,
    )
    reference: RiskSampleSeries = base_outcome.risk_series
    base_trial_provider = _make_trial_provider(
        bank=bank,
        project=project,
        import_batch_id=import_batch_id,
        scenario_id=effective.run_config.scenario_id,
        run_id=resolved_run_id,
        data_version_hash=str(state.data_version_hash),
        run_config_hash=run_config_hash,
        software_version=software_version,
        base_load=base_load,
        renewable=renewable,
        convergence=convergence,
        year_weights=year_weights,
        reference=reference,
        solver_config=solver_config,
        shared_ids=shared_ids,
        candidate_ids=candidate_ids,
        sample_limit=sample_limit,
    )
    # V1-T3-D seam 3: report each REAL equal-risk trial boundary; the
    # search has no reliable total upfront, so total stays None (the
    # page shows no percentage it cannot know)
    trial_counter = {"n": 0}

    def trial_provider(capacity_mw: float) -> ElccTrialOutcome:
        trial_counter["n"] += 1
        _safe_observe("trial", trial_counter["n"], None)
        outcome: ElccTrialOutcome = base_trial_provider(capacity_mw)
        return outcome

    elcc = run_elcc(
        config=solver_config,
        reference=reference,
        base_load_by_year=base_load,
        trial_provider=trial_provider,
        primary_risk_metric=effective.run_config.primary_risk_metric,
    )
    # The published base_risk must EXACTLY equal risk_metrics.base[primary]
    # (MethodResult contract). The solver records the reference-series
    # mean via np.mean, while the engine aggregate uses its online stats;
    # for sparse (non-degenerate) deficit series the two can differ in
    # the last ulp, which the strict contract rejects. The engine
    # aggregate is the authoritative metric (same one adequacy and the
    # trial evaluations publish), so align the published scalar here -
    # identical estimate, exact contract.
    base_metrics = dict(base_outcome.risk_metrics or {})
    primary = effective.run_config.primary_risk_metric.value
    if primary in base_metrics and elcc.base_risk != float(base_metrics[primary]):
        elcc = dataclasses.replace(elcc, base_risk=float(base_metrics[primary]))
    return _publish_elcc(
        project=project,
        run_service=run_service,
        resolved_run_id=resolved_run_id,
        request_id=request_id,
        created_by=created_by,
        elcc=elcc,
        base_metrics=dict(base_outcome.risk_metrics or {}),
        candidate_metrics=(dict(elcc.candidate_metrics) if elcc.candidate_metrics else None),
        matched_metrics=dict(elcc.matched_metrics) if elcc.matched_metrics else None,
        bank=bank,
        dataset_id=str(state.dataset_id),
        effective_grade=effective_grade,
        software_version=software_version,
        app_config=effective,
        base_deficit_events=list(base_outcome.deficit_events),
        base_sample_count=base_outcome.sample_count,
        candidate_member_ids=sorted(selected),
        emergency_programs_summary=_emergency_summary_payload(tables),
    )


def run_elcc_pipeline(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: ElccApplicationConfig | dict[str, Any],
    checkpoint_path: Path | str | None = None,
    observe: Any = None,
    attach_run: Any = None,
) -> dict[str, Any]:
    """Run ELCC end-to-end with a persisted lifecycle.

    The FULL normalized config is validated and frozen BEFORE create_run
    (no run side effects on invalid configs); the frozen effective
    config and its lineage hashes (method_config_hash /
    application_config_hash) enter create_run idempotency and the
    RunManifest. ``checkpoint_path`` is accepted for API symmetry but
    unused by the ELCC chain (full-sample series runs do not checkpoint).

    V1-T3-D seam 3: ``attach_run`` (optional) is called with the REAL
    run id right after create_run - BEFORE the first long computation -
    so callers can surface the formal identity mid-run; ``observe``
    (optional) receives the REAL boundaries that already exist (base
    series done, each equal-risk trial) as (phase, subtask, observed,
    total) with an honest ``total=None`` for the search (no reliable
    trial count upfront). Neither callback can change the computation:
    observer failures are isolated, and the search path is untouched.
    """
    app_config = _parse_app_config(app_config)
    project = Path(project_dir)
    (
        tables,
        time_axis_by_year,
        effective,
        _config_payload,
        app_hash,
        selected,
        bank,
        resolved_run_id,
        run_dir,
    ) = _prepare_run(
        project=project,
        import_batch_id=import_batch_id,
        data_version_id=data_version_id,
        request_id=request_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        app_config=app_config,
        attach_run=attach_run,
    )
    run_service = RunService(project, ImportService(project))
    terminal = run_service.run_state(resolved_run_id)["lifecycle_status"]
    if terminal == LifecycleStatus.FAILED.value:
        raise P1WorkflowError(
            "P1_RUN_TERMINAL_FAILED",
            "run already failed; a new computation attempt requires a new request_id",
        )
    if terminal == LifecycleStatus.COMPLETED.value:
        _verify_config_lineage(run_dir, app_hash)
        return _replay_projection(project, resolved_run_id, bank.bank_id, run_service)
    run_service.transition(
        request_id=f"{request_id}-queue",
        run_id=resolved_run_id,
        target_lifecycle=LifecycleStatus.QUEUED.value,
    )
    run_service.transition(
        request_id=f"{request_id}-start",
        run_id=resolved_run_id,
        target_lifecycle=LifecycleStatus.RUNNING.value,
    )
    try:
        projection = _run_computation(
            project=project,
            run_service=run_service,
            resolved_run_id=resolved_run_id,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            import_batch_id=import_batch_id,
            tables=tables,
            time_axis_by_year=time_axis_by_year,
            effective=effective,
            selected=selected,
            bank=bank,
            run_config_hash=_published_run_config_hash(run_dir),
            observe=observe,
        )
    except BaseException:
        try:
            run_service.transition(
                request_id=f"{request_id}-fail",
                run_id=resolved_run_id,
                target_lifecycle=LifecycleStatus.FAILED.value,
            )
        except Exception:  # noqa: BLE001, S110 - best-effort failure marking
            pass
        raise
    return projection


def _published_run_config_hash(run_dir: Path) -> str:
    """run_config_hash from the published run manifest (single source of
    truth; never recomputed with a different serialization)."""
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    return str(manifest["run_config_hash"])


def _verify_config_lineage(run_dir: Path, application_config_hash: str) -> None:
    """Completed-replay verification of the FULL config lineage:

    - the frozen elcc_config.json must exist and its hash must equal
      the manifest's application_config_hash (and the current request);
    - run_config.yaml hash must equal the manifest's run_config_hash;
    - the method config embedded in run_config.yaml must hash to the
      manifest's method_config_hash.
    External tampering of any link blocks the replay."""
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "run_manifest.json missing for a completed run",
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config_path = run_dir / ELCC_CONFIG_FILE
    if not config_path.exists():
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "frozen elcc_config.json missing for a completed run",
        )
    payload = config_path.read_text(encoding="utf-8").rstrip("\n")
    actual_app = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if actual_app != application_config_hash:
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "frozen elcc_config.json hash does not match the request",
        )
    if manifest.get("application_config_hash") != application_config_hash:
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "run_manifest application_config_hash does not match the frozen elcc_config.json",
        )
    run_config_path = run_dir / "run_config.yaml"
    if not run_config_path.exists():
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "run_config.yaml missing for a completed run",
        )
    import hashlib as _hashlib

    run_config_bytes = run_config_path.read_bytes()
    if _hashlib.sha256(run_config_bytes).hexdigest() != manifest.get("run_config_hash"):
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "run_config.yaml hash does not match the run manifest",
        )
    import yaml

    run_config = yaml.safe_load(run_config_bytes.decode("utf-8"))
    method_config = run_config.get("method_config")
    if method_config is None:
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "run_config.yaml carries no method_config for an elcc run",
        )
    method_payload = json.dumps(method_config, sort_keys=True, ensure_ascii=False)
    actual_method = _hashlib.sha256(method_payload.encode("utf-8")).hexdigest()
    if actual_method != manifest.get("method_config_hash"):
        raise P1WorkflowError(
            P1_ELCC_INVALID_CONFIG,
            "method config embedded in run_config.yaml does not match "
            "the manifest method_config_hash",
        )


def _year_weights_from_tables(tables: dict[str, Any], config: Any) -> dict[int, float]:
    """Weather-year weights from the frozen weight-set table (single
    authoritative source; no runtime override)."""

    weights_table = tables["weather_year_weight_sets"]
    rows = weights_table[
        weights_table["weight_set_id"].astype(str) == config.weather_year_weight_set_id
    ]
    if rows.empty:
        raise P1WorkflowError(
            "P1_WEIGHT_SET_MISSING",
            f"weight_set_id {config.weather_year_weight_set_id!r} not found",
        )
    weights: dict[int, float] = {}
    for _, row in rows.iterrows():
        year = int(float(row["weather_year"]))
        weights[year] = float(row["weight"])
    return weights


def _publish_elcc(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    request_id: str,
    created_by: str,
    elcc: Any,
    base_metrics: dict[str, float],
    candidate_metrics: dict[str, float] | None,
    matched_metrics: dict[str, float] | None,
    bank: Any,
    dataset_id: str,
    effective_grade: str,
    software_version: str,
    app_config: ElccApplicationConfig,
    base_deficit_events: list[dict[str, Any]] | None = None,
    base_sample_count: int | None = None,
    candidate_member_ids: list[str] | None = None,
    emergency_programs_summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from capacity_credit.contracts.enums import (
        AllocationType,
        CapacityValueType,
    )
    from capacity_credit.contracts.results import ScenarioBirthCertificate

    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    # zero-sample runs never become a MethodResult; any sampled ELCC
    # (converged or not) publishes a MethodResult with null capacity
    # fields, the bracket, the search trace and diagnostics
    zero = elcc.sample_count == 0
    mode = "zero" if zero else "samples"
    result_id, artifact_manifest_id = reserve_publish_ids(
        run_dir=run_dir,
        request_id=request_id,
        sample_count=elcc.sample_count,
        mode=mode,
    )
    if zero:
        _write_result_link(project, resolved_run_id, request_id, None, None)
    else:
        if result_id is None or artifact_manifest_id is None:
            raise P1WorkflowError(
                "P1_PUBLISH_ID_MISSING",
                "pre-allocated result ids missing for a sample run",
            )
        # Round 9: upstream diagnostics come from the OPENED, frozen
        # ScenarioBank (object resource-model diagnostics + the
        # SINGLE_WEATHER_YEAR coverage warning from the
        # probability-baseline year count). The workflow never
        # re-guesses thermal parameters, and never adds upstream codes
        # to the search trace.
        from capacity_credit.contracts.elcc_diagnostics import (
            collect_upstream_diagnostics,
        )

        upstream = collect_upstream_diagnostics(
            object_entries=list(bank.index["objects"]),
            probability_baseline_years=[
                int(year) for year in bank.index["probability_baseline_years"]
            ],
        )
        # MR-02 (A2 方案一): a run WITHOUT a calibrated-baseline closure
        # honestly declares it (registry-validated diagnostic); a run
        # WITH one has already passed the workflow's ref validation
        if app_config.calibrated_baseline_ref is None:
            upstream.append({"code": "BASELINE_NOT_CALIBRATED", "severity": "info"})
        # R6/MR-03: a FORMAL result without a PASSED seed-replication
        # gate bound to its run dir says so (teaching chains are exempt
        # by adjudication and keep *_CI_NOT_AVAILABLE instead)
        run_mode_value = getattr(
            app_config.run_config.run_mode, "value", str(app_config.run_config.run_mode)
        )
        if run_mode_value == "formal":
            from capacity_credit.application.result_service import (
                read_seed_replication_summary,
            )

            replication = read_seed_replication_summary(run_dir)
            if replication is None or not replication.get("gate", {}).get("passed"):
                upstream.append(
                    {"code": "SEED_REPLICATION_GATE_MISSING", "severity": "info"}
                )
        result = build_elcc_method_result(
            run_id=uuid.UUID(resolved_run_id),
            result_id=uuid.UUID(result_id),
            resource_or_portfolio_id=app_config.candidate.resource_or_portfolio_id,
            elcc=elcc,
            base_metrics=base_metrics,
            candidate_metrics=candidate_metrics,
            matched_metrics=matched_metrics,
            scenario_bank_id=bank.bank_id,
            effective_data_grade=DataGrade(effective_grade),
            software_version=software_version,
            method_profile=app_config.run_config.method_profile,
            capacity_value_type=CapacityValueType(app_config.run_config.capacity_value_type.value),
            baseline_portfolio_version=app_config.run_config.baseline_portfolio_version,
            allocation_type=AllocationType(app_config.run_config.allocation_type.value),
            # R3-B3: the four scenario design variables (方法说明第四章(三))
            # travel WITH the result as its birth certificate
            scenario_birth_certificate=ScenarioBirthCertificate(
                caliber=app_config.run_config.capacity_value_type.value,
                increment_mw=app_config.candidate.reference_capacity_mw,
                placement="system_wide",
                composition_mode=(
                    "combination" if len(candidate_member_ids or ()) > 1 else "individual"
                ),
                member_ids=list(candidate_member_ids or ()),
            ),
            allocation_rule_id=app_config.run_config.allocation_rule_id,
            primary_risk_metric=app_config.run_config.primary_risk_metric,
            reference_capacity_mw=app_config.candidate.reference_capacity_mw,
            confidence_level=app_config.search.confidence_level,
            upstream_diagnostics=upstream,
        )
        result_path = run_dir / "method_result.json"
        write_method_result(result, result_path)
        trace_path = _write_search_trace(
            elcc, run_dir, app_config, final_result_status=result.result_status
        )
        manifest = _build_elcc_manifest(
            run_id=resolved_run_id,
            result_id=result_id,
            artifact_manifest_id=artifact_manifest_id,
            result_path=result_path,
            trace_path=trace_path,
            generated_by=created_by,
            producer_service="capacity_credit.application.elcc_workflow",
        )
        validate_manifest_before_publish(
            manifest,
            result_path=result_path,
            expected_run_id=resolved_run_id,
            expected_result_id=result_id,
            expected_manifest_id=artifact_manifest_id,
        )
        write_artifact_manifest(
            manifest,
            run_dir / "artifact_manifest.json",
            result_path=result_path,
            expected_run_id=resolved_run_id,
            expected_result_id=result_id,
            expected_manifest_id=artifact_manifest_id,
        )
        # R3-B1: the ELCC BASE series' deficit events persist as run-dir
        # evidence (the convergence_evidence.json pattern) - the gap
        # picture (方法说明第三章(六)) becomes a real artifact instead
        # of a discarded-by-product; empty events write an honest zero
        from capacity_credit.application.result_service import (
            write_assumption_register,
            write_gap_evidence,
        )

        write_gap_evidence(
            run_dir,
            list(base_deficit_events or ()),
            source="elcc_base_series",
            sample_count=base_sample_count,
        )
        # R3-B2: the assumption register (方法说明第八章 风险3) carries
        # the FROZEN effective config VERBATIM plus the result's own
        # diagnostics and the version-controlled method-boundary list
        write_assumption_register(
            run_dir,
            run_config_payload=app_config.model_dump(mode="json"),
            result=result,
            emergency_programs=emergency_programs_summary,
        )
        _write_result_link(
            project,
            resolved_run_id,
            request_id,
            result_id,
            artifact_manifest_id,
        )
    run_service.transition(
        request_id=f"{request_id}-complete",
        run_id=resolved_run_id,
        target_lifecycle=LifecycleStatus.COMPLETED.value,
    )
    return _replay_projection(project, resolved_run_id, bank.bank_id, run_service)


def _write_search_trace(
    elcc: Any,
    run_dir: Path,
    app_config: ElccApplicationConfig,
    *,
    final_result_status: ResultStatus,
) -> Path:
    """Publish the FULL search trace through the strict typed contract
    (final summary + points + bound decisions + complete monotonicity
    ledger + diagnostics + sample-set/generation identity) and verify
    the read-back.

    result_status is the FINAL MethodResult status (upstream warnings
    may promote valid -> valid_with_warning); the formal T23-A machine
    writer binds the trace status to the final result status strictly,
    so a trace recorded from the raw search status would be rejected.
    risk_convergence / search_convergence stay the formal solver
    states."""
    canonical = (
        elcc.trace[-1].canonical_sample_set_identity
        if elcc.trace
        else (elcc.primary_risk_metric or "unknown")
    )
    sample_set = elcc.trace[-1].sample_set_identity if elcc.trace else "unknown"
    from capacity_credit.domain.risk_units import risk_metric_unit

    artifact = build_search_trace(
        primary_risk_metric=elcc.primary_risk_metric,
        risk_metric_unit=risk_metric_unit(app_config.run_config.primary_risk_metric),
        estimate_mw=elcc.estimate_mw,
        bracket_low_mw=elcc.bracket_low_mw,
        bracket_high_mw=elcc.bracket_high_mw,
        result_status=final_result_status,
        risk_convergence=elcc.risk_convergence,
        search_convergence=elcc.search_convergence,
        load_growth_mode=elcc.load_growth_mode,
        sample_set_identity=sample_set,
        canonical_sample_set_identity=canonical,
        diagnostics=list(elcc.diagnostics),
        trace=list(elcc.trace),
        bound_decisions=list(elcc.bound_decisions),
        monotonicity_ledger=list(elcc.monotonicity_ledger),
    )
    return write_search_trace(artifact, run_dir / SEARCH_TRACE_FILE)


def _build_elcc_manifest(
    *,
    run_id: str,
    result_id: str,
    artifact_manifest_id: str,
    result_path: Path,
    trace_path: Path,
    generated_by: str,
    producer_service: str,
) -> Any:
    from datetime import datetime

    from capacity_credit.application.result_service import _file_sha256
    from capacity_credit.contracts.artifacts import (
        ArtifactEntry,
        ArtifactManifest,
    )
    from capacity_credit.contracts.enums import (
        ArtifactType,
        ConfidentialityLevel,
        RedactionStatus,
    )

    return ArtifactManifest(
        artifact_schema_version="1.0",
        artifact_manifest_id=uuid.UUID(artifact_manifest_id),
        run_id=uuid.UUID(run_id),
        source_run_id=None,
        result_id=uuid.UUID(result_id),
        report_mapping_version="1.0",
        confidentiality_level=ConfidentialityLevel.INTERNAL,
        redaction_status=RedactionStatus.NOT_REQUIRED,
        generated_by=generated_by,
        generated_at=datetime.now(UTC),
        artifacts=[
            ArtifactEntry(
                artifact_type=ArtifactType.JSON_RESULT,
                relative_path="method_result.json",
                sha256=_file_sha256(result_path),
                media_type="application/json",
                producer_service=producer_service,
                schema_or_template_version="1.3",
            ),
            ArtifactEntry(
                artifact_type=ArtifactType.SEARCH_TRACE,
                relative_path=SEARCH_TRACE_FILE,
                sha256=_file_sha256(trace_path),
                media_type="application/json",
                producer_service=producer_service,
                schema_or_template_version="1.0",
            ),
        ],
    )


def _replay_projection(
    project: Path,
    resolved_run_id: str,
    scenario_bank_id: str,
    run_service: RunService,
) -> dict[str, Any]:
    projection = run_projection(project_dir=project, run_id=resolved_run_id)
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    result_path = run_dir / "method_result.json"
    if result_path.exists():
        result = read_method_result(result_path)
        projection.update(
            {
                "sample_count": result.sample_count,
                "risk_convergence": result.risk_convergence.value,
                "result_status": result.result_status.value,
                "accredited_capacity_mw": result.accredited_capacity_mw,
            }
        )
    else:
        projection.update(
            {
                "sample_count": 0,
                "risk_convergence": None,
                "result_status": None,
                "accredited_capacity_mw": None,
            }
        )
    projection["nonzero_sample_count"] = None
    projection["scenario_bank_id"] = scenario_bank_id
    return projection


__all__ = [
    "ELCC_CONFIG_FILE",
    "P1_ELCC_CANDIDATE_HOURLY_DUPLICATE",
    "P1_ELCC_CANDIDATE_HOURLY_EXTRA",
    "P1_ELCC_CANDIDATE_HOURLY_INVALID",
    "P1_ELCC_CANDIDATE_HOURLY_MISSING",
    "P1_ELCC_CANDIDATE_UNKNOWN",
    "P1_ELCC_CAPACITY_BASIS_MISMATCH",
    "P1_ELCC_CAPACITY_CROSS_YEAR",
    "P1_ELCC_CAPACITY_MISMATCH",
    "P1_ELCC_INVALID_CONFIG",
    "P1_ELCC_PROFILE_INVALID",
    "P1_ELCC_UPPER_BOUND_MISMATCH",
    "SEARCH_TRACE_FILE",
    "run_elcc_pipeline",
]
