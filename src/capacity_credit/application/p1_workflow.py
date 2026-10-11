"""P1 end-to-end application workflow (T09, review round 2).

Thin orchestration between the CLI and the existing services:

- imports register staged copies; ``validate`` re-reads the registered
  staging content and ``scenario build`` / ``run adequacy`` read only
  the frozen version (the mutable original source directory is never
  authoritative);
- thermal capacity comes from ``thermal_units.capacity_mw``; a
  reliability map referencing a missing class blocks
  (``P1_RELIABILITY_CLASS_MISSING``) as scenario-layer defense in depth,
  while the DQ-08 orphan check blocks invalid data at import time;
- renewable output enters the margin through ``build_renewable_series``
  grouped by (resource_group_id, scenario_id, weather_year), summed
  hour by hour across resource groups, with installed capacity looked
  up per (resource_group_id, year), on the real frozen load timestamp
  axis and the scenario-bank timezone;
- weather-year weights come from an explicit ``weight_set_id`` written
  into run_config; ``samples``/``weight_set_id`` are explicit CLI
  options with visible defaults;
- the run_id is assigned by RunService (the CLI no longer accepts a
  run id); root_seed must match the scenario bank's root_seed;
  checkpoint config_hash comes from run_config_hash; the effective data
  grade comes from the import batch's grade evidence and missing grade
  evidence blocks;
- lifecycle is persisted: created -> queued -> running -> completed via
  RunService.transition (or -> failed on any kernel/publish failure);
  the returned projection is re-read from persistent state, never
  fabricated;
- publication is immutable-or-identical: method_result.json,
  artifact_manifest.json and result_link.json are published atomically
  and never overwritten; result_id / artifact_manifest_id are
  pre-allocated per (run, request) so replays are stable;
- run_projection verifies the full closed loop (link, method_result,
  artifact manifest ids/schema/path/hash) before returning ids;
- a failed run cannot be replayed with the same request_id; a new
  computation attempt requires a new request_id (new run_id).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat as _stat
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any, Mapping, Protocol, TypedDict, runtime_checkable

import numpy as np
import pandas as pd

from capacity_credit.application.atomic_io import safe_filename_component
from capacity_credit.application.import_service import ImportService, ServiceError
from capacity_credit.application.result_service import (
    build_adequacy_method_result,
    read_artifact_manifest,
    read_method_result_any,
    read_result_link,
    reserve_publish_ids,
    validate_manifest_before_publish,
    write_artifact_manifest,
    write_method_result,
    write_result_link,
)
from capacity_credit.application.run_service import RunService
from capacity_credit.application.scenario_service import AdequacyStopped
from capacity_credit.contracts.enums import (
    AllocationType,
    CapacityValueType,
    ChronologyMode,
    DataGrade,
    LifecycleStatus,
    RiskMetric,
    RunMode,
    RunType,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.runs import RunManifest, load_run_config
from capacity_credit.data.validation import validate_standard_tables
from capacity_credit.engine.convergence import ConvergenceConfig
from capacity_credit.resources.renewable import build_renewable_series
from capacity_credit.scenarios.scenario_bank import (
    ScenarioObject,
    build_scenario_bank,
    open_scenario_bank,
)
from capacity_credit.scenarios.thermal_states import ThermalParams

RESULT_LINK_FILE = "result_link.json"

_TABLE_FILES = {
    "load_hourly": "data/load_hourly.csv",
    "thermal_units": "data/thermal_units.csv",
    "thermal_reliability_classes": "data/thermal_reliability_classes.csv",
    "thermal_unit_reliability_map": "data/thermal_unit_reliability_map.csv",
    "renewable_hourly": "data/renewable_hourly.csv",
    "renewable_capacities": "data/renewable_capacities.csv",
    "intertie_hourly": "data/intertie_hourly.csv",
    "interties": "data/interties.csv",
    "weather_year_weight_sets": "data/weather_year_weight_sets.csv",
    "storage_units": "data/storage_units.csv",
    "hydro_units": "data/hydro_units.csv",
    "hydro_hourly": "data/hydro_hourly.csv",
    "hydro_energy_budgets": "data/hydro_energy_budgets.csv",
}

# Optional standard tables: a teaching project without storage or hydro
# legitimately omits them; the import / read layer skips them (never a
# P1_MISSING_TABLE). The resource-run pipeline treats them as optional.
_OPTIONAL_TABLES = frozenset(
    {"storage_units", "hydro_units", "hydro_hourly", "hydro_energy_budgets"}
)


class P1WorkflowError(ValueError):
    """Stable P1 workflow error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _read_csv(path: Path | str, name: str) -> pd.DataFrame:
    path_obj = Path(path)
    if not path_obj.exists():
        raise P1WorkflowError("P1_MISSING_TABLE", f"missing data table {name}")
    try:
        return pd.read_csv(path_obj)
    except Exception as exc:
        raise P1WorkflowError("P1_TABLE_UNREADABLE", f"cannot read {name}: {exc}") from exc


def _read_tables(
    files: dict[str, Path],
) -> dict[str, pd.DataFrame]:
    tables: dict[str, pd.DataFrame] = {}
    for name, rel in _TABLE_FILES.items():
        path = files.get(rel)
        if path is None:
            if name in _OPTIONAL_TABLES:
                continue
            raise P1WorkflowError("P1_MISSING_TABLE", f"registered table missing: {rel}")
        tables[name] = _read_csv(path, name)
    return tables


def _validate_tables(tables: dict[str, pd.DataFrame], grade: str) -> Any:
    return validate_standard_tables(tables, declared_grade=grade)


def _emergency_summary_payload(
    tables: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    """F2: the ACTUAL enabled emergency-program list (empty = absent
    table = layer off) for the assumption register - parsed from the
    SAME frozen tables the run consumed (single fact source)."""
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


class _ResourceSeriesOutcome:
    """Engine-outcome SHAPE over the resource evidence + shared series
    (V1-T3-C): lets the resource chain publish through the SAME result
    builder and publisher as the plain chain without a second result
    construction path. Fields are read verbatim from the audited
    evidence and the attached convergence snapshot."""

    def __init__(self, evidence: Any, series: Any, result_status: str) -> None:
        self.sample_count = len(series.sample_ids)
        self.risk_metrics = dict(evidence.risk_metrics)
        self.primary_risk_metric = series.primary_risk_metric.value
        self.primary_interval = None  # no sample-interval machinery yet:
        # the honest null (never a mean-derived interval)
        self.result_status = result_status
        self.risk_convergence = series.convergence_snapshot.risk_convergence.value
        self.search_convergence = "not_applicable"
        self.diagnostics = [
            {
                "code": d.code,
                "severity": d.severity,
                "message": d.message,
            }
            for d in evidence.diagnostics
        ]


_STATUS_BY_CONVERGENCE = {
    "converged": "valid",
    "not_converged": "not_converged",
    "low_event_count": "low_resolution",
}


def run_resource_adequacy_formal(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    run_config: Any,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    observe: Any = None,
    attach_run: Any = None,
) -> dict[str, Any]:
    """V1-T3-C: the FORMAL storage-containing adequacy run for one legal
    run-config 1.2 - the SAME RunService identity, lifecycle, execution
    lock, publisher and artifact manifest as the plain chain, with the
    MethodResult 1.3 carrying the REAL storage SOC / chronology modes,
    the resource diagnostics and the series convergence status.

    Zero side effects before the gates: frozen gates, bank identity and
    seed identity run before create_run."""
    project = Path(project_dir)
    import_service = _require_frozen_gates(project, import_batch_id, data_version_id)
    tables = _read_tables(import_service.frozen_files(import_batch_id))
    weights = _year_weights(tables, run_config.weather_year_weight_set_id)
    weather_years = sorted(weights.keys())
    roles = {year: "probability_baseline" for year in weather_years}
    bank = open_scenario_bank(
        project / "artifacts" / "scenario_banks" / run_config.scenario_bank_id
    )
    if bank.bank_id != run_config.scenario_bank_id:
        raise P1WorkflowError(
            "P1_SCENARIO_MISMATCH", "scenario bank id does not match the reference"
        )
    if int(bank.index["root_seed"]) != run_config.random_seed:
        raise P1WorkflowError(
            "P1_SEED_MISMATCH",
            f"run_config.random_seed {run_config.random_seed} does not match the "
            f"scenario bank seed {bank.index['root_seed']}",
        )
    from capacity_credit.application.adequacy_config import (
        build_convergence_config,
        build_resource_run_config,
    )
    from capacity_credit.application.resource_run import run_resource_pipeline
    from capacity_credit.application.risk_series import risk_series_from_evidence

    rr_config = build_resource_run_config(
        run_config,
        scenario_bank_id=run_config.scenario_bank_id,
        root_seed=run_config.random_seed,
        timezone=str(bank.index["timezone"]),
        weather_years=weather_years,
        year_roles=roles,
        year_weights=weights,
        declared_grade=import_service.effective_data_grade(import_batch_id),
    )

    run_service = RunService(project, import_service)
    created = run_service.create_run(
        request_id=request_id,
        run_config=run_config.model_dump(mode="json"),
        import_batch_id=import_batch_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        scenario_bank_id=run_config.scenario_bank_id,
    )
    resolved_run_id = str(created["run_id"])
    from capacity_credit.application.locks import RunLock as _RunLock

    resolved_run_id: str | None = str(created["run_id"])
    # the caller's run-association seam (the runtime snapshot learns the
    # formal run id the moment it exists - mirrors the plain chain)
    if attach_run is not None:
        attach_run(resolved_run_id)
    # an already-terminal run under the same request is RE-READ, never
    # re-executed (same-request replay = query, not a new computation)
    state_now = run_service.run_state(resolved_run_id)
    if state_now["lifecycle_status"] == LifecycleStatus.COMPLETED.value:
        return run_projection(project_dir=project, run_id=resolved_run_id)
    if state_now["lifecycle_status"] in (
        LifecycleStatus.RUNNING.value,
        LifecycleStatus.QUEUED.value,
    ):
        raise P1WorkflowError(
            "P1_RESOURCE_RUN_INTERRUPTED",
            "the resource adequacy run for this request is not finished; "
            "V1 has no resume for the resource chain - use a new request",
        )
    execution_lock = _RunLock(project / "locks" / "adequacy-execution", resolved_run_id)
    if not execution_lock.acquire():
        raise P1WorkflowError(
            "P1_EXECUTION_LOCK_BUSY",
            f"another live owner executes run {resolved_run_id}",
        )
    try:
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
        evidence = run_resource_pipeline(
            bank=bank, tables=tables, config=rr_config, observe=observe
        )
        series = risk_series_from_evidence(
            evidence,
            RiskMetric(run_config.primary_risk_metric),
            bank,
            frozenset(),
            frozenset(),
            weather_years=tuple(weather_years),
            year_weights=weights,
            portfolio_id="PORT_ADEQUACY",
            convergence_config=build_convergence_config(run_config),
        )
        outcome = _ResourceSeriesOutcome(
            evidence,
            series,
            _STATUS_BY_CONVERGENCE[series.convergence_snapshot.risk_convergence.value],
        )
        return _publish_results(
            project=project,
            run_service=run_service,
            resolved_run_id=resolved_run_id,
            request_id=request_id,
            created_by=created_by,
            outcome=outcome,
            bank=bank,
            dataset_id=f"dataset-{project.name}",
            effective_grade=import_service.effective_data_grade(import_batch_id),
            software_version=software_version,
            confidence_level=build_convergence_config(run_config).confidence_level,
            chronology_mode=run_config.chronology_mode,
            storage_soc_initialization_mode=run_config.storage_soc_initialization_mode,
            soc_year_boundary_mode=run_config.soc_year_boundary_mode,
            convergence_detail=_series_chain_convergence_detail(
                series,
                build_convergence_config(run_config),
                outcome.result_status,
            ),
        )
    except BaseException:
        # mirror the plain chain: a failure after create_run lands the
        # run in the failed terminal state (never left running)
        if resolved_run_id is not None:
            try:
                run_service.transition(
                    request_id=f"{request_id}-fail",
                    run_id=resolved_run_id,
                    target_lifecycle=LifecycleStatus.FAILED.value,
                )
            except Exception:  # noqa: BLE001, S110 - the original error is
                pass  # primary; the failed transition is best-effort
        raise
    finally:
        execution_lock.release()


def _report_status(report: Any) -> str:
    return report.status if hasattr(report, "status") else ("failed" if report.errors else "passed")


def import_project(
    *,
    project_dir: Path | str,
    dataset_id: str,
    data_version_id: str,
    created_by: str,
    grade: str = "G0_teaching",
) -> dict[str, Any]:
    """Import a project directory as a teaching dataset and validate it."""
    project = Path(project_dir)
    optional_tables = (
        "storage_units.csv",
        "hydro_units.csv",
        "hydro_hourly.csv",
        "hydro_energy_budgets.csv",
    )
    source_files = {
        f"data/{name}": project / "data" / name
        for name in (
            "load_hourly.csv",
            "thermal_units.csv",
            "thermal_reliability_classes.csv",
            "thermal_unit_reliability_map.csv",
            "renewable_hourly.csv",
            "renewable_capacities.csv",
            "intertie_hourly.csv",
            "interties.csv",
            "weather_year_weight_sets.csv",
        )
    }
    for name in optional_tables:
        path = project / "data" / name
        if path.is_file():
            source_files[f"data/{name}"] = path
    tables = _read_tables(source_files)
    report = _validate_tables(tables, grade)
    validation_status = _report_status(report)
    import_service = ImportService(project)
    computed_grade = report.computed_max_grade or grade
    import_batch_id = import_service.register_import(
        dataset_id=dataset_id,
        data_version_id=data_version_id,
        files=source_files,
        validation_status=validation_status,
        created_by=created_by,
        grades={
            "declared_data_grade": grade,
            "computed_max_data_grade": computed_grade,
            "effective_data_grade": grade,
        },
    )
    return {
        "import_batch_id": import_batch_id,
        "dataset_id": dataset_id,
        "data_version_id": data_version_id,
        "validation_status": validation_status,
        "grade": grade,
    }


def validate_project(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
) -> dict[str, Any]:
    """Re-validate the REGISTERED content (never the mutable source).

    The registered/staging integrity gate (manifest identity, exact file
    set, per-file size and sha256, aggregate data_version_hash) runs
    before any table content is re-validated.
    """
    project = Path(project_dir)
    import_service = ImportService(project)
    state = import_service.current_state(import_batch_id)
    if state.data_version_id != data_version_id:
        raise P1WorkflowError("P1_VERSION_MISMATCH", "data_version_id does not match the batch")
    import_service.verify_registered_integrity(import_batch_id)
    grades = state.grades or {}
    declared = grades.get("declared_data_grade") or "G0_teaching"
    tables = _read_tables(import_service.registered_files(import_batch_id))
    report = _validate_tables(tables, declared)
    validation_status = _report_status(report)
    return {
        "import_batch_id": import_batch_id,
        "data_version_id": data_version_id,
        "validation_status": validation_status,
        "issue_count": len(report.errors) + len(report.warnings),
    }


def approve_and_freeze(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    request_id: str,
    reviewed_by: str,
) -> dict[str, Any]:
    """Explicit teaching-fixture approval and freeze (no CLI command)."""
    project = Path(project_dir)
    import_service = ImportService(project)
    import_service.approve(
        request_id=request_id,
        import_batch_id=import_batch_id,
        reviewed_by=reviewed_by,
        review_comment="teaching fixture approval (P1 synthetic data)",
    )
    frozen = import_service.freeze(
        request_id=f"{request_id}-freeze",
        import_batch_id=import_batch_id,
    )
    return {
        "import_batch_id": import_batch_id,
        "data_version_id": frozen.data_version_id,
        "data_version_hash": frozen.data_version_hash,
        "frozen": True,
        "frozen_integrity_ok": True,
    }


def _require_frozen_gates(
    project_dir: Path,
    import_batch_id: str,
    data_version_id: str,
) -> ImportService:
    import_service = ImportService(project_dir)
    state = import_service.current_state(import_batch_id)
    if state.data_version_id != data_version_id:
        raise P1WorkflowError("P1_VERSION_MISMATCH", "data_version_id does not match the batch")
    if state.validation_status != "passed":
        raise P1WorkflowError("P1_GATE_VALIDATION", "validation_status must be passed")
    if state.import_review_status != "approved":
        raise P1WorkflowError("P1_GATE_APPROVAL", "import review status must be approved")
    if state.approved_data_version_id != state.data_version_id:
        raise P1WorkflowError(
            "P1_GATE_APPROVED_VERSION_MISMATCH",
            "approved_data_version_id must equal data_version_id",
        )
    if state.data_version_status != "frozen":
        raise P1WorkflowError("P1_GATE_FROZEN", "data version must be frozen")
    if state.grades is None:
        raise P1WorkflowError("P1_GATE_GRADE", "data grades unavailable; grade evidence required")
    import_service.verify_frozen_integrity(import_batch_id)
    return import_service


def scenario_objects_from_tables(
    tables: dict[str, pd.DataFrame],
) -> list[ScenarioObject]:
    """Public scenario-layer entry: build thermal scenario objects from
    standard tables; a reliability map referencing a missing class
    blocks with ``P1_RELIABILITY_CLASS_MISSING`` (defense in depth)."""
    return _scenario_objects(tables)


def run_resource_evidence(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    run_config: Any,
    observe: Any = None,
) -> tuple[Any, Any]:
    """V1-T3-B: the constrained-resource stage over the FROZEN data for
    one legal run-config 1.2 (the same frozen input and scenario bank a
    formal run would bind). Returns the audited resource evidence plus
    the per-sample risk series built by the SHARED risk-series adapter
    (real sample ids, year weights and the attached convergence
    snapshot - never a mean-derived variance).

    ``observe`` is forwarded to the resource pipeline's per-sample
    boundary (isolated there). Every gate failure (frozen gates, bank
    identity, seed identity, weight coverage) raises BEFORE the
    pipeline runs - zero side effects."""
    from capacity_credit.application.adequacy_config import (
        build_convergence_config,
        build_resource_run_config,
    )
    from capacity_credit.application.resource_run import run_resource_pipeline
    from capacity_credit.application.risk_series import risk_series_from_evidence

    project = Path(project_dir)
    import_service = _require_frozen_gates(project, import_batch_id, data_version_id)
    tables = _read_tables(import_service.frozen_files(import_batch_id))
    weights = _year_weights(tables, run_config.weather_year_weight_set_id)
    weather_years = sorted(weights.keys())
    roles = {year: "probability_baseline" for year in weather_years}
    bank = open_scenario_bank(
        project / "artifacts" / "scenario_banks" / run_config.scenario_bank_id
    )
    if bank.bank_id != run_config.scenario_bank_id:
        raise P1WorkflowError(
            "P1_SCENARIO_MISMATCH", "scenario bank id does not match the reference"
        )
    if int(bank.index["root_seed"]) != run_config.random_seed:
        raise P1WorkflowError(
            "P1_SEED_MISMATCH",
            f"run_config.random_seed {run_config.random_seed} does not match the "
            f"scenario bank seed {bank.index['root_seed']}",
        )
    rr_config = build_resource_run_config(
        run_config,
        scenario_bank_id=run_config.scenario_bank_id,
        root_seed=run_config.random_seed,
        timezone=str(bank.index["timezone"]),
        weather_years=weather_years,
        year_roles=roles,
        year_weights=weights,
        declared_grade=import_service.effective_data_grade(import_batch_id),
    )
    evidence = run_resource_pipeline(
        bank=bank, tables=tables, config=rr_config, observe=observe
    )
    shared_ids = frozenset(
        str(sid)
        for frame, column in (
            (tables.get("storage_units"), "storage_id"),
            (tables.get("hydro_units"), "hydro_id"),
            (tables.get("renewable_capacities"), "resource_group_id"),
        )
        if frame is not None
        for sid in frame[column]
    )
    series = risk_series_from_evidence(
        evidence,
        RiskMetric(run_config.primary_risk_metric),
        bank,
        shared_ids,
        frozenset(),
        weather_years=tuple(weather_years),
        year_weights=weights,
        portfolio_id="PORT_ADEQUACY",
        convergence_config=build_convergence_config(run_config),
    )
    return evidence, series


def _scenario_objects(tables: dict[str, pd.DataFrame]) -> list[ScenarioObject]:
    classes = tables["thermal_reliability_classes"]
    mapping = tables["thermal_unit_reliability_map"]
    units = tables["thermal_units"]
    by_class: dict[str, float] = {}
    for _, row in classes.iterrows():
        by_class[str(row["reliability_class_id"])] = float(row["for_rate"])
    capacity_by_unit: dict[str, float] = {}
    for _, row in units.iterrows():
        capacity_by_unit[str(row["unit_id"])] = float(row["capacity_mw"])
    objects: list[ScenarioObject] = []
    for _, row in mapping.iterrows():
        unit_id = str(row["unit_id"])
        class_id = str(row["reliability_class_id"])
        if class_id not in by_class:
            raise P1WorkflowError(
                "P1_RELIABILITY_CLASS_MISSING",
                f"unit {unit_id} references missing reliability class {class_id}",
            )
        capacity = capacity_by_unit.get(unit_id)
        if capacity is None:
            raise P1WorkflowError("P1_UNIT_MISSING", f"unit {unit_id} not in thermal_units")
        objects.append(
            ScenarioObject(
                object_id=unit_id,
                namespace="thermal",
                capacity_mw=capacity,
                params=ThermalParams.independent_hourly(for_rate=by_class[class_id]),
            )
        )
    if not objects:
        raise P1WorkflowError("P1_NO_UNITS", "no thermal units found in the reliability map")
    return objects


def _load_by_year(tables: dict[str, pd.DataFrame]) -> dict[int, np.ndarray]:
    load = tables["load_hourly"]
    loads: dict[int, list[tuple[str, float]]] = {}
    for _, row in load.iterrows():
        timestamp = pd.Timestamp(row["timestamp"])
        loads.setdefault(timestamp.year, []).append((str(timestamp), float(row["load_mw"])))
    result: dict[int, np.ndarray] = {}
    for year, rows in loads.items():
        rows.sort(key=lambda r: r[0])
        result[year] = np.asarray([value for _, value in rows], dtype=float)
    return result


def _time_axis_by_year(
    tables: dict[str, pd.DataFrame],
) -> dict[int, pd.Series]:
    """Real frozen load timestamp axis per weather year (never
    regenerated with pd.date_range)."""
    load = tables["load_hourly"]
    axis: dict[int, pd.Series] = {}
    for year, group in load.groupby(load["weather_year"].astype(int)):
        ordered = group.sort_values("timestamp")
        axis[int(year)] = pd.Series(pd.to_datetime(ordered["timestamp"]).to_numpy())
    return axis


def _parse_capacity_table(
    tables: dict[str, pd.DataFrame],
) -> tuple[dict[tuple[str, int], float], set[str]]:
    """Parse the frozen renewable-capacity table into
    {(group, year): installed_mw} and the known group set; unparsable
    years or invalid capacity values block with stable codes."""
    capacities = tables["renewable_capacities"]
    installed: dict[tuple[str, int], float] = {}
    known: set[str] = set()
    for _, row in capacities.iterrows():
        try:
            year = int(float(row["year"]))
        except (TypeError, ValueError) as exc:
            raise P1WorkflowError(
                "P1_RENEWABLE_CAPACITY_YEAR",
                f"unparsable capacity year {row.get('year')!r}",
            ) from exc
        raw = row["installed_capacity_mw"]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not np.isfinite(float(raw))
        ):
            raise P1WorkflowError(
                "P1_RENEWABLE_CAPACITY_VALUE",
                f"invalid installed_capacity_mw {raw!r}",
            )
        capacity = float(raw)
        if capacity < 0.0:
            raise P1WorkflowError(
                "P1_RENEWABLE_CAPACITY_VALUE",
                f"negative installed_capacity_mw {capacity}",
            )
        group = str(row["resource_group_id"])
        known.add(group)
        installed[(group, year)] = capacity
    return installed, known


def _year_frame_and_groups(
    renewable: pd.DataFrame,
    year: int,
    scenario_id: str | None,
    resource_group_ids: list[str] | None,
) -> tuple[pd.DataFrame, list[str]]:
    """Resolve the effective scenario for one weather year and return
    (year frame, selected group ids). Multi-scenario years require an
    explicit scenario_id; ambiguous multi-group data without a selection
    blocks."""
    year_frame = renewable[renewable["weather_year"].astype(int) == year].copy()
    scenarios = sorted(year_frame["scenario_id"].astype(str).unique())
    if scenario_id is not None:
        if scenario_id not in scenarios:
            raise P1WorkflowError(
                "P1_RENEWABLE_SCENARIO_MISSING",
                f"scenario {scenario_id!r} has no renewable data in year {year}",
            )
        year_frame = year_frame[year_frame["scenario_id"].astype(str) == scenario_id]
    elif len(scenarios) > 1:
        raise P1WorkflowError(
            "P1_RENEWABLE_SCENARIO_AMBIGUOUS",
            f"year {year} has multiple scenarios {scenarios}; an explicit scenario_id is required",
        )
    groups = sorted(year_frame["resource_group_id"].astype(str).unique())
    if resource_group_ids is None and len(groups) > 1:
        raise P1WorkflowError(
            "P1_RENEWABLE_CANDIDATE_AMBIGUOUS",
            f"year {year} has multiple candidate resource groups "
            f"{groups}; an explicit resource_group_id/ids selection "
            "is required",
        )
    selected = groups if resource_group_ids is None else resource_group_ids
    return year_frame, selected


def _renewable_supply_by_year(
    tables: dict[str, pd.DataFrame],
    time_axis_by_year: dict[int, pd.Series],
    *,
    timezone: str,
    scenario_id: str | None,
    resource_group_ids: list[str] | None = None,
) -> dict[int, np.ndarray]:
    """Deterministic renewable output per weather year (MW), strictly
    following the generation_basis authority column via the T06 reader.

    Grouping key is (resource_group_id, scenario_id, weather_year);
    multiple resource groups (wind, solar, ...) per year are each built
    via ``build_renewable_series`` and summed hour by hour. Installed
    capacity is looked up per (resource_group_id, year). A multi-scenario
    year requires an explicit ``scenario_id``; mixing is forbidden.

    ``resource_group_ids`` (T12 candidate selection): when provided, only
    the SELECTED candidate resource groups are summed; an unknown group
    id blocks (P1_RENEWABLE_CANDIDATE_UNKNOWN). When the data contains
    multiple groups and no explicit selection is given, the result is
    ambiguous and blocked (P1_RENEWABLE_CANDIDATE_AMBIGUOUS).
    """
    renewable = tables["renewable_hourly"]
    installed, known_groups = _parse_capacity_table(tables)
    if resource_group_ids is not None:
        for group in resource_group_ids:
            if group not in known_groups:
                raise P1WorkflowError(
                    "P1_RENEWABLE_CANDIDATE_UNKNOWN",
                    f"candidate resource group {group!r} is not in the frozen renewable capacities",
                )
    supply: dict[int, np.ndarray] = {}
    for year, axis in time_axis_by_year.items():
        year_frame, selected = _year_frame_and_groups(
            renewable, year, scenario_id, resource_group_ids
        )
        total: np.ndarray | None = None
        for group in selected:
            key = (group, year)
            if key not in installed:
                raise P1WorkflowError(
                    "P1_RENEWABLE_CAPACITY_MISSING",
                    f"installed_capacity_mw missing for group {group!r} in year {year}",
                )
            group_frame = year_frame[year_frame["resource_group_id"].astype(str) == group]
            series = build_renewable_series(
                group_frame.assign(installed_capacity_mw=installed[key]),
                timezone=timezone,
                load_axis=pd.Series(axis),
            )
            values = np.asarray(series.to_numpy(), dtype=float)
            total = values if total is None else total + values
        supply[year] = total if total is not None else np.zeros(len(axis), dtype=float)
    return supply


def _year_weights(tables: dict[str, pd.DataFrame], weight_set_id: str) -> dict[int, float]:
    weights_table = tables["weather_year_weight_sets"]
    rows = weights_table[weights_table["weight_set_id"].astype(str) == weight_set_id]
    if rows.empty:
        raise P1WorkflowError(
            "P1_WEIGHT_SET_MISSING",
            f"weight_set_id {weight_set_id!r} not found",
        )
    weights: dict[int, float] = {}
    for _, row in rows.iterrows():
        year = int(float(row["weather_year"]))
        weights[year] = float(row["weight"])
    return weights


def _validate_supply_years(
    deterministic_supply_by_year: dict[int, np.ndarray],
    bank_years: list[int],
) -> None:
    bank_year_set = {int(y) for y in bank_years}
    supply_year_set = {int(y) for y in deterministic_supply_by_year}
    if supply_year_set != bank_year_set:
        missing = sorted(bank_year_set - supply_year_set)
        extra = sorted(supply_year_set - bank_year_set)
        raise P1WorkflowError(
            "P1_SUPPLY_YEAR_MISMATCH",
            "deterministic supply years must equal bank weather years "
            f"(missing {missing}, extra {extra})",
        )


def scenario_build(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    root_seed: int,
    timezone: str,
    samples: int = 30,
    weight_set_id: str = "w1",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the reproducible scenario bank from the FROZEN teaching data."""
    project = Path(project_dir)
    import_service = _require_frozen_gates(project, import_batch_id, data_version_id)
    tables = _read_tables(import_service.frozen_files(import_batch_id))
    objects = _scenario_objects(tables)
    weights = _year_weights(tables, weight_set_id)
    weather_years = sorted(weights.keys())
    roles = {year: "probability_baseline" for year in weather_years}
    # 2026-09-22 authorized gap fix: declare each year's ACTUAL hours from
    # the frozen load table so the bank's existing
    # SCENARIO_BANK_INVALID_YEAR_HOURS guard refuses a year whose data is
    # short of the calendar year (8760/8784) - before this fix the bank
    # declared expected hours that were never checked against the rows.
    # Load hours suffice: the import chain's time-axis stage already
    # requires every timestamped table to share the load table's exact
    # hour set (TIME_MISALIGNED), so load counts proxy every table.
    load_frame = tables["load_hourly"]
    actual_load_hours = (
        pd.to_numeric(load_frame["weather_year"], errors="coerce")
        .dropna()
        .astype(int)
        .value_counts()
    )
    year_hours = {
        year: int(actual_load_hours.get(year, 0)) for year in weather_years
    }
    bank = build_scenario_bank(
        root_seed=root_seed,
        objects=objects,
        samples=samples,
        weather_years=weather_years,
        year_roles=roles,
        year_hours=year_hours,
        base_dir=project / "artifacts",
        object_stream_version="1",
        timezone=timezone,
        # R01 run bookkeeping: the bank records WHICH frozen batch/version
        # prepared it (post-hash index metadata, never part of the id)
        index_metadata={
            "import_batch_id": str(import_batch_id),
            "data_version_id": str(data_version_id),
        },
    )
    return {
        "scenario_bank_id": bank.bank_id,
        "scenario_bank_dir": str(bank.directory),
        "weather_years": weather_years,
        "root_seed": root_seed,
    }


def _validate_weight_years(weights: dict[int, float], bank_years: list[int]) -> None:
    """The frozen weight set must cover exactly the scenario-bank years
    and sum to one (normalized); anything else blocks."""
    bank_year_set = {int(y) for y in bank_years}
    weight_year_set = {int(y) for y in weights}
    if weight_year_set != bank_year_set:
        missing = sorted(bank_year_set - weight_year_set)
        extra = sorted(weight_year_set - bank_year_set)
        raise P1WorkflowError(
            "P1_WEIGHT_YEAR_MISMATCH",
            "frozen weight set years must equal the scenario bank years "
            f"(missing {missing}, extra {extra})",
        )
    for year, weight in weights.items():
        if not np.isfinite(weight) or weight < 0.0:
            raise P1WorkflowError(
                "P1_WEIGHT_INVALID",
                f"weight for year {year} must be finite and non-negative",
            )
    total = float(sum(weights.values()))
    if abs(total - 1.0) > 1e-9:
        raise P1WorkflowError(
            "P1_WEIGHT_NOT_NORMALIZED",
            f"frozen weight set must sum to 1 (got {total})",
        )


def _declared_convergence_config(
    declared: Mapping[str, Any] | None,
    primary_risk_metric: str | None,
) -> Any:
    """The caller's declared convergence profile as an engine config.

    Absent declaration -> ``None`` (the engine seam then applies the
    shared provisional default, exactly as before). A declared mapping
    starts from the shared default and applies ONLY known keys (unknown
    keys are an honest P1 error, never silently dropped); the run's
    primary risk metric still wins as the anchor metric (C-night rule)."""
    from capacity_credit.engine.convergence import default_config

    if declared is None:
        return None
    if not isinstance(declared, Mapping):
        raise P1WorkflowError(
            "P1_DECLARED_CONVERGENCE_INVALID",
            "declared_convergence 必须为映射（收敛声明），收到 "
            f"{type(declared).__name__}",
        )
    base = default_config()
    if primary_risk_metric is not None:
        base = dataclasses.replace(base, primary_risk_metric=primary_risk_metric)
    known = set(dataclasses.fields(base) and base.__dataclass_fields__.keys())
    unknown = sorted(set(map(str, declared.keys())) - known)
    if unknown:
        raise P1WorkflowError(
            "P1_DECLARED_CONVERGENCE_INVALID",
            f"declared_convergence 含未知键：{'/'.join(unknown)}"
            "（收敛声明只接受引擎收敛配置的既有字段）",
        )
    overrides = {
        key: declared[key]
        for key in known
        if key in declared and key != "primary_risk_metric"
    }
    return dataclasses.replace(base, **overrides)


def _build_run_config(
    *,
    primary_risk_metric: str,
    root_seed: int,
    scenario_bank_id: str,
    weight_set_id: str,
    scenario_id: str | None,
    run_mode: str = RunMode.TEACHING.value,
) -> dict[str, Any]:
    return {
        "run_config_schema_version": "1.0",
        "run_mode": run_mode,
        "run_type": RunType.ADEQUACY.value,
        "source_run_id": None,
        "parent_run_ids": [],
        "method_profile": "adequacy_sequential",
        "primary_risk_metric": primary_risk_metric,
        "weather_year_weight_set_id": weight_set_id,
        "chronology_mode": ChronologyMode.INDEPENDENT_WEATHER_YEARS.value,
        "storage_soc_initialization_mode": (StorageSocInitializationMode.NOT_APPLICABLE.value),
        "soc_year_boundary_mode": SocYearBoundaryMode.NOT_APPLICABLE.value,
        "assessment_anchor": None,
        "perturbation_dimension": None,
        "normalization_basis_mwh_per_mw": None,
        "capacity_value_type": CapacityValueType.NOT_APPLICABLE.value,
        "baseline_portfolio_version": None,
        "allocation_type": AllocationType.NONE.value,
        "allocation_rule_id": None,
        "random_seed": root_seed,
        "scenario_bank_id": scenario_bank_id,
        "scenario_id": scenario_id,
    }


def _result_link_path(project_dir: Path, run_id: str) -> Path:
    return project_dir / "runs" / safe_filename_component(run_id) / RESULT_LINK_FILE


_TEST_HOOK_FILE = "batch_limit_test_hook.json"


def _mark_test_hook(project_dir: Path, run_id: str, request_id: str) -> None:
    """Record that a run was produced by the private zero-batch test
    hook so the public pipeline can refuse to replay it."""
    from capacity_credit.application.atomic_io import atomic_write_object

    path = project_dir / "runs" / safe_filename_component(run_id) / _TEST_HOOK_FILE
    atomic_write_object(
        path,
        {"hook": "max_batches", "run_id": run_id, "request_id": request_id},
        immutable=False,
    )


def _reject_test_hook_replay(project_dir: Path, run_id: str) -> None:
    """The public pipeline must never replay a zero-batch hook result as
    if it were a full computation."""
    path = project_dir / "runs" / safe_filename_component(run_id) / _TEST_HOOK_FILE
    if path.exists():
        raise P1WorkflowError(
            "P1_ZERO_BATCH_HOOK_RESULT",
            "this run was produced by the private zero-batch test hook; "
            "the public pipeline refuses to reuse it - run with a new "
            "request_id",
        )


def _write_result_link(
    project_dir: Path,
    run_id: str,
    request_id: str,
    result_id: str | None,
    artifact_manifest_id: str | None,
) -> None:
    """Publish the result link via the strict ResultLink contract
    (immutable-or-identical; invalid combos rejected)."""
    path = _result_link_path(project_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_result_link(
        path,
        run_id=uuid.UUID(run_id),
        request_id=request_id,
        result_id=uuid.UUID(result_id) if result_id else None,
        artifact_manifest_id=(uuid.UUID(artifact_manifest_id) if artifact_manifest_id else None),
    )


def run_adequacy_pipeline(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    scenario_bank_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    root_seed: int,
    primary_risk_metric: str = RiskMetric.LOLE_DAYS_PER_YEAR.value,
    checkpoint_path: Path | str | None = None,
    weight_set_id: str = "w1",
    scenario_id: str | None = None,
    run_mode: str = RunMode.TEACHING.value,
    declared_convergence: Mapping[str, Any] | None = None,
    stop_check: Any = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Run adequacy end-to-end with a persisted lifecycle.

    Production entry point: every calculation-affecting input is bound
    through ``run_config`` (scenario_id, weight_set_id, primary risk
    metric, root seed, scenario bank); weather-year weights always come
    from the frozen ``weather_year_weight_sets`` table selected by
    ``run_config.weather_year_weight_set_id`` - there is no runtime
    override and no batch-limit hook. The pipeline always executes the
    full computation.

    ``declared_convergence`` is the caller's EXPLICIT convergence
    declaration (F4/C5 parity with the elcc/efc builders): declared keys
    override the shared provisional default_config(), unknown keys are
    refused, and absent -> the engine default applies unchanged. It is
    never persisted into the 1.0 run_config (a resumed run replays with
    the shared default - recorded gap until the contract grows the
    section).

    ``run_mode`` is the REAL mode the caller chose (V1-T2-C): "formal"
    runs the strict RunService formal gates and never sends a teaching
    override; "teaching" keeps the original relaxation semantics. An
    invalid value is rejected here - never silently rewritten.

    T25 control seams: ``stop_check`` is consulted at adequacy BATCH
    BOUNDARIES (after the batch checkpoint is atomically saved); a safe
    stop transitions running -> stopping -> cancelled and never
    publishes a ``MethodResult``. ``progress`` receives read-only stage
    projections (e.g. the resolved run_id for the worker association).

    The returned projection is re-read from persistent state after the
    completed transition; ``lifecycle_status`` is never fabricated.
    """
    if run_mode not in (RunMode.FORMAL.value, RunMode.TEACHING.value):
        raise P1WorkflowError(
            "P1_RUN_MODE_INVALID",
            f"run_mode {run_mode!r} is not a contract value the adequacy "
            "pipeline accepts (formal | teaching)",
        )
    return _run_adequacy_pipeline_impl(
        progress=_wrap_progress_isolated(progress),
        project_dir=project_dir,
        import_batch_id=import_batch_id,
        data_version_id=data_version_id,
        scenario_bank_id=scenario_bank_id,
        request_id=request_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        root_seed=root_seed,
        primary_risk_metric=primary_risk_metric,
        checkpoint_path=checkpoint_path,
        weight_set_id=weight_set_id,
        scenario_id=scenario_id,
        run_mode=run_mode,
        declared_convergence=declared_convergence,
        _max_batches=None,
        stop_check=stop_check,
    )


def _run_adequacy_pipeline_impl(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    scenario_bank_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    root_seed: int,
    primary_risk_metric: str = RiskMetric.LOLE_DAYS_PER_YEAR.value,
    checkpoint_path: Path | str | None = None,
    weight_set_id: str = "w1",
    scenario_id: str | None = None,
    run_mode: str = RunMode.TEACHING.value,
    declared_convergence: Mapping[str, Any] | None = None,
    _max_batches: int | None = None,
    stop_check: Any = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Private pipeline implementation (not exported in ``__all__``).

    ``_max_batches`` is a private test hook used only by tests that
    exercise the zero-sample lifecycle path; the public
    ``run_adequacy_pipeline`` never accepts it and always runs the full
    computation.
    """
    project = Path(project_dir)
    import_service = _require_frozen_gates(project, import_batch_id, data_version_id)
    bank = open_scenario_bank(project / "artifacts" / "scenario_banks" / scenario_bank_id)
    if bank.bank_id != scenario_bank_id:
        raise P1WorkflowError(
            "P1_SCENARIO_MISMATCH", "scenario bank id does not match the reference"
        )
    if int(bank.index["root_seed"]) != root_seed:
        raise P1WorkflowError(
            "P1_SEED_MISMATCH",
            f"root_seed {root_seed} does not match the scenario bank seed "
            f"{bank.index['root_seed']}",
        )
    run_config = _build_run_config(
        primary_risk_metric=primary_risk_metric,
        root_seed=root_seed,
        scenario_bank_id=scenario_bank_id,
        weight_set_id=weight_set_id,
        scenario_id=scenario_id,
        run_mode=run_mode,
    )
    run_service = RunService(project, import_service)
    created = run_service.create_run(
        request_id=request_id,
        run_config=run_config,
        import_batch_id=import_batch_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        scenario_bank_id=scenario_bank_id,
        # V1-T2-C: a teaching override reason exists ONLY for the teaching
        # relaxation path; a formal run never carries one
        teaching_override_reason=(
            "teaching fixture synthetic project (P1)"
            if run_mode == RunMode.TEACHING.value
            else None
        ),
    )
    resolved_run_id = str(created["run_id"])
    # T25 execution lock (adequacy-execution namespace - a DIFFERENT
    # namespace from the RunService lifecycle lock, so holding it across
    # compute AND lifecycle transitions cannot deadlock): ONE authority
    # for "this run's computation is being executed by THIS process",
    # shared by the initial pipeline and the resume pipeline.
    from capacity_credit.application.locks import RunLock as _RunLock

    execution_lock = _RunLock(project / "locks" / "adequacy-execution", resolved_run_id)
    if not execution_lock.acquire():
        raise P1WorkflowError(
            "P1_EXECUTION_LOCK_BUSY",
            f"another live owner executes run {resolved_run_id}",
        )
    try:
        return _run_adequacy_body(
            project=project,
            import_service=import_service,
            bank=bank,
            run_service=run_service,
            resolved_run_id=resolved_run_id,
            created=created,
            import_batch_id=import_batch_id,
            execution_lock=execution_lock,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            data_version_id=data_version_id,
            scenario_bank_id=scenario_bank_id,
            root_seed=root_seed,
            primary_risk_metric=primary_risk_metric,
            checkpoint_path=checkpoint_path,
            weight_set_id=weight_set_id,
            scenario_id=scenario_id,
            declared_convergence=declared_convergence,
            stop_check=stop_check,
            progress=progress,
            on_batch=_make_on_batch_bridge(progress, resolved_run_id),
            _max_batches=_max_batches,
        )
    finally:
        execution_lock.release()


def _run_adequacy_body(
    *,
    project: Path,
    import_service: Any,
    bank: Any,
    run_service: RunService,
    resolved_run_id: str,
    created: dict[str, Any],
    import_batch_id: str,
    execution_lock: Any,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    data_version_id: str,
    scenario_bank_id: str,
    root_seed: int,
    primary_risk_metric: str,
    checkpoint_path: Path | str | None,
    weight_set_id: str,
    scenario_id: str | None,
    declared_convergence: Mapping[str, Any] | None = None,
    stop_check: Any = None,
    progress: Any = None,
    on_batch: Any = None,
    _max_batches: int | None = None,
) -> dict[str, Any]:
    """The post-create body of the initial adequacy pipeline (the
    execution lock is ALREADY held by the caller and released there)."""
    terminal = run_service.run_state(resolved_run_id)["lifecycle_status"]
    if terminal == LifecycleStatus.FAILED.value:
        raise P1WorkflowError(
            "P1_RUN_TERMINAL_FAILED",
            "run already failed; a new computation attempt requires a new request_id",
        )
    if terminal == LifecycleStatus.COMPLETED.value:
        _reject_test_hook_replay(project, resolved_run_id)
        return _replay_projection(
            project=project,
            run_service=run_service,
            resolved_run_id=resolved_run_id,
            scenario_bank_id=bank.bank_id,
        )
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

    if progress is not None:
        progress(
            {
                "phase": "adequacy",
                "current_subtask": "compute",
                "run_id": resolved_run_id,
                "lifecycle_status": LifecycleStatus.RUNNING.value,
                "checkpoint_ref": str(
                    checkpoint_path
                    or (
                        project
                        / "runs"
                        / safe_filename_component(resolved_run_id)
                        / "checkpoint.json"
                    )
                ),
            }
        )
    emergency_summary = _emergency_summary_payload(
        _read_tables(import_service.frozen_files(import_batch_id))
    )
    try:
        state = import_service.current_state(import_batch_id)
        effective_grade = import_service.effective_data_grade(import_batch_id)
        outcome, config = _run_adequacy_engine(
            bank=bank,
            import_service=import_service,
            import_batch_id=import_batch_id,
            scenario_id=scenario_id,
            run_id=resolved_run_id,
            data_version_hash=str(state.data_version_hash),
            config_hash=str(created["run_config_hash"]),
            software_version=software_version,
            base_dir=project / "runs" / safe_filename_component(resolved_run_id),
            checkpoint_path=checkpoint_path
            or (project / "runs" / safe_filename_component(resolved_run_id) / "checkpoint.json"),
            weight_set_id=weight_set_id,
            primary_risk_metric=primary_risk_metric,
            config=_declared_convergence_config(
                declared_convergence, primary_risk_metric
            ),
            max_batches=_max_batches,
            stop_check=stop_check,
            on_batch=on_batch,
        )
        if _max_batches is not None:
            _mark_test_hook(project, resolved_run_id, request_id)
        projection = _publish_results(
            project=project,
            run_service=run_service,
            resolved_run_id=resolved_run_id,
            request_id=request_id,
            created_by=created_by,
            outcome=outcome,
            bank=bank,
            dataset_id=str(state.dataset_id),
            effective_grade=effective_grade,
            software_version=software_version,
            confidence_level=config.confidence_level,
            convergence_detail=_plain_chain_convergence_detail(outcome, config),
            emergency_programs_summary=emergency_summary,
        )
    except AdequacyStopped as stopped:
        # SAFE STOP at a batch boundary: the current batch is complete
        # and its checkpoint atomically saved (the checkpoint is written
        # BEFORE the stop seam fires); no MethodResult is published and
        # result_status stays null. running -> stopping -> cancelled.
        run_service.transition(
            request_id=f"{request_id}-stopping",
            run_id=resolved_run_id,
            target_lifecycle=LifecycleStatus.STOPPING.value,
        )
        run_service.transition(
            request_id=f"{request_id}-cancel",
            run_id=resolved_run_id,
            target_lifecycle=LifecycleStatus.CANCELLED.value,
        )
        if progress is not None:
            progress(
                {
                    "phase": "adequacy",
                    "current_subtask": "safe_stop",
                    "run_id": resolved_run_id,
                    "lifecycle_status": LifecycleStatus.CANCELLED.value,
                }
            )
        return {
            "run_id": resolved_run_id,
            "result_id": None,
            "lifecycle_status": LifecycleStatus.CANCELLED.value,
            "result_status": None,
            "completed_batch": stopped.completed_batch,
            "next_sample_index": stopped.next_sample_index,
            "checkpoint_path": str(stopped.checkpoint_path) if stopped.checkpoint_path else None,
            "published": False,
        }
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


def _wrap_progress_isolated(progress: Any) -> Any:
    """Observer isolation at the pipeline boundary (architecture §6.1):
    a failing progress observer NEVER changes the computation, never
    aborts the run and never pretends the algorithm failed - failures
    are contained and progress simply becomes unavailable."""
    if progress is None:
        return None

    def _safe(info: dict[str, Any]) -> None:
        try:
            progress(info)
        except Exception:  # noqa: BLE001 - progress isolation only
            return

    return _safe


def _make_on_batch_bridge(progress: Any, resolved_run_id: str) -> Any:
    """THE batch-boundary observation bridge (V1-T1-C): converts real
    batch counters from the adequacy engine into read-only progress
    projections. Progress failures are ISOLATED here - an observer
    exception never changes the computation, never aborts the run and
    never pretends the algorithm failed (architecture §6.1)."""

    def _on_batch(info: dict[str, Any]) -> None:
        if progress is None:
            return
        try:
            progress(
                {
                    "phase": "adequacy",
                    "current_subtask": "batch",
                    "run_id": resolved_run_id,
                    "completed_batches": info["completed_batches"],
                    "completed_samples": info["completed_samples"],
                    "total_batches_bound": info["total_batches_bound"],
                    "total_samples_bound": info["total_samples_bound"],
                }
            )
        except Exception:  # noqa: BLE001 - progress isolation only
            return

    return _on_batch


def _run_adequacy_engine(
    *,
    bank: Any,
    import_service: ImportService,
    import_batch_id: str,
    scenario_id: str | None,
    run_id: str,
    data_version_hash: str,
    config_hash: str,
    software_version: str,
    base_dir: Path,
    checkpoint_path: Path | str | None = None,
    weight_set_id: str = "w1",
    config: ConvergenceConfig | None = None,
    primary_risk_metric: str | None = None,
    year_weights: dict[int, float] | None = None,
    on_batch: Any = None,
    deterministic_supply_by_year: dict[int, np.ndarray] | None = None,
    max_batches: int | None = None,
    stop_check: Any = None,
) -> tuple[Any, ConvergenceConfig]:
    """Internal engine seam (test seam for unit-level overrides).

    ``year_weights`` / ``deterministic_supply_by_year`` / ``config`` /
    ``max_batches`` are internal test seams: the production pipeline
    always derives weights and renewable supply from the frozen tables.
    The convergence config is the shared provisional default WITH the
    run's declared primary risk metric (C-night: the page selection
    must reach the engine's convergence gate; a null ``config`` seam
    with no declared metric keeps the legacy LOLE default). Supply
    years are verified against the bank; weights are verified for exact
    year coverage and normalization.
    """
    from capacity_credit.application.scenario_service import run_adequacy
    from capacity_credit.engine.convergence import default_config

    tables = _read_tables(import_service.frozen_files(import_batch_id))
    load_by_year = _load_by_year(tables)
    time_axis_by_year = _time_axis_by_year(tables)
    if deterministic_supply_by_year is None:
        # Adequacy: ALL renewable groups are system supply. An explicit
        # selection (all known groups) preserves the T12 rule that
        # multi-group summation requires an explicit selection; unknown
        # groups are blocked before any run side effect.
        all_groups = sorted(
            {str(row["resource_group_id"]) for _, row in tables["renewable_capacities"].iterrows()}
        )
        deterministic_supply_by_year = _renewable_supply_by_year(
            tables,
            time_axis_by_year,
            timezone=str(bank.index["timezone"]),
            scenario_id=scenario_id,
            resource_group_ids=all_groups,
        )
    else:
        _validate_supply_years(deterministic_supply_by_year, list(bank.index["weather_years"]))
    if config is None:
        config = default_config()
        if primary_risk_metric is not None:
            config = dataclasses.replace(
                config, primary_risk_metric=primary_risk_metric
            )
    if year_weights is None:
        year_weights = _year_weights(tables, weight_set_id)
    _validate_weight_years(year_weights, list(bank.index["weather_years"]))
    outcome = run_adequacy(
        bank=bank,
        load_by_year=load_by_year,
        run_id=run_id,
        data_version_hash=data_version_hash,
        config_hash=config_hash,
        software_version=software_version,
        config=config,
        base_dir=base_dir,
        checkpoint_path=checkpoint_path,
        year_weights=year_weights,
        deterministic_supply_by_year=deterministic_supply_by_year,
        max_batches=max_batches,
        stop_check=stop_check,
        on_batch=on_batch,
    )
    return outcome, config


def _convergence_evidence_payload(
    *,
    metric: str,
    risk_convergence: str,
    result_status: str,
    criteria: dict[str, bool],
    should_stop: bool,
    stats: Any,
    nonzero_sample_count: int,
    recent_batch_means: list[float],
    config: Any,
    source: str,
    config_identity: str,
) -> dict[str, Any]:
    """The run's OWN convergence evidence (C-night/D2): the four T08
    criteria booleans plus the MEASURED precision of the selected
    primary metric, derived only from the audited engine/series objects
    and the shared OnlineStats/confidence_interval primitives (the same
    ones the gate itself uses - never a new statistical method). The
    recorded half-width is the GATE's measurement of its criterion; it
    is not a result confidence-interval claim and never fills a missing
    interval on the result."""
    from capacity_credit.engine.convergence import (
        confidence_interval as _confidence_interval,
    )
    from capacity_credit.engine.online_stats import OnlineStats as _OnlineStats

    stats = (
        stats
        if isinstance(stats, _OnlineStats)
        else _OnlineStats()
    )
    # zero-sample guard (P-K2, 2026-09-24): with fewer than 2 samples the
    # interval primitive rightly refuses; the evidence layer must NOT let
    # that crash the pipeline - the honest terminal for a zero-batch run
    # is completed-with-no-method-result (t09 contract), so the interval
    # readings stay an explicit null.
    interval = (
        None
        if stats.sample_count < 2
        else _confidence_interval(
            stats,
            confidence_level=config.confidence_level,
            use_student_t=config.use_student_t,
            normal_interval_min_samples=config.normal_interval_min_samples,
        )
    )
    mean = float(stats.mean)
    near_zero = abs(mean) <= float(config.near_zero_threshold)
    return {
        "convergence_evidence_schema_version": "1",
        "primary_risk_metric": str(metric),
        "risk_convergence": str(risk_convergence),
        "result_status": str(result_status),
        "should_stop": bool(should_stop),
        "criteria": {str(k): bool(v) for k, v in dict(criteria).items()},
        "measured": {
            "sample_count": int(stats.sample_count),
            "nonzero_sample_count": int(nonzero_sample_count),
            "primary_mean": mean,
            "interval_half_width": (
                None if interval is None else float(interval.half_width)
            ),
            # the relative reading exists only where the gate itself
            # used it; a near-zero mean is judged on the absolute
            # tolerance and stays an explicit null here
            "relative_half_width": (
                None
                if interval is None or near_zero
                else float(interval.half_width) / abs(mean)
            ),
            "recent_batch_means": [float(v) for v in recent_batch_means],
            "confidence_level": float(config.confidence_level),
        },
        "thresholds": config.to_snapshot(),
        "source": str(source),
        "config_identity": str(config_identity),
    }


def _plain_chain_convergence_detail(outcome: Any, config: Any) -> dict[str, Any]:
    """Re-derive the deterministic gate state for the plain engine
    chain's audited statistics (the same pure ``check_convergence``
    function on the same recorded stats - no new criteria logic)."""
    from capacity_credit.application.risk_series import (
        convergence_config_identity,
    )
    from capacity_credit.engine.convergence import check_convergence

    state = check_convergence(
        outcome.formal_primary_stats,
        list(outcome.recent_window),
        config,
    )
    recent = list(outcome.recent_window)[-config.recent_batch_window :]
    return _convergence_evidence_payload(
        metric=str(outcome.primary_risk_metric),
        risk_convergence=str(state.risk_convergence),
        result_status=str(outcome.result_status),
        criteria=dict(state.criteria),
        should_stop=bool(state.should_stop),
        stats=outcome.formal_primary_stats,
        nonzero_sample_count=int(outcome.nonzero_sample_count),
        recent_batch_means=recent,
        config=config,
        source="run_declared",
        config_identity=convergence_config_identity(config.to_snapshot()),
    )


def _series_chain_convergence_detail(
    series: Any, config: Any, result_status: str
) -> dict[str, Any]:
    """The resource/series chain's convergence evidence straight from
    the attached audited snapshot (its criteria/identity were produced
    by the shared gate at series construction)."""
    from capacity_credit.application.risk_series import _ordered_risk_values
    from capacity_credit.engine.online_stats import OnlineStats as _OnlineStats

    snapshot = series.convergence_snapshot
    stats = _OnlineStats()
    for value in _ordered_risk_values(series.values):
        stats.update(float(value))
    return _convergence_evidence_payload(
        metric=str(series.primary_risk_metric.value),
        risk_convergence=str(snapshot.risk_convergence.value),
        result_status=str(result_status),
        criteria=dict(snapshot.criteria),
        should_stop=bool(snapshot.should_stop),
        stats=stats,
        nonzero_sample_count=int(stats.nonzero_sample_count),
        recent_batch_means=list(snapshot.recent_batch_evidence),
        config=config,
        source=str(snapshot.source),
        config_identity=str(snapshot.config_identity),
    )


def _publish_results(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    request_id: str,
    created_by: str,
    outcome: Any,
    bank: Any,
    dataset_id: str,
    effective_grade: str,
    software_version: str,
    confidence_level: float,
    chronology_mode: Any = None,
    storage_soc_initialization_mode: Any = None,
    soc_year_boundary_mode: Any = None,
    convergence_detail: dict[str, Any] | None = None,
    emergency_programs_summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct, pre-allocate ids, strictly validate and atomically
    publish results (method_result, artifact manifest, result link).

    The optional SOC / chronology modes (V1-T3-C) carry the run config's
    REAL values for the resource chain; the plain chain keeps the
    legacy not_applicable projection.

    ``convergence_detail`` (C-night/D2) is the run's OWN convergence
    evidence (criteria booleans + measured precision), derived ONLY
    from the audited engine/series objects by
    :func:`_convergence_evidence_payload`; when present it is written
    verbatim to ``convergence_evidence.json`` next to the result - the
    same run-evidence pattern as ``search_trace.json``. Old runs and
    chains without the detail simply lack the file (an honest
    tri-state at read time), never a fabricated claim."""
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    zero = outcome.sample_count == 0 or outcome.risk_metrics is None
    mode = "zero" if zero else "samples"
    result_id, artifact_manifest_id = reserve_publish_ids(
        run_dir=run_dir,
        request_id=request_id,
        sample_count=outcome.sample_count,
        mode=mode,
    )
    if zero:
        _write_result_link(
            project,
            resolved_run_id,
            request_id,
            None,
            None,
        )
    else:
        if result_id is None or artifact_manifest_id is None:
            raise P1WorkflowError(
                "P1_PUBLISH_ID_MISSING",
                "pre-allocated result ids missing for a sample run",
            )
        result = build_adequacy_method_result(
            run_id=uuid.UUID(resolved_run_id),
            result_id=uuid.UUID(result_id),
            resource_or_portfolio_id=dataset_id,
            run=outcome,
            scenario_bank_id=bank.bank_id,
            effective_data_grade=DataGrade(effective_grade),
            software_version=software_version,
            confidence_level=confidence_level,
            chronology_mode=chronology_mode or ChronologyMode.INDEPENDENT_WEATHER_YEARS,
            storage_soc_initialization_mode=(
                storage_soc_initialization_mode
                or StorageSocInitializationMode.NOT_APPLICABLE
            ),
            soc_year_boundary_mode=(
                soc_year_boundary_mode or SocYearBoundaryMode.NOT_APPLICABLE
            ),
        )
        result_path = run_dir / "method_result.json"
        write_method_result(result, result_path)
        manifest = _build_artifact_manifest(
            run_id=resolved_run_id,
            result_id=result_id,
            artifact_manifest_id=artifact_manifest_id,
            result_path=result_path,
            generated_by=created_by,
            producer_service="capacity_credit.application.p1_workflow",
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
        if convergence_detail is not None:
            # D2: the run's convergence evidence is a run-dir artifact
            # (the search_trace.json pattern) - written BEFORE the
            # result link publishes, so a linked result always has its
            # evidence available; its absence on old runs stays honest
            from capacity_credit.application.atomic_io import atomic_write_object

            atomic_write_object(
                run_dir / "convergence_evidence.json",
                convergence_detail,
                immutable=False,
            )
        # R3-B1: the gap picture (方法说明第三章(六) 第一步交付物之三)
        # rides on the SAME run-evidence pattern - the adequacy run IS
        # the baseline run whose deficit events the picture aggregates.
        # Empty events still write an honest all-zero profile.
        from capacity_credit.application.result_service import (
            write_assumption_register,
            write_gap_evidence,
        )

        write_gap_evidence(
            run_dir,
            list(getattr(outcome, "deficit_events", ()) or ()),
            source="adequacy_run",
            sample_count=outcome.sample_count,
        )
        # SL-01: per-(sample, weather year) MONTHLY deficit-date counts
        # from the FULL untruncated event set, written as the run's own
        # artifact BEFORE the result link publishes - the seasonal
        # calibration projection reads THIS (the capped deficit_events
        # file cannot honestly feed a seasonal round)
        from capacity_credit.application.atomic_io import atomic_write_object
        from capacity_credit.application.calibration_service import (
            monthly_deficit_date_counts,
        )

        monthly_counts = monthly_deficit_date_counts(
            list(getattr(outcome, "deficit_events", ()) or ())
        )
        atomic_write_object(
            run_dir / "seasonal_dates.json",
            {
                "seasonal_dates_schema_version": "1.0",
                "sample_count": outcome.sample_count,
                "counts": {
                    key: {
                        str(month): count
                        for month, count in by_month.items()
                    }
                    for key, by_month in monthly_counts.items()
                },
            },
            immutable=False,
        )
        # W2 (weather-year plan section 3): the per-year risk metrics
        # sidecar rides the SAME run-evidence pattern - the run's OWN
        # year summaries + year weights, written BEFORE the result link
        # publishes; the export chain turns it into the optional eighth
        # machine artifact risk_metrics_by_weather_year.csv. The STORAGE
        # resource chain's outcome has no per-year summaries - no
        # sidecar there, the report view keeps its honest absence
        from capacity_credit.application.scenario_service import (
            weather_year_metrics_payload,
        )

        _weather_payload = weather_year_metrics_payload(outcome)
        if _weather_payload is not None:
            atomic_write_object(
                run_dir / "weather_year_metrics.json",
                _weather_payload,
                immutable=False,
            )
        # R3-B2: the assumption register carries the FROZEN run-config
        # document VERBATIM (run_config.yaml is the single fact source,
        # already frozen at create) plus the result's own diagnostics
        rc_path = run_dir / "run_config.yaml"
        if rc_path.is_file():
            import yaml

            try:
                frozen_rc = yaml.safe_load(
                    rc_path.read_text(encoding="utf-8")
                )
            except (OSError, yaml.YAMLError):
                frozen_rc = None
            if isinstance(frozen_rc, dict) and frozen_rc:
                write_assumption_register(
                    run_dir,
                    run_config_payload=frozen_rc,
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
    state_now = run_service.run_state(resolved_run_id)
    if state_now["lifecycle_status"] != LifecycleStatus.COMPLETED.value:
        raise P1WorkflowError(
            "P1_LIFECYCLE_INCONSISTENT",
            "persisted lifecycle differs from the completed transition",
        )
    return _replay_projection(
        project=project,
        run_service=run_service,
        resolved_run_id=resolved_run_id,
        scenario_bank_id=bank.bank_id,
    )


@runtime_checkable
class _ProjectionResult(Protocol):
    """Shared projection shape of a persisted MethodResult (1.3 or 2.0)."""

    sample_count: int
    risk_convergence: Any
    result_status: Any


@runtime_checkable
class _RunProjectionResult(Protocol):
    """Shared run/result id shape of a persisted MethodResult (1.3 or 2.0)."""

    run_id: Any
    result_id: Any


def _replay_projection(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    scenario_bank_id: str,
) -> dict[str, Any]:
    """Re-read the persisted projection (full closed-loop verification)."""
    projection = run_projection(project_dir=project, run_id=resolved_run_id)
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    result_path = run_dir / "method_result.json"
    if result_path.exists():
        result = read_method_result_any(result_path)
        if not isinstance(result, _ProjectionResult):
            raise P1WorkflowError(
                "P1_RESULT_SHAPE", "method_result does not expose the projection fields"
            )
        projection.update(
            {
                "sample_count": result.sample_count,
                "risk_convergence": result.risk_convergence.value,
                "result_status": result.result_status.value,
            }
        )
    else:
        projection.update(
            {
                "sample_count": 0,
                "risk_convergence": None,
                "result_status": None,
            }
        )
    projection["nonzero_sample_count"] = None
    projection["scenario_bank_id"] = scenario_bank_id
    return projection


def _build_artifact_manifest(
    *,
    run_id: str,
    result_id: str,
    artifact_manifest_id: str,
    result_path: Path,
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
            )
        ],
    )


def run_projection(
    *,
    project_dir: Path | str,
    run_id: str,
) -> dict[str, Any]:
    """Canonical run projection with full closed-loop verification.

    The result link is validated as the strict ResultLink contract; when
    it points at a result, the MethodResult and ArtifactManifest are
    loaded and cross-checked (run_id, result_id, artifact_manifest_id,
    schema, path, hash) before any id is returned. Any missing file,
    corrupt JSON, tampered field or id/hash mismatch is rejected.
    """
    project = Path(project_dir)
    import_service = ImportService(project)
    run_service = RunService(project, import_service)
    run_manifest_path = project / "runs" / safe_filename_component(run_id) / "run_manifest.json"
    if not run_manifest_path.is_file():
        raise P1WorkflowError("P1_RUN_MISSING", f"unknown run {run_id}")
    manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    projection = dict(manifest)
    projection["lifecycle_status"] = run_service.run_state(run_id)["lifecycle_status"]
    link_path = _result_link_path(project, run_id)
    if not link_path.exists():
        raise P1WorkflowError("P1_RESULT_LINK_MISSING", "result link missing for run")
    link = read_result_link(link_path)
    if str(link.run_id) != run_id:
        raise P1WorkflowError("P1_RESULT_LINK_INVALID", "result link run_id mismatch")
    if link.result_id is None or link.artifact_manifest_id is None:
        if link.result_id is not None or link.artifact_manifest_id is not None:
            raise P1WorkflowError(
                "P1_RESULT_LINK_INVALID",
                "result link ids must be both set or both null",
            )
        projection["result_id"] = None
        projection["artifact_manifest_ids"] = []
        return projection
    run_dir = project / "runs" / safe_filename_component(run_id)
    result_path = run_dir / "method_result.json"
    if not result_path.is_file():
        raise P1WorkflowError("P1_RESULT_MISSING", "method_result missing for linked run")
    result = read_method_result_any(result_path)
    if not isinstance(result, _RunProjectionResult):
        raise P1WorkflowError("P1_RESULT_SHAPE", "method_result does not expose the run/result ids")
    if str(result.run_id) != run_id:
        raise P1WorkflowError("P1_RESULT_RUN_MISMATCH", "method_result run_id mismatch")
    if str(result.result_id) != str(link.result_id):
        raise P1WorkflowError("P1_RESULT_ID_MISMATCH", "method_result result_id mismatch")
    manifest_path = run_dir / "artifact_manifest.json"
    if not manifest_path.is_file():
        raise P1WorkflowError("P1_MANIFEST_MISSING", "artifact manifest missing for linked run")
    artifact_manifest = read_artifact_manifest(
        manifest_path,
        result_path=result_path,
        expected_run_id=run_id,
        expected_result_id=str(link.result_id),
        expected_manifest_id=str(link.artifact_manifest_id),
    )
    projection["result_id"] = str(link.result_id)
    projection["artifact_manifest_ids"] = [str(artifact_manifest.artifact_manifest_id)]
    return projection


def resume_adequacy_pipeline(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    scenario_bank_id: str,
    run_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    root_seed: int,
    primary_risk_metric: str = RiskMetric.LOLE_DAYS_PER_YEAR.value,
    checkpoint_path: Path | str | None = None,
    weight_set_id: str = "w1",
    scenario_id: str | None = None,
    stop_check: Any = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Resume an INTERRUPTED adequacy run from its checkpoint (T25).

    Resume preconditions (all stable errors, no computation, no state
    change otherwise):

    - the run exists and its lifecycle is still ``running`` (a worker
      died; ``completed``/``failed``/``cancelled`` runs are NEVER
      resumed under the same run_id);
    - the OS ``RunLock`` is free at entry (no live owner - the
      authoritative owner evidence; a stale heartbeat proves nothing);
    - the checkpoint identity (data version hash, run config hash,
      software version, scenario bank id) is re-verified when the
      engine resumes.

    The FULL resume cycle runs UNDER the project execution lock
    (locks/adequacy-execution/<run_id> - the SAME lock the initial
    pipeline holds, and a DIFFERENT namespace from the RunService
    lifecycle lock locks/runs, so holding it across compute AND the
    publish/terminal transitions cannot deadlock): the post-acquire
    lifecycle re-check, checkpoint existence/identity verification,
    compute and checkpoint read/write, safe-stop handling,
    MethodResult/artifact/result-link publication and the
    completed/failed/cancelled transition; the lock is released in
    finally. A caller-side is_held probe (worker resume_job) is only a
    fast pre-check; THIS atomic acquire is the authoritative owner gate.
    Accumulated samples and statistics are restored from the
    checkpoint, never repeated. A terminal run is never resumed under
    the same run_id.
    """
    from capacity_credit.application.locks import RunLock

    project = Path(project_dir)
    import_service = _require_frozen_gates(project, import_batch_id, data_version_id)
    bank = open_scenario_bank(project / "artifacts" / "scenario_banks" / scenario_bank_id)
    if bank.bank_id != scenario_bank_id:
        raise P1WorkflowError(
            "P1_SCENARIO_MISMATCH", "scenario bank id does not match the reference"
        )
    if int(bank.index["root_seed"]) != root_seed:
        raise P1WorkflowError("P1_SEED_MISMATCH", "root_seed does not match the scenario bank seed")
    run_service = RunService(project, import_service)
    manifest_path = project / "runs" / safe_filename_component(run_id) / "run_manifest.json"
    if not manifest_path.is_file():
        raise P1WorkflowError("P1_RESUME_RUN_UNKNOWN", f"unknown run: {run_id}")
    manifest = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.run_id) != run_id:
        raise P1WorkflowError("P1_RESUME_RUN_UNKNOWN", "run manifest id mismatch")

    # T25 recovery-identity hardening: the expected identity is REBUILT
    # from the RESUME parameters and cross-checked against the frozen
    # run manifest - the manifest's run_config_hash is never passed to
    # the checkpoint check without proving the resume parameters
    # reproduce it.
    resumed_config = _build_run_config(
        primary_risk_metric=primary_risk_metric,
        root_seed=root_seed,
        scenario_bank_id=scenario_bank_id,
        weight_set_id=weight_set_id,
        scenario_id=scenario_id,
        # V1-T2-C: the mode comes from the FROZEN manifest - a formal run
        # reproduces its formal config hash on resume, never teaching
        run_mode=str(manifest.run_mode.value),
    )
    resumed_yaml = load_run_config(resumed_config).to_yaml()
    resumed_config_hash = hashlib.sha256(resumed_yaml.encode("utf-8")).hexdigest()
    if resumed_config_hash != str(manifest.run_config_hash):
        raise P1WorkflowError(
            "P1_RESUME_IDENTITY_MISMATCH",
            "resume parameters do not reproduce the frozen run_config_hash",
        )
    if software_version != str(manifest.software_version):
        raise P1WorkflowError(
            "P1_RESUME_IDENTITY_MISMATCH",
            f"resume software_version {software_version!r} differs from the frozen "
            f"manifest {manifest.software_version!r}",
        )
    if software_commit != str(manifest.software_commit):
        raise P1WorkflowError(
            "P1_RESUME_IDENTITY_MISMATCH",
            f"resume software_commit {software_commit!r} differs from the frozen "
            f"manifest {manifest.software_commit!r}",
        )
    lifecycle = run_service.run_state(run_id)["lifecycle_status"]
    if lifecycle != LifecycleStatus.RUNNING.value:
        raise P1WorkflowError(
            "P1_RESUME_STATE_NOT_ALLOWED",
            f"run lifecycle is {lifecycle}; only a running (worker-interrupted) run can resume",
        )
    checkpoint = (
        Path(checkpoint_path)
        if checkpoint_path
        else project / "runs" / safe_filename_component(run_id) / "checkpoint.json"
    )
    if not checkpoint.is_file():
        raise P1WorkflowError(
            "P1_RESUME_CHECKPOINT_MISSING",
            f"no checkpoint to resume from: {checkpoint}",
        )
    # THE execution lock (adequacy-execution namespace): the SAME
    # project-level lock the initial pipeline holds, and a DIFFERENT
    # namespace from the RunService lifecycle lock (locks/runs), so it can
    # be held across compute AND the publish/terminal transitions without
    # re-entrancy deadlock. A caller-side is_held probe (worker
    # resume_job) is only a fast pre-check - THIS atomic acquire is the
    # authoritative owner gate.
    lock = RunLock(project / "locks" / "adequacy-execution", run_id)
    if not lock.acquire():
        raise P1WorkflowError(
            "P1_RESUME_OWNER_ACTIVE",
            "a live owner holds the execution lock; resume refused",
        )
    try:
        return _resume_adequacy_body(
            project=project,
            import_service=import_service,
            run_service=run_service,
            manifest=manifest,
            run_id=run_id,
            checkpoint=checkpoint,
            import_batch_id=import_batch_id,
            bank=bank,
            scenario_id=scenario_id,
            weight_set_id=weight_set_id,
            primary_risk_metric=primary_risk_metric,
            software_version=software_version,
            request_id=request_id,
            created_by=created_by,
            stop_check=stop_check,
            progress=progress,
        )
    finally:
        # the execution lock covers the FULL resume cycle: post-acquire
        # lifecycle re-check, checkpoint existence/identity verification,
        # compute and checkpoint read/write, safe-stop handling,
        # MethodResult/artifact/result-link publication and the
        # completed/failed/cancelled transition
        lock.release()


def _resume_adequacy_body(
    *,
    project: Path,
    import_service: Any,
    run_service: RunService,
    manifest: RunManifest,
    run_id: str,
    checkpoint: Path,
    import_batch_id: str,
    bank: Any,
    scenario_id: str | None,
    weight_set_id: str,
    primary_risk_metric: str,
    software_version: str,
    request_id: str,
    created_by: str,
    stop_check: Any,
    progress: Any,
) -> dict[str, Any]:
    """The post-acquire body of the resume pipeline (the execution lock
    is ALREADY held by the caller and released there)."""
    from capacity_credit.application.checkpoints import CheckpointError

    # SECOND lifecycle re-check and checkpoint existence re-verification
    # UNDER the execution lock: the owner state may have changed between
    # the fast pre-checks and the authoritative acquire
    lifecycle = run_service.run_state(run_id)["lifecycle_status"]
    if lifecycle != LifecycleStatus.RUNNING.value:
        raise P1WorkflowError(
            "P1_RESUME_STATE_NOT_ALLOWED",
            f"run lifecycle is {lifecycle}; only a running (worker-interrupted) "
            "run can resume",
        )
    if not checkpoint.is_file():
        raise P1WorkflowError(
            "P1_RESUME_CHECKPOINT_MISSING",
            f"no checkpoint to resume from: {checkpoint}",
        )
    if progress is not None:
        progress(
            {
                "phase": "adequacy",
                "current_subtask": "resume",
                "run_id": run_id,
                "lifecycle_status": LifecycleStatus.RUNNING.value,
                "checkpoint_ref": str(checkpoint),
            }
        )
    stopped_info: AdequacyStopped | None = None
    identity_error: CheckpointError | None = None
    compute_error: BaseException | None = None
    data_state = None
    outcome = None
    config = None
    try:
        data_state = import_service.current_state(import_batch_id)
        assert data_state is not None  # frozen batch verified above
        outcome, config = _run_adequacy_engine(
            bank=bank,
            import_service=import_service,
            import_batch_id=import_batch_id,
            scenario_id=scenario_id,
            run_id=run_id,
            data_version_hash=str(manifest.data_version_hash),
            config_hash=str(manifest.run_config_hash),
            software_version=software_version,
            base_dir=project / "runs" / safe_filename_component(run_id),
            checkpoint_path=checkpoint,
            weight_set_id=weight_set_id,
            primary_risk_metric=primary_risk_metric,
            stop_check=stop_check,
        )
    except AdequacyStopped as stopped:
        stopped_info = stopped
    except CheckpointError as exc:
        identity_error = exc
    except BaseException as exc:  # noqa: BLE001 - re-raised after failed transition
        compute_error = exc
    if stopped_info is not None:
        run_service.transition(
            request_id=f"{request_id}-stopping",
            run_id=run_id,
            target_lifecycle=LifecycleStatus.STOPPING.value,
        )
        run_service.transition(
            request_id=f"{request_id}-cancel",
            run_id=run_id,
            target_lifecycle=LifecycleStatus.CANCELLED.value,
        )
        if progress is not None:
            progress(
                {
                    "phase": "adequacy",
                    "current_subtask": "safe_stop",
                    "run_id": run_id,
                    "lifecycle_status": LifecycleStatus.CANCELLED.value,
                }
            )
        return {
            "run_id": run_id,
            "result_id": None,
            "lifecycle_status": LifecycleStatus.CANCELLED.value,
            "result_status": None,
            "completed_batch": stopped_info.completed_batch,
            "next_sample_index": stopped_info.next_sample_index,
            "checkpoint_path": str(stopped_info.checkpoint_path)
            if stopped_info.checkpoint_path
            else None,
            "published": False,
        }
    if identity_error is not None:
        # identity mismatch - the run stays running; resume is stably
        # refused, never forced
        raise P1WorkflowError(
            "P1_RESUME_IDENTITY_MISMATCH",
            f"checkpoint identity does not match the run manifest: {identity_error}",
        ) from identity_error
    if compute_error is not None:
        run_service.transition(
            request_id=f"{request_id}-fail",
            run_id=run_id,
            target_lifecycle=LifecycleStatus.FAILED.value,
        )
        raise compute_error
    effective_grade = import_service.effective_data_grade(import_batch_id)
    assert config is not None  # compute completed successfully
    try:
        projection = _publish_results(
            project=project,
            run_service=run_service,
            resolved_run_id=run_id,
            request_id=request_id,
            created_by=created_by,
            outcome=outcome,
            bank=bank,
            dataset_id=str(data_state.dataset_id) if data_state else "",
            effective_grade=effective_grade,
            software_version=software_version,
            confidence_level=config.confidence_level,
            convergence_detail=_plain_chain_convergence_detail(outcome, config),
            emergency_programs_summary=_emergency_summary_payload(
                _read_tables(import_service.frozen_files(import_batch_id))
            ),
        )
    except BaseException:
        run_service.transition(
            request_id=f"{request_id}-fail",
            run_id=run_id,
            target_lifecycle=LifecycleStatus.FAILED.value,
        )
        raise
    if progress is not None:
        progress(
            {
                "phase": "adequacy",
                "current_subtask": "completed",
                "run_id": run_id,
                "lifecycle_status": LifecycleStatus.COMPLETED.value,
            }
        )
    return projection


# the Windows FILE_ATTRIBUTE_REPARSE_POINT bit (metadata-only): a path
# carrying it is a junction, a symlink or another reparse point, so its
# target is OUTSIDE the trusted first-level enumeration
_REPARSE_POINT_ATTRIBUTE = 0x400


def is_reparse_point(path: Path | str) -> bool:
    """THE shared junction/reparse-point metadata check (V1-T1-A pre-read
    guard): True when the path ITSELF carries the Windows reparse-point
    attribute - this covers directory junctions AND symlinks (never an
    is_symlink-only judgement). The check is metadata-only (lstat): the
    target is never followed, so callers can reject an entry BEFORE any
    external content is read. On POSIX this reduces to the symlink
    check; unreadable metadata is not a reparse point (existence and
    readability stay the caller's separate concern)."""
    try:
        entry_st = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(entry_st, "st_file_attributes", 0)
    if attributes & _REPARSE_POINT_ATTRIBUTE:
        return True
    if not hasattr(entry_st, "st_file_attributes"):
        return bool(_stat.S_ISLNK(entry_st.st_mode))
    return False


class ProjectSummary(TypedDict):
    """One read-only project row for the business workbench landing page
    (T26A-MUST-01): project identity plus the data grade / freeze /
    latest-batch state read EXCLUSIVELY through the formal ImportService
    read-only entries (never re-implemented inference); a project with a
    corrupt import batch is an invalid entry, never silently skipped."""

    project_dir: str
    name: str
    data_grade: str | None
    frozen: bool
    latest_batch_state: str | None
    created_at: str | None
    updated_at: str | None
    invalid: bool
    invalid_reason: str | None


def list_projects(workspace_root: Path | str) -> list[ProjectSummary]:
    """THE read-only project-list aggregation for the business workbench
    (T26A-MUST-01): enumerates ONLY first-level project directories of
    workspace_root, never recurses, never follows symlinks, never writes
    anything; hidden directories (".*") and non-project directories
    (without imports/) are skipped; a missing workspace_root returns an
    empty list; a project whose latest import batch is corrupt becomes
    an invalid entry (never silently dropped, never presented as a
    valid project). All business state (data grade, freeze, batch state)
    comes from the formal ImportService read-only entries. Ordering is
    deterministic (name ascending, project_dir on ties); repeated calls
    return identical results."""
    root = Path(workspace_root)
    if not root.is_dir():
        return []
    # V1-T2-B: the registry is read FIRST (a corrupt registry is an
    # honest entry error); a scanned project keeps its REGISTERED
    # display name after the first import - the UUID directory name
    # (project_ref) never replaces the user-visible name
    from capacity_credit.application.project_service import (
        ProjectServiceError as _ProjectServiceError,
    )
    from capacity_credit.application.project_service import (
        read_registry as _read_project_registry,
    )

    try:
        registry = _read_project_registry(root)
    except _ProjectServiceError as exc:
        raise P1WorkflowError("P1_REGISTRY_UNAVAILABLE", str(exc)) from None
    registry_names = {
        str(entry.get("project_ref", "")): str(
            entry.get("name") or entry.get("project_ref") or ""
        )
        for entry in registry["projects"]
    }
    results: list[ProjectSummary] = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        # V1-T1-A pre-read guard: a reparse-point entry (junction or
        # symlink) is never a project candidate - checked by metadata
        # ONLY, before anything under the entry is followed or read
        if is_reparse_point(entry):
            continue
        if not entry.is_dir():
            continue
        imports_dir = entry / "imports"
        # V1-T1-A pre-read guard: an imports directory that is ITSELF a
        # reparse point (junction/symlink) is not owned content - its
        # children are never enumerated and the junction target is never
        # read (checked before any content access)
        if is_reparse_point(imports_dir):
            continue
        if not imports_dir.is_dir():
            continue  # not a project candidate (no import batches)

        def _invalid(name: str, reason: str) -> ProjectSummary:
            return ProjectSummary(
                project_dir=name,
                name=registry_names.get(name, name),
                data_grade=None,
                frozen=False,
                latest_batch_state=None,
                created_at=None,
                updated_at=None,
                invalid=True,
                invalid_reason=reason[:300],
            )

        try:
            batch_ids = sorted(
                child.name
                for child in imports_dir.iterdir()
                # V1-T1-A pre-read guard: a reparse-point batch child is
                # skipped by metadata BEFORE its target is ever read
                if not child.name.startswith(".")
                and not is_reparse_point(child)
                and child.is_dir()
            )
            if not batch_ids:
                continue  # no import batches yet: not a project with state
            service = ImportService(entry)
            latest: tuple[str, str] | None = None  # (created_at, batch_id)
            for batch_id in batch_ids:
                safe_filename_component(batch_id)
                batch = service.get_batch(batch_id)
                created_at = str(batch.get("created_at", ""))
                if latest is None or created_at > latest[0]:
                    latest = (created_at, batch_id)
            assert latest is not None  # batch_ids non-empty
            state = service.current_state(latest[1])
            data_grade: str | None = None
            try:
                data_grade = service.effective_data_grade(latest[1])
            except ServiceError:
                data_grade = None  # grade evidence may be absent pre-freeze
            results.append(
                ProjectSummary(
                    project_dir=entry.name,
                    name=registry_names.get(entry.name, entry.name),
                    data_grade=data_grade,
                    frozen=state.data_version_status == "frozen",
                    latest_batch_state=state.data_version_status,
                    created_at=None,
                    updated_at=latest[0],
                    invalid=False,
                    invalid_reason=None,
                )
            )
        except (OSError, ValueError, ServiceError) as exc:
            results.append(_invalid(entry.name, str(exc)))
    # V1-T2-A registered-project merge: a CREATED project (from
    # project_registry.json) appears as a 待导入 summary until the real
    # import chain gives it batches
    scanned = {summary["project_dir"] for summary in results}
    for entry in registry["projects"]:
        ref = str(entry.get("project_ref", ""))
        if not ref or ref in scanned:
            continue
        results.append(
            ProjectSummary(
                project_dir=ref,
                name=str(entry.get("name", ref)),
                data_grade=None,
                frozen=False,
                latest_batch_state=None,
                created_at=str(entry.get("created_at") or "") or None,
                updated_at=None,
                invalid=False,
                invalid_reason=None,
            )
        )
    results.sort(key=lambda item: item["project_dir"])
    results.sort(key=lambda item: item["name"])
    return results


__all__ = [
    "P1WorkflowError",
    "ProjectSummary",
    "approve_and_freeze",
    "import_project",
    "is_reparse_point",
    "list_projects",
    "run_adequacy_pipeline",
    "run_projection",
    "run_resource_evidence",
    "scenario_build",
    "scenario_objects_from_tables",
    "validate_project",
]
