"""Resource-run input boundary (T16 fourth-review candidate).

Single module owning the input side of ``run_resource_pipeline``:

- ``ResourceRunDiagnostic``: the ONE authoritative resource-run boundary
  diagnostic (code / severity / message / source / optional table /
  locator). ``ResourceRunError`` and ``ResourceRunEvidence`` keep ONLY
  this model; the frozen MethodResult 1.3 ``Diagnostic`` is produced at
  the T17 / ELCC / EFC method-adapter boundary via
  ``ResourceRunDiagnostic.to_method_diagnostic``.
- ``merge_resource_run_diagnostics``: split publication - standard
  ValidationIssues are converted DIRECTLY (never routed through the ELCC
  registry) and every distinct position (code + table + locator +
  message) is preserved; ELCC method codes go through the registry and
  are deduplicated by code. Standard entries win on code collisions but
  never drop the other standard positions of the same code.
- ``check_snapshot_structure``: structure preflight over the fields the
  snapshot selection REQUIRES (a KeyError / DateParseError /
  AttributeError can never leak from the public entry).
- ``select_runtime_snapshot``: the ONE runtime snapshot (scenario filter
  + project-timezone weather-year filter) consumed by validation, every
  builder, the intertie plan, hydro identity, data identity and
  per-sample processing.
- ``canonical_axes_from_load``: canonical hourly axes built from the
  selected frozen load and validated with the SHARED
  ``domain.time_axis.build_hour_axis`` (no third full validator); only a
  lightweight numeric positioning function lives here.
- ``validate_resource_run_config`` / ``validate_resource_run_inputs``:
  config-bank binding and selected-snapshot standard validation.

DQ-25 lives in the standard data-validation / import quality gate; the
resource-run boundary does NOT re-run it (no config fork).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NoReturn

import numpy as np
import pandas as pd

from capacity_credit.contracts.elcc_diagnostics import convert_elcc_diagnostics
from capacity_credit.contracts.enums import (
    ChronologyMode,
    RiskMetric,
    Severity,
    SocYearBoundaryMode,
)
from capacity_credit.contracts.results import Diagnostic
from capacity_credit.contracts.storage_soc_config import StorageSocConfig
from capacity_credit.data.validation import (
    ValidationIssue,
    required_columns_for,
    validate_standard_tables,
)
from capacity_credit.domain.time_axis import TimeAxisError, build_hour_axis
from capacity_credit.scenarios.scenario_bank import ScenarioBank

DEFAULT_DELTA_T_HOURS = 1.0
_VALID_YEAR_ROLES = frozenset({"probability_baseline", "stress_test"})


# ----------------------------------------------------------------------
# unified boundary diagnostic
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceRunDiagnostic:
    """The single authoritative resource-run boundary diagnostic.

    ``source`` is ``standard`` (original standard-layer ValidationIssue),
    ``resource_run`` (boundary structure / time-axis error) or
    ``method`` (ELCC diagnostic registry). The frozen MethodResult 1.3
    ``Diagnostic`` is NOT extended here; conversion happens at the
    method-adapter boundary."""

    code: str
    severity: Severity
    message: str
    source: str = "standard"
    table: str = ""
    locator: str = ""

    def to_method_diagnostic(self) -> Diagnostic:
        return Diagnostic(code=self.code, severity=self.severity, message=self.message)


class ResourceRunError(ValueError):
    """Stable resource-run error carrying the unified boundary
    diagnostics (single authoritative list)."""

    def __init__(
        self,
        code: str,
        message: str,
        diagnostics: list[ResourceRunDiagnostic],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = diagnostics


def _boundary_diagnostic(
    code: str,
    severity: Severity,
    message: str,
    *,
    table: str = "",
    locator: str = "",
) -> ResourceRunDiagnostic:
    return ResourceRunDiagnostic(
        code=code,
        severity=severity,
        message=message,
        source="resource_run",
        table=table,
        locator=locator,
    )


# ----------------------------------------------------------------------
# config
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceRunConfig:
    """Method-ready configuration for one resource run."""

    scenario_bank_id: str
    root_seed: int
    samples: int
    weather_years: tuple[int, ...]
    year_roles: dict[int, str]
    timezone: str
    primary_risk_metric: str = RiskMetric.LOLE_DAYS_PER_YEAR.value
    year_weights: dict[int, float] | None = None
    declared_grade: str = "G0_teaching"
    storage_soc: StorageSocConfig | None = None
    chronology_mode: ChronologyMode = ChronologyMode.INDEPENDENT_WEATHER_YEARS
    soc_year_boundary_mode: SocYearBoundaryMode = (
        SocYearBoundaryMode.REINITIALIZE_WITH_CONFIGURED_MODE
    )
    dispatch_strategy: str = "eue_greedy"
    risk_window_length_hours: int = 24
    risk_threshold: float = 0.05
    reserve_soc_fraction: float = 0.0
    intertie_availability_policy: str = "zero"
    emergency_limit_source: str | None = None
    hydro_adjustable_request_mw: float = 0.0
    scenario_id: str | None = None
    auto_schedule_planned_outages: bool = False
    merge_events_across_years: bool = False
    delta_t_hours: float = DEFAULT_DELTA_T_HOURS
    perfect_capacity_mw: float = 0.0
    # F1: emergency-layer override ("auto" = table-driven, "disabled" =
    # layer off on the same frozen data - the on/off sensitivity axis)
    emergency_layer: str = "auto"


def _validate_bank_binding(
    config: ResourceRunConfig,
    bank: ScenarioBank,
) -> None:
    """Bank identity / years / samples / timezone / seed gates."""
    if config.scenario_bank_id != bank.bank_id:
        raise ResourceRunError(
            "RESOURCE_RUN_BANK_MISMATCH",
            "config.scenario_bank_id does not match the opened bank",
            [],
        )
    bank_years = {int(year) for year in bank.index["weather_years"]}
    config_years = set(config.weather_years)
    if not config_years or not config_years <= bank_years:
        raise ResourceRunError(
            "RESOURCE_RUN_YEAR_MISMATCH",
            "config.weather_years must be a non-empty subset of the bank years",
            [],
        )
    if (
        isinstance(config.samples, bool)
        or not isinstance(config.samples, int)
        or not 1 <= config.samples <= int(bank.index["sample_count"])
    ):
        raise ResourceRunError(
            "RESOURCE_RUN_SAMPLES_MISMATCH",
            "config.samples must lie within the bank sample range",
            [],
        )
    if config.timezone != str(bank.index["timezone"]):
        raise ResourceRunError(
            "RESOURCE_RUN_TIMEZONE_MISMATCH",
            "config.timezone must equal the bank timezone",
            [],
        )
    if config.root_seed != int(bank.index["root_seed"]):
        raise ResourceRunError(
            "RESOURCE_RUN_SEED_MISMATCH",
            "config.root_seed must equal the bank root seed",
            [],
        )


def _validate_year_roles_and_weights(config: ResourceRunConfig) -> list[int]:
    """Year-role and weight gates; returns the probability years."""
    config_years = set(config.weather_years)
    if set(config.year_roles) != config_years:
        raise ResourceRunError(
            "RESOURCE_RUN_ROLES_MISMATCH",
            "config.year_roles keys must exactly match config.weather_years",
            [],
        )
    for role in config.year_roles.values():
        if role not in _VALID_YEAR_ROLES:
            raise ResourceRunError(
                "RESOURCE_RUN_ROLES_MISMATCH",
                f"unknown year role {role!r}",
                [],
            )
    probability_years = [
        y for y, role in config.year_roles.items() if role == "probability_baseline"
    ]
    if not probability_years:
        raise ResourceRunError(
            "RESOURCE_RUN_NO_BASELINE_YEAR",
            "at least one probability-baseline year is required",
            [],
        )
    _validate_year_weights(config.year_weights, probability_years)
    return probability_years


def _validate_year_weights(
    weights: dict[int, float] | None,
    probability_years: list[int],
) -> None:
    """Year-weight gates: keys match, finite, non-negative, sum 1."""
    if weights is None:
        return
    if set(weights) != set(probability_years):
        raise ResourceRunError(
            "RESOURCE_RUN_WEIGHTS_MISMATCH",
            "year_weights keys must exactly match the probability years",
            [],
        )
    total = 0.0
    for value in weights.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResourceRunError(
                "RESOURCE_RUN_WEIGHTS_INVALID", "weights must be real numbers", []
            )
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise ResourceRunError(
                "RESOURCE_RUN_WEIGHTS_INVALID",
                "weights must be finite and non-negative",
                [],
            )
        total += number
    if abs(total - 1.0) > 1e-9:
        raise ResourceRunError("RESOURCE_RUN_WEIGHTS_INVALID", "weights must sum to 1", [])


def _validate_method_fields(config: ResourceRunConfig) -> None:
    """Metric / strategy / delta_t gates."""
    try:
        RiskMetric(config.primary_risk_metric)
    except ValueError:
        raise ResourceRunError(
            "RESOURCE_RUN_INVALID_METRIC",
            f"unknown primary_risk_metric {config.primary_risk_metric!r}",
            [],
        ) from None
    if config.dispatch_strategy not in (
        "eue_greedy",
        "risk_window",
        "perfect_foresight",
    ):
        raise ResourceRunError(
            "RESOURCE_RUN_INVALID_STRATEGY",
            f"unknown dispatch_strategy {config.dispatch_strategy!r}",
            [],
        )
    if (
        isinstance(config.perfect_capacity_mw, bool)
        or not isinstance(config.perfect_capacity_mw, (int, float))
        or not math.isfinite(float(config.perfect_capacity_mw))
        or float(config.perfect_capacity_mw) < 0.0
    ):
        raise ResourceRunError(
            "RESOURCE_RUN_INVALID_METHOD",
            "perfect_capacity_mw must be a finite non-negative number",
            [],
        )
    if (
        isinstance(config.delta_t_hours, bool)
        or not isinstance(config.delta_t_hours, (int, float))
        or not math.isfinite(float(config.delta_t_hours))
        or float(config.delta_t_hours) != DEFAULT_DELTA_T_HOURS
    ):
        raise ResourceRunError(
            "RESOURCE_RUN_DELTA_T_UNSUPPORTED",
            "the engine is hourly-only; delta_t_hours must be 1.0",
            [],
        )


def validate_resource_run_config(
    config: ResourceRunConfig,
    bank: ScenarioBank,
) -> None:
    """Bind the config to the OPENED scenario bank before any dispatch.

    Every mismatch raises ResourceRunError with a stable code - no
    KeyError / AttributeError / ZeroDivisionError can leak."""
    _validate_bank_binding(config, bank)
    _validate_year_roles_and_weights(config)
    _validate_method_fields(config)


# ----------------------------------------------------------------------
# split publication (standard DQ direct, method codes via registry)
# ----------------------------------------------------------------------


def _standard_diagnostic(issue: ValidationIssue) -> ResourceRunDiagnostic:
    return ResourceRunDiagnostic(
        code=issue.code,
        severity=Severity.ERROR if issue.severity == "error" else Severity.WARNING,
        message=issue.message,
        source="standard",
        table=issue.table,
        locator=issue.locator,
    )


def _method_diagnostic(diagnostic: Diagnostic) -> ResourceRunDiagnostic:
    return ResourceRunDiagnostic(
        code=diagnostic.code,
        severity=diagnostic.severity,
        message=diagnostic.message,
        source="method",
    )


def _standard_identity(diagnostic: ResourceRunDiagnostic) -> tuple[str, str, str, str, str]:
    return (
        diagnostic.code,
        diagnostic.severity.value,
        diagnostic.table,
        diagnostic.locator,
        diagnostic.message,
    )


def _diagnostic_sort_key(
    diagnostic: ResourceRunDiagnostic,
) -> tuple[str, str, str, str, str]:
    return (
        diagnostic.code,
        diagnostic.table,
        diagnostic.locator,
        diagnostic.message,
        diagnostic.severity.value,
    )


def _merge_resource_run_diagnostic_entries(
    standard_diags: list[ResourceRunDiagnostic],
    method_codes: list[str],
) -> list[ResourceRunDiagnostic]:
    """Core deterministic merge for the resource-run boundary.

    Standard entries are deduplicated by position identity (code +
    severity + table + locator + message): two standard issues with the
    SAME code but DIFFERENT table/locator are BOTH kept, identical
    duplicates collapse to one. Method entries are deduplicated by code.
    When a standard and a method entry share a code, the STANDARD
    entries win (original message + positions kept) and the method entry
    is dropped. Output is sorted deterministically (code, table,
    locator, message, severity) - never by input order."""
    by_identity: dict[tuple[str, ...], ResourceRunDiagnostic] = {}
    for diagnostic in standard_diags:
        by_identity.setdefault(_standard_identity(diagnostic), diagnostic)
    standard_codes = {diagnostic.code for diagnostic in by_identity.values()}
    for method_diag in convert_elcc_diagnostics(sorted(set(method_codes))):
        if method_diag.code in standard_codes:
            continue
        by_identity.setdefault(("method", method_diag.code), _method_diagnostic(method_diag))
    return sorted(by_identity.values(), key=_diagnostic_sort_key)


def merge_resource_run_diagnostics(
    standard_issues: list[ValidationIssue],
    method_codes: list[str],
) -> list[ResourceRunDiagnostic]:
    """Split publication for the resource-run boundary (standard
    ValidationIssues + ELCC method codes). See
    ``_merge_resource_run_diagnostic_entries`` for the merge rules."""
    return _merge_resource_run_diagnostic_entries(
        [_standard_diagnostic(issue) for issue in standard_issues],
        method_codes,
    )


def merge_published_diagnostics(
    standard_diags: list[ResourceRunDiagnostic],
    method_codes: list[str],
) -> list[ResourceRunDiagnostic]:
    """Merge the ALREADY-converted standard diagnostics with ELCC
    method codes (pipeline publication). Same rules as
    ``merge_resource_run_diagnostics``."""
    return _merge_resource_run_diagnostic_entries(standard_diags, method_codes)


# ----------------------------------------------------------------------
# snapshot-selection field contract (minimal, reuses canonical columns)
# ----------------------------------------------------------------------


_SCENARIOIZED_TABLES = (
    "load_hourly",
    "renewable_hourly",
    "hydro_hourly",
    "hydro_energy_budgets",
    "intertie_hourly",
)

# Fields the snapshot selection MUST read before any other processing.
# This is the minimal snapshot-selection subset - NOT a second copy of
# the full standard field dictionary (see ``required_columns_for`` for
# the single canonical source).
_SNAPSHOT_FIELDS_BY_TABLE: dict[str, tuple[str, ...]] = {
    "load_hourly": ("scenario_id", "timestamp"),
    "renewable_hourly": ("scenario_id", "timestamp"),
    "hydro_hourly": ("scenario_id", "timestamp"),
    "hydro_energy_budgets": ("scenario_id", "period_start", "period_end"),
    "intertie_hourly": ("scenario_id", "timestamp"),
    "renewable_capacities": ("year",),
}


def check_snapshot_structure(tables: dict[str, pd.DataFrame]) -> None:
    """Structure preflight over the snapshot-selection fields.

    Every present, non-empty table that the snapshot selection reads
    must carry its required columns BEFORE any column indexing - a
    KeyError / DateParseError / AttributeError can never leak from the
    public entry. Full data quality still runs on the SELECTED snapshot
    via ``validate_standard_tables``."""
    for name, fields in _SNAPSHOT_FIELDS_BY_TABLE.items():
        if name not in tables or tables[name].empty:
            continue
        missing = [field for field in fields if field not in tables[name].columns]
        if not missing:
            continue
        # cross-check against the canonical standard contract (single
        # source of required fields) - never a duplicate dictionary
        canonical = required_columns_for(name)
        if canonical and not all(field in canonical for field in fields):
            unknown = [field for field in fields if field not in canonical]
            raise ResourceRunError(
                "RESOURCE_RUN_STRUCTURE_INVALID",
                f"snapshot-selection fields {unknown} are not part of the "
                f"standard contract for {name}",
                [
                    _boundary_diagnostic(
                        "RESOURCE_RUN_SNAPSHOT_FIELD_UNKNOWN",
                        Severity.ERROR,
                        f"snapshot-selection fields {unknown} are not part of the "
                        f"standard contract for {name}",
                        table=name,
                    )
                ],
            )
        raise ResourceRunError(
            "RESOURCE_RUN_STRUCTURE_INVALID",
            f"{name} is missing snapshot-selection columns {missing}",
            [
                _boundary_diagnostic(
                    "RESOURCE_RUN_SNAPSHOT_FIELD_MISSING",
                    Severity.ERROR,
                    f"{name} is missing snapshot-selection columns {missing}",
                    table=name,
                    locator=", ".join(missing),
                )
            ],
        )


def _scenario_ids_in(frame: pd.DataFrame) -> set[str]:
    if "scenario_id" not in frame.columns:
        return set()
    return {str(value) for value in frame["scenario_id"].dropna().unique()}


def resolve_scenario(
    config: ResourceRunConfig,
    tables: dict[str, pd.DataFrame],
) -> str:
    """Explicit scenario selection: every scenarioized table must agree
    on the chosen scenario. Missing / ambiguous / inconsistent selections
    raise a stable ResourceRunError - never take the first row, never mix
    or sum scenarios."""
    per_table: dict[str, set[str]] = {}
    for name in _SCENARIOIZED_TABLES:
        if name in tables and not tables[name].empty:
            per_table[name] = _scenario_ids_in(tables[name])
    requested = config.scenario_id
    if requested is None:
        ids = set()
        for table, table_ids in per_table.items():
            if len(table_ids) == 0:
                continue
            if len(table_ids) > 1:
                raise ResourceRunError(
                    "RESOURCE_RUN_SCENARIO_AMBIGUOUS",
                    f"{table} carries multiple scenarios {sorted(table_ids)}; "
                    "an explicit scenario_id is required",
                    [],
                )
            ids.add(next(iter(table_ids)))
        if len(ids) > 1:
            raise ResourceRunError(
                "RESOURCE_RUN_SCENARIO_INCONSISTENT",
                f"scenarioized tables disagree on the single scenario: {sorted(ids)}",
                [],
            )
        if not ids:
            raise ResourceRunError(
                "RESOURCE_RUN_SCENARIO_MISSING",
                "no scenario_id found in the scenarioized tables",
                [],
            )
        return next(iter(ids))
    if not isinstance(requested, str) or not requested.strip():
        raise ResourceRunError(
            "RESOURCE_RUN_SCENARIO_INVALID",
            "scenario_id must be a non-empty string",
            [],
        )
    for table, table_ids in per_table.items():
        if table_ids and requested not in table_ids:
            raise ResourceRunError(
                "RESOURCE_RUN_SCENARIO_INCONSISTENT",
                f"scenario_id {requested!r} is absent from {table} (present: {sorted(table_ids)})",
                [],
            )
    return requested


# ----------------------------------------------------------------------
# project-timezone timestamp parsing
# ----------------------------------------------------------------------


def _raise_field_time_error(table: str, locator: str, message: str) -> NoReturn:
    raise ResourceRunError(
        "RESOURCE_RUN_TIME_AXIS_INVALID",
        f"{table}.{locator}: {message}",
        [
            _boundary_diagnostic(
                "RESOURCE_RUN_TIME_AXIS_INVALID",
                Severity.ERROR,
                f"{table}.{locator}: {message}",
                table=table,
                locator=locator,
            )
        ],
    )


def _raise_snapshot_time_error(table: str, message: str) -> NoReturn:
    _raise_field_time_error(table, "timestamp", message)


def _coerce_project_tz(values: pd.Series, table: str, locator: str, timezone: str) -> pd.Series:
    """THE single safe time parse for the input boundary: coerce to
    datetime, convert to the PROJECT timezone and validate.

    The absolute instant is preserved (only the representation
    changes). Unparseable / NaT / naive / mixed-timezone inputs raise a
    stable ResourceRunError with table + locator - a pandas
    DateParseError / TypeError / ValueError can never leak. Hourly
    timestamps AND budget period boundaries reuse this same low-level
    rule (no second, semantically-different parser)."""
    parsed = pd.to_datetime(values, errors="coerce", utc=False)
    if parsed.dtype == object:
        kinds: set[object] = set()
        for value in parsed:
            if value is None or pd.isna(value):
                _raise_field_time_error(table, locator, "unparseable or NaT value")
            tzinfo = getattr(value, "tzinfo", None)
            if tzinfo is None:
                _raise_field_time_error(table, locator, "naive value")
            kinds.add(tzinfo)
        if len(kinds) > 1:
            _raise_field_time_error(table, locator, "mixed timezones")
    elif parsed.dt.tz is None:
        _raise_field_time_error(table, locator, "naive value")
    local = parsed.dt.tz_convert(timezone)
    if local.isna().any():
        _raise_field_time_error(table, locator, "NaT value")
    return local


def parse_project_timestamps(values: pd.Series, table: str, timezone: str) -> pd.Series:
    """Parse hourly timestamps and convert to the PROJECT timezone
    (see ``_coerce_project_tz`` for the shared rule)."""
    return _coerce_project_tz(values, table, "timestamp", timezone)


# ----------------------------------------------------------------------
# runtime snapshot selection
# ----------------------------------------------------------------------


def select_runtime_snapshot(
    tables: dict[str, pd.DataFrame],
    scenario_id: str,
    weather_years: tuple[int, ...],
    timezone: str,
) -> dict[str, pd.DataFrame]:
    """The ONE runtime snapshot consumed by validation, every builder,
    the intertie plan, hydro identity, data identity and per-sample
    processing.

    Scenarioized hourly tables are filtered to the selected scenario,
    their timestamps are normalized to the PROJECT timezone (absolute
    instants unchanged) and only the SELECTED weather years (local
    calendar years) are kept. ``hydro_energy_budgets`` keeps budgets
    whose [period_start, period_end) actually overlaps ANY selected
    year's local annual interval (no contiguous min-max span).
    ``renewable_capacities`` keeps rows whose year safely parses to a
    selected year (illegal values never silently disappear). Cross-year
    definition tables (thermal / storage / intertie specs) are never
    dropped."""
    working = dict(tables)
    year_set = set(weather_years)
    for name in _SCENARIOIZED_TABLES:
        if name not in tables or tables[name].empty:
            continue
        frame = tables[name]
        frame = frame[frame["scenario_id"].astype(str) == scenario_id]
        if name == "hydro_energy_budgets":
            # safe parse -> PROJECT timezone (absolute instant kept; no
            # tz_localize(None) wall-clock stripping)
            starts = _coerce_project_tz(frame["period_start"], name, "period_start", timezone)
            ends = _coerce_project_tz(frame["period_end"], name, "period_end", timezone)
            if (starts >= ends).any():
                raise ResourceRunError(
                    "RESOURCE_RUN_TIME_AXIS_INVALID",
                    f"{name}: period_start must be before period_end",
                    [
                        _boundary_diagnostic(
                            "RESOURCE_RUN_TIME_AXIS_INVALID",
                            Severity.ERROR,
                            f"{name}: period_start must be before period_end",
                            table=name,
                            locator="period_start/period_end",
                        )
                    ],
                )
            # half-open [period_start, period_end) overlap with ANY
            # selected year's PROJECT-timezone annual interval (still
            # per-year; never a min-max contiguous span)
            keep = np.zeros(len(frame), dtype=bool)
            for year in sorted(year_set):
                year_start = pd.Timestamp(year, 1, 1, tz=timezone)
                next_start = pd.Timestamp(year + 1, 1, 1, tz=timezone)
                keep |= (starts < next_start) & (ends > year_start)
            frame = frame[keep].copy()
            # normalize the budget boundaries to the PROJECT timezone so
            # UTC-equivalent inputs yield the SAME snapshot / identity
            frame["period_start"] = starts[keep]
            frame["period_end"] = ends[keep]
        else:
            local = parse_project_timestamps(frame["timestamp"], name, timezone)
            # normalized representation (project timezone) - absolute
            # instants unchanged, local calendar year drives the filter
            frame = frame[local.dt.year.isin(year_set)].copy()
            frame["timestamp"] = local[local.dt.year.isin(year_set)]
        working[name] = frame
    if "renewable_capacities" in tables and not tables["renewable_capacities"].empty:
        caps = tables["renewable_capacities"]
        years_parsed = pd.to_numeric(caps["year"], errors="coerce")
        if years_parsed.isna().any():
            raise ResourceRunError(
                "RESOURCE_RUN_STRUCTURE_INVALID",
                "renewable_capacities carries an unparseable year",
                [
                    _boundary_diagnostic(
                        "RESOURCE_RUN_SNAPSHOT_FIELD_INVALID",
                        Severity.ERROR,
                        "renewable_capacities carries an unparseable year",
                        table="renewable_capacities",
                        locator="year",
                    )
                ],
            )
        working["renewable_capacities"] = caps[years_parsed.astype(int).isin(year_set)]
    return working


# ----------------------------------------------------------------------
# canonical time axis (shared validator + lightweight positioning)
# ----------------------------------------------------------------------


def canonical_index_by_timestamp(
    axes: dict[int, tuple[pd.Series, np.ndarray]],
    year: int,
) -> dict[pd.Timestamp, int]:
    return {pd.Timestamp(ts): index for index, ts in enumerate(axes[year][0])}


def align_to_canonical_axis(
    timestamps: pd.Series,
    values: np.ndarray,
    axis_index: dict[pd.Timestamp, int],
    hours: int,
    label: str,
) -> np.ndarray:
    """Align a value series to the canonical hourly axis by timestamp;
    missing / duplicate / extra hours raise stably (never row order).
    This is the ONLY local time-axis logic - full axis validation is
    delegated to ``build_hour_axis``."""
    stamps = pd.to_datetime(timestamps)
    if stamps.duplicated().any():
        raise ResourceRunError(
            "RESOURCE_RUN_TIME_AXIS_INVALID",
            f"{label} carries duplicate timestamps",
            [],
        )
    aligned = np.zeros(hours, dtype=np.float64)
    seen = set()
    for ts, value in zip(stamps, np.asarray(values, dtype=float)):
        index = axis_index.get(pd.Timestamp(ts))
        if index is None:
            raise ResourceRunError(
                "RESOURCE_RUN_TIME_AXIS_INVALID",
                f"{label} timestamp not on the canonical hourly axis",
                [],
            )
        if index in seen:
            raise ResourceRunError(
                "RESOURCE_RUN_TIME_AXIS_INVALID",
                f"{label} carries duplicate canonical hours",
                [],
            )
        seen.add(index)
        aligned[index] = value
    if len(seen) != hours:
        raise ResourceRunError(
            "RESOURCE_RUN_TIME_AXIS_INCOMPLETE",
            f"{label} does not cover the complete canonical hourly axis",
            [],
        )
    return aligned


def canonical_axes_from_load(
    tables: dict[str, pd.DataFrame],
    bank: ScenarioBank,
    years: tuple[int, ...],
    timezone: str,
) -> dict[int, tuple[pd.Series, np.ndarray]]:
    """Canonical hourly axes built from the SELECTED frozen load input
    and cross-checked against the ScenarioBank metadata.

    Validation is delegated to the SHARED ``build_hour_axis``
    (domain/time_axis.py). Row order never matters: the axis is the
    sorted unique timestamp set in the project timezone. Missing /
    duplicate / extra / NaT / naive-timezone inputs raise a stable
    ResourceRunError."""
    load = tables["load_hourly"]
    if load.empty:
        raise ResourceRunError(
            "RESOURCE_RUN_TIME_AXIS_INVALID",
            "load_hourly is empty after scenario/weather-year selection",
            [],
        )
    stamps = parse_project_timestamps(load["timestamp"], "load_hourly", timezone)
    if stamps.duplicated().any():
        _raise_snapshot_time_error("load_hourly", "duplicate timestamps")
    axes: dict[int, tuple[pd.Series, np.ndarray]] = {}
    for year in sorted({int(y) for y in years}):
        year_frame = load[stamps.dt.year == year]
        if year_frame.empty:
            raise ResourceRunError(
                "RESOURCE_RUN_TIME_AXIS_INVALID",
                f"load_hourly has no rows for weather year {year}",
                [],
            )
        ordered = pd.to_datetime(year_frame["timestamp"]).sort_values().reset_index(drop=True)
        try:
            axis = build_hour_axis(
                ordered,
                timezone=str(bank.index["timezone"]),
                weather_year=year,
            )
        except TimeAxisError as error:
            raise ResourceRunError(
                "RESOURCE_RUN_TIME_AXIS_INVALID",
                f"load_hourly year {year}: {error.message}",
                [],
            ) from None
        bank_hours = int(bank.index["year_hours"][str(year)])
        if len(axis.timestamps) != bank_hours:
            raise ResourceRunError(
                "RESOURCE_RUN_TIME_AXIS_INVALID",
                f"load_hourly year {year} has {len(axis.timestamps)} hours "
                f"but the bank declares {bank_hours}",
                [],
            )
        axes[year] = (axis.timestamps, axis.local_dates)
    return axes


# ----------------------------------------------------------------------
# input validation
# ----------------------------------------------------------------------


def validate_resource_run_inputs(
    tables: dict[str, pd.DataFrame],
    config: ResourceRunConfig,
) -> list[ResourceRunDiagnostic]:
    """Structure preflight + selected-snapshot standard validation.

    Standard DQ codes are published DIRECTLY (original code / severity /
    message / table / locator) - the ELCC-specific registry is never the
    gate. Returns the merged STANDARD diagnostics (warnings) for the
    resource run; blocking errors raise ResourceRunError with the same
    unified model."""
    check_snapshot_structure(tables)
    report = validate_standard_tables(tables, config.declared_grade, timezone=config.timezone)
    if report.errors:
        raise ResourceRunError(
            "RESOURCE_RUN_BLOCKED",
            "standard-layer validation failed; resource run cannot proceed",
            merge_resource_run_diagnostics(report.errors, []),
        )
    return merge_resource_run_diagnostics(report.warnings, [])


__all__ = [
    "DEFAULT_DELTA_T_HOURS",
    "ResourceRunConfig",
    "ResourceRunDiagnostic",
    "ResourceRunError",
    "align_to_canonical_axis",
    "canonical_axes_from_load",
    "canonical_index_by_timestamp",
    "check_snapshot_structure",
    "merge_published_diagnostics",
    "merge_resource_run_diagnostics",
    "parse_project_timestamps",
    "resolve_scenario",
    "select_runtime_snapshot",
    "validate_resource_run_config",
    "validate_resource_run_inputs",
]
