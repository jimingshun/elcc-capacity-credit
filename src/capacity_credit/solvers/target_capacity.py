"""Absolute-target capacity search (Step 2 Stage A, 需求类设计 §3).

与 equal_risk 的分工：``search_equal_risk`` 只做等风险配对匹配（trial−reference
差值对 0），本模块解的是**绝对目标**求根问题——risk(X) 随追加完美容量 X 单调
不增，求满足 risk(X) ≤ 目标 的最小 X。复用既有完美容量点评估机制（调用方经
``efc_components.make_perfect_trial_provider`` 注入 risk_provider），本模块不改
充裕度内核、不碰随机流、不降收敛要求。

诚实终态纪律（与 ELCC/EFC 同门）：
- 收敛＝区间宽度 ≤ 容差（MW），点值保守取区间上端（该点风险已验证 ≤ 目标）；
- 现状已达标（下界处即满足）⇒ 点值=下界＋TARGET_ALREADY_MET 诊断；
- 界内不可达 ⇒ 无点值＋TARGET_NOT_REACHED_IN_BOUNDS；
- 迭代耗尽 ⇒ 无点值＋MAX_ITERATIONS_EXHAUSTED；
- 单调性违例逐点记台账（不静默、不fabricate），严重反向（下界风险<上界风险）
  ⇒ 无点值＋MONOTONICITY_VIOLATED。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from capacity_credit.contracts.enums import ResultStatus, SearchConvergence


class TargetCapacityConfigError(ValueError):
    """Invalid search specification (stable display kind)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class TargetCapacitySearchResult:
    """Honest outcome of the absolute-target bisection."""

    estimate_mw: float | None
    bracket_low_mw: float
    bracket_high_mw: float
    bracket_low_risk: float | None
    bracket_high_risk: float | None
    result_status: ResultStatus
    search_convergence: SearchConvergence
    iterations: int
    monotonicity_ledger: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)


def search_target_capacity(
    *,
    lower_bound_mw: float,
    upper_bound_mw: float,
    capacity_tolerance_mw: float,
    max_iterations: int,
    target_value: float,
    risk_provider: Callable[[float], float],
) -> TargetCapacitySearchResult:
    """Bisect the monotone-nonincreasing risk(X) against an absolute target.

    ``risk_provider(X)`` returns the engine's own aggregate primary-risk
    metric at perfect capacity X (the caller binds the metric selection
    and the frozen evidence; this module never computes risk itself)."""

    if not (upper_bound_mw > lower_bound_mw):
        raise TargetCapacityConfigError(
            "upper_bound_mw must be greater than lower_bound_mw"
        )
    if capacity_tolerance_mw <= 0.0:
        raise TargetCapacityConfigError("capacity_tolerance_mw must be > 0")
    if max_iterations < 1:
        raise TargetCapacityConfigError("max_iterations must be >= 1")
    if target_value <= 0.0:
        raise TargetCapacityConfigError("target_value must be > 0")

    evaluations: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    def evaluate(x: float) -> float:
        risk = float(risk_provider(x))
        evaluations.append({"capacity_mw": x, "risk": risk})
        return risk

    def build_monotonicity_ledger() -> list[dict[str, Any]]:
        """按容量序（而非评估序）判定违例：相邻采样点上风险抬升即违例。"""
        ledger: list[dict[str, Any]] = []
        ordered = sorted(evaluations, key=lambda e: e["capacity_mw"])
        for prev, cur in zip(ordered, ordered[1:]):
            ledger.append(
                {
                    "from_capacity_mw": prev["capacity_mw"],
                    "to_capacity_mw": cur["capacity_mw"],
                    "from_risk": prev["risk"],
                    "to_risk": cur["risk"],
                    "violation": cur["risk"] > prev["risk"] + 1e-12,
                }
            )
        return ledger

    lo, hi = float(lower_bound_mw), float(upper_bound_mw)
    risk_lo = evaluate(lo)
    risk_hi = evaluate(hi)

    if risk_lo <= target_value:
        # 现状（下界处）已满足目标：诚实给 0 追加＋诊断，不跑搜索
        diagnostics.append(
            {"code": "TARGET_ALREADY_MET", "severity": "info",
             "message": "下界处风险已满足目标，无需追加容量"}
        )
        return TargetCapacitySearchResult(
            estimate_mw=lo,
            bracket_low_mw=lo,
            bracket_high_mw=lo,
            bracket_low_risk=risk_lo,
            bracket_high_risk=risk_lo,
            result_status=ResultStatus.VALID,
            # 已达标是确定解（下界处即满足），收敛态如实 CONVERGED；
            # TARGET_ALREADY_MET 诊断区分「无需搜索」与「搜索收敛」
            search_convergence=SearchConvergence.CONVERGED,
            iterations=0,
            monotonicity_ledger=build_monotonicity_ledger(),
            diagnostics=diagnostics,
            evaluations=evaluations,
        )

    if risk_hi > target_value:
        diagnostics.append(
            {"code": "TARGET_NOT_REACHED_IN_BOUNDS", "severity": "warning",
             "message": "搜索上界处风险仍高于目标：区间内不可达（请扩大上界）"}
        )
        return TargetCapacitySearchResult(
            estimate_mw=None,
            bracket_low_mw=lo,
            bracket_high_mw=hi,
            bracket_low_risk=risk_lo,
            bracket_high_risk=risk_hi,
            result_status=ResultStatus.NOT_CONVERGED,
            search_convergence=SearchConvergence.BRACKET_ONLY,
            iterations=0,
            monotonicity_ledger=build_monotonicity_ledger(),
            diagnostics=diagnostics,
            evaluations=evaluations,
        )

    if risk_lo < risk_hi:
        # 总体反向：完美容量只会填补缺口，风险应随容量下降；反向说明
        # 证据自相矛盾——不给点值
        diagnostics.append(
            {"code": "MONOTONICITY_VIOLATED", "severity": "warning",
             "message": "下界风险低于上界风险，与单调递减假设矛盾，拒绝给点值"}
        )
        return TargetCapacitySearchResult(
            estimate_mw=None,
            bracket_low_mw=lo,
            bracket_high_mw=hi,
            bracket_low_risk=risk_lo,
            bracket_high_risk=risk_hi,
            result_status=ResultStatus.NOT_CONVERGED,
            search_convergence=SearchConvergence.PLATEAU_UNRESOLVED,
            iterations=0,
            monotonicity_ledger=build_monotonicity_ledger(),
            diagnostics=diagnostics,
            evaluations=evaluations,
        )

    iterations = 0
    while (hi - lo) > capacity_tolerance_mw and iterations < max_iterations:
        mid = lo + (hi - lo) / 2.0
        risk_mid = evaluate(mid)
        iterations += 1
        if risk_mid > target_value:
            lo, risk_lo = mid, risk_mid
        else:
            hi, risk_hi = mid, risk_mid

    converged = (hi - lo) <= capacity_tolerance_mw
    if not converged:
        diagnostics.append(
            {"code": "MAX_ITERATIONS_EXHAUSTED", "severity": "warning",
             "message": f"迭代耗尽（{max_iterations} 次），区间仍未夹到容差内"}
        )
    violations = [
        entry for entry in build_monotonicity_ledger() if entry["violation"]
    ]
    if violations:
        diagnostics.append(
            {"code": "MONOTONICITY_VIOLATIONS_RECORDED", "severity": "info",
             "message": f"记录到 {len(violations)} 处单调性违例（台账在案，不静默）"}
        )

    return TargetCapacitySearchResult(
        # 保守语义：点值取区间上端（该点风险已验证 ≤ 目标）
        estimate_mw=hi if converged else None,
        bracket_low_mw=lo,
        bracket_high_mw=hi,
        bracket_low_risk=risk_lo,
        bracket_high_risk=risk_hi,
        result_status=ResultStatus.VALID if converged else ResultStatus.NOT_CONVERGED,
        search_convergence=(
            SearchConvergence.CONVERGED if converged else SearchConvergence.BRACKET_ONLY
        ),
        iterations=iterations,
        monotonicity_ledger=build_monotonicity_ledger(),
        diagnostics=diagnostics,
        evaluations=evaluations,
    )


@dataclass
class TargetLoadScaleSearchResult:
    """Q-D4（2026-10-10 裁决）：调整负荷口径的诚实二分结果（λ 轴）。

    estimate_scale＝满足目标的最大 λ（保守取区间下端——该点风险已验证
    ≤ 目标）；换算负荷水平 MW 由调用方用峰荷完成（本模块不掺单位）。
    """

    estimate_scale: float | None
    bracket_low_scale: float
    bracket_high_scale: float
    bracket_low_risk: float | None
    bracket_high_risk: float | None
    result_status: ResultStatus
    search_convergence: SearchConvergence
    iterations: int
    monotonicity_ledger: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)


def search_target_load_scale(
    *,
    lower_bound_scale: float,
    upper_bound_scale: float,
    scale_tolerance: float,
    max_iterations: int,
    target_value: float,
    risk_provider: Callable[[float], float],
) -> TargetLoadScaleSearchResult:
    """Q-D4：Bisect the monotone-nondecreasing risk(λ) against an
    absolute target; return the LARGEST verified-satisfying λ.

    ``risk_provider(λ)`` returns the engine's aggregate primary-risk
    metric with the native load curve uniformly scaled by λ (supply
    frozen). Mirrors search_target_capacity's honest-terminal discipline
    with the axis inverted: the conservative point is the bracket LOW
    end (risk verified ≤ target there)."""

    if not (upper_bound_scale > lower_bound_scale):
        raise TargetCapacityConfigError(
            "upper_bound_scale must be greater than lower_bound_scale"
        )
    if lower_bound_scale <= 0.0:
        raise TargetCapacityConfigError("lower_bound_scale must be > 0")
    if scale_tolerance <= 0.0:
        raise TargetCapacityConfigError("scale_tolerance must be > 0")
    if max_iterations < 1:
        raise TargetCapacityConfigError("max_iterations must be >= 1")
    if target_value <= 0.0:
        raise TargetCapacityConfigError("target_value must be > 0")

    evaluations: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    def evaluate(scale: float) -> float:
        risk = float(risk_provider(scale))
        evaluations.append({"load_scale": scale, "risk": risk})
        return risk

    def build_monotonicity_ledger() -> list[dict[str, Any]]:
        """按 λ 序（而非评估序）判定违例：相邻采样点上风险下降即违例。"""
        ledger: list[dict[str, Any]] = []
        ordered = sorted(evaluations, key=lambda e: e["load_scale"])
        for prev, cur in zip(ordered, ordered[1:]):
            ledger.append(
                {
                    "from_load_scale": prev["load_scale"],
                    "to_load_scale": cur["load_scale"],
                    "from_risk": prev["risk"],
                    "to_risk": cur["risk"],
                    "violation": cur["risk"] < prev["risk"] - 1e-12,
                }
            )
        return ledger

    lo, hi = float(lower_bound_scale), float(upper_bound_scale)
    risk_lo = evaluate(lo)
    risk_hi = evaluate(hi)

    if risk_lo > target_value:
        # 最小负荷水平都已超目标：负荷侧界内不可达（应降低下界或目标）
        diagnostics.append(
            {"code": "LOAD_LOWER_BOUND_EXCEEDED", "severity": "warning",
             "message": "下界（最小负荷缩放）处风险已超目标：区间内不可达"
                        "（请降低下界或放宽目标）"}
        )
        return TargetLoadScaleSearchResult(
            estimate_scale=None,
            bracket_low_scale=lo,
            bracket_high_scale=hi,
            bracket_low_risk=risk_lo,
            bracket_high_risk=risk_hi,
            result_status=ResultStatus.NOT_CONVERGED,
            search_convergence=SearchConvergence.BRACKET_ONLY,
            iterations=0,
            monotonicity_ledger=build_monotonicity_ledger(),
            diagnostics=diagnostics,
            evaluations=evaluations,
        )

    if risk_hi <= target_value:
        # 整个区间都满足：系统能可靠供给的负荷至少到上界（可扩界重试）
        diagnostics.append(
            {"code": "LOAD_UPPER_BOUND_SATISFIED", "severity": "info",
             "message": "上界（最大负荷缩放）处风险仍满足目标：目标负荷"
                        "水平至少为上界，可扩大上界重试"}
        )
        return TargetLoadScaleSearchResult(
            estimate_scale=hi,
            bracket_low_scale=lo,
            bracket_high_scale=hi,
            bracket_low_risk=risk_lo,
            bracket_high_risk=risk_hi,
            result_status=ResultStatus.VALID,
            search_convergence=SearchConvergence.CONVERGED,
            iterations=0,
            monotonicity_ledger=build_monotonicity_ledger(),
            diagnostics=diagnostics,
            evaluations=evaluations,
        )

    if risk_lo > risk_hi:
        # 防御分支：负荷只会增加需求，风险应随 λ 不降；反向说明证据
        # 自相矛盾——不给点值（与容量轴同门纪律）
        diagnostics.append(
            {"code": "MONOTONICITY_VIOLATED", "severity": "warning",
             "message": "下界风险高于上界风险，与单调不降假设矛盾，拒绝给点值"}
        )
        return TargetLoadScaleSearchResult(
            estimate_scale=None,
            bracket_low_scale=lo,
            bracket_high_scale=hi,
            bracket_low_risk=risk_lo,
            bracket_high_risk=risk_hi,
            result_status=ResultStatus.NOT_CONVERGED,
            search_convergence=SearchConvergence.PLATEAU_UNRESOLVED,
            iterations=0,
            monotonicity_ledger=build_monotonicity_ledger(),
            diagnostics=diagnostics,
            evaluations=evaluations,
        )

    iterations = 0
    while (hi - lo) > scale_tolerance and iterations < max_iterations:
        mid = lo + (hi - lo) / 2.0
        risk_mid = evaluate(mid)
        iterations += 1
        if risk_mid > target_value:
            hi, risk_hi = mid, risk_mid
        else:
            lo, risk_lo = mid, risk_mid

    converged = (hi - lo) <= scale_tolerance
    if not converged:
        diagnostics.append(
            {"code": "MAX_ITERATIONS_EXHAUSTED", "severity": "warning",
             "message": f"迭代耗尽（{max_iterations} 次），区间仍未夹到容差内"}
        )
    violations = [
        entry for entry in build_monotonicity_ledger() if entry["violation"]
    ]
    if violations:
        diagnostics.append(
            {"code": "MONOTONICITY_VIOLATIONS_RECORDED", "severity": "info",
             "message": f"记录到 {len(violations)} 处单调性违例（台账在案，不静默）"}
        )

    return TargetLoadScaleSearchResult(
        # 保守语义：点值取区间下端（该点风险已验证 ≤ 目标）
        estimate_scale=lo if converged else None,
        bracket_low_scale=lo,
        bracket_high_scale=hi,
        bracket_low_risk=risk_lo,
        bracket_high_risk=risk_hi,
        result_status=ResultStatus.VALID if converged else ResultStatus.NOT_CONVERGED,
        search_convergence=(
            SearchConvergence.CONVERGED if converged else SearchConvergence.BRACKET_ONLY
        ),
        iterations=iterations,
        monotonicity_ledger=build_monotonicity_ledger(),
        diagnostics=diagnostics,
        evaluations=evaluations,
    )


__all__ = [
    "TargetCapacityConfigError",
    "TargetCapacitySearchResult",
    "TargetLoadScaleSearchResult",
    "search_target_capacity",
    "search_target_load_scale",
]
