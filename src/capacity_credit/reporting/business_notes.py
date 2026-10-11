"""Business-reading report notes (C-night, package F).

THE single canonical source for the deterministic business wording the
workbench attaches to a published report and the UI pages reuse:
metric explanations, convergence stop-reason / next-step builders and
the fixed conclusion boundaries. Every function is a pure dict→text
projection over AUDITED evidence (MethodResult projection, the run's
convergence-evidence record, the pinned frozen input summary) - no
model call, no invented statistics, no policy advice.

The note payload schema consumed by the Excel/HTML renderers:

    {
      "business_notes_version": "1",
      "sections": [
        {
          "heading": str,
          "paragraphs": [str, ...],
          "table": {"columns": [str, ...], "rows": [[...], ...]} | None,
        },
        ...
      ],
    }
"""

from __future__ import annotations

from typing import Any

BUSINESS_NOTES_VERSION = "1"

BUSINESS_METRIC_EXPLANATIONS: dict[str, str] = {
    "lole_days_per_year": (
        "LOLE（缺电期望天数）：平均每年预计出现缺电的日历日数量。"
        "它不是停电事件次数，也不代表整天停电：一天内缺电1小时也计入该天。"
        "因此不能用 LOLE 乘24换算成 LOLH。"
    ),
    "lolh_hours_per_year": (
        "LOLH（缺电期望小时数）：平均每年累计缺电的小时数，"
        "把一年内所有缺电小时加总。"
    ),
    "eue_mwh_per_year": (
        "EUE（期望未满足电量）：平均每年预计缺少的电量（MWh），"
        "反映缺电的规模。"
    ),
    "neue_ppm": (
        "NEUE（归一化期望未满足电量）：EUE 占总需求电量的比例，单位 ppm"
        "（百万分之一），用于在不同体量的系统之间比较。"
        "它由 EUE 与需求电量按多气象年加权计算得出，不是各年比率的简单平均。"
    ),
}

METRIC_BUSINESS_LABELS: dict[str, str] = {
    "lole_days_per_year": "期望缺电天数（LOLE）",
    "lolh_hours_per_year": "期望缺电小时数（LOLH）",
    "eue_mwh_per_year": "期望缺供电量（EUE）",
    "neue_ppm": "缺供电量占比（NEUE）",
}

CRITERIA_LABELS: dict[str, str] = {
    "min_samples_met": "样本量达到最低要求",
    "precision_met": "主指标区间精度达标",
    "nonzero_samples_met": "非零风险样本足够",
    "recent_batches_stable": "最近批次均值稳定",
}


def format_number(value: Any) -> str:
    """A stable business number rendering (thousands separators; four
    decimals at most; integers without a decimal point)."""
    number = float(value)
    if number == round(number) and abs(number) < 1e6:
        return f"{round(number):,}"
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def convergence_stop_reason(evidence: dict[str, Any]) -> str:
    """The ACTUAL stop reason in business words, derived only from the
    recorded criteria (D2): 样本不足 is never the blanket answer."""
    criteria = dict(evidence.get("criteria") or {})
    measured = dict(evidence.get("measured") or {})
    risk_conv = str(evidence.get("risk_convergence") or "")
    nonzero = measured.get("nonzero_sample_count")
    if risk_conv == "converged":
        return (
            "四项判据全部满足，计算收敛。注意：这是主风险指标的收敛，"
            "不代表另外三项指标达到相同精度。"
        )
    if criteria.get("nonzero_samples_met") is False:
        if nonzero == 0:
            return (
                "本次模拟样本中没有观察到任何缺供事件（全部样本的风险指标为0），"
                "非零风险样本判据不满足；计算在未满足判据的情况下停止，"
                "不能当作已收敛。"
            )
        return (
            f"观察到缺供的样本偏少（{nonzero} 个，低于判据要求），"
            "统计证据不足，计算在未满足判据的情况下停止。"
        )
    if criteria.get("min_samples_met") is False:
        measured_sample = measured.get("sample_count")
        min_samples = (evidence.get("thresholds") or {}).get("min_samples")
        return (
            f"样本量未达到最低要求（{measured_sample}/{min_samples}），"
            "样本预算不足，计算在未满足判据的情况下停止。"
        )
    if criteria.get("precision_met") is False:
        return (
            "主风险指标的区间精度未达到要求（估计波动过大），"
            "计算在未满足判据的情况下停止。"
        )
    if criteria.get("recent_batches_stable") is False:
        return "最近批次均值尚不稳定，计算在未满足判据的情况下停止。"
    return "部分判据未满足，计算停止；具体见逐判据读数。"


def convergence_next_step(evidence: dict[str, Any]) -> str:
    """The next step that MATCHES the actual stop reason (D2/D3)."""
    criteria = dict(evidence.get("criteria") or {})
    measured = dict(evidence.get("measured") or {})
    risk_conv = str(evidence.get("risk_convergence") or "")
    if risk_conv == "converged":
        return (
            "可结合结果状态直接阅读本结果；仍需注意收敛不等于供电达标。"
        )
    if criteria.get("nonzero_samples_met") is False:
        if measured.get("nonzero_sample_count") == 0:
            return (
                "数值0按实际结果显示；这不能推断系统无风险，也不能给出统计精度。"
                "可结合业务经验判断当前裕度；如需统计上的风险区间，需要能观察到"
                "缺供事件的评估条件或更多样本，但增加样本不保证一定满足精度要求。"
            )
        return (
            "可准备更大的模拟样本集（更多样本数）后重新计算，"
            "以积累更多缺供样本。"
        )
    if criteria.get("min_samples_met") is False:
        return (
            "当前样本量不足：生成更大样本数的模拟样本集（或使用包含更多样本的"
            "样本集）后重新计算。"
        )
    if criteria.get("precision_met") is False or criteria.get(
        "recent_batches_stable"
    ) is False:
        return "可增加模拟样本数量后重新计算，让估计更稳定。"
    return "请参考逐判据读数定位未满足的判据后，调整样本设置重新计算。"


RESULT_STATUS_BUSINESS: dict[str, str] = {
    "valid": "计算已完成，结果有效。",
    "valid_with_warning": "计算已完成，结果有效但携带警告（见警告与限制）。",
    "not_converged": "计算已完成但未满足收敛要求：数值是当前证据下的诚实读数，不构成最终结论。",
    "low_resolution": "观察到的事件过少，结果只能作为低分辨率读数。",
}


def build_convergence_section(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """模拟方法与计算完成情况 (F1 §3): the criteria table + the actual
    stop reason; an absent evidence record says so honestly."""
    if not isinstance(evidence, dict):
        return {
            "heading": "计算完成情况",
            "paragraphs": [
                (
                    "本结果没有判据级收敛证据（旧版本运行）。"
                    "只能显示样本数与最终收敛状态；重新计算可获得逐判据明细。"
                )
            ],
            "table": None,
        }
    criteria = dict(evidence.get("criteria") or {})
    measured = dict(evidence.get("measured") or {})
    thresholds = dict(evidence.get("thresholds") or {})
    metric = str(evidence.get("primary_risk_metric") or "")
    rows: list[list[str]] = []
    label_of = {v: k for k, v in CRITERIA_LABELS.items()}
    for label in (
        "样本量达到最低要求",
        "非零风险样本足够",
        "主指标区间精度达标",
        "最近批次均值稳定",
    ):
        key = label_of.get(label, label)
        value = criteria.get(key)
        state = "满足" if value is True else "未满足" if value is False else "无法判断"
        requirement = ""
        if key == "min_samples_met":
            requirement = f"最低样本量 {thresholds.get('min_samples', '—')}"
        elif key == "nonzero_samples_met":
            requirement = f"非零样本 ≥{thresholds.get('min_nonzero_samples', '—')}"
        elif key == "precision_met":
            requirement = (
                f"相对半宽 ≤{float(thresholds.get('relative_half_width_tolerance', 0)) * 100:.1f}%"
                if thresholds.get("relative_half_width_tolerance") is not None
                else "按既有误差容限检查"
            )
        rows.append([label, requirement, state])
    paragraphs = [
        (
            f"主要关注指标：{METRIC_BUSINESS_LABELS.get(metric, metric or '—')}。"
            "收敛按该指标判定；结果仍同时输出全部四项基础充裕度指标，"
            "主指标收敛不代表另外三项满足相同精度。"
        ),
        f"实际停止原因：{convergence_stop_reason(evidence)}",
        f"对应的下一步：{convergence_next_step(evidence)}",
    ]
    measured_parts: list[str] = []
    if measured.get("sample_count") is not None:
        measured_parts.append(f"使用样本 {measured['sample_count']} 个")
    if measured.get("nonzero_sample_count") is not None:
        measured_parts.append(f"非零风险样本 {measured['nonzero_sample_count']} 个")
    if (
        measured.get("primary_mean") is not None
        and measured.get("interval_half_width") is not None
    ):
        if measured.get("relative_half_width") is not None:
            measured_parts.append(
                f"主指标均值 {format_number(measured['primary_mean'])}，"
                f"判据区间半宽 {format_number(measured['interval_half_width'])}"
                f"（相对半宽 {float(measured['relative_half_width']) * 100:.2f}%）"
            )
        else:
            measured_parts.append(
                f"主指标均值 {format_number(measured['primary_mean'])}，"
                "均值为0或接近0时按绝对误差判据判定，不计算不稳定的相对误差"
            )
    if measured_parts:
        paragraphs.append("判据读数：" + "；".join(measured_parts) + "。")
    return {
        "heading": "计算完成情况",
        "paragraphs": paragraphs,
        "table": {
            "columns": ["判据", "要求或检查方式", "实际情况"],
            "rows": rows,
        },
    }


def build_metric_section(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """基础供电风险及业务解释 (F1 §4): only metrics that EXIST on the
    result, each with its business explanation (E2 boundaries inside)."""
    if not metrics:
        return {
            "heading": "基础供电风险及业务解释",
            "paragraphs": ["本结果状态未携带基础风险指标数值。"],
            "table": None,
        }
    columns = ["指标", "实际计算结果", "业务解释"]
    rows: list[list[str]] = []
    for key, label in METRIC_BUSINESS_LABELS.items():
        if metrics.get(key) is not None:
            rows.append(
                [label, format_number(metrics[key]), BUSINESS_METRIC_EXPLANATIONS[key]]
            )
    return {
        "heading": "基础供电风险及业务解释",
        "paragraphs": [
            (
                "四项指标从缺电天数、缺电小时数、缺电电量和归一化比例四个角度"
                "描述同一组模拟结果，应共同理解，不能只看其中一项。"
            )
        ],
        "table": {"columns": columns, "rows": rows},
    }


CONCLUSION_BOUNDARY_TEXT = (
    "收敛只说明估计达到了本次设定的精度要求，不表示该方案供电达标；"
    "本工具未内置任何考核标准，不在无适用标准时判定“合格/不合格”，"
    "也不自动套用其他地区阈值。"
)

SCOPE_BOUNDARY_TEXT = (
    "本结果不包含季节、高峰或持续缺电事件分析（那需要逐时证据的专门核查）；"
    "没有对照试算时，不能给出储能、新能源或联络线能降低多少风险的结论；"
    "示例数据始终保留示例标识，不用于实际地区结论。"
)


def build_notes_from_run(
    *,
    method: str,
    method_label: str,
    result_status: str,
    result_status_business: str,
    metrics: dict[str, Any] | None,
    convergence_evidence: dict[str, Any] | None,
    scope_paragraphs: list[str],
    scope_table: dict[str, Any] | None,
    method_paragraphs: list[str],
    improvement_note: str,
    appendix_rows: list[list[str]],
) -> dict[str, Any]:
    """Assemble the F1 note structure from caller-derived AUDITED facts.
    The caller (application layer) reads the run's own artifacts; this
    module never re-reads business files and never invents values."""
    sections: list[dict[str, Any]] = []
    sections.append(
        {
            "heading": "结论摘要",
            "paragraphs": [
                result_status_business,
                CONCLUSION_BOUNDARY_TEXT,
            ],
            "table": None,
        }
    )
    sections.append(
        {
            "heading": "评估范围与供需条件",
            "paragraphs": scope_paragraphs,
            "table": scope_table,
        }
    )
    sections.append(
        {
            "heading": "模拟方法与计算完成情况",
            "paragraphs": method_paragraphs,
            "table": None,
        }
    )
    sections.append(build_convergence_section(convergence_evidence))
    sections.append(build_metric_section(metrics))
    sections.append(
        {
            "heading": "改善方向与后续评估",
            "paragraphs": [improvement_note],
            "table": None,
        }
    )
    sections.append(
        {
            "heading": "使用边界",
            "paragraphs": [CONCLUSION_BOUNDARY_TEXT, SCOPE_BOUNDARY_TEXT],
            "table": None,
        }
    )
    sections.append(
        {
            "heading": "附录：结果来源与复核",
            "paragraphs": [
                (
                    "本节内容为确定性模板与可核验派生值；百分比等换算基于实际数值，"
                    "不是新一轮模型计算。机器结果与已发布清单未被本节修改。"
                )
            ],
            "table": {"columns": ["项目", "值"], "rows": appendix_rows},
        }
    )
    return {
        "business_notes_version": BUSINESS_NOTES_VERSION,
        "method": str(method),
        "method_label": str(method_label),
        "sections": sections,
    }


__all__ = [
    "BUSINESS_METRIC_EXPLANATIONS",
    "BUSINESS_NOTES_VERSION",
    "CONCLUSION_BOUNDARY_TEXT",
    "CRITERIA_LABELS",
    "METRIC_BUSINESS_LABELS",
    "RESULT_STATUS_BUSINESS",
    "SCOPE_BOUNDARY_TEXT",
    "build_convergence_section",
    "build_metric_section",
    "build_notes_from_run",
    "convergence_next_step",
    "convergence_stop_reason",
    "format_number",
]
