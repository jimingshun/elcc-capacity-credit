"""容量市场仿真出清.

分层（镜像结算/系数模块的成熟模式）：

- 需求曲线（Q-M3）：VRR 三点凸折线，锚＝目标可靠容量（引用或显式）×
  Net CONE（引用或显式）；默认形状 (0.97,1.75)(1.00,1.30)(1.05,1.00)
  （PJM 历史形状参照，系数可改）；价格领口可选（不填不启用、无默认值）。
  两端饱和（单区统一出清下的诚实简化，随曲线快照披露）。
- 出清引擎（Q-M2，纯函数、零随机流）：合格主体按报价升序堆栈与需求曲线
  交点 → 统一出清价＝边际机组报价；同价段按申报容量比例分配并披露；
  cap 处供给仍不足最低采购要求 → 触顶出清＋缺口披露（Q-M5）；申报>
  核定即阻断（Q-M4）。
- 市场力（Q-M1）：HHI/最大占比/关键供应商（剔除后 cap 内供给不足最低
  采购要求者）；阈值由调用方判读（研究定位＝披露不阻断）；缓解＝关键
  供应商报价替换为参考成本重清一次、两版并列。
- 报价台账＋冻结（Q-M6）：镜像结算价表版本化存储与系数"试算＋冻结"模式
  （冻结一次性不可变、绑定项目正式结果、rule_version 必填）。
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import (
    atomic_write_bytes,
    hash_payload,
    stable_dump,
)

# Q-M3：默认 VRR 形状（容量比, 价格比）——PJM 历史形状参照，调用方可改
DEFAULT_VRR_ANCHORS: tuple[tuple[float, float], tuple[float, float], tuple[float, float]] = (
    (0.97, 1.75),
    (1.00, 1.30),
    (1.05, 1.00),
)

MARKET_DIR = "market"
OFFERS_DIR = "offers"
OFFERS_MANIFEST = "offers_manifest.json"
CLEARING_RESULT_FILE = "clearing_result.json"
CLEARING_MANIFEST_FILE = "clearing_manifest.json"


class MarketError(ValueError):
    """市场模块拒绝（display kind + message）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------------------
# 需求曲线（Q-M3）
# ---------------------------------------------------------------------------


def build_demand_curve(
    *,
    target_mw: float,
    net_cone: float,
    anchors: Any = None,
    cap: Any = None,
    floor: Any = None,
    quantity_deviation_percent: Any = 0.0,
) -> dict[str, Any]:
    """构造 VRR 三点需求曲线（确定性纯函数）。

    锚＝目标容量×Net CONE；anchors＝((容量比,价格比)×3)（默认 PJM 参照形状，
    可改）；价格领口 cap/floor 可选（不填不启用——Q-M3：无默认数值）；
    quantity_deviation_percent 对容量轴平移（%，可为负）。"""
    try:
        target = float(target_mw)
        cone = float(net_cone)
    except (TypeError, ValueError):
        raise MarketError("invalid_input", "目标容量与 Net CONE 必须为数值") from None
    if target <= 0:
        raise MarketError("invalid_input", "目标容量必须 >0（MW）")
    if cone <= 0:
        raise MarketError("invalid_input", "Net CONE 必须为数值且 >0（元/kW·年）")
    if anchors is None:
        anchors = DEFAULT_VRR_ANCHORS
    if not isinstance(anchors, (list, tuple)) or len(anchors) != 3:
        raise MarketError(
            "invalid_input", "anchors 必须为三个 (容量比, 价格比) 点组"
        )
    pts: list[tuple[float, float]] = []
    for pair in anchors:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise MarketError("invalid_input", "每个锚点须为 (容量比, 价格比)")
        try:
            qr, pr = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            raise MarketError("invalid_input", "锚点系数必须为数值") from None
        if qr <= 0 or pr <= 0:
            raise MarketError("invalid_input", "锚点系数必须 >0")
        pts.append((qr, pr))
    if not (pts[0][0] < pts[1][0] < pts[2][0]):
        raise MarketError("invalid_input", "锚点容量比必须严格递增")
    if not (pts[0][1] > pts[1][1] > pts[2][1]):
        raise MarketError("invalid_input", "锚点价格比必须严格递减（凸需求曲线）")
    try:
        deviation = float(quantity_deviation_percent or 0.0)
    except (TypeError, ValueError):
        raise MarketError("invalid_input", "数量偏差必须为数值（%）") from None
    if deviation <= -100.0:
        raise MarketError("invalid_input", "数量偏差不能 ≤ −100%")
    factor = 1.0 + deviation / 100.0
    points: list[list[float]] = [
        [round(target * qr * factor, 6), round(cone * pr, 6)]
        for qr, pr in pts
    ]
    cap_value: float | None = None
    floor_value: float | None = None
    if cap is not None:
        try:
            cap_value = float(cap)
        except (TypeError, ValueError):
            raise MarketError("invalid_input", "价格上限必须为数值（元/kW·年）") from None
        if cap_value <= 0:
            raise MarketError("invalid_input", "价格上限必须 >0")
    if floor is not None:
        try:
            floor_value = float(floor)
        except (TypeError, ValueError):
            raise MarketError("invalid_input", "价格下限必须为数值（元/kW·年）") from None
        if floor_value <= 0:
            raise MarketError("invalid_input", "价格下限必须 >0")
    if cap_value is not None and floor_value is not None and cap_value <= floor_value:
        raise MarketError("invalid_input", "价格上限必须大于下限")
    if cap_value is not None:
        for point in points:
            point[1] = min(point[1], cap_value)
    if floor_value is not None:
        for point in points:
            point[1] = max(point[1], floor_value)
    return {
        "curve_schema_version": "1.0",
        "target_mw": target,
        "net_cone": cone,
        "anchors": [[qr, pr] for qr, pr in pts],
        "cap": cap_value,
        "floor": floor_value,
        "quantity_deviation_percent": deviation,
        "points": points,
        "note": (
            "VRR 三点折线（Q-M3 ；锚＝目标容量×Net CONE）；需求量在曲线"
            "两端饱和（单区统一出清的诚实简化）；价格领口未启用时曲线价域"
            "即锚点价域"
        ),
    }


def demand_quantity(curve: dict[str, Any], price: float) -> float:
    """需求曲线上的需求量：三点线性插值，两端饱和。"""
    points = sorted(curve["points"], key=lambda p: p[1])  # 价升序（量降序）
    lowest_q, lowest_p = points[0][0], points[0][1]
    highest_q, highest_p = points[-1][0], points[-1][1]
    if price <= lowest_p:
        return float(lowest_q)
    if price >= highest_p:
        return float(highest_q)
    for (q0, p0), (q1, p1) in zip(points, points[1:]):
        if p0 <= price <= p1:
            if p1 == p0:
                return float(max(q0, q1))
            share = (price - p0) / (p1 - p0)
            return float(q0 + (q1 - q0) * share)
    return float(highest_q)


# ---------------------------------------------------------------------------
# 出清引擎（Q-M2/Q-M4/Q-M5，纯函数）
# ---------------------------------------------------------------------------


def _validated_offers(offers: Any) -> list[dict[str, Any]]:
    """报价校验（Q-M4 前置）：申报 ≤ 核定；价格 >0；主体唯一。"""
    if not isinstance(offers, list):
        raise MarketError("invalid_input", "offers 必须为列表")
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for i, o in enumerate(offers):
        if not isinstance(o, dict):
            raise MarketError("invalid_input", f"offers[{i}] 必须为对象")
        sid = str(o.get("subject_id") or "").strip()
        if not sid:
            raise MarketError("invalid_input", f"offers[{i}] 缺主体标识")
        if sid in seen:
            raise MarketError("invalid_input", f"主体重复报价：{sid}")
        seen.add(sid)
        try:
            accredited = float(o.get("accredited_capacity_mw"))
            offered = float(o.get("offered_capacity_mw"))
            price = float(o.get("offer_price"))
        except (TypeError, ValueError):
            raise MarketError(
                "invalid_input",
                f"主体 {sid} 的核定/申报容量与报价必须为数值",
            ) from None
        if offered < 0 or accredited < 0 or price <= 0:
            raise MarketError(
                "invalid_input", f"主体 {sid}：容量 ≥0 且报价 >0（元/kW·年）"
            )
        if offered > accredited + 1e-9:
            raise MarketError(
                "invalid_input",
                f"主体 {sid} 申报容量 {offered} 超过核定可靠容量 {accredited}"
                "（Q-M4：申报不得超核定，引用 ELCC/EFC 点值）",
            )
        clean.append(
            {
                "subject_id": sid,
                "resource_type": str(o.get("resource_type") or ""),
                "accredited_capacity_mw": accredited,
                "offered_capacity_mw": offered,
                "offer_price": price,
                "qualified": bool(o.get("qualified", True)),
            }
        )
    return clean


def _clear_once(
    *,
    curve: dict[str, Any],
    offers: list[dict[str, Any]],
    min_procurement_mw: float,
    price_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """一次出清（供给堆栈升序 ∩ 需求曲线）。overrides 用于缓解重清。"""
    cap = curve.get("cap")
    effective = [
        {**o, "offer_price": (price_overrides or {}).get(o["subject_id"], o["offer_price"])}
        for o in offers
        if o["qualified"] and (cap is None or o["offer_price"] <= cap or (price_overrides or {}).get(o["subject_id"]) is not None)
    ]
    effective = [o for o in effective if o["offer_price"] <= (cap if cap is not None else float("inf"))]
    if not effective:
        return {
            "status": "not_cleared",
            "clearing_price": None,
            "procured_mw": 0.0,
            "shortfall_mw": round(min_procurement_mw, 6),
            "completion_ratio": 0.0,
            "winners": [],
            "losers": [dict(o, cleared_mw=0.0) for o in offers],
            "tie_allocated": False,
            "marginal": None,
        }
    prices = sorted({o["offer_price"] for o in effective})
    candidates = prices + ([cap] if cap is not None else [])

    def supply_at(price: float) -> float:
        return sum(o["offered_capacity_mw"] for o in effective if o["offer_price"] <= price + 1e-9)

    clearing_price: float | None = None
    for price in sorted(candidates):
        if supply_at(price) >= demand_quantity(curve, price) - 1e-9:
            clearing_price = price
            break
    if clearing_price is None:
        # 供给在 cap 处仍不足（无 cap 时理论上不可达——需求高端饱和有界）
        price_cap = cap if cap is not None else candidates[-1]
        procured = supply_at(price_cap)
        return {
            "status": (
                "cleared_with_shortfall" if procured > 0 else "not_cleared"
            ),
            "clearing_price": price_cap if procured > 0 else None,
            "procured_mw": round(procured, 6),
            "shortfall_mw": round(max(0.0, min_procurement_mw - procured), 6),
            "completion_ratio": round(procured / curve["target_mw"], 6) if curve["target_mw"] else 0.0,
            "winners": _allocate(
                effective, supply_at(price_cap), demand_quantity(curve, price_cap), price_cap
            )["winners"],
            "losers": [
                dict(o, cleared_mw=0.0)
                for o in offers
                if o["subject_id"] not in {w["subject_id"] for w in _allocate(
                    effective, supply_at(price_cap), demand_quantity(curve, price_cap), price_cap
                )["winners"]}
            ],
            "tie_allocated": False,
            "marginal": None,
        }
    demanded = demand_quantity(curve, clearing_price)
    supplied = supply_at(clearing_price)
    procured = min(supplied, demanded)
    allocation = _allocate(effective, supplied, procured, clearing_price)
    winners = allocation["winners"]
    winner_ids = {w["subject_id"] for w in winners}
    losers = [dict(o, cleared_mw=0.0) for o in offers if o["subject_id"] not in winner_ids]
    shortfall = max(0.0, min_procurement_mw - procured)
    status = "cleared_with_shortfall" if shortfall > 1e-6 else "cleared"
    marginal = None
    if allocation["tie_group"] is not None:
        marginal = {
            "kind": "tie_group",
            "price": clearing_price,
            "subjects": sorted(allocation["tie_group"]),
        }
    else:
        full_stack_below = sum(
            o["offered_capacity_mw"]
            for o in effective
            if o["offer_price"] < clearing_price - 1e-9
        )
        marginal_subjects = [
            o["subject_id"]
            for o in effective
            if abs(o["offer_price"] - clearing_price) < 1e-9
        ]
        marginal = {
            "kind": "marginal_unit",
            "price": clearing_price,
            "subjects": sorted(marginal_subjects),
            "below_stack_mw": round(full_stack_below, 6),
        }
    return {
        "status": status,
        "clearing_price": clearing_price,
        "procured_mw": round(procured, 6),
        "shortfall_mw": round(shortfall, 6),
        "completion_ratio": (
            round(procured / curve["target_mw"], 6) if curve["target_mw"] else 0.0
        ),
        "winners": winners,
        "losers": losers,
        "tie_allocated": allocation["tie_group"] is not None,
        "marginal": marginal,
    }


def _allocate(
    effective: list[dict[str, Any]],
    supplied_at_price: float,
    procured: float,
    price: float,
) -> dict[str, Any]:
    """把 procured 分配到 ≤price 的申报上；同价段按申报比例分配（Q-M2）。"""
    below = [o for o in effective if o["offer_price"] < price - 1e-9]
    at_price = [o for o in effective if abs(o["offer_price"] - price) < 1e-9]
    below_mw = sum(o["offered_capacity_mw"] for o in below)
    winners: list[dict[str, Any]] = [
        {
            "subject_id": o["subject_id"],
            "resource_type": o["resource_type"],
            "offered_capacity_mw": o["offered_capacity_mw"],
            "cleared_mw": o["offered_capacity_mw"],
            "clearing_price": price,
            "offer_price": o["offer_price"],
        }
        for o in below
    ]
    need = procured - below_mw
    tie_group: list[str] | None = None
    if need < -1e-9:
        need = 0.0
    at_total = sum(o["offered_capacity_mw"] for o in at_price)
    if at_total <= need + 1e-9:
        winners.extend(
            {
                "subject_id": o["subject_id"],
                "resource_type": o["resource_type"],
                "offered_capacity_mw": o["offered_capacity_mw"],
                "cleared_mw": o["offered_capacity_mw"],
                "clearing_price": price,
                "offer_price": o["offer_price"],
            }
            for o in at_price
        )
    elif at_price and len(at_price) >= 2:
        # Q-M2：同价段≥2 家且需部分分配 ⇒ 按申报比例分配并披露
        tie_group = sorted(o["subject_id"] for o in at_price)
        for o in at_price:
            share = o["offered_capacity_mw"] / at_total
            winners.append(
                {
                    "subject_id": o["subject_id"],
                    "resource_type": o["resource_type"],
                    "offered_capacity_mw": o["offered_capacity_mw"],
                    "cleared_mw": round(need * share, 6),
                    "clearing_price": price,
                    "offer_price": o["offer_price"],
                }
            )
    elif at_price:
        # 单一边际主体部分中标＝普通边际，不是并列报价
        for o in at_price:
            winners.append(
                {
                    "subject_id": o["subject_id"],
                    "resource_type": o["resource_type"],
                    "offered_capacity_mw": o["offered_capacity_mw"],
                    "cleared_mw": round(need, 6),
                    "clearing_price": price,
                    "offer_price": o["offer_price"],
                }
            )
    return {"winners": winners, "tie_group": tie_group}


def _market_power(
    *,
    curve: dict[str, Any],
    offers: list[dict[str, Any]],
    min_procurement_mw: float,
) -> dict[str, Any]:
    """Q-M1：HHI/最大占比/关键供应商（披露不阻断；阈值由调用方判读）。"""
    stack = [o for o in offers if o["qualified"]]
    total = sum(o["offered_capacity_mw"] for o in stack)
    cap = curve.get("cap")
    cap_effective = cap if cap is not None else max(
        (o["offer_price"] for o in stack), default=0.0
    )
    if total <= 0:
        return {"hhi": None, "max_share": None, "pivotal_subjects": []}

    def supply_without(sid: str) -> float:
        return sum(
            o["offered_capacity_mw"]
            for o in stack
            if o["subject_id"] != sid
            and (cap is None or o["offer_price"] <= cap)
        )

    pivotal = [
        o["subject_id"]
        for o in stack
        if supply_without(o["subject_id"]) < min_procurement_mw - 1e-9
    ]
    shares = {o["subject_id"]: o["offered_capacity_mw"] / total for o in stack}
    hhi = sum(share * share * 10000.0 for share in shares.values())
    return {
        "hhi": round(hhi, 2),
        "max_share": round(max(shares.values()), 6) if shares else None,
        "pivotal_subjects": pivotal,
        "note": (
            "HHI/最大占比按申报容量份额（出清前口径）；关键供应商＝剔除后"
            "领口内供给不足最低采购要求；阈值为参数化判读（Q-M1：披露不阻断）"
        ),
    }


def compute_clearing(
    *,
    curve: dict[str, Any],
    offers: Any,
    min_procurement_mw: float,
    mitigation_reference_cost: Any = None,
) -> dict[str, Any]:
    """统一出清（纯函数）：校验（Q-M4）→ 出清（Q-M2/Q-M5）→ 市场力
    （Q-M1）→ 可选缓解重清一次（两版并列，不覆盖原结果）。"""
    try:
        min_proc = float(min_procurement_mw)
    except (TypeError, ValueError):
        raise MarketError("invalid_input", "最低采购要求必须为数值（MW）") from None
    if min_proc <= 0:
        raise MarketError("invalid_input", "最低采购要求必须 >0（MW）")
    if not isinstance(curve, dict) or not curve.get("points"):
        raise MarketError("invalid_input", "需求曲线缺失（先构造 VRR 曲线）")
    clean = _validated_offers(offers)
    base = _clear_once(curve=curve, offers=clean, min_procurement_mw=min_proc)
    power = _market_power(
        curve=curve, offers=clean, min_procurement_mw=min_proc
    )
    result: dict[str, Any] = {
        "clearing_schema_version": "1.0",
        **base,
        "min_procurement_mw": min_proc,
        "target_mw": curve["target_mw"],
        "market_power": power,
        "mitigation": None,
    }
    if mitigation_reference_cost is not None:
        try:
            ref_cost = float(mitigation_reference_cost)
        except (TypeError, ValueError):
            raise MarketError("invalid_input", "缓解参考成本必须为数值（元/kW·年）") from None
        if ref_cost <= 0 or not power["pivotal_subjects"]:
            if ref_cost <= 0:
                raise MarketError("invalid_input", "缓解参考成本必须 >0")
        overrides = {
            sid: min(ref_cost, o["offer_price"])
            for sid, o in ((o["subject_id"], o) for o in clean)
            if sid in set(power["pivotal_subjects"])
        }
        mitigated = _clear_once(
            curve=curve,
            offers=clean,
            min_procurement_mw=min_proc,
            price_overrides=overrides if overrides else None,
        )
        result["mitigation"] = {
            "reference_cost": ref_cost,
            "replaced_subjects": sorted(overrides),
            "price_before": base["clearing_price"],
            "price_after": mitigated["clearing_price"],
            "procured_before": base["procured_mw"],
            "procured_after": mitigated["procured_mw"],
            "status_after": mitigated["status"],
            "note": "缓解＝关键供应商报价替换为参考成本后重清一次；两版并列，正式结果仍为缓解前",
        }
    return result


__all__ = [
    "DEFAULT_VRR_ANCHORS",
    "MarketError",
    "build_demand_curve",
    "compute_clearing",
    "demand_quantity",
    "freeze_clearing_result",
    "import_offers_csv",
    "read_clearing_result",
    "read_offers",
    "render_offers_template",
    "write_offers",
]


# ---------------------------------------------------------------------------
# 报价台账（Q-M6：版本化存储——镜像结算价表模式）与 CSV（复用结算模板纪律）
# ---------------------------------------------------------------------------

OFFERS_CSV_COLUMNS = (
    "subject_id",
    "resource_type",
    "accredited_capacity_mw",
    "offered_capacity_mw",
    "offer_price",
    "qualified",
)
_OFFERS_REQUIRED = OFFERS_CSV_COLUMNS
_OFFERS_TEXT = ("subject_id", "resource_type", "qualified")


def _require_market_project(workspace_root: Path | str, project_ref: str) -> Path:
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        _find_entry,
        measurement_type_of,
        read_registry,
    )

    root = Path(workspace_root)
    try:
        registry = read_registry(root)
        entry = _find_entry(registry, project_ref)
    except ProjectServiceError as exc:
        raise MarketError(exc.kind, exc.message) from None
    locked = measurement_type_of(entry)
    if locked != "market_simulation":
        raise MarketError(
            "invalid_input",
            f"市场仿真操作需要容量市场仿真项目（当前锁定类型：{locked or '未登记'}）",
        )
    return root / project_ref


def _validated_offer_rows(rows: Any) -> list[dict[str, Any]]:
    """台账行校验：主体唯一/容量与报价数值域/申报≤核定（Q-M4）/
    qualified 布尔语义（空＝合格）。"""
    if not isinstance(rows, list) or not rows:
        raise MarketError("invalid_input", "报价台账必须为非空列表（至少一个主体）")
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for i, item in enumerate(rows):
        if not isinstance(item, dict):
            raise MarketError("invalid_input", f"报价行[{i}]必须为对象")
        sid = str(item.get("subject_id") or "").strip()
        if not sid:
            raise MarketError("invalid_input", f"报价行[{i}]缺主体标识")
        if sid in seen:
            raise MarketError("invalid_input", f"主体重复报价：{sid}")
        seen.add(sid)
        try:
            accredited = float(item.get("accredited_capacity_mw"))
            offered = float(item.get("offered_capacity_mw"))
            price = float(item.get("offer_price"))
        except (TypeError, ValueError):
            raise MarketError(
                "invalid_input",
                f"主体 {sid} 的核定/申报容量与报价必须为数值",
            ) from None
        if accredited < 0 or offered < 0 or price <= 0:
            raise MarketError(
                "invalid_input", f"主体 {sid}：容量 ≥0 且报价 >0（元/kW·年）"
            )
        if offered > accredited + 1e-9:
            raise MarketError(
                "invalid_input",
                f"主体 {sid} 申报容量 {offered} 超过核定 {accredited}"
                "（Q-M4：不得超 ELCC/EFC 核定点值）",
            )
        qualified_raw = item.get("qualified", True)
        if isinstance(qualified_raw, str):
            qualified = qualified_raw.strip().lower() not in ("false", "0", "否", "暂停")
        else:
            qualified = bool(qualified_raw)
        clean.append(
            {
                "subject_id": sid,
                "resource_type": str(item.get("resource_type") or "").strip(),
                "accredited_capacity_mw": accredited,
                "offered_capacity_mw": offered,
                "offer_price": price,
                "qualified": qualified,
            }
        )
    return clean


def write_offers(
    *, workspace_root: Path | str, project_ref: str, rows: Any
) -> dict[str, Any]:
    """报价台账写入＝新版本（历史保留，镜像结算价表模式）。"""
    project_dir = _require_market_project(workspace_root, project_ref)
    clean = _validated_offer_rows(rows)
    base = project_dir / MARKET_DIR / OFFERS_DIR
    base.mkdir(parents=True, exist_ok=True)
    manifest_path = base / OFFERS_MANIFEST
    manifest: dict[str, Any] = {"versions": [], "current": None}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version_no = len(manifest["versions"]) + 1
    record = {
        "offers_schema_version": "1.0",
        "version": version_no,
        "offers": clean,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    version_file = f"v{version_no:03d}.json"
    atomic_write_bytes(base / version_file, stable_dump(record), immutable=True)
    manifest["versions"].append(
        {"version": version_no, "file": version_file, "sha256": hash_payload(record)}
    )
    manifest["current"] = version_no
    atomic_write_bytes(manifest_path, stable_dump(manifest), immutable=False)
    return {"version": version_no, "offers": clean}


def read_offers(workspace_root: Path | str, project_ref: str) -> dict[str, Any]:
    """读报价台账：当前版本＋全部历史（未录入→current None 如实）。"""
    base = (
        Path(workspace_root) / project_ref / MARKET_DIR / OFFERS_DIR
    )
    manifest_path = base / OFFERS_MANIFEST
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


def render_offers_template() -> tuple[str, bytes]:
    """可填写 CSV 模板（表头行；utf-8-sig；无示例行——示例价不得入正式链路）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(OFFERS_CSV_COLUMNS)
    return "market_offers.csv", buffer.getvalue().encode("utf-8-sig")


def import_offers_csv(
    *, workspace_root: Path | str, project_ref: str, csv_text: str
) -> dict[str, Any]:
    """CSV 批量导入（镜像结算 CSV 纪律：缺列/非数值/未知列指明行拒绝；
    空值＝缺失不补 0）。"""
    raw = str(csv_text or "").lstrip(chr(0xFEFF))
    if not raw.strip():
        raise MarketError("invalid_input", "CSV 内容为空")
    reader = csv.reader(io.StringIO(raw))
    try:
        header = next(reader)
    except StopIteration:
        raise MarketError("invalid_input", "CSV 内容为空") from None
    header = [h.strip() for h in header]
    missing = [c for c in _OFFERS_REQUIRED if c not in header]
    if missing:
        raise MarketError(
            "invalid_input", f"CSV 缺必需列：{'、'.join(missing)}（请用模板列）"
        )
    unknown = [h for h in header if h and h not in OFFERS_CSV_COLUMNS]
    if unknown:
        raise MarketError(
            "invalid_input",
            f"CSV 含未知列：{'、'.join(unknown)}（不静默忽略；请用模板列）",
        )
    rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            continue
        row: dict[str, Any] = {}
        for col, cell in zip(header, raw_row):
            col = col.strip()
            if not col:
                continue
            value = cell.strip()
            if col in _OFFERS_TEXT:
                row[col] = value
                continue
            if value == "":
                continue
            try:
                row[col] = float(value)
            except ValueError:
                raise MarketError(
                    "invalid_input",
                    f"第{index}行 {col} 必须为数值（实际 {value!r}）",
                ) from None
        for col in _OFFERS_REQUIRED:
            if col not in row or row[col] in ("", None):
                row[col] = True if col == "qualified" else None
                if col != "qualified":
                    raise MarketError(
                        "invalid_input", f"第{index}行 {col} 不能为空（缺失≠0）"
                    )
        rows.append(row)
    if not rows:
        raise MarketError("invalid_input", "CSV 无数据行")
    return write_offers(
        workspace_root=workspace_root, project_ref=project_ref, rows=rows
    )


# ---------------------------------------------------------------------------
# 冻结正式结果（Q-M6：一次性不可变＋绑定项目正式结果——镜像系数冻结）
# ---------------------------------------------------------------------------


def freeze_clearing_result(
    *,
    workspace_root: Path | str,
    project_ref: str,
    curve_payload: dict[str, Any],
    offers: Any,
    min_procurement_mw: float,
    rule_version: str,
    confirmer: str,
    mitigation_reference_cost: Any = None,
    software_version: str = "",
    software_commit: str = "",
) -> dict[str, Any]:
    """出清（公式 v1）→ 写正式结果（manifest last）→ 绑定项目正式结果。

    全部校验先于副作用：类型锁/已完成门/确认人/rule_version/输入无效一律
    写盘前拒绝；绑定失败发生在写盘前（request_conflict 先检，正式结果
    永不成为孤儿）。"""
    from capacity_credit.application.project_service import (
        ProjectServiceError,
        bind_formal_result,
        get_formal_result,
    )

    root = Path(workspace_root)
    if not str(confirmer or "").strip():
        raise MarketError("invalid_input", "冻结是显式用户动作：确认人必填（留痕追溯）")
    if not str(rule_version or "").strip():
        raise MarketError(
            "invalid_input", "rule_version 必填（Q-M6：市场规则版本随结果追溯）"
        )
    project_dir = _require_market_project(root, project_ref)
    try:
        existing = get_formal_result(root, project_ref)
    except ProjectServiceError as exc:
        raise MarketError(exc.kind, exc.message) from None
    if isinstance(existing, dict) and existing.get("run_ref"):
        raise MarketError(
            "request_conflict",
            "该项目已有正式出清结果"
            f"（{str(existing.get('run_ref'))[:8]}…），正式结果不可替换；"
            "重跑请复制/新建市场仿真项目。",
        )
    curve = build_demand_curve(**curve_payload)
    result = compute_clearing(
        curve=curve,
        offers=offers,
        min_procurement_mw=min_procurement_mw,
        mitigation_reference_cost=mitigation_reference_cost,
    )
    clearing_ref = uuid.uuid4().hex
    target_dir = project_dir / MARKET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    formal = {
        "market_clearing_schema_version": "1.0",
        "clearing_ref": clearing_ref,
        "project_ref": project_ref,
        "rule_version": str(rule_version).strip(),
        "confirmer": confirmer.strip(),
        "frozen_at": datetime.now(UTC).isoformat(),
        "curve": curve,
        **result,
    }
    formal_sha = hash_payload(formal)
    atomic_write_bytes(
        target_dir / CLEARING_RESULT_FILE, stable_dump(formal), immutable=True
    )
    manifest = {
        "market_clearing_schema_version": "1.0",
        "clearing_ref": clearing_ref,
        "project_ref": project_ref,
        "status": "frozen",
        "rule_version": str(rule_version).strip(),
        "confirmer": confirmer.strip(),
        "frozen_at": formal["frozen_at"],
        "formal_path": CLEARING_RESULT_FILE,
        "formal_sha256": formal_sha,
        "result_status": result["status"],
        "clearing_price": result["clearing_price"],
        "procured_mw": result["procured_mw"],
        "shortfall_mw": result["shortfall_mw"],
        "target_mw": result["target_mw"],
        "software_version": software_version,
        "software_commit": software_commit,
    }
    atomic_write_bytes(
        target_dir / CLEARING_MANIFEST_FILE, stable_dump(manifest), immutable=True
    )
    try:
        bind_formal_result(
            root,
            project_ref,
            domain="market",
            run_ref=clearing_ref,
            result_sha256=formal_sha,
        )
    except ProjectServiceError as exc:
        raise MarketError(exc.kind, exc.message) from None
    return manifest


def read_clearing_result(project_dir: Path | str) -> dict[str, Any] | None:
    """读正式出清结果（结果索引与报告的数据源）：无→None；坏→诚实摘要。"""
    base = Path(project_dir) / MARKET_DIR
    manifest_path = base / CLEARING_MANIFEST_FILE
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        formal = json.loads(
            (base / str(manifest.get("formal_path") or CLEARING_RESULT_FILE)).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError) as exc:
        return {"invalid": True, "invalid_reason": f"出清记录不可读取：{exc}"}
    if not isinstance(formal, dict) or not isinstance(manifest, dict):
        return {"invalid": True, "invalid_reason": "出清记录形状无效"}
    return {"invalid": False, "manifest": manifest, "formal": formal}


# ---------------------------------------------------------------------------
# 报告导出（Q-M6：报告服务同款纪律——同身份重放、清单哈希校验）
# ---------------------------------------------------------------------------


def export_market_report(
    *, workspace_root: Path | str, project_ref: str
) -> dict[str, Any]:
    """导出已冻结出清结果的报告集；未冻结诚实拒绝。"""
    project_dir = _require_market_project(workspace_root, project_ref)
    record = read_clearing_result(project_dir)
    if record is None:
        raise MarketError("invalid_input", "出清结果未冻结，不能导出报告")
    if record.get("invalid"):
        raise MarketError(
            "invalid_input", f"出清记录不可读取：{record.get('invalid_reason')}"
        )
    from capacity_credit.reporting.market_report import (
        MarketReportError,
        export_market_report as render_market_report,
    )

    output = (
        Path(workspace_root)
        / "reports"
        / "market"
        / project_ref
        / str(record["formal"].get("clearing_ref"))
        / "export"
    )
    try:
        return render_market_report(formal=record["formal"], output_dir=output)
    except MarketReportError as exc:
        raise MarketError(exc.code, exc.message) from None


def read_market_report_downloads(
    *, workspace_root: Path | str, project_ref: str
) -> dict[str, Any]:
    """校验过的报告下载集（哈希对清单逐个复核；不触发导出）。"""
    import hashlib

    project_dir = _require_market_project(workspace_root, project_ref)
    record = read_clearing_result(project_dir)
    if record is None or record.get("invalid"):
        return {"available": False, "reason": "出清结果未冻结", "files": []}
    export_dir = (
        Path(workspace_root)
        / "reports"
        / "market"
        / project_ref
        / str(record["formal"].get("clearing_ref"))
        / "export"
    )
    manifest_path = export_dir / "market_report_manifest.json"
    if not manifest_path.is_file():
        return {"available": False, "reason": "尚未导出报告", "files": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = []
        for entry in manifest.get("files", []):
            name = str(entry.get("name", ""))
            data = (export_dir / name).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != entry.get("sha256"):
                raise ValueError(f"文件哈希不一致：{name}")
            files.append(
                {
                    "name": name,
                    "bytes": data,
                    "sha256": digest,
                    "content_type": str(
                        entry.get("content_type", "application/octet-stream")
                    ),
                }
            )
    except (OSError, ValueError) as exc:
        return {"available": False, "reason": str(exc), "files": []}
    return {"available": True, "reason": None, "files": files}
