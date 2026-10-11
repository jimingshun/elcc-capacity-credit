"""EFC shared components (T17 round-1; T18 round-3 convergence fix).

Single stable home for the EFC application pieces that were previously
duplicated between the workflow and the tests:

- ``resolve_weather_year_weights``: the FORMAL weather-year weight set
  from the frozen tables (never a hard-coded equal weight);
- ``risk_series_from_evidence``: moved to the NEUTRAL shared adapter
  ``application.risk_series`` (EFC and MRI both call it - no second
  copy, no second convergence fact source);
- ``resource_run_config_from``: the resource-run config built from the
  frozen bank + EFC run settings;
- ``make_perfect_trial_provider``: the S+F trial provider using the
  kernel trial-only perfect-capacity input (B) - never a renewable
  flat-output resource. Its convergence evidence comes from the shared
  adapter's snapshot ATTACHED to the trial series - never a hard-coded
  CONVERGED.

The EFC workflow AND the integration tests call these production
functions - there is no second copy of the logic in the tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from capacity_credit.application.p1_workflow import P1WorkflowError
from capacity_credit.application.resource_run import (
    ResourceRunConfig,
    run_resource_pipeline,
)
from capacity_credit.application.risk_series import risk_series_from_evidence
from capacity_credit.contracts.enums import RiskConvergence, RiskMetric
from capacity_credit.solvers.efc import EfcTrialOutcome
from capacity_credit.solvers.equal_risk import canonical_sample_set_identity
from capacity_credit.solvers.paired_evaluation import (
    RiskSampleSeries,
    evaluate_paired_differences,
)

EFC_PORTFOLIO_ID = "PORT_EFC"


def convergence_config_from(method_convergence: Any) -> Any:
    """Convert the run's DECLARED convergence standard
    (``ElccMethodConvergenceConfig`` / EFC method config ``convergence``)
    into the shared engine ``ConvergenceConfig``. The trial convergence
    evidence is then computed with the SAME declared standard the run
    declares - never a hard-coded CONVERGED and never a second set of
    thresholds. Only the shared engine's fields are forwarded (the EFC
    config's schema/note fields never leak into the engine)."""
    from capacity_credit.engine.convergence import ConvergenceConfig

    if method_convergence is None:
        return None
    payload = method_convergence.model_dump(mode="json")
    forwarded = {
        key: value
        for key, value in payload.items()
        if key in ConvergenceConfig.__dataclass_fields__
    }
    return ConvergenceConfig(**forwarded)


def resolve_weather_year_weights(
    tables: dict[str, pd.DataFrame],
    weight_set_id: str,
) -> dict[int, float]:
    """The formal weather-year weight set (single source: the frozen
    ``weather_year_weight_sets`` standard table). The EFC adapter never
    hard-codes equal weights."""
    weights_table = tables.get("weather_year_weight_sets")
    if weights_table is None or weights_table.empty:
        raise P1WorkflowError(
            "P1_EFC_WEIGHT_SET_MISSING",
            f"weather_year_weight_sets table is required for EFC (weight_set_id={weight_set_id!r})",
        )
    rows = weights_table[weights_table["weight_set_id"].astype(str) == weight_set_id]
    if rows.empty:
        raise P1WorkflowError(
            "P1_EFC_WEIGHT_SET_MISSING",
            f"weight_set_id {weight_set_id!r} not found",
        )
    weights: dict[int, float] = {}
    for _, row in rows.iterrows():
        year = int(float(row["weather_year"]))
        weights[year] = float(row["weight"])
    return weights


def resource_run_config_from(
    bank: Any,
    *,
    samples: int,
    primary_risk_metric: RiskMetric,
    scenario_id: str | None,
    storage_soc: Any = None,
    perfect_capacity_mw: float = 0.0,
    weather_years: tuple[int, ...] | None = None,
    year_weights: dict[int, float] | None = None,
    emergency_layer: str = "auto",
) -> ResourceRunConfig:
    """The resource-run config for one EFC system evaluation, built from
    the frozen bank and the EFC run settings (probability-baseline
    roles only - stress years never enter). The FORMAL year weights are
    written into ``ResourceRunConfig.year_weights`` so the evidence
    risk_metrics, the RiskSampleSeries, the EFC result and the
    MethodResult all use the SAME weighting."""
    years = tuple(sorted(int(y) for y in (weather_years or bank.index["weather_years"])))
    return ResourceRunConfig(
        scenario_bank_id=bank.bank_id,
        root_seed=int(bank.index["root_seed"]),
        samples=samples,
        weather_years=years,
        year_roles={year: "probability_baseline" for year in years},
        year_weights=dict(year_weights) if year_weights is not None else None,
        timezone=str(bank.index["timezone"]),
        primary_risk_metric=primary_risk_metric.value,
        scenario_id=scenario_id,
        storage_soc=storage_soc,
        perfect_capacity_mw=perfect_capacity_mw,
        emergency_layer=emergency_layer,
    )


def resolve_probability_year_weights(
    tables: dict[str, pd.DataFrame],
    weight_set_id: str,
    probability_years: tuple[int, ...] | list[int],
) -> dict[int, float]:
    """The FORMAL weather-year weights restricted to the
    probability-baseline years with STRICT year-set equality: the
    weight-set years must exactly equal the probability years - a
    missing or extra year is a stable block, never a silent
    intersection drop."""
    weights = resolve_weather_year_weights(tables, weight_set_id)
    prob = tuple(sorted(int(y) for y in probability_years))
    weight_years = tuple(sorted(int(y) for y in weights))
    if set(prob) != set(weight_years):
        missing = sorted(set(prob) - set(weight_years))
        extra = sorted(set(weight_years) - set(prob))
        raise P1WorkflowError(
            "P1_EFC_YEAR_WEIGHT_MISMATCH",
            "weight-set years must exactly equal the probability years "
            f"(missing {missing}, extra {extra})",
        )
    return {year: float(weights[year]) for year in prob}


def make_perfect_trial_provider(
    *,
    bank: Any,
    base_tables: dict[str, pd.DataFrame],
    rrc: ResourceRunConfig,
    reference: RiskSampleSeries,
    metric: RiskMetric,
    shared_ids: frozenset[str],
    run_id: str,
    weather_years: tuple[int, ...],
    year_weights: dict[int, float],
    convergence_config: Any = None,
    criterion_window: tuple[set[int], set[int]] | None = None,
) -> Callable[[float], EfcTrialOutcome]:
    """The S+F trial provider: the base system S evaluated with the
    kernel's trial-only perfect-capacity input F. The perfect capacity
    responds first to the remaining deficit after the ordinary supply
    (no new surplus), the ordinary supply's original surplus is
    preserved for storage charging, and storage then dispatches on the
    remaining net load - no renewable flat-output resource.

    Convergence evidence comes from the shared adapter's snapshot
    ATTACHED to the trial series (never a hard-coded CONVERGED).
    ``convergence_config`` may override the shared ConvergenceConfig
    (e.g. an explicit provisional teaching config for G0 synthetic
    banks); production callers leave it None and use the default."""

    def provider(capacity_mw: float) -> EfcTrialOutcome:
        import dataclasses

        from capacity_credit.contracts.risk_convergence import convergence_config_identity
        from capacity_credit.engine.convergence import default_config

        trial_config = dataclasses.replace(rrc, perfect_capacity_mw=float(capacity_mw))
        evidence = run_resource_pipeline(
            bank=bank,
            tables=base_tables,
            config=trial_config,
            run_id=f"{run_id}_{float(capacity_mw):.4f}",
        )
        series = risk_series_from_evidence(
            evidence,
            metric,
            bank,
            shared_ids,
            frozenset({"PERFECT_F"}),
            weather_years,
            year_weights,
            convergence_config=convergence_config,
            criterion_window=criterion_window,
        )
        paired = evaluate_paired_differences(reference=reference, candidate=series)
        snapshot = series.convergence_snapshot
        declared_config = convergence_config or default_config()
        config_identity = (
            snapshot.config_identity
            if snapshot is not None
            else convergence_config_identity(declared_config.to_snapshot())
        )
        return EfcTrialOutcome(
            paired_result=paired,
            risk_metrics=dict(evidence.risk_metrics),
            risk_convergence=snapshot.risk_convergence
            if snapshot is not None
            else RiskConvergence.NOT_CONVERGED,
            convergence_criteria=dict(snapshot.criteria) if snapshot is not None else {},
            should_stop=snapshot.should_stop if snapshot is not None else False,
            recent_batch_evidence=snapshot.recent_batch_evidence if snapshot is not None else (),
            sample_set_identity=canonical_sample_set_identity(series),
            config_identity=config_identity,
        )

    return provider




def compute_efc_evidence(
    *,
    bank: Any,
    tables_base: dict[str, pd.DataFrame],
    tables_candidate: dict[str, pd.DataFrame],
    rrc: ResourceRunConfig,
    metric: RiskMetric,
    weather_years: tuple[int, ...],
    year_weights: dict[int, float],
    shared_ids: frozenset[str],
    candidate_ids: frozenset[str],
    upper_bound_mw: float,
    run_id: str,
    efc_config: Any,
    convergence_config: Any = None,
    stage_callback: Callable[[str, str], None] | None = None,
    trial_observer: Any = None,
    criterion_window: tuple[set[int], set[int]] | None = None,
) -> dict[str, Any]:
    """THE single shared EFC computation (fifth round): base/candidate
    resource runs + per-sample risk series + the perfect-capacity trial
    provider + the EFC solver. The EFC application workflow
    (run_efc_pipeline) AND the PRAS benchmark (pras_runner) both call
    this function - no second orchestration exists. Returns the solver
    result plus the intermediate evidences/series for evidence
    assembly.

    T22-M round 1: an OPTIONAL coarse-stage callback (stage, state)
    may be supplied - it is invoked ONLY at the real computation
    boundaries (baseline resource run, candidate resource run, run_efc)
    with start/complete pairing. Default None keeps the previous
    behaviour byte-for-byte unchanged; events never enter the results.

    V1-T3-E: an OPTIONAL ``trial_observer`` (capacity_mw) may be
    supplied - it is invoked at each REAL S+F trial boundary (after the
    trial capacity is chosen, before the trial runs); failures are the
    CALLER's isolation duty (the workflow wraps it), the default None
    keeps the behaviour byte-for-byte unchanged and events never enter
    the results."""
    from capacity_credit.solvers.efc import run_efc

    if stage_callback is not None:
        stage_callback("baseline_risk", "start")
    base_ev = run_resource_pipeline(
        bank=bank, tables=tables_base, config=rrc, run_id=run_id + "_base"
    )
    if stage_callback is not None:
        stage_callback("baseline_risk", "complete")
    if stage_callback is not None:
        stage_callback("augmented_risk", "start")
    cand_ev = run_resource_pipeline(
        bank=bank, tables=tables_candidate, config=rrc, run_id=run_id + "_candidate"
    )
    if stage_callback is not None:
        stage_callback("augmented_risk", "complete")
    reference = risk_series_from_evidence(
        cand_ev,
        metric,
        bank,
        shared_ids,
        candidate_ids,
        weather_years,
        year_weights,
        convergence_config=convergence_config,
        criterion_window=criterion_window,
    )
    base_series = risk_series_from_evidence(
        base_ev,
        metric,
        bank,
        shared_ids,
        frozenset(),
        weather_years,
        year_weights,
        convergence_config=convergence_config,
        criterion_window=criterion_window,
    )
    provider = make_perfect_trial_provider(
        bank=bank,
        base_tables=tables_base,
        rrc=rrc,
        reference=reference,
        metric=metric,
        shared_ids=shared_ids,
        run_id=run_id,
        weather_years=weather_years,
        year_weights=year_weights,
        convergence_config=convergence_config,
        criterion_window=criterion_window,
    )
    if trial_observer is not None:
        # V1-T3-E: report each REAL trial boundary; the observer is the
        # caller's isolation duty, the trial path itself is untouched
        base_provider = provider
        trial_counter = {"n": 0}

        def provider(capacity_mw: float) -> Any:
            trial_counter["n"] += 1
            try:
                trial_observer(capacity_mw)
            except Exception:  # noqa: BLE001, S110 - progress isolation only
                pass
            return base_provider(capacity_mw)

    base_risk: float | None = None
    if base_series.sample_ids:
        total = 0.0
        for year in weather_years:
            for sample in base_series.sample_ids:
                total += year_weights[year] * base_series.values[sample]
        base_risk = total / len(base_series.sample_ids)
    if stage_callback is not None:
        stage_callback("efc_search", "start")
    efc = run_efc(
        config=efc_config,
        reference=reference,
        trial_provider=provider,
        primary_risk_metric=metric,
        base_risk=base_risk,
        candidate_metrics=dict(cand_ev.risk_metrics),
    )
    if stage_callback is not None:
        stage_callback("efc_search", "complete")
    return {
        "efc": efc,
        "base_ev": base_ev,
        "candidate_ev": cand_ev,
        "base_series": base_series,
        "reference": reference,
        "provider": provider,
        "base_risk": base_risk,
    }

__all__ = [
    "EFC_PORTFOLIO_ID",
    "compute_efc_evidence",
    "make_perfect_trial_provider",
    "resolve_probability_year_weights",
    "resolve_weather_year_weights",
    "resource_run_config_from",
]
