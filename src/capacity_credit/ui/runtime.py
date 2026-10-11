"""V1-T1-A workbench runtime bridge (the Streamlit-side service bridge).

ONLY the facade bridge lives here: the pages call get_workbench_service
with their explicit workspace_root and receive the read-only
WorkbenchService. There is no calculation executor, no task queue, no
global business cache and no Streamlit import: module import has no
side effects (no argument parsing, no workspace reads, no rendering),
and every render constructs the facade FRESH so each render re-verifies
the workspace and re-reads the formal facts (the shell never caches
freeze/approval conclusions between renders).
"""

from __future__ import annotations

from pathlib import Path

from capacity_credit.application.local_runtime import LocalRuntime, get_shared_runtime
from capacity_credit.application.workbench_service import WorkbenchService


def get_workbench_service(workspace_root: Path | str) -> WorkbenchService:
    """Construct (fresh, un-cached) the read-only facade for the
    explicit workspace_root. An invalid workspace raises the facade's
    WorkbenchError to the caller - the bridge never swallows it, never
    falls back to a default path and never writes anything."""
    return WorkbenchService(workspace_root)


def get_local_runtime(workspace_root: Path | str) -> LocalRuntime:
    """The workspace-SHARED LocalRuntime (V1-T1-B): every caller for the
    same normalized workspace - every page render, every browser
    session - receives the SAME runtime object, so there is exactly one
    executor and one admission slot per workspace per process. The
    session_state is never the authority for the active task; a runtime
    created fresh (e.g. after a hot reload) is still bounded by the
    same workspace RunLock and can never silently take over another
    process's active execution."""
    return get_shared_runtime(workspace_root)


__all__ = [
    "LocalRuntime",
    "WorkbenchService",
    "get_local_runtime",
    "get_workbench_service",
]
