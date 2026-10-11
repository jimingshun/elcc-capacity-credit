"""Data ingestion primitives: raw file inspection (read-only)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


class ZipPathTraversalError(ValueError):
    """Raised when a ZIP member escapes the archive root."""


@dataclass(frozen=True)
class RawFileInfo:
    path: str
    name: str
    size_bytes: int
    sha256: str
    encoding: str
    format: str
    line_count: int
    first_line: str
    sheet_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ZipMemberInfo:
    name: str
    size: int
    crc32: str
    is_dir: bool
    encoding: str
    line_count: int


@dataclass(frozen=True)
class ZipArchiveInfo:
    path: str
    size_bytes: int
    sha256: str
    members: list[ZipMemberInfo]


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_encoding(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "unknown"


def _parse_text_file(path: Path, raw: bytes, encoding: str) -> tuple[int, str]:
    text = raw.decode(encoding, errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines), lines[0][:300] if lines else ""


def inspect_raw_file(path: Path | str) -> RawFileInfo:
    """Inspect a raw file read-only; records size, SHA-256, encoding."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    raw = target.read_bytes()
    encoding = detect_encoding(raw)
    suffix = target.suffix.lower()
    if suffix in (".csv", ".txt"):
        fmt = "csv"
    elif suffix in (".xlsx", ".xls"):
        fmt = "excel"
    elif suffix == ".zip":
        fmt = "zip"
    else:
        fmt = suffix.lstrip(".") or "unknown"
    line_count, first_line = 0, ""
    if fmt in ("csv", "txt") and encoding != "unknown":
        line_count, first_line = _parse_text_file(target, raw, encoding)
    sheet_names: list[str] = []
    if fmt == "excel":
        try:
            sheets = pd.read_excel(target, sheet_name=None, nrows=1)
            sheet_names = list(sheets)
        except (ValueError, OSError, ImportError):
            sheet_names = []
    return RawFileInfo(
        path=str(target),
        name=target.name,
        size_bytes=len(raw),
        sha256=sha256_file(target),
        encoding=encoding,
        format=fmt,
        line_count=line_count,
        first_line=first_line,
        sheet_names=sheet_names,
    )


def _member_escapes_root(name: str) -> bool:
    normalized = Path(name.replace("\\", "/")).as_posix()
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        return True
    if ":" in normalized.split("/")[0]:
        return True
    return normalized.startswith("/")


def inspect_zip_archive(path: Path | str) -> ZipArchiveInfo:
    """Inspect a ZIP read-only; rejects path-traversal members."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    members: list[ZipMemberInfo] = []
    with zipfile.ZipFile(target) as archive:
        for info in archive.infolist():
            if _member_escapes_root(info.filename):
                raise ZipPathTraversalError(
                    f"zip member escapes archive root: {info.filename!r}"
                )
            encoding: str = "n/a"
            line_count = 0
            if not info.is_dir() and info.filename.lower().endswith(".csv"):
                raw = archive.read(info)
                encoding = detect_encoding(raw)
                if encoding != "unknown":
                    text = raw.decode(encoding, errors="replace")
                    line_count = len([l for l in text.splitlines() if l.strip()])
            members.append(
                ZipMemberInfo(
                    name=info.filename,
                    size=info.file_size,
                    crc32=f"{info.CRC:08x}",
                    is_dir=info.is_dir(),
                    encoding=encoding,
                    line_count=line_count,
                )
            )
    return ZipArchiveInfo(
        path=str(target),
        size_bytes=target.stat().st_size,
        sha256=sha256_file(target),
        members=members,
    )


def read_csv(path: Path | str, encoding: str | None = None) -> pd.DataFrame:
    """Read a CSV read-only; detects encoding when not given."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    raw = target.read_bytes()
    if encoding is None:
        encoding = detect_encoding(raw)
        if encoding == "unknown":
            raise ValueError("DQ02_ENCODING_UNKNOWN: cannot decode file")
    buffer = io.BytesIO(raw)
    frame = pd.read_csv(buffer, encoding=encoding)
    frame = frame.loc[:, ~frame.columns.astype(str).str.startswith("Unnamed")]
    return frame


def read_excel_sheets(path: Path | str) -> dict[str, pd.DataFrame]:
    """Read all sheets of an Excel workbook read-only."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    return pd.read_excel(target, sheet_name=None)


__all__ = [
    "RawFileInfo",
    "ZipArchiveInfo",
    "ZipMemberInfo",
    "ZipPathTraversalError",
    "detect_encoding",
    "inspect_raw_file",
    "inspect_zip_archive",
    "read_csv",
    "read_excel_sheets",
    "sha256_file",
]
