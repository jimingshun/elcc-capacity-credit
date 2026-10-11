"""B1 calibration decisions - the CONFIRMED criterion semantics (CAL-01..CAL-09).

Baseline calibration answers the inverse question: how far must the load
be scaled so the system's risk lands on the criterion? The engine has no
load-side search, so calibration is orchestrated as a bisection over
DERIVED load-scaled data versions (B1), each round being one ordinary
adequacy run.

This module is the PURE decision core - no IO, no kernel call, no run
submission. It encodes exactly what the user confirmed:

- CAL-01 the criterion metric is one of LOLE / LOLH / EUE / NEUE (the
  EFC winter-evening-peak criterion is NOT in this round);
- CAL-02 the target value has NO default: a missing/blank target is a
  refusal, never 0.1 day/yr silently adopted from a prototype example;
- CAL-03 tolerance band ±5% by default, configurable;
- CAL-04 bisection over the initial interval [-5%, +10%];
- CAL-05 at most 8 rounds, then NOT converged;
- CAL-07 the engine's own convergence verdict is never relaxed: a round
  whose run did not converge is marked unusable and does NOT move the
  interval (it is not evidence);
- CAL-08 a non-converged calibration yields the final interval, the
  per-round trace and the diagnosis - never a point value;
- CAL-09 the output is an explanatory "baseline load level (+x%)" plus
  the gap picture; the baseline data is never rewritten here.

Axis extension (branch plan 2026-09-19, PC-01, contract §1): the same
band semantics serve a SECOND calibration axis. The load axis scales
load (risk rises with the probe); the perfect_capacity axis appends
synthetic zero-FOR capacity (risk FALLS with the probe), so the
bisection shrinks the interval in the REVERSED direction. Decision
outputs carry the axis-generic fields probe_value + unit_label; the
load axis keeps scale_percent as its compatibility alias. The capacity
axis has NO default initial interval (CAL-02 spirit): the batch must
state its own MW window.
"""

from __future__ import annotations

import math
from typing import Any

CRITERION_METRICS = {
    "lole_days_per_year": "LOLE 期望缺电天数（天/年）",
    "lolh_hours_per_year": "LOLH 期望缺电小时数（小时/年）",
    "eue_mwh_per_year": "EUE 期望缺供电量（MWh/年）",
    "neue_ppm": "NEUE 缺供电量占比（ppm）",
}

DEFAULT_TOLERANCE_PERCENT = 5.0
INITIAL_INTERVAL_PERCENT = (-5.0, 10.0)
MAX_ROUNDS = 8

AXIS_LOAD_SCALE = "load_scale"
AXIS_PERFECT_CAPACITY = "perfect_capacity"
AXIS_UNIT_LABELS = {AXIS_LOAD_SCALE: "%", AXIS_PERFECT_CAPACITY: "MW"}
# the smallest interval width that still admits a meaningful probe:
# 0.05 percentage points of load scale / 1 MW of perfect capacity
MIN_STEP_PERCENT = 0.05
DEFAULT_MIN_STEP_MW = 1.0
# SL-01: the heating + non-heating targets must sum to the annual
# target within this absolute tolerance (day/year) - the days
# additivity makes the pair and the anchor one consistent criterion
SEASONAL_ANNUAL_TOLERANCE = 0.005

ROUND_USABLE = "usable"
ROUND_UNUSABLE = "unusable"
VERDICT_WITHIN = "within_band"
VERDICT_TOO_LOOSE = "too_loose"  # risk below target -> load can rise
VERDICT_TOO_TIGHT = "too_tight"  # risk above target -> load must fall

DECISION_PROBE = "probe"
DECISION_CONVERGED = "converged"
DECISION_NOT_CONVERGED = "not_converged"
DECISION_EXHAUSTED = "interval_exhausted"


class CalibrationError(ValueError):
    """A calibration refusal (display kind + reason)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def validate_criterion(
    metric: Any, target: Any, tolerance_percent: Any = DEFAULT_TOLERANCE_PERCENT
) -> dict[str, Any]:
    """The confirmed criterion (CAL-01/02/03). A missing target is a hard
    refusal: the tool has NO default criterion value."""
    key = str(metric or "").strip()
    if key not in CRITERION_METRICS:
        raise CalibrationError(
            "invalid_input",
            f"判据指标必须为 {'/'.join(CRITERION_METRICS)} 之一，收到 {metric!r}"
            "（EFC 冬季晚峰口径本轮不含）",
        )
    if target is None or (isinstance(target, str) and not target.strip()):
        raise CalibrationError(
            "invalid_input",
            "标定目标值必须由您显式给定：本工具没有默认判据"
            "（原型中的 0.1 天/年 是示例，不作默认）",
        )
    try:
        value = float(target)
    except (TypeError, ValueError):
        raise CalibrationError(
            "invalid_input", f"标定目标值必须为数值，收到 {target!r}"
        ) from None
    if value <= 0:
        raise CalibrationError(
            "invalid_input", "标定目标值必须大于0（风险为0不是可标定判据）"
        )
    try:
        tolerance = float(tolerance_percent)
    except (TypeError, ValueError):
        raise CalibrationError(
            "invalid_input", f"容差带必须为数值（%），收到 {tolerance_percent!r}"
        ) from None
    if tolerance <= 0 or tolerance >= 50:
        raise CalibrationError(
            "invalid_input", "容差带必须在 0—50% 之间（默认 ±5%）"
        )
    return {
        "metric": key,
        "metric_label": CRITERION_METRICS[key],
        "target": value,
        "tolerance_percent": tolerance,
        "band": [value * (1 - tolerance / 100.0), value * (1 + tolerance / 100.0)],
    }


def _axis_vocabulary(axis: Any) -> dict[str, Any]:
    """Validate ONLY the axis identity (key + unit label + direction) -
    classification never needs the interval, so the no-default rule for
    the capacity window is enforced exclusively in next_decision."""
    if axis is None or (isinstance(axis, str) and axis.strip() in ("", AXIS_LOAD_SCALE)):
        return {
            "key": AXIS_LOAD_SCALE,
            "unit_label": AXIS_UNIT_LABELS[AXIS_LOAD_SCALE],
            "increases_risk": True,
        }
    if isinstance(axis, str):
        key = axis.strip()
    elif isinstance(axis, dict):
        key = str(axis.get("key") or "").strip()
    else:
        key = ""
    if key != AXIS_PERFECT_CAPACITY:
        raise CalibrationError(
            "invalid_input",
            f"未知标定轴 {axis!r}（可用：{AXIS_LOAD_SCALE}/{AXIS_PERFECT_CAPACITY}）",
        )
    return {
        "key": AXIS_PERFECT_CAPACITY,
        "unit_label": AXIS_UNIT_LABELS[AXIS_PERFECT_CAPACITY],
        "increases_risk": False,
    }


def _resolve_axis(axis: Any, interval: Any) -> dict[str, Any]:
    """Normalize the axis parameter into {key, unit_label,
    increases_risk, min_step, interval}.

    The load axis keeps its confirmed default window [-5%, +10%]; the
    perfect-capacity axis refuses to invent one (explicit
    initial_interval_mw required - CAL-02's no-default rule applied to
    the probe axis itself).
    """
    identity = _axis_vocabulary(axis)
    if identity["key"] == AXIS_LOAD_SCALE:
        if interval is None:
            low, high = INITIAL_INTERVAL_PERCENT
        else:
            low, high = float(interval[0]), float(interval[1])
        return {
            **identity,
            "min_step": MIN_STEP_PERCENT,
            "interval": (low, high),
        }
    config = axis if isinstance(axis, dict) else {}
    if interval is None:
        interval = config.get("initial_interval_mw")
    if interval is None:
        raise CalibrationError(
            "invalid_input",
            "完美容量轴必须显式给出初始区间（MW，[下界, 上界]）："
            "工具不为容量轴发明默认档位（CAL-02 同源）",
        )
    try:
        low, high = float(interval[0]), float(interval[1])
    except (TypeError, ValueError, IndexError):
        raise CalibrationError(
            "invalid_input",
            f"完美容量初始区间必须为 [下界, 上界] 数值对，收到 {interval!r}",
        ) from None
    if (
        not math.isfinite(low)
        or not math.isfinite(high)
        or low < 0
        or low >= high
    ):
        raise CalibrationError(
            "invalid_input",
            "完美容量初始区间必须满足 0 ≤ 下界 < 上界（MW），收到 "
            f"[{low}, {high}]",
        )
    min_step = config.get("min_step_mw", DEFAULT_MIN_STEP_MW)
    try:
        min_step = float(min_step)
    except (TypeError, ValueError):
        raise CalibrationError(
            "invalid_input", f"min_step_mw 必须为数值（MW），收到 {min_step!r}"
        ) from None
    if not math.isfinite(min_step) or min_step <= 0:
        raise CalibrationError(
            "invalid_input", "min_step_mw 必须为大于0的有限MW数"
        )
    return {**identity, "min_step": min_step, "interval": (low, high)}


def _probe_key(axis_cfg: dict[str, Any]) -> str:
    return (
        "scale_percent"
        if axis_cfg["key"] == AXIS_LOAD_SCALE
        else "capacity_mw"
    )


def _interval_key(axis_cfg: dict[str, Any]) -> str:
    return (
        "interval_percent"
        if axis_cfg["key"] == AXIS_LOAD_SCALE
        else "interval_mw"
    )


def classify_round(
    criterion: dict[str, Any],
    scale_percent: float,
    metric_value: Any,
    *,
    axis: Any = None,
    run_converged: bool = True,
    diagnosis: str | None = None,
) -> dict[str, Any]:
    """One round's verdict (CAL-07): an unconverged run is UNUSABLE and
    carries no interval information - it is never treated as evidence and
    never relaxed into a number.

    ``scale_percent`` is the probe value positionally; on the
    perfect-capacity axis it is reported under ``capacity_mw`` and both
    axes carry the generic ``probe_value``/``unit_label`` pair (contract
    §1)."""
    axis_cfg = _axis_vocabulary(axis)
    probe_key = _probe_key(axis_cfg)
    probe_value = float(scale_percent)
    if not run_converged:
        return {
            probe_key: probe_value,
            "probe_value": probe_value,
            "unit_label": axis_cfg["unit_label"],
            "metric_value": None,
            "verdict": None,
            "state": ROUND_UNUSABLE,
            "diagnosis": diagnosis or "该轮运行未收敛：按 CAL-07 标记不可用，不移动区间",
        }
    if metric_value is None:
        return {
            probe_key: probe_value,
            "probe_value": probe_value,
            "unit_label": axis_cfg["unit_label"],
            "metric_value": None,
            "verdict": None,
            "state": ROUND_UNUSABLE,
            "diagnosis": diagnosis or "该轮无风险指标值：标记不可用，不移动区间",
        }
    try:
        value = float(metric_value)
    except (TypeError, ValueError):
        raise CalibrationError(
            "invalid_input", f"风险指标值必须为数值，收到 {metric_value!r}"
        ) from None
    low, high = criterion["band"]
    if value < low:
        verdict = VERDICT_TOO_LOOSE
    elif value > high:
        verdict = VERDICT_TOO_TIGHT
    else:
        verdict = VERDICT_WITHIN
    return {
        probe_key: probe_value,
        "probe_value": probe_value,
        "unit_label": axis_cfg["unit_label"],
        "metric_value": value,
        "verdict": verdict,
        "state": ROUND_USABLE,
        "diagnosis": diagnosis,
    }


def _round_probe(round_: dict[str, Any]) -> float:
    """The probe value of one classified round (probe_value is always
    present from classify_round; scale_percent is the legacy alias)."""
    value = round_.get("probe_value")
    if value is None:
        value = round_["scale_percent"]
    return float(value)


def next_decision(
    criterion: dict[str, Any],
    history: list[dict[str, Any]],
    interval: tuple[float, float] | None = None,
    max_rounds: int = MAX_ROUNDS,
    *,
    axis: Any = None,
) -> dict[str, Any]:
    """The next action from the usable rounds so far (CAL-04/05/08).

    ``axis`` selects the calibration axis (None = the load axis, the
    confirmed default). Returns one of:

    - probe: the next probe value to run (bisection midpoint); the
      axis-generic fields probe_value + unit_label ride on every kind,
      with scale_percent/capacity_mw as the axis-native alias;
    - converged: a usable round landed inside the band (its probe value
      is the calibrated level, reported per CAL-09 as an explanatory
      intermediate that never rewrites the baseline);
    - not_converged: the round budget is spent - interval + trace +
      diagnosis only, NEVER a point value;
    - interval_exhausted: the interval collapsed below a meaningful step.
    """
    axis_cfg = _resolve_axis(axis, interval)
    probe_key = _probe_key(axis_cfg)
    interval_key = _interval_key(axis_cfg)
    usable = [r for r in history if r.get("state") == ROUND_USABLE]
    for round_ in usable:
        if round_.get("verdict") == VERDICT_WITHIN:
            value = _round_probe(round_)
            note = (
                "标定结果为解释性中间成果（基准负荷水平 相对预测 "
                f"{value:+.2f}%）；不回写基线数据"
                if axis_cfg["key"] == AXIS_LOAD_SCALE
                else (
                    "标定结果为解释性中间成果（建议向基线追加完美容量 "
                    f"{value:g} MW 后风险落入判据带；各轮为研究合成版本，"
                    "不产生基准替代数据，不回写基线数据）"
                )
            )
            return {
                "kind": DECISION_CONVERGED,
                probe_key: value,
                "probe_value": value,
                "unit_label": axis_cfg["unit_label"],
                "metric_value": round_.get("metric_value"),
                "rounds_used": len(history),
                "usable_rounds": len(usable),
                "criterion": criterion,
                "note": note,
            }
    if len(history) >= max_rounds:
        low, high = _shrink(axis_cfg["interval"], usable, axis_cfg=axis_cfg)
        return {
            "kind": DECISION_NOT_CONVERGED,
            interval_key: [low, high],
            "unit_label": axis_cfg["unit_label"],
            "rounds_used": len(history),
            "usable_rounds": len(usable),
            "unusable_rounds": len(history) - len(usable),
            "criterion": criterion,
            "diagnosis": (
                f"已达最大轮次 {max_rounds} 仍未落入 ±"
                f"{criterion['tolerance_percent']}% 容差带："
                "只给区间与逐轮轨迹，不给点值（CAL-08）。"
                "可放宽容差、增大单轮模拟年数或检查资源参数后重跑。"
            ),
        }
    low, high = _shrink(axis_cfg["interval"], usable, axis_cfg=axis_cfg)
    if high - low < axis_cfg["min_step"]:
        return {
            "kind": DECISION_EXHAUSTED,
            interval_key: [low, high],
            "unit_label": axis_cfg["unit_label"],
            "rounds_used": len(history),
            "usable_rounds": len(usable),
            "criterion": criterion,
            "diagnosis": (
                (
                    "区间已收缩到 0.05 个百分点以内仍未命中容差带："
                    "判据容差可能小于模拟分辨率，请放宽容差或提高样本精度。"
                )
                if axis_cfg["key"] == AXIS_LOAD_SCALE
                else (
                    f"区间已收缩到 {axis_cfg['min_step']:g} MW 以内仍未命中"
                    "容差带：初始档位不当，建议重开任务并扩大初始区间"
                    "（初始区间建议以基线试算缺口为锚）。"
                )
            ),
        }
    mid = round((low + high) / 2.0, 4)
    return {
        "kind": DECISION_PROBE,
        probe_key: mid,
        "probe_value": mid,
        "unit_label": axis_cfg["unit_label"],
        interval_key: [low, high],
        "rounds_used": len(history),
        "usable_rounds": len(usable),
        "criterion": criterion,
    }


def _shrink(
    interval: tuple[float, float],
    usable: list[dict[str, Any]],
    *,
    axis_cfg: dict[str, Any] | None = None,
) -> tuple[float, float]:
    """Bisection shrink from the USABLE rounds only (an unusable round
    carries no information and must not move the interval).

    The load axis raises risk with the probe (too loose -> the calibrated
    level is higher); the perfect-capacity axis LOWERS risk with the
    probe, so the same verdicts move the interval the other way."""
    increases_risk = True if axis_cfg is None else axis_cfg["increases_risk"]
    low, high = float(interval[0]), float(interval[1])
    for round_ in usable:
        verdict = round_.get("verdict")
        probe = _round_probe(round_)
        if verdict == VERDICT_TOO_LOOSE:
            # risk below target: on the load axis the calibrated level is
            # higher; on the capacity axis the addition was TOO BIG
            if increases_risk:
                low = max(low, probe)
            else:
                high = min(high, probe)
        elif verdict == VERDICT_TOO_TIGHT:
            if increases_risk:
                high = min(high, probe)
            else:
                low = max(low, probe)
    if low > high:
        low, high = high, low
    return low, high


def gap_picture_note(rounds: list[dict[str, Any]]) -> str:
    """CAL-09: the gap picture is an explanatory intermediate; state what
    it is and what it is not (no baseline rewrite, no policy conclusion).
    R3-B1: the picture now EXISTS as run-dir artifacts - the baseline
    adequacy run persists deficit_events.json + gap_profile.json
    (月份×时段), so the note points at real data instead of claiming one."""
    usable = sum(1 for r in rounds if r.get("state") == ROUND_USABLE)
    return (
        f"逐轮轨迹 {len(rounds)} 轮（可用 {usable} 轮）。缺口画像以基准 adequacy "
        "运行落盘的 deficit_events.json 与 gap_profile.json（月份×时段）为准，"
        "仅作解释性中间成果：不回写基线数据、不作为正式结论、不直接推导补偿标准。"
    )


def validate_seasonal_criterion(criterion: Any) -> dict[str, Any]:
    """SL-01 (method sign-off 2026-09-19): the ANNUAL criterion plus an
    explicit heating/non-heating target pair.

    The pair must satisfy the annual-consistency check - the two
    seasonal targets sum to the annual target within
    SEASONAL_ANNUAL_TOLERANCE (0.005 day/year) - because a year's
    deficit days partition into the two disjoint segments, so
    seasonal LOLE days ADD to the annual LOLE; a pair drifting off the
    anchor is a refusal, never silently recalibrated. Each season
    carries its own band from the SAME tolerance percent (CAL-03)."""
    base = validate_criterion(
        criterion.get("metric"),
        criterion.get("target"),
        criterion.get("tolerance_percent", DEFAULT_TOLERANCE_PERCENT),
    )
    seasonal = criterion.get("seasonal")
    if (
        not isinstance(seasonal, dict)
        or not all(
            isinstance(seasonal.get(k), (int, float))
            and not isinstance(seasonal.get(k), bool)
            for k in ("heating", "non_heating")
        )
    ):
        raise CalibrationError(
            "invalid_input",
            "季节判据必须显式给出 seasonal: {heating, non_heating} 两个数值目标"
            "（本工具不会把年度判据悄悄退化为单旋钮）",
        )
    heating_target = float(seasonal["heating"])
    non_target = float(seasonal["non_heating"])
    if heating_target <= 0 or non_target <= 0:
        raise CalibrationError(
            "invalid_input",
            "季节判据目标必须都大于0（CAL-02：没有默认判据，也没有 0 风险判据）",
        )
    drift = abs(heating_target + non_target - base["target"])
    if drift > SEASONAL_ANNUAL_TOLERANCE:
        raise CalibrationError(
            "invalid_input",
            f"年度一致性校验失败：供暖季 {heating_target} + 非供暖季 "
            f"{non_target} = {heating_target + non_target} 与年度目标 "
            f"{base['target']} 偏差 {drift:.4f} 天/年，超过容差 "
            f"{SEASONAL_ANNUAL_TOLERANCE}（天数口径下两季之和必须等于年度）",
        )
    tol = base["tolerance_percent"]

    def _segment(target: float) -> dict[str, Any]:
        return {
            "target": target,
            "band": [target * (1 - tol / 100.0), target * (1 + tol / 100.0)],
        }

    return {
        "metric": base["metric"],
        "metric_label": base["metric_label"],
        "target": base["target"],
        "tolerance_percent": tol,
        "band": base["band"],
        "heating": _segment(heating_target),
        "non_heating": _segment(non_target),
        "annual_consistent": True,
    }


def monthly_deficit_date_counts(
    events: list[dict[str, Any]],
) -> dict[str, dict[int, int]]:
    """SL-01 publication helper: per-(sample, weather year) MONTHLY
    deficit-date counts from a run's FULL untruncated event set.

    Written at publication time (before any evidence cap can bite) as
    the run's ``seasonal_dates.json``; the seasonal projection later
    groups the months into segments however the batch defines them.
    An event spanning a month boundary contributes one date to EACH
    month it covers; events are pre-split at year boundaries."""
    from datetime import datetime, timedelta

    covered: dict[str, dict[int, set]] = {}
    for event in events:
        try:
            sample = str(event.get("sample_id") or "")
            year = int(event.get("weather_year"))
            start = datetime.fromisoformat(str(event.get("start_time")))
            end = datetime.fromisoformat(str(event.get("end_time")))
        except (TypeError, ValueError) as exc:
            raise CalibrationError(
                "invalid_input",
                f"缺电事件缺少数值/时间字段，无法做逐月归属：{exc}",
            ) from exc
        if end < start:
            start, end = end, start
        key = f"{sample}|{year}"
        bucket = covered.setdefault(key, {})
        date = start.date()
        while date <= end.date():
            bucket.setdefault(date.month, set()).add(date)
            date += timedelta(days=1)
    return {
        key: {month: len(dates) for month, dates in sorted(by_month.items())}
        for key, by_month in covered.items()
    }


def seasonal_segment_days(
    events: list[dict[str, Any]], *, heating_months: Any
) -> dict[tuple[str, int], dict[str, int]]:
    """SL-01 wiring: per-(sample, weather year) segment deficit-DAY
    counts from a run's persisted event dicts.

    Each event covers EVERY calendar date from its start to its end (a
    multi-day event contributes all its covered dates to the segments
    those dates fall in). Persisted events are pre-split at weather-
    YEAR boundaries by the engine, so a pair never mixes two years.
    The month set must be a non-empty proper subset of 1..12 without
    duplicates - the same rule as the derivation rule's parameter."""
    from datetime import datetime, timedelta

    try:
        month_values = [int(m) for m in heating_months]
    except (TypeError, ValueError):
        raise CalibrationError(
            "invalid_input",
            f"heating_months 必须为 1–12 的月份列表，收到 {heating_months!r}",
        ) from None
    months = frozenset(month_values)
    if (
        not months
        or len(month_values) != len(months)
        or not months <= frozenset(range(1, 13))
        or months == frozenset(range(1, 13))
    ):
        raise CalibrationError(
            "invalid_input",
            "heating_months 月份集合必须为 1–12 的非空真子集且不重复"
            f"（不能是全部 12 个月），收到 {heating_months!r}",
        )
    covered: dict[tuple[str, int], dict[str, set]] = {}
    for event in events:
        try:
            sample = str(event.get("sample_id") or "")
            year = int(event.get("weather_year"))
            start = datetime.fromisoformat(str(event.get("start_time")))
            end = datetime.fromisoformat(str(event.get("end_time")))
        except (TypeError, ValueError) as exc:
            raise CalibrationError(
                "invalid_input",
                f"缺电事件缺少数值/时间字段，无法做季节归属：{exc}",
            ) from exc
        if end < start:
            start, end = end, start
        key = (sample, year)
        bucket = covered.setdefault(
            key, {"heating": set(), "non_heating": set()}
        )
        date = start.date()
        while date <= end.date():
            segment = "heating" if date.month in months else "non_heating"
            bucket[segment].add(date)
            date += timedelta(days=1)
    return {
        key: {
            "heating": len(segs["heating"]),
            "non_heating": len(segs["non_heating"]),
        }
        for key, segs in covered.items()
    }


def seasonal_aggregate_segment_lole(
    counts: dict[tuple[str, int], dict[str, int]],
    weights: dict[int, float],
    *,
    samples_by_year: dict[int, int],
) -> dict[str, float]:
    """T06 semantics applied to the segment counts: per-year SAMPLE
    MEAN first, then the year weights - the same aggregation the
    annual LOLE uses, so the two segments stay consistent with it.

    ``samples_by_year`` is the run's per-year SAMPLE count: samples
    without any deficit event contribute ZERO days and MUST be in the
    denominator (a mean over only-the-events samples would inflate the
    segment LOLE). A weight year whose sample count is missing or zero
    is a refusal (a declared year the run never simulated must not
    silently vanish)."""
    per_year: dict[int, list[dict[str, int]]] = {}
    for (sample, year), segments in counts.items():
        per_year.setdefault(year, []).append(segments)
    out = {"heating": 0.0, "non_heating": 0.0}
    for year, weight in weights.items():
        sample_count = int(samples_by_year.get(year) or 0)
        if sample_count <= 0:
            raise CalibrationError(
                "invalid_input",
                f"权重年 {year} 没有样本（sample_count 缺失或为 0）："
                "该年未被本次运行模拟，拒绝静默消失",
            )
        samples = per_year.get(year, [])
        for segment in ("heating", "non_heating"):
            total = sum(float(s[segment]) for s in samples)
            mean = total / sample_count
            out[segment] += weight * mean
    return out


SEASONAL_HEATING_KEY = "heating"
SEASONAL_NON_HEATING_KEY = "non_heating"


def _seasonal_shrink(
    interval: tuple[float, float], rounds: list[dict[str, Any]]
) -> tuple[float, float]:
    """Load-axis bisection shrink for ONE season, with the WITHIN-band
    rounds PINNING the knob: a landed season's interval collapses to
    [x, x] so the next probe keeps that knob at its achieved value
    while the other season keeps bisecting. Unusable rounds (CAL-07)
    carry no information and never move the interval."""
    low, high = float(interval[0]), float(interval[1])
    for round_ in rounds:
        verdict = round_.get("verdict")
        probe = _round_probe(round_)
        if verdict == VERDICT_WITHIN:
            low = max(low, probe)
            high = min(high, probe)
        elif verdict == VERDICT_TOO_LOOSE:
            low = max(low, probe)
        elif verdict == VERDICT_TOO_TIGHT:
            high = min(high, probe)
    if low > high:
        low, high = high, low
    return low, high


def seasonal_next_decision(
    criterion: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    heating_interval: tuple[float, float],
    non_heating_interval: tuple[float, float],
    max_rounds: int = MAX_ROUNDS,
) -> dict[str, Any]:
    """The dual-knob seasonal bisection (SL-01; CAL-04/05/07/08
    semantics per season).

    ``history`` carries one entry per run: ``{"heating": round,
    "non_heating": round}`` with rounds from ``classify_round`` (load
    axis - adding load raises risk). ``*_interval`` are the two knobs'
    current percent windows and have NO default (CAL-02 spirit: the
    batch states its own seasonal windows).

    Terminal semantics:
    - converged: ONE round landed BOTH segments inside their bands -
      the pair (a, b) is the calibrated answer, reported as an
      explanatory intermediate (CAL-09) that never rewrites data;
    - not_converged: the round budget is spent - BOTH final intervals
      plus the trace, never a point value;
    - interval_exhausted: a season's window collapsed below the step
      WITHOUT landing - the diagnosis names that season.
    A season that landed inside its band pins its knob at the achieved
    value (interval [x, x]) while the other keeps bisecting."""

    def _season_rounds(key: str) -> list[dict[str, Any]]:
        return [
            r[key]
            for r in history
            if isinstance(r, dict) and isinstance(r.get(key), dict)
        ]

    def _landed(key: str) -> float | None:
        for round_ in _season_rounds(key):
            if round_.get("verdict") == VERDICT_WITHIN:
                return _round_probe(round_)
        return None

    # 1. convergence needs BOTH segments inside their bands in the
    # SAME round (the pair is calibrated together, one run per round)
    for round_ in history:
        heating_round = round_.get("heating") or {}
        non_round = round_.get("non_heating") or {}
        if (
            heating_round.get("verdict") == VERDICT_WITHIN
            and non_round.get("verdict") == VERDICT_WITHIN
        ):
            a = _round_probe(heating_round)
            b = _round_probe(non_round)
            return {
                "kind": DECISION_CONVERGED,
                "heating": {
                    "scale_percent": a,
                    "probe_value": a,
                    "unit_label": "%",
                },
                "non_heating": {
                    "scale_percent": b,
                    "probe_value": b,
                    "unit_label": "%",
                },
                "rounds_used": len(history),
                "usable_rounds": sum(
                    1
                    for r in history
                    if (r.get("heating") or {}).get("state") == ROUND_USABLE
                ),
                "criterion": criterion,
                "note": (
                    "季节标定结果为解释性中间成果（供暖季 "
                    f"{a:+.2f}%、非供暖季 {b:+.2f}% 时两季风险均落入各自"
                    "判据带）；不回写基线数据"
                ),
            }

    heating_rounds = _season_rounds(SEASONAL_HEATING_KEY)
    non_rounds = _season_rounds(SEASONAL_NON_HEATING_KEY)
    heating_low, heating_high = _seasonal_shrink(
        heating_interval, heating_rounds
    )
    non_low, non_high = _seasonal_shrink(non_heating_interval, non_rounds)
    heating_landed = _landed(SEASONAL_HEATING_KEY)
    non_landed = _landed(SEASONAL_NON_HEATING_KEY)

    def _segment_view(
        low: float, high: float, landed: float | None
    ) -> dict[str, Any]:
        return {
            "scale_percent": None if landed is None else round(landed, 4),
            "probe_value": None if landed is None else round(landed, 4),
            "unit_label": "%",
            "interval_percent": [round(low, 4), round(high, 4)],
            "landed": landed is not None,
        }

    heating_view = _segment_view(heating_low, heating_high, heating_landed)
    non_view = _segment_view(non_low, non_high, non_landed)

    if len(history) >= max_rounds:
        return {
            "kind": DECISION_NOT_CONVERGED,
            "heating": heating_view,
            "non_heating": non_view,
            "rounds_used": len(history),
            "criterion": criterion,
            "diagnosis": (
                f"已达最大轮次 {max_rounds} 仍未出现两季同轮落入各自容差带"
                "的一轮：只给两季区间与逐轮轨迹，不给点值（CAL-08）。"
                "可放宽容差、增大单轮模拟年数或调整季节判据拆解后重跑。"
            ),
        }
    collapsed = []
    if heating_landed is None and heating_high - heating_low < MIN_STEP_PERCENT:
        collapsed.append("供暖季")
    if non_landed is None and non_high - non_low < MIN_STEP_PERCENT:
        collapsed.append("非供暖季")
    if collapsed:
        return {
            "kind": DECISION_EXHAUSTED,
            "heating": heating_view,
            "non_heating": non_view,
            "rounds_used": len(history),
            "criterion": criterion,
            "diagnosis": (
                f"{'、'.join(collapsed)}的区间已收缩到 "
                f"{MIN_STEP_PERCENT} 个百分点以内仍未命中该季容差带："
                "初始档位不当，建议重开任务并扩大该季初始区间。"
            ),
        }
    return {
        "kind": DECISION_PROBE,
        "heating": {
            "scale_percent": round((heating_low + heating_high) / 2.0, 4),
            "probe_value": round((heating_low + heating_high) / 2.0, 4),
            "unit_label": "%",
            "interval_percent": [
                round(heating_low, 4),
                round(heating_high, 4),
            ],
            "landed": heating_landed is not None,
        },
        "non_heating": {
            "scale_percent": round((non_low + non_high) / 2.0, 4),
            "probe_value": round((non_low + non_high) / 2.0, 4),
            "unit_label": "%",
            "interval_percent": [round(non_low, 4), round(non_high, 4)],
            "landed": non_landed is not None,
        },
        "rounds_used": len(history),
        "usable_rounds": sum(
            1
            for r in history
            if (r.get("heating") or {}).get("state") == ROUND_USABLE
        ),
        "criterion": criterion,
    }


def validate_calibrated_baseline_ref(
    ref: dict[str, Any],
    *,
    manifest: dict[str, Any],
    data_version_hash: str,
) -> dict[str, Any]:
    """MR-02 (A2 方案一): check a provided calibrated_baseline_ref
    against the calibration batch manifest and the run's frozen data
    version, BEFORE any run side effect.

    A valid reference names ONE usable round whose scale percent sits
    INSIDE the criterion band (a converged calibration). Any mismatch is
    a stable ``calibration_ref_mismatch`` refusal - the baseline
    closure is never silently accepted."""
    if not isinstance(ref, dict):
        raise CalibrationError(
            "calibration_ref_mismatch",
            f"calibrated_baseline_ref 必须为字典，收到 {ref!r}",
        )
    batch_id = str(ref.get("calibration_batch_id") or "")
    if not batch_id or batch_id != str(manifest.get("batch_ref") or ""):
        raise CalibrationError(
            "calibration_ref_mismatch",
            f"calibrated_baseline_ref.batch_id {batch_id!r} 与批次清单"
            f" {manifest.get('batch_ref')!r} 不一致",
        )
    criterion = manifest.get("criterion") or {}
    for ref_key, manifest_key in (
        ("criterion_metric", "metric"),
        ("criterion_target_value", "target"),
        ("tolerance_percent", "tolerance_percent"),
    ):
        ref_value = ref.get(ref_key)
        manifest_value = criterion.get(manifest_key)
        if isinstance(ref_value, str) or isinstance(manifest_value, str):
            same = str(ref_value) == str(manifest_value)
        else:
            try:
                same = float(ref_value) == float(manifest_value)
            except (TypeError, ValueError):
                same = False
        if not same:
            raise CalibrationError(
                "calibration_ref_mismatch",
                f"calibrated_baseline_ref.{ref_key}={ref_value!r} 与批次判据"
                f" {manifest_key}={manifest_value!r} 不一致",
            )
    if str(ref.get("data_version_hash") or "") != str(data_version_hash or ""):
        raise CalibrationError(
            "calibration_ref_mismatch",
            "calibrated_baseline_ref.data_version_hash 与本运行冻结数据版本"
            f" {data_version_hash!r} 不一致：标定闭环只对同一冻结世界有效",
        )
    scale = ref.get("load_scale_percent")
    try:
        scale_value = float(scale)
    except (TypeError, ValueError):
        raise CalibrationError(
            "calibration_ref_mismatch",
            f"load_scale_percent 必须为数值，收到 {scale!r}",
        ) from None
    target = float(criterion.get("target"))
    tolerance = float(criterion.get("tolerance_percent"))
    low, high = target * (1 - tolerance / 100.0), target * (1 + tolerance / 100.0)
    for point in manifest.get("points") or []:
        if point.get("status") != "完成":
            continue
        try:
            point_scale = float((point.get("params") or {}).get("scale_percent"))
            point_value = float(point.get("metric_value"))
        except (TypeError, ValueError):
            continue
        if point_scale == scale_value and low <= point_value <= high:
            return {"converged": True, "point_id": str(point.get("point_id"))}
    raise CalibrationError(
        "calibration_ref_mismatch",
        f"批次中没有 scale={scale_value}% 且指标落入容差带 "
        f"[{low:.6g}, {high:.6g}] 的可用标定轮：该参照不是已收敛标定",
    )


__all__ = [
    "AXIS_LOAD_SCALE",
    "AXIS_PERFECT_CAPACITY",
    "AXIS_UNIT_LABELS",
    "CRITERION_METRICS",
    "DEFAULT_MIN_STEP_MW",
    "DEFAULT_TOLERANCE_PERCENT",
    "DECISION_CONVERGED",
    "DECISION_EXHAUSTED",
    "DECISION_NOT_CONVERGED",
    "DECISION_PROBE",
    "INITIAL_INTERVAL_PERCENT",
    "MAX_ROUNDS",
    "MIN_STEP_PERCENT",
    "SEASONAL_ANNUAL_TOLERANCE",
    "CalibrationError",
    "VERDICT_TOO_LOOSE",
    "VERDICT_TOO_TIGHT",
    "VERDICT_WITHIN",
    "classify_round",
    "gap_picture_note",
    "next_decision",
    "seasonal_next_decision",
    "seasonal_aggregate_segment_lole",
    "seasonal_segment_days",
    "monthly_deficit_date_counts",
    "validate_calibrated_baseline_ref",
    "validate_criterion",
    "validate_seasonal_criterion",
]
