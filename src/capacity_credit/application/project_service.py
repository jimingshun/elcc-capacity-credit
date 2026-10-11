"""V1-T2-A project registration (the minimal project identity carrier).

A created project is identified by an internally generated
``project_ref`` (a uuid4 hex safe path component) and persisted in ONE
workspace-level registry file (``project_registry.json``) holding the
business fields the user actually entered: display name, source
description and the declared resource composition. NO import batch,
data version, quality report or freeze record is fabricated here - a
freshly created project is 待导入 until the real import chain (T2-B)
runs.

Idempotency: the caller-supplied ``request_id`` is stored per entry;
retrying the SAME request with the SAME effective parameters returns
the SAME project (never a second one), while the same request_id with
different parameters is an honest conflict. The same NAME under a
different request is a DISTINCT project (display names are not
identities; existing projects are never overwritten).

Writes go through the established atomic primitives; a failed write
means no project exists (the created directory is rolled back when it
is still empty and owned by this request).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    hash_payload,
    safe_filename_component,
)

REGISTRY_FILE = "project_registry.json"
_REGISTRY_SCHEMA_VERSION = 1
_MAX_NAME_LEN = 200
_MAX_DESCRIPTION_LEN = 2000
_ALLOWED_RESOURCE_KEYS = ("storage", "hydro")
_ALLOWED_RESOURCE_STATES = ("included", "excluded", "unconfirmed")
# night-5: the project DOMAIN. A reliability project declares the
# resource composition; a cost project (fixed cost / Net CONE / Gross
# CONE) never does. Entries written before this field existed have no
# project_kind and read as reliability - old projects are never
# re-labelled.
# P-A (four-module design 2026-09-16 §3.1): settlement and market
# domains join the registry; a legacy entry still reads as reliability.
_PROJECT_KINDS = ("reliability", "cost", "settlement", "market")

# R1 restructure: a project is created AS ONE measurement type and
# locked to it. Cost kinds name their single cost method; reliability
# kinds name the wired reliability method. Entries written before this
# field existed carry no measurement_type (legacy) and are governed by
# the domain guard only.
# P-A: the full four-module type registry. Each type belongs to exactly
# ONE domain (_DOMAIN_OF_TYPE) - the pair is validated at creation,
# never silently re-mapped.
_MEASUREMENT_TYPES = (
    "cost_fixed",
    "cost_gross_cone",
    "cost_net_cone",
    "adequacy",
    "elcc",
    "efc",
    "demand_target_capacity",
    "demand_capacity_gap",
    "demand_coefficient",
    "settlement_annual",
    "market_simulation",
)

_DOMAIN_OF_TYPE: dict[str, str] = {
    "cost_fixed": "cost",
    "cost_gross_cone": "cost",
    "cost_net_cone": "cost",
    "adequacy": "reliability",
    "elcc": "reliability",
    "efc": "reliability",
    "demand_target_capacity": "reliability",
    "demand_capacity_gap": "reliability",
    "demand_coefficient": "reliability",
    "settlement_annual": "settlement",
    "market_simulation": "market",
}


class ProjectServiceError(ValueError):
    """Light-weight project registration error (display kind + reason).

    kind: invalid_input / request_conflict / unavailable. A display
    category, not a persistent diagnostic-code family; never reuses
    WORKER or business codes."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _registry_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / REGISTRY_FILE


def read_registry(workspace_root: Path | str) -> dict[str, Any]:
    """The parsed workspace project registry. A missing file is an empty
    registry; a corrupt one is an honest ``unavailable`` error - never a
    silent empty list."""
    path = _registry_path(workspace_root)
    if not path.is_file():
        return {"schema_version": _REGISTRY_SCHEMA_VERSION, "projects": []}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectServiceError(
            "unavailable", f"项目登记文件损坏，无法读取：{exc}"
        ) from None
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("projects"), list)
    ):
        raise ProjectServiceError(
            "unavailable", "项目登记文件结构无效（缺少 projects 列表）"
        )
    return parsed


def _atomic_write_registry(path: Path, registry: dict[str, Any]) -> None:
    """The single registry write (atomic primitives; the payload is the
    canonical stable dump so identities stay comparable)."""
    atomic_write_bytes(path, stable_dump(registry))


from capacity_credit.application.atomic_io import stable_dump  # noqa: E402


def project_kind_of(entry: dict[str, Any]) -> str:
    """The project domain of a registry entry: the stored project_kind
    when present, 'reliability' for legacy entries written before the
    field existed (a missing field is never read as 'cost')."""
    value = entry.get("project_kind")
    return str(value) if value in _PROJECT_KINDS else "reliability"


def measurement_type_of(entry: dict[str, Any]) -> str | None:
    """The LOCKED measurement type of a registry entry (None for legacy
    entries written before the field existed - those stay governed by
    the domain guard only)."""
    value = entry.get("measurement_type")
    return str(value) if value in _MEASUREMENT_TYPES else None


def _validate_fields(
    name: Any,
    description: Any,
    resource_composition: Any,
    project_kind: Any,
    measurement_type: Any,
) -> tuple[str, str, dict[str, str] | None, str | None]:
    if not isinstance(name, str) or not name.strip():
        raise ProjectServiceError("invalid_input", "项目名称必须为非空文本")
    name = name.strip()
    if len(name) > _MAX_NAME_LEN:
        raise ProjectServiceError(
            "invalid_input", f"项目名称过长（最多{_MAX_NAME_LEN}字符）"
        )
    if any(ch in name for ch in ('/', '\\', '\n', '\r', '\t')) or name in (
        ".",
        "..",
    ):
        raise ProjectServiceError(
            "invalid_input", "项目名称包含不允许的字符（分隔符、换行或相对路径段）"
        )
    if any(ord(ch) < 32 for ch in name):
        raise ProjectServiceError("invalid_input", "项目名称包含控制字符")
    if not isinstance(description, str):
        raise ProjectServiceError("invalid_input", "项目说明必须为文本")
    description = description.strip()
    if len(description) > _MAX_DESCRIPTION_LEN:
        raise ProjectServiceError(
            "invalid_input", f"项目说明过长（最多{_MAX_DESCRIPTION_LEN}字符）"
        )
    if project_kind not in _PROJECT_KINDS:
        raise ProjectServiceError(
            "invalid_input",
            "项目领域必须为 reliability/cost 之一",
        )
    locked: str | None = None
    if measurement_type is not None:
        # R1: a project created with a measurement type is locked to it
        if measurement_type not in _MEASUREMENT_TYPES:
            raise ProjectServiceError(
                "invalid_input",
                "测算类型必须为 "
                + "/".join(_MEASUREMENT_TYPES)
                + " 之一",
            )
        # P-A: the type must belong to the declared domain (the pair
        # table is the single authority - no domain-specific special
        # cases beyond the cost-composition rule below)
        if _DOMAIN_OF_TYPE[str(measurement_type)] != project_kind:
            raise ProjectServiceError(
                "invalid_input",
                f"测算类型 {measurement_type} 属于 "
                f"{_DOMAIN_OF_TYPE[str(measurement_type)]} 域，与项目领域 "
                f"{project_kind} 不匹配",
            )
        locked = str(measurement_type)
    if project_kind != "reliability":
        # only the RELIABILITY domain declares a resource composition
        # (night-5 cost rule, P-A: settlement/market behave the same as
        # cost) - requiring none and refusing one keeps the domains
        # honestly separated (no silent default)
        if resource_composition is not None:
            raise ProjectServiceError(
                "invalid_input",
                f"{project_kind}域项目不申报储能/水电资源构成；请省略资源构成",
            )
        return name, description, None, locked
    composition = _validate_composition(resource_composition)
    return name, description, composition, locked


def _validate_composition(resource_composition: Any) -> dict[str, str]:
    """The three-state storage/hydro declaration (shared by creation
    and the post-creation update path - ONE vocabulary, no second
    schema)."""
    if not isinstance(resource_composition, dict):
        raise ProjectServiceError(
            "invalid_input",
            "资源构成必须为对象（含 storage/hydro 的三态声明）",
        )
    composition: dict[str, str] = {}
    for key in _ALLOWED_RESOURCE_KEYS:
        value = resource_composition.get(key)
        if value not in _ALLOWED_RESOURCE_STATES:
            raise ProjectServiceError(
                "invalid_input",
                f"资源构成 {key} 必须为 included/excluded/unconfirmed 三态之一",
            )
        composition[key] = str(value)
    return composition


def create_project(
    *,
    workspace_root: Path | str,
    request_id: str,
    name: str,
    description: str,
    resource_composition: dict[str, str] | None = None,
    project_kind: str = "reliability",
    measurement_type: str | None = None,
    object_name: str | None = None,
    province: str | None = None,
    base_year: int | None = None,
) -> dict[str, Any]:
    """Create (or idempotently re-return) one registered project.

    - the same request_id with the same effective parameters returns
      the SAME project (a retried creation never produces a second
      project);
    - the same request_id with different parameters is a conflict;
    - the same NAME under a different request is a distinct project -
      existing projects are never overwritten;
    - a reliability project requires the resource composition; a cost
      project (night-5) must omit it (the domains stay separated);
    - R1: a project may be created with a measurement_type (locked);
      omitting it keeps a legacy domain-guarded project;
    - everything is validated BEFORE any write; a failed registry write
      rolls the created directory back (only when still empty and owned
      by this request) and re-raises - no half-created project ever
      reaches the formal list."""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ProjectServiceError("invalid_input", "request_id 必须为非空字符串")
    name, description, composition, locked = _validate_fields(
        name, description, resource_composition, project_kind, measurement_type
    )
    root = Path(workspace_root)
    if not root.is_dir():
        raise ProjectServiceError("unavailable", f"工作区不存在：{root}")
    try:
        params_hash = hash_payload(
            {
                "name": name,
                "description": description,
                "resource_composition": composition,
                "project_kind": project_kind,
                "measurement_type": locked,
            }
        )
    except ValueError as exc:
        # hash_payload rejects path-like text in business identities -
        # surface it as the service's own readable refusal, never an
        # internal English error
        raise ProjectServiceError(
            "invalid_input",
            f"项目说明包含不允许的内容（疑似本机路径文本）：{exc}",
        ) from None
    registry = read_registry(root)
    projects = registry["projects"]
    for entry in projects:
        if entry.get("request_id") == request_id:
            if entry.get("params_hash") != params_hash:
                raise ProjectServiceError(
                    "request_conflict",
                    f"同一创建请求 {request_id!r} 已使用不同参数："
                    "明确创建另一个项目请使用新请求",
                )
            return {
                "project_ref": entry["project_ref"],
                "name": entry["name"],
                "request_id": request_id,
            }
    project_ref = uuid.uuid4().hex
    while (root / project_ref).exists():
        project_ref = uuid.uuid4().hex
    project_dir = root / project_ref
    try:
        project_dir.mkdir()
    except OSError as exc:
        raise ProjectServiceError(
            "unavailable", f"项目目录创建失败：{exc}"
        ) from None
    entry = {
        "project_ref": project_ref,
        "name": name,
        "description": description,
        "resource_composition": composition,
        "project_kind": project_kind,
        "measurement_type": locked,
        "object_name": (str(object_name).strip() if object_name else None),
        "province": (str(province).strip() if province else None),
        "base_year": (int(base_year) if base_year else None),
        "created_at": datetime.now(UTC).isoformat(),
        "request_id": request_id,
        "params_hash": params_hash,
    }
    projects.append(entry)
    try:
        _atomic_write_registry(_registry_path(root), registry)
    except BaseException:
        # roll back ONLY this request's own artifact: the just-created
        # empty directory (never an existing project or an unknown dir)
        try:
            if project_dir.is_dir() and not any(project_dir.iterdir()):
                project_dir.rmdir()
        except OSError:
            pass  # keep the uncertainty; the registry write already failed
        raise
    return {
        "project_ref": project_ref,
        "name": name,
        "request_id": request_id,
        "measurement_type": locked,
    }


def set_project_archived(
    workspace_root: Path | str, project_ref: str, *, archived: bool
) -> dict[str, Any]:
    """Soft archive / restore (R5b): the registry entry carries an
    ``archived`` flag; the default project list hides archived entries
    and the archived list (?archived=true) shows them. The project
    directory and its runs are never touched."""
    root = Path(workspace_root)
    if not root.is_dir():
        raise ProjectServiceError("unavailable", f"工作区不存在：{root}")
    registry = read_registry(root)
    for entry in registry["projects"]:
        if entry.get("project_ref") == project_ref:
            entry["archived"] = bool(archived)
            _atomic_write_registry(_registry_path(root), registry)
            return {"project_ref": project_ref, "archived": bool(archived)}
    raise ProjectServiceError(
        "invalid_input", f"项目不存在或不是本工作区的正式项目：{project_ref}"
    )


def set_resource_composition(
    workspace_root: Path | str,
    project_ref: str,
    resource_composition: Any,
) -> dict[str, Any]:
    """Update the declared storage/hydro composition AFTER creation
    (user ruling 2026-09-17: the creation form only asks for the name;
    the declaration moves to the data-input step). Registry entry only
    - imports/runs are never touched. Honest refusals:

    - a project whose formal result is already bound is read-only
      (P-A: one project, one formal result - the declaration that
      produced it can no longer change);
    - the cost domain never declares a composition (night-5 rule,
      same boundary as creation);
    - values must be the SAME three-state vocabulary as creation."""
    root = Path(workspace_root)
    if not root.is_dir():
        raise ProjectServiceError("unavailable", f"工作区不存在：{root}")
    composition = _validate_composition(resource_composition)
    registry = read_registry(root)
    entry = _find_entry(registry, project_ref)
    if project_kind_of(entry) != "reliability":
        raise ProjectServiceError(
            "invalid_input",
            "仅可靠性域项目申报储能/水电资源构成；成本域项目请省略资源构成",
        )
    binding = entry.get("formal_result")
    if isinstance(binding, dict) and binding.get("run_ref"):
        raise ProjectServiceError(
            "project_completed",
            "该项目已完成业务测算（正式结果已绑定 "
            f"{binding.get('domain')}／{str(binding.get('run_ref'))[:8]}…），"
            "资源构成申报不可再修改",
        )
    entry["resource_composition"] = composition
    _atomic_write_registry(_registry_path(root), registry)
    return {"project_ref": project_ref, "resource_composition": composition}


def _find_entry(
    registry: dict[str, Any], project_ref: str
) -> dict[str, Any]:
    for entry in registry["projects"]:
        if entry.get("project_ref") == project_ref:
            return entry
    raise ProjectServiceError(
        "invalid_input", f"项目不存在或不是本工作区的正式项目：{project_ref}"
    )


def get_formal_result(
    workspace_root: Path | str, project_ref: str
) -> dict[str, Any] | None:
    """THE unique formal-result binding of one project (P-A). None when
    the project has no formal result yet; an unknown project is an
    honest invalid_input refusal."""
    root = Path(workspace_root)
    if not root.is_dir():
        raise ProjectServiceError("unavailable", f"工作区不存在：{root}")
    registry = read_registry(root)
    entry = _find_entry(registry, project_ref)
    binding = entry.get("formal_result")
    return dict(binding) if isinstance(binding, dict) else None


def bind_formal_result(
    workspace_root: Path | str,
    project_ref: str,
    *,
    domain: str,
    run_ref: str,
    result_sha256: str | None = None,
) -> dict[str, Any]:
    """Bind THE one formal result of a project (P-A §3.3 of the design):
    idempotent for the SAME (domain, run_ref); a DIFFERENT run on an
    already-bound project is a hard refusal (a formal result is never
    silently replaced). Callers must validate the run record BEFORE
    binding (completed + valid result) - this function owns only the
    registry invariant."""
    root = Path(workspace_root)
    if not root.is_dir():
        raise ProjectServiceError("unavailable", f"工作区不存在：{root}")
    if not str(run_ref).strip():
        raise ProjectServiceError("invalid_input", "run_ref 必须为非空字符串")
    registry = read_registry(root)
    entry = _find_entry(registry, project_ref)
    existing = entry.get("formal_result")
    if isinstance(existing, dict) and existing.get("run_ref"):
        if (
            existing.get("run_ref") == str(run_ref)
            and existing.get("domain") == str(domain)
        ):
            return dict(existing)
        raise ProjectServiceError(
            "request_conflict",
            "该项目已有正式结果"
            f"（{existing.get('domain')}／{existing.get('run_ref')}），"
            "不能改绑其他运行；修改参数重新测算请复制为新项目",
        )
    binding = {
        "domain": str(domain),
        "run_ref": str(run_ref),
        "result_sha256": (str(result_sha256) if result_sha256 else None),
        "bound_at": datetime.now(UTC).isoformat(),
    }
    entry["formal_result"] = binding
    _atomic_write_registry(_registry_path(root), registry)
    return binding


def copy_project(
    workspace_root: Path | str,
    *,
    source_project_ref: str,
    request_id: str,
    source_run_ref: str | None = None,
) -> dict[str, Any]:
    """Copy-as-new-project (P-A §3.5): a NEW project with the source's
    measurement type and business fields, recording the source project,
    the source run (the run whose snapshot prefills the form) and the
    copy time. The source entry is never modified; the same request_id
    re-returns the same copy (creation idempotency applies)."""
    root = Path(workspace_root)
    if not root.is_dir():
        raise ProjectServiceError("unavailable", f"工作区不存在：{root}")
    registry = read_registry(root)
    source = _find_entry(registry, source_project_ref)
    source_name = str(source.get("name") or "")
    handle = create_project(
        workspace_root=root,
        request_id=request_id,
        name=source_name + "（副本）",
        description=str(source.get("description") or ""),
        resource_composition=source.get("resource_composition"),
        project_kind=project_kind_of(source),
        measurement_type=measurement_type_of(source),
        object_name=source.get("object_name"),
        province=source.get("province"),
        base_year=source.get("base_year"),
    )
    # amend the fresh copy with the provenance fields (second atomic
    # write; re-setting the same values on a retried request is a no-op
    # in content)
    registry = read_registry(root)
    entry = _find_entry(registry, handle["project_ref"])
    entry["source_project_ref"] = source_project_ref
    entry["source_run_ref"] = (
        str(source_run_ref) if source_run_ref else None
    )
    entry["copied_at"] = entry.get("copied_at") or datetime.now(UTC).isoformat()
    _atomic_write_registry(_registry_path(root), registry)
    return {
        "project_ref": handle["project_ref"],
        "name": handle["name"],
        "source_project_ref": source_project_ref,
        "source_run_ref": entry["source_run_ref"],
        "copied_at": entry["copied_at"],
    }


__all__ = [
    "REGISTRY_FILE",
    "ProjectServiceError",
    "bind_formal_result",
    "copy_project",
    "create_project",
    "get_formal_result",
    "measurement_type_of",
    "project_kind_of",
    "read_registry",
    "set_project_archived",
    "set_resource_composition",
]
