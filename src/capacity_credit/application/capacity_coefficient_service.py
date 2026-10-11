"""容量供需系数（demand_coefficient，Step 2 Stage C）.

需求类设计 §5（主责口径）＋ **Q-D3 六项裁决（用户 2026-10-07）**：

- 公式 v1（冻结）：系数 = clamp(目标可靠容量 ÷ 核定可靠容量, 0.0, 1.0)
- Q-D3a 分子＝目标可靠容量（MW，≥0；引用 demand_target_capacity 已收敛
  点值或显式输入）
- Q-D3b 分母＝核定可靠容量（MW，**必须 >0**；引用 ELCC/EFC 认可容量点值
  或人工核定值）——分母为零是真正会"报错"的点，输入门堵死（Q-D3d 配套）
- Q-D3c 封顶 1.0（cap_triggered 标记；原始比值如实保留）
- Q-D3d 下限 0.0（floor_triggered 标记；分母>0∧分子≥0 时数学上不可触发，
  标记位如实保留为公式完整性证据）
- Q-D3e 年度口径（caliber="年度"；月度折算留 Q-S 系列）
- Q-D3f 可被结算引用的正式参数——本轮不持久化（R-C1）：交付"正式口径
  计算器"，冻结为结算可引用正式结果留 Step 3 与结算消费一起设计

确定性算术（纯函数）：无内核改动、无运行记录、零副作用；输出携带
formula_version、原始比值、触发标记与分子分母实际取值＋来源说明
（引用路径的血缘由路由层解析后写入 lineage）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CapacityCoefficientError(ValueError):
    """Invalid coefficient input (display kind carried by the route)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CapacityCoefficientInput(BaseModel):
    """§5.2 输入字段（分子/分母各显式输入或引用二选一；引用由路由层解析
    为点值后进入本模型——服务本体保持纯函数）。"""

    model_config = ConfigDict(extra="forbid")

    target_capacity_mw: float | None = Field(default=None, ge=0.0)
    accredited_capacity_mw: float | None = Field(default=None, gt=0.0)
    target_year: int
    numerator_source_note: str = ""
    accredited_source_note: str = ""

    @model_validator(mode="after")
    def _require_inputs(self):
        if self.target_capacity_mw is None:
            raise CapacityCoefficientError(
                "分子 target_capacity_mw 必填（显式输入，或提供 "
                "numerator_reference 引用目标可靠容量结果）"
            )
        if self.accredited_capacity_mw is None:
            raise CapacityCoefficientError(
                "分母 accredited_capacity_mw 必填且必须 >0（显式输入，或提供 "
                "denominator_reference 引用 ELCC/EFC 认可容量结果）"
            )
        return self


def compute_capacity_coefficient(payload: dict[str, Any]) -> dict[str, Any]:
    """公式 v1（纯函数）：clamp(分子 ÷ 分母, 0.0, 1.0)＋触发标记＋血缘。"""
    try:
        data = CapacityCoefficientInput.model_validate(payload)
    except CapacityCoefficientError:
        raise
    except Exception as exc:  # noqa: BLE001 - honest invalid-input surface
        raise CapacityCoefficientError(f"供需系数输入无效：{exc}") from exc

    raw = data.target_capacity_mw / data.accredited_capacity_mw
    coefficient = min(1.0, max(0.0, raw))
    return {
        "formula_version": "v1",
        "caliber": "年度",
        "coefficient": round(coefficient, 6),
        "raw_ratio": raw,
        "cap_triggered": raw > 1.0,
        "floor_triggered": raw < 0.0,
        "inputs": {
            "target_capacity_mw": data.target_capacity_mw,
            "accredited_capacity_mw": data.accredited_capacity_mw,
        },
        "declared_inputs": {
            "target_year": data.target_year,
            "numerator_source_note": data.numerator_source_note,
            "accredited_source_note": data.accredited_source_note,
        },
    }


__all__ = [
    "CapacityCoefficientError",
    "CapacityCoefficientInput",
    "compute_capacity_coefficient",
]
