"""T21 PRAS input-mapping audit (offline, pure Python, no Julia).

Maps a PrasCaseDefinition (benchmarks/pras/*/case.yaml) to an EXPECTED
PRAS object/field interface with explicit lossless/lossy marking. This
is an audit / intermediate representation - it NEVER claims a real PRAS
object was produced.

Controlled status model (local, no global enums touched):

- pras_version_status: pending_environment_approval | locked
- input_mapping_status: pending_environment_approval | audited_confirmed
- external_execution_status: not_run | completed

Illegal combinations (e.g. pending+completed) are rejected. The repo
cases stay pending/not_run and can NEVER be matched. Input hashes cover
only model/comparison-affecting fields (case_input_payload) - workflow
metadata never enters the input identity. Hourly teaching sequences are
required to prove timeline alignment - a bare profile string is never
enough. Hashes reuse stable_dump + SHA-256.

Timeline protocol (the SINGLE controlled timeline contract, entered into
case_input_hash through comparison_protocol):

- annualization: controlled protocol id
  (repeat_daily_profile_365_to_8760h is the ONLY frozen protocol);
- base_horizon_hours: the 24h daily profile length (must equal the case
  horizon_hours);
- profile_repeat_count: how many times the base profile is repeated;
- target_horizon_hours: base_horizon_hours x profile_repeat_count
  (24 x 365 = 8760; non-integer annualization definitions are REJECTED).

prepare_comparison_timeline() is the Python-side SINGLE timeline
expansion entry: p01_run.py and every consumer must use it instead of
maintaining a second copy of TARGET_HOURS/DAILY_REPEATS.

PRAS SystemModel uses integer MW/MWh: every model quantity that maps to
a PRAS integer array must be exactly representable (is_integer); the
offline mapping audit validates load_profile_mw, thermal capacity_mw,
the VRE MW curve (direct MW values or pu x installed_capacity_mw), and
storage power/energy BEFORE any Julia run - a Python audit that claims
lossless while Julia exact_int would reject is a mapping error
(PRAS_INTEGER_REPRESENTATION_REQUIRED).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from capacity_credit.application.atomic_io import stable_dump

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_PRS_CASES = _ROOT / "benchmarks" / "pras"
_PENDING = "pending_environment_approval"
_LOCKED = "locked"
_AUDITED = "audited_confirmed"
_ALLOWED_UNITS = frozenset({"mw", "pu"})
_ALLOWED_CAPACITY_VALUE_MODES = frozenset({"efc_perfect_capacity", "elcc_load_growth"})
_VERSION_STATUSES = frozenset({_PENDING, _LOCKED})
_MAPPING_STATUSES = frozenset({_PENDING, _AUDITED})
_EXECUTION_STATUSES = frozenset({"not_run", "completed"})
_SCORING_WINDOWS = frozenset({"full_formal_timeline"})
_CROSS_YEAR_BOUNDARIES = frozenset({"contiguous_formal_timeline"})
_DISPATCH_PROTOCOLS = frozenset(
    {"common_expressible_formal_timeline_only", "no_dispatch_components"}
)
_CASE_TYPES = frozenset({"conventional", "vre", "storage"})

# THE frozen timeline protocols: protocol_id -> (profile_repeat_count,
# target_horizon_hours). Unknown protocols are rejected at case-loading
# time. The 365.25 / 8766h definitions are deliberately NOT registered.
TIMELINE_PROTOCOLS: dict[str, tuple[int, int]] = {
    "repeat_daily_profile_365_to_8760h": (365, 8760),
}


@dataclass(frozen=True)
class ComparisonTimeline:
    """The Python-side SINGLE timeline expansion (pure function output
    of prepare_comparison_timeline). Both p01_run.py and the PRAS runner
    must agree on these numbers; the protocol identity is part of the
    case input hash."""

    protocol_id: str
    base_horizon_hours: int
    effective_horizon_hours: int
    profile_repeat_count: int
    load_profile_mw: tuple[float, ...]
    vre_profile_mw: tuple[float, ...] | None



class PrasMappingError(ValueError):
    """Stable mapping-audit error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PrasVersionEvidence:
    """THE single version/evidence fact source (composed into
    PrasEvidenceBundle by pras_results - no duplicate fields there)."""

    julia_version: str | None = None
    pras_version: str | None = None
    pras_commit: str | None = None
    python_model_version: str | None = None
    python_git_head: str | None = None
    system_input_hash: str | None = None
    mapping_audit_hash: str | None = None
    run_script_hash: str | None = None
    random_seed: int | None = None
    sample_count: int | None = None
    raw_output_ref: str | None = None
    raw_output_hash: str | None = None
    version_status: str = _PENDING
    mapping_status: str = _PENDING
    execution_status: str = "not_run"
    case_id: str | None = None


@dataclass(frozen=True)
class PrasMappingEntry:
    """One python-field -> PRAS object/field mapping with explicit loss
    marking; unsupported fields are never silently dropped."""

    python_field: str
    pras_object: str
    pras_field: str
    source_unit: str | None
    target_unit: str | None
    conversion: str
    lossy: bool
    loss_reason: str
    supported: bool
    evidence_status: str


def _entry(
    python_field: str,
    pras_object: str,
    pras_field: str,
    *,
    source_unit: str | None = None,
    target_unit: str | None = None,
    conversion: str = "direct copy",
    lossy: bool = False,
    loss_reason: str = "",
    supported: bool = True,
    evidence_status: str = "provisional",
) -> PrasMappingEntry:
    if lossy and not loss_reason:
        raise PrasMappingError("MAPPING_LOSS_WITHOUT_REASON", python_field)
    return PrasMappingEntry(
        python_field=python_field,
        pras_object=pras_object,
        pras_field=pras_field,
        source_unit=source_unit,
        target_unit=target_unit,
        conversion=conversion,
        lossy=lossy,
        loss_reason=loss_reason,
        supported=supported,
        evidence_status=evidence_status,
    )


@dataclass(frozen=True)
class PrasMappingAudit:
    """Closed mapping audit for one case. External version/run evidence
    lives ONLY in PrasVersionEvidence + PrasEvidenceBundle - never here."""

    case_id: str
    entries: tuple[PrasMappingEntry, ...]
    input_hash: str
    audit_hash: str

    def lossy_fields(self) -> list[str]:
        return [e.python_field for e in self.entries if e.lossy]

    def unsupported_fields(self) -> list[str]:
        return [e.python_field for e in self.entries if not e.supported]


def _assert_integer_representable(value: float, what: str) -> None:
    """PRAS SystemModel integer MW/MWh arrays: the value must be exactly
    representable as an integer - never silently rounded."""
    if isinstance(value, bool) or not np.isfinite(float(value)):
        raise PrasMappingError(
            "PRAS_INTEGER_REPRESENTATION_REQUIRED",
            f"{what} must be finite for PRAS integer representation, got {value!r}",
        )
    if not float(value).is_integer():
        raise PrasMappingError(
            "PRAS_INTEGER_REPRESENTATION_REQUIRED",
            f"{what}={value!r} is not exactly representable as an integer MW/MWh "
            "(PRAS SystemModel integer arrays; silent rounding is forbidden)",
        )




@dataclass(frozen=True)
class PrasCaseDefinition:
    """Structured PRAS comparison case (mirrors benchmarks/pras/*/case.yaml).
    Synthetic teaching data only; every version/execution status is
    explicit and pending until the environment is approved. Hourly
    teaching sequences (load_profile_mw / vre.profile_values) are the
    alignment evidence - bare identifiers are never sufficient."""

    case_id: str
    schema_version: str
    case_type: str
    synthetic_data: bool
    timezone: str
    time_resolution: str
    weather_year: int
    load_mw: float
    load_shape: str
    start_timestamp: str
    horizon_hours: int
    load_profile_mw: tuple[float, ...]
    random_seed: int
    sample_count: int
    units: tuple[dict[str, Any], ...]
    vre: dict[str, Any] | None
    storage: dict[str, Any] | None
    intertie: dict[str, Any] | None
    capacity_value_mode: str | None
    python_model_version: str
    python_git_head: str
    pras_version_status: str
    input_mapping_status: str
    external_execution_status: str
    comparison_protocol: dict[str, Any]
    numeric_tolerance: float
    comparison_metrics: tuple[str, ...]
    expected_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.schema_version or not self.case_id:
            raise PrasMappingError("CASE_MISSING_IDENTITY", "case_id/schema_version required")
        if self.synthetic_data is not True:
            raise PrasMappingError("CASE_SYNTHETIC_FLAG", "synthetic_data must be true")
        # controlled case_type with resource-composition consistency
        if self.case_type not in _CASE_TYPES:
            raise PrasMappingError("CASE_TYPE_UNKNOWN", f"case_type={self.case_type!r}")
        if self.case_type == "conventional":
            if self.vre is not None or self.storage is not None or self.intertie is not None:
                raise PrasMappingError(
                    "CASE_TYPE_CONVENTIONAL_CONTRADICTION",
                    "conventional cases must not carry vre/storage/intertie",
                )
        elif self.case_type == "vre":
            if self.vre is None:
                raise PrasMappingError(
                    "CASE_TYPE_VRE_MISSING_VRE",
                    "vre cases must declare a vre resource",
                )
            if self.storage is not None:
                raise PrasMappingError(
                    "CASE_TYPE_VRE_WITH_STORAGE",
                    "vre cases must not carry storage",
                )
        elif self.case_type == "storage" and self.storage is None:
            raise PrasMappingError(
                "CASE_TYPE_STORAGE_MISSING_STORAGE",
                "storage cases must declare a storage unit",
            )
        load = float(self.load_mw)
        if not np.isfinite(load) or load <= 0.0:
            raise PrasMappingError("CASE_LOAD_INVALID", "load_mw must be finite and positive")
        tol = float(self.numeric_tolerance)
        if not np.isfinite(tol) or tol < 0.0:
            raise PrasMappingError("CASE_TOLERANCE_INVALID", "numeric_tolerance must be finite and non-negative")
        if self.sample_count <= 0 or self.random_seed < 0:
            raise PrasMappingError("CASE_SAMPLING_INVALID", "sample_count/random_seed invalid")
        if self.horizon_hours <= 0:
            raise PrasMappingError("CASE_HORIZON_INVALID", "horizon_hours must be positive")
        profile = np.asarray(self.load_profile_mw, dtype=float)
        if profile.size != self.horizon_hours:
            raise PrasMappingError(
                "CASE_PROFILE_LENGTH_MISMATCH",
                f"load_profile_mw length {profile.size} != horizon_hours {self.horizon_hours}",
            )
        if not np.isfinite(profile).all() or (profile < 0).any():
            raise PrasMappingError("CASE_PROFILE_INVALID", "load_profile_mw must be finite and non-negative")
        # controlled status model with illegal-combination blocking
        if self.pras_version_status not in _VERSION_STATUSES:
            raise PrasMappingError("CASE_UNKNOWN_VERSION_STATUS", self.pras_version_status)
        if self.input_mapping_status not in _MAPPING_STATUSES:
            raise PrasMappingError("CASE_UNKNOWN_MAPPING_STATUS", self.input_mapping_status)
        if self.external_execution_status not in _EXECUTION_STATUSES:
            raise PrasMappingError("CASE_BAD_EXECUTION_STATUS", self.external_execution_status)
        if self.external_execution_status == "completed":
            if self.pras_version_status != _LOCKED:
                raise PrasMappingError(
                    "EXECUTION_REQUIRES_VERSION_LOCK",
                    "completed requires pras_version_status=locked",
                )
            if self.input_mapping_status != _AUDITED:
                raise PrasMappingError(
                    "EXECUTION_REQUIRES_MAPPING_AUDIT",
                    "completed requires input_mapping_status=audited_confirmed",
                )
        # capacity-value direction vs comparison metrics cross-check:
        # efc_mw requires efc_perfect_capacity; ELCC metrics require
        # elcc_load_growth; mutually exclusive directions are rejected
        metrics = set(self.comparison_metrics or ())
        declared_mode = self.capacity_value_mode
        # mutual exclusion is checked FIRST - the exclusive branch must be
        # reachable regardless of the declared mode
        if "efc_mw" in metrics and "elcc_mw" in metrics:
            raise PrasMappingError(
                "CAPACITY_DIRECTIONS_EXCLUSIVE",
                "EFC and ELCC capacity metrics must not be declared together",
            )
        if "efc_mw" in metrics and declared_mode != "efc_perfect_capacity":
            raise PrasMappingError(
                "CAPACITY_METRIC_MODE_MISMATCH",
                "comparison metric efc_mw requires capacity_value_mode=efc_perfect_capacity",
            )
        if "elcc_mw" in metrics and declared_mode != "elcc_load_growth":
            raise PrasMappingError(
                "CAPACITY_METRIC_MODE_MISMATCH",
                "comparison metric elcc_mw requires capacity_value_mode=elcc_load_growth",
            )
        # controlled comparison protocol
        protocol = self.comparison_protocol or {}
        if protocol.get("order") != "risk_first_then_capacity_value":
            raise PrasMappingError("PROTOCOL_ORDER_UNCONTROLLED", str(protocol.get("order")))
        if protocol.get("method") != "joint_95_percent_interval":
            raise PrasMappingError("PROTOCOL_METHOD_UNCONTROLLED", str(protocol.get("method")))
        if protocol.get("z_quantile") != "z_0.975":
            raise PrasMappingError("PROTOCOL_Z_UNCONTROLLED", str(protocol.get("z_quantile")))
        # controlled timeline contract: annualization protocol id,
        # base/profile-repeat/target-horizon consistency (24x365=8760,
        # 365.25/8766 rejected). The protocol id is part of the input
        # identity so a timeline change invalidates the case hash.
        self._validate_timeline_protocol(protocol)
        dispatch_id = protocol.get("dispatch_protocol_id")
        if dispatch_id not in _DISPATCH_PROTOCOLS:
            raise PrasMappingError("PROTOCOL_DISPATCH_UNCONTROLLED", str(dispatch_id))
        if self.storage is None:
            if dispatch_id != "no_dispatch_components":
                raise PrasMappingError(
                    "PROTOCOL_DISPATCH_NO_STORAGE",
                    "non-storage cases require dispatch_protocol_id=no_dispatch_components",
                )
        else:
            storage_dispatch = self.storage.get("dispatch_protocol")
            if (
                dispatch_id != "common_expressible_formal_timeline_only"
                or storage_dispatch != "common_expressible_formal_timeline_only"
                or dispatch_id != storage_dispatch
            ):
                raise PrasMappingError(
                    "PROTOCOL_STORAGE_DISPATCH_MISMATCH",
                    "storage case requires comparison dispatch_protocol_id == "
                    "storage.dispatch_protocol == common_expressible_formal_timeline_only",
                )
        if self.storage is not None:
            _validate_storage(self.storage, self)
        self._validate_integer_representability()


    def _validate_timeline_protocol(self, protocol: dict[str, Any]) -> None:
        """Frozen timeline contract. Unknown annualization protocols are
        rejected AT CASE LOADING; base_horizon_hours must equal the case
        horizon_hours; target_horizon_hours must equal
        base_horizon_hours x profile_repeat_count; the 365.25 / 8766h
        definitions are rejected. Any mismatch raises a stable
        PrasMappingError."""
        if not isinstance(protocol, dict):
            raise PrasMappingError("PROTOCOL_TIMELINE_INVALID", "comparison_protocol must be an object")
        annualization = protocol.get("annualization")
        if annualization not in TIMELINE_PROTOCOLS:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_UNKNOWN",
                f"annualization protocol {annualization!r} is not a frozen controlled protocol",
            )
        expected_repeat, expected_target = TIMELINE_PROTOCOLS[annualization]
        for key in ("base_horizon_hours", "profile_repeat_count", "target_horizon_hours"):
            raw = protocol.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, np.integer)):
                raise PrasMappingError(
                    "TIMELINE_PROTOCOL_INVALID",
                    f"comparison_protocol.{key} must be a strict integer, got {raw!r}",
                )
        base = int(protocol["base_horizon_hours"])
        repeat = int(protocol["profile_repeat_count"])
        target = int(protocol["target_horizon_hours"])
        if base != self.horizon_hours:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_INCONSISTENT",
                f"base_horizon_hours={base} must equal horizon_hours={self.horizon_hours}",
            )
        if repeat != expected_repeat:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_INCONSISTENT",
                f"profile_repeat_count={repeat} must equal {expected_repeat} for {annualization}",
            )
        if target != expected_target:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_INCONSISTENT",
                f"target_horizon_hours={target} must equal {expected_target} for {annualization}",
            )
        if base * repeat != target:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_INCONSISTENT",
                f"target_horizon_hours={target} != base_horizon_hours x profile_repeat_count "
                f"({base} x {repeat} = {base * repeat})",
            )
        if 8766 in (base * repeat, target) or repeat == 365 and target == 8766:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_36525_FORBIDDEN",
                "the 365.25 / 8766h annualization definition is forbidden",
            )


    def _validate_integer_representability(self: PrasCaseDefinition) -> None:
        """Pre-flight integer representability for every quantity that maps
        to a PRAS integer array (load, thermal capacity, VRE MW curve in its
        ACTUAL unit convention, storage power/energy). Runs inside
        __post_init__ AND audit_case_mapping so the
        load_case/PrasCaseDefinition/audit chain can never claim lossless
        while Julia exact_int would reject."""
        for index, value in enumerate(self.load_profile_mw):
            _assert_integer_representable(float(value), f"load_profile_mw[{index}]")
        for unit in self.units:
            uid = unit.get("id")
            if isinstance(uid, str) and uid.strip():
                _assert_integer_representable(float(unit["capacity_mw"]), f"units.{uid.strip()}.capacity_mw")
        if self.vre is not None:
            uid = self.vre.get("id")
            output_unit = self.vre.get("output_unit")
            installed = float(self.vre.get("installed_capacity_mw") or 0.0)
            for index, value in enumerate(self.vre.get("profile_values") or ()):
                if output_unit == "mw":
                    actual = float(value)
                    what = f"vre.{uid}.profile_values[{index}]"
                else:  # pu x installed_capacity_mw
                    actual = float(value) * installed
                    what = f"vre.{uid}.profile_values[{index}] x installed_capacity_mw"
                _assert_integer_representable(actual, what)
        if self.storage is not None:
            _assert_integer_representable(
                float(self.storage.get("power_mw") or 0.0), f"storage.{self.storage.get('id')}.power_mw"
            )
            _assert_integer_representable(
                float(self.storage.get("energy_mwh") or 0.0), f"storage.{self.storage.get('id')}.energy_mwh"
            )




@dataclass(frozen=True)
class _UnitView:
    index: int
    unit: dict[str, Any]

    @property
    def uid(self) -> str:
        value = self.unit.get("id")
        if not isinstance(value, str) or not value.strip():
            raise PrasMappingError("UNIT_MISSING_ID", f"unit[{self.index}] id required")
        return value.strip()

    @property
    def capacity(self) -> float:
        raw = self.unit.get("capacity_mw")
        if raw is None or isinstance(raw, bool):
            raise PrasMappingError("UNIT_CAPACITY_MISSING", self.uid)
        value = float(raw)
        if not np.isfinite(value) or value <= 0.0:
            raise PrasMappingError("UNIT_CAPACITY_INVALID", f"{self.uid} capacity_mw={value!r}")
        return value

    @property
    def metric(self) -> str:
        value = self.unit.get("availability_metric")
        if value not in ("for", "mttf_mttr"):
            raise PrasMappingError("UNIT_AVAILABILITY_METRIC_MISSING", self.uid)
        assert isinstance(value, str)
        return value

    @property
    def for_rate(self) -> float | None:
        if self.metric != "for":
            return None
        raw = self.unit.get("for")
        if raw is None or isinstance(raw, bool):
            raise PrasMappingError("UNIT_FOR_MISSING", self.uid)
        value = float(raw)
        if not np.isfinite(value) or not 0.0 <= value <= 1.0:
            raise PrasMappingError("UNIT_FOR_OUT_OF_RANGE", f"{self.uid} for={value!r}")
        return value

    @property
    def mttf_mttr(self) -> tuple[float, float] | None:
        if self.metric != "mttf_mttr":
            return None
        mttf = self.unit.get("mttf_hours")
        mttr = self.unit.get("mttr_hours")
        if mttf is None or mttr is None:
            raise PrasMappingError("UNIT_MTTF_MTTR_MISSING", self.uid)
        mttf_v, mttr_v = float(mttf), float(mttr)
        if not (np.isfinite(mttf_v) and np.isfinite(mttr_v)) or mttf_v <= 0 or mttr_v <= 0:
            raise PrasMappingError("UNIT_MTTF_MTTR_INVALID", self.uid)
        return mttf_v, mttr_v


def _validate_vre(vre: dict[str, Any], case: PrasCaseDefinition) -> str:
    uid = vre.get("id")
    if not isinstance(uid, str) or not uid.strip():
        raise PrasMappingError("VRE_MISSING_ID", "vre id required")
    # candidate_resource must strictly reference THIS vre id - the
    # single-vre model cannot accept missing/blank/foreign candidates
    candidate = vre.get("candidate_resource")
    if candidate is None:
        raise PrasMappingError(
            "VRE_CANDIDATE_RESOURCE_MISSING",
            f"vre {uid} candidate_resource must be present (strict non-empty string)",
        )
    if not isinstance(candidate, str) or not candidate.strip():
        raise PrasMappingError(
            "VRE_CANDIDATE_RESOURCE_MISSING",
            f"vre {uid} candidate_resource must be a strict non-empty string "
            "(None/number/bool/blank rejected)",
        )
    if candidate.strip() != uid:
        raise PrasMappingError(
            "VRE_CANDIDATE_RESOURCE_MISMATCH",
            f"vre candidate_resource {candidate!r} must equal the vre id {uid!r}",
        )
    output_unit = vre.get("output_unit")
    if output_unit not in _ALLOWED_UNITS:
        raise PrasMappingError("VRE_UNIT_NOT_EXPLICIT", f"{uid} output_unit must be mw or pu")
    values = vre.get("profile_values")
    if values is None:
        raise PrasMappingError(
            "VRE_PROFILE_VALUES_MISSING",
            f"{uid} profile_values required (a bare profile string cannot prove alignment)",
        )
    array = np.asarray(values, dtype=float)
    if array.size != len(case.load_profile_mw):
        raise PrasMappingError(
            "VRE_PROFILE_LENGTH_MISMATCH",
            f"{uid} profile_values length {array.size} != load timeline {len(case.load_profile_mw)}",
        )
    if not np.isfinite(array).all() or (array < 0).any():
        raise PrasMappingError("VRE_PROFILE_INVALID", f"{uid} profile_values must be finite and non-negative")
    if output_unit == "mw":
        capacity = float(vre["installed_capacity_mw"])
        if capacity <= 0:
            raise PrasMappingError("VRE_CAPACITY_INVALID", uid)
        if float(np.max(array)) > capacity:
            raise PrasMappingError(
                "VRE_PROFILE_EXCEEDS_CAPACITY",
                f"{uid} MW output exceeds installed_capacity_mw {capacity}",
            )
    else:  # pu
        if float(np.min(array)) < 0.0 or float(np.max(array)) > 1.0:
            raise PrasMappingError("VRE_PROFILE_PU_OUT_OF_RANGE", f"{uid} p.u. values must lie in [0,1]")
    return uid


def _validate_capacity_value_mode(mode: str | None) -> str | None:
    if mode is None:
        return None
    if mode == "elcc_load_growth_and_efc_perfect_capacity":
        raise PrasMappingError(
            "CAPACITY_VALUE_DIRECTION_MIXED",
            "ELCC load-growth and EFC perfect-capacity directions must not be mixed",
        )
    if mode not in _ALLOWED_CAPACITY_VALUE_MODES:
        raise PrasMappingError("CAPACITY_VALUE_MODE_UNKNOWN", mode)
    return mode


def _validate_storage(storage: dict[str, Any], case: PrasCaseDefinition) -> str:
    uid = storage.get("id")
    if not isinstance(uid, str) or not uid.strip():
        raise PrasMappingError("STORAGE_MISSING_ID", "storage id required")
    for key in ("power_mw", "energy_mwh", "charge_efficiency", "discharge_efficiency",
                "soc_min", "soc_max", "initial_soc"):
        if storage.get(key) is None:
            raise PrasMappingError("STORAGE_FIELD_MISSING", f"{uid}.{key}")
    for key in ("charge_efficiency", "discharge_efficiency"):
        value = float(storage[key])
        if not 0.0 < value <= 1.0:
            raise PrasMappingError("STORAGE_EFFICIENCY_INVALID", f"{uid}.{key}={value}")
    power = float(storage["power_mw"])
    energy = float(storage["energy_mwh"])
    if power <= 0 or energy <= 0:
        raise PrasMappingError("STORAGE_SIZE_INVALID", uid)
    initial = float(storage["initial_soc"])
    soc_min = float(storage["soc_min"])
    soc_max = float(storage["soc_max"])
    if not 0.0 <= soc_min <= soc_max <= 1.0 or not soc_min <= initial <= soc_max:
        raise PrasMappingError("STORAGE_SOC_INVALID", uid)
    # controlled protocol fields (P-03 contract: enums + strict integers)
    scoring_window = storage.get("scoring_window")
    if scoring_window not in _SCORING_WINDOWS:
        raise PrasMappingError("STORAGE_SCORING_WINDOW_UNCONTROLLED", f"{uid} scoring_window={scoring_window!r}")
    cross_year = storage.get("cross_year_boundary")
    if cross_year not in _CROSS_YEAR_BOUNDARIES:
        raise PrasMappingError(
            "STORAGE_CROSS_YEAR_UNCONTROLLED", f"{uid} cross_year_boundary={cross_year!r}"
        )
    dispatch = storage.get("dispatch_protocol")
    if dispatch not in _DISPATCH_PROTOCOLS:
        raise PrasMappingError("STORAGE_DISPATCH_UNCONTROLLED", f"{uid} dispatch_protocol={dispatch!r}")
    for key in ("pre_history_hours", "burnin_hours"):
        raw = storage.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, np.integer)) or int(raw) < 0:
            raise PrasMappingError(
                "STORAGE_HOURS_INVALID", f"{uid}.{key} must be a non-negative integer"
            )
    # strict integer scoring hours: bool/float/str are rejected - never
    # silently int()-coerced
    start_raw = storage.get("scoring_start_hour")
    end_raw = storage.get("scoring_end_hour")
    if (
        isinstance(start_raw, bool)
        or not isinstance(start_raw, (int, np.integer))
        or isinstance(end_raw, bool)
        or not isinstance(end_raw, (int, np.integer))
    ):
        raise PrasMappingError(
            "STORAGE_SCORING_HOURS_TYPE",
            f"{uid} scoring_start_hour/scoring_end_hour must be strict integers",
        )
    start = int(start_raw)
    end = int(end_raw)
    # The scoring window follows the COMPARISON PROTOCOL's effective
    # horizon (target_horizon_hours, 8760) - never the 24h base template.
    protocol = case.comparison_protocol or {}
    target_raw = protocol.get("target_horizon_hours")
    if isinstance(target_raw, bool) or not isinstance(target_raw, (int, np.integer)):
        raise PrasMappingError(
            "STORAGE_TIMELINE_MISSING",
            f"{uid} scoring window requires a controlled target_horizon_hours",
        )
    target_horizon = int(target_raw)
    if scoring_window == "full_formal_timeline" and (
        start != 0 or end != target_horizon
    ):
        raise PrasMappingError(
            "STORAGE_FULL_TIMELINE_BOUNDS",
            f"{uid} full_formal_timeline requires scoring_start_hour==0 and "
            f"scoring_end_hour==effective_horizon_hours=={target_horizon} "
            f"(the comparison protocol target, never the {case.horizon_hours}h base template)",
        )
    if start < 0 or end > target_horizon or start >= end:
        raise PrasMappingError(
            "STORAGE_SCORING_WINDOW_INVALID",
            f"{uid} scoring window [{start}, {end}) must satisfy "
            f"0 <= start < end <= effective_horizon_hours={target_horizon}",
        )
    # finite checks: NaN/inf must never enter the case hash / audit
    for key in ("power_mw", "energy_mwh", "charge_efficiency", "discharge_efficiency",
                "soc_min", "soc_max", "initial_soc"):
        value = float(storage[key])
        if not np.isfinite(value):
            raise PrasMappingError("STORAGE_NON_FINITE", f"{uid}.{key} must be finite")
    return uid


def case_input_payload(case: PrasCaseDefinition) -> dict[str, Any]:
    """ONLY model/comparison-affecting fields - workflow metadata
    (version/mapping/execution status, expected_evidence prose) never
    enters the input identity."""
    return {
        "case_id": case.case_id,
        "schema_version": case.schema_version,
        "case_type": case.case_type,
        "timezone": case.timezone,
        "time_resolution": case.time_resolution,
        "weather_year": case.weather_year,
        "start_timestamp": case.start_timestamp,
        "horizon_hours": case.horizon_hours,
        "load_mw": case.load_mw,
        "load_shape": case.load_shape,
        "load_profile_mw": list(case.load_profile_mw),
        "random_seed": case.random_seed,
        "sample_count": case.sample_count,
        "units": case.units,
        "vre": case.vre,
        "storage": case.storage,
        "intertie": case.intertie,
        "capacity_value_mode": case.capacity_value_mode,
        "numeric_tolerance": case.numeric_tolerance,
        "comparison_protocol": case.comparison_protocol,
        "comparison_metrics": list(case.comparison_metrics),
        "python_model_version": case.python_model_version,
    }


def case_input_hash(case: PrasCaseDefinition) -> str:
    """Stable input hash over case_input_payload (stable_dump + SHA-256);
    changing status metadata never changes it, changing any model field
    always does."""
    return hashlib.sha256(stable_dump(case_input_payload(case))).hexdigest()


def required_mapping_fields(case: PrasCaseDefinition) -> list[str]:
    """THE single list of model/comparison fields that MUST enter the
    mapping audit (plus explicit protocol/evidence classifications):

    - model fields (load timeline, units, vre, storage, capacity-value
      direction): each must appear in the audit entries;
    - comparison-protocol fields (comparison_protocol, numeric_tolerance,
      comparison_metrics): validated in PrasCaseDefinition.__post_init__
      and covered by the case input hash - protocol classification;
    - external execution/version evidence fields: live only in
      PrasVersionEvidence/PrasEvidenceBundle - evidence classification;
    - descriptive metadata (load_shape, expected_evidence prose, notes):
      informational only, never model-affecting.

    No model field may be silently omitted."""
    fields = [
        "load_mw",
        "load_profile_mw",
        "start_timestamp",
        "horizon_hours",
        "timezone",
        "time_resolution",
        "weather_year",
    ]
    for unit in case.units:
        uid = unit.get("id")
        if isinstance(uid, str) and uid.strip():
            fields.append(f"units.{uid.strip()}.capacity_mw")
            fields.append(f"units.{uid.strip()}.availability")
    if case.vre is not None:
        uid = case.vre.get("id")
        if isinstance(uid, str) and uid.strip():
            prefix = f"vre.{uid.strip()}"
            fields += [
                f"{prefix}.installed_capacity_mw",
                f"{prefix}.output_unit",
                f"{prefix}.profile_values",
                f"{prefix}.candidate_resource",
            ]
    # capacity_value_mode is registered whenever declared - vre OR storage
    if case.capacity_value_mode is not None:
        fields.append("capacity_value_mode")
    if case.storage is not None:
        uid = case.storage.get("id")
        if isinstance(uid, str) and uid.strip():
            prefix = f"storage.{uid.strip()}"
            fields += [
                f"{prefix}.power_mw",
                f"{prefix}.energy_mwh",
                f"{prefix}.charge_efficiency",
                f"{prefix}.discharge_efficiency",
                f"{prefix}.soc_min",
                f"{prefix}.soc_max",
                f"{prefix}.initial_soc",
                f"{prefix}.initial_soc_formation",
                f"{prefix}.pre_history_hours",
                f"{prefix}.burnin_hours",
                f"{prefix}.scoring_window",
                f"{prefix}.scoring_start_hour",
                f"{prefix}.scoring_end_hour",
                f"{prefix}.cross_year_boundary",
                f"{prefix}.dispatch_protocol",
            ]
    return fields


def mapping_completeness_blocks(
    case: PrasCaseDefinition, audit: PrasMappingAudit | None = None,
) -> str | None:
    """Completeness proof: every required model/comparison field must be
    covered by the audit entries. An already-generated audit may be
    passed in so the acceptance gate generates it exactly once; the
    public call keeps its existing single-argument shape."""
    audit = audit or audit_case_mapping(case)
    covered = {e.python_field for e in audit.entries}
    missing = [f for f in required_mapping_fields(case) if f not in covered]
    if missing:
        return "mapping audit omits required model fields: " + ", ".join(missing)
    return None


def audit_case_mapping(case: PrasCaseDefinition) -> PrasMappingAudit:
    """Audit the python fields of one case against the EXPECTED PRAS
    interface. Pure local computation; no subprocess/network/install.
    The case is re-validated here (dataclasses.replace variants skip
    __post_init__)."""
    case.__post_init__()
    entries: list[PrasMappingEntry] = []
    # locally provable direct-copy / unit-consistent / timeline-aligned
    # fields are marked confirmed (acceptance semantics); only
    # genuinely unproven interfaces stay provisional.
    entries.append(_entry("load_mw", "demand", "load_mw", source_unit="mw", target_unit="mw",
                          conversion="hourly copy", lossy=False, supported=True,
                          evidence_status="confirmed"))
    entries.append(_entry("load_profile_mw", "demand", "load_profile_mw", source_unit="mw",
                          target_unit="mw", conversion="24h teaching sequence (length==horizon)",
                          lossy=False, supported=True, evidence_status="confirmed"))
    entries.append(_entry("start_timestamp", "system", "start_timestamp", lossy=False, supported=True,
                          evidence_status="confirmed"))
    entries.append(_entry("horizon_hours", "system", "horizon_hours", lossy=False, supported=True,
                          evidence_status="confirmed"))
    entries.append(_entry("timezone", "system", "timezone", lossy=False, supported=True,
                          evidence_status="confirmed"))
    entries.append(_entry("time_resolution", "system", "time_resolution", lossy=False, supported=True,
                          evidence_status="confirmed"))
    entries.append(_entry("weather_year", "system", "weather_year", lossy=False, supported=True,
                          evidence_status="confirmed"))
    if not case.units:
        raise PrasMappingError("CASE_NO_UNITS", "at least one unit required")
    for index, unit in enumerate(case.units):
        view = _UnitView(index, unit)
        uid = view.uid
        _capacity = view.capacity  # validate (raises on invalid)
        entries.append(_entry(f"units.{uid}.capacity_mw", "generator", "capacity_mw",
                              source_unit="mw", target_unit="mw",
                              conversion="unit-consistent copy", lossy=False, supported=True,
                              evidence_status="confirmed"))
        if view.metric == "for":
            for_rate = view.for_rate
            assert for_rate is not None
            entries.append(_entry(f"units.{uid}.availability", "generator", "availability_model",
                                  conversion=f"for={for_rate} direct", lossy=False, supported=True,
                                  evidence_status="confirmed"))
        else:
            pair = view.mttf_mttr
            assert pair is not None
            mttf, mttr = pair
            entries.append(_entry(
                f"units.{uid}.availability", "generator", "availability_model",
                conversion=(
                    f"steady-state for=mttr_hours/(mttf_hours+mttr_hours) "
                    f"with mttf_hours={mttf}, mttr_hours={mttr}"
                ),
                lossy=True,
                loss_reason=(
                    "steady-state FOR loses state-duration and transition structure; "
                    "PRAS interface not locked - direct MTTF/MTTR transfer is "
                    "re-audited only after a PRAS version is locked"
                ),
                supported=True,
                evidence_status="provisional",
            ))
    if case.vre is not None:
        uid = _validate_vre(case.vre, case)
        entries.append(_entry(f"vre.{uid}.profile_values", "resource", "variable_profile",
                              source_unit=str(case.vre["output_unit"]),
                              target_unit=str(case.vre["output_unit"]),
                              conversion="synchronized 24h teaching sequence (length==load)",
                              lossy=False, supported=True, evidence_status="confirmed"))
        entries.append(_entry(f"vre.{uid}.installed_capacity_mw", "resource", "installed_capacity_mw",
                              source_unit="mw", target_unit="mw",
                              conversion="unit-consistent copy", lossy=False, supported=True,
                              evidence_status="confirmed"))
        entries.append(_entry(f"vre.{uid}.candidate_resource", "resource", "candidate_resource",
                              conversion=f"candidate_resource references vre id {uid}",
                              lossy=False, supported=True,
                              evidence_status="confirmed"))
        entries.append(_entry(f"vre.{uid}.output_unit", "resource", "output_unit",
                              source_unit=str(case.vre["output_unit"]),
                              target_unit=str(case.vre["output_unit"]),
                              conversion="explicit unit convention", lossy=False, supported=True,
                              evidence_status="confirmed"))
    mode = _validate_capacity_value_mode(case.capacity_value_mode)
    if mode is not None:
        pras_field = "efc" if mode == "efc_perfect_capacity" else "elcc"
        entries.append(_entry("capacity_value_mode", "capacity_value", pras_field,
                              conversion=f"{mode} (single direction)", lossy=False, supported=True,
                              evidence_status="confirmed"))
    if case.storage is not None:
        uid = _validate_storage(case.storage, case)
        declared_ok = (
            float(case.storage["initial_soc"]) == 0.0
            and case.storage.get("initial_soc_formation") == "zero_initial_soc_common_protocol"
            and int(case.storage.get("pre_history_hours", -1)) == 0
            and int(case.storage.get("burnin_hours", -1)) == 0
        )
        entries.append(_entry(f"storage.{uid}.power_mw", "storage", "power_mw",
                              source_unit="mw", target_unit="mw", lossy=False, supported=True,
                              evidence_status="confirmed"))
        entries.append(_entry(f"storage.{uid}.energy_mwh", "storage", "energy_mwh",
                              source_unit="mwh", target_unit="mwh", lossy=False, supported=True,
                              evidence_status="confirmed"))
        for key in ("charge_efficiency", "discharge_efficiency"):
            entries.append(_entry(f"storage.{uid}.{key}", "storage", key,
                                  source_unit="p.u.", target_unit="p.u.",
                                  conversion="direct copy (p.u. unit-consistent)",
                                  lossy=False, supported=True, evidence_status="confirmed"))
        for key in ("soc_min", "soc_max"):
            entries.append(_entry(f"storage.{uid}.{key}", "storage", key,
                                  source_unit="p.u.", target_unit="p.u.",
                                  conversion="direct copy (p.u.)", lossy=False, supported=True,
                                  evidence_status="confirmed"))
        # ACTUAL first-period SOC evidence requires a locked PRAS
        # execution: the YAML declaration alone can never prove it, so
        # every behavioral storage mapping stays provisional/lossy and
        # P-03 can never reach matched until that evidence exists.
        soc_reason = (
            "actual first-period SOC evidence requires a locked PRAS execution; "
            "the YAML declaration alone cannot prove the runtime initial SOC"
        )
        entries.append(_entry(f"storage.{uid}.initial_soc", "storage", "initial_soc",
                              source_unit="p.u.", target_unit="p.u.",
                              conversion=("zero initial SOC common protocol (declared), burnin_hours=0"
                                          if declared_ok else "protocol mismatch flagged"),
                              lossy=True,
                              loss_reason=soc_reason,
                              supported=True,
                              evidence_status="provisional"))
        entries.append(_entry(f"storage.{uid}.initial_soc_formation", "storage", "initial_soc_formation",
                              conversion=str(case.storage.get("initial_soc_formation")),
                              lossy=True,
                              loss_reason="actual SOC formation requires a locked PRAS execution",
                              supported=True, evidence_status="provisional"))
        entries.append(_entry(f"storage.{uid}.pre_history_hours", "storage", "pre_history_hours",
                              conversion="pre_history_hours=" + str(case.storage.get("pre_history_hours")),
                              lossy=True,
                              loss_reason="actual pre-history length requires a locked PRAS execution",
                              supported=True, evidence_status="provisional"))
        entries.append(_entry(f"storage.{uid}.cross_year_boundary", "storage", "cross_year_boundary",
                              conversion=str(case.storage.get("cross_year_boundary")),
                              lossy=True,
                              loss_reason="actual cross-year SOC behavior requires a locked PRAS execution",
                              supported=True, evidence_status="provisional"))
        entries.append(_entry(f"storage.{uid}.burnin_hours", "storage", "burnin_hours",
                              conversion="burnin_hours=" + str(case.storage.get("burnin_hours")) +
                              " (no implicit warm-up, declared)",
                              lossy=True,
                              loss_reason="actual burnin/discard interval requires a locked PRAS execution",
                              supported=True, evidence_status="provisional"))
        scoring = _entry(f"storage.{uid}.scoring_window", "storage", "scoring_window",
                         conversion=("scoring_start_hour=" + str(case.storage.get("scoring_start_hour")) +
                                     ", scoring_end_hour=" + str(case.storage.get("scoring_end_hour")) +
                                     " (effective horizon 8760h)"),
                         lossy=True,
                         loss_reason="actual scoring start/end positions require a locked PRAS execution",
                         supported=True, evidence_status="provisional")
        entries.append(_entry(f"storage.{uid}.scoring_start_hour", "storage", "scoring_start_hour",
                              source_unit="hour", target_unit="hour",
                              conversion=("strict integer, effective-timeline start: "
                                          + str(case.storage.get("scoring_start_hour"))),
                              lossy=True,
                              loss_reason="actual scoring start requires a locked PRAS execution",
                              supported=True, evidence_status="provisional"))
        entries.append(_entry(f"storage.{uid}.scoring_end_hour", "storage", "scoring_end_hour",
                              source_unit="hour", target_unit="hour",
                              conversion=("strict integer, effective-timeline end: "
                                          + str(case.storage.get("scoring_end_hour"))),
                              lossy=True,
                              loss_reason="actual scoring end requires a locked PRAS execution",
                              supported=True, evidence_status="provisional"))
        entries.append(scoring)
        entries.append(_entry(f"storage.{uid}.dispatch_protocol", "storage", "dispatch_protocol",
                              conversion="common expressible formal timeline only",
                              lossy=True,
                              loss_reason=("PRAS dispatch logic cannot be confirmed identical to the "
                                           "Python scheduler before the PRAS version is locked"),
                              supported=True, evidence_status="provisional"))
    input_hash = case_input_hash(case)
    audit_hash = hashlib.sha256(
        stable_dump(
            {
                "case_id": case.case_id,
                "case_input_hash": input_hash,
                "mappings": [
                    {
                        "python_field": e.python_field,
                        "pras_object": e.pras_object,
                        "pras_field": e.pras_field,
                        "source_unit": e.source_unit,
                        "target_unit": e.target_unit,
                        "conversion": e.conversion,
                        "lossy": e.lossy,
                        "loss_reason": e.loss_reason,
                        "supported": e.supported,
                        "evidence_status": e.evidence_status,
                    }
                    for e in entries
                ],
            }
        )
    ).hexdigest()
    return PrasMappingAudit(
        case_id=case.case_id,
        entries=tuple(entries),
        input_hash=input_hash,
        audit_hash=audit_hash,
    )


def prepare_comparison_timeline(case: PrasCaseDefinition) -> ComparisonTimeline:
    """Python-side SINGLE timeline expansion entry (pure function).

    Returns the frozen ComparisonTimeline with the base/effective hours,
    the repeated load profile, the repeated VRE MW curve (None when the
    case has no vre), the repeat count and the protocol id. Consumers
    (p01_run.py) must call THIS function and must NOT maintain their own
    TARGET_HOURS / DAILY_REPEATS / second timeline logic.

    The case itself is re-validated (protocol contract + integer
    representability) so a mutated case cannot silently bypass the
    loading-time checks."""
    case.__post_init__()
    protocol = dict(case.comparison_protocol or {})
    annualization = protocol.get("annualization")
    if annualization not in TIMELINE_PROTOCOLS:
        raise PrasMappingError(
            "TIMELINE_PROTOCOL_UNKNOWN",
            f"annualization protocol {annualization!r} is not a frozen controlled protocol",
        )
    repeat, target = TIMELINE_PROTOCOLS[annualization]
    base = case.horizon_hours
    load_base = tuple(float(v) for v in case.load_profile_mw)
    if len(load_base) != base:
        raise PrasMappingError(
            "CASE_PROFILE_LENGTH_MISMATCH",
            f"load_profile_mw length {len(load_base)} != horizon_hours {base}",
        )
    load_expanded = tuple(v for _ in range(repeat) for v in load_base)
    if len(load_expanded) != target:
        raise PrasMappingError(
            "TIMELINE_PROTOCOL_INCONSISTENT",
            f"expanded load length {len(load_expanded)} != target_horizon_hours {target}",
        )
    vre_expanded: tuple[float, ...] | None = None
    if case.vre is not None:
        output_unit = case.vre.get("output_unit")
        installed = float(case.vre.get("installed_capacity_mw") or 0.0)
        if output_unit == "mw":
            vre_base = tuple(float(v) for v in (case.vre.get("profile_values") or ()))
        else:  # pu x installed_capacity_mw
            vre_base = tuple(float(v) * installed for v in (case.vre.get("profile_values") or ()))
        vre_expanded = tuple(v for _ in range(repeat) for v in vre_base)
        if len(vre_expanded) != target:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_INCONSISTENT",
                f"expanded vre length {len(vre_expanded)} != target_horizon_hours {target}",
            )
    return ComparisonTimeline(
        protocol_id=annualization,
        base_horizon_hours=base,
        effective_horizon_hours=target,
        profile_repeat_count=repeat,
        load_profile_mw=load_expanded,
        vre_profile_mw=vre_expanded,
    )


def load_case_definition(relative_dir: str) -> PrasCaseDefinition:
    """Load a case from benchmarks/pras/<relative_dir>/case.yaml."""
    case_path = _PRS_CASES / relative_dir / "case.yaml"
    if not case_path.is_file():
        raise PrasMappingError("CASE_FILE_MISSING", str(case_path))
    payload = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PrasMappingError("CASE_FILE_INVALID", str(case_path))
    kwargs: dict[str, Any] = dict(payload)
    kwargs["units"] = tuple(payload.get("units") or ())
    kwargs["comparison_metrics"] = tuple(payload.get("comparison_metrics") or ())
    kwargs["expected_evidence"] = tuple(payload.get("expected_evidence") or ())
    kwargs["load_profile_mw"] = tuple(payload.get("load_profile_mw") or ())
    if payload.get("vre"):
        kwargs["vre"] = dict(payload["vre"])
        if payload["vre"].get("profile_values"):
            kwargs["vre"]["profile_values"] = tuple(payload["vre"]["profile_values"])
    kwargs.setdefault("capacity_value_mode", None)
    return PrasCaseDefinition(**kwargs)


# THE single shared baseline evidence file list (T22-B4). BOTH
# tools/check_p5_release.py (compute_scripts_hash) and
# benchmarks/pras/python/p01_run.py + src/capacity_credit/benchmark/
# pras_runner.py (production_file_hashes) consume EXACTLY this tuple -
# no second copy of the list exists anywhere. It covers the real
# execution chain: the julia entry/scripts, the python benchmark entry,
# the comparison/assembly code, the EFC components/solver, the
# equal-risk solver and the resource-run / storage chronologies that
# P-02/P-03 actually depend on. T22-M: the formal long-run monitoring
# entry tools/run_long_validation.py is part of the identity (19 files).
# Sorted for deterministic hashing.
PRAS_EVIDENCE_FILES: tuple[str, ...] = (
    "benchmarks/pras/julia/Project.toml",
    "benchmarks/pras/julia/run_case.jl",
    "benchmarks/pras/python/p01_run.py",
    "src/capacity_credit/application/efc_components.py",
    "src/capacity_credit/application/resource_run.py",
    "src/capacity_credit/application/resource_run_inputs.py",
    "src/capacity_credit/benchmark/pras_adapter.py",
    "src/capacity_credit/benchmark/pras_results.py",
    "src/capacity_credit/benchmark/pras_runner.py",
    "src/capacity_credit/engine/adequacy.py",
    "src/capacity_credit/engine/metrics.py",
    "src/capacity_credit/resources/storage.py",
    "src/capacity_credit/resources/storage_chronology.py",
    "src/capacity_credit/resources/storage_dispatch.py",
    "src/capacity_credit/scenarios/scenario_bank.py",
    "src/capacity_credit/scenarios/thermal_states.py",
    "src/capacity_credit/solvers/efc.py",
    "src/capacity_credit/solvers/equal_risk.py",
    "tools/run_long_validation.py",
)

__all__ = [
    "PRAS_EVIDENCE_FILES",
    "PrasCaseDefinition",
    "PrasMappingAudit",
    "PrasMappingEntry",
    "PrasMappingError",
    "PrasVersionEvidence",
    "audit_case_mapping",
    "case_input_hash",
    "case_input_payload",
    "load_case_definition",
    "mapping_completeness_blocks",
    "prepare_comparison_timeline",
    "required_mapping_fields",
]
