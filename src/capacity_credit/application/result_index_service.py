"""Night-6 A2/A3: THE unified cross-domain results index.

One read-only projection joins the reliability runs (project/runs/,
through the facade's EXISTING queries) and the cost runs
(project/cost_runs/, through the EXISTING cost service listing) for
every consumer - the home recent results, the project workbench
statistics and the public results page. Pages never scan directories
themselves and never build a second index.

Hard boundaries:

- READ-ONLY: listing projects or opening rows never computes, never
  writes, never touches lifecycle or publication; the physical
  directories stay separate (no merged runs+cost_runs store, no second
  lock / state machine / publish manifest);
- every row carries a COMPOSITE route (project_ref + domain + run_ref):
  a bare run UUID is never assumed globally unique across domains and
  never cross-routes;
- a corrupt record stays an honest invalid row (never dropped, never
  laundered into 暂无结果) and one damaged record never hides the
  healthy ones;
- bounded queries: per-project and total limits; a limit cut is
  DECLARED (incomplete + reason) instead of pretending completeness;
- each domain keeps its OWN status semantics: a reliability run
  without a published MethodResult is 未发布结果 (not 计算失败); a failed
  cost solve is 计算失败 even though the record directory exists.
"""

from __future__ import annotations

from typing import Any

from capacity_credit.application.atomic_io import safe_filename_component
from capacity_credit.application.workbench_service import WorkbenchError

DOMAIN_RELIABILITY = "reliability"
DOMAIN_COST = "cost"
DOMAIN_SETTLEMENT = "settlement"
DOMAIN_MARKET = "market"
_DOMAINS = (DOMAIN_RELIABILITY, DOMAIN_COST, DOMAIN_SETTLEMENT, DOMAIN_MARKET)

# user-readable method/kind labels (display only; a row's own identity
# fields stay untouched)
_METHOD_LABELS: dict[str, str] = {
    "adequacy": "电力供应充裕度评估",
    "elcc": "有效容量评估（ELCC）",
    "efc": "等效可靠容量评估（EFC）",
    "demand_target_capacity": "目标可靠容量测算",
    "demand_coefficient": "容量供需系数（公式 v1）",
    "settlement_annual": "年度容量补偿结算",
    "market_simulation": "容量市场仿真出清",
}
_COST_KIND_LABELS: dict[str, str] = {
    "cost_fixed": "新建机组固定成本核算",
    "cost_net_cone": "年度Net CONE测算",
    "cost_gross_cone": "完整Gross CONE测算",
}

_DEFAULT_PER_PROJECT_LIMIT = 10
_DEFAULT_TOTAL_LIMIT = 50


def make_route(project_ref: str, domain: str, run_ref: str) -> dict[str, str]:
    """THE composite route identity of one result row: project_ref and
    run_ref are validated single path components, the domain must be a
    known domain - anything else fails closed (a route is never built
    from a guessed or partial identity)."""
    safe_filename_component(project_ref)
    safe_filename_component(run_ref)
    if domain not in _DOMAINS:
        raise ValueError(f"unknown result domain: {domain!r}")
    return {"project_ref": project_ref, "domain": domain, "run_ref": run_ref}


def same_route(a: dict[str, str], b: dict[str, str]) -> bool:
    """Equality of two routes is the FULL composite (project + domain +
    run); matching only the run UUID is never equality."""
    return (
        a.get("project_ref") == b.get("project_ref")
        and a.get("domain") == b.get("domain")
        and a.get("run_ref") == b.get("run_ref")
    )


def method_label(method: str | None) -> str:
    """The readable label of a method or cost kind (an unknown value
    stays 未知方法, never a guessed name)."""
    raw = str(method or "")
    return _METHOD_LABELS.get(raw) or _COST_KIND_LABELS.get(raw, "未知方法")


def reliability_status_label(row: dict[str, Any]) -> str:
    """The reliability domain's OWN state semantics (identical to the
    established history-row wording): an unpublished run is 未发布结果,
    a failed lifecycle is 计算失败 - a missing result file is never
    laundered into success."""
    lifecycle = str(row.get("lifecycle_status") or "")
    published = bool(row.get("published"))
    if lifecycle == "running":
        return "正在计算"
    if lifecycle == "failed":
        return "计算失败"
    if lifecycle in ("cancelled", "stopping"):
        return "已中断"
    if published and lifecycle == "completed":
        return "计算完成"
    if published:
        return "有结果（生命周期未知）"
    return "未发布结果"


def cost_status_label(status: str) -> str:
    """The cost domain's OWN state semantics: the manifest status is
    the authority (a record directory existing is never success)."""
    raw = str(status or "")
    if raw == "completed":
        return "计算完成"
    if raw == "failed":
        return "计算失败"
    if raw == "running":
        return "正在计算"
    return "未完成"


def _cost_object_name(input_payload: dict[str, Any]) -> str | None:
    """The measured object of one cost record, from its OWN input
    snapshot (never guessed from the project or the kind)."""
    for key in ("object_name", "reference_unit", "machine_id"):
        value = input_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _cost_row_from_summary(
    service: Any, project_ref: str, project_name: str, summary: dict[str, Any]
) -> dict[str, Any]:
    """One cost index row from the existing cost listing summary; the
    record body is read through the facade projection ONLY to resolve
    the object name and readability (bounded by the per-project limit)
    - a read failure degrades the row honestly instead of dropping it."""
    run_ref = str(summary.get("run_ref") or "")
    invalid = bool(summary.get("invalid"))
    created = summary.get("created_at") or None
    row: dict[str, Any] = {
        "project_ref": project_ref,
        "project_name": project_name,
        "domain": DOMAIN_COST,
        "method": str(summary.get("cost_kind") or "") or None,
        "method_label": method_label(summary.get("cost_kind")),
        "run_ref": run_ref,
        "created_at": created,
        "created_at_label": _beijing_time_label(created) if not invalid else "未知",
        "object_name": None,
        "readable": False,
        "invalid": invalid,
        "invalid_reason": summary.get("invalid_reason"),
        "status_label": "记录损坏" if invalid else cost_status_label(
            str(summary.get("status") or "")
        ),
        "detail_unavailable_reason": None,
        "route": make_route(project_ref, DOMAIN_COST, run_ref),
    }
    if invalid:
        row["created_at"] = None
        return row
    try:
        record = service.get_cost_run_projection(project_ref, run_ref)
    except Exception as exc:  # noqa: BLE001 - an unreadable record body
        # is an honest per-row degradation (never a hidden drop)
        row["detail_unavailable_reason"] = f"记录内容不可读取：{exc}"
        row["status_label"] = "记录不可读取"
        return row
    manifest = record.get("manifest") if isinstance(record, dict) else None
    input_payload = record.get("input") if isinstance(record, dict) else None
    if isinstance(manifest, dict) and manifest.get("status"):
        row["status_label"] = cost_status_label(str(manifest["status"]))
    if isinstance(input_payload, dict):
        row["object_name"] = _cost_object_name(input_payload)
    row["readable"] = True
    return row


def _reliability_row(
    project_ref: str, project_name: str, row_data: dict[str, Any]
) -> dict[str, Any]:
    """One reliability index row from the EXISTING run summary row (no
    extra reads, no re-derivation of lifecycle facts)."""
    run_ref = str(row_data.get("run_ref") or "")
    created = row_data.get("created_at")
    return {
        "project_ref": project_ref,
        "project_name": project_name,
        "domain": DOMAIN_RELIABILITY,
        "method": row_data.get("method"),
        "method_label": method_label(row_data.get("method")),
        "run_ref": run_ref,
        "created_at": created,
        "created_at_label": _beijing_time_label(created),
        "object_name": project_name,
        "readable": bool(row_data.get("published")),
        "invalid": False,
        "invalid_reason": None,
        "status_label": reliability_status_label(row_data),
        "detail_unavailable_reason": None,
        "route": make_route(project_ref, DOMAIN_RELIABILITY, run_ref),
    }


def _coefficient_row_from_summary(
    project_ref: str, project_name: str, summary: dict[str, Any]
) -> dict[str, Any]:
    """One coefficient-freeze index row (Step 3 S0): the freeze manifest
    is the authority - a frozen coefficient reads 已冻结, a corrupt
    record reads 记录损坏 (never laundered into success). The row carries
    the point value and formula_version for settlement selection."""
    freeze_ref = str(summary.get("freeze_ref") or "")
    invalid = bool(summary.get("invalid"))
    created = summary.get("created_at") or None
    row: dict[str, Any] = {
        "project_ref": project_ref,
        "project_name": project_name,
        "domain": DOMAIN_RELIABILITY,
        "method": "demand_coefficient",
        "method_label": method_label("demand_coefficient"),
        "run_ref": freeze_ref,
        "created_at": created,
        "created_at_label": _beijing_time_label(created) if not invalid else "未知",
        "object_name": project_name,
        "readable": not invalid,
        "invalid": invalid,
        "invalid_reason": summary.get("invalid_reason"),
        "status_label": "记录损坏" if invalid else "已冻结",
        "detail_unavailable_reason": None,
        "coefficient": summary.get("coefficient"),
        "formula_version": summary.get("formula_version"),
        "target_year": summary.get("target_year"),
        "confirmer": summary.get("confirmer"),
        "route": (
            make_route(project_ref, DOMAIN_RELIABILITY, freeze_ref)
            if freeze_ref
            else None
        ),
    }
    if invalid:
        row["created_at"] = None
    return row


def _settlement_row_from_summary(
    project_ref: str, project_name: str, summary: dict[str, Any]
) -> dict[str, Any]:
    """One settlement annual-summary index row (Step 3 S3 收尾): the
    published annual summary is the authority - a published year reads
    已发布, a corrupt record reads 记录损坏 (never laundered into
    success). The row carries the settlement-year point values for the
    results page."""
    annual_ref = str(summary.get("annual_ref") or "")
    invalid = bool(summary.get("invalid"))
    created = summary.get("published_at") or None
    row: dict[str, Any] = {
        "project_ref": project_ref,
        "project_name": project_name,
        "domain": DOMAIN_SETTLEMENT,
        "method": "settlement_annual",
        "method_label": method_label("settlement_annual"),
        "run_ref": annual_ref,
        "created_at": created,
        "created_at_label": _beijing_time_label(created) if not invalid else "未知",
        "object_name": project_name,
        "readable": not invalid,
        "invalid": invalid,
        "invalid_reason": summary.get("invalid_reason"),
        "status_label": "记录损坏" if invalid else "已发布",
        "detail_unavailable_reason": None,
        "settlement_year": summary.get("settlement_year"),
        "total_paid": summary.get("total_paid"),
        "total_deduction": summary.get("total_deduction"),
        "formula_version": summary.get("formula_version"),
        "route": (
            make_route(project_ref, DOMAIN_SETTLEMENT, annual_ref)
            if annual_ref
            else None
        ),
    }
    if invalid:
        row["created_at"] = None
    return row


def _market_row_from_summary(
    project_ref: str, project_name: str, summary: dict[str, Any]
) -> dict[str, Any]:
    """One market-clearing index row (Q-M6 2026-10-11): the frozen clearing
    manifest is the authority - a frozen clearing reads 已冻结, a corrupt
    record reads 记录损坏 (never laundered into success). The row carries
    the clearing point values for the results page."""
    clearing_ref = str(summary.get("clearing_ref") or "")
    invalid = bool(summary.get("invalid"))
    created = summary.get("frozen_at") or None
    row: dict[str, Any] = {
        "project_ref": project_ref,
        "project_name": project_name,
        "domain": DOMAIN_MARKET,
        "method": "market_simulation",
        "method_label": method_label("market_simulation"),
        "run_ref": clearing_ref,
        "created_at": created,
        "created_at_label": _beijing_time_label(created) if not invalid else "未知",
        "object_name": project_name,
        "readable": not invalid,
        "invalid": invalid,
        "invalid_reason": summary.get("invalid_reason"),
        "status_label": "记录损坏" if invalid else "已冻结",
        "detail_unavailable_reason": None,
        "clearing_price": summary.get("clearing_price"),
        "procured_mw": summary.get("procured_mw"),
        "shortfall_mw": summary.get("shortfall_mw"),
        "rule_version": summary.get("rule_version"),
        "route": (
            make_route(project_ref, DOMAIN_MARKET, clearing_ref)
            if clearing_ref
            else None
        ),
    }
    if invalid:
        row["created_at"] = None
    return row


def _time_sort_key(row: dict[str, Any]) -> str:
    return str(row.get("created_at") or "")


def _beijing_time_label(value: Any) -> str:
    """The display wall-clock label of one recorded time (Beijing,
    UTC offsets honoured). Missing/unparsable input stays 未知 - this
    is a pure display projection, the record itself stays untouched."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    if value is None:
        return "未知"
    try:
        if isinstance(value, str):
            parsed = _dt.fromisoformat(value)
        elif isinstance(value, _dt):
            parsed = value
        else:
            return "未知"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.now().astimezone().tzinfo)
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (ValueError, OSError):
        return "未知"


def list_project_results(
    service: Any,
    project_ref: str,
    *,
    per_project_limit: int | None = None,
) -> dict[str, Any]:
    """The unified listing for ONE project (the public results page):
    the same row builders and the same honesty rules as
    list_unified_results, scoped to a single validated project (the
    facade's own query entries validate the reference). Workspace
    boundary failures propagate as WorkbenchError (the page projects
    them); a corrupt run/cost listing becomes an honest invalid row
    instead of a silent gap."""
    run_rows = service.list_run_summaries(
        project_ref, limit=per_project_limit
    )
    cost_summaries = service.list_cost_run_summaries(project_ref)
    if per_project_limit is not None:
        cost_summaries = cost_summaries[:per_project_limit]
    project_name = project_ref
    for summary in service.list_projects():
        if summary.get("project_dir") == project_ref:
            project_name = str(summary.get("name") or project_ref)
            break
    rows = [_reliability_row(project_ref, project_name, row) for row in run_rows]
    rows.extend(
        _cost_row_from_summary(service, project_ref, project_name, summary)
        for summary in cost_summaries
    )
    # Step 3 S0: coefficient freezes are a THIRD read channel (their own
    # immutable record under coefficient/; never a run, never a cost run)
    rows.extend(
        _coefficient_row_from_summary(project_ref, project_name, summary)
        for summary in service.list_coefficient_summaries(project_ref)
    )
    # Step 3 S3 收尾: a published annual settlement summary is the
    # FOURTH read channel (its own immutable record under settlement/;
    # never a run, never a cost run)
    rows.extend(
        _settlement_row_from_summary(project_ref, project_name, summary)
        for summary in service.list_settlement_summaries(project_ref)
    )
    # Q-M1—M6 (2026-10-11): a frozen market clearing is the FIFTH read
    # channel (its own immutable record under market/)
    rows.extend(
        _market_row_from_summary(project_ref, project_name, summary)
        for summary in service.list_market_summaries(project_ref)
    )
    rows.sort(
        key=lambda row: (
            _time_sort_key(row),
            str(row.get("run_ref") or ""),
        ),
        reverse=True,
    )
    invalid_rows = [row for row in rows if row["invalid"]]
    return {
        "project_ref": project_ref,
        "project_name": project_name,
        "rows": rows,
        "invalid": bool(invalid_rows),
        "incomplete": bool(invalid_rows),
        "incomplete_reason": (
            f"{len(invalid_rows)}条记录损坏，其内容未纳入详情"
            if invalid_rows
            else None
        ),
    }


def count_by_domain(rows: list[dict[str, Any]]) -> dict[str, int]:
    """The per-domain row counts backing project/home statistics (the
    rollup that keeps a cost-only project from reading as 暂无结果)."""
    counts = {domain: 0 for domain in _DOMAINS}
    for row in rows:
        domain = str(row.get("domain") or "")
        if domain in counts:
            counts[domain] += 1
    return counts


def list_unified_results(
    service: Any,
    *,
    per_project_limit: int | None = _DEFAULT_PER_PROJECT_LIMIT,
    total_limit: int | None = _DEFAULT_TOTAL_LIMIT,
) -> dict[str, Any]:
    """THE read-only unified listing across every valid project and
    both domains (newest first). Bounded by per_project_limit and
    total_limit; a cut is declared. A workspace listing failure is
    projected (workspace_error) - never an empty successful model.

    The ONLY facade calls made here are the established listing and
    projection getters; no computation, export or write entry is ever
    touched (the index is a pure read projection)."""
    try:
        summaries = service.list_projects()
    except Exception as exc:  # noqa: BLE001 - WorkbenchError and any
        # registry-level failure are the caller's honest entry error
        return {
            "rows": [],
            "projects_considered": 0,
            "invalid_projects": [],
            "incomplete": False,
            "incomplete_reason": None,
            "workspace_error": {
                "title": "工作区不可读取",
                "detail": str(exc),
                "advice": "请检查工作区路径与权限后重试。",
            },
        }
    rows: list[dict[str, Any]] = []
    invalid_projects: list[str] = []
    considered = 0

    def _workspace_error(exc: Exception) -> dict[str, Any]:
        return {
            "rows": [],
            "projects_considered": 0,
            "invalid_projects": [],
            "incomplete": False,
            "incomplete_reason": None,
            "workspace_error": {
                "title": "工作区不可读取",
                "detail": str(exc),
                "advice": "请检查工作区路径与权限后重试。",
            },
        }

    for summary in summaries:
        if summary.get("invalid"):
            invalid_projects.append(str(summary.get("project_dir")))
            continue
        project_ref = str(summary.get("project_dir"))
        project_name = str(summary.get("name") or project_ref)
        considered += 1
        try:
            run_rows = service.list_run_summaries(
                project_ref,
                limit=per_project_limit,
            )
        except WorkbenchError as exc:
            # a workspace-boundary failure (root vanished/unreadable)
            # aborts the whole listing honestly - never a partial
            # model pretending completeness
            return _workspace_error(exc)
        except Exception as exc:  # noqa: BLE001 - one project's failing
            # run query must not hide the other projects' healthy rows
            rows.append(
                {
                    "project_ref": project_ref,
                    "project_name": project_name,
                    "domain": DOMAIN_RELIABILITY,
                    "method": None,
                    "method_label": "未知方法",
                    "run_ref": "",
                    "created_at": None,
                    "created_at_label": "未知",
                    "object_name": None,
                    "readable": False,
                    "invalid": True,
                    "invalid_reason": f"运行历史不可读取：{exc}",
                    "status_label": "记录损坏",
                    "detail_unavailable_reason": None,
                    "route": None,
                }
            )
            run_rows = []
        for row_data in run_rows:
            rows.append(_reliability_row(project_ref, project_name, row_data))
        try:
            cost_summaries = service.list_cost_run_summaries(project_ref)
        except WorkbenchError as exc:
            return _workspace_error(exc)
        except Exception:  # noqa: BLE001 - same per-project isolation
            cost_summaries = []
        if per_project_limit is not None:
            cost_summaries = cost_summaries[:per_project_limit]
        for cost_summary in cost_summaries:
            rows.append(
                _cost_row_from_summary(
                    service, project_ref, project_name, cost_summary
                )
            )
        try:
            coef_summaries = service.list_coefficient_summaries(project_ref)
        except WorkbenchError as exc:
            return _workspace_error(exc)
        except Exception:  # noqa: BLE001 - same per-project isolation
            coef_summaries = []
        for coef_summary in coef_summaries:
            rows.append(
                _coefficient_row_from_summary(
                    project_ref, project_name, coef_summary
                )
            )
        try:
            settlement_summaries = service.list_settlement_summaries(project_ref)
        except WorkbenchError as exc:
            return _workspace_error(exc)
        except Exception:  # noqa: BLE001 - same per-project isolation
            settlement_summaries = []
        for settlement_summary in settlement_summaries:
            rows.append(
                _settlement_row_from_summary(
                    project_ref, project_name, settlement_summary
                )
            )
        try:
            market_summaries = service.list_market_summaries(project_ref)
        except WorkbenchError as exc:
            return _workspace_error(exc)
        except Exception:  # noqa: BLE001 - same per-project isolation
            market_summaries = []
        for market_summary in market_summaries:
            rows.append(
                _market_row_from_summary(project_ref, project_name, market_summary)
            )
    rows.sort(
        key=lambda row: (
            _time_sort_key(row),
            str(row.get("project_ref") or ""),
            str(row.get("run_ref") or ""),
        ),
        reverse=True,
    )
    incomplete = False
    incomplete_reason = None
    if invalid_projects:
        incomplete = True
        incomplete_reason = (
            f"{len(invalid_projects)}个项目条目损坏，其结果未纳入列表"
        )
    if total_limit is not None and len(rows) > total_limit:
        rows = rows[:total_limit]
        incomplete = True
        incomplete_reason = (
            incomplete_reason or "结果数量超过显示上限，仅显示最近的结果"
        )
    return {
        "rows": rows,
        "projects_considered": considered,
        "invalid_projects": invalid_projects,
        "incomplete": incomplete,
        "incomplete_reason": incomplete_reason,
        "workspace_error": None,
    }


_AGGREGATE_METHODS = ("elcc", "efc")


def _birth_certificate_key(row: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """R3-B3: the comparable-caliber part of a row's birth certificate
    (increment scale / placement / composition). Legacy rows without a
    certificate group together under None - honestly unknown, never
    assumed equal to any declared caliber."""
    cert = row.get("scenario_birth_certificate") or {}
    if not isinstance(cert, dict):
        cert = {}
    return (
        cert.get("increment_mw"),
        cert.get("placement"),
        cert.get("composition_mode"),
        tuple(cert.get("member_ids") or ()),
    )


def group_by_caliber(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PURE caliber grouping of aggregate rows (no IO): rows sharing the
    same scenario bank, data grade, method, scenario birth certificate
    (方法说明第四章(三) 四变量) AND EFC de-rating identity (MR-01:
    technology class + delivery year + criterion mode - 降额系数只对所属
    交付年有效) are directly comparable and form ONE group; more than one
    group means the table spans calibers, which is surfaced instead of
    silently mixed (第八章风险1)."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            row.get("scenario_bank_id"),
            row.get("effective_data_grade"),
            row.get("method"),
            *_birth_certificate_key(row),
            *(
                row.get("technology_class"),
                row.get("delivery_year"),
                row.get("auction_round"),
                row.get("criterion_mode"),
            ),
        )
        groups.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for key, members in groups.items():
        out.append(
            {
                "scenario_bank_id": key[0],
                "effective_data_grade": key[1],
                "method": key[2],
                "increment_mw": key[3],
                "placement": key[4],
                "composition_mode": key[5],
                "member_ids": list(key[6]),
                "technology_class": key[7],
                "delivery_year": key[8],
                "auction_round": key[9],
                "criterion_mode": key[10],
                "rows": members,
                "resolved_count": sum(
                    1 for m in members if not m.get("unresolved")
                ),
                "unresolved_count": sum(
                    1 for m in members if m.get("unresolved")
                ),
            }
        )
    out.sort(
        key=lambda g: (
            str(g.get("scenario_bank_id") or ""),
            str(g.get("effective_data_grade") or ""),
        )
    )
    return out


def aggregate_method_results(service: Any, method: str) -> dict[str, Any]:
    """THE read-only multi-resource aggregate (prototype step 05's
    six-resource table; for EFC this IS the per-technology-class
    de-rating table of MR-01.2 - rows are grouped by
    (technology_class, delivery_year, auction_round, criterion_mode)
    plus the scenario birth certificate, so factors from different
    classes or delivery years never share one comparable group) built
    from FORMAL results only: one project carries one formal result, so
    a per-resource table is a cross-project read projection - never a
    batch run and never a second state machine.

    Facts come from the established projection getters (no kernel call,
    no computation, no write). Rows keep their lineage (project_ref +
    run_ref + result hash); an unresolved search stays unresolved (never
    smoothed into a point value); unreadable runs are listed honestly.
    """
    if method not in _AGGREGATE_METHODS:
        raise ValueError(
            f"聚合仅支持 elcc/efc，收到 {method!r}"
        )
    rows: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    projects_considered = 0
    for summary in service.list_projects():
        if summary.get("invalid"):
            continue
        ref = str(summary.get("project_dir") or "")
        if not ref:
            continue
        try:
            formal = service.get_formal_result(ref)
        except Exception:  # noqa: BLE001 - honest per-project skip
            continue
        if not formal or formal.get("domain") != DOMAIN_RELIABILITY:
            continue
        run_ref = str(formal.get("run_ref") or "")
        if not run_ref:
            continue
        projects_considered += 1
        try:
            projection = service.get_run_projection(ref, run_ref)
        except Exception as exc:  # noqa: BLE001 - surfaced, never dropped
            unreadable.append(
                {
                    "project_ref": ref,
                    "project_name": str(summary.get("name") or ""),
                    "run_ref": run_ref,
                    "reason": str(exc)[:300],
                }
            )
            continue
        if str(projection.get("method") or "") != method:
            continue
        capacity = dict(projection.get("capacity_view") or {})
        accredited = capacity.get("accredited_capacity_mw")
        rows.append(
            {
                "project_ref": ref,
                "project_name": str(summary.get("name") or ""),
                "run_ref": run_ref,
                "method": method,
                "method_label": method_label(method),
                "resource": capacity.get("resource_or_portfolio_id"),
                "reference_capacity_mw": capacity.get("reference_capacity_mw"),
                "accredited_capacity_mw": accredited,
                "capacity_credit_fraction": capacity.get(
                    "capacity_credit_fraction"
                ),
                "search_bracket_low_mw": capacity.get("search_bracket_low_mw"),
                "search_bracket_high_mw": capacity.get(
                    "search_bracket_high_mw"
                ),
                "interpolation_method": capacity.get("interpolation_method"),
                "effective_data_grade": capacity.get("effective_data_grade"),
                # R3-B3: the birth certificate rides with every row so
                # grouping can separate calibers (None on legacy results)
                "scenario_birth_certificate": capacity.get(
                    "scenario_birth_certificate"
                ),
                # MR-01: de-rating identity columns (EFC; None elsewhere)
                "technology_class": (
                    (capacity.get("reporting") or {}).get("technology_class")
                    if isinstance(capacity.get("reporting"), dict)
                    else None
                ),
                "delivery_year": capacity.get("delivery_year"),
                "auction_round": capacity.get("auction_round"),
                "criterion_mode": capacity.get("criterion_mode"),
                "scenario_bank_id": projection.get("scenario_bank_id"),
                "result_status": projection.get("result_status"),
                "result_sha256": formal.get("result_sha256"),
                "unresolved": accredited is None,
                "route": make_route(ref, DOMAIN_RELIABILITY, run_ref),
            }
        )
    groups = group_by_caliber(rows)
    return {
        "method": method,
        "method_label": method_label(method),
        "rows": rows,
        "groups": groups,
        "cross_caliber": len(groups) > 1,
        "unreadable": unreadable,
        "projects_considered": projects_considered,
        "count": len(rows),
    }


__all__ = [
    "DOMAIN_COST",
    "DOMAIN_RELIABILITY",
    "aggregate_method_results",
    "cost_status_label",
    "group_by_caliber",
    "count_by_domain",
    "list_project_results",
    "list_unified_results",
    "make_route",
    "method_label",
    "reliability_status_label",
    "same_route",
]
