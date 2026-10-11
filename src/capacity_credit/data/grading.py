"""Data grade computation and three-state consistency (data contract
V0.3 §26).

``computed_max_data_grade`` is the highest grade objectively supported
by the data; every conclusion is explainable via ``reasons``.

Rules (review-aligned):

- ``G0_teaching`` only for teaching_synthetic / built-in case sources;
- non-teaching data without at least one COMPLETE synchronized weather
  year (verified in the project timezone and calendar, 8760/8784 hours)
  is ``None`` (undetermined), never disguised as G0;
- 1--2 complete years are at most ``G1_preliminary`` even with
  sequential reliability parameters;
- >=3 complete years plus sequential reliability can reach
  ``G2_research`` (G3 additionally requires locked rules, independent
  review and frozen versions).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field

import pandas as pd

GRADE_ORDER = ["G0_teaching", "G1_preliminary", "G2_research", "G3_rule_support"]

_GRADE_RANK = {grade: index for index, grade in enumerate(GRADE_ORDER)}


@dataclass
class GradeResult:
    grade: str | None
    reasons: list[str] = field(default_factory=list)


def _is_leap(year: int) -> bool:
    return calendar.isleap(year)


def _complete_year_hours(year: int) -> int:
    return 8784 if _is_leap(year) else 8760


def _complete_years(
    tables: dict[str, pd.DataFrame], timezone: str = "Asia/Shanghai"
) -> tuple[list[int], list[str]]:
    """Complete synchronized weather years in the project timezone.

    A year counts only when the load axis covers exactly 8760/8784 hours
    of that local calendar year. Returns (years, reasons).
    """
    load = tables.get("load_hourly")
    if load is None or "timestamp" not in load.columns:
        return [], ["no load_hourly time axis"]
    stamps = pd.to_datetime(load["timestamp"], utc=True, errors="coerce").dropna()
    if stamps.empty:
        return [], ["empty load time axis"]
    local = stamps.dt.tz_convert(timezone)
    years: list[int] = []
    reasons: list[str] = []
    for year in sorted(local.dt.year.unique()):
        sub = local[local.dt.year == year]
        expected = _complete_year_hours(int(year))
        if len(sub) == expected and sub.min().date() == pd.Timestamp(
            year, 1, 1, tz=timezone
        ).date() and sub.max().date() == pd.Timestamp(
            year, 12, 31, tz=timezone
        ).date():
            years.append(int(year))
        else:
            reasons.append(
                f"weather year {year} incomplete: {len(sub)}/{expected} hours"
            )
    if not years:
        reasons.append("no complete synchronized weather year")
    return years, reasons


def _is_teaching(tables: dict[str, pd.DataFrame]) -> bool:
    if "source_ledger" in tables:
        ledger = tables["source_ledger"]
        if "source_type" in ledger.columns and (
            ledger["source_type"] == "teaching_synthetic"
        ).any():
            return True
    return False


def compute_max_data_grade(
    tables: dict[str, pd.DataFrame], timezone: str = "Asia/Shanghai"
) -> GradeResult:
    """Compute the highest objectively supported data grade."""
    reasons: list[str] = []
    teaching = _is_teaching(tables)
    if teaching:
        reasons.append("teaching_synthetic source present -> max G0_teaching")
        return GradeResult("G0_teaching", reasons)

    years, year_reasons = _complete_years(tables, timezone)
    reasons.extend(year_reasons)

    sequential = False
    classes = tables.get("thermal_reliability_classes")
    if classes is not None and "authoritative_parameter_set" in classes.columns:
        sequential = not (
            classes["authoritative_parameter_set"].fillna("").astype(str) == "for_only"
        ).all()

    if len(years) == 0:
        reasons.append("grade undetermined (no complete synchronized weather year)")
        return GradeResult(None, reasons)
    if len(years) <= 2:
        reasons.append(
            f"{len(years)} complete weather year(s) -> max G1_preliminary"
        )
        return GradeResult("G1_preliminary", reasons)
    if not sequential:
        reasons.append("FOR-only reliability -> max G1_preliminary")
        return GradeResult("G1_preliminary", reasons)
    reasons.append(">=3 complete weather years with sequential reliability -> max G2_research")
    reasons.append("G3_rule_support requires locked rules, independent review and frozen versions")
    return GradeResult("G2_research", reasons)


def check_grade_consistency(
    declared: str,
    computed_max: str | None,
    effective: str | None = None,
) -> None:
    """Enforce ``effective <= declared <= computed_max``.

    Raises ``ValueError`` with the violating relation otherwise; a
    ``None`` computed max (undetermined) is always inconsistent with a
    concrete declared grade.
    """
    if computed_max is None:
        raise ValueError(
            f"declared {declared} cannot be used: computed_max undetermined"
        )
    if declared not in _GRADE_RANK or computed_max not in _GRADE_RANK:
        raise ValueError(f"unknown grade value: declared={declared!r} max={computed_max!r}")
    if effective is not None and effective not in _GRADE_RANK:
        raise ValueError(f"unknown grade value: effective={effective!r}")
    if _GRADE_RANK[declared] > _GRADE_RANK[computed_max]:
        raise ValueError(
            f"declared {declared} exceeds computed_max {computed_max}"
        )
    if effective is not None and _GRADE_RANK[effective] > _GRADE_RANK[declared]:
        raise ValueError(
            f"effective {effective} exceeds declared {declared}"
        )


__all__ = ["GRADE_ORDER", "GradeResult", "check_grade_consistency", "compute_max_data_grade"]
