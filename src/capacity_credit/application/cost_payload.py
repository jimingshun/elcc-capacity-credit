"""The cost payload assembly boundary (slice ① web serve).

THE single place that turns PERCENT-valued form fields into contract
payloads (night-7 D2 contract, extracted verbatim from the Streamlit
form layer so the web serve layer and the Streamlit UI share ONE
conversion boundary):

- a percent field is divided by 100 EXACTLY ONCE, here
  (``percent_to_ratio``); ``price_index`` stays a MULTIPLE;
- a blank numeric entry becomes ``None`` (missing) and is refused by
  the completeness gate - it is NEVER silently 0
  (``missing_is_unmeasured``);
- no function here imports Streamlit or reads session state: the
  caller collects the plain ``name -> value`` form mapping.

The Streamlit form layer (ui/cost_forms.py) delegates to these
builders after collecting its namespaced draft state into the same
plain mapping, so the web and Streamlit front doors cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from capacity_credit.application.workbench_service import WorkbenchError


def percent_to_ratio(value: float | None) -> float | None:
    """The single UI->contract boundary for percent inputs (D2): the
    field shows 4.3 for 4.3% and the payload receives 0.043 - divided
    EXACTLY ONCE, here. None stays None (missing is never silently 0)."""
    if value is None:
        return None
    return float(value) / 100.0


def num_field(value: Any, field: str) -> Any:
    """One optional numeric entry: blank/None → None (the completeness
    gate decides), a number passes through, anything else must be a
    real number string or the refusal carries the field name."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise WorkbenchError("invalid_input", f"{field} 必须为数值")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        raise WorkbenchError(
            "invalid_input", f"{field} 必须为数值：收到 {text!r}"
        ) from None


def _text(form: Mapping[str, Any], name: str, default: str = "") -> str:
    value = form.get(name)
    return default if value is None else str(value)


def _int(form: Mapping[str, Any], name: str, default: Any) -> int:
    value = form.get(name)
    if value is None:
        return int(default)
    if isinstance(value, str) and value.strip() == "":
        return int(default)
    return int(float(value))


def _float(form: Mapping[str, Any], name: str, default: Any) -> float:
    value = form.get(name)
    if value is None:
        return float(default)
    if isinstance(value, str) and value.strip() == "":
        return float(default)
    return float(value)


def _numval(
    form: Mapping[str, Any], name: str, field: str, default: Any
) -> Any:
    """A DEFAULTED numeric entry: missing/None/blank → the form default
    (Streamlit number widgets are never blank; the web form prefills
    the same defaults), a real number passes, a non-numeric string is
    refused with the field name."""
    value = form.get(name)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    return num_field(value, field)


def build_fixed_cost_payload(
    form: Mapping[str, Any], tech_toggles: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Fixed-cost payload: percents convert EXACTLY ONCE at this
    boundary; ``price_index`` stays a multiple (never a percent).
    Missing/blank entries fall back to the SAME form defaults the
    Streamlit widgets prefill (the defaults live here, once)."""
    y_count = _int(form, "cyears", 2)
    schedule_percent = [
        _numval(form, f"w{index}", f"第{index + 1}年投资比例", 0.0)
        for index in range(y_count)
    ]
    schedule: list[Any] = [
        None if p is None else p / 100.0 for p in schedule_percent
    ]
    # P-C custom machine: the select carries "__custom__" and the free
    # fields carry the user facts; a catalog machine ignores them
    custom_selected = _text(form, "machine") == "__custom__"
    if custom_selected:
        cap_mw = _int(form, "cm_cap", 0)
        machine_id = _text(form, "cm_name") or "自定义机型"
        custom_machine: dict[str, Any] | None = {
            "base_cost_per_kw": _numval(
                form, "cm_cost", "自定义机型基准造价", 0.0
            ),
            "capacity_mw": int(cap_mw),
            "machine_label": machine_id,
        }
    else:
        machine_id = _text(form, "machine")
        custom_machine = None
    return {
        "machine_id": machine_id,
        "custom_machine": custom_machine,
        "province": _text(form, "province"),
        "zone": (_text(form, "zone").strip() or "全省"),
        "tech_toggles": {
            str(name): bool(enabled)
            for name, enabled in (tech_toggles or {}).items()
            if enabled
        },
        "price_index": _float(form, "price_index", 1.0),
        "equity_ratio": percent_to_ratio(
            _numval(form, "equity", "资本金比例", 20.0)
        ),
        "loan_rate": percent_to_ratio(
            _numval(form, "loan", "贷款利率", 4.3)
        ),
        "loan_years": _int(form, "loan_years", 15),
        "construction_years": y_count,
        "investment_schedule": schedule,
        "depreciation_years": _int(form, "dep", 15),
        "salvage_rate": percent_to_ratio(
            _numval(form, "salvage", "残值率", 5.0)
        ),
        "operating_years": _int(form, "op", 30),
        "fixed_asset_formation_rate": percent_to_ratio(
            _numval(form, "form", "固定资产形成率", 95.0)
        ),
        "fixed_om": _numval(form, "om", "固定运维费", 71.0),
        "insurance_rate": percent_to_ratio(
            _numval(form, "ins", "保险费率", 0.25)
        ),
        "amortization_ratio": percent_to_ratio(
            _numval(form, "amr", "摊销比例", 5.0)
        ),
        "amortization_years": _int(form, "amy", 8),
        "vat_rate": percent_to_ratio(
            _numval(form, "vat", "增值税率", 13.0)
        ),
    }


def build_gross_cone_payload(form: Mapping[str, Any]) -> dict[str, Any]:
    """Gross CONE payload: 折现率/税率/形成率 are percents converted at
    this boundary; the income tax rate can never reach 1.0 or beyond
    (the field caps at 99.9%). Missing/blank numeric entries fall back
    to the SAME form defaults the Streamlit widgets prefill."""
    y_count = _int(form, "y", 2)
    investments = [
        _numval(form, f"i{index}", f"第{index + 1}年投资", 0.0)
        for index in range(y_count)
    ]
    renewal_count = _int(form, "rc", 0)
    renewals = [
        {
            "year": _int(form, f"ry{index}", 1),
            "amount": _numval(form, f"ra{index}", f"更新投资{index + 1}金额", 0.0),
        }
        for index in range(renewal_count)
    ]
    tax_percent = _numval(form, "tax", "统一所得税率", 25.0)
    return {
        "object_name": _text(form, "obj"),
        "region": _text(form, "region"),
        "target_year": _int(form, "ty", 2030),
        "price_basis_year": _int(form, "pby", 2026),
        "investments": investments,
        "operating_years": _int(form, "n", 30),
        "discount_rate": percent_to_ratio(
            _numval(form, "d", "折现率", 7.0)
        ),
        "discount_tax_basis": "税后",
        "price_basis": "名义",
        "model_price_basis": "名义",
        "tax_rate": percent_to_ratio(tax_percent),
        "tax_depreciation_years": _int(form, "dep", 15),
        "fixed_asset_formation_rate": percent_to_ratio(
            _numval(form, "form", "固定资产形成率", 100.0)
        ),
        "fixed_om_per_kw": _numval(form, "om", "固定运维费", 0.0),
        "residual_value": _numval(form, "res", "残值", 0.0),
        "working_capital": _numval(form, "wc", "营运资金", 0.0),
        "renewal_investments": renewals,
        "capacity_kw": _numval(form, "cap", "装机容量", 0.0),
        "capacity_denominator_kind": "装机容量",
    }


def build_net_cone_payload(form: Mapping[str, Any]) -> dict[str, Any]:
    """Net CONE payload: every revenue/cost entry is a refused missing
    input when blank (never 0); the Gross side switches between the
    internal reference and the external value."""

    def num(name: str, field: str) -> Any:
        return num_field(form.get(name), field)

    gross_mode_raw = _text(form, "g_mode", "外部输入")
    internal = gross_mode_raw == "引用本工具Gross运行"
    repetition = True if form.get("g_rep") else None
    if internal:
        gross_annualization = "等额年度序列"
    else:
        gross_annualization = _text(form, "g_ann", "首年年度值")
    return {
        "scenario_id": _text(form, "scenario"),
        "reference_unit": _text(form, "unit"),
        "region": _text(form, "region"),
        "assessment_year": _int(form, "year", 2026),
        "price_year": _int(form, "price_year", 2026),
        "tax_basis": "不含增值税、所得税前",
        "capacity_denominator_kind": _text(form, "cap_kind", "装机容量"),
        "capacity_kw": float(form.get("capacity") or 0.0),
        "source_note": _text(form, "source"),
        "spot_revenue": num("spot", "现货电能量收入"),
        "fuel_cost": num("fuel", "燃料成本"),
        "vom_cost": num("vom", "变动运维成本"),
        "emission_cost": num("emis", "排放成本"),
        "start_other_cost": num("start", "启动及空载等其他变动成本"),
        "contract_settlement": num("contract", "合约差价净结算"),
        "reserve_revenue": num("reserve", "备用服务收入"),
        "frequency_revenue": num("freq", "调频服务收入"),
        "other_ancillary_revenue": num("other", "其他适用辅助服务收入"),
        "ancillary_incremental_cost": num("anccost", "辅助服务额外增量成本"),
        "gross_value": (
            None if internal else num("gross", "外部Gross CONE")
        ),
        "gross_source": _text(form, "gross_source"),
        "gross_confirmed": bool(form.get("gross_conf")),
        "gross_reference_unit": _text(form, "g_unit"),
        "gross_region": _text(form, "g_region"),
        "gross_assessment_year": _int(form, "g_year", 2026),
        "gross_price_year": _int(form, "g_py", 2026),
        "gross_tax_basis": _text(form, "g_tax", "不含增值税、所得税前"),
        "gross_capacity_denominator_kind": _text(form, "g_cap", "装机容量"),
        "gross_annualization": gross_annualization,
        "gross_mode": "internal_reference" if internal else "external",
        "gross_run_ref": _text(form, "g_run_ref"),
        "eas_repetition_assumption": repetition,
        "comparison_eas_per_kw": num("comp", "对照E&AS"),
        "comparison_confirmed": bool(form.get("comp_conf")),
    }


__all__ = [
    "build_fixed_cost_payload",
    "build_gross_cone_payload",
    "build_net_cone_payload",
    "num_field",
    "percent_to_ratio",
]
