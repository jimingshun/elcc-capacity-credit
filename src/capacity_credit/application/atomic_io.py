"""Atomic persistence and canonical hashing.

Canonical UTF-8 serialization (JSON, sorted keys) feeds SHA-256 hashes.
``hash_payload`` applies an explicit business-field projection: volatile
metadata keys (``created_at``/``generated_at``) are excluded, ``Path``
objects and absolute-path strings are rejected, and all other business
fields are preserved recursively - so identical business content hashes
identically across directories and generation times.

Atomic writes follow: same-directory temp file - flush - fsync - atomic
publish. Temp names start with ``.``, end with ``.tmp`` and contain a
UUID, so they can never be confused with formal objects.

- Immutable targets use an atomic create-if-not-exists publish
  (``os.link`` of the fully written, fsynced temp file to the target;
  fails atomically if the target exists). This avoids the TOCTOU race
  of ``if not exists: os.replace(...)`` and never exposes an empty
  target file.
- Only mutable targets use ``os.replace`` (overwrite).
- Exception paths remove the temp file created by *this* call; leftover
  temp files from a truly crashed process are historical residue for a
  later cleanup mechanism, never treated as ordinary exceptions.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Volatile metadata keys that must not silently enter business hashes.
EXCLUDED_METADATA_KEYS = frozenset({"generated_at", "created_at"})

_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]|^[\\/]")


class ObjectExistsError(FileExistsError):
    """Raised when an immutable target already exists."""


class GuardFileError(RuntimeError):
    """Raised when a guard file exists but cannot be safely reused
    (e.g. a legacy zero-length file that may be in use elsewhere)."""


class CorruptClaimError(RuntimeError):
    """Raised when an existing claim file is NOT a valid claim (old
    format without a claim_token, damaged, zero-byte or partial) - such
    a file must never be interpreted as a successful claim."""


def ensure_guard_file(path: Path | str) -> None:
    """Race-free first initialization of a byte-range-lock guard file.

    Semantics:

    - missing: the 1-byte content is fully written to a same-directory
      temp file (flush + fsync) and published via atomic
      create-if-not-exists, so no zero-length intermediate is ever
      exposed and concurrent initializers see a complete file;
    - exists with size >= 1: reused as-is (never overwritten, never
      deleted);
    - exists with size == 0 (legacy): cannot confirm it is unused;
      raise ``GuardFileError`` instead of silently preempting.
    """
    target = Path(path)
    if target.exists():
        if target.stat().st_size == 0:
            raise GuardFileError(
                f"guard file exists but is empty: {target}; "
                "refusing to overwrite a possibly in-use file"
            )
        return
    try:
        atomic_write_bytes(target, b"\x00", immutable=True)
    except ObjectExistsError:
        pass
    if target.stat().st_size == 0:
        raise GuardFileError(
            f"guard file exists but is empty: {target}; "
            "refusing to overwrite a possibly in-use file"
        )


_TRANSIENT_IO_WINERRORS = frozenset({32, 33})  # SHARING_VIOLATION / LOCK_VIOLATION


def is_transient_io_conflict(exc: OSError) -> bool:
    """THE single transient-IO-conflict classifier (shared by the
    control-request read path and the terminal-intent write path): a
    conflict is transient only for a Windows sharing/lock violation
    (winerror 32/33) or the POSIX transient busy/permission errno
    (EACCES/EBUSY/EPERM) without a Windows winerror; a real access denial
    (e.g. winerror 5) is never transient."""
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_IO_WINERRORS
    return exc.errno in (errno.EACCES, errno.EBUSY, errno.EPERM)


def safe_filename_component(name: str) -> str:
    """Validate a single path component (run_id / request_id / keys).

    Rejects empty names, ``.``/``..``, path separators, drive colons and
    NUL so that claims and lock files cannot escape their directory.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("invalid file component: empty")
    if (
        name in (".", "..")
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\x00" in name
    ):
        raise ValueError(f"invalid file component: {name!r}")
    return name


def stable_dump(payload: object) -> bytes:
    """Canonical UTF-8 JSON bytes with sorted keys and trailing newline."""
    text = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


def _canonicalize(value: Any) -> Any:
    """Recursive business-field projection for hashing.

    - excludes volatile metadata keys (``created_at``/``generated_at``);
    - rejects ``Path`` objects and absolute-path strings;
    - preserves every other business field, including nested ones.
    """
    if isinstance(value, Path):
        raise TypeError(
            "local absolute paths (Path objects) are not allowed in business hashes"
        )
    if isinstance(value, dict):
        return {
            key: _canonicalize(item)
            for key, item in value.items()
            if key not in EXCLUDED_METADATA_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, str):
        if _ABSOLUTE_PATH_RE.match(value):
            raise ValueError(
                "local absolute paths are not allowed in business hashes"
            )
        return value
    return value


def hash_payload(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical business-field projection of ``payload``."""
    return hashlib.sha256(stable_dump(_canonicalize(dict(payload)))).hexdigest()


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def atomic_write_bytes(
    path: Path | str, data: bytes, *, immutable: bool = False
) -> None:
    """Write ``data`` atomically to ``path``.

    With ``immutable=True`` an existing target is rejected atomically
    (create-if-not-exists publish, no overwrite, no empty target ever
    exposed). Temp files created by this call are removed on any
    exception path.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _unlink_quiet(tmp)
        raise
    if immutable:
        try:
            # atomic create-if-not-exists on the same volume
            os.link(tmp, target)
        except FileExistsError:
            _unlink_quiet(tmp)
            raise ObjectExistsError(str(target)) from None
        except BaseException:
            _unlink_quiet(tmp)
            raise
        _unlink_quiet(tmp)
    else:
        try:
            os.replace(tmp, target)
        except BaseException:
            _unlink_quiet(tmp)
            raise


def atomic_write_object(
    path: Path | str,
    payload: Mapping[str, Any],
    *,
    immutable: bool = False,
) -> None:
    """Canonically serialize ``payload`` and write it atomically."""
    atomic_write_bytes(path, stable_dump(payload), immutable=immutable)


def _claim_payload_is_valid(payload: object, safe_key: str) -> bool:
    """A valid claim payload: the exact request key AND a 32-hex
    claim_token - an old-format, damaged or partial claim never
    qualifies."""
    if not isinstance(payload, dict) or payload.get("request_id") != safe_key:
        return False
    token = payload.get("claim_token")
    return (
        isinstance(token, str)
        and len(token) == 32
        and all(char in "0123456789abcdef" for char in token)
    )


def _require_valid_existing_claim(claim: Path, safe_key: str) -> None:
    """Fail closed on an existing claim file that is not a valid claim:
    a zero-byte, partial, damaged or old-format file is NEVER a
    successful claim."""
    try:
        payload = json.loads(claim.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise CorruptClaimError(
            f"existing claim file is not a valid claim: {claim} ({exc})"
        ) from exc
    if not _claim_payload_is_valid(payload, safe_key):
        raise CorruptClaimError(
            f"existing claim file has an unrecognized format: {claim}"
        )


def _write_claim_fd(directory: Path | str, key: str) -> tuple[int, Path, str] | None:
    """Create the claim file O_EXCL and return (fd, path, token) or None
    on a duplicate - the single seam for atomic_claim and
    atomic_claim_with_token. The payload is written in a completion
    loop (short writes retried, the actual byte count verified); ANY
    write failure removes THIS call's just-created file so no empty or
    partial claim can masquerade as a successful one."""
    safe_key = safe_filename_component(key)
    claims_dir = Path(directory)
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim = claims_dir / f"{safe_key}.claim"
    token = uuid.uuid4().hex
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        _require_valid_existing_claim(claim, safe_key)
        return None
    try:
        payload = stable_dump({"request_id": safe_key, "claim_token": token})
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError("claim payload write made no progress")
            written += count
        if written != len(payload):
            raise OSError("claim payload write is incomplete")
    except BaseException as exc:
        os.close(fd)
        try:
            os.remove(claim)  # remove THIS call's just-created file
        except OSError as remove_exc:
            exc.add_note(f"partial claim residue could not be removed: {claim} ({remove_exc})")
        raise
    return fd, claim, token


def atomic_claim(directory: Path | str, key: str) -> Path | None:
    """Idempotently claim ``key`` via an exclusive marker file.

    Returns the claim file on first claim, ``None`` on duplicates. The
    O_EXCL creation is atomic across processes, so a duplicate
    ``request_id`` can never produce a second side effect. ``key`` is
    validated as a safe file component. The claim payload carries a
    unique ``claim_token`` (per-creation ownership evidence) since the
    T26-A launch-rollback contract; the return shape is unchanged."""
    opened = _write_claim_fd(directory, key)
    if opened is None:
        return None
    fd, claim, _token = opened
    os.close(fd)
    return claim


def atomic_claim_with_token(directory: Path | str, key: str) -> tuple[Path, str] | None:
    """atomic_claim PLUS the in-memory ownership token of the claim
    created by THIS call (never re-read from disk - a same-format
    replaced claim can never be mistaken for ours). Returns
    ``(claim_path, claim_token)`` on first claim, ``None`` on
    duplicates."""
    opened = _write_claim_fd(directory, key)
    if opened is None:
        return None
    fd, claim, token = opened
    os.close(fd)
    return claim, token


__all__ = [
    "EXCLUDED_METADATA_KEYS",
    "GuardFileError",
    "ObjectExistsError",
    "atomic_claim",
    "atomic_claim_with_token",
    "atomic_write_bytes",
    "atomic_write_object",
    "ensure_guard_file",
    "hash_payload",
    "is_transient_io_conflict",
    "safe_filename_component",
    "stable_dump",
]
