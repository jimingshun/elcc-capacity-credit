"""结果索引接口（/api/results*）。任务 D 工作 2 拆出；零行为变化。"""

from __future__ import annotations

from typing import Any

from capacity_credit.application import result_index_service
from capacity_credit.webapp_routes import route


@route("GET", "/api/results/recent")
def list_recent(h: Any, qs: dict, body: Any) -> None:
    # 用户2026-09-17：首页「最近结果」只索引最近3次
    rows = result_index_service.list_unified_results(
        h._service(), per_project_limit=5, total_limit=3
    )
    return h._json(200, rows)


@route("GET", "/api/results/aggregate")
def aggregate(h: Any, qs: dict, body: Any) -> None:
    # P-AGG: the read-only cross-project multi-resource table
    # (one project = one formal result, so a per-resource view is
    # an aggregate READ, never a batch run)
    agg_method = (qs.get("method") or [""])[0]
    try:
        return h._json(
            200,
            result_index_service.aggregate_method_results(
                h._service(), agg_method
            ),
        )
    except ValueError as exc:
        return h._json(
            400, {"kind": "invalid_input", "message": str(exc)}
        )


@route("GET", "/api/results")
def list_project_results(h: Any, qs: dict, body: Any) -> None:
    ref = (qs.get("project") or [""])[0]
    return h._json(
        200,
        result_index_service.list_project_results(h._service(), ref),
    )
