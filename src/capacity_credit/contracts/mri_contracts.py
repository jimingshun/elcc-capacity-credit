"""T18 MRI/rMRI contracts (run-config 1.1 + method-result 2.0).

This module is the single source for the T18-0 frozen MRI/rMRI contract
(methodology V0.8 §16A):

- ``EngineeringTolerance`` / ``PhysicalDomain`` / ``CrnConfig`` /
  ``StabilityThresholds`` are the run-config 1.1 structured objects with
  the EXACT field shapes frozen in §16A.4.1--16A.4.4 (directly
  serializable to Pydantic / JSON Schema);
- ``MriMethodConfig`` is the typed method section embedded in the formal
  RunConfig 1.1 for ``run_type=mri|rmri``;
- ``RunConfig11`` is the versioned formal run config (1.1) that accepts
  the MRI method config; the version reader dispatches 1.0 vs 1.1;
- ``PerturbationVector`` / ``StepEvaluation`` / ``MriResult2`` are the
  method-result 2.0 evidence types (never a second risk calculator).

The new 2.0 result model lives next to the existing 1.3 model WITHOUT
modifying ``results.MethodResult``: the version reader is the only place
that understands both. ``extra="forbid"`` is used everywhere so a 1.3
reader can never claim to ignore 2.0 fields.
"""

from __future__ import annotations

import math
import uuid
from typing import Any, Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capacity_credit.contracts.enums import (
    AllocationType,
    AssessmentAnchor,
    CapacityValueType,
    ChronologyMode,
    DataGrade,
    DifferenceScheme,
    Method,
    PerturbationDimension,
    ResultStatus,
    RiskConvergence,
    RiskMetric,
    RmriStatus,
    RunMode,
    RunType,
    SearchConvergence,
    SocYearBoundaryMode,
    StabilitySourceType,
    StepMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.results import Diagnostic

_STEP_UNIT = Literal["mw", "mwh"]
_STEP_CONSISTENCY_RULE = Literal["joint_confidence_interval"]


class EngineeringTolerance(BaseModel):
    """run-config 1.1 ``engineering_tolerance`` (§16A.4.1)."""

    model_config = ConfigDict(extra="forbid")

    capacity_mw: float | None = None
    energy_mwh: float | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for field in ("capacity_mw", "energy_mwh"):
            value = getattr(self, field)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{field} must be a real number or null")
                if value <= 0.0:
                    raise ValueError(f"{field} must be greater than 0 when set")
        return self


class PhysicalDomain(BaseModel):
    """run-config 1.1 ``physical_domain`` (§16A.4.2)."""

    model_config = ConfigDict(extra="forbid")

    step_unit: _STEP_UNIT
    max_positive_step: float
    max_negative_step: float = 0.0
    constraint_source: str

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if isinstance(self.max_positive_step, bool) or not isinstance(
            self.max_positive_step, (int, float)
        ):
            raise TypeError("max_positive_step must be a real number")
        if self.max_positive_step <= 0.0:
            raise ValueError("max_positive_step must be greater than 0")
        if isinstance(self.max_negative_step, bool) or not isinstance(
            self.max_negative_step, (int, float)
        ):
            raise TypeError("max_negative_step must be a real number")
        if self.max_negative_step < 0.0:
            raise ValueError("max_negative_step must be non-negative")
        if not isinstance(self.constraint_source, str) or not self.constraint_source.strip():
            raise ValueError("constraint_source must be a non-empty string")
        return self


class CrnConfig(BaseModel):
    """run-config 1.1 ``crn_config`` (§16A.4.3).

    MRI/rMRI never allow ``enabled=false``; ``scenario_bank_id`` keeps a
    single canonical position at run-config top level and is referenced
    here, never duplicated as a second source of truth.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: Literal[True] = True
    random_stream_version: str
    pairing_key_fields: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (
            not isinstance(self.random_stream_version, str)
            or not self.random_stream_version.strip()
        ):
            raise ValueError("random_stream_version must be a non-empty string")
        if not self.pairing_key_fields:
            raise ValueError("pairing_key_fields must be a non-empty list")
        if len(set(self.pairing_key_fields)) != len(self.pairing_key_fields):
            raise ValueError("pairing_key_fields must be unique")
        return self


class StabilityThresholds(BaseModel):
    """run-config 1.1 ``stability_thresholds`` (§16A.4.4)."""

    model_config = ConfigDict(extra="forbid")

    denominator_relative_se_max: float | None = None
    step_consistency_rule: _STEP_CONSISTENCY_RULE = "joint_confidence_interval"
    source_type: StabilitySourceType
    source_ref: str

    @model_validator(mode="after")
    def _validate(self) -> Self:
        value = self.denominator_relative_se_max
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("denominator_relative_se_max must be a real number or null")
            if value <= 0.0:
                raise ValueError("denominator_relative_se_max must be greater than 0 when set")
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise ValueError("source_ref must be a non-empty string")
        return self


class CustomPerturbationComponent(BaseModel):
    """One custom-vector component with an explicit physical unit
    (method-result 2.0 ``custom_components`` / run-config 1.1
    ``custom_perturbation_vector.components``). A frozen structured
    component - never an unlabelled ``Any`` dict."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["power_mw", "energy_mwh", "other"]
    name: str
    value: float
    unit: str

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("component name must be a non-empty string")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("component value must be a real number")  # noqa: TRY004
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("component unit must be a non-empty string")
        return self


class CustomPerturbationVector(BaseModel):
    """run-config 1.1 ``custom_perturbation_vector`` (structured, frozen).

    ``value`` of each component is the per-scalar-step delta in the
    component's physical unit; ``scalar_step_unit`` declares the scalar
    step unit. ``mw_equivalent_per_scalar_unit`` is the explicit MW
    equivalent mapping used ONLY for rMRI normalisation (absent -> the
    raw MRI is preserved with ``RMRI_CUSTOM_NORMALIZATION_MISSING``)."""

    model_config = ConfigDict(extra="forbid")

    scalar_step_unit: str
    components: list[CustomPerturbationComponent] = Field(min_length=1)
    mw_equivalent_per_scalar_unit: float | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not isinstance(self.scalar_step_unit, str) or not self.scalar_step_unit.strip():
            raise ValueError("scalar_step_unit must be a non-empty string")
        names = [component.name for component in self.components]
        if len(set(names)) != len(names):
            raise ValueError("custom_perturbation_vector component names must be unique")
        for component in self.components:
            if component.role == "other":
                raise ValueError(
                    "custom_perturbation_vector role=other requires an explicit target field "
                    "mapping; none is implemented (power_mw/energy_mwh only)"
                )
        if self.mw_equivalent_per_scalar_unit is not None:
            if isinstance(self.mw_equivalent_per_scalar_unit, bool) or not isinstance(
                self.mw_equivalent_per_scalar_unit, (int, float)
            ):
                raise ValueError("mw_equivalent_per_scalar_unit must be a real number or null")
            if self.mw_equivalent_per_scalar_unit <= 0.0:
                raise ValueError("mw_equivalent_per_scalar_unit must be greater than 0 when set")
        return self


class MriMethodConfig(BaseModel):
    """Typed MRI method section (run-config 1.1 ``method_config``).

    ``resource_or_portfolio_id`` identifies the REAL target resource /
    portfolio being perturbed (resolved against the frozen standard
    tables by object identity - never a surrogate for the perturbation
    dimension). ``duration_hours`` is the fixed base duration required
    for ``fixed_duration`` so ``delta_energy_mwh = duration_hours x
    delta_power_mw`` and the before/after duration stay equal."""

    model_config = ConfigDict(extra="forbid")

    method_config_schema_version: Literal["1.1"] = "1.1"
    resource_or_portfolio_id: str
    perturbation_dimension: PerturbationDimension
    difference_scheme: DifferenceScheme
    step_mode: StepMode
    base_step: float | None = None
    duration_hours: float | None = None
    step_multipliers: list[float] = Field(default_factory=lambda: [0.5, 1.0, 2.0])
    engineering_tolerance: EngineeringTolerance
    physical_domain: PhysicalDomain
    crn_config: CrnConfig
    stability_thresholds: StabilityThresholds
    normalization_basis_mwh_per_mw: float | None = None
    custom_perturbation_vector: CustomPerturbationVector | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (
            not isinstance(self.resource_or_portfolio_id, str)
            or not self.resource_or_portfolio_id.strip()
        ):
            raise ValueError("resource_or_portfolio_id must be a non-empty string")
        if self.step_mode is StepMode.EXPLICIT:
            if self.base_step is None:
                raise ValueError("base_step is required when step_mode=explicit")
        else:
            if self.base_step is not None:
                raise ValueError("base_step must be null when step_mode=adaptive")
        if self.base_step is not None:
            if isinstance(self.base_step, bool) or not isinstance(self.base_step, (int, float)):
                raise ValueError("base_step must be a real number")
            if self.base_step <= 0.0:
                raise ValueError("base_step must be greater than 0")
        if self.perturbation_dimension is PerturbationDimension.FIXED_DURATION:
            if self.duration_hours is None:
                raise ValueError("duration_hours is required for fixed_duration")
            if isinstance(self.duration_hours, bool) or not isinstance(
                self.duration_hours, (int, float)
            ):
                raise ValueError("duration_hours must be a real number")
            if self.duration_hours <= 0.0:
                raise ValueError("duration_hours must be greater than 0")
        elif self.duration_hours is not None:
            raise ValueError("duration_hours is only valid for fixed_duration")
        if len(self.step_multipliers) < 3:
            raise ValueError("step_multipliers must have at least 3 elements")
        if 1.0 not in self.step_multipliers:
            raise ValueError("step_multipliers must include 1.0")
        if len(set(self.step_multipliers)) != len(self.step_multipliers):
            raise ValueError("step_multipliers must be unique")
        for multiplier in self.step_multipliers:
            if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
                raise TypeError("step_multipliers must be real numbers")
            if multiplier <= 0.0:
                raise ValueError("step_multipliers must be positive")
        # engineering_tolerance conditionally required per dimension.
        if (
            self.perturbation_dimension
            in (
                PerturbationDimension.FIXED_DURATION,
                PerturbationDimension.POWER_ONLY,
            )
            and self.engineering_tolerance.capacity_mw is None
        ):
            raise ValueError(
                "engineering_tolerance.capacity_mw is required for fixed_duration/power_only"
            )
        if (
            self.perturbation_dimension is PerturbationDimension.ENERGY_ONLY
            and self.engineering_tolerance.energy_mwh is None
        ):
            raise ValueError("engineering_tolerance.energy_mwh is required for energy_only")
        if (
            self.perturbation_dimension is PerturbationDimension.CUSTOM_VECTOR
            and self.custom_perturbation_vector is None
        ):
            raise ValueError("custom_perturbation_vector is required for custom_vector")
        if (
            self.perturbation_dimension is not PerturbationDimension.CUSTOM_VECTOR
            and self.custom_perturbation_vector is not None
        ):
            raise ValueError(
                "custom_perturbation_vector is only valid for perturbation_dimension=custom_vector"
            )
        return self


class RunConfig11(BaseModel):
    """Formal run-config 1.1 (MRI/rMRI). Field set extends 1.0 with the
    structured MRI objects; the version reader dispatches 1.0/1.1."""

    model_config = ConfigDict(extra="forbid")

    run_config_schema_version: Literal["1.1"] = "1.1"
    run_mode: RunMode
    run_type: RunType
    source_run_id: uuid.UUID | None = None
    parent_run_ids: list[uuid.UUID] = Field(default_factory=list)
    method_profile: str
    primary_risk_metric: RiskMetric
    weather_year_weight_set_id: str
    chronology_mode: ChronologyMode
    storage_soc_initialization_mode: StorageSocInitializationMode
    soc_year_boundary_mode: SocYearBoundaryMode
    assessment_anchor: AssessmentAnchor
    perturbation_dimension: PerturbationDimension
    normalization_basis_mwh_per_mw: float | None = None
    capacity_value_type: CapacityValueType
    baseline_portfolio_version: str | None = None
    allocation_type: AllocationType
    allocation_rule_id: str | None = None
    random_seed: int
    scenario_bank_id: str
    scenario_id: str | None = None
    method_config: MriMethodConfig

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.run_type not in (RunType.MRI, RunType.RMRI):
            raise ValueError("run-config 1.1 requires run_type=mri or run_type=rmri")
        # Duplicate fact-source closure (§16A.4): the top-level mirrors of
        # the method-section fields must agree; the workflow only reads the
        # canonical method_config location.
        if self.perturbation_dimension != self.method_config.perturbation_dimension:
            raise ValueError(
                "top-level perturbation_dimension must equal "
                "method_config.perturbation_dimension (single authoritative source)"
            )
        if self.normalization_basis_mwh_per_mw != self.method_config.normalization_basis_mwh_per_mw:
            raise ValueError(
                "top-level normalization_basis_mwh_per_mw must equal "
                "method_config.normalization_basis_mwh_per_mw (single authoritative source)"
            )
        if self.run_type is RunType.MRI:
            if self.method_config.stability_thresholds.denominator_relative_se_max is not None:
                raise ValueError(
                    "stability_thresholds.denominator_relative_se_max must be null for run_type=mri"
                )
        else:
            se_max = self.method_config.stability_thresholds.denominator_relative_se_max
            if se_max is None:
                raise ValueError(
                    "stability_thresholds.denominator_relative_se_max is required and must be "
                    "greater than 0 for run_type=rmri"
                )
            if se_max <= 0.0:
                raise ValueError(
                    "stability_thresholds.denominator_relative_se_max must be greater than 0 "
                    "for run_type=rmri"
                )
        if (
            self.perturbation_dimension is PerturbationDimension.ENERGY_ONLY
            and self.normalization_basis_mwh_per_mw is not None
            and self.normalization_basis_mwh_per_mw <= 0.0
        ):
            raise ValueError("normalization_basis_mwh_per_mw must be greater than 0 when set")
        return self

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_yaml(cls, text: str) -> RunConfig11:
        import yaml

        return cls.model_validate(yaml.safe_load(text))

    @classmethod
    def from_json(cls, text: str) -> RunConfig11:
        return cls.model_validate_json(text)


class PerturbationVector(BaseModel):
    """method-result 2.0 structured perturbation vector (§16A.3).

    ``custom_components`` is a verifiable structured list of applied
    component deltas (name / role / value / unit) - never a bare
    ``dict[str, Any]``."""

    model_config = ConfigDict(extra="forbid")

    dimension: PerturbationDimension
    scalar_step: float
    delta_power_mw: float = 0.0
    delta_energy_mwh: float = 0.0
    duration_before_h: float | None = None
    duration_after_h: float | None = None
    custom_components: list[CustomPerturbationComponent] | None = None


class StepEvaluation(BaseModel):
    """method-result 2.0 one step evaluation (§16A.2)."""

    model_config = ConfigDict(extra="forbid")

    step_multiplier: float
    scalar_step: float
    perturbation_vector: PerturbationVector
    baseline_risk: float
    minus_risk: float | None = None
    plus_risk: float
    paired_risk_difference: float
    applied_difference_scheme: DifferenceScheme
    mri_estimate: float
    standard_error: float | None = None
    lower_ci: float | None = None
    upper_ci: float | None = None
    crn_identity: str
    diagnostics: list[str] = Field(default_factory=list)


class MriResult2(BaseModel):
    """method-result 2.0 for MRI/rMRI (T18 write target)."""

    model_config = ConfigDict(extra="forbid")

    result_schema_version: Literal["2.0"] = "2.0"
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
    assessment_anchor: AssessmentAnchor
    anchor_risk_metric: RiskMetric | None = None
    anchor_risk_value: float | None = None
    anchor_calibration_method: str | None = None
    anchor_standard_source_id: str | None = None
    primary_risk_metric: RiskMetric
    mri_value: float | None = None
    mri_unit: str
    perfect_capacity_mri_value: float | None = None
    rmri_value: float | None = None
    rmri_status: RmriStatus = RmriStatus.NOT_APPLICABLE
    requested_difference_scheme: DifferenceScheme
    applied_difference_scheme: DifferenceScheme
    normalization_basis_mwh_per_mw: float | None = None
    equivalent_mri_per_mw: float | None = None
    base_step: float
    step_unit: _STEP_UNIT
    step_multipliers: list[float] = Field(default_factory=lambda: [0.5, 1.0, 2.0])
    step_evaluations: list[StepEvaluation] = Field(default_factory=list)
    perfect_capacity_step_evaluations: list[StepEvaluation] = Field(default_factory=list)
    reference_capacity_mw: float | None = None
    accredited_capacity_mw: float | None = None
    capacity_credit_fraction: float | None = None
    sample_count: int = Field(ge=0)
    scenario_bank_id: str | None = None
    result_status: ResultStatus
    risk_convergence: RiskConvergence
    search_convergence: SearchConvergence = SearchConvergence.NOT_APPLICABLE
    effective_data_grade: DataGrade
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_mri_rules(self) -> Self:
        if self.method not in (Method.MRI, Method.RMRI):
            raise ValueError("method-result 2.0 requires method=mri or method=rmri")
        if self.method is Method.MRI:
            if self.rmri_status is not RmriStatus.NOT_APPLICABLE:
                raise ValueError("run_type=mri requires rmri_status=not_applicable")
            if (
                self.rmri_value is not None
                or self.perfect_capacity_mri_value is not None
                or self.accredited_capacity_mw is not None
                or self.capacity_credit_fraction is not None
            ):
                raise ValueError(
                    "run_type=mri must not form rMRI, perfect-capacity MRI or capacity credit"
                )
        if self.method is Method.RMRI:
            if self.rmri_status is RmriStatus.STABLE and self.rmri_value is None:
                raise ValueError("rmri_status=stable requires rmri_value")
            if (
                self.rmri_status in (RmriStatus.UNSTABLE, RmriStatus.NOT_APPLICABLE)
                and self.rmri_value is not None
            ):
                raise ValueError("rmri_value must be null unless rmri_status=stable")
        # Capacity-credit derivation (§16A.7.1).
        if self.rmri_status is RmriStatus.STABLE and self.rmri_value is not None:
            if self.accredited_capacity_mw is not None:
                if self.reference_capacity_mw is None:
                    raise ValueError(
                        "accredited_capacity_mw requires reference_capacity_mw "
                        "(never inferred from the finite-difference step)"
                    )
                expected = self.rmri_value * self.reference_capacity_mw
                if abs(self.accredited_capacity_mw - expected) > 1e-9 * max(1.0, abs(expected)):
                    raise ValueError(
                        "accredited_capacity_mw must equal rmri_value x reference_capacity_mw"
                    )
            if (
                self.capacity_credit_fraction is not None
                and abs(self.capacity_credit_fraction - self.rmri_value) > 1e-12
            ):
                raise ValueError("capacity_credit_fraction must equal rmri_value when stable")
        if self.mri_value is not None and self.sample_count == 0:
            raise ValueError("a point mri_value requires a positive sample count")
        matrix_errors = validate_mri_status_matrix(self)
        if matrix_errors:
            raise ValueError(f"method-result 2.0 status matrix violation: {matrix_errors[0]}")
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> MriResult2:
        return cls.model_validate_json(text)


# Diagnostic codes frozen by §16A.6 (single source of truth for T18).
MRI_DIAGNOSTIC_CODES = frozenset(
    {
        "RMRI_NOT_REQUESTED",
        "RMRI_NORMALIZATION_MISSING",
        "RMRI_CUSTOM_NORMALIZATION_MISSING",
        "MRI_ASSESSMENT_ANCHOR_MISSING",
        "MRI_UNIT_MISMATCH",
        "MRI_SCENARIO_MISMATCH",
        "RMRI_DENOMINATOR_UNSTABLE",
        "MRI_STEP_SENSITIVE",
        "MRI_LOCAL_NON_MONOTONIC",
        "MRI_STRATEGY_SWITCH",
        "MRI_CRN_IDENTITY_MISMATCH",
        "MRI_PHYSICAL_DOMAIN_VIOLATION",
        "MRI_DIFFERENCE_SCHEME_FALLBACK",
        "MRI_INVALID_CONFIG",
        "MRI_RESOURCE_KIND_UNSUPPORTED",
    }
)


# Diagnostic severity registry (§16A.6 + §16A.9): a diagnostic's severity
# is mapped by code + result status here - never hard-coded to "info" at
# the publish boundary. An UNKNOWN code at the publish boundary is rejected
# (never silently defaulted to warning).
MRI_DIAGNOSTIC_SEVERITY: dict[str, Literal["info", "warning", "error"]] = {
    "RMRI_NOT_REQUESTED": "info",
    "RMRI_NORMALIZATION_MISSING": "warning",
    "RMRI_CUSTOM_NORMALIZATION_MISSING": "warning",
    "RMRI_DENOMINATOR_UNSTABLE": "warning",
    "MRI_STEP_SENSITIVE": "warning",
    "MRI_LOCAL_NON_MONOTONIC": "warning",
    "MRI_STRATEGY_SWITCH": "warning",
    "MRI_DIFFERENCE_SCHEME_FALLBACK": "warning",
    "MRI_ASSESSMENT_ANCHOR_MISSING": "error",
    "MRI_UNIT_MISMATCH": "error",
    "MRI_SCENARIO_MISMATCH": "error",
    "MRI_CRN_IDENTITY_MISMATCH": "error",
    "MRI_PHYSICAL_DOMAIN_VIOLATION": "error",
    "MRI_INVALID_CONFIG": "error",
    "MRI_RESOURCE_KIND_UNSUPPORTED": "error",
}


# Canonical method_error / data_error category classification used by the
# closed status matrix: invalid_method requires a method_error diagnostic,
# invalid_data requires a data_error diagnostic.
MRI_METHOD_ERROR_CODES = frozenset(
    {
        "MRI_INVALID_CONFIG",
        "MRI_RESOURCE_KIND_UNSUPPORTED",
        "MRI_ASSESSMENT_ANCHOR_MISSING",
        "MRI_PHYSICAL_DOMAIN_VIOLATION",
    }
)
MRI_DATA_ERROR_CODES = frozenset(
    {
        "MRI_SCENARIO_MISMATCH",
        "MRI_CRN_IDENTITY_MISMATCH",
        "MRI_UNIT_MISMATCH",
    }
)


# Stability-warning group: a stability warning claims the rMRI numerator /
# denominator is NOT trustworthy. When rmri_status=stable these are
# forbidden (they would invent an unstable state); when rmri_status=unstable
# at least one of them is REQUIRED.
MRI_STABILITY_WARNING_CODES = frozenset(
    {
        "RMRI_DENOMINATOR_UNSTABLE",
        "MRI_STEP_SENSITIVE",
        "MRI_LOCAL_NON_MONOTONIC",
        "MRI_STRATEGY_SWITCH",
    }
)
# Normalization-missing group: valid_with_warning + rmri_status=not_applicable
# requires exactly one of these (frozen normalization-missing scenarios).
MRI_NORMALIZATION_MISSING_CODES = frozenset(
    {
        "RMRI_NORMALIZATION_MISSING",
        "RMRI_CUSTOM_NORMALIZATION_MISSING",
    }
)

# Named diagnostic-code groups referenced by the combination policy.
MRI_DIAGNOSTIC_GROUPS: dict[str, frozenset[str]] = {
    "method_error": MRI_METHOD_ERROR_CODES,
    "data_error": MRI_DATA_ERROR_CODES,
    "stability_warning": MRI_STABILITY_WARNING_CODES,
    "normalization_missing": MRI_NORMALIZATION_MISSING_CODES,
}


class StatusDiagnosticPolicy(TypedDict):
    """Declarative per-status diagnostic policy (frozen 16A.9)."""

    required_warning: bool
    forbidden_severities: frozenset[str]
    required_category: frozenset[str]
    forbidden_category: frozenset[str]


# The frozen diagnostic status policy (16A.9): for each result_status,
# which diagnostic severities are forbidden, whether at least one registered
# warning is REQUIRED, and which canonical error categories are required /
# forbidden. This is the SINGLE declarative rule source consumed by BOTH
# ``validate_mri_status_matrix`` and ``schema_export`` (schema_export builds
# its conditionals FROM these constants - never a second hand-written
# matrix).
DIAGNOSTIC_POLICY_BY_STATUS: dict[ResultStatus, StatusDiagnosticPolicy] = {
    ResultStatus.VALID: {
        "required_warning": False,
        "forbidden_severities": frozenset({"warning", "error"}),
        "required_category": frozenset(),
        "forbidden_category": frozenset(),
    },
    ResultStatus.VALID_WITH_WARNING: {
        "required_warning": True,
        "forbidden_severities": frozenset({"error"}),
        "required_category": frozenset(),
        "forbidden_category": frozenset(),
    },
    ResultStatus.NOT_CONVERGED: {
        "required_warning": False,
        "forbidden_severities": frozenset({"error"}),
        "required_category": frozenset(),
        "forbidden_category": frozenset({"method_error", "data_error"}),
    },
    ResultStatus.LOW_RESOLUTION: {
        "required_warning": False,
        "forbidden_severities": frozenset({"error"}),
        "required_category": frozenset(),
        "forbidden_category": frozenset({"method_error", "data_error"}),
    },
    ResultStatus.INVALID_METHOD: {
        "required_warning": False,
        "forbidden_severities": frozenset(),
        "required_category": frozenset({"method_error"}),
        "forbidden_category": frozenset({"data_error"}),
    },
    ResultStatus.INVALID_DATA: {
        "required_warning": False,
        "forbidden_severities": frozenset(),
        "required_category": frozenset({"data_error"}),
        "forbidden_category": frozenset({"method_error"}),
    },
}


# The risk-convergence mapping (frozen 16A.9). This is ONLY the
# ``result_status -> risk_convergence`` mapping - it is NOT the full status
# matrix. The full diagnostic/severity/category matrix lives in
# ``DIAGNOSTIC_POLICY_BY_STATUS`` and both the Pydantic validator and the
# JSON Schema export consume that single source.
RISK_CONVERGENCE_BY_STATUS: dict[ResultStatus, RiskConvergence] = {
    ResultStatus.VALID: RiskConvergence.CONVERGED,
    ResultStatus.VALID_WITH_WARNING: RiskConvergence.CONVERGED,
    ResultStatus.NOT_CONVERGED: RiskConvergence.NOT_CONVERGED,
    ResultStatus.LOW_RESOLUTION: RiskConvergence.LOW_EVENT_COUNT,
    ResultStatus.INVALID_METHOD: RiskConvergence.NOT_CONVERGED,
    ResultStatus.INVALID_DATA: RiskConvergence.NOT_CONVERGED,
}

_POINT_ESTIMATE_FIELDS = (
    "mri_value",
    "perfect_capacity_mri_value",
    "rmri_value",
    "equivalent_mri_per_mw",
    "accredited_capacity_mw",
    "capacity_credit_fraction",
)

# Fields that must be FINITE whenever they are non-null on a published
# method-result 2.0 (the JSON Schema cannot express finiteness - a JSON
# document cannot contain NaN/Infinity, so the model is the authoritative
# finite check and the schema enforces the structural envelope).
_FINITE_WHEN_PUBLISHED_FIELDS = _POINT_ESTIMATE_FIELDS + ("reference_capacity_mw",)


class ResultCombinationPolicy(TypedDict):
    """Declarative per-(method, result_status, rmri_status) result policy
    (frozen 16A.9). Keys are the tuple ``(Method, ResultStatus, RmriStatus)``;
    values close the full method x result_status x rmri_status x diagnostics x
    required-fields x step-audit matrix. Consumed by BOTH
    ``validate_mri_status_matrix`` and ``schema_export``."""

    required_non_null_fields: frozenset[str]
    required_null_fields: frozenset[str]
    required_diagnostic_codes: frozenset[str]
    required_diagnostic_group: str | None
    forbidden_diagnostic_codes: frozenset[str]
    require_warning: bool
    require_step_evidence: bool
    require_perfect_capacity_step_evidence: bool
    perfect_capacity_evidence_if_present: bool


def _result_combo(
    *,
    required_non_null: frozenset[str] = frozenset(),
    required_null: frozenset[str] = frozenset(),
    required_codes: frozenset[str] = frozenset(),
    required_group: str | None = None,
    forbidden_codes: frozenset[str] = frozenset(),
    require_warning: bool = False,
    require_step_evidence: bool = False,
    require_perfect_step_evidence: bool = False,
    perfect_evidence_if_present: bool = False,
) -> ResultCombinationPolicy:
    """Small constructor so the matrix reads as a table, not nested dicts."""
    return {
        "required_non_null_fields": required_non_null,
        "required_null_fields": required_null,
        "required_diagnostic_codes": required_codes,
        "required_diagnostic_group": required_group,
        "forbidden_diagnostic_codes": forbidden_codes,
        "require_warning": require_warning,
        "require_step_evidence": require_step_evidence,
        "require_perfect_capacity_step_evidence": require_perfect_step_evidence,
        "perfect_capacity_evidence_if_present": perfect_evidence_if_present,
    }


_MRI_NA = RmriStatus.NOT_APPLICABLE
_MRI_POINT_NULL_ALL = frozenset(_POINT_ESTIMATE_FIELDS)
_MRI_RMRI_DERIVED_NULL = frozenset(
    {"rmri_value", "equivalent_mri_per_mw", "accredited_capacity_mw", "capacity_credit_fraction"}
)
_MRI_NOT_ENTERED_NULL = frozenset(
    {
        "mri_value",
        "rmri_value",
        "perfect_capacity_mri_value",
        "equivalent_mri_per_mw",
        "accredited_capacity_mw",
        "capacity_credit_fraction",
    }
)
# rmri entered the rMRI calculation (perfect-capacity denominator computed)
# but the shared convergence did NOT certify a point: no mri/rmri/derived
# point is published; perfect_capacity_mri_value evidence is kept.
_MRI_ENTERED_NOT_CONVERGED_NULL = frozenset(
    {
        "mri_value",
        "rmri_value",
        "equivalent_mri_per_mw",
        "accredited_capacity_mw",
        "capacity_credit_fraction",
    }
)


# The COMPLETE declarative method x result_status x rmri_status x diagnostics
# x required-fields x step-audit matrix (frozen 16A.9). This is the SINGLE
# rule source for the combination semantics; ``DIAGNOSTIC_POLICY_BY_STATUS``
# continues to own the generic severity/category rules and
# ``RISK_CONVERGENCE_BY_STATUS`` owns the risk-state mapping. A combination
# NOT present here is an unsupported/illegal method-result 2.0.
MRI_RESULT_POLICY_BY_COMBINATION: dict[
    tuple[Method, ResultStatus, RmriStatus], ResultCombinationPolicy
] = {
    # -------- method=mri (rmri_status is always not_applicable) --------
    (Method.MRI, ResultStatus.VALID, _MRI_NA): _result_combo(
        required_non_null=frozenset({"mri_value"}),
        required_null=_MRI_POINT_NULL_ALL - frozenset({"mri_value"}),
        required_codes=frozenset({"RMRI_NOT_REQUESTED"}),
        require_step_evidence=True,
    ),
    (Method.MRI, ResultStatus.VALID_WITH_WARNING, _MRI_NA): _result_combo(
        required_non_null=frozenset({"mri_value"}),
        required_null=_MRI_POINT_NULL_ALL - frozenset({"mri_value"}),
        required_codes=frozenset({"RMRI_NOT_REQUESTED"}),
        require_warning=True,
        require_step_evidence=True,
    ),
    (Method.MRI, ResultStatus.NOT_CONVERGED, _MRI_NA): _result_combo(
        required_null=_MRI_NOT_ENTERED_NULL,
    ),
    (Method.MRI, ResultStatus.LOW_RESOLUTION, _MRI_NA): _result_combo(
        required_null=_MRI_NOT_ENTERED_NULL,
    ),
    (Method.MRI, ResultStatus.INVALID_METHOD, _MRI_NA): _result_combo(
        required_null=_MRI_POINT_NULL_ALL,
    ),
    (Method.MRI, ResultStatus.INVALID_DATA, _MRI_NA): _result_combo(
        required_null=_MRI_POINT_NULL_ALL,
    ),
    # -------- method=rmri --------
    (Method.RMRI, ResultStatus.VALID, RmriStatus.STABLE): _result_combo(
        required_non_null=frozenset({"mri_value", "perfect_capacity_mri_value", "rmri_value"}),
        require_step_evidence=True,
        require_perfect_step_evidence=True,
    ),
    (Method.RMRI, ResultStatus.VALID_WITH_WARNING, RmriStatus.STABLE): _result_combo(
        required_non_null=frozenset({"mri_value", "perfect_capacity_mri_value", "rmri_value"}),
        forbidden_codes=MRI_STABILITY_WARNING_CODES,
        require_warning=True,
        require_step_evidence=True,
        require_perfect_step_evidence=True,
    ),
    (Method.RMRI, ResultStatus.VALID_WITH_WARNING, RmriStatus.UNSTABLE): _result_combo(
        required_non_null=frozenset({"mri_value", "perfect_capacity_mri_value"}),
        required_null=frozenset(
            {"rmri_value", "accredited_capacity_mw", "capacity_credit_fraction"}
        ),
        required_group="stability_warning",
        require_warning=True,
        require_step_evidence=True,
        require_perfect_step_evidence=True,
    ),
    (Method.RMRI, ResultStatus.VALID_WITH_WARNING, _MRI_NA): _result_combo(
        required_non_null=frozenset({"mri_value"}),
        required_null=_MRI_RMRI_DERIVED_NULL,
        required_group="normalization_missing",
        require_warning=True,
        require_step_evidence=True,
        perfect_evidence_if_present=True,
    ),
    (Method.RMRI, ResultStatus.NOT_CONVERGED, _MRI_NA): _result_combo(
        required_null=_MRI_NOT_ENTERED_NULL,
    ),
    (Method.RMRI, ResultStatus.NOT_CONVERGED, RmriStatus.UNSTABLE): _result_combo(
        required_null=_MRI_ENTERED_NOT_CONVERGED_NULL,
        perfect_evidence_if_present=True,
    ),
    (Method.RMRI, ResultStatus.LOW_RESOLUTION, _MRI_NA): _result_combo(
        required_null=_MRI_NOT_ENTERED_NULL,
    ),
    (Method.RMRI, ResultStatus.LOW_RESOLUTION, RmriStatus.UNSTABLE): _result_combo(
        required_null=_MRI_ENTERED_NOT_CONVERGED_NULL,
        perfect_evidence_if_present=True,
    ),
    (Method.RMRI, ResultStatus.INVALID_METHOD, _MRI_NA): _result_combo(
        required_null=_MRI_POINT_NULL_ALL,
    ),
    (Method.RMRI, ResultStatus.INVALID_DATA, _MRI_NA): _result_combo(
        required_null=_MRI_POINT_NULL_ALL,
    ),
}


def _diagnostic_category(code: str) -> str | None:
    """The canonical method_error/data_error category of a code (None for
    warnings / info codes)."""
    if code in MRI_METHOD_ERROR_CODES:
        return "method_error"
    if code in MRI_DATA_ERROR_CODES:
        return "data_error"
    return None


def _is_finite_real(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _check_step_audit(result: MriResult2, errors: list[str]) -> None:
    """Closed step / audit-evidence envelope for the valid-family results
    (sample_count / base_step / step_multipliers / step_evaluations /
    perfect-capacity step evaluations). invalid_method / invalid_data keep
    the zero-simulation path (sample_count=0, base_step=0, no evaluations);
    not_converged / low_resolution may keep partial evidence."""
    if result.sample_count <= 0:
        errors.append("valid-family results require sample_count > 0")
    if not _is_finite_real(result.base_step) or result.base_step <= 0.0:
        errors.append("valid-family results require a finite positive base_step")
    multipliers = result.step_multipliers
    if len(multipliers) < 3:
        errors.append("step_multipliers requires at least 3 entries")
    if 1.0 not in multipliers:
        errors.append("step_multipliers must include 1.0")
    if len(set(multipliers)) != len(multipliers):
        errors.append("step_multipliers must be unique")
    for multiplier in multipliers:
        if not _is_finite_real(multiplier) or multiplier <= 0.0:
            errors.append("step_multipliers entries must be finite positive numbers")
    if not result.step_evaluations:
        errors.append("valid-family results require non-empty step_evaluations")
        return
    covered: set[float] = set()
    for evaluation in result.step_evaluations:
        if (
            _is_finite_real(evaluation.step_multiplier)
            and evaluation.step_multiplier in multipliers
        ):
            covered.add(evaluation.step_multiplier)
        if not _is_finite_real(evaluation.scalar_step) or not _is_finite_real(
            evaluation.step_multiplier
        ):
            errors.append("step_evaluations step_multiplier/scalar_step must be finite")
            continue
        expected = evaluation.step_multiplier * result.base_step
        if abs(evaluation.scalar_step - expected) > 1e-9 * max(1.0, abs(expected)):
            errors.append(
                "step_evaluation scalar_step must equal step_multiplier x base_step "
                f"(multiplier={evaluation.step_multiplier}, scalar_step={evaluation.scalar_step}, "
                f"base_step={result.base_step})"
            )
    if covered != set(multipliers):
        missing = sorted(set(multipliers) - covered)
        errors.append(f"step_evaluations must cover every step_multiplier; missing {missing}")


def _check_perfect_capacity_step_audit(result: MriResult2, errors: list[str]) -> None:
    """The perfect-capacity denominator step audit must be complete whenever
    the denominator is published (stable/unstable rMRI or a computed
    perfect-capacity denominator on valid_with_warning/not_converged paths)."""
    if not result.perfect_capacity_step_evaluations:
        errors.append(
            "a published perfect-capacity MRI requires non-empty perfect_capacity_step_evaluations"
        )
        return
    multipliers = set(result.step_multipliers)
    covered: set[float] = set()
    for evaluation in result.perfect_capacity_step_evaluations:
        if (
            _is_finite_real(evaluation.step_multiplier)
            and evaluation.step_multiplier in multipliers
        ):
            covered.add(evaluation.step_multiplier)
    if covered != multipliers:
        missing = sorted(multipliers - covered)
        errors.append(
            f"perfect_capacity_step_evaluations must cover every step_multiplier; missing {missing}"
        )


def validate_mri_status_matrix(result: MriResult2) -> list[str]:
    """The CLOSED method-result 2.0 matrix: returns a list of violation
    messages (empty = valid). It is built from THREE single-source rule sets
    that ``schema_export`` mirrors from the SAME constants:

    - ``DIAGNOSTIC_POLICY_BY_STATUS``: generic severity / category rules;
    - ``MRI_RESULT_POLICY_BY_COMBINATION``: the full
      ``method x result_status x rmri_status x diagnostics x required-fields x
      step-audit`` matrix (any combination NOT in the policy is rejected);
    - ``RISK_CONVERGENCE_BY_STATUS``: the risk-state mapping.

    It also freezes the diagnostics (registered codes, exact severity) and
    the step / audit evidence for valid-family results."""
    errors: list[str] = []
    status = result.result_status
    method = result.method
    rmri_status = result.rmri_status

    for diagnostic in result.diagnostics:
        if diagnostic.code not in MRI_DIAGNOSTIC_CODES:
            errors.append(
                f"diagnostic code {diagnostic.code!r} is not a registered MRI diagnostic code"
            )
        expected_severity = MRI_DIAGNOSTIC_SEVERITY.get(diagnostic.code)
        if expected_severity is not None and diagnostic.severity.value != expected_severity:
            errors.append(
                f"diagnostic {diagnostic.code!r} severity {diagnostic.severity.value} "
                f"must equal the registered severity {expected_severity}"
            )

    # ---- generic per-status severity / category rules (single source) ----
    policy = DIAGNOSTIC_POLICY_BY_STATUS[status]
    forbidden_severities = policy["forbidden_severities"]
    required_warning = policy["required_warning"]
    required_category = policy["required_category"]
    forbidden_category = policy["forbidden_category"]
    for diagnostic in result.diagnostics:
        if diagnostic.severity.value in forbidden_severities:
            errors.append(
                f"result_status={status.value} must not carry "
                f"{diagnostic.severity.value} diagnostics"
            )
        category = _diagnostic_category(diagnostic.code)
        if category in forbidden_category:
            errors.append(
                f"result_status={status.value} must not carry {category} diagnostic "
                f"{diagnostic.code!r} - a {category} root cause must form "
                "invalid_method/invalid_data, not stay in the current status"
            )
    if required_warning and not any(
        diagnostic.severity.value == "warning" for diagnostic in result.diagnostics
    ):
        errors.append(
            "valid_with_warning requires at least one registered warning diagnostic "
            "(an info-only diagnostic such as RMRI_NOT_REQUESTED cannot promote to "
            "valid_with_warning)"
        )
    for category in required_category:
        if not any(
            _diagnostic_category(diagnostic.code) == category for diagnostic in result.diagnostics
        ):
            errors.append(
                f"result_status={status.value} requires a registered {category} diagnostic "
                "(MRI_METHOD_ERROR_CODES / MRI_DATA_ERROR_CODES); an arbitrary "
                "severity=error code cannot substitute"
            )

    # ---- full combination matrix (single source) ----
    combo = MRI_RESULT_POLICY_BY_COMBINATION.get((method, status, rmri_status))
    if combo is None:
        errors.append(
            f"unsupported method-result 2.0 combination: "
            f"method={method.value}, result_status={status.value}, "
            f"rmri_status={rmri_status.value}"
        )
        return errors
    # Explicit-null result contract: a result document must EXPLICITLY carry
    # every combination-required field (a missing field is NOT a compliant
    # document, even when Pydantic could default it to None). The presence
    # set comes from Pydantic ``model_fields_set`` - never a second hand-
    # written field table.
    provided = set(result.model_fields_set)

    def _require_present(field: str, reason: str) -> None:
        if field not in provided:
            errors.append(f"{field} must be explicitly present in the result document ({reason})")

    for field in combo["required_non_null_fields"]:
        _require_present(field, "required non-null")
        if getattr(result, field) is None:
            errors.append(
                f"{field} must be non-null for "
                f"(method={method.value}, result_status={status.value}, "
                f"rmri_status={rmri_status.value})"
            )
    for field in combo["required_null_fields"]:
        _require_present(field, "required null - field omission is not a compliant result")
        if getattr(result, field) is not None:
            errors.append(f"{field} must be null for result_status={status.value}")
    for code in combo["required_diagnostic_codes"]:
        if not any(diagnostic.code == code for diagnostic in result.diagnostics):
            errors.append(f"result_status={status.value} requires diagnostic {code!r}")
    if combo["required_diagnostic_group"] is not None:
        group = MRI_DIAGNOSTIC_GROUPS[combo["required_diagnostic_group"]]
        if not any(diagnostic.code in group for diagnostic in result.diagnostics):
            errors.append(
                f"result_status={status.value} requires at least one "
                f"{combo['required_diagnostic_group']} diagnostic {sorted(group)}"
            )
    for code in combo["forbidden_diagnostic_codes"]:
        if any(diagnostic.code == code for diagnostic in result.diagnostics):
            errors.append(
                f"result_status={status.value} must not carry diagnostic {code!r} "
                f"under rmri_status={rmri_status.value}"
            )
    # Diagnostics must be explicitly present whenever the combination (or
    # the generic severity/category policy) requires specific diagnostics.
    diagnostics_required = bool(
        combo["required_diagnostic_codes"]
        or combo["required_diagnostic_group"]
        or combo["require_warning"]
        or policy["required_category"]
    )
    if diagnostics_required:
        _require_present("diagnostics", "required diagnostics")

    # ---- global method rules ----
    if method is Method.RMRI and any(
        diagnostic.code == "RMRI_NOT_REQUESTED" for diagnostic in result.diagnostics
    ):
        errors.append("method=rmri must not carry RMRI_NOT_REQUESTED")

    # ---- finite published values (JSON documents cannot express NaN/Inf) ----
    for field in _FINITE_WHEN_PUBLISHED_FIELDS:
        value = getattr(result, field)
        if value is not None and not _is_finite_real(value):
            errors.append(f"{field} must be a finite number when non-null")

    # ---- step / audit evidence closure ----
    if combo["require_step_evidence"]:
        for field in ("sample_count", "base_step", "step_multipliers", "step_evaluations"):
            _require_present(field, "valid-family step / audit evidence")
        _check_step_audit(result, errors)
    if combo["require_perfect_capacity_step_evidence"]:
        _require_present("perfect_capacity_step_evaluations", "perfect-capacity step evidence")
        _check_perfect_capacity_step_audit(result, errors)
    elif (
        combo["perfect_capacity_evidence_if_present"]
        and "perfect_capacity_mri_value" in provided
        and result.perfect_capacity_mri_value is not None
    ):
        _require_present(
            "perfect_capacity_step_evaluations",
            "perfect-capacity evidence when a perfect-capacity MRI is published",
        )
        _check_perfect_capacity_step_audit(result, errors)

    # ---- risk-state mapping (single source) ----
    expected = RISK_CONVERGENCE_BY_STATUS.get(status)
    if expected is not None and result.risk_convergence is not expected:
        errors.append(
            f"risk_convergence={result.risk_convergence.value} is inconsistent with "
            f"result_status={status.value} (expected {expected.value})"
        )
    return errors


def read_method_result_version(payload: dict[str, Any]) -> str:
    """Version dispatch for method-result documents (1.3 vs 2.0)."""
    version = payload.get("result_schema_version")
    if version in ("1.3", "2.0"):
        return str(version)
    raise ValueError(f"unsupported result_schema_version {version!r}")


def read_run_config_version(payload: dict[str, Any]) -> str:
    """Version dispatch for run-config documents (1.0 vs 1.1)."""
    version = payload.get("run_config_schema_version")
    if version in ("1.0", "1.1"):
        return str(version)
    raise ValueError(f"unsupported run_config_schema_version {version!r}")


__all__ = [
    "DIAGNOSTIC_POLICY_BY_STATUS",
    "MRI_DATA_ERROR_CODES",
    "MRI_DIAGNOSTIC_CODES",
    "MRI_DIAGNOSTIC_GROUPS",
    "MRI_DIAGNOSTIC_SEVERITY",
    "MRI_METHOD_ERROR_CODES",
    "MRI_NORMALIZATION_MISSING_CODES",
    "MRI_RESULT_POLICY_BY_COMBINATION",
    "MRI_STABILITY_WARNING_CODES",
    "RISK_CONVERGENCE_BY_STATUS",
    "CrnConfig",
    "CustomPerturbationComponent",
    "CustomPerturbationVector",
    "EngineeringTolerance",
    "MriMethodConfig",
    "MriResult2",
    "PerturbationVector",
    "PhysicalDomain",
    "RunConfig11",
    "StabilityThresholds",
    "StepEvaluation",
    "read_method_result_version",
    "read_run_config_version",
    "validate_mri_status_matrix",
]
