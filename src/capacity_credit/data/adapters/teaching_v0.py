"""Teaching template v0 adapter.

Converts the 12 legacy teaching CSVs (real template structure inspected
read-only from the user-provided ZIP, SHA-256
71f27928c0797275cb68cf3e0e405a22f1b4c4d97507d21f4e29ab8f630e983a)
into standard-layer tables. Every legacy construct is handled only at
this boundary: trailing ``#来源`` lines, ``-999``, percentages,
wide-to-long reshaping, hour-end timestamps, naive local timestamps,
legacy enums and name-mapping candidates. Original inputs are never
modified; unconvertible records go to the rejected isolation area;
every conversion records ``original_value``/``normalized_value``/
``rule_version``/row locator in the transformation log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from capacity_credit.application.atomic_io import hash_payload
from capacity_credit.data.io import detect_encoding, read_csv
from capacity_credit.data.registry import (
    RawFileRegistry,
    SourceLedger,
    TransformationLog,
)
from capacity_credit.data.validation import IssueCode, ValidationIssue

TEACHING_TABLE_NAMES = {
    "A1": "load_hourly",
    "B1": "thermal_units",
    "B2": "thermal_reliability_classes",
    "C1": "hydro_units",
    "C2": "hydro_hourly",
    "D0": "renewable_capacities",
    "D1": "renewable_hourly",
    "D2": "renewable_plants",
    "E1": "storage_units",
    "G1": "interties",
    "G2": "intertie_hourly",
    "H1": "adequacy_standards",
}

_TECHNOLOGY_RULES = [
    (re.compile(r"燃煤.*超超临界"), "coal_ultra_supercritical"),
    (re.compile(r"燃煤.*热电联产|热电"), "coal_chp"),
    (re.compile(r"燃煤"), "coal_condensing"),
    (re.compile(r"燃气.*联合循环|燃气"), "gas_ccgt"),
    (re.compile(r"核电"), "nuclear"),
    (re.compile(r"生物质"), "biomass"),
]

_RELIABILITY_RULES = [
    (re.compile(r"燃煤.*1000"), "燃煤≥1000MW"),
    (re.compile(r"燃煤.*600"), "燃煤600MW级"),
    (re.compile(r"燃煤.*300"), "燃煤300MW级"),
    (re.compile(r"燃气"), "燃气联合循环"),
]

_RENEWABLE_TECHNOLOGY = {
    "陆上风电": ("WIND_PROVINCE", "onshore_wind"),
    "海上风电": ("WIND_OFFSHORE_PROVINCE", "offshore_wind"),
    "集中式光伏": ("SOLAR_PROVINCE", "solar"),
}

_PLANT_STATUS = {
    "在运": "operating",
    "在建": "construction",
    "规划": "planned",
    "退役": "retired",
}

_RULE_VERSION = "teaching_v0.1"
_LOCAL_TZ = "Asia/Shanghai"

_LOAD_COLUMNS = {"时间戳", "年份", "月份", "日", "小时", "负荷MW"}
_RENEWABLE_HOURLY_COLUMNS = {"时间戳", "风电出力MW", "光伏出力MW"}
_INTERTIE_HOURLY_COLUMNS = {"时间戳", "直流外送通道MW", "交流联络线MW"}
_D0_COLUMNS = {"年份", "风电装机MW", "光伏装机MW"}
_D2_COLUMNS = {"场站ID", "场站名称", "类型", "容量MW", "目标年状态"}
_E1_COLUMNS = {"电站ID", "电站名称", "功率MW", "能量MWh", "往返效率%", "可用率%"}
_G1_COLUMNS = {"通道名称", "类型", "额定容量MW", "正常运行限额MW", "方向"}
_H1_COLUMNS = {"参数代码", "参数名称", "取值", "单位", "口径说明", "来源", "类别"}
_C1_COLUMNS = {"电站名称", "装机容量MW", "调节类型", "设计保证率", "保证出力MW"}
_C2_COLUMNS = {"电站名称", "装机容量MW"}
_B1_COLUMNS = {"机组ID", "电厂名称", "机组编号", "机组类型", "铭牌容量MW"}
_B2_COLUMNS = {"机组分类", "强迫停运率FOR"}

# column names that carry an explicit unit marker
_UNIT_MARKERS = ("MW", "MWh", "%", "h", "FOR", "系数", "率", "量", "限", "标准", "参数", "档位")


class IntervalUnknownError(ValueError):
    """Stable block: source_interval_label unknown or missing."""


def parse_hourly_axis(
    series: pd.Series,
    *,
    timezone: str = _LOCAL_TZ,
    interval_label: str | None = "hour_start",
) -> pd.Series:
    """Parse a naive/aware hourly axis into UTC.

    - naive strings are localized in ``timezone`` (Asia/Shanghai for
      domestic teaching templates), never interpreted as UTC;
    - tz-aware input is converted, never re-localized;
    - ``hour_end`` labels are shifted to the hour start in the source
      timezone;
    - ``unknown``/missing labels are blocked with a stable code.
    """
    if interval_label not in ("hour_start", "hour_end"):
        raise IntervalUnknownError(
            "DQ26_INTERVAL_UNKNOWN: source_interval_label must be hour_start or hour_end"
        )
    stamps = pd.to_datetime(series, errors="coerce")
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize(timezone)
    else:
        stamps = stamps.dt.tz_convert(timezone)
    if interval_label == "hour_end":
        stamps = stamps - pd.Timedelta(hours=1)
    return stamps.dt.tz_convert("UTC")


@dataclass
class TeachingImportResult:
    standard_tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    transformation_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    source_ledger: pd.DataFrame = field(default_factory=pd.DataFrame)
    rejected: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    issues: list[ValidationIssue] = field(default_factory=list)
    name_mismatches: list[str] = field(default_factory=list)
    grade_reasons: list[str] = field(default_factory=list)
    neue_conversions: list[tuple[str, str, float]] = field(default_factory=list)
    recognized_tables: int = 0


class TeachingV0Adapter:
    """Adapter for the 12-table teaching CSV template."""

    @staticmethod
    def identify(files: dict[str, Path]) -> dict[str, Path]:
        """Identify the 12 teaching tables by file-name prefix."""
        identified: dict[str, Path] = {}
        for key in TEACHING_TABLE_NAMES:
            matches = [p for name, p in files.items() if name.startswith(key)]
            if not matches or not matches[0].is_file():
                raise FileNotFoundError(f"DQ01_FILE_MISSING: teaching table {key} not found")
            identified[key] = matches[0]
        return identified

    @staticmethod
    def convert_neue(value: str, unit: str | None) -> float:
        """Convert a legacy NEUE value to ppm; unitless values block."""
        if unit is None or str(unit).strip() == "":
            raise ValueError("DQ33_NEUE_UNIT_MISSING: unit required for NEUE conversion")
        number = float(str(value).replace("%", ""))
        unit_text = str(unit).strip()
        if "%" in unit_text:
            return number * 10000.0
        if unit_text == "ppm":
            return number
        if unit_text in ("比例", "fraction"):
            return number * 1_000_000.0
        raise ValueError(f"DQ33_NEUE_UNIT_UNKNOWN: unsupported unit {unit_text!r}")

    @classmethod
    def adapt(
        cls,
        context: dict[str, Any],
        files: dict[str, Path],
    ) -> TeachingImportResult:
        """Run the full teaching import; never writes to raw inputs."""
        region_id = str(context.get("region_id", "EXAMPLE_PROVINCE"))
        timezone = str(context.get("timezone", _LOCAL_TZ))
        interval_label = context.get("source_interval_label", None)
        template_version = context.get("template_version", None)
        if template_version not in (None, "teaching_v0"):
            raise ValueError(
                f"DQ04_VERSION_UNSUPPORTED: unsupported template version {template_version!r}"
            )

        tables = cls.identify(files)
        registry = RawFileRegistry()
        ledger = SourceLedger()
        transforms = TransformationLog()
        result = TeachingImportResult()
        result.recognized_tables = len(tables)

        produced: set[str] = set()
        for key, path in sorted(tables.items()):
            raw = registry.register(path)
            ledger.add(
                {
                    "source_id": f"SRC_{key}",
                    "source_title": f"teaching template {key}",
                    "provider": "teaching_synthetic_example",
                    "source_type": "teaching_synthetic",
                    "retrieved_at": raw.registered_at,
                    "effective_status": "not_applicable",
                    "definition_note": "synthetic teaching data, not a real province",
                    "confidentiality": "internal",
                    "checksum": raw.sha256,
                    "review_status": "unverified",
                }
            )
            raw_info = {
                "input_file_hash": raw.sha256,
                "input_table": key,
            }

            if detect_encoding(path.read_bytes()) == "unknown":
                result.issues.append(
                    ValidationIssue(
                        IssueCode.ENCODING_UNKNOWN,
                        "error",
                        "cannot decode teaching CSV",
                        table=key,
                    )
                )
                continue
            frame = read_csv(path)

            if key == "A1":
                produced.add("load_hourly")
                cls._adapt_load(
                    frame, region_id, timezone, interval_label, raw_info, transforms, result
                )
            elif key == "B1":
                produced.add("thermal_units")
                cls._adapt_thermal(frame, region_id, raw_info, transforms, ledger, result)
            elif key == "B2":
                produced.add("thermal_reliability_classes")
                cls._adapt_reliability(frame, region_id, raw_info, transforms, result)
            elif key == "C1":
                produced.add("hydro_units")
                cls._adapt_hydro_units(frame, region_id, raw_info, transforms, result)
            elif key == "C2":
                produced.add("hydro_hourly")
                cls._adapt_hydro_hourly(frame, raw_info, transforms, result)
            elif key == "D0":
                produced.add("renewable_capacities")
                cls._adapt_renewable_capacities(frame, region_id, raw_info, transforms, result)
            elif key == "D1":
                produced.add("renewable_hourly")
                cls._adapt_renewable_hourly(
                    frame, region_id, timezone, interval_label, raw_info, transforms, result
                )
            elif key == "D2":
                produced.add("renewable_plants")
                cls._adapt_renewable_plants(frame, region_id, raw_info, transforms, result)
            elif key == "E1":
                produced.add("storage_units")
                cls._adapt_storage(frame, region_id, raw_info, transforms, result)
            elif key == "G1":
                produced.add("interties")
                cls._adapt_interties(frame, region_id, raw_info, transforms, result)
            elif key == "G2":
                produced.add("intertie_hourly")
                cls._adapt_intertie_hourly(
                    frame, timezone, interval_label, raw_info, transforms, result
                )
            elif key == "H1":
                produced.add("adequacy_standards")
                cls._adapt_standards(frame, raw_info, transforms, result)

        # a recognized source table without standard output is an error
        for key, table in TEACHING_TABLE_NAMES.items():
            if key in tables and table not in result.standard_tables:
                result.issues.append(
                    ValidationIssue(
                        IssueCode.SOURCE_TABLE_NO_OUTPUT,
                        "error",
                        f"recognized source table {key} produced no standard output",
                        table=table,
                    )
                )

        result.grade_reasons.append("teaching_synthetic source present -> max G0_teaching")
        result.standard_tables["source_ledger"] = ledger.to_frame()
        result.transformation_log = transforms.to_frame()
        result.source_ledger = ledger.to_frame()
        return result

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _local_year(stamps: pd.Series, timezone: str) -> pd.Series:
        """Weather year in the project-local timezone, never UTC."""
        return pd.Series(stamps.dt.tz_convert(timezone).dt.year, index=stamps.index)

    @staticmethod
    def _missing_columns(
        frame: pd.DataFrame, required: set[str], table: str, result: TeachingImportResult
    ) -> bool:
        missing = sorted(required - set(frame.columns))
        if missing:
            result.issues.append(
                ValidationIssue(
                    IssueCode.MISSING_REQUIRED_COLUMNS,
                    "error",
                    f"missing required columns: {missing}",
                    table=table,
                )
            )
            return True
        return False

    @staticmethod
    def _note_unknown_columns(
        frame: pd.DataFrame,
        known: set[str],
        table: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        for column in frame.columns:
            if column in known:
                continue
            transforms.add(
                {
                    "input_file_hash": raw_info["input_hash"]
                    if "input_hash" in raw_info
                    else raw_info["input_file_hash"],
                    "input_table": raw_info["input_table"],
                    "output_table": table,
                    "rule_id": "unknown_column_preserved",
                    "rule_version": _RULE_VERSION,
                    "field_name": column,
                    "original_value": column,
                    "normalized_value": "kept in raw only",
                    "row_locator": "header",
                    "severity": "warning",
                    "automatic": True,
                }
            )
            result.issues.append(
                ValidationIssue(
                    IssueCode.UNKNOWN_COLUMN_PRESERVED,
                    "warning",
                    f"unknown optional column {column!r} kept in raw only",
                    table=table,
                    locator=column,
                )
            )

    @staticmethod
    def _record_id(*parts: object) -> str:
        return hash_payload({"parts": [str(p) for p in parts]})[:16]

    @staticmethod
    def _log(
        transforms: TransformationLog,
        raw_info: dict[str, str],
        rule_id: str,
        output_table: str,
        field_name: str,
        original_value: object,
        normalized_value: object,
        row_locator: str,
        severity: str = "info",
        automatic: bool = True,
    ) -> None:
        transforms.add(
            {
                "input_file_hash": raw_info["input_file_hash"],
                "input_table": raw_info["input_table"],
                "output_table": output_table,
                "rule_id": rule_id,
                "rule_version": _RULE_VERSION,
                "field_name": field_name,
                "original_value": str(original_value) if original_value is not None else "",
                "normalized_value": str(normalized_value) if normalized_value is not None else "",
                "row_locator": row_locator,
                "severity": severity,
                "automatic": automatic,
            }
        )

    @staticmethod
    def _num(value: object) -> float | None:
        if value is None:
            return None
        text = str(value).strip()
        if text == "" or text == "-999" or text == "-":
            return None
        try:
            return float(text)
        except ValueError:
            return None

    # -- per-table adapters ----------------------------------------------

    @classmethod
    def _adapt_load(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        timezone: str,
        interval_label: str | None,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _LOAD_COLUMNS, "load_hourly", result):
            return
        try:
            stamps = parse_hourly_axis(
                frame["时间戳"], timezone=timezone, interval_label=interval_label
            )
        except IntervalUnknownError as exc:
            result.issues.append(
                ValidationIssue(IssueCode.INTERVAL_UNKNOWN, "error", str(exc), table="load_hourly")
            )
            return
        if interval_label == "hour_end":
            cls._log(
                transforms,
                raw_info,
                "hour_end_to_start",
                "load_hourly",
                "timestamp",
                "hour_end",
                "hour_start",
                "A1:*",
            )
        load = pd.to_numeric(frame["负荷MW"], errors="coerce")
        legacy = load == -999
        load = load.mask(legacy)
        for idx in legacy[legacy].index:
            cls._log(
                transforms,
                raw_info,
                "legacy_missing_to_null",
                "load_hourly",
                "load_mw",
                "-999",
                "null",
                f"A1:{idx}",
                severity="warning",
            )
        year = cls._local_year(stamps, timezone)
        out = pd.DataFrame(
            {
                "record_id": [
                    cls._record_id("load", region_id, f"ACTUAL_{y}", s)
                    for y, s in zip(year, stamps)
                ],
                "region_id": region_id,
                "scenario_id": [f"ACTUAL_{y}" for y in year],
                "weather_year": year,
                "timestamp": stamps,
                "load_mw": load,
                "load_basis": "generation_side_net",
                "is_forecast": False,
                "source_id": f"SRC_{raw_info['input_table']}",
                "data_status": "modelled",
                "quality_flag": "valid",
            }
        )
        out = out.dropna(subset=["timestamp"])
        if not out.empty:
            result.standard_tables["load_hourly"] = out.reset_index(drop=True)

    @classmethod
    def _adapt_thermal(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        ledger: SourceLedger,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _B1_COLUMNS, "thermal_units", result):
            return
        cls._note_unknown_columns(
            frame,
            _B1_COLUMNS
            | {
                "投运年份",
                "计划退役年份",
                "是否供热",
                "供暖期最大出力MW",
                "供暖期最小出力MW",
                "厂用电率%",
                "计划检修月份1",
                "计划检修月份2",
            },
            "thermal_units",
            raw_info,
            transforms,
            result,
        )
        units: list[dict[str, Any]] = []
        outages: list[dict[str, Any]] = []
        mapping: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            unit_id = str(row.get("机组ID", "")).strip()
            if unit_id.startswith("#"):
                ledger.add(
                    {
                        "source_title": "teaching template B1 legacy source line",
                        "provider": str(row.get("电厂名称", "")).strip(),
                        "source_type": "teaching_synthetic",
                        "effective_status": "not_applicable",
                        "definition_note": "trailing #来源 line migrated to ledger",
                        "confidentiality": "internal",
                        "review_status": "unverified",
                    }
                )
                continue
            if not unit_id:
                result.rejected.setdefault("thermal_units", []).append(dict(row))
                continue
            label = str(row.get("机组类型", "")).strip()
            technology = cls._technology_code(label)
            cls._log(
                transforms,
                raw_info,
                "legacy_technology_to_controlled",
                "thermal_units",
                "technology_code",
                label,
                technology,
                f"B1:{idx}",
            )
            capacity = cls._num(row.get("铭牌容量MW"))
            if capacity is None or capacity < 0:
                result.rejected.setdefault("thermal_units", []).append(dict(row))
                continue
            aux_raw = row.get("厂用电率%")
            aux = cls._num(aux_raw)
            if (
                aux is not None
                and aux_raw is not None
                and str(aux_raw).strip() not in ("", "-999", "-")
            ):
                aux = aux / 100.0
                cls._log(
                    transforms,
                    raw_info,
                    "percent_to_fraction",
                    "thermal_units",
                    "auxiliary_rate",
                    aux_raw,
                    aux,
                    f"B1:{idx}",
                )
            year_start = cls._num(row.get("投运年份"))
            year_end = cls._num(row.get("计划退役年份"))
            if year_start:
                cls._log(
                    transforms,
                    raw_info,
                    "year_to_jan1_bound",
                    "thermal_units",
                    "commission_date",
                    row.get("投运年份"),
                    f"{int(year_start)}-01-01",
                    f"B1:{idx}",
                )
            units.append(
                {
                    "record_id": cls._record_id("unit", unit_id),
                    "unit_id": unit_id,
                    "plant_id": f"PLANT_{cls._record_id('plant', str(row.get('电厂名称', '')))}",
                    "plant_name": str(row.get("电厂名称", "")).strip(),
                    "unit_name": str(row.get("机组编号", "")).strip(),
                    "technology_code": technology,
                    "technology_label_raw": label,
                    "capacity_mw": capacity,
                    "capacity_basis": "gross_nameplate" if aux is not None else "net_dependable",
                    "auxiliary_rate": aux,
                    "commission_date": f"{int(year_start)}-01-01" if year_start else None,
                    "retirement_date": f"{int(year_end)}-12-31" if year_end else None,
                    "heat_supply": str(row.get("是否供热", "")).strip() == "是",
                    "heating_season_max_mw": cls._num(row.get("供暖期最大出力MW")),
                    "heating_season_min_mw": cls._num(row.get("供暖期最小出力MW")),
                    "region_id": region_id,
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
            for month_col in ("计划检修月份1", "计划检修月份2"):
                month = row.get(month_col)
                if month is not None and str(month).strip() not in ("", "-", "-999"):
                    try:
                        m = int(str(month).strip())
                    except ValueError:
                        continue
                    cls._log(
                        transforms,
                        raw_info,
                        "maintenance_month_to_modelled_event",
                        "planned_outages",
                        "start_timestamp",
                        month,
                        f"2025-{m:02d}-01",
                        f"B1:{idx}",
                        severity="warning",
                    )
                    outages.append(
                        {
                            "record_id": cls._record_id("outage", unit_id, month_col),
                            "resource_id": unit_id,
                            "outage_id": f"OUT_{cls._record_id(unit_id, month_col)}",
                            "start_timestamp": f"2025-{m:02d}-01T00:00:00+08:00",
                            "end_timestamp": f"2025-{m:02d}-28T00:00:00+08:00",
                            "available_fraction": 0.0,
                            "status": "modelled",
                            "source_id": f"SRC_{raw_info['input_table']}",
                        }
                    )
            class_id = cls._reliability_class_candidate(label, capacity)
            if class_id is None:
                result.issues.append(
                    ValidationIssue(
                        IssueCode.RELIABILITY_MAP_MISSING,
                        "error",
                        f"no reliability class candidate for unit {unit_id}; "
                        "mapping missing blocks formal runs",
                        table="thermal_unit_reliability_map",
                        locator=unit_id,
                    )
                )
            else:
                mapping.append(
                    {
                        "record_id": cls._record_id("map", unit_id),
                        "unit_id": unit_id,
                        "reliability_class_id": class_id,
                        "mapping_method": "rule",
                        "confidence": "low",
                        "reviewed_by": None,
                        "source_id": f"SRC_{raw_info['input_table']}",
                    }
                )
        if units:
            result.standard_tables["thermal_units"] = pd.DataFrame(units)
        if outages:
            result.standard_tables["planned_outages"] = pd.DataFrame(outages)
        if mapping:
            result.standard_tables["thermal_unit_reliability_map"] = pd.DataFrame(mapping)

    @staticmethod
    def _technology_code(label: str) -> str:
        for pattern, code in _TECHNOLOGY_RULES:
            if pattern.search(label):
                return code
        return "other_thermal"

    @staticmethod
    def _reliability_class_candidate(label: str, capacity: float | None) -> str | None:
        for pattern, class_name in _RELIABILITY_RULES:
            if pattern.search(label):
                return f"CLASS_{TeachingV0Adapter._record_id('class', class_name)}"
        if "燃煤" in label and capacity is not None:
            if capacity >= 1000:
                class_name = "燃煤≥1000MW"
            elif capacity >= 500:
                class_name = "燃煤600MW级"
            else:
                class_name = "燃煤300MW级"
            return f"CLASS_{TeachingV0Adapter._record_id('class', class_name)}"
        return None

    @classmethod
    def _adapt_reliability(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _B2_COLUMNS, "thermal_reliability_classes", result):
            return
        cls._note_unknown_columns(
            frame,
            _B2_COLUMNS
            | {"等效可用系数", "降额50%状态概率", "计划停运率", "样本机组数", "数据来源"},
            "thermal_reliability_classes",
            raw_info,
            transforms,
            result,
        )
        rows: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            class_name = str(row.get("机组分类", "")).strip()
            if not class_name:
                continue
            for_rate = cls._num(row.get("强迫停运率FOR"))
            if for_rate is not None and for_rate > 1:
                for_rate = for_rate / 100.0
                cls._log(
                    transforms,
                    raw_info,
                    "percent_to_fraction",
                    "thermal_reliability_classes",
                    "for_rate",
                    row.get("强迫停运率FOR"),
                    for_rate,
                    f"B2:{idx}",
                )
            rows.append(
                {
                    "record_id": cls._record_id("class", class_name),
                    "reliability_class_id": f"CLASS_{cls._record_id('class', class_name)}",
                    "class_name": class_name,
                    "model_type": "for_only",
                    "authoritative_parameter_set": "for_only",
                    "for_rate": for_rate,
                    "equivalent_availability_factor": cls._num(row.get("等效可用系数")),
                    "partial_derate_probability": cls._num(row.get("降额50%状态概率")),
                    "planned_outage_rate": cls._num(row.get("计划停运率")),
                    "sample_unit_count": cls._num(row.get("样本机组数")),
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
            cls._log(
                transforms,
                raw_info,
                "for_only_model_noted",
                "thermal_reliability_classes",
                "model_type",
                "FOR only",
                "for_only",
                f"B2:{idx}",
                severity="warning",
            )
        if rows:
            result.standard_tables["thermal_reliability_classes"] = pd.DataFrame(rows)

    @classmethod
    def _adapt_hydro_units(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _C1_COLUMNS, "hydro_units", result):
            return
        cls._note_unknown_columns(
            frame, _C1_COLUMNS | {"丰水期月份", "备注"}, "hydro_units", raw_info, transforms, result
        )
        rows: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            name = str(row.get("电站名称", "")).strip()
            if not name:
                continue
            assurance = cls._assurance_fraction(frame, row, raw_info, transforms, idx)
            regulation = str(row.get("调节类型", "")).strip()
            regulation_code = {
                "季调节": "seasonal",
                "日调节": "daily",
                "径流式": "run_of_river",
            }.get(regulation, "seasonal")
            rows.append(
                {
                    "record_id": cls._record_id("hydro", name),
                    "hydro_id": f"HYDRO_{cls._record_id('hydro', name)}",
                    "hydro_name": name,
                    "capacity_mw": cls._num(row.get("装机容量MW")),
                    "regulation_type": regulation_code,
                    "firm_output_mw": cls._num(row.get("保证出力MW")),
                    "design_assurance_fraction": assurance,
                    "region_id": region_id,
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
        if rows:
            result.standard_tables["hydro_units"] = pd.DataFrame(rows)

    @classmethod
    def _assurance_fraction(
        cls,
        frame: pd.DataFrame,
        row: pd.Series,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        idx: object,
    ) -> float | None:
        """Design assurance: unit decided by the column name, not the value."""
        column = "设计保证率%" if "设计保证率%" in frame.columns else "设计保证率"
        value = cls._num(row.get(column))
        if value is None:
            return None
        if "%" in column:
            value = value / 100.0
            cls._log(
                transforms,
                raw_info,
                "percent_to_fraction",
                "hydro_units",
                "design_assurance_fraction",
                row.get(column),
                value,
                f"C1:{idx}",
            )
        elif value > 1:
            cls._log(
                transforms,
                raw_info,
                "assurance_fraction_over_1",
                "hydro_units",
                "design_assurance_fraction",
                row.get(column),
                value,
                f"C1:{idx}",
                severity="warning",
            )
        return value

    @classmethod
    def _adapt_hydro_hourly(
        cls,
        frame: pd.DataFrame,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _C2_COLUMNS, "hydro_hourly", result):
            return
        monthly_cols = [f"{m}月出力系数" for m in range(1, 13)]
        missing_monthly = [c for c in monthly_cols if c not in frame.columns]
        if missing_monthly:
            result.issues.append(
                ValidationIssue(
                    IssueCode.MISSING_REQUIRED_COLUMNS,
                    "error",
                    f"missing monthly coefficient columns: {missing_monthly}",
                    table="hydro_hourly",
                )
            )
            return
        rows: list[dict[str, Any]] = []
        hydro_names: set[str] = set()
        if "hydro_units" in result.standard_tables:
            hydro_names = set(result.standard_tables["hydro_units"]["hydro_name"])
        for idx, row in frame.iterrows():
            name = str(row.get("电站名称", "")).strip()
            match = cls._hydro_name_match(name, hydro_names)
            if match is None:
                result.name_mismatches.append(
                    f"hydro name mismatch: C2 {name!r} has no C1 candidate; "
                    "manual mapping required, row blocked"
                )
                result.rejected.setdefault("hydro_hourly", []).append(dict(row))
                result.issues.append(
                    ValidationIssue(
                        IssueCode.NAME_MISMATCH,
                        "error",
                        f"C2 hydro {name!r} cannot be matched to C1; blocked",
                        table="hydro_hourly",
                        locator=name,
                    )
                )
                continue
            if match != name:
                result.name_mismatches.append(
                    f"hydro name mismatch: C2 {name!r} matched C1 candidate {match!r} "
                    "(needs manual review)"
                )
                cls._log(
                    transforms,
                    raw_info,
                    "hydro_name_match_candidate",
                    "hydro_hourly",
                    "hydro_id",
                    name,
                    match,
                    f"C2:{idx}",
                    severity="warning",
                    automatic=False,
                )
            capacity = cls._num(row.get("装机容量MW"))
            if capacity is None:
                capacity = 1.0
            for month in range(1, 13):
                coeff = cls._num(row.get(f"{month}月出力系数"))
                if coeff is None:
                    continue
                fixed = coeff * capacity
                rows.append(
                    {
                        "record_id": cls._record_id("hydro_hourly", match, month),
                        "hydro_id": f"HYDRO_{cls._record_id('hydro', match)}",
                        "scenario_id": "BASE",
                        "timestamp": f"2025-{month:02d}-01T00:00:00+08:00",
                        "fixed_output_mw": fixed,
                        "max_output_mw": capacity,
                        "source_id": f"SRC_{raw_info['input_table']}",
                    }
                )
            cls._log(
                transforms,
                raw_info,
                "monthly_coefficient_to_fixed_profile",
                "hydro_hourly",
                "fixed_output_mw",
                "coefficients",
                "monthly fixed profile",
                f"C2:{idx}",
                severity="warning",
            )
        if rows:
            result.standard_tables["hydro_hourly"] = pd.DataFrame(rows)

    @staticmethod
    def _hydro_name_match(name: str, hydro_names: set[str]) -> str | None:
        """Candidate hydro name matching: exact first, then a single
        ``水``-normalized candidate; never guesses across multiple."""
        if name in hydro_names:
            return name
        normalized = name.replace("水", "")
        candidates = [n for n in hydro_names if n.replace("水", "") == normalized]
        if len(candidates) == 1:
            return candidates[0]
        return None

    @classmethod
    def _adapt_renewable_capacities(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _D0_COLUMNS, "renewable_capacities", result):
            return
        rows: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            year = cls._num(row.get("年份"))
            if year is None:
                continue
            for column, (group, technology) in {
                "风电装机MW": ("WIND_PROVINCE", "onshore_wind"),
                "光伏装机MW": ("SOLAR_PROVINCE", "solar"),
            }.items():
                value = cls._num(row.get(column))
                if value is None:
                    continue
                rows.append(
                    {
                        "record_id": cls._record_id("cap", group, year),
                        "resource_group_id": group,
                        "technology": technology,
                        "region_id": region_id,
                        "year": int(year),
                        "installed_capacity_mw": value,
                        "capacity_basis": "operational",
                        "source_id": f"SRC_{raw_info['input_table']}",
                    }
                )
        cls._log(
            transforms,
            raw_info,
            "wide_to_long",
            "renewable_capacities",
            "resource_group_id",
            "wind/solar columns",
            "WIND_PROVINCE/SOLAR_PROVINCE",
            "D0:*",
        )
        if rows:
            result.standard_tables["renewable_capacities"] = pd.DataFrame(rows)

    @classmethod
    def _adapt_renewable_hourly(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        timezone: str,
        interval_label: str | None,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _RENEWABLE_HOURLY_COLUMNS, "renewable_hourly", result):
            return
        cls._note_unknown_columns(
            frame, _RENEWABLE_HOURLY_COLUMNS, "renewable_hourly", raw_info, transforms, result
        )
        try:
            stamps = parse_hourly_axis(
                frame["时间戳"], timezone=timezone, interval_label=interval_label
            )
        except IntervalUnknownError as exc:
            result.issues.append(
                ValidationIssue(
                    IssueCode.INTERVAL_UNKNOWN, "error", str(exc), table="renewable_hourly"
                )
            )
            return
        year = cls._local_year(stamps, timezone)
        long_frames: list[pd.DataFrame] = []
        for column, group in {
            "风电出力MW": "WIND_PROVINCE",
            "光伏出力MW": "SOLAR_PROVINCE",
        }.items():
            values = pd.to_numeric(frame[column], errors="coerce")
            out = pd.DataFrame(
                {
                    "record_id": [
                        cls._record_id("ren", group, f"ACTUAL_{y}", s) for y, s in zip(year, stamps)
                    ],
                    "resource_group_id": group,
                    "scenario_id": [f"ACTUAL_{y}" for y in year],
                    "weather_year": year,
                    "timestamp": stamps,
                    "available_generation_mw": values,
                    "actual_generation_mw": None,
                    "generation_basis": "available",
                    "installed_capacity_mw": None,
                    "curtailment_mw": None,
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
            out = out.dropna(subset=["timestamp", "available_generation_mw"])
            if not out.empty:
                # resource-run column consistency at the STANDARD input
                # boundary: fill installed_capacity_mw from the frozen
                # renewable_capacities table (same group + year), never
                # inside EFC / resource-run
                capacities = result.standard_tables.get("renewable_capacities")
                if capacities is not None and not capacities.empty:
                    by_group_year = {
                        (str(row["resource_group_id"]), int(row["year"])): float(
                            row["installed_capacity_mw"]
                        )
                        for _, row in capacities.iterrows()
                    }
                    installed = [by_group_year.get((group, int(y))) for y in out["weather_year"]]
                    out["installed_capacity_mw"] = installed
                long_frames.append(out)
        cls._log(
            transforms,
            raw_info,
            "wide_to_long",
            "renewable_hourly",
            "resource_group_id",
            "wind/solar columns",
            "WIND_PROVINCE/SOLAR_PROVINCE",
            "D1:*",
        )
        cls._log(
            transforms,
            raw_info,
            "generation_basis_selected_available",
            "renewable_hourly",
            "generation_basis",
            "wind/solar output",
            "available",
            "D1:*",
            severity="warning",
        )
        if long_frames:
            result.standard_tables["renewable_hourly"] = pd.concat(long_frames, ignore_index=True)

    @classmethod
    def _adapt_renewable_plants(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _D2_COLUMNS, "renewable_plants", result):
            return
        cls._note_unknown_columns(
            frame,
            _D2_COLUMNS | {"所在市", "纬度", "经度", "投产年份", "区域分组"},
            "renewable_plants",
            raw_info,
            transforms,
            result,
        )
        rows: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            plant_id = str(row.get("场站ID", "")).strip()
            name = str(row.get("场站名称", "")).strip()
            if not plant_id or not name:
                result.rejected.setdefault("renewable_plants", []).append(dict(row))
                continue
            tech_label = str(row.get("类型", "")).strip()
            mapping = _RENEWABLE_TECHNOLOGY.get(tech_label)
            if mapping is None:
                result.rejected.setdefault("renewable_plants", []).append(dict(row))
                result.issues.append(
                    ValidationIssue(
                        IssueCode.UNKNOWN_PLANT_TYPE,
                        "warning",
                        f"unknown plant type {tech_label!r} requires review",
                        table="renewable_plants",
                        locator=plant_id,
                    )
                )
                continue
            status = _PLANT_STATUS.get(str(row.get("目标年状态", "")).strip())
            if status is None:
                result.rejected.setdefault("renewable_plants", []).append(dict(row))
                continue
            year = cls._num(row.get("投产年份"))
            rows.append(
                {
                    "record_id": cls._record_id("plant", plant_id),
                    "plant_id": plant_id,
                    "plant_name": name,
                    "technology": mapping[1],
                    "capacity_mw": cls._num(row.get("容量MW")),
                    "region_id": region_id,
                    "city_name": str(row.get("所在市", "")).strip() or None,
                    "latitude": cls._num(row.get("纬度")),
                    "longitude": cls._num(row.get("经度")),
                    "commission_date": f"{int(year)}-01-01" if year else None,
                    "target_status": status,
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
        if rows:
            result.standard_tables["renewable_plants"] = pd.DataFrame(rows)

    @classmethod
    def _adapt_storage(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _E1_COLUMNS, "storage_units", result):
            return
        cls._note_unknown_columns(
            frame,
            _E1_COLUMNS | {"类型", "时长h", "技术路线", "投运年份", "并网点", "所在市", "备注"},
            "storage_units",
            raw_info,
            transforms,
            result,
        )
        rows: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            storage_id = str(row.get("电站ID", "")).strip()
            name = str(row.get("电站名称", "")).strip()
            if not storage_id or not name:
                result.rejected.setdefault("storage_units", []).append(dict(row))
                continue
            tech_label = str(row.get("技术路线", "")).strip()
            type_label = str(row.get("类型", "")).strip()
            if "抽蓄" in tech_label or "抽水蓄能" in type_label:
                storage_type = "pumped_hydro"
            elif "新型储能" in type_label or tech_label in ("磷酸铁锂", "锂电"):
                storage_type = "battery"
            else:
                result.rejected.setdefault("storage_units", []).append(dict(row))
                result.issues.append(
                    ValidationIssue(
                        IssueCode.UNKNOWN_STORAGE_TYPE,
                        "warning",
                        f"unknown storage type {type_label!r}/{tech_label!r} requires review",
                        table="storage_units",
                        locator=storage_id,
                    )
                )
                continue
            power = cls._num(row.get("功率MW"))
            energy = cls._num(row.get("能量MWh"))
            rte_raw = row.get("往返效率%")
            rte = cls._num(rte_raw)
            if (
                rte is not None
                and rte_raw is not None
                and str(rte_raw).strip() not in ("", "-999", "-")
            ):
                rte = rte / 100.0
                cls._log(
                    transforms,
                    raw_info,
                    "percent_to_fraction",
                    "storage_units",
                    "round_trip_efficiency",
                    rte_raw,
                    rte,
                    f"E1:{idx}",
                )
                cls._log(
                    transforms,
                    raw_info,
                    "efficiency_split_assumed",
                    "storage_units",
                    "charge_efficiency",
                    rte_raw,
                    "sqrt(rte)",
                    f"E1:{idx}",
                    severity="warning",
                )
            avail_raw = row.get("可用率%")
            avail = cls._num(avail_raw)
            if (
                avail is not None
                and avail_raw is not None
                and str(avail_raw).strip() not in ("", "-999", "-")
            ):
                avail = avail / 100.0
            duration = cls._num(row.get("时长h"))
            if duration is not None and power and energy:
                computed = energy / power
                if abs(computed - duration) / duration > 0.05:
                    result.issues.append(
                        ValidationIssue(
                            IssueCode.STORAGE_DURATION_MISMATCH,
                            "warning",
                            f"storage {storage_id} duration {duration}h vs "
                            f"energy/power {computed:.2f}h",
                            table="storage_units",
                            locator=storage_id,
                        )
                    )
            cls._log(
                transforms,
                raw_info,
                "soc_bounds_defaulted_teaching",
                "storage_units",
                "soc_min_fraction",
                "missing",
                "0.0",
                f"E1:{idx}",
                severity="warning",
            )
            cls._log(
                transforms,
                raw_info,
                "soc_bounds_defaulted_teaching",
                "storage_units",
                "soc_max_fraction",
                "missing",
                "1.0",
                f"E1:{idx}",
                severity="warning",
            )
            rows.append(
                {
                    "record_id": cls._record_id("storage", storage_id),
                    "storage_id": storage_id,
                    "storage_name": name,
                    "storage_type": storage_type,
                    "charge_power_mw": power,
                    "discharge_power_mw": power,
                    "energy_capacity_mwh": energy,
                    "round_trip_efficiency": rte,
                    "soc_min_fraction": 0.0,
                    "soc_max_fraction": 1.0,
                    "self_discharge_per_hour": 0.0,
                    "availability_model": "fixed",
                    "availability_fraction": avail,
                    "grid_connection_limit_mw": None,
                    "region_id": region_id,
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
        if rows:
            result.standard_tables["storage_units"] = pd.DataFrame(rows)

    @classmethod
    def _adapt_interties(
        cls,
        frame: pd.DataFrame,
        region_id: str,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _G1_COLUMNS, "interties", result):
            return
        cls._note_unknown_columns(
            frame,
            _G1_COLUMNS | {"对侧区域", "电压等级", "计划检修月份", "备注"},
            "interties",
            raw_info,
            transforms,
            result,
        )
        rows: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            name = str(row.get("通道名称", "")).strip()
            if not name:
                continue
            direction = str(row.get("方向", "")).strip()
            limit = cls._num(row.get("正常运行限额MW"))
            if direction == "送出":
                export_limit: float | None = limit
                import_limit: float | None = 0.0
            elif direction == "受入":
                export_limit = 0.0
                import_limit = limit
            else:
                result.issues.append(
                    ValidationIssue(
                        IssueCode.INTERTIE_DIRECTION_UNKNOWN,
                        "error",
                        f"intertie {name!r} direction {direction!r} unknown",
                        table="interties",
                        locator=name,
                    )
                )
                continue
            cls._log(
                transforms,
                raw_info,
                "single_limit_split_by_direction",
                "interties",
                "normal_export_limit_mw",
                limit,
                f"export={export_limit} import={import_limit}",
                f"G1:{idx}",
            )
            avail = 1.0  # availability declared per channel where available
            rows.append(
                {
                    "record_id": cls._record_id("tie", name),
                    "intertie_id": f"INTERTIE_{cls._record_id('tie', name)}",
                    "intertie_name": name,
                    "intertie_type": str(row.get("类型", "")).strip(),
                    "rated_capacity_mw": cls._num(row.get("额定容量MW")),
                    "normal_import_limit_mw": import_limit,
                    "normal_export_limit_mw": export_limit,
                    "availability_model": "fixed",
                    "availability_fraction": avail,
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
        if rows:
            result.standard_tables["interties"] = pd.DataFrame(rows)

    @classmethod
    def _tie_mapping_candidate(
        cls,
        column: str,
        ties: pd.DataFrame,
        sign: str,
    ) -> str | None:
        """Map a G2 channel column to a G1 stable intertie id.

        Exact name match first; otherwise type + direction rule
        candidates. ``None`` means the mapping is not confirmable and
        the column must be blocked (no orphan ids are generated).
        """

        def supports_direction(tie: pd.Series) -> bool:
            import_limit = cls._num(tie.get("normal_import_limit_mw"))
            export_limit = cls._num(tie.get("normal_export_limit_mw"))
            if sign == "positive":
                return export_limit is not None and export_limit > 0.0
            if sign == "negative":
                return import_limit is not None and import_limit > 0.0
            return False

        bare = column.replace("MW", "").strip()
        exact = ties[ties["intertie_name"] == bare]
        if len(exact) == 1 and supports_direction(exact.iloc[0]):
            return str(exact.iloc[0]["intertie_id"])
        candidates: list[str] = []
        for _, tie in ties.iterrows():
            tie_type = str(tie["intertie_type"])
            direction_match = supports_direction(tie)
            if (
                ("直流" in column and tie_type == "直流")
                or ("交流" in column and tie_type == "交流")
            ) and direction_match:
                candidates.append(str(tie["intertie_id"]))
        if len(candidates) == 1:
            return candidates[0]
        return None

    @classmethod
    def _adapt_intertie_hourly(
        cls,
        frame: pd.DataFrame,
        timezone: str,
        interval_label: str | None,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _INTERTIE_HOURLY_COLUMNS, "intertie_hourly", result):
            return
        ties = result.standard_tables.get("interties")
        ties_df = (
            ties
            if ties is not None
            else pd.DataFrame(columns=["intertie_id", "intertie_name", "intertie_type"])
        )
        net_column = None
        channel_columns: dict[str, str] = {}
        for column in frame.columns:
            if column == "时间戳":
                continue
            if column.startswith("净交换"):
                net_column = column
                continue
            if column in ("直流外送通道MW", "交流联络线MW"):
                channel_columns[column] = column
                continue
            result.issues.append(
                ValidationIssue(
                    IssueCode.UNMAPPABLE_TIE_COLUMNS,
                    "error",
                    f"intertie column {column!r} cannot be mapped",
                    table="intertie_hourly",
                    locator=column,
                )
            )
            result.rejected.setdefault("unmapped_tie_columns", []).append({"source_column": column})
        try:
            stamps = parse_hourly_axis(
                frame["时间戳"], timezone=timezone, interval_label=interval_label
            )
        except IntervalUnknownError as exc:
            result.issues.append(
                ValidationIssue(
                    IssueCode.INTERVAL_UNKNOWN, "error", str(exc), table="intertie_hourly"
                )
            )
            return
        year = cls._local_year(stamps, timezone)
        long_frames: list[pd.DataFrame] = []
        channel_sum: pd.Series | None = None
        for column in channel_columns:
            values = pd.to_numeric(frame[column], errors="coerce")
            channel_sum = values if channel_sum is None else channel_sum.add(values, fill_value=0.0)
            sign = "positive" if values.dropna().mean() >= 0 else "negative"
            tie_id = cls._tie_mapping_candidate(column, ties_df, sign)
            if tie_id is None:
                result.issues.append(
                    ValidationIssue(
                        IssueCode.UNMAPPABLE_TIE_COLUMNS,
                        "error",
                        f"G2 column {column!r} cannot be confirmed against G1; "
                        "manual mapping required, column blocked",
                        table="intertie_hourly",
                        locator=column,
                    )
                )
                result.rejected.setdefault("unmapped_tie_columns", []).append(
                    {"source_column": column}
                )
                continue
            cls._log(
                transforms,
                raw_info,
                "tie_column_mapped_via_rule",
                "intertie_hourly",
                "intertie_id",
                column,
                tie_id,
                "G2:*",
                severity="warning",
                automatic=False,
            )
            export = values.clip(lower=0.0)
            import_mw = (-values).clip(lower=0.0)
            out = pd.DataFrame(
                {
                    "record_id": [
                        cls._record_id("tie_h", tie_id, f"ACTUAL_{y}", s)
                        for y, s in zip(year, stamps)
                    ],
                    "intertie_id": tie_id,
                    "scenario_id": [f"ACTUAL_{y}" for y in year],
                    "timestamp": stamps,
                    "scheduled_import_mw": import_mw,
                    "scheduled_export_mw": export,
                    "source_id": f"SRC_{raw_info['input_table']}",
                }
            )
            out = out.dropna(subset=["timestamp", "scheduled_export_mw"])
            if not out.empty:
                long_frames.append(out)
        if net_column is not None:
            net = pd.to_numeric(frame[net_column], errors="coerce")
            if channel_sum is not None:
                diff = (net - channel_sum).abs()
                if diff.max() <= 0.5:
                    cls._log(
                        transforms,
                        raw_info,
                        "net_exchange_conservation_ok",
                        "intertie_hourly",
                        net_column,
                        "net signed column",
                        "consistent with channel sum",
                        "G2:*",
                    )
                else:
                    result.issues.append(
                        ValidationIssue(
                            IssueCode.INTERTIE_NET_MISMATCH,
                            "warning",
                            f"net exchange check column {net_column!r} inconsistent "
                            "with channel sums",
                            table="intertie_hourly",
                            locator=net_column,
                        )
                    )
        cls._log(
            transforms,
            raw_info,
            "wide_to_long_split_sign",
            "intertie_hourly",
            "scheduled_import_mw/scheduled_export_mw",
            "net signed column",
            "non-negative import/export",
            "G2:*",
        )
        if long_frames:
            result.standard_tables["intertie_hourly"] = pd.concat(long_frames, ignore_index=True)

    @classmethod
    def _adapt_standards(
        cls,
        frame: pd.DataFrame,
        raw_info: dict[str, str],
        transforms: TransformationLog,
        result: TeachingImportResult,
    ) -> None:
        if cls._missing_columns(frame, _H1_COLUMNS, "adequacy_standards", result):
            return
        rows: list[dict[str, Any]] = []
        for idx, row in frame.iterrows():
            code = str(row.get("参数代码", "")).strip()
            value = row.get("取值")
            unit = str(row.get("单位", "")).strip()
            note = str(row.get("口径说明", "")).strip()
            category = str(row.get("类别", "")).strip()
            if not code or not category:
                result.rejected.setdefault("adequacy_standards", []).append(dict(row))
                continue
            if category == "可靠性标准":
                number = cls._num(value)
                if number is None or "/" in str(value):
                    # multi-tier or non-numeric: separate, never fabricate
                    cls._log(
                        transforms,
                        raw_info,
                        "algorithm_parameter_separated",
                        "run_config",
                        code,
                        value,
                        "multi-tier/non-numeric -> run parameters",
                        f"H1:{idx}",
                        severity="warning",
                    )
                    continue
                metric = cls._standard_metric(code, unit)
                if metric is None:
                    cls._log(
                        transforms,
                        raw_info,
                        "standard_metric_unmapped",
                        "adequacy_standards",
                        "metric",
                        code,
                        "unmapped",
                        f"H1:{idx}",
                        severity="warning",
                    )
                    result.issues.append(
                        ValidationIssue(
                            IssueCode.STANDARD_METRIC_UNMAPPED,
                            "warning",
                            f"standard {code!r} metric cannot be mapped",
                            table="adequacy_standards",
                            locator=code,
                        )
                    )
                    continue
                converted = number
                if metric == "neue_ppm":
                    converted = cls.convert_neue(str(number), unit or None)
                    result.neue_conversions.append(("DQ33_NEUE_CONVERTED", str(value), converted))
                    cls._log(
                        transforms,
                        raw_info,
                        "neue_unit_converted",
                        "adequacy_standards",
                        "value",
                        value,
                        converted,
                        f"H1:{idx}",
                    )
                rows.append(
                    {
                        "record_id": cls._record_id("std", code),
                        "standard_id": f"STD_{code}",
                        "metric": metric,
                        "value": converted,
                        "unit": "ppm" if metric == "neue_ppm" else unit,
                        "jurisdiction": "teaching",
                        "status": "research_scenario",
                        "effective_from": None,
                        "effective_to": None,
                        "source_id": f"SRC_{raw_info['input_table']}",
                        "note": note,
                    }
                )
                cls._log(
                    transforms,
                    raw_info,
                    "legacy_enum_to_controlled",
                    "adequacy_standards",
                    "metric",
                    code,
                    metric,
                    f"H1:{idx}",
                )
            else:
                cls._log(
                    transforms,
                    raw_info,
                    "algorithm_parameter_separated",
                    "run_config",
                    code,
                    f"{value} {unit}".strip(),
                    "kept out of data tables",
                    f"H1:{idx}",
                    severity="warning" if category == "现行政策" else "info",
                )
        if rows:
            result.standard_tables["adequacy_standards"] = pd.DataFrame(rows)

    @staticmethod
    def _standard_metric(code: str, unit: str) -> str | None:
        if "LOLE" in code:
            return "lole_days_per_year"
        if "LOLH" in code:
            return "lolh_hours_per_year"
        if "EUE" in code:
            return "eue_mwh_per_year" if "用电量" not in unit else "neue_ppm"
        if "NEUE" in code:
            return "neue_ppm"
        return None


__all__ = [
    "TEACHING_TABLE_NAMES",
    "IntervalUnknownError",
    "TeachingImportResult",
    "TeachingV0Adapter",
    "parse_hourly_axis",
]
