"""容量缺口与备用裕度（demand_capacity_gap，Step 2 Stage B）.

需求类设计 §4（主责口径）：确定性算术 preview——无内核改动、无运行记录、
零副作用。公式（§4.2）：

- 缺口/盈余 = 目标可靠容量 −（现有核定可靠容量 − 预计退役 + 已确定新增）
- 可靠容量口径备用裕度 =（核定 − 退役 + 新增 − 峰值负荷）÷ 峰值负荷
- 铭牌口径备用裕度 =（铭牌容量合计 − 峰值负荷）÷ 峰值负荷；
  **未提供铭牌时该键缺失（不补零、不冒充可得）**
- 两口径**分别显示**，不得混算互替
- 构成分解＝退役/新增/目标−峰荷三项**测算事实并列**（不生成归因判断——
  四层结论模型之「分析判断」留人工审核）；分资源构成（Q-D2 
  2026-10-10）＝现有核定按资源类型分组求和＋占比，**构成分解非缺供
  责任分摊**；未填报的构成合计与核定差额如实披露（不阻断——本服务
  为确定性算术零副作用）
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Q-D2：供给侧资源类型枚举（联络线属供给侧冻结资源，
# 与需求类设计 §8 口径一致）
_RESOURCE_TYPES_GAP = (
    "coal", "gas", "hydro", "storage", "solar", "wind", "intertie", "other",
)


class CapacityGapError(ValueError):
    """Invalid gap-calculation input (display kind carried by the route)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CapacityGapInput(BaseModel):
    """§4.1 输入字段（显式输入或引用二选一；引用由路由层解析为
    target_capacity_mw 后进入本模型——服务本体保持纯函数）。"""

    model_config = ConfigDict(extra="forbid")

    target_capacity_mw: float | None = Field(default=None, ge=0.0)
    accredited_capacity_mw: float = Field(ge=0.0)
    retirement_mw: float = Field(default=0.0, ge=0.0)
    additions_mw: float = Field(default=0.0, ge=0.0)
    peak_load_mw: float = Field(gt=0.0)
    nameplate_capacity_mw: float | None = Field(default=None, ge=0.0)
    target_year: int
    accredited_source_note: str = ""
    peak_load_source_note: str = ""
    # Q-D2：可选分资源构成（resource_type→核定 MW；
    # 取各资源 ELCC/EFC 正式结果点值，未核定类型不填报）
    resource_composition: list[dict[str, Any]] | None = Field(default=None)

    @model_validator(mode="after")
    def _require_target(self):
        if self.target_capacity_mw is None:
            raise CapacityGapError(
                "target_capacity_mw 必填（显式输入，或提供 target_reference 引用）"
            )
        return self


def _validated_composition(
    composition: list[dict[str, Any]] | None,
) -> list[tuple[str, float]] | None:
    """Q-D2 构成输入校验：类型枚举／MW ≥0／同类型不重复／条目为对象；
    未提供→None（输出不含构成分解键，不冒充可得）。"""
    if composition is None:
        return None
    if not isinstance(composition, list) or not composition:
        raise CapacityGapError("resource_composition 必须为非空列表（或省略）")
    seen: set[str] = set()
    clean: list[tuple[str, float]] = []
    for i, item in enumerate(composition):
        if not isinstance(item, dict):
            raise CapacityGapError(f"resource_composition[{i}] 必须为对象")
        rtype = str(item.get("resource_type") or "").strip()
        if rtype not in _RESOURCE_TYPES_GAP:
            raise CapacityGapError(
                "resource_composition 资源类型必须为 "
                f"{'/'.join(_RESOURCE_TYPES_GAP)} 之一（实际 {rtype!r}）"
            )
        if rtype in seen:
            raise CapacityGapError(f"resource_composition 资源类型重复：{rtype}")
        seen.add(rtype)
        try:
            mw = float(item.get("accredited_mw"))
        except (TypeError, ValueError):
            raise CapacityGapError(
                f"resource_composition {rtype} 的核定容量必须为数值（MW）"
            ) from None
        if mw < 0:
            raise CapacityGapError(
                f"resource_composition {rtype} 的核定容量必须 ≥0"
            )
        clean.append((rtype, mw))
    # 确定性顺序＝枚举序（展示与回放稳定）
    order = {t: i for i, t in enumerate(_RESOURCE_TYPES_GAP)}
    clean.sort(key=lambda pair: order[pair[0]])
    return clean


def compute_capacity_gap(payload: dict[str, Any]) -> dict[str, Any]:
    """确定性算术（纯函数）：错误＝CapacityGapError，输出键＝§4.2 全集。"""
    try:
        data = CapacityGapInput.model_validate(payload)
    except CapacityGapError:
        raise
    except Exception as exc:  # noqa: BLE001 - honest invalid-input surface
        raise CapacityGapError(f"缺口测算输入无效：{exc}") from exc

    effective = (
        data.accredited_capacity_mw - data.retirement_mw + data.additions_mw
    )
    gap = data.target_capacity_mw - effective
    out: dict[str, Any] = {
        "gap_mw": round(gap, 6),
        "gap_direction": "缺口" if gap > 0 else ("盈余" if gap < 0 else "持平"),
        "effective_reliable_capacity_mw": round(effective, 6),
        "reliable_capacity_margin": round((effective - data.peak_load_mw) / data.peak_load_mw, 8),
        # 构成分解＝测算事实并列（不作归因判断；Q-D2 分资源口径不输出）
        "composition": {
            "retirement_mw": data.retirement_mw,
            "additions_mw": data.additions_mw,
            "target_minus_peak_mw": round(data.target_capacity_mw - data.peak_load_mw, 6),
        },
        "declared_inputs": {
            "target_year": data.target_year,
            "accredited_source_note": data.accredited_source_note,
            "peak_load_source_note": data.peak_load_source_note,
            "nameplate_declared": data.nameplate_capacity_mw is not None,
        },
    }
    if data.nameplate_capacity_mw is not None:
        out["nameplate_capacity_mw"] = data.nameplate_capacity_mw
        out["nameplate_margin"] = round(
            (data.nameplate_capacity_mw - data.peak_load_mw) / data.peak_load_mw, 8
        )
    # Q-D2：构成分解（占比），非缺供责任分摊
    composition = _validated_composition(data.resource_composition)
    if composition is not None:
        total = round(sum(mw for _t, mw in composition), 6)
        rows = [
            {
                "resource_type": rtype,
                "accredited_mw": round(mw, 6),
                "share": (round(mw / total, 8) if total > 0 else None),
            }
            for rtype, mw in composition
        ]
        mismatch = round(data.accredited_capacity_mw - total, 6)
        out["resource_contribution"] = {
            "rows": rows,
            "total_mw": total,
            "note": (
                "构成分解，非缺供责任分摊；数值应取各资源 ELCC/EFC 正式"
                "结果点值，未核定类型不填报（不拿铭牌凑数）"
            ),
            "mismatch_mw": mismatch,
            "mismatch_disclosed": abs(mismatch) > 1e-6,
        }
    return out


__all__ = ["CapacityGapError", "CapacityGapInput", "compute_capacity_gap"]
