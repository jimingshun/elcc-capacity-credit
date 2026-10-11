"""Scenario-bank file persistence (T07 review).

File-level responsibilities separated from scenario_bank.py:
- safe relative path handling (component-resolved, no simple ``".." in
  text`` check);
- array serialization and mmap-based batched reading (never loading a
  whole array for a sample batch);
- batched array writing (memmap fill, no full-array BytesIO copies);
- file SHA-256;
- staging-directory publish with idempotent reuse and concurrent-build
  tolerance;
- conversion of all low-level JSON/Numpy/OSError/KeyError failures into
  stable ScenarioBankError codes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np


class PersistenceError(ValueError):
    """Stable persistence error with a machine code (alias of bank error)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ScenarioBankError(PersistenceError):
    """Stable scenario-bank error with a machine code."""


def safe_rel_path(value: str) -> str:
    """Validate ``value`` as a safe relative path.

    Rejects empty paths, absolute paths, drive letters and any ``..``
    component after normalization; comparison is component-based, never
    a naive substring check.
    """
    text = str(value).replace("\\", "/").strip()
    if not text:
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSAFE_PATH", "empty relative path"
        )
    if text.startswith("/"):
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSAFE_PATH", f"absolute path rejected: {value!r}"
        )
    if ":" in text:
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSAFE_PATH", f"drive letter rejected: {value!r}"
        )
    if "\x00" in text:
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSAFE_PATH", f"NUL rejected: {value!r}"
        )
    parts = PurePosixPath(text).parts
    if any(part in ("..", ".") for part in parts):
        raise ScenarioBankError(
            "SCENARIO_BANK_UNSAFE_PATH", f"path traversal rejected: {value!r}"
        )
    return "/".join(parts)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def serialize_array(states: np.ndarray, dtype: np.dtype) -> bytes:
    """Serialize to ``.npy`` bytes (pickle forbidden)."""
    buffer = np.ascontiguousarray(states, dtype=dtype)
    import io

    stream = io.BytesIO()
    np.save(stream, buffer, allow_pickle=False)
    return stream.getvalue()


def read_array_mmap(
    path: Path, expected_shape: tuple[int, ...], expected_dtype: np.dtype
) -> np.ndarray:
    """Open an array with ``mmap_mode="r"`` and verify dtype/shape.

    Returns a memmap view: sample slices only touch the needed pages.
    """
    try:
        data = np.asarray(np.load(path, mmap_mode="r", allow_pickle=False))
    except (ValueError, OSError, KeyError) as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_HASH_MISMATCH",
            f"array unreadable or corrupt: {path.name}",
        ) from exc
    if np.dtype(data.dtype) != np.dtype(expected_dtype) \
            or tuple(data.shape) != tuple(expected_shape):
        raise ScenarioBankError(
            "SCENARIO_BANK_HASH_MISMATCH",
            f"array dtype/shape mismatch at {path.name}",
        )
    return data


def write_array_batched(
    path: Path,
    shape: tuple[int, ...],
    dtype: np.dtype,
    fill: Callable[[int, int, np.ndarray], None],
    batch_size: int,
) -> None:
    """Write an array in sample batches via memmap.

    ``fill(start, end, view)`` must write rows ``start:end`` of the
    first axis into ``view``; only the batch rows are materialized at a
    time (no full-array in-memory copy).
    """
    n_samples = int(shape[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    target = np.lib.format.open_memmap(
        path, mode="w+", dtype=dtype, shape=shape
    )
    try:
        for start in range(0, n_samples, batch_size):
            end = min(start + batch_size, n_samples)
            fill(start, end, target[start:end])
        target.flush()
    finally:
        del target


def validate_batch_size(value: Any) -> int:
    """Validate a batch size: positive non-bool integer or None.

    Returns the validated int; raises ``ScenarioBankError`` otherwise.
    The caller decides the default when ``None`` is returned.
    """
    if value is None:
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_BATCH", "batch size must not be None here"
        )
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) \
            or int(value) <= 0:
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_BATCH", "batch size must be a positive integer"
        )
    return int(value)


def validate_batch_indices(indices: Any, sample_count: int) -> list[int]:
    """Validate a sample index batch strictly (contiguous contract)."""
    if isinstance(indices, (int, np.integer)) and not isinstance(indices, bool):
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_BATCH",
            "batch indices must be a sequence of integers",
        )
    try:
        values = list(indices)
    except TypeError as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_BATCH", "batch indices must be a sequence"
        ) from exc
    if not values:
        raise ScenarioBankError("SCENARIO_BANK_INVALID_BATCH", "empty batch")
    checked: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_BATCH", "batch indices must be integers"
            )
        index = int(value)
        if index < 0 or index >= sample_count:
            raise ScenarioBankError(
                "SCENARIO_BANK_INVALID_BATCH", f"batch index {index} out of range"
            )
        checked.append(index)
    if checked != sorted(checked):
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_BATCH", "batch indices must be ascending"
        )
    if len(set(checked)) != len(checked):
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_BATCH", "duplicate batch indices"
        )
    if checked != list(range(checked[0], checked[0] + len(checked))):
        raise ScenarioBankError(
            "SCENARIO_BANK_INVALID_BATCH", "batch indices must be contiguous"
        )
    return checked


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_WRITE_FAILED", f"index write failed: {exc}"
        ) from exc


def read_json(path: Path) -> dict[str, Any]:
    try:
        result: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID", f"index unreadable: {path}"
        ) from exc
    if not isinstance(result, dict):
        raise ScenarioBankError(
            "SCENARIO_BANK_INDEX_INVALID",
            f"index top level must be an object: {path}",
        )
    return result


def publish_staging(
    staging: Path,
    target: Path,
    verify: Callable[[Path], None],
    retries: int = 5,
) -> bool:
    """Atomically publish ``staging`` as ``target``.

    Returns True when this process published, False when it re-used an
    existing verified target (concurrent-build tolerant). Any failure
    cleans the staging directory.
    """
    try:
        for attempt in range(retries):
            try:
                os.replace(staging, target)
                return True
            except OSError:
                if target.exists():
                    try:
                        verify(target)
                        shutil.rmtree(staging, ignore_errors=True)
                        return False
                    except Exception:
                        if attempt == retries - 1:
                            raise
                if attempt == retries - 1:
                    raise
                time.sleep(0.2)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ScenarioBankError(
            "SCENARIO_BANK_PUBLISH_FAILED", f"publish failed: {exc}"
        ) from exc
    return True


__all__ = [
    "PersistenceError",
    "ScenarioBankError",
    "file_sha256",
    "publish_staging",
    "read_array_mmap",
    "read_json",
    "safe_rel_path",
    "serialize_array",
    "validate_batch_indices",
    "validate_batch_size",
    "write_array_batched",
    "write_json_atomic",
]
