"""Append-only audit log (JSONL) with cross-process mutual exclusion.

Audit events are appended as one JSON line per record; existing events
are never modified in place. ``event_id`` is system-generated and cannot
be overridden: an event carrying the reserved ``event_id`` key is
rejected. Appends hold a byte-range guard lock (per-audit ``<path>.lock``
file) across processes, write the complete record once, then flush and
fsync before returning. Event fields are an internal implementation
structure (not a controlled P0 contract schema); event type and state
values reuse the controlled enums of the design baseline.
"""

from __future__ import annotations

import json
import msvcrt
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import ensure_guard_file

_RESERVED_KEYS = frozenset({"event_id"})


class AuditLog:
    """Append-only, UTF-8 JSON Lines audit store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._guard_path = self.path.with_name(self.path.name + ".lock")

    def _acquire_guard(self) -> int:
        """Blocking cross-process guard lock; returns an open fd."""
        guard = self._guard_path
        ensure_guard_file(guard)
        fd = os.open(guard, os.O_RDWR, 0o644)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _release_guard(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def append(self, event: Mapping[str, Any]) -> str:
        """Append one event; returns the system-generated ``event_id``.

        Raises ``ValueError`` if the event carries the reserved
        ``event_id`` key. The record is written once, flushed and
        fsynced before returning.
        """
        if any(key in _RESERVED_KEYS for key in event):
            raise ValueError("event_id is reserved and cannot be supplied by callers")
        event_id = str(uuid.uuid4())
        record: dict[str, Any] = {"event_id": event_id, **dict(event)}
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        payload = line.encode("utf-8")
        guard_fd = self._acquire_guard()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            self._release_guard(guard_fd)
        return event_id

    def read(self) -> list[dict[str, Any]]:
        """Read all events in append order (no guard needed)."""
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    events.append(json.loads(stripped))
        return events


__all__ = ["AuditLog"]
