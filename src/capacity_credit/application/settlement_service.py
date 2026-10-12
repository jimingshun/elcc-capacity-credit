"""年度结算项目骨架（Step 3 S1-M1；主责设计 2026-09-16-annual-settlement-design.md §2/§5）.

创建：七字段基础信息创建即写入并锁定（`settlement/year_config.json`，
原子写＋不可变）＋**年度唯一门**（Q-S6：同工作区同年度已存在非
归档结算项目→诚实拒绝并点名）；幂等重试（同 request_id 返回同一项目，
不误触唯一门）。

价表（Q-S1：录入制＋强制来源）：`settlement/price_table/` 版本化
（vNNN.json 不可变＋manifest 最后写入记录 current 与历史）；数值>0、
政策来源必填、rule_version 必填；重录＝新版本，历史永不覆盖。

Net CONE 参考列表：只读派生（扫描成本域已完成 cost_net_cone 运行），
供价表录入页并列展示——**只展示不代填**（工具测算值不冒充政策价）。
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    hash_payload,
    stable_dump,
)

SETTLEMENT_DIR = "settlement"
YEAR_CONFIG_FILE = "year_config.json"
PRICE_TABLE_DIR = "price_table"
PRICE_TABLE_MANIFEST = "price_table_manifest.json"


class SettlementServiceError(ValueError):
    """A settlement use-case refusal (display kind + reason)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class SettlementConfigInput(BaseModel):
    """设计 §2 七字段（创建即锁定；修改走复制为新项目）。"""

    model_config = ConfigDict(extra="forbid")

    settlement_year: int = Field(ge=2020)
    region: str = Field(min_length=1)
    mechanism_version: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    data_cutoff: str = Field(min_length=1)
    price_basis: str = Field(min_length=1)
    rule_notes: str = Field(min_length=1)


_REQUIRED_CONFIG_KEYS = tuple(SettlementConfigInput.model_fields.keys())


def validate_settlement_config(payload: Any) -> dict[str, Any]:
    """七字段校验：缺失逐项聚合（一次列出全部缺失，不笼统拒绝）。"""
    if not isinstance(payload, dict):
        raise SettlementServiceError(
            "invalid_input", "settlement_config 必须为对象（七字段基础信息）"
        )
    missing = [k for k in _REQUIRED_CONFIG_KEYS if payload.get(k) in (None, "")]
    if missing:
        raise SettlementServiceError(
            "invalid_input",
            "结算基础信息缺字段：" + "、".join(missing),
        )
    try:
        data = SettlementConfigInput.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise SettlementServiceError(
            "invalid_input", f"结算基础信息无效：{exc}"
        ) from exc
    return data.model_dump(mode="json")


def read_year_config(project_dir: Path | str) -> dict[str, Any] | None:
    """读年度基础信息（None＝未写入）。"""
    import json

    path = Path(project_dir) / SETTLEMENT_DIR / YEAR_CONFIG_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SettlementServiceError(
            "unavailable", f"年度基础信息不可读取：{exc}"
        ) from None


def _settlement_projects(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        e
        for e in registry.get("projects", [])
        if e.get("measurement_type") == "settlement_annual"
        and not e.get("archived")
    ]


def _year_unique_gate(
    workspace_root: Path, registry: dict[str, Any], year: int, exclude_ref: str | None = None
) -> None:
    """Q-S6：同工作区同年度只允许一个非归档结算项目。"""
    for entry in _settlement_projects(registry):
        ref = str(entry.get("project_ref") or "")
        if not ref or ref == exclude_ref:
            continue
        config = read_year_config(workspace_root / ref)
        if config and config.get("settlement_year") == year:
            raise SettlementServiceError(
                "request_conflict",
                f"年度 {year} 已有结算项目「{entry.get('name') or ref}」"
                "（一年只允许一个正式结算项目）；修改口径请复制为新项目",
            )


def create_settlement_project(
    *,
    workspace_root: Path | str,
    request_id: str,
    name: str,
    description: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """创建年度结算项目：七字段创建即写入＋年度唯一门＋幂等重试。

    幂等：同 request_id 已存在且配置一致→返回同一项目（不误触唯一门）；
    同 request_id 配置不同→request_conflict（create_project 同款纪律）。"""
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        create_project,
        read_registry,
    )

    root = Path(workspace_root)
    data = validate_settlement_config(config)
    registry = read_registry(root)
    # 幂等重试优先于唯一门：找到自己的 request_id 且配置一致即返回
    for entry in registry.get("projects", []):
        if entry.get("request_id") == request_id:
            ref = str(entry.get("project_ref") or "")
            existing = read_year_config(root / ref) if ref else None
            if existing is not None and existing.get("settlement_year") == data["settlement_year"]:
                return {
                    "project_ref": ref,
                    "name": entry.get("name"),
                    "request_id": request_id,
                    "settlement_year": existing["settlement_year"],
                }
            break  # request_id 在但配置/类型不符 → 交回 create_project 
    _year_unique_gate(root, registry, int(data["settlement_year"]))
    try:
        handle = create_project(
            workspace_root=root,
            request_id=request_id,
            name=name,
            description=description,
            project_kind="settlement",
            measurement_type="settlement_annual",
        )
    except ProjectServiceError as exc:
        raise SettlementServiceError(exc.kind, exc.message) from None
    project_ref = str(handle["project_ref"])
    year_config = {
        "settlement_year_config_schema_version": "1.0",
        **data,
        "created_at": datetime.now(UTC).isoformat(),
        "locked": True,
    }
    atomic_write_bytes(
        root / project_ref / SETTLEMENT_DIR / YEAR_CONFIG_FILE,
        stable_dump(year_config),
        immutable=True,
    )
    return {
        "project_ref": project_ref,
        "name": handle["name"],
        "request_id": request_id,
        "settlement_year": data["settlement_year"],
    }


# ---------------------------------------------------------------------------
# 补偿价表（Q-S1：录入制＋强制来源；版本化）
# ---------------------------------------------------------------------------


def write_price_table(
    *,
    workspace_root: Path | str,
    project_ref: str,
    compensation_standard: Any,
    policy_source: Any,
    rule_version: Any,
    by_resource_type: Any = None,
) -> dict[str, Any]:
    """录入价表＝新版本（历史保留）。政策来源必填（R-ST-01 的数据源）。"""
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        _find_entry,
        read_registry,
    )

    root = Path(workspace_root)
    try:
        registry = read_registry(root)
        entry = _find_entry(registry, project_ref)
    except ProjectServiceError as exc:
        raise SettlementServiceError(exc.kind, exc.message) from None
    if entry.get("measurement_type") != "settlement_annual":
        raise SettlementServiceError(
            "invalid_input",
            "价表录入需要年度容量补偿结算项目（当前项目类型不符）",
        )
    try:
        standard = float(compensation_standard)
    except (TypeError, ValueError):
        raise SettlementServiceError(
            "invalid_input", "补偿标准必须为数值（元/kW·年）"
        ) from None
    if standard <= 0:
        raise SettlementServiceError(
            "invalid_input", "补偿标准必须 >0（元/kW·年）"
        )
    source = str(policy_source or "").strip()
    if not source:
        raise SettlementServiceError(
            "invalid_input",
            "政策来源必填（政策文件文号/条文）——无来源的价表不得进入正式结算链路",
        )
    version_label = str(rule_version or "").strip()
    if not version_label:
        raise SettlementServiceError("invalid_input", "rule_version 必填")
    by_type = None
    if by_resource_type is not None:
        if not isinstance(by_resource_type, dict) or not by_resource_type:
            raise SettlementServiceError(
                "invalid_input", "分类补偿开启时须给出资源类型→标准表"
            )
        by_type = {}
        for rtype, val in by_resource_type.items():
            try:
                num = float(val)
            except (TypeError, ValueError):
                raise SettlementServiceError(
                    "invalid_input", f"分类补偿 {rtype} 的标准必须为数值"
                ) from None
            if num <= 0:
                raise SettlementServiceError(
                    "invalid_input", f"分类补偿 {rtype} 的标准必须 >0"
                )
            by_type[str(rtype)] = num

    base = root / project_ref / SETTLEMENT_DIR / PRICE_TABLE_DIR
    base.mkdir(parents=True, exist_ok=True)
    manifest_path = base / PRICE_TABLE_MANIFEST
    manifest: dict[str, Any] = {"versions": [], "current": None}
    if manifest_path.exists():
        import json

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version_no = len(manifest["versions"]) + 1
    record = {
        "price_table_schema_version": "1.0",
        "version": version_no,
        "compensation_standard": standard,
        "policy_source": source,
        "rule_version": version_label,
        "by_resource_type": by_type,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    version_file = f"v{version_no:03d}.json"
    atomic_write_bytes(base / version_file, stable_dump(record), immutable=True)
    manifest["versions"].append(
        {"version": version_no, "file": version_file, "sha256": hash_payload(record)}
    )
    manifest["current"] = version_no
    # manifest 最后写入（版本工件不可变在先）
    atomic_write_bytes(manifest_path, stable_dump(manifest), immutable=False)
    return {"version": version_no, "price_table": record}


def read_price_table(
    workspace_root: Path | str, project_ref: str
) -> dict[str, Any]:
    """读价表：当前版本＋全部历史（未录入→current None、history 空）。"""
    import json

    base = Path(workspace_root) / project_ref / SETTLEMENT_DIR / PRICE_TABLE_DIR
    manifest_path = base / PRICE_TABLE_MANIFEST
    if not manifest_path.exists():
        return {"current": None, "history": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    history: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in manifest.get("versions", []):
        record = json.loads((base / item["file"]).read_text(encoding="utf-8"))
        history.append(record)
        if item.get("version") == manifest.get("current"):
            current = record
    return {"current": current, "history": history}


def list_net_cone_references(workspace_root: Path | str) -> list[dict[str, Any]]:
    """Net CONE 正式结果只读列表（价表录入页的定价参考；零计算零写入）。"""
    from capacity_credit.application import cost_service
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        read_registry,
    )

    root = Path(workspace_root)
    try:
        registry = read_registry(root)
    except ProjectServiceError:
        return []
    rows: list[dict[str, Any]] = []
    for entry in registry.get("projects", []):
        ref = str(entry.get("project_ref") or "")
        if not ref or entry.get("project_kind") != "cost":
            continue
        for summary in cost_service.list_cost_runs(root / ref):
            if summary.get("invalid") or summary.get("cost_kind") != "cost_net_cone":
                continue
            if summary.get("status") != "completed":
                continue
            try:
                record = cost_service.read_cost_run(
                    root / ref, str(summary.get("run_ref") or "")
                )
            except cost_service.CostServiceError:
                continue
            result = record.get("result") or {}
            caliber = result.get("caliber") or {}
            manifest = record.get("manifest") or {}
            rows.append(
                {
                    "project_ref": ref,
                    "project_name": entry.get("name") or ref,
                    "run_ref": summary.get("run_ref"),
                    "net_cone_raw": result.get("net_cone_raw"),
                    "region": caliber.get("region"),
                    "assessment_year": caliber.get("assessment_year"),
                    "price_year": caliber.get("price_year"),
                    "result_sha256": manifest.get("result_sha256"),
                }
            )
    rows.sort(key=lambda r: str(r.get("run_ref") or ""))
    return rows


# ---------------------------------------------------------------------------
# 主体台账（Step 3 S1-M2，设计 §3.1：subjects.json 版本化，历史保留）
# ---------------------------------------------------------------------------

SUBJECTS_DIR = "subjects"
SUBJECTS_MANIFEST = "subjects_manifest.json"
_RESOURCE_TYPES = ("coal", "gas", "hydro", "storage", "solar", "wind", "other")


def standard_for_resource_type(
    price: dict[str, Any], resource_type: Any
) -> tuple[float, str]:
    """分类补偿取价（Q-S1；收尾欠账③）：价表开启 by_resource_type 后按
    主体资源类型取价；类型缺项回退基础标准并如实返回来源
    （classified/base/base_fallback——公式侧披露，不静默换价）。"""
    base = float(price["compensation_standard"])
    by_type = price.get("by_resource_type")
    if not by_type:
        return base, "base"
    rtype = str(resource_type or "").strip()
    if rtype in by_type:
        return float(by_type[rtype]), "classified"
    return base, "base_fallback"


def _require_settlement_project(workspace_root: Path | str, project_ref: str) -> Path:
    """共享前置：项目存在且类型锁定为 settlement_annual。"""
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        _find_entry,
        read_registry,
    )

    root = Path(workspace_root)
    try:
        registry = read_registry(root)
        entry = _find_entry(registry, project_ref)
    except ProjectServiceError as exc:
        raise SettlementServiceError(exc.kind, exc.message) from None
    if entry.get("measurement_type") != "settlement_annual":
        raise SettlementServiceError(
            "invalid_input", "该操作需要年度容量补偿结算项目（当前项目类型不符）"
        )
    return root / project_ref


def write_subjects(
    *,
    workspace_root: Path | str,
    project_ref: str,
    subjects: Any,
) -> dict[str, Any]:
    """台账修改＝新版本（历史保留）。主体标识同版本唯一；容量 ≥0；
    空台账拒绝（R-ST-03 数据源：台账非空才可进月度检查）。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    if not isinstance(subjects, list) or not subjects:
        raise SettlementServiceError(
            "invalid_input", "主体台账必须为非空列表（至少一个结算主体）"
        )
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for item in subjects:
        if not isinstance(item, dict):
            raise SettlementServiceError("invalid_input", "台账条目必须为对象")
        sid = str(item.get("subject_id") or "").strip()
        if not sid:
            raise SettlementServiceError("invalid_input", "主体标识必填")
        if sid in seen:
            raise SettlementServiceError(
                "invalid_input", f"主体标识重复：{sid}（同版本内必须唯一）"
            )
        seen.add(sid)
        rtype = str(item.get("resource_type") or "").strip()
        if rtype not in _RESOURCE_TYPES:
            raise SettlementServiceError(
                "invalid_input",
                f"资源类型必须为 {'/'.join(_RESOURCE_TYPES)} 之一（实际 {rtype!r}）",
            )
        try:
            capacity = float(item.get("declared_capacity_mw"))
        except (TypeError, ValueError):
            raise SettlementServiceError(
                "invalid_input", f"主体 {sid} 申报容量基线必须为数值（MW）"
            ) from None
        if capacity < 0:
            raise SettlementServiceError(
                "invalid_input", f"主体 {sid} 申报容量基线必须 ≥0"
            )
        clean.append(
            {
                "subject_id": sid,
                "resource_type": rtype,
                "declared_capacity_mw": capacity,
                "note": str(item.get("note") or ""),
            }
        )
    base = project_dir / SETTLEMENT_DIR / SUBJECTS_DIR
    base.mkdir(parents=True, exist_ok=True)
    manifest_path = base / SUBJECTS_MANIFEST
    manifest: dict[str, Any] = {"versions": [], "current": None}
    if manifest_path.exists():
        import json

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version_no = len(manifest["versions"]) + 1
    record = {
        "subjects_schema_version": "1.0",
        "version": version_no,
        "subjects": clean,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    version_file = f"v{version_no:03d}.json"
    atomic_write_bytes(base / version_file, stable_dump(record), immutable=True)
    manifest["versions"].append(
        {"version": version_no, "file": version_file, "sha256": hash_payload(record)}
    )
    manifest["current"] = version_no
    atomic_write_bytes(manifest_path, stable_dump(manifest), immutable=False)
    return {"version": version_no, "subjects": clean}


def read_subjects(workspace_root: Path | str, project_ref: str) -> dict[str, Any]:
    """读台账：当前版本＋全部历史（未录入→current None、history 空）。"""
    import json

    base = Path(workspace_root) / project_ref / SETTLEMENT_DIR / SUBJECTS_DIR
    manifest_path = base / SUBJECTS_MANIFEST
    if not manifest_path.exists():
        return {"current": None, "history": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    history: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in manifest.get("versions", []):
        record = json.loads((base / item["file"]).read_text(encoding="utf-8"))
        history.append(record)
        if item.get("version") == manifest.get("current"):
            current = record
    return {"current": current, "history": history}


# ---------------------------------------------------------------------------
# CSV 模板与导入解析（收尾欠账④；设计 §6「模板走既有模板目录模式」）
# 列集＝既有条目 Schema 的字段名（唯一 Schema：解析只做形状/数值转换，
# 业务校验仍由 write_subjects / write_month_input 把关）
# ---------------------------------------------------------------------------

SUBJECTS_CSV_COLUMNS = (
    "subject_id",
    "resource_type",
    "declared_capacity_mw",
    "note",
)
MONTH_CSV_COLUMNS = (
    "subject_id",
    "declared_capacity_mw",
    "reliability_ratio",
    "availability_factor",
    "expected_output_mw",
    "actual_output_mw",
    "unmet_hours",
    "deduction_price_per_mwh",
    "deduction_cap",
    "manual_adjustment",
    "adjustment_reason",
)
_SUBJECTS_REQUIRED = ("subject_id", "resource_type", "declared_capacity_mw")
_MONTH_REQUIRED = (
    "subject_id",
    "declared_capacity_mw",
    "reliability_ratio",
    "availability_factor",
    "expected_output_mw",
    "actual_output_mw",
)
_SUBJECTS_TEXT = ("subject_id", "resource_type", "note")
_MONTH_TEXT = ("subject_id", "adjustment_reason")

_TEMPLATE_FILES = {
    "subjects": "settlement_subjects.csv",
    "month": "settlement_month_entries.csv",
}


def _csv_columns(kind: str) -> tuple[str, ...]:
    if kind == "subjects":
        return SUBJECTS_CSV_COLUMNS
    if kind == "month":
        return MONTH_CSV_COLUMNS
    raise SettlementServiceError(
        "invalid_input", f"未知模板/导入类型：{kind!r}（可选 subjects/month）"
    )


def _csv_required(kind: str) -> tuple[str, ...]:
    return _SUBJECTS_REQUIRED if kind == "subjects" else _MONTH_REQUIRED


def _csv_text_columns(kind: str) -> tuple[str, ...]:
    return _SUBJECTS_TEXT if kind == "subjects" else _MONTH_TEXT


def render_settlement_template(kind: str) -> tuple[str, bytes]:
    """可填写 CSV 模板（表头行；utf-8-sig 让 Excel 正确打开中文表头）。
    模板不带示例行：任何示例价格/数字不得经模板进入正式链路。"""
    columns = _csv_columns(kind)
    file_name = _TEMPLATE_FILES[kind]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    return file_name, buffer.getvalue().encode("utf-8-sig")


def parse_settlement_csv(kind: str, csv_text: str) -> list[dict[str, Any]]:
    """CSV 文本→条目列表（形状转换；业务校验在 write_*）。规则：
    表头须覆盖必需列、不得含未知列（不静默忽略）；数值列非数值→拒绝并
    指明行；空值＝缺失（缺失≠0，留待月度检查 R-ST-07 如实报缺）。"""
    columns = _csv_columns(kind)
    required = _csv_required(kind)
    text_columns = _csv_text_columns(kind)
    raw = str(csv_text or "").lstrip(chr(0xFEFF))
    if not raw.strip():
        raise SettlementServiceError("invalid_input", "CSV 内容为空")
    reader = csv.reader(io.StringIO(raw))
    try:
        header = next(reader)
    except StopIteration:
        raise SettlementServiceError("invalid_input", "CSV 内容为空") from None
    header = [h.strip() for h in header]
    missing = [c for c in required if c not in header]
    if missing:
        raise SettlementServiceError(
            "invalid_input", f"CSV 缺必需列：{'、'.join(missing)}（请用模板列）"
        )
    unknown = [h for h in header if h and h not in columns]
    if unknown:
        raise SettlementServiceError(
            "invalid_input",
            f"CSV 含未知列：{'、'.join(unknown)}（不静默忽略；请用模板列）",
        )
    rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            continue  # 空行跳过（Excel 尾行常见）
        row: dict[str, Any] = {}
        for col, cell in zip(header, raw_row):
            col = col.strip()
            if not col:
                continue
            value = cell.strip()
            if col in text_columns:
                row[col] = value
                continue
            if value == "":
                continue  # 缺失≠0：不写 0，留缺
            try:
                row[col] = float(value)
            except ValueError:
                raise SettlementServiceError(
                    "invalid_input",
                    f"第{index}行 {col} 必须为数值（实际 {value!r}）",
                ) from None
        for col in required:
            if col not in row or row[col] in ("", None):
                raise SettlementServiceError(
                    "invalid_input", f"第{index}行 {col} 不能为空（缺失≠0）"
                )
        rows.append(row)
    if not rows:
        raise SettlementServiceError("invalid_input", "CSV 无数据行")
    return rows


# ---------------------------------------------------------------------------
# 引用管理（Step 3 S1-M2，设计 §4：五项同口径门＋聚合逐项列出）
# ---------------------------------------------------------------------------

REFERENCES_FILE = "references.json"
_REFERENCE_KINDS = ("coefficient", "accredited_capacity", "target_capacity", "net_cone")


def list_reference_options(workspace_root: Path | str) -> dict[str, Any]:
    """可引用结果只读清单（全只读派生，零计算）：
    冻结系数（S0）＋ Net CONE（本模块既有列表）＋ ELCC/EFC 与目标容量
    （经结果索引行：已发布且有点值的正式结果）。"""
    from capacity_credit.application.capacity_coefficient_freeze_service import (
        list_coefficient_freezes,
    )
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        read_registry,
    )

    root = Path(workspace_root)
    try:
        registry = read_registry(root)
    except ProjectServiceError:
        registry = {"projects": []}
    name_of = {str(e.get("project_ref")): e.get("name") for e in registry.get("projects", [])}
    coefficients = []
    for row in list_coefficient_freezes(root):
        if row.get("invalid"):
            continue
        coefficients.append(
            {
                "project_ref": row["project_ref"],
                "project_name": row.get("project_name"),
                "freeze_ref": row.get("freeze_ref"),
                "coefficient": row.get("coefficient"),
                "formula_version": row.get("formula_version"),
                "target_year": row.get("target_year"),
                "confirmer": row.get("confirmer"),
                "frozen_at": row.get("frozen_at"),
            }
        )
    net_cone = list_net_cone_references(root)
    # ELCC/EFC 与目标可靠容量：经运行目录 + 结果索引既有事实（只读）
    accredited: list[dict[str, Any]] = []
    target_capacity: list[dict[str, Any]] = []
    try:
        from capacity_credit.application.workbench_service import WorkbenchService

        service = WorkbenchService(root)
        for entry in registry.get("projects", []):
            ref = str(entry.get("project_ref") or "")
            if not ref:
                continue
            try:
                runs = service.list_run_summaries(ref)
            except Exception:  # noqa: BLE001 - 单项目不可读不拖垮清单
                continue
            for row in runs:
                method = row.get("method")
                if method not in ("elcc", "efc", "demand_target_capacity"):
                    continue
                if not row.get("published"):
                    continue
                try:
                    projection = service.get_run_projection(ref, str(row.get("run_ref")))
                except Exception:  # noqa: BLE001
                    continue
                cv = projection.get("capacity_view") or {}
                point = cv.get("accredited_capacity_mw") if method in ("elcc", "efc") else cv.get("target_capacity_mw")
                if point is None:
                    continue
                target = {
                    "project_ref": ref,
                    "project_name": name_of.get(ref) or ref,
                    "run_ref": row.get("run_ref"),
                    "method": method,
                    "point_value_mw": point,
                }
                (target_capacity if method == "demand_target_capacity" else accredited).append(target)
    except Exception:  # noqa: BLE001 - 门面不可用不拖垮系数/Net CONE 清单
        pass
    return {
        "coefficients": coefficients,
        "accredited_capacity": accredited,
        "target_capacity": target_capacity,
        "net_cone": net_cone,
    }


def _caliber_gate(
    *,
    config: dict[str, Any],
    kind: str,
    facts: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """五项同口径门（设计 §4.3）：返回 (blockers, warnings)。
    口径不一致＝阻断；来源缺字段＝警告（无法核验，需人工确认留痕）。"""
    blockers: list[str] = []
    warnings: list[str] = []
    year = facts.get("year")
    if year is not None and int(year) != int(config.get("settlement_year")):
        blockers.append(
            f"年份口径不一致：引用结果 {year} vs 结算年度 {config.get('settlement_year')}"
        )
    region = facts.get("region")
    if region is None:
        warnings.append(f"地区：来源未携带地区字段，无法核验（结算地区＝{config.get('region')}）")
    elif str(region) != str(config.get("region")):
        blockers.append(f"地区口径不一致：引用 {region} vs 结算 {config.get('region')}")
    obj = facts.get("object")
    if obj is None and kind != "coefficient":
        warnings.append("资源对象：来源未携带对象字段，无法核验")
    cap = facts.get("capacity_kind")
    if cap is None:
        warnings.append("容量口径：来源未携带容量分母口径，无法核验")
    price = facts.get("price_basis")
    if price is None and kind == "net_cone":
        warnings.append("价格基准：来源未携带价格口径，无法核验")
    elif price is not None and str(price) not in str(config.get("price_basis") or ""):
        blockers.append(f"价格基准口径不一致：引用 {price} vs 结算 {config.get('price_basis')}")
    return blockers, warnings


def add_reference(
    *,
    workspace_root: Path | str,
    project_ref: str,
    kind: str,
    target: Any,
    confirm_warnings: bool = False,
    confirmer: str = "",
) -> dict[str, Any]:
    """挂引用：解析目标（系数只认 S0 冻结；其余经索引事实）→ 五项同口径门
    聚合逐项校验 → blockers 阻断（确认不可绕过）/warnings 需人工确认留痕。"""
    project_dir = _require_settlement_project(workspace_root, project_ref)
    config = read_year_config(project_dir)
    if config is None:
        raise SettlementServiceError(
            "invalid_input", "该项目未写入年度基础信息（不是结算项目）"
        )
    if kind not in _REFERENCE_KINDS:
        raise SettlementServiceError(
            "invalid_input", f"引用类型必须为 {'/'.join(_REFERENCE_KINDS)} 之一"
        )
    if not isinstance(target, dict):
        raise SettlementServiceError("invalid_input", "target 必须为对象")
    root = Path(workspace_root)

    resolved: dict[str, Any]
    facts: dict[str, Any]
    if kind == "coefficient":
        from capacity_credit.application.capacity_coefficient_freeze_service import (
            read_coefficient_freeze,
        )

        t_project = str(target.get("project_ref") or "")
        t_freeze = str(target.get("freeze_ref") or "")
        record = read_coefficient_freeze(root / t_project) if t_project else None
        if record is None or record.get("invalid"):
            raise SettlementServiceError(
                "invalid_input", f"引用的冻结系数不存在或记录损坏：{t_project}/{t_freeze}"
            )
        manifest = record["manifest"]
        if str(manifest.get("freeze_ref")) != t_freeze:
            raise SettlementServiceError(
                "invalid_input", f"引用的冻结系数不存在：{t_freeze}"
            )
        resolved = {
            "project_ref": t_project,
            "freeze_ref": t_freeze,
            "coefficient": manifest.get("coefficient"),
            "formula_version": manifest.get("formula_version"),
            "sha256": manifest.get("formal_sha256"),
        }
        facts = {"year": manifest.get("target_year")}
    elif kind == "net_cone":
        t_run = str(target.get("run_ref") or "")
        found = None
        for row in list_net_cone_references(root):
            if str(row.get("run_ref")) == t_run:
                found = row
                break
        if found is None:
            raise SettlementServiceError(
                "invalid_input", f"引用的 Net CONE 运行不存在：{t_run}"
            )
        resolved = dict(found)
        resolved["sha256"] = found.get("result_sha256")
        facts = {"year": found.get("assessment_year"), "region": found.get("region")}
    else:
        # accredited_capacity / target_capacity：经运行投影事实（只读）
        t_project = str(target.get("project_ref") or "")
        t_run = str(target.get("run_ref") or "")
        try:
            from capacity_credit.application.workbench_service import WorkbenchService

            projection = WorkbenchService(root).get_run_projection(t_project, t_run)
        except Exception as exc:  # noqa: BLE001
            raise SettlementServiceError(
                "invalid_input", f"引用的运行不存在或不可读：{t_project}/{t_run}（{exc}）"
            ) from None
        method = projection.get("method")
        expect = ("elcc", "efc") if kind == "accredited_capacity" else ("demand_target_capacity",)
        if method not in expect:
            raise SettlementServiceError(
                "invalid_input", f"引用运行方法不符（期望 {'/'.join(expect)}，实际 {method}）"
            )
        cv = projection.get("capacity_view") or {}
        point_key = "accredited_capacity_mw" if kind == "accredited_capacity" else "target_capacity_mw"
        if cv.get(point_key) is None:
            raise SettlementServiceError(
                "invalid_input", "引用结果未收敛（无点值），不可引用"
            )
        resolved = {
            "project_ref": t_project,
            "run_ref": t_run,
            "method": method,
            "point_value_mw": cv[point_key],
            "sha256": projection.get("result_id"),
        }
        facts = {"year": cv.get("delivery_year"), "object": cv.get("resource_or_portfolio_id")}

    blockers, warnings = _caliber_gate(config=config, kind=kind, facts=facts)
    if blockers:
        raise SettlementServiceError(
            "invalid_input", "引用口径不一致（逐项列出）：" + "；".join(blockers)
        )
    if warnings and not confirm_warnings:
        raise SettlementServiceError(
            "invalid_input",
            "引用存在无法核验项（逐项列出）：" + "；".join(warnings)
            + "。确认上述口径差异可接受后，携 confirm_warnings=true 重新提交（留痕）",
        )
    references_path = project_dir / SETTLEMENT_DIR / REFERENCES_FILE
    references: list[dict[str, Any]] = []
    if references_path.exists():
        import json

        references = json.loads(references_path.read_text(encoding="utf-8"))
    entry = {
        "kind": kind,
        "target": resolved,
        "checks": {"facts": facts},
        "warnings": warnings,
        "warnings_confirmed": bool(confirm_warnings and warnings),
        "confirmer": str(confirmer or "").strip(),
        "added_at": datetime.now(UTC).isoformat(),
        # 顶层透传常用键（列表/校验直读）
        "project_ref": resolved.get("project_ref"),
        "run_ref": resolved.get("run_ref"),
        "freeze_ref": resolved.get("freeze_ref"),
        "formula_version": resolved.get("formula_version"),
    }
    references.append(entry)
    atomic_write_bytes(references_path, stable_dump(references), immutable=False)
    return {"reference": entry, "count": len(references)}


def list_references(workspace_root: Path | str, project_ref: str) -> list[dict[str, Any]]:
    """读引用清单（未挂引用→空列表）。"""
    import json

    path = Path(workspace_root) / project_ref / SETTLEMENT_DIR / REFERENCES_FILE
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "SettlementServiceError",
    "add_reference",
    "create_settlement_project",
    "list_net_cone_references",
    "list_reference_options",
    "list_references",
    "read_price_table",
    "read_subjects",
    "read_year_config",
    "validate_settlement_config",
    "write_price_table",
    "write_subjects",
]
