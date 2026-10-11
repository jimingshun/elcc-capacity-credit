"""Standard-layer validation with stable issue codes.

Validation runs in the documented order (data contract V0.3 §25.1):
file presence -> fields -> single table -> foreign keys -> time axis ->
units -> physics -> data grade. Errors and warnings are separate;
errors use stable issue codes (design-document codes such as
``DATA_TIME_DUPLICATE`` are reused verbatim; DQ-prefixed codes map to
validation plan DQ-xx ids). Automatic validation may only conclude
``passed``/``failed`` (validated/blocked); it never approves or freezes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, cast

import pandas as pd

from capacity_credit.data.grading import compute_max_data_grade


class IssueCode:
    """Stable issue codes (design codes + DQ-mapped codes)."""

    FILE_MISSING = "DQ01_FILE_MISSING"
    FILE_EMPTY = "DQ01_FILE_EMPTY"
    ENCODING_UNKNOWN = "DQ02_ENCODING_UNKNOWN"
    MISSING_REQUIRED_COLUMNS = "DQ03_MISSING_REQUIRED_COLUMNS"
    VERSION_UNSUPPORTED = "DQ04_VERSION_UNSUPPORTED"
    UNKNOWN_COLUMN_PRESERVED = "DQ05_UNKNOWN_COLUMN_PRESERVED"
    DUPLICATE_PRIMARY_KEY = "DQ07_DUPLICATE_PRIMARY_KEY"
    DATA_ID_ORPHAN = "DATA_ID_ORPHAN"
    NAME_MISMATCH = "DQ09_NAME_MISMATCH"
    UNMAPPABLE_TIE_COLUMNS = "DQ10_UNMAPPABLE_TIE_COLUMNS"
    NEGATIVE_POWER = "DQ11_NEGATIVE_POWER"
    EFFICIENCY_OUT_OF_RANGE = "DQ12_EFFICIENCY_OUT_OF_RANGE"
    STORAGE_SOC_INVALID = "STORAGE_SOC_INVALID"
    STORAGE_SOC_FRACTION_RANGE = "DQ24_SOC_FRACTION_RANGE"
    STORAGE_ENERGY_CAPACITY_INVALID = "DQ24_ENERGY_CAPACITY_INVALID"
    STORAGE_USABLE_ENERGY_MISMATCH = "DQ24_USABLE_ENERGY_MISMATCH"
    PERCENT_NOT_CONVERTED = "DQ14_PERCENT_NOT_CONVERTED"
    UNIT_MISSING = "DQ15_UNIT_MISSING"
    LEGACY_MISSING_IN_STANDARD = "DQ16_LEGACY_MISSING_IN_STANDARD"
    TIME_MISALIGNED = "DQ19_TIME_MISALIGNED"
    DATA_TIME_DUPLICATE = "DATA_TIME_DUPLICATE"
    DATA_TIME_GAP = "DATA_TIME_GAP"
    GRADE_UNDETERMINED = "DQ20_GRADE_UNDETERMINED"
    WEIGHT_INVALID = "DQ21_WEIGHT_INVALID"
    GENERATION_BASIS_MIXED = "DQ22_GENERATION_BASIS_MIXED"
    AUTHORITY_COLUMN_GAP = "DQ23_AUTHORITY_COLUMN_GAP"
    FOR_MTTF_MTTR_INCONSISTENT = "DQ25_FOR_MTTF_MTTR_INCONSISTENT"
    REJECTED_ESSENTIAL = "DQ28_REJECTED_ESSENTIAL"
    GRADE_ABOVE_MAX = "DQ29_GRADE_ABOVE_MAX"
    LEGACY_ENUM_IN_STANDARD = "DQ32_LEGACY_ENUM_IN_STANDARD"
    DQ27_DOUBLE_COUNT = "DQ27_DOUBLE_COUNT"
    DQ27_RATE_CONSISTENCY_ONLY = "DQ27_RATE_CONSISTENCY_ONLY"
    DQ27_INVALID_RATE = "DQ27_INVALID_RATE"
    DQ27_INVALID_FLAG = "DQ27_INVALID_FLAG"
    DQ27_ORPHAN_OUTAGE = "DQ27_ORPHAN_OUTAGE"
    DQ27_INVALID_RESOURCE_ID = "DQ27_INVALID_RESOURCE_ID"
    DQ30_MISSING_ALIGNMENT = "DQ30_MISSING_ALIGNMENT"
    DQ30_INVALID_ALIGNMENT = "DQ30_INVALID_ALIGNMENT"
    DQ30_INVALID_LEAP_RULE = "DQ30_INVALID_LEAP_RULE"
    DQ30_DUPLICATE_MAPPING = "DQ30_DUPLICATE_MAPPING"
    DQ30_INVALID_WEATHER_YEAR = "DQ30_INVALID_WEATHER_YEAR"
    DQ30_INVALID_TARGET_YEAR = "DQ30_INVALID_TARGET_YEAR"
    DQ30_MISSING_SCOPE = "DQ30_MISSING_SCOPE"
    DQ30_INVALID_SCOPE = "DQ30_INVALID_SCOPE"
    DQ30_MISSING_INTERTIE_ID = "DQ30_MISSING_INTERTIE_ID"
    # engineering codes for real-template handling (not controlled enums)
    INTERVAL_UNKNOWN = "DQ26_INTERVAL_UNKNOWN"
    SOURCE_TABLE_NO_OUTPUT = "DQ03_SOURCE_TABLE_NO_OUTPUT"
    RELIABILITY_MAP_MISSING = "DQ08_RELIABILITY_MAP_MISSING"
    UNKNOWN_PLANT_TYPE = "DQ05_UNKNOWN_PLANT_TYPE"
    UNKNOWN_STORAGE_TYPE = "DQ05_UNKNOWN_STORAGE_TYPE"
    STORAGE_DURATION_MISMATCH = "DQ11_STORAGE_DURATION_MISMATCH"
    INTERTIE_DIRECTION_UNKNOWN = "DQ10_INTERTIE_DIRECTION_UNKNOWN"
    INTERTIE_NET_MISMATCH = "DQ19_INTERTIE_NET_MISMATCH"
    STANDARD_METRIC_UNMAPPED = "DQ03_STANDARD_METRIC_UNMAPPED"
    LEDGER_TOTAL_MISMATCH = "DQ33_LEDGER_TOTAL_MISMATCH"
    EMERGENCY_PROGRAM_INVALID = "DQ34_EMERGENCY_PROGRAM_INVALID"


class ValidationError(ValueError):
    """Raised when validation cannot proceed at all."""


@dataclass
class ValidationIssue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    table: str = ""
    locator: str = ""


@dataclass
class ValidationReport:
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    computed_max_grade: str | None = None
    grade_reasons: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "failed" if self.errors else "passed"


_REQUIRED_TABLES = {
    "load_hourly",
    "thermal_units",
    "thermal_reliability_classes",
    "thermal_unit_reliability_map",
    "renewable_hourly",
    "renewable_capacities",
    "intertie_hourly",
    "interties",
}

_PRIMARY_KEYS: dict[str, list[str]] = {
    "load_hourly": ["region_id", "scenario_id", "timestamp"],
    "thermal_units": ["unit_id"],
    "thermal_reliability_classes": ["reliability_class_id"],
    "thermal_unit_reliability_map": ["unit_id"],
    "renewable_hourly": ["resource_group_id", "scenario_id", "timestamp"],
    "renewable_capacities": ["resource_group_id", "year"],
    "storage_units": ["storage_id"],
    "hydro_units": ["hydro_id"],
    "interties": ["intertie_id"],
    "intertie_hourly": ["intertie_id", "scenario_id", "timestamp"],
    "renewable_plants": ["plant_id"],
    "adequacy_standards": ["standard_id"],
    "planned_outages": ["outage_id"],
    # W1/MR-04: ordered emergency programs (row order = dispatch order)
    "emergency_programs": ["program_id"],
}

# full required-field definitions per standard table (not just keys)
_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "load_hourly": {
        "record_id",
        "region_id",
        "scenario_id",
        "weather_year",
        "timestamp",
        "load_mw",
        "load_basis",
        "is_forecast",
        "source_id",
        "data_status",
        "quality_flag",
    },
    "thermal_units": {
        "record_id",
        "unit_id",
        "plant_id",
        "plant_name",
        "unit_name",
        "technology_code",
        "technology_label_raw",
        "capacity_mw",
        "capacity_basis",
        "region_id",
        "source_id",
    },
    "thermal_reliability_classes": {
        "record_id",
        "reliability_class_id",
        "class_name",
        "model_type",
        "authoritative_parameter_set",
        "for_rate",
        "source_id",
    },
    "thermal_unit_reliability_map": {
        "record_id",
        "unit_id",
        "reliability_class_id",
        "mapping_method",
        "confidence",
        "source_id",
    },
    "renewable_hourly": {
        "record_id",
        "resource_group_id",
        "scenario_id",
        "weather_year",
        "timestamp",
        "generation_basis",
        "source_id",
    },
    "renewable_capacities": {
        "record_id",
        "resource_group_id",
        "technology",
        "region_id",
        "year",
        "installed_capacity_mw",
        "capacity_basis",
        "source_id",
    },
    "storage_units": {
        "record_id",
        "storage_id",
        "storage_name",
        "storage_type",
        "charge_power_mw",
        "discharge_power_mw",
        "energy_capacity_mwh",
        "soc_min_fraction",
        "soc_max_fraction",
        "self_discharge_per_hour",
        "region_id",
        "source_id",
    },
    "hydro_units": {
        "record_id",
        "hydro_id",
        "hydro_name",
        "capacity_mw",
        "regulation_type",
        "region_id",
        "source_id",
    },
    "hydro_hourly": {"record_id", "hydro_id", "scenario_id", "timestamp", "source_id"},
    "interties": {
        "record_id",
        "intertie_id",
        "intertie_name",
        "intertie_type",
        "rated_capacity_mw",
        "normal_import_limit_mw",
        "normal_export_limit_mw",
        "availability_model",
        "source_id",
    },
    "intertie_hourly": {
        "record_id",
        "intertie_id",
        "scenario_id",
        "timestamp",
        "scheduled_import_mw",
        "scheduled_export_mw",
        "source_id",
    },
    "renewable_plants": {
        "record_id",
        "plant_id",
        "plant_name",
        "technology",
        "capacity_mw",
        "region_id",
        "target_status",
        "source_id",
    },
    "adequacy_standards": {
        "record_id",
        "standard_id",
        "metric",
        "value",
        "unit",
        "jurisdiction",
        "status",
        "source_id",
        "note",
    },
    "planned_outages": {
        "record_id",
        "resource_id",
        "outage_id",
        "start_timestamp",
        "end_timestamp",
        "available_fraction",
        "status",
        "source_id",
    },
    # W1/MR-04: ordered emergency programs (row order = dispatch order);
    # storage is NOT in this table (it enters via the storage fleet)
    "emergency_programs": {
        "record_id",
        "program_id",
        "program_name",
        "kind",
        "available_capacity_mw",
        "max_duration_hours_per_event",
        "max_events_per_year",
        "energy_limit_mwh_per_year",
        "availability_fraction",
        "source_id",
    },
}

_LEGACY_RISK_METRICS = {"lole", "lolh", "eue", "neue", "LOLE", "LOLH", "EUE", "NEUE"}
_LEGACY_GRADES = {"G0", "G1", "G2", "G3"}
_LEGACY_CAPACITY_BASIS = {"gross", "net", "profile"}


def required_columns_for(table: str) -> frozenset[str]:
    """Public accessor for the canonical standard required-field
    definitions (single source - never a second dictionary copy)."""
    return frozenset(_REQUIRED_COLUMNS.get(table, ()))


def _add(
    report: ValidationReport,
    issues: list[ValidationIssue],
    severity: str,
) -> None:
    for issue in issues:
        if severity == "error":
            report.errors.append(issue)
        else:
            report.warnings.append(issue)


def _check_axis(
    series: pd.Series,
    table: str,
    issues: list[ValidationIssue],
    timezone: str | None = None,
) -> None:
    stamps = pd.to_datetime(series, utc=True, errors="coerce")
    if stamps.isna().any():
        issues.append(
            ValidationIssue(
                IssueCode.DATA_TIME_GAP,
                "error",
                "unparseable timestamp",
                table=table,
            )
        )
    if stamps.duplicated().any():
        issues.append(
            ValidationIssue(
                IssueCode.DATA_TIME_DUPLICATE,
                "error",
                "duplicate timestamp",
                table=table,
                locator="timestamp",
            )
        )
    if timezone is None:
        ordered = stamps.dropna().sort_values()
        _check_consecutive_axis(ordered, table, issues)
        return
    # weather-year semantics: each LOCAL calendar year must be complete
    # and internally consecutive; gaps BETWEEN year blocks are allowed
    # (non-contiguous weather-year subsets are valid).
    local = stamps.dropna().dt.tz_convert(timezone)
    for _year, group in local.groupby(local.dt.year):
        _check_consecutive_axis(
            group.sort_values().reset_index(drop=True),
            table,
            issues,
        )


def _check_consecutive_axis(
    ordered: pd.Series,
    table: str,
    issues: list[ValidationIssue],
) -> None:
    if len(ordered) < 2:
        return
    diffs = ordered.diff().dropna()
    bad = diffs[diffs != pd.Timedelta(hours=1)]
    if len(bad) > 0:
        issues.append(
            ValidationIssue(
                IssueCode.DATA_TIME_GAP,
                "error",
                "non-consecutive hourly axis",
                table=table,
                locator=str(bad.index[0]),
            )
        )


def _validate_time_axis(
    tables: dict[str, pd.DataFrame],
    report: ValidationReport,
    timezone: str | None = None,
) -> None:
    issues: list[ValidationIssue] = []
    hour_sets: dict[str, set[pd.Timestamp]] = {}
    for table, keys in _PRIMARY_KEYS.items():
        if table not in tables:
            continue
        frame = tables[table]
        if "timestamp" not in frame.columns:
            continue
        group_keys = [key for key in keys if key != "timestamp" and key in frame.columns]
        if group_keys:
            for _group_key, sub in frame.groupby(group_keys, dropna=False):
                _check_axis(sub["timestamp"], table, issues, timezone=timezone)
        else:
            _check_axis(frame["timestamp"], table, issues, timezone=timezone)
        hour_sets[table] = set(
            pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dropna()
        )
    aligned = [s for s in hour_sets.values() if s]
    if len(aligned) >= 2:
        base = aligned[0]
        for table, stamps in hour_sets.items():
            if table == "load_hourly":
                continue
            if stamps != base:
                issues.append(
                    ValidationIssue(
                        IssueCode.TIME_MISALIGNED,
                        "error",
                        "hour sets differ from load_hourly",
                        table=table,
                    )
                )
    _add(report, issues, "error")


def _strict_numeric_column(frame: pd.DataFrame, column: str) -> list[float] | None:
    """DQ-24 strict numeric gate: bool dtype, object/string
    pseudo-numbers, NaN and inf are all rejected - they never bypass the
    SOC / capacity / usable-energy validation."""
    series = frame[column]
    if not pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return None
    values = pd.to_numeric(series, errors="coerce")
    if values.isna().any():
        return None
    result: list[float] = []
    for value in values:
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        result.append(numeric)
    return result


def _validate_storage_soc_bounds(
    storage: pd.DataFrame, issues: list[ValidationIssue]
) -> tuple[list[float] | None, list[float] | None]:
    """DQ-24 SOC-fraction gate: strict numerics, [0, 1] range and the
    min < max ordering rule."""
    soc_min = (
        _strict_numeric_column(storage, "soc_min_fraction")
        if "soc_min_fraction" in storage.columns
        else None
    )
    soc_max = (
        _strict_numeric_column(storage, "soc_max_fraction")
        if "soc_max_fraction" in storage.columns
        else None
    )
    for column, values in (("soc_min_fraction", soc_min), ("soc_max_fraction", soc_max)):
        if column in storage.columns and values is None:
            issues.append(
                ValidationIssue(
                    IssueCode.STORAGE_SOC_FRACTION_RANGE,
                    "error",
                    f"{column} must be strictly numeric in [0, 1] (bool/string/NaN/inf rejected)",
                    table="storage_units",
                )
            )
    if soc_min is not None and soc_max is not None:
        if any(value < 0.0 or value > 1.0 for value in soc_min):
            _append_storage_issue(
                issues, IssueCode.STORAGE_SOC_FRACTION_RANGE, "soc_min_fraction outside [0, 1]"
            )
        if any(value < 0.0 or value > 1.0 for value in soc_max):
            _append_storage_issue(
                issues, IssueCode.STORAGE_SOC_FRACTION_RANGE, "soc_max_fraction outside [0, 1]"
            )
        if any(min_value >= max_value for min_value, max_value in zip(soc_min, soc_max)):
            _append_storage_issue(
                issues, IssueCode.STORAGE_SOC_INVALID, "soc_min_fraction >= soc_max_fraction"
            )
    return soc_min, soc_max


def _validate_storage_capacity(
    storage: pd.DataFrame, issues: list[ValidationIssue]
) -> list[float] | None:
    """DQ-24 energy-capacity gate: strictly numeric, finite, positive."""
    capacity = (
        _strict_numeric_column(storage, "energy_capacity_mwh")
        if "energy_capacity_mwh" in storage.columns
        else None
    )
    if "energy_capacity_mwh" in storage.columns and capacity is None:
        _append_storage_issue(
            issues,
            IssueCode.STORAGE_ENERGY_CAPACITY_INVALID,
            "energy_capacity_mwh must be strictly numeric, finite and "
            "positive (bool/string/NaN/inf rejected)",
        )
    elif capacity is not None and any(value <= 0.0 for value in capacity):
        _append_storage_issue(
            issues,
            IssueCode.STORAGE_ENERGY_CAPACITY_INVALID,
            "energy_capacity_mwh must be positive",
        )
    return capacity


def _validate_storage_usable_energy(
    storage: pd.DataFrame,
    issues: list[ValidationIssue],
    storage_usable_energy_tolerance_mwh: float,
    soc_min: list[float] | None,
    soc_max: list[float] | None,
    capacity: list[float] | None,
) -> None:
    """DQ-24 usable-energy formula check (efficiency never multiplied in)."""
    if "usable_energy_mwh" not in storage.columns:
        return
    if (
        soc_min is None
        or soc_max is None
        or capacity is None
        or any(value <= 0.0 for value in capacity)
        or any(min_value >= max_value for min_value, max_value in zip(soc_min, soc_max))
    ):
        return
    usable = _strict_numeric_column(storage, "usable_energy_mwh")
    if usable is None:
        _append_storage_issue(
            issues,
            IssueCode.STORAGE_USABLE_ENERGY_MISMATCH,
            "usable_energy_mwh must be strictly numeric and finite (bool/string/NaN/inf rejected)",
        )
        return
    if any(
        abs(usable_value - cap * (max_value - min_value)) > storage_usable_energy_tolerance_mwh
        for usable_value, cap, min_value, max_value in zip(usable, capacity, soc_min, soc_max)
    ):
        _append_storage_issue(
            issues,
            IssueCode.STORAGE_USABLE_ENERGY_MISMATCH,
            "usable_energy_mwh deviates from energy_capacity_mwh x "
            "(soc_max_fraction - soc_min_fraction) beyond the configured "
            f"tolerance {storage_usable_energy_tolerance_mwh} MWh "
            "(efficiency must not be multiplied in)",
        )


def _validate_storage_round_trip(storage: pd.DataFrame, issues: list[ValidationIssue]) -> None:
    if "round_trip_efficiency" not in storage.columns or not pd.api.types.is_numeric_dtype(
        storage["round_trip_efficiency"]
    ):
        return
    bad = storage["round_trip_efficiency"][
        (storage["round_trip_efficiency"] <= 0) | (storage["round_trip_efficiency"] > 1)
    ]
    if len(bad) > 0:
        _append_storage_issue(
            issues, IssueCode.EFFICIENCY_OUT_OF_RANGE, "efficiency outside (0, 1]"
        )


def _append_storage_issue(issues: list[ValidationIssue], code: str, message: str) -> None:
    issues.append(ValidationIssue(code, "error", message, table="storage_units"))


def _validate_storage_units_round13(
    storage: pd.DataFrame,
    issues: list[ValidationIssue],
    storage_usable_energy_tolerance_mwh: float,
) -> None:
    """DQ-24 storage-unit physics validation (SOC bounds, capacity and
    the usable-energy formula, efficiency never multiplied in)."""
    soc_min, soc_max = _validate_storage_soc_bounds(storage, issues)
    capacity = _validate_storage_capacity(storage, issues)
    _validate_storage_usable_energy(
        storage, issues, storage_usable_energy_tolerance_mwh, soc_min, soc_max, capacity
    )
    _validate_storage_round_trip(storage, issues)


def validate_standard_tables(
    tables: dict[str, pd.DataFrame],
    declared_grade: str,
    adapter_issues: list[ValidationIssue] | None = None,
    storage_usable_energy_tolerance_mwh: float = 1e-6,
    timezone: str | None = None,
    declared_total_capacity_mw: float | None = None,
    ledger_total_tolerance_percent: float = 1.0,
) -> ValidationReport:
    """Run the standard-layer validation pipeline.

    ``storage_usable_energy_tolerance_mwh`` is the DQ-24 explicit,
    configurable absolute tolerance for the usable-energy formula check
    (never a silent default that hides a real mismatch).

    ``declared_total_capacity_mw`` (R4-B7, optional) is the user's
    DECLARED planning-calibre total installed capacity; when provided,
    the ledger total (thermal + renewable capacity, storage discharge
    power, hydro capacity) reconciles against it within
    ``ledger_total_tolerance_percent`` - a drift is a WARNING, never a
    blocker (方法说明第八章风险4 第三查)."""
    if (
        isinstance(storage_usable_energy_tolerance_mwh, bool)
        or not isinstance(storage_usable_energy_tolerance_mwh, (int, float))
        or not math.isfinite(float(storage_usable_energy_tolerance_mwh))
        or storage_usable_energy_tolerance_mwh < 0.0
    ):
        raise ValidationError(
            "storage_usable_energy_tolerance_mwh must be a finite non-negative number"
        )
    report = ValidationReport()
    issues: list[ValidationIssue] = []

    # stage 1: files / required tables
    for table in sorted(_REQUIRED_TABLES):
        if table not in tables:
            issues.append(
                ValidationIssue(
                    IssueCode.FILE_MISSING, "error", "required table missing", table=table
                )
            )

    # stage 2: required columns (full field definitions, not just keys)
    invalid_tables: set[str] = set()
    for table, frame in tables.items():
        required = _REQUIRED_COLUMNS.get(table)
        if required is None:
            continue
        missing = sorted(col for col in required if col not in frame.columns)
        if missing:
            invalid_tables.add(table)
            issues.append(
                ValidationIssue(
                    IssueCode.MISSING_REQUIRED_COLUMNS,
                    "error",
                    f"missing required columns: {missing}",
                    table=table,
                )
            )

    # Later stages only inspect structurally valid tables. A malformed
    # table is already blocked above and must never turn a DQ report into
    # an implementation exception such as KeyError.
    validation_tables = {
        name: frame for name, frame in tables.items() if name not in invalid_tables
    }

    # stage 3: duplicate primary keys
    for table, keys in _PRIMARY_KEYS.items():
        if table not in validation_tables:
            continue
        frame = validation_tables[table]
        present = [k for k in keys if k in frame.columns]
        if len(present) < len(keys):
            continue
        if frame.duplicated(subset=present).any():
            issues.append(
                ValidationIssue(
                    IssueCode.DUPLICATE_PRIMARY_KEY,
                    "error",
                    "duplicate primary key",
                    table=table,
                )
            )

    # stage 4: foreign keys and coverage completeness
    if "thermal_units" in validation_tables and "thermal_unit_reliability_map" in validation_tables:
        units = validation_tables["thermal_units"]
        mapping = validation_tables["thermal_unit_reliability_map"]
        unit_ids = set(units["unit_id"])
        mapped_ids = set(mapping["unit_id"])
        if not unit_ids <= mapped_ids:
            issues.append(
                ValidationIssue(
                    IssueCode.RELIABILITY_MAP_MISSING,
                    "error",
                    "every in-service thermal unit requires exactly one reliability mapping",
                    table="thermal_unit_reliability_map",
                )
            )
        if mapping.duplicated(subset=["unit_id"]).any():
            issues.append(
                ValidationIssue(
                    IssueCode.DUPLICATE_PRIMARY_KEY,
                    "error",
                    "duplicate reliability mapping per unit",
                    table="thermal_unit_reliability_map",
                )
            )
    if "thermal_units" in validation_tables:
        units = validation_tables["thermal_units"]
        if "unit_id" in units.columns and len(units) < 2:
            issues.append(
                ValidationIssue(
                    IssueCode.REJECTED_ESSENTIAL,
                    "error",
                    "too few thermal units; essential granularity missing",
                    table="thermal_units",
                )
            )
    if (
        "thermal_unit_reliability_map" in validation_tables
        and "thermal_reliability_classes" in validation_tables
    ):
        classes = set(validation_tables["thermal_reliability_classes"]["reliability_class_id"])
        mapping = validation_tables["thermal_unit_reliability_map"]
        orphans = mapping[~mapping["reliability_class_id"].isin(classes)]
        if len(orphans) > 0:
            issues.append(
                ValidationIssue(
                    IssueCode.DATA_ID_ORPHAN,
                    "error",
                    "orphan reliability_class_id",
                    table="thermal_unit_reliability_map",
                )
            )
    if "hydro_hourly" in validation_tables and "hydro_units" in validation_tables:
        hydro_ids = set(validation_tables["hydro_units"]["hydro_id"])
        hydro_hourly = validation_tables["hydro_hourly"]
        orphans = hydro_hourly[~hydro_hourly["hydro_id"].isin(hydro_ids)]
        if len(orphans) > 0:
            issues.append(
                ValidationIssue(
                    IssueCode.DATA_ID_ORPHAN,
                    "error",
                    "orphan hydro_id in hydro_hourly",
                    table="hydro_hourly",
                )
            )
    if "intertie_hourly" in validation_tables and "interties" in validation_tables:
        ties = set(validation_tables["interties"]["intertie_id"])
        intertie_hourly = validation_tables["intertie_hourly"]
        orphans = intertie_hourly[~intertie_hourly["intertie_id"].isin(ties)]
        if len(orphans) > 0:
            issues.append(
                ValidationIssue(
                    IssueCode.DATA_ID_ORPHAN,
                    "error",
                    "orphan intertie_id",
                    table="intertie_hourly",
                )
            )
    if "renewable_hourly" in validation_tables and "renewable_capacities" in validation_tables:
        groups = set(validation_tables["renewable_capacities"]["resource_group_id"])
        renewable_hourly = validation_tables["renewable_hourly"]
        orphans = renewable_hourly[~renewable_hourly["resource_group_id"].isin(groups)]
        if len(orphans) > 0:
            issues.append(
                ValidationIssue(
                    IssueCode.DATA_ID_ORPHAN,
                    "error",
                    "orphan resource_group_id",
                    table="renewable_hourly",
                )
            )

    # stage 5: time axis
    _validate_time_axis(validation_tables, report, timezone=timezone)

    # stage 5b (2026-09-22 user-authorized gap fix): DQ-21 weight sets at
    # IMPORT time. The SAME validate_weather_year_weights the engine calls
    # at run time now also gates the import chain (never a second copy) -
    # declared years = the load table's weather years (the load curve is
    # the weather-year bottleneck by contract). Before this fix a bad
    # weight set imported as "passed" and only failed at run time.
    weights_frame = validation_tables.get("weather_year_weight_sets")
    load_frame = validation_tables.get("load_hourly")
    if (
        weights_frame is not None
        and load_frame is not None
        and "weather_year" in load_frame.columns
    ):
        declared_years = sorted(
            {
                int(year)
                for year in pd.to_numeric(
                    load_frame["weather_year"], errors="coerce"
                )
                .dropna()
                .unique()
            }
        )
        if declared_years:
            issues.extend(
                validate_weather_year_weights(weights_frame, declared_years)
            )

    # stage 6/7: units and physics
    for mapping_table in ("intertie_hourly", "interties"):
        if (
            mapping_table in validation_tables
            and "profile_scope" in validation_tables[mapping_table].columns
        ):
            # never drop_duplicates on unvalidated raw columns (they may
            # contain lists/dicts/objects); the public validator handles
            # repeats through canonical scalar keys
            issues.extend(validate_intertie_mapping_config(validation_tables[mapping_table]))
    issues.extend(
        _validate_dq27_unit_outage_consistency(
            validation_tables.get("thermal_units"),
            validation_tables.get("thermal_unit_reliability_map"),
            validation_tables.get("thermal_reliability_classes"),
            validation_tables.get("planned_outages"),
        )
    )
    if "thermal_units" in validation_tables:
        cap = validation_tables["thermal_units"]["capacity_mw"]
        if pd.api.types.is_numeric_dtype(cap) and (cap < 0).any():
            issues.append(
                ValidationIssue(
                    IssueCode.NEGATIVE_POWER,
                    "error",
                    "negative capacity_mw",
                    table="thermal_units",
                )
            )
    if "storage_units" in validation_tables:
        _validate_storage_units_round13(
            validation_tables["storage_units"],
            issues,
            storage_usable_energy_tolerance_mwh,
        )
    if "emergency_programs" in validation_tables:
        _validate_emergency_programs(
            validation_tables["emergency_programs"], issues
        )
    if "renewable_hourly" in validation_tables:
        ren = validation_tables["renewable_hourly"]
        for group, sub in ren.groupby(["resource_group_id", "scenario_id"]):
            if sub["generation_basis"].nunique() > 1:
                issues.append(
                    ValidationIssue(
                        IssueCode.GENERATION_BASIS_MIXED,
                        "error",
                        "generation_basis mixed within group/scenario",
                        table="renewable_hourly",
                        locator=str(group),
                    )
                )
            authority = str(sub["generation_basis"].iloc[0])
            column = {
                "available": "available_generation_mw",
                "actual": "actual_generation_mw",
                "modelled": "modelled_generation_mw",
            }.get(authority)
            if column and column in sub.columns and sub[column].isna().any():
                issues.append(
                    ValidationIssue(
                        IssueCode.AUTHORITY_COLUMN_GAP,
                        "error",
                        "authority column has gaps; per-hour fallback forbidden",
                        table="renewable_hourly",
                        locator=f"{group[0]}/{authority}",
                    )
                )

    # stage 7: legacy markers and enums never in the standard layer
    for table, frame in tables.items():
        if frame.empty:
            issues.append(
                ValidationIssue(
                    IssueCode.FILE_EMPTY,
                    "error",
                    "empty standard table",
                    table=table,
                )
            )
        numeric = frame.select_dtypes(include="number")
        if numeric.size and (numeric == -999).any().any():
            issues.append(
                ValidationIssue(
                    IssueCode.LEGACY_MISSING_IN_STANDARD,
                    "error",
                    "-999 present in standard layer",
                    table=table,
                )
            )
        for column in frame.columns:
            series = frame[column]
            if not pd.api.types.is_object_dtype(series) and not pd.api.types.is_string_dtype(
                series
            ):
                continue
            # legacy-enum scan over BUILTIN str scalars only: hostile
            # objects or str SUBCLASSES (whose __str__/__hash__ may
            # raise) are never collected, hashed or stringified - they
            # simply cannot match a legacy enum. Values are read by
            # POSITION (series.tolist() never touches the frame index,
            # whose labels may themselves be hostile)
            values = {
                value
                for value in series.tolist()
                if value is not None
                and not (type(value) is float and math.isnan(value))
                and type(value) is str
            }
            if column == "metric" and values & _LEGACY_RISK_METRICS:
                issues.append(
                    ValidationIssue(
                        IssueCode.LEGACY_ENUM_IN_STANDARD,
                        "error",
                        "legacy risk metric enum in standard layer",
                        table=table,
                        locator=column,
                    )
                )
            if "grade" in column and values & _LEGACY_GRADES:
                issues.append(
                    ValidationIssue(
                        IssueCode.LEGACY_ENUM_IN_STANDARD,
                        "error",
                        "legacy data grade in standard layer",
                        table=table,
                        locator=column,
                    )
                )
            if column == "capacity_basis" and values & _LEGACY_CAPACITY_BASIS:
                issues.append(
                    ValidationIssue(
                        IssueCode.LEGACY_ENUM_IN_STANDARD,
                        "error",
                        "legacy capacity basis in standard layer",
                        table=table,
                        locator=column,
                    )
                )

    # stage 8: data grade
    grade_result = compute_max_data_grade(validation_tables)
    report.computed_max_grade = grade_result.grade
    report.grade_reasons = grade_result.reasons
    order = ["G0_teaching", "G1_preliminary", "G2_research", "G3_rule_support"]
    if grade_result.grade is None:
        issues.append(
            ValidationIssue(
                IssueCode.GRADE_UNDETERMINED,
                "error",
                "no complete synchronized weather year; grade undetermined",
            )
        )
    elif declared_grade not in order:
        issues.append(
            ValidationIssue(
                IssueCode.GRADE_ABOVE_MAX,
                "error",
                f"declared grade {declared_grade!r} not a controlled value",
            )
        )
    elif order.index(declared_grade) > order.index(grade_result.grade):
        issues.append(
            ValidationIssue(
                IssueCode.GRADE_ABOVE_MAX,
                "error",
                f"declared {declared_grade} exceeds computed max {grade_result.grade}",
            )
        )

    if adapter_issues:
        for issue in adapter_issues:
            (report.errors if issue.severity == "error" else report.warnings).append(issue)

    _check_ledger_total(tables, declared_total_capacity_mw, ledger_total_tolerance_percent, issues)

    for issue in issues:
        (report.errors if issue.severity == "error" else report.warnings).append(issue)
    return report


_LEDGER_SUM_TABLES: tuple[tuple[str, str], ...] = (
    ("thermal_units", "capacity_mw"),
    ("renewable_capacities", "capacity_mw"),
    ("storage_units", "discharge_power_mw"),
    ("hydro_units", "capacity_mw"),
)


_EMERGENCY_KINDS = frozenset({"demand_response", "orderly_curtailment"})


def _validate_emergency_programs(
    frame: pd.DataFrame, issues: list[ValidationIssue]
) -> None:
    """W1/MR-04: row-level rules for the ordered emergency-programs
    table (方法说明第二章(二)4 / 第三章(一) 第7类). Row order is the
    dispatch order; every program needs a controlled kind, a positive
    capacity, integer duration/event limits and an optional positive
    annual energy cap. Storage deliberately never appears here (it
    enters via the storage fleet and dispatches FIRST by contract)."""
    kind = frame["kind"].astype(str).str.strip()
    bad_kind = ~kind.isin(_EMERGENCY_KINDS)
    for position in frame.index[bad_kind]:
        issues.append(
            ValidationIssue(
                IssueCode.EMERGENCY_PROGRAM_INVALID,
                "error",
                f"应急程序 kind 必须为 {'/'.join(sorted(_EMERGENCY_KINDS))} 之一，"
                f"收到 {frame.at[position, 'kind']!r}",
                table="emergency_programs",
                locator=str(position),
            )
        )
    capacity = pd.to_numeric(frame["available_capacity_mw"], errors="coerce")
    bad_capacity = capacity.isna() | (capacity <= 0)
    for position in frame.index[bad_capacity]:
        issues.append(
            ValidationIssue(
                IssueCode.EMERGENCY_PROGRAM_INVALID,
                "error",
                "available_capacity_mw 必须为正数",
                table="emergency_programs",
                locator=str(position),
            )
        )
    for column, low in (
        ("max_duration_hours_per_event", 1),
        ("max_events_per_year", 1),
    ):
        values = pd.to_numeric(frame[column], errors="coerce")
        bad = values.isna() | (values < low) | (values % 1 != 0)
        for position in frame.index[bad]:
            issues.append(
                ValidationIssue(
                    IssueCode.EMERGENCY_PROGRAM_INVALID,
                    "error",
                    f"{column} 必须为 ≥ {low} 的整数",
                    table="emergency_programs",
                    locator=str(position),
                )
            )
    if "energy_limit_mwh_per_year" in frame.columns:
        energy = pd.to_numeric(frame["energy_limit_mwh_per_year"], errors="coerce")
        bad_energy = energy.notna() & (energy <= 0)
        for position in frame.index[bad_energy]:
            issues.append(
                ValidationIssue(
                    IssueCode.EMERGENCY_PROGRAM_INVALID,
                    "error",
                    "energy_limit_mwh_per_year 提供时必须为正数",
                    table="emergency_programs",
                    locator=str(position),
                )
            )


def _check_ledger_total(
    tables: dict[str, pd.DataFrame],
    declared_total_capacity_mw: float | None,
    tolerance_percent: float,
    issues: list[ValidationIssue],
) -> None:
    """R4-B7 (方法说明第八章风险4 第三查): reconcile the LEDGER capacity
    total against the DECLARED planning figure when one is supplied.

    The ledger sums what the 台帐 actually carries (thermal + renewable
    capacity, storage discharge power, hydro capacity). A drift beyond
    the relative tolerance is a WARNING - planning calibres may
    legitimately differ (reserve margins, under-construction units), so
    the mismatch is surfaced for human reconciliation, never silently
    ignored and never an import blocker. No declaration = no check."""
    if declared_total_capacity_mw is None:
        return
    try:
        declared = float(declared_total_capacity_mw)
    except (TypeError, ValueError):
        declared = float("nan")
    if not math.isfinite(declared) or declared <= 0.0:
        issues.append(
            ValidationIssue(
                IssueCode.LEDGER_TOTAL_MISMATCH,
                "warning",
                f"申报的规划口径总容量 {declared_total_capacity_mw!r} 无效（须为正数），"
                "台帐合计对账未执行，请核对申报口径",
            )
        )
        return
    parts: dict[str, float] = {}
    for table, column in _LEDGER_SUM_TABLES:
        frame = tables.get(table)
        if frame is None or column not in frame.columns:
            continue
        try:
            values = pd.to_numeric(frame[column], errors="coerce").dropna()
        except Exception:  # noqa: BLE001 - a broken column simply contributes 0
            continue
        parts[table] = float(values.sum())
    ledger_total = sum(parts.values())
    drift_percent = abs(ledger_total - declared) / declared * 100.0
    if drift_percent <= float(tolerance_percent):
        return
    breakdown = ", ".join(f"{name}={value:.2f}" for name, value in sorted(parts.items()))
    issues.append(
        ValidationIssue(
            IssueCode.LEDGER_TOTAL_MISMATCH,
            "warning",
            (
                f"台帐合计 {ledger_total:.2f} MW 与规划口径申报 {declared:.2f} MW "
                f"偏差 {drift_percent:.2f}%（容差 {tolerance_percent}%）："
                f"[{breakdown}]；请核对台帐与规划口径（备用/在建口径差异属正常，"
                "但必须人工确认）"
            ),
        )
    )


def _is_valid_rate_value(value: object) -> bool:
    """Strict rate field gate: real finite number in [0, 1] (bool /
    strings / NaN / Inf / 10**400 / illegal objects all invalid; 0 and 1
    are legal per the closed-interval rule)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if _is_missing(value):
        return False
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(number) and 0.0 <= number <= 1.0


def _validate_dq27_class_fields(
    classes: pd.DataFrame,
) -> tuple[dict[str, object], list[ValidationIssue]]:
    """DQ-27 step A (fifth review): FIELD-level validation over EVERY
    thermal_reliability_classes row - independent of the unit mapping
    and of whether any unit uses the class. Each non-empty
    planned_outage_rate is validated exactly once per row; an illegal
    rate yields ONE DQ27_INVALID_RATE error with
    locator='reliability_class_id=<id>;planned_outage_rate'. Empty
    (None / NaN / pd.NA) rates are OPTIONAL and produce nothing.
    Duplicate class primary keys stay blocked by the existing
    DUPLICATE_PRIMARY_KEY stage - DQ-27 uses first-row-wins and never
    masks duplicates."""
    rate_by_class: dict[str, object] = {}
    issues: list[ValidationIssue] = []
    for _, row in classes.iterrows():
        class_id = str(row["reliability_class_id"])
        rate = row.get("planned_outage_rate")
        rate_by_class.setdefault(class_id, rate)
        if rate is None or _is_missing(rate):
            continue
        if not _is_valid_rate_value(rate):
            issues.append(
                ValidationIssue(
                    IssueCode.DQ27_INVALID_RATE,
                    "error",
                    "planned_outage_rate must be a finite number in [0, 1]",
                    table="thermal_reliability_classes",
                    locator=f"reliability_class_id={class_id};planned_outage_rate",
                )
            )
    return rate_by_class, issues


def _strict_index_type(label: object) -> type | None:
    """UNIFIED strict builtin index gate (eighth review): returns the
    type ONLY for exact builtin bool / int / float / str. Every
    subclass (int/float/str subclasses included) and arbitrary object
    returns None - their __hash__ / __eq__ / __str__ / __repr__ /
    __format__ must never run."""
    if type(label) in (bool, int, float, str):
        return type(label)
    return None


def _is_missing_index_label(label: object) -> bool:
    """Missing index label test WITHOUT invoking any custom object
    method: None, and NaN on a STRICT builtin float only (float
    subclasses are unsafe and always position-tagged); pd.NA falls
    through as an unsafe non-controlled type."""
    if label is None:
        return True
    return type(label) is float and math.isnan(label)


def _is_safe_index_label(label: object) -> bool:
    """Controlled basic index types whose str() is pure builtin code:
    EXACT builtin bool / int / float / str (via _strict_index_type).
    int/float/str subclasses and arbitrary objects are NEVER safe."""
    return _strict_index_type(label) is not None


def _duplicate_index_flags(labels: list[object]) -> list[bool]:
    """Positional duplicate detection: every label that is missing or
    non-safe is ALWAYS flagged (position tag required); safe builtin
    labels are flagged when the same label appears more than once.
    Only strict builtin labels ever enter the seen dict (no custom
    __hash__/__eq__ can run)."""
    flags = [False] * len(labels)
    seen: dict[object, int] = {}
    for position, label in enumerate(labels):
        if _is_missing_index_label(label) or not _is_safe_index_label(label):
            flags[position] = True
            continue
        if label in seen:
            flags[position] = True
            flags[seen[label]] = True
        else:
            seen[label] = position
    return flags


def _outage_row_locator(label: object, position: int, duplicated: bool) -> str:
    """Stable locator from strict builtin types only: a unique safe
    label keeps the row=<index> form; duplicated safe labels gain
    ;position=<pos>; missing labels (None / strict-float NaN) use
    index=<tag>;position=<pos>; every other object (int/float/str
    subclasses included) uses index=<classname>;position=<pos> - object
    __str__/__repr__ is NEVER invoked."""
    if label is None:
        return f"index=None;position={position}"
    if type(label) is float and math.isnan(label):
        return f"index=nan;position={position}"
    if _strict_index_type(label) is not None:
        base = str(label)
        if duplicated:
            return f"row={base};position={position}"
        return f"row={base}"
    return f"index={type(label).__name__};position={position}"


def _collect_valid_outage_rows(
    outages: pd.DataFrame,
    unit_ids: set[str],
    issues: list[ValidationIssue],
) -> dict[str, pd.DataFrame]:
    """DQ-27 resource_id gates (C901 split): every row is validated
    BEFORE any grouping - only non-empty strings are grouped (illegal
    types yield DQ27_INVALID_RESOURCE_ID with a stable locator, never
    an object memory address); valid-but-unknown ids yield
    DQ27_ORPHAN_OUTAGE, independently of any rate.

    SIXTH-review fix: the valid-row gate is strictly POSITIONAL. A
    plain boolean list aligned to row positions is built with
    enumerate (never label-based .loc assignment on a frame index that
    may carry duplicates), and legal rows are selected with a pure
    positional iloc indexer. With duplicate indices the locator gains
    a positional discriminator (row=<index>;position=<pos>) so every
    illegal row stays individually auditable."""
    resource_values = outages["resource_id"].tolist()
    index_labels = outages.index.tolist()
    duplicated_flags = _duplicate_index_flags(index_labels)
    valid_flags = [False] * len(outages)
    for position, value in enumerate(resource_values):
        # SEVENTH-review fix: strict builtin-str gate - str SUBCLASSES
        # (whose strip/__str__/__hash__ may be hostile) never pass and
        # never enter strip()/groupby/str()
        if type(value) is str and value.strip():
            valid_flags[position] = True
        else:
            issues.append(
                ValidationIssue(
                    IssueCode.DQ27_INVALID_RESOURCE_ID,
                    "error",
                    "planned_outages.resource_id must be a non-empty string",
                    table="planned_outages",
                    locator=_outage_row_locator(
                        index_labels[position], position, duplicated_flags[position]
                    ),
                )
            )
    by_resource: dict[str, pd.DataFrame] = {}
    # only rows selected by POSITION are grouped - never astype(str)/
    # map/apply over unvalidated raw values, and no str()/repr()/
    # formatting on illegal values (a hostile __str__ must never run)
    valid_positions = [position for position, ok in enumerate(valid_flags) if ok]
    valid_frame = outages.iloc[valid_positions].copy()
    for resource_id, outage_rows in valid_frame.groupby("resource_id", dropna=False):
        key = str(resource_id)
        if key in unit_ids:
            by_resource[key] = outage_rows
        else:
            issues.append(
                ValidationIssue(
                    IssueCode.DQ27_ORPHAN_OUTAGE,
                    "error",
                    "planned_outages resource_id has no matching thermal unit",
                    table="planned_outages",
                    locator=f"resource_id={key}",
                )
            )
    return by_resource


def _validate_dq27_unit_outage_consistency(
    units: pd.DataFrame | None,
    mapping: pd.DataFrame | None,
    classes: pd.DataFrame | None,
    outages: pd.DataFrame | None,
) -> list[ValidationIssue]:
    """DQ-27 on the REAL contract chain (C901 split):

    thermal_units.unit_id
      -> thermal_unit_reliability_map.reliability_class_id
      -> thermal_reliability_classes.planned_outage_rate
      -> planned_outages.resource_id

    SEMANTIC SPLIT (fourth review): the standard data layer only judges
    DATA COEXISTENCE - a non-empty rate together with confirmed /
    planned / modelled outage rows marks the rate CONSISTENCY-ONLY
    (DQ27_RATE_CONSISTENCY_ONLY warning), never a DQ27_DOUBLE_COUNT
    error. The runtime double-count block (applying the rate onto
    capacity that already carries outage effects) lives exclusively in
    the resource-run gate (derive_outage_derating /
    THERMAL_DOUBLE_COUNT_ATTEMPT) and is never inferred here.

    - planned_outage_rate is OPTIONAL (contract §9.1 必需=否): a
      missing column or empty/None/NaN/pd.NA cells never produce a
      DQ-27 diagnostic; illegal non-empty values produce
      DQ27_INVALID_RATE;
    - planned_outages.resource_id is validated row by row BEFORE any
      grouping: only non-empty strings are grouped; illegal types
      (None / pd.NA / list / dict / numbers / objects) produce
      DQ27_INVALID_RESOURCE_ID with a row-based locator (never an
      object memory address); valid-but-unknown ids produce
      DQ27_ORPHAN_OUTAGE - independently of whether any rate exists;
    - every unit is judged ONLY by its own outage rows (sharing a
      reliability class never shares outage state); units without a
      mappable class are skipped here (diagnosed upstream by DQ08 /
      DATA_ID_ORPHAN)."""
    issues: list[ValidationIssue] = []
    unit_ids = set() if units is None else set(units["unit_id"].astype(str))
    outage_by_resource: dict[str, pd.DataFrame] = {}
    if outages is not None and not outages.empty and "resource_id" in outages.columns:
        outage_by_resource = _collect_valid_outage_rows(outages, unit_ids, issues)
    if units is None or mapping is None or mapping.empty:
        return issues
    if "unit_id" not in mapping.columns or "reliability_class_id" not in mapping.columns:
        return issues
    unit_to_class: dict[str, str] = {}
    for _, row in mapping.iterrows():
        unit_id = str(row["unit_id"])
        mapped_class_id = str(row["reliability_class_id"])
        unit_to_class.setdefault(unit_id, mapped_class_id)
    if classes is None or classes.empty or "planned_outage_rate" not in classes.columns:
        return issues
    # step A: field-level validation over ALL class rows (independent of
    # the unit mapping) - one DQ27_INVALID_RATE per illegal class row
    rate_by_class, class_issues = _validate_dq27_class_fields(classes)
    issues.extend(class_issues)
    for _, unit_row in units.iterrows():
        unit_id = str(unit_row["unit_id"])
        class_id = unit_to_class.get(unit_id)
        if class_id is None:
            continue  # missing mapping diagnosed upstream (DQ08)
        rate = rate_by_class.get(class_id)
        if rate is None or _is_missing(rate):
            # optional field: empty rate never blocks
            continue
        if not _is_valid_rate_value(rate):
            # step B never repeats the field-legality judgement - the
            # single class-field error above covers it
            continue
        has_rows = unit_id in outage_by_resource and not outage_by_resource[unit_id].empty
        issues.extend(
            validate_outage_derating_consistency(
                planned_outage_rate=cast(Any, rate),
                explicit_outage_rows_present=has_rows,
                auto_scheduled_events_present=False,
                unit_id=unit_id,
                reliability_class_id=class_id,
            )
        )
    return issues


def validate_outage_derating_consistency(
    *,
    planned_outage_rate: float | None,
    explicit_outage_rows_present: bool,
    auto_scheduled_events_present: bool,
    unit_id: str | None = None,
    reliability_class_id: str | None = None,
) -> list[ValidationIssue]:
    """Public DQ-27 entry: recognises planned-outage double counting at
    the import boundary. The strict-bool flag gates run FIRST (even for
    planned_outage_rate=None, illegal flags yield DQ27_INVALID_FLAG -
    never DQ27_INVALID_RATE). Rate + auto-scheduled trajectories block
    (error DQ27_DOUBLE_COUNT); rate + explicit trajectories enter the
    explicit consistency-only path (warning); illegal rates block with
    DQ27_INVALID_RATE. Diagnostic codes / severities / field paths are
    stable; locator carries unit_id + reliability_class_id +
    planned_outage_rate (the REAL contract field positions)."""
    locator = "planned_outage_rate"
    if unit_id:
        locator = f"unit_id={unit_id};planned_outage_rate"
        if reliability_class_id:
            locator = (
                f"unit_id={unit_id};reliability_class_id={reliability_class_id};planned_outage_rate"
            )
    for name, flag in (
        ("explicit_outage_rows_present", explicit_outage_rows_present),
        ("auto_scheduled_events_present", auto_scheduled_events_present),
    ):
        if not isinstance(flag, bool):
            return [
                ValidationIssue(
                    IssueCode.DQ27_INVALID_FLAG,
                    "error",
                    f"{name} must be a strict bool",
                    table="thermal_reliability_classes",
                    locator=locator,
                )
            ]
    if planned_outage_rate is None:
        return []
    try:
        from capacity_credit.scenarios.thermal_states import (
            ThermalStateError,
            derive_outage_derating,
        )

        decision = derive_outage_derating(
            planned_outage_rate=planned_outage_rate,
            complete_planned_outages_present=explicit_outage_rows_present,
            hourly_capacity_includes_outages=False,
            auto_scheduled_events_present=auto_scheduled_events_present,
        )
    except ThermalStateError as error:
        code = (
            IssueCode.DQ27_DOUBLE_COUNT
            if error.code == "THERMAL_DOUBLE_COUNT_ATTEMPT"
            else IssueCode.DQ27_INVALID_RATE
        )
        return [
            ValidationIssue(
                code,
                "error",
                error.message,
                table="thermal_reliability_classes",
                locator=locator,
            )
        ]
    if decision.rate_role == "consistency_only" and explicit_outage_rows_present:
        return [
            ValidationIssue(
                IssueCode.DQ27_RATE_CONSISTENCY_ONLY,
                "warning",
                "planned_outage_rate is consistency-only: explicit outage "
                "trajectories are present; the rate must never be applied "
                "on top of them",
                table="thermal_reliability_classes",
                locator=locator,
            )
        ]
    return []


def _is_missing(value: object) -> bool:
    """Scalar-missing test: None / NaN / NaT / pd.NA count as missing;
    strings and ints (including huge ints) never do."""
    if value is None:
        return True
    if isinstance(value, (bool, str)):
        return False
    if isinstance(value, int):
        return False
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        return bool(pd.isna(cast(Any, value)))
    except (TypeError, ValueError, OverflowError):
        return False


def _strict_year_int(value: object) -> int | None:
    """Overflow-safe strict-integer-year gate; None = invalid."""
    if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return None
    if _is_missing(value):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    year = int(number)
    if not 1900 <= year <= 2200:
        return None
    return year


def _canonical_mapping_value(value: object) -> str:
    """Hashable canonical key for one mapping cell (C: lists / dicts /
    objects must never break deduplication)."""
    if _is_missing(value):
        return "<NA>"
    if isinstance(value, (str, int, float, bool)):
        return f"{type(value).__name__}:{value}"
    return f"<{type(value).__name__}>"


def validate_intertie_mapping_config(frame: pd.DataFrame) -> list[ValidationIssue]:
    """Public DQ-30 entry for cross-year mapping columns (data contract
    §21: profile_scope / weather_year / profile_alignment /
    leap_day_rule). Never indexes the frame with a dropna-shortened
    Series - all row selection uses full-length boolean masks aligned
    with the original index. Decisions (documented, never implicit):

    - profile_scope column absent -> not applicable, no issues;
    - a row whose profile_scope is empty -> DQ30_MISSING_SCOPE (the
      field is required per contract; partial-empty and all-empty are
      treated identically - never silently guessed);
    - an unknown scope value -> DQ30_INVALID_SCOPE (semantic code,
      never DQ30_INVALID_ALIGNMENT);
    - target_year is NOT a row field of the contract table (run-level
      configuration): a missing / NaN target_year is allowed and is
      supplied by the run config; an INVALID target_year value blocks
      with DQ30_INVALID_TARGET_YEAR;
    - missing or empty intertie_id -> DQ30_MISSING_INTERTIE_ID;
    - conflicting mapping tuples for the same intertie_id ->
      DQ30_DUPLICATE_MAPPING;
    - mixed scopes across DIFFERENT channels are legal (per-channel
      mapping), no issues.

    All numeric conversions are overflow-safe; only stable
    ValidationIssue objects are ever returned."""
    issues: list[ValidationIssue] = []
    if "profile_scope" not in frame.columns:
        return issues
    if "intertie_id" not in frame.columns:
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_MISSING_INTERTIE_ID,
                "error",
                "intertie_id column missing from the mapping frame",
                table="interties",
                locator="intertie_id",
            )
        )
        return issues
    ids = frame["intertie_id"]
    for index, value in ids.items():
        if not isinstance(value, str) or not value.strip():
            issues.append(
                ValidationIssue(
                    IssueCode.DQ30_MISSING_INTERTIE_ID,
                    "error",
                    "intertie_id must be a non-empty string",
                    table="interties",
                    locator=f"row={index}",
                )
            )
    scopes = frame["profile_scope"]
    scope_notna = scopes.notna()
    for index in frame.index[~scope_notna]:
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_MISSING_SCOPE,
                "error",
                "profile_scope is required per row (contract §21.2)",
                table="interties",
                locator=f"row={index}",
            )
        )
    known_scopes = scopes[scope_notna].astype(str).unique()
    for scope in known_scopes:
        if scope not in ("specific_weather_year", "all_weather_years"):
            issues.append(
                ValidationIssue(
                    IssueCode.DQ30_INVALID_SCOPE,
                    "error",
                    f"unknown profile_scope {scope!r}",
                    table="interties",
                    locator="profile_scope",
                )
            )
    scope_text = scopes.astype(str)
    all_mask = scope_notna & scope_text.eq("all_weather_years")
    specific_mask = scope_notna & scope_text.eq("specific_weather_year")
    for _, row in frame[all_mask].iterrows():
        _check_all_weather_years_row(row, issues)
    for _, row in frame[specific_mask].iterrows():
        _check_specific_weather_year_row(row, issues)
    _check_conflicting_mapping_tuples(frame, ids, issues)
    return issues


def _check_conflicting_mapping_tuples(
    frame: pd.DataFrame,
    ids: pd.Series,
    issues: list[ValidationIssue],
) -> None:
    """DQ-30 duplicate-mapping gate (C901 split): the same intertie_id
    carrying DIFFERENT mapping tuples blocks; identical repeated tuples
    (normal hourly rows) do not."""
    mapping_columns = [
        column
        for column in (
            "profile_scope",
            "weather_year",
            "profile_alignment",
            "leap_day_rule",
            "target_year",
        )
        if column in frame.columns
    ]
    clean_mask = ids.astype(str).str.strip().ne("")
    group_keys = ids.map(_canonical_mapping_value)
    for intertie_id, sub in frame[clean_mask].groupby(group_keys[clean_mask], sort=False):
        tuples = (
            sub[mapping_columns]
            .apply(lambda row: "|".join(_canonical_mapping_value(v) for v in row), axis=1)
            .drop_duplicates()
        )
        if len(tuples) > 1:
            issues.append(
                ValidationIssue(
                    IssueCode.DQ30_DUPLICATE_MAPPING,
                    "error",
                    f"conflicting mapping tuples for intertie_id={intertie_id}",
                    table="interties",
                    locator=f"intertie_id={intertie_id}",
                )
            )


def _check_all_weather_years_row(row: pd.Series, issues: list[ValidationIssue]) -> None:
    """DQ-30 per-row checks for all_weather_years (C901 split;
    overflow-safe)."""
    locator = f"intertie_id={row.get('intertie_id')}"
    weather_year = row.get("weather_year")
    if not _is_missing(weather_year):
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_INVALID_WEATHER_YEAR,
                "error",
                "all_weather_years requires weather_year to be empty",
                table="interties",
                locator=locator,
            )
        )
    alignment = row.get("profile_alignment")
    if _is_missing(alignment) or not str(alignment).strip():
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_MISSING_ALIGNMENT,
                "error",
                "all_weather_years requires an explicit profile_alignment",
                table="interties",
                locator=locator,
            )
        )
    elif str(alignment) not in ("hour_of_year", "month_day_hour"):
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_INVALID_ALIGNMENT,
                "error",
                f"profile_alignment must be hour_of_year or month_day_hour, got {alignment!r}",
                table="interties",
                locator=locator,
            )
        )
    leap_rule = row.get("leap_day_rule")
    if not _is_missing(leap_rule) and str(leap_rule) not in ("drop_leap_day", "copy_feb28"):
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_INVALID_LEAP_RULE,
                "error",
                f"unknown leap_day_rule {leap_rule!r}",
                table="interties",
                locator=locator,
            )
        )
    target_year = row.get("target_year")
    if not _is_missing(target_year) and _strict_year_int(target_year) is None:
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_INVALID_TARGET_YEAR,
                "error",
                f"target_year must be a strict integer in 1900..2200, got {target_year!r}",
                table="interties",
                locator=locator,
            )
        )


def _check_specific_weather_year_row(row: pd.Series, issues: list[ValidationIssue]) -> None:
    """DQ-30 per-row checks for specific_weather_year (C901 split;
    overflow-safe)."""
    weather_year = row.get("weather_year")
    if _strict_year_int(weather_year) is None:
        issues.append(
            ValidationIssue(
                IssueCode.DQ30_INVALID_WEATHER_YEAR,
                "error",
                "specific_weather_year requires an integer weather_year (never the string 'ALL')",
                table="interties",
                locator=f"intertie_id={row.get('intertie_id')}",
            )
        )


__all__ = [
    "IssueCode",
    "ValidationError",
    "ValidationIssue",
    "ValidationReport",
    "default_equal_weights",
    "required_columns_for",
    "validate_for_mttf_mttr",
    "validate_intertie_mapping_config",
    "validate_outage_derating_consistency",
    "validate_standard_tables",
    "validate_weather_year_weights",
]


_WEIGHT_SET_COLUMNS = {"weight_set_id", "weather_year", "weight", "source_id"}


def validate_weather_year_weights(
    frame: pd.DataFrame, declared_years: list[int]
) -> list[ValidationIssue]:
    """DQ-21: non-negative, finite, complete coverage, unique keys,
    normalized, sourced weights."""
    issues: list[ValidationIssue] = []
    missing = sorted(_WEIGHT_SET_COLUMNS - set(frame.columns))
    if missing:
        return [
            ValidationIssue(
                IssueCode.WEIGHT_INVALID,
                "error",
                f"missing weight columns: {missing}",
                table="weather_year_weight_sets",
            )
        ]
    if frame.empty:
        return [
            ValidationIssue(
                IssueCode.WEIGHT_INVALID,
                "error",
                "empty weight set",
                table="weather_year_weight_sets",
            )
        ]
    weights = pd.to_numeric(frame["weight"], errors="coerce")
    if weights.isna().any() or (weights < 0).any() or (~weights.map(math.isfinite)).any():
        issues.append(
            ValidationIssue(
                IssueCode.WEIGHT_INVALID,
                "error",
                "negative, missing or non-finite weight",
                table="weather_year_weight_sets",
            )
        )
    if frame.duplicated(subset=["weight_set_id", "weather_year"]).any():
        issues.append(
            ValidationIssue(
                IssueCode.WEIGHT_INVALID,
                "error",
                "duplicate weight_set_id+weather_year",
                table="weather_year_weight_sets",
            )
        )
    for weight_set_id, sub in frame.groupby("weight_set_id"):
        years = set(sub["weather_year"].astype(int))
        if years != set(declared_years):
            issues.append(
                ValidationIssue(
                    IssueCode.WEIGHT_INVALID,
                    "error",
                    f"weight set {weight_set_id} does not cover declared years",
                    table="weather_year_weight_sets",
                    locator=str(weight_set_id),
                )
            )
        if abs(float(sub["weight"].sum()) - 1.0) > 1e-9:
            issues.append(
                ValidationIssue(
                    IssueCode.WEIGHT_INVALID,
                    "error",
                    f"weight set {weight_set_id} not normalized",
                    table="weather_year_weight_sets",
                    locator=str(weight_set_id),
                )
            )
        if sub["source_id"].isna().any() or (sub["source_id"].astype(str).str.strip() == "").any():
            issues.append(
                ValidationIssue(
                    IssueCode.WEIGHT_INVALID,
                    "error",
                    f"weight set {weight_set_id} missing source_id",
                    table="weather_year_weight_sets",
                    locator=str(weight_set_id),
                )
            )
    return issues


def default_equal_weights(years: list[int]) -> pd.DataFrame:
    """Default equal-weight set; generation is traced via source_id."""
    if not years:
        columns = sorted(_WEIGHT_SET_COLUMNS | {"data_status", "note"})
        return pd.DataFrame(columns=columns)
    weight = 1.0 / len(years)
    return pd.DataFrame(
        {
            "weight_set_id": ["equal"] * len(years),
            "weather_year": years,
            "weight": [weight] * len(years),
            "source_id": ["equal_weight_default"] * len(years),
            "data_status": ["assumed"] * len(years),
            "note": ["auto-generated equal weights, traced"] * len(years),
        }
    )


def validate_for_mttf_mttr(
    for_rate: float,
    mttf_hours: float,
    mttr_hours: float,
    tol_for: float,
    authoritative_parameter_set: str | None,
) -> list[ValidationIssue]:
    """DQ-25: FOR/MTTF/MTTR triangle consistency with explicit tolerance.

    ``tol_for`` must be provided by configuration (pending benchmark
    lock); it is never hard-coded here. When an authoritative parameter
    set is confirmed, non-authoritative values only produce a warning.
    """
    values = (for_rate, mttf_hours, mttr_hours, tol_for)
    if (
        not all(math.isfinite(value) for value in values)
        or not 0.0 <= for_rate <= 1.0
        or mttf_hours <= 0.0
        or mttr_hours < 0.0
        or tol_for < 0.0
    ):
        return [
            ValidationIssue(
                IssueCode.FOR_MTTF_MTTR_INCONSISTENT,
                "error",
                "invalid parameter domain: require finite FOR in [0, 1], "
                "MTTF > 0, MTTR >= 0 and tol_for >= 0",
                table="thermal_reliability_classes",
            )
        ]

    derived = mttr_hours / (mttf_hours + mttr_hours)
    inconsistent = abs(for_rate - derived) > tol_for
    if not inconsistent:
        return []
    severity = "warning" if authoritative_parameter_set else "error"
    return [
        ValidationIssue(
            IssueCode.FOR_MTTF_MTTR_INCONSISTENT,
            severity,
            f"FOR {for_rate} vs derived {derived:.6f} outside tol {tol_for}; "
            "authoritative_parameter_set required for G2/G3",
            table="thermal_reliability_classes",
        )
    ]
