"""Strict internal hour-start time axis (T06).

The kernel only accepts standardized hour-start data: timezone-aware,
unique, strictly consecutive hourly timestamps forming complete
calendar years (8760/8784) in the project timezone. Local calendar days
drive LOLE-day; ``weather_year`` must match the local timestamp year.
Hour-end conversion is an adapter responsibility and never re-guessed
here.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass

import numpy as np
import pandas as pd

_AXIS_MARKERS = ("MW", "MWh")


class TimeAxisError(ValueError):
    """Stable time-axis error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def expected_hours_for_year(year: int) -> int:
    """8760 for plain years, 8784 for leap years (Gregorian calendar)."""
    return 8784 if calendar.isleap(year) else 8760


def local_date_series(stamps: pd.Series, timezone: str) -> pd.Series:
    """Local calendar dates in the project timezone (never UTC dates)."""
    return pd.Series(stamps.dt.tz_convert(timezone).dt.date.to_numpy())


@dataclass
class HourAxis:
    timestamps: pd.Series
    weather_year: int
    timezone: str
    local_dates: np.ndarray


def build_hour_axis(
    series: pd.Series,
    *,
    timezone: str,
    weather_year: int | None = None,
    other_axes: dict[str, pd.Series] | None = None,
) -> HourAxis:
    """Validate and build the standardized hour axis.

    Raises ``TimeAxisError`` with stable codes: AXIS_EMPTY,
    AXIS_NAIVE_TIMESTAMP, AXIS_NOT_UNIQUE, AXIS_NOT_CONTIGUOUS,
    AXIS_INCOMPLETE_YEAR, AXIS_YEAR_MISMATCH, AXIS_MISALIGNED.
    """
    stamps = pd.to_datetime(series)
    if stamps.empty:
        raise TimeAxisError("AXIS_EMPTY", "hour axis is empty")
    if stamps.dt.tz is None:
        raise TimeAxisError(
            "AXIS_NAIVE_TIMESTAMP",
            "kernel requires timezone-aware hour-start timestamps",
        )
    if stamps.duplicated().any():
        raise TimeAxisError("AXIS_NOT_UNIQUE", "duplicate timestamps in hour axis")
    ordered = stamps.sort_values()
    if not ordered.index.equals(stamps.index):
        raise TimeAxisError(
            "AXIS_NOT_CONTIGUOUS", "timestamps must be strictly increasing"
        )
    diffs = ordered.diff().dropna()
    if (diffs != pd.Timedelta(hours=1)).any():
        raise TimeAxisError(
            "AXIS_NOT_CONTIGUOUS", "hour axis must be strictly consecutive hours"
        )
    local = stamps.dt.tz_convert(timezone)
    local_years = set(local.dt.year.unique())
    if len(local_years) != 1:
        raise TimeAxisError(
            "AXIS_INCOMPLETE_YEAR",
            f"hour axis spans multiple local years: {sorted(local_years)}",
        )
    year = int(local_years.pop())
    expected = expected_hours_for_year(year)
    if len(stamps) != expected:
        raise TimeAxisError(
            "AXIS_INCOMPLETE_YEAR",
            f"year {year} requires {expected} hours, got {len(stamps)}",
        )
    jan1 = pd.Timestamp(year, 1, 1, tz=timezone)
    dec31 = pd.Timestamp(year, 12, 31, 23, 0, tz=timezone)
    if local.min() != jan1 or local.max() != dec31:
        raise TimeAxisError(
            "AXIS_INCOMPLETE_YEAR",
            f"year {year} axis must span {jan1} .. {dec31}",
        )
    if weather_year is not None and weather_year != year:
        raise TimeAxisError(
            "AXIS_YEAR_MISMATCH",
            f"declared weather_year {weather_year} != local year {year}",
        )
    if other_axes:
        base_utc = ordered.dt.tz_convert("UTC").reset_index(drop=True)
        for name, other in other_axes.items():
            other_stamps = pd.to_datetime(other)
            if other_stamps.empty:
                raise TimeAxisError("AXIS_EMPTY", f"other axis {name} is empty")
            if other_stamps.dt.tz is None:
                raise TimeAxisError(
                    "AXIS_NAIVE_TIMESTAMP", f"other axis {name} must be timezone-aware"
                )
            if pd.isna(other_stamps).any():
                raise TimeAxisError(
                    "AXIS_INVALID_TIMESTAMP", f"other axis {name} contains NaT"
                )
            if other_stamps.duplicated().any():
                raise TimeAxisError(
                    "AXIS_NOT_UNIQUE", f"other axis {name} has duplicate timestamps"
                )
            other_utc = (
                other_stamps.dt.tz_convert("UTC").sort_values().reset_index(drop=True)
            )
            if other_utc.size != base_utc.size or not other_utc.equals(base_utc):
                raise TimeAxisError(
                    "AXIS_MISALIGNED",
                    f"hour set of {name} differs from the primary axis",
                )
    return HourAxis(
        timestamps=stamps.reset_index(drop=True),
        weather_year=year,
        timezone=timezone,
        local_dates=local_date_series(stamps, timezone).to_numpy(),
    )


__all__ = ["HourAxis", "TimeAxisError", "build_hour_axis", "expected_hours_for_year", "local_date_series"]
