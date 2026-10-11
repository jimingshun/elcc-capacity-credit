"""V1-T2-B upload submission workflow (the single upload chain).

The chain follows the ESTABLISHED import contract, never a second one:
uploaded BYTES -> controlled temporary staging -> the existing readers
(plain ``pd.read_csv`` with the established encoding detection) -> the
existing quality validator (``validate_standard_tables``) -> the
existing ``ImportService.register_import`` -> a traceable import batch.
The workflow never approves, never freezes and never touches frozen
versions.

Honest states (three, kept separate):

- ``parse_error``  - the submission cannot enter the original
  registration contract at all (unknown/reserved slot, oversized or
  empty file, undecodable/unparseable bytes, missing required tables,
  resource-composition conflicts, a declared grade above the computed
  maximum that the GradeEvidence contract refuses): NO batch is created
  and the issues explain why (faithful to ``p1_workflow.import_project``
  aborting before registration);
- ``validation_failed`` - every table was readable and the original
  validator produced errors: the batch IS registered with the original
  ``validation_status="failed"`` (data version ``blocked``), keeping
  the failed state traceable - never a fabricated success batch;
- ``passed`` - the original validator passed: a ``validated`` batch.

Idempotency is CONTENT-based (no second request ledger): the data
version id is derived deterministically from the exact uploaded bytes
(dataset id + per-file size/sha256), so resubmitting the same effective
content returns the SAME existing batch (``reused=True``) instead of
creating a duplicate, while changed bytes form a new version that never
overwrites an old frozen one.

The uploaded display file name is display/provenance only: the staging
path is derived from the stable table key, never from the name.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from capacity_credit.application.atomic_io import hash_payload
from capacity_credit.application.import_service import ImportService
from capacity_credit.data.io import detect_encoding
from capacity_credit.data.template_catalog import TemplateSlot, get_slot
from capacity_credit.data.validation import (
    IssueCode,
    validate_standard_tables,
)

# an honest entry guard against accidental oversize uploads; the
# Streamlit server upload limit is NOT changed by this package
MAX_UPLOAD_BYTES_PER_FILE = 64 * 1024 * 1024

_PARSE_UNREADABLE = "DQ02_CSV_UNREADABLE"
_FILE_OVERSIZE = "DQ01_FILE_OVERSIZE"
_COMPOSITION_CONFLICT = "DQ05_COMPOSITION_CONFLICT"
_COMPOSITION_UNCONFIRMED = "DQ05_COMPOSITION_UNCONFIRMED"
_GRADE_ORDER = ("G0_teaching", "G1_preliminary", "G2_research", "G3_rule_support")

_REQUIRED_TABLES = frozenset(
    {
        "load_hourly",
        "thermal_units",
        "thermal_reliability_classes",
        "thermal_unit_reliability_map",
        "renewable_hourly",
        "renewable_capacities",
        "intertie_hourly",
        "interties",
        "weather_year_weight_sets",
    }
)


class UploadRejection(ValueError):
    """A submission-level refusal before any processing (unknown or
    reserved slot, illegal grade, non-byte content). Carries an honest
    display message."""


def _issue(
    code: str, severity: str, message: str, table: str = "", locator: str = ""
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "table": table,
        "locator": locator,
        "message": message,
    }


def _derived_version_id(dataset_id: str, entries: list[dict[str, Any]]) -> str:
    """The stable content identity: deterministic from the exact byte
    inventory, so the same effective content always maps to the same
    data version (and an existing batch is reused, never duplicated)."""
    digest = hash_payload({"dataset_id": dataset_id, "files": entries})
    return f"dv-{digest[:24]}"


def _result(
    status: str,
    *,
    import_batch_id: str | None,
    data_version_id: str | None,
    reused: bool,
    validation_status: str | None,
    declared_grade: str,
    created_by: str,
    file_provenance: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    row_counts: dict[str, int],
    previews: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": status,
        "import_batch_id": import_batch_id,
        "data_version_id": data_version_id,
        "reused": reused,
        "validation_status": validation_status,
        "declared_data_grade": declared_grade,
        "created_by": created_by,
        "files": file_provenance,
        "issues": issues,
        "row_counts": row_counts,
        "previews": previews,
    }


def submit_upload_import(
    *,
    project_dir: Path | str,
    files: dict[str, tuple[str, bytes]],
    declared_grade: str,
    created_by: str,
    declared_composition: dict[str, str] | None = None,
    declared_total_capacity_mw: float | None = None,
    ledger_total_tolerance_percent: float = 1.0,
) -> dict[str, Any]:
    """Run the upload chain for ONE explicit submission and return the
    honest projection (see module docstring for the three states).

    ``files`` maps stable table_key -> (display_file_name, raw_bytes).
    The display name never becomes a filesystem path.
    """
    project = Path(project_dir)
    if not isinstance(declared_grade, str) or declared_grade not in _GRADE_ORDER:
        raise UploadRejection(
            f"非法数据等级声明：{declared_grade!r}（须为合同受控值之一）"
        )
    if not files:
        raise UploadRejection("未提交任何上传文件")
    composition = {
        key: str((declared_composition or {}).get(key, "unconfirmed"))
        for key in ("storage", "hydro")
    }

    # -- slot validation (before any processing) -------------------------
    for table_key, (display_name, raw) in sorted(files.items()):
        slot = get_slot(str(table_key))
        if slot is None or slot.columns is None:
            raise UploadRejection(
                f"未知或预留的上传槽位：{table_key!r}"
                "（预留表在本版本没有读取器，不提供上传）"
            )
        if not isinstance(display_name, str) or not display_name.strip():
            raise UploadRejection(f"槽位 {table_key} 的显示文件名缺失")
        if not isinstance(raw, (bytes, bytearray)):
            raise UploadRejection(f"槽位 {table_key} 的上传内容不是字节")

    issues: list[dict[str, Any]] = []
    file_provenance: list[dict[str, Any]] = []
    frames: dict[str, pd.DataFrame] = {}
    row_counts: dict[str, int] = {}
    previews: dict[str, dict[str, Any]] = {}
    staged_files: dict[str, Path] = {}
    parse_failed = False

    with tempfile.TemporaryDirectory(prefix="elcc-upload-") as scratch:
        staged_dir = Path(scratch) / "staging"
        staged_dir.mkdir()
        for table_key, (display_name, raw) in sorted(files.items()):
            slot: TemplateSlot = get_slot(str(table_key))  # type: ignore[assignment]
            assert slot is not None and slot.columns is not None
            raw = bytes(raw)
            sha256 = hashlib.sha256(raw).hexdigest()
            relative_path = f"data/{slot.file_name}"
            file_provenance.append(
                {
                    "table_key": table_key,
                    "file_name": display_name,  # display/provenance only
                    "relative_path": relative_path,
                    "size_bytes": len(raw),
                    "sha256": sha256,
                }
            )
            if len(raw) > MAX_UPLOAD_BYTES_PER_FILE:
                issues.append(
                    _issue(
                        _FILE_OVERSIZE,
                        "error",
                        f"文件超过本入口上限 {MAX_UPLOAD_BYTES_PER_FILE} 字节"
                        f"（实际 {len(raw)} 字节）；本入口不承诺未测量的大文件容量",
                        table=table_key,
                        locator=display_name,
                    )
                )
                parse_failed = True
                continue
            if len(raw) == 0:
                issues.append(
                    _issue(
                        IssueCode.FILE_EMPTY,
                        "error",
                        "上传文件为空（0字节）",
                        table=table_key,
                        locator=display_name,
                    )
                )
                parse_failed = True
                continue
            encoding = detect_encoding(raw)
            if encoding == "unknown":
                issues.append(
                    _issue(
                        IssueCode.ENCODING_UNKNOWN,
                        "error",
                        "无法识别文件编码（读取器支持 utf-8-sig/utf-8/gb18030）",
                        table=table_key,
                        locator=display_name,
                    )
                )
                parse_failed = True
                continue
            # the EXACT uploaded bytes are staged under the table-key
            # derived path for the original register step; display names
            # are never path components
            staged_target = staged_dir / slot.file_name
            staged_target.write_bytes(raw)
            staged_files[relative_path] = staged_target
            try:
                frame = pd.read_csv(io.BytesIO(raw), encoding=encoding)
            except Exception as exc:  # noqa: BLE001 - any parser failure is
                # an honest per-file parse issue, never a crash
                issues.append(
                    _issue(
                        _PARSE_UNREADABLE,
                        "error",
                        f"CSV 无法解析：{exc}",
                        table=table_key,
                        locator=display_name,
                    )
                )
                parse_failed = True
                continue
            extra = [c for c in frame.columns if c not in slot.columns]
            for column in extra:
                issues.append(
                    _issue(
                        IssueCode.UNKNOWN_COLUMN_PRESERVED,
                        "warning",
                        f"模板外的未知列已原样保留：{column}",
                        table=table_key,
                        locator=str(column),
                    )
                )
            frames[table_key] = frame
            row_counts[table_key] = len(frame)
            previews[table_key] = {
                "total_rows": len(frame),
                "columns": [str(c) for c in frame.columns][
                    : len(slot.columns) + 8
                ],
                "head": [
                    [("" if pd.isna(v) else str(v))[:80] for v in row]
                    for row in frame.head(5).itertuples(index=False, name=None)
                ],
                "display_name": display_name,
                "sha256": sha256,
                "encoding": encoding,
            }

        # -- composition consistency: declared vs ACTUAL uploads; a
        #    conflict blocks - tables are never dropped to fit ----------
        composition_issues = _composition_issues(frames, composition, row_counts)
        issues.extend(composition_issues)
        if composition_issues:
            parse_failed = True

        # -- required-table presence (faithful to the original chain:
        #    _read_tables aborts BEFORE registration when a required
        #    table is missing) ----------------------------------------
        missing_required = sorted(
            table for table in _REQUIRED_TABLES if table not in frames
        )
        if missing_required:
            for table in missing_required:
                issues.append(
                    _issue(
                        IssueCode.FILE_MISSING,
                        "error",
                        "必需表未上传（原导入合同在注册前即中止）",
                        table=table,
                    )
                )
            parse_failed = True

        if parse_failed:
            return _result(
                "parse_error",
                import_batch_id=None,
                data_version_id=None,
                reused=False,
                validation_status=None,
                declared_grade=declared_grade,
                created_by=created_by,
                file_provenance=file_provenance,
                issues=issues,
                row_counts=row_counts,
                previews=previews,
            )

        # -- the ORIGINAL quality validator (full frames, never a
        #    preview subset) -------------------------------------------
        report = validate_standard_tables(
            frames,
            declared_grade=declared_grade,
            declared_total_capacity_mw=declared_total_capacity_mw,
            ledger_total_tolerance_percent=ledger_total_tolerance_percent,
        )
        for issue in report.errors:
            issues.append(
                _issue(
                    issue.code,
                    "error",
                    issue.message,
                    table=issue.table,
                    locator=issue.locator,
                )
            )
        for issue in report.warnings:
            issues.append(
                _issue(
                    issue.code,
                    "warning",
                    issue.message,
                    table=issue.table,
                    locator=issue.locator,
                )
            )

        dataset_id = f"dataset-{project.name}"
        content_entries = [
            {
                "relative_path": entry["relative_path"],
                "size_bytes": entry["size_bytes"],
                "sha256": entry["sha256"],
            }
            for entry in sorted(
                file_provenance, key=lambda f: f["relative_path"]
            )
        ]
        data_version_id = _derived_version_id(dataset_id, content_entries)
        import_service = ImportService(project)

        # a declared grade above the computed maximum makes the batch
        # UNREGISTRABLE under the GradeEvidence contract (relation
        # declared <= computed_max) - an explicit no-batch refusal, not
        # a fabricated failed batch
        if any(
            i["code"] == IssueCode.GRADE_ABOVE_MAX and i["severity"] == "error"
            for i in issues
        ):
            issues.append(
                _issue(
                    IssueCode.GRADE_ABOVE_MAX,
                    "error",
                    "声明等级超过数据可支持的最高等级：原注册合同拒绝该等级证据，"
                    "本次提交未注册批次；请更正声明等级后重新提交",
                )
            )
            return _result(
                "parse_error",
                import_batch_id=None,
                data_version_id=None,
                reused=False,
                validation_status=None,
                declared_grade=declared_grade,
                created_by=created_by,
                file_provenance=file_provenance,
                issues=issues,
                row_counts=row_counts,
                previews=previews,
            )

        # -- content idempotency + registration under the SAME version
        #    lock approve/freeze use: the reuse scan and the registration
        #    are one atomic scope per (dataset, version), so concurrent
        #    identical submissions can never create duplicate batches --
        existing_batch: str | None = None
        reused_state = None
        with import_service.locked_version_scope(dataset_id, data_version_id):
            existing_batch = _find_batch_with_version(
                import_service, data_version_id
            )
            if existing_batch is not None:
                reused_state = import_service.current_state(existing_batch)
            else:
                # register through the ORIGINAL service with the ORIGINAL
                # state mapping (failed stays failed; register_import
                # re-reads and hashes the exact staged uploaded bytes)
                validation_status = report.status
                batch_id = import_service.register_import(
                    dataset_id=dataset_id,
                    data_version_id=data_version_id,
                    files=staged_files,
                    validation_status=validation_status,
                    created_by=created_by,
                    grades={
                        "declared_data_grade": declared_grade,
                        "computed_max_data_grade": report.computed_max_grade
                        or declared_grade,
                        "effective_data_grade": declared_grade,
                    },
                )
        if existing_batch is not None:
            assert reused_state is not None
            # the batch anchor is the authority: report the DECLARED grade
            # the batch actually carries, never this submission's fresh text
            anchor_declared = (reused_state.grades or {}).get(
                "declared_data_grade", declared_grade
            )
            return _result(
                "passed"
                if reused_state.validation_status == "passed"
                else "validation_failed",
                import_batch_id=existing_batch,
                data_version_id=data_version_id,
                reused=True,
                validation_status=reused_state.validation_status,
                declared_grade=str(anchor_declared),
                created_by=created_by,
                file_provenance=file_provenance,
                issues=issues,
                row_counts=row_counts,
                previews=previews,
            )
        return _result(
            "passed" if validation_status == "passed" else "validation_failed",
            import_batch_id=batch_id,
            data_version_id=data_version_id,
            reused=False,
            validation_status=validation_status,
            declared_grade=declared_grade,
            created_by=created_by,
            file_provenance=file_provenance,
            issues=issues,
            row_counts=row_counts,
            previews=previews,
        )


def _find_batch_with_version(
    import_service: ImportService, data_version_id: str
) -> str | None:
    """The existing batch of this project carrying the same content
    identity (imports/ children are enumerated defensively; a corrupt
    child is skipped, never marked)."""
    imports_dir = import_service.root / "imports"
    if not imports_dir.is_dir():
        return None
    from capacity_credit.application.p1_workflow import is_reparse_point

    for child in sorted(imports_dir.iterdir()):
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if is_reparse_point(child) or not child.is_dir():
            continue
        try:
            state = import_service.current_state(child.name)
        except Exception:  # noqa: BLE001, S112 - a corrupt batch is simply
            # not a reuse match; the skip is the honest outcome here
            continue
        if state.data_version_id == data_version_id:
            return child.name
    return None


def _composition_issues(
    frames: dict[str, pd.DataFrame],
    composition: dict[str, str],
    row_counts: dict[str, int],
) -> list[dict[str, Any]]:
    """Declared resource composition vs ACTUAL uploads: a conflict is a
    blocking submission issue - tables are never silently dropped to
    make the data 'fit the declaration'."""
    found: list[dict[str, Any]] = []

    def _uploaded(table: str) -> bool:
        return table in frames and row_counts.get(table, 0) > 0

    for resource, slots in (
        ("storage", ("storage_units",)),
        ("hydro", ("hydro_units", "hydro_hourly")),
    ):
        state = composition[resource]
        uploaded = [s for s in slots if _uploaded(s)]
        label = {"storage": "储能", "hydro": "水电"}[resource]
        if state == "excluded" and uploaded:
            found.append(
                _issue(
                    _COMPOSITION_CONFLICT,
                    "error",
                    f"项目已声明“明确不包含{label}”，但上传了非空表"
                    f"{'、'.join(uploaded)}；请在项目构成中更正声明，"
                    "或移除该表——不会静默丢弃已上传的表",
                    table=uploaded[0],
                )
            )
        elif state == "unconfirmed" and uploaded:
            found.append(
                _issue(
                    _COMPOSITION_UNCONFIRMED,
                    "error",
                    f"资源构成尚未确认，但上传了{label}表（{'、'.join(uploaded)}）；"
                    f"请先在项目构成中明确是否包含{label}",
                    table=uploaded[0],
                )
            )
        elif state == "included":
            for slot in slots:
                if slot not in frames:
                    found.append(
                        _issue(
                            IssueCode.FILE_MISSING,
                            "error",
                            f"项目已声明包含{label}，但未上传其必需表 {slot}",
                            table=slot,
                        )
                    )
    # hydro_hourly requires its hydro_units companion (catalog cross-ref)
    if _uploaded("hydro_hourly") and not _uploaded("hydro_units"):
        found.append(
            _issue(
                IssueCode.DATA_ID_ORPHAN,
                "error",
                "上传了水电逐小时出力但缺少水电站清单（hydro_units）",
                table="hydro_hourly",
            )
        )
    return found


__all__ = [
    "MAX_UPLOAD_BYTES_PER_FILE",
    "UploadRejection",
    "submit_upload_import",
]
