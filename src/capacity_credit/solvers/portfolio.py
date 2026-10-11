"""Portfolio capacity-credit combination solver (T19, P4).

Pure COMBINATION solver over already-computed results - it never
re-runs the reliability engine, the ELCC/EFC/MRI solvers, the storage
kernel or the result construction. It consumes the FORMAL
``MethodResult`` objects (written and read through the production
``result_service``) and produces the portfolio accounting:

- the physical PORTFOLIO capacity credit from an INDEPENDENT combined
  run (base system S -> S + portfolio);
- each member's INDEPENDENT capacity credit (S -> S + member);
- the arithmetic sum of the independent member credits - DIAGNOSTIC
  ONLY, never presented as the physical portfolio result;
- the non-additivity evidence (portfolio - sum);
- MARGINAL credits: an increment resource added to a declared baseline
  portfolio version (S + baseline -> S + baseline + increment), so a
  marginal value is NEVER a meaning-free residual of two subtractions;
- a GOVERNANCE-RULE allocation that is stored SEPARATELY from the
  physical portfolio result (allocation_type=governance_rule + a rule
  id), never mixed into the physical result.

MW values and percentages (capacity_credit_fraction) are kept distinct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from capacity_credit.contracts.enums import AllocationType, CapacityValueType
from capacity_credit.contracts.results import MethodResult
from capacity_credit.contracts.runs import (
    _check_allocation,
    _check_capacity_value,
)


class PortfolioComputationError(ValueError):
    """Stable portfolio-combination error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT = "P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT"
P_PORTFOLIO_REQUIRES_OWN_RUN = "P_PORTFOLIO_REQUIRES_OWN_RUN"
P_PORTFOLIO_DUPLICATE_MEMBER_RUN = "P_PORTFOLIO_DUPLICATE_MEMBER_RUN"
P_PORTFOLIO_DUPLICATE_MEMBER_ID = "P_PORTFOLIO_DUPLICATE_MEMBER_ID"
P_PENETRATION_IDENTITY = "P_PENETRATION_IDENTITY"
P_MARGINAL_REQUIRES_BASELINE = "P_MARGINAL_REQUIRES_BASELINE"
P_MARGINAL_REQUIRES_MARGINAL_RESULT = "P_MARGINAL_REQUIRES_MARGINAL_RESULT"
P_GOVERNANCE_REQUIRES_RULE_ID = "P_GOVERNANCE_REQUIRES_RULE_ID"
P_SUM_IS_DIAGNOSTIC_ONLY = "P_SUM_IS_DIAGNOSTIC_ONLY"


@dataclass(frozen=True)
class PortfolioMember:
    """One portfolio member (a resource / portfolio group)."""

    member_id: str
    nominal_capacity_mw: float


@dataclass(frozen=True)
class MemberCredit:
    """Independent capacity credit of one member (S -> S + member)."""

    member_id: str
    run_id: str
    capacity_credit_mw: float | None
    capacity_credit_fraction: float | None
    nominal_capacity_mw: float | None
    result_status: str
    risk_convergence: str


@dataclass(frozen=True)
class MarginalCredit:
    """Marginal capacity credit of an increment resource against a
    DECLARED baseline portfolio version (S + baseline -> S + baseline +
    increment). ``baseline_portfolio_version`` is mandatory."""

    member_id: str
    run_id: str
    baseline_portfolio_version: str
    incremental_capacity_mw: float | None
    marginal_credit_mw: float | None
    marginal_credit_fraction: float | None
    result_status: str


@dataclass(frozen=True)
class GovernanceAllocation:
    """Governance-rule allocation of the portfolio physical result. Kept
    strictly separate from the physical result (WF-10 / §11.3): it is a
    rule decision, not a physical calculation."""

    allocation_rule_id: str
    member_allocations: tuple[tuple[str, float | None], ...]  # (member_id, allocated_mw)


@dataclass(frozen=True)
class PortfolioSummary:
    """The closed portfolio accounting. ``sum_of_individuals_mw`` and
    ``arithmetic_difference_mw`` are DIAGNOSTIC ONLY - they are never the
    physical portfolio result. ``diagnostic_complete`` is True ONLY when
    EVERY member credit is non-null (a partial arithmetic sum is never
    presented as the full diagnostic sum)."""

    portfolio_member_ids: tuple[str, ...]
    portfolio_run_id: str
    portfolio_capacity_mw: float | None  # physical combined result (MW)
    portfolio_credit_fraction: float | None  # physical combined result (%)
    portfolio_nominal_capacity_mw: float  # sum of member nominal capacities
    sum_of_individuals_mw: float | None  # DIAGNOSTIC ONLY (None unless ALL members non-null)
    arithmetic_difference_mw: float | None  # portfolio - sum (None unless both non-null)
    diagnostic_complete: bool
    member_credits: tuple[MemberCredit, ...]
    marginal_credits: tuple[MarginalCredit, ...]
    allocation: GovernanceAllocation | None
    capacity_credit_type: str = "portfolio_physical"


@dataclass(frozen=True)
class PenetrationPoint:
    """One point of a penetration sequence (§11.4): a candidate capacity
    level with its INDEPENDENT total capacity credit."""

    point_id: str
    nominal_capacity_mw: float
    total_credit_mw: float | None
    result_status: str
    run_id: str | None = None
    capacity_credit_fraction: float | None = None  # total / nominal (average credit rate)
    increment_credit_mw: float | None = None  # vs the previous point (interval increment)
    marginal_credit_rate: float | None = None  # increment / nominal increment (first point None)

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON payload (nulls preserved; no default=str hacks)."""
        return {
            "point_id": self.point_id,
            "nominal_capacity_mw": self.nominal_capacity_mw,
            "run_id": self.run_id,
            "total_credit_mw": self.total_credit_mw,
            "capacity_credit_fraction": self.capacity_credit_fraction,
            "increment_credit_mw": self.increment_credit_mw,
            "marginal_credit_rate": self.marginal_credit_rate,
            "result_status": self.result_status,
        }


@dataclass(frozen=True)
class PenetrationSequence:
    """An ORDERED, traceable penetration curve (§11.4). Monotonicity is
    OBSERVED, never assumed; unsolvable / boundary points keep a
    structured result_status and a null credit (never zero-filled)."""

    points: tuple[PenetrationPoint, ...]
    scaling_description: str

    @property
    def is_monotonic(self) -> bool | None:
        from itertools import pairwise

        credits = [p.total_credit_mw for p in self.points if p.total_credit_mw is not None]
        if len(credits) < 2:
            return None
        deltas = [b - a for a, b in pairwise(credits)]
        if all(delta > 0 for delta in deltas):
            return True
        return bool(all(delta < 0 for delta in deltas))

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON payload of the ordered curve including the OBSERVED
        monotonicity result (never assumed)."""
        return {
            "points": [point.to_dict() for point in self.points],
            "scaling_description": self.scaling_description,
            "is_monotonic": self.is_monotonic,
        }


def penetration_sequence(
    points: list[PenetrationPoint],
    scaling_description: str,
) -> PenetrationSequence:
    """Close an ordered penetration curve: computes each point's average
    credit rate (total / nominal), the interval increment vs the previous
    point and the marginal credit rate. Points are kept in the ORDER the
    caller provides (capacity levels are explicit, never re-sorted). A
    point with a null credit (unsolvable / not_converged / boundary) is
    preserved with its structured result_status - never zero-filled."""
    if not points:
        raise PortfolioComputationError(
            P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT,
            "penetration_sequence requires at least one point",
        )
    # Identity / order closure: point ids unique, non-null run ids unique,
    # nominal capacities strictly increasing positive. The sequence is NOT
    # auto-sorted - an out-of-order caller is a hard error, never silently
    # repaired (an ordered, traceable curve is a contract invariant).
    point_ids = [point.point_id for point in points]
    if len(set(point_ids)) != len(point_ids):
        raise PortfolioComputationError(
            P_PENETRATION_IDENTITY,
            "penetration points must have unique point ids",
        )
    run_ids = [point.run_id for point in points if point.run_id is not None]
    if len(set(run_ids)) != len(run_ids):
        raise PortfolioComputationError(
            P_PENETRATION_IDENTITY,
            "penetration points must come from distinct independent runs",
        )
    for index, point in enumerate(points):
        if point.nominal_capacity_mw <= 0.0:
            raise PortfolioComputationError(
                P_PENETRATION_IDENTITY,
                f"penetration point {point.point_id!r} nominal_capacity_mw must be positive",
            )
        if index > 0 and point.nominal_capacity_mw <= points[index - 1].nominal_capacity_mw:
            raise PortfolioComputationError(
                P_PENETRATION_IDENTITY,
                "penetration nominal capacities must be strictly increasing "
                "(no automatic re-ordering)",
            )
    closed: list[PenetrationPoint] = []
    previous: PenetrationPoint | None = None
    for point in points:
        fraction = (
            point.total_credit_mw / point.nominal_capacity_mw
            if point.total_credit_mw is not None and point.nominal_capacity_mw > 0
            else None
        )
        increment: float | None = None
        marginal_rate: float | None = None
        if (
            previous is not None
            and previous.total_credit_mw is not None
            and point.total_credit_mw is not None
        ):
            increment = point.total_credit_mw - previous.total_credit_mw
            nominal_delta = point.nominal_capacity_mw - previous.nominal_capacity_mw
            if nominal_delta > 0:
                marginal_rate = increment / nominal_delta
        closed.append(
            PenetrationPoint(
                point_id=point.point_id,
                nominal_capacity_mw=point.nominal_capacity_mw,
                run_id=point.run_id,
                total_credit_mw=point.total_credit_mw,
                capacity_credit_fraction=fraction,
                increment_credit_mw=increment,
                marginal_credit_rate=marginal_rate,
                result_status=point.result_status,
            )
        )
        previous = point
    return PenetrationSequence(points=tuple(closed), scaling_description=scaling_description)


def _credits_if_all_present(member_credits: list[MemberCredit]) -> list[float]:
    values = [mc.capacity_credit_mw for mc in member_credits]
    if any(value is None for value in values):
        raise PortfolioComputationError(
            P_SUM_IS_DIAGNOSTIC_ONLY,
            "the diagnostic arithmetic sum requires every member credit to be present",
        )
    return [float(value) for value in values if value is not None]


def _result_run_id(result: MethodResult) -> str:
    return str(result.run_id)


def member_credit_from_result(
    member_id: str,
    result: MethodResult,
    nominal_capacity_mw: float | None,
) -> MemberCredit:
    """Extract an INDEPENDENT member capacity credit from a formal
    MethodResult (ELCC/EFC). The result must be applicable (not adequacy /
    not_converged results carry null credit, which is preserved - never
    zero-filled)."""
    if result.capacity_value_type is CapacityValueType.NOT_APPLICABLE:
        raise PortfolioComputationError(
            P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT,
            f"member {member_id!r} result has capacity_value_type=not_applicable",
        )
    return MemberCredit(
        member_id=member_id,
        run_id=_result_run_id(result),
        capacity_credit_mw=result.accredited_capacity_mw,
        capacity_credit_fraction=result.capacity_credit_fraction,
        nominal_capacity_mw=nominal_capacity_mw,
        result_status=result.result_status.value,
        risk_convergence=result.risk_convergence.value,
    )


def marginal_credit_from_result(
    member_id: str,
    result: MethodResult,
) -> MarginalCredit:
    """Extract a MARGINAL capacity credit from a formal MethodResult
    (ELCC/EFC) computed as S + baseline -> S + baseline + increment.
    WF-10: marginal REQUIRES ``capacity_value_type=marginal`` and a
    declared ``baseline_portfolio_version``."""
    if result.capacity_value_type is not CapacityValueType.MARGINAL:
        raise PortfolioComputationError(
            P_MARGINAL_REQUIRES_MARGINAL_RESULT,
            f"marginal credit for {member_id!r} requires capacity_value_type=marginal",
        )
    if result.baseline_portfolio_version is None:
        raise PortfolioComputationError(
            P_MARGINAL_REQUIRES_BASELINE,
            f"marginal credit for {member_id!r} requires baseline_portfolio_version",
        )
    return MarginalCredit(
        member_id=member_id,
        run_id=_result_run_id(result),
        baseline_portfolio_version=result.baseline_portfolio_version,
        incremental_capacity_mw=result.reference_capacity_mw,
        marginal_credit_mw=result.accredited_capacity_mw,
        marginal_credit_fraction=result.capacity_credit_fraction,
        result_status=result.result_status.value,
    )


def governance_allocation(
    allocation_rule_id: str,
    member_allocations: tuple[tuple[str, float | None], ...],
) -> GovernanceAllocation:
    """Build the governance-rule allocation. It is a rule decision stored
    SEPARATELY from the physical result; the shared contract rule
    (``allocation_type=governance_rule`` requires ``allocation_rule_id``)
    is enforced here too, and the rule id must be a non-blank string."""
    _check_allocation(AllocationType.GOVERNANCE_RULE, allocation_rule_id)
    if not isinstance(allocation_rule_id, str) or not allocation_rule_id.strip():
        raise PortfolioComputationError(
            P_GOVERNANCE_REQUIRES_RULE_ID,
            "governance allocation requires a non-blank allocation_rule_id",
        )
    if not member_allocations:
        raise PortfolioComputationError(
            P_GOVERNANCE_REQUIRES_RULE_ID,
            "governance allocation requires at least one member allocation",
        )
    return GovernanceAllocation(
        allocation_rule_id=allocation_rule_id,
        member_allocations=member_allocations,
    )


def _validate_portfolio_result(result: MethodResult) -> None:
    """WF-10 / C-24: the physical portfolio result must come from an
    INDEPENDENT combined run (its own run_id) and be labelled as a
    portfolio capacity value. It must NOT carry a governance allocation."""
    if result.capacity_value_type is not CapacityValueType.PORTFOLIO:
        raise PortfolioComputationError(
            P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT,
            "the physical portfolio result must have capacity_value_type=portfolio",
        )
    if result.allocation_type is not AllocationType.NONE:
        raise PortfolioComputationError(
            P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT,
            "the physical portfolio result must use allocation_type=none "
            "(governance allocation is stored separately)",
        )
    if result.allocation_rule_id is not None:
        raise PortfolioComputationError(
            P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT,
            "the physical portfolio result must not carry allocation_rule_id",
        )


def combine_portfolio(
    *,
    portfolio_result: MethodResult,
    member_credits: list[MemberCredit],
    nominal_capacity_mw: float,
    marginal_credits: list[MarginalCredit] | None = None,
    allocation: GovernanceAllocation | None = None,
) -> PortfolioSummary:
    """Close the portfolio accounting:

    - physical portfolio credit = the INDEPENDENT combined run result;
    - sum_of_individuals = DIAGNOSTIC arithmetic sum of independent
      member credits (never the physical result);
    - arithmetic_difference = portfolio - sum (non-additivity evidence);
    - marginal credits (baseline-portfolio-version bound) and the
      governance allocation are kept separate from the physical result.
    """
    _validate_portfolio_result(portfolio_result)
    portfolio_mw = portfolio_result.accredited_capacity_mw
    portfolio_fraction = portfolio_result.capacity_credit_fraction
    members = tuple(mc.member_id for mc in member_credits)
    if not members:
        raise PortfolioComputationError(
            P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT,
            "combine_portfolio requires at least one member credit",
        )
    # Independent-run closure (C-24 / WF-10): every member must come from
    # its OWN independent run, no two members may share a run_id, and the
    # physical portfolio result must come from a run DISTINCT from every
    # member credit. These are PRODUCTION function invariants - the caller
    # can never rely on test-side run_id comparisons.
    if len(set(members)) != len(members):
        raise PortfolioComputationError(
            P_PORTFOLIO_DUPLICATE_MEMBER_ID,
            "member credits must have unique member ids",
        )
    member_run_ids = [mc.run_id for mc in member_credits]
    if len(set(member_run_ids)) != len(member_run_ids):
        raise PortfolioComputationError(
            P_PORTFOLIO_DUPLICATE_MEMBER_RUN,
            "member credits must come from distinct independent runs",
        )
    portfolio_run_id = _result_run_id(portfolio_result)
    if portfolio_run_id in set(member_run_ids):
        raise PortfolioComputationError(
            P_PORTFOLIO_REQUIRES_OWN_RUN,
            "the physical portfolio result must come from an independent "
            "run distinct from every member credit run",
        )
    # Diagnostic arithmetic sum: only formed when EVERY member credit is
    # non-null; a partial sum is never presented as the full diagnostic
    # sum, and a missing member is never zero-filled.
    all_present = all(mc.capacity_credit_mw is not None for mc in member_credits)
    sum_mw: float | None = (
        float(sum(float(value) for value in _credits_if_all_present(member_credits)))
        if all_present
        else None
    )
    difference: float | None = None
    if portfolio_mw is not None and sum_mw is not None:
        difference = portfolio_mw - sum_mw
    if allocation is not None:
        for member_id in allocation.member_allocations:
            if member_id[0] not in set(members):
                raise PortfolioComputationError(
                    P_GOVERNANCE_REQUIRES_RULE_ID,
                    f"governance allocation references unknown member {member_id[0]!r}",
                )
    return PortfolioSummary(
        portfolio_member_ids=members,
        portfolio_run_id=portfolio_run_id,
        portfolio_capacity_mw=portfolio_mw,
        portfolio_credit_fraction=portfolio_fraction,
        portfolio_nominal_capacity_mw=nominal_capacity_mw,
        sum_of_individuals_mw=sum_mw,
        arithmetic_difference_mw=difference,
        diagnostic_complete=all_present,
        member_credits=tuple(member_credits),
        marginal_credits=tuple(marginal_credits or ()),
        allocation=allocation,
    )


def validate_marginal_spec(
    capacity_value_type: Any, baseline_portfolio_version: str | None
) -> None:
    """WF-10 contract rule for a marginal run config: marginal requires
    a baseline portfolio version (shared rule in contracts.runs)."""
    _check_capacity_value(capacity_value_type, baseline_portfolio_version)


__all__ = [
    "P_GOVERNANCE_REQUIRES_RULE_ID",
    "P_MARGINAL_REQUIRES_BASELINE",
    "P_MARGINAL_REQUIRES_MARGINAL_RESULT",
    "P_PENETRATION_IDENTITY",
    "P_PORTFOLIO_DUPLICATE_MEMBER_ID",
    "P_PORTFOLIO_DUPLICATE_MEMBER_RUN",
    "P_PORTFOLIO_REQUIRES_OWN_RUN",
    "P_PORTFOLIO_REQUIRES_PORTFOLIO_RESULT",
    "P_SUM_IS_DIAGNOSTIC_ONLY",
    "GovernanceAllocation",
    "MarginalCredit",
    "MemberCredit",
    "PenetrationPoint",
    "PenetrationSequence",
    "PortfolioComputationError",
    "PortfolioMember",
    "PortfolioSummary",
    "combine_portfolio",
    "governance_allocation",
    "marginal_credit_from_result",
    "member_credit_from_result",
    "penetration_sequence",
    "validate_marginal_spec",
]
