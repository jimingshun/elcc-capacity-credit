"""Scenario bank orchestration and domain index (T07 review).

Content-addressed, immutable, local scenario bank. Orchestration lives
here; file-level persistence (safe paths, array IO, staging publish,
mmap batch reads) lives in ``scenarios.persistence``.

- all input validation (object ids, years, roles, hours, batch size)
  happens before any staging directory or array file is created;
  unsafe object ids are rejected with SCENARIO_BANK_UNSAFE_OBJECT_ID
  and never reach file paths (array file names use the SHA-256 of the
  normalized object key);
- object ids and namespaces are normalized (strip + NFKC) everywhere:
  stream keys, file names and index fields; normalized duplicates block;
- one array file per object and per weather year, shape
  (samples, hours_of_year); hours come from
  ``time_axis.expected_hours_for_year`` (leap/plain years coexist);
- arrays reflect planned outages, commissioning/retirement boundaries,
  delta_t_hours and the initial-state policy; FOR-only objects carry a
  machine-readable THERMAL_DURATION_UNAVAILABLE diagnostic in the index;
- ``scenario_bank_id`` covers business metadata AND array content
  hashes (never the id itself, timestamps or local paths);
- ``open_scenario_bank`` verifies the full index contract, including
  explicit object_id/weather_year mapping on every array entry.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from capacity_credit.application.atomic_io import hash_payload
from capacity_credit.domain.time_axis import expected_hours_for_year
from capacity_credit.scenarios.persistence import (
    ScenarioBankError,
    file_sha256,
    publish_staging,
    read_array_mmap,
    read_json,
    safe_rel_path,
    validate_batch_indices,
    validate_batch_size,
    write_array_batched,
    write_json_atomic,
)
from capacity_credit.scenarios.random_streams import (
    make_generator,
    normalize_stream_key,
)
from capacity_credit.scenarios.thermal_states import (
    OutageEvent,
    ThermalMode,
    ThermalParams,
    ThermalStateError,
    simulate_unit,
)

SCENARIO_BANK_SCHEMA_VERSION = "1"
OBJECT_STREAM_VERSION = "1"
ARRAY_DTYPE = np.float32
BATCH_SIZE = 32
_YEAR_ROLES = ("probability_baseline", "stress_test")
_ID_HASH_LEN = 16

@dataclass(frozen=True)
class ScenarioObject:
    object_id: str
    namespace: str
    capacity_mw: float
    params: ThermalParams
    planned_outages: tuple[OutageEvent, ...] = ()
    commissioning_hour: int | None = None
    retirement_hour: int | None = None
    delta_t_hours: float = 1.0
    initial_state: bool | None = None


@dataclass(frozen=True)
class _NormalizedObject:
    """Per-object normalized record (never a dict keyed by the raw id,
    so identical raw ids cannot silently collapse)."""

    original: ScenarioObject
    object_id: str
    namespace: str


def _normalize_identifier(value: str) -> str:
    normalized = normalize_stream_key(value)
    if not normalized:
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSAFE_OBJECT_ID", "object ids must not be blank"
        )
    return normalized


def _initial_state_policy(obj: ScenarioObject) -> str:
    """Policy consistent with the mode and the explicit initial_state."""
    if obj.params.mode == ThermalMode.SEQUENTIAL:
        return "explicit" if obj.initial_state is not None else "steady_state"
    if obj.params.mode == ThermalMode.INDEPENDENT_HOURLY:
        return "independent_draw"
    return "deterministic"


def _is_safe_identifier(value: str) -> bool:
    if not value:
        return False
    if "/" in value or "\\" in value or ":" in value or "\x00" in value:
        return False
    if value.startswith("."):
        return False
    return value not in (".", "..")


def _array_rel_path(object_key_hash: str, weather_year: int) -> str:
    return safe_rel_path(f"arrays/obj_{object_key_hash}_y{weather_year}.npy")


def _objects_payload(
    records: list[_NormalizedObject],
) -> list[dict[str, Any]]:
    payloads = []
    for record in records:
        obj = record.original
        entry = {
            "object_id": record.object_id,
            "normalized_object_key": record.object_id,
            "namespace": record.namespace,
            "capacity_mw": float(obj.capacity_mw),
            "mode": obj.params.mode.value,
            "for_rate": obj.params.for_rate,
            "mttf_hours": obj.params.mttf_hours,
            "mttr_hours": obj.params.mttr_hours,
            "delta_t_hours": float(obj.delta_t_hours),
            "commissioning_hour": obj.commissioning_hour,
            "retirement_hour": obj.retirement_hour,
            "initial_state": obj.initial_state,
            "initial_state_policy": _initial_state_policy(obj),
            "planned_outages": [
                {
                    "outage_id": e.outage_id,
                    "start_hour": e.start_hour,
                    "end_hour": e.end_hour,
                    "available_fraction": float(e.available_fraction),
                    "status": e.status,
                    "source_id": e.source_id,
                }
                for e in obj.planned_outages
            ],
        }
        if obj.params.mode == ThermalMode.INDEPENDENT_HOURLY:
            entry["diagnostics"] = [
                {
                    "code": "THERMAL_DURATION_UNAVAILABLE",
                    "severity": "warning",
                    "message": (
                        "independent hourly mode cannot support forced-outage "
                        "duration conclusions; maximum data grade is G1 "
                        "initial review"
                    ),
                }
            ]
        else:
            entry["diagnostics"] = []
        payloads.append(entry)
    return payloads


def _validate_objects_before_write(
    objects: list[ScenarioObject], hours_map: dict[int, int]
) -> list[_NormalizedObject]:
    """Full input validation of every object BEFORE any file write.

    Returns per-object normalized records (a list, never a dict keyed by
    the raw id, so identical raw ids cannot silently collapse).
    Raises ScenarioBankError with stable codes otherwise.
    """
    if not objects:
        raise ScenarioBankError(
            "SCENARIO_BANK_EMPTY_OBJECTS",
            "scenario bank requires at least one random object",
        )
    if not isinstance(objects, list) or any(
        not isinstance(obj, ScenarioObject) for obj in objects
    ):
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_OBJECT", "objects must be ScenarioObject instances"
        )
    records: list[_NormalizedObject] = []
    for obj in objects:
        if not isinstance(obj.object_id, str):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT", "object_id must be a string"
            )
        raw = obj.object_id
        if not _is_safe_identifier(raw):
            raise ScenarioBankError(
                "SCENARIO_BANK_UNSAFE_OBJECT_ID",
                f"unsafe object id {raw!r} (path components, drive or NUL)",
            )
        key = _normalize_identifier(raw)
        if not _is_safe_identifier(key):
            raise ScenarioBankError(
                "SCENARIO_BANK_UNSAFE_OBJECT_ID",
                f"unsafe normalized object id {key!r}",
            )
        if not isinstance(obj.namespace, str):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT", "namespace must be a string"
            )
        ns = normalize_stream_key(obj.namespace)
        if not ns:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT", "namespace must not be blank"
            )
        if not isinstance(obj.params, ThermalParams):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT", "params must be a ThermalParams"
            )
        if not isinstance(obj.capacity_mw, (int, float, np.integer, np.floating)) \
                or isinstance(obj.capacity_mw, bool):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "capacity_mw must be a finite number",
            )
        if not np.isfinite(float(obj.capacity_mw)) or float(obj.capacity_mw) <= 0.0:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "capacity_mw must be finite and strictly positive",
            )
        if not isinstance(obj.delta_t_hours, (int, float, np.integer, np.floating)) \
                or isinstance(obj.delta_t_hours, bool):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "delta_t_hours must be a finite number",
            )
        if not np.isfinite(float(obj.delta_t_hours)) \
                or float(obj.delta_t_hours) <= 0.0:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "delta_t_hours must be finite and strictly positive",
            )
        if obj.initial_state is not None and not isinstance(obj.initial_state, bool):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "initial_state must be a bool or None",
            )
        if obj.initial_state is not None and obj.params.mode != ThermalMode.SEQUENTIAL:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "initial_state is only meaningful for SEQUENTIAL mode",
            )
        if not isinstance(obj.planned_outages, (list, tuple)):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "planned_outages must be a sequence of OutageEvent",
            )
        _validate_outage_events(list(obj.planned_outages), hours_map)
        if obj.commissioning_hour is not None and (
            isinstance(obj.commissioning_hour, bool)
            or not isinstance(obj.commissioning_hour, (int, np.integer))
            or int(obj.commissioning_hour) < 0
        ):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "commissioning_hour must be a non-negative integer",
            )
        if obj.retirement_hour is not None and (
            isinstance(obj.retirement_hour, bool)
            or not isinstance(obj.retirement_hour, (int, np.integer))
            or int(obj.retirement_hour) <= 0
        ):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "retirement_hour must be a positive integer",
            )
        commissioning = None if obj.commissioning_hour is None else int(obj.commissioning_hour)
        retirement = None if obj.retirement_hour is None else int(obj.retirement_hour)
        if commissioning is not None and retirement is not None \
                and commissioning >= retirement:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OBJECT",
                "commissioning_hour must be before retirement_hour",
            )
        for hours in hours_map.values():
            if commissioning is not None and commissioning >= hours:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INVALID_OBJECT",
                    "commissioning_hour must fit every weather-year axis",
                )
            if retirement is not None and retirement > hours:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INVALID_OBJECT",
                    "retirement_hour must fit every weather-year axis",
                )
        records.append(_NormalizedObject(
            original=obj, object_id=key, namespace=ns,
        ))
    keys = [r.object_id for r in records]
    if len(set(keys)) != len(keys):
        raise ScenarioBankError(
            "SCENARIO_BANK_DUPLICATE_OBJECT",
            "duplicate object id after normalization (strip/NFKC); object ids "
            "must be unique across the whole bank regardless of namespace",
        )
    return records


def _validate_outage_events(
    outages: list[OutageEvent], hours_map: dict[int, int]
) -> None:
    """Full per-event validation (types, ranges, overlap) before any write."""
    events: list[OutageEvent] = []
    for event in outages:
        if not isinstance(event, OutageEvent):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OUTAGE",
                "planned_outages must contain only OutageEvent",
            )
        for field, value in (("outage_id", event.outage_id),
                             ("status", event.status),
                             ("source_id", event.source_id)):
            if not isinstance(value, str) or not value.strip():
                raise ScenarioBankError(
                    "SCENARIO_BANK_INVALID_OUTAGE",
                    f"outage {field} must be a non-blank string",
                )
        if isinstance(event.start_hour, bool) \
                or not isinstance(event.start_hour, (int, np.integer)) \
                or isinstance(event.end_hour, bool) \
                or not isinstance(event.end_hour, (int, np.integer)):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OUTAGE",
                "outage start_hour/end_hour must be integers",
            )
        start, end = int(event.start_hour), int(event.end_hour)
        if start < 0 or start >= end:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OUTAGE",
                "outage window must satisfy 0 <= start < end",
            )
        if not isinstance(event.available_fraction,
                          (int, float, np.integer, np.floating)) \
                or isinstance(event.available_fraction, bool) \
                or not np.isfinite(float(event.available_fraction)) \
                or not 0.0 <= float(event.available_fraction) <= 1.0:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_OUTAGE",
                "available_fraction must be finite and in [0, 1]",
            )
        for hours in hours_map.values():
            if end > hours:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INVALID_OUTAGE",
                    "outage window must fit every weather-year axis",
                )
        events.append(OutageEvent(
            outage_id=event.outage_id, start_hour=start, end_hour=end,
            available_fraction=float(event.available_fraction),
            status=event.status, source_id=event.source_id,
        ))
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if a.start_hour < b.end_hour and b.start_hour < a.end_hour:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INVALID_OUTAGE",
                    f"overlapping planned outages {a.outage_id} and {b.outage_id}",
                )


def probability_baseline_years(year_roles: dict[int, str]) -> list[int]:
    """Default probability-baseline year selection: stress years excluded."""
    return sorted(
        year for year, role in year_roles.items()
        if role == "probability_baseline"
    )


class ScenarioBank:
    def __init__(self, directory: Path, index: dict[str, Any]) -> None:
        self.directory = Path(directory)
        self.index = index
        self.bank_id = str(index["scenario_bank_id"])

    def _validate_rel_path(self, rel_path: str) -> Path:
        safe = safe_rel_path(rel_path)
        return self.directory / Path(*safe.split("/"))

    def _array_entry(self, object_id: str, weather_year: int | None) -> dict[str, Any]:
        lookup = normalize_stream_key(object_id)
        candidates = [
            a for a in self.index["arrays"]
            if a["object_id"] == lookup
            and (weather_year is None or a["weather_year"] == weather_year)
        ]
        if not candidates:
            raise ScenarioBankError(
                "SCENARIO_BANK_MISSING_OBJECT",
                f"unknown object {object_id!r}",
            )
        if len(candidates) > 1:
            raise ScenarioBankError(
                "SCENARIO_BANK_MISSING_OBJECT",
                f"object {object_id!r} has multiple years; pass weather_year",
            )
        result: dict[str, Any] = candidates[0]
        return result

    def read_object_array(
        self, object_id: str, weather_year: int | None = None
    ) -> np.ndarray:
        entry = self._array_entry(object_id, weather_year)
        path = self._validate_rel_path(entry["rel_path"])
        try:
            actual = file_sha256(path)
        except OSError as exc:
            raise ScenarioBankError(
                "SCENARIO_BANK_HASH_MISMATCH",
                f"array unreadable: {entry['rel_path']}",
            ) from exc
        if actual != entry["sha256"]:
            raise ScenarioBankError(
                "SCENARIO_BANK_HASH_MISMATCH",
                f"content hash mismatch for {entry['rel_path']}",
            )
        return read_array_mmap(
            path, tuple(entry["shape"]), np.dtype(entry["dtype"])
        )

    def iter_batches(
        self, batch_size: int | None = None
    ) -> Iterator[dict[str, Any]]:
        if batch_size is None:
            size = validate_batch_size(self.index.get("batch_size", BATCH_SIZE))
        else:
            size = validate_batch_size(batch_size)
        sample_count = int(self.index["sample_count"])
        for start in range(0, sample_count, size):
            yield {
                "sample_indices": list(range(start, min(start + size, sample_count))),
                "start": start,
                "end": min(start + size, sample_count),
            }

    def read_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(batch, dict) or "sample_indices" not in batch:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_BATCH",
                "batch must be a mapping with sample_indices",
            )
        indices = validate_batch_indices(
            batch["sample_indices"], int(self.index["sample_count"])
        )
        arrays: dict[str, Any] = {}
        for object_id in sorted({o["object_id"] for o in self.index["objects"]}):
            entries = [
                a for a in self.index["arrays"] if a["object_id"] == object_id
            ]
            per_year = {}
            for entry in entries:
                path = self._validate_rel_path(entry["rel_path"])
                try:
                    actual = file_sha256(path)
                except OSError as exc:
                    raise ScenarioBankError(
                        "SCENARIO_BANK_HASH_MISMATCH",
                        f"array unreadable: {entry['rel_path']}",
                    ) from exc
                if actual != entry["sha256"]:
                    raise ScenarioBankError(
                        "SCENARIO_BANK_HASH_MISMATCH",
                        f"content hash mismatch for {entry['rel_path']}",
                    )
                full = read_array_mmap(
                    path, tuple(entry["shape"]), np.dtype(entry["dtype"])
                )
                per_year[int(entry["weather_year"])] = full[np.asarray(indices)]
            arrays[object_id] = per_year
        return {"sample_indices": indices, "data": arrays}


def _validate_year_inputs(
    weather_years: list[int],
    year_roles: dict[int, str],
    year_hours: dict[int, int] | None,
    n_hours: int | None = None,
) -> dict[int, int]:
    if not isinstance(weather_years, list) or not weather_years:
        raise ScenarioBankError(
            "SCENARIO_BANK_DUPLICATE_YEAR", "weather_years must be a non-empty list"
        )
    if any(isinstance(y, bool) or not isinstance(y, (int, np.integer))
           for y in weather_years):
        raise ScenarioBankError(
            "SCENARIO_BANK_DUPLICATE_YEAR",
            "weather_years must contain non-boolean integers",
        )
    years = [int(y) for y in weather_years]
    if len(set(years)) != len(years):
        raise ScenarioBankError(
            "SCENARIO_BANK_DUPLICATE_YEAR", "weather_years must be unique"
        )
    if not isinstance(year_roles, dict):
        raise ScenarioBankError(
            "SCENARIO_BANK_YEAR_ROLE_MISSING", "year_roles must be a mapping"
        )
    roles_parsed: dict[int, str] = {}
    for raw_year, role in year_roles.items():
        if isinstance(raw_year, bool) \
                or not isinstance(raw_year, (int, np.integer)):
            raise ScenarioBankError(
                "SCENARIO_BANK_YEAR_ROLE_MISSING",
                f"year_roles key {raw_year!r} is not an integer",
            )
        if not isinstance(role, str):
            raise ScenarioBankError(
                "SCENARIO_BANK_YEAR_ROLE_MISSING",
                f"year_roles value {role!r} is not a string",
            )
        if role not in _YEAR_ROLES:
            raise ScenarioBankError(
                "SCENARIO_BANK_YEAR_ROLE_MISSING", f"unknown scenario_role {role!r}"
            )
        roles_parsed[int(raw_year)] = role
    if set(years) != set(roles_parsed.keys()):
        raise ScenarioBankError(
            "SCENARIO_BANK_YEAR_ROLE_MISSING",
            "year_roles must cover exactly the weather years",
        )
    if year_hours is not None:
        if not isinstance(year_hours, dict) or not year_hours:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_YEAR_HOURS",
                "year_hours must be a non-empty mapping when provided",
            )
        if any(
            isinstance(k, bool) or not isinstance(k, (int, np.integer))
            or isinstance(v, bool) or not isinstance(v, (int, np.integer))
            for k, v in year_hours.items()
        ):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_YEAR_HOURS",
                "year_hours keys and values must be non-boolean integers",
            )
        if {int(k) for k in year_hours} != set(years):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_YEAR_HOURS",
                "year_hours must cover exactly the weather years",
            )
        for raw_year, hours in year_hours.items():
            if int(hours) != expected_hours_for_year(int(raw_year)):
                raise ScenarioBankError(
                    "SCENARIO_BANK_INVALID_YEAR_HOURS",
                    f"year {raw_year} requires "
                    f"{expected_hours_for_year(int(raw_year))} hours, got {hours}",
                )
    hours_map: dict[int, int] = {}
    for year in years:
        expected = expected_hours_for_year(int(year))
        if n_hours is not None:
            declared = int(n_hours)
        elif year_hours is not None and year in year_hours:
            declared = int(year_hours[year])
        else:
            declared = expected
        if declared != expected:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_YEAR_HOURS",
                f"year {year} requires {expected} hours, got {declared}",
            )
        hours_map[int(year)] = expected
    return hours_map


def _validate_scalar_inputs(
    samples: Any, n_hours: Any, batch_size: Any
) -> tuple[int, int | None, int]:
    if isinstance(samples, bool) or not isinstance(samples, (int, np.integer)) \
            or int(samples) <= 0:
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_SAMPLES", "samples must be a positive integer"
        )
    if n_hours is not None:
        if isinstance(n_hours, bool) or not isinstance(n_hours, (int, np.integer)) \
                or int(n_hours) <= 0:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_HOURS", "n_hours must be a positive integer"
            )
        n_hours = int(n_hours)
    return int(samples), n_hours, batch_size


def _validate_timezone(timezone: Any) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(timezone, str) or not timezone.strip():
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_TIMEZONE", "timezone must be a non-blank string"
        )
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_TIMEZONE", f"unknown timezone {timezone!r}"
        ) from exc
    return timezone


def build_scenario_bank(
    *,
    root_seed: int,
    objects: list[ScenarioObject],
    samples: int,
    weather_years: list[int],
    year_roles: dict[int, str],
    timezone: str,
    n_hours: int | None = None,
    base_dir: Path | str,
    object_stream_version: str = OBJECT_STREAM_VERSION,
    schema_version: str = SCENARIO_BANK_SCHEMA_VERSION,
    batch_size: int = BATCH_SIZE,
    year_hours: dict[int, int] | None = None,
    index_metadata: dict[str, Any] | None = None,
) -> ScenarioBank:
    """Build (or idempotently reuse) an immutable scenario bank.

    ``index_metadata`` is OPTIONAL run bookkeeping (e.g. which frozen
    data version prepared the bank). It is written into index.json AFTER
    the content-addressed bank_id is computed, so it NEVER joins the
    identity: identical content still reuses one bank, and a legacy
    index without it keeps opening.

    Public entry point: all low-level failures are translated into
    stable ScenarioBankError codes (never ThermalStateError,
    TypeError, ValueError or KeyError leaking out).
    """
    try:
        return _build_impl(
            root_seed=root_seed,
            objects=objects,
            samples=samples,
            weather_years=weather_years,
            year_roles=year_roles,
            timezone=timezone,
            n_hours=n_hours,
            base_dir=base_dir,
            object_stream_version=object_stream_version,
            schema_version=schema_version,
            batch_size=batch_size,
            year_hours=year_hours,
            index_metadata=index_metadata,
        )
    except ScenarioBankError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError,
            ThermalStateError) as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_INPUT", str(exc)
        ) from exc
    except OSError as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_IO_ERROR", f"filesystem error during build: {exc}"
        ) from exc


def _build_impl(
    *,
    root_seed: int,
    objects: list[ScenarioObject],
    samples: int,
    weather_years: list[int],
    year_roles: dict[int, str],
    timezone: str,
    n_hours: int | None = None,
    base_dir: Path | str,
    object_stream_version: str = OBJECT_STREAM_VERSION,
    schema_version: str = SCENARIO_BANK_SCHEMA_VERSION,
    batch_size: int = BATCH_SIZE,
    year_hours: dict[int, int] | None = None,
    index_metadata: dict[str, Any] | None = None,
) -> ScenarioBank:
    """Internal build implementation (see build_scenario_bank docstring)."""
    if isinstance(root_seed, bool) or not isinstance(root_seed, (int, np.integer)) \
            or int(root_seed) < 0:
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_SEED", "root_seed must be a non-negative integer"
        )
    if schema_version != SCENARIO_BANK_SCHEMA_VERSION:
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSUPPORTED_VERSION",
            f"unknown schema_version {schema_version!r}",
        )
    if object_stream_version != OBJECT_STREAM_VERSION:
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSUPPORTED_VERSION",
            f"unknown object_stream_version {object_stream_version!r}",
        )
    tz = _validate_timezone(timezone)
    samples, n_hours, batch_size = _validate_scalar_inputs(samples, n_hours, batch_size)
    batch_size = validate_batch_size(batch_size)
    hours_map = _validate_year_inputs(weather_years, year_roles, year_hours, n_hours)

    records = _validate_objects_before_write(list(objects), hours_map)
    objects_sorted = sorted(records, key=lambda r: r.object_id)
    objects_payload = _objects_payload(objects_sorted)

    base = Path(base_dir)
    banks_dir = base / "scenario_banks"
    banks_dir.mkdir(parents=True, exist_ok=True)
    import uuid

    staging = banks_dir / f".staging.{root_seed}.{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        array_entries: list[dict[str, Any]] = []
        for record in objects_sorted:
            key = record.object_id
            key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
            ns = record.namespace
            for year in sorted(hours_map.keys()):
                hours = hours_map[year]
                rel_name = _array_rel_path(key_hash, year)
                target_path = staging / Path(*rel_name.split("/"))
                target_path.parent.mkdir(parents=True, exist_ok=True)

                def fill_batch(
                    start: int,
                    end: int,
                    view: np.ndarray,
                    record: _NormalizedObject = record,
                    year: int = year,
                    hours: int = hours,
                    ns: str = ns,
                ) -> None:
                    obj = record.original
                    for sample in range(start, end):
                        generator = make_generator(
                            root_seed, record.object_id,
                            f"{ns}:sample{sample}:year{year}",
                        )
                        result = simulate_unit(
                            obj.params,
                            hours,
                            generator,
                            planned_outages=list(obj.planned_outages),
                            commissioning_hour=obj.commissioning_hour,
                            retirement_hour=obj.retirement_hour,
                            delta_t_hours=obj.delta_t_hours,
                            initial_state=obj.initial_state,
                        )
                        view[sample - start, :] = result.capacity_fraction

                write_array_batched(
                    target_path, (samples, hours), np.dtype(ARRAY_DTYPE),
                    fill_batch, batch_size,
                )
                array_entries.append({
                    "object_id": key,
                    "weather_year": year,
                    "rel_path": rel_name,
                    "shape": [samples, hours],
                    "dtype": str(np.dtype(ARRAY_DTYPE)),
                    "sha256": file_sha256(target_path),
                })

        index_payload: dict[str, Any] = {
            "schema_version": schema_version,
            "root_seed": int(root_seed),
            "object_stream_version": object_stream_version,
            "sample_count": samples,
            "weather_years": sorted(hours_map.keys()),
            "year_roles": {str(y): role for y, role in sorted(year_roles.items())},
            "year_hours": {str(y): h for y, h in sorted(hours_map.items())},
            "n_hours": n_hours,
            "timezone": tz,
            "objects": objects_payload,
            "data_compat": {
                "array_dtype": str(np.dtype(ARRAY_DTYPE)),
                "object_stream_version": object_stream_version,
            },
            "arrays": array_entries,
            "batch_size": batch_size,
            "probability_baseline_years": probability_baseline_years(year_roles),
        }
        bank_id = hash_payload(index_payload)
        index_payload["scenario_bank_id"] = bank_id
        # post-hash run bookkeeping (R01): records WHICH frozen data the
        # bank was prepared under. Written after the identity hash and
        # never part of it - the first recorded binding survives the
        # idempotent reuse path (the existing bank is kept, not rewritten)
        if index_metadata:
            index_payload["source_binding"] = {
                **dict(index_metadata),
                "created_at": datetime.now(UTC).isoformat(),
            }
        write_json_atomic(staging / "index.json", index_payload)
        # full verification of the staged bank BEFORE publishing
        open_scenario_bank(staging)
        target = banks_dir / bank_id

        def verify_existing(target_path: Path) -> None:
            open_scenario_bank(target_path)

        publish_staging(staging, target, verify_existing)
        return open_scenario_bank(target)
    finally:
        if staging.exists():
            import shutil

            shutil.rmtree(staging, ignore_errors=True)


def _is_canonical_year_key(value: Any) -> bool:
    """True only for canonical decimal year strings like ``"2025"``
    (rejects ``"02025"``, ``"2025.0"``, negative or non-digit keys)."""
    if not isinstance(value, str):
        return False
    if not value.isdigit():
        return False
    return str(int(value)) == value


def _validate_persisted_object_entry(
    entry: dict[str, Any], hours_map: dict[int, int]
) -> None:
    """Full validation of one persisted object entry (formal contract
    written by _objects_payload; no permissive int()/str() coercion)."""
    object_id = entry.get("object_id")
    if not isinstance(object_id, str) or not object_id:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "object_id must be a non-empty string"
        )
    if normalize_stream_key(object_id) != object_id:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            f"object_id {object_id!r} is not the normalized form",
        )
    if not _is_safe_identifier(object_id):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", f"object_id {object_id!r} is unsafe"
        )
    normalized_key = entry.get("normalized_object_key")
    if not isinstance(normalized_key, str) or normalized_key != object_id:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "normalized_object_key must equal the normalized object_id",
        )
    namespace = entry.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "namespace must be a non-empty string"
        )
    if normalize_stream_key(namespace) != namespace:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            f"namespace {namespace!r} is not the normalized form",
        )
    capacity = entry.get("capacity_mw")
    if isinstance(capacity, bool) \
            or not isinstance(capacity, (int, float, np.integer, np.floating)) \
            or not np.isfinite(float(capacity)) or float(capacity) <= 0.0:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "capacity_mw must be finite and positive"
        )
    delta_t = entry.get("delta_t_hours")
    if isinstance(delta_t, bool) \
            or not isinstance(delta_t, (int, float, np.integer, np.floating)) \
            or not np.isfinite(float(delta_t)) or float(delta_t) <= 0.0:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "delta_t_hours must be finite and positive",
        )
    try:
        mode = ThermalMode(entry.get("mode"))
    except (ValueError, TypeError) as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            f"unknown mode {entry.get('mode')!r}",
        ) from exc
    try:
        ThermalParams(
            mode=mode,
            for_rate=entry.get("for_rate"),
            mttf_hours=entry.get("mttf_hours"),
            mttr_hours=entry.get("mttr_hours"),
        )
    except ThermalStateError as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            f"invalid reliability parameter combination: {exc}",
        ) from exc
    initial_state = entry.get("initial_state")
    if initial_state is not None and not isinstance(initial_state, bool):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "initial_state must be a bool or null",
        )
    policy = entry.get("initial_state_policy")
    if mode == ThermalMode.SEQUENTIAL:
        expected = "explicit" if initial_state is not None else "steady_state"
    elif mode == ThermalMode.INDEPENDENT_HOURLY:
        expected = "independent_draw"
        if initial_state is not None:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "INDEPENDENT_HOURLY must not carry initial_state",
            )
    else:
        expected = "deterministic"
        if initial_state is not None:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "FIXED modes must not carry initial_state",
            )
    if policy != expected:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            f"initial_state_policy {policy!r} inconsistent with mode/initial_state",
        )
    commissioning = entry.get("commissioning_hour")
    retirement = entry.get("retirement_hour")
    for field, value, lower in (("commissioning_hour", commissioning, 0),
                                ("retirement_hour", retirement, 1)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, np.integer))
        ):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"{field} must be a non-boolean integer or null",
            )
        if value is not None and int(value) < lower:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"{field} must be >= {lower}",
            )
    if commissioning is not None and retirement is not None \
            and int(commissioning) >= int(retirement):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "commissioning_hour must be before retirement_hour",
        )
    if commissioning is not None:
        for hours in hours_map.values():
            if int(commissioning) >= hours:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INDEX_INVALID",
                    "commissioning_hour must fit every weather-year axis",
                )
    if retirement is not None:
        for hours in hours_map.values():
            if int(retirement) > hours:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INDEX_INVALID",
                    "retirement_hour must fit every weather-year axis",
                )
    outages_raw = entry.get("planned_outages")
    if not isinstance(outages_raw, list):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "planned_outages must be a list"
        )
    outages: list[OutageEvent] = []
    for raw in outages_raw:
        if not isinstance(raw, dict):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "outage entries must be objects"
            )
        for required in ("outage_id", "start_hour", "end_hour",
                         "available_fraction", "status", "source_id"):
            if required not in raw:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INDEX_INVALID", f"outage field {required} missing"
                )
        try:
            outages.append(OutageEvent(
                outage_id=raw["outage_id"],
                start_hour=raw["start_hour"],
                end_hour=raw["end_hour"],
                available_fraction=raw["available_fraction"],
                status=raw["status"],
                source_id=raw["source_id"],
            ))
        except (TypeError, ValueError) as exc:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "outage field type invalid"
            ) from exc
    try:
        _validate_outage_events(outages, hours_map)
    except ScenarioBankError as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            f"persisted planned_outages invalid: {exc}",
        ) from exc
    diagnostics = entry.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "diagnostics must be a list"
        )
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "diagnostic entries must be objects",
            )
        code = diagnostic.get("code")
        if not isinstance(code, str) or not code:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "diagnostic code must be a non-empty string",
            )
        severity = diagnostic.get("severity")
        if severity not in ("warning", "error"):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"diagnostic severity {severity!r} unsupported",
            )
        message = diagnostic.get("message")
        if not isinstance(message, str) or not message:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "diagnostic message must be a non-empty string",
            )
    duration_codes = [
        d for d in diagnostics
        if d.get("code") == "THERMAL_DURATION_UNAVAILABLE"
    ]
    if mode == ThermalMode.INDEPENDENT_HOURLY:
        if len(duration_codes) != 1:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "INDEPENDENT_HOURLY object must carry exactly one "
                "THERMAL_DURATION_UNAVAILABLE diagnostic",
            )
        if duration_codes[0].get("severity") != "warning":
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "THERMAL_DURATION_UNAVAILABLE severity must be warning",
            )
    elif duration_codes:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "non-INDEPENDENT_HOURLY object must not carry "
            "THERMAL_DURATION_UNAVAILABLE diagnostic",
        )


def _validate_bank_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "scenario_bank_id must be a 64-hex string"
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "scenario_bank_id must be hex"
        ) from exc
    return value


def open_scenario_bank(directory: Path | str) -> ScenarioBank:
    """Open and fully verify an existing scenario bank index.

    Public entry point: all low-level failures are translated into
    stable ScenarioBankError codes with the original exception chained.
    """
    try:
        return _open_impl(directory)
    except ScenarioBankError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError,
            OSError) as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", f"index verification failed: {exc}"
        ) from exc


def _open_impl(directory: Path | str) -> ScenarioBank:
    """Internal open implementation (see open_scenario_bank docstring)."""
    root = Path(directory)
    index_path = root / "index.json"
    if not index_path.exists():
        raise ScenarioBankError(
            "SCENARIO_BANK_NOT_FOUND", f"no index.json in {root}"
        )
    index = read_json(index_path)
    if index.get("schema_version") != SCENARIO_BANK_SCHEMA_VERSION:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "schema_version mismatch"
        )
    bank_id = _validate_bank_id(index.get("scenario_bank_id"))
    if not isinstance(index.get("root_seed"), int) \
            or isinstance(index["root_seed"], bool) or index["root_seed"] < 0:
        raise ScenarioBankError("SCENARIO_BANK_INDEX_INVALID", "root_seed invalid")
    if index.get("object_stream_version") != OBJECT_STREAM_VERSION:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "object_stream_version unsupported"
        )
    if not isinstance(index.get("timezone"), str) or not index["timezone"]:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "timezone missing or invalid"
        )
    try:
        _validate_timezone(index["timezone"])
    except ScenarioBankError as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", f"timezone invalid: {index['timezone']!r}"
        ) from exc
    if not isinstance(index.get("data_compat"), dict):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "data_compat must be an object"
        )
    if index.get("data_compat", {}).get("array_dtype") != str(np.dtype(ARRAY_DTYPE)):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "data_compat array_dtype mismatch"
        )
    if index.get("data_compat", {}).get("object_stream_version") \
            != OBJECT_STREAM_VERSION:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "data_compat version mismatch"
        )
    try:
        validate_batch_size(index.get("batch_size"))
    except ScenarioBankError as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "batch_size invalid"
        ) from exc
    sample_count = index.get("sample_count")
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) \
            or sample_count <= 0:
        raise ScenarioBankError("SCENARIO_BANK_INDEX_INVALID", "sample_count invalid")

    years_raw = index.get("weather_years", [])
    roles = index.get("year_roles", {})
    hours_map = index.get("year_hours", {})
    if not isinstance(years_raw, list) or not years_raw:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "weather_years must be a non-empty list"
        )
    if any(isinstance(y, bool) or not isinstance(y, int) for y in years_raw):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "weather_years must contain non-boolean integers",
        )
    years = [int(y) for y in years_raw]
    if not isinstance(roles, dict) or not isinstance(hours_map, dict):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "year_roles and year_hours must be objects",
        )
    if set(years) != {int(k) for k in roles}:
        raise ScenarioBankError("SCENARIO_BANK_INDEX_INVALID", "year_roles incomplete")
    if set(years) != {int(k) for k in hours_map}:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "year_hours incomplete"
        )
    for key in roles:
        if not _is_canonical_year_key(key):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"year_roles key {key!r} is not a canonical decimal year",
            )
    for key in hours_map:
        if not _is_canonical_year_key(key):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"year_hours key {key!r} is not a canonical decimal year",
            )
    if len(set(years)) != len(years):
        raise ScenarioBankError("SCENARIO_BANK_INDEX_INVALID", "weather_years invalid")
    for year in years:
        role = roles[str(year)]
        if not isinstance(role, str) or role not in _YEAR_ROLES:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", f"unknown role {role!r}"
            )
        hours_value = hours_map[str(year)]
        if isinstance(hours_value, bool) or not isinstance(hours_value, int):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "year_hours values must be integers"
            )
        expected = expected_hours_for_year(int(year))
        if int(hours_value) != expected:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", f"year_hours mismatch for {year}"
            )
    persisted_n_hours = index.get("n_hours")
    if persisted_n_hours is not None:
        if isinstance(persisted_n_hours, bool) \
                or not isinstance(persisted_n_hours, int) \
                or persisted_n_hours <= 0:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "n_hours must be null or a positive non-boolean integer",
            )
        hour_values = {int(v) for v in hours_map.values()}
        if len(hour_values) != 1 or persisted_n_hours != hour_values.pop():
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "n_hours must match every year_hours value (null for mixed years)",
            )

    objects_raw = index.get("objects", [])
    if not isinstance(objects_raw, list):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "objects must be a list"
        )
    objects: list[dict[str, Any]] = []
    for raw_entry in objects_raw:
        if not isinstance(raw_entry, dict):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "object entries must be objects"
            )
        objects.append(raw_entry)
    for entry in objects:
        for required in ("object_id", "namespace", "mode", "initial_state_policy"):
            if required not in entry:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INDEX_INVALID", f"object field {required} missing"
                )
        _validate_persisted_object_entry(entry, hours_map)
    object_ids = [o["object_id"] for o in objects]
    if not object_ids or len(set(object_ids)) != len(object_ids):
        raise ScenarioBankError("SCENARIO_BANK_INDEX_INVALID", "objects must be unique")

    arrays_raw = index.get("arrays", [])
    if not isinstance(arrays_raw, list):
        raise ScenarioBankError("SCENARIO_BANK_INDEX_INVALID", "arrays must be a list")
    arrays: list[dict[str, Any]] = []
    for raw_entry in arrays_raw:
        if not isinstance(raw_entry, dict):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "array entries must be objects"
            )
        arrays.append(raw_entry)
    seen_keys: set[tuple[str, int]] = set()
    seen_paths: set[str] = set()
    for entry in arrays:
        for required in ("object_id", "weather_year", "rel_path",
                         "shape", "dtype", "sha256"):
            if required not in entry:
                raise ScenarioBankError(
                    "SCENARIO_BANK_INDEX_INVALID", f"array field {required} missing"
                )
        array_object_id = entry["object_id"]
        if not isinstance(array_object_id, str) \
                or normalize_stream_key(array_object_id) != array_object_id \
                or not _is_safe_identifier(array_object_id):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "array object_id must be the normalized safe form",
            )
        if array_object_id not in object_ids:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "array maps unknown object"
            )
        if isinstance(entry["weather_year"], bool) \
                or not isinstance(entry["weather_year"], int):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "array weather_year must be a non-boolean integer",
            )
        weather_year = int(entry["weather_year"])
        if weather_year not in years:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "array maps unknown weather_year"
            )
        key = (entry["object_id"], weather_year)
        if key in seen_keys:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "duplicate object/year array entry"
            )
        seen_keys.add(key)
        rel_raw = entry["rel_path"]
        if not isinstance(rel_raw, str) or not rel_raw:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "rel_path must be a non-empty string",
            )
        try:
            rel = safe_rel_path(rel_raw)
        except ScenarioBankError as exc:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"rel_path {rel_raw!r} is unsafe or non-canonical",
            ) from exc
        if rel != rel_raw:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"rel_path {rel_raw!r} is not the canonical POSIX relative form",
            )
        if rel in seen_paths:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "duplicate rel_path"
            )
        seen_paths.add(rel)
        if not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "sha256 must be 64-hex"
            )
        try:
            int(entry["sha256"], 16)
        except ValueError as exc:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "sha256 must be hex"
            ) from exc
        if not isinstance(entry["shape"], list) or len(entry["shape"]) != 2:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "shape must be (samples, hours_of_year)",
            )
        if any(isinstance(v, bool) or not isinstance(v, (int, np.integer))
               or int(v) <= 0 for v in entry["shape"]):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                "shape elements must be positive non-boolean integers",
            )
        if int(entry["shape"][0]) != sample_count \
                or int(entry["shape"][1]) != int(hours_map[str(entry["weather_year"])]):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID", "shape inconsistent with index"
            )
        try:
            array_dtype = np.dtype(entry["dtype"])
        except (TypeError, ValueError) as exc:
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"dtype {entry['dtype']!r} is not parsable",
            ) from exc
        if array_dtype != np.dtype(ARRAY_DTYPE):
            raise ScenarioBankError(
                "SCENARIO_BANK_INDEX_INVALID",
                f"dtype {entry['dtype']!r} incompatible with {ARRAY_DTYPE}",
            )
        path = root / Path(*rel.split("/"))
        if not path.exists():
            raise ScenarioBankError(
                "SCENARIO_BANK_MISSING_ARRAY", f"array missing: {rel}"
            )
        if file_sha256(path) != entry["sha256"]:
            raise ScenarioBankError(
                "SCENARIO_BANK_HASH_MISMATCH", f"content hash mismatch for {rel}"
            )
        # mmap verification of the real dtype and shape
        read_array_mmap(path, tuple(entry["shape"]), array_dtype)
    if len(seen_keys) != len(object_ids) * len(years):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "objects/arrays mapping incomplete"
        )
    persisted_baseline = index.get("probability_baseline_years")
    expected_baseline = probability_baseline_years(
        {int(y): r for y, r in roles.items()}
    )
    if not isinstance(persisted_baseline, list):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "probability_baseline_years must be a list",
        )
    if any(isinstance(v, bool) or not isinstance(v, int)
           for v in persisted_baseline):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "probability_baseline_years must contain non-boolean integers",
        )
    baseline = [int(v) for v in persisted_baseline]
    if len(set(baseline)) != len(baseline):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "probability_baseline_years must be unique",
        )
    if baseline != sorted(baseline):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            "probability_baseline_years must be strictly ascending",
        )
    if baseline != expected_baseline:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", "probability_baseline_years mismatch"
        )
    payload = dict(index)
    payload.pop("scenario_bank_id", None)
    # post-hash run bookkeeping (R01: which frozen data prepared the
    # bank) never joins the identity - excluded here exactly as it was
    # excluded when the id was computed
    payload.pop("source_binding", None)
    if bank_id != hash_payload(payload):
        raise ScenarioBankError(
            "SCENARIO_BANK_HASH_MISMATCH", "index business metadata hash mismatch"
        )
    return ScenarioBank(root, index)


__all__ = [
    "ARRAY_DTYPE",
    "BATCH_SIZE",
    "OBJECT_STREAM_VERSION",
    "SCENARIO_BANK_SCHEMA_VERSION",
    "ScenarioBank",
    "ScenarioBankError",
    "ScenarioObject",
    "build_scenario_bank",
    "open_scenario_bank",
    "probability_baseline_years",
]
