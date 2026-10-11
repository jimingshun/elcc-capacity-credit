"""Thermal two-state availability model (T07).

Modes (explicit enum):

- FIXED_UP: fully available, never flips;
- FIXED_DOWN: fully unavailable, never flips;
- SEQUENTIAL: hourly two-state Markov chain with
  p(UP->DOWN) = 1 - exp(-dt/MTTF), p(DOWN->UP) = 1 - exp(-dt/MTTR),
  FOR = MTTR / (MTTF + MTTR);
- INDEPENDENT_HOURLY: per-hour independent Bernoulli with P(up)=1-FOR;
  only allowed for FOR-only input (G1 initial-grade), emits
  THERMAL_DURATION_UNAVAILABLE and cannot support duration claims.

Initial state: steady-state initialization (P(UP)=1-FOR) is the T07
implementation decision, written into the result metadata. Planned
outages use half-open [start, end) intervals and override the
availability fraction inside the window (never double-counting
planned_outage_rate); overlapping events block. Commissioning hour is
half-open (capacity 0 before it) and retirement hour is half-open
(capacity 0 from it onwards).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np

_YEAR_HOURS = 8760

_TOL = 1e-12

# Software numeric tolerance for FOR consistency checks (not a policy
# standard; deliberately named and tested as an implementation constant).
FOR_CONSISTENCY_TOLERANCE = 1e-9


class ThermalMode(Enum):
    FIXED_UP = "fixed_up"
    FIXED_DOWN = "fixed_down"
    SEQUENTIAL = "sequential"
    INDEPENDENT_HOURLY = "independent_hourly"


class ThermalStateError(ValueError):
    """Stable thermal-state error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OutageEvent:
    outage_id: str
    start_hour: int
    end_hour: int
    available_fraction: float
    status: str
    source_id: str


@dataclass(frozen=True)
class ThermalParams:
    mode: ThermalMode
    for_rate: float | None = None
    mttf_hours: float | None = None
    mttr_hours: float | None = None

    def __post_init__(self) -> None:
        """Non-bypassable construction validation (direct constructor too)."""
        if not isinstance(self.mode, ThermalMode):
            raise ThermalStateError("THERMAL_INVALID_PARAMS", "mode must be a ThermalMode")
        if self.for_rate is not None:
            self._validate_for(self.for_rate)
        if self.mttf_hours is not None:
            self._validate_positive(self.mttf_hours, "mttf_hours")
        if self.mttr_hours is not None:
            self._validate_positive(self.mttr_hours, "mttr_hours")
        if self.mode == ThermalMode.FIXED_UP:
            if self.for_rate not in (None, 0.0):
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS", "FIXED_UP requires for_rate 0 or None"
                )
            if self.mttf_hours is not None or self.mttr_hours is not None:
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS",
                    "FIXED_UP must not carry MTTF/MTTR",
                )
        elif self.mode == ThermalMode.FIXED_DOWN:
            if self.for_rate not in (None, 1.0):
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS", "FIXED_DOWN requires for_rate 1 or None"
                )
            if self.mttf_hours is not None or self.mttr_hours is not None:
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS",
                    "FIXED_DOWN must not carry MTTF/MTTR",
                )
        elif self.mode == ThermalMode.INDEPENDENT_HOURLY:
            if self.for_rate is None:
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS",
                    "INDEPENDENT_HOURLY requires for_rate",
                )
            if self.mttf_hours is not None or self.mttr_hours is not None:
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS",
                    "INDEPENDENT_HOURLY must not carry MTTF/MTTR",
                )
        elif self.mode == ThermalMode.SEQUENTIAL:
            if self.mttf_hours is None or self.mttr_hours is None:
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS", "SEQUENTIAL requires MTTF and MTTR"
                )
            if self.for_rate is not None:
                derived = self.mttr_hours / (self.mttf_hours + self.mttr_hours)
                if abs(float(self.for_rate) - derived) > FOR_CONSISTENCY_TOLERANCE:
                    raise ThermalStateError(
                        "THERMAL_FOR_CONSISTENCY",
                        f"for_rate {self.for_rate} inconsistent with "
                        f"derived FOR {derived} (MTTF/MTTR)",
                    )

    @classmethod
    def fixed_up(cls) -> ThermalParams:
        return cls(mode=ThermalMode.FIXED_UP, for_rate=0.0)

    @classmethod
    def fixed_down(cls) -> ThermalParams:
        return cls(mode=ThermalMode.FIXED_DOWN, for_rate=1.0)

    @classmethod
    def independent_hourly(cls, for_rate: float) -> ThermalParams:
        cls._validate_for(for_rate)
        return cls(mode=ThermalMode.INDEPENDENT_HOURLY, for_rate=float(for_rate))

    @classmethod
    def derive_mttf(cls, for_rate: float, mttr_hours: float) -> float:
        """MTTF = MTTR * (1-FOR) / FOR (FOR + MTTR derivation)."""
        cls._validate_for(for_rate)
        cls._validate_positive(mttr_hours, "mttr_hours")
        if for_rate == 0.0:
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS",
                "FOR=0 is a deterministic boundary, derivation is not needed",
            )
        return float(mttr_hours * (1.0 - for_rate) / for_rate)

    @classmethod
    def from_parameters(
        cls,
        *,
        for_rate: float | None,
        mttf_hours: float | None,
        mttr_hours: float | None,
    ) -> ThermalParams:
        """Select mode from the provided parameter combination."""
        if for_rate is not None:
            cls._validate_for(for_rate)
        if mttf_hours is not None:
            cls._validate_positive(mttf_hours, "mttf_hours")
        if mttr_hours is not None:
            cls._validate_positive(mttr_hours, "mttr_hours")

        if for_rate is not None and mttf_hours is None and mttr_hours is None:
            if for_rate == 0.0:
                return cls.fixed_up()
            if for_rate == 1.0:
                return cls.fixed_down()
            return cls(mode=ThermalMode.INDEPENDENT_HOURLY, for_rate=float(for_rate))
        if for_rate is not None and mttr_hours is not None and mttf_hours is None:
            if for_rate == 0.0:
                return cls.fixed_up()
            if for_rate == 1.0:
                return cls.fixed_down()
            mttf = cls.derive_mttf(for_rate, mttr_hours)
            return cls(
                mode=ThermalMode.SEQUENTIAL,
                for_rate=float(for_rate),
                mttf_hours=mttf,
                mttr_hours=float(mttr_hours),
            )
        if mttf_hours is not None and mttr_hours is not None:
            # FOR+MTTF+MTTR all given: keep the input FOR and verify
            # consistency inside __post_init__ (never silently discard)
            return cls(
                mode=ThermalMode.SEQUENTIAL,
                for_rate=float(for_rate) if for_rate is not None else None,
                mttf_hours=float(mttf_hours),
                mttr_hours=float(mttr_hours),
            )
        raise ThermalStateError(
            "THERMAL_INVALID_PARAMS",
            "require FOR, FOR+MTTR, or MTTF+MTTR",
        )

    @staticmethod
    def _is_numeric(value: Any) -> bool:
        return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(
            value, bool
        )

    @staticmethod
    def _validate_for(value: float) -> None:
        if not ThermalParams._is_numeric(value):
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS",
                "FOR must be a real number (bool/str/sequence rejected)",
            )
        if not np.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            raise ThermalStateError("THERMAL_INVALID_PARAMS", "FOR must be finite and in [0, 1]")

    @staticmethod
    def _validate_positive(value: float, field: str) -> None:
        if not ThermalParams._is_numeric(value):
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS",
                f"{field} must be a real number (bool/str/sequence rejected)",
            )
        if not np.isfinite(float(value)) or float(value) <= 0.0:
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS", f"{field} must be finite and positive"
            )

    def steady_state_for(self) -> float:
        """Steady-state forced outage rate for this parameter set."""
        if self.mode in (ThermalMode.FIXED_UP,):
            return 0.0
        if self.mode == ThermalMode.FIXED_DOWN:
            return 1.0
        if self.mode == ThermalMode.INDEPENDENT_HOURLY:
            if self.for_rate is None:
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS",
                    "independent hourly mode requires for_rate",
                )
            return float(self.for_rate)
        if self.mttf_hours is None or self.mttr_hours is None:
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS", "sequential mode requires MTTF and MTTR"
            )
        return float(self.mttr_hours / (self.mttf_hours + self.mttr_hours))

    def transition_matrix(self, delta_t_hours: float = 1.0) -> np.ndarray:
        """Hourly transition matrix [[p_uu, p_ud], [p_du, p_dd]].

        Fixed modes yield the identity (deterministic); independent hourly
        yields per-hour Bernoulli transitions; sequential uses
        p(UP->DOWN)=1-exp(-dt/MTTF), p(DOWN->UP)=1-exp(-dt/MTTR).
        """
        if not np.isfinite(float(delta_t_hours)) or float(delta_t_hours) <= 0.0:
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS", "delta_t_hours must be finite and positive"
            )
        dt = float(delta_t_hours)
        if self.mode == ThermalMode.FIXED_UP:
            return np.array([[1.0, 0.0], [0.0, 1.0]])
        if self.mode == ThermalMode.FIXED_DOWN:
            return np.array([[1.0, 0.0], [0.0, 1.0]])
        if self.mode == ThermalMode.INDEPENDENT_HOURLY:
            if self.for_rate is None:
                raise ThermalStateError(
                    "THERMAL_INVALID_PARAMS",
                    "independent hourly mode requires for_rate",
                )
            q = float(self.for_rate)
            return np.array([[1.0 - q, q], [1.0 - q, q]])
        if self.mttf_hours is None or self.mttr_hours is None:
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS", "sequential mode requires MTTF and MTTR"
            )
        p_ud = 1.0 - np.exp(-dt / float(self.mttf_hours))
        p_du = 1.0 - np.exp(-dt / float(self.mttr_hours))
        return np.array([[1.0 - p_ud, p_ud], [p_du, 1.0 - p_du]])


@dataclass(frozen=True)
class ThermalScenarioResult:
    capacity_fraction: np.ndarray
    mode: ThermalMode
    initial_state: bool
    initial_state_policy: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _validate_n_hours(n_hours: int) -> int:
    if isinstance(n_hours, bool) or not isinstance(n_hours, (int, np.integer)):
        raise ThermalStateError("THERMAL_INVALID_PARAMS", "n_hours must be an integer")
    if n_hours <= 0:
        raise ThermalStateError("THERMAL_INVALID_PARAMS", "n_hours must be positive")
    return int(n_hours)


def _validate_delta_t(delta_t_hours: float) -> float:
    if not np.isfinite(float(delta_t_hours)) or float(delta_t_hours) <= 0.0:
        raise ThermalStateError(
            "THERMAL_INVALID_PARAMS", "delta_t_hours must be finite and positive"
        )
    return float(delta_t_hours)


def _validate_outages(outages: tuple[OutageEvent, ...], n_hours: int) -> tuple[OutageEvent, ...]:
    events = list(outages)
    for event in events:
        if not isinstance(event, OutageEvent):
            raise ThermalStateError(
                "THERMAL_INVALID_INPUT", "planned outages entries must be OutageEvent"
            )
        if (
            not isinstance(event.start_hour, (int, np.integer))
            or not isinstance(event.end_hour, (int, np.integer))
            or event.start_hour < 0
            or event.end_hour > n_hours
            or event.start_hour >= event.end_hour
        ):
            raise ThermalStateError(
                "THERMAL_OUTAGE_OVERLAP",
                "outage window invalid or out of range [0, n_hours)",
            )
        if (
            not np.isfinite(float(event.available_fraction))
            or not 0.0 <= float(event.available_fraction) <= 1.0
        ):
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS",
                "available_fraction must be finite and in [0, 1]",
            )
    for i, a in enumerate(events):
        for b in events[i + 1 :]:
            if a.start_hour < b.end_hour and b.start_hour < a.end_hour:
                raise ThermalStateError(
                    "THERMAL_OUTAGE_OVERLAP",
                    f"overlapping planned outages {a.outage_id} and {b.outage_id}",
                )
    return tuple(events)


def simulate_unit(
    params: ThermalParams,
    n_hours: int,
    generator: Any,
    *,
    initial_state: bool | None = None,
    planned_outages: list[OutageEvent] | tuple[OutageEvent, ...] = (),
    commissioning_hour: int | None = None,
    retirement_hour: int | None = None,
    delta_t_hours: float = 1.0,
) -> ThermalScenarioResult:
    """Simulate one unit's hourly capacity fraction over ``n_hours``.

    Returns a fraction in [0, 1] per hour (1 = fully available, 0 = fully
    unavailable, intermediate = planned-outage derate). The random
    availability state is 0/1; planned outages, commissioning and
    retirement are applied afterwards without mutating any input.
    """
    n = _validate_n_hours(n_hours)
    dt = _validate_delta_t(delta_t_hours)
    outages = _validate_outages(tuple(planned_outages), n)
    if initial_state is not None and not isinstance(initial_state, bool):
        raise ThermalStateError(
            "THERMAL_INVALID_INITIAL_STATE",
            "initial_state must be a bool or None",
        )
    if (
        params.mode
        in (ThermalMode.INDEPENDENT_HOURLY, ThermalMode.FIXED_UP, ThermalMode.FIXED_DOWN)
        and initial_state is not None
    ):
        raise ThermalStateError(
            "THERMAL_INVALID_INITIAL_STATE",
            f"initial_state is only meaningful for SEQUENTIAL mode (mode={params.mode.value})",
        )

    if commissioning_hour is not None:
        if (
            not isinstance(commissioning_hour, (int, np.integer))
            or not 0 <= int(commissioning_hour) < n
        ):
            raise ThermalStateError(
                "THERMAL_INVALID_LIFECYCLE",
                "commissioning_hour must be in [0, n_hours)",
            )
        commissioning = int(commissioning_hour)
    else:
        commissioning = 0
    if retirement_hour is not None:
        if not isinstance(retirement_hour, (int, np.integer)) or not 0 < int(retirement_hour) <= n:
            raise ThermalStateError(
                "THERMAL_INVALID_LIFECYCLE",
                "retirement_hour must be in (0, n_hours]",
            )
        retirement = int(retirement_hour)
    else:
        retirement = n
    if commissioning >= retirement:
        raise ThermalStateError(
            "THERMAL_INVALID_LIFECYCLE",
            "commissioning_hour must be before retirement_hour",
        )

    state = np.zeros(n, dtype=np.int8)
    if params.mode == ThermalMode.FIXED_UP:
        state[:] = 1
    elif params.mode == ThermalMode.FIXED_DOWN:
        state[:] = 0
    elif params.mode == ThermalMode.INDEPENDENT_HOURLY:
        if params.for_rate is None:
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS",
                "independent hourly mode requires for_rate",
            )
        draws = generator.random(n)
        state[:] = (draws > float(params.for_rate)).astype(np.int8)
    elif params.mode == ThermalMode.SEQUENTIAL:
        if params.mttf_hours is None or params.mttr_hours is None:
            raise ThermalStateError(
                "THERMAL_INVALID_PARAMS", "sequential mode requires MTTF and MTTR"
            )
        mttf = float(params.mttf_hours)
        mttr = float(params.mttr_hours)
        p_ud = 1.0 - np.exp(-dt / mttf)
        p_du = 1.0 - np.exp(-dt / mttr)
        if initial_state is None:
            # steady-state initialization (T07 implementation decision)
            start = generator.random() < (1.0 - params.steady_state_for())
        else:
            start = bool(initial_state)
        current = int(start)
        state[0] = current
        draws = generator.random(n - 1)
        for i in range(1, n):
            p = p_ud if current == 1 else p_du
            if draws[i - 1] < p:
                current = 1 - current
            state[i] = current
    else:  # pragma: no cover - defensive
        raise ThermalStateError("THERMAL_INVALID_PARAMS", "unknown mode")

    fraction = state.astype(np.float64)
    for event in outages:
        window = slice(event.start_hour, event.end_hour)
        fraction[window] = float(event.available_fraction)
    if commissioning > 0:
        fraction[:commissioning] = 0.0
    if retirement < n:
        fraction[retirement:] = 0.0

    warnings: list[str] = []
    if params.mode == ThermalMode.INDEPENDENT_HOURLY:
        warnings.append(
            "THERMAL_DURATION_UNAVAILABLE: independent hourly mode cannot "
            "support forced-outage duration conclusions (G1 initial grade)"
        )
    initial = bool(state[0]) if n > 0 else False
    if params.mode == ThermalMode.SEQUENTIAL:
        policy = "explicit" if initial_state is not None else "steady_state"
    elif params.mode == ThermalMode.INDEPENDENT_HOURLY:
        policy = "independent_draw"
    else:
        policy = "deterministic"
    return ThermalScenarioResult(
        capacity_fraction=fraction,
        mode=params.mode,
        initial_state=initial,
        initial_state_policy=policy,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class OutageSchedulingResult:
    """T-08 auto-scheduling audit bundle."""

    unit_id: str
    scheduled: bool
    auto_scheduled: bool
    events: tuple[OutageEvent, ...]
    input_outage_month: int
    input_outage_duration_hours: int
    candidate_windows: tuple[tuple[int, int], ...]
    risk_week_rule: str
    highest_risk_week_start_hour: int
    final_window_start_hour: int | None
    reason: str
    diagnostics: tuple[str, ...]
    model_assumptions: tuple[str, ...]
    deterministic_plan_present: bool
    planned_outage_rate_consistency_only: bool


def _month_start_hour(month: int, year_hours: int = _YEAR_HOURS) -> int:
    """Start hour of a month within a year axis (pure helper). The axis
    length must be 8760 (common year, Feb 28) or 8784 (leap year,
    Feb 29) - anything else is a stable error, never an implicit
    guess."""
    if year_hours not in (8760, 8784):
        raise ThermalStateError(
            "THERMAL_INVALID_AXIS",
            f"year axis must be 8760 or 8784 hours, got {year_hours}",
        )
    month_lengths = (
        31,
        29 if year_hours == 8784 else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return sum(month_lengths[: month - 1]) * 24


def _month_bounds_from_time_axis(
    time_axis: tuple[datetime, ...],
    outage_month: int,
    risk_length: int,
) -> tuple[int, int, int]:
    """C.2: month boundaries are derived from the ACTUAL project
    time_axis (calendar + timezone), never from fixed month lengths.
    The SHARED hourly validator enforces uniqueness, strict increase,
    hourly continuity and a uniform timezone (never a weaker local
    copy)."""
    from capacity_credit.engine.hourly_axis import (
        HourlyAxisError,
        validate_hourly_time_axis,
    )

    try:
        validate_hourly_time_axis(time_axis)
    except HourlyAxisError as error:
        raise ThermalStateError("THERMAL_INVALID_TIME_AXIS", error.message) from None
    years = {ts.year for ts in time_axis}
    if len(years) != 1:
        raise ThermalStateError(
            "THERMAL_MULTI_YEAR_AXIS",
            "a single annual outage scheduling run requires a single-year "
            "time axis; cross-year axes need an explicit year (T16 wiring)",
        )
    if len(time_axis) != risk_length:
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT",
            "time_axis length must equal the risk forecast length",
        )
    month_indexes = [index for index, ts in enumerate(time_axis) if ts.month == outage_month]
    if not month_indexes:
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT",
            f"outage month {outage_month} not present in the time axis",
        )
    month_start = month_indexes[0]
    month_end = len(time_axis)
    for index in range(month_start + 1, len(time_axis)):
        if time_axis[index].month != outage_month:
            month_end = index
            break
    return month_start, month_end, len(time_axis)


def _validate_outage_scheduling_inputs(
    unit_id: str,
    outage_month: int,
    outage_duration_hours: int,
    risk_forecast_by_hour: tuple[float, ...],
) -> None:
    """T-08 input gates (C901 split): strict types, ranges, finite risk."""
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise ThermalStateError("THERMAL_INVALID_INPUT", "unit_id must be a non-empty string")
    if (
        isinstance(outage_month, bool)
        or not isinstance(outage_month, int)
        or not 1 <= outage_month <= 12
    ):
        raise ThermalStateError("THERMAL_INVALID_INPUT", "outage_month must be an integer in 1..12")
    if (
        isinstance(outage_duration_hours, bool)
        or not isinstance(outage_duration_hours, int)
        or outage_duration_hours <= 0
    ):
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT", "outage_duration_hours must be a positive integer"
        )
    if not isinstance(risk_forecast_by_hour, tuple) or not risk_forecast_by_hour:
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT", "risk_forecast_by_hour must be a non-empty tuple"
        )
    for value in risk_forecast_by_hour:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ThermalStateError("THERMAL_INVALID_INPUT", "risk values must be real numbers")
        try:
            number = float(value)
        except (OverflowError, ValueError):
            raise ThermalStateError(
                "THERMAL_INVALID_INPUT", "risk values must be finite real numbers"
            ) from None
        if not math.isfinite(number):
            raise ThermalStateError("THERMAL_INVALID_INPUT", "risk values must be finite")


def _validate_deterministic_outage_events(
    events: object,
    total_hours: int,
) -> tuple[OutageEvent, ...]:
    """C.4: deterministic events entering the scheduler are validated
    with the SAME outage-event contract as simulate_unit (entry type,
    non-empty ids, window bounds, fractions, overlaps) - never a raw
    passthrough."""
    if not isinstance(events, tuple):
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT",
            "deterministic_events must be a tuple of OutageEvent "
            "(an empty tuple means 'no deterministic events')",
        )
    for event in events:
        if not isinstance(event, OutageEvent):
            raise ThermalStateError(
                "THERMAL_INVALID_INPUT",
                "deterministic_events entries must be OutageEvent",
            )
        for name, value in (
            ("outage_id", event.outage_id),
            ("status", event.status),
            ("source_id", event.source_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ThermalStateError(
                    "THERMAL_INVALID_INPUT", f"event {name} must be a non-empty string"
                )
    _validate_outages(tuple(events), total_hours)
    return tuple(events)


def _deterministic_scheduling_result(
    unit_id: str,
    outage_month: int,
    outage_duration_hours: int,
    deterministic_events: tuple[OutageEvent, ...],
) -> OutageSchedulingResult:
    """T-08 passthrough when a deterministic plan exists (C901 split):
    never rescheduled."""
    return OutageSchedulingResult(
        unit_id=unit_id,
        scheduled=False,
        auto_scheduled=False,
        events=tuple(deterministic_events),
        input_outage_month=outage_month,
        input_outage_duration_hours=outage_duration_hours,
        candidate_windows=(),
        risk_week_rule="not_applied_deterministic_plan_present",
        highest_risk_week_start_hour=-1,
        final_window_start_hour=None,
        reason="deterministic planned outages already present; never rescheduled",
        diagnostics=(),
        model_assumptions=(),
        deterministic_plan_present=True,
        planned_outage_rate_consistency_only=True,
    )


def _highest_risk_week(
    risk_forecast_by_hour: tuple[float, ...],
    month_start: int,
    month_end: int,
    total_hours: int,
) -> tuple[int, int]:
    """T-08 risk-week rule (C901 split): year-aligned 168-hour weeks
    intersecting the outage month; mean over the overlap; ties broken
    by the earliest week."""
    week_hours = 7 * 24
    week_scores: list[tuple[float, int]] = []
    for week_start in range(0, total_hours, week_hours):
        week_end = min(total_hours, week_start + week_hours)
        overlap_start = max(week_start, month_start)
        overlap_end = min(week_end, month_end)
        if overlap_end <= overlap_start:
            continue
        window = risk_forecast_by_hour[overlap_start:overlap_end]
        week_scores.append((sum(window) / len(window), week_start))
    highest_week_start = (
        max(week_scores, key=lambda item: (item[0], -item[1]))[1] if week_scores else month_start
    )
    highest_week_end = min(total_hours, highest_week_start + week_hours)
    return highest_week_start, highest_week_end


def schedule_planned_outage(
    *,
    unit_id: str,
    outage_month: int,
    outage_duration_hours: int,
    risk_forecast_by_hour: tuple[float, ...],
    deterministic_events: tuple[OutageEvent, ...] = (),
    time_axis: tuple[datetime, ...] | None = None,
) -> OutageSchedulingResult:
    """T-08 planned-outage auto-scheduling (minimal, testable helper).

    Auto-scheduling runs ONLY when no deterministic hourly
    ``planned_outages`` are present. With a month + duration + risk
    forecast it enumerates every candidate window inside the month
    (half-open [start, end)), identifies the highest-risk week, and
    chooses the earliest window that avoids it; when NO alternative
    window exists the original duration and window constraints are kept
    and a stable warning is emitted (never a silent shortening).
    Generated events carry status="modelled" and a documented
    source_id; the audit records inputs, candidate windows, the
    risk-week rule, scores, the final window and the reason. This is a
    per-unit rule - NOT a province-wide optimal maintenance schedule."""
    _validate_outage_scheduling_inputs(
        unit_id, outage_month, outage_duration_hours, risk_forecast_by_hour
    )
    if not isinstance(deterministic_events, tuple):
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT",
            "deterministic_events must be a tuple (an empty tuple means "
            "'no deterministic events'); None/list are illegal and must "
            "never bypass the type gate",
        )
    if time_axis is not None:
        month_start, month_end, total_hours = _month_bounds_from_time_axis(
            time_axis, outage_month, len(risk_forecast_by_hour)
        )
    else:
        total_hours = len(risk_forecast_by_hour)
        if total_hours not in (8760, 8784):
            raise ThermalStateError(
                "THERMAL_INVALID_AXIS",
                "without an explicit time_axis the risk forecast must span "
                f"8760 or 8784 hours, got {total_hours}",
            )
        month_start = _month_start_hour(outage_month, total_hours)
        month_end = (
            _month_start_hour(outage_month + 1, total_hours) if outage_month < 12 else total_hours
        )
    if deterministic_events:
        _validate_deterministic_outage_events(deterministic_events, total_hours)
        return _deterministic_scheduling_result(
            unit_id, outage_month, outage_duration_hours, deterministic_events
        )
    month_length = month_end - month_start
    if outage_duration_hours > month_length:
        raise ThermalStateError(
            "THERMAL_OUTAGE_NO_ALTERNATIVE",
            "outage duration exceeds the month window",
        )
    candidate_windows = tuple(
        (start, start + outage_duration_hours)
        for start in range(month_start, month_end - outage_duration_hours + 1)
    )
    highest_week_start, highest_week_end = _highest_risk_week(
        risk_forecast_by_hour, month_start, month_end, total_hours
    )
    alternatives = [
        (start, end)
        for start, end in candidate_windows
        if start >= highest_week_end or end <= highest_week_start
    ]
    diagnostics: list[str] = []
    if alternatives:
        start, end = alternatives[0]  # earliest deterministic choice
        reason = (
            "earliest candidate window avoiding the highest-risk week "
            f"[{highest_week_start}, {highest_week_end})"
        )
    else:
        start, end = candidate_windows[0]
        diagnostics.append("THERMAL_OUTAGE_NO_ALTERNATIVE")
        reason = (
            "no feasible alternative window: the original duration and "
            "window constraints are kept (never silently shortened)"
        )
    event = OutageEvent(
        outage_id=f"T08_{unit_id}_{start}_{end}",
        start_hour=start,
        end_hour=end,
        available_fraction=0.0,
        status="modelled",
        source_id="outage_scheduler_v1",
    )
    return OutageSchedulingResult(
        unit_id=unit_id,
        scheduled=True,
        auto_scheduled=True,
        events=(event,),
        input_outage_month=outage_month,
        input_outage_duration_hours=outage_duration_hours,
        candidate_windows=candidate_windows,
        risk_week_rule=(
            "highest mean weekly risk score within the outage month; "
            "ties broken by the earliest week"
        ),
        highest_risk_week_start_hour=highest_week_start,
        final_window_start_hour=start,
        reason=reason,
        diagnostics=tuple(diagnostics),
        model_assumptions=(
            "per-unit heuristic scheduling; not a province-wide optimal maintenance schedule",
            "risk forecast is an external input",
        ),
        deterministic_plan_present=False,
        planned_outage_rate_consistency_only=True,
    )


@dataclass(frozen=True)
class DeratingDecision:
    """DQ-27 double-count decision."""

    apply_rate: bool
    rate: float
    rate_role: str  # "consistency_only" | "derating"


def derive_outage_derating(
    *,
    planned_outage_rate: float | None,
    complete_planned_outages_present: bool,
    hourly_capacity_includes_outages: bool,
    auto_scheduled_events_present: bool = False,
) -> DeratingDecision:
    """DQ-27 double-count protection: the planned_outage_rate is applied
    ONLY when no complete deterministic outage plan and no outage-
    inclusive hourly capacity exist; otherwise it is consistency-only.
    Combining the rate with auto-scheduled events is a double-count
    attempt and blocks with a stable error."""
    for name, flag in (
        ("complete_planned_outages_present", complete_planned_outages_present),
        ("hourly_capacity_includes_outages", hourly_capacity_includes_outages),
        ("auto_scheduled_events_present", auto_scheduled_events_present),
    ):
        if not isinstance(flag, bool):
            raise ThermalStateError("THERMAL_INVALID_INPUT", f"{name} must be a strict bool")
    if planned_outage_rate is None:
        return DeratingDecision(apply_rate=False, rate=0.0, rate_role="consistency_only")
    if isinstance(planned_outage_rate, bool) or not isinstance(planned_outage_rate, (int, float)):
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT", "planned_outage_rate must be a real number"
        )
    try:
        rate = float(planned_outage_rate)
    except (OverflowError, ValueError):
        raise ThermalStateError(
            "THERMAL_INVALID_INPUT", "planned_outage_rate must be a finite real number"
        ) from None
    if not math.isfinite(rate) or not 0.0 <= rate <= 1.0:
        raise ThermalStateError("THERMAL_INVALID_INPUT", "planned_outage_rate must lie in [0, 1]")
    if auto_scheduled_events_present:
        raise ThermalStateError(
            "THERMAL_DOUBLE_COUNT_ATTEMPT",
            "auto-scheduled outage events must never be multiplied by planned_outage_rate again",
        )
    if complete_planned_outages_present or hourly_capacity_includes_outages:
        return DeratingDecision(apply_rate=False, rate=rate, rate_role="consistency_only")
    return DeratingDecision(apply_rate=True, rate=rate, rate_role="derating")


__all__ = [
    "DeratingDecision",
    "OutageEvent",
    "OutageSchedulingResult",
    "ThermalMode",
    "ThermalParams",
    "ThermalScenarioResult",
    "ThermalStateError",
    "derive_outage_derating",
    "schedule_planned_outage",
    "simulate_unit",
]
