"""Shared candidate resource resolution and base-system construction
(T17 round-1).

One stable service that resolves the candidate selection against the
frozen standard tables by OBJECT IDENTITY (never a hard-coded resource
naming rule inside EFC) and builds the base system S by REMOVING the
candidate's special state:

- renewable (wind/PV): removes the selected ``resource_group_id`` rows
  from ``renewable_hourly`` and ``renewable_capacities``;
- storage: removes the selected ``storage_id`` rows from
  ``storage_units``;
- hydro: removes the selected ``hydro_id`` rows from ``hydro_units``,
  ``hydro_hourly`` and ``hydro_energy_budgets``;
- combinations of the above: every matched identity is removed once.

The candidate nominal / max-instantaneous-output capacity is resolved
from the SAME tables (nameplate installed capacity for wind/PV, max
discharge power for storage, declared capacity for hydro) and feeds the
EFC search upper bound.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import pandas as pd

from capacity_credit.application.p1_workflow import P1WorkflowError
from capacity_credit.contracts.elcc_method_config import ElccMethodCandidateConfig

P1_EFC_CANDIDATE_UNKNOWN = "P1_EFC_CANDIDATE_UNKNOWN"
P1_EFC_CANDIDATE_CAPACITY_INCONSISTENT = "P1_EFC_CANDIDATE_CAPACITY_INCONSISTENT"


@dataclass(frozen=True)
class CandidateResolution:
    """The resolved candidate selection and the base system S."""

    selected_ids: frozenset[str]
    kinds: frozenset[str]
    candidate_tables: dict[str, pd.DataFrame]
    base_tables: dict[str, pd.DataFrame]
    candidate_capacity_mw: float


def _resolve_renewable(base: dict[str, pd.DataFrame], ids: set[str]) -> set[str]:
    if "renewable_capacities" not in base:
        return set()
    caps = base["renewable_capacities"]
    matched = set(caps["resource_group_id"].astype(str)) & ids
    if not matched:
        return set()
    base["renewable_hourly"] = base["renewable_hourly"][
        ~base["renewable_hourly"]["resource_group_id"].astype(str).isin(matched)
    ]
    base["renewable_capacities"] = caps[~caps["resource_group_id"].astype(str).isin(matched)]
    return matched


def _resolve_storage(base: dict[str, pd.DataFrame], ids: set[str]) -> set[str]:
    if "storage_units" not in base:
        return set()
    storage = base["storage_units"]
    matched = set(storage["storage_id"].astype(str)) & ids
    if not matched:
        return set()
    base["storage_units"] = storage[~storage["storage_id"].astype(str).isin(matched)]
    return matched


def _resolve_hydro(base: dict[str, pd.DataFrame], ids: set[str]) -> set[str]:
    if "hydro_units" not in base:
        return set()
    hydro = base["hydro_units"]
    matched = set(hydro["hydro_id"].astype(str)) & ids
    if not matched:
        return set()
    base["hydro_units"] = hydro[~hydro["hydro_id"].astype(str).isin(matched)]
    for name in ("hydro_hourly", "hydro_energy_budgets"):
        if name in base:
            base[name] = base[name][~base[name]["hydro_id"].astype(str).isin(matched)]
    return matched


def _constant_renewable_capacity(
    caps: pd.DataFrame,
    selected: frozenset[str],
) -> dict[str, float]:
    """Candidate renewable nameplate capacity: CONSTANT across weather
    years (the approved ELCC cross-year consistency rule). A group whose
    installed capacity differs across years is a stable block - it is
    never silently summed over the year rows."""
    constant: dict[str, float] = {}
    for group in sorted(selected):
        rows = caps[caps["resource_group_id"].astype(str) == group]
        values = rows["installed_capacity_mw"].astype(float)
        if values.empty:
            continue
        first = float(values.iloc[0])
        if (values - first).abs().max() > 1e-6:
            raise P1WorkflowError(
                P1_EFC_CANDIDATE_CAPACITY_INCONSISTENT,
                f"candidate renewable {group!r} installed capacity differs "
                f"across weather years ({sorted(set(values.tolist()))}); "
                "cross-year capacity must be constant (never summed)",
            )
        constant[group] = first
    return constant


def _candidate_capacity_mw(
    tables: dict[str, pd.DataFrame],
    selected: frozenset[str],
    kinds: frozenset[str],
) -> float:
    """Resolved candidate capacity: nameplate installed capacity for
    wind/PV (CONSTANT across weather years, never summed over years),
    max discharge power for storage, declared capacity for hydro;
    combinations sum the contributions over MEMBERS (not over a member's
    year records)."""
    total = 0.0
    if "renewable" in kinds and "renewable_capacities" in tables:
        caps = tables["renewable_capacities"]
        constant = _constant_renewable_capacity(caps, selected)
        total += sum(constant[group] for group in sorted(constant))
    if "storage" in kinds and "storage_units" in tables:
        storage = tables["storage_units"]
        rows = storage[storage["storage_id"].astype(str).isin(selected)]
        total += float(rows["discharge_power_mw"].astype(float).sum())
    if "hydro" in kinds and "hydro_units" in tables:
        hydro = tables["hydro_units"]
        rows = hydro[hydro["hydro_id"].astype(str).isin(selected)]
        total += float(rows["capacity_mw"].astype(float).sum())
    return float(total)


def _ensure_nonempty_renewable(base: dict[str, pd.DataFrame]) -> None:
    """Keep the REQUIRED renewable standard tables non-empty for the
    base system S. When the candidate was the only renewable group, add
    a zero-capacity placeholder row per weather year (empty renewable
    fleet representation; output 0 MW, capacity_factor 0)."""
    if "renewable_hourly" not in base or not base["renewable_hourly"].empty:
        return
    load = base["load_hourly"]
    stamps = pd.to_datetime(load["timestamp"])
    scenario = str(load["scenario_id"].iloc[0])
    region = str(load["region_id"].iloc[0])
    frames: list[pd.DataFrame] = []
    cap_rows: list[dict[str, object]] = []
    for year in sorted({int(y) for y in stamps.dt.year.unique()}):
        axis = stamps[stamps.dt.year == year]
        hours = len(axis)
        frames.append(
            pd.DataFrame(
                {
                    "record_id": [f"ZERO_REN_BASE_{year}_{h}" for h in range(hours)],
                    "resource_group_id": ["ZERO_RENEWABLE_BASE"] * hours,
                    "scenario_id": [scenario] * hours,
                    "weather_year": [str(year)] * hours,
                    "timestamp": axis,
                    "generation_basis": ["available"] * hours,
                    "available_generation_mw": [0.0] * hours,
                    "actual_generation_mw": [0.0] * hours,
                    "capacity_factor": [0.0] * hours,
                    "installed_capacity_mw": [0.0] * hours,
                    "source_id": ["zero_renewable_base"] * hours,
                }
            )
        )
        cap_rows.append(
            {
                "record_id": f"ZERO_REN_BASE_CAP_{year}",
                "resource_group_id": "ZERO_RENEWABLE_BASE",
                "technology": "wind_onshore",
                "region_id": region,
                "year": year,
                "installed_capacity_mw": 0.0,
                "capacity_basis": "nameplate",
                "source_id": "zero_renewable_base",
            }
        )
    base["renewable_hourly"] = pd.concat(frames, ignore_index=True)
    base["renewable_capacities"] = pd.DataFrame(cap_rows)


def resolve_candidate_and_base_tables(
    tables: dict[str, pd.DataFrame],
    candidate: ElccMethodCandidateConfig,
) -> CandidateResolution:
    """Resolve the candidate selection by object identity and build the
    base system S (candidate special state removed). Both the candidate
    tables (S+X) and the base tables (S) come from the SAME frozen
    standard tables; the candidate config uses the shared
    ``ElccMethodCandidateConfig`` selection form (single group id or
    member id list) with NO EFC-specific naming rule."""
    selected = frozenset(candidate.selected_group_ids())
    base = copy.deepcopy(tables)
    kinds: set[str] = set()
    remaining = set(selected)

    matched = _resolve_renewable(base, remaining)
    if matched:
        kinds.add("renewable")
        remaining -= matched
    matched = _resolve_storage(base, remaining)
    if matched:
        kinds.add("storage")
        remaining -= matched
    matched = _resolve_hydro(base, remaining)
    if matched:
        kinds.add("hydro")
        remaining -= matched

    if remaining:
        raise P1WorkflowError(
            P1_EFC_CANDIDATE_UNKNOWN,
            f"candidate ids not found in the frozen standard tables: {sorted(remaining)}",
        )
    if not kinds:
        raise P1WorkflowError(
            P1_EFC_CANDIDATE_UNKNOWN,
            "no candidate resource kind resolved from the selection",
        )
    # a candidate kind that removed ALL of its rows leaves an EMPTY
    # optional table behind; drop it so the base system S stays a valid
    # standard table set (never a DQ01_FILE_EMPTY)
    for name in ("hydro_units", "hydro_hourly", "hydro_energy_budgets", "storage_units"):
        if name in base and base[name].empty:
            del base[name]
    # renewable_hourly / renewable_capacities are REQUIRED standard
    # tables; when the candidate was the only renewable group the base
    # system has no renewable fleet, so keep a single zero-capacity
    # placeholder row per weather year (empty renewable fleet - the
    # candidate's contribution is fully removed)
    _ensure_nonempty_renewable(base)
    capacity = _candidate_capacity_mw(tables, selected, frozenset(kinds))
    return CandidateResolution(
        selected_ids=selected,
        kinds=frozenset(kinds),
        candidate_tables=dict(tables),
        base_tables=base,
        candidate_capacity_mw=capacity,
    )


__all__ = [
    "P1_EFC_CANDIDATE_CAPACITY_INCONSISTENT",
    "P1_EFC_CANDIDATE_UNKNOWN",
    "CandidateResolution",
    "resolve_candidate_and_base_tables",
]
