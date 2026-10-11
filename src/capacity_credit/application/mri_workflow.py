"""MRI/rMRI application workflow (T18).

Thin MRI/rMRI orchestrator on top of the existing components
(ScenarioBank / ImportService / RunService / resource_run /
paired_evaluation / equal-risk search / the T18 solver). Shared logic is
CALLED from the stable shared modules - ``efc_components`` (formal year
weights, per-sample weighted risk series, resource-run config) and
``mri_components`` (perturbed trial provider + perfect-capacity
provider). The workflow NEVER duplicates the ELCC/EFC pipeline.

Pipeline:

1. parse + validate the versioned MRI application config;
2. open + verify the frozen ScenarioBank (seed binding);
3. require the frozen import gates and read the standard tables;
4. create the formal run (run-config 1.1, method_config_hash from the
   normalized MRI method config);
5. compute base risk R(S,L), the perturbed-system trials for the step
   audit (SAME bank / CRN identity), and (for rmri) the perfect-capacity
   MRI denominator - all solver-computed;
6. publish MethodResult 2.0 (method=mri|rmri), the step audit and the
   artifact manifest with the RunService lifecycle;
7. every failure maps to a stable error code.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import atomic_write_bytes, safe_filename_component
from capacity_credit.application.efc_components import (
    resolve_probability_year_weights,
    resource_run_config_from,
)
from capacity_credit.application.import_service import ImportService
from capacity_credit.application.mri_components import (
    make_perfect_capacity_provider,
    make_perturbed_trial_provider,
    resolve_mri_target,
    validate_declared_duration,
)
from capacity_credit.application.p1_workflow import (
    P1WorkflowError,
    _read_tables,
    _replay_projection,
    _require_frozen_gates,
    _write_result_link,
)
from capacity_credit.application.resource_run import run_resource_pipeline
from capacity_credit.application.risk_series import risk_series_from_evidence
from capacity_credit.application.run_service import RunService
from capacity_credit.contracts.enums import (
    AllocationType,
    CapacityValueType,
    DataGrade,
    LifecycleStatus,
    Method,
    PerturbationDimension,
    ResultStatus,
    RiskConvergence,
    RmriStatus,
    Severity,
)
from capacity_credit.contracts.mri_contracts import (
    MRI_DIAGNOSTIC_SEVERITY,
    MriMethodConfig,
    MriResult2,
    RunConfig11,
)
from capacity_credit.contracts.results import Diagnostic
from capacity_credit.contracts.runs import load_run_config
from capacity_credit.scenarios.scenario_bank import open_scenario_bank
from capacity_credit.solvers.mri import (
    MriConfigError,
    MriResult,
    MriSolverConfig,
    run_mri,
)

MRI_CONFIG_FILE = "mri_config.json"

P1_MRI_INVALID_CONFIG = "P1_MRI_INVALID_CONFIG"
P1_MRI_BANK_REQUIRED = "P1_MRI_BANK_REQUIRED"
P1_MRI_SCENARIO_MISMATCH = "P1_MRI_SCENARIO_MISMATCH"
P1_MRI_SEED_MISMATCH = "P1_MRI_SEED_MISMATCH"
P1_MRI_RUN_TERMINAL_FAILED = "P1_MRI_RUN_TERMINAL_FAILED"


class MriApplicationConfig:
    """Versioned MRI application configuration (run-config 1.1).

    Lightweight container: ``run_config`` is the formal ``RunConfig11``
    (1.1), so ``to_run_config()`` is the identity and the persisted
    artifact IS the frozen run config (single source of truth)."""

    def __init__(self, run_config: RunConfig11) -> None:
        self.run_config = run_config

    @classmethod
    def from_yaml(cls, text: str) -> MriApplicationConfig:
        import yaml

        payload = yaml.safe_load(text)
        if not isinstance(payload, dict):
            raise TypeError("MRI config must be a mapping")
        run_config = load_run_config(payload)
        if not isinstance(run_config, RunConfig11):
            raise TypeError("MRI application config requires run-config 1.1")
        return cls(run_config)

    def to_json(self) -> str:
        return self.run_config.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> MriApplicationConfig:
        return cls(RunConfig11.model_validate_json(text))


def _parse_app_config(app_config: Any) -> MriApplicationConfig:
    if isinstance(app_config, MriApplicationConfig):
        return app_config
    if isinstance(app_config, dict):
        run_config = load_run_config(app_config)
        if not isinstance(run_config, RunConfig11):
            raise P1WorkflowError(
                P1_MRI_INVALID_CONFIG,
                "MRI application config requires run-config 1.1",
            )
        return MriApplicationConfig(run_config)
    if isinstance(app_config, RunConfig11):
        return MriApplicationConfig(app_config)
    raise P1WorkflowError(
        P1_MRI_INVALID_CONFIG,
        "app_config must be an MriApplicationConfig, RunConfig11 or a dict",
    )


def _open_bank(project: Path, config: RunConfig11) -> Any:
    scenario_bank_id = config.scenario_bank_id
    if not scenario_bank_id:
        raise P1WorkflowError(P1_MRI_BANK_REQUIRED, "scenario_bank_id is required for MRI")
    bank = open_scenario_bank(project / "artifacts" / "scenario_banks" / scenario_bank_id)
    if bank.bank_id != scenario_bank_id:
        raise P1WorkflowError(
            P1_MRI_SCENARIO_MISMATCH,
            "scenario bank id does not match the reference",
        )
    if int(bank.index["root_seed"]) != config.random_seed:
        raise P1WorkflowError(
            P1_MRI_SEED_MISMATCH,
            f"random_seed {config.random_seed} does not match the scenario bank seed",
        )
    return bank


def _method_config_hash(method_config: MriMethodConfig) -> str:
    payload = json.dumps(method_config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prepare_run(
    *,
    project: Path,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: MriApplicationConfig,
) -> tuple[dict[str, Any], MriApplicationConfig, Any, str, Path]:
    run_config = app_config.run_config
    bank = _open_bank(project, run_config)
    import_service = ImportService(project)
    _require_frozen_gates(project, import_batch_id, data_version_id)
    tables = _read_tables(import_service.frozen_files(import_batch_id))
    payload = app_config.to_json()

    run_service = RunService(project, import_service)
    try:
        created = run_service.create_run(
            request_id=request_id,
            run_config=run_config.model_dump(mode="json"),
            import_batch_id=import_batch_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            scenario_bank_id=run_config.scenario_bank_id,
            teaching_override_reason="teaching fixture synthetic project (T18 MRI)",
        )
    except Exception as exc:
        raise P1WorkflowError(
            P1_MRI_INVALID_CONFIG,
            f"invalid MRI config at the freeze boundary: {exc}",
        ) from exc
    resolved_run_id = str(created["run_id"])
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / MRI_CONFIG_FILE
    if config_path.exists():
        existing = config_path.read_text(encoding="utf-8").strip()
        if existing != payload:
            raise P1WorkflowError(
                P1_MRI_INVALID_CONFIG,
                "existing frozen mri_config.json differs from the current request payload",
            )
    else:
        atomic_write_bytes(
            config_path,
            (payload + "\n").encode("utf-8"),
            immutable=True,
        )
    return tables, app_config, bank, resolved_run_id, run_dir


def _solver_config(
    app_config: MriApplicationConfig,
    actual_duration_before_h: float | None = None,
) -> MriSolverConfig:
    rc = app_config.run_config
    method = rc.method_config
    # For fixed_duration the solver's physical duration is the REAL storage
    # E/P duration resolved by the workflow (the declared duration_hours is
    # only a validated declaration and never enters the solver).
    duration_hours = method.duration_hours
    if method.perturbation_dimension is PerturbationDimension.FIXED_DURATION:
        duration_hours = actual_duration_before_h
    return MriSolverConfig(
        perturbation_dimension=method.perturbation_dimension,
        difference_scheme=method.difference_scheme,
        step_mode=method.step_mode,
        base_step=method.base_step,
        step_multipliers=tuple(method.step_multipliers),
        physical_domain=method.physical_domain,
        normalization_basis_mwh_per_mw=method.normalization_basis_mwh_per_mw,
        duration_hours=duration_hours,
        custom_perturbation_vector=method.custom_perturbation_vector,
        confidence_level=0.95,
        run_type=rc.run_type.value,
        denominator_relative_se_max=method.stability_thresholds.denominator_relative_se_max,
        engineering_tolerance_capacity_mw=method.engineering_tolerance.capacity_mw,
        engineering_tolerance_energy_mwh=method.engineering_tolerance.energy_mwh,
    )


def _preflight_resource_compatibility(
    target: Any,
    method: MriMethodConfig,
) -> None:
    """PURE CONFIG PREFLIGHT (no resource simulation): resource kind x
    perturbation dimension x custom component compatibility. Called after
    target resolution and BEFORE the base ``run_resource_pipeline``; an
    incompatibility raises ``MriConfigError(MRI_RESOURCE_KIND_UNSUPPORTED)``
    so the workflow can form an invalid_method result with zero resource
    runs. Only canonical ``MRI_RESOURCE_KIND_UNSUPPORTED`` is ever used."""
    kind = target.kind
    dimension = method.perturbation_dimension
    if kind == "renewable" and dimension in (
        PerturbationDimension.ENERGY_ONLY,
        PerturbationDimension.FIXED_DURATION,
    ):
        raise MriConfigError(
            "MRI_RESOURCE_KIND_UNSUPPORTED",
            "renewable only supports power-only perturbation",
        )
    if kind == "renewable" and dimension is PerturbationDimension.CUSTOM_VECTOR:
        custom = method.custom_perturbation_vector
        if custom is not None and any(
            component.role == "energy_mwh" for component in custom.components
        ):
            raise MriConfigError(
                "MRI_RESOURCE_KIND_UNSUPPORTED",
                "renewable does not support energy_mwh custom components",
            )
    if kind == "hydro" and dimension is PerturbationDimension.FIXED_DURATION:
        raise MriConfigError(
            "MRI_RESOURCE_KIND_UNSUPPORTED",
            "fixed_duration requires a storage resource",
        )


def _publish_mri(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    request_id: str,
    created_by: str,
    mri: MriResult,
    bank: Any,
    effective_grade: str,
    software_version: str,
    app_config: MriApplicationConfig,
) -> dict[str, Any]:
    from capacity_credit.application.result_service import (
        _file_sha256,
        reserve_publish_ids,
        validate_manifest_before_publish,
        write_artifact_manifest,
        write_method_result,
    )
    from capacity_credit.contracts.artifacts import ArtifactEntry, ArtifactManifest
    from capacity_credit.contracts.enums import (
        ArtifactType,
        ConfidentialityLevel,
        RedactionStatus,
    )

    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    terminal = mri.result_status in (ResultStatus.INVALID_METHOD, ResultStatus.INVALID_DATA)
    if mri.sample_count == 0 and terminal:
        publish_mode = "terminal_method_result"
    elif mri.sample_count == 0:
        publish_mode = "zero_no_result"
    else:
        publish_mode = "sampled_result"
    result_id, artifact_manifest_id = reserve_publish_ids(
        run_dir=run_dir,
        request_id=request_id,
        sample_count=mri.sample_count,
        mode=publish_mode,
    )
    if result_id is None or artifact_manifest_id is None:
        # zero_no_result: no samples entered the method and the result is
        # NOT a structured terminal result -> no method_result published.
        _write_result_link(project, resolved_run_id, request_id, None, None)
        run_service.transition(
            request_id=f"{request_id}-complete",
            run_id=resolved_run_id,
            target_lifecycle=LifecycleStatus.COMPLETED.value,
        )
        return _replay_projection(
            project=project,
            run_service=run_service,
            resolved_run_id=resolved_run_id,
            scenario_bank_id=bank.bank_id,
        )

    rc = app_config.run_config
    method = Method.MRI if rc.run_type.value == "mri" else Method.RMRI
    result = MriResult2(
        result_id=uuid.UUID(result_id),
        run_id=uuid.UUID(resolved_run_id),
        method=method,
        method_profile=rc.method_profile,
        resource_or_portfolio_id=rc.method_config.resource_or_portfolio_id,
        capacity_value_type=CapacityValueType(rc.capacity_value_type.value),
        baseline_portfolio_version=rc.baseline_portfolio_version,
        allocation_type=AllocationType(rc.allocation_type.value),
        allocation_rule_id=rc.allocation_rule_id,
        chronology_mode=rc.chronology_mode,
        storage_soc_initialization_mode=rc.storage_soc_initialization_mode,
        soc_year_boundary_mode=rc.soc_year_boundary_mode,
        assessment_anchor=rc.assessment_anchor,
        primary_risk_metric=rc.primary_risk_metric,
        mri_value=mri.mri_value,
        mri_unit=mri.mri_unit,
        perfect_capacity_mri_value=mri.perfect_capacity_mri_value,
        rmri_value=mri.rmri_value,
        rmri_status=mri.rmri_status,
        requested_difference_scheme=mri.requested_difference_scheme,
        applied_difference_scheme=mri.applied_difference_scheme,
        normalization_basis_mwh_per_mw=rc.method_config.normalization_basis_mwh_per_mw,
        equivalent_mri_per_mw=mri.equivalent_mri_per_mw,
        base_step=mri.base_step,
        step_unit=mri.step_unit,
        step_multipliers=list(mri.step_multipliers),
        step_evaluations=list(mri.step_evaluations),
        perfect_capacity_step_evaluations=list(mri.perfect_capacity_step_evaluations),
        reference_capacity_mw=mri.reference_capacity_mw,
        accredited_capacity_mw=mri.accredited_capacity_mw,
        capacity_credit_fraction=mri.capacity_credit_fraction,
        sample_count=mri.sample_count,
        scenario_bank_id=bank.bank_id,
        result_status=mri.result_status,
        risk_convergence=mri.risk_convergence,
        effective_data_grade=DataGrade(effective_grade),
        diagnostics=[
            Diagnostic(
                code=code,
                severity=Severity(MRI_DIAGNOSTIC_SEVERITY[code]),
                message=code,
            )
            for code in mri.diagnostics
        ],
    )
    result_path = run_dir / "method_result.json"
    write_method_result(result, result_path)
    manifest = ArtifactManifest(
        artifact_schema_version="1.0",
        artifact_manifest_id=uuid.UUID(artifact_manifest_id),
        run_id=uuid.UUID(resolved_run_id),
        source_run_id=None,
        result_id=uuid.UUID(result_id),
        report_mapping_version="1.0",
        confidentiality_level=ConfidentialityLevel.INTERNAL,
        redaction_status=RedactionStatus.NOT_REQUIRED,
        generated_by=created_by,
        generated_at=datetime.now(UTC),
        artifacts=[
            ArtifactEntry(
                artifact_type=ArtifactType.JSON_RESULT,
                relative_path="method_result.json",
                sha256=_file_sha256(result_path),
                media_type="application/json",
                producer_service="capacity_credit.application.mri_workflow",
                schema_or_template_version="2.0",
            ),
        ],
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
    _write_result_link(project, resolved_run_id, request_id, result_id, artifact_manifest_id)
    run_service.transition(
        request_id=f"{request_id}-complete",
        run_id=resolved_run_id,
        target_lifecycle=LifecycleStatus.COMPLETED.value,
    )
    return _replay_projection(
        project=project,
        run_service=run_service,
        resolved_run_id=resolved_run_id,
        scenario_bank_id=bank.bank_id,
    )


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
    effective: MriApplicationConfig,
    bank: Any,
) -> dict[str, Any]:
    effective_grade = ImportService(project).effective_data_grade(import_batch_id)
    rc = effective.run_config
    method = rc.method_config
    # Full bank sample set - never a silent 1000-cap; teaching fast mode
    # is EXPLICIT via run_mode=teaching (evidence written by RunService).
    samples = int(bank.index["sample_count"])
    probability_years = tuple(sorted(int(y) for y in bank.index["probability_baseline_years"]))
    year_weights = resolve_probability_year_weights(
        tables, rc.weather_year_weight_set_id, probability_years
    )
    rrc = resource_run_config_from(
        bank,
        samples=samples,
        primary_risk_metric=rc.primary_risk_metric,
        scenario_id=rc.scenario_id,
        weather_years=probability_years,
        year_weights=year_weights,
    )
    shared_ids = frozenset({str(obj["object_id"]) for obj in bank.index["objects"]})
    metric = rc.primary_risk_metric

    # The REAL target resource and its reported capacity (candidate
    # resolution by object identity - never the perturbation dimension or
    # engineering_tolerance). fixed_duration validates the DECLARED
    # duration_hours against the real storage E/P duration BEFORE any
    # computation.
    target = resolve_mri_target(tables, method.resource_or_portfolio_id)
    if method.perturbation_dimension is PerturbationDimension.FIXED_DURATION:
        validate_declared_duration(method.duration_hours, target.actual_duration_before_h)
    # PURE CONFIG PREFLIGHT (no resource simulation): resource kind x
    # perturbation dimension x custom component compatibility is checked
    # here, BEFORE the base run_resource_pipeline. An incompatibility forms
    # an invalid_method result with zero resource simulations.
    try:
        _preflight_resource_compatibility(target, method)
    except MriConfigError as exc:
        if exc.code == "MRI_RESOURCE_KIND_UNSUPPORTED":
            from capacity_credit.domain.risk_units import mri_unit
            from capacity_credit.solvers.mri import _step_unit

            step_unit = _step_unit(_solver_config(effective))
            unit = mri_unit(metric, step_unit)
            invalid = MriResult(
                run_type=rc.run_type.value,
                mri_value=None,
                mri_unit=unit,
                equivalent_mri_per_mw=None,
                perfect_capacity_mri_value=None,
                rmri_value=None,
                rmri_status=RmriStatus.NOT_APPLICABLE,
                requested_difference_scheme=method.difference_scheme,
                applied_difference_scheme=method.difference_scheme,
                base_step=0.0,
                step_unit=step_unit,
                step_multipliers=tuple(method.step_multipliers),
                step_evaluations=(),
                perfect_capacity_step_evaluations=(),
                result_status=ResultStatus.INVALID_METHOD,
                risk_convergence=RiskConvergence.NOT_CONVERGED,
                sample_count=0,  # no samples entered the method computation
                diagnostics=("MRI_RESOURCE_KIND_UNSUPPORTED",),
            )
            return _publish_mri(
                project=project,
                run_service=run_service,
                resolved_run_id=resolved_run_id,
                request_id=request_id,
                created_by=created_by,
                mri=invalid,
                bank=bank,
                effective_grade=effective_grade,
                software_version=software_version,
                app_config=effective,
            )
        raise
    peak_load_mw = float(
        max(
            max(
                tables["load_hourly"]["load_mw"].astype(float).max(),
                0.0,
            ),
            1e-6,
        )
    )

    base_ev = run_resource_pipeline(bank=bank, tables=tables, config=rrc, run_id="mri_base")
    reference = risk_series_from_evidence(
        base_ev,
        metric,
        bank,
        shared_ids,
        frozenset(),
        probability_years,
        year_weights,
        portfolio_id="PORT_MRI",
    )
    trial_provider = make_perturbed_trial_provider(
        bank=bank,
        base_tables=tables,
        rrc=rrc,
        metric=metric,
        shared_ids=shared_ids,
        run_id=resolved_run_id,
        weather_years=probability_years,
        year_weights=year_weights,
        resource_or_portfolio_id=method.resource_or_portfolio_id,
        resource_kind=target.kind,
        perturbation_dimension=method.perturbation_dimension,
    )
    perfect_provider = make_perfect_capacity_provider(
        bank=bank,
        base_tables=tables,
        rrc=rrc,
        metric=metric,
        shared_ids=shared_ids,
        run_id=resolved_run_id,
        weather_years=probability_years,
        year_weights=year_weights,
    )
    try:
        mri = run_mri(
            config=_solver_config(effective, target.actual_duration_before_h),
            reference=reference,
            trial_provider=trial_provider,
            perfect_capacity_provider=perfect_provider if rc.run_type.value == "rmri" else None,
            primary_risk_metric=metric,
            candidate_capacity_mw=target.candidate_capacity_mw,
            candidate_energy_mwh=target.candidate_energy_mwh,
            peak_load_mw=peak_load_mw,
            reference_capacity_mw=target.candidate_capacity_mw,
        )
    except MriConfigError as exc:
        raise P1WorkflowError(
            exc.code if exc.code else "P1_MRI_SOLVER_FAILED",
            exc.message,
        ) from exc
    return _publish_mri(
        project=project,
        run_service=run_service,
        resolved_run_id=resolved_run_id,
        request_id=request_id,
        created_by=created_by,
        mri=mri,
        bank=bank,
        effective_grade=effective_grade,
        software_version=software_version,
        app_config=effective,
    )


def run_mri_pipeline(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: MriApplicationConfig | dict[str, Any] | RunConfig11,
) -> dict[str, Any]:
    """Run MRI/rMRI end-to-end with a persisted lifecycle.

    The FULL normalized config (run-config 1.1) is validated and frozen
    BEFORE create_run (no run side effects on invalid configs)."""
    app_config = _parse_app_config(app_config)
    project = Path(project_dir)
    tables, effective, bank, resolved_run_id, _run_dir = _prepare_run(
        project=project,
        import_batch_id=import_batch_id,
        data_version_id=data_version_id,
        request_id=request_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        app_config=app_config,
    )
    run_service = RunService(project, ImportService(project))
    terminal = run_service.run_state(resolved_run_id)["lifecycle_status"]
    if terminal == LifecycleStatus.FAILED.value:
        raise P1WorkflowError(
            P1_MRI_RUN_TERMINAL_FAILED,
            "run already failed; a new computation attempt requires a new request_id",
        )
    if terminal == LifecycleStatus.COMPLETED.value:
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
            effective=effective,
            bank=bank,
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


__all__ = [
    "MRI_CONFIG_FILE",
    "P1_MRI_BANK_REQUIRED",
    "P1_MRI_INVALID_CONFIG",
    "P1_MRI_RUN_TERMINAL_FAILED",
    "P1_MRI_SCENARIO_MISMATCH",
    "P1_MRI_SEED_MISMATCH",
    "MriApplicationConfig",
    "run_mri_pipeline",
]
