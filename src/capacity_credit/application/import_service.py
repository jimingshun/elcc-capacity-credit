"""Import approval, freeze and supersede service (T05, review-hardened).

File-backed, event-sourced persistence reusing the T03 primitives:

- ``batch.json`` is generated strictly by the ``ImportBatch`` contract
  and round-trips through it; file inventory and grade evidence live in
  a versioned ``staging_manifest.json`` sidecar linked by ids/hashes;
- state mapping is strict: ``pending -> draft``, ``failed -> blocked``,
  ``passed -> validated``; automatic validation never approves/freezes;
- single-file hashes are ``sha256(file_bytes)``; ``data_version_hash``
  is a canonical hash over the sorted business inventory
  (relative_path, size_bytes, file sha256) without volatile metadata;
- freezing verifies the manifest schema, identity, grade evidence,
  per-file safe relative paths/sizes/hashes, exact file-set equality
  and the recomputed aggregate hash; the manifest is published last;
- retries reuse identical frozen content and block differing content
  (not via "file exists" exceptions);
- approval/freeze/supersede hold a batch-level cross-process lock and
  are idempotent by ``request_id`` via pre-registration plus event
  replay; parameter conflicts are rejected;
- supersede validates the target exists, forbids self/circular/
  repeated supersede, and keeps full lineage.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from capacity_credit.application.atomic_io import (
    ObjectExistsError,
    atomic_write_bytes,
    hash_payload,
    safe_filename_component,
)
from capacity_credit.application.audit import AuditLog
from capacity_credit.application.locks import RunLock
from capacity_credit.contracts.enums import (
    DataVersionStatus,
    ImportReviewStatus,
    ValidationStatus,
)
from capacity_credit.contracts.grades import GradeEvidence
from capacity_credit.contracts.imports import ImportBatch

EVT_IMPORT_CREATED = "import_created"
EVT_VALIDATION = "validation"
EVT_APPROVAL = "approval"
EVT_FREEZE = "freeze"
EVT_SUPERSEDE = "supersede"

STAGING_MANIFEST_VERSION = "1.0"
FROZEN_MANIFEST_VERSION = "1.0"

_VALIDATION_TO_VERSION = {
    "pending": "draft",
    "failed": "blocked",
    "passed": "validated",
}


class ServiceError(Exception):
    """Stable business error with a machine code (not a message match)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sha256_bytes(data: bytes) -> str:
    """Single-file SHA-256 over the raw bytes."""
    return hashlib.sha256(data).hexdigest()


def safe_relative_path(path: str) -> str:
    """Validate a multi-level relative path (``/``-separated components).

    Accepts ``processed/load_hourly.parquet``; rejects absolute paths,
    backslash mixes, empty components, ``.``/``..`` and drive letters.
    """
    if not isinstance(path, str) or not path:
        raise ServiceError("PATH_TRAVERSAL", f"unsafe relative path {path!r}")
    if path.startswith(("/", "\\")) or ":" in path.split("/")[0]:
        raise ServiceError("PATH_TRAVERSAL", f"unsafe relative path {path!r}")
    if "\\" in path:
        raise ServiceError("PATH_TRAVERSAL", f"unsafe relative path {path!r}")
    components = path.split("/")
    if any(comp in ("", ".", "..") for comp in components):
        raise ServiceError("PATH_TRAVERSAL", f"unsafe relative path {path!r}")
    return path


@dataclass
class ImportState:
    import_batch_id: str
    dataset_id: str
    data_version_id: str
    data_version_hash: str
    validation_status: str
    data_version_status: str
    import_review_status: str
    approved_data_version_id: str | None
    superseded_by_import_batch_id: str | None
    grades: dict[str, str] | None


@dataclass
class ApprovalResult:
    request_id: str
    import_batch_id: str
    import_review_status: str
    approved_data_version_id: str


@dataclass
class FreezeResult:
    import_batch_id: str
    data_version_id: str
    data_version_hash: str


class ImportService:
    """Import batch lifecycle service (file-backed, event-sourced)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths -----------------------------------------------------------

    def _batch_dir(self, import_batch_id: str) -> Path:
        return self.root / "imports" / safe_filename_component(import_batch_id)

    def _batch_path(self, import_batch_id: str) -> Path:
        return self._batch_dir(import_batch_id) / "batch.json"

    def _staging_manifest_path(self, import_batch_id: str) -> Path:
        return self._batch_dir(import_batch_id) / "staging_manifest.json"

    def _audit(self, import_batch_id: str) -> AuditLog:
        return AuditLog(self._batch_dir(import_batch_id) / "audit.jsonl")

    def _requests_dir(self, import_batch_id: str) -> Path:
        return self._batch_dir(import_batch_id) / "requests"

    def _batch_lock(self, import_batch_id: str) -> RunLock:
        locks_dir = self.root / "locks" / "imports"
        return RunLock(locks_dir, import_batch_id)

    def _versions_dir(self, dataset_id: str) -> Path:
        return self.root / "versions" / safe_filename_component(dataset_id)

    def _frozen_dir(self, dataset_id: str, data_version_id: str) -> Path:
        return self._versions_dir(dataset_id) / safe_filename_component(data_version_id)

    def _frozen_manifest_path(self, dataset_id: str, data_version_id: str) -> Path:
        return self._frozen_dir(dataset_id, data_version_id) / "manifest.json"

    # -- generic helpers -------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, data: bytes, *, immutable: bool = False) -> None:
        atomic_write_bytes(path, data, immutable=immutable)

    def _append_event(self, import_batch_id: str, event: dict[str, Any]) -> None:
        self._audit(import_batch_id).append(event)

    @staticmethod
    def _request_file(scope_dir: Path, request_id: str) -> Path:
        return scope_dir / f"{safe_filename_component(request_id)}.json"

    @staticmethod
    def _params_hash(action: str, params: dict[str, Any]) -> str:
        return hash_payload({"action": action, **params})

    def _read_request(
        self, scope_dir: Path, request_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Idempotency pre-registration read; conflicts raise."""
        request_file = self._request_file(scope_dir, request_id)
        if not request_file.exists():
            return None
        recorded = cast(
            dict[str, Any],
            json.loads(request_file.read_text(encoding="utf-8")),
        )
        if recorded.get("params_hash") != self._params_hash(action, params):
            raise ServiceError(
                "REQUEST_ID_CONFLICT",
                f"request_id {request_id} reused with different parameters",
            )
        return recorded

    def _pre_register(
        self,
        scope_dir: Path,
        request_id: str,
        action: str,
        params: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically pre-register a request BEFORE any side effect.

        Returns the pre-registration record (either freshly created or
        the existing one with matching parameters). Callers must hold
        the relevant lock.
        """
        scope_dir.mkdir(parents=True, exist_ok=True)
        request_file = self._request_file(scope_dir, request_id)
        if request_file.exists():
            existing = cast(
                dict[str, Any],
                json.loads(request_file.read_text(encoding="utf-8")),
            )
            if existing.get("params_hash") != self._params_hash(action, params):
                raise ServiceError(
                    "REQUEST_ID_CONFLICT",
                    f"request_id {request_id} reused with different parameters",
                )
            return existing
        record = {
            "request_id": request_id,
            "action": action,
            "params_hash": self._params_hash(action, params),
            "status": "pending",
            **(extra or {}),
        }
        self._atomic_write(
            request_file,
            (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
            immutable=True,
        )
        return record

    def _complete_request(
        self, scope_dir: Path, request_id: str, result: dict[str, Any]
    ) -> None:
        """Mark a request committed with its result.

        Idempotent short-circuit: when the record is already committed
        with the identical result, no write is performed - this avoids
        repeated ``os.replace`` on the same request file by concurrent
        recovery callers and lets a failed replace recover naturally on
        retry (never blind retry loops).
        """
        request_file = self._request_file(scope_dir, request_id)
        recorded = cast(
            dict[str, Any],
            json.loads(request_file.read_text(encoding="utf-8")),
        )
        if recorded.get("status") == "committed" and all(
            recorded.get(key) == value for key, value in result.items()):
            return
        recorded.update(result)
        recorded["status"] = "committed"
        self._atomic_write(
            request_file,
            (json.dumps(recorded, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
            immutable=False,
        )

    def _event_applied(
        self, import_batch_id: str, action: str, request_id: str
    ) -> bool:
        """Replay check: has this request's side effect already landed?"""
        return any(
            e.get("event_type") == action and e.get("request_id") == request_id
            for e in self._audit(import_batch_id).read()
        )

    @staticmethod
    def _acquire_lock(lock: RunLock, timeout: float = 10.0) -> None:
        """Blocking acquire with retries; concurrent callers wait."""
        import time

        deadline = time.monotonic() + timeout
        while True:
            if lock.acquire():
                return
            if time.monotonic() >= deadline:
                raise ServiceError("GATE_LOCKED", "lock acquisition timed out")
            time.sleep(0.02)

    # -- registration ----------------------------------------------------

    def register_import(
        self,
        *,
        dataset_id: str,
        data_version_id: str,
        files: dict[str, Path],
        validation_status: str,
        created_by: str,
        grades: dict[str, str] | None = None,
    ) -> str:
        """Register an import batch with ZERO side effects on failure.

        All validation (validation_status, GradeEvidence required/enum/
        exact-shape/relation) completes BEFORE the import_batch_id is
        generated, before any directory is created and before any input
        file is copied: a rejected registration never leaves an orphan
        batch directory, staging copies, batch.json, staging_manifest.json
        or audit events behind. The validated GradeEvidence object is
        reused, never re-parsed from the raw dict.
        """
        try:
            ValidationStatus(validation_status)
        except ValueError as exc:
            raise ServiceError(
                "GATE_INVALID_STATE", f"invalid validation_status {validation_status!r}"
            ) from exc
        if grades is None:
            raise ServiceError(
                "GATE_GRADE_INCONSISTENT",
                "grade evidence (declared/computed_max/effective) is "
                "required at registration",
            )
        try:
            grade_evidence = GradeEvidence.model_validate(grades)
        except Exception as exc:
            raise ServiceError(
                "GATE_GRADE_INCONSISTENT",
                f"invalid grade evidence: {exc}",
            ) from exc
        import_batch_id = str(uuid.uuid4())
        batch_dir = self._batch_dir(import_batch_id)
        staging_dir = batch_dir / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        file_entries: list[dict[str, Any]] = []
        for rel_path, local in files.items():
            safe = safe_relative_path(rel_path)
            data = local.read_bytes()
            self._atomic_write(staging_dir / safe, data)
            file_entries.append(
                {
                    "relative_path": safe,
                    "size_bytes": len(data),
                    "sha256": sha256_bytes(data),
                }
            )
        file_entries.sort(key=lambda f: f["relative_path"])
        data_version_hash = hash_payload(
            {
                "dataset_id": dataset_id,
                "data_version_id": data_version_id,
                "files": file_entries,
            }
        )

        batch = ImportBatch(
            import_schema_version="1.0",
            import_batch_id=uuid.UUID(import_batch_id),
            dataset_id=dataset_id,
            data_version_id=data_version_id,
            data_version_hash=data_version_hash,
            data_version_status=DataVersionStatus(
                _VALIDATION_TO_VERSION[validation_status]
            ),
            validation_status=ValidationStatus(validation_status),
            import_review_status=ImportReviewStatus.PENDING_REVIEW,
            reviewed_by=None,
            reviewed_at=None,
            review_comment=None,
            approved_data_version_id=None,
            superseded_by_import_batch_id=None,
            created_by=created_by,
            created_at=datetime.now(UTC),
            grades=grade_evidence,
        )
        self._atomic_write(
            self._batch_path(import_batch_id),
            (batch.model_dump_json() + "\n").encode("utf-8"),
            immutable=True,
        )
        staging_manifest: dict[str, Any] = {
            "staging_manifest_version": STAGING_MANIFEST_VERSION,
            "import_batch_id": import_batch_id,
            "dataset_id": dataset_id,
            "data_version_id": data_version_id,
            "data_version_hash": data_version_hash,
            "grades": grades,
            "files": file_entries,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._atomic_write(
            self._staging_manifest_path(import_batch_id),
            (json.dumps(staging_manifest, sort_keys=True, ensure_ascii=False) + "\n")
            .encode("utf-8"),
            immutable=True,
        )
        self._append_event(import_batch_id, {
            "event_type": EVT_IMPORT_CREATED,
            "import_batch_id": import_batch_id,
            "data_version_id": data_version_id,
            "data_version_hash": data_version_hash,
            "occurred_at": datetime.now(UTC).isoformat(),
        })
        self._append_event(import_batch_id, {
            "event_type": EVT_VALIDATION,
            "import_batch_id": import_batch_id,
            "validation_status": validation_status,
            "data_version_status": _VALIDATION_TO_VERSION[validation_status],
            "occurred_at": datetime.now(UTC).isoformat(),
        })
        return import_batch_id

    @staticmethod
    def _validate_grades(grades: dict[str, str] | None) -> None:
        """Exact grade-evidence shape via the formal GradeEvidence
        contract: the three authoritative DataGrade fields and nothing
        else, with effective <= declared <= computed_max."""
        from capacity_credit.contracts.grades import GradeEvidence

        try:
            GradeEvidence.model_validate(grades)
        except Exception as exc:
            raise ServiceError(
                "GATE_GRADE_INCONSISTENT",
                f"invalid grade evidence: {exc}",
            ) from exc

    def staging_manifest(self, import_batch_id: str) -> dict[str, Any]:
        path = self._staging_manifest_path(import_batch_id)
        if not path.is_file():
            raise ServiceError("GATE_INVALID_STATE", "staging manifest missing")
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    # -- state projection -------------------------------------------------

    def get_batch(self, import_batch_id: str) -> dict[str, Any]:
        """Read the batch record through the strict ImportBatch contract
        (never a raw ``json.loads`` trust); contract violations are
        stable ``GATE_INVALID_STATE`` errors."""
        from capacity_credit.contracts.imports import ImportBatch

        path = self._batch_path(import_batch_id)
        if not path.is_file():
            raise ServiceError("GATE_INVALID_STATE", f"unknown import batch {import_batch_id}")
        try:
            batch = ImportBatch.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            raise ServiceError(
                "GATE_INVALID_STATE",
                f"import batch contract invalid: {exc}",
            ) from exc
        return cast(dict[str, Any], json.loads(batch.model_dump_json()))

    def current_state(self, import_batch_id: str) -> ImportState:
        batch = self.get_batch(import_batch_id)
        state = ImportState(
            import_batch_id=import_batch_id,
            dataset_id=batch["dataset_id"],
            data_version_id=batch["data_version_id"],
            data_version_hash=batch["data_version_hash"],
            validation_status=batch["validation_status"],
            data_version_status=batch["data_version_status"],
            import_review_status=batch["import_review_status"],
            approved_data_version_id=batch["approved_data_version_id"],
            superseded_by_import_batch_id=batch["superseded_by_import_batch_id"],
            # grade evidence comes from the immutable batch anchor, never
            # from the mutable staging manifest
            grades=batch.get("grades"),
        )
        for event in self._audit(import_batch_id).read():
            event_type = event.get("event_type")
            if event_type == EVT_APPROVAL:
                state.import_review_status = "approved"
                state.approved_data_version_id = event.get("approved_data_version_id")
            elif event_type == EVT_FREEZE:
                state.data_version_status = "frozen"
            elif event_type == EVT_SUPERSEDE:
                state.import_review_status = "superseded"
                state.data_version_status = "superseded"
                state.superseded_by_import_batch_id = event.get(
                    "superseded_by_import_batch_id"
                )
        return state

    # -- approval ---------------------------------------------------------

    def approve(
        self,
        *,
        request_id: str,
        import_batch_id: str,
        reviewed_by: str,
        review_comment: str,
    ) -> ApprovalResult:
        params = {
            "import_batch_id": import_batch_id,
            "reviewed_by": reviewed_by,
            "review_comment": review_comment,
        }
        lock = self._batch_lock(import_batch_id)
        self._acquire_lock(lock)
        try:
            scope = self._requests_dir(import_batch_id)
            pre = self._read_request(scope, request_id, EVT_APPROVAL, params)
            if pre is not None and pre.get("status") == "committed":
                return ApprovalResult(
                    request_id=request_id,
                    import_batch_id=import_batch_id,
                    import_review_status="approved",
                    approved_data_version_id=pre["approved_data_version_id"],
                )
            state = self.current_state(import_batch_id)
            if state.superseded_by_import_batch_id is not None:
                raise ServiceError("GATE_SUPERSEDED", "import batch was superseded")
            if state.validation_status != "passed":
                raise ServiceError(
                    "GATE_VALIDATION_NOT_PASSED",
                    "approval requires validation_status=passed",
                )
            if state.import_review_status != "pending_review":
                if self._event_applied(import_batch_id, EVT_APPROVAL, request_id):
                    self._complete_request(scope, request_id, {
                        "approved_data_version_id": state.data_version_id,
                    })
                    return ApprovalResult(
                        request_id=request_id,
                        import_batch_id=import_batch_id,
                        import_review_status="approved",
                        approved_data_version_id=state.data_version_id,
                    )
                raise ServiceError(
                    "GATE_INVALID_STATE",
                    f"approval not allowed from {state.import_review_status}",
                )
            if not reviewed_by.strip() or not review_comment.strip():
                raise ServiceError(
                    "GATE_APPROVAL_EVIDENCE_REQUIRED",
                    "approval requires reviewed_by and review_comment",
                )
            self._pre_register(scope, request_id, EVT_APPROVAL, params)
            self._append_event(import_batch_id, {
                "event_type": EVT_APPROVAL,
                "import_batch_id": import_batch_id,
                "reviewed_by": reviewed_by,
                "reviewed_at": datetime.now(UTC).isoformat(),
                "occurred_at": datetime.now(UTC).isoformat(),
                "review_comment": review_comment,
                "approved_data_version_id": state.data_version_id,
                "request_id": request_id,
            })
            self._complete_request(scope, request_id, {
                "approved_data_version_id": state.data_version_id,
            })
            return ApprovalResult(
                request_id=request_id,
                import_batch_id=import_batch_id,
                import_review_status="approved",
                approved_data_version_id=state.data_version_id,
            )
        finally:
            lock.release()

    # -- freeze -----------------------------------------------------------

    def _recompute_staging_hash(self, import_batch_id: str) -> str:
        manifest = self.staging_manifest(import_batch_id)
        batch = self.get_batch(import_batch_id)
        entries: list[dict[str, Any]] = []
        for file_entry in manifest["files"]:
            path = self._batch_dir(import_batch_id) / "staging" / file_entry[
                "relative_path"
            ]
            data = path.read_bytes()
            entries.append(
                {
                    "relative_path": file_entry["relative_path"],
                    "size_bytes": len(data),
                    "sha256": sha256_bytes(data),
                }
            )
        entries.sort(key=lambda f: f["relative_path"])
        return hash_payload(
            {
                "dataset_id": batch["dataset_id"],
                "data_version_id": batch["data_version_id"],
                "files": entries,
            }
        )

    def freeze(self, *, request_id: str, import_batch_id: str) -> FreezeResult:
        params = {"import_batch_id": import_batch_id}
        state = self.current_state(import_batch_id)
        version_lock = self._version_lock(state.dataset_id, state.data_version_id)
        self._acquire_lock(version_lock)
        batch_lock = self._batch_lock(import_batch_id)
        try:
            self._acquire_lock(batch_lock)
            scope = self._requests_dir(import_batch_id)
            pre = self._read_request(scope, request_id, EVT_FREEZE, params)
            if pre is not None and pre.get("status") == "committed":
                return FreezeResult(
                    import_batch_id=import_batch_id,
                    data_version_id=pre["data_version_id"],
                    data_version_hash=pre["data_version_hash"],
                )
            state = self.current_state(import_batch_id)
            if state.superseded_by_import_batch_id is not None:
                raise ServiceError("GATE_SUPERSEDED", "import batch was superseded")
            if state.validation_status != "passed":
                raise ServiceError(
                    "GATE_VALIDATION_NOT_PASSED",
                    "freeze requires validation_status=passed",
                )
            if state.import_review_status != "approved":
                raise ServiceError(
                    "GATE_NOT_APPROVED",
                    "freeze requires import_review_status=approved",
                )
            if state.approved_data_version_id != state.data_version_id:
                raise ServiceError(
                    "GATE_APPROVED_VERSION_MISMATCH",
                    "approved_data_version_id must equal data_version_id",
                )
            if state.data_version_status not in ("validated", "frozen"):
                raise ServiceError(
                    "GATE_INVALID_STATE",
                    f"freeze not allowed from {state.data_version_status}",
                )
            if state.grades is None:
                raise ServiceError(
                    "GATE_GRADE_UNAVAILABLE",
                    "data grades unavailable; cannot freeze without grade evidence",
                )
            self._pre_register(scope, request_id, EVT_FREEZE, params)

            manifest_path = self._frozen_manifest_path(
                state.dataset_id, state.data_version_id
            )
            if manifest_path.is_file():
                # recovery: manifest already published; never rewrite it
                self.verify_frozen_integrity(import_batch_id)
                self._ensure_event(import_batch_id, EVT_FREEZE, request_id, {
                    "event_type": EVT_FREEZE,
                    "import_batch_id": import_batch_id,
                    "data_version_id": state.data_version_id,
                    "data_version_hash": state.data_version_hash,
                    "request_id": request_id,
                    "occurred_at": datetime.now(UTC).isoformat(),
                })
                self._complete_request(scope, request_id, {
                    "data_version_id": state.data_version_id,
                    "data_version_hash": state.data_version_hash,
                })
                return FreezeResult(
                    import_batch_id=import_batch_id,
                    data_version_id=state.data_version_id,
                    data_version_hash=state.data_version_hash,
                )

            current_hash = self._recompute_staging_hash(import_batch_id)
            if current_hash != state.data_version_hash:
                raise ServiceError(
                    "GATE_FROZEN_CONTENT_CHANGED",
                    "staging content changed after validation",
                )
            if self.staging_manifest(import_batch_id).get("grades") != state.grades:
                raise ServiceError(
                    "GATE_FROZEN_GRADE_CHANGED",
                    "staging grade evidence differs from the batch anchor",
                )
            file_entries: list[dict[str, Any]] = []
            for file_entry in self.staging_manifest(import_batch_id)["files"]:
                src = self._batch_dir(import_batch_id) / "staging" / file_entry[
                    "relative_path"
                ]
                data = src.read_bytes()
                self._write_frozen_file(
                    state.dataset_id, state.data_version_id,
                    file_entry["relative_path"], data,
                )
                file_entries.append({
                    "relative_path": file_entry["relative_path"],
                    "size_bytes": len(data),
                    "sha256": sha256_bytes(data),
                })
            file_entries.sort(key=lambda f: f["relative_path"])
            frozen_manifest: dict[str, Any] = {
                "frozen_manifest_version": FROZEN_MANIFEST_VERSION,
                "dataset_id": state.dataset_id,
                "data_version_id": state.data_version_id,
                "data_version_hash": state.data_version_hash,
                "declared_data_grade": state.grades["declared_data_grade"],
                "computed_max_data_grade": state.grades["computed_max_data_grade"],
                "effective_data_grade": state.grades["effective_data_grade"],
                "files": file_entries,
                "created_at": datetime.now(UTC).isoformat(),
            }
            self._atomic_write(
                manifest_path,
                (json.dumps(frozen_manifest, sort_keys=True, ensure_ascii=False) + "\n")
                .encode("utf-8"),
                immutable=True,
            )
            self._ensure_event(import_batch_id, EVT_FREEZE, request_id, {
                "event_type": EVT_FREEZE,
                "import_batch_id": import_batch_id,
                "data_version_id": state.data_version_id,
                "data_version_hash": state.data_version_hash,
                "request_id": request_id,
                "occurred_at": datetime.now(UTC).isoformat(),
            })
            self._complete_request(scope, request_id, {
                "data_version_id": state.data_version_id,
                "data_version_hash": state.data_version_hash,
            })
            return FreezeResult(
                import_batch_id=import_batch_id,
                data_version_id=state.data_version_id,
                data_version_hash=state.data_version_hash,
            )
        finally:
            batch_lock.release()
            version_lock.release()

    def _write_frozen_file(
        self,
        dataset_id: str,
        data_version_id: str,
        relative_path: str,
        data: bytes,
    ) -> None:
        """Publish one frozen file idempotently.

        - target missing: immutable atomic create;
        - target exists with identical bytes: idempotent success (reuse);
        - target exists with different bytes: block
          (GATE_FROZEN_CONTENT_CHANGED);
        - concurrent create race: catch ObjectExistsError, re-read and
          compare.
        """
        safe = safe_relative_path(relative_path)
        target = self._frozen_dir(dataset_id, data_version_id) / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._atomic_write(target, data, immutable=True)
            return
        except ObjectExistsError:
            pass
        existing = target.read_bytes()
        if sha256_bytes(existing) != sha256_bytes(data):
            raise ServiceError(
                "GATE_FROZEN_CONTENT_CHANGED",
                f"frozen file differs for {safe}; overwrite forbidden",
            )

    def _version_lock(self, dataset_id: str, data_version_id: str) -> RunLock:
        lock_key = safe_filename_component(
            f"{dataset_id}__{data_version_id}"
        )
        return RunLock(self.root / "locks" / "versions", lock_key)

    def locked_version_scope(
        self, dataset_id: str, data_version_id: str
    ) -> AbstractContextManager[None]:
        """Public cross-service lock scope for ONE data version (the SAME
        version lock approve/freeze use): blocking acquire on enter,
        release on exit. Lets the upload submission make its
        reuse-scan + registration sequence atomic against concurrent
        identical submissions and against approve/freeze of the same
        version - without inventing a second lock family."""
        version_lock = self._version_lock(dataset_id, data_version_id)

        @contextmanager
        def _scope() -> Iterator[None]:
            self._acquire_lock(version_lock)
            try:
                yield
            finally:
                version_lock.release()

        return _scope()

    def _ensure_event(
        self,
        import_batch_id: str,
        event_type: str,
        request_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Append an event only if no event of the same type/request_id
        exists; returns True when appended."""
        if any(
            e.get("event_type") == event_type and e.get("request_id") == request_id
            for e in self._audit(import_batch_id).read()
        ):
            return False
        self._append_event(import_batch_id, event)
        return True

    def registered_files(self, import_batch_id: str) -> dict[str, Path]:
        """Public: files registered for this batch (staging copy).

        Used by ``validate``: validation re-reads the registered staging
        content, never the mutable original source directory. The full
        registered/staging integrity gate runs before any path is
        returned (manifest identity, exact file set, per-file size and
        sha256, aggregate data_version_hash).
        """
        self.verify_registered_integrity(import_batch_id)
        manifest = self.staging_manifest(import_batch_id)
        staging = self._batch_dir(import_batch_id) / "staging"
        files: dict[str, Path] = {}
        for entry in manifest.get("files", []):
            rel = safe_relative_path(entry["relative_path"])
            path = staging / rel
            if not path.is_file():
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"registered file missing: {rel}"
                )
            files[rel] = path
        return files

    def verify_registered_integrity(self, import_batch_id: str) -> dict[str, Any]:
        """Full registered/staging integrity verification (manifest
        identity, exact file-set equality, per-file size and sha256,
        aggregate data_version_hash recomputed with the exact same
        canonical algorithm as ``register_import``)."""
        manifest = self.staging_manifest(import_batch_id)
        batch = self.get_batch(import_batch_id)
        required = {
            "staging_manifest_version", "import_batch_id", "dataset_id",
            "data_version_id", "data_version_hash", "files",
        }
        if not required <= set(manifest):
            raise ServiceError(
                "GATE_INTEGRITY_FAILED", "staging manifest schema invalid"
            )
        if manifest["import_batch_id"] != import_batch_id:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED", "staging manifest import_batch_id mismatch"
            )
        if manifest["dataset_id"] != batch["dataset_id"]:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED", "staging manifest dataset_id mismatch"
            )
        if manifest["data_version_id"] != batch["data_version_id"]:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED", "staging manifest data_version_id mismatch"
            )
        if manifest["data_version_hash"] != batch["data_version_hash"]:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                "staging manifest data_version_hash mismatch",
            )
        # grade evidence must exist in the staging manifest, satisfy the
        # exact GradeEvidence shape/relation, and match the batch anchor
        # field by field
        staging_grades = manifest.get("grades")
        try:
            self._validate_grades(staging_grades)
        except ServiceError as exc:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                f"staging grade evidence invalid: {exc}",
            ) from exc
        if staging_grades != batch.get("grades"):
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                "staging grade evidence differs from the immutable batch anchor",
            )
        staging = self._batch_dir(import_batch_id) / "staging"
        recomputed: list[dict[str, Any]] = []
        for entry in manifest.get("files", []):
            try:
                rel = safe_relative_path(entry["relative_path"])
            except ServiceError as exc:
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"staging path unsafe: {exc}"
                ) from exc
            target = staging / rel
            if not target.is_file():
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"registered file missing: {rel}"
                )
            data = target.read_bytes()
            if len(data) != entry.get("size_bytes"):
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"registered file size mismatch: {rel}"
                )
            if sha256_bytes(data) != entry.get("sha256"):
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"registered file hash mismatch: {rel}"
                )
            recomputed.append({
                "relative_path": rel,
                "size_bytes": len(data),
                "sha256": sha256_bytes(data),
            })
        recomputed.sort(key=lambda f: f["relative_path"])
        disk_files = {
            p.relative_to(staging).as_posix()
            for p in staging.rglob("*")
            if p.is_file()
        }
        manifest_files = {e["relative_path"] for e in manifest.get("files", [])}
        if disk_files != manifest_files:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                "registered file set differs between manifest and disk",
            )
        aggregate = hash_payload({
            "dataset_id": batch["dataset_id"],
            "data_version_id": batch["data_version_id"],
            "files": recomputed,
        })
        if aggregate != batch["data_version_hash"]:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                "registered aggregate hash mismatch with data_version_hash",
            )
        return manifest

    def frozen_files(self, import_batch_id: str) -> dict[str, Path]:
        """Public: registered frozen input files (safe relative paths).

        Returns a mapping ``relative_path -> Path`` for the frozen
        version of this batch. Frozen integrity is verified inside this
        method, so callers can never read a tampered frozen set. The
        original source directory is never authoritative after freeze.
        """
        self.verify_frozen_integrity(import_batch_id)
        state = self.current_state(import_batch_id)
        manifest_path = self._frozen_manifest_path(
            state.dataset_id, state.data_version_id
        )
        if not manifest_path.is_file():
            raise ServiceError(
                "GATE_INTEGRITY_FAILED", "frozen manifest missing"
            )
        manifest = cast(
            dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        files: dict[str, Path] = {}
        for entry in manifest["files"]:
            rel = safe_relative_path(entry["relative_path"])
            path = self._frozen_dir(state.dataset_id, state.data_version_id) / rel
            if not path.is_file():
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED",
                    f"frozen file missing: {rel}",
                )
            files[rel] = path
        return files

    def effective_data_grade(self, import_batch_id: str) -> str:
        """Public: effective data grade saved with the batch."""
        state = self.current_state(import_batch_id)
        grades = state.grades or {}
        effective = grades.get("effective_data_grade")
        if not effective:
            raise ServiceError(
                "GATE_GRADE_UNAVAILABLE", "effective data grade unavailable"
            )
        return effective

    def verify_frozen_integrity(self, import_batch_id: str) -> dict[str, Any]:
        """Full frozen integrity verification (schema, identity, grades,
        per-file hashes, exact file-set equality, aggregate hash)."""
        state = self.current_state(import_batch_id)
        manifest_path = self._frozen_manifest_path(state.dataset_id, state.data_version_id)
        if not manifest_path.is_file():
            raise ServiceError("GATE_INTEGRITY_FAILED", "frozen manifest missing")
        manifest = cast(
            dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        required = {
            "frozen_manifest_version", "dataset_id", "data_version_id",
            "data_version_hash", "declared_data_grade",
            "computed_max_data_grade", "effective_data_grade", "files",
        }
        if not required <= set(manifest):
            raise ServiceError("GATE_INTEGRITY_FAILED", "frozen manifest schema invalid")
        if manifest["dataset_id"] != state.dataset_id:
            raise ServiceError("GATE_INTEGRITY_FAILED", "manifest dataset_id mismatch")
        if manifest["data_version_id"] != state.data_version_id:
            raise ServiceError("GATE_INTEGRITY_FAILED", "manifest data_version_id mismatch")
        if manifest["data_version_hash"] != state.data_version_hash:
            raise ServiceError("GATE_INTEGRITY_FAILED", "manifest data_version_hash mismatch")
        try:
            self._validate_grades({
                "declared_data_grade": manifest["declared_data_grade"],
                "computed_max_data_grade": manifest["computed_max_data_grade"],
                "effective_data_grade": manifest["effective_data_grade"],
            })
        except ServiceError as exc:
            raise ServiceError("GATE_INTEGRITY_FAILED", f"grade evidence invalid: {exc}")
        # grade evidence must match the immutable batch anchor exactly
        if state.grades != {
            "declared_data_grade": manifest["declared_data_grade"],
            "computed_max_data_grade": manifest["computed_max_data_grade"],
            "effective_data_grade": manifest["effective_data_grade"],
        }:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                "frozen grade evidence differs from the immutable batch anchor",
            )
        # the mutable staging manifest must agree with the frozen anchor
        staging = self.staging_manifest(import_batch_id)
        if staging.get("grades") != state.grades:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                "staging grade evidence differs from the immutable batch anchor",
            )

        frozen_dir = self._frozen_dir(state.dataset_id, state.data_version_id)
        recomputed: list[dict[str, Any]] = []
        for entry in manifest["files"]:
            try:
                rel = safe_relative_path(entry["relative_path"])
            except ServiceError as exc:
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"manifest path unsafe: {exc}"
                ) from exc
            target = frozen_dir / rel
            if not target.is_file():
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"frozen file missing: {rel}"
                )
            data = target.read_bytes()
            if len(data) != entry["size_bytes"]:
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"frozen file size mismatch: {rel}"
                )
            if sha256_bytes(data) != entry["sha256"]:
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED", f"frozen file hash mismatch: {rel}"
                )
            recomputed.append({
                "relative_path": rel,
                "size_bytes": len(data),
                "sha256": sha256_bytes(data),
            })
        recomputed.sort(key=lambda f: f["relative_path"])
        # exact file-set equality: manifest vs disk
        disk_files = {
            p.relative_to(frozen_dir).as_posix()
            for p in frozen_dir.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        manifest_files = {e["relative_path"] for e in manifest["files"]}
        if disk_files != manifest_files:
            raise ServiceError(
                "GATE_INTEGRITY_FAILED",
                "frozen file set differs between manifest and disk",
            )
        # aggregate hash must match manifest and batch
        aggregate = hash_payload({
            "dataset_id": state.dataset_id,
            "data_version_id": state.data_version_id,
            "files": recomputed,
        })
        if aggregate != manifest["data_version_hash"]:
            raise ServiceError("GATE_INTEGRITY_FAILED", "aggregate hash mismatch")
        return manifest

    # -- supersede --------------------------------------------------------

    def supersede(
        self,
        *,
        request_id: str,
        import_batch_id: str,
        superseded_by_import_batch_id: str,
    ) -> None:
        params = {
            "import_batch_id": import_batch_id,
            "superseded_by_import_batch_id": superseded_by_import_batch_id,
        }
        lock = self._batch_lock(import_batch_id)
        self._acquire_lock(lock)
        try:
            scope = self._requests_dir(import_batch_id)
            pre = self._read_request(scope, request_id, EVT_SUPERSEDE, params)
            if pre is not None and pre.get("status") == "committed":
                return
            if import_batch_id == superseded_by_import_batch_id:
                raise ServiceError("SUPERSEDE_SELF", "a batch cannot supersede itself")
            if not self._batch_path(superseded_by_import_batch_id).is_file():
                raise ServiceError(
                    "SUPERSEDE_TARGET_MISSING",
                    f"replacement batch {superseded_by_import_batch_id} does not exist",
                )
            state = self.current_state(import_batch_id)
            if state.superseded_by_import_batch_id is not None:
                raise ServiceError(
                    "SUPERSEDE_ALREADY_SUPERSEDED",
                    "superseded batch cannot be superseded again",
                )
            if self._supersede_creates_cycle(import_batch_id, superseded_by_import_batch_id):
                raise ServiceError("SUPERSEDE_CYCLE", "supersede chain would form a cycle")
            self._pre_register(scope, request_id, EVT_SUPERSEDE, params)
            self._append_event(import_batch_id, {
                "event_type": EVT_SUPERSEDE,
                "import_batch_id": import_batch_id,
                "superseded_by_import_batch_id": superseded_by_import_batch_id,
                "request_id": request_id,
                "occurred_at": datetime.now(UTC).isoformat(),
            })
            self._complete_request(scope, request_id, {})
        finally:
            lock.release()

    def _supersede_creates_cycle(
        self, import_batch_id: str, superseded_by: str
    ) -> bool:
        current = superseded_by
        visited: set[str] = set()
        while True:
            if current == import_batch_id:
                return True
            if current in visited:
                return False
            visited.add(current)
            if not self._batch_path(current).is_file():
                return False
            state = self.current_state(current)
            if state.superseded_by_import_batch_id is None:
                return False
            current = state.superseded_by_import_batch_id


__all__ = [
    "EVT_APPROVAL",
    "EVT_FREEZE",
    "EVT_IMPORT_CREATED",
    "EVT_SUPERSEDE",
    "EVT_VALIDATION",
    "ApprovalResult",
    "FreezeResult",
    "ImportService",
    "ImportState",
    "ServiceError",
    "safe_relative_path",
    "sha256_bytes",
]
