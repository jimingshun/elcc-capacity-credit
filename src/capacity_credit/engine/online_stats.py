"""Serializable, mergeable online statistics (T08).

Welford/Chan moments with:

- sample_count, mean, M2, sample variance (n-1 denominator),
  standard error, nonzero_sample_count;
- single-value, array and batch updates;
- merge of two stats states (Chan combination);
- strict serialization/deserialization;
- explicit empty/single-sample behaviour:
  - empty: variance and standard error are NaN;
  - single sample: variance = 0.0, standard error NaN;
- bool, str, NaN, +-inf and other non-finite values are rejected.

Weather years are kept stratified by the caller (adequacy engine):
combined estimates always use per-year means weighted by q_w, never a
pooled unweighted mean of raw observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

_VARIANCE_MIN_SAMPLES = 2


class StatError(ValueError):
    """Stable online-statistics error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise StatError("STAT_INVALID_VALUE", "value must be a real number")
    number = float(value)
    if not np.isfinite(number):
        raise StatError("STAT_INVALID_VALUE", "value must be finite")
    return number


@dataclass
class OnlineStats:
    sample_count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    nonzero_sample_count: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.sample_count, bool) \
                or not isinstance(self.sample_count, (int, np.integer)) \
                or self.sample_count < 0:
            raise StatError("STAT_INVALID_STATE", "sample_count invalid")
        if isinstance(self.nonzero_sample_count, bool) \
                or not isinstance(self.nonzero_sample_count, (int, np.integer)) \
                or self.nonzero_sample_count < 0 \
                or self.nonzero_sample_count > self.sample_count:
            raise StatError("STAT_INVALID_STATE", "nonzero_sample_count invalid")
        if not np.isfinite(float(self.mean)) or not np.isfinite(float(self.m2)):
            raise StatError("STAT_INVALID_STATE", "mean/m2 must be finite")
        if self.m2 < 0.0:
            raise StatError("STAT_INVALID_STATE", "m2 must be non-negative")
        if self.sample_count == 0 and (self.mean != 0.0 or self.m2 != 0.0):
            raise StatError(
                "STAT_INVALID_STATE", "empty state must have canonical mean=0/m2=0"
            )
        if self.sample_count == 1 and self.m2 != 0.0:
            raise StatError(
                "STAT_INVALID_STATE", "single-sample state must have m2=0"
            )

    @property
    def sample_variance(self) -> float:
        if self.sample_count < _VARIANCE_MIN_SAMPLES:
            if self.sample_count == 1:
                return 0.0
            return float("nan")
        return self.m2 / (self.sample_count - 1)

    @property
    def standard_error(self) -> float:
        if self.sample_count < _VARIANCE_MIN_SAMPLES:
            return float("nan")
        return float(np.sqrt(self.sample_variance / self.sample_count))

    def update(self, value: Any) -> None:
        number = _validate_value(value)
        self.sample_count += 1
        delta = number - self.mean
        self.mean += delta / self.sample_count
        delta2 = number - self.mean
        self.m2 += delta * delta2
        if number > 0.0:
            self.nonzero_sample_count += 1

    def update_batch(self, values: Any) -> None:
        try:
            array = np.asarray(values)
        except (TypeError, ValueError) as exc:
            raise StatError(
                "STAT_INVALID_VALUE", "batch must be a numeric sequence"
            ) from exc
        if array.ndim != 1:
            raise StatError(
                "STAT_INVALID_VALUE", "batch must be a 1-D numeric sequence"
            )
        if array.size == 0:
            raise StatError("STAT_INVALID_VALUE", "batch must not be empty")
        if not np.issubdtype(array.dtype, np.number):
            raise StatError(
                "STAT_INVALID_VALUE", "batch must contain numeric values"
            )
        for value in array:
            self.update(value)

    def merge(self, other: OnlineStats) -> None:
        if not isinstance(other, OnlineStats):
            raise StatError("STAT_INVALID_STATE", "merge requires OnlineStats")
        count = self.sample_count + other.sample_count
        if count == 0:
            return
        delta = other.mean - self.mean
        merged_m2 = (
            self.m2
            + other.m2
            + delta * delta * self.sample_count * other.sample_count / count
        )
        self.mean = (
            self.sample_count * self.mean + other.sample_count * other.mean
        ) / count
        self.m2 = merged_m2
        self.sample_count = count
        self.nonzero_sample_count += other.nonzero_sample_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "mean": self.mean,
            "m2": self.m2,
            "nonzero_sample_count": self.nonzero_sample_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OnlineStats:
        if not isinstance(payload, dict):
            raise StatError("STAT_INVALID_STATE", "state must be a mapping")
        allowed = {"sample_count", "mean", "m2", "nonzero_sample_count"}
        if set(payload.keys()) != allowed:
            raise StatError(
                "STAT_INVALID_STATE",
                f"state fields must be exactly {sorted(allowed)}",
            )
        sample_count = payload["sample_count"]
        nonzero = payload["nonzero_sample_count"]
        if isinstance(sample_count, bool) \
                or not isinstance(sample_count, (int, np.integer)) \
                or sample_count < 0:
            raise StatError("STAT_INVALID_STATE", "sample_count invalid")
        if isinstance(nonzero, bool) \
                or not isinstance(nonzero, (int, np.integer)) \
                or nonzero < 0 or nonzero > int(sample_count):
            raise StatError("STAT_INVALID_STATE", "nonzero_sample_count invalid")
        try:
            mean = _validate_value(payload["mean"])
            m2 = _validate_value(payload["m2"])
        except StatError as exc:
            raise StatError("STAT_INVALID_STATE", "mean/m2 must be finite numbers"
                            ) from exc
        if m2 < 0.0:
            raise StatError("STAT_INVALID_STATE", "m2 must be non-negative")
        count = int(sample_count)
        if count == 0 and (mean != 0.0 or m2 != 0.0):
            raise StatError(
                "STAT_INVALID_STATE",
                "empty state must have canonical mean=0 and m2=0",
            )
        if count == 1 and m2 != 0.0:
            raise StatError(
                "STAT_INVALID_STATE", "single-sample state must have m2=0"
            )
        return cls(
            sample_count=count,
            mean=mean,
            m2=m2,
            nonzero_sample_count=int(nonzero),
        )


def merge_stats(left: OnlineStats, right: OnlineStats) -> OnlineStats:
    """Merge two stats states into a new state (Chan combination)."""
    result = OnlineStats(
        sample_count=left.sample_count,
        mean=left.mean,
        m2=left.m2,
        nonzero_sample_count=left.nonzero_sample_count,
    )
    result.merge(right)
    return result


@dataclass(frozen=True)
class IntervalResult:
    lower: float
    upper: float
    half_width: float
    confidence_level: float
    method: str  # "student_t" | "normal"


def confidence_interval(
    stats: OnlineStats,
    *,
    confidence_level: float = 0.95,
    use_student_t: bool = True,
    normal_interval_min_samples: int = 200,
) -> IntervalResult:
    """Confidence interval for the mean.

    ``use_student_t`` selects Student-t for small samples and the normal
    approximation once ``sample_count >= normal_interval_min_samples``;
    both switches are explicit parameters (never hard-coded policy).
    """
    if not isinstance(confidence_level, (int, float)) \
            or isinstance(confidence_level, bool) \
            or not 0.0 < float(confidence_level) < 1.0:
        raise StatError("STAT_INVALID_STATE", "confidence_level must be in (0, 1)")
    if stats.sample_count < _VARIANCE_MIN_SAMPLES:
        raise StatError(
            "STAT_INVALID_STATE", "at least 2 samples are required for an interval"
        )
    from scipy import stats as sp_stats

    n = stats.sample_count
    alpha = 1.0 - float(confidence_level)
    if use_student_t and n < normal_interval_min_samples:
        critical = float(sp_stats.t.ppf(1.0 - alpha / 2.0, df=n - 1))
        method = "student_t"
    else:
        critical = float(sp_stats.norm.ppf(1.0 - alpha / 2.0))
        method = "normal"
    half_width = critical * stats.standard_error
    return IntervalResult(
        lower=stats.mean - half_width,
        upper=stats.mean + half_width,
        half_width=float(half_width),
        confidence_level=float(confidence_level),
        method=method,
    )


__all__ = [
    "IntervalResult",
    "OnlineStats",
    "StatError",
    "confidence_interval",
    "merge_stats",
]
