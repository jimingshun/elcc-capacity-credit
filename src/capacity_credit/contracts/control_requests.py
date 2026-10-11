"""T25 atomic control-request contract (application service contract
V0.2 §5.2) - THE single control-request type source.

A control request is a small, strongly-typed document persisted by the
RunService control-request store (``run_service.ControlRequestStore`` -
the only persistence entry) under a controlled scope directory. Request
files are written atomically (temp file + fsync + rename), so a
half-written temp file can never be read as a legal request.

- ``action``: ``start`` | ``resume`` | ``stop_safely``
- ``request_status``: ``pending`` -> ``accepted`` -> ``completed`` |
  ``rejected`` (transitions are auditable in the stored record)
- ``run_id`` / ``expected_lifecycle_status`` may be ``None`` only for
  ``action="start"`` (the run does not exist yet); otherwise they are
  mandatory and strictly validated.
- The contract NEVER duplicates lifecycle or method-result state: it
  carries the request itself plus stable diagnostic strings only.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTROL_SCHEMA_VERSION = "1.0"
ControlAction = Literal["start", "resume", "stop_safely"]
ControlRequestStatus = Literal["pending", "accepted", "rejected", "completed"]

_TERMINAL_REQUEST_STATES = frozenset({"rejected", "completed"})


class ControlRequest(BaseModel):
    """One atomic lifecycle control request (strict contract)."""

    model_config = ConfigDict(extra="forbid")

    control_schema_version: Literal["1.0"]
    request_id: str
    run_id: uuid.UUID | None = None
    action: ControlAction
    requested_by: str
    requested_at: datetime
    expected_lifecycle_status: str | None = None
    request_status: ControlRequestStatus = "pending"
    diagnostics: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None

    @field_validator("request_id")
    @classmethod
    def _validate_request_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("request_id must be a canonical UUID string") from exc
        if str(parsed) != value:
            raise ValueError("request_id must be a canonical UUID string")
        return value

    @field_validator("requested_by")
    @classmethod
    def _validate_requested_by(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("requested_by must be a non-empty identity")
        return value

    @field_validator("requested_at")
    @classmethod
    def _validate_requested_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must carry an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_action_fields(self) -> ControlRequest:
        if self.action == "start":
            if self.run_id is not None:
                raise ValueError("action=start must not declare a run_id yet")
            if self.expected_lifecycle_status is not None:
                raise ValueError("action=start must not declare expected_lifecycle_status")
        elif self.action == "resume":
            if self.run_id is None:
                raise ValueError("action=resume requires a run_id")
            if not self.expected_lifecycle_status:
                raise ValueError("action=resume requires expected_lifecycle_status")
        else:  # stop_safely
            # a run-scoped stop names the run; a JOB-scoped stop (the
            # T24-B demo task has no RunService run) may precede the run
            # association and stays job-scoped with run_id=None
            if self.run_id is not None and not self.expected_lifecycle_status:
                raise ValueError(
                    "action=stop_safely with a run_id requires expected_lifecycle_status"
                )
        if self.request_status == "pending" and (self.diagnostics or self.result):
            raise ValueError("a pending request carries no diagnostics or result")
        if self.request_status == "rejected" and not self.diagnostics:
            raise ValueError("a rejected request must record a stable diagnostic")
        return self

    def params_hash(self) -> str:
        """Stable hash of the request PARAMETERS (idempotency scope):
        any parameter change with the same request_id is a conflict."""
        payload = json.dumps(
            {
                "action": self.action,
                "run_id": str(self.run_id) if self.run_id else None,
                "expected_lifecycle_status": self.expected_lifecycle_status,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes) -> ControlRequest:
        return cls.model_validate_json(data)

    @classmethod
    def create(
        cls,
        *,
        action: ControlAction,
        request_id: str | None = None,
        run_id: str | None = None,
        requested_by: str,
        expected_lifecycle_status: str | None = None,
    ) -> ControlRequest:
        return cls(
            control_schema_version="1.0",
            request_id=request_id or str(uuid.uuid4()),
            # pass the raw string: pydantic coerces/validates it against
            # the UUID-typed field (malformed ids become ValidationError)
            run_id=cast("Any", run_id) or None,
            action=action,
            requested_by=requested_by,
            requested_at=datetime.now(UTC),
            expected_lifecycle_status=expected_lifecycle_status,
        )


def export_json_schema(path: Path | str) -> str:
    """Export the control-request JSON Schema (the published
    ``schemas/control-request-1.0.json`` contract document)."""
    schema = (
        json.dumps(ControlRequest.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    Path(path).write_text(schema, encoding="utf-8")
    return schema


__all__ = [
    "CONTROL_SCHEMA_VERSION",
    "ControlAction",
    "ControlRequest",
    "ControlRequestStatus",
    "export_json_schema",
]
