"""Common-random-number paired risk evaluation (T10, review round 2).

Generic, ELCC-search-decoupled paired evaluation (methodology §4.6,
§12.6, §17.5):

- every risk sample series carries its DECLARED source identity:
  scenario_bank_id, the RiskMetric enum (unit derived from the single
  public definition in ``domain.risk_units``), the exact sample-id set,
  weather years, a stable year-weight identity, the portfolio id and
  the shared-object track identity; the per-sample values are DECLARED
  to be the adequacy engine's formal primary risk already aggregated
  over weather years with the year weights - the pairing layer never
  re-weights and cannot itself prove where the values came from;
- reference and candidate each carry their own identity;
  ``evaluate_paired_differences`` FORCE-CHECKS declared identities
  (bank, metric, sample set, weather years + weight identity, shared
  track, candidate/shared disjointness) and blocks whenever the declared
  identities disagree; it cannot in T10 prove that any values mapping
  was really computed by the adequacy engine - that source closure is
  T12 application-service work (constructing RiskSampleSeries from real
  ScenarioBank / AdequacyEngine run artefacts);
- pairing is by sample_id identity: sample ids, weather years and the
  per-sample values are normalized to ascending order at construction,
  so any input ordering with the same sets yields the identical paired
  result and statistics; sample ids missing, duplicated or mismatched
  block;
- per-sample difference:  D_s = R_candidate,s - R_reference,s with the
  fixed sign convention (candidate/trial minus reference). Only the
  sign-convention slice of C-06 is covered here; no search brackets,
  bisection, interpolation or plateau judgement (T11);
- paired statistics reuse ``engine.online_stats.OnlineStats`` for
  mean / sample variance / standard error / sample count only; the
  nonzero-sample count is computed by the pairing layer as
  ``difference != 0.0`` (OnlineStats counts positive values, which is
  the wrong semantics for signed differences) and exposed as an
  immutable snapshot;
- boundaries match OnlineStats: n=0 blocks, n=1 -> variance 0 and
  standard error NaN, non-finite values block;
- all failures use stable ``PairedEvaluationError`` codes; sample ids
  and weather years are strict integers (bool/str rejected), risk keys
  are type-checked explicitly (no KeyError leaks), object ids are
  canonicalized non-blank strings and canonicalization collisions are
  blocked instead of silently merged by frozensets.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import numpy as np

from capacity_credit.contracts.enums import RiskMetric
from capacity_credit.contracts.risk_convergence import RiskConvergenceSnapshot
from capacity_credit.domain.identifiers import (
    IdentifierError,
    canonical_object_id_set,
)
from capacity_credit.domain.risk_units import risk_metric_unit
from capacity_credit.engine.online_stats import OnlineStats

_WEIGHT_SUM_TOLERANCE = 1e-9
_FORMAL_PRIMARY_CALIBRE = "formal_primary_risk_weighted_by_weather_year"


class PairedEvaluationError(ValueError):
    """Stable paired-evaluation error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _paired_object_id_set(values: Any, field_name: str) -> frozenset[str]:
    """Canonicalize through the single public definition; collisions
    block instead of silently merging (stable paired error codes)."""
    try:
        return canonical_object_id_set(values, field_name)
    except IdentifierError as exc:
        code = {
            "IDENTIFIER_NOT_STRING": "PAIRED_EMPTY_IDENTIFIER",
            "IDENTIFIER_BLANK": "PAIRED_EMPTY_IDENTIFIER",
            "IDENTIFIER_EMPTY": "PAIRED_EMPTY_IDENTIFIER",
            "IDENTIFIER_COLLISION": "PAIRED_OBJECT_ID_COLLISION",
        }.get(exc.code, "PAIRED_EMPTY_IDENTIFIER")
        raise PairedEvaluationError(code, exc.message) from exc


def _canonical_identifier(value: str, field_name: str) -> str:
    from capacity_credit.domain.identifiers import (
        IdentifierError,
        canonical_object_id,
    )

    try:
        return canonical_object_id(value, field_name)
    except IdentifierError as exc:
        raise PairedEvaluationError(
            "PAIRED_EMPTY_IDENTIFIER",
            exc.message,
        ) from exc


def _strict_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise PairedEvaluationError(
            "PAIRED_INVALID_IDENTIFIER",
            f"{field_name} must be a strict integer (bool/str rejected)",
        )
    return int(value)


def _normalize_sample_ids(values: Any) -> tuple[int, ...]:
    sample_ids = tuple(_strict_int(s, "sample_id") for s in values)
    if not sample_ids:
        raise PairedEvaluationError("PAIRED_NO_SAMPLES", "at least one sample id is required")
    if len(set(sample_ids)) != len(sample_ids):
        raise PairedEvaluationError("PAIRED_DUPLICATE_SAMPLE_ID", "sample ids must be unique")
    return tuple(sorted(sample_ids))


def _year_weight_identity(weights: Mapping[int, float]) -> str:
    """Stable identity of the normalized year-weight calibre."""
    payload = {str(year): repr(float(weights[year])) for year in sorted(weights)}
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RiskSampleSeries:
    """Immutable per-sample DECLARED formal primary risk series with
    full source identity (the pairing layer never re-weights). Sample
    ids, weather years and values are normalized to ascending order.

    ``convergence_snapshot`` is the CLOSED shared-T08 convergence snapshot
    ATTACHED by the neutral application-layer adapter
    (``application.risk_series``). It is optional so a pure-pairing
    series can be constructed without convergence evidence, but a formal
    MRI/EFC result MUST be backed by a full snapshot; a missing snapshot
    is only ever treated as unknown / not_converged - a bare
    ``RiskConvergence`` enum without criteria / should_stop / evidence is
    never a valid convergence claim."""

    scenario_bank_id: str
    primary_risk_metric: RiskMetric
    sample_ids: tuple[int, ...]
    weather_years: tuple[int, ...]
    year_weights: Mapping[int, float]
    portfolio_id: str
    shared_object_ids: frozenset[str]
    candidate_object_ids: frozenset[str]
    values: Mapping[int, float]
    year_weights_identity: str | None = None
    risk_calibre: str = _FORMAL_PRIMARY_CALIBRE
    convergence_snapshot: RiskConvergenceSnapshot | None = None

    def __post_init__(self) -> None:
        bank_id = _canonical_identifier(self.scenario_bank_id, "scenario_bank_id")
        object.__setattr__(self, "scenario_bank_id", bank_id)
        portfolio_id = _canonical_identifier(self.portfolio_id, "portfolio_id")
        object.__setattr__(self, "portfolio_id", portfolio_id)
        if not isinstance(self.primary_risk_metric, RiskMetric):
            raise PairedEvaluationError(
                "PAIRED_METRIC_MISMATCH",
                "primary_risk_metric must be a RiskMetric enum value",
            )
        if self.convergence_snapshot is not None and not isinstance(
            self.convergence_snapshot, RiskConvergenceSnapshot
        ):
            raise PairedEvaluationError(
                "PAIRED_INVALID_CONVERGENCE",
                "convergence_snapshot must be a RiskConvergenceSnapshot when set",
            )
        sample_ids = _normalize_sample_ids(self.sample_ids)
        object.__setattr__(self, "sample_ids", sample_ids)
        years = tuple(_strict_int(y, "weather_year") for y in self.weather_years)
        if not years:
            raise PairedEvaluationError(
                "PAIRED_YEAR_WEIGHTS_MISMATCH", "weather years must not be empty"
            )
        if len(set(years)) != len(years):
            raise PairedEvaluationError(
                "PAIRED_YEAR_WEIGHTS_MISMATCH", "weather years must be unique"
            )
        years = tuple(sorted(years))
        object.__setattr__(self, "weather_years", years)
        if not isinstance(self.year_weights, Mapping):
            raise PairedEvaluationError(
                "PAIRED_YEAR_WEIGHTS_MISMATCH", "year_weights must be a mapping"
            )
        weights: dict[int, float] = {}
        for raw_year, raw_weight in self.year_weights.items():
            year = _strict_int(raw_year, "weather_year")
            if isinstance(raw_weight, bool) or not isinstance(
                raw_weight, (int, float, np.integer, np.floating)
            ):
                raise PairedEvaluationError(
                    "PAIRED_YEAR_WEIGHTS_MISMATCH",
                    "year weights must be real numbers",
                )
            weight = float(raw_weight)
            if not np.isfinite(weight) or weight < 0.0:
                raise PairedEvaluationError(
                    "PAIRED_YEAR_WEIGHTS_MISMATCH",
                    "year weights must be finite and non-negative",
                )
            weights[year] = weight
        if set(weights) != set(years):
            raise PairedEvaluationError(
                "PAIRED_YEAR_WEIGHTS_MISMATCH",
                "year weights must cover exactly the weather years",
            )
        if abs(sum(weights.values()) - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise PairedEvaluationError(
                "PAIRED_YEAR_WEIGHTS_MISMATCH",
                "year weights must sum to 1",
            )
        object.__setattr__(self, "year_weights", MappingProxyType(weights))
        expected_identity = _year_weight_identity(weights)
        if self.year_weights_identity is None:
            object.__setattr__(self, "year_weights_identity", expected_identity)
        elif self.year_weights_identity != expected_identity:
            raise PairedEvaluationError(
                "PAIRED_YEAR_WEIGHTS_MISMATCH",
                "year_weights_identity does not match the year weights",
            )
        shared = _paired_object_id_set(self.shared_object_ids, "shared_object_ids")
        candidate = _paired_object_id_set(
            self.candidate_object_ids,
            "candidate_object_ids",
        )
        if shared & candidate:
            raise PairedEvaluationError(
                "PAIRED_OBJECT_SET_CONFLICT",
                "shared and candidate object sets must be disjoint",
            )
        object.__setattr__(self, "shared_object_ids", shared)
        object.__setattr__(self, "candidate_object_ids", candidate)
        if self.risk_calibre != _FORMAL_PRIMARY_CALIBRE:
            raise PairedEvaluationError(
                "PAIRED_CALIBRE_MISMATCH",
                "risk_calibre must be the weather-year-weighted formal "
                "primary risk; the pairing layer never re-weights",
            )
        object.__setattr__(self, "risk_calibre", _FORMAL_PRIMARY_CALIBRE)
        if not isinstance(self.values, Mapping):
            raise PairedEvaluationError("PAIRED_SAMPLE_SET_MISMATCH", "values must be a mapping")
        validated: dict[int, float] = {}
        for sample_id in sample_ids:
            if sample_id not in self.values:
                raise PairedEvaluationError(
                    "PAIRED_SAMPLE_SET_MISMATCH",
                    f"values missing sample_id {sample_id}",
                )
            raw = self.values[sample_id]
            if isinstance(raw, bool) or not isinstance(raw, (int, float, np.integer, np.floating)):
                raise PairedEvaluationError(
                    "PAIRED_NON_FINITE_RISK",
                    "risk values must be real numbers",
                )
            number = float(raw)
            if not np.isfinite(number):
                raise PairedEvaluationError(
                    "PAIRED_NON_FINITE_RISK",
                    "risk values must be finite",
                )
            if number < 0.0:
                raise PairedEvaluationError(
                    "PAIRED_RISK_RANGE",
                    "risk values must be non-negative for the supported risk metrics",
                )
            validated[sample_id] = number
        for key in self.values:
            try:
                sample_key = _strict_int(key, "sample_id")
            except PairedEvaluationError:
                raise PairedEvaluationError(
                    "PAIRED_SAMPLE_SET_MISMATCH",
                    "values contain non-integer sample keys",
                ) from None
            if sample_key not in validated:
                raise PairedEvaluationError(
                    "PAIRED_SAMPLE_SET_MISMATCH",
                    f"values contain sample ids outside the series (extra {sample_key})",
                )
        object.__setattr__(self, "values", MappingProxyType(validated))

    @property
    def risk_metric_unit(self) -> str:
        """Unit derived from the metric through the single public
        definition (never a free input)."""
        return risk_metric_unit(self.primary_risk_metric)


@dataclass(frozen=True)
class PairedDifference:
    """One per-sample paired risk difference."""

    sample_id: int
    reference_risk: float
    candidate_risk: float
    difference: float


@dataclass(frozen=True)
class PairedStats:
    """Immutable snapshot of the paired-difference statistics."""

    sample_count: int
    mean: float
    sample_variance: float
    standard_error: float
    nonzero_sample_count: int

    @classmethod
    def from_online(
        cls,
        stats: OnlineStats,
        *,
        nonzero_sample_count: int,
    ) -> PairedStats:
        """mean/variance/se/count come from OnlineStats; the nonzero
        count is computed by the pairing layer (difference != 0.0)."""
        return cls(
            sample_count=stats.sample_count,
            mean=stats.mean,
            sample_variance=stats.sample_variance,
            standard_error=stats.standard_error,
            nonzero_sample_count=nonzero_sample_count,
        )


@dataclass(frozen=True)
class PairedEvaluationResult:
    """Paired difference set plus an immutable statistics snapshot."""

    scenario_bank_id: str
    primary_risk_metric: RiskMetric
    risk_metric_unit: str
    sample_count: int
    differences: tuple[PairedDifference, ...]
    stats: PairedStats
    reference_identity: RiskSampleSeries
    candidate_identity: RiskSampleSeries
    sign_convention: str = (
        "candidate_risk - reference_risk; positive means the trial "
        "system risk is above the base (C-06 sign-convention slice only; "
        "search is T11)"
    )


def evaluate_paired_differences(
    *,
    reference: RiskSampleSeries,
    candidate: RiskSampleSeries,
) -> PairedEvaluationResult:
    """Pair the two systems by sample-id identity (never by array
    position) and compute D_s = candidate - reference with paired
    statistics.

    The DECLARED source identities of both series are force-checked:
    scenario bank, risk metric, sample set, weather years and the
    year-weight identity, shared-object track identity, and
    candidate/shared disjointness. When the declared identities agree
    the pairing proceeds; the layer cannot in T10 prove the values were
    computed by the adequacy engine (T12 application-service closure).
    """
    checks = [
        (
            "PAIRED_BANK_MISMATCH",
            reference.scenario_bank_id == candidate.scenario_bank_id,
            "scenario_bank_id differs between reference and candidate",
        ),
        (
            "PAIRED_METRIC_MISMATCH",
            reference.primary_risk_metric == candidate.primary_risk_metric,
            "primary risk metric differs between reference and candidate",
        ),
        (
            "PAIRED_SAMPLE_SET_MISMATCH",
            set(reference.sample_ids) == set(candidate.sample_ids),
            "sample id sets differ between reference and candidate",
        ),
        (
            "PAIRED_YEAR_WEIGHTS_MISMATCH",
            set(reference.weather_years) == set(candidate.weather_years)
            and reference.year_weights_identity == candidate.year_weights_identity,
            "weather years or year-weight calibre differ between the systems",
        ),
        (
            "PAIRED_SHARED_TRACK_MISMATCH",
            reference.shared_object_ids == candidate.shared_object_ids,
            "shared-object track identity differs between reference and candidate",
        ),
        (
            # R2-C2: convergence evidence must be symmetric - one series
            # carrying the shared-T08 snapshot while the other does not
            # is not pairable (the missing side is unknown evidence,
            # never assumed converged)
            "PAIRED_CONVERGENCE_EVIDENCE_ASYMMETRIC",
            (reference.convergence_snapshot is None)
            == (candidate.convergence_snapshot is None),
            "one series carries a convergence snapshot and the other does not",
        ),
    ]
    for code, ok, message in checks:
        if not ok:
            raise PairedEvaluationError(code, message)
    if (
        reference.convergence_snapshot is not None
        and candidate.convergence_snapshot is not None
        and reference.convergence_snapshot.config_identity
        != candidate.convergence_snapshot.config_identity
    ):
        raise PairedEvaluationError(
            "PAIRED_CONVERGENCE_CONFIG_MISMATCH",
            "the two series were converged under different convergence configs",
        )
    if candidate.candidate_object_ids & reference.shared_object_ids:
        raise PairedEvaluationError(
            "PAIRED_OBJECT_SET_CONFLICT",
            "candidate new objects conflict with the shared objects",
        )
    if reference.candidate_object_ids & candidate.shared_object_ids:
        raise PairedEvaluationError(
            "PAIRED_OBJECT_SET_CONFLICT",
            "reference new objects conflict with the candidate shared objects",
        )
    differences: list[PairedDifference] = []
    nonzero_count = 0
    for sample_id in reference.sample_ids:
        reference_risk = reference.values[sample_id]
        candidate_risk = candidate.values[sample_id]
        difference = candidate_risk - reference_risk
        if difference != 0.0:
            nonzero_count += 1
        differences.append(
            PairedDifference(
                sample_id=sample_id,
                reference_risk=reference_risk,
                candidate_risk=candidate_risk,
                difference=difference,
            )
        )
    stats = OnlineStats()
    stats.update_batch([entry.difference for entry in differences])
    return PairedEvaluationResult(
        scenario_bank_id=reference.scenario_bank_id,
        primary_risk_metric=reference.primary_risk_metric,
        risk_metric_unit=reference.risk_metric_unit,
        sample_count=len(differences),
        differences=tuple(differences),
        stats=PairedStats.from_online(
            stats,
            nonzero_sample_count=nonzero_count,
        ),
        reference_identity=reference,
        candidate_identity=candidate,
    )


__all__ = [
    "PairedDifference",
    "PairedEvaluationError",
    "PairedEvaluationResult",
    "PairedStats",
    "RiskSampleSeries",
    "evaluate_paired_differences",
]
