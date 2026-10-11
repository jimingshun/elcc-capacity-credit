"""Load-growth adapter and immutable portfolio views (T10, review rounds 1-3).

Implements the three load-increase modes of methodology §8.2 and the
immutable system/portfolio view used for common-random-number paired
evaluation (§4.6):

- fixed MW shift:  L'_t = L_t + dL, peak_increment_mw == dL;
- proportional:   L'_t = (1 + a) L_t, capacity calibre is the peak of
  the increment curve max_t(L'_t - L_t), never the mean increment;
- specified shape: relative  L'_t = L_t * (1 + b * multiplier_t)
                    absolute  L'_t = L_t + b * addition_mw_t
  with profile identity, source_id (linked to the P0 source ledger; the
  raw-file hash closes at T12), data status, calibre and scale preserved,
  the profile time axis strictly validated (year-set equality, strict
  int keys, non-empty shapes, strictly increasing 1-hour axis, timestamp
  year == weather year, timezone-name equality - even when the absolute
  instants agree - and exact hour-by-hour equality with the run axis:
  missing/duplicate/extra hours and leap-year misalignment block), no
  relative/absolute mixing, finite values and non-negative grown load.

Immutability is structural: frozen dataclasses, NumPy arrays
defensively copied and read-only, and per-year mappings defensively
copied and exposed as ``MappingProxyType`` so neither the caller's
dictionaries nor dict-style mutation can bypass it. Input loads are
never mutated; the portfolio view never copies scenario bank
trajectories, only object identities/references. Resource input order
cannot change normalized object identity (frozensets + the single
canonicalization in ``domain.identifiers`` with collision blocking).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd

_VALID_STATUS = frozenset({"modelled", "assumed", "derived"})
_VALID_BASIS = frozenset({"growth_multiplier", "growth_addition_mw"})
_ONE_HOUR = pd.Timedelta(hours=1)


class GrowthError(ValueError):
    """Stable load-growth error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_identifier(value: Any, field_name: str) -> str:
    """Strict identifier validation: only a non-blank string is accepted;
    None, numbers, bools and containers are rejected (never coerced)."""
    if value is None or isinstance(value, bool) or not isinstance(value, str):
        raise GrowthError(
            "GROWTH_EMPTY_IDENTIFIER",
            f"{field_name} must be a non-blank string",
        )
    stripped = value.strip()
    if not stripped:
        raise GrowthError(
            "GROWTH_EMPTY_IDENTIFIER", f"{field_name} must not be blank"
        )
    return stripped


def _strict_year(value: Any, field_name: str = "weather_year") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise GrowthError(
            "GROWTH_YEAR_MISMATCH",
            f"{field_name} must be a strict integer (bool/str rejected)",
        )
    return int(value)


def _canonical_object_set(
    values: Any, field_name: str,
) -> frozenset[str]:
    """Canonicalize an object-id set through the single public
    definition; collisions block instead of silently merging."""
    from capacity_credit.domain.identifiers import (
        IdentifierError,
        canonical_object_id_set,
    )

    try:
        return canonical_object_id_set(values, field_name)
    except IdentifierError as exc:
        code = {
            "IDENTIFIER_NOT_STRING": "GROWTH_EMPTY_IDENTIFIER",
            "IDENTIFIER_BLANK": "GROWTH_EMPTY_IDENTIFIER",
            "IDENTIFIER_EMPTY": "GROWTH_EMPTY_IDENTIFIER",
            "IDENTIFIER_COLLISION": "GROWTH_OBJECT_ID_COLLISION",
        }.get(exc.code, "GROWTH_EMPTY_IDENTIFIER")
        raise GrowthError(code, exc.message) from exc


def _validate_scale(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise GrowthError("GROWTH_INVALID_SCALE", "scale must be a real number")
    number = float(value)
    if not np.isfinite(number) or number < 0.0:
        raise GrowthError(
            "GROWTH_INVALID_SCALE", "scale must be finite and non-negative"
        )
    return number


def _read_only_1d(
    values: Any, code: str, *, require_non_negative: bool = True,
) -> np.ndarray:
    try:
        array = np.array(values, dtype=float, copy=True)
    except (TypeError, ValueError) as exc:
        raise GrowthError(code, f"values must be numeric: {exc}") from exc
    if array.ndim != 1:
        raise GrowthError(code, "values must be 1-D")
    if array.size == 0:
        raise GrowthError("GROWTH_EMPTY_SHAPE", "values must not be empty")
    if not np.isfinite(array).all():
        raise GrowthError(code, "values must be finite")
    if require_non_negative and (array < 0.0).any():
        raise GrowthError(code, "values must be non-negative")
    array.setflags(write=False)
    return array


def _strict_year_keys(mapping: Mapping[Any, Any], code: str) -> None:
    for key in mapping:
        if isinstance(key, bool) or not isinstance(key, (int, np.integer)):
            raise GrowthError(
                code, "weather-year keys must be strict integers"
            )
        if int(key) != key:
            raise GrowthError(code, "weather-year keys must be integers")


def _copy_loads(load_by_year: Mapping[int, Any]) -> dict[int, np.ndarray]:
    if not isinstance(load_by_year, Mapping) or not load_by_year:
        raise GrowthError("GROWTH_INVALID_LOAD", "load_by_year must be non-empty")
    _strict_year_keys(load_by_year, "GROWTH_INVALID_LOAD")
    copies: dict[int, np.ndarray] = {}
    for year, values in load_by_year.items():
        copies[int(year)] = _read_only_1d(values, "GROWTH_INVALID_LOAD")
    return copies


def _normalize_axis(series: Any) -> pd.DatetimeIndex:
    try:
        stamps = pd.to_datetime(pd.Series(series))
    except (TypeError, ValueError) as exc:
        raise GrowthError("GROWTH_AXIS_MISMATCH", f"axis unparsable: {exc}") from exc
    if stamps.dt.tz is None:
        raise GrowthError(
            "GROWTH_AXIS_MISMATCH", "axis timestamps must be timezone-aware"
        )
    if stamps.isna().any():
        raise GrowthError("GROWTH_AXIS_MISMATCH", "axis must not contain NaT")
    index = pd.DatetimeIndex(stamps)
    return index


def _validate_profile_axis(index: pd.DatetimeIndex, year: int) -> None:
    """Strict axis validation: non-empty, strictly increasing, adjacent
    hours exactly 1 hour apart, and the local timestamp year equal to
    the declared weather year (leap-year misalignment shows up here)."""
    if index.size == 0:
        raise GrowthError("GROWTH_EMPTY_SHAPE", "profile axis must not be empty")
    if index.has_duplicates:
        raise GrowthError(
            "GROWTH_AXIS_MISMATCH", "axis must not contain duplicates"
        )
    if not index.is_monotonic_increasing:
        raise GrowthError(
            "GROWTH_AXIS_MISMATCH", "axis timestamps must be strictly increasing"
        )
    diffs = pd.Series(index).diff().dropna()
    if (diffs != _ONE_HOUR).any():
        raise GrowthError(
            "GROWTH_AXIS_MISMATCH",
            "adjacent hours must be exactly one hour apart",
        )
    local_years = index.tz_convert(index.tz).year
    if (local_years != year).any():
        raise GrowthError(
            "GROWTH_YEAR_MISMATCH",
            f"timestamp year must equal the declared weather year {year}",
        )


@dataclass(frozen=True)
class PortfolioView:
    """Immutable system/portfolio view for paired evaluation.

    Only object identities/references are stored; the scenario bank
    random trajectories themselves are never copied here. The load
    mapping is exposed read-only (MappingProxyType + read-only arrays).
    Object ids go through the single canonicalization in
    ``domain.identifiers`` (collisions block, never silently merge).
    """

    portfolio_id: str
    weather_years: tuple[int, ...]
    load_by_year: Mapping[int, np.ndarray]
    shared_object_ids: frozenset[str]
    candidate_object_ids: frozenset[str]
    all_object_ids: frozenset[str]
    time_axis_reference: str | None = None

    def __post_init__(self) -> None:
        portfolio_id = _validate_identifier(self.portfolio_id, "portfolio_id")
        object.__setattr__(self, "portfolio_id", portfolio_id)
        years = tuple(_strict_year(y) for y in self.weather_years)
        if not years:
            raise GrowthError("GROWTH_EMPTY_IDENTIFIER", "weather_years empty")
        if len(set(years)) != len(years):
            raise GrowthError(
                "GROWTH_YEAR_MISMATCH", "weather years must be unique"
            )
        object.__setattr__(self, "weather_years", years)
        loads = _copy_loads(self.load_by_year)
        if set(loads) != set(years):
            raise GrowthError(
                "GROWTH_YEAR_MISMATCH",
                "load years must equal the portfolio weather years",
            )
        object.__setattr__(
            self, "load_by_year", MappingProxyType(loads),
        )
        shared = _canonical_object_set(self.shared_object_ids, "shared_object_ids")
        candidate = _canonical_object_set(
            self.candidate_object_ids, "candidate_object_ids",
        )
        if shared & candidate:
            raise GrowthError(
                "GROWTH_OBJECT_SET_CONFLICT",
                "shared and candidate object sets must be disjoint",
            )
        all_objects = _canonical_object_set(self.all_object_ids, "all_object_ids")
        if all_objects != shared | candidate:
            raise GrowthError(
                "GROWTH_OBJECT_SET_CONFLICT",
                "all_object_ids must equal shared | candidate",
            )
        object.__setattr__(self, "shared_object_ids", shared)
        object.__setattr__(self, "candidate_object_ids", candidate)
        object.__setattr__(self, "all_object_ids", all_objects)
        if self.time_axis_reference is not None:
            object.__setattr__(
                self,
                "time_axis_reference",
                _validate_identifier(self.time_axis_reference, "time_axis_reference"),
            )


def _parse_payload_year(raw: Any) -> int:
    """Parse a serialized (string) weather-year key; anything that is
    not an exact integer literal is a stable profile error."""
    text = str(raw).strip()
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise GrowthError(
            "GROWTH_INVALID_PROFILE", f"invalid profile weather year {raw!r}",
        ) from exc
    if str(value) != text:
        raise GrowthError(
            "GROWTH_INVALID_PROFILE",
            f"invalid profile weather year {raw!r}",
        )
    return value


@dataclass(frozen=True)
class LoadGrowthResult:
    """Full growth result: grown load, increment curves and peak MW.

    Structural immutability: the per-year mappings are defensive copies
    of the caller's dictionaries wrapped in MappingProxyType; arrays are
    defensively copied and read-only; year keys are strict integers; the
    three year sets must agree; peaks are finite and non-negative.
    """

    mode: str
    grown_load_by_year: Mapping[int, np.ndarray]
    increment_by_year: Mapping[int, np.ndarray]
    peak_increment_mw_by_year: Mapping[int, float]
    peak_increment_mw: float
    profile_id: str | None = None
    source_id: str | None = None
    data_status: str | None = None
    basis: str | None = None
    scale: float | None = None

    def __post_init__(self) -> None:
        for field_name in ("grown_load_by_year", "increment_by_year",
                           "peak_increment_mw_by_year"):
            mapping = getattr(self, field_name)
            if not isinstance(mapping, Mapping) or not mapping:
                raise GrowthError(
                    "GROWTH_INVALID_RESULT",
                    f"{field_name} must be a non-empty mapping",
                )
            _strict_year_keys(mapping, "GROWTH_INVALID_RESULT")
        year_sets = [
            set(self.grown_load_by_year),
            set(self.increment_by_year),
            set(self.peak_increment_mw_by_year),
        ]
        if any(year_sets[0] != other for other in year_sets[1:]):
            raise GrowthError(
                "GROWTH_INVALID_RESULT",
                "grown/increment/peak year sets must be identical",
            )
        grown: dict[int, np.ndarray] = {}
        increments: dict[int, np.ndarray] = {}
        peaks: dict[int, float] = {}
        for year, values in self.grown_load_by_year.items():
            grown[int(year)] = _read_only_1d(values, "GROWTH_INVALID_RESULT")
        for year, values in self.increment_by_year.items():
            increments[int(year)] = _read_only_1d(values, "GROWTH_INVALID_RESULT")
        for year, raw in self.peak_increment_mw_by_year.items():
            if isinstance(raw, bool) or not isinstance(
                raw, (int, float, np.integer, np.floating)
            ):
                raise GrowthError(
                    "GROWTH_INVALID_RESULT",
                    "peak increment must be a real number",
                )
            peak = float(raw)
            if not np.isfinite(peak) or peak < 0.0:
                raise GrowthError(
                    "GROWTH_INVALID_RESULT",
                    "peak increment must be finite and non-negative",
                )
            peaks[int(year)] = peak
        for year, grown_array in grown.items():
            if grown_array.size != increments[year].size:
                raise GrowthError(
                    "GROWTH_INVALID_RESULT",
                    f"grown and increment arrays must match in year {year}",
                )
        # physical invariant per year: declared per-year peak == max(increment)
        for year, increment in increments.items():
            actual_peak = float(np.max(increment))
            if peaks[year] != actual_peak:
                raise GrowthError(
                    "GROWTH_INVALID_RESULT",
                    f"peak_increment_mw_by_year[{year}] must equal "
                    f"max(increment_by_year[{year}]) (declared {peaks[year]}, "
                    f"actual {actual_peak})",
                )
        object.__setattr__(self, "grown_load_by_year", MappingProxyType(grown))
        object.__setattr__(self, "increment_by_year", MappingProxyType(increments))
        object.__setattr__(
            self, "peak_increment_mw_by_year", MappingProxyType(peaks),
        )
        if isinstance(self.peak_increment_mw, bool) or not isinstance(
            self.peak_increment_mw, (int, float, np.integer, np.floating)
        ):
            raise GrowthError(
                "GROWTH_INVALID_RESULT",
                "peak_increment_mw must be a real number",
            )
        overall = float(self.peak_increment_mw)
        if not np.isfinite(overall) or overall < 0.0:
            raise GrowthError(
                "GROWTH_INVALID_RESULT",
                "peak_increment_mw must be finite and non-negative",
            )
        if overall != float(max(peaks.values())):
            raise GrowthError(
                "GROWTH_INVALID_RESULT",
                "peak_increment_mw must equal the max per-year peak",
            )
        object.__setattr__(self, "peak_increment_mw", overall)


def apply_fixed_mw_growth(
    load_by_year: Mapping[int, Any], delta_mw: Any,
) -> LoadGrowthResult:
    """L'_t = L_t + dL; every hour grows by exactly dL."""
    if isinstance(delta_mw, bool) or not isinstance(
        delta_mw, (int, float, np.integer, np.floating)
    ):
        raise GrowthError("GROWTH_INVALID_DELTA", "delta_mw must be a real number")
    delta = float(delta_mw)
    if not np.isfinite(delta) or delta < 0.0:
        raise GrowthError(
            "GROWTH_INVALID_DELTA", "delta_mw must be finite and non-negative"
        )
    loads = _copy_loads(load_by_year)
    grown: dict[int, np.ndarray] = {}
    increments: dict[int, np.ndarray] = {}
    peaks: dict[int, float] = {}
    for year, load in loads.items():
        increment = np.full(load.shape, delta, dtype=float)
        grown[year] = _read_only_1d(load + increment, "GROWTH_INVALID_LOAD")
        increments[year] = _read_only_1d(increment, "GROWTH_NON_FINITE")
        peaks[year] = delta
    return LoadGrowthResult(
        mode="fixed_mw",
        grown_load_by_year=grown,
        increment_by_year=increments,
        peak_increment_mw_by_year=peaks,
        peak_increment_mw=float(max(peaks.values())),
    )


def apply_scaling_growth(
    load_by_year: Mapping[int, Any], alpha: Any,
) -> LoadGrowthResult:
    """L'_t = (1 + a) L_t; the MW calibre is the annual peak of the
    increment curve max_t(a * L_t), never the mean increment."""
    scale = _validate_scale(alpha)
    loads = _copy_loads(load_by_year)
    grown: dict[int, np.ndarray] = {}
    increments: dict[int, np.ndarray] = {}
    peaks: dict[int, float] = {}
    for year, load in loads.items():
        increment = scale * load
        grown[year] = _read_only_1d(load + increment, "GROWTH_INVALID_LOAD")
        increments[year] = _read_only_1d(increment, "GROWTH_NON_FINITE")
        peaks[year] = float(np.max(increment))
    return LoadGrowthResult(
        mode="scaling",
        grown_load_by_year=grown,
        increment_by_year=increments,
        peak_increment_mw_by_year=peaks,
        peak_increment_mw=float(max(peaks.values())),
    )


@dataclass(frozen=True)
class LoadGrowthProfile:
    """Specified growth shape (data contract §23.1).

    One calibre per profile (growth_multiplier or growth_addition_mw);
    the shape time axis must equal the run load axis hour by hour
    (compared by timezone NAME, then by exact instants). ``source_id``
    links the profile to the P0 source ledger; the raw file hash lives
    in that ledger (sha256/input_file_hash) and is wired in at the T12
    data-import integration - the domain object carries no invented
    hash field, and T10 does not claim a complete DQ-31 or any
    application-service source closure.
    """

    load_growth_profile_id: str
    source_id: str
    data_status: str
    basis: str
    scale: float
    shape_by_year: Mapping[int, np.ndarray]
    timestamps_by_year: Mapping[int, Any]
    addition_shape_by_year: Mapping[int, np.ndarray] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "load_growth_profile_id",
            _validate_identifier(self.load_growth_profile_id, "load_growth_profile_id"),
        )
        object.__setattr__(
            self, "source_id", _validate_identifier(self.source_id, "source_id")
        )
        if not isinstance(self.data_status, str) or not self.data_status.strip():
            raise GrowthError(
                "GROWTH_INVALID_STATUS", "data_status must be a non-blank string",
            )
        status = self.data_status.strip()
        if status not in _VALID_STATUS:
            raise GrowthError(
                "GROWTH_INVALID_STATUS",
                f"data_status must be one of {sorted(_VALID_STATUS)}",
            )
        object.__setattr__(self, "data_status", status)
        if not isinstance(self.basis, str) or not self.basis.strip():
            raise GrowthError(
                "GROWTH_INVALID_BASIS", "basis must be a non-blank string",
            )
        basis = self.basis.strip()
        if basis not in _VALID_BASIS:
            raise GrowthError(
                "GROWTH_INVALID_BASIS",
                f"basis must be one of {sorted(_VALID_BASIS)}",
            )
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "scale", _validate_scale(self.scale))
        if not self.shape_by_year or not self.timestamps_by_year:
            raise GrowthError(
                "GROWTH_YEAR_MISMATCH", "profile must cover at least one weather year"
            )
        _strict_year_keys(self.shape_by_year, "GROWTH_YEAR_MISMATCH")
        _strict_year_keys(self.timestamps_by_year, "GROWTH_YEAR_MISMATCH")
        if set(self.shape_by_year) != set(self.timestamps_by_year):
            raise GrowthError(
                "GROWTH_YEAR_MISMATCH",
                "shape_by_year and timestamps_by_year year sets must be equal",
            )
        shapes: dict[int, np.ndarray] = {}
        axes: dict[int, pd.DatetimeIndex] = {}
        for raw_year, values in self.shape_by_year.items():
            year = _strict_year(raw_year)
            shapes[year] = _read_only_1d(
                values, "GROWTH_NON_FINITE", require_non_negative=False,
            )
            axis = _normalize_axis(self.timestamps_by_year[raw_year])
            _validate_profile_axis(axis, year)
            if axis.size != shapes[year].size:
                raise GrowthError(
                    "GROWTH_AXIS_MISMATCH",
                    "profile axis length differs from the shape values",
                )
            axes[year] = axis
        object.__setattr__(self, "shape_by_year", MappingProxyType(shapes))
        object.__setattr__(self, "timestamps_by_year", MappingProxyType(axes))
        if self.addition_shape_by_year is not None:
            raise GrowthError(
                "GROWTH_BASIS_MIXED",
                "a single profile cannot carry both relative and absolute "
                "growth columns",
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic serialization (no drift on round trip)."""
        return {
            "load_growth_profile_id": self.load_growth_profile_id,
            "source_id": self.source_id,
            "data_status": self.data_status,
            "basis": self.basis,
            "scale": self.scale,
            "shape_by_year": {
                str(year): list(map(float, values))
                for year, values in sorted(self.shape_by_year.items())
            },
            "timestamps_by_year": {
                str(year): {
                    "timezone": str(self.timestamps_by_year[year].tz),
                    "stamps": [str(ts) for ts in self.timestamps_by_year[year]],
                }
                for year in sorted(self.timestamps_by_year)
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LoadGrowthProfile:
        """Stable deserialization: any malformed payload raises a
        GrowthError, never a KeyError/AttributeError/pandas exception."""
        if not isinstance(payload, dict):
            raise GrowthError("GROWTH_INVALID_PROFILE", "profile must be a mapping")
        required = {
            "load_growth_profile_id", "source_id", "data_status", "basis",
            "scale", "shape_by_year", "timestamps_by_year",
        }
        if set(payload) != required:
            raise GrowthError(
                "GROWTH_INVALID_PROFILE",
                f"profile fields must be exactly {sorted(required)}",
            )
        try:
            shapes = {
                _parse_payload_year(year): np.asarray(values, dtype=float)
                for year, values in payload["shape_by_year"].items()
            }
            axes: dict[int, Any] = {}
            for year, stamps in payload["timestamps_by_year"].items():
                year_key = _parse_payload_year(year)
                if not isinstance(stamps, dict) \
                        or "stamps" not in stamps or "timezone" not in stamps:
                    raise GrowthError(
                        "GROWTH_INVALID_PROFILE",
                        "timestamps_by_year entries must carry timezone and stamps",
                    )
                if not isinstance(stamps["stamps"], (list, tuple)):
                    raise GrowthError(
                        "GROWTH_INVALID_PROFILE", "stamps must be a sequence",
                    )
                try:
                    axis = pd.Series(
                        pd.to_datetime(stamps["stamps"], utc=True)
                    ).dt.tz_convert(str(stamps["timezone"]))
                except Exception as exc:
                    raise GrowthError(
                        "GROWTH_INVALID_PROFILE",
                        f"invalid timezone or stamps: {exc}",
                    ) from exc
                axes[year_key] = axis
            # identity fields go through strict validation with their raw
            # values (no str() coercion before validation)
            return cls(
                load_growth_profile_id=payload["load_growth_profile_id"],
                source_id=payload["source_id"],
                data_status=payload["data_status"],
                basis=payload["basis"],
                scale=payload["scale"],
                shape_by_year=shapes,
                timestamps_by_year=axes,
            )
        except GrowthError:
            raise
        except Exception as exc:
            raise GrowthError(
                "GROWTH_INVALID_PROFILE", f"profile payload invalid: {exc}",
            ) from exc


def apply_specified_growth(
    load_by_year: Mapping[int, Any],
    time_axis_by_year: Mapping[int, Any],
    profile: LoadGrowthProfile,
) -> LoadGrowthResult:
    """Apply the specified growth profile to the run loads.

    The profile axis must equal the run load axis hour by hour with the
    same timezone NAME (not only the same absolute instants) for every
    weather year; missing profile years block; grown load must stay
    finite and non-negative.
    """
    loads = _copy_loads(load_by_year)
    _strict_year_keys(time_axis_by_year, "GROWTH_YEAR_MISMATCH")
    run_axes = {
        int(year): _normalize_axis(axis) for year, axis in time_axis_by_year.items()
    }
    if set(run_axes) != set(loads):
        raise GrowthError(
            "GROWTH_YEAR_MISMATCH",
            "load years must equal the run time-axis years",
        )
    if set(profile.shape_by_year) != set(loads):
        raise GrowthError(
            "GROWTH_YEAR_MISMATCH",
            "profile years must equal the run load years",
        )
    grown: dict[int, np.ndarray] = {}
    increments: dict[int, np.ndarray] = {}
    peaks: dict[int, float] = {}
    for year, load in loads.items():
        run_axis = run_axes[year]
        profile_axis = profile.timestamps_by_year[year]
        # timezone comparison by NAME: same name with a different tzinfo
        # implementation is allowed; different names block even when the
        # absolute instants agree
        if str(run_axis.tz) != str(profile_axis.tz):
            raise GrowthError(
                "GROWTH_AXIS_MISMATCH",
                f"timezone name differs between the profile and the run "
                f"axis in year {year} "
                f"({str(profile_axis.tz)!r} vs {str(run_axis.tz)!r})",
            )
        if run_axis.size != profile_axis.size or not (
            run_axis == profile_axis
        ).all():
            raise GrowthError(
                "GROWTH_AXIS_MISMATCH",
                f"profile axis differs from the run load axis in year {year} "
                "(missing/duplicate/extra hours or leap-year mismatch)",
            )
        shape = profile.shape_by_year[year]
        if profile.basis == "growth_multiplier":
            increment = profile.scale * shape * load
        else:
            increment = profile.scale * shape
        grown[year] = _read_only_1d(load + increment, "GROWTH_NEGATIVE_LOAD")
        increments[year] = _read_only_1d(increment, "GROWTH_NON_FINITE")
        peaks[year] = float(np.max(increment))
    return LoadGrowthResult(
        mode="specified",
        grown_load_by_year=grown,
        increment_by_year=increments,
        peak_increment_mw_by_year=peaks,
        peak_increment_mw=float(max(peaks.values())),
        profile_id=profile.load_growth_profile_id,
        source_id=profile.source_id,
        data_status=profile.data_status,
        basis=profile.basis,
        scale=profile.scale,
    )


__all__ = [
    "GrowthError",
    "LoadGrowthProfile",
    "LoadGrowthResult",
    "PortfolioView",
    "apply_fixed_mw_growth",
    "apply_scaling_growth",
    "apply_specified_growth",
]
