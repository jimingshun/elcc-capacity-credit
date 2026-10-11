"""T15 intertie sign migration, conservation, limits, availability,
channel mapping, cross-year DQ-30 mapping and the C-29 exogenous plan
identity (methodology V0.4 §5.4 / §17.14, data contract V0.3 §20/§21).

- legacy template "positive = export, negative = import" migrates to
  two NON-NEGATIVE fields (``scheduled_import_mw`` /
  ``scheduled_export_mw``); local supply contribution =
  import - export; legacy net export = export - import;
- per-channel, per-hour direction exclusivity
  (import x export == 0); DIFFERENT channels may flow opposite
  directions in the same hour;
- import/export never exceed the available (normal / hourly) limits;
  normal limits never exceed the rated capacity; hourly limits above
  rated require an explicitly declared emergency-limit source;
- availability=0 either zeroes the plan (``zero`` policy) or blocks
  with ``INTERTIE_PLAN_INFEASIBLE`` (``infeasible`` policy) - silent
  over-limit flow is never kept;
- far-end extra support is recorded SEPARATELY from the baseline plan
  (``external_support_available_mw``); unknown support stays empty and
  raises the method warning - never fabricated from rated capacity;
- channel association uses ONLY ``intertie_id``; name-based matching
  (G1 -> G2 columns) blocks on ambiguity and returns candidates -
  column order is never used;
- the C-29 exogenous plan identity is a deterministic fingerprint of
  the plan inputs ONLY (channel ids, schedules, availability,
  alignment rules) - a load trial has no input into it;
- DQ-30 ``all_weather_years`` mapping is explicit (profile_alignment +
  leap_day_rule); the string "ALL" never enters the integer weather
  year; leap-day hours are never silently dropped or duplicated;
- strict numeric gates; inputs never mutated; deterministic results.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any


class IntertieError(ValueError):
    """Stable intertie-model error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_float(value: Any, label: str) -> float:
    """float() wrapper that never leaks OverflowError (10**400 etc.)."""
    try:
        return float(value)
    except (OverflowError, ValueError):
        raise IntertieError(
            "INTERTIE_INVALID_INPUT", f"{label} must be a finite real number"
        ) from None


def _strict_nonneg(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IntertieError("INTERTIE_INVALID_INPUT", f"{label} must be a real number")
    number = _safe_float(value, label)
    if not math.isfinite(number):
        raise IntertieError("INTERTIE_INVALID_INPUT", f"{label} must be finite")
    if number < 0.0:
        raise IntertieError("INTERTIE_INVALID_INPUT", f"{label} must be non-negative")
    return number


@dataclass(frozen=True)
class IntertieHourlyRecord:
    """Normalized hourly intertie record (data contract §21.2)."""

    intertie_id: str
    scenario_id: str
    timestamp: datetime
    scheduled_import_mw: float
    scheduled_export_mw: float
    available_import_limit_mw: float | None
    available_export_limit_mw: float | None
    external_support_available_mw: float | None
    source_id: str

    def with_timestamp(self, timestamp: datetime) -> IntertieHourlyRecord:
        import dataclasses

        return dataclasses.replace(self, timestamp=timestamp)


def migrate_legacy_signed_flow(
    values: tuple[float, ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Legacy "positive = export, negative = import" -> two non-negative
    fields (I-01)."""
    if not isinstance(values, tuple):
        raise IntertieError("INTERTIE_INVALID_INPUT", "legacy flow must be a tuple")
    imports = []
    exports = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise IntertieError("INTERTIE_INVALID_INPUT", "legacy flow values must be real numbers")
        numeric = _safe_float(value, "legacy flow")
        if not math.isfinite(numeric):
            raise IntertieError("INTERTIE_INVALID_INPUT", "legacy flow values must be finite")
        number = abs(numeric)
        if numeric > 0:
            exports.append(number)
            imports.append(0.0)
        else:
            imports.append(number)
            exports.append(0.0)
    return tuple(imports), tuple(exports)


def local_net_supply_mw(record: IntertieHourlyRecord) -> float:
    """Local supply contribution: import - export."""
    return float(record.scheduled_import_mw) - float(record.scheduled_export_mw)


def legacy_net_export_mw(record: IntertieHourlyRecord) -> float:
    """Legacy net export: export - import."""
    return float(record.scheduled_export_mw) - float(record.scheduled_import_mw)


@dataclass(frozen=True)
class IntertieClipRecord:
    """Per-direction clip audit: original value, constrained value,
    stable reason code and the full record locator (intertie_id,
    scenario_id, timestamp) - never a silent clip."""

    direction: str  # "import" | "export"
    original_mw: float
    constrained_mw: float
    reason_code: str
    intertie_id: str = ""
    scenario_id: str = ""
    timestamp: datetime | None = None


@dataclass(frozen=True)
class IntertieValidationResult:
    """Audited constrained plan per record (aligned with the input)."""

    import_mw: tuple[float, ...]
    export_mw: tuple[float, ...]
    candidate_extra_import_mw: tuple[float, ...]
    external_support_unknown_warning: bool
    diagnostics: tuple[str, ...]
    clip_audit: tuple[IntertieClipRecord, ...] = ()


@dataclass(frozen=True)
class _SingleRecordValidation:
    scheduled_import: float
    scheduled_export: float
    extra_import: float
    unknown_support: bool
    diagnostics: tuple[str, ...]
    clips: tuple[IntertieClipRecord, ...] = ()


def _apply_availability_policy(
    intertie_id: str,
    available_import: float,
    available_export: float,
    scheduled_import: float,
    scheduled_export: float,
    availability_policy: str,
    diagnostics: list[str],
) -> tuple[float, float]:
    """Unavailable-limit policy per direction (C901 split): zero the
    schedule or raise INTERTIE_PLAN_INFEASIBLE."""
    if available_import <= 0.0 and scheduled_import > 0.0:
        if availability_policy == "infeasible":
            raise IntertieError(
                "INTERTIE_PLAN_INFEASIBLE",
                f"channel {intertie_id!r} is unavailable but the plan schedules import",
            )
        diagnostics.append("INTERTIE_PLAN_ZEROED")
        scheduled_import = 0.0
    if available_export <= 0.0 and scheduled_export > 0.0:
        if availability_policy == "infeasible":
            raise IntertieError(
                "INTERTIE_PLAN_INFEASIBLE",
                f"channel {intertie_id!r} is unavailable but the plan schedules export",
            )
        diagnostics.append("INTERTIE_PLAN_ZEROED")
        scheduled_export = 0.0
    return scheduled_import, scheduled_export


def _clip_direction(
    direction: str,
    scheduled: float,
    available: float,
    strict_limits: bool,
    clips: list[IntertieClipRecord],
    record: IntertieHourlyRecord,
) -> float:
    """Limit enforcement per direction (C901 split): strict mode blocks;
    otherwise the clip is audited with its stable reason code."""
    if scheduled <= available + 1e-12:
        return scheduled
    if strict_limits:
        raise IntertieError(
            "INTERTIE_FLOW_EXCEEDS_LIMIT",
            f"strict mode: scheduled {direction} exceeds the available limit",
        )
    clips.append(
        IntertieClipRecord(
            direction=direction,
            original_mw=scheduled,
            constrained_mw=available,
            reason_code=f"INTERTIE_{direction.upper()}_CLIPPED",
            intertie_id=record.intertie_id,
            scenario_id=record.scenario_id,
            timestamp=record.timestamp,
        )
    )
    return available


def _validate_single_record(
    record: IntertieHourlyRecord,
    *,
    rated: float,
    normal_import: float,
    normal_export: float,
    availability_policy: str,
    emergency_limit_source: str | None,
    strict_limits: bool = False,
) -> _SingleRecordValidation:
    """Per-hour constraint resolution (C901 split): direction
    exclusivity, availability policy, and clipping to the available
    limits (audited; strict mode blocks instead of clipping)."""
    diagnostics: list[str] = []
    clips: list[IntertieClipRecord] = []
    _check_record_identity(record)
    scheduled_import = _strict_nonneg(record.scheduled_import_mw, "scheduled_import_mw")
    scheduled_export = _strict_nonneg(record.scheduled_export_mw, "scheduled_export_mw")
    if scheduled_import > 0.0 and scheduled_export > 0.0:
        raise IntertieError(
            "INTERTIE_SIMULTANEOUS_DIRECTIONS",
            f"channel {record.intertie_id!r} schedules both directions in the same hour",
        )
    available_import = (
        _strict_nonneg(record.available_import_limit_mw, "available_import_limit_mw")
        if record.available_import_limit_mw is not None
        else normal_import
    )
    available_export = (
        _strict_nonneg(record.available_export_limit_mw, "available_export_limit_mw")
        if record.available_export_limit_mw is not None
        else normal_export
    )
    for label, hourly in (
        ("available_import_limit_mw", available_import),
        ("available_export_limit_mw", available_export),
    ):
        if hourly > rated + 1e-12 and not emergency_limit_source:
            raise IntertieError(
                "INTERTIE_EMERGENCY_LIMIT_UNDECLARED",
                f"{label} exceeds the rated capacity and no emergency limit source is declared",
            )
    scheduled_import, scheduled_export = _apply_availability_policy(
        record.intertie_id,
        available_import,
        available_export,
        scheduled_import,
        scheduled_export,
        availability_policy,
        diagnostics,
    )
    scheduled_import = _clip_direction(
        "import", scheduled_import, available_import, strict_limits, clips, record
    )
    scheduled_export = _clip_direction(
        "export", scheduled_export, available_export, strict_limits, clips, record
    )
    if record.external_support_available_mw is None:
        extra = 0.0
        unknown = True
    else:
        extra = _strict_nonneg(
            record.external_support_available_mw,
            "external_support_available_mw",
        )
        unknown = False
    # B.6 physical cap: candidate extra import can never exceed the
    # remaining import room after the baseline plan is constrained
    remaining_import_room = max(0.0, available_import - scheduled_import)
    if extra > remaining_import_room + 1e-12:
        extra = remaining_import_room
        diagnostics.append("INTERTIE_EXTRA_IMPORT_CAPPED")
    return _SingleRecordValidation(
        scheduled_import=scheduled_import,
        scheduled_export=scheduled_export,
        extra_import=extra,
        unknown_support=unknown,
        diagnostics=tuple(sorted(set(diagnostics))),
        clips=tuple(clips),
    )


def _check_availability_policy(availability_policy: str) -> None:
    if availability_policy not in ("zero", "infeasible"):
        raise IntertieError(
            "INTERTIE_INVALID_INPUT",
            "availability_policy must be 'zero' or 'infeasible'",
        )


def validate_intertie_records(
    records: tuple[IntertieHourlyRecord, ...],
    *,
    rated_capacity_mw: float,
    normal_import_limit_mw: float,
    normal_export_limit_mw: float,
    availability_policy: str,
    emergency_limit_source: str | None = None,
    strict_limits: bool = False,
) -> IntertieValidationResult:
    """Validate and constrain a normalized hourly plan (I-02..I-05,
    I-07). Inputs never mutated; deterministic. ``strict_limits``
    blocks instead of clipping (clip_audit records every clip)."""
    if not isinstance(records, tuple) or not records:
        raise IntertieError("INTERTIE_INVALID_INPUT", "records must be a non-empty tuple")
    if not all(isinstance(record, IntertieHourlyRecord) for record in records):
        raise IntertieError(
            "INTERTIE_INVALID_INPUT", "records entries must be IntertieHourlyRecord"
        )
    for record in records:
        _check_record_identity(record)
    channel_ids = {record.intertie_id for record in records}
    if len(channel_ids) != 1:
        raise IntertieError(
            "INTERTIE_MULTI_CHANNEL",
            "validate_intertie_records is the scalar single-channel entry; "
            "use validate_intertie_plan_by_channel with IntertieSpec per "
            f"channel (got {sorted(channel_ids)})",
        )
    rated = _strict_nonneg(rated_capacity_mw, "rated_capacity_mw")
    if rated <= 0.0:
        raise IntertieError("INTERTIE_INVALID_INPUT", "rated_capacity_mw must be positive")
    normal_import = _strict_nonneg(normal_import_limit_mw, "normal_import_limit_mw")
    normal_export = _strict_nonneg(normal_export_limit_mw, "normal_export_limit_mw")
    if normal_import > rated + 1e-12 or normal_export > rated + 1e-12:
        raise IntertieError(
            "INTERTIE_NORMAL_LIMIT_EXCEEDS_RATED",
            "normal limits must not exceed the rated capacity",
        )
    _check_availability_policy(availability_policy)
    diagnostics: list[str] = []
    import_mw: list[float] = []
    export_mw: list[float] = []
    extra_import: list[float] = []
    clip_audit: list[IntertieClipRecord] = []
    unknown_support = False
    for record in records:
        validated = _validate_single_record(
            record,
            rated=rated,
            normal_import=normal_import,
            normal_export=normal_export,
            availability_policy=availability_policy,
            emergency_limit_source=emergency_limit_source,
            strict_limits=strict_limits,
        )
        diagnostics.extend(validated.diagnostics)
        unknown_support = unknown_support or validated.unknown_support
        import_mw.append(validated.scheduled_import)
        export_mw.append(validated.scheduled_export)
        extra_import.append(validated.extra_import)
        clip_audit.extend(validated.clips)
    return IntertieValidationResult(
        import_mw=tuple(import_mw),
        export_mw=tuple(export_mw),
        candidate_extra_import_mw=tuple(extra_import),
        external_support_unknown_warning=unknown_support,
        diagnostics=tuple(sorted(set(diagnostics))),
        clip_audit=tuple(clip_audit),
    )


@dataclass(frozen=True)
class IntertieSpec:
    """Per-channel rated / normal limits (B.7): a single validation
    never shares one fuzzy limit set across channels."""

    intertie_id: str
    rated_capacity_mw: float
    normal_import_limit_mw: float
    normal_export_limit_mw: float


def _validate_intertie_specs(
    specs: tuple[IntertieSpec, ...],
) -> dict[str, tuple[float, float, float]]:
    """Per-channel spec gates (C901 split): unique ids, strict numerics,
    normal limits within rated capacity."""
    spec_map: dict[str, tuple[float, float, float]] = {}
    for spec in specs:
        if not isinstance(spec, IntertieSpec):
            raise IntertieError("INTERTIE_INVALID_INPUT", "specs entries must be IntertieSpec")
        if not isinstance(spec.intertie_id, str) or not spec.intertie_id.strip():
            raise IntertieError(
                "INTERTIE_INVALID_INPUT", "spec intertie_id must be a non-empty string"
            )
        if spec.intertie_id in spec_map:
            raise IntertieError(
                "INTERTIE_INVALID_INPUT", f"duplicate intertie spec {spec.intertie_id!r}"
            )
        rated = _strict_nonneg(spec.rated_capacity_mw, "rated_capacity_mw")
        if rated <= 0.0:
            raise IntertieError("INTERTIE_INVALID_INPUT", "rated_capacity_mw must be positive")
        normal_import = _strict_nonneg(spec.normal_import_limit_mw, "normal_import_limit_mw")
        normal_export = _strict_nonneg(spec.normal_export_limit_mw, "normal_export_limit_mw")
        if normal_import > rated + 1e-12 or normal_export > rated + 1e-12:
            raise IntertieError(
                "INTERTIE_NORMAL_LIMIT_EXCEEDS_RATED",
                "normal limits must not exceed the rated capacity",
            )
        spec_map[spec.intertie_id] = (rated, normal_import, normal_export)
    return spec_map


def validate_intertie_plan_by_channel(
    records: tuple[IntertieHourlyRecord, ...],
    *,
    specs: tuple[IntertieSpec, ...],
    availability_policy: str,
    emergency_limit_source: str | None = None,
    strict_limits: bool = False,
) -> IntertieValidationResult:
    """Per-channel validation entry: each record is constrained by its
    OWN IntertieSpec; unknown channels block with
    INTERTIE_UNKNOWN_CHANNEL (never silently shared limits)."""
    if not isinstance(records, tuple) or not records:
        raise IntertieError("INTERTIE_INVALID_INPUT", "records must be a non-empty tuple")
    if not all(isinstance(record, IntertieHourlyRecord) for record in records):
        raise IntertieError(
            "INTERTIE_INVALID_INPUT", "records entries must be IntertieHourlyRecord"
        )
    if not isinstance(specs, tuple) or not specs:
        raise IntertieError("INTERTIE_INVALID_INPUT", "specs must be a non-empty tuple")
    _check_availability_policy(availability_policy)
    spec_map = _validate_intertie_specs(specs)
    diagnostics: list[str] = []
    import_mw: list[float] = []
    export_mw: list[float] = []
    extra_import: list[float] = []
    clip_audit: list[IntertieClipRecord] = []
    unknown_support = False
    for record in records:
        channel = spec_map.get(record.intertie_id)
        if channel is None:
            raise IntertieError(
                "INTERTIE_UNKNOWN_CHANNEL",
                f"no IntertieSpec for channel {record.intertie_id!r}",
            )
        validated = _validate_single_record(
            record,
            rated=channel[0],
            normal_import=channel[1],
            normal_export=channel[2],
            availability_policy=availability_policy,
            emergency_limit_source=emergency_limit_source,
            strict_limits=strict_limits,
        )
        diagnostics.extend(validated.diagnostics)
        unknown_support = unknown_support or validated.unknown_support
        import_mw.append(validated.scheduled_import)
        export_mw.append(validated.scheduled_export)
        extra_import.append(validated.extra_import)
        clip_audit.extend(validated.clips)
    return IntertieValidationResult(
        import_mw=tuple(import_mw),
        export_mw=tuple(export_mw),
        candidate_extra_import_mw=tuple(extra_import),
        external_support_unknown_warning=unknown_support,
        diagnostics=tuple(sorted(set(diagnostics))),
        clip_audit=tuple(clip_audit),
    )


def match_intertie_channels(
    *,
    source_names: tuple[str, ...],
    column_names: tuple[str, ...],
) -> dict[str, str]:
    """G1 name -> G2 column association (I-06): only unambiguous
    exact-name matches resolve; ambiguity blocks with candidates -
    column order is NEVER used."""
    resolved: dict[str, str] = {}
    for source_name in source_names:
        matches = [column for column in column_names if column.startswith(source_name)]
        if len(matches) == 1:
            resolved[source_name] = matches[0]
        elif len(matches) > 1:
            raise IntertieError(
                "INTERTIE_CHANNEL_AMBIGUOUS",
                f"source name {source_name!r} matches multiple columns "
                f"{matches}; explicit mapping required (column order never used)",
            )
    return resolved


_VALID_ALIGNMENTS = ("hour_of_year", "month_day_hour")
_MIN_YEAR = 1900
_MAX_YEAR = 2200


def validate_profile_scope(
    *,
    profile_scope: str,
    weather_year: object,
    profile_alignment: str | None,
    leap_day_rule: str | None,
    target_year: int,
) -> None:
    """DQ-30 scope rules: specific_weather_year requires an INTEGER
    weather year (the string 'ALL' never enters); all_weather_years
    requires an empty weather year AND an explicit profile_alignment
    from the closed whitelist (hour_of_year | month_day_hour). The
    target year must be a strict integer in a reasonable range."""
    if (
        isinstance(target_year, bool)
        or not isinstance(target_year, int)
        or not _MIN_YEAR <= target_year <= _MAX_YEAR
    ):
        raise IntertieError(
            "INTERTIE_INVALID_YEAR",
            f"target_year must be a strict integer in {_MIN_YEAR}..{_MAX_YEAR}",
        )
    if profile_scope == "specific_weather_year":
        if (
            isinstance(weather_year, bool)
            or not isinstance(weather_year, int)
            or not _MIN_YEAR <= weather_year <= _MAX_YEAR
        ):
            raise IntertieError(
                "INTERTIE_INVALID_WEATHER_YEAR",
                "specific_weather_year requires an integer weather_year (never the string 'ALL')",
            )
    elif profile_scope == "all_weather_years":
        if weather_year is not None:
            raise IntertieError(
                "INTERTIE_INVALID_WEATHER_YEAR",
                "all_weather_years requires weather_year to be empty",
            )
        if not isinstance(profile_alignment, str) or not profile_alignment.strip():
            raise IntertieError(
                "INTERTIE_ALIGNMENT_REQUIRED",
                "all_weather_years requires an explicit profile_alignment",
            )
        if profile_alignment not in _VALID_ALIGNMENTS:
            raise IntertieError(
                "INTERTIE_INVALID_ALIGNMENT",
                f"profile_alignment must be one of {_VALID_ALIGNMENTS}, got {profile_alignment!r}",
            )
        if leap_day_rule is not None and leap_day_rule not in (
            "drop_leap_day",
            "copy_feb28",
        ):
            raise IntertieError(
                "INTERTIE_INVALID_LEAP_RULE",
                f"unknown leap_day_rule {leap_day_rule!r}",
            )
    else:
        raise IntertieError("INTERTIE_INVALID_SCOPE", f"unknown profile_scope {profile_scope!r}")


@dataclass(frozen=True)
class AllWeatherYearsMapping:
    """Explicit all_weather_years -> target-year mapping result with
    audit identity (DQ-30). ``mapping_pairs`` is the ordered
    source-key -> target-key relation; ``dropped_keys`` records every
    explicitly dropped source hour; ``mapping_identity`` combines the
    structural mapping with the mapped-content fingerprint so input
    plan drift is always discoverable."""

    records: tuple[IntertieHourlyRecord, ...]
    affected_leap_hours: int
    leap_day_rule: str
    target_year: int
    mapping_identity: str
    mapping_pairs: tuple[tuple[str, str], ...]
    dropped_keys: tuple[str, ...]
    content_fingerprint: str


def _timestamp_key(timestamp: datetime) -> str:
    """Stable positional key ``MM-DDTHH`` for mapping audits."""
    return f"{timestamp.month:02d}-{timestamp.day:02d}T{timestamp.hour:02d}"


def _expected_axis_keys(year: int) -> list[tuple[int, int, int]]:
    """Complete hourly (month, day, hour) axis for one calendar year."""
    count = 8784 if _is_leap(year) else 8760
    start = datetime(year, 1, 1, tzinfo=UTC)
    return [
        (
            (start + timedelta(hours=h)).month,
            (start + timedelta(hours=h)).day,
            (start + timedelta(hours=h)).hour,
        )
        for h in range(count)
    ]


def _validate_mapping_records(
    records: tuple[IntertieHourlyRecord, ...],
) -> list[IntertieHourlyRecord]:
    """B.3 strict record validation (C901 split): entry types, non-empty
    ids, timestamp type / timezone / ordering / uniqueness and the
    completeness of the source axis. Never leaks native attribute
    errors."""
    if not isinstance(records, tuple) or not records:
        raise IntertieError("INTERTIE_INVALID_INPUT", "records must be a non-empty tuple")
    ordered: list[IntertieHourlyRecord] = []
    seen_keys: set[tuple[str, str, datetime]] = set()
    previous: datetime | None = None
    tzinfo = None
    tz_seen = False
    for record in records:
        if not isinstance(record, IntertieHourlyRecord):
            raise IntertieError(
                "INTERTIE_INVALID_INPUT", "records entries must be IntertieHourlyRecord"
            )
        _check_record_identity(record)
        timestamp = record.timestamp
        if not tz_seen:
            tzinfo = timestamp.tzinfo
            tz_seen = True
        elif timestamp.tzinfo != tzinfo:
            raise IntertieError(
                "INTERTIE_TIMESTAMP_TZ_MISMATCH",
                "all record timestamps must share the same timezone",
            )
        key = (record.intertie_id, record.scenario_id, timestamp)
        if key in seen_keys:
            raise IntertieError(
                "INTERTIE_DUPLICATE_RECORD",
                f"duplicate intertie_id + scenario_id + timestamp {key}",
            )
        seen_keys.add(key)
        if previous is not None and timestamp <= previous:
            raise IntertieError(
                "INTERTIE_TIMESTAMP_UNORDERED",
                "records must be strictly increasing in time",
            )
        previous = timestamp
        ordered.append(record)
    return _check_source_axis_complete(ordered)


def _check_record_identity(record: IntertieHourlyRecord) -> None:
    """B.3 identity gates (C901 split): non-empty ids, datetime
    timestamp. Never leaks native attribute errors."""
    for name, value in (
        ("intertie_id", record.intertie_id),
        ("scenario_id", record.scenario_id),
        ("source_id", record.source_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise IntertieError("INTERTIE_INVALID_INPUT", f"{name} must be a non-empty string")
    if not isinstance(record.timestamp, datetime):
        raise IntertieError("INTERTIE_INVALID_INPUT", "record.timestamp must be a datetime")


def _check_source_axis_complete(
    ordered: list[IntertieHourlyRecord],
) -> list[IntertieHourlyRecord]:
    """B.3 source-axis gates (C901 split): single source year and the
    complete hourly axis of that year."""
    source_years = {record.timestamp.year for record in ordered}
    if len(source_years) != 1:
        raise IntertieError(
            "INTERTIE_INVALID_SCOPE",
            "all_weather_years profiles must carry a single source year",
        )
    source_year = next(iter(source_years))
    actual_keys = sorted(
        (record.timestamp.month, record.timestamp.day, record.timestamp.hour) for record in ordered
    )
    if actual_keys != _expected_axis_keys(source_year):
        raise IntertieError(
            "INTERTIE_INCOMPLETE_AXIS",
            "the source profile must cover the complete hourly axis of "
            "its source year (every month/day/hour exactly once)",
        )
    return ordered


def _map_hour_of_year(
    ordered: list[IntertieHourlyRecord],
    source_year: int,
    target_year: int,
    leap_day_rule: str | None,
    tzinfo: tzinfo | None,
) -> tuple[list[IntertieHourlyRecord], list[tuple[str, str]], list[str], int]:
    """hour_of_year branch: source hour-of-year positions map onto
    target positions; leap boundary handled by dropping the Feb-29
    positions (leap source -> common target) or inserting copies at the
    Feb-29 positions (common source -> leap target)."""
    source_leap = _is_leap(source_year)
    target_leap = _is_leap(target_year)
    target_start = datetime(target_year, 1, 1, tzinfo=tzinfo)
    kept: list[IntertieHourlyRecord] = []
    pairs: list[tuple[str, str]] = []
    dropped: list[str] = []
    affected = 0
    for record in ordered:
        timestamp = record.timestamp
        position = (timestamp.timetuple().tm_yday - 1) * 24 + timestamp.hour
        source_key = _timestamp_key(timestamp)
        if timestamp.month == 2 and timestamp.day == 29:
            if not target_leap:
                dropped.append(source_key)
                affected += 1
                continue
            target_position = position
        else:
            target_position = position
            if (
                target_leap
                and not source_leap
                and leap_day_rule == "copy_feb28"
                and position >= 1416
            ):
                target_position = position + 24
            elif (
                not target_leap
                and source_leap
                and leap_day_rule == "drop_leap_day"
                and position >= 1440
            ):
                target_position = position - 24
        target_timestamp = target_start + timedelta(hours=target_position)
        kept.append(record.with_timestamp(target_timestamp))
        pairs.append((source_key, _timestamp_key(target_timestamp)))
    if target_leap and not source_leap and leap_day_rule == "copy_feb28":
        feb28 = [r for r in kept if r.timestamp.month == 2 and r.timestamp.day == 28]
        for record in feb28:
            source_key = _timestamp_key(record.timestamp.replace(year=source_year))
            target_timestamp = record.timestamp.replace(day=29)
            kept.append(record.with_timestamp(target_timestamp))
            pairs.append((source_key, _timestamp_key(target_timestamp)))
            affected += 1
    kept.sort(key=lambda record: record.timestamp)
    pairs.sort(key=lambda pair: pair[1])
    return kept, pairs, dropped, affected


def _map_month_day_hour(
    ordered: list[IntertieHourlyRecord],
    source_year: int,
    target_year: int,
    leap_day_rule: str | None,
) -> tuple[list[IntertieHourlyRecord], list[tuple[str, str]], list[str], int]:
    """month_day_hour branch: the same calendar (month, day, hour) is
    rebuilt in the target year; Feb-29 is explicitly dropped when the
    target is common and Feb-28 explicitly duplicated when the target
    is leap."""
    target_leap = _is_leap(target_year)
    source_leap = _is_leap(source_year)
    kept: list[IntertieHourlyRecord] = []
    pairs: list[tuple[str, str]] = []
    dropped: list[str] = []
    affected = 0
    for record in ordered:
        timestamp = record.timestamp
        source_key = _timestamp_key(timestamp)
        if timestamp.month == 2 and timestamp.day == 29 and not target_leap:
            dropped.append(source_key)
            affected += 1
            continue
        target_timestamp = timestamp.replace(year=target_year)
        kept.append(record.with_timestamp(target_timestamp))
        pairs.append((source_key, _timestamp_key(target_timestamp)))
    if target_leap and not source_leap and leap_day_rule == "copy_feb28":
        feb28 = [r for r in kept if r.timestamp.month == 2 and r.timestamp.day == 28]
        for record in feb28:
            source_key = _timestamp_key(record.timestamp.replace(year=source_year))
            target_timestamp = record.timestamp.replace(day=29)
            kept.append(record.with_timestamp(target_timestamp))
            pairs.append((source_key, _timestamp_key(target_timestamp)))
            affected += 1
    kept.sort(key=lambda record: record.timestamp)
    pairs.sort(key=lambda pair: pair[1])
    return kept, pairs, dropped, affected


def map_all_weather_years_profile(
    records: tuple[IntertieHourlyRecord, ...],
    *,
    profile_alignment: str,
    leap_day_rule: str | None,
    target_year: int,
) -> AllWeatherYearsMapping:
    """Map an all_weather_years profile onto one target year (DQ-30).

    Two EXPLICIT branches participate in the mapping logic:
    ``hour_of_year`` keeps hour-of-year positions (Feb-29 positions
    dropped / inserted at the leap boundary) and ``month_day_hour``
    rebuilds the same calendar day in the target year. For complete
    single-year hourly profiles the branches are equivalent (proven by
    test); the chosen alignment is audited in the identity and must
    never be written without participating in the mapping. Leap-day
    hours are never silently dropped or duplicated: every dropped /
    duplicated hour is recorded in the audit and the mapping identity
    combines the structural mapping with the mapped-content
    fingerprint."""
    ordered = _validate_mapping_records(records)
    validate_profile_scope(
        profile_scope="all_weather_years",
        weather_year=None,
        profile_alignment=profile_alignment,
        leap_day_rule=leap_day_rule,
        target_year=target_year,
    )
    source_year = ordered[0].timestamp.year
    _check_leap_boundary_rules(source_year, target_year, leap_day_rule)

    if profile_alignment == "hour_of_year":
        kept, pairs, dropped, affected = _map_hour_of_year(
            ordered, source_year, target_year, leap_day_rule, ordered[0].timestamp.tzinfo
        )
    else:  # month_day_hour (whitelist enforced upstream)
        kept, pairs, dropped, affected = _map_month_day_hour(
            ordered, source_year, target_year, leap_day_rule
        )
    target_keys = sorted(
        (record.timestamp.month, record.timestamp.day, record.timestamp.hour) for record in kept
    )
    if target_keys != _expected_axis_keys(target_year):
        raise IntertieError(
            "INTERTIE_INCOMPLETE_AXIS",
            "the mapped target profile must cover the complete hourly axis of the target year",
        )
    structure_payload = (
        f"{profile_alignment}|{leap_day_rule}|{source_year}|{target_year}|"
        f"{sorted({record.intertie_id for record in records})}"
    )
    content_payload = "|".join(
        f"{record.intertie_id}|{record.scenario_id}|{record.scheduled_import_mw}|"
        f"{record.scheduled_export_mw}|{record.available_import_limit_mw}|"
        f"{record.available_export_limit_mw}|{record.timestamp.isoformat()}"
        for record in sorted(kept, key=lambda r: (r.intertie_id, r.timestamp))
    )
    content_fingerprint = hashlib.sha256(content_payload.encode("utf-8")).hexdigest()
    return AllWeatherYearsMapping(
        records=tuple(kept),
        affected_leap_hours=affected,
        leap_day_rule=leap_day_rule or "",
        target_year=target_year,
        mapping_identity=hashlib.sha256(
            f"{structure_payload}|{content_fingerprint}".encode()
        ).hexdigest(),
        mapping_pairs=tuple(pairs),
        dropped_keys=tuple(dropped),
        content_fingerprint=content_fingerprint,
    )


def _check_leap_boundary_rules(
    source_year: int,
    target_year: int,
    leap_day_rule: str | None,
) -> None:
    """DQ-30 leap-boundary gates (C901 split): crossing a leap boundary
    requires the explicit matching rule - never a silent drop or
    duplication."""
    source_leap = _is_leap(source_year)
    target_leap = _is_leap(target_year)
    if source_leap != target_leap and leap_day_rule is None:
        raise IntertieError(
            "INTERTIE_LEAP_RULE_REQUIRED",
            f"mapping across a leap boundary (source {source_year} -> "
            f"target {target_year}) requires an explicit leap_day_rule",
        )
    if not source_leap and target_leap and leap_day_rule != "copy_feb28":
        raise IntertieError(
            "INTERTIE_LEAP_RULE_REQUIRED",
            "mapping a common-year source onto a leap-year target requires "
            "leap_day_rule='copy_feb28' (never a silent Feb-29 duplication)",
        )
    if source_leap and not target_leap and leap_day_rule != "drop_leap_day":
        raise IntertieError(
            "INTERTIE_LEAP_RULE_REQUIRED",
            "mapping a leap-year source onto a common-year target requires "
            "leap_day_rule='drop_leap_day' (never a silent Feb-29 drop)",
        )


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


@dataclass(frozen=True)
class IntertiePlanIdentity:
    """C-29 deterministic exogenous plan identity: a fingerprint of the
    PLAN inputs only (channel ids, scenario ids, scheduled flows,
    availability, alignment rules). Load trials have no input into it.
    ``support_fingerprint`` covers the candidate extra-import decision
    input (external_support_available_mw) SEPARATELY - changing it
    changes this controlled identity even when the baseline plan stays
    identical; T16 resource-run identity consumes plan fingerprint +
    support fingerprint + spec identity."""

    fingerprint: str
    channel_ids: tuple[str, ...]
    support_fingerprint: str = ""


def _normalize_identity_number(value: object, label: str) -> float:
    """Identity serialization gate (fourth review): every numeric field
    entering a fingerprint is validated AND normalized to a controlled
    finite float BEFORE any hash is computed (bool / NaN / +-inf /
    overflow / illegal objects never produce an identity; int 1 and
    1.0 serialize identically; -0.0 normalizes to 0.0)."""
    number = _strict_nonneg(value, label)
    if number == 0.0:
        return 0.0
    return number


def compute_intertie_plan_identity(
    records: tuple[IntertieHourlyRecord, ...],
    *,
    alignment_rule: str,
    leap_day_rule: str,
) -> IntertiePlanIdentity:
    """Deterministic identity of the baseline scheduled exchange plan
    (C-29 / methodology §17.14). Illegal records and unknown rules
    block with stable errors (never a native attribute leak). The
    identity covers the PLAN inputs only: channel ids, scenario ids,
    schedules, availability, alignment rules. Rated / normal limits are
    deliberately EXCLUDED - they carry their own identity via
    ``compute_intertie_spec_identity`` and T16 must combine both into
    the resource-run identity."""
    if not isinstance(records, tuple) or not records:
        raise IntertieError("INTERTIE_INVALID_INPUT", "records must be a non-empty tuple")
    seen_keys: set[tuple[str, str, datetime]] = set()
    tzinfo = None
    tz_seen = False
    normalized: list[
        tuple[IntertieHourlyRecord, float, float, float | None, float | None, float | None]
    ] = []
    for record in records:
        if not isinstance(record, IntertieHourlyRecord):
            raise IntertieError(
                "INTERTIE_INVALID_INPUT", "records entries must be IntertieHourlyRecord"
            )
        _check_record_identity(record)
        if not tz_seen:
            tzinfo = record.timestamp.tzinfo
            tz_seen = True
        elif record.timestamp.tzinfo != tzinfo:
            raise IntertieError(
                "INTERTIE_TIMESTAMP_TZ_MISMATCH",
                "all identity timestamps must share the same timezone",
            )
        import_norm = _normalize_identity_number(record.scheduled_import_mw, "scheduled_import_mw")
        export_norm = _normalize_identity_number(record.scheduled_export_mw, "scheduled_export_mw")
        available_import_norm = (
            None
            if record.available_import_limit_mw is None
            else _normalize_identity_number(
                record.available_import_limit_mw, "available_import_limit_mw"
            )
        )
        available_export_norm = (
            None
            if record.available_export_limit_mw is None
            else _normalize_identity_number(
                record.available_export_limit_mw, "available_export_limit_mw"
            )
        )
        support_norm = (
            None
            if record.external_support_available_mw is None
            else _normalize_identity_number(
                record.external_support_available_mw,
                "external_support_available_mw",
            )
        )
        key = (record.intertie_id, record.scenario_id, record.timestamp)
        if key in seen_keys:
            raise IntertieError(
                "INTERTIE_DUPLICATE_RECORD",
                f"duplicate intertie_id + scenario_id + timestamp {key}",
            )
        seen_keys.add(key)
        normalized.append(
            (
                record,
                import_norm,
                export_norm,
                available_import_norm,
                available_export_norm,
                support_norm,
            )
        )
    if not isinstance(alignment_rule, str) or alignment_rule not in _VALID_ALIGNMENTS:
        raise IntertieError(
            "INTERTIE_INVALID_ALIGNMENT",
            f"alignment_rule must be one of {_VALID_ALIGNMENTS}, got {alignment_rule!r}",
        )
    if not isinstance(leap_day_rule, str) or leap_day_rule not in (
        "",
        "drop_leap_day",
        "copy_feb28",
    ):
        raise IntertieError(
            "INTERTIE_INVALID_LEAP_RULE",
            f"unknown leap_day_rule {leap_day_rule!r}",
        )
    ordered = sorted(
        normalized,
        key=lambda item: (item[0].intertie_id, item[0].scenario_id, item[0].timestamp),
    )
    payload_parts = [
        f"{record.intertie_id}|{record.scenario_id}|{import_norm}|"
        f"{export_norm}|{available_import_norm}|{available_export_norm}|"
        f"{record.timestamp.isoformat()}"
        for record, import_norm, export_norm, available_import_norm, available_export_norm, _support_norm in ordered
    ]
    payload = f"{alignment_rule}|{leap_day_rule}|" + "|".join(payload_parts)
    support_payload = "|".join(
        f"{record.intertie_id}|{record.scenario_id}|{record.timestamp.isoformat()}|{support_norm}"
        for record, _import_norm, _export_norm, _available_import_norm, _available_export_norm, support_norm in ordered
    )
    return IntertiePlanIdentity(
        fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        channel_ids=tuple(sorted({item[0].intertie_id for item in normalized})),
        support_fingerprint=hashlib.sha256(support_payload.encode("utf-8")).hexdigest(),
    )


def compute_intertie_spec_identity(specs: tuple[IntertieSpec, ...]) -> str:
    """C-29 supplement: deterministic identity of the rated / normal
    limit set ONLY. Changing any channel limit changes this fingerprint;
    it is deliberately separate from the plan identity - T16 must
    combine plan identity + spec identity into the resource-run
    identity (a limit change can never leave the identity unchanged
    without an audit trail)."""
    if not isinstance(specs, tuple) or not specs:
        raise IntertieError("INTERTIE_INVALID_INPUT", "specs must be a non-empty tuple")
    spec_map = _validate_intertie_specs(specs)
    payload = "|".join(
        f"{intertie_id}|{rated}|{normal_import}|{normal_export}"
        for intertie_id, (rated, normal_import, normal_export) in sorted(spec_map.items())
    )
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "AllWeatherYearsMapping",
    "IntertieClipRecord",
    "IntertieError",
    "IntertieHourlyRecord",
    "IntertiePlanIdentity",
    "IntertieSpec",
    "IntertieValidationResult",
    "compute_intertie_plan_identity",
    "compute_intertie_spec_identity",
    "legacy_net_export_mw",
    "local_net_supply_mw",
    "map_all_weather_years_profile",
    "match_intertie_channels",
    "migrate_legacy_signed_flow",
    "validate_intertie_plan_by_channel",
    "validate_intertie_records",
    "validate_profile_scope",
]
