"""P-B server-side graded check (four-module design 2026-09-16 §7-C7).

ONE server authority for the pre-flight system check: the page renders
THIS result (the old in-page JS rule copy is retired), while the cost
engines stay the authoritative final gate at submission. Properties:

- GRADED: blockers stop the run; warnings never do. Every issue names
  the rule, field, group, current value, legal range and a suggestion,
  so "前往修改" can locate the exact field;
- AGGREGATED: the Net CONE internal-reference caliber gate lists EVERY
  mismatched item with both sides (the engine's fail-fast gate stays
  as-is; this is the review view of the same contract);
- ZERO SIDE EFFECTS: a check reads at most (the referenced Gross run)
  and never creates runs, batches or registry writes;
- USER UNITS: bounds are expressed in the units the user typed
  (percents stay percents) - the payload percent conversion happens
  exactly once at submission, not here.

Defaults mirror the form prefill (the same numbers the payload
builders fall back to); a blank numeric therefore checks as its
default exactly as a submission would.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from capacity_credit.application.workbench_service import WorkbenchError
from capacity_credit.costs.catalog import (
    machine_catalog,
    province_coefficients,
    zone_coefficients,
)
from capacity_credit.costs.net_cone import _CALIBER_PAIRS

_LEVEL_BLOCKER = "blocker"
_LEVEL_WARNING = "warning"


def _issue(
    level: str,
    rule: str,
    field: str,
    group: str,
    label: str,
    current: Any,
    reason: str,
    range_: str,
    suggestion: str,
) -> dict[str, Any]:
    return {
        "level": level,
        "rule": rule,
        "field": field,
        "group": group,
        "label": label,
        "current": "" if current is None else str(current),
        "reason": reason,
        "range": range_,
        "suggestion": suggestion,
    }


def _raw(form: MappingLike, name: str, default: Any) -> Any:
    """The effective value of one form field (blank → the same default
    a submission would apply)."""
    value = form.get(name)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    return value


def _num(form: MappingLike, name: str, default: float) -> float | None:
    value = _raw(form, name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(form: MappingLike, name: str, default: float) -> float | None:
    value = _num(form, name, default)
    if value is None or float(value) != int(value):
        return None
    return float(value)


def _text(form: MappingLike, name: str) -> str:
    value = form.get(name)
    return str(value).strip() if value is not None else ""


class MappingLike(dict):
    """Typing alias only (the form is a plain mapping)."""


# ---- fixed cost (R-FX-*) ------------------------------------------------

_FIXED_DEFAULTS = {
    "price_index": 1.0, "equity": 20.0, "loan": 4.3, "loan_years": 15,
    "cyears": 2, "dep": 15, "salvage": 5.0, "op": 30, "form": 95.0,
    "om": 71.0, "ins": 0.25, "amr": 5.0, "amy": 8, "vat": 13.0,
}


def _check_fixed(form: MappingLike, project_dir: Path) -> tuple[list, list]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def bad(field, label, current, reason, range_, suggestion):
        blockers.append(_issue(_LEVEL_BLOCKER, "bound", field,
                               "参数", label, current, reason, range_,
                               suggestion))

    machines = machine_catalog()
    machine = _text(form, "machine")
    if not machine:
        bad("machine", "参考机型", "（未选）", "参考机型不能为空。",
            "目录机型或自定义机型", "请从机型目录选择，或选“自定义机型”并填写参数。")
    elif machine == "__custom__":
        # P-C custom machine: the free fields become required facts
        if not _text(form, "cm_name"):
            bad("cm_name", "自定义机型名称", "（未填）",
                "自定义机型必须命名（作为结果中的机型标识）。", "非空文本", "请填写机型名称。")
        cm_cost = _num(form, "cm_cost", None)
        if cm_cost is None or cm_cost <= 0:
            bad("cm_cost", "自定义机型基准造价（元/kW）", _raw(form, "cm_cost", "（未填）"),
                "自定义机型必须提供大于0的基准造价。", ">0", "如 3750 元/kW（示例口径，需核实）。")
        cm_cap = _int(form, "cm_cap", None)
        if cm_cap is None or cm_cap <= 0:
            bad("cm_cap", "自定义机型建档容量（MW）", _raw(form, "cm_cap", "（未填）"),
                "自定义机型必须提供正整数建档容量。", ">0 的整数", "如 1320（2×660MW）。")
    elif machine not in machines:
        bad("machine", "参考机型", machine, "未知机型：引擎对目录外机型硬拒绝。",
            "目录机型：" + "／".join(sorted(machines)), "请从机型目录选择。")
    province = _text(form, "province")
    provinces = province_coefficients()
    if province not in provinces:
        bad("province", "目标省份", province or "（未填）",
            "未知省份（目录外省份提交将被拒绝）。", "目录省份：" + "／".join(sorted(provinces)),
            "请从省份目录选择。")
    else:
        zone = _text(form, "zone") or "全省"
        if f"{province}|{zone}" not in zone_coefficients():
            bad("zone", "目标分区", zone,
                "该省未配置此分区映射（引擎不回退通用值）。",
                f"{province} 的目录分区（当前：全省）", "请选择目录内分区或扩展分区目录。")
    price_index = _num(form, "price_index", _FIXED_DEFAULTS["price_index"])
    if price_index is None:
        bad("price_index", "造价指数换算系数", _raw(form, "price_index", ""),
            "必须为数值（倍数，不是百分比）。", ">0 的倍数", "如 1.05 表示上浮5%。")
    elif price_index <= 0:
        bad("price_index", "造价指数换算系数", price_index,
            "造价指数必须大于0。", ">0 的倍数", "1.00 表示不调整。")
    elif price_index != 1.0:
        warnings.append(_issue(_LEVEL_WARNING, "warn_price_index", "price_index",
                               "参数", "造价指数换算系数", price_index,
                               "造价指数不是1.00：请确认这是有依据的调整。",
                               "1.00=不调整", "确认调整依据后可继续。"))
    for name, label, low, high in (
        ("cyears", "建设期（年）", 1, 10), ("loan_years", "还贷年限（年）", 1, 60),
        ("dep", "综合折旧年限（年）", 1, 60), ("op", "经营期（年）", 1, 60),
        ("amy", "摊销年限（年）", 1, 60),
    ):
        value = _int(form, name, _FIXED_DEFAULTS[name])
        if value is None:
            bad(name, label, _raw(form, name, ""), "必须为整数。",
                f"{low}—{high} 的整数", f"请填写 {low}—{high} 的整数。")
        elif not low <= value <= high:
            bad(name, label, value, f"必须在 {low}—{high}（整数）范围内。",
                f"{low}—{high} 的整数", f"请填写 {low}—{high} 的整数。")
    op = _int(form, "op", _FIXED_DEFAULTS["op"]) or _FIXED_DEFAULTS["op"]
    loan_years = _int(form, "loan_years", _FIXED_DEFAULTS["loan_years"])
    if loan_years is not None and loan_years > op:
        bad("loan_years", "还贷年限（年）", loan_years,
            f"还贷年限不得超过经营期（当前 {int(op)} 年）。", f"1—经营期（{int(op)}）的整数",
            "请缩短还贷年限或延长经营期。")
    amy = _int(form, "amy", _FIXED_DEFAULTS["amy"])
    if amy is not None and amy > op:
        bad("amy", "摊销年限（年）", amy,
            f"摊销年限不得超过经营期（当前 {int(op)} 年）。", f"1—经营期（{int(op)}）的整数",
            "请缩短摊销年限或延长经营期。")
    for name, label in (
        ("equity", "资本金比例（%）"), ("salvage", "残值率（%）"),
        ("form", "固定资产形成率（%）"), ("ins", "保险费率（%）"),
        ("amr", "摊销比例（%）"), ("vat", "增值税率（%）"),
    ):
        value = _num(form, name, _FIXED_DEFAULTS[name])
        if value is None:
            bad(name, label, _raw(form, name, ""), "必须为数值。",
                "0—100", "按百分数填写（如 20 表示 20%）。")
        elif not 0 <= value <= 100:
            bad(name, label, value, "比例必须在 0—100 之间（百分数）。",
                "0—100", "按百分数填写（如 20 表示 20%）。")
    loan = _num(form, "loan", _FIXED_DEFAULTS["loan"])
    if loan is None:
        bad("loan", "贷款利率（%）", _raw(form, "loan", ""),
            "必须为数值。", "≥0（正式合同上限待定义）", "0 为合法输入。")
    elif loan < 0:
        bad("loan", "贷款利率（%）", loan, "贷款利率不得小于0。",
            "≥0（正式合同上限待定义）", "0 为合法输入。")
    elif loan > 10:
        warnings.append(_issue(_LEVEL_WARNING, "warn_loan", "loan", "参数",
                               "贷款利率（%）", loan,
                               "贷款利率超过10%：正式合同上限定义前请复核。",
                               "≥0", "确认利率来源后可继续。"))
    om = _num(form, "om", _FIXED_DEFAULTS["om"])
    if om is None or om < 0:
        bad("om", "固定运维费（元/kW·年）", _raw(form, "om", ""),
            "固定运维费不得小于0。", "≥0", "不适用请填0。")
    form_rate = _num(form, "form", _FIXED_DEFAULTS["form"])
    amr = _num(form, "amr", _FIXED_DEFAULTS["amr"])
    if form_rate is not None and amr is not None and form_rate + amr > 100:
        bad("amr", "摊销比例（%）", amr,
            "固定资产形成率与摊销比例合计不得超过100%（同一投资不得重复分配）。",
            "形成率+摊销比例 ≤ 100", "请调低其中一项。")
    cyears = int(_int(form, "cyears", _FIXED_DEFAULTS["cyears"]) or 0)
    schedule: list[float] = []
    for index in range(cyears):
        value = _num(form, f"w{index}", 0.0)
        if value is None or value < 0:
            bad(f"w{index}", f"第{index + 1}年投资比例（%）",
                _raw(form, f"w{index}", ""), "投资比例必须为非负数值。",
                "0—100", "按百分数填写。")
            schedule.append(-1.0)
        else:
            schedule.append(value)
    if schedule and not any(v < 0 for v in schedule):
        total = sum(schedule)
        if abs(total - 100.0) > 1e-6:
            bad("cyears", "逐年投资比例（合计）", f"{total:.2f}%",
                f"逐年投资比例合计必须等于100%（当前 {total:.2f}%）。",
                "合计 = 100%", "请调整各年比例使合计为100%。")
    return blockers, warnings


# ---- Gross CONE (R-GC-*) ------------------------------------------------

_GROSS_DEFAULTS = {
    "ty": 2030, "pby": 2026, "y": 2, "n": 30, "d": 7.0, "tax": 25.0,
    "dep": 15, "form": 100.0, "om": 0.0, "res": 0.0, "wc": 0.0,
    "cap": 0.0,
}


def _check_gross(form: MappingLike, project_dir: Path) -> tuple[list, list]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def bad(field, label, current, reason, range_, suggestion):
        blockers.append(_issue(_LEVEL_BLOCKER, "bound", field,
                               "参数", label, current, reason, range_,
                               suggestion))

    for name, label in (("obj", "对象名称"), ("region", "地区")):
        if not _text(form, name):
            bad(name, label, "（未填）", f"{label}不能为空（缺失＝未测算）。",
                "非空文本", "请填写测算对象与地区。")
    for name, label in (("ty", "投产/交付年度"), ("pby", "价格基准年")):
        value = _int(form, name, _GROSS_DEFAULTS[name])
        if value is None or value < 1900:
            bad(name, label, _raw(form, name, ""), "必须为≥1900的整数年份。",
                "≥1900 的整数", "如 2030。")
    y = _int(form, "y", _GROSS_DEFAULTS["y"])
    if y is None or not 1 <= y <= 10:
        bad("y", "建设期（年）", _raw(form, "y", ""),
            "建设期必须为1—10的整数。", "1—10 的整数", "按投资笔数填写。")
        y = int(_GROSS_DEFAULTS["y"])
    y = int(y)
    investments: list[float] = []
    for index in range(y):
        value = _num(form, f"i{index}", 0.0)
        if value is None or value < 0:
            bad(f"i{index}", f"第{index + 1}年投资（元）",
                _raw(form, f"i{index}", ""), "分年投资必须为非负数值（元）。",
                "≥0", "不适用年份填0。")
        investments.append(value if value is not None else -1.0)
    if not any(v < 0 for v in investments) and sum(investments) <= 0:
        bad("y", "分年投资（合计）", sum(investments),
            "分年投资合计必须大于0。", ">0", "请填写各年投资额。")
    n = _int(form, "n", _GROSS_DEFAULTS["n"])
    if n is None or not 1 <= n <= 60:
        bad("n", "经营期（年）", _raw(form, "n", ""),
            "经营期必须为1—60的整数。", "1—60 的整数", "如 30。")
    d = _num(form, "d", _GROSS_DEFAULTS["d"])
    if d is None or d < 0:
        bad("d", "折现率（%）", _raw(form, "d", ""),
            "折现率不得小于0。", "0—1000（%），正式合同上限待定义", "税后名义口径。")
    tax = _num(form, "tax", _GROSS_DEFAULTS["tax"])
    if tax is None or not 0 <= tax < 99.9:
        bad("tax", "所得税率（%）", _raw(form, "tax", ""),
            "所得税率必须在 0—99.9 之间。", "0—99.9", "如 25。")
    dep = _int(form, "dep", _GROSS_DEFAULTS["dep"])
    if dep is None or not 1 <= dep <= 60:
        bad("dep", "税务折旧年限（年）", _raw(form, "dep", ""),
            "必须为1—60的整数。", "1—60 的整数", "直线法。")
    frm = _num(form, "form", _GROSS_DEFAULTS["form"])
    if frm is None or not 0 <= frm <= 100:
        bad("form", "固定资产形成率（%）", _raw(form, "form", ""),
            "必须在 0—100 之间。", "0—100", "如 100。")
    for name, label in (
        ("om", "固定运维费（元/kW·年）"), ("res", "残值（元）"),
        ("wc", "营运资金（元）"),
    ):
        value = _num(form, name, _GROSS_DEFAULTS[name])
        if value is None or value < 0:
            bad(name, label, _raw(form, name, ""),
                f"{label}原则上不得小于0（不适用请显式填0）。", "≥0", "不适用请填0。")
    cap = _num(form, "cap", _GROSS_DEFAULTS["cap"])
    if cap is None or cap <= 0:
        bad("cap", "装机容量（kW）", _raw(form, "cap", ""),
            "装机容量必须大于0。", ">0（kW）", "如 2000000 kW（2000 MW）。")
    return blockers, warnings


# ---- Net CONE (R-NC-* incl. the AGGREGATED caliber gate) ----------------

_NET_MONEY = (
    ("spot", "现货电能量收入（元/年）", True),
    ("fuel", "燃料成本（元/年）", False), ("vom", "变动运维成本（元/年）", False),
    ("emis", "排放成本（元/年）", False),
    ("start", "启动及空载等其他变动成本（元/年）", False),
    ("contract", "合约差价净结算（元/年）", True),
    ("reserve", "备用服务收入（元/年）", True), ("freq", "调频服务收入（元/年）", True),
    ("other", "其他适用辅助服务收入（元/年）", True),
    ("anccost", "辅助服务额外增量成本（元/年）", False),
)


def _check_net(form: MappingLike, project_dir: Path) -> tuple[list, list]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def bad(field, label, current, reason, range_, suggestion, rule="bound"):
        blockers.append(_issue(_LEVEL_BLOCKER, rule, field,
                               "参数" if rule != "caliber" else "同口径核对",
                               label, current, reason, range_, suggestion))

    for name, label in (
        ("scenario", "来源情景/编号"), ("unit", "参考机组"),
        ("region", "地区/节点/分区"), ("source", "E&AS数据来源说明"),
    ):
        if not _text(form, name):
            bad(name, label, "（未填）", f"{label}不能为空（缺失＝未测算）。",
                "非空文本", "请填写。")
    capacity = _num(form, "capacity", 0.0)
    if capacity is None or capacity <= 0:
        bad("capacity", "容量分母（kW）", _raw(form, "capacity", ""),
            "容量分母必须为大于0的数值并显式录入。", ">0（kW）", "与Gross侧同口径同数值。")
    for name, label, signed in _NET_MONEY:
        raw = form.get(name)
        blank = raw is None or (isinstance(raw, str) and raw.strip() == "")
        if blank:
            bad(name, label, "（留空）",
                "留空＝未测算，提交将被拒绝（缺失不按0处理）。",
                "数值（" + ("可有符号" if signed else "非负") + "）",
                "无该项业务请显式填0。")
            continue
        value = _num(form, name, 0.0)
        if value is None:
            bad(name, label, raw, "必须为数值。", "数值", "请填写数值。")
        elif not signed and value < 0:
            bad(name, label, value, f"{label}为成本类字段，不得为负。", "≥0",
                "负值仅收入类字段允许。")
    g_mode = _text(form, "g_mode") or "外部输入"
    internal = g_mode == "引用本工具Gross运行"
    if not internal:
        gross_value = _num(form, "gross", None)
        if gross_value is None:
            bad("gross", "外部Gross CONE数值", _raw(form, "gross", "（留空）"),
                "外部输入模式必须填写Gross数值（缺失＝未测算）。", "≥0", "请填写或切换引用模式。")
        elif gross_value < 0:
            bad("gross", "外部Gross CONE数值", gross_value,
                "外部Gross CONE不得小于0。", "≥0", "请核对数值。")
        if not _text(form, "gross_source"):
            bad("gross_source", "Gross来源说明", "（未填）",
                "外部输入模式必须填写Gross来源与方法说明。", "非空文本", "请填写来源。")
        warnings.append(_issue(_LEVEL_WARNING, "warn_external_gross", "gross",
                               "参数", "Gross来源", "外部输入",
                               "使用外部Gross CONE：请确保其与本测算同机组、同年份、同计价口径。",
                               "同口径", "核对后可继续。"))
    if form.get("gross_conf") not in ("1", True, "true", 1):
        bad("gross_conf", "同口径确认", "未确认",
            "必须勾选“已核对同口径”。", "已确认", "核对机组/地区/年度/价格年/税务/容量分母与年化方式后勾选。")
    if internal:
        run_ref = _text(form, "g_run_ref")
        if not run_ref:
            bad("g_run_ref", "被引用Gross运行", "（未选）",
                "引用模式必须选择一个已有Gross结果。", "本工作区完成的Gross运行",
                "请从下拉选择。")
        else:
            _check_net_reference(form, project_dir, run_ref, bad)
        if form.get("g_rep") not in ("1", True, "true", 1):
            bad("g_rep", "等额序列年度重复假设", "未确认",
                "等额Gross序列须确认“每年重复该年度收支”的情景假设（不会默认成立）。",
                "已确认", "确认情景假设后勾选，或改用首年口径。")
    return blockers, warnings


def _check_net_reference(form: MappingLike, project_dir: Path,
                         run_ref: str, bad: Callable) -> None:
    """The AGGREGATED same-caliber gate (A07): EVERY mismatched item is
    listed with both sides - the engine's fail-fast refusal stays the
    submission authority; this is its review view."""
    from capacity_credit.application import cost_service
    from capacity_credit.application.cost_payload import build_net_cone_payload

    payload = build_net_cone_payload(form)
    try:
        record = cost_service._read_cost_run_workspace(project_dir, run_ref)
    except cost_service.CostServiceError as exc:
        bad("g_run_ref", "被引用Gross运行", run_ref,
            f"被引用运行不可用：{exc.message}", "本工作区完成的Gross CONE运行",
            "请重新选择可引用的Gross结果。", rule="caliber")
        return
    manifest = record.get("manifest") or {}
    if manifest.get("cost_kind") != "cost_gross_cone" or manifest.get(
        "status"
    ) != "completed" or not isinstance(record.get("result"), dict):
        bad("g_run_ref", "被引用Gross运行", run_ref,
            "被引用运行不是已完成的Gross CONE运行（或未求得结果），不能引用。",
            "已完成且求得Gross CONE的运行", "请重新选择。", rule="caliber")
        return
    gross_input = record.get("input") or {}
    filled = dict(payload)
    filled["gross_reference_unit"] = str(gross_input.get("object_name", ""))
    filled["gross_region"] = str(gross_input.get("region", ""))
    filled["gross_assessment_year"] = gross_input.get("target_year")
    filled["gross_price_year"] = gross_input.get("price_basis_year")
    filled["gross_tax_basis"] = "不含增值税、所得税前"
    filled["gross_capacity_denominator_kind"] = str(
        gross_input.get("capacity_denominator_kind", "")
    )
    for gross_field, base_field, label in _CALIBER_PAIRS:
        gross_side = filled.get(gross_field)
        net_side = filled.get(base_field)
        if gross_side != net_side:
            bad(gross_field, f"{label}（Net侧 vs 被引用Gross侧）",
                f"{label} Net侧={net_side!r}",
                f"口径不一致：{label} Net侧={net_side!r}，"
                f"被引用Gross运行={gross_side!r}。",
                f"与被引用Gross运行一致（{gross_side!r}）",
                "请修改Net侧口径，或选择同口径的Gross结果。",
                rule="caliber")
    gross_capacity = gross_input.get("capacity_kw")
    net_capacity = payload.get("capacity_kw")
    if (isinstance(gross_capacity, (int, float))
            and isinstance(net_capacity, (int, float))
            and float(gross_capacity) != float(net_capacity)):
        bad("gross_capacity_kw", "容量分母数值（Net侧 vs 被引用Gross侧）",
            f"Net侧={net_capacity!r}kW",
            f"容量分母不一致：Net侧={net_capacity!r}kW，"
            f"被引用Gross运行={gross_capacity!r}kW（同口径含数值分母本身）。",
            f"{gross_capacity!r} kW",
            "请使两侧容量分母数值一致，或更换引用。", rule="caliber")


_CHECKERS: dict[str, Callable[[MappingLike, Path], tuple[list, list]]] = {
    "cost_fixed": _check_fixed,
    "cost_gross_cone": _check_gross,
    "cost_net_cone": _check_net,
}


def graded_cost_check(
    kind: str,
    form: dict[str, Any],
    project_dir: Path,
    tech_toggles: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """THE pre-flight graded check (zero side effects). Unknown kinds
    refuse with invalid_input like the submit boundary."""
    checker = _CHECKERS.get(kind)
    if checker is None:
        raise WorkbenchError("invalid_input", f"未知成本类型 {kind}")
    blockers, warnings = checker(form, Path(project_dir))
    return {
        "kind": kind,
        "blockers": blockers,
        "warnings": warnings,
        "rules_checked": len(blockers) + len(warnings) + _RULE_BASELINE[kind],
    }


# the minimum number of rules each kind always walks (bounds that pass
# silently are still rules checked - "已检查字段数" is never padded, it
# counts the rule table)
_RULE_BASELINE = {"cost_fixed": 26, "cost_gross_cone": 20, "cost_net_cone": 24}


__all__ = ["graded_cost_check"]
