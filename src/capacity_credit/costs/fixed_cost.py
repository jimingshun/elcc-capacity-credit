"""Fixed-cost accounting calculation (night-5 N2).

THE accounting-cost formula chain, mirroring the reviewed v1.2 Excel
template sheet by sheet (造价计算 / 年化计算 / 年度明细):

- 修正单位静态造价 = 机型基准造价 × K_机型 × K_省 × K_分区 × K_技术 × 造价指数
  (K_机型 ≡ 1: the machine difference lives in the base cost; applying
  both would double-count).
- 静态总投资(亿元) = 修正单位静态造价 × 装机MW × 1000 / 1e8.
- 计息系数_t = Σ_{j<t} w_j + 0.5·w_t; IDC = 静态投资 × (1−资本金比例)
  × 贷款利率 × Σ 计息系数 (simple interest on the static loan balance,
  the template's disclosed simplification - no compounding).
- 逐年费用: depreciation (straight line to the salvage), interest
  (equal-principal declining balance), fixed O&M, insurance on the
  fixed-asset value, amortization; the first-year value and the
  arithmetic period average over the (truncated) operating years.

This is the ACCOUNTING cost view: no shareholder return, no full time
value - it is never a Gross CONE. All template anchors are recomputed
from the chain; no anchor constant is hardcoded.
"""

from __future__ import annotations

from typing import Any

from capacity_credit.costs.catalog import (
    TECH_TOGGLES,
    catalog_disclaimer,
    machine_catalog,
    province_coefficients,
    zone_coefficients,
)

_RESULT_SCHEMA_VERSION = "1.0"
_INT_RANGES = {
    "loan_years": (1, 60),
    "construction_years": (1, 10),
    "depreciation_years": (1, 60),
    "operating_years": (1, 60),
    "amortization_years": (1, 60),
}
_RATIO_FIELDS = (
    "equity_ratio",
    "salvage_rate",
    "fixed_asset_formation_rate",
    "insurance_rate",
    "amortization_ratio",
    "vat_rate",
)


class CostInputError(ValueError):
    """A specific cost-input refusal (display kind + reason).

    kind is a display category ('invalid_input'); the message names the
    offending field/value so the user can fix the form - unknown
    machine/province/zone keys never fall back to a default."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CostInputError("invalid_input", message)


def _validate_payload(payload: dict[str, Any]) -> None:
    for field in (
        "machine_id",
        "province",
        "zone",
        "price_index",
        "equity_ratio",
        "loan_rate",
        "loan_years",
        "construction_years",
        "investment_schedule",
        "depreciation_years",
        "salvage_rate",
        "operating_years",
        "fixed_asset_formation_rate",
        "fixed_om",
        "insurance_rate",
        "amortization_ratio",
        "amortization_years",
        "vat_rate",
    ):
        _require(field in payload, f"缺少必填输入：{field}")
    machines = machine_catalog()
    machine_id = payload["machine_id"]
    custom = payload.get("custom_machine")
    if machine_id not in machines:
        # P-C: a custom machine is accepted ONLY with explicit user
        # facts (positive base cost per kW, positive integer capacity
        # in MW); anything else keeps the catalog-only refusal
        _require(
            isinstance(custom, dict)
            and isinstance(custom.get("base_cost_per_kw"), (int, float))
            and float(custom["base_cost_per_kw"]) > 0
            and isinstance(custom.get("capacity_mw"), int)
            and not isinstance(custom.get("capacity_mw"), bool)
            and int(custom["capacity_mw"]) > 0,
            "自定义机型必须提供大于0的基准造价（元/kW）与正整数建档容量（MW）"
            "（未知目录机型且无有效自定义参数）",
        )
    provinces = province_coefficients()
    _require(
        payload["province"] in provinces,
        f"未知省份：{payload['province']}",
    )
    zones = zone_coefficients()
    zone_key = f"{payload['province']}|{payload['zone']}"
    _require(
        zone_key in zones,
        f"未知分区：{zone_key}（未配置映射，不回退通用值）",
    )
    price_index = payload["price_index"]
    _require(
        isinstance(price_index, (int, float)) and price_index > 0,
        "造价指数换算系数必须大于0",
    )
    for field, (low, high) in _INT_RANGES.items():
        value = payload[field]
        _require(
            isinstance(value, int)
            and not isinstance(value, bool)
            and low <= value <= high,
            f"{field} 必须为{low}—{high}整数",
        )
    schedule = payload["investment_schedule"]
    _require(
        isinstance(schedule, list)
        and len(schedule) == payload["construction_years"]
        and all(
            isinstance(w, (int, float))
            and not isinstance(w, bool)
            and w >= 0
            for w in schedule
        ),
        "逐年投资比例必须为非负数且个数等于建设期年数",
    )
    _require(
        abs(sum(float(w) for w in schedule) - 1.0) < 1e-9,
        "逐年投资比例之和必须为1（当前和不为1）",
    )
    for field in _RATIO_FIELDS:
        value = payload[field]
        _require(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= 1,
            f"{field} 必须在0—1范围内",
        )
    _require(
        payload["loan_rate"] >= 0,
        "贷款利率必须非负（0为合法输入）",
    )
    _require(
        payload["fixed_om"] >= 0,
        "固定运维费必须非负",
    )
    _require(
        payload["fixed_asset_formation_rate"] + payload["amortization_ratio"]
        <= 1.0,
        "固定资产形成率与摊销比例之和不得超过1（同一投资不得重复分配）",
    )
    toggles = payload.get("tech_toggles") or {}
    _require(
        isinstance(toggles, dict)
        and set(toggles) <= set(TECH_TOGGLES),
        f"未知技术修正项（可用：{'/'.join(TECH_TOGGLES)}）",
    )


def _accrual_factors(schedule: list[float], construction_years: int) -> list[float]:
    """计息系数 per construction year: Σ_{j<t} w_j + 0.5·w_t (years
    beyond the construction period contribute 0)."""
    factors: list[float] = []
    for index in range(len(schedule)):
        year = index + 1
        if year > construction_years:
            factors.append(0.0)
            continue
        prior = sum(float(w) for w in schedule[:index])
        factors.append(prior + 0.5 * float(schedule[index]))
    return factors


def compute_fixed_cost(payload: dict[str, Any]) -> dict[str, Any]:
    """Compute the accounting fixed-cost chain from the given form
    payload (validated copy in, plain-JSON dict out)."""
    _validate_payload(payload)
    machines = machine_catalog()
    custom = payload.get("custom_machine")
    if payload["machine_id"] in machines:
        machine = machines[payload["machine_id"]]
    else:
        # P-C custom machine: the validated user facts become the
        # machine record (k_machine = 1 by definition - the difference
        # lives in the base cost itself, same as the catalog)
        machine = {
            "k_machine": 1.0,
            "capacity_mw": int(custom["capacity_mw"]),
            "base_cost_per_kw": float(custom["base_cost_per_kw"]),
            "basis": "自定义机型（用户录入参数，待核实）",
        }
    provinces = province_coefficients()
    zones = zone_coefficients()
    toggles = payload.get("tech_toggles") or {}

    k_machine = float(machine["k_machine"])
    k_province = float(provinces[payload["province"]])
    k_zone = float(zones[f"{payload['province']}|{payload['zone']}"])
    k_technology = 1.0 + sum(
        TECH_TOGGLES[name] for name, enabled in toggles.items() if enabled
    )
    price_index = float(payload["price_index"])
    installed_mw = int(machine["capacity_mw"])
    capacity_kw = installed_mw * 1000

    unit_static_cost = (
        float(machine["base_cost_per_kw"])
        * k_machine
        * k_province
        * k_zone
        * k_technology
        * price_index
    )
    static_investment_yi = unit_static_cost * capacity_kw / 1e8
    equity_ratio = float(payload["equity_ratio"])
    loan_rate = float(payload["loan_rate"])
    factors = _accrual_factors(
        list(payload["investment_schedule"]),
        int(payload["construction_years"]),
    )
    idc_yi = (
        static_investment_yi
        * (1.0 - equity_ratio)
        * loan_rate
        * sum(factors)
    )
    dynamic_investment_yi = static_investment_yi + idc_yi
    unit_dynamic_cost = dynamic_investment_yi * 1e8 / capacity_kw

    formation = float(payload["fixed_asset_formation_rate"])
    salvage = float(payload["salvage_rate"])
    fa = unit_dynamic_cost * formation
    loan_per_kw = unit_dynamic_cost * (1.0 - equity_ratio)
    depreciation_years = int(payload["depreciation_years"])
    loan_years = int(payload["loan_years"])
    amortization_years = int(payload["amortization_years"])
    operating_years = int(payload["operating_years"])
    fixed_om = float(payload["fixed_om"])
    insurance_rate = float(payload["insurance_rate"])
    amortization_ratio = float(payload["amortization_ratio"])
    vat_rate = float(payload["vat_rate"])
    depreciation_annual = fa * (1.0 - salvage) / depreciation_years
    amortization_annual = unit_dynamic_cost * amortization_ratio / amortization_years

    yearly: list[dict[str, Any]] = []
    for year in range(1, operating_years + 1):
        depreciation = depreciation_annual if year <= depreciation_years else 0.0
        interest = (
            loan_per_kw * loan_rate * (1.0 - (year - 1) / loan_years)
            if year <= loan_years
            else 0.0
        )
        insurance = fa * insurance_rate
        amortization = amortization_annual if year <= amortization_years else 0.0
        total = depreciation + interest + fixed_om + insurance + amortization
        yearly.append(
            {
                "year": year,
                "depreciation": depreciation,
                "interest": interest,
                "fixed_om": fixed_om,
                "insurance": insurance,
                "amortization": amortization,
                "total": total,
            }
        )

    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "depreciation": row["depreciation"],
            "interest": row["interest"],
            "fixed_om": row["fixed_om"],
            "insurance": row["insurance"],
            "amortization": row["amortization"],
            "total": row["total"],
            "total_with_vat": row["total"] * (1.0 + vat_rate),
        }

    first_year = _summary(yearly[0])
    average_row = {
        key: sum(row[key] for row in yearly) / operating_years
        for key in ("depreciation", "interest", "fixed_om", "insurance", "amortization", "total")
    }
    period_average = _summary(average_row)

    uncovered: list[str] = []
    if operating_years < depreciation_years:
        uncovered.append("折旧年限")
    if operating_years < loan_years:
        uncovered.append("还贷年限")
    if operating_years < amortization_years:
        uncovered.append("摊销年限")
    uncovered_note = (
        "经营期短于" + "、".join(uncovered) + "：仅统计期间费用，"
        "未覆盖期末账面资产/剩余债务/未摊销余额；本结果是期间平均成本，"
        "不是全寿命投资回收值。"
        if uncovered
        else None
    )
    leftover = 1.0 - formation - amortization_ratio

    return {
        "result_schema_version": _RESULT_SCHEMA_VERSION,
        "calculation_kind": "fixed_cost",
        "machine_id": payload["machine_id"],
        "province": payload["province"],
        "zone": payload["zone"],
        "installed_mw": installed_mw,
        "capacity_kw": capacity_kw,
        "coefficients": {
            "k_machine": k_machine,
            "k_province": k_province,
            "k_zone": k_zone,
            "k_technology": k_technology,
            "price_index": price_index,
        },
        "unit_static_cost": unit_static_cost,
        "static_investment_yi": static_investment_yi,
        "idc_yi": idc_yi,
        "dynamic_investment_yi": dynamic_investment_yi,
        "unit_dynamic_cost": unit_dynamic_cost,
        "fixed_asset_value_per_kw": fa,
        "loan_per_kw": loan_per_kw,
        "first_year": first_year,
        "period_average": period_average,
        "yearly": yearly,
        "uncovered_note": uncovered_note,
        "leftover_investment_ratio": leftover,
        "assumptions": {
            "shareholder_return": False,
            "catalog_disclaimer": catalog_disclaimer(),
            "idc_simplification": (
                "建设期利息按静态借款余额单利计息（模板既定简化，不计复利）"
            ),
            "vat_note": "含税值仅为简化报价等效换算，不代表实际增值税成本",
            "accounting_scope": (
                "会计费用加总口径：不含股东资本回报、不体现全部时间价值，"
                "不是Gross CONE"
            ),
        },
    }


__all__ = ["CostInputError", "compute_fixed_cost"]
