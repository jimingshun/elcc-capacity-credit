"""Run configuration and run manifest contracts (application service
contract V0.2 §3--§4)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capacity_credit.contracts.efc_method_config import EfcMethodConfig
from capacity_credit.contracts.elcc_method_config import ElccMethodConfig
from capacity_credit.contracts.enums import (
    AllocationType,
    AssessmentAnchor,
    CapacityValueType,
    ChronologyMode,
    LifecycleStatus,
    PerturbationDimension,
    RiskMetric,
    RunMode,
    RunType,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.storage_soc_config import StorageSocConfig

_SHA256_PATTERN = r"[0-9a-fA-F]{64}"

_MRI_RUN_TYPES = frozenset({RunType.MRI, RunType.RMRI})


def _ensure_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("must be timezone-aware ISO 8601")
    return v


def _check_chronology(chronology: ChronologyMode, soc_boundary: SocYearBoundaryMode) -> None:
    """Chronology / SOC-boundary consistency (independent years never
    carry over; continuous multiyear always carries over)."""
    if (
        chronology is ChronologyMode.CONTINUOUS_MULTIYEAR
        and soc_boundary is not SocYearBoundaryMode.CARRY_OVER
    ):
        raise ValueError(
            "soc_year_boundary_mode must be carry_over when chronology_mode is continuous_multiyear"
        )
    if (
        chronology is ChronologyMode.INDEPENDENT_WEATHER_YEARS
        and soc_boundary is SocYearBoundaryMode.CARRY_OVER
    ):
        raise ValueError(
            "soc_year_boundary_mode=carry_over requires chronology_mode=continuous_multiyear"
        )


def _check_storage_soc_consistency(
    storage_mode: StorageSocInitializationMode,
    chronology: ChronologyMode,
    soc_boundary: SocYearBoundaryMode,
    storage_soc: StorageSocConfig | None,
) -> None:
    """T14 formal config chain: the top-level storage SOC mode and the
    typed ``storage_soc`` section must agree at BOTH the application
    config and the formal RunConfig layers.

    - not_applicable -> storage_soc must be null;
    - weather_cycle_warmup / stochastic_sample_burnin -> storage_soc
      required and its initialization_mode must equal the top-level
      mode;
    - storage runs with independent_weather_years require
      reinitialize_with_configured_mode (continuous_multiyear already
      requires carry_over via ``_check_chronology``); an applicable
      storage mode never pairs with a not_applicable year boundary."""
    has_storage = storage_soc is not None
    if storage_mode is StorageSocInitializationMode.NOT_APPLICABLE:
        if has_storage:
            raise ValueError(
                "storage_soc must be null when storage_soc_initialization_mode is not_applicable"
            )
        return
    if storage_soc is None:
        raise ValueError(
            f"storage_soc is required when storage_soc_initialization_mode is {storage_mode.value}"
        )
    if storage_soc.initialization_mode is not storage_mode:
        raise ValueError(
            "storage_soc.initialization_mode must equal the top-level "
            "storage_soc_initialization_mode"
        )
    if soc_boundary is SocYearBoundaryMode.NOT_APPLICABLE:
        raise ValueError("a storage run requires an applicable soc_year_boundary_mode")
    if (
        chronology is ChronologyMode.INDEPENDENT_WEATHER_YEARS
        and soc_boundary is not SocYearBoundaryMode.REINITIALIZE_WITH_CONFIGURED_MODE
    ):
        raise ValueError(
            "storage runs with independent_weather_years require "
            "soc_year_boundary_mode=reinitialize_with_configured_mode"
        )


def _check_mri_fields(
    run_type: RunType,
    assessment_anchor: AssessmentAnchor | None,
    perturbation_dimension: PerturbationDimension | None,
) -> None:
    """MRI/rMRI require an anchor and a perturbation dimension; other
    run types must leave both null."""
    if run_type in _MRI_RUN_TYPES:
        if assessment_anchor is None:
            raise ValueError("assessment_anchor is required for run_type=mri/rmri")
        if perturbation_dimension is None:
            raise ValueError("perturbation_dimension is required for run_type=mri/rmri")
    else:
        if assessment_anchor is not None:
            raise ValueError("assessment_anchor must be null for non-MRI run types")
        if perturbation_dimension is not None:
            raise ValueError("perturbation_dimension must be null for non-MRI run types")


def _check_capacity_value(
    capacity_value_type: CapacityValueType,
    baseline_portfolio_version: str | None,
) -> None:
    """Marginal capacity value requires a baseline portfolio version."""
    if capacity_value_type is CapacityValueType.MARGINAL and baseline_portfolio_version is None:
        raise ValueError("baseline_portfolio_version is required for capacity_value_type=marginal")


def _check_allocation(
    allocation_type: AllocationType,
    allocation_rule_id: str | None,
) -> None:
    """Governance-rule allocation requires a rule id."""
    if allocation_type is AllocationType.GOVERNANCE_RULE and allocation_rule_id is None:
        raise ValueError("allocation_rule_id is required for allocation_type=governance_rule")


def _check_method_config_usage(
    run_type: RunType,
    method_config: ElccMethodConfig | EfcMethodConfig | None,
) -> None:
    """FORMAL RunConfig contract (round 3, extended for EFC in T17):
    run_type=elcc REQUIRES a complete typed ELCC method config and
    run_type=efc REQUIRES a complete typed EFC method config; any other
    run type must carry method_config=null. The model, from_yaml,
    from_json and the JSON Schema reach the same conclusion on every
    payload."""
    if run_type is RunType.ELCC:
        if not isinstance(method_config, ElccMethodConfig):
            raise ValueError(
                "method_config is required for run_type=elcc as an "
                "ElccMethodConfig (formal frozen RunConfig)"
            )
        return
    if run_type is RunType.EFC:
        if not isinstance(method_config, EfcMethodConfig):
            raise ValueError(
                "method_config is required for run_type=efc as an "
                "EfcMethodConfig (formal frozen RunConfig)"
            )
        return
    if method_config is not None:
        raise ValueError("method_config is only valid for run_type=elcc/efc")


def load_run_config(payload: dict[str, Any]) -> Any:
    """Version dispatch for frozen run-config documents: returns the
    1.0 ``RunConfig``, the 1.1 ``RunConfig11`` or the 1.2 ``RunConfig12``
    model. Each version reader is ``extra=forbid``: a 1.0/1.1 reader
    never accepts a newer document, and 1.2 never relaxes the old
    validations (V1-T3-A)."""
    from capacity_credit.contracts.mri_contracts import RunConfig11

    version = payload.get("run_config_schema_version")
    if version == "1.1":
        return RunConfig11.model_validate(payload)
    if version == "1.2":
        return RunConfig12.model_validate(payload)
    return RunConfig.model_validate(payload)


class RunConfig(BaseModel):
    """Frozen run configuration (``run_config.yaml``)."""

    model_config = ConfigDict(extra="forbid")

    run_config_schema_version: Literal["1.0"]
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
    assessment_anchor: AssessmentAnchor | None = None
    perturbation_dimension: PerturbationDimension | None = None
    normalization_basis_mwh_per_mw: float | None = None
    capacity_value_type: CapacityValueType
    baseline_portfolio_version: str | None = None
    allocation_type: AllocationType
    allocation_rule_id: str | None = None
    random_seed: int
    scenario_bank_id: str | None = None
    scenario_id: str | None = None
    method_config: ElccMethodConfig | EfcMethodConfig | None = None

    @model_validator(mode="after")
    def _validate_conditional_fields(self) -> Self:
        _check_chronology(self.chronology_mode, self.soc_year_boundary_mode)
        if self.run_type is RunType.REPORT and self.source_run_id is None:
            raise ValueError("source_run_id is required for run_type=report")
        _check_mri_fields(self.run_type, self.assessment_anchor, self.perturbation_dimension)
        _check_capacity_value(self.capacity_value_type, self.baseline_portfolio_version)
        _check_allocation(self.allocation_type, self.allocation_rule_id)
        _check_method_config_usage(self.run_type, self.method_config)
        _check_storage_soc_consistency(
            self.storage_soc_initialization_mode,
            self.chronology_mode,
            self.soc_year_boundary_mode,
            self.method_config.storage_soc if self.method_config else None,
        )
        return self

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> RunConfig:
        import yaml

        return cls.model_validate(yaml.safe_load(text))

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> RunConfig:
        return cls.model_validate_json(text)


class ConvergenceConfig12(BaseModel):
    """Structured convergence settings for run-config 1.2 (V1-T3-A): the
    field semantics are the shared ``ConvergenceConfig`` evidence gate
    (engine layer), serialized here as the typed frozen-config section.
    ``primary_risk_metric`` deliberately defaults to null = the OUTER
    run-config metric; an explicit value must equal it (one authority,
    never two movable copies)."""

    model_config = ConfigDict(extra="forbid")

    convergence_config_schema_version: Literal["1.0"] = "1.0"
    batch_size: int
    min_samples: int
    max_samples: int
    confidence_level: float
    relative_half_width_tolerance: float
    absolute_half_width_tolerance: float
    near_zero_threshold: float
    min_nonzero_samples: int
    recent_batch_window: int
    recent_batch_stability_tolerance: float
    use_student_t: bool = True
    normal_interval_min_samples: int = 200
    primary_risk_metric: str | None = None
    provisional_defaults: bool = True


class AdequacyConfig12(BaseModel):
    """Structured adequacy EXECUTION settings for run-config 1.2
    (V1-T3-A, D1 §6.3): the field semantics reuse the resource-run
    configuration (application layer ``ResourceRunConfig``) and the
    typed ``StorageSocConfig``. Facts the OUTER run-config already owns
    are deliberately NOT duplicated (scenario bank, root seed, weather
    years / weights) - a duplicate reference must agree or be refused,
    and the absence of a second copy is the strongest agreement.
    Independent adequacy never exposes search parameters:
    ``perfect_capacity_mw`` is fixed to 0."""

    model_config = ConfigDict(extra="forbid")

    adequacy_config_schema_version: Literal["1.0"] = "1.0"
    samples: int
    dispatch_strategy: str = "eue_greedy"
    risk_window_length_hours: int = 24
    risk_threshold: float = 0.05
    reserve_soc_fraction: float = 0.0
    intertie_availability_policy: str = "zero"
    emergency_limit_source: str | None = None
    hydro_adjustable_request_mw: float = 0.0
    auto_schedule_planned_outages: bool = False
    merge_events_across_years: bool = False
    # parity with the resource-layer DEFAULT_DELTA_T_HOURS (asserted by
    # test, single source of semantics stays the resource domain)
    delta_t_hours: float = 1.0
    perfect_capacity_mw: float = 0.0
    scenario_id: str | None = None
    storage_soc: StorageSocConfig | None = None
    convergence: ConvergenceConfig12

    @model_validator(mode="after")
    def _validate(self) -> Self:
        import math

        if self.perfect_capacity_mw != 0.0:
            raise ValueError(
                "adequacy_config.perfect_capacity_mw must be 0: independent "
                "adequacy never exposes a perfect-capacity parameter"
            )
        for name, value in (
            ("samples", self.samples),
            ("risk_window_length_hours", self.risk_window_length_hours),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (
            ("risk_threshold", self.risk_threshold),
            ("delta_t_hours", self.delta_t_hours),
            ("reserve_soc_fraction", self.reserve_soc_fraction),
            ("hydro_adjustable_request_mw", self.hydro_adjustable_request_mw),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(
                float(value)
            ):
                raise ValueError(f"{name} must be a finite number")
        if not 0.0 < self.risk_threshold < 1.0:
            raise ValueError("risk_threshold must lie in (0, 1)")
        if self.delta_t_hours <= 0.0:
            raise ValueError("delta_t_hours must be positive")
        if self.reserve_soc_fraction < 0.0:
            raise ValueError("reserve_soc_fraction must be non-negative")
        if self.hydro_adjustable_request_mw < 0.0:
            raise ValueError("hydro_adjustable_request_mw must be non-negative")
        if self.storage_soc is not None and self.scenario_id is not None:
            # a storage section and a scenario selection are independent;
            # this guard only documents that both are optional - no-op
            pass
        return self


class RunConfig12(BaseModel):
    """Formal run-config 1.2 (V1-T3, D1 §6.3): adequacy-only, adding the
    structured ``adequacy_config`` execution section. The 1.0 field
    semantics are unchanged (no in-place widening); the manifest stays
    1.0 and its ``run_config_hash`` covers this FULL document. The
    capacity-method lineage hashes stay null for adequacy - the
    adequacy_config travels inside the hashed run config itself."""

    model_config = ConfigDict(extra="forbid")

    run_config_schema_version: Literal["1.2"] = "1.2"
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
    assessment_anchor: AssessmentAnchor | None = None
    perturbation_dimension: PerturbationDimension | None = None
    normalization_basis_mwh_per_mw: float | None = None
    capacity_value_type: CapacityValueType
    baseline_portfolio_version: str | None = None
    allocation_type: AllocationType
    allocation_rule_id: str | None = None
    random_seed: int
    scenario_bank_id: str
    scenario_id: str | None = None
    adequacy_config: AdequacyConfig12

    @model_validator(mode="after")
    def _validate_conditional_fields(self) -> Self:
        if self.run_type is not RunType.ADEQUACY:
            raise ValueError(
                "run-config 1.2 is adequacy-only; other run types use 1.0/1.1"
            )
        if self.capacity_value_type is not CapacityValueType.NOT_APPLICABLE:
            raise ValueError(
                "run-config 1.2 (adequacy) requires capacity_value_type=not_applicable"
            )
        _check_chronology(self.chronology_mode, self.soc_year_boundary_mode)
        _check_mri_fields(self.run_type, self.assessment_anchor, self.perturbation_dimension)
        _check_storage_soc_consistency(
            self.storage_soc_initialization_mode,
            self.chronology_mode,
            self.soc_year_boundary_mode,
            self.adequacy_config.storage_soc,
        )
        # single authoritative scenario fact: the OUTER value wins; an
        # inner duplicate must agree, never diverge
        if (
            self.adequacy_config.scenario_id is not None
            and self.scenario_id is not None
            and self.adequacy_config.scenario_id != self.scenario_id
        ):
            raise ValueError(
                "adequacy_config.scenario_id must equal the outer scenario_id "
                "(single authoritative scenario source)"
            )
        if (
            self.adequacy_config.convergence.primary_risk_metric is not None
            and self.adequacy_config.convergence.primary_risk_metric
            != self.primary_risk_metric.value
        ):
            raise ValueError(
                "adequacy_config.convergence.primary_risk_metric must equal the "
                "outer primary_risk_metric (single authoritative metric source)"
            )
        return self

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> RunConfig12:
        import yaml

        return cls.model_validate(yaml.safe_load(text))

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> RunConfig12:
        return cls.model_validate_json(text)


def _check_manifest_method_config_hash(run_type: RunType, method_config_hash: str | None) -> None:
    """ELCC/EFC runs must carry the full-config-lineage hashes in the
    manifest (schema-level condition, not only a RunService gate):
    ``method_config_hash`` (normalized method config only) and
    ``application_config_hash`` (full normalized application config)."""
    if run_type in (RunType.ELCC, RunType.EFC) and method_config_hash is None:
        raise ValueError(
            "method_config_hash is required for run_type=elcc/efc (full configuration lineage)"
        )


def _check_manifest_application_config_hash(
    run_type: RunType, application_config_hash: str | None
) -> None:
    if run_type in (RunType.ELCC, RunType.EFC) and application_config_hash is None:
        raise ValueError(
            "application_config_hash is required for run_type=elcc/efc "
            "(frozen application-config lineage)"
        )


def _check_manifest_non_elcc_hashes_null(
    run_type: RunType,
    method_config_hash: str | None,
    application_config_hash: str | None,
) -> None:
    """Round-4 bidirectional condition: non-ELCC/non-EFC runs must
    carry BOTH lineage hashes as null (no misleading lineage), mirroring
    the JSON Schema conditional. Step 2 Stage A exception:
    demand_target_capacity carries application_config_hash only (its
    typed config freezes separately as target_capacity_config.json;
    method_config_hash stays null)."""
    if run_type is RunType.DEMAND_TARGET_CAPACITY:
        if method_config_hash is not None:
            raise ValueError(
                "method_config_hash must be null for "
                "run_type=demand_target_capacity (application_config_hash only)"
            )
        return
    if run_type not in (RunType.ELCC, RunType.EFC) and (
        method_config_hash is not None or application_config_hash is not None
    ):
        raise ValueError(
            "method_config_hash / application_config_hash must be null "
            "for run_type not in {elcc, efc}"
        )


class RunManifest(BaseModel):
    """Immutable run manifest (application service contract V0.2 §4.1)."""

    model_config = ConfigDict(extra="forbid")

    run_schema_version: Literal["1.0"]
    run_id: uuid.UUID
    run_type: RunType
    run_mode: RunMode
    lifecycle_status: LifecycleStatus
    dataset_id: str
    data_version_id: str
    data_version_hash: str = Field(pattern=_SHA256_PATTERN)
    import_batch_id: uuid.UUID
    run_config_path: str
    run_config_hash: str = Field(pattern=_SHA256_PATTERN)
    scenario_bank_id: str | None = None
    source_run_id: uuid.UUID | None = None
    parent_run_ids: list[uuid.UUID] = Field(default_factory=list)
    software_version: str
    software_commit: str
    method_profile: str
    capacity_value_type: CapacityValueType
    baseline_portfolio_version: str | None = None
    allocation_type: AllocationType
    allocation_rule_id: str | None = None
    created_by: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_id: uuid.UUID | None = None
    artifact_manifest_ids: list[str] = Field(default_factory=list)
    method_config_hash: str | None = None
    application_config_hash: str | None = None

    @field_validator("method_config_hash", "application_config_hash")
    @classmethod
    def _hash_pattern(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.fullmatch(_SHA256_PATTERN, v):
            raise ValueError("config lineage hashes must be SHA-256 hex digests")
        return v

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def _timezone_aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        return _ensure_aware(v)

    @model_validator(mode="after")
    def _validate_conditional_fields(self) -> Self:
        if self.run_type is RunType.REPORT and self.source_run_id is None:
            raise ValueError("source_run_id is required for run_type=report")
        if (
            self.run_type not in (RunType.VALIDATE, RunType.REPORT)
            and self.scenario_bank_id is None
        ):
            raise ValueError("scenario_bank_id is required for simulation run types")
        _check_capacity_value(self.capacity_value_type, self.baseline_portfolio_version)
        _check_allocation(self.allocation_type, self.allocation_rule_id)
        _check_manifest_method_config_hash(self.run_type, self.method_config_hash)
        _check_manifest_application_config_hash(self.run_type, self.application_config_hash)
        _check_manifest_non_elcc_hashes_null(
            self.run_type,
            self.method_config_hash,
            self.application_config_hash,
        )
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> RunManifest:
        return cls.model_validate_json(text)
