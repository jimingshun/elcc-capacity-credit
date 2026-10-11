"""Application services: lifecycle state machine.

The lifecycle transition table is the explicit, single source of truth
from the application service contract V0.2 §4.2:

    created -> queued | cancelled
    queued  -> running | cancelled | failed
    running -> stopping | completed | failed
    stopping-> cancelled | completed | failed

``lifecycle_status`` describes the task process only; it never implies
method validity (WF-03). Terminal states have no outgoing edges.
"""

from __future__ import annotations

from collections.abc import Mapping

from capacity_credit.contracts.enums import LifecycleStatus

LIFECYCLE_TRANSITIONS: Mapping[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.CREATED: frozenset(
        {LifecycleStatus.QUEUED, LifecycleStatus.CANCELLED}
    ),
    LifecycleStatus.QUEUED: frozenset(
        {
            LifecycleStatus.RUNNING,
            LifecycleStatus.CANCELLED,
            LifecycleStatus.FAILED,
        }
    ),
    LifecycleStatus.RUNNING: frozenset(
        {
            LifecycleStatus.STOPPING,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
        }
    ),
    LifecycleStatus.STOPPING: frozenset(
        {
            LifecycleStatus.CANCELLED,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
        }
    ),
}


class TransitionError(ValueError):
    """Raised for illegal lifecycle transitions."""


def is_allowed_transition(
    current: LifecycleStatus, target: LifecycleStatus
) -> bool:
    return target in LIFECYCLE_TRANSITIONS.get(current, frozenset())


def require_transition(
    current: LifecycleStatus, target: LifecycleStatus
) -> None:
    if not is_allowed_transition(current, target):
        raise TransitionError(
            f"illegal lifecycle transition {current.value} -> {target.value}"
        )


__all__ = [
    "LIFECYCLE_TRANSITIONS",
    "TransitionError",
    "is_allowed_transition",
    "require_transition",
]
