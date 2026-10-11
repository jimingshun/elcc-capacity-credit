"""Generic ELCC solver adapter (T12).

Thin ELCC adapter over T10 paired evaluation and T11 equal-risk search:
it NEVER re-implements risk formulas or a second risk kernel. The chain
closed by the application layer is:

    ScenarioBank -> AdequacyEngine -> ScenarioService
    -> RiskSampleSeries (base) -> evaluate_paired_differences
    -> search_equal_risk -> ElccResult

The adapter owns the ELCC-specific semantics:

- load-growth conversion: the search variable is the CAPACITY (MW).
  Depending on ``load_growth_mode`` the capacity is applied as
  - fixed_mw:       L' = L + capacity (ELCC == capacity);
  - scaling:        L' = (1 + a) L with a = capacity / peak(L), so the
                    reported ELCC is the PEAK load increment (never the
                    mean increment);
  - specified:      the configured shape is preserved and the scale is
                    derived from the requested capacity so the peak
                    increment equals the capacity.
- every trial evaluates the candidate system through the injected
  ``trial_provider`` (the application wires the real engine series);
  the provider returns the paired evaluation outcome + convergence
  snapshot, which become the ``EqualRiskPointEvaluation`` consumed by
  the equal-risk search;
- result semantics: base/candidate/matched risks, capacity bracket,
  capacity credit fraction (only when the reference capacity is valid
  and > 0), interpolation method, empirical risk resolution, full
  search trace, diagnostics. The methodology has NO capacity confidence
  interval algorithm in T12, so lower_ci/upper_ci stay None with an
  ELCC_CI_NOT_AVAILABLE diagnostic; the search bracket is never
  presented as a statistical confidence interval.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from capacity_credit.contracts.elcc_status import validate_status_combo
from capacity_credit.contracts.enums import (
    InterpolationMethod,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    SearchConvergence,
)
from capacity_credit.domain.portfolios import (
    GrowthError,
    LoadGrowthProfile,
    LoadGrowthResult,
    apply_fixed_mw_growth,
    apply_scaling_growth,
    apply_specified_growth,
)
from capacity_credit.solvers.equal_risk import (
    BoundDecision,
    EqualRiskPointEvaluation,
    EqualRiskSearchConfig,
    EqualRiskSearchError,
    MonotonicityCheck,
    SearchPoint,
    search_equal_risk,
)
from capacity_credit.solvers.paired_evaluation import (
    PairedEvaluationResult,
    RiskSampleSeries,
)

_GROWTH_MODES = frozenset({"fixed_mw", "scaling", "specified"})

DIAGNOSTIC_CI_NOT_AVAILABLE = "ELCC_CI_NOT_AVAILABLE"


class ElccConfigError(ValueError):
    """Stable ELCC adapter config error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require_finite_number(
    value: Any,
    field: str,
    *,
    non_negative: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ElccConfigError("ELCC_INVALID_CONFIG", f"{field} must be a real number")
    number = float(value)
    if not np.isfinite(number):
        raise ElccConfigError("ELCC_INVALID_CONFIG", f"{field} must be finite")
    if non_negative and number < 0.0:
        raise ElccConfigError("ELCC_INVALID_CONFIG", f"{field} must be non-negative")
    return number


def _validate_specified_mode(
    load_growth_mode: str,
    specified_profile: Any,
    run_time_axis_by_year: Any,
) -> None:
    """Specified-mode configuration validation (C901 split): the mode
    requires a LoadGrowthProfile AND the REAL run time axis - the
    profile axis must never validate the profile itself."""
    if load_growth_mode == "specified":
        if not isinstance(specified_profile, LoadGrowthProfile):
            raise ElccConfigError(
                "ELCC_INVALID_CONFIG",
                "specified growth requires a LoadGrowthProfile",
            )
        if not isinstance(run_time_axis_by_year, Mapping) or not run_time_axis_by_year:
            raise ElccConfigError(
                "ELCC_INVALID_RUN_AXIS",
                "specified growth requires the REAL run time axis "
                "(run_time_axis_by_year); the profile axis must never "
                "validate the profile itself",
            )
    elif specified_profile is not None:
        raise ElccConfigError(
            "ELCC_INVALID_CONFIG",
            "specified_profile is only valid for mode 'specified'",
        )


@dataclass(frozen=True)
class ElccSolverConfig:
    """ELCC adapter configuration.

    ``search`` is the underlying equal-risk search config (capacity in
    MW); ``load_growth_mode`` selects how the capacity is applied;
    ``specified_profile`` is required for mode ``specified``;
    ``reference_capacity_mw`` is the candidate nominal capacity used
    only to compute the capacity credit fraction (> 0 required when
    set); ``exogenous_invariant_fingerprint`` anchors the C-29
    invariant.

    ``run_time_axis_by_year`` (round 4) is the REAL canonical frozen
    run time axis (scenario-bank timezone NAME) used by the specified
    growth validation at solve time. It is a RUNTIME-ONLY object: it is
    never written into the persisted method configuration; specified
    mode requires it (ELCC_INVALID_RUN_AXIS otherwise) so the profile
    axis can never validate the profile itself.
    """

    search: EqualRiskSearchConfig
    load_growth_mode: str = "fixed_mw"
    specified_profile: LoadGrowthProfile | None = None
    reference_capacity_mw: float | None = None
    exogenous_invariant_fingerprint: str = "elcc_chain_v1"
    portfolio_id: str = "PORT_ELCC"
    run_time_axis_by_year: Mapping[int, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.search, EqualRiskSearchConfig):
            raise ElccConfigError(
                "ELCC_INVALID_CONFIG",
                "search must be an EqualRiskSearchConfig",
            )
        if not isinstance(self.load_growth_mode, str) or self.load_growth_mode not in _GROWTH_MODES:
            raise ElccConfigError(
                "ELCC_INVALID_CONFIG",
                f"load_growth_mode must be one of {sorted(_GROWTH_MODES)}",
            )
        _validate_specified_mode(
            self.load_growth_mode,
            self.specified_profile,
            self.run_time_axis_by_year,
        )
        if self.reference_capacity_mw is not None:
            reference = _require_finite_number(
                self.reference_capacity_mw,
                "reference_capacity_mw",
            )
            if reference <= 0.0:
                raise ElccConfigError(
                    "ELCC_INVALID_CONFIG",
                    "reference_capacity_mw must be positive when set",
                )
            object.__setattr__(self, "reference_capacity_mw", reference)
        if (
            not isinstance(self.exogenous_invariant_fingerprint, str)
            or not self.exogenous_invariant_fingerprint.strip()
        ):
            raise ElccConfigError(
                "ELCC_INVALID_CONFIG",
                "exogenous_invariant_fingerprint must be non-blank",
            )
        if not isinstance(self.portfolio_id, str) or not self.portfolio_id.strip():
            raise ElccConfigError(
                "ELCC_INVALID_CONFIG",
                "portfolio_id must be non-blank",
            )


@dataclass(frozen=True)
class ElccTrialOutcome:
    """One candidate-system trial outcome (application layer).

    ``paired_result`` pairs the candidate sample series against the
    fixed base series; the convergence snapshot fields come from the
    candidate adequacy run and are bound into the search point.
    """

    paired_result: PairedEvaluationResult
    risk_metrics: Mapping[str, float]
    risk_convergence: RiskConvergence
    convergence_criteria: Mapping[str, bool]
    should_stop: bool
    recent_batch_evidence: tuple[float, ...]
    sample_set_identity: str
    config_identity: str


ElccTrialProvider = Callable[[float], ElccTrialOutcome]


@dataclass(frozen=True)
class ElccResult:
    """Solver-internal ELCC result (formal MethodResult mapping is the
    application layer's job).

    Risk-role semantics (frozen):
    - base_risk      = R(S, L)            - reference series mean;
    - candidate_risk = R(S+X, L)          - candidate system at 0 MW load
      growth (REAL evaluation, never the base risk);
    - matched_risk   = R(S+X, L+G(C*))    - REAL re-evaluation at the
      matched capacity C* (only when valid: the matched point itself is
      CONVERGED with all four criteria and should_stop=True, and the
      paired difference satisfies the joint tolerance);
    - risk_difference = matched - base (T10 sign: candidate - reference).
    """

    estimate_mw: float | None
    bracket_low_mw: float
    bracket_high_mw: float
    low_endpoint_mw: float
    high_endpoint_mw: float
    low_endpoint_risk: float
    high_endpoint_risk: float
    base_risk: float
    candidate_risk: float | None
    matched_risk: float | None
    risk_difference: float | None
    candidate_metrics: Mapping[str, float] | None
    matched_metrics: Mapping[str, float] | None
    primary_risk_metric: RiskMetric
    capacity_credit_fraction: float | None
    interpolation_method: InterpolationMethod
    empirical_risk_resolution: float | None
    risk_convergence: RiskConvergence
    search_convergence: SearchConvergence
    result_status: ResultStatus
    sample_count: int
    load_growth_mode: str
    lower_ci: float | None = None
    upper_ci: float | None = None
    diagnostics: tuple[str, ...] = ()
    trace: tuple[SearchPoint, ...] = ()
    bound_decisions: tuple[BoundDecision, ...] = ()
    monotonicity_ledger: tuple[MonotonicityCheck, ...] = ()


def _peak_load(load_by_year: Mapping[int, Any]) -> float:
    return max(float(np.max(np.asarray(v, dtype=float))) for v in load_by_year.values())


def _validate_specified_lengths(
    profile: LoadGrowthProfile,
    base_load_by_year: Mapping[int, Any],
    run_axis: Mapping[int, Any],
) -> None:
    """Defensive solve-time length guard BEFORE the peak computation:
    shape, run-load and run-axis lengths must agree per year (the same
    domain rule apply_specified_growth enforces, checked here so no raw
    numpy broadcast error can escape)."""
    for year in profile.shape_by_year:
        shape = np.asarray(profile.shape_by_year[year], dtype=float)
        if len(shape) != len(np.asarray(base_load_by_year[year], dtype=float)):
            raise GrowthError(
                "GROWTH_AXIS_MISMATCH",
                f"profile shape length differs from the run load length in year {year}",
            )
        if year not in run_axis:
            raise GrowthError(
                "GROWTH_AXIS_MISMATCH",
                f"run time axis has no year {year}",
            )
        if len(run_axis[year]) != len(shape):
            raise GrowthError(
                "GROWTH_AXIS_MISMATCH",
                f"run time axis length differs from the profile shape length in year {year}",
            )


def growth_for_capacity(
    config: ElccSolverConfig,
    capacity_mw: float,
    base_load_by_year: Mapping[int, Any],
) -> LoadGrowthResult:
    """Convert a requested capacity (MW) into the concrete load growth
    for the configured mode. The reported peak increment always equals
    the requested capacity (fixed: delta; scaling: a = cap/peak(L);
    specified: scale derived so the shape's peak increment == cap)."""
    capacity = float(capacity_mw)
    if config.load_growth_mode == "fixed_mw":
        return apply_fixed_mw_growth(base_load_by_year, capacity)
    if config.load_growth_mode == "scaling":
        peak = _peak_load(base_load_by_year)
        if peak <= 0.0:
            raise ElccConfigError(
                "ELCC_INVALID_LOAD",
                "scaling growth requires a positive peak load",
            )
        return apply_scaling_growth(base_load_by_year, capacity / peak)
    profile = config.specified_profile
    assert profile is not None
    run_axis = config.run_time_axis_by_year
    if run_axis is None:
        raise ElccConfigError(
            "ELCC_INVALID_RUN_AXIS",
            "specified growth requires the REAL run time axis; the "
            "profile axis must never validate the profile itself",
        )
    _validate_specified_lengths(profile, base_load_by_year, run_axis)
    if profile.basis == "growth_multiplier":
        peak_term = max(
            float(
                np.max(
                    np.asarray(profile.shape_by_year[year], dtype=float)
                    * np.asarray(base_load_by_year[year], dtype=float)
                )
            )
            for year in profile.shape_by_year
        )
    else:
        peak_term = max(
            float(np.max(np.asarray(profile.shape_by_year[year], dtype=float)))
            for year in profile.shape_by_year
        )
    if peak_term <= 0.0:
        raise ElccConfigError(
            "ELCC_INVALID_PROFILE",
            "specified growth shape must have a positive peak term",
        )
    scaled = LoadGrowthProfile(
        load_growth_profile_id=profile.load_growth_profile_id,
        source_id=profile.source_id,
        data_status=profile.data_status,
        basis=profile.basis,
        scale=capacity / peak_term,
        shape_by_year=profile.shape_by_year,
        timestamps_by_year=profile.timestamps_by_year,
    )
    run_axis = config.run_time_axis_by_year
    if run_axis is None:
        raise ElccConfigError(
            "ELCC_INVALID_RUN_AXIS",
            "specified growth requires the REAL run time axis; the "
            "profile axis must never validate the profile itself",
        )
    return apply_specified_growth(base_load_by_year, run_axis, scaled)


def _build_point(
    config: ElccSolverConfig,
    reference: RiskSampleSeries,
    capacity_mw: float,
    outcome: ElccTrialOutcome,
) -> EqualRiskPointEvaluation:
    return EqualRiskPointEvaluation(
        paired_result=outcome.paired_result,
        risk_convergence=outcome.risk_convergence,
        convergence_criteria=outcome.convergence_criteria,
        exogenous_invariant_fingerprint=(config.exogenous_invariant_fingerprint),
        sample_set_identity=outcome.sample_set_identity,
        recent_batch_evidence=outcome.recent_batch_evidence,
        should_stop=outcome.should_stop,
        config_identity=outcome.config_identity,
    )


def _capacity_credit_fraction(
    estimate_mw: float | None,
    reference_capacity_mw: float | None,
) -> float | None:
    if estimate_mw is None or reference_capacity_mw is None:
        return None
    if reference_capacity_mw <= 0.0:
        return None
    return estimate_mw / reference_capacity_mw


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


def _matched_point_fully_converged(outcome: ElccTrialOutcome) -> bool:
    """The matched point is only an actual matched point when the
    candidate evaluation itself reports CONVERGED with all four T08
    criteria and should_stop=True (reuses the T11 snapshot contract;
    never guessed from strings)."""
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
    """Shared downgrade: the interpolated C* does NOT actually match the
    base risk -> non-valid bracket, all result scalars nulled, statuses
    kept consistent with the CLOSED status matrix (classifier
    semantics: LOW_EVENT_COUNT maps to result LOW_RESOLUTION, otherwise
    NOT_CONVERGED). The final risk_convergence reflects the WHOLE result
    and can therefore never stay converged after a matched-point
    failure."""
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
    config: ElccSolverConfig,
    cached_provider: Callable[[float], ElccTrialOutcome],
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
    """REAL re-evaluation of the interpolated matched capacity C*.

    A valid ELCC result requires the matched point itself to be a real
    matched point: the candidate evaluation at C* must be CONVERGED with
    all four criteria, should_stop=True, and the paired risk difference
    must satisfy the joint tolerance. Any failure downgrades the result
    to a non-valid bracket (estimate null, matched risk null, statuses
    consistent, diagnostics recorded) AND returns the final
    risk_convergence reflecting the whole result (never converged)."""
    outcome = cached_provider(float(estimate_mw))
    matched_metrics = outcome.risk_metrics or None
    matched_diff = outcome.paired_result.stats.mean
    matched_se = outcome.paired_result.stats.standard_error
    if not _matched_point_fully_converged(outcome):
        final_risk = (
            outcome.risk_convergence
            if outcome.risk_convergence is not RiskConvergence.CONVERGED
            else RiskConvergence.NOT_CONVERGED
        )
        return _downgrade_matched_point(
            diagnostics,
            "ELCC_MATCHED_POINT_NOT_CONVERGED",
            final_risk,
        )
    if _joint_tolerance_violated(matched_diff, matched_se, config.search.confidence_level):
        return _downgrade_matched_point(
            diagnostics,
            "ELCC_MATCHED_POINT_OUTSIDE_TOLERANCE",
            RiskConvergence.NOT_CONVERGED,
        )
    matched_risk = _primary_scalar_or_fallback(
        matched_metrics,
        primary_risk_metric,
        reference,
        matched_diff,
    )
    return (
        estimate_mw,
        matched_risk,
        matched_metrics,
        matched_diff,
        ResultStatus.VALID,
        SearchConvergence.CONVERGED,
        RiskConvergence.CONVERGED,
    )


def _validate_result_consistency(result: ElccResult) -> None:
    """Result-state consistency invariant (internal guard): uses the
    UNIFIED status rule (shared with the search classifier and the
    trace artifact); a VALID result must carry the full matched-point
    result set; any other status must carry none of the matched scalars
    and NOT_APPLICABLE interpolation. A matched-point failure must never
    leave risk_convergence=converged."""
    validate_status_combo(
        risk_convergence=result.risk_convergence,
        search_convergence=result.search_convergence,
        result_status=result.result_status,
    )
    valid = result.result_status is ResultStatus.VALID
    if valid:
        violations = [
            result.estimate_mw is None,
            result.matched_risk is None,
            result.risk_difference is None,
            result.interpolation_method is InterpolationMethod.NOT_APPLICABLE,
        ]
    else:
        violations = [
            result.estimate_mw is not None,
            result.matched_risk is not None,
            result.risk_difference is not None,
            result.capacity_credit_fraction is not None,
            result.interpolation_method is not InterpolationMethod.NOT_APPLICABLE,
        ]
    if any(violations):
        raise ElccConfigError(
            "ELCC_INVALID_RESULT_STATE",
            "ELCC result status contradicts its scalars/statuses",
        )


def _validate_entry(
    reference: Any,
    trial_provider: Any,
    request_more_samples: Any,
    primary_risk_metric: Any,
) -> None:
    """Entry validation: reference/metric types and callables."""
    if not isinstance(reference, RiskSampleSeries):
        raise ElccConfigError("ELCC_INVALID_REFERENCE", "reference must be a RiskSampleSeries")
    if not isinstance(primary_risk_metric, RiskMetric):
        raise ElccConfigError("ELCC_INVALID_METRIC", "primary_risk_metric must be a RiskMetric")
    if not callable(trial_provider):
        raise ElccConfigError("ELCC_INVALID_PROVIDER", "trial_provider must be callable")
    if request_more_samples is not None and not callable(request_more_samples):
        raise ElccConfigError(
            "ELCC_INVALID_CALLBACK",
            "request_more_samples must be callable or None",
        )


def run_elcc(
    *,
    config: ElccSolverConfig,
    reference: RiskSampleSeries,
    base_load_by_year: Mapping[int, Any],
    trial_provider: ElccTrialProvider,
    request_more_samples: Callable[[], bool] | None = None,
    primary_risk_metric: RiskMetric = RiskMetric.LOLE_DAYS_PER_YEAR,
) -> ElccResult:
    """Run the ELCC search over the given candidate trial provider.

    Risk-role semantics (frozen):

    - candidate risk = R(S+X, L): the candidate system at 0 MW load
      growth is evaluated explicitly and cached - it is NEVER the base
      risk and NEVER the matched point;
    - matched risk = R(S+X, L+G(C*)): when the search is valid, the
      matched capacity C* is re-evaluated for real and must be itself
      CONVERGED (all four criteria + should_stop) and satisfy the joint
      statistical tolerance; otherwise the result is downgraded to a
      non-valid bracket with diagnostics and the FINAL risk_convergence
      reflects the whole result (never converged after a matched-point
      failure);
    - risk difference = matched - base (T10 sign: candidate-reference
      at the matched point).

    lower_ci / upper_ci stay None (no capacity CI algorithm in T12) and
    ELCC_CI_NOT_AVAILABLE is always recorded. Caching reuses real
    evaluations at 0 MW / endpoints / the matched point; it never skips
    a required evaluation.
    """
    _validate_entry(reference, trial_provider, request_more_samples, primary_risk_metric)
    cache: dict[float, ElccTrialOutcome] = {}

    def cached_provider(capacity_mw: float) -> ElccTrialOutcome:
        capacity = float(capacity_mw)
        outcome = cache.get(capacity)
        if outcome is None:
            outcome = trial_provider(capacity)
            if not isinstance(outcome, ElccTrialOutcome):
                raise ElccConfigError(
                    "ELCC_INVALID_PROVIDER",
                    "trial_provider must return an ElccTrialOutcome",
                )
            cache[capacity] = outcome
        return outcome

    def evaluator(capacity_mw: float) -> EqualRiskPointEvaluation:
        return _build_point(config, reference, capacity_mw, cached_provider(capacity_mw))

    # candidate risk: REAL evaluation of the candidate system at 0 MW
    candidate_outcome = cached_provider(0.0)
    candidate_metrics = candidate_outcome.risk_metrics or None
    candidate_risk = _primary_scalar_or_fallback(
        candidate_metrics,
        primary_risk_metric,
        reference,
        candidate_outcome.paired_result.stats.mean,
    )

    try:
        search_result = search_equal_risk(
            config=config.search,
            reference=reference,
            evaluator=evaluator,
            request_more_samples=request_more_samples,
        )
    except EqualRiskSearchError as exc:
        raise ElccConfigError(
            exc.code if exc.code else "ELCC_SEARCH_FAILED",
            exc.message,
        ) from exc

    base_risk = float(np.mean(list(reference.values.values())))
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

    diagnostics.append(DIAGNOSTIC_CI_NOT_AVAILABLE)

    result = ElccResult(
        estimate_mw=estimate_mw,
        bracket_low_mw=search_result.bracket_low_mw,
        bracket_high_mw=search_result.bracket_high_mw,
        low_endpoint_mw=search_result.low_endpoint_mw,
        high_endpoint_mw=search_result.high_endpoint_mw,
        low_endpoint_risk=search_result.low_endpoint_risk,
        high_endpoint_risk=search_result.high_endpoint_risk,
        base_risk=base_risk,
        candidate_risk=candidate_risk,
        matched_risk=matched_risk,
        risk_difference=risk_difference,
        candidate_metrics=candidate_outcome.risk_metrics or None,
        matched_metrics=matched_metrics,
        primary_risk_metric=primary_risk_metric,
        capacity_credit_fraction=_capacity_credit_fraction(
            estimate_mw,
            config.reference_capacity_mw,
        ),
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
        load_growth_mode=config.load_growth_mode,
        diagnostics=tuple(diagnostics),
        trace=search_result.trace,
        bound_decisions=search_result.bound_decisions,
        monotonicity_ledger=search_result.monotonicity_ledger,
    )
    _validate_result_consistency(result)
    return result


def reference_risk_from(reference: RiskSampleSeries) -> float:
    """Reference (base) system primary risk scalar."""
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


__all__ = [
    "DIAGNOSTIC_CI_NOT_AVAILABLE",
    "ElccConfigError",
    "ElccResult",
    "ElccSolverConfig",
    "ElccTrialOutcome",
    "ElccTrialProvider",
    "growth_for_capacity",
    "run_elcc",
]
