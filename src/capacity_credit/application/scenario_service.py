"""Minimal application orchestration for sequential adequacy (T08).

- open and verify an existing scenario bank;
- validate bank id, timezone, years, year_roles, sample range,
  year_weights (exact probability-baseline coverage, finite non-negative
  values, sum == 1) and load vectors (1-D, finite, non-negative, exact
  hour counts) at the service boundary;
- read ScenarioBank batches by configuration (one read_batch per batch);
- call the adequacy kernel per batch;
- save a checkpoint after each successful batch;
- evaluate convergence via the explicit state machine (should_stop);
- stop on convergence or max_samples;
- resume from a matching checkpoint on exact batch boundaries without
  skipping or recomputing samples;
- results before/after resume equal an uninterrupted run, including the
  full deficit-event table.

The CLI wiring belongs to T09; this module exposes a plain callable API.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np

from capacity_credit.application.checkpoints import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointError,
    CheckpointState,
    load_checkpoint,
    save_checkpoint,
)
from capacity_credit.engine.adequacy import (
    METRIC_NAMES,
    AdequacyEngine,
    AdequacyEngineError,
    validate_year_weights,
)
from capacity_credit.engine.convergence import ConvergenceConfig, default_config
from capacity_credit.scenarios.scenario_bank import ScenarioBank


class ScenarioServiceError(ValueError):
    """Stable scenario-service error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AdequacyResult:
    risk_metrics: dict[str, float] | None
    primary_risk_metric: str
    primary_interval: dict[str, Any] | None
    year_summaries: dict[int, dict[str, Any]]
    sample_count: int
    nonzero_sample_count: int
    deficit_events: list[Any]
    diagnostics: list[Any]
    stress_diagnostics: dict[int, dict[str, Any]] | None
    result_status: str
    risk_convergence: str
    search_convergence: str
    scenario_bank_id: str
    stats_by_year: dict[int, dict[str, Any]]
    formal_primary_stats: Any
    recent_window: list[float]
    criteria: dict[str, bool]
    # W2 (weather-year plan section 3): the run's OWN probability-baseline
    # year weights - the exposure chain (run-dir sidecar -> machine table
    # risk_metrics_by_weather_year -> report view) consumes THIS, never a
    # re-derived value. Defaults keep older constructor sites honest.
    year_weights: dict[int, float] | None = None


def _validate_bank(bank: ScenarioBank) -> None:
    if not isinstance(bank.index.get("timezone"), str):
        raise ScenarioServiceError("SERVICE_INVALID_BANK", "bank timezone missing")
    if not bank.index.get("weather_years"):
        raise ScenarioServiceError("SERVICE_INVALID_BANK", "bank has no weather years")
    roles = bank.index["year_roles"]
    baseline = [int(y) for y, role in roles.items() if role == "probability_baseline"]
    if not baseline:
        raise ScenarioServiceError(
            "SERVICE_NO_BASELINE_YEARS",
            "at least one probability-baseline year is required",
        )


def _validate_load(
    load_by_year: dict[int, np.ndarray], bank: ScenarioBank
) -> dict[int, np.ndarray]:
    if not isinstance(load_by_year, dict):
        raise ScenarioServiceError("SERVICE_LOAD_MISMATCH", "load_by_year must be a mapping")
    if set(load_by_year.keys()) != set(bank.index["weather_years"]):
        raise ScenarioServiceError(
            "SERVICE_YEAR_MISMATCH", "load years must equal bank weather years"
        )
    validated: dict[int, np.ndarray] = {}
    for year in bank.index["weather_years"]:
        values = load_by_year[year]
        try:
            array = np.asarray(values, dtype=float)
        except (TypeError, ValueError) as exc:
            raise ScenarioServiceError(
                "SERVICE_LOAD_MISMATCH", f"load for year {year} is not numeric"
            ) from exc
        if array.ndim != 1:
            raise ScenarioServiceError("SERVICE_LOAD_MISMATCH", f"load for year {year} must be 1-D")
        hours = int(bank.index["year_hours"][str(year)])
        if array.size != hours:
            raise ScenarioServiceError(
                "SERVICE_LOAD_MISMATCH",
                f"load for year {year} must have {hours} hours",
            )
        if not np.isfinite(array).all() or (array < 0).any():
            raise ScenarioServiceError(
                "SERVICE_LOAD_MISMATCH",
                f"load for year {year} must be finite and non-negative",
            )
        validated[year] = array
    return validated


def _engine_to_result(engine: AdequacyEngine, config: ConvergenceConfig) -> AdequacyResult:
    finalized = engine.finalize(config)
    year_summaries: dict[int, dict[str, Any]] = {}
    for year in engine.probability_years:
        year_summaries[year] = {
            name: {
                "sample_count": engine.stats_by_year[year][name].sample_count,
                "mean": engine.stats_by_year[year][name].mean,
            }
            for name in engine.stats_by_year[year]
        }
    stress_diagnostics: dict[int, dict[str, Any]] | None = None
    if engine.stress_years:
        stress_diagnostics = {}
        for year in engine.stress_years:
            primary = engine.stats_by_year[year]["primary"]
            stress_diagnostics[year] = {
                "sample_count": primary.sample_count,
                "mean_primary": primary.mean,
                "event_count": sum(1 for e in engine.stress_events if e["weather_year"] == year),
                "note": "stress_test year excluded from probability results",
            }
    diagnostics = [
        {
            "code": "E02_KERNEL_SLICE",
            "severity": "info",
            "message": (
                "minimal adequacy kernel slice only; full E-02 run_manifest/"
                "method_result/artifact_manifest closed loop is T09"
            ),
        }
    ]
    if finalized["sample_count"] == 0:
        diagnostics.append(
            {
                "code": "NO_SAMPLES_PROCESSED",
                "severity": "warning",
                "message": "no samples were processed; risk metrics are null",
            }
        )
    elif finalized["primary_interval"] is None:
        diagnostics.append(
            {
                "code": "INSUFFICIENT_SAMPLES_FOR_CI",
                "severity": "warning",
                "message": (
                    "fewer than two samples processed; confidence interval is not computable"
                ),
            }
        )
    return AdequacyResult(
        risk_metrics=finalized["risk_metrics"],
        primary_risk_metric=finalized["primary_risk_metric"],
        primary_interval=finalized["primary_interval"],
        year_summaries=year_summaries,
        sample_count=finalized["sample_count"],
        nonzero_sample_count=finalized["nonzero_sample_count"],
        deficit_events=finalized["deficit_events"],
        diagnostics=diagnostics,
        stress_diagnostics=stress_diagnostics,
        result_status=finalized["result_status"],
        risk_convergence=finalized["risk_convergence"],
        search_convergence=finalized["search_convergence"],
        scenario_bank_id=finalized["scenario_bank_id"],
        stats_by_year=finalized["stats_by_year"],
        formal_primary_stats=engine.formal_primary_stats,
        recent_window=list(engine.recent_window),
        criteria=engine.check_state(config).criteria,
        year_weights=dict(engine.year_weights),
    )


_WEATHER_YEAR_METRIC_KEYS = (
    "lole_days_per_year",
    "lolh_hours_per_year",
    "eue_mwh_per_year",
    "neue_ppm",
    "demand_energy_mwh",
)


def weather_year_metrics_payload(outcome: AdequacyResult) -> dict[str, Any]:
    """W2 (weather-year plan section 3): the run-dir sidecar payload for
    ``weather_year_metrics.json`` - the run's OWN per-year means and
    year weights, projected as-is. Probability-baseline years carry the
    full metric set plus their weight; stress years carry their primary
    mean with an explicit role mark and NEVER enter the weighted sum.
    Zero methodology change: this only exposes what the engine already
    computed.

    Returns None when the outcome carries no per-year summaries (the
    STORAGE resource chain's ``_ResourceSeriesOutcome`` is a different
    shape) - the caller then writes NO sidecar and the report view
    keeps its honest absence."""
    year_summaries = getattr(outcome, "year_summaries", None)
    if not year_summaries:
        return None
    primary = str(getattr(outcome, "primary_risk_metric", "lole_days_per_year"))
    weights = getattr(outcome, "year_weights", None) or {}
    rows: list[dict[str, Any]] = []
    for year in sorted(year_summaries):
        summary = year_summaries[year]
        entry: dict[str, Any] = {
            "weather_year": int(year),
            "role": "probability_baseline",
            "weight": weights.get(int(year)),
            "sample_count": int(
                summary.get(primary, {}).get("sample_count", 0)
            ),
        }
        mean = summary.get(primary, {}).get("mean")
        entry["primary_metric_mean"] = (
            float(mean) if isinstance(mean, (int, float)) else None
        )
        for key in _WEATHER_YEAR_METRIC_KEYS:
            stat = summary.get(key, {})
            value = stat.get("mean") if isinstance(stat, dict) else None
            entry[key] = (
                float(value) if isinstance(value, (int, float)) else None
            )
        rows.append(entry)
    stress = getattr(outcome, "stress_diagnostics", None) or {}
    for year in sorted(stress):
        diag = stress[year]
        mean = diag.get("mean_primary")
        rows.append(
            {
                "weather_year": int(year),
                "role": "stress_test",
                "weight": None,
                "primary_metric_mean": (
                    float(mean) if isinstance(mean, (int, float)) else None
                ),
                "sample_count": int(diag.get("sample_count", 0)),
                **{key: None for key in _WEATHER_YEAR_METRIC_KEYS},
            }
        )
    return {
        "weather_year_metrics_schema_version": "1.0",
        "source": "adequacy_run",
        "primary_risk_metric": primary,
        "rows": rows,
        "attribution_note": (
            "逐年均值＝引擎逐年在线统计的均值（透出已算出的中间量，"
            "聚合公式零变更）；仅概率基准年按权重参与加权求和，"
            "压力考验年只做压力测试、不参与平均。"
        ),
    }


class AdequacyStopped(Exception):
    """Raised at a BATCH BOUNDARY when a control request asked for a
    safe stop: the current batch was already completed and its
    checkpoint atomically saved BEFORE this exception is raised, so a
    resume never repeats or loses accumulated samples. No
    ``MethodResult`` is published for a stopped run."""

    code = "ADEQUACY_STOPPED_AT_BATCH_BOUNDARY"

    def __init__(
        self, *, completed_batch: int, next_sample_index: int, checkpoint_path: Path | None
    ) -> None:
        super().__init__(
            f"safe stop honoured at batch boundary {completed_batch} "
            f"(next_sample_index={next_sample_index})"
        )
        self.completed_batch = completed_batch
        self.next_sample_index = next_sample_index
        self.checkpoint_path = checkpoint_path


def run_adequacy(
    *,
    bank: ScenarioBank,
    load_by_year: dict[int, np.ndarray],
    run_id: str,
    data_version_hash: str,
    config_hash: str,
    software_version: str,
    config: ConvergenceConfig | None = None,
    base_dir: Path | str,
    checkpoint_path: Path | str | None = None,
    max_batches: int | None = None,
    year_weights: dict[int, float] | None = None,
    deterministic_supply_by_year: dict[int, np.ndarray] | None = None,
    stop_check: Any = None,
    on_batch: Any = None,
) -> AdequacyResult:
    """Run (or resume) a sequential adequacy computation.

    ``max_batches`` limits the number of batches processed in this call
    (used by tests to simulate an interrupted run); it is not a
    production knob. ``stop_check`` is the T25 batch-boundary control
    seam: it is consulted AFTER each batch is processed and its
    checkpoint atomically saved; a truthy return stops the loop by
    raising :class:`AdequacyStopped` (never mid-batch, never without a
    saved checkpoint).
    """
    if config is None:
        config = default_config()
    if max_batches is not None and (
        isinstance(max_batches, bool)
        or not isinstance(max_batches, (int, np.integer))
        or int(max_batches) < 0
    ):
        raise ScenarioServiceError(
            "SERVICE_INVALID_MAX_BATCHES",
            "max_batches must be a non-negative integer or null",
        )
    _validate_bank(bank)
    loads = _validate_load(load_by_year, bank)
    roles = bank.index["year_roles"]
    baseline = [int(y) for y, role in roles.items() if role == "probability_baseline"]
    try:
        weights = validate_year_weights(year_weights, baseline)
    except AdequacyEngineError as exc:
        raise ScenarioServiceError(
            "SERVICE_WEIGHT_MISMATCH",
            str(exc),
        ) from exc
    engine = AdequacyEngine(
        bank=bank,
        load_by_year=loads,
        root_seed=int(bank.index["root_seed"]),
        year_weights=weights,
        primary_risk_metric=config.primary_risk_metric,
        deterministic_supply_by_year=deterministic_supply_by_year,
    )
    start_sample, completed_batch, engine = _resume_from_checkpoint(
        engine,
        checkpoint_path=checkpoint_path,
        expected_hashes={
            "data_version_hash": data_version_hash,
            "config_hash": config_hash,
            "software_version": software_version,
            "scenario_bank_id": bank.bank_id,
        },
        run_id=run_id,
        bank=bank,
        roles=roles,
        config=config,
    )

    sample_limit = min(int(bank.index["sample_count"]), config.max_samples)
    processed_batches = 0
    for batch in engine.iter_batches(config.batch_size, sample_limit):
        if batch[0] < start_sample:
            continue
        if max_batches is not None and processed_batches >= max_batches:
            break
        engine.process_batch(batch, run_id=run_id, recent_window_max=config.recent_batch_window)
        processed_batches += 1
        completed_batch += 1
        if on_batch is not None:
            # read-only batch-boundary observation (V1-T1-C): real
            # same-scale counts only - the callback receives REAL batch
            # progress and MUST NOT influence the computation (random
            # order, batch layout, sample counts and convergence are
            # untouched); exceptions propagate to the caller, which
            # owns progress isolation
            on_batch(
                {
                    "completed_batches": processed_batches,
                    "completed_samples": int(engine.processed_samples),
                    "total_batches_bound": -(-sample_limit // int(config.batch_size)),
                    "total_samples_bound": int(sample_limit),
                }
            )
        if checkpoint_path is not None:
            save_checkpoint(
                _snapshot(
                    engine,
                    run_id,
                    data_version_hash,
                    config_hash,
                    software_version,
                    completed_batch,
                    bank.bank_id,
                    sample_limit,
                    config,
                ),
                checkpoint_path,
                expected_years=list(bank.index["weather_years"]),
                expected_metrics=METRIC_NAMES,
                recent_window_max=config.recent_batch_window,
                baseline_years=[
                    int(y) for y, role in roles.items() if role == "probability_baseline"
                ],
                stress_years=[int(y) for y, role in roles.items() if role == "stress_test"],
            )
        convergence_state = engine.check_state(config)
        if convergence_state.should_stop:
            break
        if stop_check is not None and stop_check():
            # the current batch is complete and its checkpoint saved
            raise AdequacyStopped(
                completed_batch=completed_batch,
                next_sample_index=engine.processed_samples,
                checkpoint_path=Path(checkpoint_path) if checkpoint_path else None,
            )
    return _engine_to_result(engine, config)


def _resume_from_checkpoint(
    engine: AdequacyEngine,
    *,
    checkpoint_path: Path | str | None,
    expected_hashes: dict[str, Any],
    run_id: str,
    bank: ScenarioBank,
    roles: dict[int, str],
    config: ConvergenceConfig,
) -> tuple[int, int, AdequacyEngine]:
    """Resume from a matching checkpoint on exact batch boundaries
    (never skipping or recomputing samples); a checkpoint whose primary
    metric or sample limit disagrees with the current config blocks."""
    start_sample = 0
    completed_batch = 0
    if checkpoint_path is None or not Path(checkpoint_path).exists():
        return start_sample, completed_batch, engine
    state = load_checkpoint(
        checkpoint_path,
        expected=expected_hashes,
        run_id=run_id,
        expected_years=list(bank.index["weather_years"]),
        expected_metrics=METRIC_NAMES,
        recent_window_max=config.recent_batch_window,
        baseline_years=[int(y) for y, role in roles.items() if role == "probability_baseline"],
        stress_years=[int(y) for y, role in roles.items() if role == "stress_test"],
        expected_snapshot=config.to_snapshot(),
    )
    if state.primary_risk_metric != config.primary_risk_metric:
        raise CheckpointError(
            "CHECKPOINT_CONFIG_MISMATCH",
            "checkpoint primary_risk_metric differs from the current config",
        )
    if state.sample_limit != min(int(bank.index["sample_count"]), config.max_samples):
        raise CheckpointError(
            "CHECKPOINT_INVALID",
            "checkpoint sample_limit differs from the actual bank sample limit",
        )
    return (
        state.next_sample_index,
        state.completed_batch,
        _restore_engine(engine, state),
    )


def _restore_engine(engine: AdequacyEngine, state: CheckpointState) -> AdequacyEngine:
    for year in state.stats_by_year:
        if year not in engine.stats_by_year:
            raise ScenarioServiceError(
                "SERVICE_CHECKPOINT_YEAR_MISMATCH",
                f"checkpoint year {year} not in bank",
            )
        for name, stats in state.stats_by_year[year].items():
            if name not in engine.stats_by_year[year]:
                raise ScenarioServiceError(
                    "SERVICE_CHECKPOINT_METRIC_MISMATCH", f"metric {name} unknown"
                )
            engine.stats_by_year[year][name] = stats
    engine.formal_primary_stats = state.formal_primary_stats
    engine.recent_window = list(state.recent_window)
    engine.baseline_events = list(state.baseline_events)
    engine.stress_events = list(state.stress_events)
    engine.processed_samples = state.next_sample_index
    return engine


def _snapshot(
    engine: AdequacyEngine,
    run_id: str,
    data_version_hash: str,
    config_hash: str,
    software_version: str,
    completed_batch: int,
    bank_id: str,
    sample_limit: int,
    config: ConvergenceConfig,
) -> CheckpointState:
    from datetime import datetime

    return CheckpointState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        run_id=run_id,
        data_version_hash=data_version_hash,
        config_hash=config_hash,
        scenario_bank_id=bank_id,
        software_version=software_version,
        completed_batch=completed_batch,
        next_sample_index=engine.processed_samples,
        batch_size=config.batch_size,
        sample_limit=sample_limit,
        primary_risk_metric=config.primary_risk_metric,
        convergence_config=config.to_snapshot(),
        stats_by_year=engine.stats_by_year,
        formal_primary_stats=engine.formal_primary_stats,
        nonzero_sample_count=engine.nonzero_sample_count,
        recent_window=list(engine.recent_window),
        criteria=engine.check_state(config).criteria,
        baseline_events=list(engine.baseline_events),
        stress_events=list(engine.stress_events),
        created_at=datetime.now(UTC).isoformat(),
    )


__all__ = [
    "AdequacyResult",
    "AdequacyStopped",
    "ScenarioServiceError",
    "run_adequacy",
]


# ===== T12: per-sample risk series (ELCC chain) =====


class RiskSeriesError(ValueError):
    """Stable risk-series error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RiskSeriesOutcome:
    """Per-sample formal primary risk series + convergence snapshot.

    ``risk_series`` is a T10 ``RiskSampleSeries`` whose values are the
    engine's per-sample weighted primary risks (sample ids 0..N-1 in
    processing order) - a REAL sample-source chain (ScenarioBank ->
    AdequacyEngine), never constant arrays or re-drawn randomness.
    """

    risk_series: Any
    sample_count: int
    nonzero_sample_count: int
    risk_metrics: dict[str, float] | None
    risk_convergence: str
    result_status: str
    search_convergence: str
    should_stop: bool
    criteria: dict[str, bool]
    recent_batch_evidence: tuple[float, ...]
    scenario_bank_id: str
    primary_risk_metric: str
    sample_set_identity: str
    # R2-B4 (C-29): SHA-256 digest of the ACTUAL exogenous world this
    # series was computed in (data version, bank identity/seed, weights,
    # convergence, object tracks) - never a hardcoded label. Load and
    # candidate supply are the search variables and are NOT part of it.
    exogenous_fingerprint: str = ""
    # R3-B1: engine deficit-event dicts for the probability-baseline
    # years (adequacy._event_to_dict shape) - the raw material of the
    # run-dir gap picture. Previously collected then discarded.
    deficit_events: tuple[dict[str, Any], ...] = ()


def exogenous_invariant_digest(
    *,
    data_version_hash: str,
    config_hash: str,
    software_version: str,
    scenario_bank_id: str,
    root_seed: int,
    sample_count: int,
    primary_risk_metric: str,
    year_weights: dict[int, float],
    shared_object_ids: Any,
    candidate_object_ids: Any,
    convergence_snapshot: Any,
) -> str:
    """The real C-29 exogenous invariant fingerprint: one digest over
    everything the base and every trial must hold constant (方法说明
    第四章(一) 控制变量纪律). Computed per series from the ACTUAL
    arguments, so base-vs-trial drift becomes detectable instead of
    being masked by a shared constant."""
    payload = json.dumps(
        {
            "data_version_hash": str(data_version_hash),
            "config_hash": str(config_hash),
            "software_version": str(software_version),
            "scenario_bank_id": str(scenario_bank_id),
            "root_seed": int(root_seed),
            "sample_count": int(sample_count),
            "primary_risk_metric": str(primary_risk_metric),
            "year_weights": [[int(y), float(w)] for y, w in sorted(year_weights.items())],
            "shared_object_ids": sorted(str(o) for o in shared_object_ids),
            "candidate_object_ids": sorted(str(o) for o in candidate_object_ids),
            "convergence": convergence_snapshot,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return "ex1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) <= 0:
        raise RiskSeriesError("RISK_SERIES_INVALID_CONFIG", f"{field} must be a positive integer")
    return int(value)


def run_risk_series(
    *,
    bank: ScenarioBank,
    load_by_year: dict[int, np.ndarray],
    run_id: str,
    data_version_hash: str,
    config_hash: str,
    software_version: str,
    config: ConvergenceConfig | None = None,
    year_weights: dict[int, float] | None = None,
    deterministic_supply_by_year: dict[int, np.ndarray] | None = None,
    portfolio_id: str | None = None,
    shared_object_ids: frozenset[str] | None = None,
    candidate_object_ids: frozenset[str] | None = None,
    sample_limit: int | None = None,
) -> RiskSeriesOutcome:
    """Run the adequacy engine over a FULL sample set (no early stop)
    and return the per-sample formal primary risk series with the
    engine convergence snapshot.

    This is the T12 ELCC sample-source chain: the returned
    ``RiskSampleSeries`` values are the engine's real per-sample
    weighted primary risks. ``sample_limit`` caps the processed samples
    (all samples by default); the same limit must be used for the base
    and every trial so paired evaluation sees identical sample ids.
    """
    if config is None:
        config = default_config()
    _validate_bank(bank)
    loads = _validate_load(load_by_year, bank)
    roles = bank.index["year_roles"]
    baseline = [int(y) for y, role in roles.items() if role == "probability_baseline"]
    try:
        weights = validate_year_weights(year_weights, baseline)
    except AdequacyEngineError as exc:
        raise RiskSeriesError(
            "RISK_SERIES_WEIGHT_MISMATCH",
            str(exc),
        ) from exc
    engine = AdequacyEngine(
        bank=bank,
        load_by_year=loads,
        root_seed=int(bank.index["root_seed"]),
        year_weights=weights,
        primary_risk_metric=config.primary_risk_metric,
        deterministic_supply_by_year=deterministic_supply_by_year,
    )
    sample_count = int(bank.index["sample_count"])
    if sample_limit is not None:
        sample_count = _require_positive_int(sample_limit, "sample_limit")
        sample_count = min(sample_count, int(bank.index["sample_count"]))
    for batch in engine.iter_batches(config.batch_size, sample_count):
        engine.process_batch(batch, run_id=run_id, recent_window_max=config.recent_batch_window)
    if engine.processed_samples == 0:
        raise RiskSeriesError(
            "RISK_SERIES_NO_SAMPLES",
            "no samples were processed; a risk series requires samples",
        )
    values = engine.primary_risk_samples()
    if len(values) != sample_count:
        raise RiskSeriesError(
            "RISK_SERIES_INCOMPLETE",
            f"per-sample series has {len(values)} values, expected {sample_count}",
        )
    state = engine.check_state(config)
    aggregated = engine.aggregate()
    risk_metrics = {
        "lole_days_per_year": aggregated["lole_days_per_year"],
        "lolh_hours_per_year": aggregated["lolh_hours_per_year"],
        "eue_mwh_per_year": aggregated["eue_mwh_per_year"],
        "neue_ppm": aggregated["neue_ppm"],
        "demand_energy_mwh": aggregated["demand_energy_mwh"],
        # W5/C4: LOLP as a DERIVED metric (hours-weighted LOLH over the
        # probability-baseline year hours) - never a RiskMetric enum
        # member, never a criterion candidate
        "lolp_fraction": (
            aggregated["lolh_hours_per_year"]
            / sum(float(len(loads[int(y)])) for y in baseline)
        ),
    }
    years = tuple(int(y) for y in baseline)
    from capacity_credit.solvers.paired_evaluation import RiskSampleSeries

    sample_ids = tuple(range(sample_count))
    values_map = {sample_id: values[sample_id] for sample_id in sample_ids}
    from capacity_credit.contracts.enums import RiskMetric

    resolved_shared_ids = shared_object_ids or frozenset(
        {str(o["object_id"]) for o in bank.index["objects"]}
    )
    resolved_candidate_ids = candidate_object_ids or frozenset()
    # R2-C2: attach the shared-T08 convergence snapshot through the ONE
    # neutral adapter (application.risk_series - no second batching or
    # snapshot fact source), so the BASE side of a paired evaluation
    # carries the same closed convergence evidence as the trial side.
    from capacity_credit.application.risk_series import (
        risk_convergence_snapshot_from_values,
    )

    series = RiskSampleSeries(
        scenario_bank_id=bank.bank_id,
        primary_risk_metric=RiskMetric(config.primary_risk_metric),
        sample_ids=sample_ids,
        weather_years=years,
        year_weights=weights,
        portfolio_id=portfolio_id or "PORT_ELCC",
        shared_object_ids=resolved_shared_ids,
        candidate_object_ids=resolved_candidate_ids,
        values=values_map,
        convergence_snapshot=risk_convergence_snapshot_from_values(
            values_map, config
        ),
    )
    return RiskSeriesOutcome(
        risk_series=series,
        sample_count=sample_count,
        nonzero_sample_count=engine.nonzero_sample_count,
        risk_metrics=risk_metrics,
        risk_convergence=state.risk_convergence,
        result_status=state.result_status,
        search_convergence=state.search_convergence,
        should_stop=state.should_stop,
        criteria=dict(state.criteria or {}),
        recent_batch_evidence=tuple(engine.recent_window),
        scenario_bank_id=bank.bank_id,
        primary_risk_metric=config.primary_risk_metric,
        # R2-B4: the identity embeds the content-addressed bank, so two
        # equal-size sample sets from different banks are distinguishable
        sample_set_identity=f"gen_{sample_count}@{bank.bank_id[:16]}",
        exogenous_fingerprint=exogenous_invariant_digest(
            data_version_hash=str(data_version_hash),
            config_hash=str(config_hash),
            software_version=str(software_version),
            scenario_bank_id=bank.bank_id,
            root_seed=int(bank.index["root_seed"]),
            sample_count=sample_count,
            primary_risk_metric=config.primary_risk_metric,
            year_weights=weights,
            shared_object_ids=resolved_shared_ids,
            candidate_object_ids=resolved_candidate_ids,
            convergence_snapshot=config.to_snapshot(),
        ),
        deficit_events=tuple(engine.baseline_events),
    )


__all__ = [
    "AdequacyResult",
    "RiskSeriesError",
    "RiskSeriesOutcome",
    "ScenarioServiceError",
    "exogenous_invariant_digest",
    "run_adequacy",
    "run_risk_series",
]
