"""P1 result service (T09, review round 2).

Builds the unified MethodResult 1.3 adequacy branch from a real
AdequacyResult (one or more samples), persists/reads method_result.json
and the formal ArtifactManifest 1.0 (from
``capacity_credit.contracts.artifacts`` - never a second definition),
and enforces the publication contract:

- immutable-or-identical writes (missing: atomic publish; identical
  bytes: idempotent success; different bytes: stable error, overwrite
  is forbidden);
- the artifact manifest is fully validated BEFORE any file is touched
  (formal model, run_id/result_id/artifact_manifest_id, exactly one
  json_result entry, safe relative path, method_result exists,
  SHA-256 match);
- ResultLink is a real Pydantic contract (extra=forbid, UUID ids,
  result_id and artifact_manifest_id null together or set together);
- publish ids are pre-allocated per (run, request) so a replayed
  request never regenerates result_id / artifact_manifest_id.

Zero-sample or None risk results are never converted into a
MethodResult.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from capacity_credit.application.atomic_io import (
    ObjectExistsError,
    atomic_write_bytes,
    atomic_write_object,
    safe_filename_component,
    stable_dump,
)
from capacity_credit.contracts.artifacts import (
    ArtifactEntry,
    ArtifactManifest,
    ArtifactManifest11,
    load_artifact_manifest,
)
from capacity_credit.contracts.elcc_status import (
    aggregate_elcc_product_status,
    validate_method_result_elcc_status,
)
from capacity_credit.contracts.enums import (
    AllocationType,
    ArtifactType,
    CapacityValueType,
    ChronologyMode,
    DataGrade,
    InterpolationMethod,
    Method,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    RmriStatus,
    SearchConvergence,
    Severity,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.results import (
    Diagnostic,
    MethodResult,
    RiskMetrics,
    RiskRole,
    ScenarioBirthCertificate,
)


class ResultServiceError(ValueError):
    """Stable result-service error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ResultLink(BaseModel):
    """Strict immutable result-link contract (extra fields forbidden)."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    request_id: str
    result_id: uuid.UUID | None
    artifact_manifest_id: uuid.UUID | None

    @model_validator(mode="after")
    def _validate_combo(self) -> ResultLink:
        has_result = self.result_id is not None
        has_manifest = self.artifact_manifest_id is not None
        if has_result != has_manifest:
            raise ValueError("result_id and artifact_manifest_id must be both set or both null")
        return self


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_samples(run: Any) -> None:
    """Zero-sample / None risk results can never become a MethodResult."""
    if run.sample_count == 0 or run.risk_metrics is None:
        raise ResultServiceError(
            "RESULT_NO_SAMPLES",
            "cannot build a MethodResult without at least one processed "
            "sample with complete risk metrics",
        )


def _write_immutable_or_identical(path: Path, payload: dict[str, Any], error_code: str) -> None:
    """Immutable-or-identical publication: create atomically if missing,
    reuse if the existing bytes are identical, block otherwise."""
    data = stable_dump(payload)
    if not path.exists():
        try:
            atomic_write_bytes(path, data, immutable=True)
            return
        except ObjectExistsError:
            pass
    existing = path.read_bytes()
    if existing != data:
        raise ResultServiceError(
            error_code,
            f"existing file differs at {path.name}; overwrite forbidden",
        )


def build_adequacy_method_result(
    *,
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    resource_or_portfolio_id: str,
    run: Any,
    scenario_bank_id: str,
    effective_data_grade: DataGrade,
    software_version: str,
    method_profile: str = "adequacy_sequential",
    chronology_mode: ChronologyMode = ChronologyMode.INDEPENDENT_WEATHER_YEARS,
    confidence_level: float | None = None,
    storage_soc_initialization_mode: StorageSocInitializationMode = (
        StorageSocInitializationMode.NOT_APPLICABLE
    ),
    soc_year_boundary_mode: SocYearBoundaryMode = SocYearBoundaryMode.NOT_APPLICABLE,
) -> MethodResult:
    """Build the adequacy MethodResult 1.3 from a real run output.

    V1-T3-C: the storage SOC / chronology modes default to the legacy
    not_applicable projection (plain-chain callers unchanged) and take
    the RUN CONFIG's real values for the resource chain - never a
    fabricated not_applicable for a storage run."""
    _require_samples(run)
    metrics = run.risk_metrics
    primary = RiskMetric(run.primary_risk_metric)
    diagnostics = [
        Diagnostic(
            code=d["code"],
            severity=Severity(d["severity"]),
            message=d["message"],
        )
        for d in run.diagnostics
        if d.get("code") != "E02_KERNEL_SLICE"
    ]
    return MethodResult(
        result_schema_version="1.3",
        result_id=result_id,
        run_id=run_id,
        method=Method.ADEQUACY,
        method_profile=method_profile,
        resource_or_portfolio_id=resource_or_portfolio_id,
        capacity_value_type=CapacityValueType.NOT_APPLICABLE,
        baseline_portfolio_version=None,
        allocation_type=AllocationType.NONE,
        allocation_rule_id=None,
        chronology_mode=chronology_mode,
        storage_soc_initialization_mode=storage_soc_initialization_mode,
        soc_year_boundary_mode=soc_year_boundary_mode,
        assessment_anchor=None,
        anchor_risk_metric=None,
        anchor_risk_value=None,
        anchor_calibration_method=None,
        anchor_standard_source_id=None,
        reference_capacity_mw=None,
        accredited_capacity_mw=None,
        capacity_credit_fraction=None,
        perturbation_dimension=None,
        perturbation_vector=None,
        normalization_basis_mwh_per_mw=None,
        rmri_status=RmriStatus.NOT_APPLICABLE,
        primary_risk_metric=primary,
        risk_metrics=RiskMetrics(
            base=RiskRole(
                lole_days_per_year=metrics["lole_days_per_year"],
                lolh_hours_per_year=metrics["lolh_hours_per_year"],
                eue_mwh_per_year=metrics["eue_mwh_per_year"],
                neue_ppm=metrics["neue_ppm"],
            )
        ),
        base_risk=metrics[primary.value],
        candidate_risk=None,
        matched_risk=None,
        risk_difference=None,
        search_bracket_low_mw=None,
        search_bracket_high_mw=None,
        search_bracket_low_risk=None,
        search_bracket_high_risk=None,
        interpolation_method=InterpolationMethod.NOT_APPLICABLE,
        empirical_risk_resolution=None,
        confidence_level=confidence_level,
        lower_ci=run.primary_interval["lower"] if run.primary_interval else None,
        upper_ci=run.primary_interval["upper"] if run.primary_interval else None,
        sample_count=run.sample_count,
        scenario_bank_id=scenario_bank_id,
        result_status=ResultStatus(run.result_status),
        risk_convergence=RiskConvergence(run.risk_convergence),
        search_convergence=SearchConvergence(run.search_convergence),
        effective_data_grade=effective_data_grade,
        diagnostics=diagnostics,
    )


def build_elcc_method_result(
    *,
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    resource_or_portfolio_id: str,
    elcc: Any,
    base_metrics: dict[str, float],
    candidate_metrics: dict[str, float] | None,
    matched_metrics: dict[str, float] | None,
    scenario_bank_id: str,
    effective_data_grade: DataGrade,
    software_version: str,
    method_profile: str,
    capacity_value_type: CapacityValueType,
    baseline_portfolio_version: str | None,
    allocation_type: AllocationType,
    allocation_rule_id: str | None,
    chronology_mode: ChronologyMode = ChronologyMode.INDEPENDENT_WEATHER_YEARS,
    primary_risk_metric: RiskMetric = RiskMetric.LOLE_DAYS_PER_YEAR,
    reference_capacity_mw: float | None = None,
    confidence_level: float | None = None,
    upstream_diagnostics: list[dict[str, Any]] | None = None,
    storage_soc_initialization_mode: StorageSocInitializationMode = (
        StorageSocInitializationMode.NOT_APPLICABLE
    ),
    soc_year_boundary_mode: SocYearBoundaryMode = SocYearBoundaryMode.NOT_APPLICABLE,
    scenario_birth_certificate: ScenarioBirthCertificate | None = None,
) -> MethodResult:
    """Build the ELCC MethodResult 1.3 branch from a real ELCC result.

    Compatible wrapper over the SHARED ELCC/EFC result-construction
    kernel (``_build_search_method_result``) - never a second copy of
    the result construction."""
    return _build_search_method_result(
        method=Method.ELCC,
        capacity_credit_fraction=float(elcc.capacity_credit_fraction)
        if elcc.capacity_credit_fraction is not None
        else None,
        search_result=elcc,
        run_id=run_id,
        result_id=result_id,
        resource_or_portfolio_id=resource_or_portfolio_id,
        base_metrics=base_metrics,
        candidate_metrics=candidate_metrics,
        matched_metrics=matched_metrics,
        scenario_bank_id=scenario_bank_id,
        effective_data_grade=effective_data_grade,
        software_version=software_version,
        method_profile=method_profile,
        capacity_value_type=capacity_value_type,
        baseline_portfolio_version=baseline_portfolio_version,
        allocation_type=allocation_type,
        allocation_rule_id=allocation_rule_id,
        chronology_mode=chronology_mode,
        primary_risk_metric=primary_risk_metric,
        reference_capacity_mw=reference_capacity_mw,
        confidence_level=confidence_level,
        upstream_diagnostics=upstream_diagnostics,
        storage_soc_initialization_mode=storage_soc_initialization_mode,
        soc_year_boundary_mode=soc_year_boundary_mode,
        scenario_birth_certificate=scenario_birth_certificate,
    )


def build_efc_method_result(
    *,
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    resource_or_portfolio_id: str,
    efc: Any,
    base_metrics: dict[str, float],
    candidate_metrics: dict[str, float] | None,
    matched_metrics: dict[str, float] | None,
    scenario_bank_id: str,
    effective_data_grade: DataGrade,
    software_version: str,
    method_profile: str,
    capacity_value_type: CapacityValueType,
    baseline_portfolio_version: str | None,
    allocation_type: AllocationType,
    allocation_rule_id: str | None,
    chronology_mode: ChronologyMode = ChronologyMode.INDEPENDENT_WEATHER_YEARS,
    primary_risk_metric: RiskMetric = RiskMetric.LOLE_DAYS_PER_YEAR,
    reference_capacity_mw: float | None = None,
    confidence_level: float | None = None,
    upstream_diagnostics: list[dict[str, Any]] | None = None,
    storage_soc_initialization_mode: StorageSocInitializationMode = (
        StorageSocInitializationMode.NOT_APPLICABLE
    ),
    soc_year_boundary_mode: SocYearBoundaryMode = SocYearBoundaryMode.NOT_APPLICABLE,
    scenario_birth_certificate: ScenarioBirthCertificate | None = None,
    de_rating_factor: float | None = None,
    delivery_year: int | None = None,
    auction_round: str | None = None,
    technology_class: str | None = None,
    criterion_mode: str | None = None,
) -> MethodResult:
    """Build the EFC MethodResult 1.3 branch from a real EFC result.

    Same shared kernel as ELCC (``_build_search_method_result``);
    candidate = R(S+X, L), matched = R(S+F*, L),
    accredited_capacity_mw = F*. ``de_rating_factor`` (MR-01.2) is the
    F*/nameplate ratio computed by the workflow from the FROZEN resolved
    capacity - None keeps the legacy null fraction (honest when no
    denominator was resolved)."""
    return _build_search_method_result(
        method=Method.EFC,
        capacity_credit_fraction=de_rating_factor,
        delivery_year=delivery_year,
        auction_round=auction_round,
        technology_class=technology_class,
        criterion_mode=criterion_mode,
        search_result=efc,
        run_id=run_id,
        result_id=result_id,
        resource_or_portfolio_id=resource_or_portfolio_id,
        base_metrics=base_metrics,
        candidate_metrics=candidate_metrics,
        matched_metrics=matched_metrics,
        scenario_bank_id=scenario_bank_id,
        effective_data_grade=effective_data_grade,
        software_version=software_version,
        method_profile=method_profile,
        capacity_value_type=capacity_value_type,
        baseline_portfolio_version=baseline_portfolio_version,
        allocation_type=allocation_type,
        allocation_rule_id=allocation_rule_id,
        chronology_mode=chronology_mode,
        primary_risk_metric=primary_risk_metric,
        reference_capacity_mw=reference_capacity_mw,
        confidence_level=confidence_level,
        upstream_diagnostics=upstream_diagnostics,
        storage_soc_initialization_mode=storage_soc_initialization_mode,
        soc_year_boundary_mode=soc_year_boundary_mode,
        scenario_birth_certificate=scenario_birth_certificate,
    )


def _build_search_method_result(
    *,
    method: Method,
    capacity_credit_fraction: float | None,
    search_result: Any,
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    resource_or_portfolio_id: str,
    base_metrics: dict[str, float],
    candidate_metrics: dict[str, float] | None,
    matched_metrics: dict[str, float] | None,
    scenario_bank_id: str,
    effective_data_grade: DataGrade,
    software_version: str,
    method_profile: str,
    capacity_value_type: CapacityValueType,
    baseline_portfolio_version: str | None,
    allocation_type: AllocationType,
    allocation_rule_id: str | None,
    chronology_mode: ChronologyMode = ChronologyMode.INDEPENDENT_WEATHER_YEARS,
    primary_risk_metric: RiskMetric = RiskMetric.LOLE_DAYS_PER_YEAR,
    reference_capacity_mw: float | None = None,
    confidence_level: float | None = None,
    upstream_diagnostics: list[dict[str, Any]] | None = None,
    storage_soc_initialization_mode: StorageSocInitializationMode = (
        StorageSocInitializationMode.NOT_APPLICABLE
    ),
    soc_year_boundary_mode: SocYearBoundaryMode = SocYearBoundaryMode.NOT_APPLICABLE,
    scenario_birth_certificate: ScenarioBirthCertificate | None = None,
    delivery_year: int | None = None,
    auction_round: str | None = None,
    technology_class: str | None = None,
    criterion_mode: str | None = None,
) -> MethodResult:
    """SHARED ELCC/EFC MethodResult 1.3 construction kernel.

    The search-result object (ELCC or EFC) exposes the same contract:
    diagnostics, risk_convergence, search_convergence, result_status,
    estimate_mw, candidate_risk, matched_risk, risk_difference,
    bracket_low/high_mw, low/high_endpoint_risk, interpolation_method,
    empirical_risk_resolution, sample_count. The ``method`` and the
    capacity-credit fraction are the ONLY ELCC/EFC differences.

    The ELCC/EFC solver/search diagnostics are converted through the
    SINGLE registry (contracts.elcc_diagnostics) and enter the formal
    result (ELCC_CI_NOT_AVAILABLE / EFC_CI_NOT_AVAILABLE exactly once,
    info). The product result_status is AGGREGATED from the search
    state + risk/search convergence + the mapped diagnostic
    severities/categories - never trusted from the caller: valid +
    warning becomes valid_with_warning (point estimate kept), valid +
    method/data error becomes invalid_method / invalid_data with the
    fields cleared. Unknown diagnostic codes raise ElccDiagnosticsError
    (ELCC_DIAGNOSTIC_UNKNOWN) - publication is refused, never silently
    dropped or defaulted.

    ``upstream_diagnostics`` carries the frozen scenario-bank
    diagnostics; upstream entries and solver codes are merged through
    ONE registry-backed aggregation (``aggregate_elcc_diagnostics``):
    registry-determined severity and category, unknown codes fail
    closed, caller severity reclassification is rejected, deduplicated
    by code, sorted by code. The search-trace artifact is NOT built
    here and keeps ONLY the search-layer codes.

    lower_ci/upper_ci stay None: the methodology has no capacity
    confidence-interval algorithm in T12.
    """
    from capacity_credit.contracts.elcc_diagnostics import (
        aggregate_elcc_diagnostics,
        diagnostic_category,
    )

    elcc = search_result

    diagnostic_models = aggregate_elcc_diagnostics(
        upstream=list(upstream_diagnostics or []),
        solver_codes=list(elcc.diagnostics),
    )
    has_warning = any(d.severity is Severity.WARNING for d in diagnostic_models)
    has_method_error = any(
        d.severity is Severity.ERROR and diagnostic_category(d.code) == "method_error"
        for d in diagnostic_models
    )
    has_data_error = any(
        d.severity is Severity.ERROR and diagnostic_category(d.code) == "data_error"
        for d in diagnostic_models
    )
    product_status = aggregate_elcc_product_status(
        risk_convergence=RiskConvergence(elcc.risk_convergence),
        search_convergence=SearchConvergence(elcc.search_convergence),
        search_result_status=ResultStatus(elcc.result_status),
        has_warning=has_warning,
        has_method_error=has_method_error,
        has_data_error=has_data_error,
    )
    validate_method_result_elcc_status(
        risk_convergence=RiskConvergence(elcc.risk_convergence),
        search_convergence=SearchConvergence(elcc.search_convergence),
        result_status=product_status,
        has_warning=has_warning,
        has_method_error=has_method_error,
        has_data_error=has_data_error,
    )
    valid_family = product_status in (
        ResultStatus.VALID,
        ResultStatus.VALID_WITH_WARNING,
    )
    invalid_data_state = product_status is ResultStatus.INVALID_DATA
    search_bracket_low_risk = elcc.low_endpoint_risk if valid_family else None
    search_bracket_high_risk = elcc.high_endpoint_risk if valid_family else None
    # Risk-role semantics (frozen, remediation 1):
    # - candidate role = R(S+X, L): real 0-MW candidate evaluation;
    # - matched role   = R(S+X, L+G(C*)): real matched-point evaluation,
    #   present only for the valid family;
    # - primary scalars must equal their role's primary metric.
    candidate_role = None
    matched_role = None
    if candidate_metrics is not None:
        candidate_role = RiskRole(
            lole_days_per_year=candidate_metrics["lole_days_per_year"],
            lolh_hours_per_year=candidate_metrics["lolh_hours_per_year"],
            eue_mwh_per_year=candidate_metrics["eue_mwh_per_year"],
            neue_ppm=candidate_metrics["neue_ppm"],
        )
    if valid_family and matched_metrics is not None:
        matched_role = RiskRole(
            lole_days_per_year=matched_metrics["lole_days_per_year"],
            lolh_hours_per_year=matched_metrics["lolh_hours_per_year"],
            eue_mwh_per_year=matched_metrics["eue_mwh_per_year"],
            neue_ppm=matched_metrics["neue_ppm"],
        )
    candidate_scalar = elcc.candidate_risk
    base_role = RiskRole(
        lole_days_per_year=base_metrics["lole_days_per_year"],
        lolh_hours_per_year=base_metrics["lolh_hours_per_year"],
        eue_mwh_per_year=base_metrics["eue_mwh_per_year"],
        neue_ppm=base_metrics["neue_ppm"],
    )
    # The primary scalars must match their roles exactly; the
    # MethodResult model validator enforces base/candidate/matched
    # scalar == role primary, so no scalar is ever invented here.
    return MethodResult(
        result_schema_version="1.3",
        result_id=result_id,
        run_id=run_id,
        method=method,
        method_profile=method_profile,
        resource_or_portfolio_id=resource_or_portfolio_id,
        capacity_value_type=capacity_value_type,
        baseline_portfolio_version=baseline_portfolio_version,
        allocation_type=allocation_type,
        allocation_rule_id=allocation_rule_id,
        chronology_mode=chronology_mode,
        scenario_birth_certificate=scenario_birth_certificate,
        delivery_year=delivery_year,
        auction_round=auction_round,
        technology_class=technology_class,
        criterion_mode=criterion_mode,
        # R03 (night-4): the SOC modes the chain ACTUALLY executed reach
        # the result - the caller derives them from the effective config
        # and the resource-run defaults; the legacy not_applicable
        # default keeps storage-free runs honest
        storage_soc_initialization_mode=storage_soc_initialization_mode,
        soc_year_boundary_mode=soc_year_boundary_mode,
        assessment_anchor=None,
        anchor_risk_metric=None,
        anchor_risk_value=None,
        anchor_calibration_method=None,
        anchor_standard_source_id=None,
        reference_capacity_mw=reference_capacity_mw,
        accredited_capacity_mw=elcc.estimate_mw if valid_family else None,
        capacity_credit_fraction=capacity_credit_fraction if valid_family else None,
        perturbation_dimension=None,
        perturbation_vector=None,
        normalization_basis_mwh_per_mw=None,
        rmri_status=RmriStatus.NOT_APPLICABLE,
        primary_risk_metric=primary_risk_metric,
        risk_metrics=RiskMetrics(
            base=base_role,
            candidate=candidate_role,
            matched=matched_role,
        ),
        base_risk=elcc.base_risk,
        candidate_risk=candidate_scalar,
        matched_risk=elcc.matched_risk if valid_family else None,
        risk_difference=elcc.risk_difference if valid_family else None,
        search_bracket_low_mw=elcc.bracket_low_mw if not invalid_data_state else None,
        search_bracket_high_mw=elcc.bracket_high_mw if not invalid_data_state else None,
        search_bracket_low_risk=search_bracket_low_risk,
        search_bracket_high_risk=search_bracket_high_risk,
        interpolation_method=(
            elcc.interpolation_method if valid_family else InterpolationMethod.NOT_APPLICABLE
        ),
        empirical_risk_resolution=(
            elcc.empirical_risk_resolution if not invalid_data_state else None
        ),
        confidence_level=confidence_level,
        lower_ci=None,
        upper_ci=None,
        sample_count=elcc.sample_count,
        scenario_bank_id=scenario_bank_id,
        result_status=product_status,
        risk_convergence=RiskConvergence(elcc.risk_convergence),
        search_convergence=SearchConvergence(elcc.search_convergence),
        effective_data_grade=effective_data_grade,
        diagnostics=diagnostic_models,
    )


def write_method_result(result: BaseModel, path: Path | str) -> None:
    """Publish method_result.json (1.3 or 2.0) with immutable-or-identical
    semantics, then verify the round trip through the version reader."""
    path_obj = Path(path)
    _write_immutable_or_identical(
        path_obj, result.model_dump(mode="json"), "RESULT_EXISTS_DIFFERENT"
    )
    read_method_result_any(path_obj)


GAP_EVIDENCE_MAX_EVENTS = 20000


def write_gap_evidence(
    run_dir: Path | str,
    events: list[dict[str, Any]],
    *,
    source: str,
    sample_count: int | None = None,
    max_events: int = GAP_EVIDENCE_MAX_EVENTS,
) -> dict[str, Any]:
    """R3-B1: persist ONE run's deficit-event evidence (方法说明第三章
    (六) 第一步交付物之三 缺口画像) as TWO run-dir artifacts following
    the convergence_evidence.json pattern - unregistered evidence files,
    written BEFORE the result link publishes:

    - ``deficit_events.json``: raw engine event dicts, capped at
      ``max_events`` with an honest ``truncated`` flag (a long formal
      run can produce very many events; the cap is recorded, never
      silent);
    - ``gap_profile.json``: the month×hour aggregation built by the
      single reporting helper (no second aggregation implementation).

    An empty event list still writes both files - a zero-loss system
    gets an honest all-zero profile, never a missing artifact."""
    from capacity_credit.reporting.machine import build_gap_profile

    dir_obj = Path(run_dir)
    capped = bool(len(events) > max_events)
    kept = list(events[:max_events])
    events_payload = {
        "gap_evidence_schema_version": "1.0",
        "source": str(source),
        "sample_count": sample_count,
        "event_count": len(kept),
        "total_event_count": len(events),
        "truncated": capped,
        "max_events": max_events,
        "events": kept,
    }
    profile = build_gap_profile(
        kept,
        source=str(source),
        sample_count=sample_count,
        truncated=capped,
    )
    atomic_write_object(
        dir_obj / "deficit_events.json", events_payload, immutable=False
    )
    atomic_write_object(
        dir_obj / "gap_profile.json", profile, immutable=False
    )
    return {"event_count": len(kept), "truncated": capped}


def write_assumption_register(
    run_dir: Path | str,
    *,
    run_config_payload: dict[str, Any],
    result: BaseModel | None = None,
    extra_limitations: list[str] | None = None,
    emergency_programs: list[dict[str, Any]] | None = None,
) -> None:
    """R3-B2: persist ONE run's assumption register (方法说明第八章
    风险3) as the run-dir artifact ``assumptions.json`` - the frozen
    run-config document verbatim, the result's own structured
    diagnostics (the machine fact source for assumption-class claims,
    e.g. STORAGE_* ASSUMED codes), and the version-controlled declared
    method-boundary list. Same unregistered-evidence pattern as
    ``write_gap_evidence``."""
    from capacity_credit.reporting.machine import build_assumption_register

    diagnostics = [
        {
            "code": str(diag.code),
            "severity": getattr(
                getattr(diag, "severity", None), "value", str(getattr(diag, "severity", ""))
            ),
            "message": str(getattr(diag, "message", "")),
        }
        for diag in (getattr(result, "diagnostics", None) or [])
    ]
    register = build_assumption_register(
        run_config_payload=run_config_payload,
        diagnostics=diagnostics,
        extra_limitations=extra_limitations,
    )
    if emergency_programs is not None:
        register["emergency_programs"] = [dict(p) for p in emergency_programs]
        register["emergency_layer"] = (
            "enabled" if emergency_programs else "not_configured"
        )
    atomic_write_object(
        Path(run_dir) / "assumptions.json", register, immutable=False
    )


def write_seed_replication_summary(run_dir: Path | str, summary: dict[str, Any]) -> None:
    """R6/MR-03: persist the replication gate summary NEXT TO the formal
    result it bounds (run-dir unregistered-evidence pattern). The formal
    MethodResult JSON is never rewritten (immutable publication) - the
    report layer consumes this artifact for the point ± CI presentation,
    and the publish gate reads its verdict."""
    atomic_write_object(
        Path(run_dir) / "seed_replication_summary.json",
        summary,
        immutable=False,
    )


def read_seed_replication_summary(
    run_dir: Path | str,
) -> dict[str, Any] | None:
    """The honest tri-state reader: a present summary parses to a dict,
    a missing or corrupt one is None (never a fabricated pass)."""
    path = Path(run_dir) / "seed_replication_summary.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and "gate" in payload else None


def read_method_result(path: Path | str) -> MethodResult:
    """Read and strictly validate a MethodResult 1.3."""
    path_obj = Path(path)
    try:
        payload = json.loads(path_obj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResultServiceError("RESULT_INVALID_FILE", f"method_result unreadable: {exc}") from exc
    try:
        return MethodResult.model_validate(payload)
    except Exception as exc:
        raise ResultServiceError(
            "RESULT_INVALID", f"method_result failed validation: {exc}"
        ) from exc


def read_method_result_any(path: Path | str) -> BaseModel:
    """Read and strictly validate a MethodResult (1.3 or 2.0)."""
    from capacity_credit.contracts.mri_contracts import MriResult2

    path_obj = Path(path)
    try:
        payload = json.loads(path_obj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResultServiceError("RESULT_INVALID_FILE", f"method_result unreadable: {exc}") from exc
    version = payload.get("result_schema_version")
    try:
        if version == "2.0":
            return MriResult2.model_validate(payload)
        return MethodResult.model_validate(payload)
    except Exception as exc:
        raise ResultServiceError(
            "RESULT_INVALID", f"method_result failed validation: {exc}"
        ) from exc


def _find_result_entry(manifest: ArtifactManifest | ArtifactManifest11) -> ArtifactEntry | None:
    """The single formal MethodResult entry (json_result / schema 1.3 or 2.0)."""
    results = [
        entry
        for entry in manifest.artifacts
        if entry.artifact_type is ArtifactType.JSON_RESULT
        and entry.schema_or_template_version in ("1.3", "2.0")
    ]
    if len(results) == 1:
        return results[0]
    return None


def _validate_optional_entries(
    manifest: ArtifactManifest | ArtifactManifest11,
    run_dir: Path,
) -> None:
    """Validate every non-result artifact entry: it must be a safe-relative
    existing file with a matching SHA-256. artifact-manifest 1.0 keeps its
    historical type set (search_trace only for additional entries) and
    REJECTS analysis_summary; artifact-manifest 1.1 accepts search_trace and
    analysis_summary."""
    is_11 = manifest.artifact_schema_version == "1.1"
    for entry in manifest.artifacts:
        if entry.artifact_type is ArtifactType.JSON_RESULT and entry.schema_or_template_version in (
            "1.3",
            "2.0",
        ):
            continue
        if entry.artifact_type is ArtifactType.ANALYSIS_SUMMARY:
            if not is_11:
                raise ResultServiceError(
                    "ARTIFACT_TYPE_INVALID",
                    "artifact-manifest 1.0 must not carry analysis_summary artifacts",
                )
            if entry.schema_or_template_version != "1.0":
                raise ResultServiceError(
                    "ARTIFACT_SCHEMA_MISMATCH",
                    "analysis_summary artifact schema_or_template_version must be 1.0",
                )
        elif entry.artifact_type is not ArtifactType.SEARCH_TRACE:
            raise ResultServiceError(
                "ARTIFACT_TYPE_INVALID",
                "additional artifact entries must be search_trace or analysis_summary",
            )
        elif entry.schema_or_template_version != "1.0":
            raise ResultServiceError(
                "ARTIFACT_SCHEMA_MISMATCH",
                "search_trace artifact schema_or_template_version must be 1.0",
            )
        try:
            safe = safe_filename_component(entry.relative_path)
        except ValueError as exc:
            raise ResultServiceError(
                "ARTIFACT_PATH_UNSAFE",
                f"artifact relative path unsafe: {exc}",
            ) from exc
        artifact_path = run_dir / safe
        if not artifact_path.exists():
            raise ResultServiceError(
                "ARTIFACT_RESULT_MISSING",
                f"artifact file missing: {entry.relative_path}",
            )
        if _file_sha256(artifact_path) != entry.sha256:
            raise ResultServiceError(
                "ARTIFACT_HASH_MISMATCH",
                f"artifact sha256 mismatch: {entry.relative_path}",
            )


def _verify_expected_ids(
    manifest: ArtifactManifest | ArtifactManifest11,
    *,
    expected_run_id: str | None,
    expected_result_id: str | None,
    expected_manifest_id: str | None,
) -> None:
    """Expected-id cross checks (run / result / manifest ids)."""
    if expected_run_id is not None and str(manifest.run_id) != expected_run_id:
        raise ResultServiceError("ARTIFACT_RUN_MISMATCH", "manifest run_id does not match the run")
    if expected_result_id is not None and str(manifest.result_id) != expected_result_id:
        raise ResultServiceError("ARTIFACT_RESULT_MISMATCH", "manifest result_id does not match")
    if (
        expected_manifest_id is not None
        and str(manifest.artifact_manifest_id) != expected_manifest_id
    ):
        raise ResultServiceError("ARTIFACT_MANIFEST_MISMATCH", "manifest id does not match")


def _verify_formal_result_entry(
    manifest: ArtifactManifest | ArtifactManifest11,
    result_path_obj: Path,
) -> None:
    """The formal MethodResult entry checks shared by every manifest
    consumer: exactly one json_result/1.3 entry with a safe relative
    path equal to the result file name, an existing file and a matching
    SHA-256."""
    if not manifest.artifacts:
        raise ResultServiceError(
            "ARTIFACT_ENTRY_COUNT",
            "manifest must contain at least one artifact entry",
        )
    entry = _find_result_entry(manifest)
    if entry is None:
        raise ResultServiceError(
            "ARTIFACT_ENTRY_COUNT",
            "manifest must contain exactly one json_result/1.3 entry",
        )
    if entry.artifact_type is not ArtifactType.JSON_RESULT:
        raise ResultServiceError(
            "ARTIFACT_TYPE_INVALID",
            "the formal artifact entry must be json_result",
        )
    if entry.schema_or_template_version != "1.3" and entry.schema_or_template_version != "2.0":
        raise ResultServiceError(
            "ARTIFACT_SCHEMA_MISMATCH",
            "artifact schema_or_template_version must be 1.3 or 2.0 for a MethodResult artifact",
        )
    try:
        safe = safe_filename_component(entry.relative_path)
    except ValueError as exc:
        raise ResultServiceError(
            "ARTIFACT_PATH_UNSAFE", f"artifact relative path unsafe: {exc}"
        ) from exc
    if safe != result_path_obj.name:
        raise ResultServiceError(
            "ARTIFACT_PATH_MISMATCH", "artifact path does not match result file"
        )
    if not result_path_obj.exists():
        raise ResultServiceError("ARTIFACT_RESULT_MISSING", "method_result.json does not exist")
    if _file_sha256(result_path_obj) != entry.sha256:
        raise ResultServiceError("ARTIFACT_HASH_MISMATCH", "artifact sha256 mismatch")


def _verify_link_cross_check(
    manifest: ArtifactManifest | ArtifactManifest11, path_obj: Path
) -> None:
    """Immutable result-link cross-check when the link file exists next
    to the manifest: a present-but-corrupt link must never be treated as
    'no link' - any read_result_link failure propagates."""
    link_path = path_obj.parent / "result_link.json"
    if not link_path.exists():
        return
    link = read_result_link(link_path)
    if str(link.run_id) != str(manifest.run_id):
        raise ResultServiceError(
            "ARTIFACT_LINK_RUN_MISMATCH",
            "result link run_id does not match the manifest",
        )
    if link.result_id is not None and str(link.result_id) != str(manifest.result_id):
        raise ResultServiceError(
            "ARTIFACT_LINK_RESULT_MISMATCH",
            "result link result_id does not match the manifest",
        )
    if link.artifact_manifest_id is not None and str(link.artifact_manifest_id) != str(
        manifest.artifact_manifest_id
    ):
        raise ResultServiceError(
            "ARTIFACT_LINK_MANIFEST_MISMATCH",
            "result link manifest id does not match the manifest",
        )


def validate_manifest_before_publish(
    manifest: ArtifactManifest | ArtifactManifest11,
    *,
    result_path: Path | str,
    expected_run_id: str,
    expected_result_id: str | None,
    expected_manifest_id: str,
) -> None:
    """Full pre-publish manifest validation; nothing is written until
    every check passes.

    - artifact-manifest 1.0 / 1.1 RESULT: ``result_id`` non-null and
      exactly one json_result MethodResult entry (+ optional
      search_trace / analysis_summary);
    - artifact-manifest 1.1 COORDINATION (portfolio / sensitivity):
      ``result_id`` null and only analysis_summary / search_trace entries
      (verified for ids + artifact path/hash/schema)."""
    if manifest.artifact_schema_version == "1.1" and manifest.manifest_kind == "coordination":
        _verify_expected_ids(
            manifest,
            expected_run_id=expected_run_id,
            expected_result_id=None,
            expected_manifest_id=expected_manifest_id,
        )
        _validate_optional_entries(manifest, Path(result_path).parent)
        return
    _verify_expected_ids(
        manifest,
        expected_run_id=expected_run_id,
        expected_result_id=expected_result_id,
        expected_manifest_id=expected_manifest_id,
    )
    _verify_formal_result_entry(manifest, Path(result_path))
    _validate_optional_entries(manifest, Path(result_path).parent)


def write_artifact_manifest(
    manifest: ArtifactManifest,
    path: Path | str,
    *,
    result_path: Path | str,
    expected_run_id: str,
    expected_result_id: str,
    expected_manifest_id: str,
) -> None:
    """Validate the manifest completely, then publish with
    immutable-or-identical semantics, then verify the read-back."""
    path_obj = Path(path)
    validate_manifest_before_publish(
        manifest,
        result_path=result_path,
        expected_run_id=expected_run_id,
        expected_result_id=expected_result_id,
        expected_manifest_id=expected_manifest_id,
    )
    _write_immutable_or_identical(
        path_obj, manifest.model_dump(mode="json"), "ARTIFACT_EXISTS_DIFFERENT"
    )
    read_artifact_manifest(
        path_obj,
        result_path=Path(result_path),
        expected_run_id=expected_run_id,
        expected_result_id=expected_result_id,
        expected_manifest_id=expected_manifest_id,
    )


def read_artifact_manifest(
    path: Path | str,
    *,
    result_path: Path | str,
    expected_run_id: str | None = None,
    expected_result_id: str | None = None,
    expected_manifest_id: str | None = None,
) -> ArtifactManifest | ArtifactManifest11:
    """Read and strictly validate the artifact manifest closed loop
    (run_id / result_id / schema / path / hash), including the immutable
    result-link cross-check when the link file exists next to the run
    directory. The reader dispatches EXPLICITLY on artifact_schema_version
    (1.0 result-bound vs 1.1 versioned); a 1.1 COORDINATION manifest
    validates its analysis_summary / search_trace entries without a formal
    MethodResult."""
    path_obj = Path(path)
    result_path_obj = Path(result_path)
    if not path_obj.exists():
        raise ResultServiceError("ARTIFACT_INVALID_FILE", "artifact manifest missing")
    try:
        manifest = load_artifact_manifest(json.loads(path_obj.read_text(encoding="utf-8")))
    except Exception as exc:
        raise ResultServiceError(
            "ARTIFACT_INVALID", f"artifact manifest failed validation: {exc}"
        ) from exc
    _verify_expected_ids(
        manifest,
        expected_run_id=expected_run_id,
        expected_result_id=expected_result_id,
        expected_manifest_id=expected_manifest_id,
    )
    _verify_link_cross_check(manifest, path_obj)
    if manifest.artifact_schema_version == "1.1" and manifest.manifest_kind == "coordination":
        _validate_optional_entries(manifest, result_path_obj.parent)
        return manifest
    _verify_formal_result_entry(manifest, result_path_obj)
    _validate_optional_entries(manifest, result_path_obj.parent)
    return manifest


def read_result_link(path: Path | str) -> ResultLink:
    """Read and strictly validate the result link (missing fields, extra
    fields, invalid UUIDs and invalid null-combos all rejected)."""
    path_obj = Path(path)
    try:
        payload = json.loads(path_obj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResultServiceError("LINK_INVALID_FILE", f"result_link unreadable: {exc}") from exc
    try:
        return ResultLink.model_validate(payload)
    except Exception as exc:
        raise ResultServiceError("LINK_INVALID", f"result_link failed validation: {exc}") from exc


def write_result_link(
    path: Path | str,
    *,
    run_id: uuid.UUID,
    request_id: str,
    result_id: uuid.UUID | None,
    artifact_manifest_id: uuid.UUID | None,
) -> ResultLink:
    """Publish the result link with immutable-or-identical semantics."""
    path_obj = Path(path)
    link = ResultLink(
        run_id=run_id,
        request_id=request_id,
        result_id=result_id,
        artifact_manifest_id=artifact_manifest_id,
    )
    _write_immutable_or_identical(path_obj, link.model_dump(mode="json"), "LINK_EXISTS_DIFFERENT")
    return read_result_link(path_obj)


_ALLOWED_PUBLISH_MODES = frozenset(
    {
        # legacy aliases (kept for callers that predate the semantic modes)
        "zero",
        "samples",
        # semantic modes
        "zero_no_result",  # sample_count==0, no structured result to publish
        "sampled_result",  # sample_count>0, normal publish
        "terminal_method_result",  # sample_count==0 but a structured terminal
        #                          # method result (invalid_method/invalid_data)
        #                          # is still published
    }
)


def reserve_publish_ids(
    *,
    run_dir: Path,
    request_id: str,
    sample_count: int,
    mode: str,
) -> tuple[str | None, str | None]:
    """Idempotently pre-allocate result_id / artifact_manifest_id for a
    (run, request) pair; replays reuse the recorded ids.

    ``mode`` decides whether ids are allocated:
    ``zero_no_result`` (and the legacy ``zero``) allocate nothing;
    ``sampled_result`` / ``terminal_method_result`` (and the legacy
    ``samples``) allocate both ids. A structured terminal result
    (invalid_method / invalid_data with sample_count=0) therefore still
    receives ids and can be published as method_result.json +
    artifact_manifest.json - without faking sample_count."""
    if mode not in _ALLOWED_PUBLISH_MODES:
        raise ResultServiceError(
            "INVALID_PUBLISH_MODE",
            f"unknown publish mode {mode!r}",
        )
    scope = run_dir / "requests"
    scope.mkdir(parents=True, exist_ok=True)
    request_file = scope / f"{safe_filename_component(request_id)}-publish.json"
    params = {"request_id": request_id, "mode": mode, "sample_count": sample_count}
    if request_file.exists():
        recorded = json.loads(request_file.read_text(encoding="utf-8"))
        if recorded.get("params_hash") != _params_hash("publish_results", params):
            raise ResultServiceError(
                "REQUEST_ID_CONFLICT",
                f"request_id {request_id} reused with different publish params",
            )
        return recorded.get("result_id"), recorded.get("artifact_manifest_id")
    if mode in ("zero", "zero_no_result"):
        result_id = None
        manifest_id = None
    else:
        result_id = str(uuid.uuid4())
        manifest_id = str(uuid.uuid4())
    record = {
        "request_id": request_id,
        "action": "publish_results",
        "params_hash": _params_hash("publish_results", params),
        "status": "committed",
        "result_id": result_id,
        "artifact_manifest_id": manifest_id,
    }
    try:
        atomic_write_bytes(request_file, stable_dump(record), immutable=True)
    except ObjectExistsError:
        recorded = json.loads(request_file.read_text(encoding="utf-8"))
        if recorded.get("params_hash") != _params_hash("publish_results", params):
            raise ResultServiceError(
                "REQUEST_ID_CONFLICT",
                f"request_id {request_id} reused with different publish params",
            ) from None
        return recorded.get("result_id"), recorded.get("artifact_manifest_id")
    return result_id, manifest_id


def _params_hash(action: str, params: dict[str, Any]) -> str:
    from capacity_credit.application.atomic_io import hash_payload

    return hash_payload({"action": action, **params})


__all__ = [
    "ResultLink",
    "ResultServiceError",
    "build_adequacy_method_result",
    "build_elcc_method_result",
    "read_artifact_manifest",
    "read_method_result",
    "read_result_link",
    "reserve_publish_ids",
    "validate_manifest_before_publish",
    "write_artifact_manifest",
    "write_method_result",
    "write_result_link",
]
