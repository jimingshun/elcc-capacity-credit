"""Cost business report export (night-5 N2).

The BUSINESS projection of one cost-run record as an Excel workbook,
published through the SAME discipline as the reliability report chain
(build fully in memory, immutable files, manifest written LAST,
same-identity replay by hash comparison). The report is a projection
of the record ONLY - it never recomputes business values and never
reads the project's latest inputs.

This module does NOT touch the MethodResult report chain
(export_service/report_model); the cost domain has its own template
blocks. Cost tables and reliability tables stay separate by domain.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MANIFEST_FILE = "cost_report_manifest.json"
_REPORT_XLSX = "cost_report.xlsx"
_REPORT_HTML = "cost_report.html"
_GENERATOR = "cost-report-1.0"

_KIND_LABELS = {
    "cost_fixed": "固定成本核算",
    "cost_net_cone": "年度Net CONE测算",
    "cost_gross_cone": "完整Gross CONE测算",
}


class CostReportError(ValueError):
    """A cost-report export refusal (code + message)."""

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


def _fmt(value: Any, digits: int = 4) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}f}"
    return str(value)


def _excel_bytes(
    *,
    manifest: dict[str, Any],
    input_payload: dict[str, Any],
    result: dict[str, Any],
) -> bytes:
    """Build the cost workbook fully in memory (openpyxl; Excel itself
    is never required on the machine)."""
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    bold = Font(bold=True)

    # ① 对象与结论
    ws = wb.active
    ws.title = "对象与结论"
    kind = str(manifest.get("cost_kind", ""))
    ws["B2"] = f"{_KIND_LABELS.get(kind, kind)}业务报告"
    ws["B2"].font = bold
    rows = [
        ("运行编号", manifest.get("run_ref", "")),
        ("完成时间", manifest.get("created_at", "")),
        ("对象（项目）", manifest.get("project_ref", "")),
        ("输入快照哈希（SHA-256前16位）", str(manifest.get("input_snapshot_sha256", ""))[:16]),
        ("软件版本", str(manifest.get("software_version", ""))),
        ("源码提交", str(manifest.get("software_commit", ""))),
    ]
    for i, (label, value) in enumerate(rows, start=4):
        ws.cell(row=i, column=2, value=label).font = bold
        ws.cell(row=i, column=3, value=str(value))

    # ② 成本域结果表（固定成本）
    if result.get("calculation_kind") == "fixed_cost":
        ws2 = wb.create_sheet("造价链")
        ws2["B2"] = "造价计算链（单位：元/kW、亿元、MW）"
        ws2["B2"].font = bold
        rows2 = [
            ("修正后静态单位造价（元/kW）", result.get("unit_static_cost")),
            ("装机容量（MW）", result.get("installed_mw")),
            ("静态总投资（亿元）", result.get("static_investment_yi")),
            ("建设期利息（亿元）", result.get("idc_yi")),
            ("动态总投资（亿元）", result.get("dynamic_investment_yi")),
            ("单位动态造价（元/kW）", result.get("unit_dynamic_cost")),
            ("固定资产原值（元/kW）", result.get("fixed_asset_value_per_kw")),
            ("贷款本金（元/kW）", result.get("loan_per_kw")),
        ]
        for i, (label, value) in enumerate(rows2, start=4):
            ws2.cell(row=i, column=2, value=label).font = bold
            ws2.cell(row=i, column=3, value=_fmt(value))
        coeff = result.get("coefficients", {})
        ws2.cell(row=14, column=2, value="系数血统").font = bold
        for i, (key, label) in enumerate(
            [
                ("k_machine", "K_机型"),
                ("k_province", "K_省"),
                ("k_zone", "K_分区"),
                ("k_technology", "K_技术"),
                ("price_index", "造价指数"),
            ],
            start=15,
        ):
            ws2.cell(row=i, column=2, value=label)
            ws2.cell(row=i, column=3, value=_fmt(coeff.get(key)))

        ws3 = wb.create_sheet("首年与平均")
        ws3["B2"] = "费用构成：首年值 vs 经营期算术平均（元/kW·年，不含税）"
        ws3["B2"].font = bold
        first = result.get("first_year", {})
        avg = result.get("period_average", {})
        headers = ["费用项", "首年值", "期间平均", "首年含税等效", "平均含税等效"]
        for col, header in enumerate(headers, start=2):
            cell = ws3.cell(row=4, column=col, value=header)
            cell.font = bold
        components = [
            ("折旧费", "depreciation"),
            ("财务费（利息）", "interest"),
            ("固定运维费", "fixed_om"),
            ("保险费", "insurance"),
            ("摊销", "amortization"),
            ("合计", "total"),
        ]
        vat = 1.0
        first_total = float(first.get("total", 0) or 0)
        avg_total = float(avg.get("total", 0) or 0)
        if first_total:
            vat = float(first.get("total_with_vat", first_total) or first_total) / first_total
        for i, (label, key) in enumerate(components, start=5):
            ws3.cell(row=i, column=2, value=label)
            ws3.cell(row=i, column=3, value=_fmt(first.get(key)))
            ws3.cell(row=i, column=4, value=_fmt(avg.get(key)))
            ws3.cell(
                row=i,
                column=5,
                value=_fmt(
                    (float(first.get(key, 0) or 0)) * vat if key != "total" else first.get("total_with_vat")
                ),
            )
            ws3.cell(
                row=i,
                column=6,
                value=_fmt(
                    (float(avg.get(key, 0) or 0)) * vat if key != "total" else avg.get("total_with_vat")
                ),
            )
        note = result.get("uncovered_note")
        if note:
            ws3["B13"] = note
        leftover = result.get("leftover_investment_ratio")
        if leftover:
            ws3["B14"] = (
                f"固定资产形成率+摊销比例之外尚有 {leftover:.2%} 的投资未在本表费用中分配，"
                "须另行解释，不声称全部投资已回收。"
            )

        ws4 = wb.create_sheet("逐年明细")
        ws4["B2"] = "经营期逐年费用（元/kW·年）"
        ws4["B2"].font = bold
        headers = ["经营年", "折旧", "利息", "固定运维", "保险", "摊销", "合计"]
        for col, header in enumerate(headers, start=2):
            ws4.cell(row=4, column=col, value=header).font = bold
        for i, row in enumerate(result.get("yearly", []), start=5):
            ws4.cell(row=i, column=2, value=row.get("year"))
            for col, key in enumerate(
                ("depreciation", "interest", "fixed_om", "insurance", "amortization", "total"),
                start=3,
            ):
                ws4.cell(row=i, column=col, value=_fmt(row.get(key)))

    # ②b Net CONE域结果表
    if result.get("calculation_kind") == "cost_net_cone":
        ws6 = wb.create_sheet("NetCONE")
        ws6["B2"] = "年度E&AS与Net CONE（口径见假设页）"
        ws6["B2"].font = bold
        rows6 = [
            ("电能量净收益（元/年）", result.get("energy_net_revenue")),
            ("辅助服务净收益（元/年）", result.get("ancillary_net_revenue")),
            ("E&AS年度净收益（元/年）", result.get("eas_annual")),
            ("E&AS抵扣（元/kW·年）", result.get("eas_offset_per_kw")),
            ("外部Gross CONE（元/kW·年）", (result.get("gross") or {}).get("value")),
            ("Gross来源模式", (result.get("gross") or {}).get("mode")),
            ("Net CONE原始值（元/kW·年）", result.get("net_cone_raw")),
            ("结果含义", result.get("net_interpretation")),
        ]
        for i, (label, value) in enumerate(rows6, start=4):
            ws6.cell(row=i, column=2, value=label).font = bold
            ws6.cell(row=i, column=3, value=_fmt(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else ("" if value is None else str(value)))
        comp = result.get("contract_comparison")
        if comp:
            ws6.cell(row=14, column=2, value=comp.get("label", "合约影响差额")).font = bold
            ws6.cell(row=14, column=3, value=_fmt(comp.get("difference")))

    # ②c 完整Gross CONE域结果表
    if result.get("calculation_kind") == "cost_gross_cone":
        ws7 = wb.create_sheet("GrossCONE")
        ws7["B2"] = "完整Gross CONE（年度自由现金流路线，NPV=0求固定收入）"
        ws7["B2"].font = bold
        solve = result.get("solve", {})
        rows7 = [
            ("Gross CONE（元/kW·年）", result.get("gross_cone_per_kw")),
            ("求解状态", solve.get("status")),
            ("NPV残差（元）", solve.get("npv_residual")),
            ("迭代次数", solve.get("iterations")),
            ("折现率", result.get("inputs_summary", {}).get("discount_rate")),
            ("统一所得税率", result.get("inputs_summary", {}).get("tax_rate")),
        ]
        for i, (label, value) in enumerate(rows7, start=4):
            ws7.cell(row=i, column=2, value=label).font = bold
            ws7.cell(row=i, column=3, value=_fmt(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else ("" if value is None else str(value)))
        if solve.get("reason"):
            ws7["B12"] = solve["reason"]
        ws8 = wb.create_sheet("GrossCONE现金流")
        ws8["B2"] = "投资与运营现金流（元；年末记账）"
        ws8["B2"].font = bold
        headers = ["阶段", "年", "投资", "收入", "固定运维", "所得税", "净现金流"]
        for col, header in enumerate(headers, start=2):
            ws8.cell(row=4, column=col, value=header).font = bold
        row_cursor = 5
        for row in result.get("cashflow", {}).get("construction_years", []):
            ws8.cell(row=row_cursor, column=2, value="建设期")
            ws8.cell(row=row_cursor, column=3, value=row.get("year"))
            ws8.cell(row=row_cursor, column=4, value=_fmt(row.get("investment")))
            ws8.cell(row=row_cursor, column=8, value=_fmt(row.get("net_flow")))
            row_cursor += 1
        for row in result.get("cashflow", {}).get("operating_years", []):
            ws8.cell(row=row_cursor, column=2, value="经营期")
            ws8.cell(row=row_cursor, column=3, value=row.get("year"))
            ws8.cell(row=row_cursor, column=5, value=_fmt(row.get("revenue")))
            ws8.cell(row=row_cursor, column=6, value=_fmt(row.get("fixed_om")))
            ws8.cell(row=row_cursor, column=7, value=_fmt(row.get("tax")))
            ws8.cell(row=row_cursor, column=8, value=_fmt(row.get("net_flow")))
            row_cursor += 1

    # ③ 假设与披露（所有成本域共用）
    ws5 = wb.create_sheet("假设与披露")
    ws5["B2"] = "数据来源、口径与假设（如实披露，不冒充已校准标准）"
    ws5["B2"].font = bold
    assumptions = result.get("assumptions", {})
    rows5 = [(str(k), str(v)) for k, v in assumptions.items()]
    inputs = sorted(input_payload.items())
    ws5.cell(row=4, column=2, value="输入快照（本运行固定输入）").font = bold
    for i, (key, value) in enumerate(inputs, start=5):
        ws5.cell(row=i, column=2, value=str(key))
        ws5.cell(row=i, column=3, value=_fmt(value) if isinstance(value, (int, float)) else str(value))
    start_assumptions = 5 + len(inputs) + 1
    ws5.cell(row=start_assumptions, column=2, value="口径披露").font = bold
    for i, (key, value) in enumerate(rows5, start=start_assumptions + 1):
        ws5.cell(row=i, column=2, value=str(key))
        ws5.cell(row=i, column=3, value=str(value))

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _html_bytes(
    *,
    manifest: dict[str, Any],
    result: dict[str, Any],
    excel_name: str,
    excel_sha: str,
) -> bytes:
    """A minimal business HTML reading view (same identity facts; the
    Excel workbook is the authoritative artifact)."""
    kind = str(manifest.get("cost_kind", ""))
    rows = []
    if result.get("calculation_kind") == "fixed_cost":
        first = result.get("first_year", {})
        avg = result.get("period_average", {})
        rows = [
            ("首年固定成本（不含税）", f"{float(first.get('total', 0)):,.4f} 元/kW·年"),
            ("经营期平均（不含税）", f"{float(avg.get('total', 0)):,.4f} 元/kW·年"),
            ("首年含税等效", f"{float(first.get('total_with_vat', 0)):,.4f} 元/kW·年"),
            ("平均含税等效", f"{float(avg.get('total_with_vat', 0)):,.4f} 元/kW·年"),
            ("静态总投资", f"{float(result.get('static_investment_yi', 0)):,.6f} 亿元"),
            ("建设期利息", f"{float(result.get('idc_yi', 0)):,.6f} 亿元"),
            ("单位动态造价", f"{float(result.get('unit_dynamic_cost', 0)):,.4f} 元/kW"),
        ]
    elif result.get("calculation_kind") == "cost_net_cone":
        net_raw = result.get("net_cone_raw")
        rows = [
            ("E&AS年度净收益", f"{float(result.get('eas_annual', 0)):,.0f} 元/年"),
            ("E&AS抵扣", f"{float(result.get('eas_offset_per_kw', 0)):,.4f} 元/kW·年"),
            ("Net CONE原始值",
             "未测算" if net_raw is None else f"{float(net_raw):,.4f} 元/kW·年"),
        ]
    elif result.get("calculation_kind") == "cost_gross_cone":
        rate = result.get("gross_cone_per_kw")
        solve = result.get("solve", {})
        rows = [
            ("Gross CONE",
             "未求得（无解）" if rate is None else f"{float(rate):,.4f} 元/kW·年"),
            ("求解状态", str(solve.get("status", ""))),
            ("NPV残差", str(solve.get("npv_residual", ""))),
        ]
    assumption_rows = "".join(
        f"<li><code>{k}</code>：{v}</li>" for k, v in (result.get("assumptions", {})).items()
    )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_KIND_LABELS.get(kind, kind)}业务报告</title></head>
<body>
<h1>{_KIND_LABELS.get(kind, kind)}业务报告</h1>
<p>运行编号：{manifest.get('run_ref', '')}｜完成时间：{manifest.get('created_at', '')}</p>
<p>权威工件：{excel_name}（SHA-256：{excel_sha[:16]}…）；本页为业务阅读版，不替代工件。</p>
<h2>关键结果</h2>
<table border="1" cellpadding="4">{''.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k, v in rows)}</table>
<h2>口径与假设</h2>
<ul>{assumption_rows}</ul>
<p>本报告为成本域业务投影：不修改机器结果，不读取项目最新输入替代运行固定输入。</p>
</body></html>
"""
    return html.encode("utf-8")


def export_cost_report(
    *,
    manifest: dict[str, Any],
    input_payload: dict[str, Any],
    result: dict[str, Any],
    output_dir: Path | str,
) -> dict[str, Any]:
    """Publish the cost report set (Excel + HTML + manifest last) for
    one completed cost run. An existing manifest replays ONLY when the
    whole set still matches every recorded hash; a different set under
    the same identity is a hard refusal (never an overwrite)."""
    output = Path(output_dir)
    run_ref = str(manifest.get("run_ref", ""))
    if not run_ref:
        raise CostReportError(
            "COST_REPORT_INPUT_INVALID", "成本运行清单缺少run_ref"
        )
    output.mkdir(parents=True, exist_ok=True)
    xlsx = _excel_bytes(
        manifest=manifest, input_payload=input_payload, result=result
    )
    html = _html_bytes(
        manifest=manifest,
        result=result,
        excel_name=_REPORT_XLSX,
        excel_sha=_sha256(xlsx),
    )
    entries = [
        {
            "name": _REPORT_XLSX,
            "sha256": _sha256(xlsx),
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
    report_manifest = {
        "cost_report_schema_version": "1.0",
        "generator": _GENERATOR,
        "run_ref": run_ref,
        "cost_kind": manifest.get("cost_kind"),
        "input_snapshot_sha256": manifest.get("input_snapshot_sha256"),
        "result_sha256": manifest.get("result_sha256"),
        "files": entries,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    manifest_bytes = _canonical(report_manifest)

    existing = output / _MANIFEST_FILE
    if existing.is_file():
        try:
            previous = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CostReportError(
                "COST_REPORT_EXISTS_DIFFERENT",
                f"既有成本报告清单不可读取：{exc}",
            ) from exc
        if previous.get("files") == report_manifest["files"] and previous.get(
            "result_sha256"
        ) == report_manifest["result_sha256"]:
            return {
                "replayed": True,
                "run_ref": run_ref,
                "output_dir": str(output),
                "files": [entry["name"] for entry in entries],
            }
        raise CostReportError(
            "COST_REPORT_EXISTS_DIFFERENT",
            "同身份目录已存在不同的成本报告集；不覆盖既有报告",
        )
    for entry in entries:
        name = entry["name"]
        data = xlsx if name == _REPORT_XLSX else html
        path = output / name
        if path.exists():
            raise CostReportError(
                "COST_REPORT_EXISTS_DIFFERENT", f"报告文件已存在：{name}"
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
        "run_ref": run_ref,
        "output_dir": str(output),
        "files": [entry["name"] for entry in entries],
    }


__all__ = ["CostReportError", "export_cost_report"]
