"""T23-A: THE single report-mapping (version 1.1).

Every field shown in the conclusion card, the machine-readable outputs
(machine.py) and the future Excel/HTML/UI layers comes from this one
immutable mapping table - no consumer ever copies a private field list.
Units are derived from the existing RiskMetric / risk_units semantics
and the formal result fields; capacity_credit_fraction is a RATIO in
machine values with an EXPLICIT percent conversion for display only.

Four null semantics are kept distinct (never collapsed to "" or 0):
- zero: the formal result really carries 0 (status preserved);
- not_applicable: null + the mapped status field says not_applicable;
- missing: null with no not_applicable status (not computed / absent);
- not_converged: result_status == not_converged (no midpoint used).

1.1（Q-D1  2026-10-10）：risk_events 白名单与 parquet 模式新增
duration_hours（事件持续小时，正整数）；report_model 兼容读 1.0。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from capacity_credit.domain.risk_units import risk_metric_unit

REPORT_MAPPING_VERSION = "1.1"

# Risk scalar fields whose canonical unit follows the primary risk metric.
_RISK_METRIC_FIELDS = frozenset(
    {
        "base_risk",
        "candidate_risk",
        "matched_risk",
        "risk_difference",
        "search_bracket_low_risk",
        "search_bracket_high_risk",
        "empirical_risk_resolution",
        "lower_ci",
        "upper_ci",
        "anchor_risk_value",
    }
)

# MRI fields whose unit follows the MRI step unit (mri_unit on the result).
_MRI_VALUE_FIELDS = frozenset({"mri_value", "perfect_capacity_mri_value"})

_ALL_METHODS = ("adequacy", "elcc", "efc", "mri", "rmri")
_SEARCH_METHODS = ("elcc", "efc", "mri", "rmri")


@dataclass(frozen=True)
class ReportField:
    """One immutable report field mapping (order in the table is the
    canonical order - never dict-accidental). machine_targets names the
    machine artifacts this field is a COLUMN of - machine.py derives
    its result columns from this table and never keeps a private
    result-field list."""

    field_id: str
    result_path: str
    label: str
    canonical_unit: str | None
    display_unit: str | None
    display_precision: int | None
    null_rule: str
    applies_to: tuple[str, ...]
    status_field: str | None
    is_conclusion_card: bool
    is_machine_result: bool
    allows_percent_conversion: bool
    source_note: str
    machine_targets: tuple[str, ...] = ()


def _f(
    field_id: str,
    result_path: str,
    label: str,
    *,
    canonical_unit: str | None = None,
    display_unit: str | None = None,
    display_precision: int | None = None,
    null_rule: str = "null_ok",
    applies_to: tuple[str, ...] = _ALL_METHODS,
    status_field: str | None = None,
    is_conclusion_card: bool = False,
    is_machine_result: bool = False,
    allows_percent_conversion: bool = False,
    source_note: str = "",
    machine_targets: tuple[str, ...] = (),
) -> ReportField:
    return ReportField(
        field_id=field_id,
        result_path=result_path,
        label=label,
        canonical_unit=canonical_unit,
        display_unit=display_unit,
        display_precision=display_precision,
        null_rule=null_rule,
        applies_to=applies_to,
        status_field=status_field,
        is_conclusion_card=is_conclusion_card,
        is_machine_result=is_machine_result,
        allows_percent_conversion=allows_percent_conversion,
        source_note=source_note,
        machine_targets=machine_targets,
    )


# The canonical, ordered field table (deterministic order; unique ids).
REPORT_FIELDS: tuple[ReportField, ...] = (
    _f("run_id", "run_id", "运行标识", is_conclusion_card=True, is_machine_result=True, machine_targets=("capacity_credit_results", "adequacy_metrics", "run_summary")),
    _f("result_id", "result_id", "结果标识", is_conclusion_card=True, is_machine_result=True, machine_targets=("capacity_credit_results", "adequacy_metrics", "run_summary")),
    _f("method", "method", "方法", is_conclusion_card=True, is_machine_result=True, machine_targets=("capacity_credit_results", "run_summary")),
    _f("method_profile", "method_profile", "方法配置档", is_conclusion_card=True, is_machine_result=True, machine_targets=("run_summary",)),
    _f("result_schema_version", "result_schema_version", "结果Schema版本", is_conclusion_card=True, is_machine_result=True, machine_targets=("run_summary",)),
    _f(
        "resource_or_portfolio_id",
        "resource_or_portfolio_id",
        "资源或组合标识",
        is_machine_result=True,
        machine_targets=("capacity_credit_results",),
    ),
    _f(
        "capacity_value_type",
        "capacity_value_type",
        "容量价值类型",
        null_rule="null_requires_status",
        status_field="capacity_value_type",
        is_machine_result=True,
        machine_targets=("capacity_credit_results",),
    ),
    _f(
        "result_status",
        "result_status",
        "结果状态",
        null_rule="preserve_zero",
        status_field="result_status",
        is_conclusion_card=True,
        is_machine_result=True,
        source_note="formal ResultStatus, never re-derived",
        machine_targets=("capacity_credit_results", "adequacy_metrics", "run_summary"),
    ),
    _f(
        "risk_convergence",
        "risk_convergence",
        "风险收敛状态",
        is_conclusion_card=True,
        is_machine_result=True,
        source_note="formal RiskConvergence",
        machine_targets=("run_summary",),
    ),
    _f(
        "search_convergence",
        "search_convergence",
        "搜索收敛状态",
        is_conclusion_card=True,
        is_machine_result=True,
        machine_targets=("run_summary",),
    ),
    _f(
        "effective_data_grade",
        "effective_data_grade",
        "有效数据等级",
        is_conclusion_card=True,
        source_note="uses effective_data_grade, never the retired data_grade",
        is_machine_result=True,
        machine_targets=("run_summary",),
    ),
    _f("primary_risk_metric", "primary_risk_metric", "主风险指标", is_conclusion_card=True, is_machine_result=True, machine_targets=("run_summary",)),
    _f(
        "base_risk",
        "base_risk",
        "基准风险",
        canonical_unit=None,
        null_rule="preserve_zero",
        is_conclusion_card=True,
        is_machine_result=True,
        source_note="canonical unit = risk_metric_unit(primary_risk_metric)",
        machine_targets=("run_summary",),
    ),
    _f(
        "candidate_risk",
        "candidate_risk",
        "候选风险",
        canonical_unit=None,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
        is_conclusion_card=True,
        is_machine_result=True,
        source_note="canonical unit = risk_metric_unit(primary_risk_metric)",
        machine_targets=("run_summary",),
    ),
    _f(
        "matched_risk",
        "matched_risk",
        "匹配风险",
        canonical_unit=None,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
        is_conclusion_card=True,
        is_machine_result=True,
        source_note="canonical unit = risk_metric_unit(primary_risk_metric)",
        machine_targets=("run_summary",),
    ),
    _f(
        "risk_difference",
        "risk_difference",
        "风险差",
        canonical_unit=None,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
        source_note="canonical unit = risk_metric_unit(primary_risk_metric)",
    ),
    _f(
        "reference_capacity_mw",
        "reference_capacity_mw",
        "参考容量",
        canonical_unit="mw",
        display_unit="MW",
        display_precision=2,
        null_rule="null_requires_status",
        applies_to=("elcc", "efc", "mri", "rmri"),
        is_conclusion_card=True,
        is_machine_result=True,
        machine_targets=("capacity_credit_results", "run_summary"),
    ),
    _f(
        "accredited_capacity_mw",
        "accredited_capacity_mw",
        "认证容量",
        canonical_unit="mw",
        display_unit="MW",
        display_precision=2,
        null_rule="null_requires_status",
        applies_to=("elcc", "efc", "mri", "rmri"),
        is_conclusion_card=True,
        is_machine_result=True,
        machine_targets=("capacity_credit_results", "run_summary"),
    ),
    _f(
        "capacity_credit_fraction",
        "capacity_credit_fraction",
        "容量信用比例",
        canonical_unit="ratio",
        display_unit="%",
        display_precision=1,
        null_rule="null_requires_status",
        applies_to=("elcc", "rmri"),
        is_conclusion_card=True,
        is_machine_result=True,
        allows_percent_conversion=True,
        source_note="machine value is a RATIO (0.35); percent display is the "
        "single explicit conversion, consumers never multiply by 100",
        machine_targets=("capacity_credit_results", "run_summary"),
    ),
    _f(
        "rmri_status",
        "rmri_status",
        "rMRI 状态",
        null_rule="preserve_zero",
        status_field="rmri_status",
        applies_to=("mri", "rmri"),
        is_conclusion_card=True,
        is_machine_result=True,
        machine_targets=("capacity_credit_results", "run_summary"),
    ),
    _f(
        "rmri_value",
        "rmri_value",
        "rMRI 值",
        canonical_unit="ratio",
        display_unit="%",
        display_precision=3,
        null_rule="null_requires_status",
        status_field="rmri_status",
        applies_to=("mri", "rmri"),
        is_machine_result=True,
        allows_percent_conversion=True,
    ),
    _f(
        "mri_value",
        "mri_value",
        "MRI 值",
        canonical_unit=None,
        null_rule="null_requires_status",
        applies_to=("mri", "rmri"),
        is_machine_result=True,
        source_note="canonical unit = mri_unit from the formal result",
    ),
    _f(
        "mri_unit",
        "mri_unit",
        "MRI 单位",
        applies_to=("mri", "rmri"),
        is_machine_result=True,
    ),
    _f(
        "search_bracket_low_mw",
        "search_bracket_low_mw",
        "搜索区间下界 (MW)",
        canonical_unit="mw",
        display_unit="MW",
        display_precision=2,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
        source_note="brackets preserved for not_converged evidence",
    ),
    _f(
        "search_bracket_high_mw",
        "search_bracket_high_mw",
        "搜索区间上界 (MW)",
        canonical_unit="mw",
        display_unit="MW",
        display_precision=2,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
        source_note="brackets preserved for not_converged evidence",
    ),
    _f(
        "search_bracket_low_risk",
        "search_bracket_low_risk",
        "搜索区间下界风险",
        canonical_unit=None,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
    ),
    _f(
        "search_bracket_high_risk",
        "search_bracket_high_risk",
        "搜索区间上界风险",
        canonical_unit=None,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
    ),
    _f("interpolation_method", "interpolation_method", "插值方法"),
    _f(
        "storage_soc_initialization_mode",
        "storage_soc_initialization_mode",
        "储能SOC初始化模式",
        source_note="R03: the mode the resource chain actually executed",
        is_machine_result=True,
        machine_targets=("run_summary", "capacity_credit_results"),
    ),
    _f(
        "soc_year_boundary_mode",
        "soc_year_boundary_mode",
        "储能SOC年边界",
        source_note="R03: the mode the resource chain actually executed",
        is_machine_result=True,
        machine_targets=("run_summary", "capacity_credit_results"),
    ),
    _f(
        "empirical_risk_resolution",
        "empirical_risk_resolution",
        "经验风险分辨率",
        canonical_unit=None,
        null_rule="null_requires_status",
        applies_to=_SEARCH_METHODS,
    ),
    _f(
        "confidence_level",
        "confidence_level",
        "置信水平",
        canonical_unit="ratio",
        display_unit="%",
        display_precision=1,
        null_rule="null_requires_status",
        allows_percent_conversion=True,
    ),
    _f(
        "lower_ci",
        "lower_ci",
        "置信区间下界",
        canonical_unit=None,
        null_rule="null_requires_status",
    ),
    _f(
        "upper_ci",
        "upper_ci",
        "置信区间上界",
        canonical_unit=None,
        null_rule="null_requires_status",
    ),
    _f("sample_count", "sample_count", "样本数", null_rule="preserve_zero"),
    _f("scenario_bank_id", "scenario_bank_id", "场景库标识"),
    _f(
        "assessment_anchor",
        "assessment_anchor",
        "评估锚点",
        applies_to=("mri", "rmri"),
    ),
    _f("anchor_risk_metric", "anchor_risk_metric", "锚点风险指标", applies_to=("mri", "rmri")),
    _f(
        "anchor_risk_value",
        "anchor_risk_value",
        "锚点风险值",
        canonical_unit=None,
        applies_to=("mri", "rmri"),
    ),
    _f("allocation_type", "allocation_type", "分配类型"),
    _f("baseline_portfolio_version", "baseline_portfolio_version", "基准组合版本"),
    _f(
        "normalization_basis_mwh_per_mw",
        "normalization_basis_mwh_per_mw",
        "归一化基准",
        canonical_unit="mwh/mw",
        applies_to=("mri", "rmri"),
    ),
    _f("step_unit", "step_unit", "步进单位", applies_to=("mri", "rmri")),
    # --- derived machine columns (value semantics still mapped here) ---
    _f(
        "capacity_credit_unit",
        "capacity_credit_unit",
        "容量信用单位",
        canonical_unit="ratio",
        applies_to=("elcc", "rmri"),
        is_machine_result=True,
        machine_targets=("capacity_credit_results",),
        source_note="derived: ratio when capacity_credit_fraction is present, else empty",
    ),
    _f(
        "result_applicability",
        "result_applicability",
        "结果适用性",
        applies_to=_ALL_METHODS,
        is_machine_result=True,
        machine_targets=("capacity_credit_results",),
        source_note="derived: not_applicable / no_capacity_credit / applicable / applicable_no_fraction",
    ),
    _f(
        "reference_capacity_status",
        "reference_capacity_mw",
        "参考容量状态",
        null_rule="null_requires_status",
        applies_to=_ALL_METHODS,
        status_field="capacity_value_type",
        is_machine_result=True,
        machine_targets=("capacity_credit_results",),
        source_note="derived null-status column: value/zero/not_applicable/not_computed/not_converged",
    ),
    _f(
        "accredited_capacity_status",
        "accredited_capacity_mw",
        "认证容量状态",
        null_rule="null_requires_status",
        applies_to=_ALL_METHODS,
        status_field="capacity_value_type",
        is_machine_result=True,
        machine_targets=("capacity_credit_results",),
        source_note="derived null-status column",
    ),
    _f(
        "capacity_credit_fraction_status",
        "capacity_credit_fraction",
        "容量信用比例状态",
        null_rule="null_requires_status",
        applies_to=_ALL_METHODS,
        status_field="capacity_value_type",
        is_machine_result=True,
        machine_targets=("capacity_credit_results",),
        source_note="derived null-status column",
    ),
    _f(
        "primary_risk_metric_unit",
        "primary_risk_metric_unit",
        "主风险指标单位",
        applies_to=_ALL_METHODS,
        is_machine_result=True,
        machine_targets=("run_summary",),
        source_note="derived: risk_metric_unit(primary_risk_metric)",
    ),
    _f(
        "role",
        "role",
        "角色",
        applies_to=_ALL_METHODS,
        is_machine_result=True,
        machine_targets=("adequacy_metrics",),
        source_note="adequacy_metrics long-table role: base/candidate/matched",
    ),
    _f(
        "metric",
        "metric",
        "风险指标",
        applies_to=_ALL_METHODS,
        is_machine_result=True,
        machine_targets=("adequacy_metrics",),
    ),
    _f(
        "value",
        "value",
        "指标值",
        canonical_unit=None,
        applies_to=_ALL_METHODS,
        is_machine_result=True,
        machine_targets=("adequacy_metrics",),
    ),
    _f(
        "unit",
        "unit",
        "指标单位",
        applies_to=_ALL_METHODS,
        is_machine_result=True,
        machine_targets=("adequacy_metrics",),
        source_note="derived from risk_metric_unit(metric)",
    ),
    _f(
        "primary_metric",
        "primary_metric",
        "主指标标记",
        applies_to=_ALL_METHODS,
        is_machine_result=True,
        machine_targets=("adequacy_metrics",),
    ),
)


_FIELD_BY_ID: Mapping[str, ReportField] = MappingProxyType(
    {field.field_id: field for field in REPORT_FIELDS}
)


def field_ids() -> tuple[str, ...]:
    """Canonical, deterministic field order (unique ids)."""
    return tuple(field.field_id for field in REPORT_FIELDS)


def get_field(field_id: str) -> ReportField:
    """Stable lookup; unknown ids raise a stable error."""
    field = _FIELD_BY_ID.get(field_id)
    if field is None:
        raise KeyError(f"unknown report field: {field_id}")
    return field


def _dotted_get(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def extract_field(result: BaseModel, field_id: str) -> Any:
    """Extract ONE formal result field by its mapped dotted path from
    the ALREADY-VALIDATED model, in JSON-machine form (uuids become
    strings, floats stay exact; never display-rounded)."""
    field = get_field(field_id)
    payload = result.model_dump(mode="json")
    return _dotted_get(payload, field.result_path)


def conclusion_card_field_ids() -> tuple[str, ...]:
    return tuple(f.field_id for f in REPORT_FIELDS if f.is_conclusion_card)


def machine_result_field_ids(target: str | None = None) -> tuple[str, ...]:
    """Machine-result field ids in canonical order. With a target (e.g.
    "capacity_credit_results") only fields carrying that product target
    are returned - machine.py derives its result columns from THIS
    projection, never from a private table."""
    if target is None:
        return tuple(f.field_id for f in REPORT_FIELDS if f.is_machine_result)
    return tuple(
        f.field_id
        for f in REPORT_FIELDS
        if f.is_machine_result and target in f.machine_targets
    )


def field_applies_to(field_id: str, method: str) -> bool:
    return method in get_field(field_id).applies_to


def _risk_field_unit(result: BaseModel) -> str | None:
    metric_value = _dotted_get(result.model_dump(mode="json"), "primary_risk_metric")
    try:
        from capacity_credit.contracts.enums import RiskMetric

        return risk_metric_unit(RiskMetric(str(metric_value)))
    except (ValueError, KeyError):
        return None


def unit_for_field(field_id: str, result: BaseModel) -> str | None:
    """Canonical unit of a field for a SPECIFIC result: risk scalars
    follow risk_metric_unit(primary_risk_metric), MRI values follow the
    formal mri_unit; capacity fields are fixed; fraction is ratio."""
    field = get_field(field_id)
    if field_id in _RISK_METRIC_FIELDS:
        return _risk_field_unit(result)
    if field_id in _MRI_VALUE_FIELDS:
        mri_unit_value = _dotted_get(result.model_dump(mode="json"), "mri_unit")
        return str(mri_unit_value) if mri_unit_value is not None else None
    return field.canonical_unit


def display_format(value: Any, field_id: str) -> str:
    """Display-only formatting - NEVER touches the machine value.
    Uses the mapped display unit/precision; percent conversion is the
    single explicit rule for fields that allow it."""
    field = get_field(field_id)
    if value is None:
        return ""
    if isinstance(value, float) and field.display_precision is not None:
        text = f"{value:.{field.display_precision}f}".rstrip("0").rstrip(".")
    elif isinstance(value, float):
        text = repr(value)
    else:
        text = str(value)
    if field.allows_percent_conversion and isinstance(value, (int, float)):
        return f"{value * 100:.{field.display_precision or 1}f}%"
    return text


def _status_is_not_applicable(result: BaseModel, field_id: str) -> bool:
    field = get_field(field_id)
    if field.status_field is None:
        return False
    status_value = _dotted_get(result.model_dump(mode="json"), field.status_field)
    return bool(status_value == "not_applicable")


def classify_value(field_id: str, result: BaseModel) -> str:
    """Method-aware null-status classification of ONE mapped field
    (T23-A round 1): the business semantics are decided in THIS order -
    1) not_applicable: the method does not apply to the field, or the
       mapped status field says not_applicable;
    2) not_converged: result_status == not_converged (never a midpoint);
    3) not_computed: the field applies but no value was formed;
    4) zero: a REAL 0 from the formal result;
    5) value: any other real value.
    'missing' is only the fallback for fields the result model lacks.
    Uses ONLY the formal result fields - never re-derives status."""
    method = _dotted_get(result.model_dump(mode="json"), "method")
    field = get_field(field_id)
    if method is not None and method not in field.applies_to:
        return "not_applicable"
    if _status_is_not_applicable(result, field_id):
        return "not_applicable"
    value = extract_field(result, field_id)
    if value is None:
        status = _dotted_get(result.model_dump(mode="json"), "result_status")
        if status == "not_converged":
            return "not_converged"
        return "not_computed"
    if isinstance(value, (int, float)) and value == 0:
        return "zero"
    return "value"


def summary_field_ids() -> tuple[str, ...]:
    """run_summary top-level mapped fields (run_summary machine target
    projection) - machine.py builds the summary from THIS projection,
    never from a private getattr table."""
    return machine_result_field_ids("run_summary")


def resolve_field(field_id: str, result: BaseModel) -> Any:
    """THE unified field-resolution entry (T23-A round 2): machine.py
    consumes ONLY this projection for every mapped field - derivation
    rules (result_applicability, capacity_credit_unit, capacity status
    columns, primary unit) live HERE, not in the consumer."""
    if field_id == "result_applicability":
        method = _dotted_get(result.model_dump(mode="json"), "method")
        fraction = extract_field(result, "capacity_credit_fraction")
        if method == "adequacy":
            return "not_applicable"
        if method == "mri":
            return "no_capacity_credit"
        if method in ("elcc", "rmri") and fraction is not None:
            return "applicable"
        if method == "efc":
            return "applicable_no_fraction"
        return "missing"
    if field_id == "capacity_credit_unit":
        fraction = extract_field(result, "capacity_credit_fraction")
        return "ratio" if fraction is not None else ""
    if field_id == "primary_risk_metric_unit":
        return unit_for_field("base_risk", result)
    if field_id in (
        "reference_capacity_status",
        "accredited_capacity_status",
        "capacity_credit_fraction_status",
    ):
        return classify_value(get_field(field_id).result_path, result)
    return extract_field(result, field_id)


__all__ = [
    "REPORT_FIELDS",
    "REPORT_MAPPING_VERSION",
    "ReportField",
    "classify_value",
    "conclusion_card_field_ids",
    "display_format",
    "extract_field",
    "field_applies_to",
    "field_ids",
    "get_field",
    "machine_result_field_ids",
    "resolve_field",
    "summary_field_ids",
    "unit_for_field",
]
