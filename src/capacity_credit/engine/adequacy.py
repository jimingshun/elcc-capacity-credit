"""Minimal adequacy kernel (T08, review round 1).

Batch adequacy computation over scenario-bank arrays:

- one ``ScenarioBank.read_batch`` call per batch (never per-sample
  ``read_object_array`` re-reads); capacity_mw is cached at engine
  start; hour axes / local dates / load checks are prebuilt per year;
- per Monte-Carlo sample the formal weighted primary is computed as
  ``R_s = sum(q_w * R_s,w)`` over probability-baseline years and updated
  exactly once into ``formal_primary_stats`` (never a pooled merge of
  different years);
- per-year stats stay stratified; annual results are reported as
  ``R = sum(q_w * mean(R_w))`` which matches the formal mean in machine
  tolerance;
- only probability-baseline events enter the formal deficit event table;
  stress-test events go to an independent diagnostic list;
- every formal event carries the real ``run_id``;
- ``compute_margin`` / ``compute_sample_metrics`` / ``detect_deficit_events``
  remain the single sources of truth.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np

from capacity_credit.contracts.enums import RiskMetric
from capacity_credit.engine.balance import compute_margin
from capacity_credit.engine.convergence import (
    ConvergenceConfig,
    ConvergenceState,
    check_convergence,
)
from capacity_credit.engine.events import detect_deficit_events
from capacity_credit.engine.metrics import compute_sample_metrics
from capacity_credit.engine.online_stats import OnlineStats, confidence_interval
from capacity_credit.scenarios.scenario_bank import ScenarioBank

METRIC_NAMES = (
    "primary",
    "lole_days_per_year",
    "lolh_hours_per_year",
    "eue_mwh_per_year",
    "neue_ppm",
    "demand_energy_mwh",
)


class AdequacyEngineError(ValueError):
    """Stable adequacy-engine error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_WEIGHT_SUM_TOLERANCE = 1e-9


def validate_year_weights(
    year_weights: dict[int, float] | None,
    baseline_years: list[int],
) -> dict[int, float]:
    """Reusable low-level year-weight validation (engine-level, so the
    engine never depends on the application layer)."""
    if year_weights is None:
        if not baseline_years:
            raise AdequacyEngineError(
                "ADEQUACY_NO_BASELINE_YEARS",
                "at least one probability-baseline year is required",
            )
        return {y: 1.0 / len(baseline_years) for y in baseline_years}
    if not isinstance(year_weights, dict):
        raise AdequacyEngineError("ADEQUACY_INVALID_WEIGHTS", "year_weights must be a mapping")
    if set(year_weights.keys()) != set(baseline_years):
        raise AdequacyEngineError(
            "ADEQUACY_INVALID_WEIGHTS",
            "year_weights must cover exactly the probability-baseline years",
        )
    for year, weight in year_weights.items():
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float, np.integer, np.floating))
            or not np.isfinite(float(weight))
            or float(weight) < 0.0
        ):
            raise AdequacyEngineError(
                "ADEQUACY_INVALID_WEIGHTS",
                f"weight for year {year} must be finite and non-negative",
            )
    total = sum(float(w) for w in year_weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise AdequacyEngineError(
            "ADEQUACY_INVALID_WEIGHTS",
            f"year weights must sum to 1 (got {total})",
        )
    return {int(y): float(w) for y, w in year_weights.items()}


def _hour_axis(year: int, hours: int, timezone: str) -> Any:
    import pandas as pd

    start = pd.Timestamp(year, 1, 1, tz=timezone)
    return pd.Series([start + pd.Timedelta(hours=i) for i in range(hours)])


def validate_batch_indices(batch_indices: list[Any], sample_count: int) -> list[int]:
    """Strict batch-index validation: non-empty, non-bool integers,
    unique, strictly increasing and inside the bank sample range."""
    if not isinstance(batch_indices, (list, tuple)) or not batch_indices:
        raise AdequacyEngineError(
            "ADEQUACY_INVALID_BATCH", "batch must be a non-empty index sequence"
        )
    checked: list[int] = []
    for value in batch_indices:
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
            raise AdequacyEngineError(
                "ADEQUACY_INVALID_BATCH", "batch indices must be non-boolean integers"
            )
        checked.append(int(value))
    if checked != sorted(checked):
        raise AdequacyEngineError(
            "ADEQUACY_INVALID_BATCH", "batch indices must be strictly increasing"
        )
    if len(set(checked)) != len(checked):
        raise AdequacyEngineError("ADEQUACY_INVALID_BATCH", "batch indices must be unique")
    if any(index < 0 or index >= sample_count for index in checked):
        raise AdequacyEngineError("ADEQUACY_INVALID_BATCH", "batch index out of bank sample range")
    return checked


def _event_to_dict(event: Any, run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "sample_id": event.sample_id,
        "weather_year": event.weather_year,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat(),
        "duration_hours": event.duration_hours,
        "event_eue_mwh": event.event_eue_mwh,
        "max_unserved_power_mw": event.max_unserved_power_mw,
        "year_range": list(event.year_range),
        "crosses_year_boundary": event.crosses_year_boundary,
    }


class AdequacyEngine:
    """Sequential batch adequacy engine over a scenario bank."""

    def __init__(
        self,
        *,
        bank: ScenarioBank,
        load_by_year: dict[int, np.ndarray],
        root_seed: int,
        year_weights: dict[int, float] | None = None,
        primary_risk_metric: RiskMetric | str = RiskMetric.LOLE_DAYS_PER_YEAR,
        deterministic_supply_by_year: dict[int, np.ndarray] | None = None,
    ) -> None:
        self.bank = bank
        self.load_by_year = load_by_year
        self.root_seed = root_seed
        self.deterministic_supply_by_year = deterministic_supply_by_year
        if deterministic_supply_by_year is not None:
            for year, supply in deterministic_supply_by_year.items():
                if year not in bank.index["weather_years"]:
                    raise AdequacyEngineError(
                        "ADEQUACY_SUPPLY_YEAR_MISMATCH",
                        f"deterministic supply year {year} not in bank",
                    )
                hours = int(bank.index["year_hours"][str(year)])
                if np.asarray(supply).size != hours:
                    raise AdequacyEngineError(
                        "ADEQUACY_SUPPLY_MISMATCH",
                        f"deterministic supply for year {year} must have {hours} hours",
                    )
                if (
                    not np.isfinite(np.asarray(supply, dtype=float)).all()
                    or (np.asarray(supply, dtype=float) < 0).any()
                ):
                    raise AdequacyEngineError(
                        "ADEQUACY_SUPPLY_MISMATCH",
                        "deterministic supply must be finite and non-negative",
                    )
        if isinstance(primary_risk_metric, RiskMetric):
            self.primary_risk_metric = primary_risk_metric.value
        elif isinstance(primary_risk_metric, str) and primary_risk_metric in {
            m.value for m in RiskMetric
        }:
            self.primary_risk_metric = primary_risk_metric
        else:
            raise AdequacyEngineError(
                "ADEQUACY_INVALID_PRIMARY_METRIC",
                f"primary_risk_metric {primary_risk_metric!r} unsupported",
            )
        self.years = list(bank.index["weather_years"])
        roles = bank.index["year_roles"]
        self.probability_years = [
            int(y) for y, role in roles.items() if role == "probability_baseline"
        ]
        self.stress_years = [int(y) for y, role in roles.items() if role == "stress_test"]
        if not self.probability_years:
            raise AdequacyEngineError(
                "ADEQUACY_NO_BASELINE_YEARS",
                "at least one probability-baseline year is required",
            )
        self.year_weights = validate_year_weights(year_weights, self.probability_years)
        self.stats_by_year: dict[int, dict[str, OnlineStats]] = {
            y: {name: OnlineStats() for name in METRIC_NAMES} for y in self.years
        }
        self.formal_primary_stats = OnlineStats()
        self.recent_window: list[float] = []
        self.baseline_events: list[dict[str, Any]] = []
        self.stress_events: list[dict[str, Any]] = []
        self.processed_samples = 0
        self.sample_primary_risks: list[float] = []
        self._capacity_map = {
            o["object_id"]: float(o["capacity_mw"]) for o in bank.index["objects"]
        }
        self._axes: dict[int, tuple[Any, np.ndarray]] = {
            year: _build_axis(bank, year) for year in self.years
        }

    @property
    def sample_count(self) -> int:
        return self.formal_primary_stats.sample_count

    @property
    def nonzero_sample_count(self) -> int:
        return self.formal_primary_stats.nonzero_sample_count

    def _weighted_primary(self) -> float:
        return sum(
            self.year_weights[year] * self.stats_by_year[year]["primary"].mean
            for year in self.probability_years
        )

    def process_batch(
        self,
        batch_indices: list[Any],
        run_id: str = "batch",
        recent_window_max: int = 100,
    ) -> None:
        """Process one batch with exactly one ``read_batch`` call."""
        batch = validate_batch_indices(batch_indices, int(self.bank.index["sample_count"]))
        data = self.bank.read_batch({"sample_indices": batch})
        batch_primary: list[float] = []
        for position, sample in enumerate(batch):
            weighted_eue = 0.0
            weighted_demand = 0.0
            weighted = 0.0
            for year in self.years:
                axis, local_dates = self._axes[year]
                hours = len(axis)
                supply = np.zeros(hours, dtype=np.float64)
                for object_id, capacity in self._capacity_map.items():
                    frac = data["data"][object_id][year][position]
                    supply += np.asarray(frac, dtype=float) * capacity
                if self.deterministic_supply_by_year is not None:
                    supply += np.asarray(self.deterministic_supply_by_year[year], dtype=float)
                load = self.load_by_year[year]
                balance = compute_margin(supply, load)
                metrics = compute_sample_metrics(
                    balance,
                    local_dates=local_dates,
                )
                self.stats_by_year[year]["primary"].update(
                    getattr(metrics, self.primary_risk_metric)
                )
                self.stats_by_year[year]["lole_days_per_year"].update(metrics.lole_days_per_year)
                self.stats_by_year[year]["lolh_hours_per_year"].update(metrics.lolh_hours_per_year)
                self.stats_by_year[year]["eue_mwh_per_year"].update(metrics.eue_mwh_per_year)
                self.stats_by_year[year]["neue_ppm"].update(metrics.neue_ppm)
                self.stats_by_year[year]["demand_energy_mwh"].update(metrics.demand_energy_mwh)
                if year in self.probability_years:
                    q = self.year_weights[year]
                    if self.primary_risk_metric == RiskMetric.NEUE_PPM.value:
                        weighted_eue += q * metrics.eue_mwh_per_year
                        weighted_demand += q * metrics.demand_energy_mwh
                    else:
                        weighted += q * getattr(metrics, self.primary_risk_metric)
                events = detect_deficit_events(
                    balance,
                    local_dates,
                    axis=axis,
                    sample_id=f"s{sample}",
                    run_id=run_id,
                )
                target = (
                    self.baseline_events if year in self.probability_years else self.stress_events
                )
                target.extend(_event_to_dict(event, run_id) for event in events)
            if self.primary_risk_metric == RiskMetric.NEUE_PPM.value:
                if weighted_demand <= 0:
                    raise AdequacyEngineError(
                        "ADEQUACY_INVALID_DEMAND",
                        "weighted demand energy must be positive",
                    )
                weighted = 1_000_000.0 * weighted_eue / weighted_demand
            self.formal_primary_stats.update(weighted)
            batch_primary.append(weighted)
        self.recent_window.append(float(np.mean(batch_primary)))
        self.recent_window = self.recent_window[-recent_window_max:]
        self.processed_samples += len(batch)
        # per-sample formal weighted primary (T12 ELCC chain): aligned
        # with the batch order -> sample ids 0..N-1 in processing order
        self.sample_primary_risks.extend(batch_primary)

    def primary_risk_samples(self) -> list[float]:
        """Per-sample formal weighted primary risk values in processing
        order (sample ids 0..N-1 for an uninterrupted full run)."""
        return list(self.sample_primary_risks)

    def primary_interval(self, config: ConvergenceConfig) -> Any:
        return confidence_interval(
            self.formal_primary_stats,
            confidence_level=config.confidence_level,
            use_student_t=config.use_student_t,
            normal_interval_min_samples=config.normal_interval_min_samples,
        )

    def aggregate(self) -> dict[str, float]:
        """Weighted per-year means (never pooled); NEUE re-derived."""
        if self.formal_primary_stats.sample_count == 0:
            raise AdequacyEngineError(
                "ADEQUACY_NO_SAMPLES",
                "risk metrics are unavailable before any sample is processed",
            )
        aggregated: dict[str, float] = {}
        for name in METRIC_NAMES:
            if name in ("primary",):
                continue
            aggregated[name] = sum(
                self.year_weights[year] * self.stats_by_year[year][name].mean
                for year in self.probability_years
            )
        eue = aggregated["eue_mwh_per_year"]
        demand = aggregated["demand_energy_mwh"]
        if demand <= 0:
            raise AdequacyEngineError(
                "ADEQUACY_INVALID_DEMAND", "aggregated demand energy must be positive"
            )
        aggregated["neue_ppm"] = 1_000_000.0 * eue / demand
        return aggregated

    def check_state(self, config: ConvergenceConfig) -> ConvergenceState:
        return check_convergence(
            self.formal_primary_stats,
            self.recent_window,
            config,
        )

    def run(
        self, max_samples: int, config: ConvergenceConfig, run_id: str = "batch"
    ) -> dict[str, Any]:
        """Run batches until convergence or ``max_samples`` (no checkpoints)."""
        for batch in self.iter_batches(config.batch_size, max_samples):
            self.process_batch(batch, run_id=run_id, recent_window_max=config.recent_batch_window)
            state = self.check_state(config)
            if state.should_stop:
                break
        return self.finalize(config)

    def finalize(self, config: ConvergenceConfig) -> dict[str, Any]:
        sample_count = self.formal_primary_stats.sample_count
        if sample_count == 0:
            return {
                "risk_metrics": None,
                "primary_risk_metric": self.primary_risk_metric,
                "primary_interval": None,
                "sample_count": 0,
                "nonzero_sample_count": 0,
                "stats_by_year": self.stats_by_year,
                "deficit_events": list(self.baseline_events),
                "risk_convergence": "not_converged",
                "result_status": "not_converged",
                "search_convergence": "not_applicable",
                "scenario_bank_id": self.bank.bank_id,
            }
        aggregated = self.aggregate()
        state = self.check_state(config)
        interval_payload: dict[str, Any] | None = None
        if sample_count >= 2:
            try:
                interval = self.primary_interval(config)
                interval_payload = {
                    "lower": interval.lower,
                    "upper": interval.upper,
                    "half_width": interval.half_width,
                    "method": interval.method,
                }
            except Exception as exc:
                raise AdequacyEngineError(
                    "ADEQUACY_CI_FAILURE",
                    f"confidence interval computation failed: {exc}",
                ) from exc
        metrics = {
            "lole_days_per_year": aggregated["lole_days_per_year"],
            "lolh_hours_per_year": aggregated["lolh_hours_per_year"],
            "eue_mwh_per_year": aggregated["eue_mwh_per_year"],
            "neue_ppm": aggregated["neue_ppm"],
            "demand_energy_mwh": aggregated["demand_energy_mwh"],
        }
        return {
            "risk_metrics": metrics,
            "primary_risk_metric": self.primary_risk_metric,
            "primary_interval": interval_payload,
            "sample_count": sample_count,
            "nonzero_sample_count": self.formal_primary_stats.nonzero_sample_count,
            "stats_by_year": self.stats_by_year,
            "deficit_events": list(self.baseline_events),
            "risk_convergence": state.risk_convergence,
            "result_status": state.result_status,
            "search_convergence": state.search_convergence,
            "scenario_bank_id": self.bank.bank_id,
        }

    def iter_batches(self, batch_size: int, max_samples: int | None = None) -> Iterator[list[int]]:
        sample_count = int(self.bank.index["sample_count"])
        if max_samples is not None:
            sample_count = min(sample_count, max_samples)
        for start in range(self.processed_samples, sample_count, batch_size):
            yield list(range(start, min(start + batch_size, sample_count)))


def _build_axis(bank: ScenarioBank, year: int) -> tuple[Any, np.ndarray]:
    import pandas as pd

    hours = int(bank.index["year_hours"][str(year)])
    timezone = bank.index["timezone"]
    start = pd.Timestamp(year, 1, 1, tz=timezone)
    axis = pd.Series([start + pd.Timedelta(hours=i) for i in range(hours)])
    return axis, axis.dt.date.to_numpy()


def compute_batch_metrics(
    bank: ScenarioBank,
    batch_indices: list[int],
    load_by_year: dict[int, np.ndarray],
    root_seed: int,
    run_id: str = "batch",
) -> dict[str, Any]:
    """Per-sample metrics and events for one batch (one read_batch call).

    Kept for callers that need raw per-sample metrics; the engine path
    uses the same logic internally. ``run_id`` is carried into every
    event.
    """
    batch = validate_batch_indices(batch_indices, int(bank.index["sample_count"]))
    data = bank.read_batch({"sample_indices": batch})
    objects = [o["object_id"] for o in bank.index["objects"]]
    capacities = {o["object_id"]: float(o["capacity_mw"]) for o in bank.index["objects"]}
    years = list(bank.index["weather_years"])
    bank.index["year_roles"]
    samples_by_year: dict[int, list[Any]] = {y: [] for y in years}
    events_by_year: dict[int, list[Any]] = {y: [] for y in years}
    axes = {y: _build_axis(bank, y) for y in years}
    for position, sample in enumerate(batch):
        for year in years:
            axis, local_dates = axes[year]
            hours = len(axis)
            supply = np.zeros(hours, dtype=np.float64)
            for object_id in objects:
                frac = data["data"][object_id][year][position]
                supply += np.asarray(frac, dtype=float) * capacities[object_id]
            load = np.asarray(load_by_year[year], dtype=float)
            if load.size != hours:
                raise AdequacyEngineError(
                    "ADEQUACY_LOAD_MISMATCH",
                    f"load length {load.size} != year hours {hours}",
                )
            balance = compute_margin(supply, load)
            metrics = compute_sample_metrics(balance, local_dates=local_dates)
            events = detect_deficit_events(
                balance,
                local_dates,
                axis=axis,
                sample_id=f"s{sample}",
                run_id=run_id,
            )
            samples_by_year[year].append(metrics)
            events_by_year[year].extend(_event_to_dict(event, run_id) for event in events)
    return {"samples": samples_by_year, "events": events_by_year}


__all__ = [
    "METRIC_NAMES",
    "AdequacyEngine",
    "AdequacyEngineError",
    "compute_batch_metrics",
    "validate_batch_indices",
]
