"""结算接口（/api/settlement/*）。Step 3 S1-M1（工单 §2）。

所有导入模块级（本包纪律：函数内导入遮蔽根治面）。
"""

from __future__ import annotations

from typing import Any

from capacity_credit.application.settlement_annual_service import (
    AnnualSummaryError,
    compute_annual_summary,
    export_annual_report,
    publish_annual_summary,
    read_annual_report_downloads,
)
from capacity_credit.application.settlement_monthly_service import (
    MonthlySettlementError,
    adjust_month,
    check_month,
    list_adjustments,
    list_months,
    lock_month,
    trial_month,
    write_month_input,
)
from capacity_credit.application.settlement_service import (
    SettlementServiceError,
    add_reference,
    list_net_cone_references,
    list_reference_options,
    list_references,
    parse_settlement_csv,
    read_price_table,
    read_subjects,
    read_year_config,
    render_settlement_template,
    write_price_table,
    write_subjects,
)
from capacity_credit.webapp_routes import route


@route("GET", "/api/settlement/config")
def get_config(h: Any, qs: dict, body: Any) -> None:
    """年度基础信息只读读回（创建即写入并锁定；未写入→404 诚实）。"""
    ref = (qs.get("project") or [""])[0]
    try:
        config = read_year_config(h.workspace / ref)
    except SettlementServiceError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    if config is None:
        return h._json(
            400,
            {"kind": "invalid_input",
             "message": "该项目未写入年度基础信息（不是结算项目或创建未完成）"},
        )
    return h._json(200, config)


@route("POST", "/api/settlement/price-table")
def post_price_table(h: Any, qs: dict, body: Any) -> None:
    """补偿价表录入（Q-S1：数值>0＋政策来源必填＋rule_version 必填；
    重录＝新版本，历史保留）。"""
    try:
        result = write_price_table(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            compensation_standard=body.get("compensation_standard"),
            policy_source=body.get("policy_source"),
            rule_version=body.get("rule_version"),
            by_resource_type=body.get("by_resource_type"),
        )
    except SettlementServiceError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, result)


@route("GET", "/api/settlement/price-table")
def get_price_table(h: Any, qs: dict, body: Any) -> None:
    """读价表：当前版本＋全部历史（未录入→current None 如实）。"""
    ref = (qs.get("project") or [""])[0]
    return h._json(200, read_price_table(h.workspace, ref))


@route("GET", "/api/settlement/net-cone-references")
def get_net_cone_references(h: Any, qs: dict, body: Any) -> None:
    """Net CONE 正式结果只读列表（价表录入的定价参考；只展示不代填）。"""
    return h._json(200, {"rows": list_net_cone_references(h.workspace)})


@route("POST", "/api/settlement/subjects")
def post_subjects(h: Any, qs: dict, body: Any) -> None:
    """主体台账写入（Step3 S1-M2）：每次修改＝新版本（历史保留）。"""
    try:
        result = write_subjects(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            subjects=body.get("subjects"),
        )
    except SettlementServiceError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, result)


@route("GET", "/api/settlement/subjects")
def get_subjects(h: Any, qs: dict, body: Any) -> None:
    """读台账：当前版本＋全部历史。"""
    ref = (qs.get("project") or [""])[0]
    return h._json(200, read_subjects(h.workspace, ref))


@route("POST", "/api/settlement/references")
def post_reference(h: Any, qs: dict, body: Any) -> None:
    """挂引用（Step3 S1-M2）：五项同口径门聚合逐项校验；口径不一致阻断，
    无法核验＝警告需 confirm_warnings=true 人工确认留痕。"""
    try:
        result = add_reference(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            kind=str(body.get("kind") or ""),
            target=body.get("target"),
            confirm_warnings=bool(body.get("confirm_warnings")),
            confirmer=str(body.get("confirmer") or ""),
        )
    except SettlementServiceError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, result)


@route("GET", "/api/settlement/references")
def get_references(h: Any, qs: dict, body: Any) -> None:
    """读引用清单（未挂引用→空列表）。"""
    ref = (qs.get("project") or [""])[0]
    return h._json(200, {"rows": list_references(h.workspace, ref)})


@route("GET", "/api/settlement/reference-options")
def get_reference_options(h: Any, qs: dict, body: Any) -> None:
    """可引用结果只读清单（冻结系数＋ELCC/EFC＋目标容量＋Net CONE）。"""
    return h._json(200, list_reference_options(h.workspace))


@route("POST", "/api/settlement/month/input")
def post_month_input(h: Any, qs: dict, body: Any) -> None:
    """月度填报（Step3 S2-M1）：写快照＋状态推进待检查（输入变化使既有
    检查失效）；已锁定月拒绝（调整留 S4）。"""
    try:
        result = write_month_input(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            month=int(body.get("month")),
            entries=body.get("entries"),
        )
    except (MonthlySettlementError, TypeError, ValueError) as exc:
        kind = exc.kind if isinstance(exc, MonthlySettlementError) else "invalid_input"
        return h._json(400, {"kind": kind, "message": str(exc)})
    return h._json(200, result)


@route("POST", "/api/settlement/month/check")
def post_month_check(h: Any, qs: dict, body: Any) -> None:
    """月度检查（R-ST-01..08 聚合逐项；零副作用读＋状态推进）。"""
    try:
        result = check_month(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            month=int(body.get("month")),
        )
    except (MonthlySettlementError, TypeError, ValueError) as exc:
        kind = exc.kind if isinstance(exc, MonthlySettlementError) else "invalid_input"
        return h._json(400, {"kind": kind, "message": str(exc)})
    return h._json(200, result)


@route("POST", "/api/settlement/month/trial")
def post_month_trial(h: Any, qs: dict, body: Any) -> None:
    """月度试算（公式 v1）：技术预览——留痕可多次、不改状态、不产生
    正式金额；前置＝检查通过（待确认）。"""
    try:
        result = trial_month(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            month=int(body.get("month")),
        )
    except (MonthlySettlementError, TypeError, ValueError) as exc:
        kind = exc.kind if isinstance(exc, MonthlySettlementError) else "invalid_input"
        return h._json(400, {"kind": kind, "message": str(exc)})
    return h._json(200, result)


@route("GET", "/api/settlement/months")
def get_months(h: Any, qs: dict, body: Any) -> None:
    """12 月状态一览（工作台网格数据源）。"""
    ref = (qs.get("project") or [""])[0]
    try:
        months = list_months(h.workspace, ref)
    except SettlementServiceError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, {"months": months})


@route("POST", "/api/settlement/month/lock")
def post_month_lock(h: Any, qs: dict, body: Any) -> None:
    """确认锁定（Step3 S2-M2，显式用户动作）：锁人必填＋时间＋输入哈希；
    前置＝待确认；重复锁定与已锁定直改均诚实拒绝。"""
    try:
        result = lock_month(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            month=int(body.get("month")),
            locked_by=str(body.get("locked_by") or ""),
        )
    except (MonthlySettlementError, TypeError, ValueError) as exc:
        kind = exc.kind if isinstance(exc, MonthlySettlementError) else "invalid_input"
        return h._json(400, {"kind": kind, "message": str(exc)})
    return h._json(200, result)


@route("GET", "/api/settlement/summary")
def get_summary(h: Any, qs: dict, body: Any) -> None:
    """年度汇总读取（Step3 S3）：已发布→正式结果；未发布→预览（如实
    非正式＋进行中计数）。"""
    ref = (qs.get("project") or [""])[0]
    try:
        return h._json(200, compute_annual_summary(h.workspace, ref))
    except SettlementServiceError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})


@route("POST", "/api/settlement/summary/publish")
def post_summary_publish(h: Any, qs: dict, body: Any) -> None:
    """年度汇总发布（Step3 S3）：12/12 门＋一次性不可变＋绑定项目正式
    结果（completed 后月度动作全关）。"""
    try:
        result = publish_annual_summary(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            published_by=str(body.get("published_by") or ""),
        )
    except AnnualSummaryError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._json(200, result)


@route("POST", "/api/settlement/month/adjust")
def post_month_adjust(h: Any, qs: dict, body: Any) -> None:
    """锁定后调整（Step3 S4）：追加式；原因/调整人必填；差额有符号；
    状态转「已锁定·有调整」；发布后只读拒绝。"""
    try:
        result = adjust_month(
            workspace_root=h.workspace,
            project_ref=str(body.get("project_ref") or ""),
            month=int(body.get("month")),
            subject_id=str(body.get("subject_id") or ""),
            delta_mw=body.get("delta_mw"),
            delta_amount=body.get("delta_amount"),
            reason=str(body.get("reason") or ""),
            adjuster=str(body.get("adjuster") or ""),
        )
    except (MonthlySettlementError, TypeError, ValueError) as exc:
        kind = exc.kind if isinstance(exc, MonthlySettlementError) else "invalid_input"
        return h._json(400, {"kind": kind, "message": str(exc)})
    return h._json(200, result)


@route("GET", "/api/settlement/adjustments")
def get_adjustments(h: Any, qs: dict, body: Any) -> None:
    """调整追溯清单（时间线；month 过滤可选）。"""
    ref = (qs.get("project") or [""])[0]
    month_raw = (qs.get("month") or [""])[0]
    month = int(month_raw) if str(month_raw).strip() else None
    try:
        rows = list_adjustments(h.workspace, ref, month)
    except (SettlementServiceError, TypeError, ValueError) as exc:
        kind = exc.kind if isinstance(exc, SettlementServiceError) else "invalid_input"
        return h._json(400, {"kind": kind, "message": str(exc)})
    return h._json(200, {"rows": rows})


@route("GET", "/api/settlement/report")
def get_report(h: Any, qs: dict, body: Any) -> None:
    """年度汇总报告导出（C-S5 年度口径；设计 §7.2）：Excel/HTML 经报告
    服务同款纪律（同身份重放、清单哈希校验）；只对已发布正式结果出报告。
    month= 月度报告暂未实施（诚实拒绝，不冒充）。"""
    ref = (qs.get("project") or [""])[0]
    fmt = (qs.get("format") or ["xlsx"])[0]
    month_raw = (qs.get("month") or [""])[0]
    if str(month_raw).strip():
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": "月度报告导出暂未实施（当前仅支持 annual 年度汇总报告）",
            },
        )
    try:
        export_annual_report(workspace_root=h.workspace, project_ref=ref)
        downloads = read_annual_report_downloads(
            workspace_root=h.workspace, project_ref=ref
        )
    except AnnualSummaryError as exc:
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
        {
            "kind": "invalid_input",
            "message": downloads.get("reason") or "报告文件不存在",
        },
    )


@route("GET", "/api/settlement/template")
def get_template(h: Any, qs: dict, body: Any) -> None:
    """台账/考核 CSV 模板下载（收尾欠账④；设计 §6 模板走既有模板目录
    模式）：表头可填写 CSV（utf-8-sig）；未知类型诚实拒绝。"""
    kind = (qs.get("kind") or [""])[0]
    try:
        file_name, payload = render_settlement_template(kind)
    except SettlementServiceError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    return h._bytes(payload, "text/csv; charset=utf-8", file_name)


@route("POST", "/api/settlement/import")
def post_import(h: Any, qs: dict, body: Any) -> None:
    """台账/考核 CSV 批量导入（收尾欠账④）：解析后走既有
    write_subjects / write_month_input（单一 Schema，版本化/状态机
    语义不变）；坏 CSV 诚实拒绝并指明行。"""
    kind = str(body.get("kind") or "")
    try:
        rows = parse_settlement_csv(kind, body.get("csv"))
        if kind == "subjects":
            result = write_subjects(
                workspace_root=h.workspace,
                project_ref=str(body.get("project_ref") or ""),
                subjects=rows,
            )
        else:
            month = body.get("month")
            try:
                month = int(month)
            except (TypeError, ValueError):
                raise SettlementServiceError(
                    "invalid_input", "月度导入必须给出月份（month=1..12）"
                ) from None
            if not 1 <= month <= 12:
                raise SettlementServiceError(
                    "invalid_input", f"月份必须在 1—12（实际 {month}）"
                )
            result = write_month_input(
                workspace_root=h.workspace,
                project_ref=str(body.get("project_ref") or ""),
                month=month,
                entries=rows,
            )
    except (SettlementServiceError, MonthlySettlementError) as exc:
        kind_label = exc.kind if hasattr(exc, "kind") else "invalid_input"
        return h._json(400, {"kind": kind_label, "message": exc.message})
    return h._json(200, result)
