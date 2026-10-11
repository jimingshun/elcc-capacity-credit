"""Target-capacity application workflow (Step 2 Stage A, 需求类设计 §3.3).

瘦编排：迭代调用既有充裕度资源管线做绝对目标搜索。复用件——
``efc_components.make_perfect_trial_provider``（S+F 完美容量点评估，内核
``_apply_perfect_capacity`` 只补缺不造余量）、``resource_run_config_from``
（正式年权重进 ResourceRunConfig）、``risk_series_from_evidence``（基准系列）。
新件——``solvers.target_capacity.search_target_capacity``（绝对目标区间二分，
与等风险配对搜索分工）。不改充裕度内核、不改随机流、不降收敛要求。

与 EFC 管线的结构差异：本项目无自有数据——冻结表/样本集/风险基线全部来自
config.source 指向的已完成充裕度项目（只读，血缘入 MethodResult）；正式运行
记录写在本项目目录下，类型化配置冻结为 target_capacity_config.json
（RunConfig 走 1.0 形状 method_config=None，application_config_hash 入身份）。

诚实终态：搜索收敛⇒MethodResult valid＋点值（达标所需追加容量，保守取区间
上端）；未收敛/界内不可达⇒not_converged 无点值（区间＋诊断）；现状已达标
⇒0＋TARGET_ALREADY_MET 诊断。发布链＝既有 reserve_publish_ids→
method_result→artifact manifest→result_link→COMPLETED→投影回放。
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
    make_perfect_trial_provider,
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
    _file_sha256,
    reserve_publish_ids,
    validate_manifest_before_publish,
    write_artifact_manifest,
    write_method_result,
)
from capacity_credit.application.run_service import RunService
from capacity_credit.contracts.artifacts import ArtifactEntry, ArtifactManifest
from capacity_credit.contracts.enums import (
    ArtifactType,
    CapacityValueType,
    ChronologyMode,
    ConfidentialityLevel,
    DataGrade,
    LifecycleStatus,
    RedactionStatus,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.results import MethodResult, RiskMetrics
from capacity_credit.contracts.target_capacity_config import (
    TargetCapacityApplicationConfig,
)
from capacity_credit.scenarios.scenario_bank import open_scenario_bank
from capacity_credit.solvers.target_capacity import (
    search_target_capacity,
    search_target_load_scale,
)

TARGET_CAPACITY_CONFIG_FILE = "target_capacity_config.json"

P1_TARGET_CAPACITY_INVALID_CONFIG = "P1_TARGET_CAPACITY_INVALID_CONFIG"
P1_TARGET_CAPACITY_BANK_REQUIRED = "P1_TARGET_CAPACITY_BANK_REQUIRED"
P1_TARGET_CAPACITY_SCENARIO_MISMATCH = "P1_TARGET_CAPACITY_SCENARIO_MISMATCH"
P1_TARGET_CAPACITY_SEED_MISMATCH = "P1_TARGET_CAPACITY_SEED_MISMATCH"
P1_TARGET_CAPACITY_RUN_TERMINAL_FAILED = "P1_TARGET_CAPACITY_RUN_TERMINAL_FAILED"


def _parse_app_config(
    app_config: TargetCapacityApplicationConfig | dict[str, Any],
) -> TargetCapacityApplicationConfig:
    try:
        if isinstance(app_config, TargetCapacityApplicationConfig):
            payload = app_config.model_dump(mode="json")
        elif isinstance(app_config, dict):
            payload = app_config
        else:
            raise TypeError(
                "app_config must be a TargetCapacityApplicationConfig or a dict"
            )
        return TargetCapacityApplicationConfig.model_validate(payload)
    except Exception as exc:
        raise P1WorkflowError(
            P1_TARGET_CAPACITY_INVALID_CONFIG,
            f"invalid target-capacity config: {exc}",
        ) from exc


def _open_source_bank(
    source_project: Path, config: TargetCapacityApplicationConfig
) -> Any:
    scenario_bank_id = config.run_config.scenario_bank_id
    if not scenario_bank_id:
        raise P1WorkflowError(
            P1_TARGET_CAPACITY_BANK_REQUIRED,
            "run_config.scenario_bank_id is required for target capacity",
        )
    bank = open_scenario_bank(
        source_project / "artifacts" / "scenario_banks" / scenario_bank_id
    )
    if bank.bank_id != scenario_bank_id:
        raise P1WorkflowError(
            P1_TARGET_CAPACITY_SCENARIO_MISMATCH,
            "scenario bank id does not match the reference",
        )
    if int(bank.index["root_seed"]) != config.run_config.random_seed:
        raise P1WorkflowError(
            P1_TARGET_CAPACITY_SEED_MISMATCH,
            f"random_seed {config.run_config.random_seed} does not match the "
            f"scenario bank seed {bank.index['root_seed']}",
        )
    return bank


def _prepare_run(
    *,
    project: Path,
    source_project: Path,
    source_import_batch_id: str,
    source_data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: TargetCapacityApplicationConfig,
    attach_run: Any = None,
) -> tuple[dict[str, Any], Any, Any, str, Path]:
    """Source gates (read-only) + create_run + frozen-config persistence."""
    bank = _open_source_bank(source_project, app_config)
    source_import = ImportService(source_project)
    _require_frozen_gates(source_project, source_import_batch_id, source_data_version_id)
    tables = _read_tables(source_import.frozen_files(source_import_batch_id))
    normalized = app_config.model_dump(mode="json")
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    app_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # 运行记录写在本项目目录；批次门校验对 SOURCE 项目的真实冻结批次
    # （RunService 的 import_service 与 root 解耦——跨项目血缘经既有
    # frozen/approved 门，不另写第二套校验）
    run_service = RunService(project, ImportService(source_project))
    try:
        created = run_service.create_run(
            request_id=request_id,
            run_config=app_config.to_run_config().model_dump(mode="json"),
            import_batch_id=source_import_batch_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            scenario_bank_id=app_config.run_config.scenario_bank_id,
            teaching_override_reason=(
                "teaching fixture synthetic project (Step 2 target capacity)"
                if app_config.run_config.run_mode.value == "teaching"
                else None
            ),
            application_config_hash=app_hash,
        )
    except Exception as exc:
        raise P1WorkflowError(
            P1_TARGET_CAPACITY_INVALID_CONFIG,
            f"invalid target-capacity config at the freeze boundary: {exc}",
        ) from exc
    resolved_run_id = str(created["run_id"])
    if attach_run is not None:
        attach_run(resolved_run_id)
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / TARGET_CAPACITY_CONFIG_FILE
    if config_path.exists():
        existing = config_path.read_bytes().decode("utf-8").strip()
        if existing != payload:
            raise P1WorkflowError(
                P1_TARGET_CAPACITY_INVALID_CONFIG,
                "existing frozen target_capacity_config.json differs from the "
                "current request payload",
            )
    else:
        atomic_write_bytes(
            config_path,
            (payload + "\n").encode("utf-8"),
            immutable=True,
        )
    return tables, bank, run_service, resolved_run_id, run_dir


def _run_computation(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    source_project: Path,
    source_import_batch_id: str,
    tables: dict[str, Any],
    app_config: TargetCapacityApplicationConfig,
    bank: Any,
    observe: Any = None,
) -> dict[str, Any]:
    from capacity_credit.application.resource_run import run_resource_pipeline
    from capacity_credit.application.risk_series import risk_series_from_evidence

    def stage(stage_name: str, state: str) -> None:
        if observe is None:
            return
        try:
            observe("target_capacity", f"{stage_name}_{state}", None, None)
        except Exception:  # noqa: BLE001 - progress isolation only
            return

    stage("baseline_risk", "start")
    samples = int(bank.index["sample_count"])
    probability_years = tuple(
        sorted(int(y) for y in bank.index["probability_baseline_years"])
    )
    year_weights = resolve_probability_year_weights(
        tables, app_config.run_config.weather_year_weight_set_id, probability_years
    )
    metric = app_config.run_config.primary_risk_metric
    rrc = resource_run_config_from(
        bank,
        samples=samples,
        primary_risk_metric=metric,
        scenario_id=app_config.run_config.scenario_id,
        weather_years=probability_years,
        year_weights=year_weights,
    )
    shared_ids = frozenset({str(obj["object_id"]) for obj in bank.index["objects"]})

    # 基准（X=0）评估：现状风险系列＝provider 所需 reference，也是结果里的
    # base_risk（现状口径下的主指标）
    base_evidence = run_resource_pipeline(
        bank=bank,
        tables=tables,
        config=rrc,
        run_id=f"{resolved_run_id}_base",
    )
    base_series = risk_series_from_evidence(
        base_evidence,
        metric,
        bank,
        shared_ids,
        frozenset({"PERFECT_F"}),
        probability_years,
        year_weights,
    )
    stage("baseline_risk", "complete")

    stage("target_search", "start")
    # Q-D4（2026-10-10 裁决）：调整负荷口径＝λ 轴搜索——供给侧全部冻结，
    # 逐点把原生负荷曲线乘 λ（表副本喂既有管线；不改 ResourceRunConfig、
    # 不改内核、不改随机流）；点值换算为负荷水平 MW（λ×峰荷）后进合同。
    load_scale_factor: float | None = None
    peak_load_mw: float | None = None
    if app_config.caliber == "adjust_load":
        from types import SimpleNamespace

        peak_load_mw = float(tables["load_hourly"]["load_mw"].max())
        load_outcomes: dict[float, Any] = {}

        def load_risk_provider(scale: float) -> float:
            scaled = dict(tables)
            scaled["load_hourly"] = tables["load_hourly"].assign(
                load_mw=tables["load_hourly"]["load_mw"].astype(float)
                * float(scale)
            )
            evidence = run_resource_pipeline(
                bank=bank,
                tables=scaled,
                config=rrc,
                run_id=f"{resolved_run_id}_L{float(scale):.6f}",
            )
            series = risk_series_from_evidence(
                evidence,
                metric,
                bank,
                shared_ids,
                frozenset({"PERFECT_F"}),
                probability_years,
                year_weights,
            )
            load_outcomes[round(float(scale), 6)] = series
            if observe is not None:
                try:
                    observe("target_capacity", "trial", len(load_outcomes), None)
                except Exception:  # noqa: BLE001
                    pass
            # 引擎自己的主指标聚合值（与充裕度结果同一数字来源）
            return float(evidence.risk_metrics[metric.value])

        load_search = search_target_load_scale(
            lower_bound_scale=app_config.search.lower_bound_load_scale,
            upper_bound_scale=app_config.search.upper_bound_load_scale,
            scale_tolerance=app_config.search.load_scale_tolerance,
            max_iterations=app_config.search.max_iterations,
            target_value=app_config.target.value,
            risk_provider=load_risk_provider,
        )
        anchor_lambda = (
            load_search.estimate_scale
            if load_search.estimate_scale is not None
            else load_search.bracket_low_scale
        )
        anchor_series = load_outcomes.get(round(float(anchor_lambda), 6))
        anchor_snapshot = (
            anchor_series.convergence_snapshot
            if anchor_series is not None
            else None
        )
        risk_convergence = (
            anchor_snapshot.risk_convergence
            if anchor_snapshot is not None
            else RiskConvergence.NOT_CONVERGED
        )
        search = SimpleNamespace(
            estimate_mw=(
                load_search.estimate_scale * peak_load_mw
                if load_search.estimate_scale is not None
                else None
            ),
            bracket_low_mw=load_search.bracket_low_scale * peak_load_mw,
            bracket_high_mw=load_search.bracket_high_scale * peak_load_mw,
            bracket_low_risk=load_search.bracket_low_risk,
            bracket_high_risk=load_search.bracket_high_risk,
            result_status=load_search.result_status,
            search_convergence=load_search.search_convergence,
            iterations=load_search.iterations,
            monotonicity_ledger=load_search.monotonicity_ledger,
            diagnostics=load_search.diagnostics,
            evaluations=load_search.evaluations,
        )
        load_scale_factor = load_search.estimate_scale
    else:
        provider = make_perfect_trial_provider(
            bank=bank,
            base_tables=tables,
            rrc=rrc,
            reference=base_series,
            metric=metric,
            shared_ids=shared_ids,
            run_id=resolved_run_id,
            weather_years=probability_years,
            year_weights=year_weights,
        )
        outcomes: dict[float, Any] = {}

        def risk_provider(capacity_mw: float) -> float:
            outcome = provider(float(capacity_mw))
            outcomes[round(float(capacity_mw), 6)] = outcome
            if observe is not None:
                try:
                    observe("target_capacity", "trial", len(outcomes), None)
                except Exception:  # noqa: BLE001
                    pass
            # 引擎自己的主指标聚合值（与充裕度结果同一数字来源）
            return float(outcome.risk_metrics[metric.value])

        search = search_target_capacity(
            lower_bound_mw=app_config.search.lower_bound_mw,
            upper_bound_mw=app_config.search.upper_bound_mw,
            capacity_tolerance_mw=app_config.search.capacity_tolerance_mw,
            max_iterations=app_config.search.max_iterations,
            target_value=app_config.target.value,
            risk_provider=risk_provider,
        )

        # 发布侧的收敛事实取「点值点」的评估快照（无点值时取区间上端/基准）
        anchor_x = (
            search.estimate_mw
            if search.estimate_mw is not None
            else search.bracket_high_mw
        )
        anchor_outcome = outcomes.get(round(float(anchor_x), 6))
        risk_convergence = (
            anchor_outcome.risk_convergence
            if anchor_outcome is not None
            else RiskConvergence.NOT_CONVERGED
        )
    stage("target_search", "complete")
    base_metrics_raw = dict(base_evidence.risk_metrics)
    # RiskRole 合同只收四指标键（证据字典还带 primary 等聚合键——剔除，
    # 不造第二套指标语义）
    _ROLE_KEYS = (
        "lole_days_per_year",
        "lolh_hours_per_year",
        "eue_mwh_per_year",
        "neue_ppm",
    )
    base_metrics = {
        key: float(base_metrics_raw[key]) for key in _ROLE_KEYS if key in base_metrics_raw
    }
    effective_grade = ImportService(source_project).effective_data_grade(
        source_import_batch_id
    )
    return _publish(
        project=project,
        run_service=run_service,
        resolved_run_id=resolved_run_id,
        request_id=request_id,
        created_by=created_by,
        software_version=software_version,
        app_config=app_config,
        bank=bank,
        samples=samples,
        base_metrics=base_metrics,
        search=search,
        risk_convergence=risk_convergence,
        effective_grade=effective_grade,
        load_scale_factor=load_scale_factor,
        peak_load_mw=peak_load_mw,
    )


def _publish(
    *,
    project: Path,
    run_service: RunService,
    resolved_run_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    app_config: TargetCapacityApplicationConfig,
    bank: Any,
    samples: int,
    base_metrics: dict[str, float],
    search: Any,
    risk_convergence: RiskConvergence,
    effective_grade: str,
    load_scale_factor: float | None = None,
    peak_load_mw: float | None = None,
) -> dict[str, Any]:
    run_dir = project / "runs" / safe_filename_component(resolved_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    zero = samples == 0
    mode = "zero" if zero else "samples"
    result_id, artifact_manifest_id = reserve_publish_ids(
        run_dir=run_dir,
        request_id=request_id,
        sample_count=samples,
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
        metric = app_config.run_config.primary_risk_metric
        result = MethodResult.model_validate(
            dict(
                result_schema_version="1.3",
                result_id=uuid.UUID(result_id),
                run_id=uuid.UUID(resolved_run_id),
                method="demand_target_capacity",
                method_profile=app_config.run_config.method_profile,
                resource_or_portfolio_id=None,
                capacity_value_type="average",
                allocation_type="none",
                chronology_mode="independent_weather_years",
                storage_soc_initialization_mode="not_applicable",
                soc_year_boundary_mode="not_applicable",
                rmri_status="not_applicable",
                primary_risk_metric=metric.value,
                risk_metrics=dict(base=base_metrics),
                base_risk=float(base_metrics[metric.value]),
                interpolation_method="not_applicable",
                sample_count=samples,
                scenario_bank_id=bank.bank_id,
                result_status=search.result_status.value,
                risk_convergence=risk_convergence.value,
                search_convergence=search.search_convergence.value,
                effective_data_grade=effective_grade,
                accredited_capacity_mw=search.estimate_mw,
                search_bracket_low_mw=search.bracket_low_mw,
                search_bracket_high_mw=search.bracket_high_mw,
                search_bracket_low_risk=search.bracket_low_risk,
                search_bracket_high_risk=search.bracket_high_risk,
                diagnostics=[
                    {"code": d["code"], "severity": d["severity"], "message": d["message"]}
                    for d in search.diagnostics
                ],
                target_risk_value=app_config.target.value,
                solver_caliber=app_config.caliber,
                load_scale_factor=load_scale_factor,
                source_project_ref=app_config.source.project_ref,
                source_run_ref=app_config.source.run_ref,
                source_result_id=None,
            )
        )
        result_path = run_dir / "method_result.json"
        write_method_result(result, result_path)
        trace_path = run_dir / "search_trace.json"
        trace_payload = json.dumps(
            dict(
                kind="target_capacity_search_trace",
                schema_version="1.0",
                primary_risk_metric=metric.value,
                target_risk_value=app_config.target.value,
                caliber=app_config.caliber,
                result_status=search.result_status.value,
                search_convergence=search.search_convergence.value,
                iterations=search.iterations,
                bracket_low_mw=search.bracket_low_mw,
                bracket_high_mw=search.bracket_high_mw,
                estimate_mw=search.estimate_mw,
                evaluations=search.evaluations,
                monotonicity_ledger=search.monotonicity_ledger,
                diagnostics=search.diagnostics,
                **(
                    dict(
                        load_scale_estimate=load_scale_factor,
                        peak_load_mw=peak_load_mw,
                    )
                    if app_config.caliber == "adjust_load"
                    else {}
                ),
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        atomic_write_bytes(
            trace_path, (trace_payload + "\n").encode("utf-8"), immutable=True
        )
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
                    producer_service=(
                        "capacity_credit.application.target_capacity_workflow"
                    ),
                    schema_or_template_version="1.3",
                ),
                ArtifactEntry(
                    artifact_type=ArtifactType.SEARCH_TRACE,
                    relative_path="search_trace.json",
                    sha256=_file_sha256(trace_path),
                    media_type="application/json",
                    producer_service=(
                        "capacity_credit.application.target_capacity_workflow"
                    ),
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
        _write_result_link(
            project, resolved_run_id, request_id, result_id, artifact_manifest_id
        )
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


def run_target_capacity_pipeline(
    *,
    project_dir: Path | str,
    source_project_dir: Path | str,
    source_import_batch_id: str,
    source_data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    app_config: TargetCapacityApplicationConfig | dict[str, Any],
    observe: Any = None,
    attach_run: Any = None,
) -> dict[str, Any]:
    """Run target-capacity end-to-end with a persisted lifecycle."""
    app_config = _parse_app_config(app_config)
    project = Path(project_dir)
    source_project = Path(source_project_dir)
    tables, bank, run_service, resolved_run_id, run_dir = _prepare_run(
        project=project,
        source_project=source_project,
        source_import_batch_id=source_import_batch_id,
        source_data_version_id=source_data_version_id,
        request_id=request_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        app_config=app_config,
        attach_run=attach_run,
    )
    terminal = run_service.run_state(resolved_run_id)["lifecycle_status"]
    if terminal == LifecycleStatus.FAILED.value:
        raise P1WorkflowError(
            P1_TARGET_CAPACITY_RUN_TERMINAL_FAILED,
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
        return _run_computation(
            project=project,
            run_service=run_service,
            resolved_run_id=resolved_run_id,
            request_id=request_id,
            created_by=created_by,
            software_version=software_version,
            source_project=source_project,
            source_import_batch_id=source_import_batch_id,
            tables=tables,
            app_config=app_config,
            bank=bank,
            observe=observe,
        )
    except Exception:
        run_service.transition(
            request_id=f"{request_id}-fail",
            run_id=resolved_run_id,
            target_lifecycle=LifecycleStatus.FAILED.value,
        )
        raise


__all__ = [
    "P1_TARGET_CAPACITY_INVALID_CONFIG",
    "run_target_capacity_pipeline",
]
