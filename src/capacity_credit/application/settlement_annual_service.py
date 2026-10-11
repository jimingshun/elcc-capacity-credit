"""年度汇总发布（Step 3 S3；主责设计 §3.3/§7.2）.

12/12 全锁定（允许含调整版本）→ 发布 annual_summary（一次性不可变：
12 月输入哈希＋引用清单＋分主体/分资源/扣减汇总＋formula_version）→
项目 completed（formal_result 绑定）→ 只读。

金额计算与 S2 试算同一公式（应补偿=申报×1000×可靠容量系数×标准÷12×
系数×可获得率；实付=应补偿−扣减+调整），但来源是**锁定月的填报快照**
（不是试算记录——试算是技术预览，年度汇总从锁定输入重算，确保正式
金额与锁定输入严格同源）。发布后不重发布；纠错走新年度项目。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    hash_payload,
    stable_dump,
)
from capacity_credit.application.settlement_monthly_service import (
    MONTHS_DIR,
    MONTH_MANIFEST_FILE,
    INPUT_SNAPSHOT_FILE,
    STATE_LOCKED,
)
from capacity_credit.application.settlement_service import (
    SETTLEMENT_DIR,
    SettlementServiceError,
    _require_settlement_project,
    list_references,
    read_price_table,
    read_subjects,
    read_year_config,
    standard_for_resource_type,
)

ANNUAL_SUMMARY_FILE = "annual_summary.json"


def annual_report_export_dir(
    workspace_root: Path | str, project_ref: str, settlement_year: Any
) -> Path:
    """报告导出目录（与成本域 reports/cost/... 同款约定；唯一路径
    权威在这里，路由与下载都不各自拼路径）。"""
    return (
        Path(workspace_root)
        / "reports"
        / "settlement"
        / project_ref
        / f"annual-{settlement_year}"
        / "export"
    )


def export_annual_report(
    *, workspace_root: Path | str, project_ref: str
) -> dict[str, Any]:
    """导出年度汇总报告（Excel/HTML，报告服务同款纪律）。前置＝已发布
    （报告是正式结果的业务投影；未发布不出报告）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    record = read_annual_summary_record(project_dir)
    if record is None:
        raise AnnualSummaryError(
            "invalid_input", "年度汇总尚未发布，不能导出报告"
        )
    if record.get("invalid"):
        raise AnnualSummaryError(
            "invalid_input", f"年度汇总记录不可读取：{record.get('invalid_reason')}"
        )
    annual = record["annual"]
    from capacity_credit.reporting.settlement_report import (
        SettlementReportError,
        export_settlement_report,
    )

    output = annual_report_export_dir(
        workspace_root, project_ref, annual.get("settlement_year")
    )
    try:
        return export_settlement_report(annual=annual, output_dir=output)
    except SettlementReportError as exc:
        raise AnnualSummaryError(exc.code, exc.message) from None


def read_annual_report_downloads(
    *, workspace_root: Path | str, project_ref: str
) -> dict[str, Any]:
    """校验过的报告下载集（哈希对清单逐个复核；不触发导出）。"""
    import hashlib

    project_dir = _require_settlement_project(workspace_root, project_ref)
    record = read_annual_summary_record(project_dir)
    if record is None or record.get("invalid"):
        return {"available": False, "reason": "年度汇总尚未发布", "files": []}
    annual = record["annual"]
    export_dir = annual_report_export_dir(
        workspace_root, project_ref, annual.get("settlement_year")
    )
    manifest_path = export_dir / "settlement_report_manifest.json"
    if not manifest_path.is_file():
        return {"available": False, "reason": "尚未导出报告", "files": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = []
        for entry in manifest.get("files", []):
            name = str(entry.get("name", ""))
            data = (export_dir / name).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != entry.get("sha256"):
                raise ValueError(f"文件哈希不一致：{name}")
            files.append(
                {
                    "name": name,
                    "bytes": data,
                    "sha256": digest,
                    "content_type": str(
                        entry.get("content_type", "application/octet-stream")
                    ),
                }
            )
    except (OSError, ValueError) as exc:
        return {"available": False, "reason": str(exc), "files": []}
    return {"available": True, "reason": None, "files": files}


class AnnualSummaryError(SettlementServiceError):
    """年度汇总动作拒绝（display kind + reason）。"""


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _month_amounts(entry: dict[str, Any], standard: float, coefficient: float) -> dict[str, Any]:
    """与 S2 试算同一公式（确定性重算，来源＝锁定月的填报快照）。"""
    declared = float(entry["declared_capacity_mw"])
    ratio = float(entry["reliability_ratio"])
    avail = float(entry["availability_factor"])
    due = declared * 1000.0 * ratio * standard / 12.0 * coefficient * avail
    gap = max(0.0, float(entry["expected_output_mw"]) - float(entry["actual_output_mw"]))
    deduction_raw = gap * float(entry.get("deduction_price_per_mwh") or 0.0) * float(entry.get("unmet_hours") or 0.0)
    cap = entry.get("deduction_cap")
    deduction = min(deduction_raw, float(cap)) if cap is not None else deduction_raw
    adjustment = float(entry.get("manual_adjustment") or 0.0)
    paid = due - deduction + adjustment
    return {
        "due_amount": round(due, 2),
        "deduction": round(deduction, 2),
        "manual_adjustment": adjustment,
        "paid": round(paid, 2),
    }


def _collect_locked(project_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """逐月收集已锁定月的快照与金额事实；返回 (months, unlocked_labels)。"""
    months: list[dict[str, Any]] = []
    unlocked: list[str] = []
    for m in range(1, 13):
        mdir = project_dir / SETTLEMENT_DIR / MONTHS_DIR / f"m{m:02d}"
        manifest = _read_json(mdir / MONTH_MANIFEST_FILE)
        state = str(manifest.get("state")) if manifest else "未开始"
        if state not in (STATE_LOCKED, "已锁定·有调整"):
            unlocked.append(f"{m}月")
            continue
        snapshot = _read_json(mdir / INPUT_SNAPSHOT_FILE)
        months.append(
            {
                "month": m,
                "state": state,
                "input_sha256": manifest.get("input_sha256"),
                "locked_by": manifest.get("locked_by"),
                "locked_at": manifest.get("locked_at"),
                "entries": (snapshot or {}).get("entries") or [],
                "adjustments": _read_json(mdir / "adjustments.json") or [],
            }
        )
    return months, unlocked


def _aggregate(
    months: list[dict[str, Any]],
    price: dict[str, Any],
    coefficient: float,
    ledger: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    per_month: list[dict[str, Any]] = []
    per_subject: dict[str, dict[str, Any]] = {}
    per_resource: dict[str, dict[str, Any]] = {}
    resource_of: dict[str, str] = {}
    adjustments_all: list[dict[str, Any]] = []
    for m in months:
        month_paid = 0.0
        month_deduction = 0.0
        for adj in m.get("adjustments") or []:
            row = {"month": m["month"], **adj}
            adjustments_all.append(row)
            month_paid = round(month_paid + float(adj.get("delta_amount") or 0.0), 2)
        for e in m["entries"]:
            sid = e["subject_id"]
            rtype = (ledger.get(sid) or {}).get("resource_type") or e.get(
                "resource_type"
            )
            standard, standard_source = standard_for_resource_type(price, rtype)
            amounts = _month_amounts(e, standard, coefficient)
            month_paid += amounts["paid"]
            month_deduction += amounts["deduction"]
            slot = per_subject.setdefault(sid, {"subject_id": sid, "total_paid": 0.0, "total_deduction": 0.0, "months": 0})
            slot["total_paid"] = round(slot["total_paid"] + amounts["paid"], 2)
            slot["total_deduction"] = round(slot["total_deduction"] + amounts["deduction"], 2)
            slot["months"] += 1
            slot["standard_used"] = standard
            slot["standard_source"] = standard_source
            rtype_str = str(rtype or "")
            if rtype_str:
                resource_of[sid] = rtype_str
        per_month.append(
            {
                "month": m["month"],
                "paid": round(month_paid, 2),
                "deduction": round(month_deduction, 2),
                "input_sha256": m["input_sha256"],
            }
        )
    for sid, rtype in resource_of.items():
        slot = per_resource.setdefault(rtype, {"resource_type": rtype, "total_paid": 0.0, "subjects": 0})
        slot["total_paid"] = round(slot["total_paid"] + per_subject[sid]["total_paid"], 2)
        slot["subjects"] += 1
    return {
        "per_month": per_month,
        "per_subject": sorted(per_subject.values(), key=lambda r: r["subject_id"]),
        "per_resource": sorted(per_resource.values(), key=lambda r: r["resource_type"]),
        "total_paid": round(sum(m["paid"] for m in per_month), 2),
        "total_deduction": round(sum(m["deduction"] for m in per_month), 2),
        "adjustments": adjustments_all,
    }


def read_annual_summary_record(project_dir: Path | str) -> dict[str, Any] | None:
    """读年度汇总记录（结果索引第四通道的数据源）：无记录→None；
    不可读/形状无效→诚实损坏摘要（{"invalid": True, ...}），不装成功。"""
    path = Path(project_dir) / SETTLEMENT_DIR / ANNUAL_SUMMARY_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"invalid": True, "invalid_reason": f"年度汇总记录不可读取：{exc}"}
    if not isinstance(data, dict):
        return {"invalid": True, "invalid_reason": "年度汇总记录形状无效（不是对象）"}
    return {"invalid": False, "annual": data}


def compute_annual_summary(workspace_root: Path | str, project_ref: str) -> dict[str, Any]:
    """年度汇总：已发布→读正式结果；未发布→预览（如实非正式＋进行中计数）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    annual_path = project_dir / SETTLEMENT_DIR / ANNUAL_SUMMARY_FILE
    if annual_path.exists():
        annual = _read_json(annual_path)
        return {"published": True, "annual": annual, "locked_count": 12}
    config = read_year_config(project_dir)
    price = read_price_table(workspace_root, project_ref).get("current")
    refs = list_references(workspace_root, project_ref)
    coef = next((r for r in refs if r.get("kind") == "coefficient"), None)
    months, unlocked = _collect_locked(project_dir)
    out: dict[str, Any] = {
        "published": False,
        "locked_count": len(months),
        "unlocked_months": unlocked,
        "settlement_year": (config or {}).get("settlement_year"),
    }
    if price is not None and coef is not None and not unlocked:
        coefficient = float(coef["target"]["coefficient"])
        ledger = {
            s["subject_id"]: s
            for s in (read_subjects(workspace_root, project_ref).get("current") or {}).get("subjects", [])
        }
        out["preview"] = _aggregate(months, price, coefficient, ledger)
    return out


def publish_annual_summary(
    *, workspace_root: Path | str, project_ref: str, published_by: str
) -> dict[str, Any]:
    """发布年度汇总（12/12 门＋一次性不可变＋绑定项目正式结果）。"""
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        bind_formal_result,
    )

    project_dir = _require_settlement_project(workspace_root, project_ref)
    annual_path = project_dir / SETTLEMENT_DIR / ANNUAL_SUMMARY_FILE
    if annual_path.exists():
        raise AnnualSummaryError(
            "request_conflict",
            "年度汇总已发布（一次性不可变）；纠错请复制为新年度项目",
        )
    config = read_year_config(project_dir) or {}
    price = read_price_table(workspace_root, project_ref).get("current")
    refs = list_references(workspace_root, project_ref)
    coef = next((r for r in refs if r.get("kind") == "coefficient"), None)
    months, unlocked = _collect_locked(project_dir)
    if unlocked:
        raise AnnualSummaryError(
            "request_conflict",
            f"年度汇总要求 12/12 全部锁定：当前已锁定 {len(months)}/12，"
            f"未完成月份：{'、'.join(unlocked)}",
        )
    if price is None or coef is None:
        raise AnnualSummaryError(
            "invalid_input", "发布前置缺失：价表或系数引用不在"
        )
    standard = float(price["compensation_standard"])
    coefficient = float(coef["target"]["coefficient"])
    ledger = {
        s["subject_id"]: s
        for s in (read_subjects(workspace_root, project_ref).get("current") or {}).get("subjects", [])
    }
    aggregates = _aggregate(months, price, coefficient, ledger)
    annual = {
        "annual_summary_schema_version": "1.0",
        "published": True,
        "published_by": str(published_by or "").strip(),
        "published_at": datetime.now(UTC).isoformat(),
        "formula_version": "v1",
        "settlement_year": config.get("settlement_year"),
        "region": config.get("region"),
        "compensation_standard": standard,
        "policy_source": price.get("policy_source"),
        "coefficient": coefficient,
        "coefficient_formula_version": coef.get("formula_version"),
        "months_locked": 12,
        "input_hashes": [
            {"month": m["month"], "input_sha256": m["input_sha256"],
             "locked_by": m["locked_by"], "locked_at": m["locked_at"]}
            for m in months
        ],
        "references": [
            {"kind": r.get("kind"), "project_ref": r.get("project_ref"),
             "run_ref": r.get("run_ref"), "freeze_ref": r.get("freeze_ref"),
             "formula_version": r.get("formula_version")}
            for r in refs
        ],
        **aggregates,
    }
    atomic_write_bytes(annual_path, stable_dump(annual), immutable=True)
    try:
        bind_formal_result(
            workspace_root,
            project_ref,
            domain="settlement",
            run_ref=f"annual-{config.get('settlement_year')}",
            result_sha256=hash_payload(annual),
        )
    except ProjectServiceError as exc:
        raise AnnualSummaryError(exc.kind, exc.message) from None
    return {"published": True, "annual": annual, "locked_count": 12}


__all__ = [
    "AnnualSummaryError",
    "compute_annual_summary",
    "export_annual_report",
    "publish_annual_summary",
    "read_annual_report_downloads",
    "read_annual_summary_record",
]
