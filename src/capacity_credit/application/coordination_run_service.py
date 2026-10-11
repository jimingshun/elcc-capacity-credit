"""Shared coordination-run helper (T19 round-2, P4).

One small component used by BOTH ``portfolio_service`` and
``sensitivity_service`` for the coordination-run lifecycle and the
artifact-manifest-1.1 coordination publication - there is no second copy
of the run-state machine, manifest construction or atomic writer.

Lifecycle contract (E):
  created -> queued -> running -> [compute + write artifacts] -> completed
  queued/running happen BEFORE any computation / artifact write; an
  unhandled error during compute/write transitions the parent run to
  ``failed`` (with the error code and audit event) and never writes
  ``completed``; the immutable manifest is published only after all
  validation passes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Self

from capacity_credit.application.atomic_io import atomic_write_object, safe_filename_component
from capacity_credit.application.locks import RunLock
from capacity_credit.application.result_service import (
    _file_sha256,
    read_artifact_manifest,
    validate_manifest_before_publish,
)
from capacity_credit.contracts.artifacts import ArtifactEntry, ArtifactManifest11
from capacity_credit.contracts.enums import (
    ArtifactType,
    ConfidentialityLevel,
    LifecycleStatus,
    RedactionStatus,
)


class CoordinationRunServiceError(ValueError):
    """Stable coordination-run error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


P_COORDINATION_RUN_FAILED = "P_COORDINATION_RUN_FAILED"
P_COORDINATION_RUN_INVALID = "P_COORDINATION_RUN_INVALID"
P_COORDINATION_RUN_IN_PROGRESS = "P_COORDINATION_RUN_IN_PROGRESS"
P_COORDINATION_RUN_STOPPING = "P_COORDINATION_RUN_STOPPING"
P_COORDINATION_MANIFEST_INVALID = "P_COORDINATION_MANIFEST_INVALID"
P_COORDINATION_START_FAILED = "P_COORDINATION_START_FAILED"
P_COORDINATION_RECOVERY_FAILED = "P_COORDINATION_RECOVERY_FAILED"



class CoordinationAction(str, Enum):
    """The typed recovery decision for a coordination parent run (the SINGLE
    decision used by portfolio_service AND sensitivity_service)."""

    EXECUTE_NEW = "execute_new"
    RESUME_COMPUTE = "resume_compute"
    FINALIZE_EXISTING = "finalize_existing"
    REPLAY_COMPLETED = "replay_completed"


class CoordinationExecutionClaim:
    """Cross-process single-flight execution claim for ONE coordination
    run_id (application service contract V0.2 §5.1/§5.2).

    Reuses the existing shared ``RunLock`` - there is NO second lock
    module, queue or scheduler - in the dedicated
    ``locks/coordination-execution`` namespace so the claim can span the
    WHOLE cycle (recovery decision -> compute -> child scheduling ->
    artifact + manifest publish -> completed/failed transition) without
    re-entering or deadlocking the ``locks/runs`` lifecycle-transition
    lock held by RunService.transition.

    The OS-held byte-range lock is the ONLY authority for an active
    owner: a failed acquire means a LIVE process is computing/publishing
    this run; heartbeat time is never used to guess owner staleness.
    Lock files/metadata live under ``locks/`` and are never registered
    as business artifacts.

    ``acquire`` is a single non-blocking attempt; ``release`` (also on
    context exit and on garbage collection) is idempotent."""

    def __init__(self, project: Path, run_id: str) -> None:
        self._lock = RunLock(project / "locks" / "coordination-execution", run_id)
        self._acquired = False

    def acquire(self) -> bool:
        """Try to become the execution owner (non-blocking, single attempt).
        Returns False when a LIVE owner holds the claim - the caller must
        NOT resume compute, NOT call the runner, NOT write artifacts and
        NOT create a second parent run."""
        if self._acquired:
            return True
        if self._lock.acquire():
            self._acquired = True
            return True
        return False

    def release(self) -> None:
        """Release the claim if held (idempotent)."""
        if self._acquired:
            self._lock.release()
            self._acquired = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


@dataclass(frozen=True)
class CoordinationRecovery:
    """The typed result of creating / resuming a coordination parent run.

    - EXECUTE_NEW / RESUME_COMPUTE: the caller computes and writes fresh
      artifacts (RESUME_COMPUTE only when NO coordination artifact bytes
      exist yet; it never repeats an illegal lifecycle transition);
    - FINALIZE_EXISTING: the artifacts AND a VALID matching coordination
      manifest already exist while the run is still running; the caller
      must NOT recompute/rewrite anything, must read back + validate its
      business artifact and analysis-spec identity, then ONLY add the
      completed transition;
    - REPLAY_COMPLETED: the run is completed with a valid matching
      manifest; strict read-back only (no recompute, no rewrite, no
      transition).

    Only consumed fields are kept: ``manifest`` supplies the validated
    recovery metadata and ``manifest_id``; ``manifest_path`` is the immutable
    manifest location returned to callers."""

    action: CoordinationAction
    run_id: str
    run_dir: Path
    claim: CoordinationExecutionClaim
    manifest: ArtifactManifest11 | None = None
    manifest_path: Path | None = None

    @property
    def manifest_id(self) -> str | None:
        return str(self.manifest.artifact_manifest_id) if self.manifest is not None else None


def _read_existing_coordination_manifest(
    run_dir: Path, run_id: str, run_type: str
) -> tuple[bool, ArtifactManifest11 | None]:
    """Read + validate an existing 1.1 COORDINATION manifest.

    Returns ``(present, manifest)`` so that "the manifest file does NOT
    exist at all" is DISTINGUISHED from "the manifest file exists but is
    corrupt / invalid / does not match this run": ``present=True`` with
    ``manifest=None`` means present-but-invalid (a partial/corrupt
    publish), never silently treated as "no manifest"."""
    manifest_path = run_dir / "artifact_manifest.json"
    if not manifest_path.is_file():
        return False, None
    try:
        manifest = read_artifact_manifest(
            manifest_path,
            result_path=run_dir / "coordination",
            expected_run_id=run_id,
            expected_result_id=None,
        )
    except Exception:  # noqa: BLE001 - present but corrupt / invalid
        return True, None
    if not isinstance(manifest, ArtifactManifest11):
        return True, None
    if manifest.manifest_kind != "coordination" or manifest.run_type != run_type:
        return True, None
    return True, manifest


def _require_expected_artifacts(
    *,
    run_dir: Path,
    run_id: str,
    run_type: str,
    manifest: ArtifactManifest11,
    expected_artifacts: tuple[str, ...],
) -> None:
    """Require the service's business artifacts in a validated manifest.

    read_artifact_manifest already validates every registered entry's safe
    path, existence and SHA-256. This additional check closes the service
    boundary: a valid manifest for unrelated artifacts is not a valid
    replay/finalization result for the current request.
    """
    manifest_names = {entry.relative_path for entry in manifest.artifacts}
    missing = [
        name
        for name in expected_artifacts
        if name not in manifest_names
        or not (run_dir / safe_filename_component(name)).is_file()
    ]
    if missing:
        raise CoordinationRunServiceError(
            P_COORDINATION_RECOVERY_FAILED,
            f"{run_type} coordination run {run_id} has a valid manifest but expected "
            f"business artifact(s) {missing} are missing or not registered; replay or "
            "finalization is blocked and existing immutable files are not overwritten",
        )


def _running_recovery(
    run_dir: Path,
    run_id: str,
    run_type: str,
    expected_artifacts: tuple[str, ...],
    fresh: bool,
    claim: CoordinationExecutionClaim,
) -> CoordinationRecovery:
    """The running-state recovery decision (P0 late-interruption closure):

    - no manifest AND no business artifact bytes -> EXECUTE_NEW when the
      run was JUST brought to running (a fresh execution) or RESUME_COMPUTE
      when it was ALREADY running (a recovering interruption) - in both
      cases the caller computes and writes fresh, never repeating an
      illegal transition;
    - no manifest but SOME business artifact bytes -> stable recovery error
      (partial publish; existing immutable files are never overwritten);
    - manifest present but corrupt / not matching -> stable recovery error;
    - manifest valid AND every entry file exists with a matching hash AND
      every expected business artifact is present -> FINALIZE_EXISTING."""
    present, manifest = _read_existing_coordination_manifest(run_dir, run_id, run_type)
    if not present:
        partial = [
            name
            for name in expected_artifacts
            if (run_dir / safe_filename_component(name)).is_file()
        ]
        if partial:
            raise CoordinationRunServiceError(
                P_COORDINATION_RECOVERY_FAILED,
                f"{run_type} coordination run {run_id} is running with a PARTIAL publish: "
                f"business artifact(s) {partial} exist but the coordination manifest is "
                "missing; existing immutable files are NOT overwritten and the run is NOT "
                "marked completed - use a NEW request_id or perform controlled recovery",
            )
        action = (
            CoordinationAction.EXECUTE_NEW if fresh else CoordinationAction.RESUME_COMPUTE
        )
        return CoordinationRecovery(
            action=action, run_id=run_id, run_dir=run_dir, claim=claim
        )
    if manifest is None:
        raise CoordinationRunServiceError(
            P_COORDINATION_RECOVERY_FAILED,
            f"{run_type} coordination run {run_id} has a coordination manifest that is "
            "CORRUPT or does not match the run; existing files are NOT overwritten and the "
            "run is NOT marked completed - use a NEW request_id or perform controlled recovery",
        )
    _require_expected_artifacts(
        run_dir=run_dir,
        run_id=run_id,
        run_type=run_type,
        manifest=manifest,
        expected_artifacts=expected_artifacts,
    )
    return CoordinationRecovery(
        action=CoordinationAction.FINALIZE_EXISTING,
        run_id=run_id,
        run_dir=run_dir,
        manifest=manifest,
        manifest_path=run_dir / "artifact_manifest.json",
        claim=claim,
    )


def create_coordination_run(
    *,
    project: Path,
    run_service: Any,
    import_batch_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    run_type: str,
    run_config: dict[str, Any],
    request_id: str,
    scenario_bank_id: str,
    expected_artifacts: tuple[str, ...] = (),
) -> CoordinationRecovery:
    """Create a formal coordination parent run (run_type=portfolio /
    sensitivity), bring it to created -> queued -> running BEFORE any
    computation or artifact write, and return the recovery decision.

    Recovery model (P0): the SAME request_id returns the SAME run_id
    (RunService idempotency). ``expected_artifacts`` lists the business
    artifact filenames this request will write, used to distinguish
    "no artifacts yet" (RESUME_COMPUTE) from a PARTIAL publish (error).

    Decisions:
    - completed + valid matching coordination manifest containing every
      expected business artifact -> REPLAY_COMPLETED (strict read-back;
      nothing recomputed or rewritten);
    - completed + no manifest / corrupt manifest -> inconsistent-state error;
    - failed / cancelled -> stable terminal error (use a new request_id);
    - stopping -> stable P_COORDINATION_RUN_STOPPING error (never
      EXECUTE_NEW / RESUME_COMPUTE; no runner, no artifacts, the lifecycle
      is never flipped back to running);
    - unknown lifecycle -> stable error (never defaulted to running);
    - created/queued -> transition to running (round-4 startup state machine:
      queued failure keeps the run created; running failure attempts
      queued->failed and exposes the ORIGINAL error; a failed transition
      failure exposes both errors and never claims failed);
    - running + no artifacts -> RESUME_COMPUTE (no illegal transition);
    - running + valid complete manifest + complete artifacts ->
      FINALIZE_EXISTING (caller must NOT recompute/rewrite; read back +
      validate business spec identity, then only add completed);
    - running + partial artifacts / missing-or-corrupt manifest ->
      P_COORDINATION_RECOVERY_FAILED (nothing overwritten, never completed).

    Single flight (round 6): the returned recovery carries a HOLDING
    ``claim`` (CoordinationExecutionClaim) acquired BEFORE the state read
    and held across the whole recovery-decision -> compute -> publish ->
    completed/failed cycle; the caller MUST keep the claim held (enter its
    context or release it explicitly in a finally) until its work ends.
    The SECOND identical request for the same run_id cannot acquire the
    claim and gets the stable P_COORDINATION_RUN_IN_PROGRESS error - it
    never resumes compute, never calls the runner, never writes artifacts,
    never creates a second parent and never marks the first owner failed.
    After acquiring the claim the state AND the published artifacts are
    RE-READ (they may have changed while the claim was being obtained)."""
    try:
        created = run_service.create_run(
            request_id=request_id,
            run_config=run_config,
            import_batch_id=import_batch_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            scenario_bank_id=scenario_bank_id,
            teaching_override_reason="teaching fixture synthetic project (P4)",
            method_config_hash=None,
            application_config_hash=None,
        )
    except Exception as exc:
        raise CoordinationRunServiceError(
            P_COORDINATION_RUN_FAILED,
            f"failed to create {run_type} coordination run: {exc}",
        ) from exc
    run_id = str(created["run_id"])
    run_dir = project / "runs" / safe_filename_component(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Single-flight claim: acquired BEFORE the state read, held across the
    # whole execution cycle by the caller. A second identical request can
    # never obtain it while a LIVE owner computes (OS-held byte-range lock
    # is the only owner authority; heartbeat time is never used to guess
    # staleness).
    claim = CoordinationExecutionClaim(project, run_id)
    if not claim.acquire():
        raise CoordinationRunServiceError(
            P_COORDINATION_RUN_IN_PROGRESS,
            f"{run_type} coordination run {run_id} already has an ACTIVE execution "
            "owner (another process is computing/publishing this run); the request is "
            "NOT resumed, the runner is NOT called and no artifact is written - do NOT "
            "create a second parent run and do NOT mark the run failed",
        )
    try:
        # SECONDARY state check after acquiring the claim: the state may
        # have changed while the claim was being obtained.
        state_value = run_service.run_state(run_id)["lifecycle_status"]
        try:
            state = LifecycleStatus(state_value)
        except ValueError as exc:
            raise CoordinationRunServiceError(
                P_COORDINATION_RUN_INVALID,
                f"{run_type} coordination run {run_id} has UNKNOWN lifecycle status "
                f"{state_value!r}; refusing to guess recovery (never treated as running) - "
                "no runner, no artifact, no transition",
            ) from exc
        if state == LifecycleStatus.COMPLETED:
            present, manifest = _read_existing_coordination_manifest(run_dir, run_id, run_type)
            if not present:
                raise CoordinationRunServiceError(
                    P_COORDINATION_RUN_FAILED,
                    f"{run_type} coordination run {run_id} is completed but has NO "
                    "coordination manifest (inconsistent state)",
                )
            if manifest is None:
                raise CoordinationRunServiceError(
                    P_COORDINATION_RUN_FAILED,
                    f"{run_type} coordination run {run_id} is completed but its coordination "
                    "manifest is corrupt or does not match the run (inconsistent state)",
                )
            _require_expected_artifacts(
                run_dir=run_dir,
                run_id=run_id,
                run_type=run_type,
                manifest=manifest,
                expected_artifacts=expected_artifacts,
            )
            return CoordinationRecovery(
                action=CoordinationAction.REPLAY_COMPLETED,
                run_id=run_id,
                run_dir=run_dir,
                manifest=manifest,
                manifest_path=run_dir / "artifact_manifest.json",
                claim=claim,
            )
        if state in (LifecycleStatus.FAILED, LifecycleStatus.CANCELLED):
            raise CoordinationRunServiceError(
                P_COORDINATION_RUN_FAILED,
                f"{run_type} coordination run {run_id} is {state.value}; use a new request_id",
            )
        if state == LifecycleStatus.STOPPING:
            raise CoordinationRunServiceError(
                P_COORDINATION_RUN_STOPPING,
                f"{run_type} coordination run {run_id} is STOPPING (or stopped); it is NOT "
                "resumed: no runner, no new artifact, the lifecycle is NOT flipped back to "
                "running and the run is NOT marked completed/failed by this request - use a "
                "NEW request_id or perform controlled recovery",
            )

        def _transition(stage: str, target: str) -> None:
            run_service.transition(
                request_id=f"{request_id}-{stage}",
                run_id=run_id,
                target_lifecycle=target,
            )

        fresh = False
        if state == LifecycleStatus.CREATED:
            fresh = True
            try:
                _transition("queued", LifecycleStatus.QUEUED.value)
            except Exception as exc:
                raise CoordinationRunServiceError(
                    P_COORDINATION_START_FAILED,
                    f"{run_type} coordination run {run_id} queuing failed; the run stays "
                    f"created (recoverable, do NOT claim failed): {exc}",
                ) from exc
            try:
                _transition("running", LifecycleStatus.RUNNING.value)
            except Exception as exc:
                try:
                    _transition("failed-after-running", LifecycleStatus.FAILED.value)
                except Exception as fail_exc:  # noqa: BLE001 - expose both errors
                    raise CoordinationRunServiceError(
                        P_COORDINATION_START_FAILED,
                        f"{run_type} coordination run {run_id} running transition failed "
                        f"({exc}) and the run could NOT be marked failed ({fail_exc}); "
                        "it remains queued - do NOT claim failed",
                    ) from exc
                raise CoordinationRunServiceError(
                    P_COORDINATION_START_FAILED,
                    f"{run_type} coordination run {run_id} running transition failed: {exc}",
                ) from exc
        elif state == LifecycleStatus.QUEUED:
            fresh = True
            try:
                _transition("running", LifecycleStatus.RUNNING.value)
            except Exception as exc:
                try:
                    _transition("failed-after-running", LifecycleStatus.FAILED.value)
                except Exception as fail_exc:  # noqa: BLE001 - expose both errors
                    raise CoordinationRunServiceError(
                        P_COORDINATION_START_FAILED,
                        f"{run_type} coordination run {run_id} running transition failed "
                        f"({exc}) and the run could NOT be marked failed ({fail_exc}); "
                        "it remains queued - do NOT claim failed",
                    ) from exc
                raise CoordinationRunServiceError(
                    P_COORDINATION_START_FAILED,
                    f"{run_type} coordination run {run_id} running transition failed: {exc}",
                ) from exc
        elif state == LifecycleStatus.RUNNING:
            pass
        else:
            # Defensive enum exhaustiveness: if LifecycleStatus gains a new
            # member, it must never silently inherit RUNNING recovery.
            raise CoordinationRunServiceError(
                P_COORDINATION_RUN_INVALID,
                f"{run_type} coordination run {run_id} has unsupported lifecycle status "
                f"{state.value!r}; refusing to guess recovery",
            )
        # RUNNING recovery never repeats an illegal transition.
        return _running_recovery(
            run_dir, run_id, run_type, expected_artifacts, fresh=fresh, claim=claim
        )
    except BaseException:
        claim.release()
        raise


def write_coordination_manifest11(
    *,
    run_dir: Path,
    run_id: str,
    run_type: str,
    manifest_id: str,
    created_by: str,
    entries: list[ArtifactEntry],
    source_run_id: str | None = None,
) -> Path:
    """Build, validate and publish the artifact-manifest-1.1 COORDINATION
    manifest (immutable). Returns the manifest path."""
    manifest = ArtifactManifest11(
        artifact_schema_version="1.1",
        manifest_kind="coordination",
        run_type=run_type,
        artifact_manifest_id=uuid.UUID(manifest_id),
        run_id=uuid.UUID(run_id),
        source_run_id=uuid.UUID(source_run_id) if source_run_id else None,
        result_id=None,
        report_mapping_version="1.0",
        confidentiality_level=ConfidentialityLevel.INTERNAL,
        redaction_status=RedactionStatus.NOT_REQUIRED,
        generated_by=created_by,
        generated_at=datetime.now(UTC),
        artifacts=entries,
    )
    manifest_path = run_dir / "artifact_manifest.json"
    validate_manifest_before_publish(
        manifest,
        result_path=run_dir / "coordination",
        expected_run_id=run_id,
        expected_result_id=None,
        expected_manifest_id=manifest_id,
    )
    atomic_write_object(manifest_path, manifest.model_dump(mode="json"), immutable=True)
    read_artifact_manifest(
        manifest_path,
        result_path=run_dir / "coordination",
        expected_run_id=run_id,
        expected_result_id=None,
        expected_manifest_id=manifest_id,
    )
    return manifest_path


def complete_coordination_run(*, run_service: Any, run_id: str, request_id: str) -> None:
    run_service.transition(
        request_id=f"{request_id}-completed",
        run_id=run_id,
        target_lifecycle=LifecycleStatus.COMPLETED.value,
    )


def fail_coordination_run(
    *,
    run_service: Any,
    run_id: str,
    request_id: str,
    error_code: str,
    message: str,
) -> None:
    """Best-effort failed transition; preserves the already-completed
    child runs and never writes a completed manifest. If the FAILED
    transition itself fails, the raised error exposes that fact explicitly
    (the run is NOT claimed to be failed)."""
    try:
        run_service.transition(
            request_id=f"{request_id}-failed",
            run_id=run_id,
            target_lifecycle=LifecycleStatus.FAILED.value,
        )
    except Exception as transition_exc:
        raise CoordinationRunServiceError(
            P_COORDINATION_RUN_FAILED,
            f"coordination run {run_id} could NOT be marked failed "
            f"(original error: {message}; failed-transition error: {transition_exc})",
        ) from transition_exc
    raise CoordinationRunServiceError(error_code, message)


def analysis_summary_entry(run_dir: Path, name: str, producer_service: str) -> ArtifactEntry:
    """Build an analysis_summary artifact entry for a file in ``run_dir``
    (verified to exist and hashed)."""
    target = run_dir / safe_filename_component(name)
    if not target.exists():
        raise CoordinationRunServiceError(
            P_COORDINATION_MANIFEST_INVALID,
            f"coordination artifact missing: {name}",
        )
    return ArtifactEntry(
        artifact_type=ArtifactType.ANALYSIS_SUMMARY,
        relative_path=name,
        sha256=_file_sha256(target),
        media_type="application/json",
        producer_service=producer_service,
        schema_or_template_version="1.0",
    )


__all__ = [
    "P_COORDINATION_MANIFEST_INVALID",
    "P_COORDINATION_RECOVERY_FAILED",
    "P_COORDINATION_RUN_FAILED",
    "P_COORDINATION_RUN_INVALID",
    "P_COORDINATION_RUN_IN_PROGRESS",
    "P_COORDINATION_RUN_STOPPING",
    "P_COORDINATION_START_FAILED",
    "CoordinationAction",
    "CoordinationExecutionClaim",
    "CoordinationRecovery",
    "CoordinationRunServiceError",
    "analysis_summary_entry",
    "complete_coordination_run",
    "create_coordination_run",
    "fail_coordination_run",
    "write_coordination_manifest11",
]
