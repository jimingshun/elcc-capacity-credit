"""月度状态机与试算（Step 3 S2-M1；主责设计 §3/§5/§6/§9）.

持久化布局（cost_runs 同款：原子写＋不可变工件＋manifest-last）：

    projects/<ref>/settlement/months/mNN/
        input_snapshot.json     该月填报快照（试算时冻结输入）
        trial_runs/<ref>/       月度试算记录（不可变，可多次）
        month_manifest.json     月度状态（最后写入）

状态机（S2-M1 前四态；锁定在 M2）：
    未开始 → 待检查（有填报）→ 待确认（检查通过）/检查未通过（有阻断）
    输入再变化 → 回退待检查（检查失效）

试算＝技术预览：可多次、不改变月度状态、不产生正式金额（trial=True 标注）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    hash_payload,
    stable_dump,
)
from capacity_credit.application.settlement_service import (
    REFERENCES_FILE,
    SETTLEMENT_DIR,
    SettlementServiceError,
    _require_settlement_project,
    list_references,
    read_price_table,
    read_subjects,
    read_year_config,
    standard_for_resource_type,
)

MONTHS_DIR = "months"
INPUT_SNAPSHOT_FILE = "input_snapshot.json"
MONTH_MANIFEST_FILE = "month_manifest.json"
TRIAL_RUNS_DIR = "trial_runs"

STATE_NOT_STARTED = "未开始"
STATE_PENDING_CHECK = "待检查"
STATE_CHECK_FAILED = "检查未通过"
STATE_READY = "待确认"
STATE_LOCKED = "已锁定"


class MonthlySettlementError(SettlementServiceError):
    """月度结算动作拒绝（display kind + reason）。"""


def _month_dir(project_dir: Path, month: int) -> Path:
    if not isinstance(month, int) or month < 1 or month > 12:
        raise MonthlySettlementError("invalid_input", f"月份必须为 1—12（实际 {month!r}）")
    return project_dir / SETTLEMENT_DIR / MONTHS_DIR / f"m{month:02d}"


def _read_manifest(month_dir: Path) -> dict[str, Any] | None:
    path = month_dir / MONTH_MANIFEST_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise MonthlySettlementError("unavailable", f"月度清单不可读：{exc}") from None


def _write_manifest(month_dir: Path, manifest: dict[str, Any]) -> None:
    atomic_write_bytes(
        month_dir / MONTH_MANIFEST_FILE, stable_dump(manifest), immutable=False
    )


def _state_of(month_dir: Path) -> str:
    manifest = _read_manifest(month_dir)
    return str(manifest.get("state")) if manifest else STATE_NOT_STARTED


def _read_input(month_dir: Path) -> dict[str, Any] | None:
    path = month_dir / INPUT_SNAPSHOT_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_months(workspace_root: Path | str, project_ref: str) -> list[dict[str, Any]]:
    """12 月状态一览（工作台网格数据源；未开始月如实未开始）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    out = []
    for month in range(1, 13):
        mdir = project_dir / SETTLEMENT_DIR / MONTHS_DIR / f"m{month:02d}"
        manifest = _read_manifest(mdir) if mdir.exists() else None
        out.append(
            {
                "month": month,
                "state": str(manifest.get("state")) if manifest else STATE_NOT_STARTED,
                "updated_at": manifest.get("updated_at") if manifest else None,
            }
        )
    return out


def _validate_entries(entries: Any) -> list[dict[str, Any]]:
    """填报条目结构校验（字段级，R-ST-07/08 的前置形状门）。"""
    if not isinstance(entries, list) or not entries:
        raise MonthlySettlementError("invalid_input", "月度填报必须为非空条目列表")
    clean = []
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, dict):
            raise MonthlySettlementError("invalid_input", "填报条目必须为对象")
        sid = str(e.get("subject_id") or "").strip()
        if not sid:
            raise MonthlySettlementError("invalid_input", "条目缺主体标识")
        if sid in seen:
            raise MonthlySettlementError("invalid_input", f"主体重复填报：{sid}")
        seen.add(sid)
        clean.append(e)
    return clean


def _require_not_published(project_dir: Path) -> None:
    """S3 发布后整项目只读：月度填报/检查/试算/锁定一律拒绝。"""
    if (project_dir / SETTLEMENT_DIR / "annual_summary.json").exists():
        raise MonthlySettlementError(
            "request_conflict",
            "年度汇总已发布，项目进入只读（月度动作全部关闭）；纠错请复制为新年度项目",
        )


def write_month_input(
    *,
    workspace_root: Path | str,
    project_ref: str,
    month: int,
    entries: Any,
) -> dict[str, Any]:
    """月度填报：写 input_snapshot＋状态推进（任何填报变化→待检查，
    既有检查结论失效）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    _require_not_published(project_dir)
    month_dir = _month_dir(project_dir, month)
    clean = _validate_entries(entries)
    state = _state_of(month_dir)
    if state in ("已锁定", "已锁定·有调整"):
        raise MonthlySettlementError(
            "request_conflict",
            f"{month} 月已锁定，修改须走调整（S4）；填报写入被拒绝",
        )
    month_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "month_input_schema_version": "1.0",
        "month": month,
        "entries": clean,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    atomic_write_bytes(
        month_dir / INPUT_SNAPSHOT_FILE, stable_dump(snapshot), immutable=False
    )
    _write_manifest(
        month_dir,
        {
            "month": month,
            "state": STATE_PENDING_CHECK,
            "input_sha256": hash_payload(snapshot),
            "last_check": None,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    return {"month": month, "state": STATE_PENDING_CHECK, "entries": len(clean)}


def check_month(
    *, workspace_root: Path | str, project_ref: str, month: int
) -> dict[str, Any]:
    """R-ST-01..08 聚合检查（零副作用读＋状态推进到 待确认/检查未通过）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    _require_not_published(project_dir)
    month_dir = _month_dir(project_dir, month)
    snapshot = _read_input(month_dir)
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if snapshot is None:
        return {
            "month": month,
            "state": STATE_NOT_STARTED,
            "blockers": [{"rule": "R-ST-00", "message": "该月尚未填报"}],
            "warnings": [],
        }
    entries = snapshot["entries"]

    # R-ST-01 价表已录且附来源
    price = read_price_table(workspace_root, project_ref)
    current_price = price.get("current")
    if current_price is None:
        blockers.append({"rule": "R-ST-01", "message": "补偿价表未录入"})
    elif not str(current_price.get("policy_source") or "").strip():
        blockers.append({"rule": "R-ST-01", "message": "补偿价表缺政策来源"})

    # R-ST-03 台账非空且容量 ≥0；R-ST-08 容量/出力非负
    subjects = read_subjects(workspace_root, project_ref)
    current_subjects = (subjects.get("current") or {}).get("subjects") or []
    if not current_subjects:
        blockers.append({"rule": "R-ST-03", "message": "主体台账为空"})
    ledger_ids = {s["subject_id"] for s in current_subjects}
    for e in entries:
        sid = str(e.get("subject_id"))
        if sid not in ledger_ids:
            blockers.append(
                {"rule": "R-ST-03", "message": f"主体 {sid} 不在台账当前版本"}
            )
        for key in ("declared_capacity_mw", "expected_output_mw", "actual_output_mw"):
            value = e.get(key)
            if value is None:
                blockers.append(
                    {"rule": "R-ST-07", "message": f"主体 {sid} 缺字段 {key}（空白≠0）"}
                )
            elif not isinstance(value, (int, float)) or value < 0:
                blockers.append(
                    {"rule": "R-ST-08", "message": f"主体 {sid} 的 {key} 必须 ≥0"}
                )
        ratio = e.get("reliability_ratio")
        if ratio is None:
            blockers.append(
                {"rule": "R-ST-07", "message": f"主体 {sid} 缺可靠性系数 reliability_ratio"}
            )
        elif not isinstance(ratio, (int, float)) or ratio < 0 or ratio > 1:
            blockers.append(
                {"rule": "R-ST-08", "message": f"主体 {sid} 可靠性系数必须在 0—1"}
            )
        avail = e.get("availability_factor")
        if avail is None:
            blockers.append(
                {"rule": "R-ST-07", "message": f"主体 {sid} 缺可获得率 availability_factor"}
            )
        elif not isinstance(avail, (int, float)) or avail < 0 or avail > 1:
            blockers.append(
                {"rule": "R-ST-08", "message": f"主体 {sid} 可获得率必须在 0—1"}
            )
        # R-ST-04 缺口与可获得率派生一致：实际≤应出力时可获得率应 ≈ 实际/应出力
        exp = e.get("expected_output_mw")
        act = e.get("actual_output_mw")
        if isinstance(exp, (int, float)) and isinstance(act, (int, float)) and exp > 0:
            derived = min(1.0, act / exp)
            if isinstance(avail, (int, float)) and abs(avail - derived) > 1e-6:
                blockers.append(
                    {"rule": "R-ST-04",
                     "message": f"主体 {sid} 可获得率 {avail} 与派生值 {round(derived,6)} 不一致"},
                )
        # R-ST-05 人工调整非 0 必附依据
        adj = e.get("manual_adjustment") or 0.0
        if adj != 0 and not str(e.get("adjustment_reason") or "").strip():
            blockers.append(
                {"rule": "R-ST-05", "message": f"主体 {sid} 人工调整非 0 但未附依据"}
            )

    # R-ST-02/06 引用有效＋系数为冻结正式结果
    refs = list_references(workspace_root, project_ref)
    coef_refs = [r for r in refs if r.get("kind") == "coefficient"]
    if not coef_refs:
        blockers.append({"rule": "R-ST-06", "message": "未挂供需系数引用（须为已冻结正式结果）"})
    else:
        config = read_year_config(project_dir) or {}
        for r in coef_refs:
            if not r.get("formula_version"):
                blockers.append(
                    {"rule": "R-ST-06", "message": "系数引用缺 formula_version（须为 S0 冻结结果）"}
                )
            from capacity_credit.application.capacity_coefficient_freeze_service import (
                read_coefficient_freeze,
            )
            src = read_coefficient_freeze(Path(workspace_root) / str(r.get("project_ref")))
            if src is None or src.get("invalid"):
                blockers.append(
                    {"rule": "R-ST-02", "message": "系数引用目标不可读或已损坏"}
                )
            elif src["manifest"].get("target_year") != config.get("settlement_year"):
                blockers.append(
                    {"rule": "R-ST-02",
                     "message": f"系数引用年份 {src['manifest'].get('target_year')} 与结算年度 {config.get('settlement_year')} 不一致"}
                )

    state = STATE_READY if not blockers else STATE_CHECK_FAILED
    _write_manifest(
        month_dir,
        {
            "month": month,
            "state": state,
            "input_sha256": hash_payload(snapshot),
            "last_check": {
                "at": datetime.now(UTC).isoformat(),
                "blockers": len(blockers),
                "warnings": len(warnings),
            },
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    return {
        "month": month,
        "state": state,
        "blockers": blockers,
        "warnings": warnings,
    }


def trial_month(
    *, workspace_root: Path | str, project_ref: str, month: int
) -> dict[str, Any]:
    """试算（公式 v1）：技术预览——留痕 trial_runs、可多次、不改状态、
    不产生正式金额。前置：最近检查通过（待确认）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    _require_not_published(project_dir)
    month_dir = _month_dir(project_dir, month)
    if _state_of(month_dir) != STATE_READY:
        raise MonthlySettlementError(
            "request_conflict",
            f"{month} 月当前状态＝{_state_of(month_dir)}，须检查通过（待确认）后才可试算",
        )
    snapshot = _read_input(month_dir)
    if snapshot is None:
        raise MonthlySettlementError("invalid_input", "该月尚未填报")
    price = read_price_table(workspace_root, project_ref).get("current")
    refs = list_references(workspace_root, project_ref)
    coef = next((r for r in refs if r.get("kind") == "coefficient"), None)
    if price is None or coef is None:
        raise MonthlySettlementError(
            "invalid_input", "试算前置缺失：价表或系数引用不在（请重新检查）"
        )
    base_standard = float(price["compensation_standard"])
    coefficient = float(coef["target"]["coefficient"])
    ledger = {s["subject_id"]: s for s in (read_subjects(workspace_root, project_ref).get("current") or {}).get("subjects", [])}

    rows: list[dict[str, Any]] = []
    for e in snapshot["entries"]:
        sid = e["subject_id"]
        rtype = (ledger.get(sid) or {}).get("resource_type") or e.get(
            "resource_type"
        )
        standard, standard_source = standard_for_resource_type(price, rtype)
        declared = float(e["declared_capacity_mw"])
        ratio = float(e["reliability_ratio"])
        avail = float(e["availability_factor"])
        due = declared * 1000.0 * ratio * standard / 12.0 * coefficient * avail
        gap = max(0.0, float(e["expected_output_mw"]) - float(e["actual_output_mw"]))
        deduction_raw = gap * float(e.get("deduction_price_per_mwh") or 0.0) * float(e.get("unmet_hours") or 0.0)
        cap = e.get("deduction_cap")
        deduction = min(deduction_raw, float(cap)) if cap is not None else deduction_raw
        adjustment = float(e.get("manual_adjustment") or 0.0)
        paid = due - deduction + adjustment
        rows.append(
            {
                "subject_id": sid,
                "resource_type": (ledger.get(sid) or {}).get("resource_type"),
                "declared_capacity_mw": declared,
                "reliability_ratio": ratio,
                "availability_factor": avail,
                "standard_used": standard,
                "standard_source": standard_source,
                "due_amount": round(due, 2),
                "gap_mw": round(gap, 6),
                "deduction": round(deduction, 2),
                "manual_adjustment": adjustment,
                "paid": round(paid, 2),
            }
        )
    total_paid = round(sum(r["paid"] for r in rows), 2)
    trial_ref = uuid.uuid4().hex
    trial_dir = month_dir / TRIAL_RUNS_DIR / trial_ref
    trial_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        trial_dir / "input_snapshot.json", stable_dump(snapshot), immutable=True
    )
    result = {
        "trial": True,
        "trial_ref": trial_ref,
        "month": month,
        "formula": "应补偿=申报容量×1000×可靠容量系数×补偿标准÷12×供需系数×可获得率；实付=应补偿−扣减+调整",
        "compensation_standard": base_standard,
        "coefficient": coefficient,
        "coefficient_formula_version": coef.get("formula_version"),
        "price_table_version": price.get("version"),
        "subjects": rows,
        "total_paid": total_paid,
        "computed_at": datetime.now(UTC).isoformat(),
    }
    atomic_write_bytes(trial_dir / "result.json", stable_dump(result), immutable=True)
    atomic_write_bytes(
        trial_dir / "manifest.json",
        stable_dump(
            {
                "trial_ref": trial_ref,
                "month": month,
                "input_sha256": hash_payload(snapshot),
                "result_sha256": hash_payload(result),
                "created_at": result["computed_at"],
            }
        ),
        immutable=True,
    )
    return result


def lock_month(
    *, workspace_root: Path | str, project_ref: str, month: int, locked_by: str
) -> dict[str, Any]:
    """确认锁定（S2-M2，显式用户动作）：前置＝待确认（检查通过；跳步
    锁定拒绝）；记录锁人/时间/输入哈希；重复锁定诚实拒绝（一个月份
    一个锁定）；已锁定月直改填报与试算均被既有门拒绝。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    _require_not_published(project_dir)
    month_dir = _month_dir(project_dir, month)
    if not str(locked_by or "").strip():
        raise MonthlySettlementError(
            "invalid_input", "锁定是显式用户动作：锁定人必填（留痕追溯）"
        )
    state = _state_of(month_dir)
    if state == STATE_LOCKED:
        raise MonthlySettlementError(
            "request_conflict",
            f"{month} 月已锁定（正式结果不可替换）；纠错须走调整（S4）",
        )
    if state != STATE_READY:
        raise MonthlySettlementError(
            "request_conflict",
            f"{month} 月当前状态＝{state}，须检查通过（待确认）后才可锁定",
        )
    manifest = _read_manifest(month_dir) or {}
    locked_at = datetime.now(UTC).isoformat()
    manifest.update(
        {
            "state": STATE_LOCKED,
            "locked_by": locked_by.strip(),
            "locked_at": locked_at,
            "updated_at": locked_at,
        }
    )
    _write_manifest(month_dir, manifest)
    return {
        "month": month,
        "state": STATE_LOCKED,
        "locked_by": locked_by.strip(),
        "locked_at": locked_at,
        "input_sha256": manifest.get("input_sha256"),
    }


def adjust_month(
    *,
    workspace_root: Path | str,
    project_ref: str,
    month: int,
    subject_id: str,
    delta_mw: Any,
    delta_amount: Any,
    reason: str,
    adjuster: str,
) -> dict[str, Any]:
    """锁定后调整（S4，设计 §3.2）：追加式（永不覆盖）；原因/依据与
    调整人必填；差额有符号；状态转「已锁定·有调整」；当月有效值＝
    锁定值＋调整差额链（年度汇总自动并入）。发布后只读拒绝。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    _require_not_published(project_dir)
    month_dir = _month_dir(project_dir, month)
    state = _state_of(month_dir)
    if state not in (STATE_LOCKED, "已锁定·有调整"):
        raise MonthlySettlementError(
            "request_conflict",
            f"{month} 月当前状态＝{state}，仅已锁定月份可调整",
        )
    sid = str(subject_id or "").strip()
    if not sid:
        raise MonthlySettlementError("invalid_input", "调整须指定主体标识")
    if not str(reason or "").strip():
        raise MonthlySettlementError("invalid_input", "调整原因必填（留痕追溯）")
    if not str(adjuster or "").strip():
        raise MonthlySettlementError("invalid_input", "调整人必填（留痕追溯）")
    try:
        dm = float(delta_mw)
        da = float(delta_amount)
    except (TypeError, ValueError):
        raise MonthlySettlementError("invalid_input", "差额必须为数值（有符号）") from None

    path = month_dir / "adjustments.json"
    records: list[dict[str, Any]] = []
    if path.exists():
        records = json.loads(path.read_text(encoding="utf-8"))
    record = {
        "sequence": len(records) + 1,
        "subject_id": sid,
        "delta_mw": dm,
        "delta_amount": da,
        "reason": str(reason).strip(),
        "adjuster": str(adjuster).strip(),
        "adjusted_at": datetime.now(UTC).isoformat(),
    }
    records.append(record)
    atomic_write_bytes(path, stable_dump(records), immutable=False)
    manifest = _read_manifest(month_dir) or {}
    manifest.update(
        {
            "state": "已锁定·有调整",
            "adjustments_sha256": hashlib.sha256(stable_dump(records)).hexdigest(),
            "updated_at": record["adjusted_at"],
        }
    )
    _write_manifest(month_dir, manifest)
    return {"month": month, "state": "已锁定·有调整", "adjustment": record}


def list_adjustments(
    workspace_root: Path | str, project_ref: str, month: int | None = None
) -> list[dict[str, Any]]:
    """调整追溯清单（时间线，追加序；month=None＝全年）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    out: list[dict[str, Any]] = []
    months = range(1, 13) if month is None else [month]
    for m in months:
        path = project_dir / SETTLEMENT_DIR / MONTHS_DIR / f"m{m:02d}" / "adjustments.json"
        if not path.exists():
            continue
        for record in json.loads(path.read_text(encoding="utf-8")):
            out.append({"month": m, **record})
    out.sort(key=lambda r: (r["month"], r["sequence"]))
    return out


__all__ = [
    "MonthlySettlementError",
    "adjust_month",
    "check_month",
    "list_adjustments",
    "list_months",
    "lock_month",
    "trial_month",
    "write_month_input",
]
