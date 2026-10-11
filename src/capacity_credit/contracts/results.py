"""Unified method result contract, Schema 1.3 (methodology V0.4 §16)."""

from __future__ import annotations

import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capacity_credit.contracts.elcc_diagnostic_specs import (
    ElccDiagnosticsError,
    validate_diagnostic_classification,
)
from capacity_credit.contracts.elcc_status import validate_method_result_elcc_status
from capacity_credit.contracts.enums import (
    AllocationType,
    AssessmentAnchor,
    CapacityValueType,
    ChronologyMode,
    DataGrade,
    InterpolationMethod,
    Method,
    PerturbationDimension,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    RmriStatus,
    SearchConvergence,
    Severity,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)


class RiskRole(BaseModel):
    """Four risk metrics for one role (methodology V0.4 §16)."""

    model_config = ConfigDict(extra="forbid")

    lole_days_per_year: float
    lolh_hours_per_year: float
    eue_mwh_per_year: float
    neue_ppm: float


class RiskMetrics(BaseModel):
    """Risk metrics by role: base / candidate / matched."""

    model_config = ConfigDict(extra="forbid")

    base: RiskRole
    candidate: RiskRole | None = None
    matched: RiskRole | None = None


class Diagnostic(BaseModel):
    """Structured diagnostic entry (methodology V0.4 §15.2)."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Severity
    message: str


class ScenarioBirthCertificate(BaseModel):
    """R3-B3: the FOUR scenario design variables of one capacity-value
    run (方法说明第四章(三)), carried WITH the result as its birth
    certificate - results with different values are different calibers
    and must never be mixed (第八章风险1).

    caliber: the marginal/average calibre (capacity_value_type value);
    increment_mw: the connected capacity (delta calibre MUST declare it);
    placement: aggregate system-wide in the single-area model (multi-area
    is reserved and must extend this literal when implemented);
    composition: individual vs combination access, with the resolved
    member ids (combination results carry interaction effects)."""

    model_config = ConfigDict(extra="forbid")

    caliber: str
    increment_mw: float | None = None
    placement: Literal["system_wide"]
    composition_mode: Literal["individual", "combination"]
    member_ids: list[str] = Field(default_factory=list)


class MethodResult(BaseModel):
    """Unified result for adequacy / elcc / efc / mri / rmri, Schema 1.3."""

    model_config = ConfigDict(extra="forbid")

    result_schema_version: Literal["1.3"]
    result_id: uuid.UUID
    run_id: uuid.UUID
    method: Method
    method_profile: str
    resource_or_portfolio_id: str | None = None
    capacity_value_type: CapacityValueType
    baseline_portfolio_version: str | None = None
    allocation_type: AllocationType
    allocation_rule_id: str | None = None
    chronology_mode: ChronologyMode
    storage_soc_initialization_mode: StorageSocInitializationMode
    soc_year_boundary_mode: SocYearBoundaryMode
    assessment_anchor: AssessmentAnchor | None = None
    anchor_risk_metric: RiskMetric | None = None
    anchor_risk_value: float | None = None
    anchor_calibration_method: str | None = None
    anchor_standard_source_id: str | None = None
    reference_capacity_mw: float | None = None
    accredited_capacity_mw: float | None = None
    capacity_credit_fraction: float | None = None
    perturbation_dimension: PerturbationDimension | None = None
    perturbation_vector: list[float] | None = None
    normalization_basis_mwh_per_mw: float | None = None
    rmri_status: RmriStatus
    primary_risk_metric: RiskMetric
    risk_metrics: RiskMetrics
    base_risk: float
    candidate_risk: float | None = None
    matched_risk: float | None = None
    risk_difference: float | None = None
    search_bracket_low_mw: float | None = None
    search_bracket_high_mw: float | None = None
    search_bracket_low_risk: float | None = None
    search_bracket_high_risk: float | None = None
    interpolation_method: InterpolationMethod
    empirical_risk_resolution: float | None = None
    confidence_level: float | None = None
    lower_ci: float | None = None
    upper_ci: float | None = None
    sample_count: int = Field(ge=0)
    scenario_bank_id: str | None = None
    result_status: ResultStatus
    risk_convergence: RiskConvergence
    search_convergence: SearchConvergence
    effective_data_grade: DataGrade
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    # R3-B3: the four scenario design variables (方法说明第四章(三)) as
    # an optional structure - absent on legacy results (honest tri-state
    # at read time), filled by the ELCC/EFC publication chains
    scenario_birth_certificate: ScenarioBirthCertificate | None = None
    # MR-01.3: the delivery year this EFC factor is valid for (降额系数
    # 只对所属交付年有效); None = no auction linkage declared
    delivery_year: int | None = None
    auction_round: Literal["T4", "T1", "none"] | None = None
    # MR-01.2: the attested technology class of the candidate (分技术
    # 类别降额系数表的分组维度); None = undeclared (teaching/research)
    technology_class: str | None = None
    # MR-01.1: the criterion calibre this run judged risk in (annual =
    # legacy full-year metrics; winter_evening_peak = window-filtered)
    criterion_mode: str | None = None
    # Step 2 Stage A（需求类设计 §3.2）: the absolute-target search fields.
    # target_risk_value = the declared LOLE/EUE target; solver_caliber is
    # the explicitly chosen adjustment axis (Q-D4 裁决 2026-10-10:
    # adjust_load 开放——λ 轴，供给冻结、负荷保形缩放); source_* carry
    # the lineage of the completed adequacy run the search stood on.
    target_risk_value: float | None = None
    solver_caliber: Literal["adjust_perfect_capacity", "adjust_load"] | None = None
    # Q-D4: the load-scale point λ* (valid adjust_load results REQUIRE it;
    # every other caliber FORBIDS it). accredited_capacity_mw carries the
    # CONVERTED load level MW (λ* × peak load) under this caliber.
    load_scale_factor: float | None = Field(default=None, gt=0.0)
    source_project_ref: str | None = None
    source_run_ref: str | None = None
    source_result_id: str | None = None

    @model_validator(mode="after")
    def _validate_unified_result(self) -> Self:
        self._validate_primary_scalars()

        if self.method is Method.ADEQUACY:
            self._validate_adequacy_branch()
        elif self.method in (Method.MRI, Method.RMRI):
            self._validate_mri_branch()
        elif self.method is Method.DEMAND_TARGET_CAPACITY:
            self._validate_target_capacity_branch()
        else:
            self._validate_elcc_efc_branch()

        if (
            self.capacity_value_type is CapacityValueType.MARGINAL
            and self.baseline_portfolio_version is None
        ):
            raise ValueError(
                "baseline_portfolio_version is required for capacity_value_type=marginal"
            )
        if (
            self.allocation_type is AllocationType.GOVERNANCE_RULE
            and self.allocation_rule_id is None
        ):
            raise ValueError("allocation_rule_id is required for allocation_type=governance_rule")
        return self

    def _validate_primary_scalars(self) -> None:
        """Primary scalar cross-checks against the role aggregates."""
        primary = self.primary_risk_metric
        base_scalar = getattr(self.risk_metrics.base, primary.value)

        if self.base_risk != base_scalar:
            raise ValueError("base_risk must equal risk_metrics.base[primary_risk_metric]")
        if self.risk_metrics.candidate is not None:
            candidate_scalar = getattr(self.risk_metrics.candidate, primary.value)
            if self.candidate_risk is None or self.candidate_risk != candidate_scalar:
                raise ValueError(
                    "candidate_risk must equal risk_metrics.candidate[primary_risk_metric]"
                )
        if self.risk_metrics.matched is not None:
            matched_scalar = getattr(self.risk_metrics.matched, primary.value)
            if self.matched_risk is None or self.matched_risk != matched_scalar:
                raise ValueError(
                    "matched_risk must equal risk_metrics.matched[primary_risk_metric]"
                )
        elif self.matched_risk is not None:
            raise ValueError("matched_risk requires risk_metrics.matched")

    def _validate_adequacy_branch(self) -> None:
        if self.resource_or_portfolio_id is None:
            raise ValueError("method=adequacy requires resource_or_portfolio_id")
        if self.risk_metrics.candidate is not None or self.risk_metrics.matched is not None:
            raise ValueError("method=adequacy only allows risk_metrics.base")
        if (
            self.candidate_risk is not None
            or self.matched_risk is not None
            or self.risk_difference is not None
        ):
            raise ValueError(
                "method=adequacy requires candidate_risk/matched_risk/risk_difference to be null"
            )
        self._validate_adequacy_null_fields()
        if (
            self.assessment_anchor is not None
            or self.anchor_risk_metric is not None
            or self.anchor_risk_value is not None
            or self.anchor_calibration_method is not None
            or self.anchor_standard_source_id is not None
        ):
            raise ValueError("method=adequacy requires anchor fields to be null")

    def _validate_adequacy_null_fields(self) -> None:
        """Adequacy result-field nullity rules (C901 split)."""
        if (
            self.reference_capacity_mw is not None
            or self.accredited_capacity_mw is not None
            or self.capacity_credit_fraction is not None
        ):
            raise ValueError("method=adequacy requires capacity value fields to be null")
        if self.capacity_value_type is not CapacityValueType.NOT_APPLICABLE:
            raise ValueError("method=adequacy requires capacity_value_type=not_applicable")
        if self.baseline_portfolio_version is not None:
            raise ValueError("method=adequacy requires baseline_portfolio_version to be null")
        if self.allocation_type is not AllocationType.NONE:
            raise ValueError("method=adequacy requires allocation_type=none")
        if self.allocation_rule_id is not None:
            raise ValueError("method=adequacy requires allocation_rule_id to be null")
        self._validate_adequacy_status_null()
        self._validate_adequacy_search_null()

    def _validate_adequacy_status_null(self) -> None:
        """Adequacy status / interpolation / perturbation nullity."""
        if self.rmri_status is not RmriStatus.NOT_APPLICABLE:
            raise ValueError("method=adequacy requires rmri_status=not_applicable")
        if self.search_convergence is not SearchConvergence.NOT_APPLICABLE:
            raise ValueError("method=adequacy requires search_convergence=not_applicable")
        if self.interpolation_method is not InterpolationMethod.NOT_APPLICABLE:
            raise ValueError("method=adequacy requires interpolation_method=not_applicable")
        if (
            self.perturbation_dimension is not None
            or self.perturbation_vector is not None
            or self.normalization_basis_mwh_per_mw is not None
        ):
            raise ValueError("method=adequacy requires perturbation fields to be null")

    def _validate_adequacy_search_null(self) -> None:
        """Adequacy search-field nullity."""
        if (
            self.search_bracket_low_mw is not None
            or self.search_bracket_high_mw is not None
            or self.search_bracket_low_risk is not None
            or self.search_bracket_high_risk is not None
            or self.empirical_risk_resolution is not None
        ):
            raise ValueError("method=adequacy requires search fields to be null")

    def _validate_target_capacity_branch(self) -> None:
        """Step 2 Stage A（需求类设计 §3.2）：绝对目标搜索分支。

        base-only 风险角色（现状风险）；口径与来源血缘必填；目标指标仅
        LOLE/EUE；valid ⇒ 必须带点值（达标所需容量），未收敛 ⇒ 无点值
        （只报区间＋诊断——不解决的搜索绝不产出 MW）。"""
        if self.risk_metrics.candidate is not None or self.risk_metrics.matched is not None:
            raise ValueError(
                "method=demand_target_capacity only allows risk_metrics.base"
            )
        if self.solver_caliber is None:
            raise ValueError(
                "method=demand_target_capacity requires solver_caliber（求解口径须显式）"
            )
        if self.target_risk_value is None or self.target_risk_value <= 0.0:
            raise ValueError(
                "method=demand_target_capacity requires target_risk_value > 0"
            )
        if self.primary_risk_metric not in (
            RiskMetric.LOLE_DAYS_PER_YEAR,
            RiskMetric.EUE_MWH_PER_YEAR,
        ):
            raise ValueError(
                "primary_risk_metric must be LOLE or EUE for demand_target_capacity"
            )
        if self.source_project_ref is None or self.source_run_ref is None:
            raise ValueError(
                "method=demand_target_capacity requires source lineage "
                "(source_project_ref + source_run_ref)"
            )
        if (
            self.candidate_risk is not None
            or self.matched_risk is not None
            or self.risk_difference is not None
            or self.capacity_credit_fraction is not None
        ):
            raise ValueError(
                "method=demand_target_capacity forbids candidate/matched role fields"
            )
        if self.search_convergence is SearchConvergence.NOT_APPLICABLE:
            raise ValueError(
                "method=demand_target_capacity requires a real search convergence state"
            )
        if self.result_status in (ResultStatus.VALID, ResultStatus.VALID_WITH_WARNING):
            if self.accredited_capacity_mw is None:
                raise ValueError(
                    "valid demand_target_capacity requires accredited_capacity_mw "
                    "（达标所需容量点值）"
                )
            if (
                self.search_bracket_low_mw is None
                or self.search_bracket_high_mw is None
            ):
                raise ValueError(
                    "valid demand_target_capacity requires the search bracket"
                )
        elif self.accredited_capacity_mw is not None:
            raise ValueError(
                "not-converged demand_target_capacity must not carry a point value"
            )
        # Q-D4（2026-10-10 裁决）：调整负荷口径的有效结果必带 λ；
        # 其余口径禁填（口径字段不混用）
        if self.solver_caliber == "adjust_load":
            if self.result_status in (ResultStatus.VALID, ResultStatus.VALID_WITH_WARNING):
                if self.load_scale_factor is None:
                    raise ValueError(
                        "valid adjust_load demand_target_capacity requires "
                        "load_scale_factor（λ 点值）"
                    )
        elif self.load_scale_factor is not None:
            raise ValueError(
                "load_scale_factor is only allowed for solver_caliber="
                "adjust_load"
            )

    def _validate_mri_branch(self) -> None:
        if self.risk_metrics.candidate is None:
            raise ValueError("mri/rmri results require risk_metrics.candidate")
        if self.capacity_value_type is CapacityValueType.NOT_APPLICABLE:
            raise ValueError("mri/rmri results require an applicable capacity_value_type")
        if self.assessment_anchor is None:
            raise ValueError("method=mri/rmri requires assessment_anchor")
        if (
            self.anchor_risk_metric is None
            or self.anchor_risk_value is None
            or self.anchor_calibration_method is None
        ):
            raise ValueError(
                "method=mri/rmri requires anchor_risk_metric, "
                "anchor_risk_value and anchor_calibration_method"
            )
        if self.perturbation_dimension is None:
            raise ValueError("method=mri/rmri requires perturbation_dimension")
        if (
            self.assessment_anchor is AssessmentAnchor.CALIBRATED_STANDARD
            and self.anchor_standard_source_id is None
        ):
            raise ValueError(
                "assessment_anchor=calibrated_standard requires anchor_standard_source_id"
            )
        if (
            self.assessment_anchor is AssessmentAnchor.CURRENT_SYSTEM
            and self.anchor_standard_source_id is not None
        ):
            raise ValueError(
                "assessment_anchor=current_system requires anchor_standard_source_id to be null"
            )
        if self.rmri_status is not RmriStatus.NOT_APPLICABLE:
            raise ValueError("rmri_status must be not_applicable for mri/rmri results")

    def _validate_elcc_efc_branch(self) -> None:
        if self.risk_metrics.candidate is None:
            raise ValueError("elcc/efc results require risk_metrics.candidate")
        if self.capacity_value_type is CapacityValueType.NOT_APPLICABLE:
            raise ValueError("elcc/efc results require an applicable capacity_value_type")
        if (
            self.assessment_anchor is not None
            or self.anchor_risk_metric is not None
            or self.anchor_risk_value is not None
            or self.anchor_calibration_method is not None
            or self.anchor_standard_source_id is not None
        ):
            raise ValueError("anchor fields must be null for elcc/efc results")
        if self.perturbation_dimension is not None or self.perturbation_vector is not None:
            raise ValueError("perturbation fields must be null for elcc/efc results")
        if self.rmri_status is not RmriStatus.NOT_APPLICABLE:
            raise ValueError("rmri_status must be not_applicable for elcc/efc results")
        self._validate_elcc_efc_status()

    def _validate_elcc_efc_status(self) -> None:
        """Round 7: PRODUCT-result severity contract (methodology
        §15.1 / §15.2 / §16), NOT the search-outcome matrix. valid /
        valid_with_warning carry the full ELCC result; valid must be
        free of warning/error diagnostics, valid_with_warning requires
        >= 1 warning and no error; not_converged / low_resolution /
        invalid_method / invalid_data must not carry errors except that
        invalid_method requires a method_error and invalid_data requires
        a data_error diagnostic;
        invalid_data additionally nulls EVERY point-estimate field."""
        specs = []
        for diagnostic in self.diagnostics:
            try:
                specs.append(
                    validate_diagnostic_classification(
                        diagnostic.code, diagnostic.severity
                    )
                )
            except ElccDiagnosticsError as exc:
                raise ValueError(f"{exc.code}: {exc.message}") from exc
        has_warning = any(spec.severity is Severity.WARNING for spec in specs)
        has_method_error = any(spec.category == "method_error" for spec in specs)
        has_data_error = any(spec.category == "data_error" for spec in specs)
        validate_method_result_elcc_status(
            risk_convergence=self.risk_convergence,
            search_convergence=self.search_convergence,
            result_status=self.result_status,
            has_warning=has_warning,
            has_method_error=has_method_error,
            has_data_error=has_data_error,
        )
        if self.result_status in (
            ResultStatus.VALID,
            ResultStatus.VALID_WITH_WARNING,
        ):
            self._validate_elcc_efc_valid_fields()
        elif self.result_status is ResultStatus.INVALID_DATA:
            self._validate_elcc_efc_invalid_data_fields()
        else:
            self._validate_elcc_efc_non_valid_fields()

    def _validate_elcc_efc_valid_fields(self) -> None:
        """A valid / valid_with_warning elcc/efc result carries the full
        matched-point set (the warning state must NOT be wiped by the
        non-valid clearing logic)."""
        if self.accredited_capacity_mw is None:
            raise ValueError("a valid elcc/efc result must carry accredited_capacity_mw")
        if self.risk_metrics.matched is None:
            raise ValueError("a valid elcc/efc result must carry risk_metrics.matched")
        if self.matched_risk is None:
            raise ValueError("a valid elcc/efc result must carry matched_risk")
        if self.risk_difference is None:
            raise ValueError("a valid elcc/efc result must carry risk_difference")
        if self.interpolation_method is InterpolationMethod.NOT_APPLICABLE:
            raise ValueError("a valid elcc/efc result must carry a valid interpolation_method")

    def _validate_elcc_efc_invalid_data_fields(self) -> None:
        """STRUCTURED invalid_data: the data/method input is unusable;
        EVERY point-estimate field must be null - no fabricated
        capacity, matched values, search brackets or resolution. The
        real base / candidate 0 MW roles may remain (they were actually
        computed). EARLY pre-run data errors never reach a MethodResult
        (they are carried by the data-validation artifact)."""
        for field, value in (
            ("accredited_capacity_mw", self.accredited_capacity_mw),
            ("capacity_credit_fraction", self.capacity_credit_fraction),
            ("matched_risk", self.matched_risk),
            ("risk_difference", self.risk_difference),
            ("search_bracket_low_mw", self.search_bracket_low_mw),
            ("search_bracket_high_mw", self.search_bracket_high_mw),
            ("search_bracket_low_risk", self.search_bracket_low_risk),
            ("search_bracket_high_risk", self.search_bracket_high_risk),
            ("empirical_risk_resolution", self.empirical_risk_resolution),
        ):
            if value is not None:
                raise ValueError(f"an invalid_data elcc/efc result must not carry {field}")
        if self.risk_metrics.matched is not None:
            raise ValueError("an invalid_data elcc/efc result must not carry risk_metrics.matched")
        if self.interpolation_method is not InterpolationMethod.NOT_APPLICABLE:
            raise ValueError(
                "an invalid_data elcc/efc result must use interpolation_method=not_applicable"
            )

    def _validate_elcc_efc_non_valid_fields(self) -> None:
        """A non-valid elcc/efc result clears the result scalars (the
        candidate 0 MW role may legitimately remain)."""
        if self.accredited_capacity_mw is not None:
            raise ValueError("a non-valid elcc/efc result must not carry accredited_capacity_mw")
        if self.capacity_credit_fraction is not None:
            raise ValueError("a non-valid elcc/efc result must not carry capacity_credit_fraction")
        if self.risk_metrics.matched is not None:
            raise ValueError("a non-valid elcc/efc result must not carry risk_metrics.matched")
        if self.matched_risk is not None:
            raise ValueError("a non-valid elcc/efc result must not carry matched_risk")
        if self.risk_difference is not None:
            raise ValueError("a non-valid elcc/efc result must not carry risk_difference")
        if self.interpolation_method is not InterpolationMethod.NOT_APPLICABLE:
            raise ValueError(
                "a non-valid elcc/efc result must use interpolation_method=not_applicable"
            )

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> MethodResult:
        return cls.model_validate_json(text)
