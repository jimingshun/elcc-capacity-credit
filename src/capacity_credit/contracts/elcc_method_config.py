"""Strict, typed ELCC method configuration (T12 round-2/3 remediation).

Neutral contract module shared by ``contracts.runs`` (the frozen
RunConfig.method_config) and ``contracts.elcc_config`` (the application
config sections): no ``Any``, no unvalidated payloads, no circular
imports. The four method sections (search / load_growth / candidate /
convergence) are strict Pydantic models:

- every numeric field rejects bool / NaN / Inf;
- integer fields are StrictInt (bool is never coerced to an int);
- boolean fields are StrictBool (0/1 are never coerced to a bool);
- ``candidate`` requires exactly ONE selection form (single
  ``resource_group_id`` or an explicit member id list) and normalizes
  member ids: trim, reject blanks, deduplicate, then stable-sort;
- ``convergence`` is a typed, versioned contract whose teaching
  defaults are explicit and marked provisional (never hidden);
- ``load_growth.specified_profile`` is a strict, versioned
  ``LoadGrowthProfileConfig`` (round 3): every field is typed, extra
  fields are forbidden, and the FULL structural validation (axis
  year-set equality, 1-hour spacing, duplicate timestamps, shape
  lengths, basis/status literals, scale) is performed at CONFIG parse
  time by reusing the single domain authority
  ``LoadGrowthProfile.from_dict`` - never deferred to the workflow;
- ``search.confidence_level`` (equal-risk significance tests, §12.5)
  and ``convergence.confidence_level`` (risk-estimate convergence CI,
  §12.3/§12.4) MUST BE EQUAL (round 3): the frozen methodology defines
  no separate levels; any divergence requires a methodology revision
  and re-review, not a code-side extension.

Schema versioning: every instance carries ``method_config_schema_version
= "1.0"``; the normalized dump of ``ElccMethodConfig`` is the single
source of ``method_config_hash`` (see RunService).
"""

from __future__ import annotations

import math
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from capacity_credit.contracts.storage_soc_config import (
    StorageSocConfig,
)
from capacity_credit.domain.portfolios import LoadGrowthProfile

# F4/C5: the PRODUCTION convergence profile (provisional - benchmark
# lock required before a formal identity). Formal runs must NOT carry
# the untouched teaching defaults: they either set these values
# explicitly (profile opt-in) or declare genuinely custom values.
PRODUCTION_CONVERGENCE_PROFILE: dict[str, Any] = {
    "batch_size": 100,
    "min_samples": 2000,
    "max_samples": 100000,
    "confidence_level": 0.95,
    "relative_half_width_tolerance": 0.05,
    "absolute_half_width_tolerance": 0.001,
    "near_zero_threshold": 0.05,
    "min_nonzero_samples": 100,
    "recent_batch_window": 5,
    "recent_batch_stability_tolerance": 0.1,
    "use_student_t": True,
    "normal_interval_min_samples": 200,
    "provisional_defaults": True,
}


def is_teaching_default_convergence(section: Any) -> bool:
    """F4/C5: True when the convergence section equals the untouched
    teaching defaults (pydantic fills defaults, so 'user left it' and
    'user re-typed the same values' are indistinguishable - refusing
    both for FORMAL runs is the intended behavior: a formal run must
    declare real convergence intent)."""
    return section == ElccMethodConvergenceConfig()


METHOD_CONFIG_SCHEMA_VERSION: Literal["1.0"] = "1.0"

_T11_DIRECTIONS = frozenset(
    {
        "risk_increases_with_capacity",
        "risk_decreases_with_capacity",
    }
)

_GROWTH_MODES = frozenset({"fixed_mw", "scaling", "specified"})

_ELCC_METRIC_VALUES = frozenset(
    {
        "lole_days_per_year",
        "lolh_hours_per_year",
        "eue_mwh_per_year",
        "neue_ppm",
    }
)


def _finite_number(
    value: object,
    field: str,
    *,
    non_negative: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # pydantic converts ValueError from a field_validator into a
        # ValidationError; TypeError would escape the contract boundary
        raise ValueError(f"{field} must be a real number")  # noqa: TRY004
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if non_negative and number < 0.0:
        raise ValueError(f"{field} must be non-negative")
    return number


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a strict integer")  # noqa: TRY004
    return value


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a strict boolean")  # noqa: TRY004
    return value


def _trim_non_blank(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")  # noqa: TRY004
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be blank")
    return stripped


class EqualRiskSearchMethodConfig(BaseModel):
    """SHARED equal-risk search parameters (MW) for ELCC and EFC
    (strict types). The monotonic direction and the lower-bound rule are
    enforced by the ELCC/EFC subclasses - never by this base."""

    model_config = ConfigDict(extra="forbid")

    lower_bound_mw: float = 0.0
    physical_upper_bound_mw: float
    capacity_tolerance_mw: float
    max_iterations: int
    monotonic_direction: str = "risk_increases_with_capacity"
    allow_upper_bound_expansion: bool = False
    max_expanded_upper_bound_mw: float | None = None
    confidence_level: float = 0.95

    @field_validator(
        "lower_bound_mw",
        "physical_upper_bound_mw",
        "capacity_tolerance_mw",
        "confidence_level",
        mode="before",
    )
    @classmethod
    def _numeric(cls, value: object, info: Any) -> object:
        field = info.field_name or "value"
        number = _finite_number(value, field, non_negative=True)
        if field == "physical_upper_bound_mw" and number <= 0.0:
            raise ValueError("physical_upper_bound_mw must be positive")
        if field == "capacity_tolerance_mw" and number <= 0.0:
            raise ValueError("capacity_tolerance_mw must be positive")
        if field == "confidence_level" and not 0.0 < number < 1.0:
            raise ValueError("confidence_level must be in (0, 1)")
        return number

    @field_validator("max_iterations", mode="before")
    @classmethod
    def _iterations(cls, value: object) -> object:
        iterations = _strict_int(value, "max_iterations")
        if iterations <= 0:
            raise ValueError("max_iterations must be a positive integer")
        return iterations

    @field_validator("allow_upper_bound_expansion", mode="before")
    @classmethod
    def _expansion_flag(cls, value: object) -> object:
        return _strict_bool(value, "allow_upper_bound_expansion")

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.lower_bound_mw < 0.0:
            raise ValueError("lower_bound_mw must be non-negative")
        if self.physical_upper_bound_mw <= self.lower_bound_mw:
            raise ValueError("physical_upper_bound_mw must exceed lower_bound_mw")
        if self.allow_upper_bound_expansion:
            if self.max_expanded_upper_bound_mw is None:
                raise ValueError(
                    "allow_upper_bound_expansion=True requires max_expanded_upper_bound_mw"
                )
            expanded = _finite_number(
                self.max_expanded_upper_bound_mw,
                "max_expanded_upper_bound_mw",
                non_negative=True,
            )
            if expanded <= self.physical_upper_bound_mw:
                raise ValueError("max_expanded_upper_bound_mw must exceed the physical upper bound")
            object.__setattr__(self, "max_expanded_upper_bound_mw", expanded)
        else:
            if self.max_expanded_upper_bound_mw is not None:
                raise ValueError(
                    "max_expanded_upper_bound_mw requires allow_upper_bound_expansion=True"
                )
        return self


class ElccMethodSearchConfig(EqualRiskSearchMethodConfig):
    """ELCC search: risk increases with load growth; the lower bound is
    fixed at 0 MW (load-growth search). Never reused by EFC."""

    @field_validator("monotonic_direction", mode="before")
    @classmethod
    def _direction(cls, value: object) -> object:
        direction = _trim_non_blank(value, "monotonic_direction")
        if direction not in _T11_DIRECTIONS:
            raise ValueError(f"monotonic_direction must be one of {sorted(_T11_DIRECTIONS)}")
        if direction != "risk_increases_with_capacity":
            raise ValueError(
                "ELCC requires monotonic_direction="
                "risk_increases_with_capacity (growing load raises risk)"
            )
        return direction

    @model_validator(mode="after")
    def _elcc_lower_bound(self) -> Self:
        if self.lower_bound_mw != 0.0:
            raise ValueError("lower_bound_mw must be 0 MW for ELCC (load-growth search)")
        return self


class EfcMethodSearchConfig(EqualRiskSearchMethodConfig):
    """EFC search: risk DECREASES with the perfect capacity F; the
    lower bound is fixed at 0 MW (F starts at zero). The ELCC
    increasing-load branch is never reused."""

    @field_validator("monotonic_direction", mode="before")
    @classmethod
    def _direction(cls, value: object) -> object:
        direction = _trim_non_blank(value, "monotonic_direction")
        if direction not in _T11_DIRECTIONS:
            raise ValueError(f"monotonic_direction must be one of {sorted(_T11_DIRECTIONS)}")
        if direction != "risk_decreases_with_capacity":
            raise ValueError(
                "EFC requires monotonic_direction=risk_decreases_with_capacity "
                "(the perfect capacity F lowers the risk)"
            )
        return direction

    @model_validator(mode="after")
    def _efc_lower_bound(self) -> Self:
        if self.lower_bound_mw != 0.0:
            raise ValueError("lower_bound_mw must be 0 MW for EFC (F starts at zero)")
        return self


def _parse_strict_year_key(value: object, field: str) -> int:
    """A dict key must be an exact integer literal (str(int) == text)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} year keys must be exact integer literals")  # noqa: TRY004
    text = str(value).strip()
    try:
        year = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} year keys must be exact integer literals") from exc
    if str(year) != text:
        raise ValueError(f"{field} year keys must be exact integer literals")
    return year


class LoadGrowthTimestampsConfig(BaseModel):
    """One weather-year timestamp axis (mirrors the domain profile
    payload: timezone name + ISO stamps)."""

    model_config = ConfigDict(extra="forbid")

    timezone: str
    stamps: list[str]

    @field_validator("timezone", mode="before")
    @classmethod
    def _timezone(cls, value: object) -> object:
        return _trim_non_blank(value, "timezone")

    @field_validator("stamps", mode="before")
    @classmethod
    def _stamps(cls, value: object) -> object:
        if not isinstance(value, list) or not value:
            raise ValueError("stamps must be a non-empty list of ISO timestamp strings")
        for stamp in value:
            if not isinstance(stamp, str) or not stamp.strip():
                raise ValueError("stamps must be non-blank ISO timestamp strings")
        return list(value)


class LoadGrowthProfileConfig(BaseModel):
    """Strict, versioned specified-growth profile contract (round 3).

    The payload mirrors the domain ``LoadGrowthProfile.from_dict``
    document exactly (the seven fields, extra=forbid); the FULL
    structural validation - year-set equality, 1-hour strictly
    increasing axis, duplicate timestamps, shape lengths, basis/status
    literals, finite non-negative scale - is executed at CONFIG parse
    time by the single domain authority, so config, RunConfig /
    ElccMethodConfig and the generated JSON Schema all express the same
    structure and the workflow never receives a structurally invalid
    profile.
    """

    model_config = ConfigDict(extra="forbid")

    profile_schema_version: Literal["1.0"] = "1.0"
    load_growth_profile_id: str
    source_id: str
    data_status: Literal["modelled", "assumed", "derived"]
    basis: Literal["growth_multiplier", "growth_addition_mw"]
    scale: float
    shape_by_year: dict[str, list[float]]
    timestamps_by_year: dict[str, LoadGrowthTimestampsConfig]

    _profile: LoadGrowthProfile = PrivateAttr()

    @field_validator("load_growth_profile_id", "source_id", mode="before")
    @classmethod
    def _identifiers(cls, value: object, info: Any) -> object:
        return _trim_non_blank(value, info.field_name or "identifier")

    @field_validator("scale", mode="before")
    @classmethod
    def _scale(cls, value: object) -> object:
        return _finite_number(value, "scale", non_negative=True)

    @field_validator("shape_by_year", mode="before")
    @classmethod
    def _shapes(cls, value: object) -> object:
        if not isinstance(value, dict) or not value:
            raise ValueError("shape_by_year must be a non-empty mapping of year -> shape values")
        for raw_year, shape in value.items():
            _parse_strict_year_key(raw_year, "shape_by_year")
            if not isinstance(shape, list) or not shape:
                raise ValueError(f"shape_by_year[{raw_year}] must be a non-empty list")
            for item in shape:
                _finite_number(item, f"shape_by_year[{raw_year}]")
        return value

    @field_validator("timestamps_by_year", mode="before")
    @classmethod
    def _timestamp_years(cls, value: object) -> object:
        if not isinstance(value, dict) or not value:
            raise ValueError("timestamps_by_year must be a non-empty mapping of year -> axis")
        for raw_year in value:
            _parse_strict_year_key(raw_year, "timestamps_by_year")
        return value

    @model_validator(mode="after")
    def _validate_via_domain(self) -> Self:
        """Execute the single domain authority at CONFIG parse time:
        any structural violation (duplicate timestamps, 1-hour spacing,
        year mismatches, basis/status, shape lengths, scale) raises a
        ValidationError here - never deferred to the workflow."""
        try:
            profile = LoadGrowthProfile.from_dict(self.to_domain_payload())
        except Exception as exc:
            raise ValueError(f"invalid specified growth profile: {exc}") from exc
        object.__setattr__(self, "_profile", profile)
        return self

    def to_domain_payload(self) -> dict[str, Any]:
        return {
            "load_growth_profile_id": self.load_growth_profile_id,
            "source_id": self.source_id,
            "data_status": self.data_status,
            "basis": self.basis,
            "scale": self.scale,
            "shape_by_year": self.shape_by_year,
            "timestamps_by_year": {
                str(year): stamps.model_dump(mode="json")
                for year, stamps in self.timestamps_by_year.items()
            },
        }

    def to_profile(self) -> LoadGrowthProfile:
        """The already-validated domain profile (config-parse time
        validation; the workflow only re-checks BUSINESS consistency
        against the frozen load axis)."""
        return self._profile


class ElccMethodLoadGrowthConfig(BaseModel):
    """Load growth mode and parameters (methodology V0.4 §8.2)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fixed_mw", "scaling", "specified"]
    specified_profile: LoadGrowthProfileConfig | None = None

    @model_validator(mode="after")
    def _validate_profile_requirement(self) -> Self:
        if self.mode == "specified" and self.specified_profile is None:
            raise ValueError("mode=specified requires a specified_profile payload")
        if self.mode != "specified" and self.specified_profile is not None:
            raise ValueError("specified_profile is only valid for mode=specified")
        return self


class ElccMethodCandidateConfig(BaseModel):
    """Candidate resource identity and capacity.

    Exactly ONE selection form is required: a single
    ``resource_group_id`` or an explicit member id list
    (``resource_group_ids``). Member ids are normalized: trimmed,
    non-blank, deduplicated, then stable-sorted. The workflow resolves
    the selection against the frozen renewable data; unknown /
    duplicate / ambiguous / incomplete selections are blocked before
    any run side effect.
    """

    model_config = ConfigDict(extra="forbid")

    resource_or_portfolio_id: str
    resource_group_id: str | None = None
    resource_group_ids: list[str] | None = None
    reference_capacity_mw: float | None = None

    @field_validator("resource_or_portfolio_id", mode="before")
    @classmethod
    def _portfolio_id(cls, value: object) -> object:
        return _trim_non_blank(value, "resource_or_portfolio_id")

    @field_validator("resource_group_id", mode="before")
    @classmethod
    def _single_group(cls, value: object) -> object:
        if value is None:
            return None
        return _trim_non_blank(value, "resource_group_id")

    @field_validator("resource_group_ids", mode="before")
    @classmethod
    def _member_ids(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError(  # noqa: TRY004
                "resource_group_ids must be a list"
            )
        normalized: list[str] = []
        for item in value:
            if item is None or not isinstance(item, str) or not item.strip():
                raise ValueError("resource_group_ids entries must be non-blank strings")
            normalized.append(item.strip())
        if not normalized:
            raise ValueError("resource_group_ids must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("resource_group_ids must not contain duplicates")
        return sorted(set(normalized))

    @field_validator("reference_capacity_mw", mode="before")
    @classmethod
    def _reference_numeric(cls, value: object) -> object:
        if value is None:
            return None
        number = _finite_number(value, "reference_capacity_mw")
        if number <= 0.0:
            raise ValueError("reference_capacity_mw must be positive when set")
        return number

    @model_validator(mode="after")
    def _validate_selection(self) -> Self:
        single = self.resource_group_id
        multi = self.resource_group_ids
        if single is not None and multi is not None:
            raise ValueError(
                "exactly one of resource_group_id / resource_group_ids must be set, not both"
            )
        if single is None and multi is None:
            raise ValueError("exactly one of resource_group_id / resource_group_ids is required")
        return self

    def selected_group_ids(self) -> list[str]:
        if self.resource_group_id is not None:
            return [self.resource_group_id]
        assert self.resource_group_ids is not None
        return list(self.resource_group_ids)


class ElccMethodConvergenceConfig(BaseModel):
    """Engine convergence parameters (typed, versioned).

    The defaults are PROVISIONAL teaching thresholds so the ELCC chain
    converges on small synthetic banks - they are explicitly marked and
    never hidden; they are NOT policy or industry thresholds.
    ``confidence_level`` is the RISK-ESTIMATE convergence confidence
    (methodology §12.3/§12.4: the 95% interval half-width criterion).
    The frozen methodology defines NO separate equal-risk search
    confidence level, so ``ElccMethodSearchConfig.confidence_level``
    must equal this value (enforced by ElccMethodConfig); any
    divergence requires a methodology revision and re-review.
    """

    model_config = ConfigDict(extra="forbid")

    convergence_config_schema_version: Literal["1.0"] = "1.0"
    batch_size: int = 10
    min_samples: int = 6
    max_samples: int = 1000
    confidence_level: float = 0.95
    relative_half_width_tolerance: float = 0.5
    absolute_half_width_tolerance: float = 0.001
    near_zero_threshold: float = 0.05
    min_nonzero_samples: int = 3
    recent_batch_window: int = 3
    recent_batch_stability_tolerance: float = 1.0
    use_student_t: bool = True
    normal_interval_min_samples: int = 200
    provisional_defaults: bool = True
    teaching_note: str = "provisional teaching thresholds for synthetic banks; not policy"

    @field_validator(
        "confidence_level",
        "relative_half_width_tolerance",
        "absolute_half_width_tolerance",
        "near_zero_threshold",
        "recent_batch_stability_tolerance",
        mode="before",
    )
    @classmethod
    def _numeric(cls, value: object, info: Any) -> object:
        field = info.field_name or "value"
        number = _finite_number(value, field, non_negative=True)
        if field == "confidence_level" and not 0.0 < number < 1.0:
            raise ValueError("confidence_level must be in (0, 1)")
        return number

    @field_validator(
        "batch_size",
        "min_samples",
        "max_samples",
        "min_nonzero_samples",
        "recent_batch_window",
        "normal_interval_min_samples",
        mode="before",
    )
    @classmethod
    def _positive_int(cls, value: object, info: Any) -> object:
        field = info.field_name or "value"
        number = _strict_int(value, field)
        if number <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return number

    @field_validator(
        "use_student_t",
        "provisional_defaults",
        mode="before",
    )
    @classmethod
    def _bool_flag(cls, value: object, info: Any) -> object:
        return _strict_bool(value, info.field_name or "flag")

    @model_validator(mode="after")
    def _validate_ranges(self) -> Self:
        if self.max_samples < self.min_samples:
            raise ValueError("max_samples must be >= min_samples")
        return self


class CalibratedBaselineRef(BaseModel):
    """MR-02 (A2 方案一): optional reference binding a run to ONE
    converged calibration round (方法说明第八章风险2 流程锁).

    Fields are copied from the calibration batch's converged decision;
    the workflow validates data_version_hash against THIS run's frozen
    data version and the criterion triple against sane ranges BEFORE
    create_run. Absent = the run is honestly declared NOT
    baseline-calibrated (BASELINE_NOT_CALIBRATED diagnostic)."""

    model_config = ConfigDict(extra="forbid")

    calibration_batch_id: str
    load_scale_percent: float
    criterion_metric: str
    criterion_target_value: float
    tolerance_percent: float
    data_version_hash: str


class ElccMethodConfig(BaseModel):
    """Typed, versioned ELCC method configuration embedded in the
    frozen RunConfig (full-config lineage; no ``Any`` fields).

    The normalized dump of this model is part of ``run_config`` and
    therefore part of the run_config_hash, the create_run idempotency
    parameters and the run manifest lineage. Adequacy runs carry
    method_config=None (backward compatible).
    """

    model_config = ConfigDict(extra="forbid")

    method_config_schema_version: Literal["1.0"] = METHOD_CONFIG_SCHEMA_VERSION
    search: ElccMethodSearchConfig
    load_growth: ElccMethodLoadGrowthConfig
    candidate: ElccMethodCandidateConfig
    convergence: ElccMethodConvergenceConfig = Field(
        default_factory=ElccMethodConvergenceConfig,
    )
    # T14: controlled storage SOC initialization (warm-up / burn-in /
    # explicit initial SOC); enters run_config, run_config_hash and the
    # run-config JSON Schema. None = no storage in this run.
    storage_soc: StorageSocConfig | None = None
    # MR-02 (A2 方案一): optional calibrated-baseline closure; enters the
    # config lineage (run_config_hash / application_config_hash) like
    # every other calculation-affecting input. None = not calibrated.
    calibrated_baseline_ref: "CalibratedBaselineRef | None" = None

    @model_validator(mode="after")
    def _validate_confidences_equal(self) -> Self:
        """The frozen methodology (§12.3/§12.4 vs §12.5) does not define
        separate equal-risk search and risk-estimate convergence
        confidence levels: the two MUST be equal. A divergence requires
        a methodology revision and re-review - never a code-side
        extension."""
        if abs(self.search.confidence_level - self.convergence.confidence_level) > 1e-12:
            raise ValueError(
                "search.confidence_level must equal "
                "convergence.confidence_level (the frozen methodology "
                "defines no separate levels)"
            )
        return self

    def normalized_dump(self) -> dict[str, Any]:
        """Stable normalized JSON payload (single source of the
        method_config_hash)."""
        return self.model_dump(mode="json")


__all__ = [
    "METHOD_CONFIG_SCHEMA_VERSION",
    "EfcMethodSearchConfig",
    "ElccMethodCandidateConfig",
    "ElccMethodConfig",
    "ElccMethodConvergenceConfig",
    "ElccMethodLoadGrowthConfig",
    "ElccMethodSearchConfig",
    "EqualRiskSearchMethodConfig",
    "LoadGrowthProfileConfig",
    "LoadGrowthTimestampsConfig",
    "StorageSocConfig",
]
