"""MRI/rMRI finite-difference solver adapter (T18, remediation round-2).

Thin, pure-function MRI solver over the existing T10 paired-evaluation
layer and risk kernel. It NEVER re-implements risk formulas or a second
kernel: the application layer injects a trial provider that maps a
perturbation vector to a ``RiskSampleSeries`` for the perturbed system
(the SAME scenario bank / CRN identity as the frozen reference).

Semantics (methodology V0.8 §10, §16A; T18 second-round remediation):

- forward:  MRI = (R(θ) - R(θ+ε v)) / ε
- central:  MRI = (R(θ-ε v) - R(θ+ε v)) / (2ε)
  (paired risk difference uses the fixed T10 sign convention;
   MRI is positive when the perturbation lowers risk);
- step audit: step_multipliers default [0.5, 1.0, 2.0]; every step
  reuses the SAME scenario bank and CRN identity; the per-sample paired
  difference set is the evidence (never independent means);
- EVERY 0.5/1/2 step is validated against ``physical_domain``: a positive
  step above ``max_positive_step`` or a central negative step above
  ``max_negative_step`` is an explicit-mode invalid_method
  (MRI_PHYSICAL_DOMAIN_VIOLATION) - never a silent computation/trim;
- adaptive base step uses the RUN-CONFIG ``engineering_tolerance``
  (capacity_mw / energy_mwh), the real candidate capacity / energy and
  the real peak load (methodology §10.3) - no solver-internal 1e-3
  tolerance constant substitutes for configuration tolerance;
- requested central on an rMRI run is UNIFIED to forward for the WHOLE
  run (target AND perfect-capacity denominator) because the perfect
  capacity S+F has no feasible negative side; MRI_DIFFERENCE_SCHEME_FALLBACK
  is recorded - target-central / denominator-forward / no-diagnostic /
  stable is structurally impossible;
- the perfect-capacity denominator runs the SAME 0.5/1/2 audit AND the
  SAME joint-CI / local-monotonic / step-sensitive / scheme-consistency
  checks as the target; denominator_relative_se,
  step-consistency and scheme-consistency jointly decide rmri_status;
- central-difference standard error is computed from the minus/plus
  per-sample paired difference divided by 2ε; the only fallback trigger
  is an explicit MRI_PHYSICAL_DOMAIN_VIOLATION (MriConfigError) - a
  RuntimeError / run-pipeline error / config / data / scenario error
  always propagates with its original category;
- ``risk_convergence`` is NOT hard-coded: it is computed through the
  SHARED T08 convergence judgment (``engine.convergence.check_convergence``)
  wrapped in the shared ``RiskConvergenceSnapshot`` - no second
  MRI-specific formula or threshold; when the evidence does not converge
  (or event count is low) a stable rMRI is never published;
- energy_only keeps ``mri_value`` in raw risk/MWh; the MW-equivalent is
  stored separately in ``equivalent_mri_per_mw`` and ``rmri_value`` is
  derived from it (never overwrites the raw MRI).

The solver returns an immutable ``MriResult`` (solver-internal); mapping
to method-result 2.0 is the application layer's job.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from capacity_credit.contracts.enums import (
    DifferenceScheme,
    PerturbationDimension,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    RmriStatus,
    StepMode,
)
from capacity_credit.contracts.mri_contracts import (
    MRI_DIAGNOSTIC_SEVERITY,
    CustomPerturbationComponent,
    CustomPerturbationVector,
    PerturbationVector,
    PhysicalDomain,
    StepEvaluation,
)
from capacity_credit.domain.risk_units import mri_unit
from capacity_credit.engine.online_stats import OnlineStats
from capacity_credit.solvers.paired_evaluation import (
    PairedEvaluationError,
    RiskSampleSeries,
    evaluate_paired_differences,
)

_DEFAULT_STEP_MULTIPLIERS = (0.5, 1.0, 2.0)
_PEAK_LOAD_SCALE = 1e-3

DIAGNOSTIC_RMRI_NOT_REQUESTED = "RMRI_NOT_REQUESTED"
DIAGNOSTIC_NORMALIZATION_MISSING = "RMRI_NORMALIZATION_MISSING"
DIAGNOSTIC_CUSTOM_NORMALIZATION_MISSING = "RMRI_CUSTOM_NORMALIZATION_MISSING"
DIAGNOSTIC_DENOMINATOR_UNSTABLE = "RMRI_DENOMINATOR_UNSTABLE"
DIAGNOSTIC_STEP_SENSITIVE = "MRI_STEP_SENSITIVE"
DIAGNOSTIC_LOCAL_NON_MONOTONIC = "MRI_LOCAL_NON_MONOTONIC"
DIAGNOSTIC_STRATEGY_SWITCH = "MRI_STRATEGY_SWITCH"
DIAGNOSTIC_CRN_IDENTITY_MISMATCH = "MRI_CRN_IDENTITY_MISMATCH"
DIAGNOSTIC_PHYSICAL_DOMAIN_VIOLATION = "MRI_PHYSICAL_DOMAIN_VIOLATION"
DIAGNOSTIC_DIFFERENCE_SCHEME_FALLBACK = "MRI_DIFFERENCE_SCHEME_FALLBACK"
DIAGNOSTIC_UNIT_MISMATCH = "MRI_UNIT_MISMATCH"
DIAGNOSTIC_SCENARIO_MISMATCH = "MRI_SCENARIO_MISMATCH"

# Predictable method-config incompatibilities (resource type vs
# perturbation_dimension / custom component) form an invalid_method result
# at the unified method boundary - never a pipeline failure. Only canonical
# MRI diagnostic codes ever reach the MethodResult.
_METHOD_ERROR_CODES = frozenset(
    {
        "MRI_INVALID_CONFIG",
        "MRI_RESOURCE_KIND_UNSUPPORTED",
    }
)

# Unit / scenario-bank / sample-identity conflicts remain invalid_data.
_DATA_ERROR_CODES = frozenset(
    {
        DIAGNOSTIC_SCENARIO_MISMATCH,
        DIAGNOSTIC_CRN_IDENTITY_MISMATCH,
        DIAGNOSTIC_UNIT_MISMATCH,
        "PAIRED_BANK_MISMATCH",
        "PAIRED_METRIC_MISMATCH",
        "PAIRED_SAMPLE_SET_MISMATCH",
        "PAIRED_YEAR_WEIGHTS_MISMATCH",
    }
)

_PHYSICAL_DOMAIN_CODES = frozenset({DIAGNOSTIC_PHYSICAL_DOMAIN_VIOLATION})


class MriConfigError(ValueError):
    """Stable MRI solver config error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MriSolverConfig:
    """MRI solver configuration (run-config 1.1 mirror)."""

    perturbation_dimension: PerturbationDimension
    difference_scheme: DifferenceScheme
    step_mode: StepMode
    base_step: float | None
    step_multipliers: tuple[float, ...] = _DEFAULT_STEP_MULTIPLIERS
    physical_domain: PhysicalDomain | None = None
    normalization_basis_mwh_per_mw: float | None = None
    duration_hours: float | None = None
    custom_perturbation_vector: CustomPerturbationVector | None = None
    confidence_level: float = 0.95
    run_type: str = "mri"  # "mri" | "rmri"
    denominator_relative_se_max: float | None = None
    step_consistency_rule: str = "joint_confidence_interval"
    engineering_tolerance_capacity_mw: float | None = None
    engineering_tolerance_energy_mwh: float | None = None

    def __post_init__(self) -> None:
        if self.step_mode is StepMode.EXPLICIT and (
            self.base_step is None or self.base_step <= 0.0
        ):
            raise MriConfigError(
                "MRI_INVALID_CONFIG", "explicit step_mode requires a positive base_step"
            )
        if len(self.step_multipliers) < 3 or 1.0 not in self.step_multipliers:
            raise MriConfigError(
                "MRI_INVALID_CONFIG", "step_multipliers must have >=3 unique values incl. 1.0"
            )
        if len(set(self.step_multipliers)) != len(self.step_multipliers):
            raise MriConfigError("MRI_INVALID_CONFIG", "step_multipliers must be unique")
        for multiplier in self.step_multipliers:
            if multiplier <= 0.0:
                raise MriConfigError("MRI_INVALID_CONFIG", "step_multipliers must be positive")
        if self.run_type not in ("mri", "rmri"):
            raise MriConfigError("MRI_INVALID_CONFIG", "run_type must be mri or rmri")
        if self.perturbation_dimension is PerturbationDimension.FIXED_DURATION and (
            self.duration_hours is None or self.duration_hours <= 0.0
        ):
            raise MriConfigError(
                "MRI_INVALID_CONFIG",
                "fixed_duration requires a positive duration_hours",
            )
        if self.step_consistency_rule != "joint_confidence_interval":
            raise MriConfigError(
                "MRI_INVALID_CONFIG",
                "step_consistency_rule must be joint_confidence_interval",
            )
        if not 0.0 < self.confidence_level < 1.0:
            raise MriConfigError("MRI_INVALID_CONFIG", "confidence_level must be in (0, 1)")
        if self.run_type == "rmri":
            if self.denominator_relative_se_max is None or self.denominator_relative_se_max <= 0.0:
                raise MriConfigError(
                    "MRI_INVALID_CONFIG",
                    "run_type=rmri requires a positive denominator_relative_se_max",
                )
        else:
            if self.denominator_relative_se_max is not None:
                raise MriConfigError(
                    "MRI_INVALID_CONFIG",
                    "denominator_relative_se_max must be null for run_type=mri",
                )
        if (
            self.perturbation_dimension is PerturbationDimension.CUSTOM_VECTOR
            and self.custom_perturbation_vector is None
        ):
            raise MriConfigError(
                "MRI_INVALID_CONFIG", "custom_vector requires a custom_perturbation_vector"
            )


MriTrialProvider = Callable[[PerturbationVector], RiskSampleSeries]
PerfectCapacityProvider = Callable[[float], RiskSampleSeries]


def _step_unit(config: MriSolverConfig) -> Literal["mw", "mwh"]:
    """Scalar step unit from the physical domain (or the dimension default)."""
    if config.physical_domain is not None:
        return config.physical_domain.step_unit
    if config.perturbation_dimension is PerturbationDimension.ENERGY_ONLY:
        return "mwh"
    return "mw"


def _build_perturbation_vector(config: MriSolverConfig, scalar_step: float) -> PerturbationVector:
    """Build the structured perturbation vector for one scalar step.

    - fixed_duration: delta_energy_mwh = duration_hours x delta_power_mw
      and duration_before_h == duration_after_h (duration preserved);
    - power_only: only the power capacity changes (energy capacity kept);
    - energy_only: only the energy capacity changes;
    - custom_vector: every component is scaled by the scalar step and
      carried as a structured, verifiable object with its physical unit.
    """
    if config.perturbation_dimension is PerturbationDimension.FIXED_DURATION:
        assert config.duration_hours is not None
        return PerturbationVector(
            dimension=config.perturbation_dimension,
            scalar_step=scalar_step,
            delta_power_mw=scalar_step,
            delta_energy_mwh=config.duration_hours * scalar_step,
            duration_before_h=config.duration_hours,
            duration_after_h=config.duration_hours,
        )
    if config.perturbation_dimension is PerturbationDimension.POWER_ONLY:
        return PerturbationVector(
            dimension=config.perturbation_dimension,
            scalar_step=scalar_step,
            delta_power_mw=scalar_step,
            delta_energy_mwh=0.0,
            duration_before_h=config.duration_hours,
            duration_after_h=None,
        )
    if config.perturbation_dimension is PerturbationDimension.ENERGY_ONLY:
        return PerturbationVector(
            dimension=config.perturbation_dimension,
            scalar_step=scalar_step,
            delta_power_mw=0.0,
            delta_energy_mwh=scalar_step,
        )
    assert config.custom_perturbation_vector is not None
    delta_power = sum(
        component.value
        for component in config.custom_perturbation_vector.components
        if component.role == "power_mw"
    )
    delta_energy = sum(
        component.value
        for component in config.custom_perturbation_vector.components
        if component.role == "energy_mwh"
    )
    custom_components = [
        CustomPerturbationComponent(
            role=component.role,
            name=component.name,
            value=component.value * scalar_step,
            unit=component.unit,
        )
        for component in config.custom_perturbation_vector.components
    ]
    return PerturbationVector(
        dimension=config.perturbation_dimension,
        scalar_step=scalar_step,
        delta_power_mw=scalar_step * delta_power,
        delta_energy_mwh=scalar_step * delta_energy,
        custom_components=custom_components,
    )


def _mean_of(series: RiskSampleSeries) -> float:
    return float(np.mean(list(series.values.values())))


def _paired_eval(
    reference: RiskSampleSeries, candidate: RiskSampleSeries, diagnostics: list[str]
) -> tuple[float, float, float]:
    """Return (candidate_mean, paired_mean, paired_se). Pairing errors map
    to scenario / CRN identity diagnostics."""
    try:
        paired = evaluate_paired_differences(reference=reference, candidate=candidate)
    except PairedEvaluationError as exc:
        if exc.code in (
            "PAIRED_BANK_MISMATCH",
            "PAIRED_METRIC_MISMATCH",
            "PAIRED_SAMPLE_SET_MISMATCH",
        ):
            diagnostics.append(DIAGNOSTIC_SCENARIO_MISMATCH)
        else:
            diagnostics.append(DIAGNOSTIC_CRN_IDENTITY_MISMATCH)
        raise MriConfigError(
            exc.code if exc.code else "MRI_PAIRED_FAILED",
            exc.message,
        ) from exc
    return (
        _mean_of(candidate),
        float(paired.stats.mean),
        float(paired.stats.standard_error),
    )


def _central_paired_difference_stats(
    minus: RiskSampleSeries, plus: RiskSampleSeries
) -> tuple[float, float]:
    """Sample-level minus-minus-plus paired difference statistics.

    Central-difference MRI SE MUST be based on the minus/plus per-sample
    paired difference (never independent means). Returns
    (mean(D_s), standard_error(D_s)) with D_s = minus_s - plus_s."""
    stats = OnlineStats()
    for sample_id in minus.sample_ids:
        stats.update(minus.values[sample_id] - plus.values[sample_id])
    return float(stats.mean), float(stats.standard_error)


def _is_physical_error(exc: BaseException) -> bool:
    """The ONLY fallback trigger is an explicit physical-domain error."""
    return isinstance(exc, MriConfigError) and exc.code in _PHYSICAL_DOMAIN_CODES


def _evaluate_step(
    *,
    reference: RiskSampleSeries,
    config: MriSolverConfig,
    trial_provider: MriTrialProvider,
    scalar_step: float,
    multiplier: float,
    operational_scheme: DifferenceScheme,
    diagnostics: list[str],
    convergence_holder: list[RiskConvergence | None],
) -> StepEvaluation:
    """Evaluate ONE scalar step under the operational difference scheme.

    Every evaluated trial series has its attached shared-T08 convergence
    evidence recorded into ``convergence_holder``. An explicit
    ``MriConfigError(MRI_PHYSICAL_DOMAIN_VIOLATION)`` PROPAGATES (the
    caller unifies the whole run to forward or forms invalid_method);
    config / data / scenario / run-pipeline / code errors always propagate
    with their original category."""
    vector = _build_perturbation_vector(config, scalar_step)
    applied = operational_scheme
    minus_risk: float | None = None
    plus_se = 0.0
    plus_mean = 0.0
    central_paired_se = 0.0

    if operational_scheme is DifferenceScheme.CENTRAL:
        minus_vector = _build_perturbation_vector(config, -scalar_step)
        minus_candidate = trial_provider(minus_vector)
        _record_convergence(minus_candidate, convergence_holder)
        minus_mean, _m_pair, _m_se = _paired_eval(reference, minus_candidate, diagnostics)
        minus_risk = minus_mean
        plus_candidate = trial_provider(vector)
        _record_convergence(plus_candidate, convergence_holder)
        plus_mean, _p_pair, plus_se = _paired_eval(reference, plus_candidate, diagnostics)
        _central_paired_mean, central_paired_se = _central_paired_difference_stats(
            minus_candidate, plus_candidate
        )
        paired_difference = minus_mean - plus_mean
    else:
        plus_candidate = trial_provider(vector)
        _record_convergence(plus_candidate, convergence_holder)
        plus_mean, _p_pair, plus_se = _paired_eval(reference, plus_candidate, diagnostics)
        paired_difference = _mean_of(reference) - plus_mean

    if applied is DifferenceScheme.CENTRAL:
        mri_estimate = paired_difference / (2.0 * scalar_step)
        standard_error = central_paired_se / (2.0 * abs(scalar_step))
    else:
        mri_estimate = paired_difference / scalar_step
        standard_error = plus_se / abs(scalar_step)

    return StepEvaluation(
        step_multiplier=multiplier,
        scalar_step=scalar_step,
        perturbation_vector=vector,
        baseline_risk=_mean_of(reference),
        minus_risk=minus_risk,
        plus_risk=plus_mean,
        paired_risk_difference=paired_difference,
        applied_difference_scheme=applied,
        mri_estimate=mri_estimate,
        standard_error=standard_error,
        lower_ci=None,
        upper_ci=None,
        crn_identity=reference.scenario_bank_id,
        diagnostics=list(diagnostics),
    )


def _engineering_tolerance(
    config: MriSolverConfig,
    candidate_capacity_mw: float | None,
    candidate_energy_mwh: float | None,
) -> float:
    """The RUN-CONFIG engineering tolerance for the adaptive base step
    (never a solver-internal 1e-3 constant). adaptive mode REQUIRES the
    configured tolerance - no silent substitute."""
    if config.perturbation_dimension is PerturbationDimension.ENERGY_ONLY:
        tolerance = config.engineering_tolerance_energy_mwh
        if tolerance is not None and tolerance > 0.0:
            return float(tolerance)
        raise MriConfigError(
            "MRI_INVALID_CONFIG",
            "adaptive energy step requires a positive engineering_tolerance.energy_mwh",
        )
    tolerance = config.engineering_tolerance_capacity_mw
    if tolerance is not None and tolerance > 0.0:
        return float(tolerance)
    raise MriConfigError(
        "MRI_INVALID_CONFIG",
        "adaptive step requires a positive engineering_tolerance.capacity_mw",
    )


def _base_step_value(
    config: MriSolverConfig,
    candidate_capacity_mw: float | None,
    candidate_energy_mwh: float | None,
    peak_load_mw: float | None,
) -> float:
    """The concrete base step (methodology §10.3).

    explicit: the configured base_step. adaptive: for MW-step dimensions
    delta_0 = min(0.1*P_X, max(Tol_C, 0.001*P_peak)); for energy_only
    delta_E0 = min(0.1*E_X, max(Tol_E, 0.001*P_peak)). Tol_C / Tol_E are
    the RUN-CONFIG engineering tolerances (never a hard-coded 1e-3); the
    real candidate capacity / energy and peak load are always required."""
    if config.step_mode is StepMode.EXPLICIT:
        assert config.base_step is not None
        return float(config.base_step)
    peak = peak_load_mw or 0.0
    peak_term = _PEAK_LOAD_SCALE * peak
    if config.perturbation_dimension is PerturbationDimension.ENERGY_ONLY:
        energy_capacity = candidate_energy_mwh or 0.0
        if energy_capacity <= 0.0:
            raise MriConfigError(
                "MRI_INVALID_CONFIG",
                "adaptive energy step requires a candidate energy capacity",
            )
        tolerance = _engineering_tolerance(config, candidate_capacity_mw, candidate_energy_mwh)
        return float(min(0.1 * energy_capacity, max(tolerance, peak_term)))
    power_capacity = candidate_capacity_mw or 0.0
    if power_capacity <= 0.0:
        raise MriConfigError(
            "MRI_INVALID_CONFIG",
            "adaptive step requires a candidate capacity",
        )
    tolerance = _engineering_tolerance(config, candidate_capacity_mw, candidate_energy_mwh)
    return float(min(0.1 * power_capacity, max(tolerance, peak_term)))


def _positive_steps_within_domain(
    config: MriSolverConfig, scalar_steps: list[float], diagnostics: list[str]
) -> bool:
    """EVERY 0.5/1/2 positive step must respect ``max_positive_step``; a
    violation is an explicit-mode invalid_method (no trials executed)."""
    if config.physical_domain is None:
        return True
    for scalar in scalar_steps:
        if scalar > config.physical_domain.max_positive_step:
            diagnostics.append(DIAGNOSTIC_PHYSICAL_DOMAIN_VIOLATION)
            return False
    return True


def _negative_steps_within_domain(config: MriSolverConfig, scalar_steps: list[float]) -> bool:
    """Whether every central negative step is inside ``max_negative_step``.
    When not, the WHOLE run falls back to forward (safe fallback) - never
    a silent per-step scheme change, never an invalid_method for the
    negative side alone."""
    if config.physical_domain is None:
        return True
    return all(abs(-scalar) <= config.physical_domain.max_negative_step for scalar in scalar_steps)


def _perfect_capacity_audit(
    *,
    reference: RiskSampleSeries,
    config: MriSolverConfig,
    perfect_capacity_provider: PerfectCapacityProvider,
    base_step: float,
    operational_scheme: DifferenceScheme,
    diagnostics: list[str],
    convergence_holder: list[RiskConvergence | None],
) -> tuple[float, float, float, tuple[StepEvaluation, ...], bool]:
    """The perfect-capacity denominator with the SAME 0.5/1/2 step audit
    and the SAME stability checks as the target. Returns
    (denominator_mean, denominator_se, denominator_relative_se,
    perfect_capacity_step_evaluations, denominator_stable).

    The denominator point estimate is the mean of the three perfect-capacity
    step estimates; its standard error comes from the base-step (multiplier
    1.0) sample-level paired risk differences divided by F. The joint-CI,
    local-monotonic, step-sensitive and scheme-consistency checks are ALL
    executed on the denominator steps and jointly decide
    ``denominator_stable``. Every perfect series' shared-T08 convergence
    evidence is recorded into ``convergence_holder``."""
    evaluations: list[StepEvaluation] = []
    estimates: list[float] = []
    base_se: float | None = None
    for multiplier in config.step_multipliers:
        capacity = base_step * multiplier
        if operational_scheme is DifferenceScheme.CENTRAL:
            # the perfect capacity S+F has no feasible negative side; a
            # central request was already unified to forward upstream, so
            # this branch is defensive only.
            raise MriConfigError(
                DIAGNOSTIC_PHYSICAL_DOMAIN_VIOLATION,
                "perfect-capacity denominator cannot evaluate a negative (minus) side",
            )
        perfect_series = perfect_capacity_provider(capacity)
        _record_convergence(perfect_series, convergence_holder)
        try:
            paired = evaluate_paired_differences(reference=reference, candidate=perfect_series)
        except PairedEvaluationError as exc:
            diagnostics.append(DIAGNOSTIC_SCENARIO_MISMATCH)
            raise MriConfigError(
                exc.code if exc.code else "MRI_PAIRED_FAILED",
                exc.message,
            ) from exc
        # candidate - reference = mean(perfect) - mean(reference); the
        # MRI_F estimate = (mean(reference) - mean(perfect)) / F.
        paired_difference = -float(paired.stats.mean)
        mri_estimate = paired_difference / capacity
        se = float(paired.stats.standard_error) / capacity
        estimates.append(mri_estimate)
        if abs(multiplier - 1.0) < 1e-12:
            base_se = se
        evaluations.append(
            StepEvaluation(
                step_multiplier=multiplier,
                scalar_step=capacity,
                perturbation_vector=PerturbationVector(
                    dimension=PerturbationDimension.POWER_ONLY,
                    scalar_step=capacity,
                    delta_power_mw=capacity,
                    delta_energy_mwh=0.0,
                ),
                baseline_risk=_mean_of(reference),
                minus_risk=None,
                plus_risk=_mean_of(perfect_series),
                paired_risk_difference=paired_difference,
                applied_difference_scheme=operational_scheme,
                mri_estimate=mri_estimate,
                standard_error=se,
                lower_ci=None,
                upper_ci=None,
                crn_identity=reference.scenario_bank_id,
                diagnostics=list(diagnostics),
            )
        )
    denominator_mean = float(np.mean(estimates))
    assert base_se is not None
    denominator_se = float(base_se)
    relative_se = float(denominator_se / max(abs(denominator_mean), 1e-12))
    denominator_evals = tuple(evaluations)
    denominator_stable = True
    denominator_stable &= _check_local_monotonic(denominator_evals, diagnostics)
    denominator_stable &= _check_strategy_switch(denominator_evals, diagnostics)
    denominator_stable &= _check_joint_confidence_interval(
        denominator_evals, config.confidence_level, diagnostics
    )
    return denominator_mean, denominator_se, relative_se, denominator_evals, denominator_stable


def _z_score(confidence_level: float) -> float:
    """Two-sided normal z for the joint CI (large-sample default)."""
    return 1.96 if confidence_level >= 0.95 else 1.645


def _check_local_monotonic(step_evals: tuple[StepEvaluation, ...], diagnostics: list[str]) -> bool:
    """Non-monotonic across the step audit (increasing step -> MRI sign
    changes or strong magnitude reversal) flags the local relation."""
    if len(step_evals) < 2:
        return True
    signs = {1 if e.mri_estimate >= 0.0 else -1 for e in step_evals}
    if len(signs) == 2:
        diagnostics.append(DIAGNOSTIC_LOCAL_NON_MONOTONIC)
        return False
    estimates = [abs(e.mri_estimate) for e in step_evals]
    if estimates[-1] > 3.0 * (estimates[0] + 1e-12):
        diagnostics.append(DIAGNOSTIC_STEP_SENSITIVE)
        return False
    return True


def _check_joint_confidence_interval(
    step_evals: tuple[StepEvaluation, ...], confidence_level: float, diagnostics: list[str]
) -> bool:
    """step_consistency_rule=joint_confidence_interval: every step
    estimate must fall inside the joint CI of the mean estimate; a step
    outside the interval flags MRI_STEP_SENSITIVE."""
    if len(step_evals) < 2:
        return True
    estimates = np.asarray([e.mri_estimate for e in step_evals], dtype=float)
    mean_estimate = float(np.mean(estimates))
    step_se_values = [e.standard_error for e in step_evals if e.standard_error is not None]
    if not step_se_values:
        return True
    joint_se = float(np.sqrt(np.mean(np.square(step_se_values))))
    half_width = _z_score(confidence_level) * joint_se
    for evaluation in step_evals:
        if abs(evaluation.mri_estimate - mean_estimate) > half_width + 1e-12:
            diagnostics.append(DIAGNOSTIC_STEP_SENSITIVE)
            return False
    return True


def _check_strategy_switch(step_evals: tuple[StepEvaluation, ...], diagnostics: list[str]) -> bool:
    """A discrete operating/dispatch boundary is crossed when the APPLIED
    difference scheme differs across the step audit (e.g. one step falls
    back to forward while another stays central): MRI_STRATEGY_SWITCH."""
    applied = {evaluation.applied_difference_scheme for evaluation in step_evals}
    if len(applied) > 1:
        diagnostics.append(DIAGNOSTIC_STRATEGY_SWITCH)
        return False
    return True


def _aggregate_convergence(states: list[RiskConvergence | None]) -> RiskConvergence:
    """Worst-evidence aggregation across every NECESSARY risk series
    (reference + each evaluated plus/minus trial + perfect-capacity
    denominator trials): any NOT_CONVERGED -> not_converged; any
    LOW_EVENT_COUNT (when none not_converged) -> low_event_count; only
    when EVERY provided state is CONVERGED -> converged. A series with NO
    attached evidence (None) is treated as NOT_CONVERGED - an unknown
    state is never written as converged."""
    present = [state for state in states if state is not None]
    if len(present) != len(states):
        return RiskConvergence.NOT_CONVERGED
    if any(state is RiskConvergence.NOT_CONVERGED for state in present):
        return RiskConvergence.NOT_CONVERGED
    if any(state is RiskConvergence.LOW_EVENT_COUNT for state in present):
        return RiskConvergence.LOW_EVENT_COUNT
    return RiskConvergence.CONVERGED


def _apply_diagnostic_warning_policy(
    diagnostics: tuple[str, ...],
    current_status: ResultStatus,
) -> ResultStatus:
    """Final status aggregation against the frozen diagnostic registry
    (16A.9): on the DEDUPLICATED diagnostics, a registered warning
    (MRI_DIAGNOSTIC_SEVERITY[code] == "warning") promotes a valid-family
    result to ``valid_with_warning``; info never promotes (RMRI_NOT_REQUESTED
    keeps a result ``valid``); not_converged / low_resolution /
    invalid_method / invalid_data keep their fixed priority (a warning
    never overrides them)."""
    if current_status not in (ResultStatus.VALID, ResultStatus.VALID_WITH_WARNING):
        return current_status
    has_warning = any(MRI_DIAGNOSTIC_SEVERITY.get(code) == "warning" for code in diagnostics)
    return ResultStatus.VALID_WITH_WARNING if has_warning else ResultStatus.VALID


def _record_convergence(
    series: RiskSampleSeries | None, holder: list[RiskConvergence | None]
) -> None:
    if series is not None:
        snapshot = series.convergence_snapshot
        holder.append(snapshot.risk_convergence if snapshot is not None else None)


@dataclass(frozen=True)
class MriResult:
    """Solver-internal MRI/rMRI result."""

    run_type: str
    mri_value: float | None
    mri_unit: str
    equivalent_mri_per_mw: float | None
    perfect_capacity_mri_value: float | None
    rmri_value: float | None
    rmri_status: RmriStatus
    requested_difference_scheme: DifferenceScheme
    applied_difference_scheme: DifferenceScheme
    base_step: float
    step_unit: Literal["mw", "mwh"]
    step_multipliers: tuple[float, ...]
    step_evaluations: tuple[StepEvaluation, ...]
    perfect_capacity_step_evaluations: tuple[StepEvaluation, ...]
    result_status: ResultStatus
    risk_convergence: RiskConvergence
    sample_count: int
    diagnostics: tuple[str, ...]
    accredited_capacity_mw: float | None = None
    capacity_credit_fraction: float | None = None
    reference_capacity_mw: float | None = None
    denominator_relative_se: float | None = None
    denominator_relative_se_max: float | None = None


def _terminal_result(
    *,
    reference: RiskSampleSeries,
    config: MriSolverConfig,
    base_step: float,
    step_unit: Literal["mw", "mwh"],
    unit: str,
    diagnostics: list[str],
    result_status: ResultStatus,
    risk_convergence: RiskConvergence,
    applied_difference_scheme: DifferenceScheme,
) -> MriResult:
    return MriResult(
        run_type=config.run_type,
        mri_value=None,
        mri_unit=unit,
        equivalent_mri_per_mw=None,
        perfect_capacity_mri_value=None,
        rmri_value=None,
        rmri_status=RmriStatus.NOT_APPLICABLE,
        requested_difference_scheme=config.difference_scheme,
        applied_difference_scheme=applied_difference_scheme,
        base_step=base_step,
        step_unit=step_unit,
        step_multipliers=tuple(config.step_multipliers),
        step_evaluations=(),
        perfect_capacity_step_evaluations=(),
        result_status=result_status,
        risk_convergence=risk_convergence,
        sample_count=len(reference.sample_ids),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )


def _invalid_data_result(
    *,
    reference: RiskSampleSeries,
    config: MriSolverConfig,
    base_step: float,
    step_unit: Literal["mw", "mwh"],
    unit: str,
    diagnostics: list[str],
    risk_convergence: RiskConvergence,
    applied_difference_scheme: DifferenceScheme,
) -> MriResult:
    return _terminal_result(
        reference=reference,
        config=config,
        base_step=base_step,
        step_unit=step_unit,
        unit=unit,
        diagnostics=diagnostics,
        result_status=ResultStatus.INVALID_DATA,
        risk_convergence=risk_convergence,
        applied_difference_scheme=applied_difference_scheme,
    )


def _invalid_method_result(
    *,
    reference: RiskSampleSeries,
    config: MriSolverConfig,
    base_step: float,
    step_unit: Literal["mw", "mwh"],
    unit: str,
    diagnostics: list[str],
    risk_convergence: RiskConvergence,
    applied_difference_scheme: DifferenceScheme,
) -> MriResult:
    return _terminal_result(
        reference=reference,
        config=config,
        base_step=base_step,
        step_unit=step_unit,
        unit=unit,
        diagnostics=diagnostics,
        result_status=ResultStatus.INVALID_METHOD,
        risk_convergence=risk_convergence,
        applied_difference_scheme=applied_difference_scheme,
    )


def _try_steps(
    *,
    reference: RiskSampleSeries,
    config: MriSolverConfig,
    trial_provider: MriTrialProvider,
    scalar_steps: list[float],
    operational: DifferenceScheme,
    diagnostics: list[str],
    holder: list[RiskConvergence | None],
) -> list[StepEvaluation] | None:
    """Evaluate every 0.5/1/2 target step under ONE applied scheme.

    Returns the StepEvaluations, or None when an explicit
    ``MRI_PHYSICAL_DOMAIN_VIOLATION`` is raised on a CENTRAL minus side
    (the caller unifies the WHOLE run to forward and retries). Forward-side
    physical / data / method / other errors propagate as MriConfigError for
    the caller to map to the frozen status table."""
    step_evals: list[StepEvaluation] = []
    for scalar, multiplier in zip(scalar_steps, config.step_multipliers):
        try:
            step_evals.append(
                _evaluate_step(
                    reference=reference,
                    config=config,
                    trial_provider=trial_provider,
                    scalar_step=scalar,
                    multiplier=multiplier,
                    operational_scheme=operational,
                    diagnostics=diagnostics,
                    convergence_holder=holder,
                )
            )
        except MriConfigError as exc:
            if _is_physical_error(exc) and operational is DifferenceScheme.CENTRAL:
                return None
            raise
    return step_evals


def _evaluate_target_steps(
    *,
    reference: RiskSampleSeries,
    config: MriSolverConfig,
    trial_provider: MriTrialProvider,
    scalar_steps: list[float],
    operational: DifferenceScheme,
    diagnostics: list[str],
) -> tuple[list[StepEvaluation], list[RiskConvergence | None], DifferenceScheme]:
    """Run the target step audit with a WHOLE-RUN fallback: a runtime
    minus-side physical error during central unifies the entire run to
    forward (never a per-step scheme change) and retries. Returns
    (step_evaluations, trial_convergences, final_operational_scheme)."""
    holder: list[RiskConvergence | None] = []
    step_evals = _try_steps(
        reference=reference,
        config=config,
        trial_provider=trial_provider,
        scalar_steps=scalar_steps,
        operational=operational,
        diagnostics=diagnostics,
        holder=holder,
    )
    if step_evals is not None:
        return step_evals, holder, operational
    operational = DifferenceScheme.FORWARD
    diagnostics.append(DIAGNOSTIC_DIFFERENCE_SCHEME_FALLBACK)
    holder.clear()
    step_evals = _try_steps(
        reference=reference,
        config=config,
        trial_provider=trial_provider,
        scalar_steps=scalar_steps,
        operational=operational,
        diagnostics=diagnostics,
        holder=holder,
    )
    if step_evals is None:
        raise MriConfigError(
            DIAGNOSTIC_PHYSICAL_DOMAIN_VIOLATION,
            "forward difference is outside the physical domain",
        )
    return step_evals, holder, operational


def run_mri(
    *,
    config: MriSolverConfig,
    reference: RiskSampleSeries,
    trial_provider: MriTrialProvider,
    perfect_capacity_provider: PerfectCapacityProvider | None = None,
    primary_risk_metric: RiskMetric | None = None,
    candidate_capacity_mw: float | None = None,
    candidate_energy_mwh: float | None = None,
    peak_load_mw: float | None = None,
    reference_capacity_mw: float | None = None,
) -> MriResult:
    """Run the MRI/rMRI finite-difference audit over the injected trial
    provider. See the module docstring for status semantics."""
    if not isinstance(reference, RiskSampleSeries):
        raise MriConfigError("MRI_INVALID_REFERENCE", "reference must be a RiskSampleSeries")
    if not callable(trial_provider):
        raise MriConfigError("MRI_INVALID_PROVIDER", "trial_provider must be callable")

    metric = primary_risk_metric or reference.primary_risk_metric
    step_unit = _step_unit(config)
    unit = mri_unit(metric, step_unit)
    base_step = _base_step_value(config, candidate_capacity_mw, candidate_energy_mwh, peak_load_mw)
    diagnostics: list[str] = []
    scalar_steps = [base_step * multiplier for multiplier in config.step_multipliers]

    # 1) EVERY positive step must respect max_positive_step; a violation is
    #    invalid_method and NO trial is executed.
    if not _positive_steps_within_domain(config, scalar_steps, diagnostics):
        return _invalid_method_result(
            reference=reference,
            config=config,
            base_step=base_step,
            step_unit=step_unit,
            unit=unit,
            diagnostics=diagnostics,
            risk_convergence=RiskConvergence.NOT_CONVERGED,
            applied_difference_scheme=config.difference_scheme,
        )

    # 2) decide the WHOLE-RUN applied scheme BEFORE any expensive trial.
    operational_scheme = config.difference_scheme
    if config.run_type == "rmri" and operational_scheme is DifferenceScheme.CENTRAL:
        # the perfect-capacity S+F has no feasible negative side -> unify
        # the whole rMRI to forward.
        operational_scheme = DifferenceScheme.FORWARD
        diagnostics.append(DIAGNOSTIC_DIFFERENCE_SCHEME_FALLBACK)
    elif operational_scheme is DifferenceScheme.CENTRAL and not _negative_steps_within_domain(
        config, scalar_steps
    ):
        # all positive steps are feasible but a negative step is NOT -> the
        # whole run safely falls back to forward (no per-step scheme change).
        operational_scheme = DifferenceScheme.FORWARD
        diagnostics.append(DIAGNOSTIC_DIFFERENCE_SCHEME_FALLBACK)

    # 3) evaluate all target steps (a runtime minus-side physical error
    #    unifies the whole run to forward and retries).
    try:
        step_evals, trial_convergences, applied_scheme = _evaluate_target_steps(
            reference=reference,
            config=config,
            trial_provider=trial_provider,
            scalar_steps=scalar_steps,
            operational=operational_scheme,
            diagnostics=diagnostics,
        )
    except MriConfigError as exc:
        if _is_physical_error(exc):
            diagnostics.append(DIAGNOSTIC_PHYSICAL_DOMAIN_VIOLATION)
            return _invalid_method_result(
                reference=reference,
                config=config,
                base_step=base_step,
                step_unit=step_unit,
                unit=unit,
                diagnostics=diagnostics,
                risk_convergence=RiskConvergence.NOT_CONVERGED,
                applied_difference_scheme=operational_scheme,
            )
        if exc.code in _DATA_ERROR_CODES:
            return _invalid_data_result(
                reference=reference,
                config=config,
                base_step=base_step,
                step_unit=step_unit,
                unit=unit,
                diagnostics=diagnostics,
                risk_convergence=RiskConvergence.NOT_CONVERGED,
                applied_difference_scheme=operational_scheme,
            )
        if exc.code in _METHOD_ERROR_CODES:
            diagnostics.append(exc.code)
            return _invalid_method_result(
                reference=reference,
                config=config,
                base_step=base_step,
                step_unit=step_unit,
                unit=unit,
                diagnostics=diagnostics,
                risk_convergence=RiskConvergence.NOT_CONVERGED,
                applied_difference_scheme=operational_scheme,
            )
        raise
    step_evaluations = tuple(step_evals)
    operational_scheme = applied_scheme

    mri_value: float | None = float(np.mean([e.mri_estimate for e in step_evaluations]))
    mri_unit_str = unit
    equivalent_mri_per_mw: float | None = None

    run_type = config.run_type
    result_status = ResultStatus.VALID
    rmri_status = RmriStatus.NOT_APPLICABLE
    rmri_value: float | None = None
    perfect_capacity_mri_value: float | None = None
    perfect_capacity_step_evals: tuple[StepEvaluation, ...] = ()
    accredited: float | None = None
    fraction: float | None = None
    denominator_relative_se: float | None = None

    # 4) aggregate the shared-T08 convergence evidence across the reference
    #    and EVERY necessary target trial (worst evidence wins).
    target_states = [
        reference.convergence_snapshot.risk_convergence
        if reference.convergence_snapshot is not None
        else None
    ] + trial_convergences
    target_convergence = _aggregate_convergence(target_states)
    target_converged = target_convergence is RiskConvergence.CONVERGED

    if run_type == "mri":
        diagnostics.append(DIAGNOSTIC_RMRI_NOT_REQUESTED)
        if not target_converged:
            result_status = (
                ResultStatus.LOW_RESOLUTION
                if target_convergence is RiskConvergence.LOW_EVENT_COUNT
                else ResultStatus.NOT_CONVERGED
            )
            rmri_status = RmriStatus.NOT_APPLICABLE
            mri_value = None  # 16A.9: not_converged does not publish the final MRI point
        else:
            stable_steps = _check_local_monotonic(step_evaluations, diagnostics)
            stable_steps &= _check_strategy_switch(step_evaluations, diagnostics)
            stable_steps &= _check_joint_confidence_interval(
                step_evaluations, config.confidence_level, diagnostics
            )
            _ = stable_steps  # step evidence recorded; only gates rMRI status

    elif run_type == "rmri":
        if not target_converged:
            result_status = (
                ResultStatus.LOW_RESOLUTION
                if target_convergence is RiskConvergence.LOW_EVENT_COUNT
                else ResultStatus.NOT_CONVERGED
            )
            rmri_status = RmriStatus.NOT_APPLICABLE  # never entered the rMRI calculation
            mri_value = None
        else:
            assert config.denominator_relative_se_max is not None  # enforced in __post_init__
            if perfect_capacity_provider is None:
                raise MriConfigError(
                    "MRI_INVALID_PROVIDER",
                    "rmri requires a perfect_capacity_provider",
                )
            (
                denominator_mean,
                _denominator_se,
                relative_se,
                perfect_evals,
                denominator_stable,
            ) = _perfect_capacity_audit(
                reference=reference,
                config=config,
                perfect_capacity_provider=perfect_capacity_provider,
                base_step=base_step,
                operational_scheme=operational_scheme,
                diagnostics=diagnostics,
                convergence_holder=trial_convergences,
            )
            perfect_capacity_mri_value = denominator_mean
            perfect_capacity_step_evals = perfect_evals
            denominator_relative_se = relative_se

            # 5) aggregate again INCLUDING every perfect-capacity
            #    denominator trial (worst evidence wins).
            all_states = [
                reference.convergence_snapshot.risk_convergence
                if reference.convergence_snapshot is not None
                else None
            ] + trial_convergences
            all_convergence = _aggregate_convergence(all_states)
            all_converged = all_convergence is RiskConvergence.CONVERGED
            if not all_converged:
                result_status = (
                    ResultStatus.LOW_RESOLUTION
                    if all_convergence is RiskConvergence.LOW_EVENT_COUNT
                    else ResultStatus.NOT_CONVERGED
                )
                rmri_status = RmriStatus.UNSTABLE  # entered the rMRI calculation
                rmri_value = None
                mri_value = None
            else:
                # 6) ONLY after every necessary risk series converged: judge
                #    paired differences, step consistency and denominator
                #    stability (all still from shared paired_evaluation).
                stable_steps = _check_local_monotonic(step_evaluations, diagnostics)
                stable_steps &= _check_strategy_switch(step_evaluations, diagnostics)
                stable_steps &= _check_joint_confidence_interval(
                    step_evaluations, config.confidence_level, diagnostics
                )
                stable_steps &= denominator_stable

                normalization_ready = False
                mri_value_to_rmri: float | None = None
                assert mri_value is not None  # rmri never nulls a converged raw MRI point
                if config.perturbation_dimension is PerturbationDimension.ENERGY_ONLY:
                    if config.normalization_basis_mwh_per_mw is None:
                        diagnostics.append(DIAGNOSTIC_NORMALIZATION_MISSING)
                        result_status = ResultStatus.VALID_WITH_WARNING
                        rmri_status = RmriStatus.NOT_APPLICABLE
                    else:
                        equivalent = mri_value * config.normalization_basis_mwh_per_mw
                        equivalent_mri_per_mw = equivalent
                        mri_value_to_rmri = equivalent
                        normalization_ready = True
                elif config.perturbation_dimension is PerturbationDimension.CUSTOM_VECTOR:
                    if config.custom_perturbation_vector is None:
                        raise MriConfigError(
                            "MRI_INVALID_CONFIG",
                            "custom_vector requires a custom_perturbation_vector",
                        )
                    mw_equivalent = config.custom_perturbation_vector.mw_equivalent_per_scalar_unit
                    if mw_equivalent is None:
                        diagnostics.append(DIAGNOSTIC_CUSTOM_NORMALIZATION_MISSING)
                        result_status = ResultStatus.VALID_WITH_WARNING
                        rmri_status = RmriStatus.NOT_APPLICABLE
                    else:
                        equivalent = mri_value * mw_equivalent
                        equivalent_mri_per_mw = equivalent
                        mri_value_to_rmri = equivalent
                        normalization_ready = True
                else:
                    # fixed_duration / power_only: the MRI is already per MW.
                    mri_value_to_rmri = mri_value
                    normalization_ready = True

                if normalization_ready:
                    if (
                        relative_se > config.denominator_relative_se_max
                        or abs(denominator_mean) <= 1e-12
                    ):
                        diagnostics.append(DIAGNOSTIC_DENOMINATOR_UNSTABLE)
                        result_status = ResultStatus.VALID_WITH_WARNING
                        rmri_status = RmriStatus.UNSTABLE
                    elif not stable_steps:
                        result_status = ResultStatus.VALID_WITH_WARNING
                        rmri_status = RmriStatus.UNSTABLE
                    else:
                        assert mri_value_to_rmri is not None
                        ratio = mri_value_to_rmri / denominator_mean
                        rmri_value = ratio
                        rmri_status = RmriStatus.STABLE
                        result_status = ResultStatus.VALID
                        if reference_capacity_mw is not None and reference_capacity_mw > 0.0:
                            accredited = ratio * reference_capacity_mw
                            fraction = ratio

    # 7) final convergence: the shared evidence attached to the reference
    #    and every evaluated target / perfect-capacity denominator trial
    #    (worst evidence wins; None is treated as not_converged).
    final_convergence = _aggregate_convergence(
        [
            reference.convergence_snapshot.risk_convergence
            if reference.convergence_snapshot is not None
            else None
        ]
        + trial_convergences
    )

    # 8) final status aggregation against the frozen diagnostic registry:
    #    on the DEDUPLICATED diagnostics a registered warning promotes a
    #    valid-family result to valid_with_warning; info never promotes;
    #    not_converged / low_resolution / invalid_method / invalid_data
    #    keep their fixed priority (a warning never overrides them).
    final_diagnostics = tuple(dict.fromkeys(diagnostics))
    result_status = _apply_diagnostic_warning_policy(final_diagnostics, result_status)

    return MriResult(
        run_type=run_type,
        mri_value=mri_value,
        mri_unit=mri_unit_str,
        equivalent_mri_per_mw=equivalent_mri_per_mw,
        perfect_capacity_mri_value=perfect_capacity_mri_value,
        rmri_value=rmri_value,
        rmri_status=rmri_status,
        requested_difference_scheme=config.difference_scheme,
        applied_difference_scheme=operational_scheme,
        base_step=base_step,
        step_unit=step_unit,
        step_multipliers=tuple(config.step_multipliers),
        step_evaluations=step_evaluations,
        perfect_capacity_step_evaluations=perfect_capacity_step_evals,
        result_status=result_status,
        risk_convergence=final_convergence,
        sample_count=len(reference.sample_ids),
        diagnostics=final_diagnostics,
        accredited_capacity_mw=accredited,
        capacity_credit_fraction=fraction,
        reference_capacity_mw=reference_capacity_mw,
        denominator_relative_se=denominator_relative_se,
        denominator_relative_se_max=config.denominator_relative_se_max,
    )


__all__ = [
    "DIAGNOSTIC_CRN_IDENTITY_MISMATCH",
    "DIAGNOSTIC_CUSTOM_NORMALIZATION_MISSING",
    "DIAGNOSTIC_DENOMINATOR_UNSTABLE",
    "DIAGNOSTIC_DIFFERENCE_SCHEME_FALLBACK",
    "DIAGNOSTIC_LOCAL_NON_MONOTONIC",
    "DIAGNOSTIC_NORMALIZATION_MISSING",
    "DIAGNOSTIC_PHYSICAL_DOMAIN_VIOLATION",
    "DIAGNOSTIC_RMRI_NOT_REQUESTED",
    "DIAGNOSTIC_SCENARIO_MISMATCH",
    "DIAGNOSTIC_STEP_SENSITIVE",
    "DIAGNOSTIC_STRATEGY_SWITCH",
    "DIAGNOSTIC_UNIT_MISMATCH",
    "MriConfigError",
    "MriResult",
    "MriSolverConfig",
    "run_mri",
]
