"""T26-A run-center launch contract: THE thin teaching-demo application
service - the ONLY entry a UI page may call for the workspace-level
teaching demo task.

Design contract:
docs/architecture/2026-09-03-t26a-run-center-launch-contract-design.md.

Hard boundaries:

- the UI passes ONLY workspace_root and requested_by - never a jobs_dir,
  config path, output_dir, task_identity or any worker parameter;
- the launch target is the WORKSPACE-LEVEL T24-B system teaching demo
  (t24b-wind-elcc-teaching) - never the selected project's formal
  adequacy run; no project identity ever enters the task identity;
- describe_teaching_demo is PURE READ-ONLY (it never creates
  .capacity-credit, teaching-results or any other file);
- start_teaching_demo re-resolves EVERYTHING (case, config hash, software
  identity, task identity) inside the call - it never trusts a cached
  descriptor; the atomic identity ensure lives in worker.ensure_job (the
  only job persistence), and launch remains the existing idempotent
  entry;
- no second queue, lock family, state machine, business config store or
  task mapping file is created here; no compute kernel/solver/engine is
  imported; requested_by is an audit field only (never part of the task
  identity, never overwriting the original audit).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict

from capacity_credit.application import worker
from capacity_credit.demo import business_demo
from capacity_credit.demo.business_demo import DemoError, TeachingCaseDefinition

_TEACHING_CASE_ID = "t24b-wind-elcc-teaching"

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TeachingDemoDescriptor(TypedDict):
    """The read-only teaching-demo page model facts: case identity, the
    deterministic task identity, the shared teaching use-boundary code,
    the software commit and config hash the identity binds to, the
    derived output directory, and the existing job (if any) to attach.

    available=False carries the ORIGINAL stable error code in
    unavailable_reason (never a degraded plain message); the remaining
    identity fields are then honestly empty/None."""

    case_id: str
    display_name: str
    task_identity: str
    use_boundary_code: str
    source_software_commit: str
    config_sha256: str
    output_dir: str
    job_id: str | None
    job_state: str | None
    available: bool
    unavailable_reason: str | None


class TeachingDemoStartResult(TypedDict):
    """The business handle of a start call: which job, which identity,
    which output directory, whether THIS call created the job and
    whether THIS call invoked the (idempotent) launch. Never an
    executable command."""

    job_id: str
    task_identity: str
    output_dir: str
    created: bool
    launched: bool
    code: str


def _validate_workspace(workspace_root: Path | str) -> Path:
    """THE workspace boundary gate (fail-closed), in the FIXED order:

    1. the workspace_root must be an explicit, existing, ABSOLUTE path
       (never derived from cwd, environment variables, the user profile
       or the source tree);
    2. LEXICAL boundary check (no link following): not the repository,
       not inside it (tests/ and tmp/ live inside the repository);
    3. per-component reparse-point checks on every EXISTING component of
       the path chain (THE shared worker check - before ANY resolve);
    4. only then normalize (resolve) and re-verify the containment;
    5. the actual use boundaries (ensure/launch/execute/run_demo) each
       re-check their own controlled components.
    """
    text = str(workspace_root)
    if not text.strip():
        raise DemoError(
            "DEMO_WORKSPACE_INVALID", "workspace_root must be an explicit non-empty path"
        )
    raw = Path(text)
    if not raw.is_absolute():
        raise DemoError(
            "DEMO_WORKSPACE_INVALID",
            f"workspace_root must be an absolute path: {text!r}",
        )
    repo_key = os.path.normcase(str(_REPO_ROOT))
    # step 2: lexical boundary (no link following)
    lexical = os.path.normcase(os.path.normpath(str(raw)))
    if lexical == repo_key or lexical.startswith(repo_key + os.sep):
        raise DemoError(
            "DEMO_WORKSPACE_INVALID",
            "the workspace must not be the source repository or inside it "
            "(tests/ and tmp/ are inside the repository boundary)",
        )
    # step 3: per-component reparse checks BEFORE any resolve
    for part in (raw, *raw.parents):
        reason = worker.unsafe_path_component(part, directory=True)
        if reason is not None:
            raise DemoError(
                "DEMO_WORKSPACE_INVALID",
                f"workspace path component unsafe ({reason}, fail-closed): {part}",
            )
    if not raw.is_dir():
        raise DemoError(
            "DEMO_WORKSPACE_INVALID",
            f"workspace_root does not exist or is not a directory: {text!r}",
        )
    # step 4: only now normalize and re-verify the containment
    resolved = raw.resolve()
    workspace_key = os.path.normcase(str(resolved))
    if workspace_key == repo_key or workspace_key.startswith(repo_key + os.sep):
        raise DemoError(
            "DEMO_WORKSPACE_INVALID",
            "the workspace must not be the source repository or inside it "
            "(tests/ and tmp/ are inside the repository boundary)",
        )
    return resolved


def _task_identity(case: TeachingCaseDefinition, commit: str) -> str:
    """The deterministic task identity via THE single canonical recipe
    (business_demo.compute_teaching_task_identity - shared with the worker
    execution gate). A config or software-commit change is a NEW identity
    and therefore a NEW output directory."""
    return business_demo.compute_teaching_task_identity(
        case["case_id"], case["config_sha256"], commit
    )


def _assert_derived_paths_safe(
    workspace: Path, jobs_dir: Path, output_dir: Path
) -> None:
    """Fail-closed path safety for the DERIVED controlled components
    (called by BOTH describe and start; the worker's own gate is an
    INDEPENDENT second layer, never a substitute): every EXISTING
    component - .capacity-credit/, jobs/, locks/, locks/job-intents/,
    teaching-results/, the teaching case directory and the deterministic
    output directory - must be a REAL directory. A symlink, junction,
    other reparse point or a file-instead-of-directory is
    DEMO_WORKSPACE_INVALID; suspicious paths are never resolve-followed,
    deleted or rebuilt."""
    control = workspace / ".capacity-credit"
    results = workspace / "teaching-results"
    candidates = [
        control,
        control / "jobs",
        control / "jobs" / "locks",
        control / "jobs" / "locks" / "job-intents",
        results,
        results / _TEACHING_CASE_ID,
        output_dir,
    ]
    for path in candidates:
        reason = worker.unsafe_path_component(path, directory=True)
        if reason is not None:
            raise DemoError(
                "DEMO_WORKSPACE_INVALID",
                f"controlled path component unsafe ({reason}, fail-closed): {path}",
            )


def _derive_paths(workspace: Path, task_identity: str) -> tuple[Path, Path]:
    """THE directory contract: jobs_dir = <workspace>/.capacity-credit/
    jobs and output_dir = <workspace>/teaching-results/
    t24b-wind-elcc-teaching/<FULL 64-hex task_identity> - the output
    directory never relies on a truncated identity prefix (two full
    identities sharing even the 32-hex job-id prefix get DIFFERENT
    directories; the same identity always gets the same directory; no
    time, pid, session or random value enters the allocation). The
    containment check here is LEXICAL (never resolve-follows); reparse
    safety and the post-safety resolve re-verification are separate
    steps. Query paths never create these directories; creation happens
    only through worker.ensure_job and the formal worker execution."""
    jobs_dir = workspace / ".capacity-credit" / "jobs"
    output_dir = workspace / "teaching-results" / _TEACHING_CASE_ID / task_identity
    # LEXICAL containment only (never resolve-follows): both paths are
    # built from the validated workspace with fixed components
    base = os.path.normcase(os.path.normpath(str(workspace)))
    for derived in (jobs_dir, output_dir):
        key = os.path.normcase(os.path.normpath(str(derived)))
        if key != base and not key.startswith(base + os.sep):
            raise DemoError(
                "DEMO_WORKSPACE_INVALID",
                f"derived path escapes the workspace: {derived}",
            )
    return jobs_dir, output_dir


def _recheck_derived_containment(
    workspace: Path, jobs_dir: Path, output_dir: Path
) -> None:
    """Post-safety re-verification at the use boundary: AFTER the reparse
    checks, resolve and re-prove containment (defense in depth)."""
    base = os.path.normcase(str(workspace))
    for derived in (jobs_dir, output_dir):
        key = os.path.normcase(str(derived.resolve()))
        if key != base and not key.startswith(base + os.sep):
            raise DemoError(
                "DEMO_WORKSPACE_INVALID",
                f"derived path escapes the workspace: {derived}",
            )


def describe_teaching_demo(workspace_root: Path | str) -> TeachingDemoDescriptor:
    """PURE READ-ONLY teaching-demo description (never creates a
    directory or file): workspace gate -> formal case resolution -> the
    EXISTING shared software-identity entry -> task identity -> derived
    paths -> worker.find_job_by_identity.

    A workspace contract violation raises DEMO_WORKSPACE_INVALID. A
    case/config/software-identity unavailability is reported as
    available=False with the ORIGINAL stable code in unavailable_reason
    (never degraded, never a fake success)."""
    workspace = _validate_workspace(workspace_root)
    try:
        case = business_demo.resolve_teaching_case(_TEACHING_CASE_ID)
        commit = business_demo.source_identity()["head"]
    except DemoError as exc:
        return TeachingDemoDescriptor(
            case_id=_TEACHING_CASE_ID,
            display_name="",
            task_identity="",
            use_boundary_code="",
            source_software_commit="",
            config_sha256="",
            output_dir="",
            job_id=None,
            job_state=None,
            available=False,
            unavailable_reason=exc.code,
        )
    identity = _task_identity(case, commit)
    jobs_dir, output_dir = _derive_paths(workspace, identity)
    _assert_derived_paths_safe(workspace, jobs_dir, output_dir)
    _recheck_derived_containment(workspace, jobs_dir, output_dir)
    summary = worker.find_job_by_identity(jobs_dir, identity)
    return TeachingDemoDescriptor(
        case_id=case["case_id"],
        display_name=case["display_name"],
        task_identity=identity,
        use_boundary_code=case["use_boundary_code"],
        source_software_commit=commit,
        config_sha256=case["config_sha256"],
        output_dir=str(output_dir),
        job_id=summary["job_id"] if summary is not None else None,
        job_state=summary["state"] if summary is not None else None,
        available=True,
        unavailable_reason=None,
    )


def start_teaching_demo(
    workspace_root: Path | str,
    *,
    requested_by: str,
) -> TeachingDemoStartResult:
    """Ensure AND attach the workspace-level teaching demo job, launching
    it exactly when the formal job state is queued.

    EVERYTHING is re-resolved inside the call (never a cached
    descriptor): workspace gate, requested_by audit validation, the
    formal case, the shared software identity, the deterministic task
    identity, then worker.ensure_job (the atomic single-job contract).
    A queued job is launched ONCE through the existing idempotent
    launch; running/completed/failed/cancelled/blocked jobs are
    attach-only (no second task, no second spawn, no new attempt - the
    first slice offers no rerun)."""
    workspace = _validate_workspace(workspace_root)
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise DemoError(
            "DEMO_REQUESTED_BY_INVALID",
            "requested_by must be a non-empty audit identity",
        )
    case = business_demo.resolve_teaching_case(_TEACHING_CASE_ID)
    commit = business_demo.source_identity()["head"]
    identity = _task_identity(case, commit)
    jobs_dir, output_dir = _derive_paths(workspace, identity)
    _assert_derived_paths_safe(workspace, jobs_dir, output_dir)
    _recheck_derived_containment(workspace, jobs_dir, output_dir)
    # the bound case id, config hash and software commit travel with the
    # job: the worker re-verifies them BEFORE any compute or artifact
    # write (and run_demo re-verifies at the actual consumption point)
    params = {
        "config": case["config_path"],
        "output_dir": str(output_dir),
        "case_id": case["case_id"],
        "config_sha256": case["config_sha256"],
        "source_software_commit": commit,
    }
    ensured = worker.ensure_job(
        jobs_dir,
        task_identity=identity,
        task="demo",
        params=params,
        requested_by=requested_by,
    )
    job_id = ensured["job_id"]
    summary = worker.find_job_by_identity(jobs_dir, identity)
    if summary is None:
        # defensive: ensure just returned this job - a missing record now
        # is incomplete evidence, never a silent re-create
        raise worker.WorkerError(
            "WORKER_JOB_CREATION_INCOMPLETE",
            f"ensured job {job_id} is not readable back",
        )
    launched = False
    if summary["state"] == "queued":
        worker.launch(jobs_dir, job_id)
        launched = True
    return TeachingDemoStartResult(
        job_id=job_id,
        task_identity=identity,
        output_dir=str(output_dir),
        created=ensured["created"],
        launched=launched,
        code=ensured["code"],
    )


__all__ = [
    "TeachingDemoDescriptor",
    "TeachingDemoStartResult",
    "describe_teaching_demo",
    "start_teaching_demo",
]
