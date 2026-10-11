"""Strict, typed EFC method configuration (T17).

Neutral contract module shared by ``contracts.runs`` (the frozen
RunConfig.method_config for run_type=efc). The EFC method config REUSES
the ELCC/EFC shared structures (``EfcMethodSearchConfig`` from
``elcc_method_config``, the shared candidate identity
``ElccMethodCandidateConfig`` and the shared convergence
``ElccMethodConvergenceConfig``) and adds ONLY the EFC-specific
sections: the perfect-capacity definition and the search upper bound.

It deliberately carries NO ELCC load-growth section: EFC never carries
an unused ELCC field, and the normalized dump of ``EfcMethodConfig`` is
the single source of the EFC ``method_config_hash``.

Perfect capacity contract (methodology §9.2): hourly availability = 1,
no planned maintenance / forced outages, no energy / fuel / SOC /
duration limits, weather-independent, responds to deficits without
exceeding the hourly deficit and without negative unserved energy.

Upper-bound sources (methodology §9.4):

- ``storage_max_discharge_power``: storage candidates never exceed the
  maximum discharge power;
- ``nameplate_capacity``: wind/solar candidates never exceed installed
  capacity;
- ``max_instantaneous_output_sum``: candidate combinations never exceed
  the sum of the maximum instantaneous outputs;
- ``declared_mw``: an explicit configured bound (requires
  ``declared_mw``).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from capacity_credit.contracts.elcc_method_config import (
    METHOD_CONFIG_SCHEMA_VERSION,
    EfcMethodSearchConfig,
    ElccMethodCandidateConfig,
    ElccMethodConvergenceConfig,
)
from capacity_credit.contracts.storage_soc_config import StorageSocConfig

_UPPER_BOUND_SOURCES = frozenset(
    {
        "storage_max_discharge_power",
        "nameplate_capacity",
        "max_instantaneous_output_sum",
        "declared_mw",
    }
)


def _finite_number(value: object, field: str, *, non_negative: bool = False) -> float:
    from capacity_credit.contracts.elcc_method_config import _finite_number as _impl

    return _impl(value, field, non_negative=non_negative)


def _trim_non_blank(value: object, field: str) -> str:
    from capacity_credit.contracts.elcc_method_config import _trim_non_blank as _impl

    return _impl(value, field)


class EfcMethodPerfectCapacityConfig(BaseModel):
    """The frozen perfect-capacity definition used by the S+F trials."""

    model_config = ConfigDict(extra="forbid")

    perfect_capacity_schema_version: Literal["1.0"] = "1.0"
    definition_version: str = "perfect_v1"

    @field_validator("definition_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        return _trim_non_blank(value, "definition_version")


class EfcMethodUpperBoundConfig(BaseModel):
    """Declared physical upper bound for the EFC search variable F."""

    model_config = ConfigDict(extra="forbid")

    source: str
    declared_mw: float | None = None

    @field_validator("source", mode="before")
    @classmethod
    def _source(cls, value: object) -> object:
        source = _trim_non_blank(value, "source")
        if source not in _UPPER_BOUND_SOURCES:
            raise ValueError(f"upper_bound.source must be one of {sorted(_UPPER_BOUND_SOURCES)}")
        return source

    @field_validator("declared_mw", mode="before")
    @classmethod
    def _declared(cls, value: object) -> object:
        if value is None:
            return None
        number = _finite_number(value, "declared_mw")
        if number <= 0.0:
            raise ValueError("declared_mw must be positive when set")
        return number

    @model_validator(mode="after")
    def _validate_source(self) -> Self:
        if self.source == "declared_mw" and self.declared_mw is None:
            raise ValueError("upper_bound.source=declared_mw requires declared_mw")
        if self.source != "declared_mw" and self.declared_mw is not None:
            raise ValueError("declared_mw is only valid for upper_bound.source=declared_mw")
        return self


class EfcCriterionWindowConfig(BaseModel):
    """MR-01.1: the winter-evening-peak criterion window (provisional
    defaults; 正式身份前由基准测试锁定). Months/hours are interpreted on
    the run's LOCAL time axis. A window that selects nothing is refused
    at run time (METRICS_WINDOW_EMPTY)."""

    model_config = ConfigDict(extra="forbid")

    criterion_window_schema_version: Literal["1.0"] = "1.0"
    winter_months: list[int] = Field(default_factory=lambda: [11, 12, 1, 2, 3])
    evening_hours: list[int] = Field(default_factory=lambda: [17, 18, 19, 20, 21])
    provisional_status: Literal["provisional"] = "provisional"

    @field_validator("winter_months", "evening_hours", mode="before")
    @classmethod
    def _int_lists(cls, value: object, info: Any) -> object:
        if not isinstance(value, list) or not value:
            raise ValueError(f"{info.field_name} must be a non-empty list of ints")
        normalized: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"{info.field_name} entries must be ints")
            normalized.append(item)
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{info.field_name} must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def _validate_ranges(self) -> Self:
        if any(m < 1 or m > 12 for m in self.winter_months):
            raise ValueError("winter_months must be within 1..12")
        if any(h < 0 or h > 23 for h in self.evening_hours):
            raise ValueError("evening_hours must be within 0..23")
        return self


class EfcCriterionConfig(BaseModel):
    """MR-01.1: which calibre the EFC search judges risk in.

    ``annual`` (default) = the legacy full-year metrics, bit-identical
    behavior. ``winter_evening_peak`` = the SAME metrics computed on the
    window slice only (never a second metric formula)."""

    model_config = ConfigDict(extra="forbid")

    criterion_config_schema_version: Literal["1.0"] = "1.0"
    mode: Literal["annual", "winter_evening_peak"] = "annual"
    window: EfcCriterionWindowConfig | None = None


TECHNOLOGY_CLASSES = frozenset(
    {
        "pv",
        "wind_onshore",
        "wind_offshore",
        "storage",
        "thermal",
        "hydro",
        "demand_response",
        "other",
    }
)


class EfcReportingConfig(BaseModel):
    """MR-01.2/01.3: the de-rating-table identity of one EFC run - the
    technology class (with its human attestation, 方法说明第七章 类别
    归属认定) and the delivery year the factor is valid for (降额系数
    只对所属交付年有效, 跨交付年不可直接沿用)."""

    model_config = ConfigDict(extra="forbid")

    reporting_config_schema_version: Literal["1.0"] = "1.0"
    technology_class: str
    class_attested_by: str = ""
    class_attestation_basis: str = ""
    delivery_year: int | None = None
    auction_round: Literal["T4", "T1", "none"] = "none"

    @field_validator("technology_class", mode="before")
    @classmethod
    def _class_enum(cls, value: object) -> object:
        trimmed = str(value or "").strip()
        if trimmed not in TECHNOLOGY_CLASSES:
            raise ValueError(
                f"technology_class must be one of {sorted(TECHNOLOGY_CLASSES)}, got {value!r}"
            )
        return trimmed


class EfcMethodConfig(BaseModel):
    """Typed, versioned EFC method configuration embedded in the frozen
    RunConfig (full-config lineage; no ``Any`` fields).

    Shares the search (decreasing direction), candidate identity and
    convergence structures with ELCC; carries the EFC-only perfect
    capacity definition and the search upper bound. No ELCC load-growth
    section.
    """

    model_config = ConfigDict(extra="forbid")

    method_config_schema_version: Literal["1.0"] = METHOD_CONFIG_SCHEMA_VERSION
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
    # MR-01: criterion calibre + de-rating-table identity (lineage-bound)
    criterion: EfcCriterionConfig = Field(default_factory=EfcCriterionConfig)
    reporting: EfcReportingConfig | None = None
    # F1: emergency-layer override for the on/off sensitivity axis
    # (auto = table-driven, disabled = layer off on the SAME frozen
    # data - single-leaf sensitivity mutation, lineage-bound)
    emergency_layer: Literal["auto", "disabled"] = "auto"

    @model_validator(mode="after")
    def _validate_confidence_consistency(self) -> Self:
        # identical rule to ELCC (round 3): the frozen methodology
        # defines no separate equal-risk search confidence level
        if self.search.confidence_level != self.convergence.confidence_level:
            raise ValueError(
                "search.confidence_level must equal convergence.confidence_level "
                "(methodology §12.3/§12.4/§12.5 define no separate levels)"
            )
        return self

    def upper_bound_mw(self) -> float:
        """Resolve the configured upper bound to a MW value. For
        resource-derived sources the application layer supplies the
        declared value via ``declared_mw`` at run preparation (the
        method config keeps the source; the workflow binds the value
        from the frozen candidate definition)."""
        if self.upper_bound.declared_mw is not None:
            return float(self.upper_bound.declared_mw)
        raise ValueError(
            "upper_bound has no resolved MW value; the workflow must bind "
            f"the value for source={self.upper_bound.source!r}"
        )


__all__ = [
    "EfcMethodConfig",
    "EfcMethodPerfectCapacityConfig",
    "EfcMethodUpperBoundConfig",
]
