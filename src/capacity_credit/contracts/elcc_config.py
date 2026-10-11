"""ELCC application configuration (T12 remediation, schema version 1.0).

Versioned typed application config consumed by ``elcc-tool run elcc
--config <path>``. The CLI only parses this strict config and calls the
application workflow - it never implements business logic.

Round-3 separation (remediation 2): the application INPUT phase embeds
a DRAFT run-level type (``ElccRunSettings``) that never carries a
``method_config``; the FORMAL frozen ``RunConfig`` (persisted as
run_config.yaml, hashed, idempotency-keyed) REQUIRES a complete typed
``method_config`` for run_type=elcc - the model, from_yaml, from_json
and the generated JSON Schema all agree on the same payloads. No
pydantic validation context is used to make read and persist paths
behave differently.

Boundary rules (remediation):

- every numeric field rejects bool / NaN / Inf and enforces its
  positivity / non-negativity / range constraint;
- the ELCC lower capacity bound is FIXED at 0 MW (non-zero lower
  bounds are rejected unless a versioned methodology definition says
  otherwise);
- ``monotonic_direction`` is restricted to the T11-valid directions and
  the ELCC adapter requires ``risk_increases_with_capacity``;
- the candidate section selects exactly one resource_group_id OR an
  explicit, normalized (trimmed / non-blank / deduplicated /
  stable-sorted) member id list; the workflow resolves it against the
  frozen renewable data before any run side effect;
- the convergence section is a typed, versioned contract (provisional
  teaching defaults are explicit and marked, never hidden);
- ``search.confidence_level`` and ``convergence.confidence_level`` MUST
  BE EQUAL (round 3): the frozen methodology (§12.3/§12.4 vs §12.5)
  defines no separate levels; a divergence requires a methodology
  revision and re-review, not a code-side extension;
- ``load_growth.specified_profile`` is a strict versioned
  ``LoadGrowthProfileConfig`` validated at CONFIG parse time (round 3);
- the full normalized config is frozen, persisted and hashed before
  computation; the frozen effective config carries the RESOLVED
  candidate nominal capacity (from the frozen data, never
  ``max(per_year)``); the full-application hash and the
  method-config-only hash are separate fields (see RunManifest).

The method sections are defined in ``contracts.elcc_method_config``
(neutral module shared with ``contracts.runs``); this module re-exports
them so existing imports keep working.
"""

from __future__ import annotations

import json
import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capacity_credit.contracts.elcc_method_config import (
    CalibratedBaselineRef,
    ElccMethodCandidateConfig,
    ElccMethodConfig,
    ElccMethodConvergenceConfig,
    ElccMethodLoadGrowthConfig,
    ElccMethodSearchConfig,
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
    _check_storage_soc_consistency,
)
from capacity_credit.contracts.storage_soc_config import StorageSocConfig

# Backward-compatible aliases for the first-round public names.
CandidateConfig = ElccMethodCandidateConfig
ElccConvergenceConfig = ElccMethodConvergenceConfig
ElccSearchConfig = ElccMethodSearchConfig
LoadGrowthConfig = ElccMethodLoadGrowthConfig


class ElccRunSettings(BaseModel):
    """DRAFT run-level settings of the ELCC application input config.

    NOT a persistence contract: it never carries ``method_config`` and
    must not be written as run_config.yaml. ``to_run_config()`` builds
    the FORMAL frozen ``RunConfig`` (which requires a complete typed
    method_config for run_type=elcc). Keeping the draft separate from
    the formal type guarantees that the model, from_yaml / from_json
    and the JSON Schema reach identical conclusions on the same payload.
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
    def _validate_elcc_draft(self) -> Self:
        if self.run_type is not RunType.ELCC:
            raise ValueError("run_type must be 'elcc' for an ELCC application config")
        if self.capacity_value_type is CapacityValueType.NOT_APPLICABLE:
            raise ValueError("capacity_value_type must be applicable for ELCC")
        # SHARED conditional rules with the formal RunConfig (single
        # source in contracts.runs - never a second copy): chronology /
        # SOC boundary, non-MRI null anchors, marginal baseline,
        # governance allocation rule.
        _check_chronology(self.chronology_mode, self.soc_year_boundary_mode)
        _check_mri_fields(
            self.run_type,
            self.assessment_anchor,
            self.perturbation_dimension,
        )
        _check_capacity_value(self.capacity_value_type, self.baseline_portfolio_version)
        _check_allocation(self.allocation_type, self.allocation_rule_id)
        return self


class ElccApplicationConfig(BaseModel):
    """Versioned ELCC application configuration (schema 1.0)."""

    model_config = ConfigDict(extra="forbid")

    elcc_config_schema_version: Literal["1.0"] = "1.0"
    run_config: ElccRunSettings
    search: ElccMethodSearchConfig
    load_growth: ElccMethodLoadGrowthConfig
    candidate: ElccMethodCandidateConfig
    convergence: ElccMethodConvergenceConfig = Field(
        default_factory=ElccMethodConvergenceConfig,
    )
    # T14: controlled storage SOC initialization (warm-up / burn-in /
    # explicit initial SOC); MUST agree with the top-level
    # storage_soc_initialization_mode of run_config and is passed into
    # ElccMethodConfig.storage_soc by to_run_config (entering
    # run_config.yaml, run_config_hash and the JSON Schema).
    storage_soc: StorageSocConfig | None = None
    # MR-02: optional calibrated-baseline closure (absent = not calibrated)
    calibrated_baseline_ref: CalibratedBaselineRef | None = None

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
        _check_storage_soc_consistency(
            self.run_config.storage_soc_initialization_mode,
            self.run_config.chronology_mode,
            self.run_config.soc_year_boundary_mode,
            self.storage_soc,
        )
        return self

    @classmethod
    def from_yaml(cls, text: str) -> ElccApplicationConfig:
        import yaml

        payload = yaml.safe_load(text)
        if not isinstance(payload, dict):
            raise TypeError("ELCC config must be a mapping")
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
    def from_json(cls, text: str) -> ElccApplicationConfig:
        return cls.model_validate_json(text)

    def to_run_config(self) -> RunConfig:
        """Build the FORMAL frozen RunConfig with the ELCC method config
        embedded (typed, versioned); the formal type REQUIRES the
        method_config for run_type=elcc, so the embedded config always
        passes validation. The normalized dump is frozen and hashed
        before computation."""
        method_config = ElccMethodConfig(
            method_config_schema_version="1.0",
            search=self.search,
            load_growth=self.load_growth,
            candidate=self.candidate,
            convergence=self.convergence,
            storage_soc=self.storage_soc,
            calibrated_baseline_ref=self.calibrated_baseline_ref,
        )
        return RunConfig.model_validate(
            {
                **self.run_config.model_dump(mode="json"),
                "method_config": method_config.model_dump(mode="json"),
            }
        )

    def normalized_payload(self) -> str:
        """Stable canonical JSON payload of the full application config
        (single source of ``application_config_hash``)."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
        )


__all__ = [
    "CandidateConfig",
    "ElccApplicationConfig",
    "ElccConvergenceConfig",
    "ElccMethodCandidateConfig",
    "ElccMethodConfig",
    "ElccMethodConvergenceConfig",
    "ElccMethodLoadGrowthConfig",
    "ElccMethodSearchConfig",
    "ElccRunSettings",
    "ElccSearchConfig",
    "LoadGrowthConfig",
    "StorageSocConfig",
]
