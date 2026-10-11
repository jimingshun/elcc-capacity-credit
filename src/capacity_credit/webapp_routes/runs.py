"""运行接口（/api/runs/*、/api/run/snapshot）。任务 D 工作 2 拆出；
零行为变化；导入一律模块级。

路由语义注记：/api/run/snapshot 与 /api/runs/status 在旧巨型 if 链里是
两个不同分支（snapshot 只回状态；status 含完成绑定与相位信息）——
路由表按精确注册分别承载，与旧链的先后遮蔽语义一致。
"""

from __future__ import annotations

import uuid
from typing import Any

from capacity_credit.application.run_config import (
    build_adequacy_config,
    build_efc_config,
    build_elcc_config,
)
from capacity_credit.application import run_config as run_config_module
from capacity_credit.application.workbench_service import WorkbenchError
from capacity_credit.ui import runtime as ui_runtime
from capacity_credit.ui import viewmodel
from capacity_credit.webapp_routes import route


@route("GET", "/api/runs/report")
def runs_report(h: Any, qs: dict, body: Any) -> None:
    """The reliability report download (P-EFC): export through the
    facade (which gates on the CURRENT result identity loop) then
    stream the verified Excel/HTML bytes - the same discipline as
    the cost report, never a fabricated file."""
    ref = (qs.get("project") or [""])[0]
    run = (qs.get("run") or [""])[0]
    fmt = (qs.get("format") or ["xlsx"])[0]
    h._service().export_analysis_report(ref, run)
    downloads = h._service().get_report_downloads(ref, run)
    suffix = ".xlsx" if fmt == "xlsx" else ".html"
    for entry in downloads.get("files", []):
        if str(entry["name"]).endswith(suffix):
            return h._bytes(
                entry["bytes"],
                str(entry["content_type"]),
                str(entry["name"]),
            )
    h._json(
        400,
        {
            "kind": "invalid_input",
            "message": downloads.get("reason") or "报告文件不存在",
        },
    )


@route("GET", "/api/runs/report-view")
def runs_report_view(h: Any, qs: dict, body: Any) -> None:
    # P-EFC UI wiring: read-only search-trace / risk-event
    # projection for the step-04 iteration table and the gap
    # heatmap (same identity loop; honest 400 when unpublished)
    ref = (qs.get("project") or [""])[0]
    run = (qs.get("run") or [""])[0]
    return h._json(
        200, h._service().get_run_report_view(ref, run)
    )


@route("GET", "/api/run/snapshot")
def run_snapshot(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("ref") or [""])[0]
    snap = ui_runtime.get_local_runtime(h.workspace).snapshot(ref)
    return h._json(200, {"status": snap.status})


@route("GET", "/api/runs/status")
def runs_status(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("ref") or [""])[0]
    status_project = (qs.get("project") or [""])[0]
    snap = ui_runtime.get_local_runtime(h.workspace).snapshot(ref)
    if (
        snap.status == "execution_finished"
        and snap.run_ref
        and status_project
    ):
        # P-A: a finished RELIABILITY run binds as the formal
        # result when (and only when) the projection is valid
        # (completed+published+converged - the binder checks).
        # Expected refusals (unconverged/invalid, or a cost run
        # already bound by submit) leave the project honestly
        # unbound and never fail the status poll.
        try:
            h._service().bind_formal_result_from_run(
                status_project,
                domain="reliability",
                run_ref=str(snap.run_ref),
            )
        except WorkbenchError:
            pass
    return h._json(
        200,
        {
            "status": snap.status,
            "phase": snap.phase,
            "run_ref": snap.run_ref,
            "execution_error": snap.execution_error,
        },
    )


@route("GET", "/api/runs/options")
def runs_options(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("project") or [""])[0]
    return h._json(200, h._service().get_run_options(ref))


@route("POST", "/api/runs/start")
def runs_start(h: Any, qs: dict, body: Any) -> None:
    run_config = dict(body.get("config") or {})
    # the adequacy preview builds {"run_config": {...}} while the
    # plain adequacy runner consumes FLAT top-level keys - flatten
    # at this boundary so a UI adequacy start actually works
    if (
        str(body.get("method") or "") == "adequacy"
        and isinstance(run_config.get("run_config"), dict)
        and "scenario_bank_id" not in run_config
    ):
        declared_convergence = run_config.get("convergence")
        run_config = dict(run_config["run_config"])
        # the declared convergence section is a SIBLING of the
        # inner run_config (F4/C5 parity) - keep it on the flat
        # dict so the runner forwards it to the pipeline
        if isinstance(declared_convergence, dict):
            run_config["convergence"] = declared_convergence
        # builder weather_year_weight_set_id -> runner weight_set_id
        if not str(run_config.get("weight_set_id") or ""):
            run_config["weight_set_id"] = run_config.get(
                "weather_year_weight_set_id"
            )
    handle = h._service().start_calculation(
        intent_id=f"web-{uuid.uuid4().hex}",
        project_ref=str(body.get("project_ref") or ""),
        data_ref=str(body.get("data_ref") or ""),
        method=str(body.get("method") or ""),
        config=run_config,
        intent_is_new=True,
    )
    return h._json(
        200,
        {
            "request_ref": str(handle.request_ref),
            "run_ref": getattr(handle, "run_ref", None),
        },
    )


@route("GET", "/api/runs/projection")
def runs_projection(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("project") or [""])[0]
    run = (qs.get("run") or [""])[0]
    projection = h._service().get_run_projection(ref, run)
    return h._json(
        200,
        {
            "projection": projection,
            "view": viewmodel.calculation_result_view(projection),
        },
    )


@route("POST", "/api/runs/preview")
def runs_preview(h: Any, qs: dict, body: Any) -> None:
    run_method = str(body.get("method") or "")
    ref = str(body.get("project_ref") or "")
    data_ref = body.get("data_ref")
    options = h._service().get_run_options(ref)
    if run_method == "adequacy":
        config = build_adequacy_config(
            body.get("form") or {}, options, data_ref
        )
        return h._json(
            200,
            {"config": config, "summary": run_config_module.config_summary(config)},
        )
    if run_method == "elcc":
        config = build_elcc_config(
            body.get("form") or {}, options, data_ref
        )
        return h._json(
            200,
            {"config": config, "summary": run_config_module.config_summary(config)},
        )
    if run_method == "efc":
        config = build_efc_config(
            body.get("form") or {}, options, data_ref
        )
        return h._json(
            200,
            {"config": config, "summary": run_config_module.config_summary(config)},
        )
    if run_method == "demand_target_capacity":
        # Step 2 Stage A（C-D3）：表单即类型化配置载荷——校验走合同层，
        # 无效配置诚实 400（不进入任何运行副作用）
        from capacity_credit.contracts.target_capacity_config import (
            TargetCapacityApplicationConfig,
        )
        from pydantic import ValidationError as _VE

        try:
            typed = TargetCapacityApplicationConfig.model_validate(
                body.get("form") or {}
            )
        except _VE as exc:
            return h._json(
                400,
                {
                    "kind": "invalid_input",
                    "message": f"demand_target_capacity 配置无效：{exc}",
                },
            )
        config = typed.model_dump(mode="json")
        summary = {
            "method": "demand_target_capacity",
            "caliber": typed.caliber,
            "target_metric": typed.target.metric.value,
            "target_value": typed.target.value,
            "target_year": typed.target.target_year,
            "source_project_ref": typed.source.project_ref,
            "source_run_ref": typed.source.run_ref,
        }
        if typed.caliber == "adjust_load":
            # Q-D4：λ 轴（无量纲负荷缩放系数）
            summary["search_bounds_load_scale"] = [
                typed.search.lower_bound_load_scale,
                typed.search.upper_bound_load_scale,
            ]
        else:
            summary["search_bounds_mw"] = [
                typed.search.lower_bound_mw,
                typed.search.upper_bound_mw,
            ]
        return h._json(200, {"config": config, "summary": summary})
    return h._json(
        400,
        {"kind": "invalid_input", "message": f"未知方法 {run_method}"},
    )


@route("GET", "/api/demand/target-capacity/sources")
def target_capacity_sources(h: Any, qs: dict, body: Any) -> None:
    """Step 2 Stage A（C-D3）：来源选择器数据——已完成且已发布的充裕度
    运行（只读派生，不发起计算）。"""
    return h._json(200, h._service().get_target_capacity_sources())


@route("POST", "/api/demand/capacity-gap/preview")
def capacity_gap_preview(h: Any, qs: dict, body: Any) -> None:
    """Step 2 Stage B（C-D4，需求类设计 §4）：确定性算术 preview。
    target_reference（可选）＝已完成目标容量结果的只读引用解析（点值经
    投影身份环读取，未收敛结果拒绝引用）；其余输入直进纯函数。"""
    from capacity_credit.application.capacity_gap_service import (
        CapacityGapError,
        compute_capacity_gap,
    )
    from capacity_credit.application.workbench_service import WorkbenchError

    payload = dict(body or {})
    reference = payload.pop("target_reference", None)
    if reference is not None:
        if not isinstance(reference, dict):
            return h._json(
                400, {"kind": "invalid_input", "message": "target_reference 必须为对象"}
            )
        try:
            projection = h._service().get_run_projection(
                str(reference.get("project_ref") or ""),
                str(reference.get("run_ref") or ""),
            )
        except WorkbenchError as exc:
            return h._json(400, {"kind": "invalid_input", "message": exc.message})
        cv = projection.get("capacity_view") or {}
        if projection.get("method") != "demand_target_capacity":
            return h._json(
                400,
                {"kind": "invalid_input",
                 "message": "引用运行不是目标可靠容量结果（method 不符）"},
            )
        if cv.get("solver_caliber") == "adjust_load":
            # Q-D4（2026-10-10 裁决）：调整负荷口径的结果＝目标负荷水平
            # （λ×峰荷），不是容量需求——两口径并列不可互比，引用诚实拒绝
            return h._json(
                400,
                {"kind": "invalid_input",
                 "message": "引用的目标容量结果是「调整负荷」口径（目标负荷"
                            "水平，非容量需求），不可作为缺口试算的目标可靠"
                            "容量引用；请引用「调整完美容量」口径结果"},
            )
        if cv.get("target_capacity_mw") is None:
            return h._json(
                400,
                {"kind": "invalid_input",
                 "message": "引用的目标可靠容量结果未收敛（无点值），不可引用"},
            )
        payload["target_capacity_mw"] = cv["target_capacity_mw"]
    try:
        result = compute_capacity_gap(payload)
    except CapacityGapError as exc:
        return h._json(400, {"kind": "invalid_input", "message": exc.message})
    return h._json(200, result)


def _resolve_coefficient_references(h: Any, payload: dict) -> tuple[dict | None, dict]:
    """分子/分母引用经既有投影身份环核验（preview 与 freeze 共用同一解析
    纪律）：方法不符/未收敛/未知运行诚实 400；成功则把点值写入 payload 并
    回血缘。返回 (错误体或 None, lineage)。"""
    from capacity_credit.application.workbench_service import WorkbenchError

    lineage: dict = {}

    def _resolve(key: str, field: str, methods: tuple[str, ...], point_key: str,
                 label: str):
        reference = payload.pop(key, None)
        if reference is None:
            return None
        if not isinstance(reference, dict):
            return {"kind": "invalid_input", "message": f"{key} 必须为对象"}
        try:
            projection = h._service().get_run_projection(
                str(reference.get("project_ref") or ""),
                str(reference.get("run_ref") or ""),
            )
        except WorkbenchError as exc:
            return {"kind": "invalid_input", "message": exc.message}
        method = projection.get("method")
        if method not in methods:
            return {"kind": "invalid_input",
                    "message": f"{label}引用运行方法不符（期望 {'/'.join(methods)}，实际 {method}）"}
        cv = projection.get("capacity_view") or {}
        if (
            method == "demand_target_capacity"
            and cv.get("solver_caliber") == "adjust_load"
        ):
            # Q-D4（2026-10-10 裁决）：调整负荷口径＝目标负荷水平（λ×峰荷），
            # 非容量需求——供需系数分子只认「调整完美容量」口径点值
            return {"kind": "invalid_input",
                    "message": "引用的目标容量结果是「调整负荷」口径（目标负荷"
                               "水平，非容量需求），不可作为供需系数分子引用；"
                               "请引用「调整完美容量」口径结果"}
        if cv.get(point_key) is None:
            return {"kind": "invalid_input",
                    "message": f"{label}引用结果未收敛（无点值），不可引用"}
        payload[field] = cv[point_key]
        lineage[key] = {
            "project_ref": reference.get("project_ref"),
            "run_ref": reference.get("run_ref"),
            "point_key": point_key,
        }
        return None

    err = _resolve(
        "numerator_reference", "target_capacity_mw",
        ("demand_target_capacity",), "target_capacity_mw", "分子（目标可靠容量）",
    )
    if err is not None:
        return err, lineage
    err = _resolve(
        "denominator_reference", "accredited_capacity_mw",
        ("elcc", "efc"), "accredited_capacity_mw", "分母（核定可靠容量）",
    )
    if err is not None:
        return err, lineage
    return None, lineage


@route("POST", "/api/demand/coefficient/preview")
def coefficient_preview(h: Any, qs: dict, body: Any) -> None:
    """Step 2 Stage C（C-D5，需求类设计 §5＋Q-D3 六项裁决）：供需系数
    公式 v1＝clamp(目标可靠容量 ÷ 核定可靠容量, 0.0, 1.0)，年度口径。
    分子引用＝demand_target_capacity 已收敛点值；分母引用＝elcc/efc
    认可容量点值；未收敛/方法不符诚实 400。引用路径附 project+run 血缘。
    试算零副作用（持久化冻结见 /freeze，Step 3 S0）。"""
    from capacity_credit.application.capacity_coefficient_service import (
        CapacityCoefficientError,
        compute_capacity_coefficient,
    )

    payload = dict(body or {})
    err, lineage = _resolve_coefficient_references(h, payload)
    if err is not None:
        return h._json(400, err)
    try:
        result = compute_capacity_coefficient(payload)
    except CapacityCoefficientError as exc:
        return h._json(400, {"kind": "invalid_input", "message": exc.message})
    if lineage:
        result["lineage"] = lineage
    return h._json(200, result)


@route("POST", "/api/demand/coefficient/freeze")
def coefficient_freeze(h: Any, qs: dict, body: Any) -> None:
    """Step 3 S0（C-CF1，Q-S6 裁决②）：供需系数冻结为可引用正式结果。
    显式用户动作（确认人必填）；全部校验先于副作用——类型锁/已完成门/
    确认人/输入无效一律 400 且不落盘；冻结记录不可变（manifest-last），
    绑定后项目 completed（一个项目一个正式结果，二次冻结诚实拒绝）。"""
    from capacity_credit import __version__
    from capacity_credit.application.capacity_coefficient_freeze_service import (
        CoefficientFreezeError,
        freeze_capacity_coefficient,
    )
    from capacity_credit.application.workbench_service import _resolve_software_commit

    payload = dict(body or {})
    project_ref = str(payload.pop("project_ref", "") or "")
    confirmer = str(payload.pop("confirmer", "") or "")
    err, lineage = _resolve_coefficient_references(h, payload)
    if err is not None:
        return h._json(400, err)
    try:
        manifest = freeze_capacity_coefficient(
            workspace_root=h.workspace,
            project_ref=project_ref,
            payload=payload,
            lineage=lineage,
            confirmer=confirmer,
            software_version=str(__version__),
            software_commit=_resolve_software_commit(),
        )
    except CoefficientFreezeError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(
        200,
        {
            **manifest,
            "formal_result_bound": True,
        },
    )


@route("GET", "/api/demand/coefficient/list")
def coefficient_list(h: Any, qs: dict, body: Any) -> None:
    """Step 3 S0（C-CF1 列表）：全工作区冻结系数只读列表（结算选定的
    数据源；不代选）。year 查询参数过滤目标年度；损坏记录诚实 invalid 行。"""
    from capacity_credit.application.capacity_coefficient_freeze_service import (
        list_coefficient_freezes,
    )

    year_raw = (qs.get("year") or [""])[0]
    year: int | None = None
    if str(year_raw).strip():
        try:
            year = int(year_raw)
        except ValueError:
            return h._json(
                400, {"kind": "invalid_input", "message": "year 必须为整数年份"}
            )
    return h._json(
        200, {"rows": list_coefficient_freezes(h.workspace, target_year=year)}
    )
