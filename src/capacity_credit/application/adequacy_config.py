"""V1-T3-A adequacy-config adapter (the single serialization bridge).

run-config 1.2 carries the structured ``adequacy_config`` section whose
field semantics reuse the resource-run configuration; this module is
the UNIQUE validated mapping from the frozen 1.2 contract to the
application objects the resource chain actually consumes
(``ResourceRunConfig`` + ``ConvergenceConfig``). It is called BEFORE
any runner side effect: an inconsistent frozen configuration is
refused here, never silently repaired.

Outer-run-config facts (scenario bank, root seed, weather years and
their weights) stay outer-level: the adapter takes them as explicit
arguments and never invents a second copy inside the section.
"""

from __future__ import annotations

from capacity_credit.application.resource_run_inputs import (
    DEFAULT_DELTA_T_HOURS,
    ResourceRunConfig,
)
from capacity_credit.contracts.runs import RunConfig12
from capacity_credit.engine.convergence import ConvergenceConfig


class AdequacyConfigError(ValueError):
    """A frozen 1.2 adequacy configuration that cannot enter the
    resource chain (stable machine code, honest message)."""


def build_resource_run_config(
    run_config: RunConfig12,
    *,
    scenario_bank_id: str,
    root_seed: int,
    timezone: str,
    weather_years: tuple[int, ...],
    year_roles: dict[int, str],
    year_weights: dict[int, float] | None,
    declared_grade: str = "G0_teaching",
) -> ResourceRunConfig:
    """Map the frozen 1.2 contract onto the resource-run configuration
    with the OUTER bank/seed/timezone/years/weights (bank-bound facts
    come from the opened bank; the section never carries a second copy
    of them). Perfect capacity stays 0: independent adequacy is not an
    EFC trial."""
    section = run_config.adequacy_config
    if not scenario_bank_id:
        raise AdequacyConfigError(
            "ADEQUACY_CONFIG_FIELD_MISSING",
            "adequacy execution requires scenario_bank_id (from the opened bank)",
        )
    if not timezone:
        raise AdequacyConfigError(
            "ADEQUACY_CONFIG_FIELD_MISSING",
            "adequacy execution requires timezone (from the opened bank)",
        )
    # SOC consistency is already enforced at the contract layer
    # (extra=forbid + the storage_soc/chronology validators); the
    # mapping preserves the values verbatim.
    return ResourceRunConfig(
        scenario_bank_id=scenario_bank_id,
        root_seed=root_seed,
        samples=section.samples,
        weather_years=tuple(weather_years),
        year_roles=dict(year_roles),
        year_weights=dict(year_weights) if year_weights is not None else None,
        timezone=timezone,
        primary_risk_metric=run_config.primary_risk_metric.value,
        declared_grade=declared_grade,
        storage_soc=section.storage_soc,
        chronology_mode=run_config.chronology_mode,
        soc_year_boundary_mode=run_config.soc_year_boundary_mode,
        dispatch_strategy=section.dispatch_strategy,
        risk_window_length_hours=section.risk_window_length_hours,
        risk_threshold=section.risk_threshold,
        reserve_soc_fraction=section.reserve_soc_fraction,
        intertie_availability_policy=section.intertie_availability_policy,
        emergency_limit_source=section.emergency_limit_source,
        hydro_adjustable_request_mw=section.hydro_adjustable_request_mw,
        scenario_id=run_config.scenario_id or section.scenario_id,
        auto_schedule_planned_outages=section.auto_schedule_planned_outages,
        merge_events_across_years=section.merge_events_across_years,
        delta_t_hours=section.delta_t_hours,
        perfect_capacity_mw=0.0,
    )


def build_convergence_config(run_config: RunConfig12) -> ConvergenceConfig:
    """Map the frozen 1.2 convergence section onto the shared
    convergence gate configuration; a null inner metric defers to the
    outer run-config metric (parity already validated at the contract
    layer)."""
    c = run_config.adequacy_config.convergence
    return ConvergenceConfig(
        batch_size=c.batch_size,
        min_samples=c.min_samples,
        max_samples=c.max_samples,
        confidence_level=c.confidence_level,
        relative_half_width_tolerance=c.relative_half_width_tolerance,
        absolute_half_width_tolerance=c.absolute_half_width_tolerance,
        near_zero_threshold=c.near_zero_threshold,
        min_nonzero_samples=c.min_nonzero_samples,
        recent_batch_window=c.recent_batch_window,
        recent_batch_stability_tolerance=c.recent_batch_stability_tolerance,
        use_student_t=c.use_student_t,
        normal_interval_min_samples=c.normal_interval_min_samples,
        primary_risk_metric=c.primary_risk_metric
        or run_config.primary_risk_metric.value,
        provisional_defaults=c.provisional_defaults,
    )


def contract_delta_t_default() -> float:
    """The resource-layer default the contract's ``delta_t_hours``
    default must stay in parity with (asserted by test; keeps the
    single semantic source in the resource domain)."""
    return DEFAULT_DELTA_T_HOURS


__all__ = [
    "AdequacyConfigError",
    "build_convergence_config",
    "build_resource_run_config",
    "contract_delta_t_default",
]
