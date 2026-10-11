"""Shared strict hourly time-axis validator (T15 closure).

One set of rules reused by hydro / thermal / intertie call sites
(never a second, weaker copy):

- non-empty tuple;
- every entry is a datetime;
- every entry shares the SAME timezone (naive counts as one uniform
  value; mixing naive/aware or two different offsets blocks);
- strictly increasing (no duplicates, no reordering);
- adjacent entries exactly one hour apart (no gaps, no sub-/super-hour
  steps).
"""

from __future__ import annotations

from datetime import datetime, timedelta

_ONE_HOUR = timedelta(hours=1)


class HourlyAxisError(ValueError):
    """Stable hourly-axis error; callers translate it into their own
    domain error classes."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_hourly_time_axis(time_axis: tuple[datetime, ...]) -> tuple[datetime, ...]:
    if not isinstance(time_axis, tuple) or not time_axis:
        raise HourlyAxisError("time axis must be a non-empty tuple")
    for ts in time_axis:
        if not isinstance(ts, datetime):
            raise HourlyAxisError("time axis entries must be datetimes")
    tzinfo = None
    tz_seen = False
    previous: datetime | None = None
    for ts in time_axis:
        if not tz_seen:
            tzinfo = ts.tzinfo
            tz_seen = True
        elif ts.tzinfo != tzinfo:
            raise HourlyAxisError("all time-axis entries must share the same timezone")
        if previous is not None:
            if ts <= previous:
                raise HourlyAxisError(
                    "time axis must be strictly increasing (no duplicates, no reordering)"
                )
            if ts - previous != _ONE_HOUR:
                raise HourlyAxisError(
                    "time axis must be hourly continuous (adjacent entries exactly one hour apart)"
                )
        previous = ts
    return time_axis
