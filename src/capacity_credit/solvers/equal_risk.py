"""Generic equal-risk search (T11).

Search-decoupled from any ELCC/EFC adapter: the search only consumes
T10's ``RiskSampleSeries`` / ``evaluate_paired_differences`` results
through a plain evaluator callable ``(capacity_mw) ->
EqualRiskPointEvaluation``. Formal MethodResult mapping, RunService
wiring and ELCC/EFC adapters are T12.

Current T11 architecture:

- the generation barrier is ITERATIVE, never recursive: ``evaluate_once``
  performs exactly one evaluation (identity check, compact registration,
  trace append); ``stabilize_generation`` is a non-recursive fixed-point
  loop that replays the full decision-capacity inventory until one
  complete round opens no new sample generation. The number of
  generation switches is bounded by ``MAX_GENERATION_SWITCHES``; when
  exceeded the search raises ``SEARCH_SAMPLE_GENERATION_UNSTABLE``
  instead of recursing or looping forever;
- after EVERY sample-generation switch the search re-enters the full
  pipeline FORM_BRACKET -> CHECK_MONOTONICITY -> BISECT -> FINALIZE;
  ``rebuild_bracket`` recomputes low/high/width_ok/bracket_formed from
  the ACTIVE generation's points only (tightest legal cross-zero
  interval; exact difference == 0 allows an endpoint solution; no
  interval -> SEARCH_BRACKET_NOT_FORMED). bracket_formed is never
  hard-coded True at assembly time;
- bound updates are recorded as immutable ``BoundDecision`` records tied
  to the ACTIVE-generation call_id / trace record; old-generation trace
  points are audit-only and never receive bound-update results;
- ``request_more_samples`` returning True is only accepted when the
  canonical sample generation REALLY changed; otherwise the search
  reports a stable expansion-failure diagnostic (SEARCH_EXTENSION_NO_
  GENERATION_CHANGE) and never jumps to invalid_method;
- the monotonicity ledger records EVERY pairwise comparison used for a
  decision, including the acceptance comparisons, each carrying a unique
  check_id, phase, canonical generation and both endpoints' call_ids;
  SearchPoint no longer carries an un-attributable monotonic_evidence;
- the active registry and the archived generations keep only COMPACT
  immutable records (capacity, canonical generation, reference/trial
  risk, difference mean/SE, convergence snapshot, identity digests,
  call_id, trace index) - never full PairedEvaluationResult objects,
  per-sample differences or both RiskSampleSeries;
- ``RiskConvergenceSnapshot.from_convergence_state`` validates
  should_stop strictly (no bool() coercion of "false"/1/None);
- same canonical generation + capacity re-evaluation must be exactly
  reproducible; any decision-relevant drift raises
  ``SEARCH_EVALUATION_DRIFT`` (per-call audit fields are excluded);
- a missing legal bracket NEVER produces a point estimate: interpolation
  requires bracket_formed/width_ok/converged/monotonic-resolved, and a
  non-valid result is stripped of estimate/chord after classification;
- ALL public boundaries are type-checked BEFORE use: config fields,
  snapshot criteria/evidence, search entry arguments, the evaluator
  return type (SEARCH_INVALID_POINT) and a strictly boolean
  request_more_samples return (SEARCH_INVALID_CALLBACK) - native
  TypeError/AttributeError never leak out of the public API.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import numpy as np

from capacity_credit.contracts.elcc_status import (
    ALLOWED_STATUS_COMBOS,
)
from capacity_credit.contracts.enums import (
    InterpolationMethod,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    SearchConvergence,
)
from capacity_credit.contracts.risk_convergence import (
    RiskConvergenceSnapshot,
)
from capacity_credit.solvers.paired_evaluation import (
    PairedEvaluationResult,
    RiskSampleSeries,
)

_INCREASING = "risk_increases_with_capacity"
_DECREASING = "risk_decreases_with_capacity"
_VALID_DIRECTIONS = frozenset({_INCREASING, _DECREASING})

MAX_GENERATION_SWITCHES = 8

DIAGNOSTIC_BRACKET_EXPANDED = "ELCC_BRACKET_EXPANDED"
DIAGNOSTIC_INTERPOLATION_UNRESOLVED = "ELCC_INTERPOLATION_UNRESOLVED"
DIAGNOSTIC_NOT_MONOTONIC = "SEARCH_NOT_MONOTONIC"
DIAGNOSTIC_MONOTONIC_UNRESOLVED = "SEARCH_MONOTONIC_UNRESOLVED"
DIAGNOSTIC_MAX_ITERATIONS = "SEARCH_MAX_ITERATIONS"
DIAGNOSTIC_BRACKET_NOT_FORMED = "SEARCH_BRACKET_NOT_FORMED"
DIAGNOSTIC_RISK_NOT_CONVERGED = "SEARCH_RISK_NOT_CONVERGED"
DIAGNOSTIC_RESOLUTION_UNAVAILABLE = "SEARCH_RISK_RESOLUTION_UNAVAILABLE"
DIAGNOSTIC_GENERATION_UNSTABLE = "SEARCH_SAMPLE_GENERATION_UNSTABLE"
DIAGNOSTIC_EXTENSION_NO_CHANGE = "SEARCH_EXTENSION_NO_GENERATION_CHANGE"

_EUE_METRICS = frozenset({RiskMetric.EUE_MWH_PER_YEAR})


class EqualRiskSearchError(ValueError):
    """Stable equal-risk search error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _finite_number(value: Any, field: str, *, non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise EqualRiskSearchError("SEARCH_INVALID_CONFIG", f"{field} must be a real number")
    number = float(value)
    if not np.isfinite(number):
        raise EqualRiskSearchError("SEARCH_INVALID_CONFIG", f"{field} must be finite")
    if non_negative and number < 0.0:
        raise EqualRiskSearchError("SEARCH_INVALID_CONFIG", f"{field} must be non-negative")
    return number


def _validate_config_bounds(
    lower_raw: Any,
    upper_raw: Any,
    tolerance_raw: Any,
) -> tuple[float, float, float]:
    """Pure config validation: capacity bounds and tolerance."""
    lower = _finite_number(lower_raw, "lower_bound_mw", non_negative=True)
    upper = _finite_number(
        upper_raw,
        "physical_upper_bound_mw",
        non_negative=True,
    )
    tolerance = _finite_number(tolerance_raw, "capacity_tolerance_mw")
    if tolerance <= 0.0:
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG", "capacity_tolerance_mw must be positive"
        )
    if upper <= lower:
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG",
            "physical_upper_bound_mw must exceed lower_bound_mw",
        )
    return lower, upper, tolerance


def _validate_config_max_iterations(value: Any) -> int:
    """Pure config validation: positive integer iteration budget."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) <= 0:
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG", "max_iterations must be a positive integer"
        )
    return int(value)


def _validate_config_direction(value: Any) -> str:
    """Pure config validation: monotonic direction must be a string and
    one of the valid directions. Type check BEFORE member check."""
    if not isinstance(value, str):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG",
            "monotonic_direction must be a string",
        )
    if value not in _VALID_DIRECTIONS:
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG",
            f"monotonic_direction must be one of {sorted(_VALID_DIRECTIONS)}",
        )
    return value


def _validate_config_expansion(
    allow: Any,
    expanded_raw: Any,
    upper: float,
) -> float | None:
    """Pure config validation: optional upper-bound expansion."""
    if not isinstance(allow, bool):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG",
            "allow_upper_bound_expansion must be a boolean",
        )
    if not allow:
        if expanded_raw is not None:
            raise EqualRiskSearchError(
                "SEARCH_INVALID_CONFIG",
                "max_expanded_upper_bound_mw requires allow_upper_bound_expansion=True",
            )
        return None
    if expanded_raw is None:
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG",
            "allow_upper_bound_expansion=True requires max_expanded_upper_bound_mw",
        )
    expanded = _finite_number(
        expanded_raw,
        "max_expanded_upper_bound_mw",
        non_negative=True,
    )
    if expanded <= upper:
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG",
            "max_expanded_upper_bound_mw must exceed the physical bound",
        )
    return expanded


def _validate_config_confidence(value: Any) -> float:
    """Pure config validation: confidence level in (0, 1)."""
    confidence = _finite_number(value, "confidence_level")
    if not 0.0 < confidence < 1.0:
        raise EqualRiskSearchError("SEARCH_INVALID_CONFIG", "confidence_level must be in (0, 1)")
    return confidence


@dataclass(frozen=True)
class EqualRiskSearchConfig:
    """Search configuration (no convergence criteria; those come from
    the evaluator's point snapshots)."""

    lower_bound_mw: float
    physical_upper_bound_mw: float
    capacity_tolerance_mw: float
    max_iterations: int
    monotonic_direction: str = _INCREASING
    allow_upper_bound_expansion: bool = False
    max_expanded_upper_bound_mw: float | None = None
    confidence_level: float = 0.95

    def __post_init__(self) -> None:
        lower, upper, tolerance = _validate_config_bounds(
            self.lower_bound_mw,
            self.physical_upper_bound_mw,
            self.capacity_tolerance_mw,
        )
        iterations = _validate_config_max_iterations(self.max_iterations)
        direction = _validate_config_direction(self.monotonic_direction)
        expansion = _validate_config_expansion(
            self.allow_upper_bound_expansion,
            self.max_expanded_upper_bound_mw,
            upper,
        )
        confidence = _validate_config_confidence(self.confidence_level)
        object.__setattr__(self, "lower_bound_mw", lower)
        object.__setattr__(self, "physical_upper_bound_mw", upper)
        object.__setattr__(self, "capacity_tolerance_mw", tolerance)
        object.__setattr__(self, "max_iterations", iterations)
        object.__setattr__(self, "monotonic_direction", direction)
        object.__setattr__(self, "max_expanded_upper_bound_mw", expansion)
        object.__setattr__(self, "confidence_level", confidence)


def canonical_sample_set_identity(reference: RiskSampleSeries) -> str:
    """Canonical sample-set identity derived from the actual sample ids,
    scenario bank, metric and reference values - never from free text."""
    payload = (
        reference.scenario_bank_id,
        reference.primary_risk_metric.value,
        tuple(reference.sample_ids),
        tuple(sorted((s, reference.values[s]) for s in reference.sample_ids)),
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MonotonicityCheck:
    """Full paired comparison evidence for a monotonicity decision,
    including the acceptance comparisons. check_id is unique across the
    whole search; phase names the pipeline stage; the two call_ids point
    at the trace records of the compared capacities."""

    check_id: int
    phase: str
    lower_capacity: float
    higher_capacity: float
    lower_difference: float
    higher_difference: float
    lower_se: float
    higher_se: float
    critical_value: float
    lower_interval: tuple[float, float]
    higher_interval: tuple[float, float]
    mean_reversed: bool
    significant: bool
    sample_generation: str
    lower_call_id: int
    higher_call_id: int


@dataclass(frozen=True)
class ActivePointRecord:
    """Compact immutable record kept by the active registry / archives:
    only search-relevant snapshots, identity digests and trace
    references - never the full PairedEvaluationResult, per-sample
    differences or both RiskSampleSeries."""

    capacity_mw: float
    canonical_generation: str
    reference_risk: float
    trial_risk: float
    difference_mean: float
    difference_se: float
    risk_convergence: RiskConvergence
    convergence_criteria: Mapping[str, bool]
    should_stop: bool
    recent_batch_evidence: tuple[float, ...]
    identity_fingerprint: str
    exogenous_invariant_fingerprint: str
    call_id: int
    trace_index: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "convergence_criteria",
            MappingProxyType(dict(self.convergence_criteria)),
        )
        object.__setattr__(
            self,
            "recent_batch_evidence",
            tuple(self.recent_batch_evidence),
        )


@dataclass(frozen=True)
class BoundDecision:
    """One bound update driven by the ACTIVE-generation record: the
    decision is tied to the call_id and canonical generation of the
    evaluation that actually moved the bracket (never to an
    old-generation audit point)."""

    decision_id: int
    phase: str
    capacity_mw: float
    call_id: int
    canonical_generation: str
    difference_mean: float
    low_before: float
    high_before: float
    low_after: float
    high_after: float


@dataclass(frozen=True)
class EqualRiskPointEvaluation:
    """Immutable per-point evaluation snapshot produced by the
    evaluator (upstream convergence state, exogenous identity).

    ``config_identity`` is REQUIRED and MUST be the real convergence
    configuration identity that produced the evidence - it is never
    auto-derived from criteria / should_stop / recent_batch_evidence."""

    paired_result: PairedEvaluationResult
    risk_convergence: RiskConvergence
    convergence_criteria: Mapping[str, bool]
    exogenous_invariant_fingerprint: str
    sample_set_identity: str
    config_identity: str
    recent_batch_evidence: tuple[float, ...] = ()
    should_stop: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.config_identity, str) or not self.config_identity.strip():
            raise EqualRiskSearchError(
                "SEARCH_INVALID_POINT",
                "config_identity is required (the convergence configuration that "
                "produced the evidence) - never auto-derived from criteria/evidence",
            )
        identity = self.config_identity
        snapshot = RiskConvergenceSnapshot(
            risk_convergence=self.risk_convergence,
            criteria=self.convergence_criteria,
            should_stop=self.should_stop,
            recent_batch_evidence=self.recent_batch_evidence,
            config_identity=identity,
        )
        object.__setattr__(
            self,
            "convergence_criteria",
            snapshot.criteria,
        )
        object.__setattr__(
            self,
            "recent_batch_evidence",
            snapshot.recent_batch_evidence,
        )
        object.__setattr__(self, "config_identity", identity)
        if not isinstance(self.paired_result, PairedEvaluationResult):
            raise EqualRiskSearchError(
                "SEARCH_INVALID_POINT",
                "paired_result must be a PairedEvaluationResult",
            )
        if (
            not isinstance(self.exogenous_invariant_fingerprint, str)
            or not self.exogenous_invariant_fingerprint.strip()
        ):
            raise EqualRiskSearchError(
                "SEARCH_INVALID_POINT",
                "exogenous_invariant_fingerprint must be non-blank",
            )
        if not isinstance(self.sample_set_identity, str) or not self.sample_set_identity.strip():
            raise EqualRiskSearchError(
                "SEARCH_INVALID_POINT",
                "sample_set_identity must be non-blank",
            )

    @classmethod
    def from_snapshot(
        cls,
        paired_result: PairedEvaluationResult,
        snapshot: RiskConvergenceSnapshot,
        exogenous_invariant_fingerprint: str,
        sample_set_identity: str,
    ) -> EqualRiskPointEvaluation:
        return cls(
            paired_result=paired_result,
            risk_convergence=snapshot.risk_convergence,
            convergence_criteria=snapshot.criteria,
            exogenous_invariant_fingerprint=exogenous_invariant_fingerprint,
            sample_set_identity=sample_set_identity,
            recent_batch_evidence=snapshot.recent_batch_evidence,
            should_stop=snapshot.should_stop,
            config_identity=snapshot.config_identity,
        )


@dataclass(frozen=True)
class SearchPoint:
    """One immutable trial-point trace entry (one per evaluator call).
    The bracket fields snapshot the bracket at evaluation time; bound
    updates are recorded separately as BoundDecision records."""

    capacity_mw: float
    reference_risk: float
    trial_risk: float
    difference_mean: float
    difference_se: float
    sample_count: int
    nonzero_sample_count: int
    bracket_low_before: float
    bracket_high_before: float
    bracket_low_after: float
    bracket_high_after: float
    risk_convergence: RiskConvergence
    convergence_criteria: Mapping[str, bool]
    scenario_bank_id: str
    track_fingerprint: str
    exogenous_invariant_fingerprint: str
    sample_set_identity: str
    canonical_sample_set_identity: str
    call_id: int
    iteration: int
    action: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "convergence_criteria",
            MappingProxyType(dict(self.convergence_criteria)),
        )


@dataclass(frozen=True)
class EqualRiskSearchResult:
    """Solver-internal equal-risk result (no formal MethodResult yet)."""

    estimate_mw: float | None
    bracket_low_mw: float
    bracket_high_mw: float
    low_endpoint_mw: float
    high_endpoint_mw: float
    low_endpoint_risk: float
    high_endpoint_risk: float
    empirical_risk_resolution: float | None
    interpolation_method: InterpolationMethod
    risk_convergence: RiskConvergence
    search_convergence: SearchConvergence
    result_status: ResultStatus
    trace: tuple[SearchPoint, ...]
    monotonicity_ledger: tuple[MonotonicityCheck, ...]
    bound_decisions: tuple[BoundDecision, ...]
    diagnostics: tuple[str, ...]


def classify_search_outcome(
    *,
    risk_convergence: RiskConvergence,
    bracket_formed: bool,
    width_within_tolerance: bool,
    monotonic_ok: bool,
    monotonic_unresolved: bool,
    interpolated: bool,
    plateau: bool,
) -> tuple[RiskConvergence, SearchConvergence, ResultStatus]:
    """Single pure state classifier. Search conditions only derive
    search_convergence and result_status; the risk status is passed
    through untouched (LOW_EVENT_COUNT fully propagates)."""
    if not isinstance(risk_convergence, RiskConvergence):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_STATE", "risk_convergence must be a RiskConvergence"
        )
    if not monotonic_ok:
        return (
            risk_convergence,
            SearchConvergence.NOT_CONVERGED,
            ResultStatus.INVALID_METHOD,
        )
    if not bracket_formed:
        return (
            risk_convergence,
            SearchConvergence.NOT_CONVERGED,
            ResultStatus.NOT_CONVERGED,
        )
    if plateau:
        result_status = (
            ResultStatus.LOW_RESOLUTION
            if risk_convergence is RiskConvergence.LOW_EVENT_COUNT
            else ResultStatus.NOT_CONVERGED
        )
        return (
            risk_convergence,
            SearchConvergence.PLATEAU_UNRESOLVED,
            result_status,
        )
    if risk_convergence is not RiskConvergence.CONVERGED:
        result_status = (
            ResultStatus.LOW_RESOLUTION
            if risk_convergence is RiskConvergence.LOW_EVENT_COUNT
            else ResultStatus.NOT_CONVERGED
        )
        return (
            risk_convergence,
            SearchConvergence.BRACKET_ONLY,
            result_status,
        )
    if not width_within_tolerance or monotonic_unresolved or not interpolated:
        return (
            risk_convergence,
            SearchConvergence.BRACKET_ONLY,
            ResultStatus.NOT_CONVERGED,
        )
    return (
        RiskConvergence.CONVERGED,
        SearchConvergence.CONVERGED,
        ResultStatus.VALID,
    )


def combine_endpoint_risk_convergence(
    low: RiskConvergence,
    high: RiskConvergence,
) -> RiskConvergence:
    """Conservative endpoint aggregation used on EVERY path:
    NOT_CONVERGED > LOW_EVENT_COUNT > CONVERGED."""
    if low is RiskConvergence.NOT_CONVERGED or high is RiskConvergence.NOT_CONVERGED:
        return RiskConvergence.NOT_CONVERGED
    if low is RiskConvergence.LOW_EVENT_COUNT or high is RiskConvergence.LOW_EVENT_COUNT:
        return RiskConvergence.LOW_EVENT_COUNT
    return RiskConvergence.CONVERGED


def _reference_identity_payload(reference: RiskSampleSeries) -> tuple[Any, ...]:
    return (
        reference.scenario_bank_id,
        reference.primary_risk_metric,
        reference.risk_calibre,
        reference.weather_years,
        reference.year_weights_identity,
        reference.portfolio_id,
        tuple(sorted(reference.shared_object_ids)),
        tuple(sorted(reference.candidate_object_ids)),
    )


def _candidate_identity_payload(
    result: PairedEvaluationResult,
) -> tuple[Any, ...]:
    cand = result.candidate_identity
    return (
        cand.scenario_bank_id,
        cand.primary_risk_metric,
        cand.risk_calibre,
        cand.weather_years,
        cand.year_weights_identity,
        cand.portfolio_id,
        tuple(sorted(cand.shared_object_ids)),
        tuple(sorted(cand.candidate_object_ids)),
    )


def _track_fingerprint(result: PairedEvaluationResult) -> str:
    ref_identity = result.reference_identity
    payload = (
        result.scenario_bank_id,
        ref_identity.primary_risk_metric.value,
        ref_identity.risk_calibre,
        str(sorted(ref_identity.shared_object_ids)),
        str(ref_identity.sample_ids),
        ref_identity.year_weights_identity,
        ref_identity.portfolio_id,
        str(sorted(result.candidate_identity.shared_object_ids)),
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


def _sample_value_digest(sample_id: int, value: float) -> str:
    return hashlib.sha256(repr((sample_id, float(value))).encode("utf-8")).hexdigest()


_DRIFT_COMPARED_FIELDS = (
    "reference_risk",
    "trial_risk",
    "difference_mean",
    "difference_se",
    "risk_convergence",
    "should_stop",
    "recent_batch_evidence",
    "identity_fingerprint",
    "exogenous_invariant_fingerprint",
)


def _assert_no_same_generation_drift(
    active: Mapping[float, ActivePointRecord],
    record: ActivePointRecord,
    capacity_mw: float,
) -> None:
    """Same canonical generation + same capacity must be reproducible:
    re-evaluating a capacity in the same sample generation must not
    silently overwrite an existing decision-relevant record with a
    different result (SEARCH_EVALUATION_DRIFT). Per-call audit fields
    (call_id / trace_index) are excluded from the comparison."""
    existing = active.get(capacity_mw)
    if existing is None:
        return
    drifted = [
        field
        for field in _DRIFT_COMPARED_FIELDS
        if getattr(existing, field) != getattr(record, field)
    ]
    if dict(existing.convergence_criteria) != dict(record.convergence_criteria):
        drifted.append("convergence_criteria")
    if drifted:
        raise EqualRiskSearchError(
            "SEARCH_EVALUATION_DRIFT",
            f"same-generation re-evaluation of {capacity_mw} MW drifted: " + ", ".join(drifted),
        )


def _check_reference_identity(
    reference_payload: tuple[Any, ...],
    ref_identity: RiskSampleSeries,
) -> None:
    """Identity check of the trial evaluation's reference side against
    the search-anchored reference payload."""
    checks = [
        (
            "scenario bank",
            ref_identity.scenario_bank_id == reference_payload[0],
        ),
        (
            "risk metric",
            ref_identity.primary_risk_metric == reference_payload[1],
        ),
        (
            "risk calibre",
            ref_identity.risk_calibre == reference_payload[2],
        ),
        (
            "weather years",
            set(ref_identity.weather_years) == set(reference_payload[3]),
        ),
        (
            "year weights",
            ref_identity.year_weights_identity == reference_payload[4],
        ),
        (
            "portfolio",
            ref_identity.portfolio_id == reference_payload[5],
        ),
        (
            "shared objects",
            tuple(sorted(ref_identity.shared_object_ids)) == reference_payload[6],
        ),
        (
            "candidate objects",
            tuple(sorted(ref_identity.candidate_object_ids)) == reference_payload[7],
        ),
    ]
    for label, ok in checks:
        if not ok:
            raise EqualRiskSearchError(
                "SEARCH_IDENTITY_MISMATCH",
                f"trial evaluation reference {label} differs from the search reference",
            )


def _check_candidate_identity(
    anchor: tuple[Any, ...] | None,
    candidate_payload: tuple[Any, ...],
) -> tuple[Any, ...]:
    """Anchor the candidate identity at the first legal trial point and
    force-check it on every later point."""
    if anchor is None:
        return candidate_payload
    if candidate_payload != anchor:
        raise EqualRiskSearchError(
            "SEARCH_IDENTITY_MISMATCH",
            "trial evaluation candidate identity differs from the candidate anchor",
        )
    return anchor


def _check_exogenous_fingerprint(
    anchored_exogenous: str | None,
    fingerprint: str,
) -> None:
    """Exogenous invariant fingerprint must match the anchored value."""
    if anchored_exogenous is not None and fingerprint != anchored_exogenous:
        raise EqualRiskSearchError(
            "SEARCH_IDENTITY_MISMATCH",
            "trial evaluation exogenous invariant fingerprint differs "
            "from the anchored search identity",
        )


def _update_sample_value_digests(
    digests: dict[int, str],
    ref_identity: RiskSampleSeries,
    point_ids: set[int],
) -> None:
    """Track compact per-sample value digests; a changed value of an
    already-seen sample is an identity mismatch."""
    for sample_id in point_ids:
        digest = _sample_value_digest(sample_id, ref_identity.values[sample_id])
        if sample_id in digests:
            if digests[sample_id] != digest:
                raise EqualRiskSearchError(
                    "SEARCH_IDENTITY_MISMATCH",
                    "trial evaluation changed a fixed reference sample value",
                )
        else:
            digests[sample_id] = digest


class _ActiveEvaluationRegistry:
    """Single source of truth for the ACTIVE sample generation and the
    per-capacity evaluations that drive search decisions.

    The DECISION-capacity inventory persists across generation switches;
    the active evaluations are rebuilt per generation as COMPACT
    ActivePointRecord snapshots; old generations are archived read-only
    as compact summaries (never full PairedEvaluationResult objects or
    per-sample difference lists)."""

    def __init__(self, reference: RiskSampleSeries) -> None:
        self.reference_payload = _reference_identity_payload(reference)
        self.anchored_exogenous: str | None = None
        self.archived_generations: list[dict[str, Any]] = []
        self.decision_capacities: set[float] = set()
        self.generation_identity = canonical_sample_set_identity(reference)
        self.exact_sample_ids: frozenset[int] = frozenset(reference.sample_ids)
        self.reference_risk = float(np.mean(list(reference.values.values())))
        self.sample_value_digests: dict[int, str] = {
            s: _sample_value_digest(s, reference.values[s]) for s in reference.sample_ids
        }
        self.candidate_anchor: tuple[Any, ...] | None = None
        self.active: dict[float, ActivePointRecord] = {}

    def register(
        self,
        capacity_mw: float,
        evaluation: EqualRiskPointEvaluation,
        *,
        call_id: int,
        trace_index: int,
    ) -> tuple[ActivePointRecord, bool]:
        """Register one evaluation; returns (compact record, opened_new_
        generation). When a new generation opens, the previous active
        records are archived (compactly) and the active set is rebuilt.
        The decision inventory is never cleared by a generation switch."""
        result = evaluation.paired_result
        ref_identity = result.reference_identity
        _check_reference_identity(
            self.reference_payload,
            ref_identity,
        )
        self.candidate_anchor = _check_candidate_identity(
            self.candidate_anchor,
            _candidate_identity_payload(result),
        )
        _check_exogenous_fingerprint(
            self.anchored_exogenous,
            evaluation.exogenous_invariant_fingerprint,
        )
        self.decision_capacities.add(float(capacity_mw))
        point_ids = set(ref_identity.sample_ids)
        generation_identity = canonical_sample_set_identity(ref_identity)
        opened = False
        if generation_identity != self.generation_identity:
            if not self.exact_sample_ids <= point_ids:
                raise EqualRiskSearchError(
                    "SEARCH_IDENTITY_MISMATCH",
                    "trial evaluation dropped or replaced already-used samples",
                )
            self.archived_generations.append(
                {
                    "generation_identity": self.generation_identity,
                    "evaluations": dict(self.active),
                }
            )
            self.generation_identity = generation_identity
            self.exact_sample_ids = frozenset(point_ids)
            self.reference_risk = float(np.mean(list(ref_identity.values.values())))
            self.active = {}
            opened = True
        _update_sample_value_digests(
            self.sample_value_digests,
            ref_identity,
            point_ids,
        )
        record = ActivePointRecord(
            capacity_mw=float(capacity_mw),
            canonical_generation=self.generation_identity,
            reference_risk=self.reference_risk,
            trial_risk=self.reference_risk + result.stats.mean,
            difference_mean=result.stats.mean,
            difference_se=result.stats.standard_error,
            risk_convergence=evaluation.risk_convergence,
            convergence_criteria=evaluation.convergence_criteria,
            should_stop=evaluation.should_stop,
            recent_batch_evidence=evaluation.recent_batch_evidence,
            identity_fingerprint=_track_fingerprint(result),
            exogenous_invariant_fingerprint=(evaluation.exogenous_invariant_fingerprint),
            call_id=call_id,
            trace_index=trace_index,
        )
        _assert_no_same_generation_drift(
            self.active,
            record,
            float(capacity_mw),
        )
        self.active[float(capacity_mw)] = record
        return record, opened


@dataclass
class _SearchState:
    """Mutable search state shared by the pipeline phases."""

    config: EqualRiskSearchConfig
    reference: RiskSampleSeries
    evaluator: Callable[[float], EqualRiskPointEvaluation]
    request_more_samples: Callable[[], bool] | None
    registry: _ActiveEvaluationRegistry
    trace: list[SearchPoint] = field(default_factory=list)
    ledger: list[MonotonicityCheck] = field(default_factory=list)
    bound_decisions: list[BoundDecision] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    low: float = 0.0
    high: float = 0.0
    width_ok: bool = False
    bracket_formed: bool = False
    iterations_exhausted: bool = False
    monotonic_invalid: bool = False
    monotonic_unresolved: bool = False
    initial_form_done: bool = False
    extension_no_change: bool = False
    call_counter: int = 0
    iteration_counter: int = 0
    check_counter: int = 0
    decision_counter: int = 0
    generation_switches: int = 0


def _evaluate_once(
    state: _SearchState,
    capacity_mw: float,
    action: str,
) -> tuple[ActivePointRecord, bool]:
    """Perform exactly ONE evaluation: evaluator call -> identity check
    -> compact registration -> immutable trace append. Never replays and
    never recurses; generation switches are counted here and bounded by
    MAX_GENERATION_SWITCHES (SEARCH_SAMPLE_GENERATION_UNSTABLE)."""
    capacity = float(capacity_mw)
    evaluation = state.evaluator(capacity)
    if not isinstance(evaluation, EqualRiskPointEvaluation):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_POINT",
            "evaluator must return an EqualRiskPointEvaluation",
        )
    call_id = state.call_counter
    state.call_counter += 1
    if state.registry.anchored_exogenous is None:
        state.registry.anchored_exogenous = evaluation.exogenous_invariant_fingerprint
    record, opened = state.registry.register(
        capacity,
        evaluation,
        call_id=call_id,
        trace_index=len(state.trace),
    )
    if opened:
        state.generation_switches += 1
        if state.generation_switches > MAX_GENERATION_SWITCHES:
            raise EqualRiskSearchError(
                DIAGNOSTIC_GENERATION_UNSTABLE,
                "sample generation switched more than "
                f"{MAX_GENERATION_SWITCHES} times during the search",
            )
    paired = evaluation.paired_result
    point = SearchPoint(
        capacity_mw=capacity,
        reference_risk=record.reference_risk,
        trial_risk=record.trial_risk,
        difference_mean=record.difference_mean,
        difference_se=record.difference_se,
        sample_count=paired.stats.sample_count,
        nonzero_sample_count=paired.stats.nonzero_sample_count,
        bracket_low_before=state.low,
        bracket_high_before=state.high,
        bracket_low_after=state.low,
        bracket_high_after=state.high,
        risk_convergence=record.risk_convergence,
        convergence_criteria=record.convergence_criteria,
        scenario_bank_id=paired.scenario_bank_id,
        track_fingerprint=record.identity_fingerprint,
        exogenous_invariant_fingerprint=(record.exogenous_invariant_fingerprint),
        sample_set_identity=evaluation.sample_set_identity,
        canonical_sample_set_identity=record.canonical_generation,
        call_id=call_id,
        iteration=state.iteration_counter,
        action=action,
    )
    state.iteration_counter += 1
    state.trace.append(point)
    return record, opened


def _ensure_active(
    state: _SearchState,
    capacity_mw: float,
    action: str,
) -> tuple[ActivePointRecord, bool]:
    """Return the ACTIVE-generation record for a capacity, evaluating it
    only when it is missing from the active set."""
    record = state.registry.active.get(float(capacity_mw))
    if record is not None:
        return record, False
    return _evaluate_once(state, capacity_mw, action)


def _stabilize_generation(state: _SearchState) -> None:
    """Non-recursive fixed-point loop: replay the FULL decision-capacity
    inventory; a generation switch discovered mid-round discards the
    earlier active results of that round and restarts from the complete
    inventory. Returns only after one complete round without any
    sample-generation change."""
    while True:
        switched = False
        for capacity in sorted(state.registry.decision_capacities):
            _, opened = _evaluate_once(state, capacity, "re-evaluate")
            if opened:
                switched = True
                break
        if not switched:
            return


def _significance_critical(config: EqualRiskSearchConfig) -> float:
    from scipy import stats as sp_stats

    return float(sp_stats.norm.ppf(0.5 + config.confidence_level / 2.0))


def _monotonicity_checks(
    state: _SearchState,
    records: list[ActivePointRecord],
    phase: str,
) -> list[MonotonicityCheck]:
    """Build a check for EVERY consecutive active capacity pair
    (acceptance AND rejection comparisons), with unique check_ids,
    phase, canonical generation and both endpoints' call_ids."""
    critical = _significance_critical(state.config)
    ordered = sorted(records, key=lambda r: r.capacity_mw)
    checks: list[MonotonicityCheck] = []
    for lower, higher in itertools.pairwise(ordered):
        if higher.capacity_mw <= lower.capacity_mw:
            continue
        if state.config.monotonic_direction == _INCREASING:
            reversed_mean = higher.difference_mean < lower.difference_mean
        else:
            reversed_mean = higher.difference_mean > lower.difference_mean
        interval_low = (
            lower.difference_mean - critical * lower.difference_se,
            lower.difference_mean + critical * lower.difference_se,
        )
        interval_high = (
            higher.difference_mean - critical * higher.difference_se,
            higher.difference_mean + critical * higher.difference_se,
        )
        if state.config.monotonic_direction == _INCREASING:
            separated = interval_high[1] < interval_low[0]
        else:
            separated = interval_high[0] > interval_low[1]
        checks.append(
            MonotonicityCheck(
                check_id=state.check_counter,
                phase=phase,
                lower_capacity=lower.capacity_mw,
                higher_capacity=higher.capacity_mw,
                lower_difference=lower.difference_mean,
                higher_difference=higher.difference_mean,
                lower_se=lower.difference_se,
                higher_se=higher.difference_se,
                critical_value=critical,
                lower_interval=interval_low,
                higher_interval=interval_high,
                mean_reversed=reversed_mean,
                significant=separated and reversed_mean,
                sample_generation=state.registry.generation_identity,
                lower_call_id=lower.call_id,
                higher_call_id=higher.call_id,
            )
        )
        state.check_counter += 1
    return checks


def _request_real_extension(state: _SearchState) -> str:
    """Invoke request_more_samples and verify a REAL canonical generation
    change by re-evaluating one decision point. Returns:
    "refused"   - no callback or callback returned False;
    "unchanged" - callback returned True but the canonical sample
                  generation did NOT change (same batch must not be
                  treated as appended samples; a stable
                  SEARCH_EXTENSION_NO_GENERATION_CHANGE diagnostic is
                  recorded, never invalid_method);
    "restart"   - a real generation change occurred (already stabilized,
                  the caller must restart from FORM_BRACKET)."""
    if state.request_more_samples is None or state.extension_no_change:
        return "refused"
    generation_before = state.registry.generation_identity
    granted = state.request_more_samples()
    if not isinstance(granted, bool):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CALLBACK",
            "request_more_samples must return a strict boolean",
        )
    if not granted:
        return "refused"
    # re-evaluate one decision point to prove the samples really changed
    _, opened = _evaluate_once(
        state,
        min(state.registry.decision_capacities),
        "extension_probe",
    )
    if opened or state.registry.generation_identity != generation_before:
        _stabilize_generation(state)
        return "restart"
    state.extension_no_change = True
    state.diagnostics.append(DIAGNOSTIC_EXTENSION_NO_CHANGE)
    return "unchanged"


def _assess_monotonicity(state: _SearchState, phase: str) -> str:
    """Assess monotonicity over the ACTIVE points, recording every
    pairwise comparison to the ledger. Returns:
    "ok" | "invalid" | "unresolved" | "restart" (a real extension via
    the callback restarted the pipeline)."""
    records = sorted(
        state.registry.active.values(),
        key=lambda r: r.capacity_mw,
    )
    checks = _monotonicity_checks(state, records, phase)
    state.ledger.extend(checks)
    significant = any(c.significant for c in checks)
    mean_reversed = any(c.mean_reversed and not c.significant for c in checks)
    if significant:
        if state.generation_switches > 0:
            return "invalid"
        outcome = _request_real_extension(state)
        if outcome == "restart":
            return "restart"
        if outcome == "unchanged":
            return "unresolved"
        return "unresolved"
    if mean_reversed:
        return "unresolved"
    return "ok"


def _rebuild_bracket(
    state: _SearchState,
) -> tuple[bool, bool]:
    """Rebuild the bracket from the ACTIVE generation's points only:
    tightest legal cross-zero interval (risk_increases_with_capacity:
    largest capacity with difference <= 0 vs smallest capacity with
    difference >= 0; the decreasing direction reverses the sides).
    Exact difference == 0 allows an endpoint solution [c, c]; no legal
    interval returns (False, False) -> SEARCH_BRACKET_NOT_FORMED.
    Recomputes width_ok."""
    records = sorted(
        state.registry.active.values(),
        key=lambda r: r.capacity_mw,
    )
    if state.config.monotonic_direction == _INCREASING:
        low_side = [r for r in records if r.difference_mean <= 0.0]
        high_side = [r for r in records if r.difference_mean >= 0.0]
    else:
        low_side = [r for r in records if r.difference_mean >= 0.0]
        high_side = [r for r in records if r.difference_mean <= 0.0]
    if not low_side or not high_side:
        return False, False
    low_record = low_side[-1]
    high_record = high_side[0]
    if low_record.capacity_mw > high_record.capacity_mw:
        return False, False
    state.low = low_record.capacity_mw
    state.high = high_record.capacity_mw
    state.width_ok = state.high - state.low <= state.config.capacity_tolerance_mw
    return True, state.width_ok


def _endpoints_form_bracket(state: _SearchState) -> bool:
    target = state.registry.reference_risk
    low_rec = state.registry.active[state.low]
    high_rec = state.registry.active[state.high]
    if state.config.monotonic_direction == _INCREASING:
        return low_rec.trial_risk <= target <= high_rec.trial_risk
    return high_rec.trial_risk <= target <= low_rec.trial_risk


def _form_phase(state: _SearchState) -> str:
    """FORM_BRACKET: on the INITIAL entry evaluate the endpoints (+
    optional expansion probe) and the screening grid; after a generation
    switch the bracket is REBUILT from the active generation's points
    only. Returns "restart" when a generation switch occurred."""
    if not state.initial_form_done:
        _, opened = _ensure_active(
            state,
            state.config.lower_bound_mw,
            "evaluate_lower_bound",
        )
        if opened:
            _stabilize_generation(state)
            return "restart"
        if state.config.allow_upper_bound_expansion:
            _, opened = _ensure_active(
                state,
                state.config.physical_upper_bound_mw,
                "evaluate_upper_bound",
            )
            if opened:
                _stabilize_generation(state)
                return "restart"
            if not _endpoints_form_bracket(state):
                state.diagnostics.append(DIAGNOSTIC_BRACKET_EXPANDED)
                expanded = float(
                    state.config.max_expanded_upper_bound_mw or state.config.physical_upper_bound_mw
                )
                _, opened = _ensure_active(
                    state,
                    expanded,
                    "evaluate_upper_bound",
                )
                if opened:
                    _stabilize_generation(state)
                    return "restart"
                state.high = expanded
        else:
            _, opened = _ensure_active(
                state,
                state.config.physical_upper_bound_mw,
                "evaluate_upper_bound",
            )
            if opened:
                _stabilize_generation(state)
                return "restart"
        for fraction in (0.2, 0.4, 0.6, 0.8):
            screen_capacity = state.config.lower_bound_mw + fraction * (
                state.high - state.config.lower_bound_mw
            )
            _, opened = _ensure_active(state, screen_capacity, "screening")
            if opened:
                _stabilize_generation(state)
                return "restart"
        state.initial_form_done = True
        state.bracket_formed = _endpoints_form_bracket(state)
        state.width_ok = state.high - state.low <= state.config.capacity_tolerance_mw
        return "ok"
    state.bracket_formed, state.width_ok = _rebuild_bracket(state)
    return "ok"


def _bisect_phase(state: _SearchState) -> str:
    """BISECT: evaluate/ensure each midpoint under the generation
    barrier; bound updates are BoundDecision records tied to the
    ACTIVE-generation record. Returns "restart" on a generation
    switch, "done" otherwise."""
    state.iterations_exhausted = False
    for iteration in range(1, state.config.max_iterations + 1):
        mid = (state.low + state.high) / 2.0
        record, opened = _ensure_active(state, mid, "bisect")
        if opened:
            _stabilize_generation(state)
            return "restart"
        decision = _assess_monotonicity(state, "bisect")
        if decision == "restart":
            return "restart"
        if decision == "invalid":
            state.monotonic_invalid = True
            return "done"
        if decision == "unresolved":
            state.monotonic_unresolved = True
            return "done"
        difference = record.difference_mean
        if state.config.monotonic_direction == _INCREASING:
            if difference > 0.0:
                new_low, new_high = state.low, mid
            else:
                new_low, new_high = mid, state.high
        else:
            if difference > 0.0:
                new_low, new_high = mid, state.high
            else:
                new_low, new_high = state.low, mid
        state.bound_decisions.append(
            BoundDecision(
                decision_id=state.decision_counter,
                phase="bisect",
                capacity_mw=mid,
                call_id=record.call_id,
                canonical_generation=record.canonical_generation,
                difference_mean=difference,
                low_before=state.low,
                high_before=state.high,
                low_after=new_low,
                high_after=new_high,
            )
        )
        state.decision_counter += 1
        state.low, state.high = new_low, new_high
        if state.high - state.low <= state.config.capacity_tolerance_mw:
            state.width_ok = True
            return "done"
    state.iterations_exhausted = True
    return "done"


def _finalize_phase(state: _SearchState) -> str:
    """FINALIZE: re-evaluate the final endpoints under the generation
    barrier; a generation switch here (endpoints or the extension
    callback) restarts the whole pipeline so bracket_formed is recomputed
    from the new active generation. A request_more_samples() that
    returns True WITHOUT a real generation change is a stable
    expansion-failure diagnostic, never invalid_method."""
    if not state.monotonic_invalid:
        _, opened = _evaluate_once(state, state.low, "evaluate_final_low")
        if opened:
            _stabilize_generation(state)
            return "restart"
        _, opened = _evaluate_once(state, state.high, "evaluate_final_high")
        if opened:
            _stabilize_generation(state)
            return "restart"
        # FINALIZE state refresh: after the final endpoint re-evaluation
        # (which is reproducible by construction - drift raises
        # SEARCH_EVALUATION_DRIFT), recompute the bracket state from the
        # CURRENT active records so bracket_formed/width_ok can never be
        # stale relative to the records used for assembly.
        state.bracket_formed = _endpoints_form_bracket(state)
        state.width_ok = state.high - state.low <= state.config.capacity_tolerance_mw
        decision = _assess_monotonicity(state, "finalize")
        if decision == "restart":
            return "restart"
        if decision == "invalid":
            state.monotonic_invalid = True
            return "done"
        if decision == "unresolved":
            state.monotonic_unresolved = True
            return "done"
        if state.bracket_formed and state.request_more_samples is not None:
            low_rec = state.registry.active[state.low]
            high_rec = state.registry.active[state.high]
            endpoint_risk = combine_endpoint_risk_convergence(
                low_rec.risk_convergence,
                high_rec.risk_convergence,
            )
            if endpoint_risk is not RiskConvergence.CONVERGED:
                outcome = _request_real_extension(state)
                if outcome == "restart":
                    return "restart"
    return "done"


def _empirical_risk_resolution(
    metric: RiskMetric,
    final_generation_risks: list[float],
    low_risk: float,
    high_risk: float,
) -> float | None:
    """Frozen dimension: EUE -> endpoint local span (MWh/year); other
    metrics -> smallest positive risk step among the FINAL-generation
    trial risks. None when not computable (never 0 for unknown)."""
    if metric in _EUE_METRICS:
        return abs(high_risk - low_risk)
    unique = sorted(set(final_generation_risks))
    steps = [b - a for a, b in itertools.pairwise(unique) if b > a]
    if not steps:
        return None
    return min(steps)


def _assemble_result(state: _SearchState) -> EqualRiskSearchResult:
    """Assemble the solver-internal result from the ACTIVE generation's
    records; bracket_formed/width_ok are the real pipeline variables
    (never hard-coded True)."""
    low_rec = state.registry.active[state.low]
    high_rec = state.registry.active[state.high]
    low_risk = low_rec.trial_risk
    high_risk = high_rec.trial_risk
    endpoint_risk = combine_endpoint_risk_convergence(
        low_rec.risk_convergence,
        high_rec.risk_convergence,
    )
    plateau = (state.low != state.high) and (low_risk == high_risk)
    estimate_mw, interpolated, interpolation_method = _interpolate_point(
        state,
        low_risk,
        high_risk,
        endpoint_risk,
        plateau,
    )

    risk_c, search_c, status_c = classify_search_outcome(
        risk_convergence=endpoint_risk,
        bracket_formed=state.bracket_formed,
        width_within_tolerance=state.width_ok,
        monotonic_ok=not state.monotonic_invalid,
        monotonic_unresolved=state.monotonic_unresolved,
        interpolated=interpolated,
        plateau=plateau,
    )
    # post-classification consistency guard: a non-valid result must
    # never carry a point estimate or a chord interpolation method;
    # status, diagnostics and values must not contradict each other.
    if status_c is not ResultStatus.VALID:
        estimate_mw = None
        interpolation_method = InterpolationMethod.NOT_APPLICABLE
    resolution = _empirical_risk_resolution(
        state.reference.primary_risk_metric,
        [r.trial_risk for r in state.registry.active.values()],
        low_risk,
        high_risk,
    )
    final_diagnostics = _collect_result_diagnostics(
        state,
        plateau,
        endpoint_risk,
        resolution,
    )
    return EqualRiskSearchResult(
        estimate_mw=estimate_mw,
        bracket_low_mw=state.low,
        bracket_high_mw=state.high,
        low_endpoint_mw=state.low,
        high_endpoint_mw=state.high,
        low_endpoint_risk=low_risk,
        high_endpoint_risk=high_risk,
        empirical_risk_resolution=resolution,
        interpolation_method=interpolation_method,
        risk_convergence=risk_c,
        search_convergence=search_c,
        result_status=status_c,
        trace=tuple(state.trace),
        monotonicity_ledger=tuple(state.ledger),
        bound_decisions=tuple(state.bound_decisions),
        diagnostics=tuple(final_diagnostics),
    )


def _interpolate_point(
    state: _SearchState,
    low_risk: float,
    high_risk: float,
    endpoint_risk: RiskConvergence,
    plateau: bool,
) -> tuple[float | None, bool, InterpolationMethod]:
    """Explicit point-estimate preconditions: a legal bracket must exist,
    the width must be within tolerance, the endpoint risk converged,
    monotonicity valid AND resolved, and the interpolant must lie inside
    the interval. A missing bracket NEVER produces an estimate."""
    if not state.bracket_formed or plateau or not state.width_ok:
        return None, False, InterpolationMethod.NOT_APPLICABLE
    if endpoint_risk is not RiskConvergence.CONVERGED:
        return None, False, InterpolationMethod.NOT_APPLICABLE
    if state.monotonic_invalid or state.monotonic_unresolved:
        return None, False, InterpolationMethod.NOT_APPLICABLE
    target = state.registry.reference_risk
    if low_risk == target:
        return state.low, True, InterpolationMethod.CHORD
    if high_risk == target:
        return state.high, True, InterpolationMethod.CHORD
    if not (low_risk < target < high_risk or high_risk < target < low_risk):
        return None, False, InterpolationMethod.NOT_APPLICABLE
    estimate = state.low + (target - low_risk) / (high_risk - low_risk) * (state.high - state.low)
    if state.low <= estimate <= state.high:
        return estimate, True, InterpolationMethod.CHORD
    return None, False, InterpolationMethod.NOT_APPLICABLE


def _collect_result_diagnostics(
    state: _SearchState,
    plateau: bool,
    endpoint_risk: RiskConvergence,
    resolution: float | None,
) -> list[str]:
    """Pure diagnostic assembly from the final pipeline state."""
    final_diagnostics = list(state.diagnostics)
    if plateau:
        final_diagnostics.append(DIAGNOSTIC_INTERPOLATION_UNRESOLVED)
    if state.monotonic_invalid:
        final_diagnostics.append(DIAGNOSTIC_NOT_MONOTONIC)
    if not state.monotonic_invalid and state.monotonic_unresolved:
        final_diagnostics.append(DIAGNOSTIC_MONOTONIC_UNRESOLVED)
    if not state.bracket_formed:
        final_diagnostics.append(DIAGNOSTIC_BRACKET_NOT_FORMED)
    if state.iterations_exhausted and not plateau and not state.width_ok:
        final_diagnostics.append(DIAGNOSTIC_MAX_ITERATIONS)
    if endpoint_risk is not RiskConvergence.CONVERGED and not plateau:
        final_diagnostics.append(DIAGNOSTIC_RISK_NOT_CONVERGED)
    if resolution is None:
        final_diagnostics.append(DIAGNOSTIC_RESOLUTION_UNAVAILABLE)
    return final_diagnostics


def _validate_search_callables(
    evaluator: Any,
    request_more_samples: Any,
) -> None:
    """Pure entry validation: evaluator must be callable; the extension
    callback must be callable or None."""
    if not callable(evaluator):
        raise EqualRiskSearchError("SEARCH_INVALID_CONFIG", "evaluator must be callable")
    if request_more_samples is not None and not callable(request_more_samples):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CALLBACK",
            "request_more_samples must be callable or None",
        )


def search_equal_risk(
    *,
    config: EqualRiskSearchConfig,
    reference: RiskSampleSeries,
    evaluator: Callable[[float], EqualRiskPointEvaluation],
    request_more_samples: Callable[[], bool] | None = None,
) -> EqualRiskSearchResult:
    """Run the equal-risk search over the given evaluator.

    The iterative generation barrier wraps every pipeline stage: a
    generation switch anywhere re-enters FORM_BRACKET ->
    CHECK_MONOTONICITY -> BISECT -> FINALIZE with the bracket rebuilt
    from the new active generation; the number of switches is bounded by
    MAX_GENERATION_SWITCHES. Every evaluator call produces exactly one
    immutable SearchPoint with a unique call_id; bound updates are
    BoundDecision records tied to the active-generation evaluation.
    """
    if not isinstance(config, EqualRiskSearchConfig):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_CONFIG", "config must be an EqualRiskSearchConfig"
        )
    if not isinstance(reference, RiskSampleSeries):
        raise EqualRiskSearchError(
            "SEARCH_INVALID_REFERENCE", "reference must be a RiskSampleSeries"
        )
    _validate_search_callables(evaluator, request_more_samples)
    state = _SearchState(
        config=config,
        reference=reference,
        evaluator=evaluator,
        request_more_samples=request_more_samples,
        registry=_ActiveEvaluationRegistry(reference),
    )
    state.low = config.lower_bound_mw
    state.high = config.physical_upper_bound_mw
    while True:
        outcome = _form_phase(state)
        if outcome == "restart":
            continue
        decision = _assess_monotonicity(state, "form")
        if decision == "restart":
            continue
        state.monotonic_invalid = decision == "invalid"
        state.monotonic_unresolved = decision == "unresolved"
        if not state.bracket_formed or state.monotonic_invalid or state.monotonic_unresolved:
            outcome = _finalize_phase(state)
            if outcome == "restart":
                continue
            return _assemble_result(state)
        outcome = _bisect_phase(state)
        if outcome == "restart":
            continue
        outcome = _finalize_phase(state)
        if outcome == "restart":
            continue
        return _assemble_result(state)


__all__ = [
    "ALLOWED_STATUS_COMBOS",
    "DIAGNOSTIC_BRACKET_EXPANDED",
    "DIAGNOSTIC_EXTENSION_NO_CHANGE",
    "DIAGNOSTIC_GENERATION_UNSTABLE",
    "DIAGNOSTIC_INTERPOLATION_UNRESOLVED",
    "DIAGNOSTIC_MAX_ITERATIONS",
    "DIAGNOSTIC_MONOTONIC_UNRESOLVED",
    "DIAGNOSTIC_NOT_MONOTONIC",
    "DIAGNOSTIC_RESOLUTION_UNAVAILABLE",
    "DIAGNOSTIC_RISK_NOT_CONVERGED",
    "MAX_GENERATION_SWITCHES",
    "ActivePointRecord",
    "BoundDecision",
    "EqualRiskPointEvaluation",
    "EqualRiskSearchConfig",
    "EqualRiskSearchError",
    "EqualRiskSearchResult",
    "MonotonicityCheck",
    "RiskConvergenceSnapshot",
    "SearchPoint",
    "canonical_sample_set_identity",
    "classify_search_outcome",
    "combine_endpoint_risk_convergence",
    "search_equal_risk",
]
