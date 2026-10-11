"""Neutral shared risk-series + convergence evidence adapter (T18 round-4).

Single home for building a ``RiskSampleSeries`` from resource-run evidence
WITH its auditable shared-T08 convergence snapshot. EFC and MRI both call
these functions - there is no second copy and no second convergence fact
source.

- ``risk_convergence_snapshot_from_values``: the ONE batching / snapshot
  generator (shared ``ConvergenceConfig`` + ``engine.convergence.check_convergence``
  + the neutral ``RiskConvergenceSnapshot``). Batching uses the CANONICAL
  SAMPLE ORDER (sample_id order for a Mapping, preserved order for a
  Sequence) - NEVER ``sorted`` by risk value, because that destroys the
  temporal recent-window evidence. Solvers never re-batch or re-derive
  convergence; they consume the snapshot ATTACHED to a series.
- ``risk_series_from_evidence``: per-sample weighted risk aggregation over
  the probability-baseline years (C), passing the sample_id -> value
  mapping (sample identity preserved) and attaching the full snapshot.

The per-sample paired statistics (D_s mean / SE / CI / step consistency)
continue to live in ``solvers.paired_evaluation`` - never recomputed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from capacity_credit.contracts.enums import RiskMetric
from capacity_credit.contracts.risk_convergence import (
    RiskConvergenceError,
    RiskConvergenceSnapshot,
    convergence_config_identity,
)
from capacity_credit.engine.convergence import (
    ConvergenceConfig,
    check_convergence,
    default_config,
)
from capacity_credit.engine.online_stats import OnlineStats
from capacity_credit.solvers.paired_evaluation import RiskSampleSeries

DEFAULT_PORTFOLIO_ID = "PORT_EFC"


def _strict_sample_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise RiskConvergenceError(
            "CONVERGENCE_INVALID_SAMPLES",
            f"sample_id {value!r} must be a strict integer",
        )
    return int(value)


def _ordered_risk_values(values: Any) -> list[float]:
    """The CANONICAL sample-order risk values.

    - Mapping[int, float]: sample ids strictly validated and unique, then
      the values are taken in ascending sample_id order (sample identity is
      preserved - NEVER ``sorted`` by the risk values themselves);
    - Sequence: the ORIGINAL order is preserved;
    - set / other unordered iterables and missing / duplicate sample ids
      are rejected.
    """
    if isinstance(values, set):
        raise RiskConvergenceError(
            "CONVERGENCE_INVALID_SAMPLES",
            "convergence input must be a Mapping or a Sequence - a set is unordered",
        )
    if isinstance(values, Mapping):
        sample_ids = [_strict_sample_id(sample_id) for sample_id in values]
        if len(set(sample_ids)) != len(sample_ids):
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SAMPLES",
                "convergence input must not contain duplicate sample ids",
            )
        return [float(values[sample_id]) for sample_id in sorted(sample_ids)]
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        return [float(value) for value in values]
    raise RiskConvergenceError(
        "CONVERGENCE_INVALID_SAMPLES",
        "convergence input must be a Mapping[int, float] or a Sequence of numbers",
    )


def risk_convergence_snapshot_from_values(
    values: Any,
    convergence_config: ConvergenceConfig | None = None,
) -> RiskConvergenceSnapshot:
    """The SHARED T08 convergence snapshot for one set of per-sample risk
    values, batched in CANONICAL SAMPLE ORDER (see ``_ordered_risk_values``
    - never ``sorted`` by risk value). The snapshot records its config
    source / identity so MRI/EFC never rely on an unrecorded fact source."""
    explicit_config = convergence_config is not None
    config = convergence_config or default_config()
    ordered = _ordered_risk_values(values)
    stats = OnlineStats()
    for value in ordered:
        stats.update(value)
    batch_size = max(1, config.batch_size)
    batch_means: list[float] = []
    for start in range(0, len(ordered), batch_size):
        chunk = ordered[start : start + batch_size]
        if chunk:
            batch_means.append(float(np.mean(chunk)))
    # The T08 recent-window contract: only the LAST recent_batch_window
    # batch means drive the stability criterion (same as the adequacy
    # engine's ``self.recent_window[-recent_window_max:]``).
    recent_window = batch_means[-config.recent_batch_window :]
    state = check_convergence(stats, recent_window, config)
    source = "run_declared" if explicit_config else "software_default"
    config_identity = convergence_config_identity(config.to_snapshot())
    return RiskConvergenceSnapshot.from_convergence_state(
        state,
        tuple(recent_window),
        source=source,
        config_identity=config_identity,
    )


def risk_series_from_evidence(
    evidence: Any,
    metric: RiskMetric,
    bank: Any,
    shared_ids: frozenset[str],
    candidate_ids: frozenset[str],
    weather_years: tuple[int, ...],
    year_weights: dict[int, float],
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    convergence_config: ConvergenceConfig | None = None,
    criterion_window: tuple[set[int], set[int]] | None = None,
) -> RiskSampleSeries:
    """Per-sample risk series with FORMAL year-weight aggregation:
    value[sample] = sum(weight[year] * metric[year, sample]) over the
    probability-baseline years only. The sample_id -> value mapping is
    passed through to the snapshot generator (sample identity preserved),
    and the full convergence snapshot is ATTACHED to the series (the
    single convergence fact source for EFC and MRI).

    ``criterion_window`` (MR-01.1, optional) = (months, hours) sets: each
    sample's balance is sliced to the window hours BEFORE the T06 metric
    formulas run (EFC winter-evening-peak calibre). None = the legacy
    full-year metrics, bit-identical behavior."""
    from capacity_credit.engine.balance import compute_margin
    from capacity_credit.engine.metrics import (
        compute_sample_metrics,
        restrict_balance_to_window,
    )

    years = tuple(sorted(int(y) for y in weather_years))
    weights = {int(year): float(year_weights[year]) for year in years}
    per_year_sample: dict[tuple[int, int], float] = {}
    for (year, sample), supply in evidence.post_storage_supply_by_sample.items():
        balance = compute_margin(supply, evidence.load_by_year[year])
        local_dates = evidence.local_dates_by_year[year]
        if criterion_window is not None:
            months, hours = criterion_window
            balance, local_dates = restrict_balance_to_window(
                balance, local_dates, months=months, hours=hours
            )
        metrics = compute_sample_metrics(balance, local_dates=local_dates)
        per_year_sample[(int(year), int(sample))] = float(getattr(metrics, metric.value))
    samples = sorted({sample for (_year, sample) in per_year_sample})
    values: dict[int, float] = {}
    for sample in samples:
        values[sample] = float(
            sum(weights[year] * per_year_sample[(year, sample)] for year in years)
        )
    snapshot = risk_convergence_snapshot_from_values(values, convergence_config)
    return RiskSampleSeries(
        scenario_bank_id=bank.bank_id,
        primary_risk_metric=metric,
        sample_ids=tuple(samples),
        weather_years=years,
        year_weights=weights,
        portfolio_id=portfolio_id,
        shared_object_ids=shared_ids,
        candidate_object_ids=candidate_ids,
        values=values,
        convergence_snapshot=snapshot,
    )


__all__ = [
    "DEFAULT_PORTFOLIO_ID",
    "risk_convergence_snapshot_from_values",
    "risk_series_from_evidence",
]
