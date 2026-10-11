"""Raw file registry, source ledger, transformation log and standard
Parquet store.

The source ledger and transformation log follow the field contracts of
the data contract V0.3 (§1.2 raw file registration, §4 source ledger,
§24 transformation log). The standard store writes Parquet and keeps
``record_id``/timestamp values stable across export/import round trips.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from capacity_credit.data.io import RawFileInfo, inspect_raw_file

_SOURCE_LEDGER_COLUMNS = [
    "source_id",
    "source_title",
    "provider",
    "source_type",
    "publication_date",
    "retrieved_at",
    "effective_status",
    "url_or_reference",
    "definition_note",
    "license_or_access",
    "confidentiality",
    "checksum",
    "review_status",
    "reviewer",
    "notes",
]

_TRANSFORMATION_COLUMNS = [
    "transformation_id",
    "input_file_hash",
    "input_table",
    "output_table",
    "rule_id",
    "rule_version",
    "field_name",
    "original_value",
    "normalized_value",
    "row_locator",
    "severity",
    "automatic",
    "approved_by",
    "created_at",
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RawFileRecord:
    path: str
    name: str
    size_bytes: int
    sha256: str
    encoding: str
    format: str
    registered_at: str


class RawFileRegistry:
    """Read-only raw file registration with SHA-256."""

    def __init__(self) -> None:
        self.records: list[RawFileRecord] = []

    def register(self, path: Path | str) -> RawFileRecord:
        info: RawFileInfo = inspect_raw_file(path)
        record = RawFileRecord(
            path=info.path,
            name=info.name,
            size_bytes=info.size_bytes,
            sha256=info.sha256,
            encoding=info.encoding,
            format=info.format,
            registered_at=_now_iso(),
        )
        self.records.append(record)
        return record


class SourceLedger:
    """Source ledger (data contract V0.3 §4)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, record: dict[str, Any]) -> None:
        clean = {key: record.get(key) for key in _SOURCE_LEDGER_COLUMNS}
        if not clean.get("source_id"):
            clean["source_id"] = f"SRC_{uuid.uuid4().hex[:12]}"
        if not clean.get("retrieved_at"):
            clean["retrieved_at"] = _now_iso()
        self.rows.append(clean)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=_SOURCE_LEDGER_COLUMNS)


class TransformationLog:
    """Append-only transformation log (data contract V0.3 §24)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, record: dict[str, Any]) -> None:
        clean = {key: record.get(key) for key in _TRANSFORMATION_COLUMNS}
        if not clean.get("transformation_id"):
            clean["transformation_id"] = f"TR_{uuid.uuid4().hex[:12]}"
        if not clean.get("created_at"):
            clean["created_at"] = _now_iso()
        if clean.get("automatic") is None:
            clean["automatic"] = True
        self.rows.append(clean)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=_TRANSFORMATION_COLUMNS)


class StandardTableStore:
    """Parquet store for standard tables (atomic publish)."""

    def write_table(
        self,
        directory: Path | str,
        name: str,
        frame: pd.DataFrame,
        *,
        immutable: bool = False,
    ) -> Path:
        """Serialize fully in memory, then atomically publish to Parquet.

        The final path is never written directly: the complete frame is
        serialized, written to a same-directory temp file and atomically
        published (reusing T03 atomic-write semantics). ``immutable``
        rejects existing targets. Table names are validated against path
        traversal.

        Concurrent publishers of identical content converge
        idempotently: a per-table cross-process lock serializes the
        publish, and an already-identical target is reused instead of
        being replaced (no blind retries, no concurrent ``os.replace``
        on the same target).
        """
        import time

        from capacity_credit.application.atomic_io import (
            ObjectExistsError,
            atomic_write_bytes,
            safe_filename_component,
        )
        from capacity_credit.application.locks import RunLock

        safe_name = safe_filename_component(name)
        target = Path(directory) / f"{safe_name}.parquet"
        payload = frame.to_parquet(index=False)
        if immutable and target.exists():
            raise ObjectExistsError(str(target))
        lock = RunLock(Path(directory) / ".locks", f"tbl_{safe_name}")
        deadline = time.monotonic() + 10.0
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise PermissionError(f"publish lock timeout for {safe_name}")
            time.sleep(0.02)
        try:
            if target.exists() and not immutable and target.read_bytes() == payload:
                return target
            atomic_write_bytes(target, payload, immutable=immutable)
            return target
        finally:
            lock.release()

    def read_table(self, directory: Path | str, name: str) -> pd.DataFrame:
        from capacity_credit.application.atomic_io import safe_filename_component

        safe_name = safe_filename_component(name)
        target = Path(directory) / f"{safe_name}.parquet"
        if not target.is_file():
            raise FileNotFoundError(str(target))
        return pd.read_parquet(target)


__all__ = [
    "RawFileRecord",
    "RawFileRegistry",
    "SourceLedger",
    "StandardTableStore",
    "TransformationLog",
]
