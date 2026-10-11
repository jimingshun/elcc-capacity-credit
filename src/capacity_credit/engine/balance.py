"""Deterministic supply-demand balance (T06).

Single source of truth for the margin:

    margin = supply - demand
    unserved_power_mw = max(0, -margin)
    unserved_energy_mwh = unserved_power_mw * delta_t_hours

The same result drives LOLH, LOLE-day, EUE and deficit-event
identification; no other module recomputes the margin. Inputs must be
aligned vectors of equal length (broadcasting never masks dimension
errors); inputs are never mutated. The balance result keeps a copy of
the demand vector so that demand energy is always derived from the same
hourly time series, never passed in inconsistently.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class BalanceError(ValueError):
    """Stable balance error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class BalanceResult:
    margin_mw: np.ndarray
    unserved_power_mw: np.ndarray
    unserved_energy_mwh: np.ndarray
    demand_mw: np.ndarray
    delta_t_hours: float
    tolerance: float


def _validate_scalar(value: float, code: str, message: str) -> float:
    value = float(value)
    if not np.isfinite(value):
        raise BalanceError(code, message)
    return value


def compute_demand_energy_mwh(
    demand_mw: np.ndarray, delta_t_hours: float = 1.0
) -> float:
    """Demand energy = sum(demand_mw) * delta_t_hours.

    Validates the demand vector (1-D, non-empty, finite, non-negative);
    raises ``BalanceError`` otherwise.
    """
    demand = np.asarray(demand_mw)
    if demand.ndim != 1:
        raise BalanceError("BALANCE_NON_1D", "demand must be a 1-D vector")
    if demand.size == 0:
        raise BalanceError("BALANCE_EMPTY", "demand vector is empty")
    if not np.isfinite(demand).all():
        raise BalanceError("BALANCE_NON_FINITE", "demand must be finite")
    if (demand < 0).any():
        raise BalanceError(
            "BALANCE_NEGATIVE_DEMAND", "demand must be non-negative"
        )
    _validate_scalar(delta_t_hours, "BALANCE_INVALID_DELTA_T",
                     "delta_t_hours must be finite and strictly positive")
    if delta_t_hours <= 0:
        raise BalanceError("BALANCE_INVALID_DELTA_T",
                           "delta_t_hours must be strictly positive")
    return float(demand.sum() * delta_t_hours)


def compute_margin(
    supply_mw: np.ndarray,
    demand_mw: np.ndarray,
    *,
    delta_t_hours: float = 1.0,
    tolerance: float = 1e-6,
) -> BalanceResult:
    """Compute the single balance result for aligned hourly vectors."""
    supply = np.asarray(supply_mw)
    demand = np.asarray(demand_mw)
    if supply.size == 0 or demand.size == 0:
        raise BalanceError("BALANCE_EMPTY", "balance inputs must not be empty")
    if supply.ndim != 1 or demand.ndim != 1:
        raise BalanceError("BALANCE_NON_1D", "balance inputs must be 1-D vectors")
    if supply.size != demand.size:
        raise BalanceError(
            "BALANCE_LENGTH_MISMATCH",
            f"supply {supply.size} != demand {demand.size}",
        )
    if not np.isfinite(supply).all() or not np.isfinite(demand).all():
        raise BalanceError("BALANCE_NON_FINITE", "balance inputs must be finite")
    if (demand < 0).any():
        raise BalanceError(
            "BALANCE_NEGATIVE_DEMAND", "demand must be non-negative hour by hour"
        )
    delta_t = _validate_scalar(
        delta_t_hours, "BALANCE_INVALID_DELTA_T",
        "delta_t_hours must be finite and strictly positive",
    )
    if delta_t <= 0:
        raise BalanceError("BALANCE_INVALID_DELTA_T",
                           "delta_t_hours must be strictly positive")
    tol = _validate_scalar(
        tolerance, "BALANCE_INVALID_TOLERANCE",
        "tolerance must be finite and non-negative",
    )
    if tol < 0:
        raise BalanceError("BALANCE_INVALID_TOLERANCE",
                           "tolerance must be non-negative")
    margin = supply - demand
    unserved = np.maximum(0.0, -margin)
    energy = unserved * delta_t
    return BalanceResult(
        margin_mw=margin,
        unserved_power_mw=unserved,
        unserved_energy_mwh=energy,
        demand_mw=demand.copy(),
        delta_t_hours=delta_t,
        tolerance=tol,
    )


__all__ = ["BalanceError", "BalanceResult", "compute_demand_energy_mwh", "compute_margin"]
