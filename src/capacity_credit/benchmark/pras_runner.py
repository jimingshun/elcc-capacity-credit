"""T22-B shared Python benchmark result chain (P-01..P-03).

ONE python benchmark entry: benchmarks/pras/python/p01_run.py is the
only script; run_python_case() dispatches:

- P-01 conventional -> the formal adequacy kernel (AdequacyEngine);
- P-02 VRE / P-03 storage -> the PRODUCTION resource chain
  (run_resource_pipeline + risk_series_from_evidence +
  make_perfect_trial_provider + run_efc) over standard input tables
  converted from the controlled case.yaml.

The benchmark boundary ONLY converts the controlled case.yaml into
standard input tables; it never re-implements risk formulas, storage
dispatch, EFC search or convergence algorithms.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

from capacity_credit.application.efc_components import (
    resolve_probability_year_weights,
    resource_run_config_from,
)

# THE single shared baseline evidence file list (T22-B4) - the same
# tuple consumed by tools/check_p5_release.py (compute_scripts_hash) and
# p01_run.py. No second copy of the list exists.
from capacity_credit.benchmark.pras_adapter import (
    PRAS_EVIDENCE_FILES,
    PrasMappingError,
    case_input_hash,
    load_case_definition,
    prepare_comparison_timeline,
)
from capacity_credit.benchmark.pras_results import (
    compare_capacity_values,
    compare_risk_estimates,
)
from capacity_credit.contracts.enums import RiskMetric
from capacity_credit.engine.adequacy import AdequacyEngine, compute_batch_metrics
from capacity_credit.engine.convergence import ConvergenceConfig
from capacity_credit.scenarios.scenario_bank import ScenarioObject, build_scenario_bank
from capacity_credit.scenarios.thermal_states import ThermalParams

KEY_PRODUCTION_FILES = PRAS_EVIDENCE_FILES


class StageEvent(NamedTuple):
    """T22-M: ONE coarse-grained business-stage event. Emitted ONLY at
    stage boundaries - never inside sampling loops, the adequacy kernel,
    the EFC search or the storage dispatch. Carries no fabricated
    percentage or ETA; the six fields mirror the CC_PROGRESS_V1 text
    protocol shared with the Julia side."""

    case_id: str
    side: str
    stage: str
    state: str
    step: int
    total: int

_SCENARIO_ID = "BASE_2025"
_TZ = "Asia/Shanghai"
_TEACHING_CONVERGENCE = ConvergenceConfig(
    batch_size=2,
    min_samples=2,
    max_samples=100000,
    confidence_level=0.95,
    relative_half_width_tolerance=1e6,
    absolute_half_width_tolerance=1e6,
    near_zero_threshold=1e-9,
    min_nonzero_samples=1,
    recent_batch_window=1,
    recent_batch_stability_tolerance=1e6,
    provisional_defaults=True,
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def production_file_hashes(root: Path) -> dict[str, str]:
    """THE production file hash set for the python benchmark entry."""
    return {rel: _file_sha256(root / rel) for rel in KEY_PRODUCTION_FILES}


def _stats(values: np.ndarray) -> dict[str, float | int]:
    mean = float(values.mean())
    se = float(values.std(ddof=1) / math.sqrt(len(values))) if len(values) > 1 else 0.0
    return {"mean": mean, "se": se, "n": len(values)}


def _hour_axis(year: int, hours: int) -> pd.DatetimeIndex:
    start = pd.Timestamp(year, 1, 1, tz=_TZ)
    return pd.date_range(start, periods=hours, freq="h")


def build_standard_tables(
    case: Any,
    timeline: Any,
    *,
    wind_mw: np.ndarray | None = None,
    include_storage: bool = True,
) -> dict[str, pd.DataFrame]:
    """Benchmark boundary: convert the controlled case.yaml into
    standard input tables (load_hourly, thermal, renewable, storage,
    intertie, weights). include_storage=False OMITS the storage table
    entirely (a zero-capacity record is ILLEGAL for the formal
    StorageSpec); include_storage=True uses the real case parameters.
    No algorithm is re-implemented here."""
    hours = timeline.effective_horizon_hours
    axis = _hour_axis(case.weather_year, hours)
    year = case.weather_year
    load = np.asarray(timeline.load_profile_mw, dtype=float)
    if len(load) != hours:
        raise PrasMappingError(
            "TIMELINE_PROTOCOL_INCONSISTENT",
            "expanded load length != effective horizon",
        )
    tables: dict[str, pd.DataFrame] = {}
    tables["load_hourly"] = pd.DataFrame({
        "record_id": [f"L{year}_{h}" for h in range(hours)],
        "region_id": ["REGION_A"] * hours,
        "scenario_id": [_SCENARIO_ID] * hours,
        "weather_year": [str(year)] * hours,
        "timestamp": axis,
        "load_mw": load,
        "load_basis": ["measured"] * hours,
        "is_forecast": ["false"] * hours,
        "source_id": ["teaching_benchmark"] * hours,
        "data_status": ["complete"] * hours,
        "quality_flag": ["good"] * hours,
    })
    units = list(case.units)
    tables["thermal_units"] = pd.DataFrame([
        {
            "record_id": f"TU{index}",
            "unit_id": str(u["id"]),
            "plant_id": "PLANT_1",
            "plant_name": "p",
            "unit_name": str(u["id"]),
            "technology_code": "CCGT",
            "technology_label_raw": "CCGT",
            "capacity_mw": float(u["capacity_mw"]),
            "capacity_basis": "nameplate",
            "region_id": "REGION_A",
            "source_id": "teaching_benchmark",
        }
        for index, u in enumerate(units)
    ])
    tables["thermal_reliability_classes"] = pd.DataFrame([
        {
            "record_id": f"TRC{index}",
            "reliability_class_id": f"CLASS_{index}",
            "class_name": f"c{index}",
            "model_type": "sequential_two_state",
            "authoritative_parameter_set": "for_only",
            "for_rate": float(u.get("for", 0.1)),
            "source_id": "teaching_benchmark",
        }
        for index, u in enumerate(units)
    ])
    tables["thermal_unit_reliability_map"] = pd.DataFrame([
        {
            "record_id": f"TUM{index}",
            "unit_id": str(u["id"]),
            "reliability_class_id": f"CLASS_{index}",
            "mapping_method": "explicit",
            "confidence": "high",
            "source_id": "teaching_benchmark",
        }
        for index, u in enumerate(units)
    ])
    tables["intertie_hourly"] = pd.DataFrame({
        "record_id": [f"I{year}_{h}" for h in range(hours)],
        "intertie_id": ["TIE_01"] * hours,
        "scenario_id": [_SCENARIO_ID] * hours,
        "timestamp": axis,
        "net_import_mw": [0.0] * hours,
        "scheduled_import_mw": [0.0] * hours,
        "scheduled_export_mw": [0.0] * hours,
        "source_id": ["teaching_benchmark"] * hours,
    })
    tables["interties"] = pd.DataFrame([
        {
            "record_id": "IT1",
            "intertie_id": "TIE_01",
            "intertie_name": "teaching tie",
            "intertie_type": "ac",
            "rated_capacity_mw": 500.0,
            "normal_import_limit_mw": 500.0,
            "normal_export_limit_mw": 500.0,
            "availability_model": "fixed_available",
            "source_id": "teaching_benchmark",
        }
    ])
    tables["weather_year_weight_sets"] = pd.DataFrame([
        {
            "record_id": f"WY{year}",
            "weight_set_id": "w1",
            "weather_year": str(year),
            "weight": 1.0,
            "source_id": "teaching_benchmark",
            "data_status": "assumed",
            "note": "benchmark single-year weight",
        }
    ])
    if case.vre is None:
        # no-vre cases still carry a NON-EMPTY zero renewable table so
        # the standard DQ layer passes (same axis as load_hourly)
        tables["renewable_hourly"] = pd.DataFrame({
            "record_id": [f"R{year}_{h}" for h in range(hours)],
            "resource_group_id": ["NO_VRE"] * hours,
            "scenario_id": [_SCENARIO_ID] * hours,
            "weather_year": [str(year)] * hours,
            "timestamp": axis,
            "generation_basis": ["available"] * hours,
            "available_generation_mw": [0.0] * hours,
            "actual_generation_mw": [0.0] * hours,
            "capacity_factor": [0.0] * hours,
            "installed_capacity_mw": [0.0] * hours,
            "source_id": ["teaching_benchmark"] * hours,
        })
        tables["renewable_capacities"] = pd.DataFrame([
            {
                "record_id": f"RC{year}",
                "resource_group_id": "NO_VRE",
                "technology": "wind_onshore",
                "region_id": "REGION_A",
                "year": str(year),
                "installed_capacity_mw": 0.0,
                "capacity_basis": "nameplate",
                "source_id": "teaching_benchmark",
            }
        ])
    else:
        profile = wind_mw if wind_mw is not None else np.zeros(hours, dtype=float)
        profile = np.asarray(profile, dtype=float)
        if profile.size != hours:
            raise PrasMappingError(
                "TIMELINE_PROTOCOL_INCONSISTENT",
                "wind curve length != effective horizon",
            )
        group = str(case.vre["id"])
        installed = float(case.vre["installed_capacity_mw"]);
        cf = profile / installed if installed > 0 else np.zeros(hours)
        tables["renewable_hourly"] = pd.DataFrame({
            "record_id": [f"R{year}_{h}" for h in range(hours)],
            "resource_group_id": [group] * hours,
            "scenario_id": [_SCENARIO_ID] * hours,
            "weather_year": [str(year)] * hours,
            "timestamp": axis,
            "generation_basis": ["available"] * hours,
            "available_generation_mw": profile,
            "actual_generation_mw": profile,
            "capacity_factor": cf,
            "installed_capacity_mw": [installed] * hours,
            "source_id": ["teaching_benchmark"] * hours,
        })
        tables["renewable_capacities"] = pd.DataFrame([
            {
                "record_id": f"RC{year}",
                "resource_group_id": group,
                "technology": "wind_onshore",
                "region_id": "REGION_A",
                "year": str(year),
                "installed_capacity_mw": installed,
                "capacity_basis": "nameplate",
                "source_id": "teaching_benchmark",
            }
        ])
    if case.storage is not None and include_storage:
        st = case.storage
        power = float(st["power_mw"])
        energy = float(st["energy_mwh"])
        tables["storage_units"] = pd.DataFrame([
            {
                "record_id": "ST1",
                "storage_id": str(st["id"]),
                "storage_name": str(st["id"]),
                "storage_type": "battery",
                "charge_power_mw": power,
                "discharge_power_mw": power,
                "energy_capacity_mwh": energy,
                "soc_min_fraction": float(st["soc_min"]),
                "soc_max_fraction": float(st["soc_max"]),
                "self_discharge_per_hour": 0.0,
                "charge_efficiency": float(st["charge_efficiency"]),
                "discharge_efficiency": float(st["discharge_efficiency"]),
                "availability_fraction": 1.0,
                "region_id": "REGION_A",
                "source_id": "teaching_benchmark",
            }
        ])
    return tables


def _build_bank(case: Any, base_dir: Path, *, samples: int | None = None) -> Any:
    objects = [
        ScenarioObject(
            str(u["id"]),
            "thermal",
            float(u["capacity_mw"]),
            ThermalParams.independent_hourly(for_rate=float(u["for"])),
        )
        for u in case.units
    ]
    return build_scenario_bank(
        root_seed=case.random_seed,
        objects=objects,
        samples=samples if samples is not None else case.sample_count,
        weather_years=[case.weather_year],
        year_roles={case.weather_year: "probability_baseline"},
        timezone=case.timezone,
        base_dir=base_dir,
    )


def _risk_and_efc(
    case: Any,
    timeline: Any,
    base_dir: Path,
    *,
    samples: int | None = None,
    stage_events: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """P-02/P-03 formal chain through the SINGLE shared EFC
    computation (application.efc_components.compute_efc_evidence):
    baseline (NO candidate storage / zero wind) + augmented (real
    storage / wind) resource runs + risk series + EFC solver. For
    P-03 the augmented run binds the EXPLICIT zero initial SOC
    through the formal StorageSocConfig (no implicit warm-up, no
    burn-in); the baseline carries NO storage table so the SOC
    config is inert there. SOC evidence payload is RUN-DERIVED
    from ResourceRunEvidence.storage_evidence, never a case.yaml
    copy."""
    from capacity_credit.application.efc_components import compute_efc_evidence
    from capacity_credit.contracts.enums import StorageSocInitializationMode
    from capacity_credit.contracts.storage_soc_config import StorageSocConfig
    from capacity_credit.solvers.efc import EfcSolverConfig
    from capacity_credit.solvers.equal_risk import EqualRiskSearchConfig

    metric = RiskMetric.EUE_MWH_PER_YEAR
    is_storage = case.storage is not None
    n_samples = samples if samples is not None else case.sample_count
    if stage_events is not None:
        stage_events("system_build", "start")
    bank = _build_bank(case, base_dir, samples=n_samples)
    tables_aug = build_standard_tables(
        case, timeline,
        wind_mw=(np.asarray(timeline.vre_profile_mw, dtype=float)
                  if timeline.vre_profile_mw is not None else None),
        include_storage=True,
    )
    tables_base = build_standard_tables(
        case, timeline,
        wind_mw=(np.zeros(timeline.effective_horizon_hours, dtype=float)
                  if timeline.vre_profile_mw is not None else None),
        include_storage=not is_storage,  # P-03 baseline: NO storage table
    )
    weather_years = tuple(sorted(int(y) for y in bank.index["weather_years"]))
    weights = resolve_probability_year_weights(tables_aug, "w1", weather_years)
    storage_soc = None
    if is_storage:
        # explicit zero initial SOC bound into the FORMAL config:
        # _initialize_year short-circuits on explicit_initial_soc_mwh
        # (no weather-cycle warm-up, no burn-in); burnin stays None.
        storage_soc = StorageSocConfig(
            initialization_mode=StorageSocInitializationMode.WEATHER_CYCLE_WARMUP,
            explicit_initial_soc_mwh={str(case.storage["id"]): 0.0},
        )
    rrc = resource_run_config_from(
        bank,
        samples=n_samples,
        primary_risk_metric=metric,
        scenario_id=_SCENARIO_ID,
        weather_years=weather_years,
        year_weights=weights,
        storage_soc=storage_soc,
    )
    if stage_events is not None:
        stage_events("system_build", "complete")
    shared_ids = frozenset({str(o["object_id"]) for o in bank.index["objects"]})
    if case.vre is not None:
        upper = float(case.vre["installed_capacity_mw"])
    elif case.storage is not None:
        upper = float(case.storage["power_mw"])
    else:
        raise PrasMappingError("CASE_ID_UNKNOWN", "capacity-value case requires vre or storage")
    computed = compute_efc_evidence(
        bank=bank,
        tables_base=tables_base,
        tables_candidate=tables_aug,
        rrc=rrc,
        metric=metric,
        weather_years=weather_years,
        year_weights=weights,
        shared_ids=shared_ids,
        candidate_ids=frozenset(),
        upper_bound_mw=upper,
        run_id="efc_benchmark",
        stage_callback=stage_events,
        efc_config=EfcSolverConfig(
            search=EqualRiskSearchConfig(
                lower_bound_mw=0.0,
                physical_upper_bound_mw=upper,
                capacity_tolerance_mw=1.0,
                max_iterations=40,
                monotonic_direction="risk_decreases_with_capacity",
                confidence_level=0.95,
            ),
            upper_bound_mw=upper,
            upper_bound_source="declared_max_instantaneous_output",
            perfect_capacity_definition_version="perfect_v1",
        ),
        convergence_config=_TEACHING_CONVERGENCE,
    )
    efc = computed["efc"]
    aug_ev = computed["candidate_ev"]
    base_series = computed["base_series"]
    reference = computed["reference"]

    def _risk_block(series: Any, label: str) -> dict[str, Any]:
        values = np.asarray(list(series.values.values()), dtype=float)
        return {"label": label, "eue": _stats(values)}

    payload: dict[str, Any] = {
        "risk": {
            "baseline_zero_" + ("storage" if is_storage else "wind"): _risk_block(base_series, "baseline"),
            "augmented_formal_" + ("storage" if is_storage else "wind"): _risk_block(reference, "augmented"),
        },
        "capacity_value": _capacity_value_payload(efc, upper),
    }
    if is_storage:
        payload["storage_evidence"] = _storage_evidence_payload(aug_ev, timeline)
    return payload


def _capacity_value_payload(efc: Any, upper: float) -> dict[str, Any]:
    """THE python EFC capacity-value payload writer (T22-B4): the value
    is an INTERVAL from the real equal-risk search bracket. The EUE
    empirical risk resolution (MWh/yr) is recorded in a separate
    unit-qualified field - it is NEVER written as an MW standard error
    (a point standard_error_mw is never produced by the EFC chain)."""
    return {
        "metric": "efc_mw",
        "value_kind": "interval",
        "estimate_mw": efc.estimate_mw,
        "bracket_low_mw": efc.bracket_low_mw,
        "bracket_high_mw": efc.bracket_high_mw,
        "empirical_risk_resolution_eue_mwh_per_year": efc.empirical_risk_resolution,
        "matched_risk": efc.matched_risk,
        "candidate_risk": efc.candidate_risk,
        "capacity_value_mode": "efc_perfect_capacity",
        "search_capacity_tolerance_mw": 1.0,
        "upper_bound_mw": upper,
    }

def _storage_evidence_payload(aug_ev: Any, timeline: Any) -> dict[str, Any]:
    """RUN-DERIVED storage evidence: initial-state policy, burn-in
    length, warm-up diagnostics and the FIRST scoring-period SOC
    from the real dispatch trajectory (ResourceRunEvidence.
    storage_evidence). The case.yaml declaration is recorded ONLY
    under the "declared" key and never presented as run evidence."""
    if not aug_ev.storage_evidence:
        raise PrasMappingError(
            "STORAGE_EVIDENCE_MISSING",
            "augmented storage run produced no storage_evidence",
        )
    sample_evidence = aug_ev.storage_evidence[0]
    first_hour = next(
        (r for r in sample_evidence.dispatch.hours if r.hour_index == 0),
        None,
    )
    first_period_soc = None
    if first_hour is not None:
        first_period_soc = sum(
            u.step.soc_before_mwh for u in first_hour.portfolio.units
        )
    return {
        "declared": {
            "initial_soc": 0.0,
            "pre_history_hours": 0,
            "burnin_hours": 0,
        },
        "initial_state_policy": sample_evidence.initial_state_policy,
        "first_period_soc_mwh": first_period_soc,
        "burnin_length_hours": sample_evidence.burnin_length_hours,
        "warmup_diagnostics": list(sample_evidence.warmup_diagnostics),
        "scoring_start_hour": 0,
        "scoring_end_hour": timeline.effective_horizon_hours,
        "scoring_window": "full_formal_timeline",
        "effective_horizon_hours": timeline.effective_horizon_hours,
    }

def _run_conventional(
    case: Any,
    timeline: Any,
    base_dir: Path,
    *,
    stage_events: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """P-01: the existing formal adequacy kernel (AdequacyEngine).
    T22-M: system_build wraps the REAL scenario-bank modelling and
    baseline_risk wraps the REAL adequacy risk computation."""
    if stage_events is not None:
        stage_events("system_build", "start")
    load_year = np.asarray(timeline.load_profile_mw, dtype=float)
    objects = [
        ScenarioObject(
            str(u["id"]), "thermal", float(u["capacity_mw"]),
            ThermalParams.independent_hourly(for_rate=float(u["for"])),
        )
        for u in case.units
    ]
    bank = build_scenario_bank(
        root_seed=case.random_seed, objects=objects, samples=case.sample_count,
        weather_years=[case.weather_year],
        year_roles={case.weather_year: "probability_baseline"},
        timezone=case.timezone, base_dir=base_dir,
    )
    load = {case.weather_year: load_year}
    config = ConvergenceConfig(
        batch_size=500, min_samples=case.sample_count, max_samples=case.sample_count,
        confidence_level=0.95, relative_half_width_tolerance=0.5,
        absolute_half_width_tolerance=0.001, near_zero_threshold=0.05,
        min_nonzero_samples=3, recent_batch_window=3,
        recent_batch_stability_tolerance=1.0, use_student_t=True,
        normal_interval_min_samples=200,
    )
    if stage_events is not None:
        stage_events("system_build", "complete")
    if stage_events is not None:
        stage_events("baseline_risk", "start")
    engine = AdequacyEngine(bank=bank, load_by_year=load, root_seed=case.random_seed)
    lolh_samples: list[float] = []
    eue_samples: list[float] = []
    for batch in engine.iter_batches(config.batch_size, case.sample_count):
        metrics = compute_batch_metrics(bank, batch, load, root_seed=case.random_seed)
        for sample in metrics["samples"][case.weather_year]:
            lolh_samples.append(float(sample.lolh_hours_per_year))
            eue_samples.append(float(sample.eue_mwh_per_year))
    if stage_events is not None:
        stage_events("baseline_risk", "complete")
    return {
        "risk": {
            "baseline_zero_wind": {"label": "base", "lolh": _stats(np.asarray(lolh_samples)), "eue": _stats(np.asarray(eue_samples))},
        },
        "capacity_value": None,
    }


def run_python_case(
    case_rel: str,
    out_path: Path,
    base_dir: Path,
    *,
    progress: Callable[[StageEvent], None] | None = None,
) -> dict[str, Any]:
    """THE single python benchmark entry (P-01..P-03 dispatch).
    Writes the payload JSON (parent dir auto-created) and returns it.
    T22-M: an OPTIONAL coarse-stage progress callback may be supplied -
    without one the behaviour and the result are byte-for-byte
    unchanged; with one, ONE event per coarse stage boundary is emitted
    (input_validation, system_build, baseline_risk, augmented_risk,
    efc_search, output_publish, completed; P-01 skips the two
    not-applicable stages). Events never enter python_result.json."""
    case = load_case_definition(case_rel)
    total = 5 if case.case_id == "P-01" else 7
    stage_step = {
        "input_validation": 1,
        "system_build": 2,
        "baseline_risk": 3,
        "augmented_risk": 4,
        "efc_search": 5,
        "output_publish": 4 if case.case_id == "P-01" else 6,
        "completed": 5 if case.case_id == "P-01" else 7,
    }

    def _emit(stage: str, state: str) -> None:
        # T22-M: coarse stage events are start/complete PAIRED at the
        # real computation boundaries; a failing progress sink (broken
        # pipe / OSError) never interrupts the formal computation -
        # events simply stop being delivered.
        nonlocal progress
        if progress is None:
            return
        try:
            progress(
                StageEvent(
                    str(case.case_id), "python", stage, state,
                    stage_step[stage], total,
                )
            )
        except OSError:
            progress = None

    # input_validation wraps the FORMAL timeline/comparison-protocol
    # validation AFTER case loading (the stage never claims to cover
    # the unobserved case-file loading itself)
    _emit("input_validation", "start")
    timeline = prepare_comparison_timeline(case)
    _emit("input_validation", "complete")
    if case.case_id == "P-01":
        body = _run_conventional(case, timeline, base_dir, stage_events=_emit)
    elif case.case_id in ("P-02", "P-03"):
        body = _risk_and_efc(case, timeline, base_dir, stage_events=_emit)
    else:
        raise PrasMappingError(
            "CASE_ID_UNKNOWN",
            f"unsupported benchmark case_id {case.case_id!r}",
        )
    payload: dict[str, Any] = {
        "case_id": case.case_id,
        "case_input_hash": case_input_hash(case),
        "annualization_protocol": timeline.protocol_id,
        "base_horizon_hours": timeline.base_horizon_hours,
        "actual_horizon_hours": timeline.effective_horizon_hours,
        "profile_repeat_count": timeline.profile_repeat_count,
        "target_horizon_hours": timeline.effective_horizon_hours,
        "time_resolution": case.time_resolution,
        "timezone": case.timezone,
        "random_seed": case.random_seed,
        "sample_count": case.sample_count,
        "comparison_protocol": dict(case.comparison_protocol),
        "key_production_file_hashes": production_file_hashes(Path(__file__).resolve().parents[3]),
        **body,
    }
    _emit("output_publish", "start")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _emit("output_publish", "complete")
    _emit("completed", "start")
    _emit("completed", "complete")
    return payload



_IDENTITY_FIELDS = (
    "case_id",
    "annualization_protocol",
    "base_horizon_hours",
    "actual_horizon_hours",
    "profile_repeat_count",
)


def _contract_identity(case: Any) -> dict[str, Any]:
    """THE current case contract the two sides must BOTH match:
    case_id plus the comparison timeline protocol fields and the
    sampling identity (from the SAME prepare_comparison_timeline
    abstraction that drives the actual runs)."""
    from capacity_credit.benchmark.pras_adapter import prepare_comparison_timeline

    timeline = prepare_comparison_timeline(case)
    return {
        "case_id": case.case_id,
        "annualization_protocol": timeline.protocol_id,
        "base_horizon_hours": timeline.base_horizon_hours,
        "actual_horizon_hours": timeline.effective_horizon_hours,
        "profile_repeat_count": timeline.profile_repeat_count,
        "sample_count": case.sample_count,
        "random_seed": case.random_seed,
    }


def _identity_value_ok(value: Any, *, numeric: bool) -> bool:
    """Strict, stable type check - NEVER str()-based comparison.
    Strings must be non-empty str; numerics must be int/float (not
    bool) and finite."""
    if numeric:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(float(value))
    return isinstance(value, str) and bool(value.strip())


def _identity_equal(left: Any, right: Any, *, numeric: bool) -> bool:
    """Numeric fields compare by VALUE (int/float cross-compare with
    exact integer semantics), string fields compare exactly."""
    if numeric:
        return float(left) == float(right)
    return bool(left == right)


def _validate_side_identity(python_payload: dict[str, Any], julia: dict[str, Any], case: Any) -> None:
    """THE two-sided result identity gate (sixth round): BEFORE any
    numerical comparison, EVERY identity field must
    (a) exist on BOTH sides with the correct type,
    (b) be MUTUALLY consistent, and
    (c) equal the CURRENT case contract (case_id / timeline
        protocol / horizon / repeat factor / sample_count / seed) -
        two sides agreeing on a WRONG value is still a block.
    The python case_input_hash must equal the current case input
    hash. Any anomaly is a stable PRAS_RESULT_IDENTITY_MISMATCH
    raised BEFORE the comparators run; the evidence bundle can
    never substitute this identity."""
    from capacity_credit.benchmark.pras_adapter import case_input_hash
    from capacity_credit.benchmark.pras_results import PrasComparisonError

    contract = _contract_identity(case)
    problems: list[str] = []
    for field in _IDENTITY_FIELDS:
        numeric = field not in ("case_id", "annualization_protocol")
        expected = contract[field]
        py = python_payload.get(field)
        jl = julia.get(field)
        if not _identity_value_ok(py, numeric=numeric):
            problems.append(f"python {field} missing or wrong type: {py!r}")
            continue
        if not _identity_value_ok(jl, numeric=numeric):
            problems.append(f"julia {field} missing or wrong type: {jl!r}")
            continue
        if not _identity_equal(py, jl, numeric=numeric):
            problems.append(f"{field} mismatch: python={py!r}, julia={jl!r}")
            continue
        if not _identity_equal(py, expected, numeric=numeric):
            problems.append(f"{field} not equal to the case contract: {py!r} != {expected!r}")
    for field, py_key, jl_key in (("sample_count", "sample_count", "sample_count"), ("random_seed", "random_seed", "seed")):
        expected = contract[field]
        py = python_payload.get(py_key)
        jl = julia.get(jl_key)
        if not _identity_value_ok(py, numeric=True):
            problems.append(f"python {field} missing or wrong type: {py!r}")
            continue
        if not _identity_value_ok(jl, numeric=True):
            problems.append(f"julia {field} missing or wrong type: {jl!r}")
            continue
        if not _identity_equal(py, jl, numeric=True):
            problems.append(f"{field} mismatch: python={py!r}, julia={jl!r}")
            continue
        if not _identity_equal(py, expected, numeric=True):
            problems.append(f"{field} not equal to the case contract: {py!r} != {expected!r}")
    current_hash = case_input_hash(case)
    py_hash = python_payload.get("case_input_hash")
    if not isinstance(py_hash, str) or not py_hash.strip():
        problems.append("python case_input_hash missing")
    elif py_hash != current_hash:
        problems.append("python case_input_hash stale (does not match the current case)")
    if problems:
        raise PrasComparisonError(
            "PRAS_RESULT_IDENTITY_MISMATCH",
            "result identity mismatch: " + "; ".join(problems),
        )

def compare_side_results(
    python_payload: dict[str, Any],
    julia_result_text: str,
    case: Any,
    evidence: Any,
) -> dict[str, Any]:
    """THE unique result assembly/comparison entry: converts the python
    formal result and the PRAS result.txt into RiskEstimate /
    CapacityValueEstimate and runs the production comparators
    (compare_risk_estimates then compare_capacity_values). PRAS bounds
    stay value_kind=interval - the midpoint is never a point estimate
    and the half-width is never a standard error."""
    from capacity_credit.benchmark.pras_results import (
        capacity_value_estimate_from_payload,
        parse_pras_result_text,
        risk_estimate_from_payload,
    )

    julia = parse_pras_result_text(julia_result_text)
    _validate_side_identity(python_payload, julia, case)
    # risk block / julia key selection follows the CASE (P-01 has no
    # augmented system; P-02/P-03 use their formal resource label)
    if case.case_id == "P-01":
        python_risk_block = python_payload["risk"]["baseline_zero_wind"]
        julia_risk_key = "risk_base_eue"
    elif case.case_id == "P-02":
        python_risk_block = python_payload["risk"]["augmented_formal_wind"]
        julia_risk_key = "risk_augmented_formal_wind_eue"
    elif case.case_id == "P-03":
        python_risk_block = python_payload["risk"]["augmented_formal_storage"]
        julia_risk_key = "risk_augmented_formal_storage_eue"
    else:
        raise PrasMappingError("CASE_ID_UNKNOWN", f"unsupported case_id {case.case_id!r}")
    from capacity_credit.benchmark.pras_results import RiskEstimate

    python_risk = RiskEstimate(
        metric="eue_mwh_per_year",
        estimate=float(python_risk_block["eue"]["mean"]),
        standard_error=float(python_risk_block["eue"]["se"]),
    )
    risk_cmp = compare_risk_estimates(
        python_risk,
        risk_estimate_from_payload(julia, "eue_mwh_per_year", julia_risk_key),
        case=case,
        evidence=evidence,
    )
    # capacity value: P-01 carries none (risk-only case); P-02/P-03
    # compare the python INTERVAL (real search bracket) against the
    # PRAS interval - risk resolution is never an MW standard error.
    cv_cmp = None
    if python_payload.get("capacity_value") is not None:
        python_cv = capacity_value_estimate_from_payload(python_payload["capacity_value"])
        julia_cv = capacity_value_estimate_from_payload(julia, interval=True)
        cv_cmp = compare_capacity_values(
            python_cv,
            julia_cv,
            risk_result=risk_cmp,
            case=case,
            evidence=evidence,
        )
    return {
        "risk": {
            "python": python_payload["risk"],
            "julia": julia,
            "comparison": {
                "status": risk_cmp.status.value,
                "difference": risk_cmp.difference,
                "tolerance": risk_cmp.tolerance,
                "reason": risk_cmp.reason,
            },
        },
        "capacity_value": {
            "python": {
                "estimate_mw": python_cv.estimate if cv_cmp is not None else None,
                "standard_error_mw": python_cv.standard_error if cv_cmp is not None else None,
            },
            "pras": {
                "value_kind": julia_cv.value_kind if cv_cmp is not None else None,
                "lower_bound_mw": julia_cv.lower_bound_mw if cv_cmp is not None else None,
                "upper_bound_mw": julia_cv.upper_bound_mw if cv_cmp is not None else None,
            },
            "comparison": {
                "status": cv_cmp.status.value if cv_cmp is not None else None,
                "difference": cv_cmp.difference if cv_cmp is not None else None,
                "tolerance": cv_cmp.tolerance if cv_cmp is not None else None,
                "reason": cv_cmp.reason if cv_cmp is not None else None,
            },
        },
    }


__all__ = [
    "KEY_PRODUCTION_FILES",
    "StageEvent",
    "build_standard_tables",
    "compare_side_results",
    "production_file_hashes",
    "run_python_case",
]
