"""T25 resilient local job runner - THE single background-worker
implementation (application service orchestration only; no second
lifecycle, no second run lock, no second checkpoint, no second
RunService, no shell executor, no database/queue).

Architecture: ONE-SHOT detached worker subprocess per controlled job +
an atomic file control plane.

- The parent CLI creates a JOB record (``create_job``), then spawns a
  detached ``python -m capacity_credit.application.worker run``
  subprocess (argv array, ``shell=False``, ``CREATE_NO_WINDOW`` on
  Windows) and returns immediately - the CLI (and any page process)
  may exit; the worker keeps running.
- Single-flight per task identity is enforced with the EXISTING
  ``RunLock`` (msvcrt byte-range lock): the lock key derives from the
  canonical task parameters (demo) or the run identity (adequacy
  resume), so a second worker for the same task can never run compute;
  it records the stable ``WORKER_RUN_IN_PROGRESS`` diagnostic and
  exits without touching the first worker.
- The OS byte-range ``RunLock`` is THE live-owner evidence: the worker
  (holder only) refreshes the lock heartbeat; a stale heartbeat never
  authorises taking over a lock.
- Progress is a READ-ONLY projection (``progress.json``) written by the
  worker: phase, current_subtask, completed_work, total_work,
  last_progress_at, run_id, checkpoint_ref, lifecycle_status,
  diagnostic_code, monitor_degraded. It is NOT a second lifecycle
  fact source - lifecycle truth stays in RunService, lock truth in the
  RunLock, checkpoint truth in the checkpoint files.
- Control requests (start/stop_safely/resume) use the T25
  ``ControlRequest`` contract and the ``ControlRequestStore`` from
  ``run_service.py`` (the only control-request persistence), stored in
  the job's ``control/`` directory. ``stop_safely`` is honoured ONLY
  at safe boundaries (adequacy batch boundaries after the checkpoint
  is saved; demo stage boundaries before the non-checkpointed ELCC
  compute stage). A stop request that arrives inside the
  non-interruptible stage records
  ``WORKER_SAFE_STOP_UNSUPPORTED_AT_CURRENT_STAGE`` and the task runs
  to full completion - never force-killed, never faked.
- Worker process health monitoring reuses the EXISTING
  ``tools/run_long_validation.py`` (process tree/CPU/memory/stdout
  refresh); this module does NOT copy that logic. Application progress
  is the projection above.

Exit codes: 0 completed, 2 cancelled (safe stop), 3 blocked
(single-flight conflict), 4 failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from capacity_credit.application.atomic_io import (
    CorruptClaimError,
    atomic_claim_with_token,
    atomic_write_bytes,
    atomic_write_object,
    is_transient_io_conflict,
    safe_filename_component,
    stable_dump,
)
from capacity_credit.application.import_service import ServiceError
from capacity_credit.application.locks import RunLock
from capacity_credit.application.run_service import ControlRequestStore
from capacity_credit.contracts.control_requests import ControlRequest

WORKER_HEARTBEAT_SECONDS = 5.0

_EXIT_COMPLETED = 0
_EXIT_CANCELLED = 2
_EXIT_BLOCKED = 3
_EXIT_FAILED = 4

_DEMO_TASK = "demo"
_ADEQUACY_TASK = "adequacy"

_WORKER_RUN_IN_PROGRESS = "WORKER_RUN_IN_PROGRESS"
_WORKER_EXECUTION_LOCK_BUSY = "WORKER_EXECUTION_LOCK_BUSY"
_WORKER_SAFE_STOP_UNSUPPORTED = "WORKER_SAFE_STOP_UNSUPPORTED_AT_CURRENT_STAGE"
_WORKER_DISK_FULL = "WORKER_DISK_FULL"
_WORKER_CONTROL_REQUEST_INVALID = "WORKER_CONTROL_REQUEST_INVALID"
_WORKER_RESUME_STATE_NOT_ALLOWED = "WORKER_RESUME_STATE_NOT_ALLOWED"
_WORKER_RESUME_OWNER_ACTIVE = "WORKER_RESUME_OWNER_ACTIVE"
_WORKER_RESUME_CHECKPOINT_MISSING = "WORKER_RESUME_CHECKPOINT_MISSING"
_WORKER_RESUME_IDENTITY_MISMATCH = "WORKER_RESUME_IDENTITY_MISMATCH"
_WORKER_RESUME_UNSUPPORTED_FOR_TASK = "WORKER_RESUME_UNSUPPORTED_FOR_TASK"
_WORKER_TERMINAL_PUBLISH_FAILED = "WORKER_TERMINAL_PUBLISH_FAILED"
_WORKER_CONTROL_GATE_BUSY = "WORKER_CONTROL_GATE_BUSY"
_WORKER_TERMINAL_STATE_CONFLICT = "WORKER_TERMINAL_STATE_CONFLICT"
_WORKER_TERMINAL_RECOVERY_NOT_REQUIRED = "WORKER_TERMINAL_RECOVERY_NOT_REQUIRED"
_WORKER_TERMINAL_INTENT_MISSING = "WORKER_TERMINAL_INTENT_MISSING"
_WORKER_TERMINAL_INTENT_INVALID = "WORKER_TERMINAL_INTENT_INVALID"
_WORKER_TERMINAL_RECOVERY_OWNER_ACTIVE = "WORKER_TERMINAL_RECOVERY_OWNER_ACTIVE"
_TERMINAL_INTENT_VERSION = "1"
_TERMINAL_INTENT_FILE = "terminal-intent.json"
_WORKER_TERMINAL_INTENT_PERSIST_FAILED = "WORKER_TERMINAL_INTENT_PERSIST_FAILED"
_WORKER_TERMINAL_JOB_FIELDS_INVALID = "WORKER_TERMINAL_JOB_FIELDS_INVALID"
# the ONLY job-record fields a terminal publish may merge via job_fields
# (inventory of every production call site): run_id, diagnostics,
# worker_pid - identity fields (task/params/params_key/job_id/
# request_id/resume_request_id/state) can never be overridden this way
_TERMINAL_JOB_FIELDS_ALLOWED = frozenset({"run_id", "diagnostics", "worker_pid"})
# the intent's FIRST persist rides the shared transient-IO classification
# with the same small fixed budget as the control-request read path
_INTENT_WRITE_MAX_ATTEMPTS = 3
_INTENT_WRITE_BACKOFF_SECONDS = 0.15

# the control-plane gate (a RunLock in the jobs lock namespace, keyed
# per job) is SHORT-LIVED: it covers only the stop-request submit
# re-check + atomic submit, and the terminal settle + terminal progress +
# terminal job publish - never any compute, so a stop_safely submission
# during a running task is never blocked by it
_CONTROL_GATE_WAIT_SECONDS = 5.0
_CONTROL_GATE_POLL_SECONDS = 0.02


class _TerminalPublishError(Exception):
    """A terminal publish failed AFTER its fixed target state was chosen
    (the minimal pinned-target contract): the caller must NEVER retry the
    publish with a DIFFERENT target state - retry the same target or keep
    the job recoverable non-terminal with a stable diagnostic."""

    def __init__(self, target_state: str, job_id: str, cause: BaseException) -> None:
        self.target_state = target_state
        self.job_id = job_id
        self.cause = cause
        super().__init__(
            f"terminal publish of job {job_id} towards {target_state!r} failed: {cause}"
        )


class WorkerError(ValueError):
    """Stable worker error with a machine code (WORKER_<REASON>)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# job records
# ----------------------------------------------------------------------


def _jobs_root(jobs_dir: Path | str) -> Path:
    return Path(jobs_dir)


def _job_dir(jobs_dir: Path | str, job_id: str) -> Path:
    return _jobs_root(jobs_dir) / safe_filename_component(job_id)


def _job_file(jobs_dir: Path | str, job_id: str) -> Path:
    return _job_dir(jobs_dir, job_id) / "job.json"


def _progress_file(jobs_dir: Path | str, job_id: str) -> Path:
    return _job_dir(jobs_dir, job_id) / "progress.json"


def _control_store(jobs_dir: Path | str, job_id: str) -> ControlRequestStore:
    return ControlRequestStore(_job_dir(jobs_dir, job_id) / "control")


def _read_job(jobs_dir: Path | str, job_id: str) -> dict[str, Any]:
    path = _job_file(jobs_dir, job_id)
    if not path.is_file():
        raise WorkerError("WORKER_JOB_INVALID", f"job record missing: {job_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkerError("WORKER_JOB_INVALID", f"job record unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        raise WorkerError("WORKER_JOB_INVALID", f"job record inconsistent: {job_id}")
    return payload


def _write_job(jobs_dir: Path | str, job_id: str, record: dict[str, Any]) -> None:
    atomic_write_bytes(
        _job_file(jobs_dir, job_id),
        (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _update_job(jobs_dir: Path | str, job_id: str, **fields: Any) -> dict[str, Any]:
    record = _read_job(jobs_dir, job_id)
    record.update(fields)
    record["updated_at"] = datetime.now(UTC).isoformat()
    _write_job(jobs_dir, job_id, record)
    return record


def _write_progress(
    jobs_dir: Path | str,
    job_id: str,
    *,
    phase: str,
    current_subtask: str,
    completed_work: int,
    total_work: int,
    run_id: str | None,
    checkpoint_ref: str | None,
    lifecycle_status: str | None,
    diagnostic_code: str | None,
    monitor_degraded: bool = False,
) -> None:
    """The application progress projection (read-only for monitors)."""
    payload = {
        "phase": phase,
        "current_subtask": current_subtask,
        "completed_work": completed_work,
        "total_work": total_work,
        "last_progress_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "checkpoint_ref": checkpoint_ref,
        "lifecycle_status": lifecycle_status,
        "diagnostic_code": diagnostic_code,
        "monitor_degraded": monitor_degraded,
        "pid": os.getpid(),
    }
    atomic_write_bytes(
        _progress_file(jobs_dir, job_id),
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _canonical_params(task: str, params: dict[str, Any]) -> str:
    return json.dumps({"task": task, **params}, sort_keys=True, ensure_ascii=False)


def _validated_task_params(task: str, params: dict[str, Any]) -> dict[str, Any]:
    """THE shared task-parameter validation/normalization used by BOTH
    create_job and ensure_job (single implementation - external
    semantics unchanged): unknown task kinds, a missing demo config and
    missing adequacy keys are stable WORKER_TASK_INVALID errors."""
    if task not in (_DEMO_TASK, _ADEQUACY_TASK):
        raise WorkerError("WORKER_TASK_INVALID", f"unknown task kind: {task!r}")
    params = dict(params)
    if task == _DEMO_TASK:
        config = Path(params.get("config", ""))
        if not config.is_file():
            raise WorkerError("WORKER_TASK_INVALID", f"demo config missing: {config}")
        params["config"] = str(config.resolve())
        params["output_dir"] = str(Path(params["output_dir"]).resolve())
        # NOTE: an existing non-empty output directory is allowed - the
        # formal run_demo entry enforces its own controlled-replay /
        # stable-conflict semantics (never overwrite, never delete).
    else:
        for key in ("project_dir", "import_batch_id", "data_version_id", "scenario_bank_id"):
            if not params.get(key):
                raise WorkerError("WORKER_TASK_INVALID", f"adequacy job requires {key}")
        params["project_dir"] = str(Path(params["project_dir"]).resolve())
        params["root_seed"] = int(params["root_seed"])
    return params


def _job_record(
    job_id: str,
    request_id: str,
    task: str,
    params: dict[str, Any],
    requested_by: str,
    task_identity: str | None,
) -> dict[str, Any]:
    """Build ONE new job record (the single record-construction seam for
    create_job and ensure_job); task_identity is present ONLY on records
    created through the ensure contract."""
    record: dict[str, Any] = {
        "job_id": job_id,
        "request_id": request_id,
        "task": task,
        "params": params,
        "params_key": hashlib.sha256(_canonical_params(task, params).encode("utf-8")).hexdigest()[
            :32
        ],
        "requested_by": requested_by,
        "state": "queued",
        "pid": None,
        "run_id": None,
        "diagnostics": [],
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if task_identity is not None:
        record["task_identity"] = task_identity
    return record


def _persist_new_job(jobs_dir: Path | str, record: dict[str, Any]) -> None:
    """Persist ONE new job record and its PENDING start control request
    (the shared create sequence used by create_job and ensure_job); the
    start request is created PENDING: launch() accepts it only after a
    successful spawn; the worker settles it at any terminal."""
    job_id = str(record["job_id"])
    directory = _job_dir(jobs_dir, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    _write_job(jobs_dir, job_id, record)
    _safe_request_submit(
        _control_store(jobs_dir, job_id),
        ControlRequest.create(
            action="start",
            request_id=str(record["request_id"]),
            requested_by=str(record["requested_by"]),
        ),
    )


def create_job(
    jobs_dir: Path | str,
    *,
    task: str,
    params: dict[str, Any],
    requested_by: str,
) -> dict[str, Any]:
    """Create ONE controlled job record and its start control request.

    Task parameters are structured business parameters (validated per
    task kind) - never an arbitrary shell command. This is the original
    NON-idempotent creation entry (a fresh random job id per call); the
    T26-A atomic ensure/attach contract lives in ensure_job and never
    rewrites these semantics.

    The FORMAL T26 jobs root never hosts an identity-less demo: a demo
    task there MUST go through ensure_job (the identity contract).
    Legacy demo creation remains available in NON-formal caller-supplied
    jobs roots only."""
    # path safety BEFORE anything is created or read (a junctioned
    # ancestor or control-tree component is a stable refusal)
    _assert_safe_control_tree(jobs_dir)
    if task == _DEMO_TASK and _is_formal_t26_jobs_dir(jobs_dir):
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            "create_job(demo) must not create identity-less demo jobs in the "
            "formal T26 jobs root (<workspace>/.capacity-credit/jobs); use "
            "the identity-bound ensure_job contract",
        )
    params = _validated_task_params(task, params)
    record = _job_record(
        str(uuid.uuid4()), str(uuid.uuid4()), task, params, requested_by, None
    )
    _persist_new_job(jobs_dir, record)
    return record


# ----------------------------------------------------------------------
# T26-A atomic job ensure/attach (deterministic task identity)
# ----------------------------------------------------------------------

_WORKER_JOB_IDENTITY_INVALID = "WORKER_JOB_IDENTITY_INVALID"
_WORKER_JOB_IDENTITY_CONFLICT = "WORKER_JOB_IDENTITY_CONFLICT"
_WORKER_JOB_CREATION_INCOMPLETE = "WORKER_JOB_CREATION_INCOMPLETE"
_WORKER_JOB_ENSURE_BUSY = "WORKER_JOB_ENSURE_BUSY"
_WORKER_JOB_REQUESTED_BY_INVALID = "WORKER_JOB_REQUESTED_BY_INVALID"
_WORKER_JOB_PARAMS_TAMPERED = "WORKER_JOB_PARAMS_TAMPERED"
_WORKER_JOB_PATH_UNSAFE = "WORKER_JOB_PATH_UNSAFE"
_WORKER_JOB_CONFIG_CHANGED = "WORKER_JOB_CONFIG_CHANGED"
_WORKER_JOB_COMMIT_CHANGED = "WORKER_JOB_COMMIT_CHANGED"

# the SHORT-LIVED ensure gate: a RunLock in the jobs lock namespace under
# the reserved job-intents/ subdirectory, keyed by the FULL task identity;
# it covers ONLY the re-check + parameter consistency check + first
# create + existing-job return - never any spawn or compute
_ENSURE_GATE_WAIT_SECONDS = 5.0
_ENSURE_GATE_POLL_SECONDS = 0.02


class EnsureJobResult(TypedDict):
    """The atomic ensure/attach outcome: the (deterministic) job id,
    whether THIS call created the job, and the stable machine code."""

    job_id: str
    created: bool
    code: str


def _validate_task_identity(task_identity: str) -> str:
    """A task identity is EXACTLY a 64-char lowercase hex SHA-256."""
    if (
        not isinstance(task_identity, str)
        or len(task_identity) != 64
        or any(char not in "0123456789abcdef" for char in task_identity)
    ):
        raise WorkerError(
            _WORKER_JOB_IDENTITY_INVALID,
            "task_identity must be a 64-char lowercase hex SHA-256",
        )
    return task_identity


def _identity_job_id(task_identity: str) -> str:
    """The deterministic job id derived from the identity prefix."""
    return f"job-{task_identity[:32]}"


def _ensure_gate(jobs_dir: Path | str, job_id: str) -> RunLock:
    # the gate is keyed by the STORAGE job_id - never by the full
    # identity: two identities sharing the 32-hex job-id prefix MUST
    # contend for the same lock (otherwise both could create the same
    # job directory concurrently)
    return RunLock(_jobs_root(jobs_dir) / "locks" / "job-intents", job_id)


def _acquire_ensure_gate(lock: RunLock, job_id: str) -> None:
    """Bounded wait for the short-lived ensure gate (the holder only ever
    performs sub-second atomic file reads/writes)."""
    deadline = time.monotonic() + _ENSURE_GATE_WAIT_SECONDS
    while True:
        if lock.acquire():
            return
        if time.monotonic() >= deadline:
            raise WorkerError(
                _WORKER_JOB_ENSURE_BUSY,
                f"job-intent gate for job {job_id} busy beyond "
                f"{_ENSURE_GATE_WAIT_SECONDS}s",
            )
        time.sleep(_ENSURE_GATE_POLL_SECONDS)


def unsafe_path_component(path: Path, *, directory: bool | None) -> str | None:
    """THE shared fail-closed path-component check (worker, demo_service
    and business_demo all use THIS ONE - no divergent path rules): None
    when the component is absent or matches the expected kind as a REAL
    filesystem object; a short reason when it is a symlink, junction,
    other reparse point, unreadable, or the wrong kind. Purely read-only:
    never resolve-follows, never writes, never deletes or rebuilds."""
    try:
        if path.is_symlink() or path.is_junction():
            return "reparse point/link"
        if not path.exists():
            return None  # not yet created is fine
        info = path.stat(follow_symlinks=False)
    except OSError:
        return "metadata unreadable"
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return "reparse point"
    if directory is True and not path.is_dir():
        return "not a directory"
    if directory is False and not path.is_file():
        return "not a file"
    return None


def _assert_safe_control_tree(jobs_dir: Path | str, job_id: str | None = None) -> None:
    """Fail-closed path safety for the worker-controlled components -
    called by ensure_job (BEFORE any directory or file is created, since
    even the gate acquisition creates the locks namespace), by
    find_job_by_identity, by launch (BEFORE spawning) and by execute_job
    (BEFORE reading the task evidence): every EXISTING controlled
    component (jobs_dir, locks/, locks/job-intents/, the deterministic
    job dir, job.json, control/, terminal-intent.json) must be a REAL
    filesystem object of its kind. A symlink, junction, other reparse
    point or wrong kind is the stable WORKER_JOB_PATH_UNSAFE."""
    root = _jobs_root(jobs_dir)
    # the .capacity-credit PARENT chain matters as much as the jobs root
    # itself: every existing ancestor of the jobs root must also be a real
    # directory (a junction at <workspace>/.capacity-credit is refused)
    for ancestor in root.parents:
        parent_reason = unsafe_path_component(ancestor, directory=True)
        if parent_reason is not None:
            raise WorkerError(
                _WORKER_JOB_PATH_UNSAFE,
                f"unsafe jobs-dir ancestor component ({parent_reason}): {ancestor}",
            )
    candidates: list[tuple[Path, bool | None]] = [
        (root, True),
        (root / "locks", True),
        (root / "locks" / "job-intents", True),
    ]
    if job_id is not None:
        job_dir = _job_dir(root, job_id)
        candidates.extend(
            [
                (job_dir, True),
                (_job_file(root, job_id), False),
                (job_dir / "control", True),
                (_terminal_intent_path(root, job_id), False),
                (_progress_file(root, job_id), False),
                (job_dir / "monitor", True),
                (job_dir / "monitor-output.log", False),
                (job_dir / "launch.claim", False),
            ]
        )
    for path, directory in candidates:
        reason = unsafe_path_component(path, directory=directory)
        if reason is not None:
            raise WorkerError(
                _WORKER_JOB_PATH_UNSAFE,
                f"unsafe controlled path component ({reason}): {path}",
            )


def _verify_job_params_integrity(job: dict[str, Any]) -> None:
    """Re-validate and re-normalize the ARCHIVED task params through the
    shared formal entry and recompute the canonical params hash: the
    archived params, the archived params_key and the recomputation must
    all agree. A hand-edited params payload with the old params_key is
    tampering - the stable WORKER_JOB_PARAMS_TAMPERED, never executable."""
    task = job.get("task")
    params = job.get("params")
    if not isinstance(task, str) or not isinstance(params, dict):
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            "job record carries no usable task/params payload",
        )
    try:
        revalidated = _validated_task_params(task, dict(params))
    except (WorkerError, ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
        # the params contract boundary: every damage shape (missing keys,
        # wrong types, empty strings, unreadable paths, normalization
        # failures) converges to the ONE stable code - never a leaked
        # raw exception; the message names the failure kind, never the
        # full path payload
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            f"archived params fail the formal validation ({type(exc).__name__})",
        ) from exc
    if revalidated != params:
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            "archived params are not in the canonical normalized form",
        )
    recomputed = hashlib.sha256(_canonical_params(task, revalidated).encode("utf-8")).hexdigest()[
        :32
    ]
    if job.get("params_key") != recomputed:
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            "archived params do not match the archived params_key",
        )


def _is_formal_t26_jobs_dir(jobs_dir: Path | str) -> bool:
    """THE formal T26 jobs root boundary: a jobs_dir whose last two
    components are exactly .capacity-credit/jobs (lexical, case-normal)."""
    parts = [os.path.normcase(part) for part in Path(jobs_dir).parts]
    return len(parts) >= 2 and parts[-2] == ".capacity-credit" and parts[-1] == "jobs"


def _is_legacy_t25_job_id(value: Any) -> bool:
    """THE legacy T25 job-id contract - exactly what the original
    create_job produces: a canonical str(uuid.uuid4()) with version 4 and
    the RFC variant (verified via the standard-library parse; the
    normalized string must round-trip EXACTLY). Anything else (a
    deterministic job-<32hex>, an arbitrary name) is NOT a legacy id."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    if str(parsed) != value:
        return False
    if parsed.version != 4:
        return False
    return parsed.variant == uuid.RFC_4122


def _verify_bound_task_identity(
    job: dict[str, Any], *, jobs_dir: Path | str | None = None
) -> None:
    """THE legacy-vs-identity-bound distinction (single rule used by the
    ensure attach, the ensure creation path AND execute_job): ONLY a job
    with NO task_identity field at all AND a legacy (non-deterministic)
    T25 job id may take the legacy path. A field present but null, a
    deterministic job-<32-hex> id missing the field, or an identity-bound
    job missing any binding field are all stable refusals - deleting or
    nulling task_identity (and recomputing params_key) can NEVER
    downgrade a T26 job.

    For an identity-bound job the binding fields
    (case_id/config_sha256/source_software_commit) are MANDATORY, the
    full identity is recomputed from the formal canonical payload (the
    single recipe in business_demo) and must equal the archived identity,
    and the deterministic job_id must match."""
    if "task_identity" not in job:
        # the FORMAL T26 jobs root is the trusted boundary: a demo job
        # there MUST be identity-bound - a UUID demo (including a
        # whole-directory-renamed one) is NEVER a legacy there. Only in a
        # NON-formal caller-supplied jobs root may a canonical T25 UUID4
        # job (version 4, RFC variant) take the legacy path.
        if (
            jobs_dir is not None
            and _is_formal_t26_jobs_dir(jobs_dir)
            and job.get("task") == _DEMO_TASK
        ):
            raise WorkerError(
                _WORKER_JOB_IDENTITY_CONFLICT,
                f"demo job {job.get('job_id')!r} in the formal T26 jobs root "
                "must be identity-bound; a UUID legacy demo is never "
                "executed there",
            )
        job_id = job.get("job_id")
        if not _is_legacy_t25_job_id(job_id):
            raise WorkerError(
                _WORKER_JOB_IDENTITY_CONFLICT,
                f"job {job_id!r} carries no task_identity field and is not a "
                "canonical T25 (uuid4) job id; refusing the legacy downgrade",
            )
        return  # a legacy T25 (uuid-id) job: nothing was ever bound
    task_identity = job.get("task_identity")
    if job.get("task") != _DEMO_TASK:
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            "the task_identity contract is defined for the T26-A teaching "
            f"demo task only, not {job.get('task')!r}",
        )
    if (
        not isinstance(task_identity, str)
        or len(task_identity) != 64
        or any(char not in "0123456789abcdef" for char in task_identity)
    ):
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            "the archived task_identity is null or malformed",
        )
    params = job.get("params")
    record: dict[str, Any] = params if isinstance(params, dict) else {}
    case_id = record.get("case_id")
    config_sha256 = record.get("config_sha256")
    commit = record.get("source_software_commit")
    if not (
        isinstance(case_id, str)
        and case_id
        and isinstance(config_sha256, str)
        and len(config_sha256) == 64
        and all(char in "0123456789abcdef" for char in config_sha256)
        and isinstance(commit, str)
        and len(commit) == 40
        and all(char in "0123456789abcdef" for char in commit)
    ):
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            "an identity-bound demo job is missing its binding fields or "
            "carries a malformed one (case_id non-empty; config_sha256 a "
            "64-hex; source_software_commit a full 40-hex Git commit)",
        )
    from capacity_credit.demo.business_demo import compute_teaching_task_identity

    recomputed = compute_teaching_task_identity(case_id, config_sha256, commit)
    if recomputed != task_identity:
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            "the binding fields do not recompute to the archived task identity",
        )
    if _identity_job_id(task_identity) != job.get("job_id"):
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            "the archived task identity does not match the deterministic job id",
        )


def _verify_bound_output_dir(
    record: dict[str, Any], raw_output_dir: Any, jobs_dir: Path | str
) -> None:
    """THE identity-bound workspace/output closure (T26-A demo jobs only;
    legacy create_job never reaches this), in the FIXED order:

    1. LEXICAL structure and containment (never resolve-follows):
       jobs_dir must be an ABSOLUTE, lexically exact
       <workspace>/.capacity-credit/jobs; case_id must be a single safe
       filename component (safe_filename_component verbatim - no
       absolute path, '.', '..', separators, drive colons or any
       whitespace); the ONE expected output directory is derived and must
       stay STRICTLY inside the workspace (normpath equality alone never
       proves containment);
    2. every EXISTING component of the output path chain must be a real
       directory (symlink/junction/other reparse point fail-closed);
    3. only then the normalization re-proof: the stored (resolved) form
       must equal the lexical form of the caller-supplied path - a
       junction would have escaped during resolve() and lost the
       workspace origin.

    A junction-resolved or traversal-escaped external path is NEVER
    stored in the job record. Called at creation, attach/find, launch
    and execute - the archived output directory is re-verified every
    time."""
    params = record.get("params")
    output = params.get("output_dir") if isinstance(params, dict) else None
    case_id = params.get("case_id") if isinstance(params, dict) else None
    identity = record.get("task_identity")
    if (
        not isinstance(output, str)
        or not output
        or not isinstance(case_id, str)
        or not isinstance(identity, str)
    ):
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "an identity-bound demo job carries no usable output_dir/case_id",
        )
    # 1. lexical structure and containment (no resolve-follows)
    jobs_path = Path(jobs_dir)
    jobs_parts = [os.path.normcase(part) for part in jobs_path.parts]
    if (
        not jobs_path.is_absolute()
        or len(jobs_parts) < 2
        or jobs_parts[-2] != ".capacity-credit"
        or jobs_parts[-1] != "jobs"
    ):
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "jobs_dir must be exactly <workspace>/.capacity-credit/jobs "
            "for an identity-bound demo job",
        )
    # case_id is a single safe filename component (no traversal, no
    # whitespace); safe_filename_component raises ValueError on violation
    try:
        safe_filename_component(case_id)
    except ValueError as exc:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"case_id is not a single safe filename component: {exc}",
        ) from exc
    if not case_id.strip() or any(char.isspace() for char in case_id):
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "case_id must not contain whitespace",
        )
    workspace = jobs_path.parent.parent
    out_path = Path(output)
    expected = workspace / "teaching-results" / case_id / identity
    # containment: the ONE expected directory must stay STRICTLY inside
    # the workspace (a traversal can make normpath equality hold while
    # the result escapes)
    workspace_key = os.path.normcase(os.path.normpath(str(workspace)))
    expected_key = os.path.normcase(os.path.normpath(str(expected)))
    if expected_key == workspace_key or not expected_key.startswith(
        workspace_key + os.sep
    ):
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "the expected output directory escapes the workspace",
        )
    if os.path.normcase(os.path.normpath(str(out_path))) != expected_key:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "output_dir is not the workspace-derived directory of the same "
            "workspace as jobs_dir (an external directory with a correct "
            "tail shape is still refused)",
        )
    # 2. every EXISTING component of the output chain must be reparse-free
    for part in (out_path, *out_path.parents):
        reason = unsafe_path_component(part, directory=True)
        if reason is not None:
            raise WorkerError(
                _WORKER_JOB_PATH_UNSAFE,
                f"unsafe output path component ({reason}): {part}",
            )
    # 3. normalization re-proof: no link may have been followed
    lexical = os.path.normcase(os.path.normpath(str(raw_output_dir)))
    if not raw_output_dir or lexical != os.path.normcase(output):
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "output_dir normalization followed a link (the junction origin "
            "would have been lost)",
        )


def _assert_safe_request_store(store: ControlRequestStore) -> None:
    """THE shared stateless leaf-level safety for one job's control-request
    store - used at EVERY actual read/write boundary (ensure/find start-
    request load, launch load/update, execute load/update, stop polling
    list/update, stop_job submit, terminal settlement list/update): the
    control/ directory and every EXISTING *.json leaf must be REAL (never
    a link/reparse point). ControlRequestStore remains the ONLY request
    state contract - this checks PATHS only (no state transition logic
    here)."""
    reason = unsafe_path_component(store.directory, directory=True)
    if reason is not None:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"unsafe control-request directory ({reason}): {store.directory}",
        )
    if not store.directory.is_dir():
        return
    for path in sorted(store.directory.glob("*.json")):
        leaf_reason = unsafe_path_component(path, directory=False)
        if leaf_reason is not None:
            raise WorkerError(
                _WORKER_JOB_PATH_UNSAFE,
                f"unsafe control-request leaf ({leaf_reason}): {path}",
            )


def _safe_request_list(store: ControlRequestStore) -> list[dict[str, Any]]:
    """Safe list: control dir + every existing request leaf must be real
    (never links) BEFORE the enumeration runs."""
    _assert_safe_request_store(store)
    return store.list_requests()


def _safe_request_load(store: ControlRequestStore, request_id: str) -> dict[str, Any]:
    """Safe load: control dir + every existing request leaf must be real
    BEFORE the load runs."""
    _assert_safe_request_store(store)
    return store.load(request_id)


def _safe_request_update(
    store: ControlRequestStore, request_id: str, **kwargs: Any
) -> dict[str, Any]:
    """Safe update: control dir + every existing request leaf AND the
    exact target leaf must be real BEFORE the update runs."""
    _assert_safe_request_store(store)
    reason = unsafe_path_component(store.path_for(request_id), directory=False)
    if reason is not None:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"unsafe control-request target leaf ({reason}): "
            f"{store.path_for(request_id)}",
        )
    return store.update(request_id, **kwargs)


def _safe_request_submit(
    store: ControlRequestStore, request: ControlRequest
) -> dict[str, Any]:
    """Safe submit: control dir + every existing request leaf AND the
    exact target leaf (which may legitimately not exist yet) must be
    real BEFORE the submit runs."""
    _assert_safe_request_store(store)
    reason = unsafe_path_component(store.path_for(request.request_id), directory=False)
    if reason is not None:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"unsafe control-request target leaf ({reason}): "
            f"{store.path_for(request.request_id)}",
        )
    return store.submit(request)


def _load_verified_job_evidence(
    jobs_dir: Path | str,
    job_id: str,
    *,
    task_identity: str,
    task: str | None = None,
    params_key: str | None = None,
) -> dict[str, Any]:
    """THE shared READ-ONLY identity-job evidence validator (the ensure_job
    attach path AND find_job_by_identity - single implementation, reusing
    the existing ControlRequest contract and the T25 state invariants,
    never a second state machine). Fail-closed checks:

    - the job record parses and carries EXACTLY the full 64-hex task
      identity (a legacy record or a 32-hex prefix collision is the stable
      WORKER_JOB_IDENTITY_CONFLICT, never a fake 'task absent') and, when
      given, matches the caller's task and canonical params_key;
    - the ARCHIVED params re-validate/re-normalize through the shared
      formal entry and recompute to the archived params_key
      (WORKER_JOB_PARAMS_TAMPERED otherwise);
    - the start request exists, parses against the ControlRequest
      contract, IS the recorded start request (action/request_id) and
      mutually agrees with the job record (requested_by);
    - the job state is a known state COMPATIBLE with the start request's
      formal status: queued/running allow a pending or accepted start
      request; a terminal job requires the formal settlement evidence -
      EVERY request terminally settled, the start request's
      result.job_state == the job state, completed/cancelled settle the
      start request to completed, failed/blocked to rejected with a
      non-empty diagnostic.

    Anything else is the stable WORKER_JOB_CREATION_INCOMPLETE; the
    evidence is never overwritten, deleted or auto-rebuilt."""
    try:
        record = _read_job(jobs_dir, job_id)
    except WorkerError as exc:
        raise WorkerError(
            _WORKER_JOB_CREATION_INCOMPLETE,
            f"job record unreadable for identity evidence: {exc}",
        ) from exc
    existing_identity = record.get("task_identity")
    if not isinstance(existing_identity, str):
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            f"job {job_id} predates the task_identity contract; "
            "refusing to attach or migrate a legacy record",
        )
    if existing_identity != task_identity:
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            f"job {job_id} carries a different full task identity "
            "(deterministic prefix collision); refusing to attach",
        )
    if task is not None and record.get("task") != task:
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            f"job {job_id} task mismatch: {record.get('task')!r} != {task!r}",
        )
    _verify_job_params_integrity(record)
    if params_key is not None and record.get("params_key") != params_key:
        raise WorkerError(
            _WORKER_JOB_IDENTITY_CONFLICT,
            f"job {job_id} params_key mismatch for the same task identity",
        )
    # an identity-bound job must carry its FULL binding fields and they
    # must recompute to the archived identity (never a legacy downgrade)
    _verify_bound_task_identity(record, jobs_dir=jobs_dir)
    # and the archived output directory must still close over the SAME
    # workspace as this jobs_dir (a swapped plain external directory with
    # the correct tail shape is refused)
    _verify_bound_output_dir(
        record,
        record["params"].get("output_dir") if isinstance(record.get("params"), dict) else None,
        jobs_dir,
    )
    state = record.get("state")
    if state not in ("queued", "running", "completed", "failed", "cancelled", "blocked"):
        raise WorkerError(
            _WORKER_JOB_CREATION_INCOMPLETE,
            f"job {job_id} carries an unknown state: {state!r}",
        )
    request_id = record.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise WorkerError(
            _WORKER_JOB_CREATION_INCOMPLETE,
            f"job {job_id} carries no usable start request id",
        )
    store = _control_store(jobs_dir, job_id)
    # leaf-level safety BEFORE any load: the control/ directory and every
    # existing request leaf must be real files (never links)
    _assert_safe_request_store(store)
    try:
        start_request = _safe_request_load(store, request_id)
    except ServiceError as exc:
        if exc.code in ("CONTROL_REQUEST_NOT_FOUND", "CONTROL_REQUEST_INVALID"):
            raise WorkerError(
                _WORKER_JOB_CREATION_INCOMPLETE,
                f"job {job_id} start request evidence incomplete: {exc}",
            ) from exc
        raise
    if (
        start_request.get("action") != "start"
        or start_request.get("request_id") != request_id
        or start_request.get("requested_by") != record.get("requested_by")
    ):
        raise WorkerError(
            _WORKER_JOB_CREATION_INCOMPLETE,
            f"job {job_id} and its start request are inconsistent evidence",
        )
    request_status = start_request.get("request_status")
    if state in ("queued", "running"):
        if request_status not in ("pending", "accepted"):
            raise WorkerError(
                _WORKER_JOB_CREATION_INCOMPLETE,
                f"job {job_id} is {state} but its start request is {request_status!r}",
            )
        return record
    # a terminal job REQUIRES the formal settlement evidence - THE SAME
    # precise rules as recover_terminal_job (T25), never a looser second
    # state machine: every request terminally settled, a valid frozen
    # intent bound to THIS job, the intent target == the job state, the
    # exact settled-request consistency (misbound start/resume, illegal
    # honoured stop, result.job_state/diagnostic mismatches) and the job
    # diagnostics agreeing with the frozen diagnostic. All failures are
    # READ-ONLY (nothing rewritten, deleted or 'repaired').
    history = _safe_request_list(store)  # transient IO errors propagate unchanged
    for payload in history:
        if payload.get("invalid"):
            raise WorkerError(
                _WORKER_JOB_CREATION_INCOMPLETE,
                f"job {job_id} has corrupt control-request evidence: "
                f"{payload.get('request_id')}",
            )
        if payload.get("request_status") not in ("completed", "rejected"):
            raise WorkerError(
                _WORKER_JOB_CREATION_INCOMPLETE,
                f"job {job_id} is terminal ({state}) but request "
                f"{payload.get('request_id')} is {payload.get('request_status')!r}",
            )
    # the frozen intent is mandatory (every formal terminal publish
    # persists it first); the T25 stable codes propagate unchanged
    intent = _load_terminal_intent(jobs_dir, job_id)
    _verify_intent_identity(intent, record, job_id)
    if intent["fixed_target_state"] != state:
        raise WorkerError(
            _WORKER_TERMINAL_STATE_CONFLICT,
            f"job {job_id} is terminal as {state} but the frozen intent "
            f"targets {intent['fixed_target_state']}",
        )
    _check_settled_requests_consistent(history, intent, record, job_id)
    diagnostic = intent.get("diagnostic_code")
    if diagnostic is not None and diagnostic not in (record.get("diagnostics") or []):
        raise WorkerError(
            _WORKER_JOB_CREATION_INCOMPLETE,
            f"job {job_id} diagnostics do not carry the frozen terminal "
            f"diagnostic {diagnostic}",
        )
    return record


def _verify_demo_execution_identity(params: dict[str, Any]) -> None:
    """T26-A identity-bound demo jobs ONLY (params carrying
    config_sha256 / source_software_commit): re-verify the bound config
    hash and software commit BEFORE any compute or artifact write - a
    config or HEAD drift between ensure and execution is a stable refusal
    (the config bytes are read ONCE and hashed from those same bytes; an
    unreadable config is the same stable refusal, never a raw OSError).
    Legacy jobs without these fields keep the T25 behavior."""
    expected_hash = params.get("config_sha256")
    expected_commit = params.get("source_software_commit")
    if expected_hash is None and expected_commit is None:
        return  # a legacy T25 job: nothing was bound
    if expected_hash is not None:
        try:
            config_bytes = Path(str(params["config"])).read_bytes()
        except OSError as exc:
            raise WorkerError(
                _WORKER_JOB_CONFIG_CHANGED,
                f"the bound demo config is unreadable at execution: {exc}",
            ) from exc
        if hashlib.sha256(config_bytes).hexdigest() != expected_hash:
            raise WorkerError(
                _WORKER_JOB_CONFIG_CHANGED,
                "the demo config changed between ensure and execution",
            )
    if expected_commit is not None:
        from capacity_credit.demo.business_demo import DemoError, source_identity

        try:
            head = source_identity()["head"]
        except DemoError as exc:
            raise WorkerError(
                _WORKER_JOB_COMMIT_CHANGED,
                f"the software identity is unavailable at execution ({exc.code})",
            ) from exc
        if head != expected_commit:
            raise WorkerError(
                _WORKER_JOB_COMMIT_CHANGED,
                "the software HEAD changed between ensure and execution",
            )


def ensure_job(
    jobs_dir: Path | str,
    *,
    task_identity: str,
    task: str,
    params: dict[str, Any],
    requested_by: str,
) -> EnsureJobResult:
    """THE atomic ensure/attach contract (T26-A): for one deterministic
    task identity there is EXACTLY ONE job and ONE start request, across
    reruns, reconnects and concurrent processes.

    - requested_by must be a non-empty string BEFORE anything is
      created (WORKER_JOB_REQUESTED_BY_INVALID; a non-string is never
      str()-laundered);
    - every EXISTING worker-controlled path component (jobs_dir, locks/,
      locks/job-intents/, the deterministic job dir) must be a real
      directory - a reparse point/link or a file-instead-of-directory is
      WORKER_JOB_PATH_UNSAFE, checked BEFORE any creation;
    - identity absent: create the deterministic job + its pending start
      request under the short-lived ensure gate (created=True,
      WORKER_JOB_CREATED); the gate is keyed by the STORAGE job_id, so
      prefix-colliding identities serialize on the SAME gate;
    - same identity + same task + same canonical params + consistent
      evidence: return the existing job unchanged (created=False,
      WORKER_JOB_EXISTS; the original requested_by audit is NEVER
      overwritten);
    - same identity + different task/params_key, or a record carrying a
      DIFFERENT full 64-hex identity at the deterministic path (prefix
      collision): WORKER_JOB_IDENTITY_CONFLICT, nothing written;
    - a legacy record WITHOUT task_identity at the deterministic path is
      never attached or migrated (stable conflict);
    - archived params that fail re-validation or recompute to a
      different hash are WORKER_JOB_PARAMS_TAMPERED (never executed);
    - incomplete or inconsistent evidence (job dir without job.json,
      corrupt job.json, missing/corrupt/inconsistent start request, a
      terminal job without the formal settlement evidence):
      WORKER_JOB_CREATION_INCOMPLETE - never overwritten, deleted or
      auto-rebuilt;
    - the gate never covers spawn/compute; ensure never launches, never
      spawns and never runs a runner.
    """
    task_identity = _validate_task_identity(task_identity)
    # requested_by is validated BEFORE any directory or file is created;
    # a non-string is never str()-laundered into the audit field
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise WorkerError(
            _WORKER_JOB_REQUESTED_BY_INVALID,
            "requested_by must be a non-empty audit identity string",
        )
    # captured BEFORE the shared normalization: the identity-bound output
    # constraint must prove no link was followed during resolve()
    raw_output_dir = params.get("output_dir") if isinstance(params, dict) else None
    params = _validated_task_params(task, params)
    params_key = hashlib.sha256(_canonical_params(task, params).encode("utf-8")).hexdigest()[:32]
    job_id = _identity_job_id(task_identity)
    # the FORMAL jobs root is validated LEXICALLY before the gate ever
    # creates locks/job-intents content (relative/lexically-dirty/wrong-
    # tail paths are refused with zero writes)
    _assert_formal_jobs_dir_lexical(jobs_dir)
    # fail-closed path safety BEFORE anything is created (even the gate
    # acquisition creates the locks namespace)
    _assert_safe_control_tree(jobs_dir, job_id)
    gate = _ensure_gate(jobs_dir, job_id)
    _acquire_ensure_gate(gate, job_id)
    try:
        # re-check INSIDE the gate: a swap during the bounded gate wait is
        # still caught before any read or create
        _assert_safe_control_tree(jobs_dir, job_id)
        job_file = _job_file(jobs_dir, job_id)
        if not job_file.is_file():
            if _job_dir(jobs_dir, job_id).exists():
                raise WorkerError(
                    _WORKER_JOB_CREATION_INCOMPLETE,
                    f"job directory exists but the job record is missing: {job_id}",
                )
            record = _job_record(
                job_id, str(uuid.uuid4()), task, params, requested_by, task_identity
            )
            # THE identity verification BEFORE the first write: the
            # complete candidate record must pass THE SAME binding rule as
            # attach/execute (task, binding fields, full identity,
            # deterministic job_id) AND the workspace-origin directory
            # contract - never write first and let find/execute discover
            # the inconsistency
            _verify_bound_task_identity(record, jobs_dir=jobs_dir)
            _verify_bound_output_dir(record, raw_output_dir, jobs_dir)
            _persist_new_job(jobs_dir, record)
            return EnsureJobResult(job_id=job_id, created=True, code="WORKER_JOB_CREATED")
        _load_verified_job_evidence(
            jobs_dir, job_id, task_identity=task_identity, task=task, params_key=params_key
        )
        return EnsureJobResult(job_id=job_id, created=False, code="WORKER_JOB_EXISTS")
    finally:
        gate.release()


def find_job_by_identity(jobs_dir: Path | str, task_identity: str) -> JobSummary | None:
    """PURE READ-ONLY identity lookup (never creates a directory, never
    writes, never acquires a lock, never resolve-follows a link): the
    JobSummary of the job whose record carries EXACTLY this full 64-hex
    task identity, or None when NO trace of the identity exists.
    Fail-closed through the SAME shared evidence validator as ensure_job:
    unsafe controlled paths are WORKER_JOB_PATH_UNSAFE; incomplete or
    inconsistent evidence is WORKER_JOB_CREATION_INCOMPLETE; a legacy
    record or a 32-hex prefix collision at the deterministic path is
    WORKER_JOB_IDENTITY_CONFLICT - never a fake 'task absent'."""
    task_identity = _validate_task_identity(task_identity)
    root = _jobs_root(jobs_dir)
    if not root.exists() and not root.is_symlink() and not root.is_junction():
        return None
    job_id = _identity_job_id(task_identity)
    _assert_safe_control_tree(root, job_id)
    if not _job_file(root, job_id).is_file():
        if _job_dir(root, job_id).exists():
            raise WorkerError(
                _WORKER_JOB_CREATION_INCOMPLETE,
                f"job directory exists but the job record is missing: {job_id}",
            )
        return None
    record = _load_verified_job_evidence(root, job_id, task_identity=task_identity)
    return _job_summary(root, job_id, record)


def _control_gate(jobs_dir: Path | str, job_id: str) -> RunLock:
    """THE short-lived cross-process control-plane atomic gate for one
    job (the existing RunLock in the jobs lock namespace, keyed
    control-<job_id> - distinct from the adequacy-execution lock, the
    RunService lifecycle lock and the worker task lock, so no
    re-entrancy is possible). It covers ONLY the stop-submit state
    re-check + atomic submit and the terminal settle + terminal progress
    + terminal job publish - never any compute."""
    return RunLock(
        _jobs_root(jobs_dir) / "locks", f"control-{safe_filename_component(job_id)}"
    )


def _acquire_control_gate(lock: RunLock, job_id: str) -> None:
    """Bounded wait for the short-lived control-plane gate (the holder
    only ever performs sub-second atomic file writes)."""
    deadline = time.monotonic() + _CONTROL_GATE_WAIT_SECONDS
    while True:
        if lock.acquire():
            return
        if time.monotonic() >= deadline:
            raise WorkerError(
                _WORKER_CONTROL_GATE_BUSY,
                f"control-plane gate for job {job_id} busy beyond "
                f"{_CONTROL_GATE_WAIT_SECONDS}s",
            )
        time.sleep(_CONTROL_GATE_POLL_SECONDS)


def _lock_key(jobs_dir: Path | str, job: dict[str, Any]) -> str:
    # the WORKER task lock (jobs namespace) exists only as the launch/
    # worker-level single-flight helper; run-level concurrency authority
    # is the project execution lock (adequacy-execution namespace)
    return f"task-{job['params_key']}"


_MONITOR_TOOL = Path(__file__).resolve().parents[3] / "tools" / "run_long_validation.py"


def _rollback_launch_claim(claim: Path, expected_token: str | None) -> None:
    """Delete ONLY the claim created by THIS launch call, with the
    check-then-unlink window CLOSED by an atomic quarantine rename:

    1. rename the claim (whatever it now contains) to a PRIVATE
       quarantine name in the same directory - the rename is atomic, so
       the file we inspect is exactly the file we remove;
    2. verify the quarantined payload: the 'launch' key AND the
       in-memory per-creation claim_token must both match;
    3. match -> remove the quarantine; mismatch (a foreign, replaced,
       old-format or damaged claim won the race) -> rename it BACK
       untouched - foreign evidence is never deleted;
    4. any rename/unlink failure surfaces explicitly (fail-closed)."""
    if expected_token is None:
        return  # no in-memory ownership evidence: never delete blindly
    quarantine = claim.with_name(f"{claim.name}.rollback-{uuid.uuid4().hex}.tmp")
    try:
        os.rename(claim, quarantine)
    except OSError:
        return  # nothing verifiable at the path (already gone) - nothing of ours to remove
    owned = False
    try:
        payload = json.loads(quarantine.read_bytes().decode("utf-8"))
        owned = (
            isinstance(payload, dict)
            and payload.get("request_id") == "launch"
            and payload.get("claim_token") == expected_token
        )
    except (OSError, ValueError):
        owned = False
    if owned:
        try:
            quarantine.unlink()
        except OSError as exc:
            raise WorkerError(
                _WORKER_JOB_PATH_UNSAFE,
                f"reclaimed launch claim could not be removed: {quarantine} ({exc})",
            ) from exc
        return
    try:
        os.rename(quarantine, claim)  # restore the foreign evidence untouched
    except OSError as exc:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"foreign launch claim quarantine restore failed: {quarantine} ({exc})",
        ) from exc


def launch(
    jobs_dir: Path | str,
    job_id: str,
    *,
    monitor_interval_seconds: float = 5.0,
) -> dict[str, Any]:
    """Claim the job ONCE, start the DETACHED existing two-layer monitor
    (``tools/run_long_validation.py`` - never a copied sampler) which in
    turn spawns the real one-shot worker subprocess, then return
    immediately - the parent CLI/page process may exit; the monitor and
    the worker keep running.

    The job record distinguishes ``monitor_pid`` (the detached monitor)
    from ``worker_pid`` (self-reported by the worker once running) - a
    single ``pid`` never conflates the two. The launch claim (atomic
    O_EXCL marker) only prevents the SAME job from being spawned twice;
    run-level concurrency is the execution lock's job.
    """
    # path safety BEFORE reading any evidence or spawning anything (a
    # post-ensure junction/file swap is the stable WORKER_JOB_PATH_UNSAFE)
    _assert_safe_control_tree(jobs_dir, job_id)
    job = _read_job(jobs_dir, job_id)
    if job.get("state") not in ("queued", "running"):
        raise WorkerError(
            "WORKER_JOB_STATE_INVALID",
            f"job {job_id} is {job.get('state')}; only a queued/running job can launch",
        )
    # identity-bound jobs re-verify the binding and the archived output
    # closure BEFORE any spawn (legacy jobs keep the T25 behavior)
    _verify_bound_task_identity(job, jobs_dir=jobs_dir)
    if "task_identity" in job:
        _verify_bound_output_dir(
            job,
            job["params"].get("output_dir") if isinstance(job.get("params"), dict) else None,
            jobs_dir,
        )
    store = _control_store(jobs_dir, job_id)
    # request-leaf safety BEFORE the claim (control tree + every existing
    # request leaf) - a refusal here can never leave a claim behind
    _assert_safe_request_store(store)
    try:
        claimed = atomic_claim_with_token(_job_dir(jobs_dir, job_id), "launch")
    except CorruptClaimError as exc:
        # a zero-byte, partial, damaged or old-format claim file is NEVER
        # a successful launch claim - fail closed instead of treating the
        # job as already launched
        raise WorkerError(
            "WORKER_JOB_INVALID",
            f"the launch claim evidence is damaged; refusing to launch: {exc}",
        ) from exc
    if claimed is None:
        # idempotent: the same job has already been launched - never a
        # second spawn, never a pid overwrite
        return _read_job(jobs_dir, job_id)
    claim, claim_token = claimed
    # the FINAL re-check AFTER the claim and BEFORE the monitor log open
    # and the spawn: a swap in between is still caught, and ONLY this
    # call's identity-verified claim is rolled back (the in-memory token
    # proves ownership - never a historical or foreign claim, never
    # job/control/monitor evidence)
    try:
        _assert_safe_control_tree(jobs_dir, job_id)
        _assert_safe_request_store(store)
    except BaseException:
        _rollback_launch_claim(claim, claim_token)
        raise
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    monitor_dir = _job_dir(jobs_dir, job_id) / "monitor"
    worker_argv = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "capacity_credit.application.worker",
        "run",
        "--job-id",
        job_id,
        "--jobs-dir",
        str(_jobs_root(jobs_dir).resolve()),
    ]
    monitor_argv = [
        sys.executable,
        "-X",
        "utf8",
        str(_MONITOR_TOOL),
        "--run-id",
        job_id,
        "--case-id",
        f"t25-{job['task']}",
        "--side",
        "worker",
        "--interval-seconds",
        str(monitor_interval_seconds),
        "--monitor-root",
        str(monitor_dir),
        "--",
        *worker_argv,
    ]
    monitor_log = _job_dir(jobs_dir, job_id) / "monitor-output.log"
    try:
        with open(monitor_log, "ab") as log_handle:
            process = subprocess.Popen(
                monitor_argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                close_fds=True,
                creationflags=creationflags,
            )
    except BaseException as exc:
        # launch failure: the start request is stably REJECTED (never
        # stuck accepted) with the stable diagnostic BEFORE the job
        # terminal state is published (the single shared terminal entry)
        try:
            _publish_terminal_state(
                jobs_dir,
                job_id,
                store,
                job,
                state="failed",
                diagnostic_code="WORKER_LAUNCH_FAILED",
                honoured=False,
                result={"detail": str(exc)[:300]},
                progress=None,
                job_fields={"diagnostics": ["WORKER_LAUNCH_FAILED"]},
            )
        except _TerminalPublishError as publish_exc:
            print(
                f"WORKER_LAUNCH_FAILED: job {job_id} terminal publish persistence "
                f"unavailable ({publish_exc.cause}); 持久化诊断不可用",
                file=sys.stderr,
                flush=True,
            )
        raise WorkerError("WORKER_LAUNCH_FAILED", f"worker spawn failed: {exc}") from exc
    try:
        updated = _update_job(
            jobs_dir, job_id, monitor_pid=process.pid, state="running", worker_pid=None
        )
        stored = _safe_request_load(store, str(job["request_id"]))
        if stored.get("request_status") == "pending":
            _safe_request_update(store, str(job["request_id"]), status="accepted")
    except BaseException as exc:
        # a post-spawn failure: FIRST reclaim the spawned monitor/worker
        # process tree and REQUIRE convergence evidence - a failed or
        # unverifiable reclaim NEVER publishes a "safely failed" terminal
        # (the job stays running; no state may claim the worker stopped
        # while it may still be writing results)
        evidence = _reclaim_spawned_process_tree(process)
        if not evidence["converged"]:
            raise WorkerError(
                "WORKER_LAUNCH_FAILED",
                "spawned monitor/worker process tree did not converge: "
                f"{evidence['detail']}",
            ) from exc
        code = (
            exc.code
            if isinstance(exc, (WorkerError, ServiceError))
            else "WORKER_TASK_FAILED"
        )
        try:
            _publish_terminal_state(
                jobs_dir,
                job_id,
                store,
                {**job, "state": "failed"},
                state="failed",
                diagnostic_code=code,
                honoured=False,
                result={"detail": str(exc)[:300]},
                progress=None,
                job_fields={"diagnostics": [code]},
            )
        except _TerminalPublishError:
            # the terminal evidence could not settle (e.g. the same
            # corrupted leaf): the job stays recoverable via
            # recover-terminal; the original error still propagates
            pass
        raise
    return updated


def _assert_formal_jobs_dir_lexical(jobs_dir: Path | str) -> None:
    """The identity-bound T26 demo contract requires the FORMAL jobs root:
    an ABSOLUTE, lexically clean <workspace>/.capacity-credit/jobs - a
    relative path, a lexical '.'/'..' component or a wrong tail are
    refused BEFORE the ensure gate ever creates locks/job-intents
    content (the directory stays byte-identical)."""
    path = Path(jobs_dir)
    text = str(jobs_dir)
    if (
        not path.is_absolute()
        or os.path.normcase(os.path.normpath(text)) != os.path.normcase(text)
    ):
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "jobs_dir must be an absolute, lexically clean "
            "<workspace>/.capacity-credit/jobs path",
        )
    parts = [os.path.normcase(part) for part in path.parts]
    if len(parts) < 2 or parts[-2] != ".capacity-credit" or parts[-1] != "jobs":
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            "jobs_dir must end with the formal .capacity-credit/jobs root",
        )


def _verify_formal_resume_identity(
    jobs_dir: Path | str, job: dict[str, Any], params: dict[str, Any]
) -> None:
    """A FORMAL resume identity must come from existing, cross-verifiable
    job/request/run evidence - never from a bare params.resume_run_id:

    - the record must carry resumed_from_job AND run_id == resume_run_id;
    - the SOURCE job must exist, corroborate the same run_id and task,
      and its recorded params keys bound the allowed field set;
    - re-injecting the crash hook is always tampering.
    Every refusal here happens BEFORE the pipeline, the runner, any
    os._exit hook or any result write."""
    run_id = params.get("resume_run_id")
    source_id = job.get("resumed_from_job")
    if (
        not isinstance(run_id, str)
        or not run_id
        or job.get("run_id") != run_id
        or not isinstance(source_id, str)
        or not source_id
    ):
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            "resume_run_id without a matching record run_id and "
            "resumed_from_job is a forged resume identity",
        )
    if "test_crash_marker" in params:
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            "resume params re-inject the crash hook (resume_job strips it "
            "at creation)",
        )
    source_file = _job_dir(jobs_dir, source_id) / "job.json"
    reason = unsafe_path_component(source_file, directory=False)
    if reason is not None:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"unsafe resume source leaf ({reason}): {source_file}",
        )
    if not source_file.is_file():
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            f"resume source job evidence missing: {source_id}",
        )
    try:
        source = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            f"resume source job evidence unreadable: {source_id}",
        ) from exc
    if (
        not isinstance(source, dict)
        or source.get("run_id") != run_id
        or source.get("task") != job.get("task")
    ):
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            "resume source job does not corroborate the resume identity",
        )
    source_params = source.get("params")
    allowed: set[str] = set(source_params.keys()) | {"resume_run_id"} if isinstance(
        source_params, dict
    ) else {"resume_run_id"}
    extra = set(params.keys()) - allowed
    if extra:
        raise WorkerError(
            _WORKER_JOB_PARAMS_TAMPERED,
            f"resume params carry fields absent from the source job: {sorted(extra)}",
        )


def _discard_resume_staging(
    staging: Path, resume_job_id: str, resume_request_id: str
) -> None:
    """Discard ONLY this call's resume staging directory: the staged
    job.json must parse AND carry exactly this call's resume_job_id and
    resume_request_id before anything is removed. A missing/foreign/
    unreadable staging content is NEVER deleted (fail-closed, left for
    manual handling) and a deletion failure (e.g. PermissionError)
    surfaces to the caller - nothing is silently swallowed."""
    job_file = staging / "job.json"
    if not job_file.is_file():
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"resume staging carries no job.json; refusing a blind delete: {staging}",
        )
    try:
        payload = json.loads(job_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"resume staging job.json is unreadable; refusing to delete: {staging}",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("job_id") != resume_job_id
        or payload.get("request_id") != resume_request_id
    ):
        raise WorkerError(
            _WORKER_JOB_PATH_UNSAFE,
            f"resume staging content is not this call's; refusing to delete: {staging}",
        )
    shutil.rmtree(staging)  # deletion failures surface explicitly


_TASKKILL_TIMEOUT_S = 5.0
_RECLAIM_SETTLE_TIMEOUT_S = 5.0


def _taskkill_command(pid: int) -> list[str]:
    """The tree-kill command (seam for bounded-timeout proofs)."""
    return ["taskkill", "/T", "/F", "/PID", str(pid)]


def _pid_alive(pid: int) -> bool:
    """Windows-safe liveness probe (os.kill(pid, 0) is a Unix idiom)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _terminate_pid(pid: int) -> bool:
    """Terminate ONE process; False when it could not be terminated -
    callers record the failure, never swallow it."""
    if os.name != "nt":
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _snapshot_entries() -> list[tuple[int, int]]:
    """One fresh Toolhelp32 snapshot of (pid, parent_pid) - the SAME OS
    mechanism the existing monitor tool uses (never a second lifecycle);
    [] when the snapshot is unavailable, never a fabricated list."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x2

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (-1, 0xFFFFFFFFFFFFFFFF):
        return []
    entries: list[tuple[int, int]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            entries.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return entries


def _descendants_alive(root_pid: int) -> list[int]:
    """Live descendants of root in a FRESH snapshot. A dead process's
    recycled pid whose parent chain no longer reaches the root is
    excluded - pid reuse can never fabricate a survivor (nor make us
    terminate an unrelated process)."""
    entries = _snapshot_entries()
    if not entries:
        return []
    children_by_parent: dict[int, list[int]] = {}
    for child_pid, parent_pid in entries:
        children_by_parent.setdefault(parent_pid, []).append(child_pid)
    alive: list[int] = []
    seen: set[int] = set()
    frontier = [root_pid]
    while frontier:
        current = frontier.pop()
        for child_pid in children_by_parent.get(current, []):
            if child_pid not in seen:
                seen.add(child_pid)
                alive.append(child_pid)
                frontier.append(child_pid)
    return alive


def _reclaim_spawned_process_tree(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Reclaim THE spawned monitor AND its worker subtree after a
    post-spawn failure and return VERIFIABLE convergence evidence
    (converged/targets/killed/survivors/detail) - never a silent return:

    1. targets = monitor pid + ALL live descendants (Toolhelp32);
    2. taskkill /T /F with a BOUNDED timeout (timeout == primitive
       unavailable);
    3. bounded settle wait; survivors get TerminateProcess (OpenProcess/
       TerminateProcess failures are RECORDED, never swallowed);
    4. a second bounded settle wait; survivors -> converged=False.

    The caller must NOT publish a "safely failed" terminal unless
    converged is True."""
    pid = process.pid
    killed: list[int] = []
    detail: list[str] = []
    if os.name == "nt":
        try:
            result = subprocess.run(
                _taskkill_command(pid),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_TASKKILL_TIMEOUT_S,
            )
            detail.append(f"taskkill rc={result.returncode}")
        except subprocess.TimeoutExpired:
            detail.append(f"taskkill timeout>{_TASKKILL_TIMEOUT_S}s")
        except (OSError, ValueError, TypeError) as exc:
            detail.append(f"taskkill unavailable ({type(exc).__name__})")
    else:
        try:
            process.kill()
        except OSError as exc:
            detail.append(f"kill failed ({exc})")

    def _surviving_pids() -> list[int]:
        # the root is judged via OUR OWN Popen handle (pid reuse can never
        # fake it); descendants only via a FRESH parent-chain snapshot
        survivors: list[int] = []
        if process.poll() is None:
            survivors.append(pid)
        survivors.extend(_descendants_alive(pid))
        return survivors

    deadline = time.monotonic() + _RECLAIM_SETTLE_TIMEOUT_S
    survivors = _surviving_pids()
    while survivors and time.monotonic() < deadline:
        time.sleep(0.05)
        survivors = _surviving_pids()
    for survivor in survivors:
        if _terminate_pid(survivor):
            killed.append(survivor)
        else:
            detail.append(f"terminate failed pid={survivor}")
    deadline = time.monotonic() + _RECLAIM_SETTLE_TIMEOUT_S
    survivors = _surviving_pids()
    while survivors and time.monotonic() < deadline:
        time.sleep(0.05)
        survivors = _surviving_pids()
    if survivors:
        detail.append(f"survivors={survivors}")
    return {
        "converged": not survivors,
        "killed": killed,
        "survivors": survivors,
        "detail": "; ".join(detail),
    }


# ----------------------------------------------------------------------
# worker execution (runs INSIDE the detached subprocess)
# ----------------------------------------------------------------------


class _StopHandler:
    """Job-scoped stop seam: enumerates the FULL request history via
    ``ControlRequestStore.list_requests`` and looks for ``stop_safely``
    requests that are ``pending`` OR already ``accepted``. It only
    DISCOVERS and ACCEPTS stop requests - terminal settlement happens
    exactly once through the shared ``_settle_terminal_requests`` seam.
    A corrupt request refuses execution (never silently ignored).
    Terminal requests are idempotent and never re-settled."""

    def __init__(self, store: ControlRequestStore, state: dict[str, Any]) -> None:
        self.store = store
        self.state = state

    def _maybe_test_crash(self) -> None:
        """TEST-ONLY hard-crash injection (T05-style hook precedent):
        when the job params carry a ``test_crash_marker`` path and that
        marker file exists, the worker HARD-EXITS at this batch boundary
        (the current batch checkpoint is ALREADY saved) - an honest
        worker-crash simulation without unreliable external PID kills.
        Production never sets this parameter."""
        marker = self.state.get("test_crash_marker")
        if marker and Path(str(marker)).is_file():
            print(
                f"WORKER_TEST_CRASH: hard exit at batch boundary "
                f"(marker {marker}); checkpoint already saved",
                file=sys.stderr,
                flush=True,
            )
            os._exit(9)

    def _open_stop_requests(self) -> list[dict[str, Any]]:
        # leaf-level safety BEFORE enumerating/loading request files (a
        # linked request leaf is never followed)
        open_requests: list[dict[str, Any]] = []
        for payload in _safe_request_list(self.store):
            if payload.get("invalid"):
                raise WorkerError(
                    _WORKER_CONTROL_REQUEST_INVALID,
                    f"corrupt control request in job: {payload.get('request_id')}",
                )
            if payload.get("action") == "stop_safely" and payload.get("request_status") in (
                "pending",
                "accepted",
            ):
                open_requests.append(payload)
        return open_requests

    def __call__(self) -> dict[str, Any] | None:
        self._maybe_test_crash()
        for payload in self._open_stop_requests():
            if payload.get("request_status") == "pending":
                _safe_request_update(self.store, str(payload["request_id"]), status="accepted")
            return payload
        return None

def _disk_full(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in (28,)


# ----------------------------------------------------------------------
# frozen terminal intent (job-level control evidence, never a business
# artifact; published atomically BEFORE the request settlement begins)
# ----------------------------------------------------------------------


def _terminal_intent_path(jobs_dir: Path | str, job_id: str) -> Path:
    return _job_dir(jobs_dir, job_id) / _TERMINAL_INTENT_FILE


def _intent_hash(intent: dict[str, Any]) -> str:
    """Content hash over the FROZEN identity fields only (created_at /
    publish_state / intent_hash itself stay outside)."""
    frozen = {
        key: value
        for key, value in intent.items()
        if key not in ("created_at", "publish_state", "intent_hash")
    }
    return hashlib.sha256(stable_dump(frozen)).hexdigest()


def _write_terminal_intent(
    jobs_dir: Path | str,
    job_id: str,
    *,
    job: dict[str, Any],
    state: str,
    diagnostic_code: str | None,
    honoured: bool | None,
    result: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    job_fields: dict[str, Any] | None,
    publish_state: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Atomically persist the frozen terminal intent (including the job
    identity fields: task/params_key/request_id/resume_request_id and the
    freeze-time run_id); a rewrite (pending -> published) preserves the
    original created_at and never deletes the historical intent. The
    FIRST persist rides the shared transient-IO classification with a
    small fixed retry budget; a persistent failure is fail-closed:
    WORKER_TERMINAL_INTENT_PERSIST_FAILED and NO recovery claim."""
    path = _terminal_intent_path(jobs_dir, job_id)
    if created_at is None and path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        except (OSError, ValueError):
            pass
    intent: dict[str, Any] = {
        "intent_version": _TERMINAL_INTENT_VERSION,
        "job_id": job_id,
        "task": job.get("task"),
        "params_key": job.get("params_key"),
        "request_id": job.get("request_id"),
        "resume_request_id": job.get("resume_request_id"),
        "run_id": job.get("run_id"),
        "fixed_target_state": state,
        "diagnostic_code": diagnostic_code,
        "honoured": honoured,
        "result": result,
        "progress": progress,
        "job_fields": job_fields,
        "publish_state": publish_state,
        "created_at": created_at or datetime.now(UTC).isoformat(),
    }
    intent["intent_hash"] = _intent_hash(intent)
    attempt = 0
    while True:
        try:
            atomic_write_object(path, intent)
            return intent
        except OSError as exc:
            attempt += 1
            if attempt >= _INTENT_WRITE_MAX_ATTEMPTS or not is_transient_io_conflict(exc):
                raise WorkerError(
                    _WORKER_TERMINAL_INTENT_PERSIST_FAILED,
                    f"terminal intent persist failed for job {job_id} after "
                    f"{attempt} attempt(s): {exc}",
                ) from exc
            time.sleep(_INTENT_WRITE_BACKOFF_SECONDS)


def _load_terminal_intent(jobs_dir: Path | str, job_id: str) -> dict[str, Any]:
    """Load and VERIFY the frozen terminal intent (structure, identity and
    content hash); a missing intent is INTENT_MISSING, any structural or
    identity mismatch is INTENT_INVALID - never silently trusted."""
    path = _terminal_intent_path(jobs_dir, job_id)
    if not path.is_file():
        raise WorkerError(
            _WORKER_TERMINAL_INTENT_MISSING,
            f"no frozen terminal intent for job {job_id}",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkerError(
            _WORKER_TERMINAL_INTENT_INVALID,
            f"terminal intent unreadable for job {job_id}: {exc}",
        ) from exc
    valid = (
        isinstance(payload, dict)
        and payload.get("intent_version") == _TERMINAL_INTENT_VERSION
        and payload.get("job_id") == job_id
        and payload.get("fixed_target_state") in ("completed", "cancelled", "failed", "blocked")
        and isinstance(payload.get("task"), str)
        and bool(payload.get("task"))
        and isinstance(payload.get("params_key"), str)
        and bool(payload.get("params_key"))
        and isinstance(payload.get("request_id"), str)
        and bool(payload.get("request_id"))
        and isinstance(payload.get("resume_request_id"), (str, type(None)))
        and isinstance(payload.get("run_id"), (str, type(None)))
        and isinstance(payload.get("diagnostic_code"), (str, type(None)))
        and isinstance(payload.get("honoured"), (bool, type(None)))
        and isinstance(payload.get("result"), (dict, type(None)))
        and isinstance(payload.get("progress"), (dict, type(None)))
        and isinstance(payload.get("job_fields"), (dict, type(None)))
        and payload.get("publish_state") in ("pending", "published")
        and isinstance(payload.get("created_at"), str)
        and isinstance(payload.get("intent_hash"), str)
        and payload.get("intent_hash") == _intent_hash(payload)
    )
    if not valid:
        raise WorkerError(
            _WORKER_TERMINAL_INTENT_INVALID,
            f"terminal intent for job {job_id} fails structure/identity/hash checks",
        )
    return cast(dict[str, Any], payload)


def _verify_intent_identity(intent: dict[str, Any], job: dict[str, Any], job_id: str) -> None:
    """Cross-check the intent's frozen identity fields against the
    re-read job record (the content hash documents integrity only - it
    can never prove identity against a writer who recomputes it)."""
    for field in ("task", "params_key", "request_id", "resume_request_id", "run_id"):
        if intent.get(field) != job.get(field):
            raise WorkerError(
                _WORKER_TERMINAL_INTENT_INVALID,
                f"terminal intent {field} mismatch for job {job_id}",
            )
    target_run_id = (intent.get("job_fields") or {}).get("run_id")
    if target_run_id is not None and target_run_id != intent.get("run_id"):
        raise WorkerError(
            _WORKER_TERMINAL_INTENT_INVALID,
            f"terminal intent target run_id {target_run_id} differs from the "
            f"freeze-time run_id for job {job_id}",
        )


def _check_settled_requests_consistent(
    history: list[dict[str, Any]],
    intent: dict[str, Any],
    job: dict[str, Any],
    job_id: str,
) -> None:
    """The frozen target must match already-settled requests EXACTLY: a
    start/resume request_id must belong to this job; a completed
    start/resume requires result.job_state == the frozen target
    (completed/cancelled); a rejected start/resume requires
    result.job_state == the frozen target (failed/blocked) AND the frozen
    diagnostic recorded; a completed stop requires a bool
    result.honoured equal to the frozen honoured (failed/blocked targets
    always honoured=False); unknown actions or unknown terminal
    combinations are never silently skipped - every mismatch is a stable
    CONFLICT, never guessed around."""
    target = intent["fixed_target_state"]
    own_ids = {job.get("request_id"), job.get("resume_request_id")}
    expected_diag = intent.get("diagnostic_code") or "WORKER_TASK_FAILED"
    for payload in history:
        status = payload.get("request_status")
        if status not in ("completed", "rejected"):
            continue
        action = payload.get("action")
        result = payload.get("result") or {}
        if action in ("start", "resume"):
            if payload.get("request_id") not in own_ids:
                raise WorkerError(
                    _WORKER_TERMINAL_STATE_CONFLICT,
                    f"job {job_id}: settled start/resume request "
                    f"{payload.get('request_id')} does not belong to this job",
                )
            if status == "completed":
                if target not in ("completed", "cancelled") or result.get("job_state") != target:
                    raise WorkerError(
                        _WORKER_TERMINAL_STATE_CONFLICT,
                        f"job {job_id}: settled start/resume completed with "
                        f"job_state={result.get('job_state')!r} contradicts target {target}",
                    )
            else:  # rejected
                if (
                    target not in ("failed", "blocked")
                    or result.get("job_state") != target
                    or expected_diag not in (payload.get("diagnostics") or [])
                ):
                    raise WorkerError(
                        _WORKER_TERMINAL_STATE_CONFLICT,
                        f"job {job_id}: settled start/resume rejected with "
                        f"job_state={result.get('job_state')!r}/diagnostics "
                        f"contradicts target {target}",
                    )
        elif action == "stop_safely":
            if status != "completed":
                raise WorkerError(
                    _WORKER_TERMINAL_STATE_CONFLICT,
                    f"job {job_id}: a rejected stop_safely contradicts any "
                    f"terminal target {target}",
                )
            honoured = result.get("honoured")
            if not isinstance(honoured, bool):
                raise WorkerError(
                    _WORKER_TERMINAL_STATE_CONFLICT,
                    f"job {job_id}: a settled stop lacks a bool honoured",
                )
            if target in ("failed", "blocked"):
                if honoured is not False:
                    raise WorkerError(
                        _WORKER_TERMINAL_STATE_CONFLICT,
                        f"job {job_id}: an honoured stop contradicts target {target}",
                    )
            elif honoured != intent.get("honoured"):
                raise WorkerError(
                    _WORKER_TERMINAL_STATE_CONFLICT,
                    f"job {job_id}: stop honoured={honoured} contradicts the "
                    f"frozen honoured={intent.get('honoured')}",
                )
        else:
            raise WorkerError(
                _WORKER_TERMINAL_STATE_CONFLICT,
                f"job {job_id}: unknown settled action {action!r} contradicts "
                f"target {target}",
            )


def _settle_terminal_requests(
    store: ControlRequestStore,
    job: dict[str, Any],
    *,
    state: str,
    diagnostic_code: str | None,
    honoured: bool | None,
    result: dict[str, Any] | None,
) -> None:
    """THE single control-request settlement at a terminal job state
    (the ONLY implementation; the stop handler merely discovers and
    accepts stop requests): the job's start/resume request AND every
    open stop request are settled - a control request is never left
    stuck at accepted/pending forever.

    - completed job -> start/resume completed;
    - cancelled job -> start/resume completed (result carries the
      cancelled lifecycle); open stops completed with honoured;
    - failed/blocked job -> start/resume rejected with the stable
      diagnostic; open stops completed with honoured=false and the
      diagnostic.
    Terminal requests stay untouched (idempotent repeats are safe, even
    after a partially completed settlement). Persistence failures
    (OSError / CONTROL_REQUEST_IO_BUSY) PROPAGATE to the caller: the job
    terminal state must never be published on top of unsettled requests
    (never a false terminal). A corrupt/contract-invalid request BLOCKS
    the terminal publish outright (stable control-plane corruption
    evidence; the file is never deleted, overwritten or auto-rejected)."""
    # leaf-level safety BEFORE enumerating/settling request files (a
    payloads = _safe_request_list(store)
    for payload in payloads:
        if payload.get("invalid"):
            raise WorkerError(
                _WORKER_CONTROL_REQUEST_INVALID,
                f"corrupt control-plane request evidence blocks the terminal "
                f"publish of job {job.get('job_id')}: {payload.get('request_id')}",
            )
    for payload in payloads:
        action = payload.get("action")
        request_status = payload.get("request_status")
        if action in ("start", "resume") and payload.get("request_id") in (
            job.get("request_id"),
            job.get("resume_request_id"),
        ):
            if request_status in ("pending", "accepted"):
                if state in ("completed", "cancelled"):
                    if request_status == "pending":
                        _safe_request_update(
                            store, str(payload["request_id"]), status="accepted"
                        )
                    _safe_request_update(
                        store,
                        str(payload["request_id"]),
                        status="completed",
                        result={"job_state": state, **(result or {})},
                    )
                else:
                    _safe_request_update(
                        store,
                        str(payload["request_id"]),
                        status="rejected",
                        diagnostic=diagnostic_code or "WORKER_TASK_FAILED",
                        result={"job_state": state},
                    )
        elif action == "stop_safely" and request_status in ("pending", "accepted"):
            if request_status == "pending":
                _safe_request_update(
                    store, str(payload["request_id"]), status="accepted"
                )
            _safe_request_update(
                store,
                str(payload["request_id"]),
                status="completed",
                diagnostic=None if honoured else (diagnostic_code or "WORKER_TASK_FAILED"),
                result={"honoured": bool(honoured), **(result or {})},
            )


def _publish_terminal_state(
    jobs_dir: Path | str,
    job_id: str,
    store: ControlRequestStore,
    job: dict[str, Any],
    *,
    state: str,
    diagnostic_code: str | None,
    honoured: bool | None,
    result: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    job_fields: dict[str, Any] | None,
) -> None:
    """THE single terminal-publish orchestration for every job outcome
    (completed/cancelled/failed/blocked; demo and adequacy alike), in a
    FIXED order:

    1. settle the job's start/resume request AND every open stop_safely
       request (the business lifecycle / compute result is already
       determined by the caller);
    2. write the terminal progress projection;
    3. publish the terminal job state LAST.

    A terminal job state therefore ALWAYS implies every related control
    request already reached completed/rejected. The whole sequence runs
    UNDER the short-lived control-plane gate (shared with stop_job), and
    the job record + FULL request history are RE-READ under the gate
    (never a stale pre-gate snapshot). Any failure is pinned to the fixed
    target state via _TerminalPublishError: the caller must never retry
    the publish with a different target state (never a false terminal,
    never a completed->failed semantic drift). job_fields may only merge
    the inventoried non-identity fields (_TERMINAL_JOB_FIELDS_ALLOWED)."""
    if job_fields is not None and not set(job_fields) <= _TERMINAL_JOB_FIELDS_ALLOWED:
        raise WorkerError(
            _WORKER_TERMINAL_JOB_FIELDS_INVALID,
            f"job {job_id}: terminal job_fields may only carry "
            f"{sorted(_TERMINAL_JOB_FIELDS_ALLOWED)}, got {sorted(job_fields)}",
        )
    # the USE-BOUNDARY re-check AFTER the long compute and BEFORE the
    # first terminal write: the control tree, the request leaves and the
    # progress/monitor leaves this publish is about to touch must all
    # still be real, un-swapped filesystem objects
    _assert_safe_control_tree(jobs_dir, job_id)
    _assert_safe_request_store(store)
    for terminal_leaf in (
        _job_file(jobs_dir, job_id),
        _job_dir(jobs_dir, job_id) / "terminal-intent.json",
        _progress_file(jobs_dir, job_id),
        _job_dir(jobs_dir, job_id) / "monitor-output.log",
    ):
        leaf_reason = unsafe_path_component(terminal_leaf, directory=None)
        if leaf_reason is not None:
            raise WorkerError(
                _WORKER_JOB_PATH_UNSAFE,
                f"unsafe terminal-write leaf ({leaf_reason}): {terminal_leaf}",
            )
    gate = _control_gate(jobs_dir, job_id)
    _acquire_control_gate(gate, job_id)
    try:
        # authoritative re-read UNDER the gate (the job record above may
        # predate the gate); the settlement enumerates the FULL request
        # history under the same gate
        fresh_job = _read_job(jobs_dir, job_id)
        existing = fresh_job.get("state")
        if existing in ("completed", "cancelled", "failed", "blocked"):
            if existing != state:
                raise WorkerError(
                    _WORKER_TERMINAL_STATE_CONFLICT,
                    f"job {job_id} is already terminal as {existing}; "
                    f"cannot publish {state}",
                )
            # the exact same target is already published: still reconcile
            # the request settlement idempotently (terminal requests stay
            # untouched) so a partially settled history converges
            _settle_terminal_requests(
                store,
                fresh_job,
                state=state,
                diagnostic_code=diagnostic_code,
                honoured=honoured,
                result=result,
            )
            return
        # freeze the terminal intent BEFORE any request settlement begins
        # (job-level control evidence, never a business artifact); its
        # first persist retries transient IO conflicts with the small
        # fixed budget and fails closed (WORKER_TERMINAL_INTENT_PERSIST_FAILED)
        _write_terminal_intent(
            jobs_dir,
            job_id,
            job=fresh_job,
            state=state,
            diagnostic_code=diagnostic_code,
            honoured=honoured,
            result=result,
            progress=progress,
            job_fields=job_fields,
            publish_state="pending",
        )
        _settle_terminal_requests(
            store,
            fresh_job,
            state=state,
            diagnostic_code=diagnostic_code,
            honoured=honoured,
            result=result,
        )
        if progress is not None:
            _write_progress(jobs_dir, job_id, **progress)
        _update_job(jobs_dir, job_id, state=state, **(job_fields or {}))
        try:
            _write_terminal_intent(
                jobs_dir,
                job_id,
                job=fresh_job,
                state=state,
                diagnostic_code=diagnostic_code,
                honoured=honoured,
                result=result,
                progress=progress,
                job_fields=job_fields,
                publish_state="published",
            )
        except (OSError, ServiceError, WorkerError) as intent_exc:
            # the terminal state IS already the fact: a failed
            # pending->published marker update is a stable WARNING, never
            # a _TerminalPublishError, never a WORKER_TERMINAL_PUBLISH_FAILED
            # against a successfully terminalized job; the intent stays
            # pending and a later recover-terminal fixes the marker
            # idempotently
            print(
                f"WORKER_TERMINAL_INTENT_MARKER_UPDATE_FAILED: job {job_id} "
                f"intent publish marker update unavailable ({intent_exc}); "
                f"the terminal state stands; a later recover-terminal fixes "
                f"the marker; 终态标记更新失败",
                file=sys.stderr,
                flush=True,
            )
    except _TerminalPublishError:
        raise
    except (OSError, ServiceError, WorkerError) as exc:
        raise _TerminalPublishError(state, job_id, exc) from exc
    finally:
        gate.release()


def execute_job(jobs_dir: Path | str, job_id: str) -> int:
    """Execute ONE controlled job inside the worker process; the process
    exits afterwards (no daemon, no polling scheduler)."""
    # path safety BEFORE resolving or reading any task evidence (never
    # resolve-follow a swapped link)
    _assert_safe_control_tree(_jobs_root(jobs_dir), job_id)
    jobs_dir = _jobs_root(jobs_dir).resolve()
    job = _read_job(jobs_dir, job_id)
    task = job["task"]
    params = job["params"]
    store = _control_store(jobs_dir, job_id)
    # request-leaf safety BEFORE any request load/update and BEFORE any
    # state transition (a linked request leaf is never followed)
    _assert_safe_request_store(store)
    stop_handler = _StopHandler(
        store,
        {
            "job_id": job_id,
            "test_crash_marker": params.get("test_crash_marker")
            if isinstance(params, dict)
            else None,
        },
    )
    state: dict[str, Any] = {"monitor_degraded": False, "run_id": None}
    diagnostic_code: str | None = None

    lock = RunLock(jobs_dir / "locks", _lock_key(jobs_dir, {**job, "run_id": job.get("run_id")}))
    if not lock.acquire():
        # single-flight: a second worker NEVER runs compute and never
        # touches the first worker's job or lifecycle
        _publish_terminal_state(
            jobs_dir,
            job_id,
            store,
            {**job, "state": "blocked"},
            state="blocked",
            diagnostic_code=_WORKER_RUN_IN_PROGRESS,
            honoured=False,
            result={"blocked": True},
            progress={
                "phase": task,
                "current_subtask": "blocked",
                "completed_work": 0,
                "total_work": 0,
                "run_id": job.get("run_id"),
                "checkpoint_ref": None,
                "lifecycle_status": None,
                "diagnostic_code": _WORKER_RUN_IN_PROGRESS,
            },
            job_fields={"diagnostics": [_WORKER_RUN_IN_PROGRESS]},
        )
        return _EXIT_BLOCKED

    stop_heartbeat = threading.Event()

    def _heartbeat() -> None:
        while not stop_heartbeat.wait(WORKER_HEARTBEAT_SECONDS):
            try:
                lock.heartbeat()
            except Exception:  # noqa: BLE001 - monitoring degrades, compute continues
                state["monitor_degraded"] = True

    thread = threading.Thread(target=_heartbeat, daemon=True)
    thread.start()

    def _progress_cb(payload: dict[str, Any]) -> None:
        if payload.get("run_id") and payload["run_id"] != job.get("run_id"):
            # run association: persisted as soon as the run_id exists
            state["run_id"] = str(payload["run_id"])
            _update_job(jobs_dir, job_id, run_id=str(payload["run_id"]))
        _write_progress(
            jobs_dir,
            job_id,
            phase=str(payload.get("phase", task)),
            current_subtask=str(payload.get("current_subtask", "")),
            completed_work=int(payload.get("completed_work", 0)),
            total_work=int(payload.get("total_work", 0)),
            run_id=state.get("run_id") or job.get("run_id"),
            checkpoint_ref=payload.get("checkpoint_ref"),
            lifecycle_status=payload.get("lifecycle_status"),
            diagnostic_code=diagnostic_code,
            monitor_degraded=bool(state["monitor_degraded"]),
        )

    exit_code = _EXIT_FAILED
    try:
        _update_job(jobs_dir, job_id, state="running", worker_pid=os.getpid())
        # the start request enters accepted when the worker actually runs
        start_request = _safe_request_load(store, str(job["request_id"]))
        if start_request.get("request_status") == "pending":
            _safe_request_update(store, str(job["request_id"]), status="accepted")
        # the ARCHIVED task params must re-validate to themselves and
        # recompute to the archived params_key - tampered params NEVER
        # execute (any task kind); a failure is the stable WORKER_JOB_
        # PARAMS_TAMPERED terminal below (except WorkerError handler).
        # RESUME records are the exception: the P1 identity gate inside
        # resume_adequacy_pipeline (which REBUILDS the expected identity
        # from the run manifest and the frozen run config) is the
        # authoritative params-vs-run check - a legal resume record whose
        # params disagree with the frozen run identity is
        # P1_RESUME_IDENTITY_MISMATCH, never pre-empted by the params_key
        # check; only STRUCTURALLY damaged resume records (unparseable,
        # missing keys, non-dict) are WORKER_JOB_PARAMS_TAMPERED.
        if not isinstance(params, dict):
            raise WorkerError(
                _WORKER_JOB_PARAMS_TAMPERED,
                "job record carries no usable params payload",
            )
        # params integrity for EVERY job (resume included): structure,
        # canonical normalization and the archived params_key are never
        # bypassed for error-code priority
        _verify_job_params_integrity(job)
        # a FORMAL resume identity must come from existing, cross-
        # verifiable job/request/run evidence - never from a bare
        # params.resume_run_id
        if params.get("resume_run_id") is not None:
            _verify_formal_resume_identity(jobs_dir, job, params)
        # a T26-A identity-bound job must carry its full binding fields
        # (never a legacy downgrade via stripped params)
        _verify_bound_task_identity(job, jobs_dir=jobs_dir)
        if "task_identity" in job:
            # the archived output directory must still close over the same
            # workspace as this jobs_dir
            _verify_bound_output_dir(
                job,
                params.get("output_dir") if isinstance(params, dict) else None,
                jobs_dir,
            )
        # re-check the control tree and the request leaves BEFORE the task
        # dispatch (a swap during the setup above is still caught)
        _assert_safe_control_tree(jobs_dir, job_id)
        _assert_safe_request_store(store)
        if task == _DEMO_TASK:
            from capacity_credit.demo.business_demo import DemoStopped, run_demo

            # T26-A identity-bound demo jobs: re-verify the bound config
            # hash and software commit BEFORE any compute or artifact write
            _verify_demo_execution_identity(params)

            try:
                # run_demo RE-VERIFIES the bound identity at the actual
                # consumption point (single-read config bytes + software
                # identity) - the pre-dispatch check never substitutes
                summary = run_demo(
                    params["config"],
                    params["output_dir"],
                    progress=_progress_cb,
                    stop_check=stop_handler,
                    expected_config_sha256=params.get("config_sha256"),
                    expected_software_commit=params.get("source_software_commit"),
                )
            except DemoStopped as stopped:
                _publish_terminal_state(
                    jobs_dir,
                    job_id,
                    store,
                    {**job, "state": "cancelled"},
                    state="cancelled",
                    diagnostic_code=None,
                    honoured=True,
                    result={"stage": stopped.stage, "state": "cancelled"},
                    progress={
                        "phase": "demo",
                        "current_subtask": f"stopped_at:{stopped.stage}",
                        "completed_work": 0,
                        "total_work": 0,
                        "run_id": None,
                        "checkpoint_ref": None,
                        "lifecycle_status": "cancelled",
                        "diagnostic_code": stopped.code,
                        "monitor_degraded": bool(state["monitor_degraded"]),
                    },
                    job_fields={"diagnostics": [stopped.code]},
                )
                return _EXIT_CANCELLED
            diagnostics = list(summary.get("stop_diagnostics") or [])
            if _WORKER_SAFE_STOP_UNSUPPORTED in diagnostics:
                # the stop request arrived inside the non-checkpointed
                # ELCC stage: NOT honoured - the demo ran to completion
                # (recorded honestly, never faked as stopped)
                _publish_terminal_state(
                    jobs_dir,
                    job_id,
                    store,
                    {**job, "state": "completed"},
                    state="completed",
                    diagnostic_code=_WORKER_SAFE_STOP_UNSUPPORTED,
                    honoured=False,
                    result={"state": "completed"},
                    progress={
                        "phase": "demo",
                        "current_subtask": "completed",
                        "completed_work": 1,
                        "total_work": 1,
                        "run_id": None,
                        "checkpoint_ref": None,
                        "lifecycle_status": "completed",
                        "diagnostic_code": _WORKER_SAFE_STOP_UNSUPPORTED,
                        "monitor_degraded": bool(state["monitor_degraded"]),
                    },
                    job_fields={"diagnostics": diagnostics},
                )
                return _EXIT_COMPLETED
            _publish_terminal_state(
                jobs_dir,
                job_id,
                store,
                {**job, "state": "completed"},
                state="completed",
                diagnostic_code=None,
                honoured=True,
                result={"state": "completed"},
                progress={
                    "phase": "demo",
                    "current_subtask": "completed",
                    "completed_work": 1,
                    "total_work": 1,
                    "run_id": None,
                    "checkpoint_ref": None,
                    "lifecycle_status": "completed",
                    "diagnostic_code": None,
                    "monitor_degraded": bool(state["monitor_degraded"]),
                },
                job_fields={"run_id": None, "worker_pid": os.getpid()},
            )
            exit_code = _EXIT_COMPLETED
        else:
            from capacity_credit.application.p1_workflow import (
                resume_adequacy_pipeline,
                run_adequacy_pipeline,
            )

            resume_run_id = params.get("resume_run_id") or job.get("resume_run_id")
            if resume_run_id:
                projection = resume_adequacy_pipeline(
                    project_dir=params["project_dir"],
                    import_batch_id=params["import_batch_id"],
                    data_version_id=params["data_version_id"],
                    scenario_bank_id=params["scenario_bank_id"],
                    run_id=str(resume_run_id),
                    request_id=f"resume-{job['request_id']}",
                    created_by=job.get("requested_by", "worker"),
                    software_version=params["software_version"],
                    software_commit=params["software_commit"],
                    root_seed=int(params["root_seed"]),
                    primary_risk_metric=params.get("primary_risk_metric", "lole_days_per_year"),
                    weight_set_id=params.get("weight_set_id", "w1"),
                    scenario_id=params.get("scenario_id"),
                    stop_check=stop_handler,
                    progress=_progress_cb,
                )
            else:
                projection = run_adequacy_pipeline(
                    project_dir=params["project_dir"],
                    import_batch_id=params["import_batch_id"],
                    data_version_id=params["data_version_id"],
                    scenario_bank_id=params["scenario_bank_id"],
                    request_id=f"run-{job['request_id']}",
                    created_by=job.get("requested_by", "worker"),
                    software_version=params["software_version"],
                    software_commit=params["software_commit"],
                    root_seed=int(params["root_seed"]),
                    primary_risk_metric=params.get("primary_risk_metric", "lole_days_per_year"),
                    weight_set_id=params.get("weight_set_id", "w1"),
                    scenario_id=params.get("scenario_id"),
                    stop_check=stop_handler,
                    progress=_progress_cb,
                )
            lifecycle = str(projection.get("lifecycle_status"))
            terminal_progress: dict[str, Any] = {
                "phase": "adequacy",
                "current_subtask": str(projection.get("lifecycle_status")),
                "completed_work": int(projection.get("completed_batch", 0)),
                "total_work": 0,
                "run_id": str(projection.get("run_id")),
                "checkpoint_ref": projection.get("checkpoint_path"),
                "lifecycle_status": lifecycle,
                "diagnostic_code": None,
                "monitor_degraded": bool(state["monitor_degraded"]),
            }
            if lifecycle == "cancelled":
                _publish_terminal_state(
                    jobs_dir,
                    job_id,
                    store,
                    {**job, "state": "cancelled"},
                    state="cancelled",
                    diagnostic_code=None,
                    honoured=True,
                    result={
                        "checkpoint_ref": projection.get("checkpoint_path"),
                        "completed_batch": projection.get("completed_batch"),
                    },
                    progress=terminal_progress,
                    job_fields={"run_id": str(projection.get("run_id"))},
                )
                exit_code = _EXIT_CANCELLED
            elif lifecycle == "completed":
                _publish_terminal_state(
                    jobs_dir,
                    job_id,
                    store,
                    {**job, "state": "completed"},
                    state="completed",
                    diagnostic_code=None,
                    honoured=True,
                    result={"state": "completed"},
                    progress=terminal_progress,
                    job_fields={"run_id": str(projection.get("run_id"))},
                )
                exit_code = _EXIT_COMPLETED
            else:
                diagnostic_code = "WORKER_LIFECYCLE_UNEXPECTED"
                _publish_terminal_state(
                    jobs_dir,
                    job_id,
                    store,
                    {**job, "state": "failed"},
                    state="failed",
                    diagnostic_code=diagnostic_code,
                    honoured=False,
                    result={},
                    progress=terminal_progress,
                    job_fields={"run_id": str(projection.get("run_id"))},
                )
                exit_code = _EXIT_FAILED
    except WorkerError as exc:
        diagnostic_code = exc.code
        try:
            _publish_terminal_state(
                jobs_dir,
                job_id,
                store,
                {**job, "state": "failed"},
                state="failed",
                diagnostic_code=exc.code,
                honoured=False,
                result={},
                progress={
                    "phase": task,
                    "current_subtask": "failed",
                    "completed_work": 0,
                    "total_work": 0,
                    "run_id": state.get("run_id") or job.get("run_id"),
                    "checkpoint_ref": None,
                    "lifecycle_status": "failed",
                    "diagnostic_code": exc.code,
                    "monitor_degraded": bool(state["monitor_degraded"]),
                },
                job_fields={"diagnostics": [exc.code]},
            )
        except _TerminalPublishError as publish_exc:
            cause = publish_exc.cause
            cause_code = (
                cause.code
                if isinstance(cause, (ServiceError, WorkerError))
                else (_WORKER_DISK_FULL if _disk_full(cause) else _WORKER_TERMINAL_PUBLISH_FAILED)
            )
            print(
                f"{cause_code}: job {job_id} failure diagnostics persistence "
                f"unavailable ({cause}); 持久化诊断不可用",
                file=sys.stderr,
                flush=True,
            )
        exit_code = _EXIT_FAILED
    except _TerminalPublishError as exc:
        # the terminal publish already committed to its FIXED target state
        # (requests may already be settled); NEVER re-enter the publish
        # with another target: keep the job recoverable (non-terminal),
        # record the stable diagnostic and exit non-zero
        diagnostic_code = _WORKER_TERMINAL_PUBLISH_FAILED
        cause = exc.cause
        cause_code = (
            cause.code
            if isinstance(cause, (ServiceError, WorkerError))
            else (_WORKER_DISK_FULL if _disk_full(cause) else _WORKER_TERMINAL_PUBLISH_FAILED)
        )
        try:
            _update_job(
                jobs_dir,
                job_id,
                diagnostics=[
                    _WORKER_TERMINAL_PUBLISH_FAILED,
                    f"target={exc.target_state}",
                    cause_code,
                ],
            )
        except (OSError, ServiceError) as diag_exc:
            print(
                f"{_WORKER_TERMINAL_PUBLISH_FAILED}: job {job_id} even the "
                f"diagnostics persistence is unavailable ({diag_exc}); "
                f"持久化诊断不可用",
                file=sys.stderr,
                flush=True,
            )
        if cause_code == _WORKER_TERMINAL_INTENT_PERSIST_FAILED:
            note = (
                "auto-recovery unavailable (no frozen intent persisted); "
                "manual evidence handling required; 自动恢复不可用，需人工证据处置"
            )
        else:
            note = "recoverable via recover-terminal; 可通过 recover-terminal 按原目标恢复"
        print(
            f"{_WORKER_TERMINAL_PUBLISH_FAILED}: job {job_id} terminal publish "
            f"towards {exc.target_state!r} failed ({cause_code}); {note}",
            file=sys.stderr,
            flush=True,
        )
        exit_code = _EXIT_FAILED
    except BaseException as exc:
        # KeyboardInterrupt/SystemExit are recorded honestly and
        # re-raised (never masqueraded as success); every other failure
        # maps to a stable diagnostic code. (ServiceError is imported at
        # module level.)
        re_raise = isinstance(exc, (KeyboardInterrupt, SystemExit))
        if isinstance(exc, KeyboardInterrupt):
            diagnostic_code = "WORKER_INTERRUPTED"
        elif isinstance(exc, SystemExit):
            diagnostic_code = "WORKER_SYSTEM_EXIT"
        elif _disk_full(exc):
            diagnostic_code = _WORKER_DISK_FULL
        elif isinstance(exc, ServiceError):
            diagnostic_code = exc.code
        elif type(exc).__name__ in ("P1WorkflowError", "DemoError") and hasattr(exc, "code"):
            # the demo consumption-point refusal keeps its precise stable
            # code (DEMO_CONFIG_CHANGED / DEMO_COMMIT_CHANGED / ...)
            diagnostic_code = str(exc.code)
        else:
            diagnostic_code = "WORKER_TASK_FAILED"
        try:
            _publish_terminal_state(
                jobs_dir,
                job_id,
                store,
                {**job, "state": "failed"},
                state="failed",
                diagnostic_code=diagnostic_code,
                honoured=False,
                result={},
                progress={
                    "phase": task,
                    "current_subtask": "failed",
                    "completed_work": 0,
                    "total_work": 0,
                    "run_id": state.get("run_id") or job.get("run_id"),
                    "checkpoint_ref": None,
                    "lifecycle_status": "failed",
                    "diagnostic_code": diagnostic_code,
                    "monitor_degraded": bool(state["monitor_degraded"]),
                },
                job_fields={"diagnostics": [diagnostic_code, str(exc)[:500]]},
            )
        except _TerminalPublishError as publish_exc:
            cause = publish_exc.cause
            cause_code = (
                cause.code
                if isinstance(cause, (ServiceError, WorkerError))
                else (_WORKER_DISK_FULL if _disk_full(cause) else _WORKER_TERMINAL_PUBLISH_FAILED)
            )
            print(
                f"{cause_code}: job {job_id} failure diagnostics persistence "
                f"unavailable ({cause}); 持久化诊断不可用",
                file=sys.stderr,
                flush=True,
            )
        exit_code = _EXIT_FAILED
        if re_raise:
            raise
    finally:
        stop_heartbeat.set()
        thread.join(timeout=2)
        try:
            lock.release()
        except Exception:  # noqa: BLE001, S110 - OS releases on exit anyway
            pass
    return exit_code


# ----------------------------------------------------------------------
# control operations (CLI-facing, thin)
# ----------------------------------------------------------------------


def stop_job(
    jobs_dir: Path | str,
    job_id: str,
    *,
    requested_by: str,
) -> dict[str, Any]:
    """Create an IDEMPOTENT ``stop_safely`` control request for a job.
    Same request parameters return the same stored state; no checkpoint
    or lifecycle change happens here (the worker honours the request at
    the next safe boundary). The submit runs UNDER the short-lived
    control-plane gate shared with the terminal publish: the job state
    is re-read authoritatively under the gate, so a stop that arrives
    while the terminal publish is in progress blocks briefly and is
    then stably rejected - it can never slip past the settlement
    snapshot and be left pending against a terminal job."""
    _assert_safe_control_tree(jobs_dir, job_id)
    job = _read_job(jobs_dir, job_id)
    if job.get("state") in ("completed", "failed", "cancelled", "blocked"):
        # fast pre-gate rejection (never authoritative on its own)
        return {
            "job_id": job_id,
            "request_id": None,
            "request_status": "rejected",
            "diagnostics": [f"WORKER_JOB_ALREADY_{job.get('state', 'UNKNOWN').upper()}"],
        }
    gate = _control_gate(jobs_dir, job_id)
    _acquire_control_gate(gate, job_id)
    try:
        # authoritative re-read UNDER the gate: a terminal publish may
        # have completed between the pre-check and the acquire
        job = _read_job(jobs_dir, job_id)
        if job.get("state") in ("completed", "failed", "cancelled", "blocked"):
            return {
                "job_id": job_id,
                "request_id": None,
                "request_status": "rejected",
                "diagnostics": [f"WORKER_JOB_ALREADY_{job.get('state', 'UNKNOWN').upper()}"],
            }
        store = _control_store(jobs_dir, job_id)
        # leaf-level safety BEFORE submitting (a linked existing leaf is
        # never followed or overwritten)
        _assert_safe_request_store(store)
        request = ControlRequest.create(
            action="stop_safely",
            run_id=job.get("run_id"),
            requested_by=requested_by,
            expected_lifecycle_status="running",
        )
        stored = _safe_request_submit(store, request)
        return {
            "job_id": job_id,
            "request_id": str(request.request_id),
            "request_status": stored.get("request_status", "pending"),
        }
    finally:
        gate.release()


def resume_job(
    jobs_dir: Path | str,
    job_id: str,
    *,
    requested_by: str,
) -> dict[str, Any]:
    """Resume a worker-interrupted adequacy job: validates the run is
    still ``running``, that NO live owner holds the OS run lock, and
    that a checkpoint exists - all BEFORE any new process or output is
    created. ``completed``/``failed``/``cancelled`` runs are stably
    refused (a cancelled task needs a NEW run, never a status flip)."""
    _assert_safe_control_tree(jobs_dir, job_id)
    job = _read_job(jobs_dir, job_id)
    if job["task"] != _ADEQUACY_TASK:
        raise WorkerError(
            _WORKER_RESUME_UNSUPPORTED_FOR_TASK,
            f"task {job['task']} has no checkpoint; resume is unsupported",
        )
    run_id = job.get("run_id")
    if not run_id:
        raise WorkerError(
            _WORKER_RESUME_CHECKPOINT_MISSING,
            "the interrupted job never associated a run_id",
        )
    project = Path(job["params"]["project_dir"])
    from capacity_credit.application.import_service import ImportService
    from capacity_credit.application.run_service import RunService

    run_service = RunService(project, ImportService(project))
    lifecycle = run_service.run_state(str(run_id))["lifecycle_status"]
    if lifecycle in ("completed", "failed", "cancelled"):
        raise WorkerError(
            _WORKER_RESUME_STATE_NOT_ALLOWED,
            f"run lifecycle is {lifecycle}; a terminal run cannot resume under the same run_id",
        )
    if lifecycle != "running":
        raise WorkerError(
            _WORKER_RESUME_STATE_NOT_ALLOWED,
            f"run lifecycle is {lifecycle}; only a running run can resume",
        )
    # the execution lock (adequacy-execution namespace) is THE live-owner
    # gate for the run computation - the same lock the initial and
    # resumed pipelines hold; the lifecycle lock is NEVER used here
    execution_lock = RunLock(project / "locks" / "adequacy-execution", str(run_id))
    if execution_lock.is_held():
        raise WorkerError(
            _WORKER_RESUME_OWNER_ACTIVE,
            "a live owner holds the execution lock; resume refused",
        )
    checkpoint = project / "runs" / safe_filename_component(str(run_id)) / "checkpoint.json"
    if not checkpoint.is_file():
        raise WorkerError(
            _WORKER_RESUME_CHECKPOINT_MISSING,
            f"no checkpoint to resume from: {checkpoint}",
        )
    resume_job_id = str(uuid.uuid4())
    resume_request_id = str(uuid.uuid4())
    final_dir = _job_dir(jobs_dir, resume_job_id)
    # the resume job is CONSTRUCTED in a staging directory inside the
    # reserved locks namespace (never enumerated as a job) and published
    # by ONE atomic rename - a crash before the publish leaves only an
    # identifiable staging residue, never a formal half-record
    staging = _jobs_root(jobs_dir) / "locks" / f"resume-staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    params = dict(job["params"])
    params.pop("test_crash_marker", None)  # never inherit a test crash hook
    params["resume_run_id"] = str(run_id)
    record: dict[str, Any] = {
        "job_id": resume_job_id,
        "request_id": resume_request_id,
        "task": _ADEQUACY_TASK,
        "params": params,
        "params_key": hashlib.sha256(
            _canonical_params(_ADEQUACY_TASK, params).encode("utf-8")
        ).hexdigest()[:32],
        "requested_by": requested_by,
        "state": "queued",
        "pid": None,
        "run_id": str(run_id),
        "resumed_from_job": job_id,
        "diagnostics": [],
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    staging_store = ControlRequestStore(staging / "control")
    try:
        atomic_write_bytes(
            staging / "job.json",
            (
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
        )
        _safe_request_submit(
            staging_store,
            ControlRequest.create(
                action="resume",
                request_id=resume_request_id,
                run_id=str(run_id),
                requested_by=requested_by,
                expected_lifecycle_status="running",
            ),
        )
        _safe_request_update(staging_store, resume_request_id, status="accepted")
        # VERIFY the staged evidence reads back as THIS call's complete
        # record BEFORE it can ever be seen as a formal job
        staged = json.loads((staging / "job.json").read_text(encoding="utf-8"))
        if (
            not isinstance(staged, dict)
            or staged.get("job_id") != resume_job_id
            or staged.get("request_id") != resume_request_id
        ):
            raise WorkerError(
                _WORKER_JOB_CREATION_INCOMPLETE,
                "resume staging verification failed",
            )
        # THE publish: one atomic rename (fails loudly if the target exists)
        os.rename(staging, final_dir)
    except BaseException as exc:
        # ZERO-publish rollback: only THIS call's staging directory is
        # discarded (identity-verified); a cleanup failure surfaces
        # explicitly and never masks the original error
        try:
            _discard_resume_staging(staging, resume_job_id, resume_request_id)
        except BaseException as cleanup_exc:
            raise cleanup_exc from exc
        raise
    return record


def recover_terminal_job(jobs_dir: Path | str, job_id: str) -> dict[str, Any]:
    """THE single formal terminal-recovery entry for a job whose terminal
    publish failed (or whose history is partially settled).

    It ONLY replays the frozen terminal intent: it never re-runs the
    runner/ELCC compute/report export/business publication, never creates
    a second job or run, never lets the caller re-choose the target
    state, and never changes the underlying run lifecycle. The frozen
    intent is verified (structure, job identity, content hash); the same
    control gate as the terminal publish is taken (inside
    _publish_terminal_state) so the job record and the FULL request
    history are re-read under the gate and the remaining settlement
    completes idempotently.

    Stable errors: WORKER_TERMINAL_RECOVERY_OWNER_ACTIVE (a live
    worker/task owner may still be publishing), WORKER_TERMINAL_INTENT_MISSING,
    WORKER_TERMINAL_INTENT_INVALID (tampered structure/identity/hash),
    WORKER_TERMINAL_STATE_CONFLICT (the frozen target contradicts the
    settled request history). A job already terminal with a fully settled
    history is a stable NOT_REQUIRED no-op - never a blind
    state-trusting return."""
    # path safety BEFORE resolving (never resolve-follow a swapped link)
    _assert_safe_control_tree(_jobs_root(jobs_dir), job_id)
    jobs_dir = _jobs_root(jobs_dir).resolve()
    job = _read_job(jobs_dir, job_id)
    state = job.get("state")
    # a live worker/task owner may still be publishing: refuse (the task
    # lock is the owner evidence; probed, never acquired here)
    task_lock = RunLock(jobs_dir / "locks", _lock_key(jobs_dir, job))
    if task_lock.is_held():
        raise WorkerError(
            _WORKER_TERMINAL_RECOVERY_OWNER_ACTIVE,
            f"a live worker/task owner of job {job_id} may still be "
            "publishing; terminal recovery refused",
        )
    store = _control_store(jobs_dir, job_id)
    history = _safe_request_list(store)
    if any(payload.get("invalid") for payload in history):
        raise WorkerError(
            _WORKER_CONTROL_REQUEST_INVALID,
            f"corrupt control-plane request evidence in job {job_id}; "
            "terminal recovery blocked (never guessed around)",
        )
    open_requests = [
        payload
        for payload in history
        if payload.get("request_status") in ("pending", "accepted")
    ]
    intent: dict[str, Any] | None = None
    intent_error: WorkerError | None = None
    try:
        intent = _load_terminal_intent(jobs_dir, job_id)
    except WorkerError as exc:
        intent_error = exc
    # an INVALID intent (file exists but fails structure/identity/hash)
    # is never swallowed by a fully settled history; only a genuinely
    # MISSING intent takes the NOT_REQUIRED compatibility path
    if intent_error is not None and intent_error.code == _WORKER_TERMINAL_INTENT_INVALID:
        raise intent_error
    if intent is not None:
        _verify_intent_identity(intent, job, job_id)
    if state in ("completed", "cancelled", "failed", "blocked"):
        if intent is not None and intent["fixed_target_state"] != state:
            raise WorkerError(
                _WORKER_TERMINAL_STATE_CONFLICT,
                f"job {job_id} is terminal as {state} but the frozen intent "
                f"targets {intent['fixed_target_state']}",
            )
        if not open_requests:
            # fully settled history: a stable no-op - but a legal intent
            # must FIRST pass the exact settled-request consistency check
            # against the frozen target (never a blind state-trusting
            # marker update); a conflict is stable and writes nothing
            if intent is not None:
                _check_settled_requests_consistent(history, intent, job, job_id)
            if intent is not None and intent.get("publish_state") != "published":
                intent["publish_state"] = "published"
                atomic_write_object(_terminal_intent_path(jobs_dir, job_id), intent)
            return {
                "job_id": job_id,
                "state": state,
                "recovered": False,
                "code": _WORKER_TERMINAL_RECOVERY_NOT_REQUIRED,
            }
        # terminal job with an incompletely settled history: only a legal
        # frozen intent authorizes the controlled reconcile (never guess)
        if intent is None:
            assert intent_error is not None
            raise intent_error
        _check_settled_requests_consistent(history, intent, job, job_id)
        _publish_terminal_state(
            jobs_dir,
            job_id,
            store,
            job,
            state=state,
            diagnostic_code=intent["diagnostic_code"],
            honoured=intent["honoured"],
            result=intent["result"],
            progress=None,  # terminal progress/job state already published
            job_fields=None,
        )
        intent["publish_state"] = "published"
        atomic_write_object(_terminal_intent_path(jobs_dir, job_id), intent)
        return {"job_id": job_id, "state": state, "recovered": True, "code": "RECONCILED"}
    # a non-terminal job REQUIRES a legal frozen intent (never guessed)
    if intent is None:
        assert intent_error is not None
        raise intent_error
    target = intent["fixed_target_state"]
    _check_settled_requests_consistent(history, intent, job, job_id)
    _publish_terminal_state(
        jobs_dir,
        job_id,
        store,
        job,
        state=target,
        diagnostic_code=intent["diagnostic_code"],
        honoured=intent["honoured"],
        result=intent["result"],
        progress=intent["progress"],
        job_fields=intent["job_fields"],
    )
    intent["publish_state"] = "published"
    atomic_write_object(_terminal_intent_path(jobs_dir, job_id), intent)
    return {"job_id": job_id, "state": target, "recovered": True, "code": "RECOVERED"}


class JobSummary(TypedDict):
    """One read-only job row for the business workbench queue page
    (T26A-MUST-02): formal job record fields plus the current diagnostic
    code from the read-only progress projection; a corrupt record is an
    invalid entry, never silently skipped."""

    job_id: str
    task: str
    state: str
    run_id: str | None
    requested_by: str
    created_at: str
    updated_at: str
    diagnostic_code: str | None
    invalid: bool
    invalid_reason: str | None


def _job_summary(root: Path, job_id: str, record: dict[str, Any]) -> JobSummary:
    """THE single JobSummary projection (shared by list_jobs and
    find_job_by_identity): every source field is strictly validated
    (never a get() default or str() coercion masking a missing/mistyped
    field) and the diagnostic code comes from the read-only progress
    projection; raises WorkerError on any missing/illegal field."""
    for field in ("task", "state", "requested_by", "created_at", "updated_at"):
        if not isinstance(record.get(field), str):
            raise WorkerError("WORKER_JOB_INVALID", f"missing/illegal field: {field}")
    if "run_id" not in record or not isinstance(record["run_id"], (str, type(None))):
        raise WorkerError("WORKER_JOB_INVALID", "missing/illegal field: run_id")
    task = str(record["task"])
    state = str(record["state"])
    if task not in (_DEMO_TASK, _ADEQUACY_TASK):
        raise WorkerError("WORKER_JOB_INVALID", f"unknown task: {task!r}")
    if state not in ("queued", "running", "completed", "failed", "cancelled", "blocked"):
        raise WorkerError("WORKER_JOB_INVALID", f"unknown state: {state!r}")
    diagnostic_code: str | None = None
    progress_path = _progress_file(root, job_id)
    if progress_path.is_file():
        try:
            payload = json.loads(progress_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                value = payload.get("diagnostic_code")
                diagnostic_code = str(value) if value is not None else None
        except (OSError, ValueError):
            # a damaged progress projection never invalidates the
            # formal job record itself
            diagnostic_code = None
    return JobSummary(
        job_id=str(record["job_id"]),
        task=task,
        state=state,
        run_id=record.get("run_id"),
        requested_by=str(record.get("requested_by", "")),
        created_at=str(record.get("created_at", "")),
        updated_at=str(record.get("updated_at", "")),
        diagnostic_code=diagnostic_code,
        invalid=False,
        invalid_reason=None,
    )


def list_jobs(jobs_dir: Path | str) -> list[JobSummary]:
    """THE read-only job-list aggregation for the business workbench
    (T26A-MUST-02): enumerates ONLY first-level job directories, never
    recurses, never follows or writes anything; hidden directories
    (".*") and non-directories are skipped; the RESERVED control-plane
    lock namespace ("locks") is control evidence, never a job directory,
    and is skipped; a missing jobs_dir returns an empty list; a corrupt
    job.json becomes an invalid entry (never silently dropped, never
    presented as a valid business task).
    Ordering is deterministic (created_at descending, job_id ascending
    on ties); repeated calls return identical results."""
    root = _jobs_root(jobs_dir)
    _assert_safe_control_tree(root)
    if not root.is_dir():
        return []
    results: list[JobSummary] = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or entry.name == "locks":
            # the reserved control-plane lock namespace is never a job
            # directory
            continue
        # EVERY reparse type (symlink, junction, any reparse attribute) is
        # skipped WITHOUT following: the shared check runs BEFORE is_dir
        # would traverse the link, so the link target gets zero reads
        link_reason = unsafe_path_component(entry, directory=None)
        if link_reason is not None:
            continue
        if not entry.is_dir():
            continue
        # the WHOLE single-entry read+validate+project sequence stays
        # inside the per-entry isolation boundary: one damaged record
        # never aborts the listing and never becomes invalid=False
        try:
            job_id = safe_filename_component(entry.name)
            record = _read_job(root, job_id)
            results.append(_job_summary(root, job_id, record))
        except (OSError, ValueError, WorkerError) as exc:
            results.append(
                JobSummary(
                    job_id=entry.name,
                    task="",
                    state="",
                    run_id=None,
                    requested_by="",
                    created_at="",
                    updated_at="",
                    diagnostic_code=None,
                    invalid=True,
                    invalid_reason=str(exc)[:300],
                )
            )
            continue
    results.sort(key=lambda item: item["job_id"])
    results.sort(key=lambda item: item["created_at"], reverse=True)
    return results


def worker_status(jobs_dir: Path | str, job_id: str) -> dict[str, Any]:
    """PURE READ-ONLY status aggregation (never acquires a lock, never
    starts a process, never sends a control request, never writes any
    file): job record + execution/task RunLock probe + progress
    projection + FULL control-request history + the EXISTING monitor's
    status.json (process tree/CPU/memory quality) + checkpoint/run
    association. Acceptance metrics are merged from both layers:
    business progress AND process health."""
    # path safety BEFORE resolving (never resolve-follow a swapped link)
    _assert_safe_control_tree(_jobs_root(jobs_dir), job_id)
    jobs_dir = _jobs_root(jobs_dir).resolve()
    job = _read_job(jobs_dir, job_id)
    status: dict[str, Any] = {"job": job}
    progress_path = _progress_file(jobs_dir, job_id)
    progress = (
        json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.is_file() else None
    )
    status["progress"] = progress
    worker_pid = job.get("worker_pid") or (progress or {}).get("pid")
    status["worker_pid"] = worker_pid
    status["monitor_pid"] = job.get("monitor_pid")
    diagnostic_code = (progress or {}).get("diagnostic_code")
    status["diagnostic_code"] = diagnostic_code
    # ---- business progress projection
    status["phase"] = (progress or {}).get("phase")
    status["current_subtask"] = (progress or {}).get("current_subtask")
    status["completed_work"] = (progress or {}).get("completed_work")
    status["total_work"] = (progress or {}).get("total_work")
    status["last_progress_at"] = (progress or {}).get("last_progress_at")
    status["lifecycle_status"] = (progress or {}).get("lifecycle_status")
    status["checkpoint_ref"] = (progress or {}).get("checkpoint_ref")
    # ---- locks: execution lock (adequacy, per run) or task lock (demo)
    lock_refs: dict[str, Any] = {}
    if job["task"] == _ADEQUACY_TASK and job.get("run_id"):
        execution = RunLock(
            Path(job["params"]["project_dir"]) / "locks" / "adequacy-execution",
            str(job["run_id"]),
        )
        lock_refs["execution"] = str(execution.path)
        lock_refs["execution_held"] = execution.is_held()
    task_lock = RunLock(jobs_dir / "locks", _lock_key(jobs_dir, job))
    lock_refs["task"] = str(task_lock.path)
    lock_refs["task_held"] = task_lock.is_held()
    status["lock_held"] = bool(lock_refs.get("execution_held") or lock_refs.get("task_held"))
    status["lock_ref"] = lock_refs.get("execution") or lock_refs.get("task")
    status["lock_refs"] = lock_refs
    # ---- full control request history (never only the pending subset)
    store = _control_store(jobs_dir, job_id)
    status["control_requests"] = _safe_request_list(store) if store.directory.is_dir() else []
    # ---- the EXISTING monitor's status.json (two-layer process health)
    monitor_status_path = _job_dir(jobs_dir, job_id) / "monitor" / job_id / "status.json"
    for monitor_part in (
        _job_dir(jobs_dir, job_id) / "monitor",
        _job_dir(jobs_dir, job_id) / "monitor" / job_id,
        monitor_status_path,
    ):
        monitor_reason = unsafe_path_component(monitor_part, directory=None)
        if monitor_reason is not None:
            raise WorkerError(
                _WORKER_JOB_PATH_UNSAFE,
                f"unsafe monitor status component ({monitor_reason}): {monitor_part}",
            )
    if monitor_status_path.is_file():
        try:
            monitor_status = json.loads(monitor_status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            monitor_status = None
        status["monitor_status"] = monitor_status
        if monitor_status:
            status["process_state"] = monitor_status.get("process_state")
            status["outcome"] = monitor_status.get("outcome")
            status["monitor_state"] = monitor_status.get("monitor_state", "OK")
            status["monitor_degraded"] = status["monitor_state"] != "OK"
            status["metric_scope"] = monitor_status.get("metric_scope")
            status["metric_sample_quality"] = monitor_status.get("metric_sample_quality")
            status["sampled_process_count"] = monitor_status.get("sampled_process_count")
            status["cpu_seconds"] = monitor_status.get("cpu_seconds")
            status["working_set_bytes"] = monitor_status.get("working_set_bytes")
            status["monitor_pid"] = job.get("monitor_pid")
    if status.get("monitor_state") is None:
        status["monitor_state"] = "MISSING"
        status["monitor_degraded"] = True
    try:
        status["monitor_last_update_at"] = (
            datetime.fromtimestamp(monitor_status_path.stat().st_mtime, UTC).isoformat()
            if monitor_status_path.is_file()
            else None
        )
    except OSError:
        status["monitor_last_update_at"] = None
    # ---- terminal recovery state (READ-ONLY verification; never writes
    # the intent, the job or any request): missing / available / invalid /
    # not_required, with the stable diagnostic on invalid evidence
    intent_path = _terminal_intent_path(jobs_dir, job_id)
    if job.get("state") in ("completed", "failed", "cancelled", "blocked"):
        status["recovery_state"] = "not_required"
        status["recovery_available"] = False
    elif not intent_path.is_file():
        status["recovery_state"] = "missing"
        status["recovery_available"] = False
    else:
        try:
            intent = _load_terminal_intent(jobs_dir, job_id)
            _verify_intent_identity(intent, job, job_id)
        except WorkerError as exc:
            status["recovery_state"] = "invalid"
            status["recovery_available"] = False
            status["recovery_diagnostic_code"] = exc.code
        else:
            status["recovery_state"] = "available"
            status["recovery_available"] = True
    # ---- checkpoint / run association (adequacy)
    if job["task"] == _ADEQUACY_TASK and job.get("run_id"):
        checkpoint = (
            Path(job["params"]["project_dir"])
            / "runs"
            / safe_filename_component(str(job["run_id"]))
            / "checkpoint.json"
        )
        status["checkpoint_present"] = checkpoint.is_file()
        manifest = (
            Path(job["params"]["project_dir"])
            / "runs"
            / safe_filename_component(str(job["run_id"]))
            / "run_manifest.json"
        )
        if manifest.is_file():
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            status["run_lifecycle_status"] = payload.get("lifecycle_status")
    return status


# ----------------------------------------------------------------------
# subprocess entry
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 1 and argv[0] == "run":
        job_id = ""
        jobs_dir = ""
        index = 1
        while index < len(argv):
            if argv[index] == "--job-id":
                job_id = argv[index + 1]
                index += 2
            elif argv[index] == "--jobs-dir":
                jobs_dir = argv[index + 1]
                index += 2
            else:
                index += 1
        if not job_id or not jobs_dir:
            print("WORKER_JOB_INVALID: --job-id and --jobs-dir are required", file=sys.stderr)
            return 4
        return execute_job(jobs_dir, job_id)
    print("WORKER_ENTRY_INVALID", file=sys.stderr)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EnsureJobResult",
    "JobSummary",
    "WorkerError",
    "create_job",
    "ensure_job",
    "execute_job",
    "find_job_by_identity",
    "launch",
    "list_jobs",
    "main",
    "recover_terminal_job",
    "resume_job",
    "stop_job",
    "unsafe_path_component",
    "worker_status",
]
