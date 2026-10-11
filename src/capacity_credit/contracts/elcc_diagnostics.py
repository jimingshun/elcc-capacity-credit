"""ELCC diagnostic conversion over the neutral registry (T12 round 8,
extended in round 9 with the upstream aggregation boundary).

SINGLE source for converting the ELCC solver / equal-risk search string
diagnostics AND the frozen scenario-bank upstream diagnostics into
formal ``Diagnostic`` models (code -> severity + category + message).
Rules:

- every known code has exactly one severity, one category
  (info / warning / method_error / data_error) and one group
  (scenario_input / resource_model / risk_search / blocking);
- ``ELCC_CI_NOT_AVAILABLE`` is info and appears at most once (dedup by
  code);
- ``SEARCH_NOT_MONOTONIC`` is a blocking METHOD error, consistent with
  the search classifier producing result_status=invalid_method;
- an UNKNOWN code raises ``ElccDiagnosticsError`` with the stable code
  ``ELCC_DIAGNOSTIC_UNKNOWN`` - never silently dropped, never defaulted
  to warning, never given a caller-dependent severity;
- upstream entries are validated against the SAME registry: the caller
  can never reclassify a severity (fail closed with
  ``ELCC_DIAGNOSTIC_CLASSIFICATION_MISMATCH``);
- the merged list is deduplicated by code and sorted by code - the
  output never depends on the original object ordering;
- categories drive the product-status aggregation in
  ``contracts.elcc_status`` (method_error -> invalid_method,
  data_error -> invalid_data).

No data-error producer exists in T12 (early pre-run data errors are
carried by the data-validation artifact and never reach a MethodResult);
the data_error category is reserved and tested via structured inputs.
"""

from __future__ import annotations

from typing import Any

from capacity_credit.contracts.elcc_diagnostic_specs import (
    ELCC_DIAGNOSTIC_REGISTRY,
    DiagnosticCategory,
    DiagnosticGroup,
    DiagnosticSpec,
    ElccDiagnosticsError,
    diagnostic_spec,
    validate_diagnostic_classification,
)
from capacity_credit.contracts.enums import Severity
from capacity_credit.contracts.results import Diagnostic


def convert_elcc_diagnostics(codes: list[str]) -> list[Diagnostic]:
    """Convert solver/search diagnostic codes to formal Diagnostic
    models. Unknown codes raise ElccDiagnosticsError; duplicates are
    written exactly once (ELCC_CI_NOT_AVAILABLE appears at most once)."""
    seen: set[str] = set()
    converted: list[Diagnostic] = []
    for code in codes:
        if code in seen:
            continue
        spec = diagnostic_spec(code)
        converted.append(Diagnostic(code=code, severity=spec.severity, message=spec.message))
        seen.add(code)
    return converted


def collect_upstream_diagnostics(
    *,
    object_entries: list[dict[str, Any]],
    probability_baseline_years: list[int],
) -> list[dict[str, str]]:
    """Collect the frozen scenario-bank upstream diagnostics (round 9).

    The ELCC workflow reads these from the OPENED, integrity-validated
    ScenarioBank - it never re-guesses thermal parameters:

    - ``object_entries``: ``bank.index["objects"]`` entries; each entry
      may carry a ``diagnostics`` list of {code, severity, message};
    - ``probability_baseline_years``: ``bank.index["probability_baseline_years"]``
      (stress_test years are excluded by the bank contract itself);
    - exactly ONE probability-baseline year adds
      ``SINGLE_WEATHER_YEAR`` with the registry severity/message;
      multiple (or zero) baseline years add nothing.

    Returns RAW entries sorted by code (never by object order) with
    duplicates collapsed; the object scan itself is deterministic
    (objects are visited in object_id order, so an identical code
    repeated across objects keeps the first message deterministically).
    Validation against the registry happens later in
    ``aggregate_elcc_diagnostics`` (fail closed)."""
    by_code: dict[str, dict[str, str]] = {}
    ordered_objects = sorted(
        (obj for obj in object_entries if isinstance(obj, dict)),
        key=lambda obj: str(obj.get("object_id", "")),
    )
    for obj in ordered_objects:
        raw = obj.get("diagnostics") or []
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not isinstance(code, str) or not code or code in by_code:
                continue
            severity = item.get("severity")
            message = item.get("message")
            by_code[code] = {
                "code": code,
                "severity": str(severity) if severity is not None else "",
                "message": str(message) if message is not None else "",
            }
    baseline_count = len(list(probability_baseline_years))
    if baseline_count == 1:
        spec = diagnostic_spec("SINGLE_WEATHER_YEAR")
        by_code["SINGLE_WEATHER_YEAR"] = {
            "code": "SINGLE_WEATHER_YEAR",
            "severity": spec.severity.value,
            "message": spec.message,
        }
    return [by_code[code] for code in sorted(by_code)]


def aggregate_elcc_diagnostics(
    upstream: list[dict[str, Any]],
    solver_codes: list[str],
) -> list[Diagnostic]:
    """Merge frozen upstream diagnostics and solver/search codes into
    ONE canonical formal diagnostic list (round 9).

    - every upstream entry is validated against the registry: known
      code (fail closed), severity must equal the registry severity
      (the caller can never reclassify), the message must be a
      non-empty string when present (otherwise the registry message is
      used);
    - every solver code is validated against the same registry;
    - the merged list is deduplicated by code and sorted by code -
      the output never depends on the original object ordering;
    - when a code appears both upstream and in the solver list, the
      upstream (frozen) entry wins."""
    canonical: dict[str, Diagnostic] = {}
    for raw in upstream:
        if not isinstance(raw, dict):
            raise ElccDiagnosticsError(
                "ELCC_DIAGNOSTIC_INVALID_ENTRY",
                f"upstream diagnostic entry must be an object, got {raw!r}",
            )
    ordered_upstream = sorted(
        upstream,
        key=lambda entry: str(entry.get("code", "")),
    )
    for entry in ordered_upstream:
        code = entry.get("code")
        if not isinstance(code, str) or not code:
            raise ElccDiagnosticsError(
                "ELCC_DIAGNOSTIC_INVALID_ENTRY",
                f"upstream diagnostic entry must carry a non-empty string code, got {entry!r}",
            )
        spec = diagnostic_spec(code)
        raw_severity = entry.get("severity")
        try:
            severity = (
                raw_severity if isinstance(raw_severity, Severity) else Severity(raw_severity)
            )
        except ValueError as exc:
            raise ElccDiagnosticsError(
                "ELCC_DIAGNOSTIC_CLASSIFICATION_MISMATCH",
                f"upstream diagnostic {code!r} carries unparsable severity {raw_severity!r}",
            ) from exc
        validate_diagnostic_classification(code, severity)
        message = entry.get("message")
        if message is None or (isinstance(message, str) and not message.strip()):
            message = spec.message
        elif not isinstance(message, str):
            raise ElccDiagnosticsError(
                "ELCC_DIAGNOSTIC_INVALID_ENTRY",
                f"upstream diagnostic {code!r} message must be a string",
            )
        canonical.setdefault(
            code,
            Diagnostic(code=code, severity=spec.severity, message=message),
        )
    for code in solver_codes:
        spec = diagnostic_spec(code)
        canonical.setdefault(
            code,
            Diagnostic(code=code, severity=spec.severity, message=spec.message),
        )
    return [canonical[code] for code in sorted(canonical)]


def diagnostic_category(code: str) -> DiagnosticCategory:
    """Category of a known diagnostic code (raises on unknown)."""
    return diagnostic_spec(code).category


__all__ = [
    "ELCC_DIAGNOSTIC_REGISTRY",
    "DiagnosticCategory",
    "DiagnosticGroup",
    "DiagnosticSpec",
    "ElccDiagnosticsError",
    "aggregate_elcc_diagnostics",
    "collect_upstream_diagnostics",
    "convert_elcc_diagnostics",
    "diagnostic_category",
]
