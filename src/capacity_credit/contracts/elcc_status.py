"""ELCC status contracts: SEARCH outcome vs PRODUCT result (T12 round 8).

Neutral contract module: depends ONLY on the public enums (no solver
imports, no contracts -> solvers -> contracts cycle).

TWO deliberately separate rule sources:

A. SEARCH-outcome rule (``validate_search_outcome_status_combo`` /
   ``ALLOWED_SEARCH_STATUS_COMBOS``): the closed matrix of states the
   T12 equal-risk classifier can ACTUALLY produce. Served to
   solvers/equal_risk.py consumers, the ELCC result guard
   (solvers/elcc.py) and the search-trace artifact
   (contracts/search_trace.py). It still rejects valid_with_warning and
   invalid_data - the T12 classifier never produces them, and the
   search layer must not fabricate product states.

B. PRODUCT-result rule (``validate_method_result_elcc_status`` /
   ``ALLOWED_METHOD_RESULT_ELCC_STATUS_COMBOS``): serves the unified
   MethodResult 1.3 ELCC/EFC branch (contracts/results.py). It is NOT a
   mechanical reuse of the search matrix: ``result_status`` is the
   OVERALL product result status (methodology §15.1), aggregated from
   risk state, search state AND diagnostic severity:

   - ``valid`` and ``valid_with_warning`` require converged/converged
     and carry the full ELCC result (accredited capacity, matched role,
     matched risk, risk difference, valid interpolation);
   - ``valid_with_warning`` additionally requires at least one
     severity=warning diagnostic (consumer passes
     ``has_warning_diagnostic``); it can never be produced by caller
     assignment alone;
   - ``not_converged`` / ``low_resolution`` / ``invalid_method`` clear
     the point-estimate fields (the candidate 0 MW role may remain);
   - ``invalid_data`` is a STRUCTURED post-run product state: any
     (risk, search) pair is allowed (they are not a meaningful
     convergence statement for unusable data) but EVERY point-estimate
     field must be null - no fabricated capacity / matched values.
     EARLY pre-run data errors never reach a MethodResult: they are
     carried by the data-validation artifact (import validation), per
     the application-service contract (lifecycle vs result separation).
"""

from __future__ import annotations

from capacity_credit.contracts.enums import (
    ResultStatus,
    RiskConvergence,
    SearchConvergence,
)

ALLOWED_SEARCH_STATUS_COMBOS: frozenset[tuple[RiskConvergence, SearchConvergence, ResultStatus]] = (
    frozenset(
        {
            (RiskConvergence.CONVERGED, SearchConvergence.CONVERGED, ResultStatus.VALID),
            (
                RiskConvergence.CONVERGED,
                SearchConvergence.BRACKET_ONLY,
                ResultStatus.NOT_CONVERGED,
            ),
            (
                RiskConvergence.CONVERGED,
                SearchConvergence.PLATEAU_UNRESOLVED,
                ResultStatus.NOT_CONVERGED,
            ),
            (
                RiskConvergence.CONVERGED,
                SearchConvergence.NOT_CONVERGED,
                ResultStatus.NOT_CONVERGED,
            ),
            (
                RiskConvergence.CONVERGED,
                SearchConvergence.NOT_CONVERGED,
                ResultStatus.INVALID_METHOD,
            ),
            (
                RiskConvergence.NOT_CONVERGED,
                SearchConvergence.BRACKET_ONLY,
                ResultStatus.NOT_CONVERGED,
            ),
            (
                RiskConvergence.NOT_CONVERGED,
                SearchConvergence.PLATEAU_UNRESOLVED,
                ResultStatus.NOT_CONVERGED,
            ),
            (
                RiskConvergence.NOT_CONVERGED,
                SearchConvergence.NOT_CONVERGED,
                ResultStatus.NOT_CONVERGED,
            ),
            (
                RiskConvergence.NOT_CONVERGED,
                SearchConvergence.NOT_CONVERGED,
                ResultStatus.INVALID_METHOD,
            ),
            (
                RiskConvergence.LOW_EVENT_COUNT,
                SearchConvergence.BRACKET_ONLY,
                ResultStatus.LOW_RESOLUTION,
            ),
            (
                RiskConvergence.LOW_EVENT_COUNT,
                SearchConvergence.PLATEAU_UNRESOLVED,
                ResultStatus.LOW_RESOLUTION,
            ),
            (
                RiskConvergence.LOW_EVENT_COUNT,
                SearchConvergence.NOT_CONVERGED,
                ResultStatus.NOT_CONVERGED,
            ),
            (
                RiskConvergence.LOW_EVENT_COUNT,
                SearchConvergence.NOT_CONVERGED,
                ResultStatus.INVALID_METHOD,
            ),
        }
    )
)

# Backward-compatible alias (round-4/5 name).
ALLOWED_STATUS_COMBOS = ALLOWED_SEARCH_STATUS_COMBOS


class ElccStatusError(ValueError):
    """Stable status-rule violation (pydantic wraps ValueError into
    ValidationError inside model validators)."""


def validate_search_outcome_status_combo(
    *,
    risk_convergence: RiskConvergence,
    search_convergence: SearchConvergence,
    result_status: ResultStatus,
) -> None:
    """SEARCH-outcome closed contract: the (risk, search, status) triple
    must be in ALLOWED_SEARCH_STATUS_COMBOS - the states the T12
    classifier can actually produce. valid_with_warning / invalid_data
    are product states and are REJECTED here."""
    if not isinstance(risk_convergence, RiskConvergence):
        raise ElccStatusError("risk_convergence must be a RiskConvergence")
    if not isinstance(search_convergence, SearchConvergence):
        raise ElccStatusError("search_convergence must be a SearchConvergence")
    if not isinstance(result_status, ResultStatus):
        raise ElccStatusError("result_status must be a ResultStatus")
    combo = (risk_convergence, search_convergence, result_status)
    if combo not in ALLOWED_SEARCH_STATUS_COMBOS:
        raise ElccStatusError(
            f"status combination {combo} is outside the closed ELCC search-outcome matrix"
        )


# Backward-compatible alias (round-4/5 name).
validate_status_combo = validate_search_outcome_status_combo


# The search-trace ARTIFACT records the FINAL product status of the ELCC
# run (T24-B round-1): upstream scenario-bank warnings may promote a
# search-valid run to valid_with_warning, and the formal T23-A machine
# writer binds the trace status strictly to the final MethodResult
# status. The promotion combo is the ONLY product state the trace may
# carry; the search classifier itself still never produces it (the
# solvers keep using validate_search_outcome_status_combo).
_TRACE_PROMOTION_COMBO: frozenset[
    tuple[RiskConvergence, SearchConvergence, ResultStatus]
] = frozenset(
    {
        (
            RiskConvergence.CONVERGED,
            SearchConvergence.CONVERGED,
            ResultStatus.VALID_WITH_WARNING,
        ),
    }
)

ALLOWED_TRACE_STATUS_COMBOS: frozenset[
    tuple[RiskConvergence, SearchConvergence, ResultStatus]
] = frozenset(ALLOWED_SEARCH_STATUS_COMBOS | _TRACE_PROMOTION_COMBO)


def validate_trace_status_combo(
    *,
    risk_convergence: RiskConvergence,
    search_convergence: SearchConvergence,
    result_status: ResultStatus,
) -> None:
    """TRACE artifact status rule: the search-outcome matrix PLUS the
    single product promotion combo (search valid + upstream warning ->
    valid_with_warning). The trace records the FINAL result status so
    the formal machine writer binding never rejects it."""
    if not isinstance(risk_convergence, RiskConvergence):
        raise ElccStatusError("risk_convergence must be a RiskConvergence")
    if not isinstance(search_convergence, SearchConvergence):
        raise ElccStatusError("search_convergence must be a SearchConvergence")
    if not isinstance(result_status, ResultStatus):
        raise ElccStatusError("result_status must be a ResultStatus")
    combo = (risk_convergence, search_convergence, result_status)
    if combo not in ALLOWED_TRACE_STATUS_COMBOS:
        raise ElccStatusError(
            f"status combination {combo} is outside the closed search-trace status matrix"
        )


_ALL_SEARCH_PAIRS = frozenset(
    (risk, search) for risk in RiskConvergence for search in SearchConvergence
)

ALLOWED_METHOD_RESULT_ELCC_STATUS_COMBOS: frozenset[
    tuple[RiskConvergence, SearchConvergence, ResultStatus]
] = frozenset(
    {
        # converged/converged carries the full ELCC result (valid or
        # valid_with_warning - the latter needs a warning diagnostic)
        (RiskConvergence.CONVERGED, SearchConvergence.CONVERGED, ResultStatus.VALID),
        (
            RiskConvergence.CONVERGED,
            SearchConvergence.CONVERGED,
            ResultStatus.VALID_WITH_WARNING,
        ),
        # search-structured non-valid product states
        (RiskConvergence.CONVERGED, SearchConvergence.BRACKET_ONLY, ResultStatus.NOT_CONVERGED),
        (
            RiskConvergence.CONVERGED,
            SearchConvergence.PLATEAU_UNRESOLVED,
            ResultStatus.NOT_CONVERGED,
        ),
        (RiskConvergence.CONVERGED, SearchConvergence.NOT_CONVERGED, ResultStatus.NOT_CONVERGED),
        (
            RiskConvergence.CONVERGED,
            SearchConvergence.NOT_CONVERGED,
            ResultStatus.INVALID_METHOD,
        ),
        (
            RiskConvergence.NOT_CONVERGED,
            SearchConvergence.BRACKET_ONLY,
            ResultStatus.NOT_CONVERGED,
        ),
        (
            RiskConvergence.NOT_CONVERGED,
            SearchConvergence.PLATEAU_UNRESOLVED,
            ResultStatus.NOT_CONVERGED,
        ),
        (
            RiskConvergence.NOT_CONVERGED,
            SearchConvergence.NOT_CONVERGED,
            ResultStatus.NOT_CONVERGED,
        ),
        (
            RiskConvergence.NOT_CONVERGED,
            SearchConvergence.NOT_CONVERGED,
            ResultStatus.INVALID_METHOD,
        ),
        (
            RiskConvergence.LOW_EVENT_COUNT,
            SearchConvergence.BRACKET_ONLY,
            ResultStatus.LOW_RESOLUTION,
        ),
        (
            RiskConvergence.LOW_EVENT_COUNT,
            SearchConvergence.PLATEAU_UNRESOLVED,
            ResultStatus.LOW_RESOLUTION,
        ),
        (
            RiskConvergence.LOW_EVENT_COUNT,
            SearchConvergence.NOT_CONVERGED,
            ResultStatus.NOT_CONVERGED,
        ),
        (
            RiskConvergence.LOW_EVENT_COUNT,
            SearchConvergence.NOT_CONVERGED,
            ResultStatus.INVALID_METHOD,
        ),
        # invalid_data: STRUCTURED post-run product state - the data /
        # method input is unusable; risk/search are not a meaningful
        # convergence statement, so every pair is allowed and the
        # point-estimate fields must all be null (consumer-side)
    }
    | {(risk, search, ResultStatus.INVALID_DATA) for risk, search in _ALL_SEARCH_PAIRS}
    # invalid_method: STRUCTURED method-inapplicable product state - a
    # method-blocking error overrides the convergence statement, so
    # every (risk, search) pair is allowed (the severity rule requires
    # an error diagnostic)
    | {(risk, search, ResultStatus.INVALID_METHOD) for risk, search in _ALL_SEARCH_PAIRS}
)


def validate_method_result_elcc_status(
    *,
    risk_convergence: RiskConvergence,
    search_convergence: SearchConvergence,
    result_status: ResultStatus,
    has_warning: bool,
    has_method_error: bool,
    has_data_error: bool,
) -> None:
    """PRODUCT-result severity contract for the MethodResult 1.3
    ELCC/EFC branch (methodology §15.1 / §15.2 / §16). Round 7 closes
    the severity rules:

    - valid:            converged/converged, NO warning, NO error
                        (info diagnostics allowed);
    - valid_with_warning: converged/converged, >= 1 warning, NO error;
    - any error:        never coexists with valid / valid_with_warning;
                        invalid_data and invalid_method both require at
                        least one error diagnostic (the method/data
                        category distinction is enforced by the
                        diagnostic registry + product aggregation);
    - invalid_data:     requires >= 1 data_error diagnostic and no method_error;
    - invalid_method:   requires >= 1 method_error diagnostic and no data_error;
    - not_converged / low_resolution: NO error diagnostics; ordinary
                        warnings never auto-promote them to
                        valid_with_warning."""
    if not isinstance(risk_convergence, RiskConvergence):
        raise ElccStatusError("risk_convergence must be a RiskConvergence")
    if not isinstance(search_convergence, SearchConvergence):
        raise ElccStatusError("search_convergence must be a SearchConvergence")
    if not isinstance(result_status, ResultStatus):
        raise ElccStatusError("result_status must be a ResultStatus")
    if not all(
        isinstance(flag, bool)
        for flag in (has_warning, has_method_error, has_data_error)
    ):
        raise ElccStatusError("diagnostic severity/category flags must be booleans")
    if has_method_error and has_data_error:
        raise ElccStatusError(
            "a MethodResult must not mix method-blocking and data-blocking errors"
        )
    combo = (risk_convergence, search_convergence, result_status)
    if combo not in ALLOWED_METHOD_RESULT_ELCC_STATUS_COMBOS:
        raise ElccStatusError(
            f"product status combination {combo} is outside the MethodResult ELCC/EFC status rule"
        )
    has_error = has_method_error or has_data_error
    if result_status in (ResultStatus.VALID, ResultStatus.VALID_WITH_WARNING):
        if has_error:
            raise ElccStatusError(
                "result_status=valid / valid_with_warning must not carry "
                "any severity=error diagnostic"
            )
        if result_status is ResultStatus.VALID and has_warning:
            raise ElccStatusError(
                "result_status=valid must not carry any severity=warning diagnostic"
            )
        if result_status is ResultStatus.VALID_WITH_WARNING and not has_warning:
            raise ElccStatusError(
                "result_status=valid_with_warning requires at least one severity=warning diagnostic"
            )
        return
    if result_status is ResultStatus.INVALID_DATA:
        if not has_data_error or has_method_error:
            raise ElccStatusError(
                "result_status=invalid_data requires at least one data_error "
                "diagnostic and no method_error diagnostic"
            )
        return
    if result_status is ResultStatus.INVALID_METHOD:
        if not has_method_error or has_data_error:
            raise ElccStatusError(
                "result_status=invalid_method requires at least one method_error "
                "diagnostic and no data_error diagnostic"
            )
        return
    # not_converged / low_resolution
    if has_error:
        raise ElccStatusError(
            f"result_status={result_status.value} must not carry any severity=error diagnostic"
        )


def aggregate_elcc_product_status(
    *,
    risk_convergence: RiskConvergence,
    search_convergence: SearchConvergence,
    search_result_status: ResultStatus,
    has_warning: bool,
    has_method_error: bool,
    has_data_error: bool,
) -> ResultStatus:
    """PRODUCT result-status aggregation (round 7): the final product
    status is derived from the SEARCH result state + risk/search
    convergence + the MAPPED diagnostic severities/categories - never
    trusted from the caller:

    - a data-blocking error  -> invalid_data;
    - a method-blocking error -> invalid_method;
    - search valid + warning  -> valid_with_warning (point estimate kept);
    - search valid, no warning/error -> valid;
    - any non-valid search state stays as-is (warnings never promote
      not_converged / low_resolution / invalid_method)."""
    if has_data_error:
        return ResultStatus.INVALID_DATA
    if has_method_error:
        return ResultStatus.INVALID_METHOD
    if search_result_status is ResultStatus.VALID:
        if has_warning:
            return ResultStatus.VALID_WITH_WARNING
        return ResultStatus.VALID
    return search_result_status


__all__ = [
    "ALLOWED_METHOD_RESULT_ELCC_STATUS_COMBOS",
    "ALLOWED_SEARCH_STATUS_COMBOS",
    "ALLOWED_STATUS_COMBOS",
    "ALLOWED_TRACE_STATUS_COMBOS",
    "ElccStatusError",
    "aggregate_elcc_product_status",
    "validate_method_result_elcc_status",
    "validate_search_outcome_status_combo",
    "validate_status_combo",
    "validate_trace_status_combo",
]
