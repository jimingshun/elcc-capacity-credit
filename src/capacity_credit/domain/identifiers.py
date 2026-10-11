"""Single strict object-id canonicalization (T10, review rounds 2-3).

Used by both the portfolio views (``domain.portfolios``) and the paired
evaluation (``solvers.paired_evaluation``) so that canonicalization
collisions are detected identically everywhere instead of being silently
merged by frozensets.

Input contract of ``canonical_object_id_set``:

- the collection must be iterable: a single ``str``/``bytes`` is NOT an
  iterable collection of ids (it would be split into characters), and
  int / float / bool / None / other non-iterables are rejected with a
  stable ``IdentifierError`` (never a raw ``TypeError``);
- every element must be a strict string (numbers / bytes / bool /
  None rejected);
- empty collections are legal (shared/candidate object sets are
  business-wise allowed to be empty);
- elements are stripped, NFKC-normalized; two different raw ids that
  normalize to the same id block with ``IDENTIFIER_COLLISION``.
"""

from __future__ import annotations

import unicodedata
from typing import Any


class IdentifierError(ValueError):
    """Stable identifier error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_object_id(value: Any, field_name: str = "object_id") -> str:
    """Strict object-id canonicalization (string input, NFKC)."""
    if value is None or isinstance(value, bool) or not isinstance(value, str):
        raise IdentifierError(
            "IDENTIFIER_NOT_STRING",
            f"{field_name} must be a non-blank string",
        )
    normalized = unicodedata.normalize("NFKC", value.strip())
    if not normalized:
        raise IdentifierError(
            "IDENTIFIER_BLANK", f"{field_name} must not be blank",
        )
    return normalized


def canonical_object_id_set(
    values: Any, field_name: str = "object_id",
) -> frozenset[str]:
    """Canonicalize an object-id collection and BLOCK collisions.

    ``values`` must be an iterable of strict strings; a bare string is
    rejected (never split into characters); empty collections are legal.
    """
    if values is None or isinstance(values, (str, bytes, bool)):
        raise IdentifierError(
            "IDENTIFIER_NOT_ITERABLE",
            f"{field_name} must be an iterable collection of ids "
            "(a single id string or a non-iterable is rejected)",
        )
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise IdentifierError(
            "IDENTIFIER_NOT_ITERABLE",
            f"{field_name} must be an iterable collection of ids",
        ) from exc
    normalized: list[str] = []
    for value in iterator:
        normalized.append(canonical_object_id(value, field_name))
    if len(set(normalized)) != len(normalized):
        raise IdentifierError(
            "IDENTIFIER_COLLISION",
            f"{field_name} contains ids that collide after canonicalization",
        )
    return frozenset(normalized)


__all__ = [
    "IdentifierError",
    "canonical_object_id",
    "canonical_object_id_set",
]
