"""MRI/rMRI shared application components (T18, remediation round-2).

Single stable home for the MRI application pieces so the workflow and
integration tests call production functions (no second copy of the
logic). Reuses the EFC shared components for risk series / year weights /
resource-run config AND the shared candidate resolution by object
identity (``candidate_resolution``) so a perturbation ALWAYS targets a
REAL resource / portfolio - never an all-hour flat perfect-capacity
renewable substitute.

- ``resolve_mri_target`` is the SINGLE resolution entry for the target
  type / capacity / energy / storage duration (no second
  ``_resolve_kind`` fact source);
- ``perturb_resource_tables`` mutates ONLY the target resource's physical
  fields according to the perturbation dimension
  (fixed_duration / power_only / energy_only / custom_vector); the
  resolved ``resource_kind`` is REQUIRED so the classification stays in
  ``resolve_mri_target``;
- ``make_perturbed_trial_provider`` maps a ``PerturbationVector`` to a
  ``RiskSampleSeries`` for the perturbed real resource and converts the
  resource-layer ``MriResourceError`` into the solver ``MriConfigError``
  boundary (an explicit ``MRI_PHYSICAL_DOMAIN_VIOLATION`` is the only
  fallback trigger; every other error keeps its original category);
- ``make_perfect_capacity_provider``: the S+F perfect-capacity series
  used as the rMRI denominator (solver-computed, never configured).

Both providers use the SAME scenario bank / CRN identity as the frozen
reference; the perturbed candidate series is paired by
``evaluate_paired_differences`` against the reference series so any
bank/metric/sample/CRN identity mismatch blocks with a stable code.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from typing import Any

import pandas as pd

from capacity_credit.application.candidate_resolution import (
    resolve_candidate_and_base_tables,
)
from capacity_credit.application.p1_workflow import P1WorkflowError
from capacity_credit.application.resource_run import (
    ResourceRunConfig,
    run_resource_pipeline,
)
from capacity_credit.application.risk_series import risk_series_from_evidence
from capacity_credit.contracts.elcc_method_config import ElccMethodCandidateConfig
from capacity_credit.contracts.enums import PerturbationDimension, RiskMetric
from capacity_credit.contracts.mri_contracts import PerturbationVector
from capacity_credit.solvers.mri import MriConfigError
from capacity_credit.solvers.paired_evaluation import RiskSampleSeries

MRI_PORTFOLIO_ID = "PORT_MRI"

P1_MRI_RESOURCE_UNKNOWN = "P1_MRI_RESOURCE_UNKNOWN"
MRI_RESOURCE_KIND_UNSUPPORTED = "MRI_RESOURCE_KIND_UNSUPPORTED"

_DURATION_CONSISTENCY_TOL = 1e-6


class MriResourceError(ValueError):
    """Stable MRI resource perturbation error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class MriTargetResolution:
    """The resolved REAL target resource for one MRI run.

    ``actual_duration_before_h`` is the REAL storage duration
    (energy_capacity_mwh / discharge_power_mw) from the frozen table -
    the source of truth for ``fixed_duration`` (the configured
    ``duration_hours`` is only a declaration validated against it)."""

    resource_or_portfolio_id: str
    kind: str  # "storage" | "renewable" | "hydro"
    candidate_capacity_mw: float
    candidate_energy_mwh: float | None
    actual_duration_before_h: float | None = None


def resolve_mri_target(
    tables: dict[str, pd.DataFrame],
    resource_or_portfolio_id: str,
) -> MriTargetResolution:
    """Resolve the MRI target resource by object identity against the
    frozen standard tables (shared candidate_resolution fact source).

    The reported capacity comes from the SAME tables (nameplate installed
    capacity for wind/PV, max discharge power for storage, declared
    capacity for hydro) - it is NEVER the finite-difference step or
    engineering_tolerance. The storage duration_before is the REAL
    E/P ratio, never a configured override."""
    candidate = ElccMethodCandidateConfig(
        resource_or_portfolio_id=resource_or_portfolio_id,
        resource_group_id=resource_or_portfolio_id,
    )
    resolution = resolve_candidate_and_base_tables(tables, candidate)
    kinds = resolution.kinds
    kind: str
    if kinds == frozenset({"storage"}):
        kind = "storage"
    elif kinds == frozenset({"renewable"}):
        kind = "renewable"
    elif kinds == frozenset({"hydro"}):
        kind = "hydro"
    else:
        raise P1WorkflowError(
            MRI_RESOURCE_KIND_UNSUPPORTED,
            f"MRI target {resource_or_portfolio_id!r} resolves to unsupported kinds "
            f"{sorted(kinds)}",
        )
    candidate_energy_mwh: float | None = None
    actual_duration_before_h: float | None = None
    if kind == "storage":
        storage = tables["storage_units"]
        rows = storage[storage["storage_id"].astype(str) == resource_or_portfolio_id]
        if rows.empty:
            raise P1WorkflowError(P1_MRI_RESOURCE_UNKNOWN, "storage resource not found")
        candidate_energy_mwh = float(rows["energy_capacity_mwh"].astype(float).iloc[0])
        discharge = float(rows["discharge_power_mw"].astype(float).iloc[0])
        if discharge > 0.0:
            actual_duration_before_h = candidate_energy_mwh / discharge
    return MriTargetResolution(
        resource_or_portfolio_id=resource_or_portfolio_id,
        kind=kind,
        candidate_capacity_mw=resolution.candidate_capacity_mw,
        candidate_energy_mwh=candidate_energy_mwh,
        actual_duration_before_h=actual_duration_before_h,
    )


def validate_declared_duration(
    declared_duration_hours: float | None,
    actual_duration_before_h: float | None,
) -> None:
    """Run-config ``duration_hours`` is only a DECLARATION and consistency
    check: it must agree with the REAL storage E/P duration within
    engineering tolerance or the run is rejected before any computation
    (the configured value never overrides the real resource duration)."""
    if declared_duration_hours is None:
        return
    if actual_duration_before_h is None or actual_duration_before_h <= 0.0:
        raise P1WorkflowError(
            MRI_RESOURCE_KIND_UNSUPPORTED,
            "fixed_duration requires a storage resource with a real E/P duration "
            "(energy_capacity_mwh / discharge_power_mw)",
        )
    if abs(declared_duration_hours - actual_duration_before_h) > _DURATION_CONSISTENCY_TOL * max(
        actual_duration_before_h, 1e-9
    ):
        raise P1WorkflowError(
            "P1_MRI_INVALID_CONFIG",
            f"declared duration_hours={declared_duration_hours} is inconsistent with the "
            f"real storage duration {actual_duration_before_h:.6f} h "
            "(energy/discharge); the configured value never overrides the real resource",
        )


def _storage_rows(out: dict[str, pd.DataFrame], storage_id: str) -> pd.DataFrame:
    if "storage_units" not in out:
        raise MriResourceError(P1_MRI_RESOURCE_UNKNOWN, "storage_units table missing")
    rows = out["storage_units"]
    selected = rows[rows["storage_id"].astype(str) == storage_id]
    if selected.empty:
        raise MriResourceError(P1_MRI_RESOURCE_UNKNOWN, f"storage {storage_id!r} not found")
    return selected


def _perturb_storage(
    out: dict[str, pd.DataFrame],
    storage_id: str,
    dimension: PerturbationDimension,
    delta_power_mw: float,
    delta_energy_mwh: float,
) -> None:
    selected = _storage_rows(out, storage_id)
    idx = selected.index[0]
    charge = float(selected["charge_power_mw"].iloc[0])
    discharge = float(selected["discharge_power_mw"].iloc[0])
    energy = float(selected["energy_capacity_mwh"].iloc[0])

    if dimension is PerturbationDimension.POWER_ONLY:
        new_charge = charge + delta_power_mw
        new_discharge = discharge + delta_power_mw
        new_energy = energy
    elif dimension is PerturbationDimension.ENERGY_ONLY:
        new_charge = charge
        new_discharge = discharge
        new_energy = energy + delta_energy_mwh
    elif (
        dimension is PerturbationDimension.FIXED_DURATION
        or dimension is PerturbationDimension.CUSTOM_VECTOR
    ):
        new_charge = charge + delta_power_mw
        new_discharge = discharge + delta_power_mw
        new_energy = energy + delta_energy_mwh
    else:
        raise MriResourceError(
            MRI_RESOURCE_KIND_UNSUPPORTED,
            f"storage does not support perturbation_dimension={dimension.value}",
        )
    for value in (new_charge, new_discharge, new_energy):
        if value < 0.0:
            raise MriResourceError(
                "MRI_PHYSICAL_DOMAIN_VIOLATION",
                "storage perturbation would make a capacity negative",
            )
    if dimension is PerturbationDimension.FIXED_DURATION:
        # fixed_duration MUST preserve the duration on the REAL perturbed
        # table: recompute duration_after = new_energy / new_discharge and
        # require it to equal duration_before within engineering tolerance.
        before_duration = (energy / discharge) if discharge > 0.0 else None
        after_duration = (new_energy / new_discharge) if new_discharge > 0.0 else None
        if (
            before_duration is not None
            and after_duration is not None
            and abs(after_duration - before_duration)
            > _DURATION_CONSISTENCY_TOL * max(before_duration, 1e-9)
        ):
            raise MriResourceError(
                "MRI_PHYSICAL_DOMAIN_VIOLATION",
                "fixed_duration perturbed duration differs from the before duration",
            )
    out["storage_units"].loc[idx, "charge_power_mw"] = float(new_charge)
    out["storage_units"].loc[idx, "discharge_power_mw"] = float(new_discharge)
    out["storage_units"].loc[idx, "energy_capacity_mwh"] = float(new_energy)


def _perturb_renewable(
    out: dict[str, pd.DataFrame],
    group_id: str,
    delta_power_mw: float,
    delta_energy_mwh: float,
) -> None:
    if "renewable_capacities" not in out or out["renewable_capacities"].empty:
        raise MriResourceError(P1_MRI_RESOURCE_UNKNOWN, "renewable_capacities table missing")
    caps = out["renewable_capacities"]
    cap_rows = caps[caps["resource_group_id"].astype(str) == group_id]
    if cap_rows.empty:
        raise MriResourceError(P1_MRI_RESOURCE_UNKNOWN, f"renewable group {group_id!r} not found")
    if abs(delta_energy_mwh) > 1e-12:
        raise MriResourceError(
            MRI_RESOURCE_KIND_UNSUPPORTED,
            "renewable only supports power_mw components (no energy capacity to perturb); "
            "energy components are rejected, never silently ignored",
        )
    installed = float(cap_rows["installed_capacity_mw"].astype(float).iloc[0])
    if installed <= 0.0:
        raise MriResourceError(
            "MRI_PHYSICAL_DOMAIN_VIOLATION",
            "renewable perturbation requires a positive installed capacity",
        )
    new_installed = installed + delta_power_mw
    if new_installed < 0.0:
        raise MriResourceError(
            "MRI_PHYSICAL_DOMAIN_VIOLATION",
            "renewable perturbation would make the installed capacity negative",
        )
    scale = new_installed / installed
    caps.loc[cap_rows.index, "installed_capacity_mw"] = float(new_installed)
    hourly = out["renewable_hourly"]
    group_frame = hourly[hourly["resource_group_id"].astype(str) == group_id]
    for column in ("available_generation_mw", "actual_generation_mw", "installed_capacity_mw"):
        if column in group_frame.columns:
            hourly.loc[group_frame.index, column] = group_frame[column].astype(float) * scale


def _perturb_hydro(
    out: dict[str, pd.DataFrame],
    hydro_id: str,
    dimension: PerturbationDimension,
    delta_power_mw: float,
    delta_energy_mwh: float,
) -> None:
    if "hydro_units" not in out or out["hydro_units"].empty:
        raise MriResourceError(P1_MRI_RESOURCE_UNKNOWN, "hydro_units table missing")
    units = out["hydro_units"]
    unit_rows = units[units["hydro_id"].astype(str) == hydro_id]
    if unit_rows.empty:
        raise MriResourceError(P1_MRI_RESOURCE_UNKNOWN, f"hydro {hydro_id!r} not found")
    idx = unit_rows.index[0]
    capacity = float(unit_rows["capacity_mw"].iloc[0])
    new_capacity = capacity + delta_power_mw
    if new_capacity < 0.0:
        raise MriResourceError(
            "MRI_PHYSICAL_DOMAIN_VIOLATION",
            "hydro perturbation would make the capacity negative",
        )
    units.loc[idx, "capacity_mw"] = float(new_capacity)
    if dimension is PerturbationDimension.FIXED_DURATION:
        raise MriResourceError(
            MRI_RESOURCE_KIND_UNSUPPORTED,
            "fixed_duration is only defined for storage E/P resources (hydro has no "
            "single E/P duration)",
        )
    if (
        delta_energy_mwh != 0.0
        and "hydro_energy_budgets" in out
        and not out["hydro_energy_budgets"].empty
    ):
        budgets = out["hydro_energy_budgets"]
        budget_rows = budgets[budgets["hydro_id"].astype(str) == hydro_id]
        if not budget_rows.empty:
            budgets.loc[budget_rows.index, "energy_budget_mwh"] = (
                budget_rows["energy_budget_mwh"].astype(float) + delta_energy_mwh
            )


def perturb_resource_tables(
    tables: dict[str, pd.DataFrame],
    *,
    resource_kind: str,
    resource_or_portfolio_id: str,
    perturbation_dimension: PerturbationDimension,
    delta_power_mw: float,
    delta_energy_mwh: float,
) -> dict[str, pd.DataFrame]:
    """Return tables with ONLY the target resource's physical fields
    perturbed (a real resource perturbation, never an all-hour flat
    substitute). ``resource_kind`` comes from ``resolve_mri_target`` (the
    single classification source). Negative deltas remove from the
    existing capacity; only a genuine physical-boundary violation raises.
    Any unsupported component is REJECTED - never silently ignored."""
    out = copy.deepcopy(tables)
    if resource_kind == "storage":
        _perturb_storage(
            out,
            resource_or_portfolio_id,
            perturbation_dimension,
            delta_power_mw,
            delta_energy_mwh,
        )
    elif resource_kind == "renewable":
        if perturbation_dimension is PerturbationDimension.ENERGY_ONLY:
            raise MriResourceError(
                MRI_RESOURCE_KIND_UNSUPPORTED,
                "energy_only requires a storage resource (renewables have no energy capacity)",
            )
        if perturbation_dimension is PerturbationDimension.FIXED_DURATION:
            raise MriResourceError(
                MRI_RESOURCE_KIND_UNSUPPORTED,
                "fixed_duration requires a storage resource (renewables have no E/P duration)",
            )
        _perturb_renewable(out, resource_or_portfolio_id, delta_power_mw, delta_energy_mwh)
    elif resource_kind == "hydro":
        _perturb_hydro(
            out,
            resource_or_portfolio_id,
            perturbation_dimension,
            delta_power_mw,
            delta_energy_mwh,
        )
    else:
        raise MriResourceError(
            P1_MRI_RESOURCE_UNKNOWN,
            f"unknown resource_kind {resource_kind!r} for {resource_or_portfolio_id!r}",
        )
    return out


def make_perturbed_trial_provider(
    *,
    bank: Any,
    base_tables: dict[str, pd.DataFrame],
    rrc: ResourceRunConfig,
    metric: RiskMetric,
    shared_ids: frozenset[str],
    run_id: str,
    weather_years: tuple[int, ...],
    year_weights: dict[int, float],
    resource_or_portfolio_id: str,
    resource_kind: str,
    perturbation_dimension: PerturbationDimension,
) -> Callable[[PerturbationVector], RiskSampleSeries]:
    """The perturbed-system trial provider: each ``PerturbationVector``
    modifies the REAL target resource (storage / renewable / hydro) and
    re-runs the resource pipeline, so energy_only really enters SOC and
    storage dispatch. The candidate object identity is the target id.

    The resource-layer ``MriResourceError`` is converted into the solver
    boundary ``MriConfigError``: an explicit ``MRI_PHYSICAL_DOMAIN_VIOLATION``
    is the ONLY fallback trigger; every other code (unknown / unsupported
    kind) keeps its original code as a config error."""

    def provider(vector: PerturbationVector) -> RiskSampleSeries:
        try:
            trial_tables = perturb_resource_tables(
                base_tables,
                resource_kind=resource_kind,
                resource_or_portfolio_id=resource_or_portfolio_id,
                perturbation_dimension=perturbation_dimension,
                delta_power_mw=vector.delta_power_mw,
                delta_energy_mwh=vector.delta_energy_mwh,
            )
        except MriResourceError as exc:
            raise MriConfigError(exc.code, exc.message) from exc
        evidence = run_resource_pipeline(
            bank=bank,
            tables=trial_tables,
            config=rrc,
            run_id=f"{run_id}_{vector.scalar_step:.4f}",
        )
        return risk_series_from_evidence(
            evidence,
            metric,
            bank,
            shared_ids,
            frozenset({resource_or_portfolio_id}),
            weather_years,
            year_weights,
            portfolio_id=MRI_PORTFOLIO_ID,
        )

    return provider


def make_perfect_capacity_provider(
    *,
    bank: Any,
    base_tables: dict[str, pd.DataFrame],
    rrc: ResourceRunConfig,
    metric: RiskMetric,
    shared_ids: frozenset[str],
    run_id: str,
    weather_years: tuple[int, ...],
    year_weights: dict[int, float],
) -> Callable[[float], RiskSampleSeries]:
    """The S+F perfect-capacity provider used as the rMRI denominator
    (kernel perfect-capacity input; solver-computed, not configured)."""

    def provider(capacity_mw: float) -> RiskSampleSeries:
        trial_config = dataclasses.replace(rrc, perfect_capacity_mw=float(capacity_mw))
        evidence = run_resource_pipeline(
            bank=bank,
            tables=base_tables,
            config=trial_config,
            run_id=f"{run_id}_F{float(capacity_mw):.4f}",
        )
        return risk_series_from_evidence(
            evidence,
            metric,
            bank,
            shared_ids,
            frozenset({"PERFECT_F"}),
            weather_years,
            year_weights,
            portfolio_id=MRI_PORTFOLIO_ID,
        )

    return provider


__all__ = [
    "MRI_PORTFOLIO_ID",
    "MRI_RESOURCE_KIND_UNSUPPORTED",
    "P1_MRI_RESOURCE_UNKNOWN",
    "MriResourceError",
    "MriTargetResolution",
    "make_perfect_capacity_provider",
    "make_perturbed_trial_provider",
    "perturb_resource_tables",
    "resolve_mri_target",
    "validate_declared_duration",
]
