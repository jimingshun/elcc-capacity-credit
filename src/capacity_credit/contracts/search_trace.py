"""Strict search-trace artifact contract (T12 remediation, schema 1.0).

The full ELCC search trace is published as an independent, machine
readable artifact (never merged into the MethodResult 1.3 JSON). The
contract is typed and versioned: writing serializes through the model,
reading re-validates strictly, and any tampering (missing fields, extra
fields, wrong schema version, wrong values, semantic violations) is
rejected on READ-BACK as well as at write time.

Semantic contract (round-2 remediation):

- numeric fields reject NaN / Inf and bool-typed values;
- capacities, sample counts and standard errors are non-negative;
- every bracket snapshot satisfies low <= high (before and after);
- ``load_growth_mode`` is one of fixed_mw / scaling / specified /
  efc_perfect_capacity (EFC traces use the perfect-capacity search
  variable and never a load-growth shape);
- sample-set identity and canonical generation are non-blank strings;
- confidence intervals satisfy lower <= upper;
- every BoundDecision.call_id and every monotonicity lower/higher
  call_id references an existing trace point;
- the artifact-level sample-set identity / canonical generation must
  equal the FINAL trace point's identity / generation;
- a valid/converged result must carry an estimate and at least one
  search point; a non-valid result must NOT carry an estimate;
- an estimate must lie inside the final bracket.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capacity_credit.contracts.elcc_status import validate_trace_status_combo
from capacity_credit.contracts.enums import (
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    SearchConvergence,
)
from capacity_credit.domain.risk_units import risk_metric_unit
from capacity_credit.solvers.equal_risk import (
    BoundDecision,
    MonotonicityCheck,
    SearchPoint,
)

TRACE_SCHEMA_VERSION: Literal["1.0"] = "1.0"

_VALID_LOAD_GROWTH_MODES = frozenset({"fixed_mw", "scaling", "specified", "efc_perfect_capacity"})


def _finite(value: object, field: str, *, non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a real number")  # noqa: TRY004  # noqa: TRY004
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if non_negative and number < 0.0:
        raise ValueError(f"{field} must be non-negative")
    return number


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a strict integer")  # noqa: TRY004
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


def _non_blank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value.strip()


class SearchTracePoint(BaseModel):
    """One trace point (mirrors SearchPoint)."""

    model_config = ConfigDict(extra="forbid")

    call_id: int
    iteration: int
    action: str
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
    scenario_bank_id: str
    track_fingerprint: str
    exogenous_invariant_fingerprint: str
    sample_set_identity: str
    canonical_sample_set_identity: str

    @field_validator(
        "capacity_mw",
        "bracket_low_before",
        "bracket_high_before",
        "bracket_low_after",
        "bracket_high_after",
        mode="before",
    )
    @classmethod
    def _non_negative_float(cls, value: object, info: Any) -> object:
        return _finite(value, info.field_name or "value", non_negative=True)

    @field_validator("reference_risk", "trial_risk", "difference_mean", mode="before")
    @classmethod
    def _finite_float(cls, value: object, info: Any) -> object:
        return _finite(value, info.field_name or "value")

    @field_validator("difference_se", mode="before")
    @classmethod
    def _non_negative_se(cls, value: object) -> object:
        return _finite(value, "difference_se", non_negative=True)

    @field_validator("call_id", "iteration", "sample_count", "nonzero_sample_count", mode="before")
    @classmethod
    def _int_fields(cls, value: object, info: Any) -> object:
        return _non_negative_int(value, info.field_name or "value")

    @field_validator("sample_set_identity", "canonical_sample_set_identity", mode="before")
    @classmethod
    def _identity(cls, value: object, info: Any) -> object:
        return _non_blank(value, info.field_name or "identity")

    @field_validator(
        "action",
        "scenario_bank_id",
        "track_fingerprint",
        "exogenous_invariant_fingerprint",
        mode="before",
    )
    @classmethod
    def _text(cls, value: object, info: Any) -> object:
        return _non_blank(value, info.field_name or "text")

    @model_validator(mode="after")
    def _validate_brackets(self) -> Self:
        if self.bracket_low_before > self.bracket_high_before:
            raise ValueError("bracket_low_before must not exceed bracket_high_before")
        if self.bracket_low_after > self.bracket_high_after:
            raise ValueError("bracket_low_after must not exceed bracket_high_after")
        if self.nonzero_sample_count > self.sample_count:
            raise ValueError("nonzero_sample_count must not exceed sample_count")
        return self


class SearchTraceBoundDecision(BaseModel):
    """One bound decision (mirrors BoundDecision)."""

    model_config = ConfigDict(extra="forbid")

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

    @field_validator(
        "capacity_mw",
        "low_before",
        "high_before",
        "low_after",
        "high_after",
        mode="before",
    )
    @classmethod
    def _non_negative_float(cls, value: object, info: Any) -> object:
        return _finite(value, info.field_name or "value", non_negative=True)

    @field_validator("difference_mean", mode="before")
    @classmethod
    def _finite_float(cls, value: object) -> object:
        return _finite(value, "difference_mean")

    @field_validator("decision_id", "call_id", mode="before")
    @classmethod
    def _int_fields(cls, value: object, info: Any) -> object:
        return _non_negative_int(value, info.field_name or "value")

    @field_validator("phase", "canonical_generation", mode="before")
    @classmethod
    def _text(cls, value: object, info: Any) -> object:
        return _non_blank(value, info.field_name or "text")

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.low_before > self.high_before:
            raise ValueError("low_before must not exceed high_before")
        if self.low_after > self.high_after:
            raise ValueError("low_after must not exceed high_after")
        return self


class SearchTraceMonotonicityCheck(BaseModel):
    """One monotonicity ledger entry (mirrors MonotonicityCheck)."""

    model_config = ConfigDict(extra="forbid")

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

    @field_validator(
        "lower_capacity",
        "higher_capacity",
        "critical_value",
        mode="before",
    )
    @classmethod
    def _non_negative_float(cls, value: object, info: Any) -> object:
        return _finite(value, info.field_name or "value", non_negative=True)

    @field_validator(
        "lower_difference",
        "higher_difference",
        mode="before",
    )
    @classmethod
    def _finite_float(cls, value: object, info: Any) -> object:
        return _finite(value, info.field_name or "value")

    @field_validator("lower_se", "higher_se", mode="before")
    @classmethod
    def _non_negative_se(cls, value: object, info: Any) -> object:
        return _finite(value, info.field_name or "value", non_negative=True)

    @field_validator("check_id", "lower_call_id", "higher_call_id", mode="before")
    @classmethod
    def _int_fields(cls, value: object, info: Any) -> object:
        return _non_negative_int(value, info.field_name or "value")

    @field_validator("mean_reversed", "significant", mode="before")
    @classmethod
    def _bool_fields(cls, value: object, info: Any) -> object:
        if not isinstance(value, bool):
            raise ValueError(  # noqa: TRY004
                f"{info.field_name or 'flag'} must be a strict boolean"
            )
        return value

    @field_validator("phase", "sample_generation", mode="before")
    @classmethod
    def _text(cls, value: object, info: Any) -> object:
        return _non_blank(value, info.field_name or "text")

    @field_validator("lower_interval", "higher_interval", mode="before")
    @classmethod
    def _interval(cls, value: object, info: Any) -> object:
        field = info.field_name or "interval"
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"{field} must be a (lower, upper) pair")
        lower = _finite(value[0], f"{field}[0]")
        upper = _finite(value[1], f"{field}[1]")
        if lower > upper:
            raise ValueError(f"{field} lower bound must not exceed the upper bound")
        return (lower, upper)

    @model_validator(mode="after")
    def _validate_capacity_order(self) -> Self:
        if self.higher_capacity <= self.lower_capacity:
            raise ValueError("higher_capacity must exceed lower_capacity")
        return self


def _validate_bracket_and_growth(artifact: SearchTraceArtifact) -> None:
    """Bracket ordering, legal load-growth mode and estimate-in-bracket."""
    if artifact.bracket_low_mw > artifact.bracket_high_mw:
        raise ValueError("bracket_low_mw must not exceed bracket_high_mw")
    if artifact.load_growth_mode not in _VALID_LOAD_GROWTH_MODES:
        raise ValueError(f"load_growth_mode must be one of {sorted(_VALID_LOAD_GROWTH_MODES)}")
    if artifact.estimate_mw is not None and not (
        artifact.bracket_low_mw <= artifact.estimate_mw <= artifact.bracket_high_mw
    ):
        raise ValueError("estimate_mw must lie inside the final bracket")


def _validate_call_references(
    artifact: SearchTraceArtifact,
    point_call_ids: set[int],
) -> None:
    """Every BoundDecision / monotonicity call_id must reference an
    existing trace point."""
    for decision in artifact.bound_decisions:
        if decision.call_id not in point_call_ids:
            raise ValueError(
                f"bound decision call_id {decision.call_id} references a non-existent trace point"
            )
    for check in artifact.monotonicity_ledger:
        if check.lower_call_id not in point_call_ids:
            raise ValueError(
                f"monotonicity lower_call_id {check.lower_call_id} "
                "references a non-existent trace point"
            )
        if check.higher_call_id not in point_call_ids:
            raise ValueError(
                f"monotonicity higher_call_id {check.higher_call_id} "
                "references a non-existent trace point"
            )


def _validate_identity_consistency(artifact: SearchTraceArtifact) -> None:
    """The artifact-level sample identity / canonical generation must
    equal the FINAL trace point's identity / generation."""
    if not artifact.points:
        return
    final = artifact.points[-1]
    if final.sample_set_identity != artifact.sample_set_identity:
        raise ValueError(
            "artifact sample_set_identity must equal the final trace point's sample_set_identity"
        )
    if final.canonical_sample_set_identity != artifact.canonical_sample_set_identity:
        raise ValueError(
            "artifact canonical_sample_set_identity must equal the "
            "final trace point's canonical_sample_set_identity"
        )


def _validate_result_state(artifact: SearchTraceArtifact) -> None:
    """UNIFIED status rule (shared with the search classifier and the
    ELCC result guard) + estimate rules: valid requires converged
    states AND an estimate + at least one point; any non-valid result
    must NOT carry an estimate."""
    validate_trace_status_combo(
        risk_convergence=artifact.risk_convergence,
        search_convergence=artifact.search_convergence,
        result_status=artifact.result_status,
    )
    # valid_with_warning keeps the point estimate (product rule: search
    # valid + upstream warning); all other non-valid states must not
    # carry an estimate.
    if artifact.result_status in (ResultStatus.VALID, ResultStatus.VALID_WITH_WARNING):
        if artifact.estimate_mw is None:
            raise ValueError("a valid/converged result must carry an estimate_mw")
        if not artifact.points:
            raise ValueError("a valid/converged result must carry at least one search point")
    else:
        if artifact.estimate_mw is not None:
            raise ValueError("a non-valid result must not carry an estimate_mw")


def _validate_identity_uniqueness(artifact: SearchTraceArtifact) -> None:
    """call_id / decision_id / check_id uniqueness across the whole
    trace."""
    point_ids = [point.call_id for point in artifact.points]
    if len(set(point_ids)) != len(point_ids):
        raise ValueError("point call_ids must be unique across the trace")
    decision_ids = [decision.decision_id for decision in artifact.bound_decisions]
    if len(set(decision_ids)) != len(decision_ids):
        raise ValueError("decision_ids must be unique across the trace")
    check_ids = [check.check_id for check in artifact.monotonicity_ledger]
    if len(set(check_ids)) != len(check_ids):
        raise ValueError("check_ids must be unique across the trace")


def _validate_point_consistency(artifact: SearchTraceArtifact) -> None:
    """All trace points must share the same scenario bank and the same
    track / exogenous fingerprints (one consistent search run)."""
    if not artifact.points:
        return
    banks = {point.scenario_bank_id for point in artifact.points}
    if len(banks) != 1:
        raise ValueError("all trace points must share one scenario_bank_id")
    tracks = {point.track_fingerprint for point in artifact.points}
    if len(tracks) != 1:
        raise ValueError("all trace points must share one track_fingerprint")
    exogenous = {point.exogenous_invariant_fingerprint for point in artifact.points}
    if len(exogenous) != 1:
        raise ValueError("all trace points must share one exogenous_invariant_fingerprint")


class SearchTraceArtifact(BaseModel):
    """Strict, versioned search-trace artifact."""

    model_config = ConfigDict(extra="forbid")

    trace_schema_version: Literal["1.0"]
    primary_risk_metric: RiskMetric
    risk_metric_unit: str
    result_status: ResultStatus
    risk_convergence: RiskConvergence
    search_convergence: SearchConvergence
    estimate_mw: float | None
    bracket_low_mw: float
    bracket_high_mw: float
    load_growth_mode: str
    sample_set_identity: str
    canonical_sample_set_identity: str
    diagnostics: list[str] = Field(default_factory=list)
    points: list[SearchTracePoint] = Field(default_factory=list)
    bound_decisions: list[SearchTraceBoundDecision] = Field(default_factory=list)
    monotonicity_ledger: list[SearchTraceMonotonicityCheck] = Field(
        default_factory=list,
    )

    @field_validator(
        "risk_metric_unit",
        "load_growth_mode",
        "sample_set_identity",
        "canonical_sample_set_identity",
        mode="before",
    )
    @classmethod
    def _text(cls, value: object, info: Any) -> object:
        return _non_blank(value, info.field_name or "text")

    @field_validator("bracket_low_mw", "bracket_high_mw", mode="before")
    @classmethod
    def _non_negative_float(cls, value: object, info: Any) -> object:
        return _finite(value, info.field_name or "value", non_negative=True)

    @field_validator("estimate_mw", mode="before")
    @classmethod
    def _optional_float(cls, value: object) -> object:
        if value is None:
            return None
        return _finite(value, "estimate_mw", non_negative=True)

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _diagnostics(cls, value: object) -> object:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("diagnostics must be a list of strings")
        return list(value)

    @model_validator(mode="after")
    def _validate_semantics(self) -> Self:
        if self.risk_metric_unit != risk_metric_unit(self.primary_risk_metric):
            raise ValueError("risk_metric_unit must equal risk_metric_unit(primary_risk_metric)")
        _validate_bracket_and_growth(self)
        _validate_call_references(self, {point.call_id for point in self.points})
        _validate_identity_consistency(self)
        _validate_result_state(self)
        _validate_identity_uniqueness(self)
        _validate_point_consistency(self)
        return self


def _point_from_search(point: SearchPoint) -> SearchTracePoint:
    return SearchTracePoint(
        call_id=point.call_id,
        iteration=point.iteration,
        action=point.action,
        capacity_mw=point.capacity_mw,
        reference_risk=point.reference_risk,
        trial_risk=point.trial_risk,
        difference_mean=point.difference_mean,
        difference_se=point.difference_se,
        sample_count=point.sample_count,
        nonzero_sample_count=point.nonzero_sample_count,
        bracket_low_before=point.bracket_low_before,
        bracket_high_before=point.bracket_high_before,
        bracket_low_after=point.bracket_low_after,
        bracket_high_after=point.bracket_high_after,
        risk_convergence=point.risk_convergence,
        scenario_bank_id=point.scenario_bank_id,
        track_fingerprint=point.track_fingerprint,
        exogenous_invariant_fingerprint=point.exogenous_invariant_fingerprint,
        sample_set_identity=point.sample_set_identity,
        canonical_sample_set_identity=point.canonical_sample_set_identity,
    )


def _decision_from_search(decision: BoundDecision) -> SearchTraceBoundDecision:
    return SearchTraceBoundDecision(
        decision_id=decision.decision_id,
        phase=decision.phase,
        capacity_mw=decision.capacity_mw,
        call_id=decision.call_id,
        canonical_generation=decision.canonical_generation,
        difference_mean=decision.difference_mean,
        low_before=decision.low_before,
        high_before=decision.high_before,
        low_after=decision.low_after,
        high_after=decision.high_after,
    )


def _check_from_search(check: MonotonicityCheck) -> SearchTraceMonotonicityCheck:
    return SearchTraceMonotonicityCheck(
        check_id=check.check_id,
        phase=check.phase,
        lower_capacity=check.lower_capacity,
        higher_capacity=check.higher_capacity,
        lower_difference=check.lower_difference,
        higher_difference=check.higher_difference,
        lower_se=check.lower_se,
        higher_se=check.higher_se,
        critical_value=check.critical_value,
        lower_interval=check.lower_interval,
        higher_interval=check.higher_interval,
        mean_reversed=check.mean_reversed,
        significant=check.significant,
        sample_generation=check.sample_generation,
        lower_call_id=check.lower_call_id,
        higher_call_id=check.higher_call_id,
    )


def build_search_trace(
    *,
    primary_risk_metric: RiskMetric,
    risk_metric_unit: str,
    estimate_mw: float | None,
    bracket_low_mw: float,
    bracket_high_mw: float,
    result_status: ResultStatus,
    risk_convergence: RiskConvergence,
    search_convergence: SearchConvergence,
    load_growth_mode: str,
    sample_set_identity: str,
    canonical_sample_set_identity: str,
    diagnostics: list[str],
    trace: list[SearchPoint],
    bound_decisions: list[BoundDecision],
    monotonicity_ledger: list[MonotonicityCheck],
) -> SearchTraceArtifact:
    """Build the strict trace artifact from a real ELCC search result."""
    return SearchTraceArtifact(
        trace_schema_version=TRACE_SCHEMA_VERSION,
        primary_risk_metric=primary_risk_metric,
        risk_metric_unit=risk_metric_unit,
        result_status=result_status,
        risk_convergence=risk_convergence,
        search_convergence=search_convergence,
        estimate_mw=estimate_mw,
        bracket_low_mw=bracket_low_mw,
        bracket_high_mw=bracket_high_mw,
        load_growth_mode=load_growth_mode,
        sample_set_identity=sample_set_identity,
        canonical_sample_set_identity=canonical_sample_set_identity,
        diagnostics=list(diagnostics),
        points=[_point_from_search(p) for p in trace],
        bound_decisions=[_decision_from_search(d) for d in bound_decisions],
        monotonicity_ledger=[_check_from_search(c) for c in monotonicity_ledger],
    )


def write_search_trace(
    artifact: SearchTraceArtifact,
    path: Path | str,
) -> Path:
    """Write the strict trace artifact (model-serialized, verified by a
    read-back round trip)."""
    from capacity_credit.application.atomic_io import atomic_write_bytes

    path_obj = Path(path)
    payload = artifact.model_dump_json()
    atomic_write_bytes(
        path_obj,
        (payload + "\n").encode("utf-8"),
        immutable=True,
    )
    read_search_trace(path_obj)
    return path_obj


def read_search_trace(path: Path | str) -> SearchTraceArtifact:
    """Strict read-back: any malformed / tampered / semantically
    invalid payload is rejected."""
    path_obj = Path(path)
    try:
        payload = json.loads(path_obj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"search trace unreadable: {exc}") from exc
    return SearchTraceArtifact.model_validate(payload)


__all__ = [
    "TRACE_SCHEMA_VERSION",
    "SearchTraceArtifact",
    "SearchTraceBoundDecision",
    "SearchTraceMonotonicityCheck",
    "SearchTracePoint",
    "build_search_trace",
    "read_search_trace",
    "write_search_trace",
]
