"""B1 derived data versions - THE confirmed derivation rules.

User-confirmed decisions (2026-09-16, checklist DR-01..DR-10):

- DR-03 storage duration axis: discharge power stays, energy becomes
  power x hours; efficiency / SOC window / availability / dispatch and
  self-discharge are untouched;
- DR-04 renewable penetration axis: installed capacity scales by k and
  the hourly output scales by the SAME k (weather-year shape and
  inter-site correlation preserved, so capacity_factor is invariant);
- DR-06 load calibration axis: every hourly load value is multiplied by
  (1 + x); intertie, thermal capacity, renewables and storage are never
  touched;
- DR-07 forbidden tables (reliability classes / unit map / weather-year
  weight sets) pass through BYTE-IDENTICAL and the record attests it;
  PC-01 REFINES this for one rule: add_perfect_capacity_mw must append
  rows to the two reliability tables by design (its synthetic zero-FOR
  unit needs a class and a mapping), so for THAT rule the attestation
  becomes append-only - every pre-existing row stays byte-identical in
  order and the record names every appended row (rulings R9/R11,
  branch plan 2026-09-19 task B);
- DR-05 penetration is renewable installed capacity over TOTAL system
  installed capacity, and the formula actually used is written into the
  record (so the report can state the caliber);
- a missing slot or a non-numeric cell is an honest refusal naming the
  slot/row/column - never a silent 0 and never a guessed default.

Rules are PURE: baseline CSV bytes in, derived CSV bytes plus a
derivation record out. No IO, no project state, no kernel call - the
caller (the derived-version pipeline) owns reading the frozen baseline,
registering the import batch and the human confirmation DR-01 requires.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Mapping
from typing import Any

RULE_STORAGE_DURATION = "storage_duration"
RULE_RENEWABLE_SCALE = "renewable_scale"
RULE_LOAD_SCALE = "load_scale"
RULE_PERFECT_CAPACITY = "add_perfect_capacity_mw"
RULE_SEASONAL_LOAD_SCALE = "seasonal_load_scale"
RULE_VERSION = "1.0"

# DR-07: never derived, always passed through byte-identical
FORBIDDEN_SLOTS = (
    "thermal_reliability_classes",
    "thermal_unit_reliability_map",
    "weather_year_weight_sets",
)

# PC-01: the forbidden tables the perfect-capacity rule may APPEND to
# (never modify or delete a pre-existing row); the weight sets stay
# byte-identical for every rule including this one
APPEND_ONLY_SLOTS = (
    "thermal_reliability_classes",
    "thermal_unit_reliability_map",
)

PERFECT_CLASS_ID = "PC_G0"
SYNTHETIC_SOURCE = "teaching_synthetic"

_STORAGE_SLOT = "storage_units"
_RENEWABLE_HOURLY = "renewable_hourly"
_RENEWABLE_CAPACITY = "renewable_capacities"
_LOAD_SLOT = "load_hourly"
_THERMAL_SLOT = "thermal_units"
_HYDRO_SLOT = "hydro_units"


class DerivationError(ValueError):
    """A derivation refusal (display kind + reason)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _read_table(data: bytes, slot: str) -> tuple[list[str], list[dict[str, str]]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    columns = list(reader.fieldnames or [])
    if not columns:
        raise DerivationError("invalid_input", f"{slot}：空表（无表头），无法派生")
    rows = [dict(row) for row in reader]
    if not rows:
        raise DerivationError("invalid_input", f"{slot}：只有表头没有数据行，无法派生")
    return columns, rows


def _write_table(columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    # utf-8-sig keeps the import chain's encoding contract
    return buffer.getvalue().encode("utf-8-sig")


def _scaled(
    rows: list[dict[str, str]],
    slot: str,
    column: str,
    factor: float,
    *,
    digits: int = 6,
) -> None:
    """Multiply one numeric column IN PLACE, refusing (never zeroing) a
    blank or non-numeric cell and naming the offending row."""
    if column not in rows[0]:
        raise DerivationError(
            "invalid_input", f"{slot}：缺少列 {column}，与数据契约不符"
        )
    for index, row in enumerate(rows):
        raw = (row.get(column) or "").strip()
        if raw == "":
            raise DerivationError(
                "invalid_input",
                f"{slot} 第{index + 1}行 {column} 为空：派生不得补0，请先修正基线数据",
            )
        try:
            value = float(raw)
        except ValueError:
            raise DerivationError(
                "invalid_input",
                f"{slot} 第{index + 1}行 {column} 非数值（{raw!r}）：无法派生",
            ) from None
        if not math.isfinite(value):
            raise DerivationError(
                "invalid_input", f"{slot} 第{index + 1}行 {column} 非有限数值"
            )
        row[column] = repr(round(value * factor, digits))


def _sum_column(
    files: Mapping[str, bytes], slot: str, column: str
) -> tuple[float | None, str | None]:
    """The total of one numeric column, or (None, reason) when the table
    is absent/empty - an absent optional table is disclosed, never read
    as 0."""
    data = files.get(slot)
    if data is None:
        return None, f"{slot} 未提供"
    try:
        _columns, rows = _read_table(data, slot)
    except DerivationError as exc:
        return None, exc.message
    if column not in rows[0]:
        return None, f"{slot} 缺少列 {column}"
    total = 0.0
    for row in rows:
        raw = (row.get(column) or "").strip()
        if raw == "":
            continue
        try:
            total += float(raw)
        except ValueError:
            return None, f"{slot} 的 {column} 含非数值（{raw!r}）"
    return total, None


def derive_storage_duration(
    files: Mapping[str, bytes], hours: float
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """DR-03: energy = discharge power x hours; power and every other
    storage parameter stay exactly as in the baseline."""
    try:
        value = float(hours)
    except (TypeError, ValueError):
        raise DerivationError(
            "invalid_input", f"时长必须为数值（小时），收到 {hours!r}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise DerivationError("invalid_input", "储能时长必须为大于0的有限小时数")
    if _STORAGE_SLOT not in files:
        raise DerivationError(
            "invalid_input",
            "基线数据没有储能表（storage_units）：无法派生长度轴，"
            "请确认项目声明包含储能",
        )
    columns, rows = _read_table(files[_STORAGE_SLOT], _STORAGE_SLOT)
    if "discharge_power_mw" not in rows[0]:
        raise DerivationError(
            "invalid_input", "storage_units 缺少 discharge_power_mw 列"
        )
    powers: list[float] = []
    for index, row in enumerate(rows):
        raw = (row.get("discharge_power_mw") or "").strip()
        try:
            power = float(raw)
        except ValueError:
            raise DerivationError(
                "invalid_input",
                f"storage_units 第{index + 1}行 discharge_power_mw 非数值（{raw!r}）",
            ) from None
        powers.append(power)
        row["energy_capacity_mwh"] = repr(round(power * value, 6))
    derived = dict(files)
    derived[_STORAGE_SLOT] = _write_table(columns, rows)
    record = {
        "rule_id": RULE_STORAGE_DURATION,
        "rule_version": RULE_VERSION,
        "params": {"hours": value},
        "changed_slots": [_STORAGE_SLOT],
        "formula": "energy_capacity_mwh = discharge_power_mw × hours",
        "untouched": [
            "charge_power_mw",
            "discharge_power_mw",
            "charge_efficiency",
            "discharge_efficiency",
            "soc_min_fraction",
            "soc_max_fraction",
            "self_discharge_per_hour",
            "availability_fraction",
        ],
        "unit_count": len(rows),
        "power_mw_range": [min(powers), max(powers)] if powers else None,
    }
    return derived, record


def derive_renewable_scale(
    files: Mapping[str, bytes], factor: float
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """DR-04: installed capacity AND hourly output scale by the same k,
    so the weather-year shape and inter-site correlation are preserved
    (capacity_factor is invariant by construction)."""
    try:
        value = float(factor)
    except (TypeError, ValueError):
        raise DerivationError(
            "invalid_input", f"缩放倍数必须为数值，收到 {factor!r}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise DerivationError("invalid_input", "新能源缩放倍数必须为大于0的有限数")
    for slot in (_RENEWABLE_CAPACITY, _RENEWABLE_HOURLY):
        if slot not in files:
            raise DerivationError(
                "invalid_input", f"基线数据缺少 {slot}：无法派生渗透率轴"
            )
    derived = dict(files)
    columns, rows = _read_table(files[_RENEWABLE_CAPACITY], _RENEWABLE_CAPACITY)
    _scaled(rows, _RENEWABLE_CAPACITY, "installed_capacity_mw", value)
    derived[_RENEWABLE_CAPACITY] = _write_table(columns, rows)
    columns, rows = _read_table(files[_RENEWABLE_HOURLY], _RENEWABLE_HOURLY)
    for column in (
        "available_generation_mw",
        "actual_generation_mw",
        "installed_capacity_mw",
    ):
        if column in rows[0]:
            _scaled(rows, _RENEWABLE_HOURLY, column, value)
    derived[_RENEWABLE_HOURLY] = _write_table(columns, rows)
    record = {
        "rule_id": RULE_RENEWABLE_SCALE,
        "rule_version": RULE_VERSION,
        "params": {"factor": value},
        "changed_slots": [_RENEWABLE_CAPACITY, _RENEWABLE_HOURLY],
        "formula": (
            "installed_capacity_mw ×= k；available/actual_generation_mw ×= k"
            "（capacity_factor 不变，形状与相关性保留）"
        ),
    }
    record["penetration"] = _penetration(derived)
    return derived, record


def derive_load_scale(
    files: Mapping[str, bytes], scale_percent: float
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """DR-06: every hourly load value x (1 + x%); nothing else moves."""
    try:
        percent = float(scale_percent)
    except (TypeError, ValueError):
        raise DerivationError(
            "invalid_input", f"负荷缩放百分比必须为数值，收到 {scale_percent!r}"
        ) from None
    if not math.isfinite(percent) or percent <= -100:
        raise DerivationError(
            "invalid_input", "负荷缩放百分比必须为有限数且大于 -100（否则负荷非正）"
        )
    if _LOAD_SLOT not in files:
        raise DerivationError("invalid_input", "基线数据缺少 load_hourly：无法派生负荷轴")
    factor = 1.0 + percent / 100.0
    columns, rows = _read_table(files[_LOAD_SLOT], _LOAD_SLOT)
    _scaled(rows, _LOAD_SLOT, "load_mw", factor)
    derived = dict(files)
    derived[_LOAD_SLOT] = _write_table(columns, rows)
    record = {
        "rule_id": RULE_LOAD_SCALE,
        "rule_version": RULE_VERSION,
        "params": {"scale_percent": percent},
        "changed_slots": [_LOAD_SLOT],
        "formula": "load_mw ×= (1 + x/100)",
        "untouched": [
            "intertie_hourly",
            "interties",
            "thermal_units",
            "renewable_hourly",
            "renewable_capacities",
            "storage_units",
            "hydro_units",
            "hydro_hourly",
        ],
        "row_count": len(rows),
    }
    return derived, record


def derive_perfect_capacity(
    files: Mapping[str, bytes], capacity_mw: float
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """PC-01: append ONE synthetic conventional unit of exactly X MW with
    a zero forced-outage rate and no energy limit - the perfect-capacity
    axis of baseline calibration (branch plan 2026-09-19, rulings R9/R11).

    Only the ADD direction exists: X must be > 0 (removal is not built).
    The unit needs a reliability class and a unit mapping, so this rule -
    and only this rule - APPENDS to the two DR-07-forbidden tables
    (append-only attested; every pre-existing row byte-identical in
    order). Load, renewables, storage, hydro, interties and the
    weather-year weight sets are never touched."""
    try:
        value = float(capacity_mw)
    except (TypeError, ValueError):
        raise DerivationError(
            "invalid_input",
            f"完美容量必须为数值（MW），收到 {capacity_mw!r}",
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise DerivationError(
            "invalid_input",
            "完美容量追加必须为大于0的有限MW数（'移除'方向第一版不做）",
        )
    for slot in (_THERMAL_SLOT, "thermal_reliability_classes",
                 "thermal_unit_reliability_map"):
        if slot not in files:
            raise DerivationError(
                "invalid_input",
                f"基线数据缺少 {slot}：完美容量机组需要可靠性类别与机组映射"
                "（PC-01），无法派生",
            )
    columns, rows = _read_table(files[_THERMAL_SLOT], _THERMAL_SLOT)
    taken = {str(r.get("unit_id") or "") for r in rows}
    unit_id = "PC_U001"
    if unit_id in taken:
        raise DerivationError(
            "invalid_input",
            f"基线已存在合成机组标识 {unit_id}：完美容量派生只从干净基线"
            "出发（合成机组不该出现在冻结基线中），已拒绝",
        )
    region = str((rows[0].get("region_id") or "").strip())
    unit_row: dict[str, Any] = {c: "" for c in columns}
    unit_row.update({
        "record_id": unit_id,
        "unit_id": unit_id,
        "plant_id": "PC_PLANT",
        "plant_name": "完美容量合成机组",
        "unit_name": f"完美容量 {value:g} MW",
        "technology_code": "PERFECT",
        "technology_label_raw": "perfect capacity (synthetic)",
        "capacity_mw": repr(round(value, 6)),
        "capacity_basis": "nameplate",
        "region_id": region,
        "source_id": SYNTHETIC_SOURCE,
    })
    rows.append(unit_row)
    class_columns, class_rows = _read_table(
        files["thermal_reliability_classes"], "thermal_reliability_classes"
    )
    existing = next(
        (
            r
            for r in class_rows
            if str(r.get("reliability_class_id") or "") == PERFECT_CLASS_ID
        ),
        None,
    )
    class_appended: list[str] = []
    if existing is None:
        class_row: dict[str, Any] = {c: "" for c in class_columns}
        class_row.update({
            "record_id": PERFECT_CLASS_ID,
            "reliability_class_id": PERFECT_CLASS_ID,
            "class_name": "完美容量合成机组（零强迫停运率）",
            "model_type": "sequential_two_state",
            "authoritative_parameter_set": "for_only",
            "for_rate": "0.0",
            "source_id": SYNTHETIC_SOURCE,
        })
        class_rows.append(class_row)
        class_appended = [PERFECT_CLASS_ID]
    else:
        try:
            existing_for = float(existing.get("for_rate"))
        except (TypeError, ValueError):
            existing_for = None
        if existing_for != 0.0:
            raise DerivationError(
                "invalid_input",
                f"可靠性类别 {PERFECT_CLASS_ID} 已存在且 for_rate="
                f"{existing.get('for_rate')!r}≠0：类别标识冲突，已拒绝",
            )
    map_columns, map_rows = _read_table(
        files["thermal_unit_reliability_map"],
        "thermal_unit_reliability_map",
    )
    map_row: dict[str, Any] = {c: "" for c in map_columns}
    map_row.update({
        "record_id": unit_id,
        "unit_id": unit_id,
        "reliability_class_id": PERFECT_CLASS_ID,
        "mapping_method": "explicit",
        "confidence": "high",
        "source_id": SYNTHETIC_SOURCE,
    })
    map_rows.append(map_row)
    derived = dict(files)
    derived[_THERMAL_SLOT] = _write_table(columns, rows)
    derived["thermal_reliability_classes"] = _write_table(
        class_columns, class_rows
    )
    derived["thermal_unit_reliability_map"] = _write_table(
        map_columns, map_rows
    )
    record: dict[str, Any] = {
        "rule_id": RULE_PERFECT_CAPACITY,
        "rule_version": RULE_VERSION,
        "params": {"capacity_mw": value},
        "changed_slots": [
            _THERMAL_SLOT,
            "thermal_reliability_classes",
            "thermal_unit_reliability_map",
        ],
        "formula": (
            f"追加一台 {value:g} MW、FOR=0、无能量限制的合成常规机组"
            "（完美容量归一 PC-01；负荷、新能源、储能、水电、联络线与"
            "权重集不动）"
        ),
        "untouched": [
            "load_hourly",
            "renewable_hourly",
            "renewable_capacities",
            "storage_units",
            "hydro_units",
            "hydro_hourly",
            "intertie_hourly",
            "interties",
            "weather_year_weight_sets",
        ],
        "appended": {
            "thermal_units": [unit_id],
            "thermal_reliability_classes": class_appended,
            "thermal_unit_reliability_map": [unit_id],
        },
        "perfect_capacity_mw": value,
        "unit_id": unit_id,
        "reliability_class_id": PERFECT_CLASS_ID,
        "synthetic_source": SYNTHETIC_SOURCE,
        "preexisting_row_counts": {
            _THERMAL_SLOT: len(rows) - 1,
            "thermal_reliability_classes": len(class_rows)
            - len(class_appended),
            "thermal_unit_reliability_map": len(map_rows) - 1,
        },
    }
    return derived, record


def derive_seasonal_load_scale(
    files: Mapping[str, bytes],
    heating_months: Any,
    heating_scale_percent: Any,
    non_heating_scale_percent: Any,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """SL-01 (method sign-off 2026-09-19): two-segment hourly load
    scaling - the heating months x(1+a%), the remaining months
    x(1+b%). The month set is an explicit rule parameter (default
    proposal [11,12,1,2,3], never hard-coded here); equal scales
    degenerate to the plain DR-06 uniform scaling. Only load_hourly
    moves; a row whose timestamp carries no parseable month refuses
    with the row named - never silently assigned to either segment."""
    months, month_values = _validate_heating_months(heating_months)
    heating = _validate_load_scale_percent(heating_scale_percent)
    non_heating = _validate_load_scale_percent(non_heating_scale_percent)
    if _LOAD_SLOT not in files:
        raise DerivationError(
            "invalid_input", "基线数据缺少 load_hourly：无法派生季节负荷轴"
        )
    columns, rows = _read_table(files[_LOAD_SLOT], _LOAD_SLOT)
    for index, row in enumerate(rows):
        timestamp = (row.get("timestamp") or "").strip()
        month = _timestamp_month(timestamp)
        if month is None:
            raise DerivationError(
                "invalid_input",
                f"load_hourly 第{index + 1}行时间戳无法解析月份"
                f"（{timestamp!r}）：季节派生拒绝猜测，请修正基线数据",
            )
        factor = 1.0 + (heating if month in months else non_heating) / 100.0
        raw = (row.get("load_mw") or "").strip()
        if raw == "":
            raise DerivationError(
                "invalid_input",
                f"load_hourly 第{index + 1}行 load_mw 为空：派生不得补0",
            )
        try:
            value = float(raw)
        except ValueError:
            raise DerivationError(
                "invalid_input",
                f"load_hourly 第{index + 1}行 load_mw 非数值（{raw!r}）",
            ) from None
        if not math.isfinite(value):
            raise DerivationError(
                "invalid_input", f"load_hourly 第{index + 1}行 load_mw 非有限数值"
            )
        row["load_mw"] = repr(round(value * factor, 6))
    derived = dict(files)
    derived[_LOAD_SLOT] = _write_table(columns, rows)
    record: dict[str, Any] = {
        "rule_id": RULE_SEASONAL_LOAD_SCALE,
        "rule_version": RULE_VERSION,
        "params": {
            "heating_months": list(month_values),
            "heating_scale_percent": heating,
            "non_heating_scale_percent": non_heating,
        },
        "changed_slots": [_LOAD_SLOT],
        "formula": (
            "load_mw ×= (1 + a/100) 当月份∈供暖季集合，否则 ×= (1 + b/100)"
            "（SL-01 两段负荷缩放）"
        ),
        "heating_month_count": len(months),
        "untouched": [
            "intertie_hourly",
            "interties",
            "thermal_units",
            "renewable_hourly",
            "renewable_capacities",
            "storage_units",
            "hydro_units",
            "hydro_hourly",
        ],
        "row_count": len(rows),
    }
    return derived, record


def _validate_heating_months(heating_months: Any) -> tuple[frozenset[int], list[int]]:
    try:
        values = [int(m) for m in heating_months]
    except (TypeError, ValueError):
        raise DerivationError(
            "invalid_input",
            f"heating_months 必须为 1–12 的月份列表，收到 {heating_months!r}",
        ) from None
    months = frozenset(values)
    if (
        not months
        or len(values) != len(months)
        or not months <= frozenset(range(1, 13))
        or months == frozenset(range(1, 13))
    ):
        raise DerivationError(
            "invalid_input",
            "heating_months 月份集合必须为 1–12 的非空真子集且不重复"
            f"（不能是全部 12 个月），收到 {heating_months!r}",
        )
    return months, values


def _validate_load_scale_percent(value: Any) -> float:
    try:
        percent = float(value)
    except (TypeError, ValueError):
        raise DerivationError(
            "invalid_input", f"负荷缩放百分比必须为数值，收到 {value!r}"
        ) from None
    if not math.isfinite(percent) or percent <= -100:
        raise DerivationError(
            "invalid_input", "负荷缩放百分比必须为有限数且大于 -100"
        )
    return percent


def _timestamp_month(timestamp: str) -> int | None:
    """The month of a standard ``YYYY-MM-DD...`` timestamp; None when
    the shape is not parseable (the caller refuses)."""
    if len(timestamp) < 7 or timestamp[4] != "-" or timestamp[7] != "-":
        return None
    try:
        month = int(timestamp[5:7])
    except ValueError:
        return None
    return month if 1 <= month <= 12 else None


def _penetration(files: Mapping[str, bytes]) -> dict[str, Any]:
    """DR-05: renewable installed capacity / TOTAL system installed
    capacity, with the formula and every absent table disclosed."""
    renewable, note_r = _sum_column(
        files, _RENEWABLE_CAPACITY, "installed_capacity_mw"
    )
    thermal, note_t = _sum_column(files, _THERMAL_SLOT, "capacity_mw")
    hydro, note_h = _sum_column(files, _HYDRO_SLOT, "capacity_mw")
    storage, note_s = _sum_column(files, _STORAGE_SLOT, "discharge_power_mw")
    parts = {
        "renewable_mw": renewable,
        "thermal_mw": thermal,
        "hydro_mw": hydro,
        "storage_power_mw": storage,
    }
    missing = [n for n in (note_r, note_t, note_h, note_s) if n]
    total = sum(v for v in parts.values() if v is not None)
    ratio = (renewable / total) if (renewable is not None and total > 0) else None
    return {
        "definition": "新能源装机 ÷ 系统总装机（火电＋水电＋新能源＋储能功率）",
        "parts": parts,
        "total_mw": total or None,
        "ratio": ratio,
        "missing": missing,
    }


_DERIVERS = {
    RULE_STORAGE_DURATION: derive_storage_duration,
    RULE_RENEWABLE_SCALE: derive_renewable_scale,
    RULE_LOAD_SCALE: derive_load_scale,
    RULE_PERFECT_CAPACITY: derive_perfect_capacity,
    RULE_SEASONAL_LOAD_SCALE: derive_seasonal_load_scale,
}


def _attest_append_only(original: bytes, derived: bytes, slot: str) -> None:
    """PC-01's DR-07 refinement: the derived table must be the ORIGINAL
    table plus appended rows only - same header, every pre-existing row
    byte-identical IN ORDER, nothing removed or rewritten."""
    before = _read_table(original, slot)
    after = _read_table(derived, slot)
    if before[0] != after[0]:
        raise DerivationError(
            "internal_error",
            f"派生规则改动了 {slot} 的表头（PC-01 仅允许追加行）：已拒绝",
        )
    old_rows = before[1]
    new_rows = after[1]
    if len(new_rows) < len(old_rows) or new_rows[: len(old_rows)] != old_rows:
        raise DerivationError(
            "internal_error",
            f"派生规则改动了 {slot} 的既有行（PC-01 仅允许追加行）：已拒绝",
        )


def derive(
    rule_id: str, files: Mapping[str, bytes], params: Mapping[str, Any]
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """THE single derivation entry: dispatch by rule id, apply the rule,
    then attest that every DR-07 forbidden table is intact. For the
    perfect-capacity rule the two reliability tables are attested
    APPEND-ONLY (existing rows byte-identical, appends named in the
    record); the weather-year weight sets must stay byte-identical for
    every rule without exception."""
    rule = _DERIVERS.get(rule_id)
    if rule is None:
        raise DerivationError(
            "invalid_input",
            f"未知派生规则 {rule_id!r}（可用：{'/'.join(sorted(_DERIVERS))}）",
        )
    if rule_id == RULE_STORAGE_DURATION:
        derived, record = rule(files, params.get("hours"))
    elif rule_id == RULE_RENEWABLE_SCALE:
        derived, record = rule(files, params.get("factor"))
    elif rule_id == RULE_PERFECT_CAPACITY:
        derived, record = rule(files, params.get("capacity_mw"))
    elif rule_id == RULE_SEASONAL_LOAD_SCALE:
        derived, record = rule(
            files,
            params.get("heating_months"),
            params.get("heating_scale_percent"),
            params.get("non_heating_scale_percent"),
        )
    else:
        derived, record = rule(files, params.get("scale_percent"))
    append_only = (
        APPEND_ONLY_SLOTS if rule_id == RULE_PERFECT_CAPACITY else ()
    )
    for slot in FORBIDDEN_SLOTS:
        if slot not in files:
            continue
        if derived.get(slot) == files[slot]:
            continue
        if slot in append_only:
            _attest_append_only(files[slot], derived.get(slot, b""), slot)
            continue
        raise DerivationError(
            "internal_error",
            f"派生规则改动了禁止修改的表 {slot}（DR-07）：已拒绝",
        )
    record["forbidden_slots_untouched"] = [
        s
        for s in FORBIDDEN_SLOTS
        if s in files and derived.get(s) == files[s]
    ]
    return derived, record


__all__ = [
    "APPEND_ONLY_SLOTS",
    "FORBIDDEN_SLOTS",
    "PERFECT_CLASS_ID",
    "RULE_LOAD_SCALE",
    "RULE_PERFECT_CAPACITY",
    "RULE_RENEWABLE_SCALE",
    "RULE_SEASONAL_LOAD_SCALE",
    "RULE_STORAGE_DURATION",
    "RULE_VERSION",
    "SYNTHETIC_SOURCE",
    "DerivationError",
    "derive",
    "derive_load_scale",
    "derive_perfect_capacity",
    "derive_renewable_scale",
    "derive_seasonal_load_scale",
    "derive_storage_duration",
]
