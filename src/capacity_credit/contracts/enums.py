"""Controlled machine-value enums.

Every value is verbatim from the current P0 design baseline:

- data contract V0.3 (``docs/data/2026-08-03-input-data-contract-and-dictionary.md``)
- application service contract V0.2
  (``docs/architecture/2026-08-04-application-service-and-run-artifact-contract.md``)
- methodology V0.4 (``docs/methodology/2026-08-03-capacity-credit-methodology-design.md``)

Old machine values (``G0``/``lole``/``converged``/...) are intentionally not
members and must never be added as silent aliases: legacy values are only
migrated at the import-adapter boundary (T04).
"""

from enum import Enum


class RiskMetric(str, Enum):
    """Primary risk metric machine values (methodology V0.4 §3.4)."""

    LOLE_DAYS_PER_YEAR = "lole_days_per_year"
    LOLH_HOURS_PER_YEAR = "lolh_hours_per_year"
    EUE_MWH_PER_YEAR = "eue_mwh_per_year"
    NEUE_PPM = "neue_ppm"


class DataGrade(str, Enum):
    """Data grade machine values (data contract V0.3 §8.1)."""

    G0_TEACHING = "G0_teaching"
    G1_PRELIMINARY = "G1_preliminary"
    G2_RESEARCH = "G2_research"
    G3_RULE_SUPPORT = "G3_rule_support"


class DataVersionStatus(str, Enum):
    """Data version lifecycle (application service contract V0.2 §2)."""

    DRAFT = "draft"
    BLOCKED = "blocked"
    VALIDATED = "validated"
    FROZEN = "frozen"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class ValidationStatus(str, Enum):
    """Automatic validation conclusion (application service contract V0.2 §2)."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ImportReviewStatus(str, Enum):
    """Human import review conclusion (application service contract V0.2 §2)."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class LifecycleStatus(str, Enum):
    """Task lifecycle status; describes the process only (contract V0.2 §4.2)."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResultStatus(str, Enum):
    """Overall method-result validity (methodology V0.4 §15.1)."""

    VALID = "valid"
    VALID_WITH_WARNING = "valid_with_warning"
    LOW_RESOLUTION = "low_resolution"
    NOT_CONVERGED = "not_converged"
    INVALID_METHOD = "invalid_method"
    INVALID_DATA = "invalid_data"


class RiskConvergence(str, Enum):
    """Risk-estimate convergence status (methodology V0.4 §15.1)."""

    CONVERGED = "converged"
    NOT_CONVERGED = "not_converged"
    LOW_EVENT_COUNT = "low_event_count"


class SearchConvergence(str, Enum):
    """Equal-risk search convergence status (methodology V0.4 §15.1)."""

    CONVERGED = "converged"
    BRACKET_ONLY = "bracket_only"
    PLATEAU_UNRESOLVED = "plateau_unresolved"
    NOT_CONVERGED = "not_converged"
    NOT_APPLICABLE = "not_applicable"


class RunType(str, Enum):
    """Run types (application service contract V0.2 §3; product design §6.2)."""

    VALIDATE = "validate"
    ADEQUACY = "adequacy"
    ELCC = "elcc"
    EFC = "efc"
    MRI = "mri"
    RMRI = "rmri"
    PORTFOLIO = "portfolio"
    SENSITIVITY = "sensitivity"
    BENCHMARK = "benchmark"
    REPORT = "report"
    DEMAND_TARGET_CAPACITY = "demand_target_capacity"


class RunMode(str, Enum):
    """Run modes (application service contract V0.2 §3)."""

    FORMAL = "formal"
    TEACHING = "teaching"
    BENCHMARK = "benchmark"


class Method(str, Enum):
    """Unified result method (methodology V0.4 §16)."""

    ADEQUACY = "adequacy"
    ELCC = "elcc"
    EFC = "efc"
    MRI = "mri"
    RMRI = "rmri"
    DEMAND_TARGET_CAPACITY = "demand_target_capacity"


class CapacityValueType(str, Enum):
    """Capacity value types (methodology V0.4 §11)."""

    AVERAGE = "average"
    MARGINAL = "marginal"
    PORTFOLIO = "portfolio"
    NOT_APPLICABLE = "not_applicable"


class ChronologyMode(str, Enum):
    """Time-series organisation mode (methodology V0.4 §6.3)."""

    INDEPENDENT_WEATHER_YEARS = "independent_weather_years"
    CONTINUOUS_MULTIYEAR = "continuous_multiyear"


class StorageSocInitializationMode(str, Enum):
    """SOC initialization mode (methodology V0.4 §6.3)."""

    WEATHER_CYCLE_WARMUP = "weather_cycle_warmup"
    STOCHASTIC_SAMPLE_BURNIN = "stochastic_sample_burnin"
    NOT_APPLICABLE = "not_applicable"


class SocYearBoundaryMode(str, Enum):
    """SOC year-boundary mode (methodology V0.4 §6.3)."""

    CARRY_OVER = "carry_over"
    REINITIALIZE_WITH_CONFIGURED_MODE = "reinitialize_with_configured_mode"
    NOT_APPLICABLE = "not_applicable"


class AssessmentAnchor(str, Enum):
    """MRI/rMRI assessment anchor (methodology V0.4 §7.4)."""

    CURRENT_SYSTEM = "current_system"
    CALIBRATED_STANDARD = "calibrated_standard"


class PerturbationDimension(str, Enum):
    """MRI perturbation dimension (methodology V0.4 §10.1)."""

    FIXED_DURATION = "fixed_duration"
    POWER_ONLY = "power_only"
    ENERGY_ONLY = "energy_only"
    CUSTOM_VECTOR = "custom_vector"


class AllocationType(str, Enum):
    """Allocation of capacity value results (methodology V0.4 §11.3)."""

    NONE = "none"
    GOVERNANCE_RULE = "governance_rule"


class SystemBoundary(str, Enum):
    """System boundary (data contract V0.3 §3.2)."""

    SINGLE_AREA = "single_area"
    SINGLE_AREA_WITH_TIES = "single_area_with_ties"
    MULTI_AREA_RESERVED = "multi_area_reserved"


class LoadBasis(str, Enum):
    """Load basis (data contract V0.3 §3.2)."""

    GENERATION_SIDE_NET = "generation_side_net"
    GENERATION_SIDE_GROSS = "generation_side_gross"
    CONSUMPTION_SIDE = "consumption_side"
    UNKNOWN = "unknown"


class TimeResolution(str, Enum):
    """Time resolution (data contract V0.3 §3.1; first version only ``1h``)."""

    H1 = "1h"


class IntervalLabel(str, Enum):
    """Interval label; standard layer uses hour start (data contract V0.3 §5.3)."""

    HOUR_START = "hour_start"


class Calendar(str, Enum):
    """Calendar (data contract V0.3 §3.1)."""

    GREGORIAN = "gregorian"


class ConfidentialityLevel(str, Enum):
    """Artifact confidentiality (application service contract V0.2 §7.1)."""

    INTERNAL = "internal"
    RESTRICTED = "restricted"
    PUBLIC_SANITIZED = "public_sanitized"


class RedactionStatus(str, Enum):
    """Artifact redaction status (application service contract V0.2 §7.1)."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    COMPLETED = "completed"


class ArtifactType(str, Enum):
    """Artifact types (application service contract V0.2 §7.1).

    T19 supplement: ``ANALYSIS_SUMMARY`` registers a portfolio /
    sensitivity coordination artifact (portfolio_summary.json,
    penetration_sequence.json, governance_allocation.json,
    sensitivity_matrix.json) into a formal artifact manifest of a
    coordination run (run_type=portfolio / sensitivity, which has no
    MethodResult)."""

    JSON_RESULT = "json_result"
    PARQUET_RESULT = "parquet_result"
    EXCEL_REPORT = "excel_report"
    HTML_REPORT = "html_report"
    FIGURE = "figure"
    VERIFY_BUNDLE = "verify_bundle"
    RERUN_BUNDLE = "rerun_bundle"
    SEARCH_TRACE = "search_trace"
    ANALYSIS_SUMMARY = "analysis_summary"


class Severity(str, Enum):
    """Diagnostic severity (methodology V0.4 §15.2; data contract V0.3 §24)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RmriStatus(str, Enum):
    """rMRI status (methodology V0.4 §16 / V0.8 §16A.1).

    The T18-0 contract freeze (methodology §16A.1) fixes the minimal
    state set to ``not_applicable`` / ``stable`` / ``unstable``; specific
    instability reasons are carried by structured diagnostic codes, never
    by additional state enum values.
    """

    NOT_APPLICABLE = "not_applicable"
    STABLE = "stable"
    UNSTABLE = "unstable"


class DifferenceScheme(str, Enum):
    """MRI finite-difference scheme (methodology §16A.4). ``difference_scheme``
    in run-config is the REQUESTED scheme; each step evaluation records the
    APPLIED scheme separately."""

    FORWARD = "forward"
    CENTRAL = "central"


class StepMode(str, Enum):
    """MRI base-step mode (methodology §16A.4): ``adaptive`` computes the base
    step from the methodology §10.3 formula; ``explicit`` requires a positive
    ``base_step``."""

    ADAPTIVE = "adaptive"
    EXPLICIT = "explicit"


class StabilitySourceType(str, Enum):
    """stability_thresholds.source_type (methodology §16A.4.4)."""

    SOFTWARE_DEFAULT = "software_default"
    METHOD_REVIEW = "method_review"
    PROJECT_CONFIG = "project_config"
    POLICY_RULE = "policy_rule"


class InterpolationMethod(str, Enum):
    """Search interpolation method.

    The methodology sample contract (V0.4 §16) documents
    ``not_applicable``; the chord interpolation machine value
    (methodology V0.4 §8.4) is frozen at ``chord`` by the T11
    pre-requisite contract resolution (T02 contract-validation report
    erratum). No synonym machine values (linear, secant, ...) exist.
    """

    NOT_APPLICABLE = "not_applicable"
    CHORD = "chord"
