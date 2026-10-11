"""Cross-process run locks.

A ``RunLock`` guards one ``run_id``: only one process may hold it at a
time (application service contract V0.2 §5.1). Two files are used per
run:

- ``<run_id>.lock``: the guard file carrying the Windows byte-range
  lock (``msvcrt.locking``). The OS releases it automatically when the
  owning process exits, so a failed acquire means a live process holds
  the lock; stale locks are therefore never auto-preempted.
- ``<run_id>.lock.json``: readable metadata (``run_id``, PID, host,
  ``acquired_at``, ``heartbeat_at``, ``lock_version``). Because the
  Windows byte-range lock blocks independent reads of the guarded
  region, the metadata lives in a separate unguarded file so monitors
  can always read it.

``acquired_at`` is fixed at first acquisition; ``heartbeat()`` updates
only ``heartbeat_at``. Repeated ``acquire()`` on the same object is
idempotent (returns True without opening new handles). ``run_id`` must
pass ``safe_filename_component`` validation.
"""

from __future__ import annotations

import json
import msvcrt
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import (
    ensure_guard_file,
    safe_filename_component,
    stable_dump,
)

LOCK_VERSION = "1"

_METADATA_KEYS = ("run_id", "pid", "host", "acquired_at", "heartbeat_at", "lock_version")


class RunLock:
    """Non-blocking cross-process lock keyed by ``run_id``."""

    def __init__(self, locks_dir: Path | str, run_id: str) -> None:
        safe_run_id = safe_filename_component(run_id)
        self.locks_dir = Path(locks_dir)
        self.run_id = safe_run_id
        self.path = self.locks_dir / f"{safe_run_id}.lock"
        self.metadata_path = self.locks_dir / f"{safe_run_id}.lock.json"
        self._fd: int | None = None
        self._locked = False

    def _write_metadata(self, acquired_at: str | None) -> None:
        now = datetime.now(UTC).isoformat()
        if acquired_at is None:
            current = self.info()
            if current is not None and isinstance(current.get("acquired_at"), str):
                acquired_at = current["acquired_at"]
            else:
                acquired_at = now
        info = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": acquired_at,
            "heartbeat_at": now,
            "lock_version": LOCK_VERSION,
        }
        payload = stable_dump(info)
        fd = os.open(self.metadata_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def acquire(self) -> bool:
        """Try to acquire the lock; idempotent for the same object."""
        if self._locked and self._fd is not None:
            return True
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        ensure_guard_file(self.path)
        fd = os.open(self.path, os.O_RDWR, 0o644)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                # A live process holds the byte-range lock; do not
                # preempt locks that cannot be confirmed stale.
                os.close(fd)
                return False
            now = datetime.now(UTC).isoformat()
            self._write_metadata(now)
            self._fd = fd
            self._locked = True
            return True
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        """Release the lock if held; never leaks handles."""
        if self._fd is None:
            return
        fd = self._fd
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
            self._fd = None
            self._locked = False

    def heartbeat(self) -> None:
        """Refresh only ``heartbeat_at`` in the metadata (holder only)."""
        if self._fd is None or not self._locked:
            raise RuntimeError("heartbeat requires an acquired run lock")
        current = self.info()
        acquired_at = current["acquired_at"] if current else None
        self._write_metadata(acquired_at)

    def is_held(self) -> bool:
        """Probe whether the lock is currently held.

        Our own acquisition state is authoritative first; otherwise the
        Windows byte-range guard is probed - a live process holding it
        makes the probe lock fail.
        """
        if self._locked and self._fd is not None:
            return True
        if not self.path.exists():
            return False
        try:
            fd = os.open(self.path, os.O_RDWR, 0o644)
        except OSError:
            return True
        try:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        finally:
            os.close(fd)

    def info(self) -> dict[str, Any] | None:
        """Read the readable metadata file (always available)."""
        if not self.metadata_path.exists():
            return None
        try:
            data = self.metadata_path.read_bytes()
        except OSError:
            return None
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if not all(key in parsed for key in _METADATA_KEYS):
            return None
        return parsed


__all__ = ["LOCK_VERSION", "RunLock"]
