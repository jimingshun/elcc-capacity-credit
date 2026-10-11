"""T14 storage chronology: SOC initialization, warm-up, burn-in, year
boundaries and scoring windows.

Module responsibilities (per the T14 architecture boundary):

- ``storage.py`` keeps the T13 single-interval physics and portfolio
  invariants (never re-implemented here); ``effective_soc_band`` is the
  SINGLE source of the effective SOC band consumed by warm-up /
  burn-in midpoints and thresholds - no drift-prone duplicate formula;
- ``storage_dispatch.py`` turns chronological signals into per-hour
  requests (all SOC transitions go through ``step_storage_portfolio``);
- this module organizes TIME: initial states, weather-cycle warm-up,
  stochastic sample burn-in, year-boundary carry-over /
  re-initialization and scoring-window boundaries.

- ``weather_cycle_warmup``: starts at the EFFECTIVE usable-band
  midpoint (reserved energy / hourly available energy respected),
  cycles the SAME year's weather/load/deterministic resource horizon
  (no forced outages), at most 5 cycles; per-unit convergence threshold
  ``max(0.001 x effective usable energy, 0.01 MWh)``; the portfolio
  converges only when EVERY unit satisfies the threshold; warm-up risk
  is never scored; always emits STORAGE_SOC_FAST_WARMUP;
  non-convergence emits STORAGE_SOC_WARMUP_NOT_CONVERGED and preserves
  the first/last SOC delta, cycle count and threshold as initial-SOC
  sensitivity evidence for T16 (never silently treated as converged).
- ``stochastic_sample_burnin``: per-sample pre-history evidence
  interface; burn-in risk is never scored; the sample spread fields are
  INFORMATIONAL distribution dispersion - they are NOT a burn-in
  convergence criterion and never drive the stability verdict.
- burn-in STABILITY comes exclusively from
  ``compare_burnin_lengths``: the same batch, the same random stream,
  two controlled burn-in lengths (from the typed config) are compared
  per-sample SOC, distribution statistics and the PAIRED scored EUE;
  a divergence beyond the configured tolerances emits
  STORAGE_SOC_BURNIN_UNSTABLE (warning -> at least valid_with_warning
  downstream).
- per-sample pairing: sample_index -> same-index pre-history stream ->
  same-index burn-in end SOC -> same-index scoring period ->
  same-index EUE evidence (``run_burnin_paired_scoring``); the result
  is aligned by sample_index, never "first / last / mean sample".
- year boundaries: continuous_multiyear initializes ONCE before the
  scoring period and carries SOC across every year boundary (never
  resetting to full / midpoint / zero); independent_weather_years
  re-initializes EVERY year and never inherits SOC between years;
  ``run_multi_year_dispatch`` is ONE deterministic trajectory, so the
  burn-in mode accepts exactly ONE sample (sample_count > 1 blocks with
  a stable code - multi-sample pairing belongs to
  ``run_burnin_paired_scoring``); illegal field combinations block
  BEFORE any run with STORAGE_CHRONOLOGY_ILLEGAL; years are processed
  in stable ascending order regardless of input order.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from capacity_credit.contracts.enums import (
    ChronologyMode,
    SocYearBoundaryMode,
    StorageSocInitializationMode,
)
from capacity_credit.contracts.storage_soc_config import (
    StorageBurninConfig,
    StorageSocConfig,
)
from capacity_credit.resources.storage import (
    PortfolioUnit,
    StorageError,
    StorageState,
    effective_soc_band,
)
from capacity_credit.resources.storage_dispatch import (
    DispatchHorizon,
    DispatchOutcome,
    run_eue_greedy,
)

_WARMUP_RELATIVE_THRESHOLD = 0.001
_WARMUP_ABSOLUTE_THRESHOLD_MWH = 0.01


@dataclass(frozen=True)
class WarmupEvidence:
    """weather_cycle_warmup evidence (initial-SOC sensitivity record)."""

    cycles_run: int
    converged: bool
    first_start_soc_mwh: dict[str, float]
    last_start_soc_mwh: dict[str, float]
    last_end_soc_mwh: dict[str, float]
    soc_delta_mwh: dict[str, float]
    threshold_mwh: dict[str, float]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class BurninSampleEvidence:
    """Per-sample burn-in evidence: the scoring initial SOC for one
    Monte Carlo sample (burn-in risk itself is never scored)."""

    sample_index: int
    initial_soc_mwh: dict[str, float]


@dataclass(frozen=True)
class BurninEvidence:
    """stochastic_sample_burnin evidence bundle. The sample-spread
    fields are INFORMATIONAL distribution dispersion - deliberately NOT
    a burn-in convergence criterion (see ``compare_burnin_lengths``)."""

    burnin_hours: int
    sample_count: int
    random_stream_identity: str
    samples: tuple[BurninSampleEvidence, ...]
    sample_spread_mean_mwh: dict[str, float]
    sample_spread_max_deviation_mwh: dict[str, float]
    provisional_status: Literal["provisional"]
    benchmark_status: Literal["pending_benchmark"]


@dataclass(frozen=True)
class SampleScoringEvidence:
    """One fully paired chain link: sample_index -> same-index burn-in
    end SOC -> same-index scored dispatch -> same-index EUE."""

    sample_index: int
    initial_soc_mwh: dict[str, float]
    eue_reduction_mwh: float


@dataclass(frozen=True)
class PairedBurninScoring:
    """Per-sample paired burn-in + scoring evidence (aligned by
    sample_index). Carries the FULL controlled configuration that
    produced it - the length comparison never accepts caller-passed
    tolerances."""

    burnin_hours: int
    soc_tolerance_mwh: float
    eue_tolerance_mwh: float
    random_stream_identity: str
    sample_count: int
    sample_ids: tuple[int, ...]
    samples: tuple[SampleScoringEvidence, ...]
    provisional_status: Literal["provisional"]
    benchmark_status: Literal["pending_benchmark"]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class BurninStabilityEvidence:
    """Burn-in LENGTH-comparison stability verdict: the same batch and
    the same random stream under two controlled burn-in lengths."""

    short_hours: int
    long_hours: int
    stable: bool
    per_sample_soc_delta_mwh: dict[str, float]
    per_sample_eue_delta_mwh: float
    soc_tolerance_mwh: float
    eue_tolerance_mwh: float
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class YearChronologyRecord:
    """One scored year: optional initialization evidence + the scored
    dispatch (initialization risk never enters the scored dispatch)."""

    year: int
    initialization: WarmupEvidence | BurninEvidence | None
    dispatch: DispatchOutcome


@dataclass(frozen=True)
class ChronologyOutcome:
    years: tuple[YearChronologyRecord, ...]
    diagnostics: tuple[str, ...]


def _require_units(units: list[PortfolioUnit]) -> dict[str, PortfolioUnit]:
    if not units:
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", "an empty storage portfolio is not legal")
    by_id: dict[str, PortfolioUnit] = {}
    for unit in units:
        if unit.spec.storage_id in by_id:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                f"duplicate storage_id {unit.spec.storage_id!r}",
            )
        by_id[unit.spec.storage_id] = unit
    return by_id


def _midpoint_states(units: list[PortfolioUnit]) -> dict[str, StorageState]:
    by_id = _require_units(units)
    return {
        storage_id: StorageState(
            soc_mwh=0.5
            * (
                effective_soc_band(unit.spec, unit.limits)[0]
                + effective_soc_band(unit.spec, unit.limits)[1]
            )
        )
        for storage_id, unit in by_id.items()
    }


def _warmup_threshold(unit: PortfolioUnit) -> float:
    e_min, e_max = effective_soc_band(unit.spec, unit.limits)
    return max(
        _WARMUP_RELATIVE_THRESHOLD * (e_max - e_min),
        _WARMUP_ABSOLUTE_THRESHOLD_MWH,
    )


def _validate_explicit_states(
    units: dict[str, PortfolioUnit], explicit: dict[str, float]
) -> dict[str, StorageState]:
    """Explicit initial SOC (S-13): the resource-id set must match
    EXACTLY (no unknown / missing ids) and every SOC must lie inside
    its unit's effective band - validated before any dispatch runs."""
    unit_ids = set(units)
    soc_ids = set(explicit)
    if soc_ids != unit_ids:
        missing = sorted(unit_ids - soc_ids)
        unknown = sorted(soc_ids - unit_ids)
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"explicit initial SOC ids must match the portfolio exactly: "
            f"missing={missing}, unknown={unknown}",
        )
    for storage_id, unit in units.items():
        e_min, e_max = effective_soc_band(unit.spec, unit.limits)
        soc_mwh = explicit[storage_id]
        if not e_min - 1e-9 <= soc_mwh <= e_max + 1e-9:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                f"explicit initial SOC {soc_mwh} MWh for {storage_id!r} is "
                f"outside its effective band [{e_min}, {e_max}] MWh",
            )
    return {storage_id: StorageState(soc_mwh=explicit[storage_id]) for storage_id in units}


def run_weather_cycle_warmup(
    units: list[PortfolioUnit],
    horizon: DispatchHorizon,
    *,
    warmup_max_cycles: int = 5,
    delta_t_hours: float = 1.0,
) -> WarmupEvidence:
    """Deterministic weather-cycle warm-up (methodology §6.3)."""
    if (
        isinstance(warmup_max_cycles, bool)
        or not isinstance(warmup_max_cycles, int)
        or not 1 <= warmup_max_cycles <= 5
    ):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "warmup_max_cycles must lie in [1, 5] (methodology V0.4 §6.3)",
        )
    _require_units(units)
    states = _midpoint_states(units)
    first_start = {storage_id: state.soc_mwh for storage_id, state in states.items()}
    diagnostics = {"STORAGE_SOC_FAST_WARMUP"}
    converged = False
    cycles_run = 0
    last_start: dict[str, float] = dict(first_start)
    last_end: dict[str, float] = dict(first_start)
    delta: dict[str, float] = {}
    thresholds = {unit.spec.storage_id: _warmup_threshold(unit) for unit in units}
    for cycle in range(1, warmup_max_cycles + 1):
        cycles_run = cycle
        last_start = {storage_id: state.soc_mwh for storage_id, state in states.items()}
        outcome = run_eue_greedy(
            units, horizon, initial_states=dict(states), delta_t_hours=delta_t_hours
        )
        last_end = {storage_id: state.soc_mwh for storage_id, state in outcome.final_states.items()}
        delta = {
            storage_id: abs(last_start[storage_id] - last_end[storage_id])
            for storage_id in last_end
        }
        states = {storage_id: StorageState(soc_mwh=last_end[storage_id]) for storage_id in last_end}
        if all(delta[storage_id] <= thresholds[storage_id] + 1e-9 for storage_id in thresholds):
            converged = True
            break
    if not converged:
        diagnostics.add("STORAGE_SOC_WARMUP_NOT_CONVERGED")
    return WarmupEvidence(
        cycles_run=cycles_run,
        converged=converged,
        first_start_soc_mwh=first_start,
        last_start_soc_mwh=last_start,
        last_end_soc_mwh=last_end,
        soc_delta_mwh=delta,
        threshold_mwh=thresholds,
        diagnostics=tuple(sorted(diagnostics)),
    )


def run_stochastic_sample_burnin(
    horizon_factory: Callable[[int], DispatchHorizon],
    units: list[PortfolioUnit],
    *,
    burnin_config: StorageBurninConfig,
    random_stream_identity: str,
    sample_count: int,
    delta_t_hours: float = 1.0,
) -> BurninEvidence:
    """Stochastic sample burn-in evidence interface (methodology §6.3).
    The horizon factory receives the sample index and MUST wire the SAME
    pre-history random stream / outage process / intertie interface used
    by the scoring period (baseline, candidate and every search point
    reuse the same stream identity). The sample-spread statistics are
    informational only - they never drive the stability verdict."""
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", "sample_count must be a positive integer")
    if not isinstance(random_stream_identity, str) or not random_stream_identity.strip():
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "random_stream_identity must be a non-empty string",
        )
    _require_units(units)
    samples: list[BurninSampleEvidence] = []
    for sample_index in range(sample_count):
        horizon = horizon_factory(sample_index)
        if horizon.hours < burnin_config.burnin_hours:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "burn-in horizon is shorter than burnin_hours",
            )
        burnin_horizon = DispatchHorizon(
            pre_storage_supply_mw=horizon.pre_storage_supply_mw[: burnin_config.burnin_hours],
            load_mw=horizon.load_mw[: burnin_config.burnin_hours],
        )
        outcome = run_eue_greedy(
            units,
            burnin_horizon,
            initial_states=_midpoint_states(units),
            delta_t_hours=delta_t_hours,
        )
        samples.append(
            BurninSampleEvidence(
                sample_index=sample_index,
                initial_soc_mwh={
                    storage_id: state.soc_mwh for storage_id, state in outcome.final_states.items()
                },
            )
        )
    storage_ids = sorted({unit.spec.storage_id for unit in units})
    means: dict[str, float] = {}
    max_deviations: dict[str, float] = {}
    for storage_id in storage_ids:
        values = [sample.initial_soc_mwh[storage_id] for sample in samples]
        mean = sum(values) / len(values)
        deviation = max(abs(value - mean) for value in values)
        means[storage_id] = mean
        max_deviations[storage_id] = deviation
    return BurninEvidence(
        burnin_hours=burnin_config.burnin_hours,
        sample_count=sample_count,
        random_stream_identity=random_stream_identity,
        samples=tuple(samples),
        sample_spread_mean_mwh=means,
        sample_spread_max_deviation_mwh=max_deviations,
        provisional_status="provisional",
        benchmark_status="pending_benchmark",
    )


def _validated_sample_ids(sample_ids: list[int]) -> list[int]:
    """Strict sample-id gate (C901 split): non-empty list of
    non-negative integers (bool rejected)."""
    if not isinstance(sample_ids, list) or not sample_ids:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "sample_ids must be a non-empty list",
        )
    ids = []
    for sample_id in sample_ids:
        if isinstance(sample_id, bool) or not isinstance(sample_id, int) or sample_id < 0:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "sample_ids must be non-negative integers",
            )
        ids.append(sample_id)
    return ids


def run_burnin_paired_scoring(
    sample_horizons: Callable[[int], tuple[DispatchHorizon, DispatchHorizon]],
    units: list[PortfolioUnit],
    *,
    burnin_config: StorageBurninConfig,
    random_stream_identity: str,
    sample_count: int | None = None,
    sample_ids: list[int] | None = None,
    delta_t_hours: float = 1.0,
) -> PairedBurninScoring:
    """Minimal per-sample pairing chain: sample_index -> same-index
    pre-history random stream -> same-index burn-in end SOC -> same-
    index scoring period -> same-index EUE evidence. The evidence is
    aligned by sample_index - never first / last / mean sample.
    ``sample_ids`` (explicit) drives the call order; the evidence is
    ALWAYS keyed by the sample id, so a permuted call order cannot
    change the aligned results."""
    if sample_ids is None:
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "sample_count must be a positive integer",
            )
        ids = list(range(sample_count))
    else:
        if sample_count is not None:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "sample_count and sample_ids are exclusive - provide exactly one of them",
            )
        ids = _validated_sample_ids(sample_ids)
    if len(set(ids)) != len(ids):
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", "sample_ids must be unique")
    if not isinstance(random_stream_identity, str) or not random_stream_identity.strip():
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "random_stream_identity must be a non-empty string",
        )
    _require_units(units)
    samples: list[SampleScoringEvidence] = []
    diagnostics: set[str] = set()
    for sample_index in ids:
        burnin_horizon, scoring_horizon = sample_horizons(sample_index)
        if burnin_horizon.hours < burnin_config.burnin_hours:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "burn-in horizon is shorter than burnin_hours",
            )
        burnin_only = DispatchHorizon(
            pre_storage_supply_mw=burnin_horizon.pre_storage_supply_mw[
                : burnin_config.burnin_hours
            ],
            load_mw=burnin_horizon.load_mw[: burnin_config.burnin_hours],
        )
        burnin_outcome = run_eue_greedy(
            units,
            burnin_only,
            initial_states=_midpoint_states(units),
            delta_t_hours=delta_t_hours,
        )
        initial_soc = {
            storage_id: state.soc_mwh for storage_id, state in burnin_outcome.final_states.items()
        }
        scoring = run_eue_greedy(
            units,
            scoring_horizon,
            initial_states={
                storage_id: StorageState(soc_mwh=soc_mwh)
                for storage_id, soc_mwh in initial_soc.items()
            },
            delta_t_hours=delta_t_hours,
        )
        diagnostics.update(scoring.diagnostics)
        samples.append(
            SampleScoringEvidence(
                sample_index=sample_index,
                initial_soc_mwh=initial_soc,
                eue_reduction_mwh=scoring.eue_reduction_mwh,
            )
        )
    return PairedBurninScoring(
        burnin_hours=burnin_config.burnin_hours,
        soc_tolerance_mwh=burnin_config.length_stability_soc_tolerance_mwh,
        eue_tolerance_mwh=burnin_config.length_stability_eue_tolerance_mwh,
        random_stream_identity=random_stream_identity,
        sample_count=len(ids),
        sample_ids=tuple(ids),
        samples=tuple(samples),
        provisional_status="provisional",
        benchmark_status="pending_benchmark",
        diagnostics=tuple(sorted(diagnostics)),
    )


def _validate_paired_sample(
    sample: SampleScoringEvidence, reference_ids: set[str] | None
) -> set[str]:
    """Per-sample evidence gate (C901 split): the SOC mapping is
    validated BEFORE any set() / items() access (a non-mapping or
    non-empty mapping violation is a stable error, never a native
    TypeError / AttributeError), resource ids are non-empty strings and
    every SOC / EUE value is a finite non-negative real number."""
    raw_soc = sample.initial_soc_mwh
    if not isinstance(raw_soc, dict):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"initial_soc_mwh must be a mapping (got {type(raw_soc).__name__})",
        )
    if not raw_soc:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "every sample must carry a non-empty resource id set",
        )
    for storage_id, soc_mwh in raw_soc.items():
        if not isinstance(storage_id, str) or not storage_id.strip():
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "resource ids must be non-empty strings",
            )
        _finite_nonneg_evidence(soc_mwh, f"soc({sample.sample_index},{storage_id})")
    _finite_nonneg_evidence(sample.eue_reduction_mwh, f"eue({sample.sample_index})")
    resource_ids = set(raw_soc)
    if reference_ids is not None and resource_ids != reference_ids:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "all samples must carry the same resource id set",
        )
    return resource_ids


def _strict_nonneg_int(value: object, label: str) -> int:
    """Strict non-negative integer gate (C901 split): bool / float /
    string never coerced."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"{label} must be a non-negative integer (got {value!r})",
        )
    return value


def _validate_paired_structure(evidence: PairedBurninScoring) -> None:
    """Strict non-empty / count / index structure gates (fourth
    remediation, C901 split): run BEFORE any set comparison or delta
    computation so empty or mistyped evidence can never reach max()
    or leak a native exception. sample indexes are strict non-negative
    Python ints (bool / float / string never coerced)."""
    if (
        isinstance(evidence.sample_count, bool)
        or not isinstance(evidence.sample_count, int)
        or evidence.sample_count <= 0
    ):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "sample_count must be a positive integer",
        )
    sample_ids = evidence.sample_ids
    if not isinstance(sample_ids, tuple) or not sample_ids:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "sample_ids must be a non-empty tuple",
        )
    for sample_id in sample_ids:
        _strict_nonneg_int(sample_id, "sample id")
    if len(set(sample_ids)) != len(sample_ids):
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", "evidence sample_ids must be unique")
    if not isinstance(evidence.samples, tuple) or not evidence.samples:
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", "samples must be a non-empty tuple")
    if evidence.sample_count != len(sample_ids) or evidence.sample_count != len(evidence.samples):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "sample_count must equal the sample_ids count and the samples count",
        )
    indexes = [_validated_sample_index(sample) for sample in evidence.samples]
    if len(set(indexes)) != len(indexes):
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", "sample indexes must be unique")
    if set(indexes) != set(sample_ids):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "sample_ids must match the evidence sample indexes exactly",
        )


def _validated_sample_index(sample: object) -> int:
    """Strict per-entry sample gate (C901 split): the entry must be a
    SampleScoringEvidence BEFORE its sample_index is read, and the index
    must be a strict non-negative integer."""
    if not isinstance(sample, SampleScoringEvidence):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"samples entries must be SampleScoringEvidence (got {type(sample).__name__})",
        )
    return _strict_nonneg_int(sample.sample_index, "sample index")


def _validate_paired_controlled_fields(evidence: PairedBurninScoring) -> None:
    """Controlled comparison fields gate (C901 split): burnin length,
    stability tolerances, stream identity and the provisional /
    pending_benchmark status values."""
    if (
        isinstance(evidence.burnin_hours, bool)
        or not isinstance(evidence.burnin_hours, int)
        or evidence.burnin_hours <= 0
    ):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burnin_hours must be a positive integer",
        )
    _finite_nonneg_evidence(evidence.soc_tolerance_mwh, "soc_tolerance_mwh")
    _finite_nonneg_evidence(evidence.eue_tolerance_mwh, "eue_tolerance_mwh")
    if (
        not isinstance(evidence.random_stream_identity, str)
        or not evidence.random_stream_identity.strip()
    ):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "random_stream_identity must be a non-empty string",
        )
    if evidence.provisional_status != "provisional":
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "provisional_status must stay 'provisional'",
        )
    if evidence.benchmark_status != "pending_benchmark":
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "benchmark_status must stay 'pending_benchmark'",
        )


def _validate_paired_evidence(evidence: PairedBurninScoring) -> None:
    """Unified PairedBurninScoring integrity gate (third + fourth
    remediation): strict non-empty / type / count / index gates run
    FIRST, then the controlled comparison fields, then the per-sample
    resource / value gates. Any violation raises
    STORAGE_CHRONOLOGY_ILLEGAL (never a KeyError / ValueError /
    TypeError or any other native exception)."""
    _validate_paired_structure(evidence)
    _validate_paired_controlled_fields(evidence)
    reference_ids: set[str] | None = None
    for sample in evidence.samples:
        reference_ids = _validate_paired_sample(sample, reference_ids)


def _finite_nonneg_evidence(value: object, label: str) -> None:
    """Strict finite non-negative gate for evidence numbers (bool /
    strings / NaN / +-inf / negatives / un-convertible values including
    HUGE integers -> STORAGE_CHRONOLOGY_ILLEGAL). The float conversion
    explicitly catches OverflowError / TypeError / ValueError - no
    native exception can leak."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"{label} must be a real number, got {value!r}",
        )
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"{label} must be a real number, got {value!r}",
        ) from None
    if not math.isfinite(numeric):
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", f"{label} must be finite, got {value!r}")
    if numeric < 0.0:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"{label} must be non-negative, got {value!r}",
        )


def _require_paired_bundle(value: object, label: str) -> None:
    """Top-level runtime type gate for the comparison inputs (C901
    split): non-bundle arguments are a stable error, never an attribute
    access exception."""
    if not isinstance(value, PairedBurninScoring):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            f"{label} must be a PairedBurninScoring bundle (got {type(value).__name__})",
        )


def compare_burnin_lengths(
    short: PairedBurninScoring,
    long: PairedBurninScoring,
) -> BurninStabilityEvidence:
    """Burn-in LENGTH-comparison stability (the ONLY convergence
    criterion): the same batch and the same random stream under two
    controlled burn-in lengths must agree per-sample SOC and per-sample
    scored EUE within the tolerances RECORDED IN THE EVIDENCE (the
    caller can never pass its own tolerances - the controlled
    StorageBurninConfig is the single source). Both evidence bundles
    pass the unified integrity gate BEFORE any delta computation."""
    _require_paired_bundle(short, "short")
    _require_paired_bundle(long, "long")
    _validate_paired_evidence(short)
    _validate_paired_evidence(long)
    if short.random_stream_identity != long.random_stream_identity:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burn-in length comparison requires the SAME random stream",
        )
    if short.soc_tolerance_mwh != long.soc_tolerance_mwh:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burn-in length comparison requires identical SOC tolerances",
        )
    if short.eue_tolerance_mwh != long.eue_tolerance_mwh:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burn-in length comparison requires identical EUE tolerances",
        )
    if not short.burnin_hours < long.burnin_hours:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burn-in length comparison requires short.burnin_hours < "
            "long.burnin_hours (equal or reversed lengths are illegal)",
        )
    if set(short.sample_ids) != set(long.sample_ids):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burn-in length comparison requires identical sample id sets",
        )
    short_storage_ids = {
        storage_id for sample in short.samples for storage_id in sample.initial_soc_mwh
    }
    long_storage_ids = {
        storage_id for sample in long.samples for storage_id in sample.initial_soc_mwh
    }
    if short_storage_ids != long_storage_ids:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burn-in length comparison requires identical resource id sets",
        )
    short_by_index = {sample.sample_index: sample for sample in short.samples}
    long_by_index = {sample.sample_index: sample for sample in long.samples}
    if set(short_by_index) != set(long_by_index):
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "burn-in length comparison requires identical sample indexes",
        )
    max_soc_delta: dict[str, float] = {}
    for storage_id in sorted(short_storage_ids):
        deltas = [
            abs(
                short_by_index[index].initial_soc_mwh[storage_id]
                - long_by_index[index].initial_soc_mwh[storage_id]
            )
            for index in short_by_index
        ]
        max_soc_delta[storage_id] = max(deltas)
    eue_deltas = [
        abs(short_by_index[index].eue_reduction_mwh - long_by_index[index].eue_reduction_mwh)
        for index in short_by_index
    ]
    max_eue_delta = max(eue_deltas)
    stable = (
        all(delta <= short.soc_tolerance_mwh + 1e-12 for delta in max_soc_delta.values())
        and max_eue_delta <= short.eue_tolerance_mwh + 1e-12
    )
    diagnostics: tuple[str, ...] = ()
    if not stable:
        diagnostics = ("STORAGE_SOC_BURNIN_UNSTABLE",)
    return BurninStabilityEvidence(
        short_hours=short.burnin_hours,
        long_hours=long.burnin_hours,
        stable=stable,
        per_sample_soc_delta_mwh=max_soc_delta,
        per_sample_eue_delta_mwh=max_eue_delta,
        soc_tolerance_mwh=short.soc_tolerance_mwh,
        eue_tolerance_mwh=short.eue_tolerance_mwh,
        diagnostics=diagnostics,
    )


def _check_chronology_legality(
    chronology_mode: ChronologyMode,
    soc_year_boundary_mode: SocYearBoundaryMode,
    soc_config: StorageSocConfig,
) -> None:
    """Illegal field combinations block BEFORE any run (stable code)."""
    if chronology_mode is ChronologyMode.CONTINUOUS_MULTIYEAR:
        if soc_year_boundary_mode is not SocYearBoundaryMode.CARRY_OVER:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "continuous_multiyear requires soc_year_boundary_mode=carry_over",
            )
    elif chronology_mode is ChronologyMode.INDEPENDENT_WEATHER_YEARS:
        if soc_year_boundary_mode is not SocYearBoundaryMode.REINITIALIZE_WITH_CONFIGURED_MODE:
            raise StorageError(
                "STORAGE_CHRONOLOGY_ILLEGAL",
                "independent_weather_years requires "
                "soc_year_boundary_mode=reinitialize_with_configured_mode",
            )
    else:  # pragma: no cover - enum is closed
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL", f"unknown chronology_mode {chronology_mode!r}"
        )
    if soc_config.initialization_mode is StorageSocInitializationMode.NOT_APPLICABLE:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "storage_soc.initialization_mode must not be not_applicable",
        )


def _initialize_year(
    units: list[PortfolioUnit],
    horizon: DispatchHorizon,
    soc_config: StorageSocConfig,
    *,
    year: int,
    burnin_horizon_factory: Callable[[int, int], DispatchHorizon] | None,
    random_stream_identity: str,
    burnin_sample_count: int,
) -> tuple[WarmupEvidence | BurninEvidence | None, dict[str, StorageState]]:
    """One initialization pass; returns evidence + scoring initial SOC.
    The burn-in horizon factory takes the EXPLICIT (year, sample_id)
    key - never a mutable internal call counter."""
    unit_map = _require_units(units)
    if soc_config.explicit_initial_soc_mwh is not None:
        return None, _validate_explicit_states(unit_map, soc_config.explicit_initial_soc_mwh)
    if soc_config.initialization_mode is StorageSocInitializationMode.WEATHER_CYCLE_WARMUP:
        warmup_evidence = run_weather_cycle_warmup(
            units, horizon, warmup_max_cycles=soc_config.warmup_max_cycles
        )
        return warmup_evidence, {
            storage_id: StorageState(soc_mwh=soc_mwh)
            for storage_id, soc_mwh in warmup_evidence.last_end_soc_mwh.items()
        }
    assert soc_config.burnin is not None
    if burnin_horizon_factory is None:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "stochastic_sample_burnin requires a burn-in horizon factory",
        )
    if burnin_sample_count != 1:
        raise StorageError(
            "STORAGE_CHRONOLOGY_ILLEGAL",
            "run_multi_year_dispatch is ONE deterministic trajectory: "
            "exactly one burn-in sample is accepted (multi-sample "
            "pairing belongs to run_burnin_paired_scoring); never first / "
            "last / mean sample",
        )
    burnin_evidence = run_stochastic_sample_burnin(
        lambda sample_id: burnin_horizon_factory(year, sample_id),
        units,
        burnin_config=soc_config.burnin,
        random_stream_identity=random_stream_identity,
        sample_count=1,
    )
    final = burnin_evidence.samples[0].initial_soc_mwh
    return burnin_evidence, {
        storage_id: StorageState(soc_mwh=soc_mwh) for storage_id, soc_mwh in final.items()
    }


def run_multi_year_dispatch(
    units: list[PortfolioUnit],
    horizons_by_year: dict[int, DispatchHorizon],
    *,
    chronology_mode: ChronologyMode,
    soc_year_boundary_mode: SocYearBoundaryMode,
    soc_config: StorageSocConfig,
    random_stream_identity: str = "",
    burnin_horizon_factory: Callable[[int, int], DispatchHorizon] | None = None,
    burnin_sample_count: int = 1,
    dispatcher: Callable[
        [list[PortfolioUnit], DispatchHorizon, dict[str, StorageState], int],
        DispatchOutcome,
    ]
    | None = None,
    end_of_cycle_recharge: Any = None,
) -> ChronologyOutcome:
    """Multi-year chronological dispatch with the configured SOC
    boundary mode (methodology §6.3). The burn-in horizon factory takes
    the EXPLICIT (year, sample_id) key - independent weather years each
    re-initialize with their OWN pre-history; within one year the
    baseline / candidate / search points reuse the SAME random stream
    identity.

    ``dispatcher`` (T16 first-review): a controlled per-horizon dispatch
    callback used INSTEAD of run_eue_greedy when provided (risk_window /
    perfect-foresight diagnostic routing). The SOC initialization and
    year-boundary implementation stays HERE - never duplicated by the
    caller.

    ``end_of_cycle_recharge`` (W2/B6-2, optional): forwarded to the
    default eue_greedy path only - the window-floor semantics live in
    the strategy (storage_dispatch.EndOfCycleRecharge); the custom
    dispatcher routes ignore it by documented scope."""
    _check_chronology_legality(chronology_mode, soc_year_boundary_mode, soc_config)
    _require_units(units)
    if not horizons_by_year:
        raise StorageError("STORAGE_CHRONOLOGY_ILLEGAL", "no years to dispatch")
    years = sorted(horizons_by_year)
    diagnostics: set[str] = set()
    records: list[YearChronologyRecord] = []
    states: dict[str, StorageState] | None = None
    for index, year in enumerate(years):
        horizon = horizons_by_year[year]
        if chronology_mode is ChronologyMode.CONTINUOUS_MULTIYEAR and index > 0:
            # single initialization before the scoring period only; the
            # year boundary carries the SOC exactly (never reset)
            initialization = None
            assert states is not None
        else:
            initialization, states = _initialize_year(
                units,
                horizon,
                soc_config,
                year=year,
                burnin_horizon_factory=burnin_horizon_factory,
                random_stream_identity=random_stream_identity,
                burnin_sample_count=burnin_sample_count,
            )
            if initialization is not None and isinstance(initialization, WarmupEvidence):
                diagnostics.update(initialization.diagnostics)
        if dispatcher is not None:
            dispatch = dispatcher(units, horizon, dict(states), year)
        else:
            dispatch = run_eue_greedy(
                units,
                horizon,
                initial_states=dict(states),
                end_of_cycle_recharge=end_of_cycle_recharge,
            )
        diagnostics.update(dispatch.diagnostics)
        records.append(
            YearChronologyRecord(year=year, initialization=initialization, dispatch=dispatch)
        )
        states = dict(dispatch.final_states)
    return ChronologyOutcome(years=tuple(records), diagnostics=tuple(sorted(diagnostics)))


__all__ = [
    "BurninEvidence",
    "BurninSampleEvidence",
    "BurninStabilityEvidence",
    "ChronologyOutcome",
    "PairedBurninScoring",
    "SampleScoringEvidence",
    "WarmupEvidence",
    "YearChronologyRecord",
    "compare_burnin_lengths",
    "run_burnin_paired_scoring",
    "run_multi_year_dispatch",
    "run_stochastic_sample_burnin",
    "run_weather_cycle_warmup",
]
