"""Sample and weather-year risk metrics (T06).

Per sample: lolh_hours_per_year, lole_days_per_year (local calendar
days), eue_mwh_per_year, demand_energy_mwh and neue_ppm. Demand energy
is always derived from the balance result's demand vector and its
``delta_t_hours`` - callers cannot pass an arbitrary, unverifiable
demand energy. Weather-year aggregation requires the sample years, the
declared years and the weight years to be the same set, computes the
per-year sample mean first, then applies year weights:
R = sum(q_w * mean_s(R_s,w)). NEUE is recomputed from the aggregated EUE
and demand (methodology V0.4 §3.2). Field names always carry units.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from capacity_credit.data.validation import default_equal_weights, validate_weather_year_weights
from capacity_credit.engine.balance import BalanceResult, compute_demand_energy_mwh

_NEUE_REL_TOL = 1e-9

_METRIC_FIELDS = (
    "lolh_hours_per_year",
    "lole_days_per_year",
    "eue_mwh_per_year",
    "demand_energy_mwh",
    "neue_ppm",
)


class MetricsError(ValueError):
    """Stable metrics error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RiskMetricsSample:
    lolh_hours_per_year: int
    lole_days_per_year: int
    eue_mwh_per_year: float
    demand_energy_mwh: float
    neue_ppm: float


@dataclass(frozen=True)
class AggregateMetrics:
    lolh_hours_per_year: float
    lole_days_per_year: float
    eue_mwh_per_year: float
    demand_energy_mwh: float
    neue_ppm: float
    generated_weights: pd.DataFrame | None = field(default=None)


def _resolve_tolerance(balance: BalanceResult, tolerance: float | None,
                       code: str) -> float:
    if tolerance is None:
        return balance.tolerance
    tolerance = float(tolerance)
    if tolerance != balance.tolerance:
        raise MetricsError(
            code,
            f"tolerance {tolerance} differs from balance tolerance "
            f"{balance.tolerance}",
        )
    return tolerance


def compute_sample_metrics(
    balance: BalanceResult,
    *,
    local_dates: np.ndarray,
    tolerance: float | None = None,
) -> RiskMetricsSample:
    """Compute sample risk metrics from the single balance result.

    Demand energy is computed from ``balance.demand_mw`` and
    ``balance.delta_t_hours``; the caller cannot pass an inconsistent
    demand energy. ``tolerance`` defaults to ``balance.tolerance`` and,
    if given, must match it.
    """
    tol = _resolve_tolerance(balance, tolerance, "METRICS_TOLERANCE_MISMATCH")
    dates = np.asarray(local_dates)
    if dates.size != balance.unserved_power_mw.size:
        raise MetricsError(
            "METRICS_LENGTH_MISMATCH",
            f"local_dates {dates.size} != balance {balance.unserved_power_mw.size}",
        )
    demand = compute_demand_energy_mwh(balance.demand_mw, balance.delta_t_hours)
    if not np.isfinite(demand) or demand <= 0:
        raise MetricsError(
            "METRICS_INVALID_DEMAND",
            "demand energy must be finite and positive",
        )
    deficit = balance.unserved_power_mw > tol
    lolh = int(deficit.sum())
    deficit_dates = pd.Series(dates[deficit]).dropna()
    lole_days = int(deficit_dates.nunique())
    eue = float(balance.unserved_energy_mwh.sum())
    neue = 1_000_000.0 * eue / demand
    return RiskMetricsSample(
        lolh_hours_per_year=lolh,
        lole_days_per_year=lole_days,
        eue_mwh_per_year=eue,
        demand_energy_mwh=demand,
        neue_ppm=neue,
    )


def restrict_balance_to_window(
    balance: BalanceResult,
    local_dates: np.ndarray,
    *,
    months: set[int],
    hours: set[int],
) -> tuple[BalanceResult, np.ndarray]:
    """MR-01.1: slice ONE sample's balance + local dates down to the
    criterion-window hours (EFC winter-evening-peak calibre, 方法修订
    MR-01.1).

    The window filter lives ABOVE the balance kernel: the arrays are
    sliced and the T06 metric formulas then run UNCHANGED on the slice -
    the balance physics is never re-implemented here. An empty month or
    hour selection (or a slice that selects no hour of the year) is a
    stable refusal: an empty window would silently zero every metric."""
    dates = np.asarray(local_dates)
    if dates.size != balance.unserved_power_mw.size:
        raise MetricsError(
            "METRICS_LENGTH_MISMATCH",
            f"local_dates {dates.size} != balance {balance.unserved_power_mw.size}",
        )
    if not months or not hours:
        raise MetricsError(
            "METRICS_WINDOW_EMPTY",
            "criterion window must name at least one month and one hour",
        )
    index = pd.DatetimeIndex(dates)
    mask = np.isin(index.month, np.fromiter(months, dtype=np.int64)) & np.isin(
        index.hour, np.fromiter(hours, dtype=np.int64)
    )
    if not bool(mask.any()):
        raise MetricsError(
            "METRICS_WINDOW_EMPTY",
            "the criterion window selects no hour of the run's time axis",
        )
    sliced = BalanceResult(
        margin_mw=balance.margin_mw[mask],
        unserved_power_mw=balance.unserved_power_mw[mask],
        unserved_energy_mwh=balance.unserved_energy_mwh[mask],
        demand_mw=balance.demand_mw[mask],
        delta_t_hours=balance.delta_t_hours,
        tolerance=balance.tolerance,
    )
    return sliced, dates[mask]


def compute_seasonal_sample_metrics(
    balance: BalanceResult,
    *,
    local_dates: np.ndarray,
    heating_months: Any,
    tolerance: float | None = None,
) -> dict[str, RiskMetricsSample]:
    """SL-01 (method sign-off 2026-09-19): split ONE sample's metrics
    into the heating and non-heating segments.

    This is the MR-01.1 machinery applied twice: each segment is a
    months-mask slice of the SAME balance and the T06 metric formulas
    run UNCHANGED on the slices - the balance physics is never
    re-implemented. Because a year's days partition into the two
    disjoint segments, the segment LOLE days ADD UP to the annual LOLE
    (the property the seasonal criterion split leans on).

    ``heating_months`` must be a non-empty proper subset of 1..12
    without duplicates (all 12 months would leave the non-heating
    segment empty and is a refusal, never a degenerate 0).
    """
    try:
        values = [int(m) for m in heating_months]
    except (TypeError, ValueError):
        raise MetricsError(
            "METRICS_HEATING_MONTHS_INVALID",
            f"heating_months must be a list of months 1-12, got "
            f"{heating_months!r}",
        ) from None
    months = frozenset(values)
    if (
        not months
        or len(values) != len(months)
        or not months <= frozenset(range(1, 13))
        or months == frozenset(range(1, 13))
    ):
        raise MetricsError(
            "METRICS_HEATING_MONTHS_INVALID",
            "heating_months must be a non-empty proper subset of months "
            f"1-12 without duplicates, got {heating_months!r}",
        )
    all_hours = set(range(24))
    heating_balance, heating_dates = restrict_balance_to_window(
        balance,
        local_dates,
        months=set(months),
        hours=all_hours,
    )
    non_balance, non_dates = restrict_balance_to_window(
        balance,
        local_dates,
        months=set(range(1, 13)) - months,
        hours=all_hours,
    )
    return {
        "heating": compute_sample_metrics(
            heating_balance, local_dates=heating_dates, tolerance=tolerance
        ),
        "non_heating": compute_sample_metrics(
            non_balance, local_dates=non_dates, tolerance=tolerance
        ),
    }


def weighted_year_mean(
    values_by_year: dict[int, list[float]],
    weights: dict[int, float],
) -> float:
    """R = sum(q_w * mean_s(R_s,w)); never a pooled unweighted mean."""
    for year, weight in weights.items():
        if not np.isfinite(weight) or weight < 0:
            raise MetricsError(
                "METRICS_INVALID_WEIGHTS",
                f"weight for year {year} must be finite and non-negative",
            )
    total = 0.0
    for year, values in values_by_year.items():
        if not values:
            raise MetricsError(
                "METRICS_NO_SAMPLES",
                f"no samples for weather year {year}",
            )
        total += weights[year] * float(np.mean(values))
    return total


def _validate_sample(sample: RiskMetricsSample) -> None:
    lolh = float(sample.lolh_hours_per_year)
    lole = float(sample.lole_days_per_year)
    eue = float(sample.eue_mwh_per_year)
    demand = float(sample.demand_energy_mwh)
    neue = float(sample.neue_ppm)
    if not all(np.isfinite(v) for v in (lolh, lole, eue, demand, neue)):
        raise MetricsError(
            "METRICS_INVALID_SAMPLE", "sample metrics must all be finite"
        )
    if lolh < 0 or lole < 0 or eue < 0 or neue < 0:
        raise MetricsError(
            "METRICS_INVALID_SAMPLE",
            "LOLH, LOLE-day, EUE and NEUE must be non-negative",
        )
    if demand <= 0:
        raise MetricsError(
            "METRICS_INVALID_SAMPLE", "sample demand energy must be positive"
        )
    if not lolh.is_integer() or not lole.is_integer():
        raise MetricsError(
            "METRICS_INVALID_SAMPLE", "LOLH and LOLE-day must be integer counts"
        )
    expected_neue = 1_000_000.0 * eue / demand
    if abs(neue - expected_neue) > _NEUE_REL_TOL * max(abs(expected_neue), 1.0):
        raise MetricsError(
            "METRICS_INVALID_SAMPLE",
            f"NEUE {neue} inconsistent with EUE/demand {expected_neue}",
        )


def aggregate_year_metrics(
    samples_by_year: dict[int, list[RiskMetricsSample]],
    weights: pd.DataFrame | None,
    *,
    declared_years: list[int],
) -> AggregateMetrics:
    """Weighted aggregation: per-year mean first, then year weights.

    ``weights`` must contain exactly one weight set (a single non-empty
    ``weight_set_id``); multiple sets are rejected with
    ``METRICS_MULTIPLE_WEIGHT_SETS`` regardless of row order. When
    ``weights`` is None, an equal-weight set is generated through
    ``default_equal_weights`` and the generation record is returned in
    ``AggregateMetrics.generated_weights``.
    """
    if weights is None:
        weights = default_equal_weights(sorted(declared_years))
        generated_weights: pd.DataFrame | None = weights
    else:
        generated_weights = None
    # general contract first: missing columns, empty table, numeric,
    # coverage, normalization and source issues are all normalized to
    # METRICS_INVALID_WEIGHTS before any weight_set_id access
    issues = validate_weather_year_weights(weights, declared_years)
    if issues:
        raise MetricsError(
            "METRICS_INVALID_WEIGHTS",
            "; ".join(issue.message for issue in issues[:3]),
        )
    if (
        weights["weight_set_id"].isna().any()
        or weights["weight_set_id"].astype(str).str.strip().eq("").any()
        or weights["weight_set_id"].nunique() != 1
    ):
        raise MetricsError(
            "METRICS_MULTIPLE_WEIGHT_SETS",
            "aggregation accepts exactly one weight set (single weight_set_id)",
        )
    weight_map: dict[int, float] = {}
    for _, row in weights.iterrows():
        weight_map[int(row["weather_year"])] = float(row["weight"])
    sample_years = set(samples_by_year.keys())
    declared = set(declared_years)
    weighted_years = set(weight_map.keys())
    if sample_years != declared:
        missing = sorted(declared - sample_years)
        extra = sorted(sample_years - declared)
        raise MetricsError(
            "METRICS_MISSING_YEAR" if missing and not extra else "METRICS_EXTRA_YEAR",
            f"sample years mismatch declared: missing={missing} extra={extra}",
        )
    if weighted_years != declared:
        raise MetricsError(
            "METRICS_MISSING_YEAR",
            f"weight years {sorted(weighted_years)} != declared {sorted(declared)}",
        )
    for year, samples in samples_by_year.items():
        if not samples:
            raise MetricsError(
                "METRICS_NO_SAMPLES", f"no samples for weather year {year}"
            )
        for sample in samples:
            _validate_sample(sample)
    values: dict[str, dict[int, list[float]]] = {
        field_name: {} for field_name in _METRIC_FIELDS
    }
    for year, samples in samples_by_year.items():
        for field_name in _METRIC_FIELDS:
            values[field_name][year] = [getattr(s, field_name) for s in samples]
    aggregated: dict[str, float] = {}
    for field_name in _METRIC_FIELDS:
        if field_name == "neue_ppm":
            continue  # recomputed below from aggregated EUE and demand
        aggregated[field_name] = weighted_year_mean(values[field_name], weight_map)
    eue = aggregated["eue_mwh_per_year"]
    demand = aggregated["demand_energy_mwh"]
    if not np.isfinite(demand) or demand <= 0:
        raise MetricsError(
            "METRICS_INVALID_DEMAND",
            "aggregated demand energy must be finite and positive",
        )
    aggregated["neue_ppm"] = 1_000_000.0 * eue / demand
    return AggregateMetrics(
        lolh_hours_per_year=aggregated["lolh_hours_per_year"],
        lole_days_per_year=aggregated["lole_days_per_year"],
        eue_mwh_per_year=eue,
        demand_energy_mwh=demand,
        neue_ppm=aggregated["neue_ppm"],
        generated_weights=generated_weights,
    )


__all__ = [
    "AggregateMetrics",
    "MetricsError",
    "RiskMetricsSample",
    "aggregate_year_metrics",
    "compute_sample_metrics",
    "weighted_year_mean",
]
