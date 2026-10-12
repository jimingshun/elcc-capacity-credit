"""项目接口（/api/projects*）。任务 D 工作 2 拆出；零行为变化。

所有导入模块级——历史事故根治面：旧巨型 ``_api`` 的函数内
``from ... import`` 会遮蔽共享绑定（两次真实事故），本包不再有
函数内导入。
"""

from __future__ import annotations

import uuid
from typing import Any

from capacity_credit.application.project_service import read_registry
from capacity_credit.application.settlement_service import (
    SettlementServiceError,
    create_settlement_project,
)
from capacity_credit.application.workbench_service import WorkbenchError
from capacity_credit.webapp_routes import route


def _project_business(h: Any, project_ref: str) -> dict[str, Any]:
    """The R5 business fields (object/province/base_year) of one
    registry entry."""
    registry = read_registry(h.workspace)
    for entry in registry.get("projects", []):
        if entry.get("project_ref") == project_ref:
            return {
                "object_name": entry.get("object_name"),
                "province": entry.get("province"),
                "base_year": entry.get("base_year"),
                "created_at": entry.get("created_at"),
            }
    return {}


def _projects(h: Any) -> list[dict[str, Any]]:
    service = h._service()
    out: list[dict[str, Any]] = []
    for summary in service.list_projects():
        entry: dict[str, Any] = {
            "project_dir": summary["project_dir"],
            "name": summary["name"],
            "invalid": summary["invalid"],
            "invalid_reason": summary.get("invalid_reason"),
            "kind": None,
            "cost_runs": None,
            "frozen": summary.get("frozen"),
            "latest_batch_state": summary.get("latest_batch_state"),
            "created_at": summary.get("created_at"),
        }
        if not summary["invalid"]:
            try:
                kind = service.get_project_kind(str(summary["project_dir"]))
                entry["kind"] = kind
                try:
                    entry["measurement_type"] = (
                        service.get_project_measurement_type(
                            str(summary["project_dir"])
                        )
                    )
                except WorkbenchError:
                    entry["measurement_type"] = None
                try:
                    info = _project_business(h, str(summary["project_dir"]))
                    entry["object_name"] = info.get("object_name")
                    entry["province"] = info.get("province")
                    entry["base_year"] = info.get("base_year")
                    if not entry.get("created_at"):
                        entry["created_at"] = info.get("created_at")
                except WorkbenchError:
                    entry["object_name"] = entry["province"] = entry["base_year"] = None
                if kind == "cost":
                    runs = [
                        s
                        for s in service.list_cost_run_summaries(
                            str(summary["project_dir"])
                        )
                        if not s.get("invalid")
                    ]
                    entry["cost_runs"] = len(runs)
            except WorkbenchError:
                entry["kind"] = None
        out.append(entry)
    return out


@route("GET", "/api/projects")
def list_projects(h: Any, qs: dict, body: Any) -> None:
    show_archived = (qs.get("archived") or ["false"])[0].lower() == "true"
    projects = _projects(h)
    for p in projects:
        try:
            p["archived"] = bool(
                h._service().get_project_archived(p["project_dir"])
            )
        except WorkbenchError:
            p["archived"] = None
        try:
            # P-A: the formal-result binding (None = 未完成) lets
            # the pages show the completion state honestly
            p["formal_result"] = h._service().get_formal_result(
                p["project_dir"]
            )
        except WorkbenchError:
            p["formal_result"] = None
    if not show_archived:
        projects = [p for p in projects if not p["archived"]]
    return h._json(200, {"projects": projects})


@route("POST", "/api/projects")
def create_project(h: Any, qs: dict, body: Any) -> None:
    name = str(body.get("name") or "")
    description = str(body.get("description") or "")
    project_kind = str(body.get("project_kind") or "cost")
    composition = body.get("resource_composition")
    if project_kind == "reliability" and not isinstance(composition, dict):
        # the facade requires the three-state declaration for
        # reliability projects; the web form asks for it and this
        # boundary fills the neutral default (honest 待确认)
        composition = {"storage": "unconfirmed", "hydro": "unconfirmed"}
    if project_kind == "settlement":
        # Step 3 S1-M1：年度结算项目——settlement_config 七字段创建即写入
        # 并锁定＋年度唯一门（Q-S6 ）；幂等＝同 client_request_id
        # 重试返回同一项目（不误触唯一门）
        try:
            handle = create_settlement_project(
                workspace_root=h.workspace,
                request_id=str(
                    body.get("client_request_id") or f"web-{uuid.uuid4().hex}"
                ),
                name=name,
                description=description,
                config=body.get("settlement_config"),
            )
        except SettlementServiceError as exc:
            return h._json(400, {"kind": exc.kind, "message": exc.message})
        return h._json(
            200,
            {"project_ref": handle["project_ref"], "name": handle["name"],
             "settlement_year": handle["settlement_year"]},
        )
    handle = h._service().create_project(
        request_id=f"web-{uuid.uuid4().hex}",
        name=name,
        description=description,
        resource_composition=composition,
        project_kind=project_kind,
        measurement_type=body.get("measurement_type"),
        object_name=body.get("object_name"),
        province=body.get("province"),
        base_year=body.get("base_year"),
    )
    h._json(
        200,
        {"project_ref": handle["project_ref"], "name": handle["name"]},
    )


@route("POST", "/api/projects/archive")
def archive_project(h: Any, qs: dict, body: Any) -> None:
    r = h._service().set_project_archived(
        str(body.get("project_ref") or ""), archived=True
    )
    return h._json(200, r)


@route("POST", "/api/projects/restore")
def restore_project(h: Any, qs: dict, body: Any) -> None:
    r = h._service().set_project_archived(
        str(body.get("project_ref") or ""), archived=False
    )
    return h._json(200, r)


@route("POST", "/api/projects/composition")
def set_composition(h: Any, qs: dict, body: Any) -> None:
    # 用户2026-09-17：创建表单只留名称，储能/水电资源构成
    # 申报后移到步骤01（输入数据清单），此端点承接后移的申报
    r = h._service().set_resource_composition(
        str(body.get("project_ref") or ""),
        body.get("resource_composition"),
    )
    return h._json(200, r)


@route("POST", "/api/projects/copy")
def copy_project(h: Any, qs: dict, body: Any) -> None:
    # P-A copy-as-new-project: a NEW project with the source's
    # type/business fields plus provenance; the optional
    # client_request_id makes a retried request return the SAME
    # copy (creation idempotency), so a double click cannot
    # create two
    r = h._service().copy_project(
        str(body.get("project_ref") or ""),
        request_id=str(
            body.get("client_request_id")
            or f"web-{uuid.uuid4().hex}"
        ),
        source_run_ref=body.get("source_run_ref"),
    )
    return h._json(200, r)
