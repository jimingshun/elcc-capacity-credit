"""Cost calculation use-case service (night-5 N2).

THE cost-domain run container: versioned cost-run records under
``<project>/cost_runs/<run_ref>/`` - NEVER ``<project>/runs/`` (whose
listing validates the formal RunManifest contract; a cost record there
would corrupt the honest reliability listing).

Layout (atomic primitives, immutable artifacts, manifest last):

    cost_runs/<run_ref>/
        input_snapshot.json     immutable - the exact submitted payload
        cost_result.json        immutable - the domain result (success)
        cost_run_manifest.json  written LAST (hashes both artifacts)

Identity: ``run_ref`` IS the LocalRuntime request_ref (the same
admission identity the reliability chain uses), so a replayed intent
re-enters the same record instead of creating a second one. A failed
computation is recorded honestly (status=failed + reason), never
silent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    hash_payload,
    safe_filename_component,
    stable_dump,
)
from capacity_credit.application.local_runtime import Observer
from capacity_credit.costs import CostInputError, compute_fixed_cost
from capacity_credit.costs.gross_cone import compute_gross_cone
from capacity_credit.costs.net_cone import compute_net_cone

COST_RUNS_DIR = "cost_runs"
COST_RUN_MANIFEST_FILE = "cost_run_manifest.json"
INPUT_SNAPSHOT_FILE = "input_snapshot.json"
RESULT_FILE = "cost_result.json"
COST_RUN_SCHEMA_VERSION = "1.0"

# the cost kinds this version implements; the facade only admits kinds
# in the DELIVERED subset (extended by their own packages)
COST_KIND_FIXED = "cost_fixed"
COST_KIND_NET_CONE = "cost_net_cone"
COST_KIND_GROSS_CONE = "cost_gross_cone"
_COST_COMPUTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    COST_KIND_FIXED: compute_fixed_cost,
    COST_KIND_NET_CONE: compute_net_cone,
    COST_KIND_GROSS_CONE: compute_gross_cone,
}


class CostServiceError(ValueError):
    """A cost use-case refusal (display kind + reason)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _read_cost_run_workspace(project_dir: Path, run_ref: str) -> dict[str, Any]:
    """Resolve one cost run by ref across the WORKSPACE (R1 cross-project
    reference): the submitting project first, then every other project
    of the workspace, until the run is found. The caller's caliber gate
    still compares the Net side against the referenced record, so a
    cross-project binding stays honest. A missing ref refuses."""
    try:
        return read_cost_run(project_dir, run_ref)
    except CostServiceError:
        pass
    root = project_dir.parent
    try:
        from capacity_credit.application.project_service import (
            project_kind_of,
            read_registry,
        )

        registry = read_registry(root)
    except Exception as exc:  # noqa: BLE001 - unreadable registry
        raise CostServiceError(
            "not_found", f"Gross来源不可用：{exc}"
        ) from None
    for entry in registry.get("projects", []):
        ref = str(entry.get("project_ref") or "")
        if not ref or ref == project_dir.name:
            continue
        if project_kind_of(entry) != "cost":
            continue
        try:
            return read_cost_run(root / ref, run_ref)
        except CostServiceError:
            continue
    raise CostServiceError(
        "not_found", f"成本运行记录不存在：{run_ref}"
    )


def resolve_gross_reference(
    project_dir: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    """THE internal Gross binding (night-5 N4; R1 cross-project): when a
    Net CONE submission declares ``gross_mode="internal_reference"``, the
    referenced cost run is resolved EXPLICITLY here - completed, a
    gross-cone run with a solved rate - and the caliber fields are
    filled FROM THE REFERENCED RECORD (never re-typed, never the
    "latest file"): the normal same-caliber gate then compares the Net
    side against the REFERENCED run. The binding provenance (run ref +
    result hash) enters the effective payload so the published snapshot
    records exactly which Gross result was consumed. A damaged/missing/
    mismatched source refuses the submission BEFORE admission; silently
    switching to a latest result is impossible by construction (the
    reference is an explicit run_ref). R1: with measurement-type
    projects the Gross and Net live in DIFFERENT projects, so the lookup
    walks the whole workspace (same-project first, then any project) -
    the caliber gate (below) is unchanged and still compares the Net
    side against the referenced record, so cross-project binding stays
    honest."""
    import json

    if payload.get("gross_mode") != "internal_reference":
        return payload
    run_ref = payload.get("gross_run_ref")
    if not isinstance(run_ref, str) or not run_ref.strip():
        raise CostServiceError(
            "invalid_input",
            "Gross来源模式为“引用本工具运行”时必须提供 gross_run_ref",
        )
    record = _read_cost_run_workspace(Path(project_dir), run_ref.strip())
    manifest = record["manifest"]
    result = record.get("result")
    if manifest.get("cost_kind") != COST_KIND_GROSS_CONE:
        raise CostServiceError(
            "invalid_input",
            f"Gross来源 {run_ref} 不是完整Gross CONE运行（{manifest.get('cost_kind')}）",
        )
    if manifest.get("status") != "completed" or not isinstance(result, dict):
        raise CostServiceError(
            "invalid_input", f"Gross来源 {run_ref} 未完成，不能被引用"
        )
    rate = result.get("gross_cone_per_kw")
    if not isinstance(rate, (int, float)):
        raise CostServiceError(
            "invalid_input",
            f"Gross来源 {run_ref} 未求得Gross CONE（无解或未完成求解），不能被引用",
        )
    gross_input = record["input"]
    gross_capacity = gross_input.get("capacity_kw")
    net_capacity = payload.get("capacity_kw")
    if (
        isinstance(gross_capacity, (int, float))
        and isinstance(net_capacity, (int, float))
        and float(gross_capacity) != float(net_capacity)
    ):
        raise CostServiceError(
            "invalid_input",
            f"容量分母不一致，拒绝引用：Net侧={net_capacity!r}kW，"
            f"被引用Gross运行={gross_capacity!r}kW（同口径校验含数值分母本身）",
        )
    effective = dict(payload)
    effective["gross_value"] = float(rate)
    effective["gross_source"] = (
        f"本工具完整Gross CONE运行 {run_ref}"
        f"（结果SHA-256 {str(manifest.get('result_sha256'))[:16]}…）"
    )
    # the caliber fields come FROM THE REFERENCED RECORD; the normal
    # gate compares them against the Net side
    effective["gross_reference_unit"] = str(gross_input.get("object_name", ""))
    effective["gross_region"] = str(gross_input.get("region", ""))
    effective["gross_assessment_year"] = int(gross_input.get("target_year", 0))
    effective["gross_price_year"] = int(gross_input.get("price_basis_year", 0))
    # the solved revenue requirement is a MARKET revenue before income
    # tax (taxes are handled inside the model); this mapping is disclosed
    effective["gross_tax_basis"] = "不含增值税、所得税前"
    effective["gross_capacity_denominator_kind"] = str(
        gross_input.get("capacity_denominator_kind", "")
    )
    effective["gross_annualization"] = "等额年度序列"
    effective["gross_confirmed"] = payload.get("gross_confirmed") is True
    effective["gross_binding"] = {
        "run_ref": run_ref.strip(),
        "result_sha256": manifest.get("result_sha256"),
        "input_snapshot_sha256": manifest.get("input_snapshot_sha256"),
    }
    # keep raw payload fields from leaking as unresolved strings
    effective.pop("gross_run_ref", None)
    return effective


def validate_cost_payload(cost_kind: str, payload: Any) -> None:
    """THE pre-admission validation gate: a malformed payload refuses
    the submission BEFORE any admission or artifact write (the cost
    analogue of the reliability data-capability gates)."""
    if cost_kind not in _COST_COMPUTERS:
        raise CostServiceError(
            "method_unavailable",
            f"成本测算类型 {cost_kind!r} 尚未接入本版本",
        )
    if not isinstance(payload, dict):
        raise CostServiceError(
            "invalid_input", "成本输入必须为对象（表单字段映射）"
        )
    try:
        _COST_COMPUTERS[cost_kind](payload)
    except CostInputError as exc:
        raise CostServiceError(exc.kind, exc.message) from None


def cost_runs_dir(project_dir: Path) -> Path:
    return project_dir / COST_RUNS_DIR


def write_cost_run(
    *,
    project_dir: Path,
    run_ref: str,
    request_id: str,
    cost_kind: str,
    payload: dict[str, Any],
    software_version: str,
    software_commit: str,
) -> dict[str, Any]:
    """Compute + publish ONE cost run record (manifest last). Cost
    artifacts are IMMUTABLE: an existing input snapshot / result /
    manifest under the same run_ref is a hard ObjectExistsError
    refusal (never an overwrite; in practice the runtime never
    re-invokes a finished intent and run_ref is a fresh uuid, so the
    facade path cannot hit it)."""
    safe_filename_component(run_ref)
    run_dir = cost_runs_dir(project_dir) / run_ref
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = _COST_COMPUTERS[cost_kind](payload)
        failure: str | None = None
    except CostInputError as exc:
        result = None
        failure = exc.message
    snapshot_bytes = stable_dump(payload)
    snapshot_path = run_dir / INPUT_SNAPSHOT_FILE
    atomic_write_bytes(snapshot_path, snapshot_bytes, immutable=True)
    result_path: Path | None = None
    result_sha: str | None = None
    if result is not None:
        result_path = run_dir / RESULT_FILE
        result_sha = hash_payload(result)
        atomic_write_bytes(
            result_path, stable_dump(result), immutable=True
        )
    manifest = {
        "cost_run_schema_version": COST_RUN_SCHEMA_VERSION,
        "run_ref": run_ref,
        "request_id": request_id,
        "project_ref": project_dir.name,
        "cost_kind": cost_kind,
        "status": "failed" if failure else "completed",
        "failure_reason": failure,
        "created_at": datetime.now(UTC).isoformat(),
        "input_snapshot_path": INPUT_SNAPSHOT_FILE,
        "input_snapshot_sha256": hash_payload(payload),
        "result_path": RESULT_FILE if result_path else None,
        "result_sha256": result_sha,
        "software_version": software_version,
        "software_commit": software_commit,
    }
    manifest_bytes = stable_dump(manifest)
    atomic_write_bytes(
        run_dir / COST_RUN_MANIFEST_FILE, manifest_bytes, immutable=True
    )
    return manifest


def make_cost_runner(
    *,
    project_dir: Path,
    cost_kind: str,
    payload: dict[str, Any],
    software_version: str,
    software_commit: str,
) -> Callable[[Observer, str], str]:
    """The runner closed over the cost context: computes synchronously
    (deterministic domain math, no sampling) and publishes the run
    record. Returns the run_ref (the runtime request identity)."""

    def _runner(observe: Observer, request_ref: str) -> str:
        manifest = write_cost_run(
            project_dir=project_dir,
            run_ref=request_ref,
            request_id=request_ref,
            cost_kind=cost_kind,
            payload=payload,
            software_version=software_version,
            software_commit=software_commit,
        )
        if manifest["status"] == "failed":
            raise CostServiceError(
                "computation_failed", manifest["failure_reason"] or "计算失败"
            )
        return request_ref

    return _runner


def list_cost_runs(project_dir: Path | str) -> list[dict[str, Any]]:
    """The read-only cost-run listing: one summary per first-level
    ``cost_runs`` directory; a corrupt manifest becomes an INVALID
    entry (never dropped, never guessed). Deterministic order
    (created_at descending, run_ref ascending on ties)."""
    root = Path(project_dir)
    base = cost_runs_dir(root)
    summaries: list[dict[str, Any]] = []
    if not base.is_dir():
        return summaries
    import json

    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        manifest_path = entry / COST_RUN_MANIFEST_FILE
        if not manifest_path.is_file():
            summaries.append(
                {
                    "run_ref": entry.name,
                    "cost_kind": "",
                    "status": "",
                    "created_at": "",
                    "invalid": True,
                    "invalid_reason": "缺少cost_run_manifest.json",
                }
            )
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not an object")
            summaries.append(
                {
                    "run_ref": str(entry.name),
                    "cost_kind": str(manifest.get("cost_kind", "")),
                    "status": str(manifest.get("status", "")),
                    "created_at": str(manifest.get("created_at", "")),
                    "invalid": False,
                    "invalid_reason": None,
                }
            )
        except (OSError, ValueError) as exc:
            summaries.append(
                {
                    "run_ref": entry.name,
                    "cost_kind": "",
                    "status": "",
                    "created_at": "",
                    "invalid": True,
                    "invalid_reason": str(exc)[:300],
                }
            )
    # deterministic: created_at descending, run_ref ascending on ties
    # (stable two-pass sort)
    summaries.sort(key=lambda s: s["run_ref"])
    summaries.sort(key=lambda s: str(s.get("created_at")), reverse=True)
    return summaries


def read_cost_run(project_dir: Path | str, run_ref: str) -> dict[str, Any]:
    """One cost-run record (manifest + snapshot + result when
    completed). A missing/illegal ref or corrupt artifacts raise an
    honest CostServiceError - never a partial replay."""
    root = Path(project_dir)
    safe_filename_component(run_ref)
    run_dir = cost_runs_dir(root) / run_ref
    manifest_path = run_dir / COST_RUN_MANIFEST_FILE
    if not manifest_path.is_file():
        raise CostServiceError("not_found", f"成本运行记录不存在：{run_ref}")
    import json

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot = json.loads(
            (run_dir / INPUT_SNAPSHOT_FILE).read_text(encoding="utf-8")
        )
        if not isinstance(manifest, dict) or not isinstance(snapshot, dict):
            raise ValueError("manifest or snapshot is not an object")
        if manifest.get("input_snapshot_sha256") != hash_payload(snapshot):
            raise ValueError("input snapshot hash mismatch")
        result: dict[str, Any] | None = None
        if manifest.get("status") == "completed":
            raw = (run_dir / RESULT_FILE).read_text(encoding="utf-8")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("result is not an object")
            if manifest.get("result_sha256") != hash_payload(result):
                raise ValueError("result hash mismatch")
    except (OSError, ValueError) as exc:
        raise CostServiceError(
            "corrupt_record", f"成本运行记录损坏：{run_ref}（{exc}）"
        ) from None
    return {
        "manifest": manifest,
        "input": snapshot,
        "result": result,
    }


def new_request_id() -> str:
    """A fresh request id for a user-initiated fresh cost submission."""
    return f"cost-{uuid.uuid4().hex}"


__all__ = [
    "COST_KIND_FIXED",
    "COST_KIND_GROSS_CONE",
    "COST_KIND_NET_CONE",
    "COST_RUN_MANIFEST_FILE",
    "CostServiceError",
    "list_cost_runs",
    "make_cost_runner",
    "new_request_id",
    "read_cost_run",
    "validate_cost_payload",
    "write_cost_run",
]
