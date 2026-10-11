"""Settlement business report export (Step 3 S3 收尾①, 2026-10-09).

The BUSINESS projection of one PUBLISHED annual settlement summary as
an Excel workbook, published through the SAME discipline as the cost
report chain (build fully in memory, immutable files, manifest written
LAST, same-identity replay by hash comparison). The report is a
projection of the immutable annual_summary record ONLY - it never
recomputes settlement amounts and never reads the project's latest
month inputs.

This module does NOT touch the MethodResult report chain
(export_service/report_model) or the cost report (cost_report); the
settlement domain has its own template blocks. Settlement tables stay
separate by domain.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MANIFEST_FILE = "settlement_report_manifest.json"
_REPORT_XLSX = "settlement_report.xlsx"
_REPORT_HTML = "settlement_report.html"
_GENERATOR = "settlement-report-1.0"


class SettlementReportError(ValueError):
    """A settlement-report export refusal (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")


def _fmt(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}f}"
    return str(value)


def _excel_bytes(*, annual: dict[str, Any]) -> bytes:
    """Build the settlement workbook fully in memory (openpyxl; Excel
    itself is never required on the machine)."""
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    bold = Font(bold=True)

    # ① 对象与结论
    ws = wb.active
    ws.title = "对象与结论"
    ws["B2"] = "年度容量补偿结算报告（正式结果业务投影）"
    ws["B2"].font = bold
    rows = [
        ("结算年度", annual.get("settlement_year")),
        ("省份或区域", annual.get("region")),
        ("发布人", annual.get("published_by")),
        ("发布时间", annual.get("published_at")),
        ("公式版本", annual.get("formula_version")),
        ("补偿标准（元/kW·年）", annual.get("compensation_standard")),
        ("政策来源", annual.get("policy_source")),
        ("供需系数", annual.get("coefficient")),
        ("系数公式版本", annual.get("coefficient_formula_version")),
        ("锁定月份数", annual.get("months_locked")),
        ("年度总实付（元）", annual.get("total_paid")),
        ("年度扣减合计（元）", annual.get("total_deduction")),
    ]
    for i, (label, value) in enumerate(rows, start=4):
        ws.cell(row=i, column=2, value=label).font = bold
        ws.cell(
            row=i,
            column=3,
            value=_fmt(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value),
        )

    # ② 月度汇总
    ws2 = wb.create_sheet("月度汇总")
    ws2["B2"] = "12 个月实付与扣减（元；来源＝锁定月填报快照重算）"
    ws2["B2"].font = bold
    headers = ["月份", "实付（元）", "扣减（元）", "输入哈希（SHA-256前16位）"]
    for col, header in enumerate(headers, start=2):
        ws2.cell(row=4, column=col, value=header).font = bold
    for i, row in enumerate(annual.get("per_month") or [], start=5):
        ws2.cell(row=i, column=2, value=row.get("month"))
        ws2.cell(row=i, column=3, value=_fmt(row.get("paid")))
        ws2.cell(row=i, column=4, value=_fmt(row.get("deduction")))
        ws2.cell(row=i, column=5, value=str(row.get("input_sha256") or "")[:16])

    # ③ 分主体汇总
    ws3 = wb.create_sheet("分主体汇总")
    ws3["B2"] = "分主体年度实付、扣减与月份数（元）"
    ws3["B2"].font = bold
    headers = ["主体标识", "年度实付（元）", "年度扣减（元）", "月份数"]
    for col, header in enumerate(headers, start=2):
        ws3.cell(row=4, column=col, value=header).font = bold
    for i, row in enumerate(annual.get("per_subject") or [], start=5):
        ws3.cell(row=i, column=2, value=str(row.get("subject_id")))
        ws3.cell(row=i, column=3, value=_fmt(row.get("total_paid")))
        ws3.cell(row=i, column=4, value=_fmt(row.get("total_deduction")))
        ws3.cell(row=i, column=5, value=row.get("months"))

    # ④ 分资源类型汇总
    ws4 = wb.create_sheet("分资源类型汇总")
    ws4["B2"] = "按主体资源类型聚合（元）"
    ws4["B2"].font = bold
    headers = ["资源类型", "年度实付（元）", "主体数"]
    for col, header in enumerate(headers, start=2):
        ws4.cell(row=4, column=col, value=header).font = bold
    for i, row in enumerate(annual.get("per_resource") or [], start=5):
        ws4.cell(row=i, column=2, value=str(row.get("resource_type")))
        ws4.cell(row=i, column=3, value=_fmt(row.get("total_paid")))
        ws4.cell(row=i, column=4, value=row.get("subjects"))

    # ⑤ 调整追溯
    ws5 = wb.create_sheet("调整追溯")
    ws5["B2"] = "全部调整记录（追加式时间线，历史永不覆盖）"
    ws5["B2"].font = bold
    headers = [
        "月份", "序号", "主体标识", "容量差额（MW）", "金额差额（元）",
        "原因", "调整人", "调整时间",
    ]
    for col, header in enumerate(headers, start=2):
        ws5.cell(row=4, column=col, value=header).font = bold
    for i, row in enumerate(annual.get("adjustments") or [], start=5):
        ws5.cell(row=i, column=2, value=row.get("month"))
        ws5.cell(row=i, column=3, value=row.get("sequence"))
        ws5.cell(row=i, column=4, value=str(row.get("subject_id")))
        ws5.cell(row=i, column=5, value=_fmt(row.get("delta_mw")))
        ws5.cell(row=i, column=6, value=_fmt(row.get("delta_amount")))
        ws5.cell(row=i, column=7, value=str(row.get("reason")))
        ws5.cell(row=i, column=8, value=str(row.get("adjuster")))
        ws5.cell(row=i, column=9, value=str(row.get("adjusted_at")))

    # ⑥ 引用与输入哈希（追溯）
    ws6 = wb.create_sheet("引用与输入哈希")
    ws6["B2"] = "引用清单与 12 月输入哈希（追溯；上游结果只读绑定）"
    ws6["B2"].font = bold
    headers = ["类型", "项目", "运行/冻结引用", "公式版本"]
    for col, header in enumerate(headers, start=2):
        ws6.cell(row=4, column=col, value=header).font = bold
    cursor = 5
    for row in annual.get("references") or []:
        ws6.cell(row=cursor, column=2, value=str(row.get("kind")))
        ws6.cell(row=cursor, column=3, value=str(row.get("project_ref")))
        ws6.cell(
            row=cursor,
            column=4,
            value=str(row.get("run_ref") or row.get("freeze_ref") or ""),
        )
        ws6.cell(row=cursor, column=5, value=str(row.get("formula_version")))
        cursor += 1
    cursor += 1
    ws6.cell(row=cursor, column=2, value="月份").font = bold
    ws6.cell(row=cursor, column=3, value="输入哈希（SHA-256前16位）").font = bold
    ws6.cell(row=cursor, column=4, value="锁定人").font = bold
    ws6.cell(row=cursor, column=5, value="锁定时间").font = bold
    for i, row in enumerate(annual.get("input_hashes") or [], start=cursor + 1):
        ws6.cell(row=i, column=2, value=row.get("month"))
        ws6.cell(row=i, column=3, value=str(row.get("input_sha256") or "")[:16])
        ws6.cell(row=i, column=4, value=str(row.get("locked_by")))
        ws6.cell(row=i, column=5, value=str(row.get("locked_at")))

    # ⑦ 披露
    ws7 = wb.create_sheet("披露")
    ws7["B2"] = "口径与边界（如实披露）"
    ws7["B2"].font = bold
    notes = [
        "本报告是已发布年度汇总（不可变记录）的业务投影：不重算金额、不修改机器结果。",
        "月度金额与年度汇总同源：锁定月填报快照按公式 v1 重算（应补偿=申报容量×1000×可靠容量系数×补偿标准÷12×供需系数×可获得率；实付=应补偿−扣减+调整）。",
        "试算为技术预览，不是正式金额；正式金额以锁定月与年度汇总为准。",
        "补偿标准与政策来源为录入制：任何示例价格不得作为正式默认值。",
        "纠错：发布后项目只读，须复制为新年度项目重新结算。",
    ]
    for i, note in enumerate(notes, start=4):
        ws7.cell(row=i, column=2, value=note)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _html_bytes(
    *, annual: dict[str, Any], excel_name: str, excel_sha: str
) -> bytes:
    """A minimal business HTML reading view (same identity facts; the
    Excel workbook is the authoritative artifact)."""
    year = annual.get("settlement_year")
    kv_rows = [
        ("结算年度", year),
        ("省份或区域", annual.get("region")),
        ("年度总实付（元）", _fmt(annual.get("total_paid"))),
        ("年度扣减合计（元）", _fmt(annual.get("total_deduction"))),
        ("补偿标准（元/kW·年）", _fmt(annual.get("compensation_standard"))),
        ("政策来源", annual.get("policy_source")),
        ("供需系数", _fmt(annual.get("coefficient"), 6)),
        ("公式版本", annual.get("formula_version")),
        ("发布人", annual.get("published_by")),
        ("发布时间", annual.get("published_at")),
    ]
    month_rows = "".join(
        f"<tr><td>{m.get('month')}月</td>"
        f"<td class='r'>{_fmt(m.get('paid'))}</td>"
        f"<td class='r'>{_fmt(m.get('deduction'))}</td></tr>"
        for m in annual.get("per_month") or []
    )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{year}年年度容量补偿结算报告</title></head>
<body>
<h1>{year}年年度容量补偿结算报告</h1>
<p>权威工件：{excel_name}（SHA-256：{excel_sha[:16]}…）；本页为业务阅读版，不替代工件。</p>
<h2>结论</h2>
<table border="1" cellpadding="4">{''.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k, v in kv_rows)}</table>
<h2>月度汇总</h2>
<table border="1" cellpadding="4"><tr><th>月份</th><th>实付（元）</th><th>扣减（元）</th></tr>{month_rows}</table>
<p>本报告为结算域业务投影：不修改机器结果，不重算正式金额；来源＝已发布年度汇总（一次性不可变）。</p>
</body></html>
"""
    return html.encode("utf-8")


def export_settlement_report(
    *, annual: dict[str, Any], output_dir: Path | str
) -> dict[str, Any]:
    """Publish the settlement report set (Excel + HTML + manifest last)
    for one PUBLISHED annual summary. An existing manifest replays ONLY
    when the whole set still matches every recorded hash; a different
    set under the same identity is a hard refusal (never an overwrite)."""
    output = Path(output_dir)
    annual_ref = f"annual-{annual.get('settlement_year')}"
    if not annual.get("settlement_year"):
        raise SettlementReportError(
            "SETTLEMENT_REPORT_INPUT_INVALID", "年度汇总缺结算年度"
        )
    if not annual.get("published"):
        raise SettlementReportError(
            "SETTLEMENT_REPORT_INPUT_INVALID",
            "年度汇总未发布，不能导出正式报告",
        )
    output.mkdir(parents=True, exist_ok=True)
    xlsx = _excel_bytes(annual=annual)
    xlsx_sha = _sha256(xlsx)
    html = _html_bytes(
        annual=annual, excel_name=_REPORT_XLSX, excel_sha=xlsx_sha
    )
    entries = [
        {
            "name": _REPORT_XLSX,
            "sha256": xlsx_sha,
            "size": len(xlsx),
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        {
            "name": _REPORT_HTML,
            "sha256": _sha256(html),
            "size": len(html),
            "content_type": "text/html; charset=utf-8",
        },
    ]
    result_sha = _sha256(_canonical(annual))
    report_manifest = {
        "settlement_report_schema_version": "1.0",
        "generator": _GENERATOR,
        "annual_ref": annual_ref,
        "settlement_year": annual.get("settlement_year"),
        "formula_version": annual.get("formula_version"),
        "result_sha256": result_sha,
        "files": entries,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    manifest_bytes = _canonical(report_manifest)

    existing = output / _MANIFEST_FILE
    if existing.is_file():
        try:
            previous = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SettlementReportError(
                "SETTLEMENT_REPORT_EXISTS_DIFFERENT",
                f"既有结算报告清单不可读取：{exc}",
            ) from exc
        if previous.get("files") == report_manifest["files"] and previous.get(
            "result_sha256"
        ) == report_manifest["result_sha256"]:
            return {
                "replayed": True,
                "annual_ref": annual_ref,
                "output_dir": str(output),
                "files": [entry["name"] for entry in entries],
            }
        raise SettlementReportError(
            "SETTLEMENT_REPORT_EXISTS_DIFFERENT",
            "同身份目录已存在不同的结算报告集；不覆盖既有报告",
        )
    for entry in entries:
        name = entry["name"]
        if (output / name).exists():
            raise SettlementReportError(
                "SETTLEMENT_REPORT_EXISTS_DIFFERENT", f"报告文件已存在：{name}"
            )
    from capacity_credit.application.atomic_io import atomic_write_bytes

    for entry in entries:
        name = entry["name"]
        data = xlsx if name == _REPORT_XLSX else html
        atomic_write_bytes(output / name, data, immutable=True)
    # manifest LAST: a partial failure never leaves a complete-looking set
    atomic_write_bytes(existing, manifest_bytes + b"\n", immutable=True)
    return {
        "replayed": False,
        "annual_ref": annual_ref,
        "output_dir": str(output),
        "files": [entry["name"] for entry in entries],
    }


__all__ = ["SettlementReportError", "export_settlement_report"]
