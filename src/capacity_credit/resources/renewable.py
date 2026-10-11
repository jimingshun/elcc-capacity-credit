"""Renewable long-table run-time reading (T06).

Strict ``generation_basis`` authority per resource group + scenario:

- run-time reading requires exactly one non-empty resource_group_id and
  one non-empty scenario_id (two scenarios are never stitched into one
  annual curve);
- ``generation_basis`` must be non-empty on every row and a single
  value within the group+scenario; only ``actual`` or ``available`` is
  accepted at run time;
- both generation columns may coexist legally, but the authority column
  is the whole column declared by ``generation_basis`` - they are never
  blended and never per-hour fallback; gaps in the authority column
  block (DQ-22/23 run-time path, reusing the data-contract IssueCode
  values GENERATION_BASIS_MIXED / AUTHORITY_COLUMN_GAP);
- weather_year must be non-empty, unique and equal to the project-time
  local timestamp year;
- installed_capacity_mw is a required column and must be finite and
  non-negative; capacity factors must lie in [0, 1 + tolerance] and the
  authority MW column must agree with CF x installed within tolerance
  (consistency check, not a conversion);
- installed-capacity scaling multiplies every hour by ``scale``
  (finite, non-negative; scale=0 yields all-zero output); scaled output
  is re-checked to be finite and non-negative;
- duplicate (group, scenario, timestamp) keys block;
- output rows are re-aligned to the load axis by timestamp (row order
  of the input never matters); the caller's frame is never mutated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from capacity_credit.data.validation import IssueCode

_SUPPORTED_BASIS = frozenset({"actual", "available"})

_REQUIRED_COLUMNS = {
    "resource_group_id", "scenario_id", "weather_year", "timestamp",
    "generation_basis", "installed_capacity_mw",
}


class RenewableError(ValueError):
    """Stable renewable reading error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _to_utc_axis(series: pd.Series) -> pd.Series:
    stamps = pd.to_datetime(series)
    if stamps.dt.tz is None:
        raise RenewableError(
            "RENEWABLE_NAIVE_TIMESTAMP", "renewable timestamps must be timezone-aware"
        )
    if pd.isna(stamps).any():
        raise RenewableError(
            "RENEWABLE_INVALID_TIMESTAMP", "renewable timestamps must not be NaT"
        )
    return stamps.dt.tz_convert("UTC")


def _validate_tolerance(value: float, code: str, message: str) -> float:
    value = float(value)
    if not np.isfinite(value) or value < 0:
        raise RenewableError(code, message)
    return value


def build_renewable_series(
    frame: pd.DataFrame,
    *,
    timezone: str,
    load_axis: pd.Series,
    scale: float = 1.0,
    capacity_factor_tolerance: float = 1e-6,
    mw_cf_tolerance: float = 1e-6,
) -> pd.Series:
    """Build the hourly renewable output series (MW) for one group.

    The authority column is decided by the frame's ``generation_basis``
    (single non-empty value for the whole frame). Returns a Series
    indexed by the sorted UTC load axis. Never mutates ``frame``.
    """
    if frame is None or frame.empty:
        raise RenewableError("RENEWABLE_EMPTY", "renewable frame is empty")
    missing = sorted(_REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise RenewableError(
            "RENEWABLE_MISSING_COLUMNS", f"missing columns: {missing}"
        )
    scale = float(scale)
    if not np.isfinite(scale) or scale < 0:
        raise RenewableError(
            "RENEWABLE_INVALID_SCALE", "scale must be finite and non-negative"
        )
    _validate_tolerance(
        capacity_factor_tolerance, "RENEWABLE_INVALID_TOLERANCE",
        "capacity_factor_tolerance must be finite and non-negative",
    )
    _validate_tolerance(
        mw_cf_tolerance, "RENEWABLE_INVALID_TOLERANCE",
        "mw_cf_tolerance must be finite and non-negative",
    )

    def _is_missing(series: pd.Series) -> bool:
        return bool(series.isna().any()) or bool(
            series.astype(str).str.strip().eq("").any()
        )

    if _is_missing(frame["resource_group_id"]) or _is_missing(frame["scenario_id"]) \
            or _is_missing(frame["weather_year"]):
        raise RenewableError(
            "RENEWABLE_IDENTIFIER_EMPTY",
            "resource_group_id/scenario_id/weather_year must not be empty or blank",
        )
    if frame["resource_group_id"].nunique() != 1:
        raise RenewableError(
            "RENEWABLE_MULTIPLE_GROUPS",
            "run-time reading requires exactly one resource_group_id",
        )
    if frame["scenario_id"].nunique() != 1:
        raise RenewableError(
            "RENEWABLE_MULTIPLE_SCENARIOS",
            "run-time reading requires exactly one scenario_id",
        )
    if _is_missing(frame["generation_basis"]):
        raise RenewableError(
            "RENEWABLE_BASIS_EMPTY",
            "generation_basis must not be empty or blank on any row",
        )
    basis_values = frame["generation_basis"].unique()
    if len(basis_values) != 1:
        raise RenewableError(
            IssueCode.GENERATION_BASIS_MIXED,
            "generation_basis must be a single value within group+scenario",
        )
    basis = str(basis_values[0])
    if basis not in _SUPPORTED_BASIS:
        raise RenewableError(
            "RENEWABLE_UNSUPPORTED_BASIS",
            f"run-time generation_basis {basis!r} unsupported (actual/available only)",
        )
    authority_column = f"{basis}_generation_mw"
    if authority_column not in frame.columns:
        raise RenewableError(
            "RENEWABLE_MISSING_AUTHORITY_COLUMN",
            f"authority column {authority_column} missing",
        )

    work = frame.copy()
    work["_ts_utc"] = _to_utc_axis(frame["timestamp"]).astype(
        "datetime64[ns, UTC]"
    ).to_numpy()
    primary_key = ["resource_group_id", "scenario_id", "_ts_utc"]
    if work.duplicated(subset=primary_key).any():
        raise RenewableError(
            "RENEWABLE_DUPLICATE_TIMESTAMP",
            "duplicate (resource_group_id, scenario_id, timestamp) rows",
        )

    load_utc = _to_utc_axis(load_axis)
    ts_index = pd.DatetimeIndex(work["_ts_utc"])
    load_index = pd.DatetimeIndex(
        load_utc.sort_values().astype("datetime64[ns, UTC]").to_numpy()
    )
    if ts_index.size != load_index.size or set(ts_index) != set(load_index):
        raise RenewableError(
            "RENEWABLE_AXIS_MISALIGNED",
            "renewable hour set differs from the load axis",
        )

    local_years = (
        pd.to_datetime(work["_ts_utc"]).dt.tz_convert(timezone).dt.year.unique()
    )
    if len(local_years) != 1:
        raise RenewableError(
            "RENEWABLE_YEAR_MISMATCH", "renewable axis spans multiple local years"
        )
    declared_year_raw = work["weather_year"].unique()
    declared_year: list[int] = []
    for raw in declared_year_raw:
        numeric = pd.to_numeric(pd.Series([raw]), errors="coerce").iloc[0]
        if pd.isna(numeric) or not np.isfinite(numeric):
            raise RenewableError(
                "RENEWABLE_INVALID_WEATHER_YEAR",
                f"weather_year {raw!r} cannot be parsed as a finite number",
            )
        if float(numeric) != int(float(numeric)):
            raise RenewableError(
                "RENEWABLE_INVALID_WEATHER_YEAR",
                f"weather_year {raw!r} is not an integer year",
            )
        declared_year.append(int(float(numeric)))
    if len(declared_year) != 1 or declared_year[0] != int(local_years[0]):
        raise RenewableError(
            "RENEWABLE_YEAR_MISMATCH",
            "weather_year does not match the local timestamp year",
        )

    aligned = work.set_index(ts_index).sort_index().reindex(load_index)
    if pd.isna(aligned[authority_column]).any():
        raise RenewableError(
            IssueCode.AUTHORITY_COLUMN_GAP,
            "authority column has gaps; per-hour fallback is forbidden",
        )
    installed = pd.to_numeric(
        aligned["installed_capacity_mw"], errors="coerce"
    ).to_numpy()
    if pd.isna(installed).any() or not np.isfinite(installed).all() \
            or (installed < 0).any():
        raise RenewableError(
            "RENEWABLE_INVALID_INSTALLED",
            "installed_capacity_mw must be finite and non-negative",
        )
    values = pd.to_numeric(
        aligned[authority_column], errors="coerce"
    ).to_numpy()
    if not np.isfinite(values).all():
        raise RenewableError("RENEWABLE_NON_FINITE", "renewable output must be finite")
    if (values < 0).any():
        raise RenewableError("RENEWABLE_NEGATIVE", "renewable output must be non-negative")

    if "capacity_factor" in aligned.columns:
        cf = pd.to_numeric(aligned["capacity_factor"], errors="coerce").to_numpy()
        if not np.isfinite(cf).all() or (cf < 0).any():
            raise RenewableError(
                "RENEWABLE_NON_FINITE", "capacity_factor must be finite and non-negative"
            )
        if (cf > 1.0 + capacity_factor_tolerance).any():
            raise RenewableError(
                "RENEWABLE_CF_RANGE",
                f"capacity_factor exceeds 1 + {capacity_factor_tolerance}",
            )
        expected = cf * installed
        mismatch = np.abs(values - expected) > mw_cf_tolerance * np.maximum(
            np.abs(expected), 1.0
        )
        if mismatch.any():
            raise RenewableError(
                "RENEWABLE_CF_MISMATCH",
                "MW column disagrees with capacity_factor x installed_capacity",
            )

    scaled = values * scale
    if not np.isfinite(scaled).all() or (scaled < 0).any():
        raise RenewableError(
            "RENEWABLE_NON_FINITE", "scaled renewable output must be finite and non-negative"
        )
    return pd.Series(scaled, index=load_index, name="renewable_mw")


__all__ = ["RenewableError", "build_renewable_series"]
