"""T21 PRAS result summary and comparison contract (pure functions).

matched = model boundaries aligned AND joint-95%-interval agreement AND
complete evidence identity AND a fully accepted mapping audit (no
unsupported, no lossy, no provisional/mismatch entries). Numerical
agreement alone is never sufficient, and the mapping acceptance is
derived from the freshly recomputed audit - never from caller-supplied
structural differences.

not_comparable uses ONE shared gate (risk and capacity) requiring a
locked/audited/completed identity, unsupported fields that exactly match
the freshly recomputed mapping audit, non-blank evidence references and
a dual-role ApprovalRecord that is retained on the result.

ApprovalRecord is the single immutable approval record used by both
explained_difference and not_comparable paths.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
from scipy import stats as sp_stats

from capacity_credit.application.atomic_io import stable_dump
from capacity_credit.benchmark.pras_adapter import (
    PrasCaseDefinition,
    PrasVersionEvidence,
    audit_case_mapping,
    case_input_hash,
    mapping_completeness_blocks,
)

Z_975 = float(sp_stats.norm.ppf(0.975))

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CV_DEFINITIONS = frozenset({"efc_perfect_capacity", "elcc_load_growth"})


class PrasComparisonError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PrasComparisonStatus(Enum):
    MATCHED = "matched"
    EXPLAINED_DIFFERENCE = "explained_difference"
    UNRESOLVED = "unresolved"
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable dual-role approval record retained on results (used by
    both explained_difference and not_comparable)."""

    method_reviewer_approval: str
    release_reviewer_approval: str
    evidence_refs: tuple[str, ...] = ()
    attribution_report_ref: str | None = None

    def __post_init__(self) -> None:
        # strict non-empty strings: None / numbers / bools / blank are
        # rejected - never str()-coerced
        for label, value in (
            ("method_reviewer_approval", self.method_reviewer_approval),
            ("release_reviewer_approval", self.release_reviewer_approval),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PrasComparisonError(
                    "APPROVAL_BLANK",
                    f"{label} must be a non-empty string (None/number/bool/blank rejected)",
                )
        method = self.method_reviewer_approval.strip()
        release = self.release_reviewer_approval.strip()
        if method == release:
            raise PrasComparisonError("APPROVERS_MUST_DIFFER", "approvers must differ")
        if not self.evidence_refs:
            raise PrasComparisonError(
                "APPROVAL_NO_EVIDENCE_REFS",
                "at least one strict non-empty evidence reference is required",
            )
        for ref in self.evidence_refs:
            if not isinstance(ref, str) or not ref.strip():
                raise PrasComparisonError(
                    "APPROVAL_BLANK_REF",
                    "evidence references must be strict non-empty strings",
                )
        if self.attribution_report_ref is not None and (
            not isinstance(self.attribution_report_ref, str)
            or not self.attribution_report_ref.strip()
        ):
            raise PrasComparisonError("APPROVAL_BLANK_ATTRIBUTION", "attribution reference must not be blank")


@dataclass(frozen=True)
class RiskEstimate:
    metric: str
    estimate: float
    standard_error: float


@dataclass(frozen=True)
class CapacityValueEstimate:
    """Capacity-value estimate with a MACHINE-DISTINGUISHABLE value kind:

    - "point_se": estimate + standard_error (Python production path);
    - "interval": PRAS-native CapacityCreditResult bounds - the
      lower_bound_mw/upper_bound_mw interval IS the result; the midpoint
      is never presented as a point estimate and the half-width is never
      presented as a standard error;
    - "null": missing / not converged / not run - never silently
      coerced into a number.

    Point and interval representations are mutually exclusive per
    value_kind; the comparator uses the frozen interval-overlap /
    search-tolerance rules below.
    """

    metric: str
    estimate: float | None = None
    standard_error: float | None = None
    lower_bound_mw: float | None = None
    upper_bound_mw: float | None = None
    value_kind: str = "point_se"
    search_capacity_tolerance: float = 0.0
    definition: str = "efc_perfect_capacity"
    dispatch_protocol_id: str = ""
    dispatch_note: str = ""

    def __post_init__(self) -> None:
        if self.definition not in _CV_DEFINITIONS:
            raise PrasComparisonError(
                "CV_DEFINITION_UNCONTROLLED",
                f"definition must be one of {sorted(_CV_DEFINITIONS)}, got {self.definition!r}",
            )
        if self.value_kind == "point_se":
            if (
                self.estimate is None
                or self.standard_error is None
                or self.lower_bound_mw is not None
                or self.upper_bound_mw is not None
            ):
                raise PrasComparisonError(
                    "CV_KIND_INCONSISTENT",
                    "value_kind=point_se requires estimate+standard_error and NO bounds",
                )
            if isinstance(self.estimate, bool) or not np.isfinite(float(self.estimate)):
                raise PrasComparisonError("ESTIMATE_INVALID", "estimate must be finite")
            if (
                isinstance(self.standard_error, bool)
                or not np.isfinite(float(self.standard_error))
                or float(self.standard_error) < 0.0
            ):
                raise PrasComparisonError("SE_INVALID", "standard_error must be finite and non-negative")
        elif self.value_kind == "interval":
            if (
                self.estimate is not None
                or self.standard_error is not None
                or self.lower_bound_mw is None
                or self.upper_bound_mw is None
            ):
                raise PrasComparisonError(
                    "CV_KIND_INCONSISTENT",
                    "value_kind=interval requires lower/upper bounds and NO point estimate",
                )
            lo = float(self.lower_bound_mw)
            up = float(self.upper_bound_mw)
            if not (np.isfinite(lo) and np.isfinite(up)) or lo > up:
                raise PrasComparisonError(
                    "CV_BOUNDS_INVALID",
                    f"interval bounds must be finite with lower <= upper, got [{lo}, {up}]",
                )
        elif self.value_kind == "null":
            if (
                self.estimate is not None
                or self.standard_error is not None
                or self.lower_bound_mw is not None
                or self.upper_bound_mw is not None
            ):
                raise PrasComparisonError(
                    "CV_KIND_INCONSISTENT",
                    "value_kind=null requires every numeric field to be None",
                )
        else:
            raise PrasComparisonError(
                "CV_KIND_UNKNOWN",
                f"value_kind must be point_se|interval|null, got {self.value_kind!r}",
            )


@dataclass(frozen=True)
class PrasEvidenceBundle:
    """Evidence bundle COMPOSING the single PrasVersionEvidence fact
    source. There is NO duplicate case_id here."""

    version_evidence: PrasVersionEvidence


def evidence_fingerprint(evidence: PrasEvidenceBundle) -> str:
    """Shared pure-function evidence identity/fingerprint over the full
    canonical evidence payload (stable_dump + SHA-256)."""
    ve = evidence.version_evidence
    payload = {
        "case_id": ve.case_id,
        "julia_version": ve.julia_version,
        "pras_version": ve.pras_version,
        "pras_commit": ve.pras_commit,
        "python_model_version": ve.python_model_version,
        "python_git_head": ve.python_git_head,
        "system_input_hash": ve.system_input_hash,
        "mapping_audit_hash": ve.mapping_audit_hash,
        "run_script_hash": ve.run_script_hash,
        "random_seed": ve.random_seed,
        "sample_count": ve.sample_count,
        "raw_output_ref": ve.raw_output_ref,
        "raw_output_hash": ve.raw_output_hash,
        "version_status": ve.version_status,
        "mapping_status": ve.mapping_status,
        "execution_status": ve.execution_status,
    }
    return hashlib.sha256(stable_dump(payload)).hexdigest()


@dataclass(frozen=True)
class PrasComparisonResult:
    status: PrasComparisonStatus
    risk_comparison: dict[str, Any] | None
    capacity_value_comparison: dict[str, Any] | None
    difference: float | None
    tolerance: float
    reason: str
    unsupported_fields: tuple[str, ...]
    structural_differences: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    case_id: str | None = None
    input_hash: str | None = None
    evidence_fingerprint: str | None = None
    approval: ApprovalRecord | None = None


def _validate_estimate(estimate: float, se: float, label: str) -> None:
    if isinstance(estimate, bool) or not np.isfinite(float(estimate)):
        raise PrasComparisonError("ESTIMATE_INVALID", f"{label} estimate must be finite")
    if isinstance(se, bool) or not np.isfinite(float(se)) or float(se) < 0.0:
        raise PrasComparisonError("SE_INVALID", f"{label} standard_error must be finite and non-negative")


def _validate_tolerance(value: Any, label: str) -> float:
    if isinstance(value, bool) or not np.isfinite(float(value)) or float(value) < 0.0:
        raise PrasComparisonError("TOLERANCE_INVALID", f"{label} must be finite and non-negative")
    return float(value)


def _require_hex64(value: str | None, label: str) -> None:
    if value is None:
        return
    if not _HEX64.match(value):
        raise PrasComparisonError(
            "EVIDENCE_HASH_INVALID",
            f"{label} must be a 64-char lowercase sha256 hex, got {value!r}",
        )


def _require_non_blank(value: str | None, label: str) -> None:
    if value is None:
        return
    if not str(value).strip():
        raise PrasComparisonError("EVIDENCE_BLANK", f"{label} must not be blank")


def _evidence_blocks_matched(case: PrasCaseDefinition, evidence: PrasEvidenceBundle) -> str | None:
    """Shared pre-matched identity check (case/evidence statuses must
    agree item-by-item; audit hash freshly recomputed)."""
    ve = evidence.version_evidence
    if ve.case_id != case.case_id:
        return f"case identity mismatch: evidence case_id={ve.case_id!r} != case {case.case_id!r}"
    if ve.version_status != case.pras_version_status:
        return f"version status mismatch: evidence {ve.version_status!r} != case {case.pras_version_status!r}"
    if ve.mapping_status != case.input_mapping_status:
        return f"mapping status mismatch: evidence {ve.mapping_status!r} != case {case.input_mapping_status!r}"
    if ve.execution_status != case.external_execution_status:
        return f"execution status mismatch: evidence {ve.execution_status!r} != case {case.external_execution_status!r}"
    if case.pras_version_status != "locked":
        return "PRAS version is not locked"
    if case.input_mapping_status != "audited_confirmed":
        return "input mapping is not audited_confirmed"
    if case.external_execution_status != "completed":
        return "external execution is not completed"
    if ve.pras_version is None or ve.pras_commit is None or ve.julia_version is None:
        return "PRAS/Julia version or commit evidence missing"
    if ve.python_model_version != case.python_model_version:
        return f"python_model_version mismatch: {ve.python_model_version!r} != {case.python_model_version!r}"
    if ve.python_git_head != case.python_git_head:
        return f"python_git_head mismatch: {ve.python_git_head!r} != {case.python_git_head!r}"
    if ve.system_input_hash != case_input_hash(case):
        return "input identity mismatch: evidence input hash != current case input hash"
    if ve.random_seed != case.random_seed:
        return f"random_seed mismatch: {ve.random_seed!r} != {case.random_seed!r}"
    if ve.sample_count != case.sample_count:
        return f"sample_count mismatch: {ve.sample_count!r} != {case.sample_count!r}"
    fresh_audit = audit_case_mapping(case).audit_hash
    if ve.mapping_audit_hash != fresh_audit:
        return "mapping audit hash mismatch (must equal the freshly recomputed audit)"
    for label, value in (
        ("julia_version", ve.julia_version),
        ("pras_version", ve.pras_version),
        ("pras_commit", ve.pras_commit),
        ("run_script_hash", ve.run_script_hash),
        ("raw_output_ref", ve.raw_output_ref),
    ):
        _require_non_blank(value, label)
        if value is None:
            return f"{label} evidence missing"
    for label, value in (
        ("run_script_hash", ve.run_script_hash),
        ("raw_output_hash", ve.raw_output_hash),
        ("mapping_audit_hash", ve.mapping_audit_hash),
    ):
        _require_hex64(value, label)
        if value is None:
            return f"{label} evidence missing"
    return None


def mapping_acceptance_blocks(case: PrasCaseDefinition) -> str | None:
    """The SINGLE mapping-acceptance gate: (a) every required model /
    comparison field is covered by the audit (completeness), (b) no
    unsupported, (c) no lossy, (d) no provisional/mismatch entries.
    Exactly ONE audit is generated per call and shared by every check."""
    audit = audit_case_mapping(case)
    completeness_block = mapping_completeness_blocks(case, audit=audit)
    if completeness_block is not None:
        return completeness_block
    unsupported = audit.unsupported_fields()
    if unsupported:
        return "mapping audit has unsupported fields: " + ", ".join(unsupported)
    lossy = audit.lossy_fields()
    if lossy:
        return "mapping audit has lossy fields: " + ", ".join(lossy)
    open_entries = [
        e.python_field
        for e in audit.entries
        if e.evidence_status not in ("confirmed",)
    ]
    if open_entries:
        return "mapping audit has non-confirmed entries: " + ", ".join(open_entries)
    return None


def _structural_differences_from_audit(case: PrasCaseDefinition) -> tuple[str, ...]:
    """Derive structural differences automatically from the audit (lossy
    / provisional / unsupported entries) - never from caller strings."""
    audit = audit_case_mapping(case)
    fields = audit.lossy_fields() + audit.unsupported_fields()
    fields += [e.python_field for e in audit.entries if e.evidence_status not in ("confirmed",)]
    return tuple(sorted(set(fields)))


def _not_comparable_gate(
    case: PrasCaseDefinition,
    evidence: PrasEvidenceBundle,
    unsupported_fields: tuple[str, ...],
    approval: ApprovalRecord | None,
) -> str | None:
    """THE shared not_comparable gate (risk AND capacity): returns a
    blocking reason or None when every condition is met."""
    identity_block = _evidence_blocks_matched(case, evidence)
    if identity_block is not None:
        return "identity incomplete: " + identity_block
    audit = audit_case_mapping(case)
    audit_unsupported = set(audit.unsupported_fields())
    if not audit_unsupported:
        return "mapping audit has no unsupported fields to declare"
    if set(unsupported_fields) != audit_unsupported:
        return "declared unsupported fields do not match the mapping audit"
    if approval is None:
        return "missing dual-role approval record"
    # ApprovalRecord construction guarantees >=1 strict non-empty
    # evidence reference and two distinct non-blank approvers
    return None


def _case_dispatch_protocol_id(case: PrasCaseDefinition) -> str | None:
    return (case.comparison_protocol or {}).get("dispatch_protocol_id")


def _result(
    status: PrasComparisonStatus,
    *,
    difference: float | None,
    tolerance: float,
    reason: str,
    unsupported_fields: tuple[str, ...] = (),
    structural_differences: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    risk_comparison: dict[str, Any] | None = None,
    capacity_value_comparison: dict[str, Any] | None = None,
    case: PrasCaseDefinition | None = None,
    evidence: PrasEvidenceBundle | None = None,
    inherit: PrasComparisonResult | None = None,
    approval: ApprovalRecord | None = None,
) -> PrasComparisonResult:
    if status is PrasComparisonStatus.NOT_COMPARABLE and not unsupported_fields:
        raise PrasComparisonError(
            "NOT_COMPARABLE_WITHOUT_FIELDS",
            "not_comparable must carry the unsupported fields",
        )
    if status is PrasComparisonStatus.EXPLAINED_DIFFERENCE and (
        not structural_differences or not evidence_refs
    ):
        raise PrasComparisonError(
            "EXPLAINED_WITHOUT_EVIDENCE",
            "explained_difference requires structural differences and evidence references",
        )
    return PrasComparisonResult(
        status=status,
        risk_comparison=risk_comparison,
        capacity_value_comparison=capacity_value_comparison,
        difference=difference,
        tolerance=tolerance,
        reason=reason,
        unsupported_fields=unsupported_fields,
        structural_differences=structural_differences,
        evidence_refs=evidence_refs,
        case_id=(
            inherit.case_id
            if inherit is not None
            else (case.case_id if case is not None else None)
        ),
        input_hash=(
            inherit.input_hash
            if inherit is not None
            else (case_input_hash(case) if case is not None else None)
        ),
        evidence_fingerprint=(
            inherit.evidence_fingerprint
            if inherit is not None
            else (evidence_fingerprint(evidence) if evidence is not None else None)
        ),
        approval=approval if approval is not None else (inherit.approval if inherit is not None else None),
    )


def _risk_payload(python_risk: RiskEstimate, pras_risk: RiskEstimate) -> dict[str, Any]:
    return {
        "metric": python_risk.metric,
        "python_estimate": python_risk.estimate,
        "python_standard_error": python_risk.standard_error,
        "pras_estimate": pras_risk.estimate,
        "pras_standard_error": pras_risk.standard_error,
        "z_quantile": "z_0.975",
    }


def compare_risk_estimates(
    python_risk: RiskEstimate,
    pras_risk: RiskEstimate,
    *,
    case: PrasCaseDefinition,
    evidence: PrasEvidenceBundle,
    unsupported_fields: tuple[str, ...] = (),
    unsupported_approval: ApprovalRecord | None = None,
    structural_differences: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
) -> PrasComparisonResult:
    """Risk comparison: joint interval + full identity + fully accepted
    mapping audit are ALL required for matched. Structural differences
    are derived from the audit; caller-supplied ones additionally block
    matched. not_comparable goes through the shared gate only."""
    if python_risk is pras_risk:
        raise PrasComparisonError(
            "OBJECTS_NOT_INDEPENDENT",
            "python and PRAS estimates must be independent objects",
        )
    if python_risk.metric != pras_risk.metric:
        raise PrasComparisonError("METRIC_MISMATCH", "risk metrics differ between the two sides")
    if python_risk.metric not in (case.comparison_metrics or ()):
        raise PrasComparisonError(
            "METRIC_NOT_IN_CASE",
            f"risk metric {python_risk.metric!r} not declared in case.comparison_metrics",
        )
    _validate_estimate(python_risk.estimate, python_risk.standard_error, "python")
    _validate_estimate(pras_risk.estimate, pras_risk.standard_error, "pras")
    tolerance = _validate_tolerance(case.numeric_tolerance, "numeric_tolerance")
    if unsupported_fields:
        gate_block = _not_comparable_gate(case, evidence, unsupported_fields, unsupported_approval)
        if gate_block is not None:
            return _result(
                PrasComparisonStatus.UNRESOLVED,
                difference=None,
                tolerance=tolerance,
                reason="cannot classify unsupported fields: " + gate_block,
                unsupported_fields=tuple(sorted(unsupported_fields)),
                risk_comparison=_risk_payload(python_risk, pras_risk),
                case=case,
                evidence=evidence,
            )
        assert unsupported_approval is not None
        return _result(
            PrasComparisonStatus.NOT_COMPARABLE,
            difference=None,
            tolerance=tolerance,
            reason="PRAS cannot express fields (locked interface evidence): "
            + ", ".join(sorted(unsupported_fields)),
            unsupported_fields=tuple(sorted(unsupported_fields)),
            evidence_refs=tuple(sorted(unsupported_approval.evidence_refs)),
            risk_comparison=_risk_payload(python_risk, pras_risk),
            case=case,
            evidence=evidence,
            approval=unsupported_approval,
        )
    difference = abs(float(python_risk.estimate) - float(pras_risk.estimate))
    joint = (
        Z_975 * np.sqrt(python_risk.standard_error**2 + pras_risk.standard_error**2)
        + tolerance
    )
    evidence_block = _evidence_blocks_matched(case, evidence)
    if evidence_block is not None:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=difference,
            tolerance=float(joint),
            reason="evidence/identity incomplete: " + evidence_block,
            structural_differences=_structural_differences_from_audit(case),
            evidence_refs=tuple(sorted(evidence_refs)),
            risk_comparison=_risk_payload(python_risk, pras_risk),
            case=case,
            evidence=evidence,
        )
    acceptance_block = mapping_acceptance_blocks(case)
    if acceptance_block is not None:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=difference,
            tolerance=float(joint),
            reason="mapping acceptance incomplete: " + acceptance_block,
            structural_differences=_structural_differences_from_audit(case),
            evidence_refs=tuple(sorted(evidence_refs)),
            risk_comparison=_risk_payload(python_risk, pras_risk),
            case=case,
            evidence=evidence,
        )
    if structural_differences:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=difference,
            tolerance=float(joint),
            reason="open structural differences block matched: "
            + ", ".join(sorted(structural_differences)),
            structural_differences=tuple(sorted(structural_differences)),
            evidence_refs=tuple(sorted(evidence_refs)),
            risk_comparison=_risk_payload(python_risk, pras_risk),
            case=case,
            evidence=evidence,
        )
    if difference <= joint:
        return _result(
            PrasComparisonStatus.MATCHED,
            difference=difference,
            tolerance=float(joint),
            reason="within joint 95% interval with complete evidence identity and accepted audit",
            risk_comparison=_risk_payload(python_risk, pras_risk),
            case=case,
            evidence=evidence,
        )
    return _result(
        PrasComparisonStatus.UNRESOLVED,
        difference=difference,
        tolerance=float(joint),
        reason="outside joint 95% interval; structural differences recorded for approval path",
        structural_differences=_structural_differences_from_audit(case),
        evidence_refs=tuple(sorted(evidence_refs)),
        risk_comparison=_risk_payload(python_risk, pras_risk),
        case=case,
        evidence=evidence,
    )


def _cv_payload(
    python_cv: CapacityValueEstimate,
    pras_cv: CapacityValueEstimate,
    tolerance: float,
    search_tol_p: float,
    search_tol_j: float,
) -> dict[str, Any]:
    return {
        "metric": python_cv.metric,
        "python_value_kind": python_cv.value_kind,
        "python_estimate": python_cv.estimate,
        "python_standard_error": python_cv.standard_error,
        "python_lower_bound_mw": python_cv.lower_bound_mw,
        "python_upper_bound_mw": python_cv.upper_bound_mw,
        "pras_value_kind": pras_cv.value_kind,
        "pras_estimate": pras_cv.estimate,
        "pras_standard_error": pras_cv.standard_error,
        "pras_lower_bound_mw": pras_cv.lower_bound_mw,
        "pras_upper_bound_mw": pras_cv.upper_bound_mw,
        "search_capacity_tolerance_python": search_tol_p,
        "search_capacity_tolerance_pras": search_tol_j,
        "numeric_tolerance": tolerance,
        "definition_python": python_cv.definition,
        "definition_pras": pras_cv.definition,
        "dispatch_protocol_id_python": python_cv.dispatch_protocol_id,
        "dispatch_protocol_id_pras": pras_cv.dispatch_protocol_id,
    }


def _cv_difference_and_joint(
    python_cv: CapacityValueEstimate,
    pras_cv: CapacityValueEstimate,
    tolerance: float,
    search_tol_p: float,
    search_tol_j: float,
) -> tuple[float, float]:
    """FROZEN interval-overlap / search-tolerance comparison rule for
    capacity values:

    - point_se vs point_se: |p - j| vs z*sqrt(se_p^2+se_j^2) + tols;
    - point_se vs interval: |p - center_j| vs z*se_p + half_width_j +
      tols (the PRAS-native interval carries its own uncertainty - the
      half-width is a bound, NOT a standard error);
    - interval vs interval: |center_p - center_j| vs half_width_p +
      half_width_j + tols.

    Returns (difference, joint)."""
    z = Z_975
    base_tols = tolerance + search_tol_p + search_tol_j
    if python_cv.value_kind == "interval" and pras_cv.value_kind == "interval":
        assert python_cv.lower_bound_mw is not None and python_cv.upper_bound_mw is not None
        assert pras_cv.lower_bound_mw is not None and pras_cv.upper_bound_mw is not None
        center_p = (float(python_cv.lower_bound_mw) + float(python_cv.upper_bound_mw)) / 2.0
        center_j = (float(pras_cv.lower_bound_mw) + float(pras_cv.upper_bound_mw)) / 2.0
        half_p = (float(python_cv.upper_bound_mw) - float(python_cv.lower_bound_mw)) / 2.0
        half_j = (float(pras_cv.upper_bound_mw) - float(pras_cv.lower_bound_mw)) / 2.0
        return abs(center_p - center_j), half_p + half_j + base_tols
    if python_cv.value_kind == "interval":
        assert python_cv.lower_bound_mw is not None and python_cv.upper_bound_mw is not None
        center_p = (float(python_cv.lower_bound_mw) + float(python_cv.upper_bound_mw)) / 2.0
        half_p = (float(python_cv.upper_bound_mw) - float(python_cv.lower_bound_mw)) / 2.0
        assert pras_cv.estimate is not None and pras_cv.standard_error is not None
        return (
            abs(center_p - float(pras_cv.estimate)),
            half_p + z * float(pras_cv.standard_error) + base_tols,
        )
    if pras_cv.value_kind == "interval":
        assert pras_cv.lower_bound_mw is not None and pras_cv.upper_bound_mw is not None
        center_j = (float(pras_cv.lower_bound_mw) + float(pras_cv.upper_bound_mw)) / 2.0
        half_j = (float(pras_cv.upper_bound_mw) - float(pras_cv.lower_bound_mw)) / 2.0
        assert python_cv.estimate is not None and python_cv.standard_error is not None
        return (
            abs(float(python_cv.estimate) - center_j),
            z * float(python_cv.standard_error) + half_j + base_tols,
        )
    assert python_cv.estimate is not None and python_cv.standard_error is not None
    assert pras_cv.estimate is not None and pras_cv.standard_error is not None
    return (
        abs(float(python_cv.estimate) - float(pras_cv.estimate)),
        z * np.sqrt(python_cv.standard_error**2 + pras_cv.standard_error**2) + base_tols,
    )


def compare_capacity_values(
    python_cv: CapacityValueEstimate,
    pras_cv: CapacityValueEstimate,
    *,
    risk_result: PrasComparisonResult,
    case: PrasCaseDefinition,
    evidence: PrasEvidenceBundle,
    unsupported_fields: tuple[str, ...] = (),
    unsupported_approval: ApprovalRecord | None = None,
) -> PrasComparisonResult | None:
    """Capacity-value comparison gated by the ACTUAL risk result with the
    SAME evidence fingerprint, controlled definition matching the case
    mode, case-controlled dispatch protocol, metric declared in
    comparison_metrics, and the shared not_comparable gate."""
    if not isinstance(risk_result, PrasComparisonResult):
        raise PrasComparisonError(
            "RISK_GATE_TYPE",
            "capacity value requires the risk comparison result object, not a boolean",
        )
    if risk_result.status is not PrasComparisonStatus.MATCHED:
        return None
    if risk_result.risk_comparison is None:
        return None
    if risk_result.case_id != case.case_id or risk_result.input_hash != case_input_hash(case):
        raise PrasComparisonError(
            "RISK_GATE_IDENTITY_MISMATCH",
            "risk result identity does not match the capacity-value case",
        )
    current_fingerprint = evidence_fingerprint(evidence)
    if risk_result.evidence_fingerprint != current_fingerprint:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=None,
            tolerance=_validate_tolerance(case.numeric_tolerance, "numeric_tolerance"),
            reason="evidence fingerprint differs between the risk and capacity stages",
            case=case,
            evidence=evidence,
        )
    if python_cv is pras_cv:
        raise PrasComparisonError(
            "OBJECTS_NOT_INDEPENDENT",
            "python and PRAS capacity values must be independent objects",
        )
    if python_cv.metric != pras_cv.metric:
        raise PrasComparisonError("METRIC_MISMATCH", "capacity-value metrics differ")
    if python_cv.metric not in (case.comparison_metrics or ()):
        raise PrasComparisonError(
            "METRIC_NOT_IN_CASE",
            f"capacity metric {python_cv.metric!r} not declared in case.comparison_metrics",
        )
    # kind-aware: point_se / interval / null are machine-distinguishable
    # (validated by CapacityValueEstimate.__post_init__); null sides are
    # NEVER coerced into numbers.
    if python_cv.value_kind == "null" or pras_cv.value_kind == "null":
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=None,
            tolerance=_validate_tolerance(case.numeric_tolerance, "numeric_tolerance"),
            reason=(
                "capacity value is null/not converged on one side: "
                f"python={python_cv.value_kind}, pras={pras_cv.value_kind}"
            ),
            capacity_value_comparison=_cv_payload(
                python_cv,
                pras_cv,
                _validate_tolerance(case.numeric_tolerance, "numeric_tolerance"),
                _validate_tolerance(python_cv.search_capacity_tolerance, "python search tolerance"),
                _validate_tolerance(pras_cv.search_capacity_tolerance, "pras search tolerance"),
            ),
            case=case,
            evidence=evidence,
        )
    tolerance = _validate_tolerance(case.numeric_tolerance, "numeric_tolerance")
    search_tol_p = _validate_tolerance(python_cv.search_capacity_tolerance, "python search tolerance")
    search_tol_j = _validate_tolerance(pras_cv.search_capacity_tolerance, "pras search tolerance")
    if unsupported_fields:
        gate_block = _not_comparable_gate(case, evidence, unsupported_fields, unsupported_approval)
        if gate_block is not None:
            return _result(
                PrasComparisonStatus.UNRESOLVED,
                difference=None,
                tolerance=tolerance,
                reason="cannot classify unsupported fields: " + gate_block,
                unsupported_fields=tuple(sorted(unsupported_fields)),
                capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
                case=case,
                evidence=evidence,
            )
        assert unsupported_approval is not None
        return _result(
            PrasComparisonStatus.NOT_COMPARABLE,
            difference=None,
            tolerance=tolerance,
            reason="PRAS cannot express fields (locked interface evidence): "
            + ", ".join(sorted(unsupported_fields)),
            unsupported_fields=tuple(sorted(unsupported_fields)),
            evidence_refs=tuple(sorted(unsupported_approval.evidence_refs)),
            capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
            case=case,
            evidence=evidence,
            approval=unsupported_approval,
        )
    # definition mismatch is a comparison-configuration difference -
    # never disguised as "PRAS cannot express"
    if python_cv.definition != pras_cv.definition or python_cv.definition != case.capacity_value_mode:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=None,
            tolerance=tolerance,
            reason=(
                "capacity-value definition mismatch: "
                f"sides=({python_cv.definition}, {pras_cv.definition}), "
                f"case={case.capacity_value_mode}"
            ),
            structural_differences=("capacity_value_definition",),
            capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
            case=case,
            evidence=evidence,
        )
    case_dispatch = _case_dispatch_protocol_id(case)
    if case_dispatch is None:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=None,
            tolerance=tolerance,
            reason="case comparison_protocol lacks a controlled dispatch_protocol_id",
            capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
            case=case,
            evidence=evidence,
        )
    if python_cv.dispatch_protocol_id != case_dispatch or pras_cv.dispatch_protocol_id != case_dispatch:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=None,
            tolerance=tolerance,
            reason=(
                "dispatch protocol mismatch with case: "
                f"python={python_cv.dispatch_protocol_id!r}, "
                f"pras={pras_cv.dispatch_protocol_id!r}, case={case_dispatch!r}"
            ),
            structural_differences=("dispatch_protocol_id",),
            capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
            case=case,
            evidence=evidence,
        )
    evidence_block = _evidence_blocks_matched(case, evidence)
    difference, joint = _cv_difference_and_joint(
        python_cv, pras_cv, tolerance, search_tol_p, search_tol_j
    )
    if evidence_block is not None:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=difference,
            tolerance=float(joint),
            reason="evidence/identity incomplete: " + evidence_block,
            capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
            case=case,
            evidence=evidence,
        )
    acceptance_block = mapping_acceptance_blocks(case)
    if acceptance_block is not None:
        return _result(
            PrasComparisonStatus.UNRESOLVED,
            difference=difference,
            tolerance=float(joint),
            reason="mapping acceptance incomplete: " + acceptance_block,
            structural_differences=_structural_differences_from_audit(case),
            capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
            case=case,
            evidence=evidence,
        )
    if difference <= joint:
        return _result(
            PrasComparisonStatus.MATCHED,
            difference=difference,
            tolerance=float(joint),
            reason="within joint 95% interval incl. BOTH search capacity tolerances",
            capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
            case=case,
            evidence=evidence,
        )
    return _result(
        PrasComparisonStatus.UNRESOLVED,
        difference=difference,
        tolerance=float(joint),
        reason="outside joint 95% interval",
        capacity_value_comparison=_cv_payload(python_cv, pras_cv, tolerance, search_tol_p, search_tol_j),
        case=case,
        evidence=evidence,
    )


def resolve_unresolved(
    result: PrasComparisonResult,
    *,
    approval: ApprovalRecord,
    previous_status: PrasComparisonStatus,
    new_status: PrasComparisonStatus,
    numeric_tolerance_override: float | None = None,
) -> PrasComparisonResult:
    """Dual-role approval resolution: ONLY unresolved -> explained_
    difference; matched can never be produced here. The original
    case_id / input_hash / evidence fingerprint are inherited."""
    if numeric_tolerance_override is not None:
        raise PrasComparisonError(
            "TOLERANCE_OVERRIDE_FORBIDDEN",
            "enlarging the numeric tolerance is not an unresolved-resolution path",
        )
    if result.status is not PrasComparisonStatus.UNRESOLVED:
        raise PrasComparisonError("RESOLUTION_NOT_UNRESOLVED", "only unresolved can be resolved")
    if previous_status is not PrasComparisonStatus.UNRESOLVED:
        raise PrasComparisonError("RESOLUTION_PREVIOUS_STATUS", "previous_status must be unresolved")
    if new_status is not PrasComparisonStatus.EXPLAINED_DIFFERENCE:
        raise PrasComparisonError(
            "RESOLUTION_MATCHED_FORBIDDEN",
            "resolve_unresolved can only produce explained_difference; "
            "matched requires a re-run of the comparison that passes",
        )
    if not result.structural_differences:
        raise PrasComparisonError(
            "RESOLUTION_NO_STRUCTURAL_DIFFERENCES",
            "explained_difference requires structural differences on the result",
        )
    if not result.evidence_refs:
        raise PrasComparisonError(
            "RESOLUTION_NO_EVIDENCE_REFS",
            "explained_difference requires evidence references on the result",
        )
    if approval is None:
        raise PrasComparisonError(
            "RESOLUTION_MISSING_APPROVAL",
            "an ApprovalRecord is required to resolve unresolved",
        )
    if approval.attribution_report_ref is None:
        raise PrasComparisonError(
            "RESOLUTION_MISSING_ATTRIBUTION",
            "attribution report reference required",
        )
    return _result(
        PrasComparisonStatus.EXPLAINED_DIFFERENCE,
        difference=result.difference,
        tolerance=result.tolerance,
        reason=(
            "resolved from " + previous_status.value + " to explained_difference with "
            "attribution report " + str(approval.attribution_report_ref) +
            "; method review owner: " + str(approval.method_reviewer_approval) +
            "; release owner: " + str(approval.release_reviewer_approval)
        ),
        structural_differences=result.structural_differences,
        evidence_refs=tuple(sorted(set(result.evidence_refs) | set(approval.evidence_refs)
                                   | {str(approval.attribution_report_ref)})),
        risk_comparison=result.risk_comparison,
        capacity_value_comparison=result.capacity_value_comparison,
        inherit=result,
        approval=approval,
    )



def parse_pras_result_text(text: str) -> dict[str, Any]:
    """Parse the PRAS runner result.txt (key=value lines) into a dict.
    Machine values stay numeric; every key=value pair is kept."""
    payload: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PrasComparisonError(
                "PRAS_RESULT_LINE_INVALID",
                "result.txt line is not key=value: " + repr(line),
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        try:
            payload[key] = int(value)
        except ValueError:
            try:
                payload[key] = float(value)
            except ValueError:
                payload[key] = value
    return payload


def risk_estimate_from_payload(
    payload: dict[str, Any], metric: str, label: str
) -> RiskEstimate:
    """THE single python/julia -> RiskEstimate assembly: finds
    payload[label] + payload[label + "_se"]; missing values are a
    stable error - never a fabricated estimate."""
    if label not in payload or label + "_se" not in payload:
        raise PrasComparisonError(
            "PRAS_RESULT_METRIC_MISSING",
            "no estimate/SE pair found for " + label,
        )
    return RiskEstimate(
        metric=metric,
        estimate=float(payload[label]),
        standard_error=float(payload[label + "_se"]),
    )


def capacity_value_estimate_from_payload(
    payload: dict[str, Any],
    *,
    definition: str = "efc_perfect_capacity",
    dispatch_protocol_id: str = "no_dispatch_components",
    search_tolerance: float = 1.0,
    interval: bool = False,
) -> CapacityValueEstimate:
    """THE single python/julia -> CapacityValueEstimate assembly.
    - python payload: estimate_mw + standard error (point_se);
    - PRAS payload (interval=True): lower/upper bound keys with
      value_kind=interval - the midpoint is NEVER a point estimate
      and the half-width is NEVER a standard error."""
    if interval:
        lower = payload.get("capacity_value_lower_bound_mw")
        upper = payload.get("capacity_value_upper_bound_mw")
        if lower is None or upper is None:
            raise PrasComparisonError(
                "PRAS_RESULT_CV_BOUNDS_MISSING",
                "interval capacity value requires lower/upper bound keys",
            )
        return CapacityValueEstimate(
            metric="efc_mw",
            value_kind="interval",
            lower_bound_mw=float(lower),
            upper_bound_mw=float(upper),
            search_capacity_tolerance=search_tolerance,
            definition=definition,
            dispatch_protocol_id=dispatch_protocol_id,
        )
    # python benchmark EFC payload: the REAL equal-risk search bracket is
    # the interval - empirical_risk_resolution (EUE, MWh/yr) is a RISK
    # resolution and must NEVER become an MW standard error. When the
    # payload declares value_kind=interval (or carries bracket bounds),
    # the capacity value IS an interval; a point estimate is not used.
    if (
        payload.get("value_kind") == "interval"
        or payload.get("bracket_low_mw") is not None
        or payload.get("bracket_high_mw") is not None
    ):
        low = payload.get("bracket_low_mw")
        high = payload.get("bracket_high_mw")
        if low is None or high is None:
            raise PrasComparisonError(
                "PRAS_RESULT_CV_BRACKET_MISSING",
                "python interval capacity value requires bracket_low_mw and bracket_high_mw",
            )
        se_tol = payload.get("search_capacity_tolerance_mw")
        return CapacityValueEstimate(
            metric="efc_mw",
            value_kind="interval",
            lower_bound_mw=float(low),
            upper_bound_mw=float(high),
            search_capacity_tolerance=float(se_tol) if se_tol is not None else search_tolerance,
            definition=definition,
            dispatch_protocol_id=dispatch_protocol_id,
        )
    estimate = payload.get("capacity_value_estimate_mw")
    se = payload.get("capacity_value_standard_error_mw")
    if estimate is None or se is None:
        # python payload spelling (no prefix)
        estimate = payload.get("estimate_mw")
        se = payload.get("standard_error_mw")
    if estimate is None or se is None:
        raise PrasComparisonError(
            "PRAS_RESULT_CV_ESTIMATE_MISSING",
            "point capacity value requires estimate and standard error keys",
        )
    return CapacityValueEstimate(
        metric="efc_mw",
        estimate=float(estimate),
        standard_error=float(se),
        search_capacity_tolerance=search_tolerance,
        definition=definition,
        dispatch_protocol_id=dispatch_protocol_id,
    )

__all__ = [
    "Z_975",
    "ApprovalRecord",
    "CapacityValueEstimate",
    "PrasComparisonError",
    "PrasComparisonResult",
    "PrasComparisonStatus",
    "PrasEvidenceBundle",
    "RiskEstimate",
    "capacity_value_estimate_from_payload",
    "compare_capacity_values",
    "compare_risk_estimates",
    "evidence_fingerprint",
    "mapping_acceptance_blocks",
    "parse_pras_result_text",
    "resolve_unresolved",
    "risk_estimate_from_payload",
]
