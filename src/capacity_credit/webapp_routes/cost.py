"""成本域接口（/api/cost/*、/api/check/<kind> 前缀）。任务 D 工作 2 拆出；
零行为变化；导入一律模块级（from-import 遮蔽事故的根治面）。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from capacity_credit.application.check_service import graded_cost_check
from capacity_credit.application.cost_payload import (
    build_fixed_cost_payload,
    build_gross_cone_payload,
    build_net_cone_payload,
)
from capacity_credit.application.workbench_service import WorkbenchError
from capacity_credit.costs.catalog import (
    TECH_TOGGLES,
    catalog_disclaimer,
    machine_catalog,
    province_coefficients,
    zone_coefficients,
)
from capacity_credit.ui import runtime as ui_runtime
from capacity_credit.webapp_routes import route, route_prefix

_COST_BUILDERS = {
    "cost_fixed": build_fixed_cost_payload,
    "cost_gross_cone": build_gross_cone_payload,
    "cost_net_cone": build_net_cone_payload,
}


def _wait_for_cost_run(workspace_root: Path | str, run_ref: str) -> str:
    """Wait briefly for the synchronous cost runner (admission is async,
    the deterministic computation is fast). Same discipline as the
    Streamlit layer's gate; an unconfirmed ref falls through so the
    projection read can report the honest state."""
    runtime = ui_runtime.get_local_runtime(workspace_root)
    deadline = time.monotonic() + 20.0
    snap = runtime.snapshot(run_ref)
    while snap.status not in ("execution_finished", "execution_failed"):
        if snap.status in ("unconfirmed",) or time.monotonic() > deadline:
            break
        time.sleep(0.2)
        snap = runtime.snapshot(run_ref)
    return snap.status


@route("GET", "/api/cost/projection")
def cost_projection(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("project") or [""])[0]
    run = (qs.get("run") or [""])[0]
    _wait_for_cost_run(h.workspace, run)
    return h._json(
        200, h._service().get_cost_run_projection(ref, run)
    )


def _gross_references(h: Any) -> list[dict[str, Any]]:
    """Every completed cost_gross_cone run in the workspace with a
    solved rate - the Net CONE internal-reference candidates. Each
    entry carries the caliber facts (object/region/year/price year/
    capacity) for the user to pick a same-caliber source; the
    server's resolve_gross_reference performs the actual binding
    and caliber gate."""
    service = h._service()
    out: list[dict[str, Any]] = []
    for project in service.list_projects():
        if project["invalid"]:
            continue
        try:
            if service.get_project_kind(str(project["project_dir"])) != "cost":
                continue
        except WorkbenchError:
            continue
        try:
            runs = service.list_cost_run_summaries(str(project["project_dir"]))
        except WorkbenchError:
            continue
        for summary in runs:
            if summary.get("invalid"):
                continue
            run_ref = str(summary.get("run_ref") or "")
            if not run_ref:
                continue
            try:
                record = service.get_cost_run_projection(
                    str(project["project_dir"]), run_ref
                )
            except WorkbenchError:
                continue
            manifest = record.get("manifest") or {}
            if manifest.get("cost_kind") != "cost_gross_cone":
                continue
            if manifest.get("status") != "completed":
                continue
            result = record.get("result") or {}
            rate = result.get("gross_cone_per_kw")
            if not isinstance(rate, (int, float)):
                continue
            inp = record.get("input") or {}
            out.append(
                {
                    "project_ref": str(project["project_dir"]),
                    "project_name": str(project["name"]),
                    "run_ref": run_ref,
                    "gross_cone_per_kw": float(rate),
                    "object_name": str(inp.get("object_name", "")),
                    "region": str(inp.get("region", "")),
                    "target_year": inp.get("target_year"),
                    "price_basis_year": inp.get("price_basis_year"),
                    "capacity_kw": inp.get("capacity_kw"),
                    "capacity_denominator_kind": str(
                        inp.get("capacity_denominator_kind", "")
                    ),
                    "result_sha256": str(manifest.get("result_sha256") or "")[:16],
                }
            )
    return out


@route("GET", "/api/cost/gross-references")
def gross_references(h: Any, qs: dict, body: Any) -> None:
    # the referenceable completed Gross runs across the
    # workspace (R4a): every completed cost_gross_cone run with
    # a solved rate, labelled for caliber review - the server's
    # resolve_gross_reference still performs the actual binding
    return h._json(
        200, {"runs": _gross_references(h)}
    )


@route("GET", "/api/cost/catalog")
def cost_catalog(h: Any, qs: dict, body: Any) -> None:
    # P-C: THE catalog source for the frontend selectors (same
    # tables the engine validates against - one source, no
    # drift between options and refusals)
    return h._json(
        200,
        {
            "machines": machine_catalog(),
            "provinces": province_coefficients(),
            "zones": zone_coefficients(),
            "tech_toggles": dict(TECH_TOGGLES),
            "disclaimer": catalog_disclaimer(),
        },
    )


@route("POST", "/api/cost/submit")
def cost_submit(h: Any, qs: dict, body: Any) -> None:
    ref = str(body.get("project_ref") or "")
    kind = str(body.get("kind") or "")
    form = body.get("form")
    builder = _COST_BUILDERS.get(kind)
    if builder is None:
        return h._json(
            400,
            {"kind": "invalid_input", "message": f"未知成本类型 {kind}"},
        )
    # R1: a cost project created WITH a measurement type is locked
    # to it - a submission of a different cost kind is refused at
    # this boundary (never reaches the runner)
    try:
        locked = h._service().get_project_measurement_type(ref)
    except WorkbenchError as exc:
        return h._json(400, {"kind": exc.kind, "message": exc.message})
    if locked is not None and locked != kind:
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": (
                    f"该项目已锁定为 {locked} 测算类型，不能提交 {kind}。"
                    "请新建对应类型的测算项目。"
                ),
            },
        )
    payload = (
        builder(form, body.get("tech_toggles"))
        if kind == "cost_fixed"
        else builder(form or {})
    )
    handle = h._service().start_cost_calculation(
        intent_id=f"web-{uuid.uuid4().hex}",
        project_ref=ref,
        cost_kind=kind,
        payload=payload,
        intent_is_new=True,
    )
    run_ref = str(handle.request_ref)
    status = _wait_for_cost_run(h.workspace, run_ref)
    # P-A: a completed cost run binds as THE project's formal result
    # (one project, one formal result); a refusal here leaves the
    # project honestly unbound - the projection stays the authority
    formal_bound = False
    if status == "execution_finished":
        try:
            h._service().bind_formal_result_from_run(
                ref, domain="cost", run_ref=run_ref
            )
            formal_bound = True
        except WorkbenchError:
            formal_bound = False
    h._json(
        200,
        {
            "run_ref": run_ref,
            "status": status,
            "formal_result_bound": formal_bound,
        },
    )


@route_prefix("POST", "/api/check/")
def graded_check(h: Any, qs: dict, body: Any) -> None:
    # P-B: the server-side graded check (zero side effects) - the
    # page renders THIS aggregate; the engine gate stays the
    # submission authority
    kind = h.path.rsplit("/", 1)[1]
    ref = str(body.get("project_ref") or "")
    summary = h._service().get_project_summary(ref)
    if summary is None or summary.get("invalid"):
        return h._json(
            400,
            {
                "kind": "invalid_input",
                "message": f"项目不存在或已损坏：{ref}",
            },
        )
    result = graded_cost_check(
        kind,
        body.get("form") or {},
        h.workspace / ref,
        body.get("tech_toggles"),
    )
    return h._json(200, result)


@route("GET", "/api/cost/report")
def cost_report(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("project") or [""])[0]
    run = (qs.get("run") or [""])[0]
    fmt = (qs.get("format") or ["xlsx"])[0]
    h._service().export_cost_report(ref, run)
    downloads = h._service().get_cost_report_downloads(ref, run)
    suffix = ".xlsx" if fmt == "xlsx" else ".html"
    for entry in downloads.get("files", []):
        if str(entry["name"]).endswith(suffix):
            return h._bytes(
                entry["bytes"],
                str(entry["content_type"]),
                str(entry["name"]),
            )
    h._json(
        400,
        {
            "kind": "invalid_input",
            "message": downloads.get("reason") or "报告文件不存在",
        },
    )
