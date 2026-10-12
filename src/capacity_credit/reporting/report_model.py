"""T24-A: report input loading/validation + the SHARED report view model.

Independent post-processing layer: consumes ONLY the seven T23-A
machine-readable artifacts. Validation runs ONCE here (identity,
mapping version, hashes, JSON/CSV/Parquet legality, required identity
fields, field types, in-directory paths); the Excel renderer and the
HTML renderer consume the SAME ReportView - there is exactly one field
interpretation. No compute engine is ever touched.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from capacity_credit.reporting.machine import (
    MACHINE_OUTPUT_FILES,
    WEATHER_YEAR_METRICS_FILE,
)

REPORT_EXPORT_SCHEMA_VERSION = "1.0"
REPORT_GENERATOR_VERSION = "t24a-1.1"
SUPPORTED_REPORT_MAPPING_VERSIONS: tuple[str, ...] = ("1.0", "1.1")
RISK_EVENT_DISPLAY_LIMIT = 200
SEARCH_TRACE_DISPLAY_LIMIT = 1000

# Formal result statuses (valid family) vs diagnostic family.
_VALID_STATUSES = frozenset({"valid", "valid_with_warning"})


class ReportExportError(ValueError):
    """Stable report-export error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceFileRecord:
    name: str
    sha256: str
    size: int


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """Streaming SHA-256 (64 KiB chunks) - never loads large artifacts
    into memory at once."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class DisplayField:
    """ONE display field shared by Excel and HTML: label/unit/display
    value/number format/null semantics all originate from mapping_v1."""

    field_id: str
    label: str
    raw_value: Any
    display_value: str
    display_unit: str | None
    null_status: str
    number_format: str | None


@dataclass(frozen=True)
class TableView:
    source: str
    total_rows: int
    displayed_rows: int
    rows: tuple[dict[str, Any], ...]
    note: str = ""


@dataclass
class ReportView:
    """THE single report view model shared by Excel and HTML."""

    run_id: str
    result_id: str
    result_status: str
    method: str
    method_profile: str
    report_mapping_version: str
    resource_or_portfolio_id: str
    primary_risk_metric: str
    primary_risk_metric_unit: str
    effective_data_grade: str
    risk_convergence: str
    search_convergence: str
    is_diagnostic: bool
    # the generator version that rendered this report (so the HTML header
    # never hardcodes a drifted literal against its own manifest)
    report_generator_version: str = ""
    # THE single shared use boundary (deterministic, derived ONCE here):
    # code = diagnostic | g0_teaching | valid; warning text is the ONLY
    # text Excel/HTML may render - neither format re-derives it.
    use_boundary_code: str = "valid"
    use_boundary_warning: str = ""
    # conclusion card (ordered, from run_summary.conclusion_card)
    conclusion_card: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # capacity credit (single row from capacity_credit_results.csv)
    capacity_row: dict[str, Any] | None = None
    # adequacy metrics long table
    adequacy: TableView | None = None
    # W2: per-weather-year risk metrics (probability years with weights
    # + role-marked stress years); None on runs without the sidecar
    weather_year_metrics: TableView | None = None
    # risk events (bounded)
    risk_events: TableView | None = None
    # Q-D1：缺供持续时间汇总（分年聚合＋跨年加权
    # 汇总，与四指标同一权重集；压力年不参与加权）；None＝无事件表
    risk_event_duration_summary: dict[str, Any] | None = None
    # search trace (bounded / not_applicable)
    search_trace: TableView | None = None
    # warnings
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    blocking_diagnostics: tuple[str, ...] = field(default_factory=tuple)
    report_diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # reproducibility (caller-provided items only + capability)
    reproducibility: dict[str, Any] = field(default_factory=dict)
    # source artifacts (name -> record)
    source_files: dict[str, SourceFileRecord] = field(default_factory=dict)
    # truncated-table stats for the export manifest
    truncated_tables: list[dict[str, Any]] = field(default_factory=list)
    # key point-estimate fields with their null semantics (for the
    # data-quality view; computed via mapping_v1.classify_value)
    key_field_statuses: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # unified display fields (label/raw/display/unit/number_format)
    display_fields: tuple[DisplayField, ...] = field(default_factory=tuple)
    # summary convenience values (for the front page)
    base_risk: Any = None
    candidate_risk: Any = None
    matched_risk: Any = None
    reference_capacity_mw: Any = None
    accredited_capacity_mw: Any = None
    capacity_credit_fraction: Any = None
    # C-night/F: optional business-reading notes (schema in
    # reporting.business_notes), attached by the export entry AFTER
    # load_and_validate - they never replace machine fields and their
    # identity (sha256) is recorded in the export manifest
    business_notes: dict[str, Any] | None = None


# ---------------------------------------------------------------------
# low-level readers (stable errors, no raw exceptions)
# ---------------------------------------------------------------------


def _read_json(inp: Path, name: str) -> dict[str, Any]:
    path = inp / name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportExportError(
            "REPORT_SOURCE_INVALID", f"{name} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReportExportError("REPORT_SOURCE_INVALID", f"{name} must be a JSON object")
    _check_json_legal(payload, name)
    return payload


def _check_json_legal(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_json_legal(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_legal(item, f"{label}[{index}]")
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not math.isfinite(float(value))
    ):
        raise ReportExportError(
            "REPORT_SOURCE_INVALID", f"{label} is not a finite number"
        )


def _read_csv(inp: Path, name: str) -> list[dict[str, Any]]:
    path = inp / name
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, ValueError) as exc:
        raise ReportExportError(
            "REPORT_SOURCE_INVALID", f"{name} is not a valid CSV: {exc}"
        ) from exc
    for index, row in enumerate(rows):
        for key, value in row.items():
            if isinstance(value, str) and value.lower() in ("nan", "inf", "-inf"):
                raise ReportExportError(
                    "REPORT_SOURCE_INVALID",
                    f"{name} row {index} column {key} holds a non-finite value",
                )
    return rows


def _read_parquet_batched(
    inp: Path, name: str, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Read ONLY the display batch: total rows come from the parquet
    metadata; rows are pulled through bounded batches (never
    pq.read_table over the full file)."""
    path = inp / name
    try:
        parquet_file = pq.ParquetFile(path)  # type: ignore[no-untyped-call]
        total = parquet_file.metadata.num_rows
        columns = parquet_file.schema_arrow.names
        rows: list[dict[str, Any]] = []
        for batch in parquet_file.iter_batches(batch_size=256):  # type: ignore[no-untyped-call]
            for index in range(batch.num_rows):
                if len(rows) >= limit:
                    break
                row: dict[str, Any] = {}
                for column in columns:
                    value = batch.column(column)[index].as_py()
                    if isinstance(value, float) and not math.isfinite(value):
                        raise ReportExportError(
                            "REPORT_SOURCE_INVALID",
                            f"{name} row {index} column {column} holds a non-finite value",
                        )
                    row[column] = value
                rows.append(row)
            if len(rows) >= limit:
                break
        return total, rows
    except ReportExportError:
        raise
    except Exception as exc:
        raise ReportExportError(
            "REPORT_SOURCE_INVALID", f"{name} is not a valid parquet table: {exc}"
        ) from exc


def _num(value: Any) -> Any:
    """Business numeric values become real numbers in the view model
    (Excel writes numeric cells); None stays None; non-numeric text
    stays as-is (e.g. applicability labels)."""
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _str(value: Any, label: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ReportExportError(
            "REPORT_SOURCE_INVALID", f"required identity field {label} is empty"
        )
    return str(value)


def _source_records(inp: Path) -> dict[str, SourceFileRecord]:
    records: dict[str, SourceFileRecord] = {}
    names: list[str] = list(MACHINE_OUTPUT_FILES)
    # W2: the optional eighth artifact is recorded WHEN PRESENT so the
    # export manifest carries its identity; its absence on older runs
    # stays an honest absence
    if (inp / WEATHER_YEAR_METRICS_FILE).is_file():
        names.append(WEATHER_YEAR_METRICS_FILE)
    for name in names:
        path = inp / name
        records[name] = SourceFileRecord(
            name=name, sha256=_sha256_file(path), size=path.stat().st_size
        )
    return records


def _bounded_rows(
    source: str, rows: list[dict[str, Any]], limit: int
) -> TableView:
    total = len(rows)
    shown = rows[:limit]
    return TableView(
        source=source,
        total_rows=total,
        displayed_rows=len(shown),
        rows=tuple(shown),
        note=f"full data remains authoritative in {source}",
    )


# ---------------------------------------------------------------------
# Q-D1：缺供持续时间汇总（分年聚合＋跨年加权）
# ---------------------------------------------------------------------


def _risk_event_duration_summary(
    input_dir: Path, weather_year_view: TableView | None
) -> dict[str, Any] | None:
    """THE single Q-D1 duration aggregation over the FULL risk-event
    record (not the bounded display batch): per-weather-year counts /
    total / average / max duration, a cross-year weighted expectation
    using the SAME weight set as the four risk KPIs (stress years never
    sum) and the all-sample maximum. No event table -> None (old runs);
    rows without duration_hours -> an honest unavailable summary."""
    if not (input_dir / "risk_events.parquet").is_file():
        return None
    _total, rows = _read_parquet_batched(
        input_dir, "risk_events.parquet", 10**9
    )
    if not rows:
        return {
            "available": False,
            "reason": "无风险事件记录（零损失或事件证据缺失）",
        }
    if any("duration_hours" not in row for row in rows):
        return {
            "available": False,
            "reason": "记录缺 duration_hours（旧版机器输出），汇总不可得",
        }
    per_year: dict[str, dict[str, Any]] = {}
    for row in rows:
        year = str(row.get("scenario_id") or "")
        slot = per_year.setdefault(
            year,
            {
                "scenario_id": year,
                "event_count": 0,
                "total_hours": 0,
                "max_duration_hours": 0,
            },
        )
        duration = int(row["duration_hours"])
        slot["event_count"] += 1
        slot["total_hours"] += duration
        slot["max_duration_hours"] = max(slot["max_duration_hours"], duration)
    for slot in per_year.values():
        slot["avg_duration_hours"] = round(
            slot["total_hours"] / slot["event_count"], 4
        )
    out: dict[str, Any] = {
        "available": True,
        "per_year": [per_year[key] for key in sorted(per_year)],
        "max_duration_hours": max(int(row["duration_hours"]) for row in rows),
        "total_event_count": len(rows),
        "note": (
            "分年聚合＋跨年加权汇总（与四指标同一权重集；压力考验年"
            "不参与加权，但计入分年事实与全样本最长）"
        ),
    }
    weights: dict[str, float] = {}
    if weather_year_view is not None:
        for row in weather_year_view.rows:
            if str(row.get("role") or "") != "probability_baseline":
                continue
            weight = row.get("weight")
            if weight is None:
                continue
            weights[f"weather_year_{int(row['weather_year'])}"] = float(weight)
    matched = {y: w for y, w in weights.items() if y in per_year}
    if matched:
        weighted_count = sum(
            w * per_year[y]["event_count"] for y, w in matched.items()
        )
        weighted_hours = sum(
            w * per_year[y]["total_hours"] for y, w in matched.items()
        )
        out["weighted_event_count_per_year"] = round(weighted_count, 6)
        out["weighted_loss_hours_per_year"] = round(weighted_hours, 6)
        out["weighted_avg_duration_hours"] = (
            round(weighted_hours / weighted_count, 6) if weighted_count > 0 else None
        )
        unmatched = sorted(set(per_year) - set(matched))
        out["weighted_note"] = (
            "加权仅含权重表匹配的概率基准年"
            + (
                f"；未匹配年份（不分权重）：{'、'.join(unmatched)}"
                if unmatched
                else ""
            )
        )
    else:
        out["weighted_note"] = "权重集缺失或与事件年份不匹配，未给加权汇总（仅分年事实）"
    return out


# ---------------------------------------------------------------------
# THE single load+validate entry (runs ONCE per export)
# ---------------------------------------------------------------------


def load_and_validate(input_dir: Path | str) -> ReportView:
    """Load and validate a T23-A machine-result directory ONCE. Any
    missing artifact, hash mismatch, identity conflict, unsupported
    mapping version or illegal value blocks the whole export."""
    inp = Path(input_dir)
    if not inp.is_dir():
        raise ReportExportError("REPORT_SOURCE_INCOMPLETE", f"input directory missing: {inp}")

    missing = [name for name in MACHINE_OUTPUT_FILES if not (inp / name).is_file()]
    if missing:
        raise ReportExportError(
            "REPORT_SOURCE_INCOMPLETE",
            f"T23-A artifacts missing: {', '.join(missing)}",
        )

    summary = _read_json(inp, "run_summary.json")
    capacity_rows = _read_csv(inp, "capacity_credit_results.csv")
    adequacy_rows = _read_csv(inp, "adequacy_metrics.csv")
    risk_total, risk_rows = _read_parquet_batched(
        inp, "risk_events.parquet", RISK_EVENT_DISPLAY_LIMIT
    )
    search_rows = _read_csv(inp, "search_trace.csv")
    warnings = _read_json(inp, "warnings.json")
    manifest = _read_json(inp, "reproducibility_manifest.json")
    # W2: the optional per-weather-year table - absent on old runs
    weather_year_rows: list[dict[str, Any]] | None = None
    if (inp / WEATHER_YEAR_METRICS_FILE).is_file():
        weather_year_rows = _read_csv(inp, WEATHER_YEAR_METRICS_FILE)

    # mapping version gate
    mapping_version = summary.get("report_mapping_version")
    if mapping_version not in SUPPORTED_REPORT_MAPPING_VERSIONS:
        raise ReportExportError(
            "REPORT_MAPPING_VERSION_UNSUPPORTED",
            f"report_mapping_version {mapping_version!r} is not supported",
        )

    # hash gate: manifest-declared outputs must match the real files
    declared = manifest.get("outputs") or {}
    for name in MACHINE_OUTPUT_FILES:
        if name == "reproducibility_manifest.json":
            continue
        actual = _sha256_bytes((inp / name).read_bytes())
        expected = declared.get(name)
        if expected != actual:
            raise ReportExportError(
                "REPORT_SOURCE_HASH_MISMATCH",
                f"{name} hash {actual[:12]}... does not match the reproducibility "
                f"manifest {str(expected)[:12]}...",
            )
    # W2: the OPTIONAL eighth artifact follows the same rule when
    # present - and a manifest that declares it demands the file
    weather_path = inp / WEATHER_YEAR_METRICS_FILE
    if weather_path.is_file():
        actual = _sha256_bytes(weather_path.read_bytes())
        expected = declared.get(WEATHER_YEAR_METRICS_FILE)
        if expected != actual:
            raise ReportExportError(
                "REPORT_SOURCE_HASH_MISMATCH",
                f"{WEATHER_YEAR_METRICS_FILE} hash {actual[:12]}... does not match "
                f"the reproducibility manifest {str(expected)[:12]}...",
            )
    elif declared.get(WEATHER_YEAR_METRICS_FILE):
        raise ReportExportError(
            "REPORT_SOURCE_INCOMPLETE",
            f"manifest-declared {WEATHER_YEAR_METRICS_FILE} is missing",
        )

    # identity gate: run_id / result_id / result_status consistent
    run_id = _str(summary.get("run_id"), "run_id")
    result_id = _str(summary.get("result_id"), "result_id")
    result_status = _str(summary.get("result_status"), "result_status")
    for label, rows in (
        ("capacity_credit_results.csv", capacity_rows),
        ("adequacy_metrics.csv", adequacy_rows),
    ):
        for index, row in enumerate(rows):
            if row.get("run_id") != run_id:
                raise ReportExportError(
                    "REPORT_SOURCE_IDENTITY_MISMATCH",
                    f"{label} row {index} run_id conflicts with run_summary",
                )
            if row.get("result_id") != result_id:
                raise ReportExportError(
                    "REPORT_SOURCE_IDENTITY_MISMATCH",
                    f"{label} row {index} result_id conflicts with run_summary",
                )
            status = row.get("result_status")
            if status and status != result_status:
                raise ReportExportError(
                    "REPORT_SOURCE_IDENTITY_MISMATCH",
                    f"{label} row {index} result_status conflicts with run_summary",
                )
    if str(warnings.get("run_id", "")) != run_id or str(warnings.get("result_id", "")) != result_id:
        raise ReportExportError(
            "REPORT_SOURCE_IDENTITY_MISMATCH",
            "warnings.json identity conflicts with run_summary",
        )
    if str(manifest.get("run_id", "")) != run_id or str(manifest.get("result_id", "")) != result_id:
        raise ReportExportError(
            "REPORT_SOURCE_IDENTITY_MISMATCH",
            "reproducibility_manifest.json identity conflicts with run_summary",
        )

    is_diagnostic = result_status not in _VALID_STATUSES

    # THE single shared use boundary (deterministic; Excel/HTML only render
    # this text - never re-derive it): diagnostic > G0_teaching > valid.
    effective_data_grade = str(summary.get("effective_data_grade") or "")
    if is_diagnostic:
        use_boundary_code = "diagnostic"
        use_boundary_warning = "结果不可作为正式测算结论（诊断报告）"
    elif effective_data_grade == "G0_teaching":
        use_boundary_code = "g0_teaching"
        use_boundary_warning = "教学/内部验证数据，不得作为正式容量信用结论"
    else:
        use_boundary_code = "valid"
        use_boundary_warning = ""

    conclusion_card_items = list((summary.get("conclusion_card") or {}).items())
    card_entries: list[dict[str, Any]] = []
    for field_id, entry in conclusion_card_items:
        if isinstance(entry, Mapping):
            card_entries.append(
                {
                    "field_id": str(field_id),
                    "label": str(entry.get("label", field_id)),
                    "value": entry.get("value"),
                    "unit": entry.get("unit"),
                    "null_status": str(entry.get("null_status", "")),
                }
            )

    # bounded tables (never silently truncated: totals always recorded)
    for row in adequacy_rows:
        if "value" in row:
            row["value"] = _num(row["value"])
    adequacy_view = _bounded_rows("adequacy_metrics.csv", adequacy_rows, 5000)
    weather_year_view: TableView | None = None
    if weather_year_rows is not None:
        for row in weather_year_rows:
            for key in (
                "weight", "primary_metric_mean",
                "lole_days_per_year", "lolh_hours_per_year",
                "eue_mwh_per_year", "neue_ppm", "demand_energy_mwh",
                "sample_count", "weather_year",
            ):
                if key in row:
                    row[key] = _num(row[key])
        weather_year_view = _bounded_rows(
            WEATHER_YEAR_METRICS_FILE, weather_year_rows, 500
        )
    risk_view = TableView(
        source="risk_events.parquet",
        total_rows=risk_total,
        displayed_rows=len(risk_rows),
        rows=tuple(risk_rows),
        note="full data remains authoritative in risk_events.parquet",
    )
    for row in search_rows:
        for key in (
            "capacity_mw", "reference_risk", "trial_risk", "difference_mean",
            "difference_se", "bracket_low_after", "bracket_high_after", "estimate_mw",
        ):
            if key in row:
                row[key] = _num(row[key])
    search_present = bool(search_rows) and any(r.get("trace_status") == "present" for r in search_rows)
    search_view = _bounded_rows("search_trace.csv", search_rows, SEARCH_TRACE_DISPLAY_LIMIT)
    if not search_present:
        search_view = TableView(
            source="search_trace.csv",
            total_rows=0,
            displayed_rows=0,
            rows=(),
            note="search trace not applicable / not provided",
        )

    truncated = []
    for view in (adequacy_view, risk_view, search_view):
        if view.total_rows > view.displayed_rows:
            truncated.append(
                {
                    "source": view.source,
                    "total_rows": view.total_rows,
                    "displayed_rows": view.displayed_rows,
                }
            )

    from capacity_credit.reporting import mapping_v1 as _mapping

    # The validated capacity-credit result row (identity/hash/consistency
    # checked above) is THE source for capacity-result fields that
    # run_summary does not carry (capacity_value_type, capacity_credit_unit,
    # result_applicability, status columns...). Machine CSV, Excel and HTML
    # consume THIS row - never a second guess.
    capacity_row = None
    if capacity_rows:
        capacity_row = dict(capacity_rows[0])
        for key in (
            "reference_capacity_mw",
            "accredited_capacity_mw",
            "capacity_credit_fraction",
        ):
            if key in capacity_row:
                capacity_row[key] = _num(capacity_row[key])

    # Shared identity/semantic fields MUST agree between run_summary and
    # the capacity_credit_results.csv row - a conflict means the machine
    # bundle is internally inconsistent and the report is blocked (never
    # silently "run_summary wins"). resource_or_portfolio_id follows the
    # existing controlled rule: both non-empty must agree; a summary that
    # is empty may fall back to the validated CSV value.
    if capacity_row is not None:
        for field in (
            "report_mapping_version",
            "run_id",
            "result_id",
            "method",
            "result_status",
        ):
            summary_value = summary.get(field)
            csv_value = capacity_row.get(field)
            if (
                summary_value is not None
                and csv_value is not None
                and str(summary_value) != str(csv_value)
            ):
                raise ReportExportError(
                    "REPORT_SOURCE_IDENTITY_MISMATCH",
                    f"capacity_credit_results.csv {field} conflicts with run_summary: "
                    f"{csv_value!r} != {summary_value!r}",
                )
        summary_rid = summary.get("resource_or_portfolio_id")
        csv_rid = capacity_row.get("resource_or_portfolio_id")
        if (
            summary_rid
            and csv_rid
            and str(summary_rid) != str(csv_rid)
        ):
            raise ReportExportError(
                "REPORT_SOURCE_IDENTITY_MISMATCH",
                f"capacity_credit_results.csv resource_or_portfolio_id conflicts with "
                f"run_summary: {csv_rid!r} != {summary_rid!r}",
            )

    class _SummaryProxy:
        """model_dump-compatible shim so the mapping_v1 classify/extract
        entries (THE null-semantics source) can be reused without
        rebuilding a full MethodResult. The payload is run_summary PLUS
        ONLY the official mapping_v1 capacity_credit_results projection
        fields that run_summary does not carry - never a whole-row
        override, never unknown CSV columns."""

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return dict(self._payload)

    summary_payload: dict[str, Any] = dict(summary)
    if capacity_row is not None:
        capacity_projection = frozenset(
            _mapping.machine_result_field_ids("capacity_credit_results")
        )
        for key, value in capacity_row.items():
            if key in capacity_projection and key not in summary_payload:
                summary_payload[key] = value
    summary_proxy = _SummaryProxy(summary_payload)
    display_fields: list[DisplayField] = []
    for field_id in (
        "capacity_value_type",
        "reference_capacity_mw",
        "accredited_capacity_mw",
        "capacity_credit_fraction",
        "capacity_credit_unit",
        "result_applicability",
        "base_risk",
        "candidate_risk",
        "matched_risk",
        "rmri_status",
        "rmri_value",
        "mri_value",
        "primary_risk_metric",
        "primary_risk_metric_unit",
    ):
        try:
            raw = _mapping.extract_field(summary_proxy, field_id)  # type: ignore[arg-type]
        except KeyError:
            continue
        mapped_field = _mapping.get_field(field_id)
        # display unit: the MAPPED display_unit wins (e.g. MW / %);
        # fall back to the canonical unit for non-display fields
        display_unit = mapped_field.display_unit or _mapping.unit_for_field(
            field_id, summary_proxy,  # type: ignore[arg-type]
        )
        number_format = None
        if display_unit == "%" or mapped_field.allows_percent_conversion:
            number_format = "0.0%"
        elif display_unit == "MW":
            number_format = "0.00"
        display_fields.append(
            DisplayField(
                field_id=field_id,
                label=_mapping.get_field(field_id).label,
                raw_value=raw,
                display_value=_mapping.display_format(raw, field_id),
                display_unit=display_unit,
                null_status=_mapping.classify_value(
                    field_id, summary_proxy,  # type: ignore[arg-type]
                ),
                number_format=number_format,
            )
        )

    key_field_statuses: list[dict[str, Any]] = []
    for field_id in (
        "base_risk",
        "candidate_risk",
        "matched_risk",
        "reference_capacity_mw",
        "accredited_capacity_mw",
        "capacity_credit_fraction",
        "rmri_value",
        "mri_value",
        "lower_ci",
        "upper_ci",
        "empirical_risk_resolution",
    ):
        try:
            value = _mapping.extract_field(summary_proxy, field_id)  # type: ignore[arg-type]
        except KeyError:
            continue
        key_field_statuses.append(
            {
                "field_id": field_id,
                "label": _mapping.get_field(field_id).label,
                "value": value,
                "null_status": _mapping.classify_value(
                    field_id, summary_proxy,  # type: ignore[arg-type]
                ),
            }
        )

    reproducibility: dict[str, Any] = {}
    for key in (
        "scenario_bank_id",
        "source_run_id",
        "inputs_hash",
        "config_hash",
        "software_version",
        "random_seed",
        "model_ready_data",
        "data_source_records",
        "rerun_capability",
        "missing_reproducibility_inputs",
    ):
        if key in manifest:
            reproducibility[key] = manifest[key]

    resource_or_portfolio_id = str(summary.get("resource_or_portfolio_id") or "")
    if not resource_or_portfolio_id and capacity_row:
        resource_or_portfolio_id = str(capacity_row.get("resource_or_portfolio_id") or "")

    return ReportView(
        run_id=run_id,
        result_id=result_id,
        result_status=result_status,
        method=_str(summary.get("method"), "method"),
        method_profile=str(summary.get("method_profile") or ""),
        report_mapping_version=str(mapping_version),
        report_generator_version=REPORT_GENERATOR_VERSION,
        resource_or_portfolio_id=resource_or_portfolio_id,
        primary_risk_metric=str(summary.get("primary_risk_metric") or ""),
        primary_risk_metric_unit=str(summary.get("primary_risk_metric_unit") or ""),
        effective_data_grade=effective_data_grade,
        risk_convergence=str(summary.get("risk_convergence") or ""),
        search_convergence=str(summary.get("search_convergence") or ""),
        is_diagnostic=is_diagnostic,
        use_boundary_code=use_boundary_code,
        use_boundary_warning=use_boundary_warning,
        conclusion_card=tuple(card_entries),
        capacity_row=capacity_row,
        adequacy=adequacy_view,
        weather_year_metrics=weather_year_view,
        risk_events=risk_view,
        risk_event_duration_summary=_risk_event_duration_summary(
            inp, weather_year_view
        ),
        search_trace=search_view,
        warnings=tuple(warnings.get("warnings") or []),
        blocking_diagnostics=tuple(warnings.get("blocking_diagnostics") or []),
        report_diagnostics=tuple(warnings.get("report_diagnostics") or []),
        reproducibility=reproducibility,
        source_files=_source_records(inp),
        truncated_tables=truncated,
        key_field_statuses=tuple(key_field_statuses),
        display_fields=tuple(display_fields),
        base_risk=summary.get("base_risk"),
        candidate_risk=summary.get("candidate_risk"),
        matched_risk=summary.get("matched_risk"),
        reference_capacity_mw=summary.get("reference_capacity_mw"),
        accredited_capacity_mw=summary.get("accredited_capacity_mw"),
        capacity_credit_fraction=summary.get("capacity_credit_fraction"),
    )


__all__ = [
    "REPORT_EXPORT_SCHEMA_VERSION",
    "REPORT_GENERATOR_VERSION",
    "RISK_EVENT_DISPLAY_LIMIT",
    "SEARCH_TRACE_DISPLAY_LIMIT",
    "SUPPORTED_REPORT_MAPPING_VERSIONS",
    "ReportExportError",
    "ReportView",
    "SourceFileRecord",
    "TableView",
    "load_and_validate",
]
