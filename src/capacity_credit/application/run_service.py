"""Run creation gate and lifecycle service (T05, review-hardened).

Formal runs require the full gate (validation, approval, frozen
version, approved-version match, frozen integrity, not superseded);
``run_mode=teaching`` may relax only gates that are actually missing,
with an explicit reason and a ``teaching_gate_relaxed`` audit event;
supersede blocks every run mode. ``run_manifest.json`` is generated
strictly by the ``RunManifest`` contract and round-trips through it
(no private extra fields; data-grade evidence goes to audit events and
the frozen manifest). ``scenario_bank_id`` has a single authoritative
source (the RunConfig field); a method argument must agree or be
absent, and simulation run types without it are blocked. create_run is
idempotent and concurrency-safe via request-level locks, atomic
pre-registration with a pre-allocated run_id, and recovery by reading
the published manifest. Lifecycle transitions hold a run-level lock and
never fabricate ``result_status``.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from pydantic import ValidationError

from capacity_credit.application.atomic_io import (
    ObjectExistsError,
    atomic_write_bytes,
    hash_payload,
    is_transient_io_conflict,
    safe_filename_component,
)
from capacity_credit.application.audit import AuditLog
from capacity_credit.application.import_service import (
    ImportService,
    ServiceError,
)
from capacity_credit.application.locks import RunLock
from capacity_credit.contracts.control_requests import ControlRequest
from capacity_credit.contracts.enums import LifecycleStatus, RunType
from capacity_credit.contracts.runs import RunManifest

EVT_RUN_CREATED = "run_created"
EVT_TEACHING_RELAXED = "teaching_gate_relaxed"
EVT_LIFECYCLE = "lifecycle_transition"
EVT_CONTROL_REQUEST = "control_request"

_SIMULATION_RUN_TYPES = {
    RunType.ADEQUACY,
    RunType.ELCC,
    RunType.EFC,
    RunType.MRI,
    RunType.RMRI,
    RunType.PORTFOLIO,
    RunType.SENSITIVITY,
    RunType.BENCHMARK,
    RunType.DEMAND_TARGET_CAPACITY,
}


@dataclass(frozen=True)
class LineageHashes:
    """SERVER-computed config-lineage hashes of a validated normalized
    ELCC config: ``method_config_hash`` (method config ONLY) and
    ``application_config_hash`` (full normalized application config).
    Caller-supplied values are verified against these; the persisted
    idempotency parameters and the RunManifest always use the
    server-computed values."""

    method_config_hash: str
    application_config_hash: str


_ALLOWED_REQUEST_TRANSITIONS = {
    "pending": {"accepted", "rejected"},
    "accepted": {"completed", "rejected"},
    "rejected": set(),
    "completed": set(),
}

# Bounded retry for TRANSIENT request-file read conflicts only: a Windows
# sharing/lock violation (another process holding the file inside its
# atomic publish window) is retried with a small FIXED attempt budget and
# a short backoff - never an unbounded wait, never a long fixed sleep.
# Exhaustion surfaces the stable CONTROL_REQUEST_IO_BUSY, which is never
# misreported as a corrupt or contract-violating file. The transient
# classification itself is the single shared atomic_io entry.
_REQUEST_READ_MAX_ATTEMPTS = 3
_REQUEST_READ_BACKOFF_SECONDS = 0.15


def _read_request_bytes(path: Path) -> bytes:
    """THE shared control-request file read (used by load and
    list_requests): retries ONLY transient conflicts (Windows
    sharing/lock violations, EBUSY and their equivalents) within the
    bounded budget, then raises the stable CONTROL_REQUEST_IO_BUSY; a
    real access denial is never retried and surfaces the explicit
    CONTROL_REQUEST_ACCESS_DENIED contract; any other OS error
    propagates unchanged (never misreported as corruption)."""
    attempt = 0
    while True:
        try:
            return path.read_bytes()
        except OSError as exc:
            if is_transient_io_conflict(exc):
                attempt += 1
                if attempt >= _REQUEST_READ_MAX_ATTEMPTS:
                    raise ServiceError(
                        "CONTROL_REQUEST_IO_BUSY",
                        f"control request file busy after {attempt} attempt(s) "
                        f"(transient sharing conflict did not clear): {path.name}",
                    ) from exc
                time.sleep(_REQUEST_READ_BACKOFF_SECONDS)
                continue
            if isinstance(exc, PermissionError):
                raise ServiceError(
                    "CONTROL_REQUEST_ACCESS_DENIED",
                    f"control request file access denied (winerror "
                    f"{getattr(exc, 'winerror', None)}): {path.name}",
                ) from exc
            raise


class ControlRequestStore:
    """THE single control-request persistence implementation
    (application service contract V0.2 §5.2; used for run-scoped
    requests through :class:`RunService` and for worker job-scoped
    requests by the T25 worker - never a second store).

    Files live in one controlled scope directory:
    ``<request_id>.json`` written atomically (temp + fsync + rename), so
    a half-written temp file (``*.tmp``) is NEVER read as a legal
    request. The same ``request_id`` with the same parameters is
    idempotent; the same ``request_id`` with different parameters is a
    stable conflict. ``pending -> accepted -> completed|rejected``
    transitions are recorded in the file (audit trail) - ``rejected``
    and ``completed`` are terminal.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path_for(self, request_id: str) -> Path:
        return self.directory / f"{safe_filename_component(request_id)}.json"

    def submit(self, request: ControlRequest) -> dict[str, Any]:
        """Persist a new request or return the existing record
        idempotently. Path traversal is impossible: the file name is a
        validated safe filename component, and the directory is fixed."""
        from capacity_credit.application.atomic_io import ObjectExistsError

        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(request.request_id)
        payload: dict[str, Any] = json.loads(request.to_json_bytes())
        payload["params_hash"] = request.params_hash()
        if path.exists():
            existing = self.load(request.request_id)
            if existing.get("params_hash") != payload["params_hash"]:
                raise ServiceError(
                    "REQUEST_ID_CONFLICT",
                    f"request_id {request.request_id} reused with different parameters",
                )
            return existing
        try:
            # create-if-absent: concurrent duplicate submits race on the
            # SAME params, so the loser re-reads the winner's record
            # (identical by params_hash) - one side effect, ever
            stored = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            atomic_write_bytes(path, stored, immutable=True)
        except ObjectExistsError:
            existing = self.load(request.request_id)
            if existing.get("params_hash") != payload["params_hash"]:
                raise ServiceError(
                    "REQUEST_ID_CONFLICT",
                    f"request_id {request.request_id} reused with different parameters",
                )
            return existing
        return payload

    def load(self, request_id: str) -> dict[str, Any]:
        """Load one stored request; a corrupt or non-contract file is a
        stable error (never silently read as a legal request)."""
        path = self.path_for(request_id)
        if not path.is_file():
            raise ServiceError(
                "CONTROL_REQUEST_NOT_FOUND", f"control request missing: {request_id}"
            )
        # the shared bounded-retry read: a transient sharing conflict is
        # CONTROL_REQUEST_IO_BUSY (never misreported as corruption)
        data = _read_request_bytes(path)
        try:
            payload = json.loads(data.decode("utf-8"))
            # the store persists its own bookkeeping keys alongside the
            # contract; validate the CONTRACT fields only
            core = {
                key: value
                for key, value in payload.items()
                if key not in ("params_hash", "updated_at")
            }
            ControlRequest.model_validate(core)
        except Exception as exc:
            raise ServiceError(
                "CONTROL_REQUEST_INVALID",
                f"control request {request_id} is corrupt or violates the contract: {exc}",
            ) from exc
        return cast(dict[str, Any], payload)

    def list_requests(self) -> list[dict[str, Any]]:
        """THE single read-only enumeration entry: EVERY stored request
        with its current status (pending/accepted/completed/rejected).
        Temp files (``*.tmp``) are never listed; a corrupt request file
        is surfaced as an invalid entry instead of being silently
        hidden - callers refuse execution rather than ignore it."""
        if not self.directory.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            # the same shared bounded-retry read: a transient sharing
            # conflict is CONTROL_REQUEST_IO_BUSY (propagated, never an
            # invalid entry); genuine parse/contract failures stay invalid
            data = _read_request_bytes(path)
            try:
                payload = json.loads(data.decode("utf-8"))
                ControlRequest.model_validate(
                    {
                        key: value
                        for key, value in payload.items()
                        if key not in ("params_hash", "updated_at")
                    }
                )
            except Exception:  # noqa: BLE001 - surfaced, never executed
                results.append(
                    {
                        "request_id": path.stem,
                        "invalid": True,
                        "path": str(path),
                    }
                )
                continue
            results.append(cast(dict[str, Any], payload))
        return results

    def pending_requests(self) -> list[dict[str, Any]]:
        """The pending subset of :meth:`list_requests`."""
        return [
            entry
            for entry in self.list_requests()
            if entry.get("request_status") == "pending"
        ]

    def update(
        self,
        request_id: str,
        *,
        status: str | None = None,
        diagnostic: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Transition a stored request (``pending -> accepted ->
        completed|rejected``); illegal transitions and terminal states
        are stable errors, and identical repeats are idempotent."""
        path = self.path_for(request_id)
        payload = self.load(request_id)
        current = payload.get("request_status", "pending")
        if status is not None:
            if status == current:
                pass  # idempotent repeat
            elif status not in _ALLOWED_REQUEST_TRANSITIONS.get(current, set()):
                raise ServiceError(
                    "CONTROL_REQUEST_STATE_INVALID",
                    f"illegal control request transition {current} -> {status}",
                )
            else:
                payload["request_status"] = status
        if diagnostic is not None and diagnostic not in payload.get("diagnostics", []):
            payload.setdefault("diagnostics", []).append(diagnostic)
        if result is not None:
            merged = dict(payload.get("result") or {})
            merged.update(result)
            payload["result"] = merged
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self._atomic_write(
            path,
            (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        return payload

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        atomic_write_bytes(path, data)


def _canonical_method_config_payload(config_dict: dict[str, Any]) -> str | None:
    """Stable normalized JSON payload of the method config ONLY (single
    source of ``method_config_hash``)."""
    method = config_dict.get("method_config")
    if method is None:
        return None
    return json.dumps(method, sort_keys=True, ensure_ascii=False)


def _canonical_application_payload(
    run_type: RunType,
    config_dict: dict[str, Any],
) -> str | None:
    """Stable normalized JSON payload of the FULL application config,
    reconstructed from the validated RunConfig + method sections in the
    exact application-config document shape - METHOD-AWARE:

    - run_type=elcc -> ElccApplicationConfig shape (search, load_growth,
      candidate, convergence, storage_soc);
    - run_type=efc  -> EfcApplicationConfig shape (search, candidate,
      convergence, perfect_capacity, upper_bound, storage_soc) - EFC
      NEVER reads load_growth;
    - any other run type -> None.

    This is the single source of ``application_config_hash``."""
    method = config_dict.get("method_config")
    if method is None:
        return None
    run_level = {key: value for key, value in config_dict.items() if key != "method_config"}
    if run_type is RunType.ELCC:
        application = {
            "elcc_config_schema_version": "1.0",
            "run_config": run_level,
            "search": method["search"],
            "load_growth": method["load_growth"],
            "candidate": method["candidate"],
            "convergence": method["convergence"],
            "storage_soc": method.get("storage_soc"),
            # MR-02: the calibrated-baseline closure is part of the
            # application-config lineage (None when absent)
            "calibrated_baseline_ref": method.get("calibrated_baseline_ref"),
        }
    elif run_type is RunType.EFC:
        application = {
            "efc_config_schema_version": "1.0",
            "run_config": run_level,
            "search": method["search"],
            "candidate": method["candidate"],
            "convergence": method["convergence"],
            "perfect_capacity": method["perfect_capacity"],
            "upper_bound": method["upper_bound"],
            "storage_soc": method.get("storage_soc"),
            # MR-01: criterion calibre + de-rating identity are part of
            # the application-config lineage (annual / None when absent)
            "criterion": method.get("criterion"),
            "reporting": method.get("reporting"),
            "emergency_layer": method.get("emergency_layer"),
        }
    else:
        return None
    return json.dumps(application, sort_keys=True, ensure_ascii=False)


class RunService:
    """Run lifecycle service (no simulation; gates and persistence only)."""

    def __init__(self, root: Path | str, import_service: ImportService) -> None:
        self.root = Path(root)
        self.import_service = import_service

    # -- paths -----------------------------------------------------------

    def _run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / safe_filename_component(run_id)

    def _requests_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "requests"

    def _create_requests_dir(self) -> Path:
        return self.root / "requests" / "create"

    def _run_lock(self, run_id: str) -> RunLock:
        return RunLock(self.root / "locks" / "runs", run_id)

    def _audit(self, run_id: str) -> AuditLog:
        return AuditLog(self._run_dir(run_id) / "audit.jsonl")

    @staticmethod
    def _atomic_write(path: Path, data: bytes, *, immutable: bool = False) -> None:
        atomic_write_bytes(path, data, immutable=immutable)

    def _write_immutable_or_identical(
        self, path: Path, expected_bytes: bytes, error_code: str
    ) -> None:
        """Create immutably if missing; reuse if byte-identical; block
        otherwise (never overwrite). Handles concurrent create races."""
        if not path.exists():
            try:
                self._atomic_write(path, expected_bytes, immutable=True)
                return
            except ObjectExistsError:
                pass
        existing = path.read_bytes()
        if existing != expected_bytes:
            raise ServiceError(
                error_code, f"existing file differs at {path.name}; overwrite forbidden"
            )

    def _ensure_event(
        self,
        run_id: str,
        event_type: str,
        request_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Append only if no event of the same type/request_id exists."""
        if any(
            e.get("event_type") == event_type and e.get("request_id") == request_id
            for e in self._audit(run_id).read()
        ):
            return False
        self._audit(run_id).append(event)
        return True

    # -- request idempotency ---------------------------------------------

    @staticmethod
    def _request_file(scope_dir: Path, request_id: str) -> Path:
        return scope_dir / f"{safe_filename_component(request_id)}.json"

    @staticmethod
    def _params_hash(action: str, params: dict[str, Any]) -> str:
        return hash_payload({"action": action, **params})

    def _read_request(
        self, scope_dir: Path, request_id: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_file = self._request_file(scope_dir, request_id)
        if not request_file.exists():
            return None
        recorded = cast(dict[str, Any], json.loads(request_file.read_text(encoding="utf-8")))
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

    def _complete_request(self, scope_dir: Path, request_id: str, result: dict[str, Any]) -> None:
        """Mark a request committed with its result.

        Idempotent short-circuit: when the record is already committed
        with the identical result, no write is performed - avoids
        repeated ``os.replace`` on the same request file by concurrent
        recovery callers; a failed replace recovers naturally on retry.
        """
        request_file = self._request_file(scope_dir, request_id)
        recorded = cast(dict[str, Any], json.loads(request_file.read_text(encoding="utf-8")))
        if recorded.get("status") == "committed" and all(
            recorded.get(key) == value for key, value in result.items()
        ):
            return
        recorded.update(result)
        recorded["status"] = "committed"
        self._atomic_write(
            request_file,
            (json.dumps(recorded, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
            immutable=False,
        )

    @staticmethod
    def _acquire_lock(lock: RunLock, timeout: float = 10.0) -> None:
        """Blocking acquire with retries; concurrent callers wait."""
        deadline = time.monotonic() + timeout
        while True:
            if lock.acquire():
                return
            if time.monotonic() >= deadline:
                raise ServiceError("GATE_LOCKED", "lock acquisition timed out")
            time.sleep(0.02)

    # -- run creation ----------------------------------------------------

    def _verify_lineage(
        self,
        run_type: RunType,
        config_dict: dict[str, Any],
        method_config_hash: str | None,
        application_config_hash: str | None,
    ) -> LineageHashes:
        """RunService-side METHOD-AWARE lineage computation and
        verification: for run_type in {elcc, efc} BOTH hashes are
        RECOMPUTED from the already-validated normalized config (never
        trusted from the caller; ELCC keeps its own document shape and
        EFC never reads load_growth); caller-supplied values that
        disagree are rejected with stable ServiceError codes. Omitted
        caller hashes are simply computed and persisted. The
        server-computed LineageHashes are the ONLY values used in
        idempotency parameters, the RunManifest and recovery
        verification."""
        method_payload = _canonical_method_config_payload(config_dict)
        if method_payload is None:
            raise ServiceError(
                "GATE_METHOD_CONFIG_REQUIRED",
                f"run_type={run_type.value} requires method_config (full configuration lineage)",
            )
        computed_method = hashlib.sha256(method_payload.encode("utf-8")).hexdigest()
        if method_config_hash is not None and method_config_hash != computed_method:
            raise ServiceError(
                "GATE_METHOD_CONFIG_HASH_MISMATCH",
                "caller-supplied method_config_hash does not match the "
                "validated normalized method config",
            )
        application_payload = _canonical_application_payload(run_type, config_dict)
        if application_payload is None:
            raise ServiceError(
                "GATE_METHOD_CONFIG_REQUIRED",
                "cannot reconstruct the application config lineage",
            )
        computed_application = hashlib.sha256(application_payload.encode("utf-8")).hexdigest()
        if application_config_hash is not None and application_config_hash != computed_application:
            raise ServiceError(
                "GATE_APPLICATION_CONFIG_HASH_MISMATCH",
                "caller-supplied application_config_hash does not match "
                "the validated normalized application config",
            )
        return LineageHashes(
            method_config_hash=computed_method,
            application_config_hash=computed_application,
        )

    def _validate_create_arguments(
        self,
        config_dict: dict[str, Any],
        scenario_bank_id: str | None,
        method_config_hash: str | None,
        application_config_hash: str | None,
    ) -> LineageHashes | None:
        """Argument-level gates: authoritative scenario bank id,
        simulation bank requirement, ELCC lineage-hash policy (non-ELCC
        runs must NOT carry ELCC lineage hashes) and the verified
        server-computed lineage. ALL failures happen before the request
        lock / request file / run directory exist."""
        config_bank = config_dict.get("scenario_bank_id")
        if (
            scenario_bank_id is not None
            and config_bank is not None
            and scenario_bank_id != config_bank
        ):
            raise ServiceError(
                "GATE_SCENARIO_BANK_CONFLICT",
                "scenario_bank_id argument differs from run_config",
            )
        if scenario_bank_id is not None:
            config_dict["scenario_bank_id"] = scenario_bank_id
        run_type = RunType(config_dict["run_type"])
        if run_type in _SIMULATION_RUN_TYPES and not config_dict.get("scenario_bank_id"):
            raise ServiceError(
                "GATE_SCENARIO_BANK_REQUIRED",
                "simulation run types require scenario_bank_id",
            )
        if run_type not in (RunType.ELCC, RunType.EFC) and (
            method_config_hash is not None
            or (
                application_config_hash is not None
                and run_type is not RunType.DEMAND_TARGET_CAPACITY
            )
        ):
            raise ServiceError(
                "GATE_LINEAGE_HASH_NOT_ALLOWED",
                "lineage hashes are only valid for run_type=elcc/efc "
                "(demand_target_capacity may carry application_config_hash only: "
                "its typed config freezes separately as "
                "target_capacity_config.json)",
            )
        if run_type is RunType.DEMAND_TARGET_CAPACITY:
            # 无 method_config 可重算（类型化配置冻结为
            # target_capacity_config.json，其完整性由发布链的 artifact
            # manifest 哈希闭环核验）——按入参直通登记 application 哈希
            return LineageHashes(
                method_config_hash=None,
                application_config_hash=application_config_hash,
            )
        if run_type not in (RunType.ELCC, RunType.EFC):
            return None
        return self._verify_lineage(
            run_type, config_dict, method_config_hash, application_config_hash
        )

    def create_run(
        self,
        *,
        request_id: str,
        run_config: dict[str, Any],
        import_batch_id: str,
        created_by: str,
        software_version: str,
        software_commit: str,
        scenario_bank_id: str | None = None,
        teaching_override_reason: str | None = None,
        method_config_hash: str | None = None,
        application_config_hash: str | None = None,
    ) -> dict[str, Any]:
        try:
            from capacity_credit.contracts.runs import load_run_config

            config = load_run_config(run_config)
        except ValidationError as exc:
            raise ServiceError(
                "GATE_RUN_CONFIG_INVALID",
                f"run_config failed validation: {exc}",
            ) from exc
        config_dict = config.model_dump(mode="json")
        lineage = self._validate_create_arguments(
            config_dict,
            scenario_bank_id,
            method_config_hash,
            application_config_hash,
        )

        params = {
            "run_config": config_dict,
            "import_batch_id": import_batch_id,
            "created_by": created_by,
            "software_version": software_version,
            "software_commit": software_commit,
            "teaching_override_reason": teaching_override_reason,
            "method_config_hash": lineage.method_config_hash if lineage else None,
            "application_config_hash": (lineage.application_config_hash if lineage else None),
        }
        lock = RunLock(self.root / "locks" / "requests", request_id)
        self._acquire_lock(lock)
        try:
            scope = self._create_requests_dir()
            pre = self._read_request(scope, request_id, "create_run", params)
            if pre is not None:
                run_id = pre["run_id"]
                # continue (or validate) the interrupted creation with the
                # pre-allocated run_id; never return without verification
            else:
                run_id = str(uuid.uuid4())
                self._pre_register(scope, request_id, "create_run", params, {"run_id": run_id})

            state = self.import_service.current_state(import_batch_id)
            run_mode = config_dict["run_mode"]
            relaxed_gates: list[str] = []
            data_source: str
            if run_mode == "teaching":
                relaxed_gates, data_source = self._teaching_gates(
                    state,
                    import_batch_id,
                    teaching_override_reason,
                    config_dict,
                )
            else:
                self._check_formal_gates(state, import_batch_id)
                data_source = "frozen"

            yaml_text = load_run_config(config_dict).to_yaml()
            yaml_bytes = yaml_text.encode("utf-8")
            run_config_hash = hashlib.sha256(yaml_bytes).hexdigest()

            run_dir = self._run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            self._write_immutable_or_identical(
                run_dir / "run_config.yaml", yaml_bytes, "GATE_RUN_CONFIG_MISMATCH"
            )

            if data_source == "frozen":
                self.import_service.verify_frozen_integrity(import_batch_id)

            manifest_model = RunManifest(
                run_schema_version="1.0",
                run_id=run_id,
                run_type=config_dict["run_type"],
                run_mode=run_mode,
                lifecycle_status=LifecycleStatus.CREATED,
                dataset_id=state.dataset_id,
                data_version_id=state.data_version_id,
                data_version_hash=state.data_version_hash,
                import_batch_id=uuid.UUID(import_batch_id),
                run_config_path=f"runs/{run_id}/run_config.yaml",
                run_config_hash=run_config_hash,
                scenario_bank_id=config_dict.get("scenario_bank_id"),
                source_run_id=config_dict.get("source_run_id"),
                parent_run_ids=config_dict.get("parent_run_ids", []),
                software_version=software_version,
                software_commit=software_commit,
                method_profile=config_dict["method_profile"],
                capacity_value_type=config_dict["capacity_value_type"],
                baseline_portfolio_version=config_dict.get("baseline_portfolio_version"),
                allocation_type=config_dict["allocation_type"],
                allocation_rule_id=config_dict.get("allocation_rule_id"),
                created_by=created_by,
                created_at=datetime.now(UTC),
                result_id=None,
                artifact_manifest_ids=[],
                method_config_hash=lineage.method_config_hash if lineage else None,
                application_config_hash=(lineage.application_config_hash if lineage else None),
            )
            manifest = cast(dict[str, Any], json.loads(manifest_model.model_dump_json()))
            manifest_path = run_dir / "run_manifest.json"
            if manifest_path.is_file():
                # recovery: validate the existing manifest against this request
                self._validate_existing_manifest(
                    manifest_path,
                    manifest_model,
                    run_id,
                    config_dict,
                    software_version,
                    software_commit,
                    import_batch_id,
                )
                manifest = self._read_manifest(run_id)
            else:
                self._atomic_write(
                    manifest_path,
                    (manifest_model.model_dump_json() + "\n").encode("utf-8"),
                    immutable=True,
                )
            grades = state.grades or {}
            self._ensure_event(
                run_id,
                EVT_RUN_CREATED,
                request_id,
                {
                    "event_type": EVT_RUN_CREATED,
                    "run_id": run_id,
                    "import_batch_id": import_batch_id,
                    "request_id": request_id,
                    "data_source": data_source,
                    "data_version_id": state.data_version_id,
                    "data_version_hash": state.data_version_hash,
                    "declared_data_grade": grades.get("declared_data_grade"),
                    "computed_max_data_grade": grades.get("computed_max_data_grade"),
                    "effective_data_grade": grades.get("effective_data_grade"),
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
            if relaxed_gates:
                self._ensure_event(
                    run_id,
                    EVT_TEACHING_RELAXED,
                    request_id,
                    {
                        "event_type": EVT_TEACHING_RELAXED,
                        "run_id": run_id,
                        "request_id": request_id,
                        "reason": teaching_override_reason,
                        "relaxed_gates": relaxed_gates,
                        "data_source": data_source,
                        "occurred_at": datetime.now(UTC).isoformat(),
                    },
                )
            self._complete_request(scope, request_id, {"run_id": run_id})
            return manifest
        finally:
            lock.release()

    def _validate_existing_manifest(
        self,
        manifest_path: Path,
        expected: RunManifest,
        run_id: str,
        config_dict: dict[str, Any],
        software_version: str,
        software_commit: str,
        import_batch_id: str,
    ) -> None:
        """Validate an already-published run_manifest against the current
        request; external tampering must block, never be treated as an
        idempotent result."""
        try:
            existing = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ServiceError(
                "GATE_RUN_MANIFEST_MISMATCH",
                f"existing run_manifest fails contract: {exc}",
            ) from exc
        expected_dict = json.loads(expected.model_dump_json())
        checks = {
            "run_id": str(existing.run_id) == run_id,
            "run_type": existing.run_type.value == config_dict["run_type"],
            "run_mode": existing.run_mode.value == config_dict["run_mode"],
            "import_batch_id": str(existing.import_batch_id) == import_batch_id,
            "run_config_hash": existing.run_config_hash == expected_dict["run_config_hash"],
            "software_version": existing.software_version == software_version,
            "software_commit": existing.software_commit == software_commit,
            "method_profile": existing.method_profile == config_dict["method_profile"],
            "capacity_value_type": existing.capacity_value_type.value
            == config_dict["capacity_value_type"],
            "method_config_hash": existing.method_config_hash
            == expected_dict["method_config_hash"],
            "application_config_hash": existing.application_config_hash
            == expected_dict["application_config_hash"],
        }
        for field, ok in checks.items():
            if not ok:
                raise ServiceError(
                    "GATE_RUN_MANIFEST_MISMATCH",
                    f"existing run_manifest field {field} inconsistent with request",
                )

    def _teaching_gates(
        self,
        state: Any,
        import_batch_id: str,
        override_reason: str | None,
        config_dict: dict[str, Any],
    ) -> tuple[list[str], str]:
        """Teaching relaxation: only gates actually missing are relaxed;
        fully gated teaching runs need no reason and no relaxation
        event; supersede blocks every mode; validation and content-hash
        always apply."""
        if state.superseded_by_import_batch_id is not None:
            raise ServiceError("GATE_SUPERSEDED", "import batch was superseded")
        if state.validation_status != "passed":
            raise ServiceError(
                "GATE_VALIDATION_NOT_PASSED",
                "teaching runs still require validation_status=passed",
            )
        relaxed: list[str] = []
        if state.import_review_status != "approved":
            relaxed.append("import_review_status=approved")
        if state.data_version_status != "frozen":
            relaxed.append("data_version_status=frozen")
        if relaxed and (not override_reason or not override_reason.strip()):
            raise ServiceError(
                "GATE_TEACHING_OVERRIDE_REQUIRED",
                "teaching relaxation requires a non-empty teaching_override_reason",
            )
        if state.data_version_status == "frozen":
            # frozen data takes precedence: verify and bind the frozen version
            self.import_service.verify_frozen_integrity(import_batch_id)
            data_source = "frozen"
        else:
            current = self.import_service._recompute_staging_hash(import_batch_id)
            if current != state.data_version_hash:
                raise ServiceError(
                    "GATE_INTEGRITY_FAILED",
                    "staging content does not match data_version_hash",
                )
            data_source = "staging"
        return relaxed, data_source

    def _check_formal_gates(self, state: Any, import_batch_id: str) -> None:
        if state.superseded_by_import_batch_id is not None:
            raise ServiceError("GATE_SUPERSEDED", "import batch was superseded")
        if state.validation_status != "passed":
            raise ServiceError(
                "GATE_VALIDATION_NOT_PASSED",
                "formal run requires validation_status=passed",
            )
        if state.import_review_status != "approved":
            raise ServiceError(
                "GATE_NOT_APPROVED",
                "formal run requires import_review_status=approved",
            )
        if state.approved_data_version_id != state.data_version_id:
            raise ServiceError(
                "GATE_APPROVED_VERSION_MISMATCH",
                "approved_data_version_id must equal data_version_id",
            )
        if state.data_version_status != "frozen":
            raise ServiceError(
                "GATE_VERSION_NOT_FROZEN",
                "formal run requires data_version_status=frozen",
            )
        if state.grades is None:
            raise ServiceError(
                "GATE_GRADE_UNAVAILABLE",
                "data grades unavailable; formal run blocked",
            )
        self.import_service.verify_frozen_integrity(import_batch_id)

    def _read_manifest(self, run_id: str) -> dict[str, Any]:
        path = self._run_dir(run_id) / "run_manifest.json"
        if not path.is_file():
            raise ServiceError("GATE_INVALID_STATE", f"unknown run {run_id}")
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    # -- lifecycle -------------------------------------------------------

    def transition(
        self,
        *,
        request_id: str,
        run_id: str,
        target_lifecycle: str,
    ) -> None:
        try:
            target = LifecycleStatus(target_lifecycle)
        except ValueError as exc:
            raise ServiceError(
                "ILLEGAL_TRANSITION", f"unknown lifecycle target {target_lifecycle!r}"
            ) from exc
        params = {"run_id": run_id, "target_lifecycle": target_lifecycle}
        lock = self._run_lock(run_id)
        self._acquire_lock(lock)
        try:
            scope = self._requests_dir(run_id)
            pre = self._read_request(scope, request_id, "lifecycle_transition", params)
            if pre is not None and pre.get("status") == "committed":
                return
            events = self.lifecycle_events(run_id)
            if any(
                e.get("request_id") == request_id and e.get("event_type") == EVT_LIFECYCLE
                for e in events
            ):
                self._complete_request(scope, request_id, {})
                return
            current = self.run_state(run_id)["lifecycle_status"]
            current_enum = LifecycleStatus(current)
            try:
                from capacity_credit.application import require_transition

                require_transition(current_enum, target)
            except ValueError as exc:
                raise ServiceError(
                    "ILLEGAL_TRANSITION",
                    f"illegal transition {current} -> {target_lifecycle}",
                ) from exc
            self._pre_register(scope, request_id, "lifecycle_transition", params)
            self._audit(run_id).append(
                {
                    "event_type": EVT_LIFECYCLE,
                    "run_id": run_id,
                    "from": current,
                    "to": target.value,
                    "request_id": request_id,
                    "occurred_at": datetime.now(UTC).isoformat(),
                }
            )
            self._complete_request(scope, request_id, {})
        finally:
            lock.release()

    def run_state(self, run_id: str) -> dict[str, Any]:
        manifest = self._read_manifest(run_id)
        current = manifest["lifecycle_status"]
        for event in self._audit(run_id).read():
            if event.get("event_type") == EVT_LIFECYCLE:
                current = event["to"]
        return {
            "run_id": run_id,
            "lifecycle_status": current,
            "result_status": None,
        }

    def lifecycle_events(self, run_id: str) -> list[dict[str, Any]]:
        return [
            event
            for event in self._audit(run_id).read()
            if event.get("event_type") == EVT_LIFECYCLE
        ]

    # -- run-scoped control requests (T25) --------------------------------

    def control_store(self, run_id: str) -> ControlRequestStore:
        """THE run-scoped control-request store (one directory per run
        inside the controlled run directory)."""
        return ControlRequestStore(self._run_dir(run_id) / "control")

    def submit_run_control_request(self, run_id: str, request: ControlRequest) -> dict[str, Any]:
        """Persist a run-scoped control request (e.g. ``stop_safely``
        against a running run). The run must exist; the request is
        validated by the contract and stored atomically."""
        self._read_manifest(run_id)  # unknown runs are rejected
        return self.control_store(run_id).submit(request)


class RunSummary(TypedDict):
    """One read-only run/result row for the business workbench results
    page (T26A-MUST-03): manifest identity + the formal lifecycle from
    RunService.run_state + result_status ONLY when a formal legal
    MethodResult exists (never inferred from filenames, job state or the
    run lifecycle); a corrupt manifest is an invalid entry, never
    silently skipped."""

    run_id: str
    run_type: str
    lifecycle_status: str
    result_status: str | None
    created_by: str
    created_at: str
    invalid: bool
    invalid_reason: str | None


def list_runs(project_dir: Path | str) -> list[RunSummary]:
    """THE read-only run/result-list aggregation for the business
    workbench (T26A-MUST-03): enumerates ONLY first-level directories of
    <project_dir>/runs, never recurses, never writes anything; hidden
    directories (".*") and non-directories are skipped; a missing
    project/runs directory returns an empty list WITHOUT constructing
    any service (no directory is ever created as a side effect); a
    corrupt manifest or an illegal method result becomes an invalid
    entry (never silently dropped, never presented as a valid run).
    Ordering is deterministic (created_at descending, run_id ascending
    on ties); repeated calls return identical results."""
    project = Path(project_dir)
    if not (project / "runs").is_dir():
        return []
    import_service = ImportService(project)
    service = RunService(project, import_service)
    results: list[RunSummary] = []
    for entry in sorted((project / "runs").iterdir()):
        if (
            entry.name.startswith(".")
            or entry.is_symlink()  # a linked directory is never followed:
            # the gate short-circuits BEFORE is_dir would follow the link
            or not entry.is_dir()
        ):
            continue

        def _invalid(entry_name: str, reason: str) -> RunSummary:
            return RunSummary(
                run_id=entry_name,
                run_type="",
                lifecycle_status="",
                result_status=None,
                created_by="",
                created_at="",
                invalid=True,
                invalid_reason=reason[:300],
            )

        # the WHOLE single-run read+validate+project sequence stays inside
        # the per-entry isolation boundary: one damaged run never aborts
        # the listing and never becomes invalid=False
        try:
            run_id = safe_filename_component(entry.name)
            # the manifest must satisfy the formal RunManifest contract
            # (never a raw json.loads read at the aggregation boundary)
            manifest_path = entry / "run_manifest.json"
            if not manifest_path.is_file():
                raise ServiceError("GATE_INVALID_STATE", f"unknown run {run_id}")
            manifest_model = RunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if str(manifest_model.run_id) != run_id:
                raise ServiceError(
                    "GATE_INVALID_STATE", "run manifest id does not match the directory"
                )
            manifest = cast(
                dict[str, Any], json.loads(manifest_model.model_dump_json())
            )
            # the lifecycle comes only from the formal run_state entry;
            # its value must satisfy the LifecycleStatus constraint, and a
            # damaged lifecycle audit is contained in this entry
            lifecycle = str(service.run_state(run_id)["lifecycle_status"])
            LifecycleStatus(lifecycle)
            result_status: str | None = None
            result_path = entry / "method_result.json"
            if result_path.is_file():
                from capacity_credit.application.result_service import (
                    read_method_result_any,
                )

                result = read_method_result_any(result_path)
                if str(getattr(result, "run_id", "")) != run_id:
                    raise ValueError(
                        f"method_result run_id {getattr(result, 'run_id', None)} "
                        f"does not belong to run {run_id}"
                    )
                status_value = getattr(result, "result_status", None)
                if status_value is None:
                    raise TypeError("method_result lacks result_status")
                result_status = str(getattr(status_value, "value", status_value))
            results.append(
                RunSummary(
                    run_id=run_id,
                    run_type=str(manifest.get("run_type", "")),
                    lifecycle_status=lifecycle,
                    result_status=result_status,
                    created_by=str(manifest.get("created_by", "")),
                    created_at=str(manifest.get("created_at", "")),
                    invalid=False,
                    invalid_reason=None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - per-entry isolation: one
            # damaged run (manifest contract, lifecycle audit, or a
            # misbound/illegal result) never aborts the listing and never
            # becomes a valid entry
            results.append(_invalid(entry.name, str(exc)))
            continue
    results.sort(key=lambda item: item["run_id"])
    results.sort(key=lambda item: item["created_at"], reverse=True)
    return results


__all__ = [
    "EVT_CONTROL_REQUEST",
    "EVT_LIFECYCLE",
    "EVT_RUN_CREATED",
    "EVT_TEACHING_RELAXED",
    "ControlRequestStore",
    "RunService",
    "RunSummary",
    "list_runs",
]
