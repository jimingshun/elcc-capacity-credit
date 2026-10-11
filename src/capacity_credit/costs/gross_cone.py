"""Full Gross CONE - annual project free-cash-flow route (night-5 N4).

THE single production Gross model (method contract §3): find the
constant annual fixed revenue R (元/kW·年) that drives the project
free-cash-flow NPV to zero at the user-declared discount rate.

Timeline (year-end convention):
- construction year t = 1..Y: outflow -I_t
- end of construction year Y: working capital outflow -W (投产年初)
- operating year k = 1..N (discount time Y+k):
    + R × capacity_kW  − fixed O&M  − income tax  − renewal_k
  with tax = tax_rate × max(revenue − O&M − straight-line tax
  depreciation, 0): NO refund, NO carryforward (the disclosed
  simplification - unimplemented tax-shield mechanics are never
  claimed).
- end of operating year N: + residual value + working-capital recovery

Financing is ABSENT by construction: this is the all-investment
(free cash flow) view; no principal or interest line exists anywhere,
and a construction-period interest input would double-count financing
(the discount rate carries the capital charge). NPV is monotone
non-decreasing in R (piecewise linear): bisection with explicit
bracket expansion; an unbracketable case is a stable refusal, never a
nominal rate. Solve diagnostics (status, residual, iterations) are
part of the result; the solve state is financial precision, NOT the
reliability statistics convergence identity.
"""

from __future__ import annotations

from typing import Any

from capacity_credit.costs.fixed_cost import CostInputError

_MAX_ITERATIONS = 200
_NPV_TOLERANCE = 1.0  # 元: the acceptance bar for the achieved residual
_RATE_TOLERANCE = 0.01  # 元/kW·年: the contract bar for the answer
_BRACKET_GROWTH = 2.0
_BRACKET_MAX = 1.0e6  # 元/kW·年: far beyond any plausible CONE
_SUPPORTED_DENOMINATOR = "装机容量"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CostInputError("invalid_input", message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_payload(payload: dict[str, Any]) -> None:
    _require(
        isinstance(payload.get("object_name"), str)
        and payload["object_name"].strip(),
        "缺少必填文本：object_name（参考机组/对象名称）",
    )
    _require(
        isinstance(payload.get("region"), str) and payload["region"].strip(),
        "缺少必填文本：region",
    )
    for field in ("target_year", "price_basis_year"):
        value = payload.get(field)
        _require(
            _is_number(value) and float(value) == int(value) and int(value) >= 1900,
            f"{field} 必须为≥1900的整数年份",
        )
    investments = payload.get("investments")
    _require(
        isinstance(investments, list)
        and 1 <= len(investments) <= 10
        and all(_is_number(v) and v >= 0 for v in investments)
        and sum(float(v) for v in investments) > 0,
        "investments 必须为1—10个非负金额（元）且合计大于0",
    )
    n = payload.get("operating_years")
    _require(
        _is_number(n) and float(n) == int(n) and 1 <= int(n) <= 60,
        "operating_years 必须为1—60整数",
    )
    d = payload.get("discount_rate")
    _require(_is_number(d) and d >= 0, "discount_rate 必须为≥0的小数")
    _require(
        d <= 10,
        "discount_rate 必须≤10（1000%）：更大数值超出本模型适用范围，拒绝计算",
    )
    _require(
        payload.get("discount_tax_basis") == "税后",
        "折现率税务口径必须声明为“税后”：本模型为税后自由现金流，"
        "税前折现率与现金流口径不匹配，拒绝计算",
    )
    _require(
        payload.get("price_basis") == payload.get("model_price_basis"),
        "价格口径不匹配：折现率的价格基准（名义/不变价）必须与模型的价格基准一致"
        f"（收到 {payload.get('price_basis')!r} vs {payload.get('model_price_basis')!r}）",
    )
    t = payload.get("tax_rate")
    _require(
        _is_number(t) and 0 <= t < 1,
        "tax_rate 必须在[0,1)范围内（统一税率假设）",
    )
    for field in ("tax_depreciation_years",):
        value = payload.get(field)
        _require(
            _is_number(value) and float(value) == int(value) and 1 <= int(value) <= 60,
            f"{field} 必须为1—60整数",
        )
    formation = payload.get("fixed_asset_formation_rate")
    _require(
        _is_number(formation) and 0 <= formation <= 1,
        "fixed_asset_formation_rate 必须在0—1范围内",
    )
    for field in ("fixed_om_per_kw", "residual_value", "working_capital"):
        value = payload.get(field)
        _require(
            _is_number(value) and value >= 0,
            f"{field} 必须为非负数值（不适用请显式填0）",
        )
    renewals = payload.get("renewal_investments", [])
    _require(isinstance(renewals, list), "renewal_investments 必须为列表")
    for item in renewals:
        year = item.get("year") if isinstance(item, dict) else None
        _require(
            isinstance(item, dict)
            and _is_number(year)
            and float(year) == int(year)
            and 1 <= int(year) <= int(payload["operating_years"])
            and _is_number(item.get("amount"))
            and item["amount"] >= 0,
            "renewal_investments 每项必须为 {year: 经营年序号(整数), amount: 非负金额}",
        )
    cap = payload.get("capacity_kw")
    _require(_is_number(cap) and cap > 0, "capacity_kw 必须大于0")
    _require(
        payload.get("capacity_denominator_kind") == _SUPPORTED_DENOMINATOR,
        f"容量分母口径本版仅支持“{_SUPPORTED_DENOMINATOR}”："
        f"收到 {payload.get('capacity_denominator_kind')!r}（认证容量/净出力转换未实现，拒绝）",
    )


def _npv_at(payload: dict[str, Any], rate_per_kw: float) -> float:
    """THE production cash-flow valuation (also used by the solver)."""
    d = float(payload["discount_rate"])
    n = int(payload["operating_years"])
    cap = float(payload["capacity_kw"])
    t = float(payload["tax_rate"])
    inv_total = sum(float(v) for v in payload["investments"])
    dep = (
        inv_total
        * float(payload["fixed_asset_formation_rate"])
        / int(payload["tax_depreciation_years"])
    )
    om_total = float(payload["fixed_om_per_kw"]) * cap
    dep_years = int(payload["tax_depreciation_years"])
    construction_span = len(payload["investments"])
    npv = -sum(
        float(amount) / (1 + d) ** (index + 1)
        for index, amount in enumerate(payload["investments"])
    )
    wc = float(payload["working_capital"])
    if wc:
        npv -= wc / (1 + d) ** construction_span
    for k in range(1, n + 1):
        time = construction_span + k
        revenue = rate_per_kw * cap
        # straight-line tax depreciation STOPS after its horizon: the
        # shield exists only while the tax base lasts
        dep_k = dep if k <= dep_years else 0.0
        tax = t * max(revenue - om_total - dep_k, 0.0)
        npv += (revenue - om_total - tax) / (1 + d) ** time
    if float(payload["residual_value"]):
        npv += float(payload["residual_value"]) / (1 + d) ** (n + len(payload["investments"]))
    if wc:
        npv += wc / (1 + d) ** (n + len(payload["investments"]))
    for item in payload.get("renewal_investments", []):
        npv -= float(item["amount"]) / (1 + d) ** (
            len(payload["investments"]) + int(item["year"])
        )
    return npv


def _solve(payload: dict[str, Any]) -> dict[str, Any]:
    """Bracket + bisection on the monotone NPV(R). Returns the solve
    block: status solved/no_solution, the rate, achieved NPV residual,
    iterations and the final bracket."""
    high = max(
        1.0,
        sum(float(v) for v in payload["investments"])
        / float(payload["capacity_kw"])
        / int(payload["operating_years"]),
    )
    low = 0.0
    iterations = 0
    npv_high = _npv_at(payload, high)
    if _npv_at(payload, 0.0) > _NPV_TOLERANCE:
        return {
            "status": "no_solution",
            "reason": (
                "在收入为0时NPV已为正（残值/对照情形使投资不需收入即可回收）："
                "有效Gross CONE不存在正值解，本模型不输出名义值"
            ),
            "gross_cone_per_kw": None,
            "npv_residual": None,
            "iterations": 0,
            "bracket": [0.0, high],
        }
    while npv_high < -_NPV_TOLERANCE and high < _BRACKET_MAX:
        high *= _BRACKET_GROWTH
        npv_high = _npv_at(payload, high)
    if npv_high < -_NPV_TOLERANCE:
        return {
            "status": "no_solution",
            "reason": (
                "在给定输入下投资无法回收（扩界至"
                f"{high:,.0f} 元/kW·年 NPV仍为负）：请核对折现率、"
                "经营期与投资额；本模型不输出名义值"
            ),
            "gross_cone_per_kw": None,
            "npv_residual": None,
            "iterations": iterations,
            "bracket": [low, high],
        }
    while iterations < _MAX_ITERATIONS:
        mid = 0.5 * (low + high)
        npv_mid = _npv_at(payload, mid)
        iterations += 1
        # primary stop: the ACHIEVED NPV residual meets the bar; the
        # bracket-width guard only guards against float underflow
        if abs(npv_mid) <= _NPV_TOLERANCE or mid == low or mid == high:
            return {
                "status": "solved",
                "reason": None,
                "gross_cone_per_kw": mid,
                "npv_residual": abs(npv_mid),
                "iterations": iterations,
                "bracket": [low, high],
            }
        if npv_mid < 0:
            low = mid
        else:
            high = mid
    mid = 0.5 * (low + high)
    npv_mid = _npv_at(payload, mid)
    return {
        "status": "solved" if abs(npv_mid) <= _NPV_TOLERANCE else "no_solution",
        "reason": None if abs(npv_mid) <= _NPV_TOLERANCE else "迭代上限内未收敛到容差",
        "gross_cone_per_kw": mid if abs(npv_mid) <= _NPV_TOLERANCE else None,
        "npv_residual": abs(npv_mid),
        "iterations": iterations,
        "bracket": [low, high],
    }


def compute_gross_cone(payload: dict[str, Any]) -> dict[str, Any]:
    """Compute the full Gross CONE: validate, build the yearly cash-flow
    projection at the solved rate and return the result with the solve
    diagnostics and the honest assumption disclosures."""
    _validate_payload(payload)
    solve = _solve(payload)
    rate = solve["gross_cone_per_kw"]
    d = float(payload["discount_rate"])
    n = int(payload["operating_years"])
    cap = float(payload["capacity_kw"])
    t = float(payload["tax_rate"])
    inv_total = sum(float(v) for v in payload["investments"])
    dep = (
        inv_total
        * float(payload["fixed_asset_formation_rate"])
        / int(payload["tax_depreciation_years"])
    )
    om_total = float(payload["fixed_om_per_kw"]) * cap
    construction_rows = [
        {
            "year": index + 1,
            "investment": float(amount),
            "net_flow": -float(amount),
        }
        for index, amount in enumerate(payload["investments"])
    ]
    operating_rows: list[dict[str, Any]] = []
    dep_years = int(payload["tax_depreciation_years"])
    if rate is not None:
        for k in range(1, n + 1):
            revenue = rate * cap
            dep_k = dep if k <= dep_years else 0.0
            tax = t * max(revenue - om_total - dep_k, 0.0)
            operating_rows.append(
                {
                    "year": k,
                    "revenue": revenue,
                    "fixed_om": om_total,
                    "tax": tax,
                    "net_flow": revenue - om_total - tax,
                }
            )
    return {
        "result_schema_version": "1.0",
        "calculation_kind": "cost_gross_cone",
        "object_name": payload["object_name"],
        "region": payload["region"],
        "target_year": int(payload["target_year"]),
        "price_basis_year": int(payload["price_basis_year"]),
        "capacity_kw": cap,
        "capacity_denominator_kind": payload["capacity_denominator_kind"],
        "gross_cone_per_kw": rate,
        "solve": {
            **solve,
            "npv_tolerance": _NPV_TOLERANCE,
            "rate_tolerance": _RATE_TOLERANCE,
            "max_iterations": _MAX_ITERATIONS,
            "route": "项目自由现金流（税后）＋税后折现率；NPV=0求固定收入参数",
        },
        "inputs_summary": {
            "investments": [float(v) for v in payload["investments"]],
            "operating_years": n,
            "discount_rate": d,
            "discount_tax_basis": payload["discount_tax_basis"],
            "price_basis": payload["price_basis"],
            "tax_rate": t,
            "tax_depreciation_annual": dep,
            "fixed_om_per_kw": float(payload["fixed_om_per_kw"]),
            "residual_value": float(payload["residual_value"]),
            "working_capital": float(payload["working_capital"]),
            "renewal_investments": [
                {"year": int(i["year"]), "amount": float(i["amount"])}
                for i in payload.get("renewal_investments", [])
            ],
        },
        "cashflow": {
            "construction_years": construction_rows,
            "operating_years": operating_rows,
        },
        "assumptions": {
            "financing": (
                "全投资自由现金流视角：不含任何还本付息行；折现率承担资本回报，"
                "建设期利息不作为独立融资支出再次进入"
            ),
            "tax": (
                "统一所得税率×当年应纳税所得（收入−运维−直线折旧税基）；"
                "负应纳税所得不退税、不结转；未实现税盾/亏损结转/增值税机制，如实未包含"
            ),
            "renewal_tax": "更新投资的税务处理未包含（其现金流按投资时点计入）",
            "timing": "现金流按年末记账；营运资金于投产年初流出、期末等额回收",
            "revenue_shape": "等额年度固定收入序列（水平化），不是逐年增长轨迹",
            "denominator": "本版仅装机容量分母；认证容量/净出力转换未实现",
            "solve_identity": (
                "财务求根状态独立于可靠性统计收敛标识；solve.status是财务精度"
            ),
        },
    }


__all__ = ["compute_gross_cone"]
