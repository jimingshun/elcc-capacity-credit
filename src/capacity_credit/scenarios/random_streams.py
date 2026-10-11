"""Stable per-object random streams (T07).

A stream is derived deterministically from:

- root seed (non-negative integer);
- object stable ID (non-blank UTF-8 business identifier);
- stream purpose / namespace (non-blank).

Identifiers are canonically normalized (strip + NFKC) before UTF-8
encoding and SHA-256; the digest bytes are used as SeedSequence entropy
together with the root seed. Python built-in ``hash()``, input row
numbers, DataFrame indices, worker numbers, wall clock and local
absolute paths are never used, so the same root seed + object id +
namespace yields the same stream across processes and input orderings,
new objects never disturb existing trajectories, and different
namespaces never share a substream.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

import numpy as np
from numpy.random import SeedSequence

from capacity_credit.application.atomic_io import stable_dump

_ENTROPY_WORDS = 4


class StreamError(ValueError):
    """Stable random-stream error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_stream_key(text: str) -> str:
    """Canonical business identifier: strip surrounding whitespace + NFKC."""
    return unicodedata.normalize("NFKC", str(text).strip())


def _canonical_key_payload(object_id: str, namespace: str) -> bytes:
    """Unambiguous structured encoding (length-prefixed list) of the key.

    Uses ``stable_dump`` (sorted canonical JSON) of the two normalized
    fields, so concatenation collisions like (``a``, ``b\\0c``) vs
    (``a\\0b``, ``c``) are impossible.
    """
    return stable_dump([object_id, namespace])


def _validate_root_seed(root_seed: Any) -> int:
    if isinstance(root_seed, bool) or not isinstance(root_seed, (int, np.integer)):
        raise StreamError(
            "STREAM_INVALID_ROOT_SEED", "root seed must be a non-negative integer"
        )
    seed = int(root_seed)
    if seed < 0:
        raise StreamError(
            "STREAM_INVALID_ROOT_SEED", "root seed must be a non-negative integer"
        )
    return seed


def _validate_identifier(value: Any, field: str) -> str:
    if value is None:
        raise StreamError("STREAM_EMPTY_IDENTIFIER", f"{field} must not be blank")
    normalized = normalize_stream_key(str(value))
    if not normalized:
        raise StreamError("STREAM_EMPTY_IDENTIFIER", f"{field} must not be blank")
    return normalized


def derive_seed(root_seed: int, object_id: str, namespace: str) -> SeedSequence:
    """SeedSequence derived from root seed and canonical identifier hash."""
    seed = _validate_root_seed(root_seed)
    object_key = _validate_identifier(object_id, "object_id")
    namespace_key = _validate_identifier(namespace, "namespace")
    payload = _canonical_key_payload(object_key, namespace_key)
    digest = hashlib.sha256(payload).digest()
    entropy: list[int] = [seed]
    for i in range(_ENTROPY_WORDS):
        chunk = digest[i * 8:(i + 1) * 8]
        entropy.append(int.from_bytes(chunk, byteorder="big"))
    return SeedSequence(entropy)


def make_generator(
    root_seed: int, object_id: str, namespace: str
) -> np.random.Generator:
    """Stable PCG64 generator for one (root_seed, object_id, namespace)."""
    sequence = derive_seed(root_seed, object_id, namespace)
    return np.random.default_rng(sequence)


def partition_sample_ids(
    total_samples: int, n_workers: int, worker: int
) -> tuple[int, ...]:
    """Pure functional round-robin partition of sample ids.

    The SAME total sample-id set is produced for any worker count: the
    union over workers of partition(total, W, w) is exactly range(total)
    for every W, and partition(total, 1, 0) is the whole set. Stream
    identity stays entirely in derive_seed/make_generator (root seed +
    object id + namespace); this function only slices the id set and
    never touches RNG state. Shared by tests and future parallel
    executors - no second partitioning scheme."""
    if (
        isinstance(total_samples, bool)
        or not isinstance(total_samples, (int, np.integer))
        or int(total_samples) < 0
    ):
        raise StreamError(
            "STREAM_INVALID_PARTITION", "total_samples must be a non-negative integer"
        )
    if (
        isinstance(n_workers, bool)
        or not isinstance(n_workers, (int, np.integer))
        or int(n_workers) <= 0
    ):
        raise StreamError("STREAM_INVALID_PARTITION", "n_workers must be positive")
    if (
        isinstance(worker, bool)
        or not isinstance(worker, (int, np.integer))
        or not 0 <= int(worker) < int(n_workers)
    ):
        raise StreamError("STREAM_INVALID_PARTITION", "worker must be in [0, n_workers)")
    total = int(total_samples)
    workers = int(n_workers)
    index = int(worker)
    return tuple(i for i in range(total) if i % workers == index)


def object_stream_fingerprint(
    root_seed: int, object_id: str, namespace: str
) -> str:
    """Stable sha256 hex fingerprint of the stream identity (testing)."""
    sequence = derive_seed(root_seed, object_id, namespace)
    state = sequence.generate_state(2)
    return hashlib.sha256(
        state.astype(">u8").tobytes() + b"|" + str(root_seed).encode("ascii")
    ).hexdigest()


__all__ = [
    "StreamError",
    "derive_seed",
    "make_generator",
    "normalize_stream_key",
    "object_stream_fingerprint",
    "partition_sample_ids",
]
