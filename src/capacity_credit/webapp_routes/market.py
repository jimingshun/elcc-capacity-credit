"""容量市场仿真接口（/api/market/*）。Q-M1—Q-M6 裁决（2026-10-11）
落地；主责设计 docs/plans/2026-10-11-market-clearing-kernel-design.md。

镜像既有纪律：引用经身份环核验（目标容量＝demand_target_capacity 已收敛
点值且非调整负荷口径；Net CONE＝cost_net_cone 已完成点值）；台账版本化；
预览零副作用可多次；冻结显式动作一次性不可变；报告走报告服务同款链。
"""

from __future__ import annotations

from typing import Any

from capacity_credit.application.market_service import (
    MarketError,
    build_demand_curve,
    compute_clearing,
    freeze_clearing_result,
    import_offers_csv,
    read_offers,
    render_offers_template,
    write_offers,
)
from capacity_credit.webapp_routes import route


def _resolve_curve_inputs(h: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """曲线输入解析：target/net_cone 各自 引用/显式 二选一（引用经身份环；
    调整负荷口径的目标容量结果是负荷水平非容量需求——同缺口门纪律拒绝）。"""
    from capacity_credit.application.workbench_service import WorkbenchError

    target_reference = payload.pop("target_reference", None)
    if target_reference is not None:
        if not isinstance(target_reference, dict):
            raise MarketError("invalid_input", "target_reference 必须为对象")
        try:
            projection = h._service().get_run_projection(
                str(target_reference.get("project_ref") or ""),
                str(target_reference.get("run_ref") or ""),
            )
        except WorkbenchError as exc:
            raise MarketError("invalid_input", exc.message) from None
        cv = projection.get("capacity_view") or {}
        if projection.get("method") != "demand_target_capacity":
            raise MarketError(
                "invalid_input",
                "目标容量引用运行方法不符（期望 demand_target_capacity，"
                f"实际 {projection.get('method')}）",
            )
        if cv.get("solver_caliber") == "adjust_load":
            raise MarketError(
                "invalid_input",
                "引用的目标容量结果是「调整负荷」口径（目标负荷水平，非容量"
                "需求），不可作为市场需求曲线锚；请引用「调整完美容量」口径结果",
            )
        if cv.get("target_capacity_mw") is None:
            raise MarketError(
                "invalid_input", "引用的目标容量结果未收敛（无点值），不可引用"
            )
        payload["target_mw"] = cv["target_capacity_mw"]

    cone_reference = payload.pop("net_cone_reference", None)
    if cone_reference is not None:
        if not isinstance(cone_reference, dict):
            raise MarketError("invalid_input", "net_cone_reference 必须为对象")
        try:
            record = h._service().get_cost_run_projection(
                str(cone_reference.get("project_ref") or ""),
                str(cone_reference.get("run_ref") or ""),
            )
        except WorkbenchError as exc:
            raise MarketError("invalid_input", exc.message) from None
        manifest = record.get("manifest") or {}
        result = record.get("result") or {}
        if str(manifest.get("cost_kind")) != "cost_net_cone":
            raise MarketError(
                "invalid_input",
                "Net CONE 引用运行类型不符（期望 cost_net_cone，"
                f"实际 {manifest.get('cost_kind')}）",
            )
        if result.get("net_cone_raw") is None:
            raise MarketError(
                "invalid_input", "引用的 Net CONE 结果缺失点值（未测算），不可引用"
            )
        payload["net_cone"] = result["net_cone_raw"]
    return payload


def _curve_from_payload(h: Any, body: dict[str, Any]) -> dict[str, Any]:
    """曲线输入解析（白名单键——offers/min_procurement 等出清键不进曲线）：
    target/net_cone 各自 引用/显式 二选一（引用经身份环）。"""
    curve_keys = (
        "target_mw",
        "net_cone",
        "anchors",
        "cap",
        "floor",
        "quantity_deviation_percent",
        "target_reference",
        "net_cone_reference",
    )
    payload = {k: body[k] for k in curve_keys if k in (body or {})}
    payload = _resolve_curve_inputs(h, payload)
    return build_demand_curve(**payload)


@route("POST", "/api/market/curve")
def post_curve(h: Any, qs: dict, body: Any) -> None:
    """VRR 需求曲线构造（Q-M3）：锚＝目标容量×Net CONE（引用或显式）；
    默认形状 PJM 参照可改；领口可选不填不启用。"""
    try:
        curve = _curve_from_payload(h, body)
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, curve)


@route("GET", "/api/market/offers")
def get_offers(h: Any, qs: dict, body: Any) -> None:
    """读报价台账：当前版本＋全部历史（未录入→current None 如实）。"""
    ref = (qs.get("project") or [""])[0]
    try:
        return h._json(200, read_offers(h.workspace, ref))
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})


@route("POST", "/api/market/offers")
def post_offers(h: Any, qs: dict, body: Any) -> None:
    """报价台账写入（Q-M4：申报≤核定，超出阻断）：每次修改＝新版本。"""
    try:
        result = write_offers(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            rows=body.get("offers"),
        )
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, result)


@route("GET", "/api/market/offers/template")
def get_offers_template(h: Any, qs: dict, body: Any) -> None:
    """报价 CSV 模板下载（表头可填写；无示例行——示例价不得入正式链路）。"""
    file_name, payload = render_offers_template()
    return h._bytes(payload, "text/csv; charset=utf-8", file_name)


@route("POST", "/api/market/offers/import")
def post_offers_import(h: Any, qs: dict, body: Any) -> None:
    """报价 CSV 批量导入（缺列/非数值/未知列指明行拒绝；空值＝缺失不补 0）。"""
    try:
        result = import_offers_csv(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            csv_text=str(body.get("csv") or ""),
        )
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, result)


def _offers_for_clearing(h: Any, body: dict[str, Any]) -> tuple[list[Any], str]:
    """出清报价来源：payload offers 优先；缺省用当前台账版本（无台账诚实拒）。"""
    offers = body.get("offers")
    if offers is not None:
        return offers, "payload"
    ledger = read_offers(h.workspace, str(body.get("project_ref") or ""))
    current = ledger.get("current")
    if not current or not current.get("offers"):
        raise MarketError(
            "invalid_input",
            "报价台账为空：请先录入/导入报价，或在请求中直接给 offers",
        )
    return current["offers"], f"ledger:v{current.get('version')}"


@route("POST", "/api/market/clearing/preview")
def post_clearing_preview(h: Any, qs: dict, body: Any) -> None:
    """出清预览（Q-M2/Q-M5，零副作用可多次）：曲线＋报价（payload 或当前
    台账）→ 统一出清价/中标清单/缺口/市场力（Q-M1 披露不阻断）/可选缓解对比
    ＋出清图数据（堆栈阶梯＋曲线点列）。"""
    try:
        curve = _curve_from_payload(h, body)
        offers, offers_source = _offers_for_clearing(h, body)
        min_proc = body.get("min_procurement_mw")
        result = compute_clearing(
            curve=curve,
            offers=offers,
            min_procurement_mw=min_proc,
            mitigation_reference_cost=body.get("mitigation_reference_cost"),
        )
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    except TypeError:
        return h._json(
            400,
            {"kind": "invalid_input", "message": "最低采购要求（min_procurement_mw）必填"},
        )
    result["offers_source"] = offers_source
    return h._json(200, result)


@route("POST", "/api/market/clearing/freeze")
def post_clearing_freeze(h: Any, qs: dict, body: Any) -> None:
    """冻结正式出清结果（Q-M6）：显式动作（确认人＋rule_version 必填）；
    一次性不可变＋绑定项目正式结果（重冻结诚实拒绝）。"""
    from capacity_credit import __version__
    from capacity_credit.application.workbench_service import _resolve_software_commit

    body = dict(body or {})
    project_ref = str(body.pop("project_ref", "") or "")
    confirmer = str(body.pop("confirmer", "") or "")
    rule_version = str(body.pop("rule_version", "") or "")
    mitigation_reference_cost = body.pop("mitigation_reference_cost", None)
    min_proc = body.pop("min_procurement_mw", None)
    offers_raw = body.pop("offers", None)
    try:
        curve = _curve_from_payload(h, body)
        offers, _source = _offers_for_clearing(
            h,
            {
                "project_ref": project_ref,
                "offers": offers_raw,
            },
        )
        manifest = freeze_clearing_result(
            workspace_root=h.workspace,
            project_ref=project_ref,
            curve_payload={
                "target_mw": curve["target_mw"],
                "net_cone": curve["net_cone"],
                "anchors": curve["anchors"],
                "cap": curve["cap"],
                "floor": curve["floor"],
                "quantity_deviation_percent": curve[
                    "quantity_deviation_percent"
                ],
            },
            offers=offers,
            min_procurement_mw=min_proc,
            rule_version=rule_version,
            confirmer=confirmer,
            mitigation_reference_cost=mitigation_reference_cost,
            software_version=str(__version__),
            software_commit=_resolve_software_commit(),
        )
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    except TypeError:
        return h._json(
            400,
            {"kind": "invalid_input", "message": "最低采购要求（min_procurement_mw）必填"},
        )
    return h._json(200, manifest)


@route("GET", "/api/market/clearing")
def get_clearing(h: Any, qs: dict, body: Any) -> None:
    """读冻结出清结果（只读投影：manifest＋formal；未冻结→frozen=None 如实）。"""
    from capacity_credit.application.market_service import read_clearing_result

    ref = (qs.get("project") or [""])[0]
    try:
        record = read_clearing_result(h.workspace / ref)
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    if record is None:
        return h._json(200, {"frozen": False})
    if record.get("invalid"):
        return h._json(400, {"kind": "invalid_input",
                             "message": record.get("invalid_reason")})
    return h._json(200, {"frozen": True, "manifest": record["manifest"],
                         "result": record["formal"]})


@route("GET", "/api/market/report")
def get_report(h: Any, qs: dict, body: Any) -> None:
    """出清报告导出（Q-M6）：Excel/HTML 经报告服务同款纪律（同身份重放、
    清单哈希校验）；只对已冻结正式结果出报告。"""
    from capacity_credit.application.market_service import (
        export_market_report,
        read_market_report_downloads,
    )

    ref = (qs.get("project") or [""])[0]
    fmt = (qs.get("format") or ["xlsx"])[0]
    try:
        export_market_report(workspace_root=h.workspace, project_ref=ref)
        downloads = read_market_report_downloads(
            workspace_root=h.workspace, project_ref=ref
        )
    except MarketError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    suffix = ".xlsx" if fmt == "xlsx" else ".html"
    for entry in downloads.get("files", []):
        if str(entry["name"]).endswith(suffix):
            return h._bytes(
                entry["bytes"],
                str(entry["content_type"]),
                str(entry["name"]),
            )
    return h._json(
        400,
        {"kind": "invalid_input", "message": downloads.get("reason") or "报告文件不存在"},
    )
