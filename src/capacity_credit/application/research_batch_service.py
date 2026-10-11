"""B1 research batches - THE container for a multi-point study.

Why this object exists: one project carries exactly ONE formal result,
but a duration curve / penetration curve / baseline calibration is ONE
business deliverable made of many runs. A research batch is that
deliverable: its own identity, its own axis definition, its own point
ledger and its own result contract - it never consumes a project's
formal-result slot (CAL-10) and never becomes a second run state
machine (each point still binds to a REAL run_ref produced by the
existing chain).

Layout (atomic primitives, manifest last):

    research_batches/<batch_ref>/
        batch_manifest.json   the batch facts + point ledger (last write)

Honesty rules:
- a point is only what its bindings say: 待派生 -> 待确认 (DR-01 human
  confirmation) -> 待运行 -> 完成 / 失败;
- a FAILED point stays a visible gap with its diagnosis (DR-09) - it is
  never interpolated, never zero-filled and never counted as done;
- the batch is 完成 only when EVERY point is 完成; anything less is
  部分完成, so a matrix never claims completeness it does not have;
- an unknown batch/point, a re-bind of a completed point or a manifest
  that does not parse is a refusal, never a guess.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    safe_filename_component,
    stable_dump,
)
from capacity_credit.application.calibration_service import (
    AXIS_PERFECT_CAPACITY,
    CalibrationError,
    classify_round,
    next_decision,
    seasonal_next_decision,
    validate_criterion,
    validate_seasonal_criterion,
)
from capacity_credit.application.derivation_service import (
    RULE_LOAD_SCALE,
    RULE_PERFECT_CAPACITY,
    RULE_SEASONAL_LOAD_SCALE,
)

BATCHES_DIR = "research_batches"
MANIFEST_FILE = "batch_manifest.json"
BATCH_SCHEMA_VERSION = "1.0"

# SL-01 signed default (method sign-off 2026-09-19, draft section 7):
# the approximate centralized-heating months, recorded explicitly on
# every seasonal batch and every seasonal probe point
DEFAULT_HEATING_MONTHS = [11, 12, 1, 2, 3]

BATCH_KINDS = ("duration_curve", "penetration_curve", "calibration", "seed_replication")
BATCH_METHODS = ("elcc", "efc", "adequacy")

POINT_PENDING_DERIVE = "待派生"
POINT_AWAITING_CONFIRM = "待确认"
POINT_PENDING_RUN = "待运行"
POINT_DONE = "完成"
POINT_FAILED = "失败"
_POINT_STATES = (
    POINT_PENDING_DERIVE,
    POINT_AWAITING_CONFIRM,
    POINT_PENDING_RUN,
    POINT_DONE,
    POINT_FAILED,
)
# forward-only transitions: a confirmed point never slides back to
# 待确认, and a terminal point is immutable (its run is the evidence)
_POINT_ORDER = {
    POINT_PENDING_DERIVE: 0,
    POINT_AWAITING_CONFIRM: 1,
    POINT_PENDING_RUN: 2,
    POINT_DONE: 3,
    POINT_FAILED: 3,
}

BATCH_DRAFT = "草稿"
BATCH_AWAITING = "待确认点位"
BATCH_RUNNING = "运行中"
BATCH_PARTIAL = "部分完成"
BATCH_COMPLETE = "完成"
BATCH_FAILED = "存在失败点位"


class ResearchBatchError(ValueError):
    """A batch refusal (display kind + reason)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _batches_root(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / BATCHES_DIR


def _manifest_path(workspace_root: Path | str, batch_ref: str) -> Path:
    safe_filename_component(batch_ref)
    return _batches_root(workspace_root) / batch_ref / MANIFEST_FILE


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _batch_status(points: list[dict[str, Any]]) -> str:
    """The batch state derived from its point ledger - never stored as an
    independent truth."""
    if not points:
        return BATCH_DRAFT
    states = [str(p.get("status")) for p in points]
    done = sum(1 for s in states if s == POINT_DONE)
    failed = sum(1 for s in states if s == POINT_FAILED)
    if done == len(points):
        return BATCH_COMPLETE
    # nothing terminal yet: the batch is waiting on people or on runs
    if done == 0 and failed == 0:
        if all(s == POINT_AWAITING_CONFIRM for s in states):
            return BATCH_AWAITING
        if all(s == POINT_PENDING_DERIVE for s in states):
            return BATCH_DRAFT
        return BATCH_RUNNING
    # a failure is surfaced IMMEDIATELY (never hidden behind 运行中):
    # with completed points it is a partial matrix carrying visible gaps
    # (DR-09), without them the batch itself has failed points
    if failed:
        return BATCH_PARTIAL if done else BATCH_FAILED
    return BATCH_RUNNING


def _validate_points(points: Any) -> list[dict[str, Any]]:
    if not isinstance(points, list) or not points:
        raise ResearchBatchError("invalid_input", "点位清单必须为非空列表")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ResearchBatchError(
                "invalid_input", f"第{index + 1}个点位必须为对象"
            )
        point_id = str(point.get("point_id") or "").strip()
        if not point_id:
            raise ResearchBatchError(
                "invalid_input", f"第{index + 1}个点位缺少 point_id"
            )
        if point_id in seen:
            raise ResearchBatchError(
                "invalid_input", f"点位标识重复：{point_id}"
            )
        seen.add(point_id)
        params = point.get("params")
        if not isinstance(params, dict):
            raise ResearchBatchError(
                "invalid_input", f"点位 {point_id} 的 params 必须为对象"
            )
        out.append(
            {
                "point_id": point_id,
                "label": str(point.get("label") or point_id),
                "params": dict(params),
                "status": POINT_PENDING_DERIVE,
                "import_batch_id": None,
                "run_ref": None,
                "derivation": None,
                "diagnosis": None,
                "updated_at": None,
            }
        )
    return out


def _axis_key(axis_field: Any) -> str:
    """The effective axis of a manifest's axis field: ONLY the exact
    string 'perfect_capacity' selects the capacity axis - legacy display
    dicts (and absence) mean the load axis (contract §1)."""
    if isinstance(axis_field, str) and axis_field.strip() == AXIS_PERFECT_CAPACITY:
        return AXIS_PERFECT_CAPACITY
    return "load_scale"


def _validate_axis_config(axis_config: Any) -> dict[str, Any]:
    """The perfect-capacity window: initial_interval_mw is REQUIRED (the
    tool invents no default MW range - CAL-02 applied to the probe
    axis); min_step_mw is optional."""
    import math

    if not isinstance(axis_config, dict):
        raise ResearchBatchError(
            "invalid_input",
            "完美容量轴必须显式给出初始区间（axis_config."
            "initial_interval_mw，MW，[下界, 上界]）："
            "工具不为容量轴发明默认档位",
        )
    interval = axis_config.get("initial_interval_mw")
    try:
        low, high = float(interval[0]), float(interval[1])
    except (TypeError, ValueError, IndexError):
        raise ResearchBatchError(
            "invalid_input",
            f"axis_config.initial_interval_mw 必须为 [下界, 上界] 数值对，"
            f"收到 {interval!r}",
        ) from None
    if not math.isfinite(low) or not math.isfinite(high) or low < 0 or low >= high:
        raise ResearchBatchError(
            "invalid_input",
            f"完美容量初始区间必须满足 0 ≤ 下界 < 上界（MW），收到 [{low}, {high}]",
        )
    out: dict[str, Any] = {"initial_interval_mw": [low, high]}
    min_step = axis_config.get("min_step_mw")
    if min_step is not None:
        try:
            min_step = float(min_step)
        except (TypeError, ValueError):
            raise ResearchBatchError(
                "invalid_input",
                f"axis_config.min_step_mw 必须为数值（MW），收到 {min_step!r}",
            ) from None
        if not math.isfinite(min_step) or min_step <= 0:
            raise ResearchBatchError(
                "invalid_input", "axis_config.min_step_mw 必须为大于0的有限MW数"
            )
        out["min_step_mw"] = min_step
    return out


def _validate_capacity_points(points: list[dict[str, Any]]) -> None:
    """A capacity batch carries ONLY add_perfect_capacity_mw points with
    a positive MW value - a load-scale point on a capacity batch is a
    contradiction, not a round."""
    for point in points:
        params = point.get("params") or {}
        rule = str(params.get("rule_id") or "")
        if rule != RULE_PERFECT_CAPACITY:
            raise ResearchBatchError(
                "invalid_input",
                f"完美容量批次点位 {point.get('point_id')} 的规则必须为 "
                f"{RULE_PERFECT_CAPACITY}，收到 {rule or '（空）'!r}",
            )
        try:
            value = float(params.get("capacity_mw"))
        except (TypeError, ValueError):
            raise ResearchBatchError(
                "invalid_input",
                f"完美容量批次点位 {point.get('point_id')} 缺少数值 "
                f"capacity_mw",
            ) from None
        if not math.isfinite(value) or value <= 0:
            raise ResearchBatchError(
                "invalid_input",
                f"完美容量批次点位 {point.get('point_id')} 的 capacity_mw "
                "必须为大于0的有限MW数",
            )


def _is_seasonal_criterion(criterion: Any) -> bool:
    """A seasonal calibration batch: kind=calibration whose criterion
    carries the explicit heating/non_heating target pair (SL-01)."""
    return isinstance(criterion, dict) and isinstance(
        criterion.get("seasonal"), dict
    )


def _normalize_seasonal_criterion(criterion: dict[str, Any]) -> dict[str, Any]:
    """Float-coerce the seasonal pair (HTTP callers may send strings)
    so validate_seasonal_criterion sees numbers."""
    out = dict(criterion)
    seasonal = dict(criterion.get("seasonal") or {})
    for key in ("heating", "non_heating"):
        try:
            seasonal[key] = float(seasonal[key])
        except (TypeError, ValueError):
            raise ResearchBatchError(
                "invalid_input",
                f"季节判据 seasonal.{key} 必须为数值，收到 "
                f"{seasonal.get(key)!r}",
            ) from None
    out["seasonal"] = seasonal
    return out


def _validate_heating_months_param(heating_months: Any) -> list[int]:
    try:
        values = [int(m) for m in heating_months]
    except (TypeError, ValueError):
        raise ResearchBatchError(
            "invalid_input",
            f"heating_months 必须为 1–12 的月份列表，收到 {heating_months!r}",
        ) from None
    months = frozenset(values)
    if (
        not months
        or len(values) != len(months)
        or not months <= frozenset(range(1, 13))
        or months == frozenset(range(1, 13))
    ):
        raise ResearchBatchError(
            "invalid_input",
            "heating_months 月份集合必须为 1–12 的非空真子集且不重复"
            f"（不能是全部 12 个月），收到 {heating_months!r}",
        )
    return values


def _validate_seasonal_windows(axis_config: Any) -> dict[str, Any]:
    """The dual-knob windows are REQUIRED for a seasonal batch - two
    knobs with no stated windows are un-calibratable, and the tool
    invents no defaults (CAL-02 spirit)."""
    import math

    if not isinstance(axis_config, dict):
        raise ResearchBatchError(
            "invalid_input",
            "季节标定批次必须显式给出两季初始区间（axis_config."
            "heating_interval_percent / non_heating_interval_percent）："
            "工具不为季节轴发明默认档位",
        )
    out: dict[str, Any] = {}
    for key in ("heating_interval_percent", "non_heating_interval_percent"):
        interval = axis_config.get(key)
        try:
            low, high = float(interval[0]), float(interval[1])
        except (TypeError, ValueError, IndexError):
            raise ResearchBatchError(
                "invalid_input",
                f"axis_config.{key} 必须为 [下界, 上界] 数值对，收到 "
                f"{interval!r}",
            ) from None
        if (
            not math.isfinite(low)
            or not math.isfinite(high)
            or low <= -100
            or low >= high
        ):
            raise ResearchBatchError(
                "invalid_input",
                f"axis_config.{key} 必须满足 -99 ≤ 下界 < 上界"
                f"（负荷缩放百分比，DR-06：> -100），收到 [{low}, {high}]",
            )
        out[key] = [low, high]
    months = axis_config.get("heating_months")
    out["heating_months"] = (
        _validate_heating_months_param(months)
        if months is not None
        else list(DEFAULT_HEATING_MONTHS)
    )
    return out


def _validate_seasonal_points(points: list[dict[str, Any]]) -> None:
    """A seasonal batch carries ONLY seasonal_load_scale points with
    BOTH scale values."""
    for point in points:
        params = point.get("params") or {}
        rule = str(params.get("rule_id") or "")
        if rule != RULE_SEASONAL_LOAD_SCALE:
            raise ResearchBatchError(
                "invalid_input",
                f"季节标定批次点位 {point.get('point_id')} 的规则必须为 "
                f"{RULE_SEASONAL_LOAD_SCALE}，收到 {rule or '（空）'!r}",
            )
        for key in ("heating_scale_percent", "non_heating_scale_percent"):
            try:
                float(params.get(key))
            except (TypeError, ValueError):
                raise ResearchBatchError(
                    "invalid_input",
                    f"季节批次点位 {point.get('point_id')} 缺少数值 {key}",
                ) from None


def create_batch(
    workspace_root: Path | str,
    *,
    request_id: str,
    kind: str,
    method: str,
    project_ref: str,
    baseline_batch_id: str,
    points: Any,
    axis: dict[str, Any] | str | None = None,
    criterion: dict[str, Any] | None = None,
    label: str = "",
    axis_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create (or idempotently re-return) ONE research batch. The same
    request_id with the same effective parameters returns the same batch;
    a different payload under the same request_id is an honest conflict."""
    root = Path(workspace_root)
    if not root.is_dir():
        raise ResearchBatchError("unavailable", f"工作区不存在：{root}")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ResearchBatchError("invalid_input", "request_id 必须为非空字符串")
    if kind not in BATCH_KINDS:
        raise ResearchBatchError(
            "invalid_input",
            f"研究批次类型必须为 {'/'.join(BATCH_KINDS)} 之一，收到 {kind!r}",
        )
    if method not in BATCH_METHODS:
        raise ResearchBatchError(
            "invalid_input",
            f"测算方法必须为 {'/'.join(BATCH_METHODS)} 之一，收到 {method!r}",
        )
    safe_filename_component(project_ref)
    if not str(baseline_batch_id).strip():
        raise ResearchBatchError(
            "invalid_input", "必须指定基线冻结批次（派生的父版本）"
        )
    validated = _validate_points(points)
    # contract §1: axis is the string 'load_scale' (the default) or
    # 'perfect_capacity'; the current UI's display-name dicts keep
    # round-tripping untouched as the load axis (backward compat)
    if axis is None:
        normalized_axis: Any = "load_scale"
    elif isinstance(axis, str):
        normalized_axis = axis.strip()
        if normalized_axis not in ("load_scale", AXIS_PERFECT_CAPACITY):
            raise ResearchBatchError(
                "invalid_input",
                f"标定轴必须为 load_scale/{AXIS_PERFECT_CAPACITY} 之一，"
                f"收到 {axis!r}",
            )
    else:
        normalized_axis = axis
    effective_axis = _axis_key(normalized_axis)
    validated_config: dict[str, Any] | None = None
    seasonal_batch = (
        kind == "calibration" and _is_seasonal_criterion(criterion or {})
    )
    if effective_axis == AXIS_PERFECT_CAPACITY:
        validated_config = _validate_axis_config(axis_config)
        _validate_capacity_points(validated)
    elif seasonal_batch:
        # SL-01: eager annual-consistency + window validation - a
        # seasonal batch whose pair drifts off the annual anchor is
        # refused here, never at the first round
        normalized_criterion = _normalize_seasonal_criterion(
            dict(criterion or {})
        )
        try:
            validate_seasonal_criterion(normalized_criterion)
        except CalibrationError as exc:
            raise ResearchBatchError(exc.kind, exc.message) from None
        validated_config = _validate_seasonal_windows(axis_config)
        _validate_seasonal_points(validated)
        # the batch-level month set is authoritative: inject it into
        # every point so each round's derivation self-describes
        for point in validated:
            point["params"]["heating_months"] = list(
                validated_config["heating_months"]
            )
    elif axis_config is not None:
        raise ResearchBatchError(
            "invalid_input",
            "axis_config 仅随完美容量轴（perfect_capacity）或季节标定"
            "（criterion.seasonal）使用",
        )
    batches = list_batches(root)
    for entry in batches:
        if entry.get("request_id") == request_id:
            same = (
                entry.get("kind") == kind
                and entry.get("method") == method
                and entry.get("project_ref") == project_ref
                and entry.get("baseline_batch_id") == baseline_batch_id
                and [p["point_id"] for p in entry.get("points", [])]
                == [p["point_id"] for p in validated]
            )
            if not same:
                raise ResearchBatchError(
                    "request_conflict",
                    f"同一创建请求 {request_id!r} 已用于不同批次参数",
                )
            return entry
    batch_ref = uuid.uuid4().hex
    batch_dir = _batches_root(root) / batch_ref
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "batch_schema_version": BATCH_SCHEMA_VERSION,
        "batch_ref": batch_ref,
        "request_id": request_id,
        "kind": kind,
        "method": method,
        "label": str(label or ""),
        "project_ref": project_ref,
        "baseline_batch_id": baseline_batch_id,
        "axis": normalized_axis,
        "axis_config": validated_config,
        "criterion": dict(criterion or {}),
        # R2-B5: pinned on the first derived round (calibration CRN);
        # null until then - never overwritten once bound
        "simulation_identity": None,
        "points": validated,
        "created_at": _now(),
        "updated_at": _now(),
    }
    manifest["status"] = _batch_status(manifest["points"])
    try:
        atomic_write_bytes(
            _manifest_path(root, batch_ref), stable_dump(manifest)
        )
    except BaseException:
        # roll back only this request's own artifact
        try:
            if batch_dir.is_dir() and not any(batch_dir.iterdir()):
                batch_dir.rmdir()
        except OSError:
            pass
        raise
    return manifest


def read_batch(workspace_root: Path | str, batch_ref: str) -> dict[str, Any]:
    """One batch manifest. A missing or corrupt manifest is an honest
    refusal - never an empty successful batch."""
    path = _manifest_path(workspace_root, batch_ref)
    if not path.is_file():
        raise ResearchBatchError(
            "not_found", f"研究批次不存在：{batch_ref}"
        )
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResearchBatchError(
            "unavailable", f"研究批次清单损坏：{exc}"
        ) from None
    if not isinstance(parsed, dict) or not isinstance(
        parsed.get("points"), list
    ):
        raise ResearchBatchError(
            "unavailable", "研究批次清单结构无效（缺少 points 列表）"
        )
    return parsed


def list_batches(workspace_root: Path | str) -> list[dict[str, Any]]:
    """Every batch in the workspace (deterministic: created_at descending,
    batch_ref ascending on ties). A corrupt manifest is listed as an
    invalid entry, never dropped."""
    base = _batches_root(workspace_root)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        path = entry / MANIFEST_FILE
        if not path.is_file():
            out.append(
                {
                    "batch_ref": entry.name,
                    "invalid": True,
                    "invalid_reason": f"缺少 {MANIFEST_FILE}",
                    "created_at": "",
                }
            )
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not an object")
            manifest["invalid"] = False
            manifest["invalid_reason"] = None
            out.append(manifest)
        except (OSError, ValueError) as exc:
            out.append(
                {
                    "batch_ref": entry.name,
                    "invalid": True,
                    "invalid_reason": str(exc)[:300],
                    "created_at": "",
                }
            )
    out.sort(key=lambda b: str(b.get("batch_ref")))
    out.sort(key=lambda b: str(b.get("created_at")), reverse=True)
    return out


def update_point(
    workspace_root: Path | str,
    batch_ref: str,
    point_id: str,
    *,
    status: str,
    import_batch_id: str | None = None,
    run_ref: str | None = None,
    derivation: dict[str, Any] | None = None,
    diagnosis: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move ONE point forward and recompute the batch state.

    Forward-only: a terminal point (完成/失败) is immutable evidence and
    refuses a re-bind; a point may not skip 待确认 (DR-01) on its way to
    待运行 unless it is already confirmed. A failed point keeps its
    diagnosis and remains a visible gap (DR-09).
    """
    if status not in _POINT_STATES:
        raise ResearchBatchError(
            "invalid_input",
            f"点位状态必须为 {'/'.join(_POINT_STATES)} 之一，收到 {status!r}",
        )
    manifest = read_batch(workspace_root, batch_ref)
    target = next(
        (p for p in manifest["points"] if p.get("point_id") == point_id), None
    )
    if target is None:
        raise ResearchBatchError(
            "invalid_input", f"批次 {batch_ref} 没有点位 {point_id}"
        )
    current = str(target.get("status"))
    if current in (POINT_DONE, POINT_FAILED):
        raise ResearchBatchError(
            "request_conflict",
            f"点位 {point_id} 已处于终态（{current}），不得改写："
            "其运行记录就是证据；需要重算请新建批次",
        )
    if (
        status == POINT_PENDING_RUN
        and current == POINT_PENDING_DERIVE
    ):
        raise ResearchBatchError(
            "invalid_input",
            f"点位 {point_id} 尚未派生与人工确认（DR-01），不能直接进入待运行",
        )
    target["status"] = status
    if import_batch_id is not None:
        target["import_batch_id"] = str(import_batch_id)
    if run_ref is not None:
        target["run_ref"] = str(run_ref)
    if derivation is not None:
        target["derivation"] = dict(derivation)
    if diagnosis is not None:
        target["diagnosis"] = str(diagnosis)[:500]
    # forward-compatible extras (bank id, metric value, ...): never allowed
    # to overwrite identity or state keys
    reserved = {"point_id", "label", "params", "status", "updated_at"}
    for key, value in (extra or {}).items():
        if key not in reserved:
            target[key] = value
    target["updated_at"] = _now()
    manifest["status"] = _batch_status(manifest["points"])
    manifest["updated_at"] = target["updated_at"]
    atomic_write_bytes(
        _manifest_path(workspace_root, batch_ref), stable_dump(manifest)
    )
    return manifest


def _append_point_seasonal(
    workspace_root: Path | str,
    manifest: dict[str, Any],
    *,
    authorized_by: str,
    heating_scale_percent: float | None,
    non_heating_scale_percent: float | None,
) -> dict[str, Any]:
    """The SL-01 dual-knob append: rebuild the per-season round history
    from the ledger, recompute the DECISION CORE's dual probe, and
    enter it as one seasonal_load_scale point carrying both scales.

    Each completed round must carry BOTH segment metrics
    (metric_heating / metric_non_heating extras - the run chain's
    seasonal attribution); a round missing either is an honest refusal,
    never a half-classified round."""
    try:
        criterion = validate_seasonal_criterion(
            _normalize_seasonal_criterion(
                dict(manifest.get("criterion") or {})
            )
        )
    except CalibrationError as exc:
        raise ResearchBatchError(exc.kind, exc.message) from None
    config = dict(manifest.get("axis_config") or {})
    heating_window = config.get("heating_interval_percent")
    non_window = config.get("non_heating_interval_percent")
    if not heating_window or not non_window:
        raise ResearchBatchError(
            "invalid_input",
            "批次清单缺少季节初始区间（axis_config."
            "heating_interval_percent / non_heating_interval_percent）："
            "拒绝为季节轴发明默认档位",
        )
    heating_months = config.get("heating_months") or list(
        DEFAULT_HEATING_MONTHS
    )
    history: list[dict[str, Any]] = []
    for point in manifest["points"]:
        state = str(point.get("status"))
        if state not in (POINT_DONE, POINT_FAILED):
            continue
        params = point.get("params") or {}
        try:
            heating_probe = float(params.get("heating_scale_percent"))
            non_probe = float(params.get("non_heating_scale_percent"))
        except (TypeError, ValueError):
            raise ResearchBatchError(
                "invalid_input",
                f"点位 {point.get('point_id')} 缺少数值 "
                "heating_scale_percent/non_heating_scale_percent，"
                "无法归类季节轮次",
            ) from None
        done = state == POINT_DONE
        for key in ("metric_heating", "metric_non_heating"):
            if done and point.get(key) is None:
                raise ResearchBatchError(
                    "invalid_input",
                    f"点位 {point.get('point_id')} 缺少数值 {key}："
                    "季节标定每轮必须携带两季指标，拒绝半轮归类",
                ) from None
        history.append(
            {
                "heating": classify_round(
                    criterion["heating"],
                    heating_probe,
                    point.get("metric_heating") if done else None,
                    run_converged=done,
                    diagnosis=point.get("diagnosis"),
                ),
                "non_heating": classify_round(
                    criterion["non_heating"],
                    non_probe,
                    point.get("metric_non_heating") if done else None,
                    run_converged=done,
                    diagnosis=point.get("diagnosis"),
                ),
            }
        )
    decision = seasonal_next_decision(
        criterion,
        history,
        heating_interval=(float(heating_window[0]), float(heating_window[1])),
        non_heating_interval=(
            float(non_window[0]),
            float(non_window[1]),
        ),
    )
    if decision.get("kind") != "probe":
        raise ResearchBatchError(
            "request_conflict",
            f"季节标定决策为 {decision.get('kind')}：批次已无待补探针"
            "（终态不追加，CAL-04/05/08）",
        )
    heating_value = float(decision["heating"]["probe_value"])
    non_value = float(decision["non_heating"]["probe_value"])
    for requested, value, label in (
        (heating_scale_percent, heating_value, "heating_scale_percent"),
        (
            non_heating_scale_percent,
            non_value,
            "non_heating_scale_percent",
        ),
    ):
        if requested is None:
            continue
        try:
            requested_value = float(requested)
        except (TypeError, ValueError):
            raise ResearchBatchError(
                "invalid_input",
                f"{label} 必须为数值，收到 {requested!r}",
            ) from None
        if abs(requested_value - value) > 1e-9:
            raise ResearchBatchError(
                "request_conflict",
                f"请求的季节探针 {requested_value:g}% 与决策核下一探针 "
                f"{value:g}% 不一致：探针只能来自决策核（R2-C1），"
                "不得另行指定",
            )
    # duplicate guard: a pending point at the SAME (heating, non) pair
    # means "run it", not "record it twice"
    for p in manifest["points"]:
        if str(p.get("status")) not in (
            POINT_PENDING_DERIVE,
            POINT_AWAITING_CONFIRM,
            POINT_PENDING_RUN,
        ):
            continue
        params = p.get("params") or {}
        try:
            pending_h = float(params.get("heating_scale_percent"))
            pending_n = float(params.get("non_heating_scale_percent"))
        except (TypeError, ValueError):
            continue
        if (
            abs(pending_h - heating_value) <= 1e-9
            and abs(pending_n - non_value) <= 1e-9
        ):
            raise ResearchBatchError(
                "request_conflict",
                f"已存在同季节探针的未完成点位 {p.get('point_id')}"
                f"（{heating_value:g}%/{non_value:g}%）：请先运行它，"
                "不重复记账",
            )
    existing = {str(p.get("point_id")) for p in manifest["points"]}
    n = len(manifest["points"])
    point_id = f"r{n + 1}"
    while point_id in existing:
        n += 1
        point_id = f"r{n + 1}"
    now = _now()
    new_point = {
        "point_id": point_id,
        "label": f"{heating_value:g}%/{non_value:g}%",
        "params": {
            "rule_id": RULE_SEASONAL_LOAD_SCALE,
            "heating_scale_percent": heating_value,
            "non_heating_scale_percent": non_value,
            "heating_months": list(heating_months),
        },
        "status": POINT_PENDING_DERIVE,
        "import_batch_id": None,
        "run_ref": None,
        "derivation": None,
        "diagnosis": None,
        "updated_at": now,
        "append_record": {
            "via": "append_point",
            "authorized_by": authorized_by,
            "axis": "load_scale_seasonal",
            "decision_probe_value": {
                "heating": heating_value,
                "non_heating": non_value,
            },
            "rounds_used": decision.get("rounds_used"),
            "appended_at": now,
        },
    }
    manifest["points"].append(new_point)
    manifest["status"] = _batch_status(manifest["points"])
    manifest["updated_at"] = now
    atomic_write_bytes(
        _manifest_path(workspace_root, manifest["batch_ref"]),
        stable_dump(manifest),
    )
    return manifest


def append_point(
    workspace_root: Path | str,
    batch_ref: str,
    scale_percent: float | None = None,
    *,
    authorized_by: str,
    capacity_mw: float | None = None,
    heating_scale_percent: float | None = None,
    non_heating_scale_percent: float | None = None,
) -> dict[str, Any]:
    """Extend a calibration batch with the decision core's NEXT probe.

    The bisection lives in the pure decision core
    (calibration_service.next_decision); this function never keeps a
    second copy of the band logic (R2-C1): it rebuilds the round history
    from the point ledger, RECOMPUTES the decision, and refuses unless
    that decision is a probe matching the requested value. A finished
    study (converged / not_converged / interval exhausted) therefore
    refuses an append through the same gate - its terminal state IS the
    decision, while a batch with unusable rounds (CAL-07) may continue.
    DR-01b: the extension records the batch authorization holder on the
    new point.

    Axis-aware (contract §1): a load-axis batch bisects in scale
    percent, a perfect_capacity batch in MW (the probe argument is
    ``capacity_mw``; passing a scale percent to a capacity batch or the
    reverse is an axis-mismatch refusal, and the capacity window comes
    from the manifest's axis_config - never invented here). A seasonal
    batch (SL-01, criterion.seasonal present) runs the DUAL-KNOB probe
    through ``seasonal_next_decision`` with both scales.
    """
    holder = str(authorized_by or "").strip()
    if not holder:
        raise ResearchBatchError(
            "invalid_input",
            "DR-01b 要求记录批次授权人：追加探针必须携带 authorized_by",
        )
    manifest = read_batch(workspace_root, batch_ref)
    if str(manifest.get("kind")) != "calibration":
        raise ResearchBatchError(
            "invalid_input",
            f"批次 {batch_ref} 类型为 {manifest.get('kind')}："
            "只有标定批次可追加探针",
        )
    if _is_seasonal_criterion(manifest.get("criterion")):
        return _append_point_seasonal(
            workspace_root,
            manifest,
            authorized_by=holder,
            heating_scale_percent=heating_scale_percent,
            non_heating_scale_percent=non_heating_scale_percent,
        )
    axis_key = _axis_key(manifest.get("axis"))
    axis_arg: Any = None
    if axis_key == AXIS_PERFECT_CAPACITY:
        if scale_percent is not None or (
            heating_scale_percent is not None
            or non_heating_scale_percent is not None
        ):
            raise ResearchBatchError(
                "invalid_input",
                "完美容量批次不接受 scale_percent/季节缩放追加参数：轴不匹配"
                "（完美容量轴的探针单位是 MW）",
            )
        requested = capacity_mw
        config = dict(manifest.get("axis_config") or {})
        if not config.get("initial_interval_mw"):
            raise ResearchBatchError(
                "invalid_input",
                "批次清单缺少完美容量初始区间（axis_config."
                "initial_interval_mw）：拒绝为容量轴发明默认档位",
            )
        axis_arg = {"key": AXIS_PERFECT_CAPACITY, **config}
        param_key = "capacity_mw"
        rule_id = RULE_PERFECT_CAPACITY
        unit_suffix = " MW"
    else:
        if capacity_mw is not None or (
            heating_scale_percent is not None
            or non_heating_scale_percent is not None
        ):
            raise ResearchBatchError(
                "invalid_input",
                "负荷轴批次不接受 capacity_mw/季节缩放追加参数：轴不匹配"
                "（负荷轴的探针单位是 %）",
            )
        requested = scale_percent
        axis_arg = None
        param_key = "scale_percent"
        rule_id = RULE_LOAD_SCALE
        unit_suffix = "%"
    try:
        criterion = validate_criterion(
            (manifest.get("criterion") or {}).get("metric"),
            (manifest.get("criterion") or {}).get("target"),
            (manifest.get("criterion") or {}).get("tolerance_percent", 5.0),
        )
    except CalibrationError as exc:
        raise ResearchBatchError(exc.kind, exc.message) from None
    history = []
    for point in manifest["points"]:
        state = str(point.get("status"))
        if state not in (POINT_DONE, POINT_FAILED):
            continue
        params = point.get("params") or {}
        try:
            probe = float(params.get(param_key))
        except (TypeError, ValueError):
            raise ResearchBatchError(
                "invalid_input",
                f"点位 {point.get('point_id')} 缺少数值 {param_key}，"
                "无法归类标定轮次",
            ) from None
        history.append(
            classify_round(
                criterion,
                probe,
                point.get("metric_value") if state == POINT_DONE else None,
                axis=axis_arg,
                run_converged=state == POINT_DONE,
                diagnosis=point.get("diagnosis"),
            )
        )
    decision = next_decision(criterion, history, axis=axis_arg)
    if decision.get("kind") != "probe":
        raise ResearchBatchError(
            "request_conflict",
            f"标定决策为 {decision.get('kind')}：批次已无待补探针"
            "（终态不追加，CAL-04/05/08）",
        )
    probe_value = float(decision["probe_value"])
    if requested is not None:
        try:
            requested_value = float(requested)
        except (TypeError, ValueError):
            raise ResearchBatchError(
                "invalid_input",
                f"{param_key} 必须为数值，收到 {requested!r}",
            ) from None
        if abs(requested_value - probe_value) > 1e-9:
            raise ResearchBatchError(
                "request_conflict",
                f"请求探针 {requested_value:g}{unit_suffix} 与决策核下一探针 "
                f"{probe_value:g}{unit_suffix} 不一致："
                "探针只能来自决策核（R2-C1），不得另行指定",
            )
    # duplicate guard: a pending point at the probe value means "run it",
    # not "record it twice" (a failed terminal point stays a gap per
    # DR-09 but does not block a fresh measurement at the same value)
    for p in manifest["points"]:
        if str(p.get("status")) not in (
            POINT_PENDING_DERIVE,
            POINT_AWAITING_CONFIRM,
            POINT_PENDING_RUN,
        ):
            continue
        try:
            pending_probe = float(p["params"].get(param_key))
        except (TypeError, ValueError):
            continue
        if abs(pending_probe - probe_value) <= 1e-9:
            raise ResearchBatchError(
                "request_conflict",
                f"已存在同探针的未完成点位 {p.get('point_id')}"
                f"（{probe_value:g}{unit_suffix}）：请先运行它，不重复记账",
            )
    existing = {str(p.get("point_id")) for p in manifest["points"]}
    n = len(manifest["points"])
    point_id = f"r{n + 1}"
    while point_id in existing:
        n += 1
        point_id = f"r{n + 1}"
    now = _now()
    new_point = {
        "point_id": point_id,
        "label": f"{probe_value:g}{unit_suffix}",
        "params": {"rule_id": rule_id, param_key: probe_value},
        "status": POINT_PENDING_DERIVE,
        "import_batch_id": None,
        "run_ref": None,
        "derivation": None,
        "diagnosis": None,
        "updated_at": now,
        "append_record": {
            "via": "append_point",
            "authorized_by": holder,
            "axis": axis_key,
            "decision_probe_value": probe_value,
            "decision_scale_percent": (
                probe_value if param_key == "scale_percent" else None
            ),
            "rounds_used": decision.get("rounds_used"),
            "appended_at": now,
        },
    }
    manifest["points"].append(new_point)
    manifest["status"] = _batch_status(manifest["points"])
    manifest["updated_at"] = now
    atomic_write_bytes(
        _manifest_path(workspace_root, batch_ref), stable_dump(manifest)
    )
    return manifest


def bind_simulation_identity(
    workspace_root: Path | str,
    batch_ref: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """R2-B5: pin the batch's simulation identity ONCE (forward-only).

    方法说明第三章(五) requires the common-random-number discipline
    across rounds: every round of one batch must face the SAME random
    stream and the SAME simulation precision, so the round-to-round
    metric movement is attributable to the load level alone - never to
    seed noise smuggled in through per-request bodies. Re-binding the
    identical identity is a no-op; any drift is an honest conflict and
    the pinned identity survives unchanged.
    """
    if not isinstance(identity, dict):
        raise ResearchBatchError(
            "invalid_input", f"模拟身份必须为字典，收到 {identity!r}"
        )
    try:
        root_seed = int(identity.get("root_seed"))
        samples = int(identity.get("samples"))
    except (TypeError, ValueError):
        raise ResearchBatchError(
            "invalid_input",
            "模拟身份要求整数 root_seed 与 samples（收到 "
            f"{identity.get('root_seed')!r} / {identity.get('samples')!r}）",
        ) from None
    weight_set_id = str(identity.get("weight_set_id") or "").strip()
    if not weight_set_id:
        raise ResearchBatchError(
            "invalid_input", "模拟身份要求非空 weight_set_id"
        )
    if samples <= 0:
        raise ResearchBatchError(
            "invalid_input", f"samples 必须为正整数，收到 {samples}"
        )
    pinned = {
        "root_seed": root_seed,
        "samples": samples,
        "weight_set_id": weight_set_id,
    }
    manifest = read_batch(workspace_root, batch_ref)
    existing = manifest.get("simulation_identity")
    if existing == pinned:
        return manifest
    if existing is not None:
        raise ResearchBatchError(
            "request_conflict",
            f"批次已钉住模拟身份 {existing}，本次请求 {pinned} 与之不一致："
            "同一批次各轮必须复用同一种子、样本数与权重集（CRN 纪律）；"
            "确需更换请新建批次",
        )
    manifest["simulation_identity"] = pinned
    manifest["updated_at"] = _now()
    atomic_write_bytes(
        _manifest_path(workspace_root, batch_ref), stable_dump(manifest)
    )
    return manifest


def set_archived(
    workspace_root: Path | str, batch_ref: str, *, archived: bool
) -> dict[str, Any]:
    """DR-10: batches are retained with the project lifecycle and can be
    archived (never auto-cleaned, never silently deleted)."""
    manifest = read_batch(workspace_root, batch_ref)
    manifest["archived"] = bool(archived)
    manifest["updated_at"] = _now()
    atomic_write_bytes(
        _manifest_path(workspace_root, batch_ref), stable_dump(manifest)
    )
    return manifest


__all__ = [
    "AXIS_PERFECT_CAPACITY",
    "BATCHES_DIR",
    "BATCH_KINDS",
    "BATCH_METHODS",
    "BATCH_SCHEMA_VERSION",
    "POINT_AWAITING_CONFIRM",
    "POINT_DONE",
    "POINT_FAILED",
    "POINT_PENDING_DERIVE",
    "POINT_PENDING_RUN",
    "RULE_LOAD_SCALE",
    "RULE_PERFECT_CAPACITY",
    "ResearchBatchError",
    "append_point",
    "bind_simulation_identity",
    "create_batch",
    "list_batches",
    "read_batch",
    "set_archived",
    "update_point",
]
