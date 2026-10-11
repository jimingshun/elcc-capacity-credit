"""T15 hydro fixed profiles and power / energy-budget constraints.

Scope (methodology V0.4 §5.3, data contract V0.3 §16/§17):

- ``build_hydro_fixed_profile``: exogenous hourly fixed output aligned
  to the project time axis;
- ``build_monthly_fixed_profile``: capacity x monthly factor -> a
  monthly fixed-average profile tagged ``modelled_fixed_profile``
  (explicitly NOT reservoir optimal dispatch); the month mapping uses
  the PROJECT time axis - February of a leap year correctly carries
  29 x 24 hours (never a fixed 720-hours-per-month assumption);
- ``check_hydro_constraints``: hourly 0 <= adjustable <= max_output and
  total = fixed + adjustable <= max_output; adjustable energy budgets
  (monthly / annual / arbitrary) with explicit LEFT-CLOSED RIGHT-OPEN
  periods and delta_t energy accounting;
- zero adjustable budget zeroes ONLY the adjustable part - the fixed
  must-run part uses an INDEPENDENT field and is never cleared;
- resource association uses ONLY ``hydro_id`` (``hydro_name`` is
  display-only; similar names never merge - ambiguous name matches
  return candidates, never a silent association);
- strict numeric gates (bool / NaN / +-inf / negatives / length
  mismatches / illegal delta_t all block with stable codes); inputs are
  never mutated and identical inputs produce identical results.

T15 does NOT implement reservoir optimal dispatch: adjustable power
comes from a caller-provided request trajectory which is constrained /
validated here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_MONTH_COUNT = 12


class HydroError(ValueError):
    """Stable hydro-model error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_float(value: Any, label: str) -> float:
    """float() wrapper that never leaks OverflowError (10**400 etc.)."""
    try:
        return float(value)
    except (OverflowError, ValueError):
        raise HydroError("HYDRO_INVALID_INPUT", f"{label} must be a finite real number") from None


def _strict_nonneg(value: object, label: str) -> float:
    """Strict non-negative finite number gate (bool / NaN / +-inf /
    negatives / overflow rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HydroError("HYDRO_INVALID_INPUT", f"{label} must be a real number")
    number = _safe_float(value, label)
    if not math.isfinite(number):
        raise HydroError("HYDRO_INVALID_INPUT", f"{label} must be finite")
    if number < 0.0:
        raise HydroError("HYDRO_INVALID_INPUT", f"{label} must be non-negative")
    return number


def _strict_positive(value: object, label: str) -> float:
    number = _strict_nonneg(value, label)
    if number <= 0.0:
        raise HydroError("HYDRO_INVALID_INPUT", f"{label} must be positive")
    return number


def _validate_time_axis(time_axis: tuple[datetime, ...]) -> tuple[datetime, ...]:
    """Project time axis gates: the SHARED hourly validator (unique,
    strictly increasing, hourly continuous, uniform timezone) - never a
    weaker local copy."""
    from capacity_credit.engine.hourly_axis import (
        HourlyAxisError,
        validate_hourly_time_axis,
    )

    try:
        return validate_hourly_time_axis(time_axis)
    except HourlyAxisError as error:
        raise HydroError("HYDRO_INVALID_TIME_AXIS", error.message) from None


@dataclass(frozen=True)
class HydroFixedProfile:
    """Fixed (non-adjustable) hydro output aligned to the project time
    axis."""

    hydro_id: str
    fixed_output_mw: tuple[float, ...]
    mode: str  # "exogenous_hourly" | "modelled_fixed_profile"


def build_hydro_fixed_profile(
    *,
    hydro_id: str,
    fixed_output_mw: tuple[float, ...],
    time_axis: tuple[datetime, ...],
) -> HydroFixedProfile:
    """Exogenous hourly fixed output (data contract §17.1
    fixed_output_mw)."""
    _validate_time_axis(time_axis)
    if not isinstance(hydro_id, str) or not hydro_id.strip():
        raise HydroError("HYDRO_INVALID_INPUT", "hydro_id must be a non-empty string")
    if not isinstance(fixed_output_mw, tuple):
        raise HydroError("HYDRO_INVALID_INPUT", "fixed_output_mw must be a tuple")
    if len(fixed_output_mw) != len(time_axis):
        raise HydroError(
            "HYDRO_INVALID_INPUT",
            "fixed_output_mw length must equal the time axis length",
        )
    values = tuple(_strict_nonneg(value, "fixed_output_mw") for value in fixed_output_mw)
    return HydroFixedProfile(
        hydro_id=hydro_id.strip(), fixed_output_mw=values, mode="exogenous_hourly"
    )


def build_monthly_fixed_profile(
    *,
    hydro_id: str,
    capacity_mw: float,
    monthly_factors: tuple[float, ...],
    time_axis: tuple[datetime, ...],
) -> HydroFixedProfile:
    """Capacity x monthly factor -> monthly fixed-average profile,
    tagged ``modelled_fixed_profile`` (data contract §17.3; NOT
    reservoir optimal dispatch). Months map through the PROJECT time
    axis (leap-year Feb 29 covered; no 720-hours-per-month
    assumption)."""
    _validate_time_axis(time_axis)
    if not isinstance(hydro_id, str) or not hydro_id.strip():
        raise HydroError("HYDRO_INVALID_INPUT", "hydro_id must be a non-empty string")
    capacity = _strict_positive(capacity_mw, "capacity_mw")
    if not isinstance(monthly_factors, tuple) or len(monthly_factors) != _MONTH_COUNT:
        raise HydroError("HYDRO_INVALID_INPUT", "monthly_factors must be a 12-tuple")
    factors = tuple(_strict_nonneg(value, "monthly_factors") for value in monthly_factors)
    values = tuple(capacity * factors[ts.month - 1] for ts in time_axis)
    return HydroFixedProfile(
        hydro_id=hydro_id.strip(),
        fixed_output_mw=values,
        mode="modelled_fixed_profile",
    )


@dataclass(frozen=True)
class HydroEnergyBudget:
    """Adjustable-energy budget over an explicit LEFT-CLOSED RIGHT-OPEN
    period [period_start, period_end)."""

    period_start: datetime
    period_end: datetime
    energy_budget_mwh: float

    def __post_init__(self) -> None:
        if not isinstance(self.period_start, datetime) or not isinstance(self.period_end, datetime):
            raise HydroError("HYDRO_INVALID_INPUT", "budget periods must be datetimes")
        if self.period_start.tzinfo != self.period_end.tzinfo:
            raise HydroError(
                "HYDRO_INVALID_INPUT",
                "budget period boundaries must share the same timezone",
            )
        if self.period_start >= self.period_end:
            raise HydroError(
                "HYDRO_INVALID_INPUT",
                "period_start must be strictly before period_end",
            )
        budget = _strict_nonneg(self.energy_budget_mwh, "energy_budget_mwh")
        object.__setattr__(self, "energy_budget_mwh", budget)


@dataclass(frozen=True)
class HydroDispatchResult:
    """Audited constrained hydro dispatch for one time axis."""

    hydro_id: str
    fixed_output_mw: tuple[float, ...]
    adjustable_output_mw: tuple[float, ...]
    total_output_mw: tuple[float, ...]
    max_output_mw: tuple[float, ...]
    period_energy_mwh: tuple[float, ...]
    period_hour_indexes: tuple[tuple[int, ...], ...]
    budget_application_rule: str = ""
    budget_periods: tuple[tuple[datetime, datetime], ...] = ()


def _apply_hourly_caps(
    fixed: tuple[float, ...],
    max_output: tuple[float, ...],
    requested: tuple[float, ...],
    length: int,
) -> list[float]:
    """Hourly power caps (C901 split): 0 <= adjustable and
    fixed + adjustable <= max."""
    adjustable: list[float] = []
    for hour in range(length):
        room = max(0.0, max_output[hour] - fixed[hour])
        adjustable.append(min(requested[hour], room))
    return adjustable


def _apply_energy_budgets(
    adjustable: list[float],
    budgets: tuple[HydroEnergyBudget, ...],
    time_axis: tuple[datetime, ...],
    delta_t: float,
) -> tuple[list[float], list[float], list[tuple[int, ...]]]:
    """Per-period energy budgets (C901 split): uniform scale within each
    left-closed right-open period (deterministic; never invents
    dispatch). All scalings are applied FIRST (in the deterministic
    budget order supplied by the caller); every period_energy_mwh is
    then recomputed from the FINAL constrained adjustable output - an
    earlier-applied overlapping budget never leaves a stale audit."""
    scaled = list(adjustable)
    period_indexes: list[tuple[int, ...]] = [
        tuple(
            hour
            for hour, ts in enumerate(time_axis)
            if budget.period_start <= ts < budget.period_end
        )
        for budget in budgets
    ]
    for budget, indexes in zip(budgets, period_indexes):
        used = sum(scaled[hour] * delta_t for hour in indexes)
        if used > budget.energy_budget_mwh + 1e-12:
            factor = budget.energy_budget_mwh / used
            for hour in indexes:
                scaled[hour] *= factor
    period_energy: list[float] = [
        sum(scaled[hour] * delta_t for hour in indexes) for indexes in period_indexes
    ]
    return scaled, period_energy, period_indexes


def _validate_series_budgets_and_delta_t(
    length: int,
    fixed_output_mw: tuple[float, ...],
    max_output_mw: tuple[float, ...],
    requested_adjustable_mw: tuple[float, ...],
    budgets: tuple[object, ...],
    delta_t_hours: float,
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...], float]:
    """Input gates for check_hydro_constraints (C901 split): series
    lengths, strict numerics, budget object types and delta_t."""
    for label, series in (
        ("fixed_output_mw", fixed_output_mw),
        ("max_output_mw", max_output_mw),
        ("requested_adjustable_mw", requested_adjustable_mw),
    ):
        if not isinstance(series, tuple) or len(series) != length:
            raise HydroError(
                "HYDRO_INVALID_INPUT",
                f"{label} must be a tuple of the time-axis length",
            )
    if not isinstance(delta_t_hours, (int, float)) or isinstance(delta_t_hours, bool):
        raise HydroError("HYDRO_INVALID_INPUT", "delta_t_hours must be a real number")
    delta_t = _safe_float(delta_t_hours, "delta_t_hours")
    if not math.isfinite(delta_t) or delta_t <= 0.0:
        raise HydroError("HYDRO_INVALID_INPUT", "delta_t_hours must be finite and positive")

    fixed = tuple(_strict_nonneg(v, "fixed_output_mw") for v in fixed_output_mw)
    max_output = tuple(_strict_nonneg(v, "max_output_mw") for v in max_output_mw)
    requested = tuple(_strict_nonneg(v, "requested_adjustable_mw") for v in requested_adjustable_mw)

    if not isinstance(budgets, tuple):
        raise HydroError("HYDRO_INVALID_INPUT", "budgets must be a tuple")
    for budget in budgets:
        if not isinstance(budget, HydroEnergyBudget):
            raise HydroError(
                "HYDRO_INVALID_INPUT",
                "budgets entries must be HydroEnergyBudget instances",
            )
        _strict_nonneg(budget.energy_budget_mwh, "energy_budget_mwh")
    return fixed, max_output, requested, delta_t


def check_hydro_constraints(
    *,
    hydro_id: str,
    fixed_output_mw: tuple[float, ...],
    max_output_mw: tuple[float, ...],
    requested_adjustable_mw: tuple[float, ...],
    time_axis: tuple[datetime, ...],
    budgets: tuple[HydroEnergyBudget, ...],
    delta_t_hours: float = 1.0,
) -> HydroDispatchResult:
    """Constrain a caller-provided adjustable request trajectory (T15
    does NOT invent an optimal dispatch): hourly
    0 <= adjustable <= max_output and total = fixed + adjustable <=
    max_output; per-budget-period adjustable ENERGY
    (sum adjustable x delta_t) <= energy_budget over [start, end)."""
    _validate_time_axis(time_axis)
    if not isinstance(hydro_id, str) or not hydro_id.strip():
        raise HydroError("HYDRO_INVALID_INPUT", "hydro_id must be a non-empty string")
    length = len(time_axis)
    fixed, max_output, requested, delta_t = _validate_series_budgets_and_delta_t(
        length, fixed_output_mw, max_output_mw, requested_adjustable_mw, budgets, delta_t_hours
    )

    for hour in range(length):
        if fixed[hour] > max_output[hour] + 1e-9:
            raise HydroError(
                "HYDRO_FIXED_EXCEEDS_CAPACITY",
                "fixed must-run output must never exceed max_output_mw (never silently clipped)",
            )

    # DETERMINISTIC overlapping-budget rule (option A): budgets are
    # applied in ASCENDING WINDOW LENGTH (period_end - period_start),
    # ties broken by (period_start, period_end) - never in caller input
    # order; every period energy is recomputed from the final
    # constrained output. A later-applied budget only ever further
    # reduces inner windows, so the LAST applied overlapping budget is
    # the one that can bind exactly.
    for budget in budgets:
        if budget.period_start.tzinfo != time_axis[0].tzinfo:
            raise HydroError(
                "HYDRO_INVALID_INPUT",
                "budget period timezone must match the project time axis",
            )
    ordered_budgets = tuple(
        sorted(
            budgets,
            key=lambda budget: (
                budget.period_end - budget.period_start,
                budget.period_start,
                budget.period_end,
            ),
        )
    )
    adjustable = _apply_hourly_caps(fixed, max_output, requested, length)
    scaled, period_energy, period_indexes = _apply_energy_budgets(
        adjustable, ordered_budgets, time_axis, delta_t
    )

    total = tuple(fixed[hour] + scaled[hour] for hour in range(length))
    return HydroDispatchResult(
        hydro_id=hydro_id.strip(),
        fixed_output_mw=fixed,
        adjustable_output_mw=tuple(scaled),
        total_output_mw=total,
        max_output_mw=max_output,
        period_energy_mwh=tuple(period_energy),
        period_hour_indexes=tuple(period_indexes),
        budget_application_rule=(
            "budgets applied in ascending window length "
            "(period_end - period_start), ties broken by (period_start, "
            "period_end) - never input order; every period_energy_mwh "
            "is recomputed from the final constrained adjustable "
            "output; the last-applied overlapping budget is the one "
            "that can bind exactly"
        ),
        budget_periods=tuple(
            (budget.period_start, budget.period_end) for budget in ordered_budgets
        ),
    )


def name_match_candidates(
    *,
    source_names: tuple[str, ...],
    candidate_ids: tuple[str, ...],
    candidate_names: tuple[str, ...],
) -> list[dict[str, str]]:
    """T16 import helper contract: similar names NEVER merge; ambiguous
    matches return candidates (each with the source name, the candidate
    id and the candidate name) instead of a silent association."""
    candidates: list[dict[str, str]] = []
    for source_name in source_names:
        matches = [
            {"source_name": source_name, "candidate_id": cid, "candidate_name": cname}
            for cid, cname in zip(candidate_ids, candidate_names)
            if source_name and cname and (source_name in cname or cname in source_name)
        ]
        if matches:
            candidates.extend(matches)
    return candidates


__all__ = [
    "HydroDispatchResult",
    "HydroEnergyBudget",
    "HydroError",
    "HydroFixedProfile",
    "build_hydro_fixed_profile",
    "build_monthly_fixed_profile",
    "check_hydro_constraints",
    "name_match_candidates",
]
