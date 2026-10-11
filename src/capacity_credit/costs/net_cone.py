"""Annual Net CONE calculation (night-5 N3).

THE annual E&AS identity (v1.2 template EAS与NetCONE sheet):

- 电能量净收益 R_E = 现货收入 − (燃料+变动运维+排放+启动空载) + 合约差价净结算
- 辅助服务净收益 = 备用+调频+其他适用收入 − 未计入能量链的增量成本
- E&AS年度净收益 = R_E + 辅助服务净收益（可为负；不扣折旧/利息/固定运维，
  不含容量电费——容量收入进入抵扣即循环定义）
- 单位抵扣 = E&AS ÷ 容量分母kW（分母>0，显式录入）
- Net原始值 = Gross − 单位抵扣（负值保留，不截零；缺失=未测算，不是0）

Gross sources: external input FIRST (value + source + confirmation +
same-caliber gate); the internal tool Gross reference joins only after
its own package delivers it. The confirmation checkbox is never a
bypass: a mismatched caliber field refuses the Net computation.
"""

from __future__ import annotations

from typing import Any

from capacity_credit.costs.fixed_cost import CostInputError

_TAX_BASIS = "不含增值税、所得税前"
_ANNUALIZATION = "首年年度值"

_SIGNED_FIELDS = (
    "spot_revenue",
    "contract_settlement",
    "reserve_revenue",
    "frequency_revenue",
    "other_ancillary_revenue",
)
_NONNEGATIVE_FIELDS = (
    "fuel_cost",
    "vom_cost",
    "emission_cost",
    "start_other_cost",
    "ancillary_incremental_cost",
)
_ENERGY_FIELDS = (
    "spot_revenue",
    "fuel_cost",
    "vom_cost",
    "emission_cost",
    "start_other_cost",
    "contract_settlement",
)
_ANCILLARY_FIELDS = (
    "reserve_revenue",
    "frequency_revenue",
    "other_ancillary_revenue",
    "ancillary_incremental_cost",
)
_CALIBER_PAIRS = (
    ("gross_reference_unit", "reference_unit", "参考机组"),
    ("gross_region", "region", "地区/节点/分区"),
    ("gross_assessment_year", "assessment_year", "评估年度"),
    ("gross_price_year", "price_year", "价格基准年"),
    ("gross_tax_basis", "tax_basis", "税务口径"),
    ("gross_capacity_denominator_kind", "capacity_denominator_kind", "容量分母口径"),
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CostInputError("invalid_input", message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_payload(payload: dict[str, Any]) -> None:
    for field in (
        "scenario_id",
        "reference_unit",
        "region",
        "source_note",
        "capacity_denominator_kind",
    ):
        value = payload.get(field)
        _require(
            isinstance(value, str) and value.strip(),
            f"缺少必填文本：{field}",
        )
    for field in ("assessment_year", "price_year"):
        value = payload.get(field)
        _require(
            _is_number(value)
            and float(value) == int(value)
            and int(value) >= 1900,
            f"{field} 必须为≥1900的整数年份",
        )
    _require(
        payload.get("tax_basis") == _TAX_BASIS,
        f"税务口径本版仅支持“{_TAX_BASIS}”",
    )
    capacity = payload.get("capacity_kw")
    _require(
        _is_number(capacity) and capacity > 0,
        "容量分母（kW）必须为大于0的数值并显式录入",
    )
    # completeness gate FIRST: any missing E&AS input is a refusal with
    # the field named - a blank is never silently read as 0
    for field in _ENERGY_FIELDS + _ANCILLARY_FIELDS:
        _require(
            field in payload and payload[field] is not None,
            f"缺少E&AS输入：{field}（缺失=未测算，不允许按0补齐；"
            "确无该项请显式填0）",
        )
    for field in _SIGNED_FIELDS:
        _require(
            _is_number(payload[field]),
            f"{field} 必须为数值（可为负）",
        )
    for field in _NONNEGATIVE_FIELDS:
        _require(
            _is_number(payload[field]) and payload[field] >= 0,
            f"{field} 必须为非负数值（不得为负成本）",
        )
    gross_value = payload.get("gross_value")
    if gross_value is not None:
        _require(
            _is_number(gross_value) and gross_value >= 0,
            "外部Gross CONE必须为非负数值",
        )


def _gross_side_ready(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether the Gross side can enter the subtraction: a value with
    source + confirmation; the caliber gate runs separately."""
    gross_value = payload.get("gross_value")
    if gross_value is None:
        return False, "缺Gross或口径未确认：未测算"
    if not str(payload.get("gross_source") or "").strip():
        return False, "缺Gross来源说明：未测算"
    if payload.get("gross_confirmed") is not True:
        return False, "Gross同口径确认未勾选：未测算"
    return True, None


def _check_caliber(payload: dict[str, Any]) -> None:
    """The same-caliber gate (run only when the Gross side is present
    with confirmation): every paired caliber field must match exactly.
    The checkbox never bypasses a mismatch."""
    for gross_field, base_field, label in _CALIBER_PAIRS:
        if gross_field not in payload:
            raise CostInputError(
                "invalid_input",
                f"缺少Gross侧口径字段：{gross_field}（{label}）",
            )
        if payload[gross_field] != payload[base_field]:
            raise CostInputError(
                "invalid_input",
                f"口径不一致，拒绝直接相减：{label} "
                f"E&AS侧={payload[base_field]!r}，Gross侧={payload[gross_field]!r}",
            )
    annualization = payload.get("gross_annualization")
    if annualization == _ANNUALIZATION:
        return
    if annualization == "等额年度序列":
        if payload.get("eas_repetition_assumption") is not True:
            raise CostInputError(
                "invalid_input",
                "等额Gross序列只能与显式确认“每年重复该年度收支”情景假设的"
                "年度E&AS抵扣（该假设不会默认成立，请勾选确认或改用首年口径）",
            )
        return
    raise CostInputError(
        "invalid_input",
        f"年化口径不支持：{annualization!r}（支持“{_ANNUALIZATION}”；"
        "等额序列须显式确认年度重复假设）",
    )


def compute_net_cone(payload: dict[str, Any]) -> dict[str, Any]:
    """Compute the annual E&AS, the per-kW offset and (when the Gross
    side is complete AND same-caliber) the raw Net CONE from the given
    form payload (validated copy in, plain-JSON dict out)."""
    _validate_payload(payload)

    energy = (
        float(payload["spot_revenue"])
        - sum(float(payload[f]) for f in _NONNEGATIVE_FIELDS[:4])
        + float(payload["contract_settlement"])
    )
    ancillary = (
        float(payload["reserve_revenue"])
        + float(payload["frequency_revenue"])
        + float(payload["other_ancillary_revenue"])
        - float(payload["ancillary_incremental_cost"])
    )
    eas_annual = energy + ancillary
    capacity_kw = float(payload["capacity_kw"])
    offset = eas_annual / capacity_kw

    gross_ready, gross_blocker = _gross_side_ready(payload)
    net_raw: float | None = None
    interpretation: str | None = None
    gross_view: dict[str, Any] | None = None
    if gross_ready:
        _check_caliber(payload)
        gross_view = {
            "mode": str(payload.get("gross_mode") or "external"),
            "value": float(payload["gross_value"]),
            "source": str(payload["gross_source"]),
            "annualization": str(payload["gross_annualization"]),
        }
        net_raw = float(payload["gross_value"]) - offset
        if net_raw < 0:
            interpretation = "净收益超过Gross；负值保留"
        elif net_raw == 0:
            interpretation = "净收益等于Gross"
        else:
            interpretation = "存在剩余固定收入需求"
    else:
        gross_blocker = gross_blocker or "缺Gross或口径未确认：未测算"
        interpretation = gross_blocker

    comparison: dict[str, Any] | None = None
    if (
        payload.get("comparison_eas_per_kw") is not None
        and payload.get("comparison_confirmed") is True
    ):
        comparison = {
            "comparison_eas_per_kw": float(payload["comparison_eas_per_kw"]),
            "difference": float(payload["comparison_eas_per_kw"]) - offset,
            "label": "合约影响差额（对照−当前）",
        }

    return {
        "result_schema_version": "1.0",
        "calculation_kind": "cost_net_cone",
        "scenario_id": payload["scenario_id"],
        "caliber": {
            "reference_unit": payload["reference_unit"],
            "region": payload["region"],
            "assessment_year": int(payload["assessment_year"]),
            "price_year": int(payload["price_year"]),
            "tax_basis": payload["tax_basis"],
            "capacity_denominator_kind": payload["capacity_denominator_kind"],
            "capacity_kw": capacity_kw,
            "annualization": _ANNUALIZATION,
        },
        "energy_net_revenue": energy,
        "ancillary_net_revenue": ancillary,
        "eas_annual": eas_annual,
        "eas_offset_per_kw": offset,
        "gross": gross_view,
        "net_cone_raw": net_raw,
        "net_interpretation": interpretation,
        "contract_comparison": comparison,
        "assumptions": {
            "entry_mode": "年度汇总录入（非调度）",
            "capacity_revenue_excluded": True,
            "fixed_cost_excluded": True,
            "negative_preserved": True,
            "missing_is_unmeasured": True,
            "gross_mode": (
                "本工具Gross结果引用（等额年度序列）"
                if payload.get("gross_mode") == "internal_reference"
                else "外部输入（先交付）"
            ),
            "comparison_label": "合约影响差额（对照−当前）；不称政策压制损失",
        },
    }


__all__ = ["CostInputError", "compute_net_cone"]
