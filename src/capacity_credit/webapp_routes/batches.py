"""研究批次接口（/api/batches*）。任务 D 工作 2 拆出；零行为变化；
导入一律模块级（根治旧链函数内 from-import 遮蔽 bug 类）。

路由语义注记：POST 处理函数不再自行 _read_json——分发表统一代读并
以 body 传入；GET 处理函数 body 为 None。点位台账语义（DR-01a/b 前向
账本、待确认续推、R2-B5 模拟身份钉住、SL-01 季节双旋钮）原样保留。
"""

from __future__ import annotations

import uuid
from typing import Any

from capacity_credit.application import calibration_service
from capacity_credit.application import research_batch_service
from capacity_credit.application import run_config as run_config_module
from capacity_credit.application.import_service import ServiceError
from capacity_credit.application.run_config import order_banks_for_data_ref
from capacity_credit.application.workbench_service import WorkbenchError
from capacity_credit.ui import runtime as ui_runtime
from capacity_credit.webapp_routes import route


def _is_seasonal_manifest(manifest: dict[str, Any]) -> bool:
    """SL-01: a calibration batch whose criterion carries the explicit
    heating/non_heating target pair runs the dual-knob seasonal
    bisection (criterion.seasonal present)."""
    criterion = (manifest or {}).get("criterion") or {}
    return isinstance(criterion, dict) and isinstance(
        criterion.get("seasonal"), dict
    )


@route("POST", "/api/batches")
def batches_create(h: Any, qs: dict, body: Any) -> None:
    # N1: create ONE research batch (DR-01a/b batch authorization
    # is the caller's explicit user action and is recorded)
    try:
        manifest = research_batch_service.create_batch(
            h.workspace,
            request_id=str(
                body.get("request_id")
                or f"web-{uuid.uuid4().hex}"
            ),
            kind=str(body.get("kind") or ""),
            method=str(body.get("method") or ""),
            project_ref=str(body.get("project_ref") or ""),
            baseline_batch_id=str(
                body.get("baseline_batch") or ""
            ),
            points=body.get("points") or [],
            axis=body.get("axis"),
            axis_config=body.get("axis_config"),
            criterion=body.get("criterion"),
            label=str(body.get("label") or ""),
        )
    except research_batch_service.ResearchBatchError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    return h._json(200, manifest)


@route("GET", "/api/batches")
def batches_list(h: Any, qs: dict, body: Any) -> None:
    return h._json(
        200, {"batches": research_batch_service.list_batches(h.workspace)}
    )


@route("GET", "/api/batches/detail")
def batches_detail(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("ref") or [""])[0]
    try:
        return h._json(
            200, research_batch_service.read_batch(h.workspace, ref)
        )
    except research_batch_service.ResearchBatchError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )


@route("POST", "/api/batches/derive-point")
def batches_derive_point(h: Any, qs: dict, body: Any) -> None:
    # DR-01a/b: the batch authorization was given by the user at
    # creation, so this chain derives, auto-approves + freezes and
    # banks the point (root seed reused from the baseline), then
    # moves the point to 待运行 - recording the authorizing user
    # and the derivation record as the assumption register.
    batch_ref = str(body.get("batch_ref") or "")
    point_id = str(body.get("point_id") or "")
    authorized_by = str(body.get("authorized_by") or "")
    if not authorized_by.strip():
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": "DR-01a/b 要求记录批次授权人：请填写 authorized_by",
            },
        )
    try:
        manifest = research_batch_service.read_batch(
            h.workspace, batch_ref
        )
    except research_batch_service.ResearchBatchError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    point = next(
        (
            p
            for p in manifest["points"]
            if p.get("point_id") == point_id
        ),
        None,
    )
    if point is None:
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": f"批次没有点位 {point_id}",
            },
        )
    point_status = str(point.get("status"))
    if point_status not in (
        "待派生",
        "失败",
        "待确认",
    ):
        return h._json(
            400,
            {
                "kind": "request_conflict",
                "message": (
                    f"点位 {point_id} 状态为 {point.get('status')}，"
                    "只有待派生（或失败后重派生）可执行派生；"
                    "待确认点位按既有派生记录续推"
                ),
            },
        )
    # R2-B5: every round of ONE calibration batch must face the
    # same random stream and precision (方法说明第三章(五) CRN).
    # The check runs BEFORE any derive side effect; a request
    # without an explicit seed adopts the pinned identity.
    try:
        req_seed_raw = body.get("root_seed")
        req_seed = (
            None if req_seed_raw is None else int(req_seed_raw)
        )
        req_samples = int(body.get("samples") or 30)
    except (TypeError, ValueError):
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": (
                    "root_seed/samples 必须为整数（收到 "
                    f"{body.get('root_seed')!r} / {body.get('samples')!r}）"
                ),
            },
        )
    weight_set_req = str(body.get("weight_set_id") or "w1")
    pinned = manifest.get("simulation_identity")
    if req_seed is None and not pinned:
        # DR-01a/b design: the point bank REUSES the baseline's
        # root seed and precision ("root seed reused from the
        # baseline"). A request with no explicit seed therefore
        # adopts the baseline-ordered bank's identity - int(None)
        # was an honest crash here before the night fix.
        options = h._service().get_run_options(
            str(manifest["project_ref"])
        )
        base_banks = order_banks_for_data_ref(
            list(options.get("scenario_banks") or []),
            str(manifest.get("baseline_batch_id") or ""),
        )
        if base_banks:
            req_seed = int(base_banks[0].get("root_seed"))
            if body.get("samples") in (None, ""):
                req_samples = int(
                    base_banks[0].get("sample_count") or 30
                )
    if pinned and manifest.get("kind") == "calibration":
        if req_seed is None:
            req_seed = int(pinned["root_seed"])
            req_samples = int(pinned["samples"])
            weight_set_req = str(pinned["weight_set_id"])
        elif (
            req_seed != int(pinned["root_seed"])
            or req_samples != int(pinned["samples"])
            or weight_set_req != str(pinned["weight_set_id"])
        ):
            return h._json(
                400,
                {
                    "kind": "request_conflict",
                    "message": (
                        f"批次已钉住模拟身份 {pinned}，本轮请求"
                        f"（root_seed={req_seed}, samples={req_samples}, "
                        f"weight_set_id={weight_set_req!r}）与之不一致："
                        "标定各轮必须复用同一种子与精度（CRN 纪律），"
                        "确需更换请新建批次"
                    ),
                },
            )
    if point_status == "待确认":
        # 待确认 ⇒ 派生与
        # 审批冻结在第一轮已完成且记录就在点位上；续推只补齐
        # 样本集准备与身份钉住，再走到待运行——不重复派生
        # （derive_data_version 会产出第二个派生批次），不新增
        # 端点。无派生记录的待确认点位诚实拒绝（不猜父版本）。
        new_batch = str(point.get("import_batch_id") or "")
        if not new_batch:
            return h._json(
                400,
                {
                    "kind": "invalid_input",
                    "message": (
                        f"点位 {point_id} 停在待确认但没有派生记录"
                        "（import_batch_id），无法续推："
                        "请重开任务（旧任务数据保留可查）"
                    ),
                },
            )
        derived = {
            "derivation": dict(point.get("derivation") or {})
        }
        try:
            summary = h._service().get_import_summary(
                str(manifest["project_ref"]), new_batch
            )
        except WorkbenchError as exc:
            return h._json(
                400, {"kind": exc.kind, "message": exc.message}
            )
        frozen = {
            "data_version_hash": str(
                summary.get("data_version_hash") or ""
            )
        }
    else:
        params = dict(point.get("params") or {})
        rule_id = str(params.pop("rule_id", "") or "")
        try:
            derived = h._service().derive_data_version(
                str(manifest["project_ref"]),
                baseline_batch_id=str(
                    manifest["baseline_batch_id"]
                ),
                rule_id=rule_id,
                params=params,
                declared_grade=str(
                    body.get("declared_grade") or ""
                ),
            )
        except WorkbenchError as exc:
            return h._json(
                400, {"kind": exc.kind, "message": exc.message}
            )
        new_batch = str(
            (derived.get("import_result") or {}).get(
                "import_batch_id"
            )
            or (derived.get("import_result") or {}).get("batch_id")
            or ""
        )
        if not new_batch:
            return h._json(
                400,
                {
                    "kind": "invalid_input",
                    "message": "派生结果未包含批次号：导入链被拒绝（如实返回，不冒充成功）",
                },
            )
        # DR-01a/b auto-approve + freeze (batch-level authorization)
        try:
            summary = h._service().get_import_summary(
                str(manifest["project_ref"]), new_batch
            )
            frozen = h._service().confirm_and_freeze(
                project_ref=str(manifest["project_ref"]),
                import_batch_id=new_batch,
                request_id=f"web-derive-{uuid.uuid4().hex}",
                reviewed_by=(
                    f"批次授权:{authorized_by}"
                ),
                review_comment=(
                    f"DR-01{'a' if manifest['kind'] != 'calibration' else 'b'}"
                    f" 批次 {batch_ref[:8]} 点位 {point_id} 自动审批冻结"
                ),
                confirmed_summary_hash=str(
                    summary.get("summary_hash") or ""
                ),
            )
        except (WorkbenchError, ServiceError) as exc:
            message = getattr(exc, "message", str(exc))
            return h._json(
                400,
                {
                    "kind": "invalid_input",
                    "message": f"自动冻结被拒绝（如实返回）：{message}",
                },
            )
    bank: dict[str, Any] = {}
    try:
        bank = h._service().prepare_scenario_bank(
            project_ref=str(manifest["project_ref"]),
            import_batch_id=new_batch,
            request_id=f"web-bank-{uuid.uuid4().hex}",
            root_seed=int(req_seed),
            samples=req_samples,
            weight_set_id=weight_set_req,
            timezone="Asia/Shanghai",
        )
    except (WorkbenchError, ServiceError, TypeError, ValueError) as exc:
        message = getattr(exc, "message", str(exc))
        research_batch_service.update_point(
            h.workspace,
            batch_ref,
            point_id,
            status="待确认",
            import_batch_id=new_batch,
            derivation=dict(derived.get("derivation") or {}),
            diagnosis=f"样本集生成被拒绝（如实保留）：{message}",
        )
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": f"样本集生成被拒绝：{message}",
            },
        )
    # R2-B5: pin the batch's simulation identity from the bank's
    # ACTUAL values on the first successful round (forward-only;
    # a concurrent drift is an honest refusal, not an overwrite)
    if manifest.get("kind") == "calibration":
        try:
            research_batch_service.bind_simulation_identity(
                h.workspace,
                batch_ref,
                {
                    "root_seed": bank.get("root_seed"),
                    # prepare_scenario_bank returns the bank id +
                    # actual root seed only: the precision half of
                    # the identity IS this request's samples/weights
                    "samples": req_samples,
                    "weight_set_id": weight_set_req,
                },
            )
        except research_batch_service.ResearchBatchError as exc:
            return h._json(
                400, {"kind": exc.kind, "message": exc.message}
            )
    record = dict(derived.get("derivation") or {})
    if point_status == "待确认":
        # 续推保留原派生记录（证据不改写），只补登续推人
        record.setdefault("authorized_by", authorized_by)
        record["resumed_by"] = authorized_by
        record["resume_note"] = (
            "W3 待确认续推：样本集补齐后推进待运行"
        )
    else:
        record["authorized_by"] = authorized_by
        record["authorization"] = (
            f"DR-01a/b 批次级授权（{authorized_by}）"
        )
    # DR-01a/b: the BATCH authorization IS the per-point
    # confirmation, so the ledger walks the forward-only chain in
    # two committed hops - 待确认 carries the derivation record +
    # authorization holder (the automatic-confirmation moment),
    # then 待运行. A single 待派生→待运行 hop is refused by the
    # ledger's own DR-01 guard (this endpoint had never run
    # end-to-end before the night fix).
    research_batch_service.update_point(
        h.workspace,
        batch_ref,
        point_id,
        status="待确认",
        import_batch_id=new_batch,
        derivation=record,
    )
    done = research_batch_service.update_point(
        h.workspace,
        batch_ref,
        point_id,
        status="待运行",
        import_batch_id=new_batch,
        derivation=record,
        extra={
            "bank_scenario_bank_id": bank.get("scenario_bank_id"),
            "root_seed": bank.get("root_seed"),
            "samples": req_samples,
            "weight_set_id": weight_set_req,
            # MR-02: the frozen data version of THIS round feeds
            # the calibrated_baseline_ref draft
            "data_version_hash": str(
                (frozen or {}).get("data_version_hash") or ""
            ),
        },
    )
    return h._json(
        200,
        {
            "batch": done,
            "point": next(
                p
                for p in done["points"]
                if p["point_id"] == point_id
            ),
            "freeze": frozen,
            "bank_root_seed": bank.get("root_seed"),
            "bank_samples": bank.get("samples"),
        },
    )


@route("POST", "/api/batches/point-run")
def batches_point_run(h: Any, qs: dict, body: Any) -> None:
    # N2: admit the point's REAL run through the same
    # preview -> start discipline as every other calculation
    batch_ref = str(body.get("batch_ref") or "")
    point_id = str(body.get("point_id") or "")
    try:
        manifest = research_batch_service.read_batch(
            h.workspace, batch_ref
        )
    except research_batch_service.ResearchBatchError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    point = next(
        (
            p
            for p in manifest["points"]
            if p.get("point_id") == point_id
        ),
        None,
    )
    if point is None:
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": f"批次没有点位 {point_id}",
            },
        )
    if str(point.get("status")) != "待运行":
        return h._json(
            400,
            {
                "kind": "request_conflict",
                "message": (
                    f"点位 {point_id} 状态为 {point.get('status')}，"
                    "只有待运行的点位可以发起测算"
                ),
            },
        )
    data_ref = str(point.get("import_batch_id") or "")
    method_name = str(manifest["method"])
    criterion = dict(manifest.get("criterion") or {})
    metric = str(criterion.get("metric") or "")
    overrides = dict(body.get("form") or {})
    form = {
        "bank_id": str(
            point.get("bank_scenario_bank_id") or ""
        ),
        "metric": metric,
        "weight_set_id": str(
            point.get("weight_set_id") or ""
        ),
    }
    if method_name == "adequacy":
        form["scenario_id"] = None
    else:
        form["group_id"] = str(
            overrides.get("group_id")
            or criterion.get("group_id")
            or ""
        )
        form["upper_mw"] = overrides.get("upper_mw", 2000)
        form["tolerance_mw"] = overrides.get("tolerance_mw", 1.0)
        form["max_iterations"] = overrides.get(
            "max_iterations", 20
        )
        form["confidence"] = overrides.get("confidence", 0.95)
        form["allow_expansion"] = bool(
            overrides.get("allow_expansion", False)
        )
        form["expanded_upper_mw"] = overrides.get(
            "expanded_upper_mw"
        )
        if method_name == "elcc":
            form["growth_mode"] = str(
                overrides.get("growth_mode") or "fixed_mw"
            )
        else:
            form["upper_source"] = str(
                overrides.get("upper_source")
                or "nameplate_capacity"
            )
            form["declared_mw"] = overrides.get("declared_mw")
    try:
        options = h._service().get_run_options(
            str(manifest["project_ref"])
        )
        # module-qualified calls ONLY: a from-import inside the old
        # shared _api would shadow the module-level names for the WHOLE
        # function (the preview routes would then see an unbound local -
        # UnboundLocalError at runtime); the split handlers keep the
        # qualified form for one shared import surface
        if method_name == "adequacy":
            config = run_config_module.build_adequacy_config(
                form, options, data_ref
            )
            # the adequacy builder returns the NESTED preview shape
            # ({"run_config": {...}, "convergence": {...}}) while
            # the built-in runner consumes FLAT top-level keys -
            # flatten here exactly like /api/runs/start does (the
            # point-run route never had this fix, so every batch
            # point run refused on an empty scenario_bank_id)
            if isinstance(config.get("run_config"), dict) and (
                "scenario_bank_id" not in config
            ):
                declared_convergence = config.get("convergence")
                config = dict(config["run_config"])
                if isinstance(declared_convergence, dict):
                    config["convergence"] = declared_convergence
                if not str(config.get("weight_set_id") or ""):
                    config["weight_set_id"] = config.get(
                        "weather_year_weight_set_id"
                    )
        elif method_name == "elcc":
            config = run_config_module.build_elcc_config(
                form, options, data_ref
            )
        else:
            config = run_config_module.build_efc_config(
                form, options, data_ref
            )
        handle = h._service().start_calculation(
            intent_id=f"web-batch-{uuid.uuid4().hex}",
            project_ref=str(manifest["project_ref"]),
            data_ref=data_ref,
            method=method_name,
            config=config,
            intent_is_new=True,
            # P0 (2026-09-20): a batch point run is a research
            # sub-run - exempt from the type lock at the facade;
            # /api/runs/start stays on the default (locked)
            research_point=True,
        )
    except (WorkbenchError, ServiceError) as exc:
        message = getattr(exc, "message", str(exc))
        return h._json(
            400, {"kind": "invalid_input", "message": message}
        )
    # the REQUEST ref is NOT the run: run_ref stays unset until
    # point-finalize reads the terminal truth (the runtime's
    # attach resolves the real formal run id)
    research_batch_service.update_point(
        h.workspace,
        batch_ref,
        point_id,
        status="待运行",
        extra={"request_ref": str(handle.request_ref)},
    )
    return h._json(
        200,
        {
            "request_ref": str(handle.request_ref),
            "run_ref": None,
            "point": next(
                p
                for p in research_batch_service.read_batch(
                    h.workspace, batch_ref
                )["points"]
                if p["point_id"] == point_id
            ),
        },
    )


@route("POST", "/api/batches/point-finalize")
def batches_point_finalize(h: Any, qs: dict, body: Any) -> None:
    # N2: read the terminal truth and write it into the ledger -
    # 完成 only for a finished, published, usable result; an
    # unconverged/failed run keeps the diagnosis and stays a gap
    batch_ref = str(body.get("batch_ref") or "")
    point_id = str(body.get("point_id") or "")
    run_ref = str(body.get("run_ref") or "")
    try:
        manifest = research_batch_service.read_batch(
            h.workspace, batch_ref
        )
    except research_batch_service.ResearchBatchError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    snap = ui_runtime.get_local_runtime(h.workspace).snapshot(
        run_ref
    )
    # the caller may pass the REQUEST ref (the point ledger stores
    # it - the real formal run id only exists once the runtime
    # attaches it); resolve to the REAL run before any projection
    real_run = str(getattr(snap, "run_ref", None) or "") or run_ref
    if snap.status not in ("execution_finished", "execution_failed"):
        return h._json(
            200,
            {
                "finalized": False,
                "status": snap.status,
                "message": "运行尚未结束：稍后再次收尾",
            },
        )
    run_ref = real_run
    if snap.status == "execution_failed":
        done = research_batch_service.update_point(
            h.workspace,
            batch_ref,
            point_id,
            status="失败",
            run_ref=run_ref,
            diagnosis=str(snap.execution_error or "运行失败")[:500],
        )
        return h._json(200, {"finalized": True, "batch": done})
    try:
        projection = h._service().get_run_projection(
            str(manifest["project_ref"]), run_ref
        )
    except WorkbenchError as exc:
        research_batch_service.update_point(
            h.workspace,
            batch_ref,
            point_id,
            status="失败",
            run_ref=run_ref,
            diagnosis=f"结果不可读：{exc.message}"[:500],
        )
        return h._json(
            200, {"finalized": True, "failed": True}
        )
    usable_states = {
        "valid",
        "valid_with_warning",
        "low_resolution",
    }
    result_status = str(projection.get("result_status") or "")
    usable = (
        projection.get("lifecycle_status") == "completed"
        and bool(projection.get("result_id"))
        and result_status in usable_states
    )
    metrics = dict(projection.get("metrics") or {})
    metric_key = str(
        (manifest.get("criterion") or {}).get("metric") or ""
    )
    metric_value = metrics.get(metric_key)
    capacity_view = dict(projection.get("capacity_view") or {})
    capacity_value = capacity_view.get("accredited_capacity_mw")
    if metric_value is None and not metric_key:
        # curve points (duration/penetration) have no criterion:
        # the point scalar IS the run's accredited capacity -
        # None when the search did not resolve (honest gap)
        metric_value = capacity_value
    if usable:
        # SL-01: a seasonal batch's round must carry BOTH
        # segment metrics (the dual-knob append refuses a
        # half-classified round) - compute them from the run's
        # own deficit-event evidence. Any refusal (truncated
        # evidence, missing weights) marks the round 失败
        # honestly: an un-attributable round is not evidence.
        seasonal_extra: dict[str, Any] | None = None
        seasonal_diagnosis: str | None = None
        if _is_seasonal_manifest(manifest):
            seasonal_point = next(
                (
                    p
                    for p in manifest["points"]
                    if p.get("point_id") == point_id
                ),
                {},
            )
            seasonal_params = seasonal_point.get("params") or {}
            try:
                seasonal_metrics = h._service().get_run_seasonal_metrics(
                    str(manifest["project_ref"]),
                    run_ref,
                    heating_months=seasonal_params.get(
                        "heating_months"
                    )
                    or [11, 12, 1, 2, 3],
                    import_batch_id=str(
                        seasonal_point.get("import_batch_id") or ""
                    ),
                    weight_set_id=str(
                        seasonal_point.get("weight_set_id") or ""
                    ),
                )
                seasonal_extra = {
                    "metric_heating": seasonal_metrics["heating"][
                        "lole_days_per_year"
                    ],
                    "metric_non_heating": seasonal_metrics[
                        "non_heating"
                    ]["lole_days_per_year"],
                }
            except WorkbenchError as exc:
                seasonal_diagnosis = (
                    f"季节归属不可得（如实保留）：{exc.message}"
                )[:500]
        if _is_seasonal_manifest(manifest) and seasonal_extra is None:
            done = research_batch_service.update_point(
                h.workspace,
                batch_ref,
                point_id,
                status="失败",
                run_ref=run_ref,
                diagnosis=(
                    seasonal_diagnosis
                    or "季节归属不可得：两季指标缺失，"
                    "按 DR-09 保留为缺口"
                )[:500],
            )
            return h._json(
                200,
                {"finalized": True, "usable": False, "batch": done},
            )
        extra: dict[str, Any] = {
            "metric_value": metric_value,
            "capacity_value": capacity_value,
        }
        if seasonal_extra is not None:
            extra.update(seasonal_extra)
        done = research_batch_service.update_point(
            h.workspace,
            batch_ref,
            point_id,
            status="完成",
            run_ref=run_ref,
            extra=extra,
        )
        return h._json(
            200,
            {
                "finalized": True,
                "usable": True,
                "metric_value": metric_value,
                "batch": done,
            },
        )
    done = research_batch_service.update_point(
        h.workspace,
        batch_ref,
        point_id,
        status="失败",
        run_ref=run_ref,
        diagnosis=(
            f"结果状态 {result_status or '未知'}：未收敛或无效，"
            "按 DR-09 保留为缺口，不插值不补值"
        )[:500],
    )
    return h._json(
        200, {"finalized": True, "usable": False, "batch": done}
    )


@route("GET", "/api/batches/calibration-next")
def batches_calibration_next(h: Any, qs: dict, body: Any) -> None:
    # N2: the pure decision core over the batch's own point
    # history (CAL-04/05/07/08) - no run is admitted here
    ref = (qs.get("ref") or [""])[0]
    try:
        manifest = research_batch_service.read_batch(
            h.workspace, ref
        )
    except research_batch_service.ResearchBatchError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    # SL-01: a seasonal batch runs the DUAL-KNOB decision - the
    # history rebuilds from each round's two segment metrics and
    # the probe carries both scales. No calibrated_baseline_ref
    # (a seasonal convergence suggests an addition structure,
    # never a baseline replacement). Windows come from the
    # batch's axis_config - no invented defaults.
    if _is_seasonal_manifest(manifest):
        try:
            normalized = dict(manifest.get("criterion") or {})
            season = dict(normalized.get("seasonal") or {})
            season["heating"] = float(season["heating"])
            season["non_heating"] = float(season["non_heating"])
            normalized["seasonal"] = season
            seasonal_criterion = (
                calibration_service.validate_seasonal_criterion(
                    normalized
                )
            )
        except calibration_service.CalibrationError as exc:
            return h._json(
                400, {"kind": exc.kind, "message": exc.message}
            )
        config = dict(manifest.get("axis_config") or {})
        heating_window = config.get("heating_interval_percent")
        non_window = config.get("non_heating_interval_percent")
        if not heating_window or not non_window:
            return h._json(
                400,
                {
                    "kind": "invalid_input",
                    "message": (
                        "批次清单缺少季节初始区间"
                        "（axis_config.heating_interval_percent / "
                        "non_heating_interval_percent）"
                    ),
                },
            )
        history = []
        for point in manifest["points"]:
            point_state = point.get("status")
            if point_state not in ("完成", "失败"):
                continue
            params = point.get("params") or {}
            try:
                heating_probe = float(
                    params.get("heating_scale_percent")
                )
                non_probe = float(
                    params.get("non_heating_scale_percent")
                )
            except (TypeError, ValueError):
                return h._json(
                    400,
                    {
                        "kind": "invalid_input",
                        "message": (
                            f"点位 {point.get('point_id')} 缺少"
                            "数值 heating_scale_percent/"
                            "non_heating_scale_percent，"
                            "无法归类季节轮次"
                        ),
                    },
                )
            done = point_state == "完成"
            for key in ("metric_heating", "metric_non_heating"):
                if done and point.get(key) is None:
                    return h._json(
                        400,
                        {
                            "kind": "invalid_input",
                            "message": (
                                f"点位 {point.get('point_id')} "
                                f"缺少数值 {key}：季节标定每轮"
                                "必须携带两季指标"
                            ),
                        },
                    )
            history.append(
                {
                    "heating": calibration_service.classify_round(
                        seasonal_criterion["heating"],
                        heating_probe,
                        point.get("metric_heating") if done else None,
                        run_converged=done,
                        diagnosis=point.get("diagnosis"),
                    ),
                    "non_heating": calibration_service.classify_round(
                        seasonal_criterion["non_heating"],
                        non_probe,
                        point.get("metric_non_heating")
                        if done
                        else None,
                        run_converged=done,
                        diagnosis=point.get("diagnosis"),
                    ),
                }
            )
        decision = calibration_service.seasonal_next_decision(
            seasonal_criterion,
            history,
            heating_interval=(
                float(heating_window[0]),
                float(heating_window[1]),
            ),
            non_heating_interval=(
                float(non_window[0]),
                float(non_window[1]),
            ),
        )
        return h._json(
            200,
            {
                "decision": decision,
                "criterion": seasonal_criterion,
                "history": history,
                "gap_note": calibration_service.gap_picture_note(
                    [r["heating"] for r in history]
                ),
                "calibrated_baseline_ref": None,
            },
        )
    try:
        criterion = calibration_service.validate_criterion(
            (manifest.get("criterion") or {}).get("metric"),
            (manifest.get("criterion") or {}).get("target"),
            (manifest.get("criterion") or {}).get(
                "tolerance_percent", 5.0
            ),
        )
    except calibration_service.CalibrationError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    # R2-C1: rounds are classified ONLY by the CAL-07 decision
    # core (calibration_service.classify_round) - this endpoint
    # must not keep a second copy of the band logic.
    # Axis fields ride on the batch object (contract §1); the
    # webapp axis branch was ratified 2026-09-19 (contract §5
    # freeze lifted for this loop ONLY - no endpoint/wire change).
    axis_key = (
        "perfect_capacity"
        if str(manifest.get("axis") or "") == "perfect_capacity"
        else ""
    )
    axis_arg: Any = None
    param_key = "scale_percent"
    if axis_key:
        axis_arg = {
            "key": axis_key,
            **(manifest.get("axis_config") or {}),
        }
        param_key = "capacity_mw"
    history = []
    try:
        for point in manifest["points"]:
            point_state = point.get("status")
            if point_state not in ("完成", "失败"):
                continue
            params = point.get("params") or {}
            try:
                probe = float(params.get(param_key))
            except (TypeError, ValueError):
                return h._json(
                    400,
                    {
                        "kind": "invalid_input",
                        "message": (
                            f"点位 {point.get('point_id')} 缺少数值 "
                            f"{param_key}，无法归类标定轮次"
                        ),
                    },
                )
            history.append(
                calibration_service.classify_round(
                    criterion,
                    probe,
                    (
                        point.get("metric_value")
                        if point_state == "完成"
                        else None
                    ),
                    run_converged=point_state == "完成",
                    diagnosis=point.get("diagnosis"),
                )
            )
    except calibration_service.CalibrationError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    decision = calibration_service.next_decision(
        criterion, history, axis=axis_arg
    )
    # MR-02: on a CONVERGED calibration, hand the caller a ready
    # calibrated_baseline_ref draft (copied from the batch, no
    # manual transcription); the frozen data version rides on
    # the converged round's own point record. Missing hash ->
    # honest empty string, never a fabricated closure.
    # Load axis ONLY: a perfect-capacity convergence suggests a
    # capacity ADDITION, not a baseline replacement dataset - no
    # calibrated_baseline_ref exists there (method draft §1).
    calibrated_ref = None
    if (
        decision.get("kind") == "converged"
        and decision.get("scale_percent") is not None
    ):
        converged_scale = float(decision.get("scale_percent"))
        converged_point = next(
            (
                p
                for p in manifest["points"]
                if p.get("status") == "完成"
                and float(
                    (p.get("params") or {}).get("scale_percent", "nan")
                )
                == converged_scale
            ),
            None,
        )
        calibrated_ref = {
            "calibration_batch_id": str(manifest.get("batch_ref") or ""),
            "load_scale_percent": converged_scale,
            "criterion_metric": str(criterion.get("metric") or ""),
            "criterion_target_value": float(criterion.get("target") or 0.0),
            "tolerance_percent": float(
                criterion.get("tolerance_percent") or 0.0
            ),
            "data_version_hash": str(
                (converged_point or {}).get("data_version_hash") or ""
            ),
        }
    return h._json(
        200,
        {
            "decision": decision,
            "criterion": criterion,
            "history": history,
            "gap_note": calibration_service.gap_picture_note(
                history
            ),
            "calibrated_baseline_ref": calibrated_ref,
        },
    )


@route("POST", "/api/batches/append-probe")
def batches_append_probe(h: Any, qs: dict, body: Any) -> None:
    # N3' : the decision core's NEXT probe enters the ledger - the
    # service recomputes the decision from the point history and
    # refuses anything that is not its probe (terminal batches,
    # caller-invented scales, duplicates), so the HTTP layer only
    # forwards the intent + the DR-01b authorization holder.
    batch_ref = str(body.get("batch_ref") or "")
    requested_scale = body.get("scale_percent")
    requested_capacity = body.get("capacity_mw")
    requested_heating = body.get("heating_scale_percent")
    requested_non_heating = body.get("non_heating_scale_percent")
    try:
        manifest = research_batch_service.append_point(
            h.workspace,
            batch_ref,
            (
                None
                if requested_scale in (None, "")
                else float(requested_scale)
            ),
            capacity_mw=(
                None
                if requested_capacity in (None, "")
                else float(requested_capacity)
            ),
            heating_scale_percent=(
                None
                if requested_heating in (None, "")
                else float(requested_heating)
            ),
            non_heating_scale_percent=(
                None
                if requested_non_heating in (None, "")
                else float(requested_non_heating)
            ),
            authorized_by=str(body.get("authorized_by") or ""),
        )
    except research_batch_service.ResearchBatchError as exc:
        return h._json(
            400, {"kind": exc.kind, "message": exc.message}
        )
    except (TypeError, ValueError):
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": "scale_percent/capacity_mw 必须为数值",
            },
        )
    new_point = (manifest.get("points") or [])[-1]
    return h._json(
        200,
        {
            "batch_ref": manifest.get("batch_ref"),
            "status": manifest.get("status"),
            "point": {
                "point_id": new_point.get("point_id"),
                "label": new_point.get("label"),
                "params": new_point.get("params"),
                "status": new_point.get("status"),
            },
        },
    )
