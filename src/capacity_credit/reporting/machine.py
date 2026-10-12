"""T23-A: machine-readable outputs (THE single write entry).

write_machine_outputs consumes ONLY:
- an already-validated formal result (read via read_method_result_any,
  MethodResult 1.3 or MriResult2 2.0);
- an optional formal search-trace artifact (read via read_search_trace,
  cross-checked against the result: metric/unit/estimate);
- caller-provided risk events (fixed whitelist + fixed PyArrow schema,
  identity fields are written by the report layer and cannot be
  overridden), source records and reproducibility context (never
  scanned from directories, never guessed).

It writes EXACTLY the seven fixed machine artifacts into the
caller-provided output directory. All seven artifacts are fully built
and pre-checked BEFORE any publish; every publish is atomic
(atomic_write_bytes with immutable=True, fixed order); a lost race is
resolved by re-reading: identical content is idempotent, differing
content raises OUTPUT_EXISTS_DIFFERENT. JSON serialization forbids
non-finite numbers (allow_nan=False); CSV/Parquet inputs are validated
up-front. The source result is never modified, no artifact_manifest is
written and nothing lands in P5 evidence or raw-outputs directories.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from capacity_credit.application.atomic_io import ObjectExistsError, atomic_write_bytes
from capacity_credit.application.result_service import read_method_result_any
from capacity_credit.contracts.enums import RiskMetric
from capacity_credit.contracts.search_trace import read_search_trace
from capacity_credit.domain.risk_units import risk_metric_unit
from capacity_credit.reporting import mapping_v1 as _mapping
from capacity_credit.reporting.mapping_v1 import (
    REPORT_MAPPING_VERSION,
    classify_value,
    conclusion_card_field_ids,
    extract_field,
    get_field,
    unit_for_field,
)

MACHINE_OUTPUT_FILES: tuple[str, ...] = (
    "run_summary.json",
    "capacity_credit_results.csv",
    "adequacy_metrics.csv",
    "risk_events.parquet",
    "search_trace.csv",
    "warnings.json",
    "reproducibility_manifest.json",
)

# W2 (weather-year plan section 3): the OPTIONAL eighth artifact. It is
# written ONLY when the run left a weather_year_metrics sidecar (the
# adequacy publication chain); older runs and non-adequacy runs keep the
# seven fixed artifacts and the report view stays an honest None.
WEATHER_YEAR_METRICS_FILE = "risk_metrics_by_weather_year.csv"

_WEATHER_YEAR_METRIC_COLUMNS: tuple[str, ...] = (
    "lole_days_per_year",
    "lolh_hours_per_year",
    "eue_mwh_per_year",
    "neue_ppm",
    "demand_energy_mwh",
)

_WEATHER_YEAR_COLUMNS: tuple[str, ...] = (
    "weather_year",
    "role",
    "weight",
    "primary_metric_mean",
) + _WEATHER_YEAR_METRIC_COLUMNS + ("sample_count",)

_RISK_METRICS: tuple[RiskMetric, ...] = (
    RiskMetric.LOLE_DAYS_PER_YEAR,
    RiskMetric.LOLH_HOURS_PER_YEAR,
    RiskMetric.EUE_MWH_PER_YEAR,
    RiskMetric.NEUE_PPM,
)

_RISK_FIELD_BY_METRIC: Mapping[RiskMetric, str] = {
    RiskMetric.LOLE_DAYS_PER_YEAR: "lole_days_per_year",
    RiskMetric.LOLH_HOURS_PER_YEAR: "lolh_hours_per_year",
    RiskMetric.EUE_MWH_PER_YEAR: "eue_mwh_per_year",
    RiskMetric.NEUE_PPM: "neue_ppm",
}

# Fixed risk-event whitelist (identity fields are report-layer owned).
# Q-D1：duration_hours＝事件持续小时（正整数），
# 引擎事件原生字段，报告视图据此做分年聚合与跨年加权汇总。
_RISK_EVENT_FIELDS: tuple[str, ...] = (
    "event_time",
    "location",
    "deficiency_power_mw",
    "deficiency_energy_mwh",
    "scenario_id",
    "sample_index",
    "duration_hours",
)
_RISK_EVENT_IDENTITY_FIELDS: tuple[str, ...] = (
    "run_id",
    "result_id",
    "report_mapping_version",
)

_RISK_EVENT_SCHEMA = pa.schema(
    [
        pa.field("run_id", pa.string()),
        pa.field("result_id", pa.string()),
        pa.field("report_mapping_version", pa.string()),
        pa.field("event_time", pa.string()),
        pa.field("location", pa.string()),
        pa.field("deficiency_power_mw", pa.float64()),
        pa.field("deficiency_energy_mwh", pa.float64()),
        pa.field("scenario_id", pa.string()),
        pa.field("sample_index", pa.int64()),
        pa.field("duration_hours", pa.int64()),
    ]
)

# Full-rerun claim requires model-ready data + a verified rebuild chain.
# every item required for a full-rerun claim (strictly validated below)
_FULL_RERUN_REQUIRED: tuple[str, ...] = (
    "inputs_hash",
    "config_hash",
    "software_version",
    "random_seed",
    "model_ready_data",
    "scenario_bank_id",
    "data_source_records",
)


class MachineOutputError(ValueError):
    """Stable machine-output error with a machine code (never raw
    underlying text as the contract)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _enum_value(value: Any) -> str:
    """Formal enum member -> machine string value (str passthrough)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    member_value = getattr(value, "value", None)
    if member_value is not None:
        return str(member_value)
    return str(value)


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return True


def _is_valid_source_record(record: Any) -> bool:
    """A structurally valid source record is a non-empty dict carrying
    at least one non-blank string value ([{}] does NOT qualify)."""
    if not isinstance(record, dict):
        return False
    if not record:
        return False
    return any(
        isinstance(value, str) and bool(value.strip())
        for value in record.values()
    )


def _is_sha256(value: Any) -> bool:
    """Structurally valid SHA-256 hex digest (64 hex chars)."""
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _is_non_blank_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_strict_int(value: Any) -> bool:
    """Strict integer (bool is NOT an integer here)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_json_input(value: Any, label: str) -> None:
    """THE single recursive JSON-input check (T23-A round 3): covers
    Mapping / list / JSON scalar types at ANY depth. Mapping keys must
    be strings; int/float must be finite; bool is allowed as a JSON
    boolean (strict per-field contracts still apply downstream, e.g.
    random_seed / risk-event numerics); set, Path and other unsupported
    objects are a stable INPUT_INVALID_JSON_TYPE - never a raw
    TypeError."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MachineOutputError(
                    "INPUT_INVALID_JSON_TYPE",
                    f"{label} has a non-string key {key!r}",
                )
            _check_json_input(item, f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_input(item, f"{label}[{index}]")
        return
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise MachineOutputError(
                "INPUT_NON_FINITE", f"{label} is not a finite number"
            )
        return
    raise MachineOutputError(
        "INPUT_INVALID_JSON_TYPE",
        f"{label} has unsupported JSON type {type(value).__name__}",
    )


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
    except ValueError as exc:
        raise MachineOutputError(
            "OUTPUT_NON_FINITE",
            f"JSON serialization rejected a non-finite number: {exc}",
        ) from exc
    return (text + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _preflight_existing(path: Path, data: bytes, label: str) -> None:
    """Pre-publish check: existing identical content is idempotent,
    differing content is rejected BEFORE any file is touched."""
    if path.exists():
        if path.read_bytes() == data:
            return
        raise MachineOutputError(
            "OUTPUT_EXISTS_DIFFERENT",
            f"{label} exists with different content: {path}",
        )


def _publish_immutable(path: Path, data: bytes, label: str) -> None:
    """Publish ONE artifact atomically (immutable create-if-not-exists).
    A lost race is resolved by re-reading: identical -> idempotent,
    differing -> OUTPUT_EXISTS_DIFFERENT (never a silent overwrite)."""
    try:
        atomic_write_bytes(path, data, immutable=True)
    except ObjectExistsError:
        existing = path.read_bytes()
        if existing == data:
            return
        raise MachineOutputError(
            "OUTPUT_EXISTS_DIFFERENT",
            f"{label} exists with different content: {path}",
        ) from None
    except OSError as exc:
        raise MachineOutputError(
            "OUTPUT_WRITE_FAILED", f"{label} write failed: {path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------
# artifact builders (pure, deterministic)
# ---------------------------------------------------------------------


def _result_status_text(result: BaseModel) -> str:
    return _enum_value(getattr(result, "result_status", None))


def _primary_metric(result: BaseModel) -> str:
    return _enum_value(getattr(result, "primary_risk_metric", None))


def _role_risk_values(result: BaseModel, role: str) -> dict[str, float | None] | None:
    risk_metrics = getattr(result, "risk_metrics", None)
    if risk_metrics is None:
        return None
    role_metrics = getattr(risk_metrics, role, None)
    if role_metrics is None:
        return None
    return {
        metric.value: float(getattr(role_metrics, _RISK_FIELD_BY_METRIC[metric]))
        for metric in _RISK_METRICS
    }


def _build_run_summary(
    result: BaseModel,
    *,
    trace_status: str,
    risk_event_status: str,
    source_record_status: str,
    seed_replication_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every mapped top-level field comes from the run_summary mapping
    projection (summary_field_ids) resolved through resolve_field - no
    private getattr table."""
    summary: dict[str, Any] = {
        "report_mapping_version": REPORT_MAPPING_VERSION,
        **{
            field_id: _mapping.resolve_field(field_id, result)
            for field_id in _mapping.summary_field_ids()
        },
        "conclusion_card": {
            field_id: {
                "label": get_field(field_id).label,
                "value": extract_field(result, field_id),
                "unit": unit_for_field(field_id, result),
                "null_status": classify_value(field_id, result),
            }
            for field_id in conclusion_card_field_ids()
        },
        "diagnostics_summary": _diagnostics_summary(result),
        "source_completeness": {
            "search_trace": trace_status,
            "risk_events": risk_event_status,
            "seed_replication": (
                "present"
                if seed_replication_summary is not None
                else "missing"
            ),
            "source_records": source_record_status,
        },
    }
    if seed_replication_summary is not None:
        estimate = (seed_replication_summary.get("estimate_mw") or {})
        gate = seed_replication_summary.get("gate") or {}
        summary["seed_replication_review"] = {
            "schema_version": seed_replication_summary.get(
                "seed_replication_schema_version"
            ),
            "group_count": seed_replication_summary.get("group_count"),
            "valid_count": seed_replication_summary.get("valid_count"),
            "estimate_mw_median": estimate.get("median"),
            "ci95_lower": estimate.get("ci95_lower"),
            "ci95_upper": estimate.get("ci95_upper"),
            "ci_method": estimate.get("ci_method"),
            "credit_fraction_iqr_pp": seed_replication_summary.get(
                "credit_fraction_iqr_pp"
            ),
            "gate_passed": gate.get("passed"),
            "gate_reasons": list(gate.get("reasons") or []),
            "provisional_status": seed_replication_summary.get(
                "provisional_status"
            ),
        }
    return summary


def _diagnostics_summary(result: BaseModel) -> dict[str, Any]:
    diagnostics = list(getattr(result, "diagnostics", []) or [])
    counts = {"info": 0, "warning": 0, "error": 0}
    blocking: list[str] = []
    for diagnostic in diagnostics:
        severity = _enum_value(getattr(diagnostic, "severity", None))
        if severity in counts:
            counts[severity] += 1
        if severity == "error":
            blocking.append(str(getattr(diagnostic, "code", "")))
    return {"counts": counts, "blocking": blocking}


def _build_capacity_credit_row(result: BaseModel) -> dict[str, Any]:
    """Every column is a REPORT_FIELDS machine field (id order from the
    mapping projection), resolved through the unified resolve_field
    entry - derivation rules live in mapping_v1, never here."""
    row: dict[str, Any] = {}
    for field_id in _mapping.machine_result_field_ids("capacity_credit_results"):
        row[field_id] = _csv_value(_mapping.resolve_field(field_id, result))
    return row


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _build_adequacy_metric_rows(result: BaseModel) -> list[dict[str, Any]]:
    """Long-table rows: role/metric/value/unit/primary_metric per role,
    plus the shared identity fields (run_id / result_id / result_status)
    resolved through mapping_v1.resolve_field on EVERY row - never
    DictWriter-padded blanks."""
    primary = _primary_metric(result)
    identity = {
        field_id: _csv_value(_mapping.resolve_field(field_id, result))
        for field_id in ("run_id", "result_id", "result_status")
    }
    rows: list[dict[str, Any]] = []
    for role in ("base", "candidate", "matched"):
        values = _role_risk_values(result, role)
        if values is None:
            continue
        for metric in _RISK_METRICS:
            rows.append(
                {
                    **identity,
                    "role": role,
                    "metric": metric.value,
                    "value": _csv_value(values[metric.value]),
                    "unit": risk_metric_unit(metric),
                    "primary_metric": "yes" if primary == metric.value else "no",
                }
            )
    return rows


def _build_weather_year_metrics_rows(
    rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """W2: validate and normalize the run's per-year metric rows into
    the fixed CSV shape. Stress years carry their primary mean only -
    their metric columns stay empty and their weight stays empty (they
    never enter the weighted sum). Returns None for an absent/empty
    input (the eighth artifact is then simply not written)."""
    if not rows:
        return None
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        year = row.get("weather_year")
        role = str(row.get("role") or "")
        if not _is_strict_int(year):
            raise MachineOutputError(
                "WEATHER_YEAR_METRICS_INVALID",
                f"rows[{index}].weather_year must be a strict integer",
            )
        if role not in ("probability_baseline", "stress_test"):
            raise MachineOutputError(
                "WEATHER_YEAR_METRICS_INVALID",
                f"rows[{index}].role must be probability_baseline|stress_test",
            )
        normalized: dict[str, Any] = {
            "weather_year": int(year),
            "role": role,
            "weight": "",
            "primary_metric_mean": "",
        }
        weight = row.get("weight")
        if weight is not None:
            if not _finite_number(weight) or float(weight) < 0.0:
                raise MachineOutputError(
                    "WEATHER_YEAR_METRICS_INVALID",
                    f"rows[{index}].weight must be a finite non-negative number",
                )
            normalized["weight"] = float(weight)
        mean = row.get("primary_metric_mean")
        if mean is not None:
            if not _finite_number(mean):
                raise MachineOutputError(
                    "WEATHER_YEAR_METRICS_INVALID",
                    f"rows[{index}].primary_metric_mean must be finite",
                )
            normalized["primary_metric_mean"] = float(mean)
        for column in _WEATHER_YEAR_METRIC_COLUMNS:
            value = row.get(column)
            if value is not None:
                if not _finite_number(value):
                    raise MachineOutputError(
                        "WEATHER_YEAR_METRICS_INVALID",
                        f"rows[{index}].{column} must be finite or null",
                    )
                value = float(value)
            normalized[column] = "" if value is None else value
        count = row.get("sample_count")
        if not _is_strict_int(count):
            raise MachineOutputError(
                "WEATHER_YEAR_METRICS_INVALID",
                f"rows[{index}].sample_count must be a strict integer",
            )
        normalized["sample_count"] = int(count)
        out.append(normalized)
    return out


def _build_risk_event_table(
    result: BaseModel, risk_events: list[dict[str, Any]] | None
) -> pa.Table:
    """Fixed whitelist + fixed PyArrow schema for EMPTY and NON-EMPTY
    tables alike. Identity fields are written by the report layer;
    callers cannot override them and unknown fields are rejected."""
    if risk_events is None:
        risk_events = []
    rows: list[dict[str, Any]] = []
    for index, event in enumerate(risk_events):
        for key in event:
            if key in _RISK_EVENT_IDENTITY_FIELDS:
                raise MachineOutputError(
                    "RISK_EVENT_IDENTITY_OVERRIDE",
                    f"risk event [{index}] must not override {key!r}",
                )
            if key not in _RISK_EVENT_FIELDS:
                raise MachineOutputError(
                    "RISK_EVENT_UNKNOWN_FIELD",
                    f"risk event [{index}] carries unknown field {key!r}",
                )
        # every whitelist field must be EXPLICITLY provided (no silent
        # defaults - missing fields are a stable error)
        for key in _RISK_EVENT_FIELDS:
            if key not in event:
                raise MachineOutputError(
                    "RISK_EVENT_MISSING_FIELD",
                    f"risk event [{index}] is missing field {key!r}",
                )
        for key in ("event_time", "location", "scenario_id"):
            value = event[key]
            if not isinstance(value, str) or not value.strip():
                raise MachineOutputError(
                    "RISK_EVENT_INVALID_TYPE",
                    f"risk event [{index}].{key} must be a non-blank string",
                )
        for key in ("deficiency_power_mw", "deficiency_energy_mwh"):
            value = event[key]
            # bool is NOT a number; must be finite and non-negative
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MachineOutputError(
                    "RISK_EVENT_INVALID_TYPE",
                    f"risk event [{index}].{key} must be a real number",
                )
            if not math.isfinite(float(value)) or float(value) < 0.0:
                raise MachineOutputError(
                    "RISK_EVENT_INVALID_TYPE",
                    f"risk event [{index}].{key} must be finite and non-negative",
                )
        sample_index = event["sample_index"]
        if isinstance(sample_index, bool) or not isinstance(sample_index, int):
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"risk event [{index}].sample_index must be a strict non-negative "
                "integer (no truncation)",
            )
        if sample_index < 0:
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"risk event [{index}].sample_index must be non-negative",
            )
        duration = event["duration_hours"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration <= 0
        ):
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"risk event [{index}].duration_hours must be a positive integer",
            )
        rows.append(
            {
                "run_id": str(getattr(result, "run_id", "")),
                "result_id": str(getattr(result, "result_id", "")),
                "report_mapping_version": REPORT_MAPPING_VERSION,
                "event_time": event["event_time"],
                "location": event["location"],
                "deficiency_power_mw": float(event["deficiency_power_mw"]),
                "deficiency_energy_mwh": float(event["deficiency_energy_mwh"]),
                "scenario_id": event["scenario_id"],
                "sample_index": sample_index,
                "duration_hours": duration,
            }
        )
    return pa.Table.from_pylist(rows, schema=_RISK_EVENT_SCHEMA)


def risk_event_rows_from_deficit_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """R3-B1: map engine-shaped deficit-event dicts (adequacy
    ``_event_to_dict``) to the machine-output whitelist rows.

    Honest mapping of what the engine records: the event start
    timestamp, the system-wide location (the reliability model is a
    single aggregate area - no locational split exists to claim), the
    weather year as the scenario label, and the parsed sample index.
    Raises MachineOutputError on unknown/missing/mistyped event fields
    (stably, with the offending index) - never a guessed row."""
    rows: list[dict[str, Any]] = []
    required = (
        "sample_id",
        "weather_year",
        "start_time",
        "duration_hours",
        "event_eue_mwh",
        "max_unserved_power_mw",
    )
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"deficit event [{index}] must be a dict, got {type(event).__name__}",
            )
        missing = [key for key in required if key not in event]
        if missing:
            raise MachineOutputError(
                "RISK_EVENT_MISSING_FIELD",
                f"deficit event [{index}] is missing field(s) {missing}",
            )
        sample_id = str(event["sample_id"])
        if not sample_id.startswith("s") or not sample_id[1:].isdigit():
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"deficit event [{index}].sample_id {sample_id!r} is not an s<N> id",
            )
        weather_year = event["weather_year"]
        if isinstance(weather_year, bool) or not isinstance(weather_year, int):
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"deficit event [{index}].weather_year must be a strict integer",
            )
        duration = event["duration_hours"]
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"deficit event [{index}].duration_hours must be a positive integer",
            )
        for key in ("event_eue_mwh", "max_unserved_power_mw"):
            value = event[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MachineOutputError(
                    "RISK_EVENT_INVALID_TYPE",
                    f"deficit event [{index}].{key} must be a real number",
                )
            if not math.isfinite(float(value)) or float(value) < 0.0:
                raise MachineOutputError(
                    "RISK_EVENT_INVALID_TYPE",
                    f"deficit event [{index}].{key} must be finite and non-negative",
                )
        start = str(event["start_time"])
        try:
            datetime.fromisoformat(start)
        except ValueError:
            raise MachineOutputError(
                "RISK_EVENT_INVALID_TYPE",
                f"deficit event [{index}].start_time {start!r} is not ISO-8601",
            ) from None
        rows.append(
            {
                "event_time": start,
                "location": "system_wide",
                "deficiency_power_mw": float(event["max_unserved_power_mw"]),
                "deficiency_energy_mwh": float(event["event_eue_mwh"]),
                "scenario_id": f"weather_year_{weather_year}",
                "sample_index": int(sample_id[1:]),
                "duration_hours": int(duration),
            }
        )
    return rows


def build_gap_profile(
    events: list[dict[str, Any]],
    *,
    source: str,
    sample_count: int | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """R3-B1: the month×hour gap picture (方法说明第三章(六) 第一步
    交付物之三) aggregated from engine deficit-event dicts.

    Recomputable, non-fabricated semantics: ``event_hours`` counts the
    month×hour cells an event COVERS (start + i hours, i<duration);
    ``event_count`` and ``eue_mwh`` are booked ONLY into the event's
    START cell - the within-event hourly energy split is unknown, so it
    is never distributed. Empty events yield an all-zero profile (an
    honest zero-loss picture), never a missing artifact."""
    from collections import defaultdict

    cells: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    by_year: dict[str, dict[str, float]] = defaultdict(
        lambda: {"event_count": 0, "event_hours": 0, "eue_mwh": 0.0}
    )
    total_event_hours = 0
    total_eue = 0.0
    for event in events:
        required = ("sample_id", "weather_year", "start_time", "duration_hours", "event_eue_mwh")
        missing = [key for key in required if key not in event]
        if missing:
            raise MachineOutputError(
                "RISK_EVENT_MISSING_FIELD",
                f"deficit event is missing field(s) {missing}",
            )
        start = datetime.fromisoformat(str(event["start_time"]))
        duration = int(event["duration_hours"])
        weather_year = str(event["weather_year"])
        eue = float(event["event_eue_mwh"])
        for offset in range(duration):
            hour_ts = start + timedelta(hours=offset)
            month_key = str(hour_ts.month)
            hour_key = str(hour_ts.hour)
            cell = cells[month_key].setdefault(
                hour_key, {"event_hours": 0}
            )
            cell["event_hours"] += 1
            total_event_hours += 1
        month_key = str(start.month)
        hour_key = str(start.hour)
        start_cell = cells[month_key].setdefault(
            hour_key, {"event_hours": 0}
        )
        start_cell["event_count"] = start_cell.get("event_count", 0) + 1
        start_cell["eue_mwh"] = start_cell.get("eue_mwh", 0.0) + eue
        year_row = by_year[weather_year]
        year_row["event_count"] += 1
        year_row["event_hours"] += duration
        year_row["eue_mwh"] = year_row["eue_mwh"] + eue
        total_eue += eue
    return {
        "gap_profile_schema_version": "1.0",
        "source": str(source),
        "sample_count": sample_count,
        "event_count": len(events),
        "events_truncated": bool(truncated),
        "total_event_hours": total_event_hours,
        "total_eue_mwh": total_eue,
        "cells": {m: dict(cells[m]) for m in sorted(cells, key=int)},
        "by_weather_year": {y: by_year[y] for y in sorted(by_year)},
        "attribution_note": (
            "event_hours 按事件覆盖的小时格累计；event_count 与 eue_mwh 记入"
            "事件起始小时格（事件内部逐时能量分布未知，不做分配，不伪造）。"
            "计数跨样本累计（模拟年事件，未按样本数平均）。"
        ),
    }


def _validate_search_trace(result: BaseModel, trace_path: Path) -> Any:
    """Stable errors for an explicitly-provided-but-broken trace."""
    if not trace_path.exists():
        raise MachineOutputError(
            "SEARCH_TRACE_FILE_MISSING",
            f"search trace file does not exist: {trace_path}",
        )
    trace = read_search_trace(trace_path)
    result_metric = _primary_metric(result)
    if _enum_value(trace.primary_risk_metric) != result_metric:
        raise MachineOutputError(
            "SEARCH_TRACE_METRIC_MISMATCH",
            f"search trace primary_risk_metric={trace.primary_risk_metric.value!r} "
            f"conflicts with result {result_metric!r}",
        )
    result_unit = unit_for_field("base_risk", result)
    if trace.risk_metric_unit != result_unit:
        raise MachineOutputError(
            "SEARCH_TRACE_METRIC_MISMATCH",
            f"search trace risk_metric_unit={trace.risk_metric_unit!r} "
            f"conflicts with result unit {result_unit!r}",
        )
    # full binding: status + both convergence states must match the
    # formal result; a not_converged result never accepts a valid /
    # converged trace carrying an estimate
    result_status = _result_status_text(result)
    result_risk_conv = _enum_value(getattr(result, "risk_convergence", None))
    result_search_conv = _enum_value(getattr(result, "search_convergence", None))
    if (
        _enum_value(trace.result_status) != result_status
        or _enum_value(trace.risk_convergence) != result_risk_conv
        or _enum_value(trace.search_convergence) != result_search_conv
    ):
        raise MachineOutputError(
            "SEARCH_TRACE_STATUS_MISMATCH",
            f"search trace status/convergence "
            f"({trace.result_status.value}/{trace.risk_convergence.value}/"
            f"{trace.search_convergence.value}) conflicts with the formal "
            f"result ({result_status}/{result_risk_conv}/{result_search_conv})",
        )
    scenario_bank_id = getattr(result, "scenario_bank_id", None)
    for point in trace.points:
        if point.scenario_bank_id != scenario_bank_id:
            raise MachineOutputError(
                "SEARCH_TRACE_SCENARIO_MISMATCH",
                f"search trace point call_id={point.call_id} uses scenario bank "
                f"{point.scenario_bank_id!r} but the formal result declares "
                f"{scenario_bank_id!r}",
            )
    accredited = getattr(result, "accredited_capacity_mw", None)
    if (
        accredited is not None
        and trace.estimate_mw is not None
        and abs(trace.estimate_mw - float(accredited)) > 1e-6 * max(1.0, float(accredited))
    ):
            raise MachineOutputError(
                "SEARCH_TRACE_ESTIMATE_MISMATCH",
                f"search trace estimate_mw={trace.estimate_mw} does not locate "
                f"the final accredited capacity {accredited}",
            )
    return trace


def _build_search_trace_csv(
    result: BaseModel, trace_path: Path | None
) -> bytes:
    header = [
        "report_mapping_version",
        "run_id",
        "result_id",
        "call_id",
        "iteration",
        "action",
        "capacity_mw",
        "reference_risk",
        "trial_risk",
        "difference_mean",
        "difference_se",
        "sample_count",
        "bracket_low_after",
        "bracket_high_after",
        "risk_convergence",
        "estimate_mw",
        "trace_status",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    if trace_path is not None:
        trace = _validate_search_trace(result, trace_path)
        run_id = str(getattr(result, "run_id", ""))
        result_id = str(getattr(result, "result_id", ""))
        for point in trace.points:
            writer.writerow(
                [
                    REPORT_MAPPING_VERSION,
                    run_id,
                    result_id,
                    point.call_id,
                    point.iteration,
                    point.action,
                    _csv_value(point.capacity_mw),
                    _csv_value(point.reference_risk),
                    _csv_value(point.trial_risk),
                    _csv_value(point.difference_mean),
                    _csv_value(point.difference_se),
                    point.sample_count,
                    _csv_value(point.bracket_low_after),
                    _csv_value(point.bracket_high_after),
                    _enum_value(point.risk_convergence),
                    _csv_value(trace.estimate_mw),
                    "present",
                ]
            )
    return buffer.getvalue().encode("utf-8")


def _build_warnings(result: BaseModel, *, trace_status: str) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    blocking: list[str] = []
    for diagnostic in getattr(result, "diagnostics", []) or []:
        severity = _enum_value(getattr(diagnostic, "severity", None))
        code = str(getattr(diagnostic, "code", ""))
        if severity in ("warning", "error"):
            warnings.append(
                {
                    "code": code,
                    "severity": severity,
                    "message": str(getattr(diagnostic, "message", "")),
                }
            )
        if severity == "error":
            blocking.append(code)
    report_diagnostics: list[dict[str, str]] = []
    if trace_status == "missing":
        report_diagnostics.append(
            {
                "code": "REPORT_SEARCH_TRACE_MISSING",
                "severity": "info",
                "message": "no formal search trace was provided to the report layer",
            }
        )
    return {
        "report_mapping_version": REPORT_MAPPING_VERSION,
        "run_id": str(getattr(result, "run_id", "")),
        "result_id": str(getattr(result, "result_id", "")),
        "warnings": warnings,
        "blocking_diagnostics": blocking,
        "report_diagnostics": report_diagnostics,
    }


def _build_reproducibility_manifest(
    result: BaseModel,
    *,
    reproducibility_context: dict[str, Any] | None,
    source_records: list[dict[str, Any]] | None,
    output_hashes: Mapping[str, str],
) -> dict[str, Any]:
    ctx = reproducibility_context or {}
    records: list[dict[str, Any]] = list(source_records or [])
    if not records and ctx.get("data_source_records"):
        records = list(ctx["data_source_records"])
    missing: list[str] = []
    scenario_bank_id = getattr(result, "scenario_bank_id", None)
    if not _is_non_blank_str(scenario_bank_id):
        missing.append("scenario_bank_id")
    # inputs/config hashes must be STRUCTURALLY valid SHA-256 digests
    if not _is_sha256(ctx.get("inputs_hash")):
        missing.append("inputs_hash")
    if not _is_sha256(ctx.get("config_hash")):
        missing.append("config_hash")
    if not _is_non_blank_str(ctx.get("software_version")):
        missing.append("software_version")
    # random_seed must be a STRICT integer (bool is rejected)
    if not _is_strict_int(ctx.get("random_seed")):
        missing.append("random_seed")
    # model_ready_data must be the BOOLEAN True - False/0/other types
    # never qualify for a full-rerun claim
    if ctx.get("model_ready_data") is not True:
        missing.append("model_ready_data")
    # data_source_records must be non-empty AND structurally valid
    # ([{}] is not a source record)
    if not records or not all(_is_valid_source_record(r) for r in records):
        missing.append("data_source_records")
    # stable order, no duplicates (single source of missing items)
    missing = list(dict.fromkeys(missing))
    # T23-A never claims T24's rerun_bundle: a full-rerun capability is
    # declared ONLY with explicit model-ready data + verified rebuild
    # inputs; otherwise verify_only with the missing items listed.
    rerun_capability = "full_rerun" if not missing else "verify_only"
    manifest: dict[str, Any] = {
        "report_mapping_version": REPORT_MAPPING_VERSION,
        "run_id": str(getattr(result, "run_id", "")),
        "result_id": str(getattr(result, "result_id", "")),
        "result_schema_version": str(getattr(result, "result_schema_version", "")),
        "method": _enum_value(getattr(result, "method", None)),
        "method_profile": str(getattr(result, "method_profile", "")),
        "scenario_bank_id": scenario_bank_id,
        "data_source_records": records,
        # The manifest intentionally does NOT contain its own hash: the
        # outputs list covers the six preceding artifacts only.
        "outputs_self_hash_excluded": True,
        "outputs": dict(sorted(output_hashes.items())),
        "rerun_capability": rerun_capability,
        "missing_reproducibility_inputs": missing,
    }
    for key in ("source_run_id", "inputs_hash", "config_hash", "software_version", "random_seed", "model_ready_data"):
        if ctx.get(key) is not None:
            manifest[key] = ctx[key]
    return manifest


# ---------------------------------------------------------------------
# THE single public write entry
# ---------------------------------------------------------------------


def write_machine_outputs(
    result_path: Path | str,
    output_dir: Path | str,
    *,
    search_trace_path: Path | str | None = None,
    risk_events: list[dict[str, Any]] | None = None,
    source_records: list[dict[str, Any]] | None = None,
    reproducibility_context: dict[str, Any] | None = None,
    seed_replication_summary: dict[str, Any] | None = None,
    weather_year_metrics: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Write the fixed machine artifacts for ONE formal result.

    Caller-provided inputs are validated up-front (finite numbers,
    risk-event whitelist); all artifacts are built and pre-checked
    BEFORE any publish; publishes are atomic in the fixed
    MACHINE_OUTPUT_FILES order (plus the OPTIONAL W2 eighth artifact
    ``risk_metrics_by_weather_year.csv``, written only when the run
    left per-year metric rows) with immutable semantics. Returns
    {file_name: sha256}.
    """
    if reproducibility_context is not None:
        _check_json_input(reproducibility_context, "reproducibility_context")
    if source_records is not None:
        for index, record in enumerate(source_records):
            _check_json_input(record, f"source_records[{index}]")
    if risk_events is not None:
        for index, event in enumerate(risk_events):
            _check_json_input(event, f"risk_events[{index}]")

    result = read_method_result_any(result_path)
    output = Path(output_dir)
    method = _enum_value(getattr(result, "method", None))
    trace_path = Path(search_trace_path) if search_trace_path is not None else None
    if method == "adequacy":
        trace_status = "not_applicable"
    elif trace_path is not None:
        # explicit path -> validate now (missing/broken trace is an error)
        _validate_search_trace(result, trace_path)
        trace_status = "present"
    else:
        trace_status = "missing"
    risk_event_status = "present" if risk_events is not None else "missing"
    source_record_status = (
        "present"
        if (source_records or (reproducibility_context or {}).get("data_source_records"))
        else "missing"
    )

    if seed_replication_summary is not None:
        _check_json_input(seed_replication_summary, "seed_replication_summary")
    summary = _build_run_summary(
        result,
        trace_status=trace_status,
        risk_event_status=risk_event_status,
        source_record_status=source_record_status,
        seed_replication_summary=seed_replication_summary,
    )
    capacity_columns = ["report_mapping_version"] + list(
        _mapping.machine_result_field_ids("capacity_credit_results")
    )
    capacity_rows = [
        {"report_mapping_version": REPORT_MAPPING_VERSION, **_build_capacity_credit_row(result)}
    ]
    _require_full_row_coverage(capacity_rows, capacity_columns, "capacity_credit_results")
    adequacy_columns = ["report_mapping_version"] + list(
        _mapping.machine_result_field_ids("adequacy_metrics")
    )
    adequacy_rows = [
        {"report_mapping_version": REPORT_MAPPING_VERSION, **row}
        for row in _build_adequacy_metric_rows(result)
    ]
    _require_full_row_coverage(adequacy_rows, adequacy_columns, "adequacy_metrics")
    risk_table = _build_risk_event_table(result, risk_events)
    weather_year_rows = _build_weather_year_metrics_rows(weather_year_metrics)

    # 1) build ALL seven artifacts IN MEMORY first (including the
    # reproducibility manifest, whose content depends on the six
    # preceding hashes - computed from bytes, not from publishes)
    artifacts: dict[str, bytes] = {
        "run_summary.json": _json_bytes(summary),
        "capacity_credit_results.csv": _csv_bytes(capacity_rows, capacity_columns),
        "adequacy_metrics.csv": _csv_bytes(adequacy_rows, adequacy_columns),
        "risk_events.parquet": _parquet_bytes(risk_table),
        "search_trace.csv": _build_search_trace_csv(result, trace_path),
        "warnings.json": _json_bytes(_build_warnings(result, trace_status=trace_status)),
    }
    # W2: the optional eighth artifact rides AFTER adequacy_metrics so
    # the fixed seven keep their order and old directories stay valid
    if weather_year_rows is not None:
        artifacts[WEATHER_YEAR_METRICS_FILE] = _csv_bytes(
            weather_year_rows, list(_WEATHER_YEAR_COLUMNS)
        )
    output_hashes = {
        name: _sha256(data) for name, data in artifacts.items()
    }
    manifest = _build_reproducibility_manifest(
        result,
        reproducibility_context=reproducibility_context,
        source_records=source_records,
        output_hashes=output_hashes,
    )
    artifacts["reproducibility_manifest.json"] = _json_bytes(manifest)
    output_hashes["reproducibility_manifest.json"] = _sha256(artifacts["reproducibility_manifest.json"])

    # 2) pre-check EVERY artifact (including the manifest) against the
    # existing files BEFORE any publish - no partial directories
    for name, data in artifacts.items():
        _preflight_existing(output / name, data, name)

    # 3) publish in the fixed order with immutable semantics. The
    # in-progress marker identifies THIS writer's own attempt (night-4
    # R05): a crash mid-publish leaves it behind, so the facade can tell
    # a recoverable partial directory from foreign tampering; success
    # removes it. The marker never enters MACHINE_OUTPUT_FILES or any
    # hash.
    marker = output / ".write_in_progress"
    atomic_write_bytes(marker, b"write-in-progress")
    # a failure here leaves the marker in place (it is only unlinked on
    # success), so the partial directory stays identifiable as this
    # writer's own failed attempt
    for name, data in artifacts.items():
        _publish_immutable(output / name, data, name)
    marker.unlink()
    return dict(sorted(output_hashes.items()))


def _require_full_row_coverage(
    rows: list[dict[str, Any]], columns: list[str], label: str
) -> None:
    """Every row must cover EVERY projected column BEFORE serialization -
    a missing cell is a stable error, never a DictWriter blank."""
    for index, row in enumerate(rows):
        missing = [column for column in columns if column not in row]
        if missing:
            raise MachineOutputError(
                "ROW_MISSING_COLUMN",
                f"{label} row {index} is missing projected columns: {missing}",
            )


def _csv_bytes(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink)  # type: ignore[no-untyped-call]
    return sink.getvalue()


# R3-B2: version-controlled method-boundary declarations (方法说明第八章
# 风险3 - undeclared assumptions are the primary review challenge). These
# are the boundaries of THIS implementation, stated in one place and
# carried verbatim into every run's assumption register. Update this list
# together with the methodology docs (MR-04 replaces the first entry).
DECLARED_METHOD_LIMITATIONS: tuple[str, ...] = (
    "应急程序顺序启用已建模：储能优先，再按 emergency_programs 表行序由需求响应/有序用电填补，仍不足记失负荷；未提供该表的项目失负荷口径未扣减、偏保守",
    "机组状态为小时离散化两状态马尔可夫链（转移概率法），短于 1 小时的故障/修复不可表达",
    "可靠性模型为全网单区聚合，分区/节点落点未实现（落点固定为 system_wide）",
    "EFC 为完美容量替代法变体（R(S+F,L)=R(S+X,L)），冬季晚峰判据/降额系数表/交付年已按 MR-01 实施（窗口与组数参数仍 provisional）",
    "收敛参数默认值为教学级 provisional（min_samples/batch_size 等），正式身份须另行冻结生产默认并做基准锁定",
    "容量信用点值为单种子结果；多种子稳健性复核按 MR-03 实施（缺门时结果携带 SEED_REPLICATION_GATE_MISSING，教学链豁免）",
    "储能调度策略对结果影响显著（方法说明第八章），所采用的策略与 SOC 边界模式以冻结 run_config 为准；周期末回充窗口默认 disabled（W2）",
)


def build_assumption_register(
    *,
    run_config_payload: dict[str, Any],
    diagnostics: list[dict[str, Any]] | None = None,
    extra_limitations: list[str] | None = None,
) -> dict[str, Any]:
    """R3-B2: the assumption register (方法说明第八章 风险3) assembled
    from SINGLE fact sources only - the frozen run-config document
    VERBATIM (no re-interpretation, no cherry-picking), the result's own
    structured diagnostics, and the version-controlled declared
    method-boundary list above. Nothing is invented here."""
    return {
        "assumptions_schema_version": "1.0",
        "frozen_run_config": dict(run_config_payload or {}),
        "assumption_diagnostics": list(diagnostics or []),
        "declared_limitations": [
            *DECLARED_METHOD_LIMITATIONS,
            *(extra_limitations or []),
        ],
        "attribution_note": (
            "frozen_run_config 为发布时冻结配置全文（单一事实源）；"
            "assumption_diagnostics 为结果对象自带诊断码；"
            "declared_limitations 为版本受控的方法边界声明（与主责方法文档同步维护）。"
        ),
    }


# ---------------------------------------------------------------------
# minimal CLI (python -m capacity_credit.reporting.machine)
# ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="capacity_credit.reporting.machine",
        description="Write the seven fixed machine-readable outputs for one formal result.",
    )
    parser.add_argument("--result", required=True, help="path to method_result.json (1.3 or 2.0)")
    parser.add_argument("--output-dir", required=True, help="caller-provided output directory")
    parser.add_argument("--search-trace", default=None, help="optional formal search-trace artifact")
    parser.add_argument("--context", default=None, help="optional reproducibility context JSON file")
    args = parser.parse_args(argv)
    context: dict[str, Any] | None = None
    if args.context:
        try:
            context = json.loads(Path(args.context).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"MACHINE_OUTPUT_ERROR CONTEXT_INVALID: {exc}", file=sys.stderr)
            return 2
    try:
        hashes = write_machine_outputs(
            args.result,
            args.output_dir,
            search_trace_path=args.search_trace,
            reproducibility_context=context,
        )
    except MachineOutputError as exc:
        print(f"MACHINE_OUTPUT_ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    for name, digest in hashes.items():
        print(f"{name} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
