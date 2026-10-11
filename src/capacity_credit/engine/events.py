"""Deficit events (T06).

Consecutive deficit hours (unserved power above the tolerance) form one
event with explicit interval semantics:

- ``start_time``: timestamp of the first deficit hour;
- ``end_time`` = ``start_time`` + duration (half-open interval
  [start, start + duration));
- ``event_eue_mwh`` and ``max_unserved_power_mw`` are recomputable from
  the hourly balance result.

Year handling is based on local calendar years (derived from the
provided local dates), never on whether the calendar date changed:
consecutive hours across midnight of the same year stay one event,
while cross-year deficits are split by default. Merging is only allowed
when explicitly enabled (``merge_across_years=True``), and the merged
event carries an unambiguous ``year_range`` instead of a fake single
caller-supplied year. Each event derives its own ``weather_year`` from
its own timestamps.

Per-event context (pre-event SOC, renewable output, outage capacity,
intertie imports) is extracted from the caller-provided hourly
``EventContext`` (arrays aligned with the balance result) or used
directly when scalar event-level values are given; full annual arrays
are never attached to individual events, and nothing is fabricated when
the caller provides no context.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from capacity_credit.engine.balance import BalanceResult


class EventError(ValueError):
    """Stable event error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EventContext:
    """Hourly context arrays (aligned with the balance result) or scalars.

    Array fields must have the same length as the balance result; scalar
    fields are treated as event-level snapshots used directly.
    """

    pre_event_soc_mwh: np.ndarray | float | None = None
    renewable_output_mw: np.ndarray | float | None = None
    outage_capacity_mw: np.ndarray | float | None = None
    intertie_import_mw: np.ndarray | float | None = None
    hydro_output_mw: np.ndarray | float | None = None


@dataclass(frozen=True)
class EventContextSnapshot:
    """Per-event values extracted from the hourly context.

    ``pre_event_soc_mwh`` is the SOC at the event start hour; the mean
    fields are the window means over the event hours - all recomputable
    from the hourly input.
    """

    pre_event_soc_mwh: float | None
    mean_renewable_output_mw: float | None
    mean_outage_capacity_mw: float | None
    mean_intertie_import_mw: float | None
    mean_hydro_output_mw: float | None


@dataclass(frozen=True)
class DeficitEvent:
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    duration_hours: int
    event_eue_mwh: float
    max_unserved_power_mw: float
    weather_year: int
    year_range: tuple[int, int]
    crosses_year_boundary: bool
    sample_id: str
    run_id: str
    context_values: EventContextSnapshot | None


def _resolve_tolerance(balance: BalanceResult, tolerance: float | None) -> float:
    if tolerance is None:
        return balance.tolerance
    tolerance = float(tolerance)
    if tolerance != balance.tolerance:
        raise EventError(
            "EVENT_TOLERANCE_MISMATCH",
            f"tolerance {tolerance} differs from balance tolerance {balance.tolerance}",
        )
    return tolerance


def _extract_context(
    context: EventContext | None,
    start: int,
    end: int,
    length: int,
) -> EventContextSnapshot | None:
    if context is None:
        return None

    def extract(field: np.ndarray | float | None) -> float | None:
        if field is None:
            return None
        if np.isscalar(field):
            return float(np.asarray(field, dtype=np.float64))
        values = np.asarray(field)
        if values.ndim != 1 or values.size != length:
            raise EventError(
                "EVENT_LENGTH_MISMATCH",
                "hourly context arrays must match the balance length",
            )
        return float(np.asarray(values[start], dtype=np.float64))

    def window_mean(field: np.ndarray | float | None) -> float | None:
        if field is None:
            return None
        if np.isscalar(field):
            return float(np.asarray(field, dtype=np.float64))
        values = np.asarray(field)
        if values.ndim != 1 or values.size != length:
            raise EventError(
                "EVENT_LENGTH_MISMATCH",
                "hourly context arrays must match the balance length",
            )
        return float(values[start : end + 1].mean())

    return EventContextSnapshot(
        pre_event_soc_mwh=extract(context.pre_event_soc_mwh),
        mean_renewable_output_mw=window_mean(context.renewable_output_mw),
        mean_outage_capacity_mw=window_mean(context.outage_capacity_mw),
        mean_intertie_import_mw=window_mean(context.intertie_import_mw),
        mean_hydro_output_mw=window_mean(context.hydro_output_mw),
    )


def detect_deficit_events(
    balance: BalanceResult,
    local_dates: np.ndarray,
    *,
    axis: pd.Series,
    sample_id: str,
    run_id: str,
    tolerance: float | None = None,
    merge_across_years: bool = False,
    context: EventContext | None = None,
) -> list[DeficitEvent]:
    """Merge consecutive deficit hours into events.

    ``axis`` carries the hour-start timestamps aligned with the balance
    result; ``local_dates`` carries the local calendar date per hour.
    Each event's ``weather_year`` is derived from its own start hour;
    cross-year splits and merges are based on local years.
    """
    deficit = balance.unserved_power_mw > _resolve_tolerance(balance, tolerance)
    n = len(deficit)
    if axis.size != n or len(local_dates) != n:
        raise EventError(
            "EVENT_LENGTH_MISMATCH",
            "axis, local_dates and balance must have equal length",
        )
    stamps = pd.to_datetime(axis).reset_index(drop=True)
    dates = np.asarray(local_dates)
    years = np.asarray([d.year for d in dates], dtype=int)

    events: list[DeficitEvent] = []
    i = 0
    while i < n:
        if not deficit[i]:
            i += 1
            continue
        start = i
        end = i
        while end + 1 < n and deficit[end + 1]:
            if not merge_across_years and years[end + 1] != years[end]:
                break
            end += 1
        duration = end - start + 1
        start_time = stamps.iloc[start]
        end_time = start_time + pd.Timedelta(hours=duration)
        first_year = int(years[start])
        last_year = int(years[end])
        events.append(
            DeficitEvent(
                start_time=start_time,
                end_time=end_time,
                duration_hours=duration,
                event_eue_mwh=float(balance.unserved_energy_mwh[start : end + 1].sum()),
                max_unserved_power_mw=float(balance.unserved_power_mw[start : end + 1].max()),
                weather_year=first_year,
                year_range=(first_year, last_year),
                crosses_year_boundary=last_year != first_year,
                sample_id=sample_id,
                run_id=run_id,
                context_values=_extract_context(context, start, end, n),
            )
        )
        i = end + 1
    return events


__all__ = [
    "DeficitEvent",
    "EventContext",
    "EventContextSnapshot",
    "EventError",
    "detect_deficit_events",
]
