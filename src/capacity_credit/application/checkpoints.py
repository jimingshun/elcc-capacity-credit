"""Batch-boundary checkpoints (T08, review round 2).

Invariants (B = batch_size, L = sample_limit, K = completed_batch,
N = next_sample_index):

- N < L  -> N == K * B (full batch boundary);
- N == L -> K == ceil(L / B) (final truncated batch allowed);
- N > L, L <= 0, non-integer/bool batch fields are rejected directly
  (never only indirectly through stats counters).

The checkpoint also persists the canonical convergence-config snapshot
(computed from the real config at save time) and ``primary_risk_metric``;
resume recomputes the snapshot from the current config and rejects any
mismatch even when the external ``config_hash`` is unchanged.

Strict validation: timezone-aware ISO-8601 created_at; exact event
structure (run_id lineage, timezone-aware times with end > start,
positive duration, finite non-negative EUE/max power, legal year_range,
bool crosses flag, year role membership via explicit baseline/stress
years); criteria entries must be booleans; JSON serialization forbids
NaN/Infinity; the expected mapping must contain exactly the four key
hashes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from capacity_credit.application.atomic_io import atomic_write_bytes
from capacity_credit.engine.convergence import ConvergenceConfig, ConvergenceError
from capacity_credit.engine.online_stats import OnlineStats, StatError

CHECKPOINT_SCHEMA_VERSION = "1"
_KNOWN_FIELDS = {
    "schema_version", "run_id", "data_version_hash", "config_hash",
    "scenario_bank_id", "software_version", "completed_batch",
    "next_sample_index", "batch_size", "sample_limit",
    "primary_risk_metric", "convergence_config", "stats_by_year",
    "formal_primary_stats", "nonzero_sample_count", "recent_window",
    "criteria", "baseline_events", "stress_events", "created_at",
}
_EVENT_FIELDS = {
    "run_id", "sample_id", "weather_year", "start_time", "end_time",
    "duration_hours", "event_eue_mwh", "max_unserved_power_mw",
    "year_range", "crosses_year_boundary",
}
_CRITERIA_KEYS = {
    "min_samples_met", "precision_met", "nonzero_samples_met",
    "recent_batches_stable",
}
_CONFIG_SNAPSHOT_KEYS = {
    "batch_size", "min_samples", "max_samples", "confidence_level",
    "relative_half_width_tolerance", "absolute_half_width_tolerance",
    "near_zero_threshold", "min_nonzero_samples", "recent_batch_window",
    "recent_batch_stability_tolerance", "use_student_t",
    "normal_interval_min_samples", "primary_risk_metric",
}


class CheckpointError(ValueError):
    """Stable checkpoint error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class CheckpointState:
    schema_version: str
    run_id: str
    data_version_hash: str
    config_hash: str
    scenario_bank_id: str
    software_version: str
    completed_batch: int
    next_sample_index: int
    batch_size: int
    sample_limit: int
    primary_risk_metric: str
    convergence_config: dict[str, Any]
    stats_by_year: dict[int, dict[str, OnlineStats]]
    formal_primary_stats: OnlineStats
    nonzero_sample_count: int
    recent_window: list[float]
    criteria: dict[str, Any]
    baseline_events: list[dict[str, Any]]
    stress_events: list[dict[str, Any]]
    created_at: str


def _validate_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"field {field} must be a non-empty string"
        )
    return value


def _require_string(payload: dict[str, Any], key: str) -> str:
    return _validate_nonempty_string(payload.get(key), key)


def _validate_integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) \
            or int(value) < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"field {field} must be a {qualifier} integer"
        )
    return int(value)


def _require_int(payload: dict[str, Any], key: str) -> int:
    return _validate_integer(payload.get(key), key)


def _require_finite(value: Any, field: str) -> float:
    if isinstance(value, bool) \
            or not isinstance(value, (int, float, np.integer, np.floating)) \
            or not np.isfinite(float(value)):
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"{field} must be a finite number"
        )
    return float(value)


def validate_checkpoint_hashes(
    state: CheckpointState,
    *,
    data_version_hash: str,
    config_hash: str,
    software_version: str,
    scenario_bank_id: str,
) -> None:
    for field, expected, actual in (
        ("data_version_hash", data_version_hash, state.data_version_hash),
        ("config_hash", config_hash, state.config_hash),
        ("software_version", software_version, state.software_version),
        ("scenario_bank_id", scenario_bank_id, state.scenario_bank_id),
    ):
        if expected != actual:
            raise CheckpointError(
                "CHECKPOINT_HASH_MISMATCH",
                f"{field} mismatch: expected {expected!r}, got {actual!r}",
            )


def _validate_expected_mapping(expected: dict[str, str]) -> None:
    required = {"data_version_hash", "config_hash", "software_version",
                "scenario_bank_id"}
    if not isinstance(expected, dict):
        raise CheckpointError("CHECKPOINT_INVALID", "expected must be a mapping")
    if set(expected.keys()) != required:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            f"expected must contain exactly {sorted(required)}",
        )
    for key, value in expected.items():
        if not isinstance(value, str) or not value:
            raise CheckpointError(
                "CHECKPOINT_INVALID", f"expected {key} must be a non-empty string"
            )


def _validate_batch_boundaries(state: CheckpointState) -> None:
    b = _validate_integer(state.batch_size, "batch_size", positive=True)
    l = _validate_integer(state.sample_limit, "sample_limit", positive=True)
    k = _validate_integer(state.completed_batch, "completed_batch")
    n = _validate_integer(state.next_sample_index, "next_sample_index")
    if n > l:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "next_sample_index exceeds sample_limit"
        )
    if n < l:
        if n != k * b:
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "next_sample_index must equal completed_batch * batch_size "
                "while below sample_limit",
            )
    else:
        import math as _math

        if k != _math.ceil(l / b):
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "completed_batch must equal ceil(sample_limit / batch_size) "
                "at the final (possibly truncated) batch",
            )
    if k == 0 and n > 0:
        raise CheckpointError("CHECKPOINT_INVALID", "completed_batch=0 but samples>0")
    if k > 0 and n == 0:
        raise CheckpointError("CHECKPOINT_INVALID", "completed_batch>0 but samples=0")


def _validate_created_at(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise CheckpointError("CHECKPOINT_INVALID", "created_at must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "created_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "created_at must carry a timezone offset"
        )


def _validate_events(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    allowed_years: list[int],
    completed_samples: set[str],
) -> None:
    if not isinstance(events, list):
        raise CheckpointError("CHECKPOINT_INVALID", "events must be a list")
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            raise CheckpointError("CHECKPOINT_INVALID", "event entries must be objects")
        if set(event.keys()) != _EVENT_FIELDS:
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                f"event fields must be exactly {sorted(_EVENT_FIELDS)}",
            )
        if event["run_id"] != run_id:
            raise CheckpointError(
                "CHECKPOINT_INVALID", "event run_id must equal checkpoint run_id"
            )
        sample_id = event["sample_id"]
        if not isinstance(sample_id, str) or sample_id not in completed_samples:
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                f"event sample_id {sample_id!r} not in completed sample range",
            )
        year = event["weather_year"]
        if isinstance(year, bool) or not isinstance(year, int) \
                or year not in allowed_years:
            raise CheckpointError(
                "CHECKPOINT_INVALID", "event weather_year not allowed for this list"
            )
        for key in ("start_time", "end_time"):
            _validate_created_at(event[key])
        start = datetime.fromisoformat(event["start_time"])
        end = datetime.fromisoformat(event["end_time"])
        if end <= start:
            raise CheckpointError(
                "CHECKPOINT_INVALID", "event end_time must be after start_time"
            )
        duration = event["duration_hours"]
        if isinstance(duration, bool) or not isinstance(duration, int) \
                or duration <= 0:
            raise CheckpointError(
                "CHECKPOINT_INVALID", "event duration_hours must be a positive integer"
            )
        delta_hours = (end - start).total_seconds() / 3600.0
        if delta_hours != float(duration):
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "event duration_hours must equal end_time - start_time",
            )
        _require_finite(event["event_eue_mwh"], "event_eue_mwh")
        _require_finite(event["max_unserved_power_mw"], "max_unserved_power_mw")
        if float(event["event_eue_mwh"]) < 0.0 \
                or float(event["max_unserved_power_mw"]) < 0.0:
            raise CheckpointError(
                "CHECKPOINT_INVALID", "event EUE/max power must be non-negative"
            )
        start_year = start.year
        # end_time is the half-open interval end (start + duration);
        # the covered last hour is end - 1 hour
        covered_end = end - (end - start) / float(duration) \
            if duration else end
        covered_end_year = covered_end.year
        if year != start_year:
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "event weather_year must equal the start-time year",
            )
        year_range = event["year_range"]
        if not isinstance(year_range, list) or len(year_range) != 2 \
                or any(isinstance(y, bool) or not isinstance(y, int)
                       for y in year_range) \
                or year_range != [start_year, covered_end_year]:
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "event year_range must equal [start-year, covered-end-year]",
            )
        if not isinstance(event["crosses_year_boundary"], bool):
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "event crosses_year_boundary must be a boolean",
            )
        if event["crosses_year_boundary"] != (covered_end_year != start_year):
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "event crosses_year_boundary must match the timestamps",
            )
        dedupe_key = (event["run_id"], str(event["sample_id"]),
                      event["start_time"])
        if dedupe_key in seen:
            raise CheckpointError(
                "CHECKPOINT_INVALID", "duplicate event record in checkpoint"
            )
        seen.add(dedupe_key)


def _validate_criteria(criteria: dict[str, Any]) -> None:
    if not isinstance(criteria, dict):
        raise CheckpointError("CHECKPOINT_INVALID", "criteria must be an object")
    if set(criteria.keys()) != _CRITERIA_KEYS:
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"criteria keys must be {sorted(_CRITERIA_KEYS)}"
        )
    for key, value in criteria.items():
        if not isinstance(value, bool):
            raise CheckpointError(
                "CHECKPOINT_INVALID", f"criteria {key} must be a boolean"
            )


def _validate_config_snapshot(snapshot: dict[str, Any]) -> ConvergenceConfig:
    if not isinstance(snapshot, dict):
        raise CheckpointError("CHECKPOINT_INVALID", "convergence_config must be an object")
    if set(snapshot.keys()) != _CONFIG_SNAPSHOT_KEYS:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            f"convergence_config fields must be {sorted(_CONFIG_SNAPSHOT_KEYS)}",
        )
    primary_metric = snapshot.get("primary_risk_metric")
    if not isinstance(primary_metric, str):
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "convergence_config primary_risk_metric must be a string",
        )

    try:
        return ConvergenceConfig(**snapshot)
    except (ConvergenceError, TypeError, ValueError) as exc:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            f"convergence_config violates config rules: {exc}",
        ) from exc


def _validate_online_stats(value: Any, field: str) -> OnlineStats:
    if not isinstance(value, OnlineStats):
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"{field} must be an OnlineStats instance"
        )
    try:
        OnlineStats.from_dict(value.to_dict())
    except StatError as exc:
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"{field} contains invalid statistics"
        ) from exc
    return value


def _validate_stats_structure(state: CheckpointState) -> None:
    """Structural pre-validation of stats_by_year before any default
    derivation (empty or malformed structures are rejected, never
    silently replaced by derived defaults)."""
    stats = state.stats_by_year
    if not isinstance(stats, dict) or not stats:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "stats_by_year must be a non-empty mapping"
        )
    for year, metrics in stats.items():
        if isinstance(year, bool) or not isinstance(year, int):
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                "stats_by_year years must be non-boolean integers",
            )
        if not isinstance(metrics, dict) or not metrics:
            raise CheckpointError(
                "CHECKPOINT_INVALID",
                f"metrics for year {year} must be a non-empty mapping",
            )
        for name, value in metrics.items():
            if not isinstance(name, str) or not name:
                raise CheckpointError(
                    "CHECKPOINT_INVALID", "metric names must be non-empty strings"
                )
            if not isinstance(value, OnlineStats):
                raise CheckpointError(
                    "CHECKPOINT_INVALID",
                    f"metric {name!r} value must be an OnlineStats",
                )


def validate_checkpoint_state(
    state: CheckpointState,
    *,
    expected_years: list[int],
    expected_metrics: tuple[str, ...],
    recent_window_max: int,
    baseline_years: list[int] | None = None,
    stress_years: list[int] | None = None,
    expected_snapshot: dict[str, Any] | None = None,
) -> None:
    """Full validation of a checkpoint state (used before save and load)."""
    if not isinstance(state, CheckpointState):
        raise CheckpointError(
            "CHECKPOINT_INVALID", "state must be a CheckpointState"
        )
    _validate_stats_structure(state)
    if not isinstance(expected_years, list) or not expected_years:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "expected_years must be a non-empty list"
        )
    if not isinstance(expected_metrics, tuple) or not expected_metrics:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "expected_metrics must be a non-empty tuple"
        )
    if state.schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError("CHECKPOINT_INVALID", "unsupported schema_version")
    for field in (
        "run_id",
        "data_version_hash",
        "config_hash",
        "scenario_bank_id",
        "software_version",
    ):
        _validate_nonempty_string(getattr(state, field), field)
    _validate_batch_boundaries(state)
    _validate_integer(state.nonzero_sample_count, "nonzero_sample_count")
    _validate_created_at(state.created_at)
    _validate_criteria(state.criteria)
    snapshot_config = _validate_config_snapshot(state.convergence_config)
    _validate_nonempty_string(state.primary_risk_metric, "primary_risk_metric")
    if state.primary_risk_metric != snapshot_config.primary_risk_metric:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "top-level primary_risk_metric must equal the config snapshot value",
        )
    if state.batch_size != snapshot_config.batch_size:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "checkpoint batch_size must equal the config snapshot value",
        )
    if state.sample_limit > snapshot_config.max_samples:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "checkpoint sample_limit cannot exceed config max_samples",
        )
    if expected_snapshot is not None \
            and state.convergence_config != expected_snapshot:
        raise CheckpointError(
            "CHECKPOINT_CONFIG_MISMATCH",
            "convergence config snapshot differs from the current config",
        )
    _validate_integer(recent_window_max, "recent_window_max", positive=True)
    if not isinstance(state.recent_window, list):
        raise CheckpointError(
            "CHECKPOINT_INVALID", "recent_window must be a list"
        )
    if len(state.recent_window) > recent_window_max:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "recent_window exceeds the configured window"
        )
    for value in state.recent_window:
        _require_finite(value, "recent_window entry")
    if not isinstance(state.stats_by_year, dict):
        raise CheckpointError(
            "CHECKPOINT_INVALID", "stats_by_year must be a mapping"
        )
    if set(state.stats_by_year.keys()) != set(expected_years):
        raise CheckpointError(
            "CHECKPOINT_INVALID", "stats_by_year years must match expected years"
        )
    metric_sets = {
        frozenset(metrics.keys()) for metrics in state.stats_by_year.values()
    }
    if len(metric_sets) != 1:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "all years must carry the exact same metric names",
        )
    for year in expected_years:
        metrics = state.stats_by_year[year]
        if not isinstance(metrics, dict):
            raise CheckpointError(
                "CHECKPOINT_INVALID", f"stats for year {year} must be a mapping"
            )
        if set(metrics.keys()) != set(expected_metrics):
            raise CheckpointError(
                "CHECKPOINT_INVALID", "metrics must match the expected metric names"
            )
    _validate_online_stats(state.formal_primary_stats, "formal_primary_stats")
    if state.formal_primary_stats.sample_count != state.next_sample_index:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "formal_primary_stats.sample_count must equal next_sample_index",
        )
    if state.formal_primary_stats.nonzero_sample_count != state.nonzero_sample_count:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "nonzero_sample_count must equal formal_primary_stats counter",
        )
    for year, metrics in state.stats_by_year.items():
        for name, stats in metrics.items():
            _validate_online_stats(stats, f"stats_by_year[{year}][{name}]")
            if stats.sample_count != state.next_sample_index:
                raise CheckpointError(
                    "CHECKPOINT_INVALID",
                    f"metric {name} sample_count must equal next_sample_index",
                )
    completed_samples = {
        f"s{i}" for i in range(state.next_sample_index)
    }
    _validate_events(
        state.baseline_events,
        run_id=state.run_id,
        allowed_years=baseline_years or expected_years,
        completed_samples=completed_samples,
    )
    _validate_events(
        state.stress_events,
        run_id=state.run_id,
        allowed_years=stress_years or [],
        completed_samples=completed_samples,
    )


def _stats_consistency(state: CheckpointState) -> None:
    if state.formal_primary_stats.sample_count != state.next_sample_index:
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "formal_primary_stats.sample_count must equal next_sample_index",
        )


def save_checkpoint(
    state: CheckpointState,
    path: Path | str,
    *,
    expected_years: list[int] | None = None,
    expected_metrics: tuple[str, ...] | None = None,
    recent_window_max: int = 100,
    baseline_years: list[int] | None = None,
    stress_years: list[int] | None = None,
) -> None:
    """Validate and atomically persist a checkpoint state."""
    if not isinstance(state, CheckpointState):
        raise CheckpointError(
            "CHECKPOINT_INVALID", "state must be a CheckpointState"
        )
    _validate_stats_structure(state)
    if expected_years is None:
        expected_years = sorted(state.stats_by_year.keys())
    if expected_metrics is None:
        expected_metrics = tuple(
            name for name in state.stats_by_year.get(
                next(iter(state.stats_by_year), 0), {}
            )
        )
    validate_checkpoint_state(
        state,
        expected_years=expected_years,
        expected_metrics=expected_metrics,
        recent_window_max=recent_window_max,
        baseline_years=baseline_years,
        stress_years=stress_years,
    )
    stats_payload = {
        str(year): {
            name: stats.to_dict()
            for name, stats in metrics.items()
        }
        for year, metrics in state.stats_by_year.items()
    }
    payload = {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "data_version_hash": state.data_version_hash,
        "config_hash": state.config_hash,
        "scenario_bank_id": state.scenario_bank_id,
        "software_version": state.software_version,
        "completed_batch": state.completed_batch,
        "next_sample_index": state.next_sample_index,
        "batch_size": state.batch_size,
        "sample_limit": state.sample_limit,
        "primary_risk_metric": state.primary_risk_metric,
        "convergence_config": state.convergence_config,
        "stats_by_year": stats_payload,
        "formal_primary_stats": state.formal_primary_stats.to_dict(),
        "nonzero_sample_count": state.nonzero_sample_count,
        "recent_window": state.recent_window,
        "criteria": state.criteria,
        "baseline_events": list(state.baseline_events),
        "stress_events": list(state.stress_events),
        "created_at": state.created_at,
    }
    try:
        data = (json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (ValueError, TypeError) as exc:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "checkpoint contains non-serializable values"
        ) from exc
    import time as _time

    last_error: OSError | None = None
    for attempt in range(4):
        try:
            atomic_write_bytes(path, data, immutable=False)
            return
        except OSError as exc:
            last_error = exc
            if attempt < 3:
                _time.sleep(0.2)
    raise CheckpointError(
        "CHECKPOINT_WRITE_FAILED", f"checkpoint write failed: {last_error}"
    ) from last_error


def load_checkpoint(
    path: Path | str,
    *,
    expected: dict[str, str],
    run_id: str | None = None,
    expected_years: list[int] | None = None,
    expected_metrics: tuple[str, ...] | None = None,
    recent_window_max: int = 100,
    baseline_years: list[int] | None = None,
    stress_years: list[int] | None = None,
    expected_snapshot: dict[str, Any] | None = None,
) -> CheckpointState:
    """Strictly load and validate a checkpoint."""
    _validate_expected_mapping(expected)
    path_obj = Path(path)
    try:
        raw = path_obj.read_bytes()
    except OSError as exc:
        raise CheckpointError(
            "CHECKPOINT_CORRUPT", f"checkpoint unreadable: {exc}"
        ) from exc
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CheckpointError(
            "CHECKPOINT_CORRUPT", "checkpoint is corrupted or truncated"
        ) from exc
    if not isinstance(payload, dict):
        raise CheckpointError("CHECKPOINT_INVALID", "checkpoint must be an object")
    unknown = set(payload.keys()) - _KNOWN_FIELDS
    if unknown:
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"unknown checkpoint fields: {sorted(unknown)}"
        )
    missing = _KNOWN_FIELDS - set(payload.keys())
    if missing:
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"missing checkpoint fields: {sorted(missing)}"
        )
    schema = payload.get("schema_version")
    if schema != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError(
            "CHECKPOINT_INVALID", f"unsupported schema_version {schema!r}"
        )
    checkpoint_run_id = _require_string(payload, "run_id")
    if run_id is not None and run_id != checkpoint_run_id:
        raise CheckpointError(
            "CHECKPOINT_HASH_MISMATCH",
            f"run_id mismatch: expected {run_id!r}, got {checkpoint_run_id!r}",
        )
    primary_metric = _require_string(payload, "primary_risk_metric")
    config_snapshot_raw = payload.get("convergence_config")
    if not isinstance(config_snapshot_raw, dict):
        raise CheckpointError(
            "CHECKPOINT_INVALID", "convergence_config must be an object"
        )
    config_snapshot = config_snapshot_raw
    _validate_config_snapshot(config_snapshot)
    state = CheckpointState(
        schema_version=schema,
        run_id=checkpoint_run_id,
        data_version_hash=_require_string(payload, "data_version_hash"),
        config_hash=_require_string(payload, "config_hash"),
        scenario_bank_id=_require_string(payload, "scenario_bank_id"),
        software_version=_require_string(payload, "software_version"),
        completed_batch=_require_int(payload, "completed_batch"),
        next_sample_index=_require_int(payload, "next_sample_index"),
        batch_size=_require_int(payload, "batch_size"),
        sample_limit=_require_int(payload, "sample_limit"),
        primary_risk_metric=primary_metric,
        convergence_config=dict(config_snapshot),
        stats_by_year={},
        formal_primary_stats=OnlineStats(),
        nonzero_sample_count=_require_int(payload, "nonzero_sample_count"),
        recent_window=[],
        criteria={},
        baseline_events=[],
        stress_events=[],
        created_at=_require_string(payload, "created_at"),
    )
    stats_payload = payload.get("stats_by_year")
    if not isinstance(stats_payload, dict):
        raise CheckpointError("CHECKPOINT_INVALID", "stats_by_year must be an object")
    for raw_year, metrics in stats_payload.items():
        if not isinstance(raw_year, str) or not raw_year.isdigit() \
                or str(int(raw_year)) != raw_year:
            raise CheckpointError(
                "CHECKPOINT_INVALID", "stats_by_year keys must be canonical years"
            )
        year = int(raw_year)
        if not isinstance(metrics, dict):
            raise CheckpointError("CHECKPOINT_INVALID", "stats entries must be objects")
        state.stats_by_year[year] = {}
        for name, stats_payload in metrics.items():
            try:
                state.stats_by_year[year][name] = OnlineStats.from_dict(stats_payload)
            except StatError as exc:
                raise CheckpointError(
                    "CHECKPOINT_INVALID", f"invalid stats state for {name}"
                ) from exc
    try:
        state.formal_primary_stats = OnlineStats.from_dict(
            payload["formal_primary_stats"]
        )
    except StatError as exc:
        raise CheckpointError(
            "CHECKPOINT_INVALID", "invalid formal_primary_stats"
        ) from exc
    window = payload.get("recent_window")
    if not isinstance(window, list):
        raise CheckpointError("CHECKPOINT_INVALID", "recent_window must be a list")
    for value in window:
        _require_finite(value, "recent_window entry")
    state.recent_window = [float(v) for v in window]
    criteria_raw = payload.get("criteria")
    if not isinstance(criteria_raw, dict):
        raise CheckpointError("CHECKPOINT_INVALID", "criteria must be an object")
    criteria = criteria_raw
    _validate_criteria(criteria)
    state.criteria = dict(criteria)
    for key in ("baseline_events", "stress_events"):
        events = payload.get(key)
        if not isinstance(events, list):
            raise CheckpointError("CHECKPOINT_INVALID", f"{key} must be a list")
        if key == "baseline_events":
            state.baseline_events = list(events)
        else:
            state.stress_events = list(events)
    if expected_years is None:
        expected_years = sorted(state.stats_by_year.keys())
    if expected_metrics is None:
        expected_metrics = tuple(
            name for name in state.stats_by_year.get(
                next(iter(state.stats_by_year), 0), {}
            )
        )
    validate_checkpoint_state(
        state,
        expected_years=expected_years,
        expected_metrics=expected_metrics,
        recent_window_max=recent_window_max,
        baseline_years=baseline_years,
        stress_years=stress_years,
        expected_snapshot=expected_snapshot,
    )
    validate_checkpoint_hashes(
        state,
        data_version_hash=expected["data_version_hash"],
        config_hash=expected["config_hash"],
        software_version=expected["software_version"],
        scenario_bank_id=expected["scenario_bank_id"],
    )
    return state


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointError",
    "CheckpointState",
    "load_checkpoint",
    "save_checkpoint",
    "validate_checkpoint_hashes",
    "validate_checkpoint_state",
]
