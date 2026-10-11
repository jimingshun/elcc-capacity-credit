"""T24-A: export coordination entry (THE single write entry).

One validation -> one shared ReportView -> Excel -> HTML ->
report_export_manifest.json published LAST. Outputs are published
atomically (immutable semantics, pre-check identical/different);
a complete same-identity report set is replayed without rewriting;
a different-identity or modified existing report is stably blocked.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import ObjectExistsError, atomic_write_bytes
from capacity_credit.reporting import excel as _excel_mod
from capacity_credit.reporting import html as _html_mod
from capacity_credit.reporting.report_model import (
    REPORT_EXPORT_SCHEMA_VERSION,
    REPORT_GENERATOR_VERSION,
    ReportExportError,
    ReportView,
    load_and_validate,
)

OUTPUT_FILES: tuple[str, ...] = (
    "report.xlsx",
    "report.html",
    "report_export_manifest.json",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _preflight_existing(path: Path, data: bytes, label: str) -> None:
    if path.exists():
        if path.read_bytes() == data:
            return
        raise ReportExportError(
            "REPORT_EXISTS_DIFFERENT",
            f"{label} exists with different content: {path}",
        )


def _publish_immutable(path: Path, data: bytes, label: str) -> None:
    try:
        atomic_write_bytes(path, data, immutable=True)
    except ObjectExistsError:
        existing = path.read_bytes()
        if existing == data:
            return
        raise ReportExportError(
            "REPORT_EXISTS_DIFFERENT",
            f"{label} exists with different content: {path}",
        ) from None
    except OSError as exc:
        raise ReportExportError(
            "REPORT_WRITE_FAILED", f"{label} write failed: {path}: {exc}"
        ) from exc


def _validate_existing_set(
    previous: dict[str, Any],
    view: ReportView,
    report_id: str,
    output: Path,
) -> None:
    """Strict replay validation: any mismatch (identity, status,
    diagnostic flag, versions, input artifact names/hashes/sizes, actual
    output hashes/sizes or a missing output file) is a stable error -
    never a replayed result."""
    mismatches: list[str] = []
    if previous.get("report_id") != report_id:
        raise ReportExportError(
            "REPORT_SOURCE_IDENTITY_MISMATCH",
            "an existing report belongs to a different source identity; "
            "refusing to overwrite",
        )
    if previous.get("source_run_id") != view.run_id or previous.get(
        "source_result_id"
    ) != view.result_id:
        raise ReportExportError(
            "REPORT_SOURCE_IDENTITY_MISMATCH",
            "an existing report belongs to a different source identity; "
            "refusing to overwrite",
        )
    if previous.get("schema_version") != REPORT_EXPORT_SCHEMA_VERSION:
        mismatches.append("schema_version")
    if previous.get("result_status") != view.result_status:
        mismatches.append("result_status")
    if previous.get("is_diagnostic") != view.is_diagnostic:
        mismatches.append("is_diagnostic")
    if previous.get("report_mapping_version") != view.report_mapping_version:
        mismatches.append("report_mapping_version")
    if previous.get("report_generator_version") != REPORT_GENERATOR_VERSION:
        mismatches.append("report_generator_version")
    if previous.get("business_notes_sha256") != (
        _sha256(json.dumps(view.business_notes, ensure_ascii=False, sort_keys=True,
                           default=str).encode("utf-8"))
        if view.business_notes is not None
        else None
    ):
        mismatches.append("business_notes")
    # input artifacts: names + hashes + sizes must equal the CURRENT files
    recorded_inputs = {
        item.get("name"): item for item in previous.get("input_artifacts") or []
    }
    for name, record in view.source_files.items():
        recorded = recorded_inputs.get(name)
        if recorded is None:
            mismatches.append(f"input:{name}")
            continue
        if recorded.get("sha256") != record.sha256 or recorded.get("size") != record.size:
            mismatches.append(f"input:{name}:hash")
    # outputs: every file must exist with the recorded hash and size
    for output_name in OUTPUT_FILES:
        path = output / output_name
        if not path.is_file():
            mismatches.append(f"output:{output_name}:missing")
            continue
        if output_name == "report_export_manifest.json":
            continue
        data = path.read_bytes()
        recorded = next(
            (item for item in previous.get("outputs") or [] if item.get("name") == output_name),
            None,
        )
        if recorded is None or recorded.get("sha256") != _sha256(data) or recorded.get("size") != len(data):
            mismatches.append(f"output:{output_name}:hash")
    if mismatches:
        raise ReportExportError(
            "REPORT_EXISTS_DIFFERENT",
            "existing report set no longer matches the current source: "
            + ", ".join(sorted(set(mismatches))),
        )


def _check_notes_legal(notes: dict[str, Any]) -> None:
    """A bounded shape check over the business notes (C-night/F): the
    schema lives in reporting.business_notes; anything else (or
    non-finite numbers sneaking into derived values) is refused before
    any artifact is built."""
    if notes.get("business_notes_version") != "1":
        raise ReportExportError(
            "REPORT_BUSINESS_NOTES_INVALID",
            "business_notes.business_notes_version must be '1'",
        )
    sections = notes.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ReportExportError(
            "REPORT_BUSINESS_NOTES_INVALID",
            "business_notes.sections must be a non-empty list",
        )
    for section in sections:
        if not isinstance(section, dict) or not isinstance(
            section.get("heading"), str
        ):
            raise ReportExportError(
                "REPORT_BUSINESS_NOTES_INVALID",
                "each business note section needs a heading",
            )
        table = section.get("table")
        table_bad = table is not None and (
            not isinstance(table, dict)
            or not isinstance(table.get("columns"), list)
            or not isinstance(table.get("rows"), list)
        )
        if table_bad:
            raise ReportExportError(
                "REPORT_BUSINESS_NOTES_INVALID",
                "a business note table needs columns and rows",
            )


def _build_export_manifest(
    view: ReportView,
    excel_bytes: bytes,
    html_bytes: bytes,
    report_id: str,
    business_notes_sha256: str | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": REPORT_EXPORT_SCHEMA_VERSION,
        "report_id": report_id,
        "source_run_id": view.run_id,
        "source_result_id": view.result_id,
        "result_status": view.result_status,
        "report_mapping_version": view.report_mapping_version,
        "report_generator_version": REPORT_GENERATOR_VERSION,
        "is_diagnostic": view.is_diagnostic,
        # C-night/F: the business notes' identity is part of the
        # published set - different notes can never replay as the same
        # set (absent notes record null)
        "business_notes_sha256": business_notes_sha256,
        "input_artifacts": [
            {
                "name": record.name,
                "sha256": record.sha256,
                "size": record.size,
            }
            for record in sorted(view.source_files.values(), key=lambda r: r.name)
        ],
        "outputs": [
            {
                "name": "report.xlsx",
                "sha256": _sha256(excel_bytes),
                "size": len(excel_bytes),
            },
            {
                "name": "report.html",
                "sha256": _sha256(html_bytes),
                "size": len(html_bytes),
            },
        ],
        "truncated_tables": view.truncated_tables,
        # the manifest intentionally does NOT contain its own hash
    }
    return manifest


def export_report(
    input_dir: Path | str,
    output_dir: Path | str,
    *,
    business_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """THE single T24-A export entry: validate once, build one shared
    view, render the FULL Excel + HTML set, publish
    report_export_manifest.json last. Single-format exports are NOT
    supported (no zero-byte placeholders).

    ``business_notes`` (C-night/F) is an optional deterministic
    business-reading payload (schema: reporting.business_notes) attached
    to the shared view and rendered as ADDITIONAL sections - the machine
    fields, units and warnings are untouched. Its identity enters the
    manifest, so a set published with different notes is never replayed
    as the same set."""
    inp_resolved = Path(input_dir).resolve()
    out_resolved = Path(output_dir).resolve()
    if out_resolved == inp_resolved or inp_resolved in out_resolved.parents:
        raise ReportExportError(
            "REPORT_OUTPUT_DIR_CONFLICT",
            "output directory must not equal or live inside the input directory",
        )
    view = load_and_validate(input_dir)
    if business_notes is not None:
        if not isinstance(business_notes, dict):
            raise ReportExportError(
                "REPORT_BUSINESS_NOTES_INVALID",
                "business_notes must be a mapping when provided",
            )
        _check_notes_legal(business_notes)
        view.business_notes = dict(business_notes)
    notes_sha256 = (
        _sha256(
            json.dumps(
                view.business_notes,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        )
        if view.business_notes is not None
        else None
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    report_id = f"{view.run_id}/{view.result_id}"

    # replay gate: an existing COMPLETE set is replayed ONLY when EVERY
    # recorded field and hash still matches (identity, status,
    # diagnostic flag, mapping/generator version, all seven input
    # artifacts incl. hash+size, and the actual Excel/HTML bytes)
    existing_manifest_path = output / "report_export_manifest.json"
    if existing_manifest_path.exists():
        try:
            previous = json.loads(existing_manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReportExportError(
                "REPORT_EXISTS_DIFFERENT",
                f"existing report_export_manifest.json is unreadable: {exc}",
            ) from exc
        _validate_existing_set(previous, view, report_id, output)
        # validation passed => the complete same-identity set exists
        return {
            "replayed": True,
            "report_id": report_id,
            "output_dir": str(output),
            "files": list(OUTPUT_FILES),
            "result_status": view.result_status,
            "is_diagnostic": view.is_diagnostic,
        }

    # build all three artifacts in memory BEFORE any publish
    excel_bytes = _excel_mod.render_excel(view)
    html_bytes = _html_mod.render_html(view)
    manifest = _build_export_manifest(
        view, excel_bytes, html_bytes, report_id, notes_sha256
    )
    manifest_bytes = _json_bytes(manifest)

    artifacts: dict[str, bytes] = {
        "report.xlsx": excel_bytes,
        "report.html": html_bytes,
        "report_export_manifest.json": manifest_bytes,
    }
    for name in OUTPUT_FILES:
        _preflight_existing(output / name, artifacts[name], name)

    for name in OUTPUT_FILES:
        if name == "report_export_manifest.json":
            continue
        _publish_immutable(output / name, artifacts[name], name)
    # manifest LAST: a partial failure never leaves a complete-looking set
    _publish_immutable(
        output / "report_export_manifest.json",
        manifest_bytes,
        "report_export_manifest.json",
    )

    return {
        "replayed": False,
        "report_id": report_id,
        "output_dir": str(output),
        "files": list(OUTPUT_FILES),
        "result_status": view.result_status,
        "is_diagnostic": view.is_diagnostic,
    }


def main(argv: list[str] | None = None) -> int:
    """Thin forwarder: the report export command lives in the single
    Typer CLI (elcc-tool report export); python -m entry forwards to it
    so no second argparse framework exists."""
    from capacity_credit.cli import app


    app(list(argv) if argv is not None else sys.argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
