"""Closed convergence snapshot contract (T18 round-4).

The SINGLE shared ``RiskConvergenceSnapshot`` used by the equal-risk
search (T11/ELCC), EFC and MRI. A convergence snapshot is a CLOSED,
consistency-checked object: ``risk_convergence`` + the four T08 criteria +
``should_stop`` + ``recent_batch_evidence`` + source/config identity. A
bare ``RiskConvergence`` enum, or a snapshot missing any of these fields,
is NEVER a valid convergence claim - the snapshot constructor rejects it.

The formulas live in ``engine.convergence`` (T08); this module only
freezes the snapshot shape and its consistency invariants. There is no
second snapshot class anywhere in the codebase.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import numpy as np

from capacity_credit.contracts.enums import RiskConvergence

_CRITERIA_FIELDS = (
    "min_samples_met",
    "precision_met",
    "nonzero_samples_met",
    "recent_batches_stable",
)


class RiskConvergenceError(ValueError):
    """Stable convergence-snapshot error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_snapshot_risk(value: Any) -> RiskConvergence:
    if not isinstance(value, RiskConvergence):
        raise RiskConvergenceError(
            "CONVERGENCE_INVALID_SNAPSHOT",
            "risk_convergence must be a RiskConvergence enum",
        )
    return value


def _validate_snapshot_criteria(value: Any) -> Mapping[str, bool]:
    if not isinstance(value, Mapping):
        raise RiskConvergenceError(
            "CONVERGENCE_INVALID_SNAPSHOT",
            "criteria must be a mapping",
        )
    if set(value) != set(_CRITERIA_FIELDS):
        raise RiskConvergenceError(
            "CONVERGENCE_INVALID_SNAPSHOT",
            "criteria must be exactly the four T08 criteria",
        )
    for criterion in value.values():
        if not isinstance(criterion, bool):
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "criteria values must be booleans",
            )
    return MappingProxyType(dict(value))


def _validate_snapshot_should_stop(value: Any) -> bool:
    if not isinstance(value, bool):
        raise RiskConvergenceError(
            "CONVERGENCE_INVALID_SNAPSHOT",
            "should_stop must be a boolean",
        )
    return value


def _validate_snapshot_evidence(value: Any) -> tuple[float, ...]:
    if not isinstance(value, (tuple, list)):
        raise RiskConvergenceError(
            "CONVERGENCE_INVALID_SNAPSHOT",
            "recent_batch_evidence must be a tuple or list",
        )
    evidence_tuple = tuple(value)
    for item in evidence_tuple:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float, np.integer, np.floating))
            or not np.isfinite(float(item))
        ):
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "recent_batch_evidence must be finite numbers",
            )
    return evidence_tuple


def _validate_snapshot_consistency(
    risk: RiskConvergence,
    criteria: Mapping[str, bool],
    should_stop: bool,
) -> None:
    all_met = all(criteria.values())
    if risk is RiskConvergence.CONVERGED:
        if not all_met:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "CONVERGED requires all four criteria",
            )
        if not should_stop:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "CONVERGED requires should_stop",
            )
        if not criteria["recent_batches_stable"]:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "CONVERGED requires recent_batches_stable",
            )
    elif risk is RiskConvergence.LOW_EVENT_COUNT:
        if criteria["nonzero_samples_met"]:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "LOW_EVENT_COUNT requires the nonzero-event criterion to fail (low-event evidence)",
            )
        if not should_stop:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "LOW_EVENT_COUNT requires should_stop",
            )
    else:  # NOT_CONVERGED
        if all_met and should_stop:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "NOT_CONVERGED cannot coincide with all criteria met and should_stop",
            )


def convergence_config_identity(payload: Mapping[str, Any]) -> str:
    """Stable identity of a convergence configuration snapshot (the
    snapshot records WHICH config produced it - never an unrecorded fact
    source)."""
    canonical = "|".join(
        f"{key}={payload[key]!r}" for key in sorted(payload) if key != "provisional_defaults"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RiskConvergenceSnapshot:
    """Frozen, consistency-checked convergence snapshot (upstream T08
    ConvergenceState adapter; the formulas live in the T08 engine, never
    duplicated here).

    ``source`` names the origin (e.g. ``software_default`` for the shared
    default ConvergenceConfig, or ``run_declared`` for the run's declared
    convergence standard); ``config_identity`` pins the exact config that
    produced the evidence."""

    risk_convergence: RiskConvergence
    criteria: Mapping[str, bool]
    should_stop: bool
    recent_batch_evidence: tuple[float, ...]
    source: str = "t08_convergence_state"
    config_identity: str = ""

    def __post_init__(self) -> None:
        risk = _validate_snapshot_risk(self.risk_convergence)
        criteria = _validate_snapshot_criteria(self.criteria)
        should_stop = _validate_snapshot_should_stop(self.should_stop)
        evidence = _validate_snapshot_evidence(self.recent_batch_evidence)
        _validate_snapshot_consistency(risk, criteria, should_stop)
        object.__setattr__(self, "criteria", criteria)
        object.__setattr__(self, "recent_batch_evidence", evidence)
        if not isinstance(self.source, str) or not self.source.strip():
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "source must be a non-empty string",
            )
        if not isinstance(self.config_identity, str) or not self.config_identity.strip():
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "config_identity must be a non-empty stable identity "
                "(the convergence config that produced the evidence) - "
                "never empty or a placeholder",
            )
        # A converged claim MUST be backed by verifiable recent-batch
        # evidence; and recent_batches_stable=True requires evidence too.
        if risk is RiskConvergence.CONVERGED and not evidence:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "CONVERGED requires non-empty recent_batch_evidence "
                "(four manual booleans cannot fabricate a formal converged snapshot)",
            )
        if criteria["recent_batches_stable"] and not evidence:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "recent_batches_stable=True requires non-empty recent_batch_evidence",
            )

    @classmethod
    def from_convergence_state(
        cls,
        convergence_state: Any,
        recent_batch_evidence: tuple[float, ...] = (),
        source: str = "t08_convergence_state",
        config_identity: str = "",
    ) -> RiskConvergenceSnapshot:
        """Thin adapter over the T08 ConvergenceState fields (the four
        criteria semantics stay in the T08 engine). should_stop is
        validated strictly BEFORE any conversion: the raw values
        "false"/1/None are rejected, never bool()-coerced. A real
        ``config_identity`` is REQUIRED (no empty default path)."""
        if not isinstance(config_identity, str) or not config_identity.strip():
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "from_convergence_state requires a non-empty config_identity "
                "(the production factory always records the convergence config)",
            )
        try:
            risk_raw = convergence_state.risk_convergence
            criteria_raw = convergence_state.criteria
            should_stop_raw = convergence_state.should_stop
        except AttributeError as exc:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "cannot adapt convergence state: missing fields",
            ) from exc
        if not isinstance(should_stop_raw, bool):
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "convergence_state.should_stop must be a boolean",
            )
        if not isinstance(criteria_raw, Mapping):
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                "convergence_state.criteria must be a mapping",
            )
        try:
            risk = RiskConvergence(risk_raw)
        except (TypeError, ValueError) as exc:
            raise RiskConvergenceError(
                "CONVERGENCE_INVALID_SNAPSHOT",
                f"cannot adapt risk_convergence: {exc}",
            ) from exc
        return cls(
            risk_convergence=risk,
            criteria=dict(criteria_raw),
            should_stop=should_stop_raw,
            recent_batch_evidence=recent_batch_evidence,
            source=source,
            config_identity=config_identity,
        )


__all__ = [
    "RiskConvergenceError",
    "RiskConvergenceSnapshot",
    "convergence_config_identity",
]
