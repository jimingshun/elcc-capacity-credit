"""数据链接口（/api/data/*）。任务 D 工作 2 拆出；零行为变化；
导入一律模块级。"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from capacity_credit.application.workbench_service import WorkbenchError
from capacity_credit.ui import viewmodel
from capacity_credit.webapp_routes import route


@route("GET", "/api/data/templates")
def data_templates(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("project") or [""])[0]
    return h._json(
        200, h._service().get_input_templates(ref)
    )


@route("GET", "/api/data/template")
def data_template_download(h: Any, qs: dict, body: Any) -> None:
    slot = (qs.get("slot") or [""])[0]
    try:
        file_name, payload = h._service().get_template_download(
            slot
        )
    except KeyError as exc:
        # template_catalog's unknown/reserved-slot refusal is a
        # KeyError - translate to the honest 400 boundary shape
        raise WorkbenchError("invalid_input", str(exc)) from None
    return h._bytes(payload, "text/csv; charset=utf-8", file_name)


@route("POST", "/api/data/import")
def data_import(h: Any, qs: dict, body: Any) -> None:
    ref = str(body.get("project_ref") or "")
    grade = str(body.get("declared_grade") or "")
    files: dict[str, tuple[str, bytes]] = {}
    for item in body.get("files") or []:
        slot = str(item.get("slot") or "")
        name = str(item.get("name") or f"{slot}.csv")
        raw = base64.b64decode(str(item.get("b64") or ""))
        files[slot] = (name, raw)
    # W3/B7-2: optional planning-calibre declaration - a missing or
    # blank value keeps the legacy no-reconciliation behavior; the
    # validator itself warns (never blocks) on drift or nonsense.
    declared_total_raw = body.get("declared_total_capacity_mw")
    try:
        declared_total = (
            None if declared_total_raw in (None, "") else float(declared_total_raw)
        )
    except (TypeError, ValueError):
        declared_total = declared_total_raw  # validator warns honestly
    tolerance_raw = body.get("ledger_total_tolerance_percent")
    try:
        tolerance = 1.0 if tolerance_raw in (None, "") else float(tolerance_raw)
    except (TypeError, ValueError):
        tolerance = 1.0
    result = h._service().import_inputs(
        project_ref=ref,
        files=files,
        declared_grade=grade,
        created_by="web-workbench",
        declared_total_capacity_mw=declared_total,
        ledger_total_tolerance_percent=tolerance,
    )
    h._json(
        200,
        {
            "result": result,
            "view": viewmodel.import_submission_view(result),
        },
    )


@route("GET", "/api/data/summary")
def data_summary(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("project") or [""])[0]
    batch = (qs.get("batch") or [""])[0]
    return h._json(
        200, h._service().get_import_summary(ref, batch)
    )


@route("GET", "/api/data/coverage")
def data_coverage(h: Any, qs: dict, body: Any) -> None:
    # W1 (three-slice plan section 2, the HTTP exposure deferred
    # to '实现期定'): the honest weather-year coverage projection
    # for one frozen batch - the data-page year card's data
    # source; unknown batches surface through the shared 400
    # boundary, never an empty projection
    ref = (qs.get("project") or [""])[0]
    batch = (qs.get("batch") or [""])[0]
    preview = (qs.get("preview_equal_weights") or [""])[0].lower()
    return h._json(
        200,
        h._service().get_weather_year_coverage(
            ref,
            import_batch_id=batch,
            preview_equal_weights=preview in ("1", "true", "yes"),
        ),
    )


@route("POST", "/api/data/derive")
def data_derive(h: Any, qs: dict, body: Any) -> None:
    # B1: derive ONE data version from a frozen baseline; the
    # chain stops at 待确认 (DR-01 requires human confirmation)
    result = h._service().derive_data_version(
        str(body.get("project_ref") or ""),
        baseline_batch_id=str(body.get("baseline_batch") or ""),
        rule_id=str(body.get("rule_id") or ""),
        params=body.get("params") or {},
        declared_grade=str(body.get("declared_grade") or ""),
    )
    return h._json(200, result)


@route("POST", "/api/data/freeze")
def data_freeze(h: Any, qs: dict, body: Any) -> None:
    result = h._service().confirm_and_freeze(
        project_ref=str(body.get("project_ref") or ""),
        import_batch_id=str(body.get("batch") or ""),
        request_id=f"web-{uuid.uuid4().hex}",
        reviewed_by=str(body.get("reviewed_by") or ""),
        review_comment=str(body.get("review_comment") or ""),
        confirmed_summary_hash=str(body.get("summary_hash") or ""),
    )
    h._json(200, result)


@route("POST", "/api/data/bank")
def data_bank(h: Any, qs: dict, body: Any) -> None:
    result = h._service().prepare_scenario_bank(
        project_ref=str(body.get("project_ref") or ""),
        import_batch_id=str(body.get("batch") or ""),
        request_id=f"web-{uuid.uuid4().hex}",
        root_seed=int(body.get("root_seed")),
        samples=int(body.get("samples") or 30),
        weight_set_id=str(body.get("weight_set_id") or "w1"),
        timezone="Asia/Shanghai",
    )
    h._json(200, result)
