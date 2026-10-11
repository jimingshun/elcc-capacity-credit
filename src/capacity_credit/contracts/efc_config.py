"""EFC application configuration contract (T17).

The DRAFT application input (``EfcRunSettings`` + method sections) is
kept separate from the FORMAL frozen ``RunConfig``: ``to_run_config()``
builds the formal RunConfig with the typed ``EfcMethodConfig`` embedded
(which the formal type REQUIRES for run_type=efc). The model,
from_yaml / from_json and the generated JSON Schema reach identical
conclusions on the same payload.
"""

from __future__ import annotations

import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capacity_credit.contracts.efc_method_config import (
    EfcCriterionConfig,
    EfcMethodConfig,
    EfcMethodPerfectCapacityConfig,
    EfcMethodUpperBoundConfig,
    EfcReportingConfig,
)
from capacity_credit.contracts.elcc_method_config import (
    EfcMethodSearchConfig,
    ElccMethodCandidateConfig,
    ElccMethodConvergenceConfig,
)
from capacity_credit.contracts.enums import (
    AllocationType,
    AssessmentAnchor,
    CapacityValueType,
    ChronologyMode,
    PerturbationDimension,
    RiskMetric,
    RunMode,
    RunType,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.runs import (
    RunConfig,
    _check_allocation,
    _check_capacity_value,
    _check_chronology,
    _check_mri_fields,
)
from capacity_credit.contracts.storage_soc_config import StorageSocConfig


class EfcRunSettings(BaseModel):
    """DRAFT run-level settings of the EFC application input config.

    NOT a persistence contract: it never carries ``method_config`` and
    must not be written as run_config.yaml. ``to_run_config()`` builds
    the FORMAL frozen ``RunConfig`` (which requires a complete typed
    method_config for run_type=efc).
    """

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

    @model_validator(mode="after")
    def _validate_efc_draft(self) -> Self:
        if self.run_type is not RunType.EFC:
            raise ValueError("run_type must be 'efc' for an EFC application config")
        if self.capacity_value_type is CapacityValueType.NOT_APPLICABLE:
            raise ValueError("capacity_value_type must be applicable for EFC")
        _check_chronology(self.chronology_mode, self.soc_year_boundary_mode)
        _check_mri_fields(
            self.run_type,
            self.assessment_anchor,
            self.perturbation_dimension,
        )
        _check_capacity_value(self.capacity_value_type, self.baseline_portfolio_version)
        _check_allocation(self.allocation_type, self.allocation_rule_id)
        return self


class EfcApplicationConfig(BaseModel):
    """Versioned EFC application configuration (schema 1.0).

    Shares search (decreasing direction), candidate identity and
    convergence with ELCC; carries the EFC-only perfect-capacity
    definition and the search upper bound. No ELCC load-growth section.
    """

    model_config = ConfigDict(extra="forbid")

    efc_config_schema_version: Literal["1.0"] = "1.0"
    run_config: EfcRunSettings
    search: EfcMethodSearchConfig
    candidate: ElccMethodCandidateConfig
    convergence: ElccMethodConvergenceConfig = Field(
        default_factory=ElccMethodConvergenceConfig,
    )
    perfect_capacity: EfcMethodPerfectCapacityConfig = Field(
        default_factory=EfcMethodPerfectCapacityConfig,
    )
    upper_bound: EfcMethodUpperBoundConfig
    storage_soc: StorageSocConfig | None = None
    # MR-01: criterion calibre (annual default = legacy bit-identical
    # behavior) and the de-rating-table identity (class + delivery year)
    criterion: EfcCriterionConfig = Field(default_factory=EfcCriterionConfig)
    reporting: EfcReportingConfig | None = None
    # F1: sensitivity on/off axis (auto = table-driven; disabled = off)
    emergency_layer: Literal["auto", "disabled"] = "auto"

    @model_validator(mode="after")
    def _validate_confidences_equal(self) -> Self:
        if abs(self.search.confidence_level - self.convergence.confidence_level) > 1e-12:
            raise ValueError(
                "search.confidence_level must equal "
                "convergence.confidence_level (the frozen methodology "
                "defines no separate levels)"
            )
        return self

    @model_validator(mode="after")
    def _validate_storage_soc_chain(self) -> Self:
        _check_storage_soc_consistency_chain(self)
        return self

    @classmethod
    def from_yaml(cls, text: str) -> EfcApplicationConfig:
        import yaml

        payload = yaml.safe_load(text)
        if not isinstance(payload, dict):
            raise TypeError("EFC config must be a mapping")
        return cls.model_validate(payload)

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(
            self.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        )

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> EfcApplicationConfig:
        return cls.model_validate_json(text)

    def to_run_config(self) -> RunConfig:
        """Build the FORMAL frozen RunConfig with the EFC method config
        embedded (typed, versioned); the formal type REQUIRES the
        method_config for run_type=efc, so the embedded config always
        passes validation."""
        method_config = EfcMethodConfig(
            method_config_schema_version="1.0",
            search=self.search,
            candidate=self.candidate,
            convergence=self.convergence,
            perfect_capacity=self.perfect_capacity,
            upper_bound=self.upper_bound,
            storage_soc=self.storage_soc,
            criterion=self.criterion,
            reporting=self.reporting,
            emergency_layer=self.emergency_layer,
        )
        return RunConfig.model_validate(
            {
                **self.run_config.model_dump(mode="json"),
                "method_config": method_config.model_dump(mode="json"),
            }
        )


def _check_storage_soc_consistency_chain(app_config: EfcApplicationConfig) -> None:
    from capacity_credit.contracts.runs import _check_storage_soc_consistency

    _check_storage_soc_consistency(
        app_config.run_config.storage_soc_initialization_mode,
        app_config.run_config.chronology_mode,
        app_config.run_config.soc_year_boundary_mode,
        app_config.storage_soc,
    )


__all__ = [
    "EfcApplicationConfig",
    "EfcRunSettings",
]
