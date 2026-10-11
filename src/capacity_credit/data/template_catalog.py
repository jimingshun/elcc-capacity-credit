"""V1-T2-A input template catalog (the field organization for users).

The catalog organizes the EXISTING import contract for users: every
slot's columns are the exact headers the current import chain consumes
(``import_project`` fixed/optional tables, proven by the teaching
fixture that flows end-to-end through import → freeze → adequacy). The
catalog is field organization and user guidance - NOT a second business
schema and NOT a validator. CSV bytes are generated from these
version-controlled definitions (no artifacts/tmp/ignore dependency);
optional-resource slots follow the declared composition, and a slot
with no established reader (hydro_energy_budgets) is an honest
reserved placeholder with no fabricated columns.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TemplateSlot:
    """One input table slot (stable table_key = import file stem)."""

    table_key: str
    file_name: str
    title: str
    category: str
    required: bool
    columns: tuple[str, ...] | None
    column_notes: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    empty_table_rule: str = ""
    cross_refs: tuple[str, ...] = field(default_factory=tuple)
    reserved_note: str = ""


_RESOURCE_KEYS = ("storage", "hydro")

# NOTE: every columns tuple below is VERBATIM the header the current
# import chain consumes (see tests: they are compared against the
# teaching fixture writers and p1_workflow's import file lists).
_TABLES: tuple[TemplateSlot, ...] = (
    TemplateSlot(
        table_key="load_hourly",
        file_name="load_hourly.csv",
        title="逐小时负荷",
        category="公共必需",
        required=True,
        columns=(
            "record_id", "region_id", "scenario_id", "weather_year",
            "timestamp", "load_mw", "load_basis", "is_forecast",
            "source_id", "data_status", "quality_flag",
        ),
        column_notes=(
            ("timestamp", "本地墙上时间（Asia/Shanghai），逐年8760/8784小时"),
            ("load_mw", "负荷，MW"),
            ("load_basis", "口径（如 measured/forecast）"),
            ("is_forecast", "是否预测值（false/true）"),
        ),
        empty_table_rule="必需表，不可缺文件",
        cross_refs=("weather_year_weight_sets: weather_year",),
    ),
    TemplateSlot(
        table_key="thermal_units",
        file_name="thermal_units.csv",
        title="火电机组清单",
        category="常规火电",
        required=True,
        columns=(
            "record_id", "unit_id", "plant_id", "plant_name", "unit_name",
            "technology_code", "technology_label_raw", "capacity_mw",
            "capacity_basis", "region_id", "source_id",
        ),
        column_notes=(
            ("unit_id", "机组业务编号（用于可靠性映射关联）"),
            ("capacity_mw", "铭牌容量，MW"),
            ("technology_code", "技术类型代码（如 CCGT）"),
        ),
        cross_refs=("thermal_unit_reliability_map: unit_id",),
    ),
    TemplateSlot(
        table_key="thermal_reliability_classes",
        file_name="thermal_reliability_classes.csv",
        title="火电可靠性分类",
        category="常规火电",
        required=True,
        columns=(
            "record_id", "reliability_class_id", "class_name", "model_type",
            "authoritative_parameter_set", "for_rate", "source_id",
        ),
        column_notes=(
            ("reliability_class_id", "可靠性分类ID（供机组映射引用）"),
            ("for_rate", "强迫停运率 FOR，0-1"),
            ("model_type", "模型类型（如 sequential_two_state）"),
        ),
        cross_refs=("thermal_unit_reliability_map: reliability_class_id",),
    ),
    TemplateSlot(
        table_key="thermal_unit_reliability_map",
        file_name="thermal_unit_reliability_map.csv",
        title="机组—可靠性分类映射",
        category="常规火电",
        required=True,
        columns=(
            "record_id", "unit_id", "reliability_class_id",
            "mapping_method", "confidence", "source_id",
        ),
        column_notes=(
            ("unit_id", "引用火电机组清单的 unit_id"),
            ("reliability_class_id", "引用可靠性分类的 class_id"),
        ),
        cross_refs=("thermal_units: unit_id", "thermal_reliability_classes: reliability_class_id"),
    ),
    TemplateSlot(
        table_key="renewable_hourly",
        file_name="renewable_hourly.csv",
        title="新能源逐小时出力",
        category="新能源",
        required=True,
        columns=(
            "record_id", "resource_group_id", "scenario_id", "weather_year",
            "timestamp", "generation_basis", "available_generation_mw",
            "actual_generation_mw", "capacity_factor",
            "installed_capacity_mw", "source_id",
        ),
        column_notes=(
            ("available_generation_mw", "可用出力，MW"),
            ("actual_generation_mw", "实际出力，MW"),
            ("resource_group_id", "新能源资源分组（与装机表一致）"),
        ),
        cross_refs=("renewable_capacities: resource_group_id",),
    ),
    TemplateSlot(
        table_key="renewable_capacities",
        file_name="renewable_capacities.csv",
        title="新能源装机（分年）",
        category="新能源",
        required=True,
        columns=(
            "record_id", "resource_group_id", "technology", "region_id",
            "year", "installed_capacity_mw", "capacity_basis", "source_id",
        ),
        column_notes=(
            ("installed_capacity_mw", "装机容量，MW"),
            ("year", "数据年份"),
        ),
        cross_refs=("renewable_hourly: resource_group_id",),
    ),
    TemplateSlot(
        table_key="intertie_hourly",
        file_name="intertie_hourly.csv",
        title="联络线逐小时交换",
        category="联络线",
        required=True,
        columns=(
            "record_id", "intertie_id", "scenario_id", "timestamp",
            "net_import_mw", "scheduled_import_mw", "scheduled_export_mw",
            "source_id",
        ),
        column_notes=(
            ("net_import_mw", "净受入电力，MW（送出为负）"),
            ("intertie_id", "引用联络线清单的 intertie_id"),
        ),
        cross_refs=("interties: intertie_id",),
    ),
    TemplateSlot(
        table_key="interties",
        file_name="interties.csv",
        title="联络线清单",
        category="联络线",
        required=True,
        columns=(
            "record_id", "intertie_id", "intertie_name", "intertie_type",
            "rated_capacity_mw", "normal_import_limit_mw",
            "normal_export_limit_mw", "availability_model", "source_id",
        ),
        column_notes=(
            ("rated_capacity_mw", "额定容量，MW"),
            ("availability_model", "可用性模型（如 fixed_available）"),
        ),
        cross_refs=("intertie_hourly: intertie_id",),
    ),
    TemplateSlot(
        table_key="weather_year_weight_sets",
        file_name="weather_year_weight_sets.csv",
        title="气象年权重集",
        category="时间轴与权重",
        required=True,
        columns=(
            "record_id", "weight_set_id", "weather_year", "weight",
            "source_id", "data_status", "note",
        ),
        column_notes=(
            ("weight_set_id", "权重集标识（运行时按集合引用）"),
            ("weight", "该气象年权重（同为全集和为1）"),
        ),
    ),
    TemplateSlot(
        table_key="storage_units",
        file_name="storage_units.csv",
        title="储能电站清单",
        category="储能",
        required=False,
        columns=(
            "record_id", "storage_id", "storage_name", "storage_type",
            "region_id", "charge_power_mw", "discharge_power_mw",
            "energy_capacity_mwh", "charge_efficiency",
            "discharge_efficiency", "soc_min_fraction", "soc_max_fraction",
            "self_discharge_per_hour", "availability_fraction", "source_id",
        ),
        column_notes=(
            ("charge_power_mw", "充电功率，MW"),
            ("discharge_power_mw", "放电功率，MW"),
            ("energy_capacity_mwh", "能量容量，MWh"),
            ("charge_efficiency", "充电效率，0-1"),
            ("discharge_efficiency", "放电效率，0-1"),
        ),
        empty_table_rule=(
            "仅当资源构成声明“包含储能”时需要；声明“明确不包含”可不提供"
            "（现行导入契约允许缺省该文件）"
        ),
    ),
    TemplateSlot(
        table_key="hydro_units",
        file_name="hydro_units.csv",
        title="水电站清单",
        category="水电",
        required=False,
        columns=(
            "record_id", "hydro_id", "hydro_name", "region_id",
            "capacity_mw", "regulation_type", "source_id",
        ),
        column_notes=(
            ("capacity_mw", "装机容量，MW"),
            ("regulation_type", "调节类型（如 reservoir/径流式）"),
        ),
        empty_table_rule=(
            "仅当资源构成声明“包含水电”时需要（现行导入契约允许缺省该文件）"
        ),
        cross_refs=("hydro_hourly: hydro_id",),
    ),
    TemplateSlot(
        table_key="hydro_hourly",
        file_name="hydro_hourly.csv",
        title="水电逐小时固定出力",
        category="水电",
        required=False,
        columns=(
            "record_id", "hydro_id", "scenario_id", "timestamp",
            "fixed_output_mw", "source_id",
        ),
        column_notes=(
            ("fixed_output_mw", "固定出力，MW"),
            ("hydro_id", "引用水电站清单的 hydro_id"),
        ),
        empty_table_rule="随水电站清单一同提供",
        cross_refs=("hydro_units: hydro_id",),
    ),
    TemplateSlot(
        table_key="hydro_energy_budgets",
        file_name="hydro_energy_budgets.csv",
        title="水电电量约束（预留）",
        category="水电",
        required=False,
        columns=None,
        reserved_note=(
            "现行导入与计算链尚未读取该表，字段契约未定义；本包不提供"
            "模板以免与后续读取器不兼容"
        ),
    ),
    TemplateSlot(
        table_key="emergency_programs",
        file_name="emergency_programs.csv",
        title="应急程序（有序）",
        category="运行规则",
        required=False,
        columns=(
            "record_id", "program_id", "program_name", "kind",
            "available_capacity_mw", "max_duration_hours_per_event",
            "max_events_per_year", "energy_limit_mwh_per_year",
            "availability_fraction", "source_id",
        ),
        column_notes=(
            ("kind", "程序类型：demand_response（需求响应）/ orderly_curtailment（有序用电）"),
            ("available_capacity_mw", "可用容量，MW（正数）"),
            ("max_duration_hours_per_event", "单次启用最长小时数（正整数）"),
            ("max_events_per_year", "年内最多启用次数（正整数）"),
            ("energy_limit_mwh_per_year", "年度能量上限，MWh（可选；提供时须为正数）"),
            ("availability_fraction", "可用率，0-1"),
        ),
        empty_table_rule=(
            "可选表：不提供=无应急程序层（失负荷口径未扣减需求响应/有序用电，"
            "方法说明第二章(二)4）。行序即启用顺序：缺口先由储能、再按行序由"
            "应急程序填补，仍不足才记失负荷"
        ),
        cross_refs=("emergency_programs: program_id",),
    ),
)

_BY_KEY = {slot.table_key: slot for slot in _TABLES}

REQUIRED_KEYS = tuple(
    slot.table_key for slot in _TABLES if slot.required and slot.columns
)

OPTIONAL_RESOURCE_SLOTS = {
    "storage": ("storage_units",),
    "hydro": ("hydro_units", "hydro_hourly"),
}


def get_slot(table_key: str) -> TemplateSlot | None:
    return _BY_KEY.get(table_key)


def all_slots() -> tuple[TemplateSlot, ...]:
    return _TABLES


def templates_for_composition(
    resource_composition: dict[str, str],
) -> dict[str, Any]:
    """The template list for one declared resource composition: the
    public required slots are ALWAYS included; optional-resource slots
    follow the three-state declaration (included → template; excluded →
    a legal-absence note; unconfirmed → the requirements cannot be
    determined yet). Nothing here fabricates data or zeroes unknown
    resources."""
    composition = {
        key: str(resource_composition.get(key, "unconfirmed"))
        for key in _RESOURCE_KEYS
    }
    slots: list[dict[str, Any]] = []
    notes: list[str] = []
    for slot in _TABLES:
        if slot.reserved_note:
            notes.append(f"{slot.title}：{slot.reserved_note}")
            continue
        if slot.required:
            slots.append(_slot_dict(slot, reason=None))
            continue
        state = composition.get(
            "storage" if slot.table_key == "storage_units" else "hydro",
            "unconfirmed",
        )
        if state == "included":
            slots.append(_slot_dict(slot, reason=None))
        elif state == "excluded":
            notes.append(
                f"{slot.title}：已声明明确不包含该资源——可不提供"
                f"（{slot.empty_table_rule}）"
            )
        else:
            notes.append(
                f"{slot.title}：资源构成尚未确认，所需模板暂不能确定"
                "（请在资源构成中明确）"
            )
    return {"slots": slots, "composition": composition, "notes": notes}


def _slot_dict(slot: TemplateSlot, reason: str | None) -> dict[str, Any]:
    return {
        "table_key": slot.table_key,
        "file_name": slot.file_name,
        "title": slot.title,
        "category": slot.category,
        "required": slot.required,
        "columns": list(slot.columns or ()),
        "column_notes": list(slot.column_notes),
        "empty_table_rule": slot.empty_table_rule,
        "cross_refs": list(slot.cross_refs),
        "reason": reason,
    }


def render_csv_bytes(table_key: str) -> tuple[str, bytes]:
    """The downloadable fillable CSV for one slot: (file_name, bytes).
    utf-8-sig so Excel opens Chinese headers correctly; header-only
    (machine-readable, no guidance rows inside the data area)."""
    slot = _BY_KEY.get(table_key)
    if slot is None or slot.columns is None:
        raise KeyError(f"unknown or reserved template slot: {table_key!r}")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(slot.columns)
    data = buffer.getvalue().encode("utf-8-sig")
    return slot.file_name, data


__all__ = [
    "REQUIRED_KEYS",
    "OPTIONAL_RESOURCE_SLOTS",
    "TemplateSlot",
    "all_slots",
    "get_slot",
    "render_csv_bytes",
    "templates_for_composition",
]
