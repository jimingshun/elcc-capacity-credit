"""Convergence configuration and criteria (T08).

All thresholds are explicit configuration values flagged as provisional
defaults for the method-validation phase - never policy or industry
standards. Risk convergence requires ALL FOUR criteria simultaneously:

1. sample_count >= min_samples;
2. primary 95% interval half width within relative tolerance (or the
   absolute tolerance when the mean is near zero);
3. nonzero-risk sample count >= min_nonzero_samples;
4. recent batch means are stable within tolerance.

Status fields are strictly separated: risk_convergence
(converged | not_converged | low_event_count), result_status
(valid | valid_with_warning | low_resolution | not_converged) and
search_convergence (always not_applicable for adequacy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from capacity_credit.contracts.enums import RiskMetric
from capacity_credit.engine.online_stats import OnlineStats, confidence_interval

_ALLOWED_PRIMARY_METRICS = {m.value for m in RiskMetric}


class ConvergenceError(ValueError):
    """Stable convergence error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ConvergenceConfig:
    batch_size: int
    min_samples: int
    max_samples: int
    confidence_level: float
    relative_half_width_tolerance: float
    absolute_half_width_tolerance: float
    near_zero_threshold: float
    min_nonzero_samples: int
    recent_batch_window: int
    recent_batch_stability_tolerance: float
    use_student_t: bool = True
    normal_interval_min_samples: int = 200
    primary_risk_metric: str = RiskMetric.LOLE_DAYS_PER_YEAR.value
    provisional_defaults: bool = True

    def __post_init__(self) -> None:
        for field_name, value in (
            ("batch_size", self.batch_size),
            ("min_samples", self.min_samples),
            ("max_samples", self.max_samples),
            ("min_nonzero_samples", self.min_nonzero_samples),
            ("recent_batch_window", self.recent_batch_window),
            ("normal_interval_min_samples", self.normal_interval_min_samples),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, np.integer)) \
                    or int(value) <= 0:
                raise ConvergenceError(
                    "CONVERGENCE_INVALID_CONFIG", f"{field_name} must be a positive integer"
                )
        if self.min_samples < 2:
            raise ConvergenceError(
                "CONVERGENCE_INVALID_CONFIG",
                "min_samples must be at least 2 (confidence intervals "
                "require two or more samples)",
            )
        if self.max_samples < self.min_samples:
            raise ConvergenceError(
                "CONVERGENCE_INVALID_CONFIG", "max_samples < min_samples"
            )
        if not isinstance(self.primary_risk_metric, str) \
                or self.primary_risk_metric not in _ALLOWED_PRIMARY_METRICS:
            raise ConvergenceError(
                "CONVERGENCE_INVALID_CONFIG",
                f"primary_risk_metric {self.primary_risk_metric!r} unsupported; "
                f"allowed: {sorted(_ALLOWED_PRIMARY_METRICS)}",
            )
        if not isinstance(self.use_student_t, bool):
            raise ConvergenceError(
                "CONVERGENCE_INVALID_CONFIG", "use_student_t must be a boolean"
            )
        for field_name, numeric_value in (
            ("confidence_level", self.confidence_level),
            ("relative_half_width_tolerance", self.relative_half_width_tolerance),
            ("absolute_half_width_tolerance", self.absolute_half_width_tolerance),
            ("near_zero_threshold", self.near_zero_threshold),
            ("recent_batch_stability_tolerance",
             self.recent_batch_stability_tolerance),
        ):
            if isinstance(numeric_value, bool) \
                    or not isinstance(numeric_value,
                                      (int, float, np.integer, np.floating)) \
                    or not np.isfinite(float(numeric_value)):
                raise ConvergenceError(
                    "CONVERGENCE_INVALID_CONFIG", f"{field_name} must be finite"
                )
        if not 0.0 < float(self.confidence_level) < 1.0:
            raise ConvergenceError(
                "CONVERGENCE_INVALID_CONFIG", "confidence_level must be in (0, 1)"
            )
        if float(self.relative_half_width_tolerance) < 0.0 \
                or float(self.absolute_half_width_tolerance) < 0.0 \
                or float(self.near_zero_threshold) < 0.0 \
                or float(self.recent_batch_stability_tolerance) < 0.0:
            raise ConvergenceError(
                "CONVERGENCE_INVALID_CONFIG", "tolerances must be non-negative"
            )

    def to_snapshot(self) -> dict[str, Any]:
        """Canonical config snapshot used for checkpoint consistency."""
        return {
            "batch_size": self.batch_size,
            "min_samples": self.min_samples,
            "max_samples": self.max_samples,
            "confidence_level": float(self.confidence_level),
            "relative_half_width_tolerance": float(
                self.relative_half_width_tolerance
            ),
            "absolute_half_width_tolerance": float(
                self.absolute_half_width_tolerance
            ),
            "near_zero_threshold": float(self.near_zero_threshold),
            "min_nonzero_samples": self.min_nonzero_samples,
            "recent_batch_window": self.recent_batch_window,
            "recent_batch_stability_tolerance": float(
                self.recent_batch_stability_tolerance
            ),
            "use_student_t": self.use_student_t,
            "normal_interval_min_samples": self.normal_interval_min_samples,
            "primary_risk_metric": self.primary_risk_metric,
        }


def default_config() -> ConvergenceConfig:
    """Provisional defaults for the method-validation phase (not policy)."""
    return ConvergenceConfig(
        batch_size=100,
        min_samples=2000,
        max_samples=100000,
        confidence_level=0.95,
        relative_half_width_tolerance=0.05,
        absolute_half_width_tolerance=0.001,
        near_zero_threshold=0.05,
        min_nonzero_samples=100,
        recent_batch_window=5,
        recent_batch_stability_tolerance=0.1,
        use_student_t=True,
        normal_interval_min_samples=200,
        provisional_defaults=True,
    )


@dataclass(frozen=True)
class ConvergenceState:
    risk_convergence: str
    result_status: str
    search_convergence: str = "not_applicable"
    should_stop: bool = False
    criteria: dict[str, bool] = None  # type: ignore[assignment]


def _half_width_satisfied(
    stats: OnlineStats, config: ConvergenceConfig
) -> bool:
    interval = confidence_interval(
        stats,
        confidence_level=config.confidence_level,
        use_student_t=config.use_student_t,
        normal_interval_min_samples=config.normal_interval_min_samples,
    )
    mean = stats.mean
    if abs(mean) <= config.near_zero_threshold:
        return interval.half_width <= config.absolute_half_width_tolerance
    return interval.half_width <= config.relative_half_width_tolerance * abs(mean)


def _recent_window_stable(
    recent_window: list[float], config: ConvergenceConfig
) -> bool:
    if len(recent_window) < 2:
        return True
    values = np.asarray(recent_window, dtype=float)
    span = float(np.max(values) - np.min(values))
    mean = float(np.mean(values))
    if abs(mean) <= config.near_zero_threshold:
        return span <= config.absolute_half_width_tolerance
    return span <= config.recent_batch_stability_tolerance * abs(mean)


def check_convergence(
    stats: OnlineStats,
    recent_window: list[float],
    config: ConvergenceConfig,
) -> ConvergenceState:
    """Evaluate convergence with an explicit state machine.

    Order: recent window not full -> never converges; then the four
    criteria; all four satisfied -> converged (even at max_samples);
    otherwise max_samples reached -> final not_converged /
    low_event_count; below max_samples -> not_converged and continue.
    ``should_stop`` and ``criteria`` are explicit so callers never guess
    from strings whether to continue.
    """
    if len(recent_window) < config.recent_batch_window:
        return ConvergenceState(
            risk_convergence="not_converged",
            result_status="not_converged",
            should_stop=False,
            criteria={
                "min_samples_met": stats.sample_count >= config.min_samples,
                "precision_met": False,
                "nonzero_samples_met": (
                    stats.nonzero_sample_count >= config.min_nonzero_samples
                ),
                "recent_batches_stable": False,
            },
        )
    min_samples_met = stats.sample_count >= config.min_samples
    nonzero_samples_met = (
        stats.nonzero_sample_count >= config.min_nonzero_samples
    )
    precision_met = _half_width_satisfied(stats, config)
    recent_batches_stable = _recent_window_stable(recent_window, config)
    criteria = {
        "min_samples_met": min_samples_met,
        "precision_met": precision_met,
        "nonzero_samples_met": nonzero_samples_met,
        "recent_batches_stable": recent_batches_stable,
    }
    if not min_samples_met:
        # below min_samples the run never forms a final low_resolution;
        # event shortage is recorded in criteria only
        return ConvergenceState(
            risk_convergence="not_converged",
            result_status="not_converged",
            should_stop=False,
            criteria=criteria,
        )
    if not nonzero_samples_met:
        if stats.sample_count >= config.max_samples:
            return ConvergenceState(
                risk_convergence="low_event_count",
                result_status="low_resolution",
                should_stop=True,
                criteria=criteria,
            )
        return ConvergenceState(
            risk_convergence="not_converged",
            result_status="not_converged",
            should_stop=False,
            criteria=criteria,
        )
    if precision_met and recent_batches_stable:
        return ConvergenceState(
            risk_convergence="converged",
            result_status="valid",
            should_stop=True,
            criteria=criteria,
        )
    if stats.sample_count >= config.max_samples:
        return ConvergenceState(
            risk_convergence="not_converged",
            result_status="not_converged",
            should_stop=True,
            criteria=criteria,
        )
    return ConvergenceState(
        risk_convergence="not_converged",
        result_status="not_converged",
        should_stop=False,
        criteria=criteria,
    )


__all__ = [
    "ConvergenceConfig",
    "ConvergenceError",
    "ConvergenceState",
    "check_convergence",
    "default_config",
]
