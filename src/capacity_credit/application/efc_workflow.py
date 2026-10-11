"""EFC application workflow (T17, round-1 remediation).

Focused EFC-only orchestration on top of the existing components
(ScenarioBank / ImportService / RunService / result_service /
resource_run / equal-risk search / paired evaluation). Shared logic is
CALLED from the stable shared modules - ``candidate_resolution``
(candidate resolution + base-system construction by object identity)
and ``efc_components`` (formal year weights, per-sample weighted risk
series, resource-run config, perfect-capacity trial provider). The
workflow NEVER imports private helpers from the ELCC workflow and never
duplicates the ELCC pipeline.

Pipeline:

1. parse + validate the typed ``EfcApplicationConfig``;
2. open + verify the frozen ScenarioBank (seed binding);
3. require the frozen import gates and read the standard tables;
4. resolve the candidate selection and the base system S by OBJECT
   IDENTITY (wind/PV, storage, hydro and combinations);
5. create the formal run (RunConfig with the typed ``EfcMethodConfig``,
   method_config_hash, application_config_hash - the RunService
   RECOMPUTES both hashes method-aware) and freeze the config;
6. compute: base system R(S,L), candidate system R(S+X,L) (the frozen
   EFC target), the S+F perfect-replacement trials (kernel trial-only
   perfect capacity) and the equal-risk search (decreasing direction);
7. publish MethodResult 1.3 (method=efc), the full search trace and the
   artifact manifest with the RunService lifecycle;
8. every failure maps to a stable error code - KeyError / ValueError /
   pandas native exceptions never leak.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import atomic_write_bytes, safe_filename_component
from capacity_credit.application.candidate_resolution import (
    P1_EFC_CANDIDATE_UNKNOWN,
    resolve_candidate_and_base_tables,
)
from capacity_credit.application.efc_components import (
    convergence_config_from,
    resolve_probability_year_weights,
    resource_run_config_from,
)
from capacity_credit.application.import_service import ImportService
from capacity_credit.application.p1_workflow import (
    P1WorkflowError,
    _read_tables,
    _replay_projection,
    _require_frozen_gates,
    _write_result_link,
)
from capacity_credit.application.result_service import (
    build_efc_method_result,
    reserve_publish_ids,
    validate_manifest_before_publish,
    write_artifact_manifest,
    write_method_result,
)
from capacity_credit.application.run_service import RunService
from capacity_credit.contracts.efc_config import EfcApplicationConfig
from capacity_credit.contracts.efc_method_config import (
    EfcCriterionWindowConfig,
    EfcMethodConfig,
)
from capacity_credit.contracts.enums import (
    AllocationType,
    CapacityValueType,
    DataGrade,
    LifecycleStatus,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.results import MethodResult
from capacity_credit.contracts.search_trace import build_search_trace, write_search_trace
from capacity_credit.scenarios.scenario_bank import open_scenario_bank
from capacity_credit.solvers.efc import (
    EfcConfigError,
    EfcResult,
    EfcSolverConfig,
)
from capacity_credit.solvers.equal_risk import EqualRiskSearchConfig

EFC_CONFIG_FILE = "efc_config.json"


def _storage_present(tables: dict[str, Any]) -> bool:
    """Whether the FROZEN tables actually contain storage units - the
    SAME table (and emptiness semantics) the resource chain consumes
    (resource_run reads ``tables['storage_units']``)."""
    storage = tables.get("storage_units")
    return storage is not None and len(storage) > 0

P1_EFC_INVALID_CONFIG = "P1_EFC_INVALID_CONFIG"
P1_EFC_BANK_REQUIRED = "P1_EFC_BANK_REQUIRED"
P1_EFC_SCENARIO_MISMATCH = "P1_EFC_SCENARIO_MISMATCH"
P1_EFC_SEED_MISMATCH = "P1_EFC_SEED_MISMATCH"
P1_EFC_UPPER_BOUND_MISMATCH = "P1_EFC_UPPER_BOUND_MISMATCH"
P1_EFC_RUN_TERMINAL_FAILED = "P1_EFC_RUN_TERMINAL_FAILED"
P1_EFC_TECHNOLOGY_CLASS_MISSING = "P1_EFC_TECHNOLOGY_CLASS_MISSING"


def _parse_app_config(app_config: EfcApplicationConfig | dict[str, Any]) -> EfcApplicationConfig:
    """Pipeline-entry config normalization with stable error mapping."""
    try:
        if isinstance(app_config, EfcApplicationConfig):
            payload = app_config.model_dump(mode="json")
        elif isinstance(app_config, dict):
            payload = app_config
        else:
            raise TypeError("app_config must be an EfcApplicationConfig or a dict")
        return EfcApplicationConfig.model_validate(payload)
    except Exception as exc:
        raise P1WorkflowError(
            P1_EFC_INVALID_CONFIG,
            f"invalid EFC config: {exc}",
        ) from exc


def _open_bank(project: Path, config: Any) -> Any:
    """Open and verify the scenario bank; the seed must match the
    frozen run config."""
    scenario_bank_id = config.scenario_bank_id
    if not scenario_bank_id:
        raise P1WorkflowError(
            P1_EFC_BANK_REQUIRED,
            "run_config.scenario_bank_id is required for EFC",
        )
    bank = open_scenario_bank(project / "artifacts" / "scenario_banks" / scenario_bank_id)
    if bank.bank_id != scenario_bank_id:
        raise P1WorkflowError(
            P1_EFC_SCENARIO_MISMATCH,
            "scenario bank id does not match the reference",
        )
    if int(bank.index["root_seed"]) != config.random_seed:
        raise P1WorkflowError(
            P1_EFC_SEED_MISMATCH,
            f"random_seed {config.random_seed} does not match the "
            f"scenario bank seed {bank.index['root_seed']}",
        )
    return bank


def _method_config_hash(app_config: EfcApplicationConfig) -> str:
    """SHA-256 of the normalized METHOD config ONLY (search + candidate
    + convergence + perfect_capacity + upper_bound + storage_soc)."""
    method = app_config.to_run_config().method_config
    assert isinstance(method, EfcMethodConfig)
    payload = json.dumps(method.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_upper_bound_mw(
    app_config: EfcApplicationConfig,
    candidate_capacity_mw: float,
) -> float:
    """Bind the configured upper-bound SOURCE to a MW value from the
    resolved candidate definition (methodology §9.4)."""
    source = app_config.upper_bound.source
    if source == "declared_mw":
        assert app_config.upper_bound.declared_mw is not None
        return float(app_config.upper_bound.declared_mw)
    if source in ("nameplate_capacity", "max_instantaneous_output_sum"):
        return float(candidate_capacity_mw)
    if source == "storage_max_discharge_power":
        return float(candidate_capacity_mw)
    raise P1WorkflowError(
        P1_EFC_UPPER_BOUND_MISMATCH,
        f"unknown upper_bound.source {source!r}",
    )


def _prepare_run(
    *,
    project: Path,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: EfcApplicationConfig,
    attach_run: Any = None,
) -> tuple[
    dict[str, Any],
    EfcApplicationConfig,
    str,
    str,
    Any,
    Any,
    str,
    Path,
    float,
]:
    """Pre-flight + create_run + frozen-config persistence (idempotent)."""
    bank = _open_bank(project, app_config.run_config)
    import_service = ImportService(project)
    _require_frozen_gates(project, import_batch_id, data_version_id)
    tables = _read_tables(import_service.frozen_files(import_batch_id))
    resolution = resolve_candidate_and_base_tables(tables, app_config.candidate)
    upper_bound_mw = _resolve_upper_bound_mw(app_config, resolution.candidate_capacity_mw)
    # MR-01.2: 类别归属认定本身是收资与核对环节 - a FORMAL EFC run must
    # declare its technology class AND record the human attestation;
    # teaching/research runs are exempt (they carry an honest
    # EFC_TECHNOLOGY_CLASS_MISSING diagnostic instead)
    reporting = app_config.reporting
    run_mode_value = getattr(
        app_config.run_config.run_mode, "value", str(app_config.run_config.run_mode)
    )
    if run_mode_value == "formal":
        from capacity_credit.contracts.elcc_method_config import (
            is_teaching_default_convergence,
        )

        if is_teaching_default_convergence(app_config.convergence):
            raise P1WorkflowError(
                "P1_CONVERGENCE_PROFILE_REQUIRED",
                "正式 EFC 运行不得携带未声明的教学级收敛默认值：请在 "
                "convergence 节显式给出生产档案（PRODUCTION_CONVERGENCE_PROFILE，"
                "provisional）或真实自定义值，拒绝创建",
            )
    if run_mode_value == "formal" and (
        reporting is None or not reporting.class_attested_by.strip()
    ):
        raise P1WorkflowError(
            P1_EFC_TECHNOLOGY_CLASS_MISSING,
            "正式 EFC 运行必须声明 technology_class 并完成类别归属认定"
            "（reporting.class_attested_by 记录认定人），拒绝创建（MR-01.2）",
        )
    normalized = app_config.model_dump(mode="json")
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    app_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    run_service = RunService(project, import_service)
    try:
        full_run_config = app_config.to_run_config().model_dump(mode="json")
        created = run_service.create_run(
            request_id=request_id,
            run_config=full_run_config,
            import_batch_id=import_batch_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            scenario_bank_id=app_config.run_config.scenario_bank_id,
            teaching_override_reason="teaching fixture synthetic project (P4 EFC)",
            method_config_hash=_method_config_hash(app_config),
            application_config_hash=app_hash,
        )
    except Exception as exc:
        raise P1WorkflowError(
            P1_EFC_INVALID_CONFIG,
            f"invalid EFC config at the freeze boundary: {exc}",
        ) from exc
    resolved_run_id = str(created["run_id"])
    # V1-T3-E seam: the formal identity exists HERE - surface it to the
    # caller before any long computation starts (mirrors the ELCC and
    # resource chains' post-create attach)
    if attach_run is not None:
        attach_run(resolved_run_id)
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / EFC_CONFIG_FILE
    if config_path.exists():
        existing = config_path.read_bytes().decode("utf-8").strip()
        if existing != payload:
            raise P1WorkflowError(
                P1_EFC_INVALID_CONFIG,
                "existing frozen efc_config.json differs from the current request payload",
            )
    else:
        atomic_write_bytes(
            config_path,
            (payload + "\n").encode("utf-8"),
            immutable=True,
        )
    return (
        tables,
        app_config,
        payload,
        app_hash,
        resolution,
        bank,
        resolved_run_id,
        run_dir,
        upper_bound_mw,
    )


def _efc_solver_config(
    app_config: EfcApplicationConfig,
    upper_bound_mw: float,
    exogenous_fingerprint: str = "efc_chain_v1",
) -> EfcSolverConfig:
    search = app_config.search
    return EfcSolverConfig(
        search=EqualRiskSearchConfig(
            lower_bound_mw=search.lower_bound_mw,
            physical_upper_bound_mw=search.physical_upper_bound_mw,
            capacity_tolerance_mw=search.capacity_tolerance_mw,
            max_iterations=search.max_iterations,
            monotonic_direction=search.monotonic_direction,
            allow_upper_bound_expansion=search.allow_upper_bound_expansion,
            max_expanded_upper_bound_mw=search.max_expanded_upper_bound_mw,
            confidence_level=search.confidence_level,
        ),
        perfect_capacity_definition_version=app_config.perfect_capacity.definition_version,
        upper_bound_source=app_config.upper_bound.source,
        upper_bound_mw=upper_bound_mw,
        # the real digest is injected by _run_computation (R5/B4); the
        # default keeps direct (non-production) callers working
        exogenous_invariant_fingerprint=exogenous_fingerprint,
        portfolio_id="PORT_EFC",
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
    effective: EfcApplicationConfig,
    resolution: Any,
    bank: Any,
    run_config_hash: str,
    upper_bound_mw: float,
    observe: Any = None,
) -> dict[str, Any]:
    """Base + candidate + S+F trials + equal-risk search + publication.

    ``observe`` (optional) is forwarded to the shared EFC computation
    as its real-boundary callbacks (coarse stages + each trial);
    failures are isolated there and never change the computation."""
    if observe is None:
        stage_callback = None
        trial_observer = None
    else:

        def stage_callback(stage: str, state: str) -> None:
            try:
                observe("efc", f"{stage}_{state}", None, None)
            except Exception:  # noqa: BLE001 - progress isolation only
                return

        trial_counter = {"n": 0}

        def trial_observer(_capacity_mw: float) -> None:
            trial_counter["n"] += 1
            try:
                observe("efc", "trial", trial_counter["n"], None)
            except Exception:  # noqa: BLE001 - progress isolation only
                return

    effective_grade = ImportService(project).effective_data_grade(import_batch_id)
    samples = min(
        int(bank.index["sample_count"]),
        effective.convergence.max_samples,
    )
    probability_years = tuple(sorted(int(y) for y in bank.index["probability_baseline_years"]))
    # FORMAL year weights with strict year-set equality (missing / extra
    # years block; never a silent intersection); the SAME weights and
    # probability years flow into every base / candidate / S+F run and
    # into the risk series / result publication.
    year_weights = resolve_probability_year_weights(
        tables, effective.run_config.weather_year_weight_set_id, probability_years
    )
    rrc = resource_run_config_from(
        bank,
        samples=samples,
        primary_risk_metric=effective.run_config.primary_risk_metric,
        scenario_id=effective.run_config.scenario_id,
        storage_soc=effective.storage_soc,
        weather_years=probability_years,
        year_weights=year_weights,
        emergency_layer=effective.emergency_layer,
    )
    shared_ids = frozenset({str(obj["object_id"]) for obj in bank.index["objects"]})
    metric = effective.run_config.primary_risk_metric
    declared_convergence = convergence_config_from(effective.convergence)
    # MR-01.1: the criterion calibre. annual (default) = None window ->
    # legacy full-year metrics bit-for-bit; winter_evening_peak = the
    # SAME T06 metrics on the window slice only. A missing explicit
    # window adopts the PROVISIONAL default and says so in the result.
    criterion_window = None
    criterion_window_defaulted = False
    if effective.criterion.mode == "winter_evening_peak":
        window = effective.criterion.window
        if window is None:
            window = EfcCriterionWindowConfig()
            criterion_window_defaulted = True
        criterion_window = (set(window.winter_months), set(window.evening_hours))
    # R5/B4 remainder: the EFC chain anchors C-29 on a REAL digest of its
    # own exogenous world - the frozen data version, bank identity/seed,
    # weights, convergence and object tracks - instead of the
    # 'efc_chain_v1' label every EFC result carried until now
    from capacity_credit.application.scenario_service import (
        exogenous_invariant_digest,
    )

    frozen_state = ImportService(project).current_state(import_batch_id)
    exogenous_fingerprint = exogenous_invariant_digest(
        data_version_hash=str(frozen_state.data_version_hash),
        config_hash=run_config_hash,
        software_version=software_version,
        scenario_bank_id=bank.bank_id,
        root_seed=int(bank.index["root_seed"]),
        sample_count=samples,
        primary_risk_metric=metric.value,
        year_weights=year_weights,
        shared_object_ids=shared_ids,
        candidate_object_ids=resolution.selected_ids,
        convergence_snapshot=declared_convergence.to_snapshot(),
    )
    from capacity_credit.application.efc_components import compute_efc_evidence

    try:
        computed = compute_efc_evidence(
            bank=bank,
            tables_base=resolution.base_tables,
            tables_candidate=resolution.candidate_tables,
            rrc=rrc,
            metric=metric,
            weather_years=probability_years,
            year_weights=year_weights,
            shared_ids=shared_ids,
            candidate_ids=resolution.selected_ids,
            upper_bound_mw=upper_bound_mw,
            run_id=resolved_run_id,
            efc_config=_efc_solver_config(
                effective, upper_bound_mw, exogenous_fingerprint
            ),
            convergence_config=declared_convergence,
            stage_callback=stage_callback,
            trial_observer=trial_observer,
            criterion_window=criterion_window,
        )
    except EfcConfigError as exc:
        raise P1WorkflowError(
            exc.code if exc.code else "P1_EFC_SEARCH_FAILED",
            exc.message,
        ) from exc
    efc = computed["efc"]
    base_ev = computed["base_ev"]
    # R03: resolve the SOC modes HERE where the frozen tables and the
    # effective config live - the modes the resource chain actually
    # executed (resource_run default = weather_cycle_warmup +
    # reinitialize-on-boundary when no storage_soc was configured)
    storage = _storage_present(tables)
    return _publish_efc(
        project=project,
        run_service=run_service,
        resolved_run_id=resolved_run_id,
        request_id=request_id,
        created_by=created_by,
        efc=efc,
        base_metrics=dict(base_ev.risk_metrics),
        candidate_metrics=dict(efc.candidate_metrics) if efc.candidate_metrics else None,
        matched_metrics=dict(efc.matched_metrics) if efc.matched_metrics else None,
        bank=bank,
        effective_grade=effective_grade,
        software_version=software_version,
        app_config=effective,
        run_config_hash=run_config_hash,
        storage_soc_initialization_mode=(
            (
                effective.storage_soc.initialization_mode
                if effective.storage_soc is not None
                else StorageSocInitializationMode.WEATHER_CYCLE_WARMUP
            )
            if storage
            else StorageSocInitializationMode.NOT_APPLICABLE
        ),
        soc_year_boundary_mode=(
            SocYearBoundaryMode.REINITIALIZE_WITH_CONFIGURED_MODE
            if storage
            else SocYearBoundaryMode.NOT_APPLICABLE
        ),
        # MR-01: de-rating identity + birth certificate inputs resolved
        # from the FROZEN tables (never re-guessed at publish time)
        candidate_member_ids=sorted(resolution.selected_ids),
        de_rating_denominator_mw=resolution.candidate_capacity_mw,
        criterion_window_defaulted=criterion_window_defaulted,
    )


def _publish_efc(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    request_id: str,
    created_by: str,
    efc: EfcResult,
    base_metrics: dict[str, float],
    candidate_metrics: dict[str, float] | None,
    matched_metrics: dict[str, float] | None,
    bank: Any,
    effective_grade: str,
    software_version: str,
    app_config: EfcApplicationConfig,
    run_config_hash: str,
    storage_soc_initialization_mode: StorageSocInitializationMode = (
        StorageSocInitializationMode.NOT_APPLICABLE
    ),
    soc_year_boundary_mode: SocYearBoundaryMode = SocYearBoundaryMode.NOT_APPLICABLE,
    candidate_member_ids: list[str] | None = None,
    de_rating_denominator_mw: float | None = None,
    criterion_window_defaulted: bool = False,
) -> dict[str, Any]:
    from capacity_credit.application.result_service import _file_sha256
    from capacity_credit.contracts.artifacts import ArtifactEntry, ArtifactManifest
    from capacity_credit.contracts.elcc_diagnostics import collect_upstream_diagnostics
    from capacity_credit.contracts.enums import (
        ArtifactType,
        ConfidentialityLevel,
        RedactionStatus,
    )
    from capacity_credit.contracts.results import ScenarioBirthCertificate

    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    zero = efc.sample_count == 0
    mode = "zero" if zero else "samples"
    result_id, artifact_manifest_id = reserve_publish_ids(
        run_dir=run_dir,
        request_id=request_id,
        sample_count=efc.sample_count,
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
        upstream = collect_upstream_diagnostics(
            object_entries=list(bank.index["objects"]),
            probability_baseline_years=[
                int(year) for year in bank.index["probability_baseline_years"]
            ],
        )
        # MR-01: honest calibre disclosures (registry-validated)
        if criterion_window_defaulted:
            upstream.append(
                {"code": "EFC_WINTER_WINDOW_PROVISIONAL", "severity": "info"}
            )
        if app_config.reporting is None:
            upstream.append(
                {"code": "EFC_TECHNOLOGY_CLASS_MISSING", "severity": "info"}
            )
        result: MethodResult = build_efc_method_result(
            run_id=uuid.UUID(resolved_run_id),
            result_id=uuid.UUID(result_id),
            resource_or_portfolio_id=app_config.candidate.resource_or_portfolio_id,
            efc=efc,
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
            allocation_rule_id=app_config.run_config.allocation_rule_id,
            primary_risk_metric=app_config.run_config.primary_risk_metric,
            confidence_level=app_config.search.confidence_level,
            upstream_diagnostics=upstream,
            # R03: the SOC modes the resource chain ACTUALLY executed -
            # resolved in _run_computation, never a fabricated
            # not_applicable when storage was really consumed
            storage_soc_initialization_mode=storage_soc_initialization_mode,
            soc_year_boundary_mode=soc_year_boundary_mode,
            # MR-01.2/01.3: the de-rating factor is F*/nameplate from the
            # FROZEN resolved capacity; the delivery-year identity rides
            # with the result (降额系数只对所属交付年有效)
            scenario_birth_certificate=ScenarioBirthCertificate(
                caliber=app_config.run_config.capacity_value_type.value,
                increment_mw=(
                    de_rating_denominator_mw
                    if de_rating_denominator_mw is not None
                    else app_config.candidate.reference_capacity_mw
                ),
                placement="system_wide",
                composition_mode=(
                    "combination" if len(candidate_member_ids or ()) > 1 else "individual"
                ),
                member_ids=list(candidate_member_ids or ()),
            ),
            de_rating_factor=(
                (
                    float(efc.estimate_mw) / float(de_rating_denominator_mw)
                    if de_rating_denominator_mw
                    and float(de_rating_denominator_mw) > 0.0
                    and efc.estimate_mw is not None
                    else None
                )
            ),
            delivery_year=(
                app_config.reporting.delivery_year
                if app_config.reporting is not None
                else None
            ),
            auction_round=(
                app_config.reporting.auction_round
                if app_config.reporting is not None
                else None
            ),
            technology_class=(
                app_config.reporting.technology_class
                if app_config.reporting is not None
                else None
            ),
            criterion_mode=app_config.criterion.mode,
        )
        result_path = run_dir / "method_result.json"
        write_method_result(result, result_path)
        trace_path = _write_efc_search_trace(efc, run_dir, app_config)
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
                    producer_service="capacity_credit.application.efc_workflow",
                    schema_or_template_version="1.3",
                ),
                ArtifactEntry(
                    artifact_type=ArtifactType.SEARCH_TRACE,
                    relative_path="search_trace.json",
                    sha256=_file_sha256(trace_path),
                    media_type="application/json",
                    producer_service="capacity_credit.application.efc_workflow",
                    schema_or_template_version="1.0",
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


def _write_efc_search_trace(
    efc: EfcResult,
    run_dir: Path,
    app_config: EfcApplicationConfig,
) -> Path:
    from capacity_credit.domain.risk_units import risk_metric_unit

    sample_set_identity = ""
    canonical_identity = ""
    if efc.trace:
        sample_set_identity = efc.trace[-1].sample_set_identity
        canonical_identity = efc.trace[-1].canonical_sample_set_identity
    artifact = build_search_trace(
        primary_risk_metric=efc.primary_risk_metric,
        risk_metric_unit=risk_metric_unit(efc.primary_risk_metric),
        estimate_mw=efc.estimate_mw,
        bracket_low_mw=efc.bracket_low_mw,
        bracket_high_mw=efc.bracket_high_mw,
        result_status=efc.result_status,
        risk_convergence=efc.risk_convergence,
        search_convergence=efc.search_convergence,
        load_growth_mode="efc_perfect_capacity",
        sample_set_identity=sample_set_identity,
        canonical_sample_set_identity=canonical_identity,
        diagnostics=list(efc.diagnostics),
        trace=list(efc.trace),
        bound_decisions=list(efc.bound_decisions),
        monotonicity_ledger=list(efc.monotonicity_ledger),
    )
    return write_search_trace(artifact, run_dir / "search_trace.json")


def _published_run_config_hash(run_dir: Path) -> str:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    return str(manifest["run_config_hash"])


def run_efc_pipeline(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: EfcApplicationConfig | dict[str, Any],
    observe: Any = None,
    attach_run: Any = None,
) -> dict[str, Any]:
    """Run EFC end-to-end with a persisted lifecycle.

    The FULL normalized config is validated and frozen BEFORE create_run
    (no run side effects on invalid configs); the frozen effective
    config and its lineage hashes enter create_run idempotency and the
    RunManifest.

    V1-T3-E seam (mirrors the ELCC chain): ``attach_run`` (optional)
    fires with the REAL run id right after create_run; ``observe``
    (optional) receives the REAL boundaries that already exist - the
    coarse computation stages (baseline_risk / augmented_risk /
    efc_search, start-complete pairing) and each S+F trial - as
    (phase, subtask, observed, total). Failures are isolated and never
    touch the search."""
    app_config = _parse_app_config(app_config)
    project = Path(project_dir)
    (
        tables,
        effective,
        _payload,
        _app_hash,
        resolution,
        bank,
        resolved_run_id,
        run_dir,
        upper_bound_mw,
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
            P1_EFC_RUN_TERMINAL_FAILED,
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
            resolution=resolution,
            bank=bank,
            run_config_hash=_published_run_config_hash(run_dir),
            upper_bound_mw=upper_bound_mw,
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


__all__ = [
    "EFC_CONFIG_FILE",
    "P1_EFC_BANK_REQUIRED",
    "P1_EFC_CANDIDATE_UNKNOWN",
    "P1_EFC_INVALID_CONFIG",
    "P1_EFC_RUN_TERMINAL_FAILED",
    "P1_EFC_SCENARIO_MISMATCH",
    "P1_EFC_SEED_MISMATCH",
    "P1_EFC_UPPER_BOUND_MISMATCH",
    "run_efc_pipeline",
]
