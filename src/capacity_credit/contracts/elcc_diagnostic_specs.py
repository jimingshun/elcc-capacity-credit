"""Neutral ELCC/EFC diagnostic specifications.

This module is the single contract source for diagnostic code, severity and
product category.  It depends only on public enums, so the Pydantic result
model, diagnostic publisher and JSON-Schema generator can all consume it
without creating a contracts/results import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from capacity_credit.contracts.enums import Severity

DiagnosticCategory = Literal["info", "warning", "method_error", "data_error"]

# Round 9: the four aggregation boundaries every formal diagnostic must
# fall into.  ``group`` is orthogonal to ``category`` (category drives
# the product-status aggregation; group identifies the diagnostic
# SOURCE, used by the aggregation boundary documentation and the
# collector).
DiagnosticGroup = Literal[
    "scenario_input",
    "resource_model",
    "risk_search",
    "blocking",
]


class ElccDiagnosticsError(ValueError):
    """Stable diagnostic-contract error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DiagnosticSpec:
    """One immutable diagnostic classification."""

    severity: Severity
    category: DiagnosticCategory
    group: DiagnosticGroup
    message: str


ELCC_DIAGNOSTIC_REGISTRY: MappingProxyType[str, DiagnosticSpec] = MappingProxyType(
    {
        "BASELINE_NOT_CALIBRATED": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="scenario_input",
            message=(
                "本运行未提供 calibrated_baseline_ref：基准负荷水平未经标定闭环"
                "确认，等风险搜索判据与判据带判据（方法说明第五章(一)）仅在基准"
                "已标定于判据线时等价（第八章风险2）"
            ),
        ),
        "ELCC_CI_NOT_AVAILABLE": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="risk_search",
            message=(
                "the methodology defines no capacity confidence-interval "
                "algorithm in T12; lower_ci/upper_ci are null and the "
                "search bracket must not be read as a statistical CI"
            ),
        ),
        "EFC_CI_NOT_AVAILABLE": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="risk_search",
            message=(
                "the methodology defines no capacity confidence-interval "
                "algorithm in T12; lower_ci/upper_ci are null and the "
                "EFC search bracket must not be read as a statistical CI"
            ),
        ),
        "EFC_WINTER_WINDOW_PROVISIONAL": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="scenario_input",
            message=(
                "EFC 冬季晚峰判据窗口取 provisional 默认（11-3月 × 17-21时），"
                "正式身份前须由基准测试锁定并显式声明（MR-01.1）"
            ),
        ),
        "EFC_TECHNOLOGY_CLASS_MISSING": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="scenario_input",
            message=(
                "本 EFC 运行未声明 technology_class（教学/研究豁免）：结果不进入"
                "分技术类别降额系数表的正式类别组（MR-01.2）"
            ),
        ),
        "SEED_REPLICATION_GATE_MISSING": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="risk_search",
            message=(
                "正式结果未绑定通过的 seed_replication 门（多种子稳健性复核，"
                "MR-03）：单种子点值不具备完整评审效力，95% 置信区间缺位"
            ),
        ),
        "EFC_UPPER_BOUND_TOUCHED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message=(
                "the EFC matched point touched the declared physical upper "
                "bound; the upper bound must not be treated as a trusted "
                "point value without checking the resource definition, "
                "statistical noise and bracket width"
            ),
        ),
        "EFC_MATCHED_POINT_NOT_CONVERGED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message=(
                "the interpolated EFC matched point did not re-evaluate as "
                "converged; the result was downgraded"
            ),
        ),
        "EFC_MATCHED_POINT_OUTSIDE_TOLERANCE": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message=(
                "the interpolated EFC matched point failed the joint "
                "statistical tolerance; the result was downgraded"
            ),
        ),
        "ELCC_BRACKET_EXPANDED": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="risk_search",
            message="the search expanded the physical upper bound",
        ),
        "ELCC_INTERPOLATION_UNRESOLVED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message="risk plateau unresolved; no point estimate was interpolated",
        ),
        "SEARCH_MONOTONIC_UNRESOLVED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message=(
                "monotonicity could not be resolved within the statistical "
                "noise; no point estimate produced"
            ),
        ),
        "SEARCH_MAX_ITERATIONS": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message="the search iteration budget was exhausted",
        ),
        "SEARCH_RISK_NOT_CONVERGED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message="the endpoint risk estimates did not converge",
        ),
        "SEARCH_RISK_RESOLUTION_UNAVAILABLE": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message="empirical risk resolution could not be computed for the final generation",
        ),
        "SEARCH_BRACKET_NOT_FORMED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message="no legal bracket crossing the target risk was formed",
        ),
        "SEARCH_EXTENSION_NO_GENERATION_CHANGE": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message=(
                "sample extension was requested but the canonical sample generation did not change"
            ),
        ),
        "ELCC_MATCHED_POINT_NOT_CONVERGED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message=(
                "the interpolated matched point did not re-evaluate as "
                "converged; the result was downgraded"
            ),
        ),
        "ELCC_MATCHED_POINT_OUTSIDE_TOLERANCE": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message=(
                "the interpolated matched point failed the joint statistical "
                "tolerance; the result was downgraded"
            ),
        ),
        # Low event count is a statistical-resolution warning.  It must never
        # be reclassified as the data-blocking error used by invalid_data.
        "RISK_EVENT_COUNT_LOW": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="risk_search",
            message="risk event count is too low for the requested resolution",
        ),
        # Reserved now because the MethodResult 1.3 contract already exposes
        # storage warnings.  The T14 producer will consume this same entry.
        "STORAGE_SOC_BURNIN_UNSTABLE": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="resource_model",
            message="storage state-of-charge burn-in did not stabilize",
        ),
        # T14 SOC initialization / dispatch disclosures (methodology V0.4
        # §6.3 / §6.4). Info = mandatory mode disclosure (no degradation);
        # warning = instability (aggregates to valid_with_warning).
        "STORAGE_SOC_FAST_WARMUP": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="resource_model",
            message=(
                "weather-cycle warm-up enabled (deterministic weather/load "
                "cycle state only; may overestimate the SOC entering the "
                "evaluation period when deficits are outage-driven; not an "
                "unconditional research-grade default)"
            ),
        ),
        "STORAGE_SOC_WARMUP_NOT_CONVERGED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="resource_model",
            message=(
                "weather-cycle warm-up did not converge within the maximum "
                "cycle count; the initial-SOC sensitivity evidence is "
                "preserved"
            ),
        ),
        "STORAGE_PERFECT_FORESIGHT": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="risk_search",
            message=(
                "perfect-foresight EUE upper bound used: a theoretical "
                "upper bound / sensitivity result, not an achievable "
                "operating strategy and never a default capacity-value "
                "conclusion"
            ),
        ),
        "SEARCH_NOT_MONOTONIC": DiagnosticSpec(
            severity=Severity.ERROR,
            category="method_error",
            group="blocking",
            message=(
                "the risk-versus-capacity relation is not monotonic in the "
                "search range; method inapplicable"
            ),
        ),
        "SEARCH_SAMPLE_GENERATION_UNSTABLE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="method_error",
            group="blocking",
            message="the sample generation switched beyond the stability bound; method aborted",
        ),
        # A post-run data-integrity failure that makes an otherwise completed
        # method result unusable.  Early import/preflight failures remain in
        # the data-validation artifact and do not create MethodResult.
        "ELCC_DATA_UNUSABLE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="blocking",
            message="post-run ELCC/EFC data integrity failed; result is unusable",
        ),
        # Round 9 upstream boundary additions.
        # scenario-input limitation: exactly one probability-baseline
        # weather year (stress_test years never count).
        "SINGLE_WEATHER_YEAR": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="scenario_input",
            message=(
                "the scenario bank carries exactly one probability-baseline "
                "weather year; no multi-year probability statement is "
                "supported for this result"
            ),
        ),
        # resource-model limitation: for_only / independent-hourly thermal
        # objects cannot support forced-outage duration conclusions.
        "THERMAL_DURATION_UNAVAILABLE": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="resource_model",
            message=(
                "independent hourly mode cannot support forced-outage "
                "duration conclusions; maximum data grade is G1 "
                "initial review"
            ),
        ),
        "STORAGE_CROSS_CHARGING_BLOCKED": DiagnosticSpec(
            severity=Severity.INFO,
            category="info",
            group="resource_model",
            message=(
                "storage cross-charging was blocked by the dispatch "
                "physical invariant (expected enforcement, audit-only)"
            ),
        ),
        # blocking method error: the DISPATCH result violates the
        # cross-charging physical invariant (a dispatch-rule violation,
        # aggregated to invalid_method; early pre-run input errors are
        # blocked before the run and never masquerade as this state).
        "STORAGE_CROSS_CHARGING_ATTEMPT": DiagnosticSpec(
            severity=Severity.ERROR,
            category="method_error",
            group="blocking",
            message=(
                "the dispatch result violates the storage cross-charging "
                "physical invariant; the method result is unusable"
            ),
        ),
        # T16 P3 resource-run boundary: DQ-24 / DQ-27 / DQ-30 codes are
        # consumed by the application resource run (never a private
        # severity mapping; blocking codes stop the run, warnings are
        # published into MethodResult / search evidence).
        "DQ24_SOC_FRACTION_RANGE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "storage SOC fractions are not strictly numeric in [0, 1] "
                "or soc_min >= soc_max (DQ-24); blocking"
            ),
        ),
        "STORAGE_SOC_INVALID": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "storage SOC bounds are invalid (soc_min_fraction >= "
                "soc_max_fraction) (DQ-24); blocking"
            ),
        ),
        "DQ24_ENERGY_CAPACITY_INVALID": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "storage energy capacity is not strictly numeric, finite "
                "and positive (DQ-24); blocking"
            ),
        ),
        "DQ24_USABLE_ENERGY_MISMATCH": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "usable_energy_mwh deviates from energy_capacity_mwh x "
                "(soc_max_fraction - soc_min_fraction) beyond the "
                "configured tolerance (DQ-24); blocking"
            ),
        ),
        "DQ27_RATE_CONSISTENCY_ONLY": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="resource_model",
            message=(
                "planned_outage_rate is consistency-only: explicit outage "
                "trajectories are present; the rate is never applied on "
                "top of them (DQ-27)"
            ),
        ),
        "DQ27_DOUBLE_COUNT": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="blocking",
            message=(
                "runtime planned-outage double count: the rate is applied "
                "onto capacity that already carries outage effects "
                "(DQ-27); blocking"
            ),
        ),
        "DQ27_INVALID_RATE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=("planned_outage_rate is not a finite number in [0, 1] (DQ-27); blocking"),
        ),
        "DQ27_INVALID_FLAG": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message="DQ-27 decision flags must be strict bools; blocking",
        ),
        "DQ27_ORPHAN_OUTAGE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=("planned_outages resource_id has no matching thermal unit (DQ-27); blocking"),
        ),
        "DQ27_INVALID_RESOURCE_ID": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "planned_outages.resource_id must be a non-empty builtin string (DQ-27); blocking"
            ),
        ),
        "DQ30_MISSING_ALIGNMENT": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "all_weather_years intertie profile requires an explicit "
                "profile_alignment (DQ-30); blocking"
            ),
        ),
        "DQ30_INVALID_ALIGNMENT": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=("profile_alignment must be hour_of_year or month_day_hour (DQ-30); blocking"),
        ),
        "DQ30_INVALID_LEAP_RULE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message="unknown leap_day_rule (DQ-30); blocking",
        ),
        "DQ30_DUPLICATE_MAPPING": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=("conflicting cross-year mapping tuples for one intertie_id (DQ-30); blocking"),
        ),
        "DQ30_INVALID_WEATHER_YEAR": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "invalid weather_year for the profile scope; the string "
                "'ALL' is never accepted (DQ-30); blocking"
            ),
        ),
        "DQ30_INVALID_TARGET_YEAR": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message="target_year must be a strict integer in 1900..2200 (DQ-30); blocking",
        ),
        "DQ30_MISSING_SCOPE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message="profile_scope is required per mapping row (DQ-30); blocking",
        ),
        "DQ30_INVALID_SCOPE": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message=(
                "profile_scope must be specific_weather_year or all_weather_years (DQ-30); blocking"
            ),
        ),
        "DQ30_MISSING_INTERTIE_ID": DiagnosticSpec(
            severity=Severity.ERROR,
            category="data_error",
            group="scenario_input",
            message="intertie_id must be a non-empty string (DQ-30); blocking",
        ),
        # intertie run-time disclosures (T15 validated-plan behaviour).
        "INTERTIE_PLAN_ZEROED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="resource_model",
            message=(
                "an intertie channel was unavailable and its scheduled "
                "exchange was zeroed by the availability policy"
            ),
        ),
        "INTERTIE_EXTRA_IMPORT_CAPPED": DiagnosticSpec(
            severity=Severity.WARNING,
            category="warning",
            group="resource_model",
            message=("candidate extra import was capped by the remaining available import room"),
        ),
    }
)


def diagnostic_spec(code: str) -> DiagnosticSpec:
    """Return the immutable spec for a known code or fail closed."""
    spec = ELCC_DIAGNOSTIC_REGISTRY.get(code)
    if spec is None:
        raise ElccDiagnosticsError(
            "ELCC_DIAGNOSTIC_UNKNOWN",
            f"unknown ELCC diagnostic code {code!r}",
        )
    return spec


def validate_diagnostic_classification(code: str, severity: Severity) -> DiagnosticSpec:
    """Reject unknown codes and caller-controlled severity reclassification."""
    spec = diagnostic_spec(code)
    if severity is not spec.severity:
        raise ElccDiagnosticsError(
            "ELCC_DIAGNOSTIC_CLASSIFICATION_MISMATCH",
            (
                f"ELCC diagnostic {code!r} requires severity={spec.severity.value}; "
                f"received {severity.value}"
            ),
        )
    return spec


__all__ = [
    "ELCC_DIAGNOSTIC_REGISTRY",
    "DiagnosticCategory",
    "DiagnosticGroup",
    "DiagnosticSpec",
    "ElccDiagnosticsError",
    "diagnostic_spec",
    "validate_diagnostic_classification",
]
