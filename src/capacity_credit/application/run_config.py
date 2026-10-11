"""The reliability run-config assembly boundary (slice 3b-1).

THE single place that turns the adequacy parameter form into the
config the facade's ``start_calculation`` consumes. The Streamlit
form layer keeps its widgets but the KEY CONTRACT (defaults, seed
read-only from the sample bank, R01 bank ordering, fixed formal run
mode) is stated here once - the web serve layer consumes the same
builder, so the two front doors cannot drift.

ELCC/EFC builders are slice 3b-2: their widget interlocks are larger
and stay in ui/forms.py until extracted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from capacity_credit.application.workbench_service import WorkbenchError

FIXED_RUN_MODE = "formal"

RISK_METRICS: tuple[tuple[str, str], ...] = (
    ("lole_days_per_year", "LOLE 期望缺电天数（天/年）"),
    ("lolh_hours_per_year", "LOLH 期望缺电小时数（小时/年）"),
    ("eue_mwh_per_year", "EUE 期望缺供电量（MWh/年）"),
    ("neue_ppm", "NEUE 缺供电量占比（ppm）"),
)

_METRIC_VALUES = frozenset(value for value, _label in RISK_METRICS)


def order_banks_for_data_ref(
    banks: list[dict[str, Any]], data_ref: str | None
) -> list[dict[str, Any]]:
    """R01: banks bound to the SELECTED data version come first; a bank
    first built under another version stays offered with its honest
    provenance label (the run gates validate seed and structure)."""
    if data_ref is None:
        return list(banks)
    bound = [
        b
        for b in banks
        if isinstance(b.get("source_binding"), dict)
        and b["source_binding"].get("import_batch_id") == data_ref
    ]
    return bound + [b for b in banks if b not in bound]


def _common_selections(
    form: Mapping[str, Any],
    options: Mapping[str, Any],
    data_ref: str | None,
) -> dict[str, Any]:
    """The shared facts every method config consumes: the sample bank
    (seed read-only), the convergence-anchor metric, the declared
    weather-year weights, the fixed formal run mode, the optional
    scenario. Raises WorkbenchError on impossible selections."""
    banks = order_banks_for_data_ref(
        list(options.get("scenario_banks") or []), data_ref
    )
    if not banks:
        raise WorkbenchError(
            "invalid_input",
            "本项目还没有模拟样本集：请先在基础数据管理生成模拟样本。",
        )
    bank_ids = [str(b["scenario_bank_id"]) for b in banks]
    bank_id = str(form.get("bank_id") or bank_ids[0])
    if bank_id not in bank_ids:
        bank_id = bank_ids[0]
    selected = next(b for b in banks if str(b["scenario_bank_id"]) == bank_id)

    metric = str(form.get("metric") or RISK_METRICS[0][0])
    if metric not in _METRIC_VALUES:
        raise WorkbenchError("invalid_input", f"未知主要风险指标：{metric!r}")

    weight_ids = list(options.get("weight_set_ids") or [])
    weight_set_id = str(form.get("weight_set_id") or "") or (
        weight_ids[0] if weight_ids else ""
    )
    if not weight_set_id:
        raise WorkbenchError(
            "invalid_input",
            "已确认数据中未声明权重集：提交将被既有配置检查拒绝。",
        )

    scenario_ids = list(options.get("scenario_ids") or [])
    scenario_id = str(form.get("scenario_id") or "") or None
    if scenario_id is not None and scenario_ids and scenario_id not in scenario_ids:
        raise WorkbenchError("invalid_input", f"未知业务情景：{scenario_id!r}")

    return {
        "run_mode": FIXED_RUN_MODE,
        "scenario_bank_id": selected["scenario_bank_id"],
        "root_seed": int(selected.get("root_seed")),
        "primary_risk_metric": metric,
        "weight_set_id": weight_set_id,
        "scenario_id": scenario_id,
    }


def build_adequacy_config(
    form: Mapping[str, Any],
    options: Mapping[str, Any],
    data_ref: str | None,
) -> dict[str, Any]:
    """The adequacy config: the sample bank's OWN root seed (read-only,
    never re-picked), the convergence-anchor metric, the declared
    weather-year weights, the fixed formal run mode and the optional
    business scenario."""
    banks = order_banks_for_data_ref(
        list(options.get("scenario_banks") or []), data_ref
    )
    if not banks:
        raise WorkbenchError(
            "invalid_input",
            "本项目还没有模拟样本集：请先在基础数据管理生成模拟样本。",
        )
    bank_ids = [str(b["scenario_bank_id"]) for b in banks]
    bank_id = str(form.get("bank_id") or bank_ids[0])
    if bank_id not in bank_ids:
        bank_id = bank_ids[0]
    selected = next(b for b in banks if str(b["scenario_bank_id"]) == bank_id)

    metric = str(form.get("metric") or RISK_METRICS[0][0])
    if metric not in _METRIC_VALUES:
        raise WorkbenchError(
            "invalid_input", f"未知主要风险指标：{metric!r}"
        )

    weight_ids = list(options.get("weight_set_ids") or [])
    weight_set_id = str(form.get("weight_set_id") or "") or (
        weight_ids[0] if weight_ids else ""
    )
    if not weight_set_id:
        raise WorkbenchError(
            "invalid_input",
            "已确认数据中未声明权重集：提交将被既有配置检查拒绝。",
        )

    scenario_ids = list(options.get("scenario_ids") or [])
    scenario_id = str(form.get("scenario_id") or "") or None
    if scenario_id is not None and scenario_ids and scenario_id not in scenario_ids:
        raise WorkbenchError(
            "invalid_input", f"未知业务情景：{scenario_id!r}"
        )

    config: dict[str, Any] = {
        "run_config": {
            "scenario_bank_id": selected["scenario_bank_id"],
            "root_seed": int(selected.get("root_seed")),
            "primary_risk_metric": metric,
            "weather_year_weight_set_id": weight_set_id,
            "run_mode": FIXED_RUN_MODE,
        },
        # F4/C5 completeness: the adequacy path declares the SAME explicit
        # small-sample provisional profile as elcc/efc. Without it the
        # formal-intent adequacy run falls into the strict engine defaults
        # (min 2000 / 5% precision), every batch calibration round lands
        # diagnostic/not_converged and can never finalize usable. Values
        # are calibrated for criterion-sized systems (LOLE ≈ 0.1 d/y at
        # max_samples: nonzero ≈ 100 → floor 30; batch 200 fills the
        # 5-batch stability window exactly at max; ±50% precision and a
        # 100% window span absorb small-sample noise near the criterion).
        "convergence": {
            "batch_size": 200,
            "min_samples": 30,
            "max_samples": 1000,
            "confidence_level": 0.95,
            "relative_half_width_tolerance": 0.5,
            "absolute_half_width_tolerance": 0.001,
            "min_nonzero_samples": 30,
            "recent_batch_window": 5,
            "recent_batch_stability_tolerance": 1.0,
            "provisional_defaults": True,
        },
    }
    if scenario_id is not None:
        config["scenario_id"] = scenario_id
    return config


def build_elcc_config(
    form: Mapping[str, Any],
    options: Mapping[str, Any],
    data_ref: str | None,
) -> dict[str, Any]:
    """The ELCC application config (V1-T3-D): the candidate group from
    the frozen capacities, the growth mode, and the search settings the
    original model consumes. The common selections come from the same
    helper as adequacy (R01 bank ordering, read-only seed)."""
    common = _common_selections(form, options, data_ref)
    candidates = options.get("elcc_candidates") or []
    if not candidates:
        raise WorkbenchError(
            "invalid_input",
            "已确认数据中没有可再生待评估资源（renewable_capacities 表）。",
        )
    group_ids = [str(c["resource_group_id"]) for c in candidates]
    group_id = str(form.get("group_id") or group_ids[0])
    if group_id not in group_ids:
        group_id = group_ids[0]
    growth_modes = list(options.get("elcc_growth_modes") or ("fixed_mw",))
    growth_mode = str(form.get("growth_mode") or growth_modes[0])
    if growth_mode not in growth_modes:
        growth_mode = growth_modes[0]
    upper = float(form.get("upper_mw") if form.get("upper_mw") is not None else 2000.0)
    tolerance = float(
        form.get("tolerance_mw")
        if form.get("tolerance_mw") is not None
        else 1.0
    )
    max_iterations = int(form.get("max_iterations") or 20)
    confidence = float(form.get("confidence") if form.get("confidence") is not None else 0.95)
    allow_expansion = bool(form.get("allow_expansion"))
    expanded_upper = (
        float(form.get("expanded_upper_mw"))
        if form.get("expanded_upper_mw") is not None
        else None
    )
    return {
        "elcc_config_schema_version": "1.0",
        "run_config": {
            "run_config_schema_version": "1.0",
            "run_mode": common["run_mode"],
            "run_type": "elcc",
            "source_run_id": None,
            "parent_run_ids": [],
            "method_profile": "elcc_teaching",
            "primary_risk_metric": common["primary_risk_metric"],
            "weather_year_weight_set_id": common["weight_set_id"],
            "chronology_mode": "independent_weather_years",
            "storage_soc_initialization_mode": "not_applicable",
            "soc_year_boundary_mode": "not_applicable",
            "assessment_anchor": None,
            "perturbation_dimension": None,
            "normalization_basis_mwh_per_mw": None,
            "capacity_value_type": "average",
            "baseline_portfolio_version": None,
            "allocation_type": "none",
            "allocation_rule_id": None,
            "random_seed": common["root_seed"],
            "scenario_bank_id": common["scenario_bank_id"],
            "scenario_id": common["scenario_id"],
        },
        "search": {
            "lower_bound_mw": 0.0,
            "physical_upper_bound_mw": upper,
            "capacity_tolerance_mw": tolerance,
            "max_iterations": max_iterations,
            "monotonic_direction": "risk_increases_with_capacity",
            "allow_upper_bound_expansion": allow_expansion,
            "max_expanded_upper_bound_mw": expanded_upper,
            "confidence_level": confidence,
        },
        "load_growth": {"mode": growth_mode, "specified_profile": None},
        # F4/C5: the wizard declares EXPLICIT convergence values (an
        # untouched-defaults section is refused for formal runs) - a
        # declared small-sample provisional profile for UI-driven runs
        "convergence": {
            "batch_size": 10,
            "min_samples": 30,
            "max_samples": 1000,
            "confidence_level": confidence,
            "relative_half_width_tolerance": 0.5,
            "absolute_half_width_tolerance": 0.001,
            "provisional_defaults": True,
        },
                # F4/C5: the wizard declares EXPLICIT convergence values (an
        # untouched-defaults section is refused for formal runs) - a
        # declared small-sample provisional profile for UI-driven runs
        "convergence": {
            "batch_size": 10,
            "min_samples": 30,
            "max_samples": 1000,
            "confidence_level": confidence,
            "relative_half_width_tolerance": 0.5,
            "absolute_half_width_tolerance": 0.001,
            "provisional_defaults": True,
        },
        "candidate": {
            "resource_or_portfolio_id": group_id,
            "resource_group_id": group_id,
            "reference_capacity_mw": None,
        },
    }


def build_efc_config(
    form: Mapping[str, Any],
    options: Mapping[str, Any],
    data_ref: str | None,
) -> dict[str, Any]:
    """The EFC application config (V1-T3-E): the candidate group from
    the frozen capacities, the search upper bound from the chosen
    source (nameplate / declared), and NO ELCC load-growth field."""
    common = _common_selections(form, options, data_ref)
    candidates = options.get("elcc_candidates") or []
    if not candidates:
        raise WorkbenchError(
            "invalid_input",
            "已确认数据中没有待评估资源（renewable_capacities 表）。",
        )
    group_ids = [str(c["resource_group_id"]) for c in candidates]
    group_id = str(form.get("group_id") or group_ids[0])
    if group_id not in group_ids:
        group_id = group_ids[0]
    sources = list(
        options.get("efc_upper_bound_sources")
        or ("nameplate_capacity", "declared_mw")
    )
    source = str(form.get("upper_source") or sources[0])
    if source not in sources:
        source = sources[0]
    declared_mw = (
        float(form.get("declared_mw"))
        if form.get("declared_mw") is not None
        else None
    )
    if source == "declared_mw" and (declared_mw is None or declared_mw <= 0):
        raise WorkbenchError(
            "invalid_input", "声明上界（declared_mw）必须为正数。"
        )
    upper = float(
        form.get("upper_mw")
        if form.get("upper_mw") is not None
        else (
            declared_mw
            if declared_mw is not None
            else next(
                (c.get("installed_capacity_mw") for c in candidates
                 if str(c["resource_group_id"]) == group_id
                 and c.get("installed_capacity_mw") is not None),
                2000.0,
            )
        )
    )
    tolerance = float(
        form.get("tolerance_mw")
        if form.get("tolerance_mw") is not None
        else 1.0
    )
    max_iterations = int(form.get("max_iterations") or 20)
    confidence = float(
        form.get("confidence") if form.get("confidence") is not None else 0.95
    )
    allow_expansion = bool(form.get("allow_expansion"))
    expanded_upper = (
        float(form.get("expanded_upper_mw"))
        if form.get("expanded_upper_mw") is not None
        else None
    )
    return {
        "efc_config_schema_version": "1.0",
        "run_config": {
            "run_config_schema_version": "1.0",
            "run_mode": common["run_mode"],
            "run_type": "efc",
            "source_run_id": None,
            "parent_run_ids": [],
            "method_profile": "efc_teaching",
            "primary_risk_metric": common["primary_risk_metric"],
            "weather_year_weight_set_id": common["weight_set_id"],
            "chronology_mode": "independent_weather_years",
            "storage_soc_initialization_mode": "not_applicable",
            "soc_year_boundary_mode": "not_applicable",
            "assessment_anchor": None,
            "perturbation_dimension": None,
            "normalization_basis_mwh_per_mw": None,
            "capacity_value_type": "average",
            "baseline_portfolio_version": None,
            "allocation_type": "none",
            "allocation_rule_id": None,
            "random_seed": common["root_seed"],
            "scenario_bank_id": common["scenario_bank_id"],
            "scenario_id": common["scenario_id"],
        },
        "search": {
            "lower_bound_mw": 0.0,
            "physical_upper_bound_mw": upper,
            "capacity_tolerance_mw": tolerance,
            "max_iterations": max_iterations,
            "monotonic_direction": "risk_decreases_with_capacity",
            "allow_upper_bound_expansion": allow_expansion,
            "max_expanded_upper_bound_mw": expanded_upper,
            "confidence_level": confidence,
        },
        "candidate": {
            "resource_or_portfolio_id": group_id,
            "resource_group_id": group_id,
            "reference_capacity_mw": None,
        },
        "upper_bound": {
            "source": source,
            "declared_mw": declared_mw,
        },
    }


def config_summary(config: Mapping[str, Any]) -> str:
    """The ACTUAL submitted-config summary (what you see is what the
    pipeline consumes) - adequacy/ELCC/EFC branches."""
    run_config = config.get("run_config") or {}
    parts = [
        f"模拟样本集：{str(run_config.get('scenario_bank_id'))[:8]}…",
        f"随机数种子：{run_config.get('random_seed') or run_config.get('root_seed')}（随样本集固定）",
        f"主要风险指标：{run_config.get('primary_risk_metric')}",
        f"气象年权重：{run_config.get('weather_year_weight_set_id')}",
        "常规正式测算"
        if run_config.get("run_mode") == "formal"
        else f"运行模式：{run_config.get('run_mode')}",
    ]
    if config.get("scenario_id"):
        parts.append(f"业务情景：{config.get('scenario_id')}")
    candidate = config.get("candidate") or {}
    if candidate.get("resource_group_id"):
        parts.append(f"待评估资源：{candidate['resource_group_id']}（装机容量来自已确认数据）")
    if config.get("elcc_config_schema_version"):
        parts.append(f"增长方式：{(config.get('load_growth') or {}).get('mode')}")
    if config.get("efc_config_schema_version"):
        upper = config.get("upper_bound") or {}
        parts.append(
            f"上界来源：{upper.get('source')}"
            + (
                f"（声明 {upper.get('declared_mw')} MW）"
                if upper.get("declared_mw") is not None
                else ""
            )
        )
    parts.append(
        f"搜索上界：{config.get('search', {}).get('physical_upper_bound_mw')} MW"
    )
    parts.append(
        f"容差：{config.get('search', {}).get('capacity_tolerance_mw')} MW"
    )
    return "｜".join(parts)


__all__ = [
    "RISK_METRICS",
    "FIXED_RUN_MODE",
    "build_adequacy_config",
    "build_elcc_config",
    "build_efc_config",
    "config_summary",
    "order_banks_for_data_ref",
]
