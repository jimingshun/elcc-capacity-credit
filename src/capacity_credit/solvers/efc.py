"""EFC (equivalent firm capacity) solver adapter (T17).

Method definition (methodology V0.4 §9): find the perfect reliable
capacity F such that

    R(S+F, L) = R(S+X, L)

System A is the candidate system S+X; system B is the perfect-reliable
replacement S+F. F is the search variable. Both systems use the SAME
load, shared resources, weather years, scenario bank, sample IDs and
common random streams; the candidate resource's special state exists
only in system A and the perfect capacity only in system B.

Perfect capacity contract (§9.2):

- hourly availability fraction = 1;
- no planned maintenance, no forced outages;
- no energy / fuel / SOC / duration limits;
- weather-independent;
- output responds to deficits, never exceeds the hourly deficit and
  never creates negative unserved energy;
- never faked through load changes, planned exchange, shared-resource
  state or random streams.

This module is a THIN adapter over the existing generic equal-risk
search (``equal_risk.search_equal_risk``) and the paired-evaluation
layer (``paired_evaluation``). The trial provider (how S+F is
evaluated) is supplied by the caller exactly like the ELCC adapter.
The search variable is F (MW); risk DECREASES as F increases, so the
search MUST run with ``monotonic_direction="risk_decreases_with_capacity"``
- the ELCC increasing-load branch is never reused.

The adapter preserves: the perfect-capacity definition version, the
candidate risk, every F search-point's replacement risk, the bracket
endpoints, the interpolated result, the risk difference and statistical
error, the monotonic direction, the upper-bound source, the full search
trace and the common random / object identities. Touching the declared
physical upper bound emits a controlled diagnostic
(``EFC_UPPER_BOUND_TOUCHED``) - the bound is never silently treated as
a trusted point value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np

from capacity_credit.contracts.enums import (
    InterpolationMethod,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    SearchConvergence,
)
from capacity_credit.solvers.equal_risk import (
    BoundDecision,
    EqualRiskPointEvaluation,
    EqualRiskSearchConfig,
    EqualRiskSearchError,
    EqualRiskSearchResult,
    MonotonicityCheck,
    SearchPoint,
    search_equal_risk,
)
from capacity_credit.solvers.paired_evaluation import (
    PairedEvaluationError,
    PairedEvaluationResult,
    RiskSampleSeries,
)

_DECREASING = "risk_decreases_with_capacity"

DIAGNOSTIC_CI_NOT_AVAILABLE = "EFC_CI_NOT_AVAILABLE"
DIAGNOSTIC_UPPER_BOUND_TOUCHED = "EFC_UPPER_BOUND_TOUCHED"
DIAGNOSTIC_MATCHED_POINT_NOT_CONVERGED = "EFC_MATCHED_POINT_NOT_CONVERGED"
DIAGNOSTIC_MATCHED_POINT_OUTSIDE_TOLERANCE = "EFC_MATCHED_POINT_OUTSIDE_TOLERANCE"


class EfcConfigError(ValueError):
    """Stable EFC adapter error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EfcSolverConfig:
    """EFC adapter configuration.

    ``search`` is the underlying equal-risk search config with
    ``monotonic_direction="risk_decreases_with_capacity"`` (enforced:
    ELCC's increasing-load branch must never be reused). ``upper_bound``
    fields declare the physical upper bound for the search variable:
    storage max discharge power / nameplate capacity / the sum of max
    instantaneous outputs. ``perfect_capacity_definition_version``
    identifies the frozen perfect-capacity definition used by the
    trial provider.
    """

    search: EqualRiskSearchConfig
    exogenous_invariant_fingerprint: str = "efc_chain_v1"
    portfolio_id: str = "PORT_EFC"
    perfect_capacity_definition_version: str = "perfect_v1"
    upper_bound_source: str = "declared_max_instantaneous_output"
    upper_bound_mw: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.search, EqualRiskSearchConfig):
            raise EfcConfigError(
                "EFC_INVALID_CONFIG",
                "search must be an EqualRiskSearchConfig",
            )
        if self.search.monotonic_direction != _DECREASING:
            raise EfcConfigError(
                "EFC_INVALID_SEARCH_DIRECTION",
                "EFC risk decreases with capacity: search.monotonic_direction "
                f"must be {_DECREASING!r}, got {self.search.monotonic_direction!r}",
            )
        if (
            not isinstance(self.exogenous_invariant_fingerprint, str)
            or not self.exogenous_invariant_fingerprint.strip()
        ):
            raise EfcConfigError(
                "EFC_INVALID_CONFIG",
                "exogenous_invariant_fingerprint must be non-blank",
            )
        if not isinstance(self.portfolio_id, str) or not self.portfolio_id.strip():
            raise EfcConfigError("EFC_INVALID_CONFIG", "portfolio_id must be non-blank")
        if (
            not isinstance(self.perfect_capacity_definition_version, str)
            or not self.perfect_capacity_definition_version.strip()
        ):
            raise EfcConfigError(
                "EFC_INVALID_CONFIG",
                "perfect_capacity_definition_version must be non-blank",
            )
        if not isinstance(self.upper_bound_source, str) or not self.upper_bound_source.strip():
            raise EfcConfigError("EFC_INVALID_CONFIG", "upper_bound_source must be non-blank")
        if self.upper_bound_mw is not None:
            if (
                isinstance(self.upper_bound_mw, bool)
                or not isinstance(self.upper_bound_mw, (int, float, np.integer, np.floating))
                or not np.isfinite(float(self.upper_bound_mw))
                or float(self.upper_bound_mw) <= 0.0
            ):
                raise EfcConfigError(
                    "EFC_INVALID_CONFIG",
                    "upper_bound_mw must be a positive finite number when set",
                )
            object.__setattr__(self, "upper_bound_mw", float(self.upper_bound_mw))
        else:
            object.__setattr__(self, "upper_bound_mw", float(self.search.physical_upper_bound_mw))


@dataclass(frozen=True)
class EfcTrialOutcome:
    """One perfect-replacement trial outcome (application layer).

    ``paired_result`` pairs the S+F sample series against the FROZEN
    candidate series R(S+X, L); the convergence snapshot fields come
    from the S+F run and are bound into the search point.
    """

    paired_result: PairedEvaluationResult
    risk_metrics: Mapping[str, float]
    risk_convergence: RiskConvergence
    convergence_criteria: Mapping[str, bool]
    should_stop: bool
    recent_batch_evidence: tuple[float, ...]
    sample_set_identity: str
    config_identity: str


EfcTrialProvider = Callable[[float], EfcTrialOutcome]


@dataclass(frozen=True)
class EfcResult:
    """Solver-internal EFC result (formal MethodResult mapping is the
    application layer's job).

    Risk-role semantics (frozen):

    - candidate_risk = R(S+X, L)  - the FROZEN candidate target (the
      search reference series mean); candidate_metrics are the SAME
      candidate evaluation's 4-metric aggregate (never the F=0 trial);
    - matched_risk   = R(S+F*, L) - REAL re-evaluation at the matched
      perfect capacity F* (only when valid: the matched point itself is
      CONVERGED with all four criteria and should_stop=True, and the
      paired difference satisfies the joint tolerance);
    - risk_difference = matched - candidate (T10 sign).
    """

    estimate_mw: float | None
    bracket_low_mw: float
    bracket_high_mw: float
    low_endpoint_mw: float
    high_endpoint_mw: float
    low_endpoint_risk: float
    high_endpoint_risk: float
    candidate_risk: float
    matched_risk: float | None
    risk_difference: float | None
    candidate_metrics: Mapping[str, float] | None
    matched_metrics: Mapping[str, float] | None
    primary_risk_metric: RiskMetric
    perfect_capacity_definition_version: str
    upper_bound_source: str
    upper_bound_mw: float
    touched_upper_bound: bool
    interpolation_method: InterpolationMethod
    empirical_risk_resolution: float | None
    risk_convergence: RiskConvergence
    search_convergence: SearchConvergence
    result_status: ResultStatus
    sample_count: int
    base_risk: float | None = None
    lower_ci: float | None = None
    upper_ci: float | None = None
    diagnostics: tuple[str, ...] = ()
    trace: tuple[SearchPoint, ...] = ()
    bound_decisions: tuple[BoundDecision, ...] = ()
    monotonicity_ledger: tuple[MonotonicityCheck, ...] = ()


def reference_risk_from(reference: RiskSampleSeries) -> float:
    """Candidate target risk scalar R(S+X, L): the frozen reference mean."""
    return float(np.mean(list(reference.values.values())))


def _primary_scalar_or_fallback(
    metrics: Mapping[str, float] | None,
    primary_risk_metric: RiskMetric,
    reference: RiskSampleSeries,
    paired_mean: float,
) -> float:
    """Primary risk scalar from the 4-metric aggregate; falls back to
    reference + paired mean when the aggregate lacks the primary key."""
    if metrics is not None:
        scalar = metrics.get(primary_risk_metric.value)
        if scalar is not None:
            return float(scalar)
    return reference_risk_from(reference) + paired_mean


def _build_point(
    config: EfcSolverConfig,
    reference: RiskSampleSeries,
    capacity_mw: float,
    outcome: EfcTrialOutcome,
) -> EqualRiskPointEvaluation:
    return EqualRiskPointEvaluation(
        paired_result=outcome.paired_result,
        risk_convergence=outcome.risk_convergence,
        convergence_criteria=outcome.convergence_criteria,
        exogenous_invariant_fingerprint=config.exogenous_invariant_fingerprint,
        sample_set_identity=outcome.sample_set_identity,
        recent_batch_evidence=outcome.recent_batch_evidence,
        should_stop=outcome.should_stop,
        config_identity=outcome.config_identity,
    )


def _joint_tolerance_violated(
    mean: float,
    standard_error: float,
    confidence_level: float,
) -> bool:
    """Joint statistical tolerance at the matched point: the re-evaluated
    paired difference must be consistent with zero at the configured
    confidence level (|mean| <= z * SE; exact zero when SE == 0)."""
    if standard_error <= 0.0:
        return abs(mean) > 1e-12
    from scipy import stats as sp_stats

    critical = float(sp_stats.norm.ppf(0.5 + confidence_level / 2.0))
    return abs(mean) > critical * standard_error


def _matched_point_fully_converged(outcome: EfcTrialOutcome) -> bool:
    """The matched point is only an actual matched point when the S+F
    evaluation itself reports CONVERGED with all four criteria and
    should_stop=True (T11 snapshot contract; never guessed)."""
    if outcome.risk_convergence is not RiskConvergence.CONVERGED:
        return False
    if not outcome.should_stop:
        return False
    if not isinstance(outcome.convergence_criteria, Mapping):
        return False
    return all(
        isinstance(value, bool) and value for value in outcome.convergence_criteria.values()
    ) and set(outcome.convergence_criteria) == {
        "min_samples_met",
        "precision_met",
        "nonzero_samples_met",
        "recent_batches_stable",
    }


def _downgrade_matched_point(
    diagnostics: list[str],
    code: str,
    risk_convergence: RiskConvergence,
) -> tuple[
    None,
    None,
    None,
    None,
    ResultStatus,
    SearchConvergence,
    RiskConvergence,
]:
    """Shared downgrade: the interpolated F* does NOT actually match the
    candidate risk -> non-valid bracket, all result scalars nulled,
    statuses kept consistent with the closed status matrix."""
    diagnostics.append(code)
    status = (
        ResultStatus.LOW_RESOLUTION
        if risk_convergence is RiskConvergence.LOW_EVENT_COUNT
        else ResultStatus.NOT_CONVERGED
    )
    return (
        None,
        None,
        None,
        None,
        status,
        SearchConvergence.BRACKET_ONLY,
        risk_convergence,
    )


def _reevaluate_matched_point(
    *,
    estimate_mw: float,
    config: EfcSolverConfig,
    cached_provider: Callable[[float], EfcTrialOutcome],
    primary_risk_metric: RiskMetric,
    reference: RiskSampleSeries,
    diagnostics: list[str],
) -> tuple[
    float | None,
    float | None,
    Mapping[str, float] | None,
    float | None,
    ResultStatus,
    SearchConvergence,
    RiskConvergence,
]:
    """REAL re-evaluation of the interpolated matched capacity F*.

    The matched point is only valid when the S+F evaluation at F*
    reports CONVERGED (all four criteria + should_stop) and the paired
    difference satisfies the joint statistical tolerance; otherwise the
    result is downgraded to a non-valid bracket with a diagnostic.
    """
    outcome = cached_provider(estimate_mw)
    stats = outcome.paired_result.stats
    matched_risk = _primary_scalar_or_fallback(
        outcome.risk_metrics,
        primary_risk_metric,
        reference,
        stats.mean,
    )
    if not _matched_point_fully_converged(outcome):
        return _downgrade_matched_point(
            diagnostics,
            DIAGNOSTIC_MATCHED_POINT_NOT_CONVERGED,
            outcome.risk_convergence,
        )
    if _joint_tolerance_violated(
        stats.mean,
        stats.standard_error,
        config.search.confidence_level,
    ):
        return _downgrade_matched_point(
            diagnostics,
            DIAGNOSTIC_MATCHED_POINT_OUTSIDE_TOLERANCE,
            outcome.risk_convergence,
        )
    return (
        estimate_mw,
        matched_risk,
        dict(outcome.risk_metrics),
        stats.mean,
        ResultStatus.VALID,
        SearchConvergence.CONVERGED,
        RiskConvergence.CONVERGED,
    )


def _validate_entry(
    reference: RiskSampleSeries,
    trial_provider: EfcTrialProvider,
    request_more_samples: Callable[[], bool] | None,
    primary_risk_metric: RiskMetric,
) -> None:
    if not isinstance(reference, RiskSampleSeries):
        raise EfcConfigError("EFC_INVALID_REFERENCE", "reference must be a RiskSampleSeries")
    if not callable(trial_provider):
        raise EfcConfigError("EFC_INVALID_PROVIDER", "trial_provider must be callable")
    if request_more_samples is not None and not callable(request_more_samples):
        raise EfcConfigError(
            "EFC_INVALID_CALLBACK",
            "request_more_samples must be callable or None",
        )
    if not isinstance(primary_risk_metric, RiskMetric):
        raise EfcConfigError("EFC_INVALID_METRIC", "primary_risk_metric must be a RiskMetric")


def _validate_result_consistency(result: EfcResult) -> None:
    """Internal consistency gates before the result leaves the solver:
    a valid result carries the matched set; a non-valid result carries
    no estimate; the touched-upper-bound flag agrees with the
    estimate."""
    if result.result_status in (ResultStatus.VALID, ResultStatus.VALID_WITH_WARNING):
        if result.estimate_mw is None:
            raise EfcConfigError(
                "EFC_INVALID_RESULT",
                "a valid EFC result must carry estimate_mw",
            )
        if result.matched_risk is None or result.matched_metrics is None:
            raise EfcConfigError(
                "EFC_INVALID_RESULT",
                "a valid EFC result must carry matched risk and metrics",
            )
        if result.risk_difference is None:
            raise EfcConfigError(
                "EFC_INVALID_RESULT",
                "a valid EFC result must carry risk_difference",
            )
    else:
        if result.estimate_mw is not None:
            raise EfcConfigError(
                "EFC_INVALID_RESULT",
                "a non-valid EFC result must not carry estimate_mw",
            )
    if result.touched_upper_bound and result.estimate_mw is None:
        raise EfcConfigError(
            "EFC_INVALID_RESULT",
            "touched_upper_bound requires an estimate",
        )


def run_efc(
    *,
    config: EfcSolverConfig,
    reference: RiskSampleSeries,
    trial_provider: EfcTrialProvider,
    request_more_samples: Callable[[], bool] | None = None,
    primary_risk_metric: RiskMetric = RiskMetric.LOLE_DAYS_PER_YEAR,
    base_risk: float | None = None,
    candidate_metrics: Mapping[str, float] | None = None,
) -> EfcResult:
    """Run the EFC search over the given perfect-replacement provider.

    Risk-role semantics (frozen):

    - candidate risk = R(S+X, L): the frozen reference (target) series
      mean - NEVER the base risk and NEVER the matched point;
    - matched risk = R(S+F*, L): when the search is valid, the matched
      perfect capacity F* is re-evaluated for real and must be itself
      CONVERGED and satisfy the joint statistical tolerance; otherwise
      the result is downgraded to a non-valid bracket;
    - risk difference = matched - candidate.

    The search runs with monotonic_direction=risk_decreases_with_capacity
    (enforced by EfcSolverConfig). Touching the declared physical upper
    bound emits EFC_UPPER_BOUND_TOUCHED and never silently turns the
    bound into a trusted point value.
    """
    _validate_entry(reference, trial_provider, request_more_samples, primary_risk_metric)
    cache: dict[float, EfcTrialOutcome] = {}

    def cached_provider(capacity_mw: float) -> EfcTrialOutcome:
        capacity = float(capacity_mw)
        outcome = cache.get(capacity)
        if outcome is None:
            try:
                outcome = trial_provider(capacity)
            except PairedEvaluationError as exc:
                raise EfcConfigError(
                    exc.code if exc.code else "EFC_PAIRED_IDENTITY_MISMATCH",
                    exc.message,
                ) from exc
            if not isinstance(outcome, EfcTrialOutcome):
                raise EfcConfigError(
                    "EFC_INVALID_PROVIDER",
                    "trial_provider must return an EfcTrialOutcome",
                )
            cache[capacity] = outcome
        return outcome

    def evaluator(capacity_mw: float) -> EqualRiskPointEvaluation:
        return _build_point(config, reference, capacity_mw, cached_provider(capacity_mw))

    candidate_risk = reference_risk_from(reference)

    try:
        search_result: EqualRiskSearchResult = search_equal_risk(
            config=config.search,
            reference=reference,
            evaluator=evaluator,
            request_more_samples=request_more_samples,
        )
    except EqualRiskSearchError as exc:
        raise EfcConfigError(
            exc.code if exc.code else "EFC_SEARCH_FAILED",
            exc.message,
        ) from exc

    matched_risk: float | None = None
    matched_metrics: Mapping[str, float] | None = None
    risk_difference: float | None = None
    estimate_mw = search_result.estimate_mw
    result_status = search_result.result_status
    search_convergence = search_result.search_convergence
    risk_convergence = search_result.risk_convergence
    diagnostics = list(search_result.diagnostics)

    if estimate_mw is not None and search_result.result_status is ResultStatus.VALID:
        (
            estimate_mw,
            matched_risk,
            matched_metrics,
            risk_difference,
            result_status,
            search_convergence,
            risk_convergence,
        ) = _reevaluate_matched_point(
            estimate_mw=estimate_mw,
            config=config,
            cached_provider=cached_provider,
            primary_risk_metric=primary_risk_metric,
            reference=reference,
            diagnostics=diagnostics,
        )

    upper_bound_mw = float(config.upper_bound_mw or config.search.physical_upper_bound_mw)
    tolerance = config.search.capacity_tolerance_mw
    touched_upper_bound = estimate_mw is not None and estimate_mw >= upper_bound_mw - tolerance
    if touched_upper_bound:
        diagnostics.append(DIAGNOSTIC_UPPER_BOUND_TOUCHED)
    diagnostics.append(DIAGNOSTIC_CI_NOT_AVAILABLE)

    result = EfcResult(
        estimate_mw=estimate_mw,
        bracket_low_mw=search_result.bracket_low_mw,
        bracket_high_mw=search_result.bracket_high_mw,
        low_endpoint_mw=search_result.low_endpoint_mw,
        high_endpoint_mw=search_result.high_endpoint_mw,
        low_endpoint_risk=search_result.low_endpoint_risk,
        high_endpoint_risk=search_result.high_endpoint_risk,
        candidate_risk=candidate_risk,
        base_risk=base_risk,
        matched_risk=matched_risk,
        risk_difference=risk_difference,
        candidate_metrics=dict(candidate_metrics) if candidate_metrics else None,
        matched_metrics=dict(matched_metrics) if matched_metrics else None,
        primary_risk_metric=primary_risk_metric,
        perfect_capacity_definition_version=config.perfect_capacity_definition_version,
        upper_bound_source=config.upper_bound_source,
        upper_bound_mw=upper_bound_mw,
        touched_upper_bound=touched_upper_bound,
        interpolation_method=(
            InterpolationMethod.NOT_APPLICABLE
            if result_status is not ResultStatus.VALID
            else search_result.interpolation_method
        ),
        empirical_risk_resolution=search_result.empirical_risk_resolution,
        risk_convergence=risk_convergence,
        search_convergence=search_convergence,
        result_status=result_status,
        sample_count=len(reference.sample_ids),
        diagnostics=tuple(diagnostics),
        trace=search_result.trace,
        bound_decisions=search_result.bound_decisions,
        monotonicity_ledger=search_result.monotonicity_ledger,
    )
    _validate_result_consistency(result)
    return result


__all__ = [
    "DIAGNOSTIC_CI_NOT_AVAILABLE",
    "DIAGNOSTIC_MATCHED_POINT_NOT_CONVERGED",
    "DIAGNOSTIC_MATCHED_POINT_OUTSIDE_TOLERANCE",
    "DIAGNOSTIC_UPPER_BOUND_TOUCHED",
    "EfcConfigError",
    "EfcResult",
    "EfcSolverConfig",
    "EfcTrialOutcome",
    "EfcTrialProvider",
    "reference_risk_from",
    "run_efc",
]
