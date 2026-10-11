"""Single public source of risk-metric units (T10, review rounds 2-3).

The pairing layer derives the unit from the RiskMetric enum through
``risk_metric_unit``; the internal table is private and read-only
(MappingProxyType), never exposed as a mutable global, so an already
constructed RiskSampleSeries unit can never be changed by external
code. Non-RiskMetric inputs raise a stable error (never a KeyError).
The values follow methodology V0.4 §3.4/§16 metric definitions.
"""

from __future__ import annotations

from types import MappingProxyType

from capacity_credit.contracts.enums import RiskMetric

_RISK_METRIC_UNITS: MappingProxyType[RiskMetric, str] = MappingProxyType(
    {
        RiskMetric.LOLE_DAYS_PER_YEAR: "days/year",
        RiskMetric.LOLH_HOURS_PER_YEAR: "hours/year",
        RiskMetric.EUE_MWH_PER_YEAR: "mwh/year",
        RiskMetric.NEUE_PPM: "ppm",
    }
)


class RiskUnitError(ValueError):
    """Stable risk-unit error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def risk_metric_unit(metric: RiskMetric) -> str:
    """Unit of a primary risk metric (derived, never a free input)."""
    if not isinstance(metric, RiskMetric):
        raise RiskUnitError(
            "RISK_UNIT_INVALID_METRIC",
            "risk_metric_unit requires a RiskMetric enum value",
        )
    return _RISK_METRIC_UNITS[metric]


_MRI_UNIT_BY_STEP: MappingProxyType[tuple[RiskMetric, str], str] = MappingProxyType(
    {
        (RiskMetric.LOLE_DAYS_PER_YEAR, "mw"): "days_per_year_per_mw",
        (RiskMetric.LOLE_DAYS_PER_YEAR, "mwh"): "days_per_year_per_mwh",
        (RiskMetric.LOLH_HOURS_PER_YEAR, "mw"): "hours_per_year_per_mw",
        (RiskMetric.LOLH_HOURS_PER_YEAR, "mwh"): "hours_per_year_per_mwh",
        (RiskMetric.EUE_MWH_PER_YEAR, "mw"): "mwh_per_year_per_mw",
        (RiskMetric.EUE_MWH_PER_YEAR, "mwh"): "mwh_per_year_per_mwh",
        (RiskMetric.NEUE_PPM, "mw"): "ppm_per_mw",
        (RiskMetric.NEUE_PPM, "mwh"): "ppm_per_mwh",
    }
)


def mri_unit(metric: RiskMetric, step_unit: str) -> str:
    """Machine MRI unit from the primary risk metric and the scalar step
    unit (methodology §16A.7.1; never free text). ``step_unit`` must be
    ``mw`` or ``mwh``; unknown combinations raise a stable error."""
    if not isinstance(metric, RiskMetric):
        raise RiskUnitError(
            "RISK_UNIT_INVALID_METRIC",
            "mri_unit requires a RiskMetric enum value",
        )
    normalized = str(step_unit).strip().lower()
    unit = _MRI_UNIT_BY_STEP.get((metric, normalized))
    if unit is None:
        raise RiskUnitError(
            "RISK_UNIT_UNKNOWN_COMBINATION",
            f"no MRI unit for metric {metric.value!r} with step_unit {step_unit!r}",
        )
    return unit


__all__ = ["RiskUnitError", "mri_unit", "risk_metric_unit"]
