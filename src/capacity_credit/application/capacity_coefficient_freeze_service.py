"""供需系数冻结（Step 3 S0；Q-S6：系数持久化为可引用正式结果）.

与成本域同款纪律的成本式轻量正式结果：**不建 RunService 运行记录、
不动 MethodResult 结果合同**。布局（原子写、不可变工件、manifest-last）：

    projects/<ref>/coefficient/
        coefficient_formal.json      不可变——公式 v1 全输出＋血缘＋确认人＋冻结时间
        coefficient_manifest.json    最后写入——冻结身份＋工件哈希＋软件版本

绑定：冻结成功后经 project_service.bind_formal_result 绑定为项目唯一正式结果
（一个项目一个正式结果；同 run_ref 幂等、异 run_ref 硬拒——二次冻结天然被拒）。
冻结是**显式用户动作**（确认人必填）；试算 preview 与冻结互不干扰（试算零副作用）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    hash_payload,
    safe_filename_component,
    stable_dump,
)
from capacity_credit.application.capacity_coefficient_service import (
    CapacityCoefficientError,
    compute_capacity_coefficient,
)

COEFFICIENT_DIR = "coefficient"
FORMAL_FILE = "coefficient_formal.json"
MANIFEST_FILE = "coefficient_manifest.json"
FREEZE_SCHEMA_VERSION = "1.0"


class CoefficientFreezeError(ValueError):
    """A coefficient freeze refusal (display kind + reason)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def freeze_capacity_coefficient(
    *,
    workspace_root: Path,
    project_ref: str,
    payload: dict[str, Any],
    lineage: dict[str, Any] | None,
    confirmer: str,
    software_version: str,
    software_commit: str,
) -> dict[str, Any]:
    """Compute（公式 v1）→ 写冻结记录（manifest last）→ 绑定正式结果。

    全部校验先于任何副作用：类型锁 / 已完成门 / 确认人 / 输入无效一律
    在写盘前拒绝（项目保持诚实未绑定）。绑定失败（已有正式结果）同样
    发生在写盘前——bind_formal_result 的 request_conflict 先于落盘
    检查，冻结记录永不成为孤儿。"""
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        _find_entry,
        bind_formal_result,
        get_formal_result,
        measurement_type_of,
        read_registry,
    )

    root = Path(workspace_root)
    if not str(confirmer or "").strip():
        raise CoefficientFreezeError(
            "invalid_input", "冻结是显式用户动作：确认人必填（留痕追溯）"
        )
    try:
        registry = read_registry(root)
        entry = _find_entry(registry, project_ref)
    except ProjectServiceError as exc:
        raise CoefficientFreezeError(exc.kind, exc.message) from None
    locked = measurement_type_of(entry)
    if locked != "demand_coefficient":
        raise CoefficientFreezeError(
            "invalid_input",
            f"供需系数冻结需要容量供需系数项目（当前锁定类型：{locked or '未登记'}）。"
            "请在系数页新建供需系数项目后再冻结。",
        )
    try:
        existing = get_formal_result(root, project_ref)
    except ProjectServiceError as exc:
        raise CoefficientFreezeError(exc.kind, exc.message) from None
    if isinstance(existing, dict) and existing.get("run_ref"):
        raise CoefficientFreezeError(
            "request_conflict",
            "该项目已有正式系数结果"
            f"（{str(existing.get('run_ref'))[:8]}…），正式结果不可替换；"
            "重新测算请复制/新建项目后冻结。",
        )
    try:
        result = compute_capacity_coefficient(payload)
    except CapacityCoefficientError as exc:
        raise CoefficientFreezeError("invalid_input", exc.message) from None

    freeze_ref = uuid.uuid4().hex
    safe_filename_component(freeze_ref)
    project_dir = root / project_ref
    formal = {
        "coefficient_freeze_schema_version": FREEZE_SCHEMA_VERSION,
        "freeze_ref": freeze_ref,
        "project_ref": project_ref,
        "confirmer": confirmer.strip(),
        "frozen_at": datetime.now(UTC).isoformat(),
        **result,
        "lineage": lineage or {},
    }
    formal_path = project_dir / COEFFICIENT_DIR / FORMAL_FILE
    formal_path.parent.mkdir(parents=True, exist_ok=True)
    formal_sha = hash_payload(formal)
    atomic_write_bytes(formal_path, stable_dump(formal), immutable=True)
    manifest = {
        "coefficient_freeze_schema_version": FREEZE_SCHEMA_VERSION,
        "freeze_ref": freeze_ref,
        "project_ref": project_ref,
        "status": "frozen",
        "confirmer": confirmer.strip(),
        "frozen_at": formal["frozen_at"],
        "formal_path": FORMAL_FILE,
        "formal_sha256": formal_sha,
        "coefficient": result["coefficient"],
        "formula_version": result["formula_version"],
        "caliber": result["caliber"],
        "target_year": result["declared_inputs"]["target_year"],
        "software_version": software_version,
        "software_commit": software_commit,
    }
    atomic_write_bytes(
        project_dir / COEFFICIENT_DIR / MANIFEST_FILE,
        stable_dump(manifest),
        immutable=True,
    )
    try:
        bind_formal_result(
            root,
            project_ref,
            domain="demand",
            run_ref=freeze_ref,
            result_sha256=formal_sha,
        )
    except ProjectServiceError as exc:
        raise CoefficientFreezeError(exc.kind, exc.message) from None
    return manifest


def read_coefficient_freeze(project_dir: Path | str) -> dict[str, Any] | None:
    """读单个项目的冻结记录（manifest＋正式工件）；无冻结→None；
    工件损坏/哈希不符→invalid 诚实行（不静默丢弃）。"""
    base = Path(project_dir) / COEFFICIENT_DIR
    manifest_path = base / MANIFEST_FILE
    if not manifest_path.exists():
        return None
    import json

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "invalid": True,
            "invalid_reason": f"冻结清单不可解析：{exc}",
            "freeze_ref": None,
        }
    formal_path = base / str(manifest.get("formal_path") or FORMAL_FILE)
    try:
        formal = json.loads(formal_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "invalid": True,
            "invalid_reason": f"冻结工件不可读取：{exc}",
            "freeze_ref": manifest.get("freeze_ref"),
        }
    if hash_payload(formal) != manifest.get("formal_sha256"):
        return {
            "invalid": True,
            "invalid_reason": "冻结工件哈希与清单不符（内容已被改动）",
            "freeze_ref": manifest.get("freeze_ref"),
        }
    return {"manifest": manifest, "formal": formal, "invalid": False}


def list_coefficient_freezes(
    workspace_root: Path | str, *, target_year: int | None = None
) -> list[dict[str, Any]]:
    """全工作区冻结系数只读列表（结算选定数据源）：登记册内全部项目，
    逐项目读冻结记录；损坏记录保留为 invalid 行（诚实不丢弃）。"""
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        read_registry,
    )

    root = Path(workspace_root)
    try:
        registry = read_registry(root)
    except ProjectServiceError:
        return []
    rows: list[dict[str, Any]] = []
    for entry in registry.get("projects", []):
        ref = str(entry.get("project_ref") or "")
        if not ref:
            continue
        record = read_coefficient_freeze(root / ref)
        if record is None:
            continue
        if record.get("invalid"):
            rows.append({
                "project_ref": ref,
                "project_name": entry.get("name") or ref,
                "freeze_ref": record.get("freeze_ref"),
                "invalid": True,
                "invalid_reason": record.get("invalid_reason"),
            })
            continue
        manifest = record["manifest"]
        if target_year is not None and manifest.get("target_year") != target_year:
            continue
        formal = record["formal"]
        rows.append({
            "project_ref": ref,
            "project_name": entry.get("name") or ref,
            "freeze_ref": manifest.get("freeze_ref"),
            "coefficient": manifest.get("coefficient"),
            "formula_version": manifest.get("formula_version"),
            "caliber": manifest.get("caliber"),
            "target_year": manifest.get("target_year"),
            "confirmer": manifest.get("confirmer"),
            "frozen_at": manifest.get("frozen_at"),
            "inputs": formal.get("inputs"),
            "lineage": formal.get("lineage") or {},
            "invalid": False,
        })
    rows.sort(key=lambda r: str(r.get("frozen_at") or ""), reverse=True)
    return rows


__all__ = [
    "CoefficientFreezeError",
    "freeze_capacity_coefficient",
    "list_coefficient_freezes",
    "read_coefficient_freeze",
]
