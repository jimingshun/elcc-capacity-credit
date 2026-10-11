"""T22-A golden manifest contract for production-path regression.

A golden manifest captures, for one real production run:

- identity & version: golden schema/case/version, method/run type,
  software & git identity, working-tree dirty flag, SHA-256 of the key
  production files of the covered chain;
- input lineage: raw input hashes, standard-layer hashes, data version
  and approval-batch references, run_config hash and
  method/application config lineage hashes;
- scenario reconstruction: scenario bank id, random seed, sample
  ids/count, weather years and weights, object random-stream version;
- results: core risk metrics, capacity value and type,
  risk/search/rMRI/result statuses, search-trace summary, diagnostic
  and warning codes, formal artifact list with real SHA-256 hashes;
- baseline governance: creation/change reason, previous version,
  before/after diff summary, method-review status and evidence refs.

Normalization boundary (stable comparison):

- EXCLUDED_METADATA keys (timestamps, UUIDs, run-relative/absolute
  paths) are RECORDED in the document but never compared;
- hashes, statuses, error codes, units, signs, null-vs-zero and
  artifact names compare EXACTLY;
- only floats under statistically-affected result paths may carry a
  per-field DECLARED tolerance (results.* risk / capacity / CI / trace
  numbers). Everything else is exact; a broad tolerance can never mask
  a change.

Golden replacement requires a documented, approved baseline change:
version > 1 needs previous_version, change_reason, change_summary,
method_review_status="approved" and evidence references. There is no
"one-click update all snapshots" and comparison never overwrites the
saved golden.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from capacity_credit.application.result_service import (
    ResultServiceError,
    read_artifact_manifest,
)

GOLDEN_SCHEMA_VERSION = "1.0"

_SHA256 = r"[0-9a-fA-F]{64}"

# Keys that are recorded as metadata but NEVER part of stable comparison
# (timestamps, UUIDs, run-relative/absolute paths - per contract:
# 生成时间、临时目录、UUID、日志顺序不得纳入稳定比较).
EXCLUDED_METADATA = frozenset(
    {
        "created_at",
        "generated_at",
        "started_at",
        "finished_at",
        "run_id",
        "result_id",
        "artifact_manifest_id",
        "import_batch_id",
        "data_version_id",
        "run_config_path",
        "sha256_raw",
        # baseline SOURCE metadata: git identity is recorded but never
        # part of cross-run stable result comparison (production code
        # changes are detected exactly through key_production_file_hashes)
        "git_commit",
        "working_tree_dirty",
        # the artifact manifest raw sha256 (verified against the file by
        # result_service.read_artifact_manifest) legitimately varies with
        # embedded run/result UUIDs, so it is recorded but excluded;
        # cross-run artifact drift is detected via
        # normalized_content_sha256
        "sha256",
    }
)

SEARCH_METHODS = frozenset({"elcc", "efc"})

# Statistically-affected float paths get this per-field tolerance; every
# other value compares exactly.
DEFAULT_FLOAT_TOLERANCE: dict[str, float] = {"rel": 1e-6, "abs": 1e-9}

# CODE-OWNED tolerance bounds: a golden manifest may only declare
# tolerances for paths inside ALLOWED_TOLERANCE_FIELDS and may never
# exceed these maxima. The whitelist is the single source for both
# document validation and payload construction, so a golden can never
# expand its own limits.
MAX_TOLERANCE_REL = 1e-3
MAX_TOLERANCE_ABS = 1e-3

_BASE_TOLERANCE_FIELDS = frozenset(
    {
        "results.risk_metrics.base.eue_mwh_per_year",
        "results.risk_metrics.base.lole_days_per_year",
        "results.risk_metrics.base.lolh_hours_per_year",
        "results.risk_metrics.base.neue_ppm",
        "results.base_risk",
        "results.confidence.lower_ci",
        "results.confidence.upper_ci",
        "results.capacity_value.search_bracket_low_mw",
        "results.capacity_value.search_bracket_high_mw",
    }
)

_SEARCH_TOLERANCE_FIELDS = frozenset(
    {
        "results.risk_metrics.candidate.eue_mwh_per_year",
        "results.risk_metrics.candidate.lole_days_per_year",
        "results.risk_metrics.candidate.lolh_hours_per_year",
        "results.risk_metrics.candidate.neue_ppm",
        "results.risk_metrics.matched.eue_mwh_per_year",
        "results.risk_metrics.matched.lole_days_per_year",
        "results.risk_metrics.matched.lolh_hours_per_year",
        "results.risk_metrics.matched.neue_ppm",
        "results.candidate_risk",
        "results.capacity_value.capacity_credit_fraction",
        "results.capacity_value.accredited_capacity_mw",
        "results.capacity_value.matched_risk",
        "results.capacity_value.risk_difference",
        "results.search_trace_summary.estimate_mw",
        "results.search_trace_summary.bracket_low_mw",
        "results.search_trace_summary.bracket_high_mw",
        "results.search_trace_summary.capacities_mw",
        "results.search_trace_summary.difference_means",
    }
)

ALLOWED_TOLERANCE_FIELDS = _BASE_TOLERANCE_FIELDS | _SEARCH_TOLERANCE_FIELDS
_ALLOWED_TOLERANCE_KEYS = frozenset({"rel", "abs"})

_RAW_INPUT_FILES = (
    "load_hourly.csv",
    "thermal_units.csv",
    "thermal_reliability_classes.csv",
    "thermal_unit_reliability_map.csv",
    "renewable_hourly.csv",
    "renewable_capacities.csv",
    "intertie_hourly.csv",
    "interties.csv",
    "weather_year_weight_sets.csv",
)


class GoldenError(ValueError):
    """Stable golden-contract error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def file_sha256(path: Path) -> str:
    # D-ARCH（2026-09-23）：对 CRLF/LF 换行免疫——Windows 上同一仓库的
    # 老检出（CRLF 落盘）与新建 worktree（LF 落盘）字节不同但语义相同，
    # 字节级哈希会让 golden 档案在不同检出之间假红。语义指纹按
    # 换行归一后的字节计算；真实内容差异仍然可区分（合同测试钉住）。
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def git_identity(repo_root: Path) -> dict[str, Any]:
    """Real git identity of the covered chain: current commit and
    working-tree dirty flag. Failure raises GoldenError - the golden
    never fabricates a commit."""
    try:
        head_proc: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            check=True,
            text=True,
        )
        commit: str = head_proc.stdout.strip()
        status_proc: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            check=True,
            text=True,
        )
        porcelain: str = status_proc.stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GoldenError("GOLDEN_GIT_UNAVAILABLE", f"cannot read git identity: {exc}") from exc
    return {
        "git_commit": commit,
        "working_tree_dirty": bool(porcelain.strip()),
    }


def production_file_hashes(repo_root: Path, rel_paths: list[str]) -> dict[str, str]:
    """SHA-256 of the key production files of the covered chain
    (repo-relative paths only; absolute paths are forbidden)."""
    hashes: dict[str, str] = {}
    for rel in sorted(rel_paths):
        path = repo_root / rel
        if not path.is_file():
            raise GoldenError("GOLDEN_PRODUCTION_FILE_MISSING", f"missing production file {rel}")
        hashes[rel] = file_sha256(path)
    return hashes


def stable_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep copy of the payload with every EXCLUDED_METADATA key
    removed (they may be recorded as metadata but never compared)."""

    def _strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: _strip(value)
                for key, value in node.items()
                if key not in EXCLUDED_METADATA
            }
        if isinstance(node, list):
            return [_strip(item) for item in node]
        return node

    return cast(dict[str, Any], _strip(payload))


def _tolerance_is_valid(tolerance: dict[str, float]) -> bool:
    """A tolerance entry is only usable when it stays inside the
    code-owned whitelist bounds: finite, non-negative, at or below the
    maxima, no unknown keys and at least one of rel/abs."""
    if not isinstance(tolerance, dict) or not tolerance:
        return False
    if not set(tolerance).issubset(_ALLOWED_TOLERANCE_KEYS):
        return False
    for key, value in tolerance.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if not math.isfinite(float(value)) or float(value) < 0.0:
            return False
        limit = MAX_TOLERANCE_REL if key == "rel" else MAX_TOLERANCE_ABS
        if float(value) > limit:
            return False
    return True


def _tolerance_for(path: str, tolerances: dict[str, dict[str, float]]) -> dict[str, float] | None:
    candidate: dict[str, float] | None = None
    if path in tolerances:
        candidate = tolerances[path]
    elif "[" in path:
        # list elements: "results.search_trace_summary.capacities_mw[0]"
        # fall back to the declared list-level tolerance.
        base = path.split("[", 1)[0]
        candidate = tolerances.get(base)
    if candidate is None or not _tolerance_is_valid(candidate):
        # an undeclared or ILLEGAL tolerance entry is never applied:
        # the value then compares exactly (fail closed, no masking).
        return None
    return candidate


def _floats_close(a: float, b: float, tol: dict[str, float]) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isnan(a) or math.isnan(b):
        return False
    rel = float(tol.get("rel", 0.0))
    abs_tol = float(tol.get("abs", 0.0))
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-12) + abs_tol


def compare_golden(saved: dict[str, Any], fresh: dict[str, Any]) -> list[dict[str, Any]]:
    """Structural comparison of the STABLE views. Diffs carry
    path/kind/expected/actual; list index paths use [i] notation."""
    tolerances = (saved.get("tolerances") or {}).get("float_fields") or {}
    diffs: list[dict[str, Any]] = []

    def _walk(expected: Any, actual: Any, path: str) -> None:
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(set(expected) | set(actual)):
                child = f"{path}.{key}" if path else str(key)
                if key not in expected:
                    diffs.append(
                        {"path": child, "kind": "extra", "expected": None, "actual": actual[key]}
                    )
                elif key not in actual:
                    diffs.append(
                        {"path": child, "kind": "missing", "expected": expected[key], "actual": None}
                    )
                else:
                    _walk(expected[key], actual[key], child)
            return
        if isinstance(expected, list) and isinstance(actual, list):
            if len(expected) != len(actual):
                diffs.append(
                    {
                        "path": path,
                        "kind": "length",
                        "expected": len(expected),
                        "actual": len(actual),
                    }
                )
            for index, (e_item, a_item) in enumerate(zip(expected, actual)):
                _walk(e_item, a_item, f"{path}[{index}]")
            return
        if isinstance(expected, bool) or isinstance(actual, bool):
            if expected != actual:
                diffs.append(
                    {"path": path, "kind": "value", "expected": expected, "actual": actual}
                )
            return
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            tol = _tolerance_for(path, tolerances)
            if tol is not None:
                if not _floats_close(float(expected), float(actual), tol):
                    diffs.append(
                        {
                            "path": path,
                            "kind": "tolerance",
                            "expected": expected,
                            "actual": actual,
                        }
                    )
            elif expected != actual:
                diffs.append(
                    {"path": path, "kind": "value", "expected": expected, "actual": actual}
                )
            return
        if expected != actual:
            diffs.append({"path": path, "kind": "value", "expected": expected, "actual": actual})

    _walk(stable_view(saved), stable_view(fresh), "")
    return diffs


def _is_sha256(value: Any) -> bool:
    import re

    return isinstance(value, str) and re.fullmatch(_SHA256, value) is not None


def _path_is_absolute(value: str) -> bool:
    if value.startswith(("/", "\\")):
        return True
    return bool(len(value) >= 2 and value[1] == ":")


def validate_golden_document(payload: dict[str, Any]) -> list[str]:
    """Semantic validation of a golden document (independent of any
    comparison): schema version, identity completeness, hash shapes,
    no absolute artifact paths, baseline-governance rules (version > 1
    requires a documented approved replacement) and the
    search-not-converged-with-point-estimate rule."""
    errors: list[str] = []
    if payload.get("golden_schema_version") != GOLDEN_SCHEMA_VERSION:
        errors.append("GOLDEN_SCHEMA_UNKNOWN: golden_schema_version must be " + GOLDEN_SCHEMA_VERSION)
    for section in (
        "golden_case_id",
        "golden_version",
        "identity",
        "input_lineage",
        "scenario_reconstruction",
        "results",
        "tolerances",
        "baseline_governance",
    ):
        if section not in payload:
            errors.append(f"GOLDEN_SECTION_MISSING: {section}")
    identity = payload.get("identity") or {}
    for identity_field in (
        "method",
        "run_type",
        "run_mode",
        "software_version",
        "git_commit",
        "working_tree_dirty",
        "key_production_file_hashes",
    ):
        if identity_field not in identity:
            errors.append(f"GOLDEN_IDENTITY_MISSING: identity.{identity_field}")
    if identity.get("method") != identity.get("run_type"):
        errors.append("GOLDEN_METHOD_RUN_TYPE_MISMATCH: identity.method must equal identity.run_type")
    if not isinstance(identity.get("working_tree_dirty"), bool):
        errors.append("GOLDEN_WORKING_TREE_DIRTY_TYPE: working_tree_dirty must be a boolean")

    lineage = payload.get("input_lineage") or {}
    for lineage_field in (
        "raw_input_hashes",
        "standard_layer_hashes",
        "data_version_hash",
        "run_config_hash",
        "method_config_hash",
        "application_config_hash",
    ):
        if lineage_field not in lineage:
            errors.append(f"GOLDEN_LINEAGE_MISSING: input_lineage.{lineage_field}")
    for hash_field in ("data_version_hash", "run_config_hash", "scenario_bank_id"):
        source = (
            lineage.get(hash_field)
            if hash_field != "scenario_bank_id"
            else (payload.get("scenario_reconstruction") or {}).get("scenario_bank_id")
        )
        if not _is_sha256(source):
            errors.append(f"GOLDEN_HASH_INVALID: {hash_field} must be a SHA-256 hex digest")
    for hash_field in ("method_config_hash", "application_config_hash"):
        value = lineage.get(hash_field)
        if value is not None and not _is_sha256(value):
            errors.append(
                f"GOLDEN_HASH_INVALID: {hash_field} must be null or a SHA-256 hex digest"
            )
    for bucket in ("raw_input_hashes", "standard_layer_hashes"):
        for rel, digest in (lineage.get(bucket) or {}).items():
            if _path_is_absolute(rel):
                errors.append(f"GOLDEN_LINEAGE_PATH_ABSOLUTE: {bucket}.{rel}")
            if not _is_sha256(digest):
                errors.append(f"GOLDEN_HASH_INVALID: {bucket}.{rel}")
    for rel, digest in (identity.get("key_production_file_hashes") or {}).items():
        if _path_is_absolute(rel):
            errors.append(f"GOLDEN_PRODUCTION_PATH_ABSOLUTE: {rel}")
        if not _is_sha256(digest):
            errors.append(f"GOLDEN_HASH_INVALID: key_production_file_hashes.{rel}")

    results = payload.get("results") or {}
    statuses = results.get("statuses") or {}
    capacity = results.get("capacity_value") or {}
    method = identity.get("method")
    has_point_estimate = (
        capacity.get("capacity_credit_fraction") is not None
        or capacity.get("accredited_capacity_mw") is not None
    )
    if method in SEARCH_METHODS and has_point_estimate and statuses.get("search_convergence") != "converged":
        errors.append(
            "GOLDEN_SEARCH_NOT_CONVERGED_POINT_ESTIMATE: "
            "a capacity point estimate requires search_convergence=converged"
        )
    for artifact in results.get("artifacts") or []:
        rel = artifact.get("relative_path")
        if not rel or _path_is_absolute(rel):
            errors.append(f"GOLDEN_ARTIFACT_PATH_ABSOLUTE: {artifact}")
        if not _is_sha256(artifact.get("sha256")):
            errors.append("GOLDEN_ARTIFACT_HASH_INVALID: artifact sha256 must be a SHA-256 hex digest")
        if not _is_sha256(artifact.get("normalized_content_sha256")):
            errors.append(
                "GOLDEN_ARTIFACT_HASH_INVALID: artifact normalized_content_sha256 "
                "must be a SHA-256 hex digest"
            )

    governance = payload.get("baseline_governance") or {}
    version = payload.get("golden_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("GOLDEN_VERSION_INVALID: golden_version must be a positive integer")
    else:
        if version == 1 and not governance.get("change_reason"):
            errors.append("GOLDEN_BASELINE_REASON_MISSING: version 1 requires a change_reason")
        if version > 1:
            missing = [
                field
                for field in ("previous_version", "change_reason", "change_summary")
                if not governance.get(field)
            ]
            if missing or governance.get("method_review_status") != "approved":
                errors.append(
                    "GOLDEN_REPLACEMENT_UNJUSTIFIED: version > 1 requires previous_version, "
                    "change_reason, change_summary and method_review_status=approved"
                )
            if not governance.get("method_review_evidence_refs"):
                errors.append(
                    "GOLDEN_REPLACEMENT_UNJUSTIFIED: version > 1 requires "
                    "method_review_evidence_refs"
                )
    # ---- tolerance whitelist / bounds (code-owned) ----
    tolerances_section = payload.get("tolerances") or {}
    float_fields = tolerances_section.get("float_fields")
    if not isinstance(float_fields, dict):
        errors.append("GOLDEN_TOLERANCES_MISSING: tolerances.float_fields must be an object")
    else:
        for path, tolerance in float_fields.items():
            if path not in ALLOWED_TOLERANCE_FIELDS:
                errors.append(f"GOLDEN_TOLERANCE_FIELD_NOT_ALLOWED: {path}")
            if not _tolerance_is_valid(tolerance):
                errors.append(f"GOLDEN_TOLERANCE_INVALID: {path}: {tolerance!r}")
    return errors


@dataclass(frozen=True)
class GoldenCase:
    """Static definition of one golden case (identity, covered chain,
    raw inputs and baseline-governance metadata)."""

    case_id: str
    method: str
    run_type: str
    run_mode: str
    dataset_id: str
    production_files: tuple[str, ...]
    raw_input_files: tuple[str, ...] = _RAW_INPUT_FILES
    golden_version: int = 1
    previous_version: int | None = None
    change_reason: str = "initial baseline"
    change_summary: str | None = None
    method_review_status: str = "awaiting_independent_review"
    method_review_evidence_refs: list[str] = field(default_factory=list)


_ADEQUACY_CHAIN = (
    "src/capacity_credit/application/atomic_io.py",
    "src/capacity_credit/application/import_service.py",
    "src/capacity_credit/application/p1_workflow.py",
    "src/capacity_credit/application/result_service.py",
    "src/capacity_credit/application/run_service.py",
    "src/capacity_credit/application/scenario_service.py",
    "src/capacity_credit/contracts/artifacts.py",
    "src/capacity_credit/contracts/enums.py",
    "src/capacity_credit/contracts/results.py",
    "src/capacity_credit/contracts/runs.py",
    "src/capacity_credit/engine/adequacy.py",
    "src/capacity_credit/engine/balance.py",
    "src/capacity_credit/engine/convergence.py",
    "src/capacity_credit/engine/events.py",
    "src/capacity_credit/engine/metrics.py",
    "src/capacity_credit/engine/online_stats.py",
    "src/capacity_credit/scenarios/random_streams.py",
    "src/capacity_credit/scenarios/scenario_bank.py",
    "src/capacity_credit/scenarios/thermal_states.py",
)

_ELCC_EXTRA_CHAIN = (
    "src/capacity_credit/application/elcc_workflow.py",
    "src/capacity_credit/application/risk_series.py",
    "src/capacity_credit/contracts/elcc_config.py",
    "src/capacity_credit/contracts/elcc_diagnostic_specs.py",
    "src/capacity_credit/contracts/elcc_method_config.py",
    "src/capacity_credit/contracts/elcc_status.py",
    "src/capacity_credit/contracts/search_trace.py",
    "src/capacity_credit/solvers/elcc.py",
    "src/capacity_credit/solvers/paired_evaluation.py",
)

_EFC_EXTRA_CHAIN = (
    "src/capacity_credit/application/efc_workflow.py",
    "src/capacity_credit/contracts/efc_config.py",
    "src/capacity_credit/contracts/efc_method_config.py",
    "src/capacity_credit/contracts/elcc_diagnostics.py",
    "src/capacity_credit/solvers/efc.py",
    "src/capacity_credit/solvers/paired_evaluation.py",
)

_MRI_EXTRA_CHAIN = (
    "src/capacity_credit/application/mri_workflow.py",
    "src/capacity_credit/application/risk_series.py",
    "src/capacity_credit/contracts/mri_contracts.py",
    "src/capacity_credit/contracts/risk_convergence.py",
    "src/capacity_credit/solvers/mri.py",
)

_PORTFOLIO_EXTRA_CHAIN = (
    "src/capacity_credit/application/portfolio_service.py",
    "src/capacity_credit/application/coordination_run_service.py",
    "src/capacity_credit/application/efc_workflow.py",
    "src/capacity_credit/solvers/portfolio.py",
    "src/capacity_credit/contracts/efc_config.py",
)

_SENSITIVITY_EXTRA_CHAIN = (
    "src/capacity_credit/application/sensitivity_service.py",
    "src/capacity_credit/application/coordination_run_service.py",
    "src/capacity_credit/application/portfolio_service.py",
    "src/capacity_credit/application/efc_workflow.py",
    "src/capacity_credit/solvers/portfolio.py",
)

CASE_ADQ = GoldenCase(
    case_id="adequacy_teaching_minimal",
    method="adequacy",
    run_type="adequacy",
    run_mode="teaching",
    dataset_id="DS_T22A_GOLDEN_ADQ",
    production_files=_ADEQUACY_CHAIN,
    change_reason="initial baseline (T22-A first minimal production-path golden)",
)

CASE_EFC = GoldenCase(
    case_id="efc_teaching_minimal",
    method="efc",
    run_type="efc",
    run_mode="teaching",
    dataset_id="DS_T22B_GOLDEN_EFC",
    production_files=_ADEQUACY_CHAIN + _EFC_EXTRA_CHAIN,
    change_reason="initial baseline (T22-B2 extended production-path golden)",
)

CASE_MRI = GoldenCase(
    case_id="mri_teaching_minimal",
    method="mri",
    run_type="mri",
    run_mode="teaching",
    dataset_id="DS_T22B_GOLDEN_MRI",
    production_files=_ADEQUACY_CHAIN + _MRI_EXTRA_CHAIN,
    change_reason="initial baseline (T22-B2 extended production-path golden)",
)

CASE_PORTFOLIO = GoldenCase(
    case_id="portfolio_teaching_minimal",
    method="portfolio",
    run_type="portfolio",
    run_mode="teaching",
    dataset_id="DS_T22B_GOLDEN_PORTFOLIO",
    production_files=_ADEQUACY_CHAIN + _PORTFOLIO_EXTRA_CHAIN,
    change_reason="initial baseline (T22-B2 extended production-path golden)",
)

CASE_SENSITIVITY = GoldenCase(
    case_id="sensitivity_teaching_minimal",
    method="sensitivity",
    run_type="sensitivity",
    run_mode="teaching",
    dataset_id="DS_T22B_GOLDEN_SENSITIVITY",
    production_files=_ADEQUACY_CHAIN + _SENSITIVITY_EXTRA_CHAIN,
    change_reason="initial baseline (T22-B2 extended production-path golden)",
)

CASE_ELCC = GoldenCase(
    case_id="elcc_teaching_minimal",
    method="elcc",
    run_type="elcc",
    run_mode="teaching",
    dataset_id="DS_T22A_GOLDEN_ELCC",
    production_files=_ADEQUACY_CHAIN + _ELCC_EXTRA_CHAIN,
    change_reason="initial baseline (T22-A first minimal production-path golden)",
)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise GoldenError("GOLDEN_RUN_ARTIFACT_MISSING", f"{label} missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise GoldenError("GOLDEN_RUN_ARTIFACT_CORRUPT", f"{label} corrupt: {path}") from exc
    if not isinstance(value, dict):
        raise GoldenError("GOLDEN_RUN_ARTIFACT_CORRUPT", f"{label} must be an object: {path}")
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    import yaml

    if not path.is_file():
        raise GoldenError("GOLDEN_RUN_ARTIFACT_MISSING", f"run_config missing: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise GoldenError("GOLDEN_RUN_ARTIFACT_CORRUPT", f"run_config corrupt: {path}") from exc
    if not isinstance(value, dict):
        raise GoldenError("GOLDEN_RUN_ARTIFACT_CORRUPT", f"run_config must be an object: {path}")
    return value


def _dir_hashes(directory: Path, file_names: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in sorted(file_names):
        path = directory / name
        if not path.is_file():
            raise GoldenError("GOLDEN_INPUT_FILE_MISSING", f"input file missing: {path}")
        hashes[f"data/{name}"] = file_sha256(path)
    return hashes


def _weather_year_weights(project_dir: Path, weight_set_id: str) -> dict[str, float]:
    path = project_dir / "data" / "weather_year_weight_sets.csv"
    weights: dict[str, float] = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("weight_set_id") == weight_set_id:
                weights[str(row.get("weather_year"))] = float(row["weight"])
    if not weights:
        raise GoldenError(
            "GOLDEN_WEIGHTS_MISSING",
            f"no weights found for weight_set_id={weight_set_id} in {path}",
        )
    return weights


def _search_trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    points = trace.get("points") or []
    return {
        "point_count": len(points),
        "estimate_mw": trace.get("estimate_mw"),
        "bracket_low_mw": trace.get("bracket_low_mw"),
        "bracket_high_mw": trace.get("bracket_high_mw"),
        "actions": [p.get("action") for p in points],
        "capacities_mw": [p.get("capacity_mw") for p in points],
        "difference_means": [p.get("difference_mean") for p in points],
        "sample_counts": [p.get("sample_count") for p in points],
    }


def coordination_normalized(payload: dict[str, Any], method: str) -> dict[str, Any]:
    """THE single coordination-result normalization for portfolio /
    sensitivity golden cases: stable view PLUS run-identity UUID values
    and run-identity spec hashes that legitimately differ between runs
    (portfolio_run_id / base_run_id / analysis_spec_hash /
    sensitivity_analysis_spec_hash) are removed. Both production
    extraction branches and the regression tests call this ONE
    function - no parallel normalization is maintained."""
    stripped = stable_view(payload)
    if method == "portfolio":
        stripped.pop("portfolio_run_id", None)
        stripped.pop("analysis_spec_hash", None)
    elif method == "sensitivity":
        stripped.pop("base_run_id", None)
        stripped.pop("sensitivity_analysis_spec_hash", None)
    return stripped


def artifact_entries_with_closed_loop(
    run_dir: Path,
    *,
    expected_run_id: str | None = None,
    expected_result_id: str | None = None,
    strip_keys: tuple[str, ...] = (),
    case_method_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Read and verify the formal artifact manifest by REUSING the
    existing closed-loop entry
    (capacity_credit.application.result_service.read_artifact_manifest):
    strict manifest model, run/result id expectations, result-link
    cross-check, safe relative paths, schema versions and the RAW
    sha256 closed loop against the actual artifact files. No second
    artifact verifier is maintained here.

    Each returned entry keeps the VERIFIED manifest raw sha256 under
    ``sha256`` and adds the normalized stable-content hash under
    ``normalized_content_sha256`` (excluded metadata such as embedded
    run/result UUIDs stripped), which is the cross-run comparable
    artifact digest.
    """
    manifest_path = run_dir / "artifact_manifest.json"
    result_path = run_dir / "method_result.json"
    try:
        manifest_model = read_artifact_manifest(
            manifest_path,
            result_path=result_path,
            expected_run_id=expected_run_id,
            expected_result_id=expected_result_id,
        )
    except ResultServiceError as exc:
        raise GoldenError(
            "GOLDEN_ARTIFACT_MANIFEST_INVALID",
            f"artifact closed-loop verification failed: {exc.message}",
        ) from exc
    entries: list[dict[str, Any]] = []
    for entry in manifest_model.artifacts:
        raw = entry.model_dump(mode="json")
        relative = raw.get("relative_path")
        artifact_file = run_dir / str(relative)
        if not artifact_file.is_file():
            raise GoldenError(
                "GOLDEN_ARTIFACT_FILE_MISSING",
                f"artifact file missing: {artifact_file}",
            )
        if str(artifact_file).lower().endswith(".json"):
            content = json.loads(artifact_file.read_text(encoding="utf-8-sig"))
            if strip_keys and case_method_hint is not None:
                stripped = coordination_normalized(content, case_method_hint)
            else:
                stripped = stable_view(content)
            normalized = json.dumps(
                stripped, sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
            digest = hashlib.sha256(normalized).hexdigest()
        else:
            digest = file_sha256(artifact_file)
        entries.append(
            {
                "artifact_type": raw.get("artifact_type"),
                "relative_path": relative,
                "schema_or_template_version": raw.get("schema_or_template_version"),
                "sha256": raw.get("sha256"),
                "normalized_content_sha256": digest,
            }
        )
    return sorted(entries, key=lambda a: str(a.get("relative_path")))

def build_golden_payload(
    *,
    project_dir: Path | str,
    run_id: str,
    case: GoldenCase,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Extract a golden payload from the PERSISTED production artifacts
    of one real run (run_manifest.json, method_result.json,
    artifact_manifest.json, search_trace.json, run_config.yaml, the
    frozen version copy and the scenario-bank index). Nothing is
    hand-written; every hash is computed from the actual files.

    Only relative names enter the payload - absolute project/tmp paths
    are never stored (and the excluded-metadata keys below are recorded
    but never compared).
    """
    project = Path(project_dir)
    root = Path(repo_root) if repo_root is not None else project.parents[2]
    run_dir = project / "runs" / run_id
    if not run_dir.is_dir():
        raise GoldenError("GOLDEN_RUN_DIR_MISSING", f"run directory missing: {run_dir}")
    manifest = _read_json(run_dir / "run_manifest.json", "run_manifest")
    method_result = None
    if case.method not in ("portfolio", "sensitivity"):
        method_result = _read_json(run_dir / "method_result.json", "method_result")
    run_config = _read_yaml(run_dir / "run_config.yaml")

    bank_id = manifest.get("scenario_bank_id")
    bank_index = None
    if bank_id is not None:
        bank_index = _read_json(
            project / "artifacts" / "scenario_banks" / str(bank_id) / "index.json",
            "scenario bank index",
        )

    trace_path = run_dir / "search_trace.json"
    trace = _read_json(trace_path, "search_trace") if trace_path.is_file() else None

    dataset_id = manifest.get("dataset_id")
    data_version_id = manifest.get("data_version_id")
    version_dir = project / "versions" / str(dataset_id) / str(data_version_id) / "data"

    coordination_strip_keys = (
        ("portfolio_run_id", "analysis_spec_hash")
        if case.method == "portfolio"
        else ("base_run_id", "sensitivity_analysis_spec_hash")
        if case.method == "sensitivity"
        else ()
    )
    artifacts = artifact_entries_with_closed_loop(
        run_dir,
        expected_run_id=str(manifest.get("run_id")) if manifest.get("run_id") else None,
        expected_result_id=(
            method_result.get("result_id") if method_result is not None else None
        ),
        strip_keys=coordination_strip_keys,
        case_method_hint=case.method,
    )
    objects: dict[str, list[str]] = {}
    if bank_index is not None:
        for obj in bank_index.get("objects") or []:
            namespace = str(obj.get("namespace", "unknown"))
            objects.setdefault(namespace, []).append(str(obj.get("object_id")))
        for namespace, object_ids in objects.items():
            objects[namespace] = sorted(object_ids)

    weights = _weather_year_weights(project, str(run_config.get("weather_year_weight_set_id")))

    risk_metrics: dict[str, Any] = {}
    if method_result is not None:
        metrics = method_result.get("risk_metrics") or {}
        for role in ("base", "candidate", "matched"):
            if metrics.get(role) is not None:
                risk_metrics[role] = dict(metrics[role])

    if method_result is None:
        capacity_value = {
            "capacity_value_type": None,
            "capacity_credit_fraction": None,
            "accredited_capacity_mw": None,
            "reference_capacity_mw": None,
            "matched_risk": None,
            "risk_difference": None,
            "search_bracket_low_mw": None,
            "search_bracket_high_mw": None,
            "interpolation_method": None,
            "resource_or_portfolio_id": None,
        }
    else:
        capacity_value = {
            "capacity_value_type": method_result.get("capacity_value_type"),
            "capacity_credit_fraction": method_result.get("capacity_credit_fraction"),
            "accredited_capacity_mw": method_result.get("accredited_capacity_mw"),
            "reference_capacity_mw": method_result.get("reference_capacity_mw"),
            "matched_risk": method_result.get("matched_risk"),
            "risk_difference": method_result.get("risk_difference"),
            "search_bracket_low_mw": method_result.get("search_bracket_low_mw"),
            "search_bracket_high_mw": method_result.get("search_bracket_high_mw"),
            "interpolation_method": method_result.get("interpolation_method"),
            "resource_or_portfolio_id": method_result.get("resource_or_portfolio_id"),
        }

    diagnostic_codes = (
        sorted({d.get("code") for d in method_result.get("diagnostics") or []})
        if method_result is not None
        else []
    )

    method = case.method
    results: dict[str, Any]
    if method == "portfolio":
        summary_path = run_dir / "portfolio_summary.json"
        if not summary_path.is_file():
            raise GoldenError("GOLDEN_RUN_ARTIFACT_MISSING", f"portfolio_summary missing: {summary_path}")
        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        stripped_summary = coordination_normalized(summary, "portfolio")
        normalized_summary = json.dumps(
            stripped_summary, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        results = {
            "method": method,
            "result_payload": {
                "portfolio_summary_normalized_sha256": hashlib.sha256(normalized_summary).hexdigest(),
            },
            "statuses": {
                "result_status": None,
                "risk_convergence": None,
                "search_convergence": "not_applicable",
                "rmri_status": None,
            },
            "diagnostic_codes": [],
            "search_trace_summary": None,
            "artifacts": artifacts,
        }
    elif method == "sensitivity":
        matrix_path = run_dir / "sensitivity_matrix.json"
        if not matrix_path.is_file():
            raise GoldenError("GOLDEN_RUN_ARTIFACT_MISSING", f"sensitivity_matrix missing: {matrix_path}")
        matrix = json.loads(matrix_path.read_text(encoding="utf-8-sig"))
        stripped_matrix = coordination_normalized(matrix, "sensitivity")
        normalized_matrix = json.dumps(
            stripped_matrix, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        results = {
            "method": method,
            "result_payload": {
                "sensitivity_matrix_normalized_sha256": hashlib.sha256(normalized_matrix).hexdigest(),
            },
            "statuses": {
                "result_status": None,
                "risk_convergence": None,
                "search_convergence": "not_applicable",
                "rmri_status": None,
            },
            "diagnostic_codes": [],
            "search_trace_summary": None,
            "artifacts": artifacts,
        }
    elif method in ("mri", "rmri"):
        if method_result is None:
            raise GoldenError(
                "GOLDEN_METHOD_RESULT_MISSING",
                f"method_result.json required for method={method} but is missing",
            )
        results = {
            "method": method_result.get("method"),
            "primary_risk_metric": method_result.get("primary_risk_metric"),
            "sample_count": method_result.get("sample_count"),
            "mri_value": method_result.get("mri_value"),
            "mri_unit": method_result.get("mri_unit"),
            "perfect_capacity_mri_value": method_result.get("perfect_capacity_mri_value"),
            "rmri_value": method_result.get("rmri_value"),
            "rmri_status": method_result.get("rmri_status"),
            "requested_difference_scheme": method_result.get("requested_difference_scheme"),
            "applied_difference_scheme": method_result.get("applied_difference_scheme"),
            "base_step": method_result.get("base_step"),
            "step_unit": method_result.get("step_unit"),
            "step_multipliers": method_result.get("step_multipliers"),
            "assessment_anchor": method_result.get("assessment_anchor"),
            "perturbation_dimension": method_result.get("perturbation_dimension"),
            "capacity_value": capacity_value,
            "statuses": {
                "result_status": method_result.get("result_status"),
                "risk_convergence": method_result.get("risk_convergence"),
                "search_convergence": method_result.get("search_convergence"),
                "rmri_status": method_result.get("rmri_status"),
            },
            "diagnostic_codes": diagnostic_codes,
            "search_trace_summary": None,
            "artifacts": artifacts,
        }
    else:
        if method_result is None:
            raise GoldenError(
                "GOLDEN_METHOD_RESULT_MISSING",
                f"method_result.json required for method={method} but is missing",
            )
        results = {
            "method": method_result.get("method"),
            "primary_risk_metric": method_result.get("primary_risk_metric"),
            "sample_count": method_result.get("sample_count"),
            "risk_metrics": risk_metrics,
            "base_risk": method_result.get("base_risk"),
            "candidate_risk": method_result.get("candidate_risk"),
            "capacity_value": capacity_value,
            "statuses": {
                "result_status": method_result.get("result_status"),
                "risk_convergence": method_result.get("risk_convergence"),
                "search_convergence": method_result.get("search_convergence"),
                "rmri_status": method_result.get("rmri_status"),
            },
            "confidence": {
                "confidence_level": method_result.get("confidence_level"),
                "lower_ci": method_result.get("lower_ci"),
                "upper_ci": method_result.get("upper_ci"),
            },
            "units": {
                "capacity_unit": "mw",
                "energy_unit": "mwh",
                "risk_metric_unit": (trace.get("risk_metric_unit") if trace else "per_year"),
            },
            "diagnostic_codes": diagnostic_codes,
            "search_trace_summary": _search_trace_summary(trace) if trace else None,
            "artifacts": artifacts,
        }

    if bank_index is None:
        sample_count = None
        weather_years: list[int] = []
        random_seed = run_config.get("random_seed")
    else:
        sample_count_raw = bank_index.get("sample_count")
        if not isinstance(sample_count_raw, int):
            raise GoldenError("GOLDEN_BANK_INDEX_CORRUPT", "scenario bank index sample_count must be an int")
        sample_count = sample_count_raw
        weather_years = [int(y) for y in bank_index.get("weather_years") or []]
        random_seed_raw = run_config.get("random_seed")
        if not isinstance(random_seed_raw, int):
            raise GoldenError("GOLDEN_RUN_CONFIG_CORRUPT", "run_config random_seed must be an int")
        random_seed = random_seed_raw

    # tolerances are constructed FROM the code-owned whitelist only:
    # a golden can never declare a tolerance for a field the code does
    # not allow, and can never exceed the code maxima. coordination-style
    # cases (portfolio/sensitivity) and mri/rmri keep an EMPTY whitelist
    # (all fields compare strictly).
    tolerances: dict[str, dict[str, float]]
    if method in ("mri", "rmri", "portfolio", "sensitivity"):
        tolerances = {}
    else:
        allowed_tolerance_fields = (
            ALLOWED_TOLERANCE_FIELDS if method in SEARCH_METHODS else _BASE_TOLERANCE_FIELDS
        )
        tolerances = {
            path: dict(DEFAULT_FLOAT_TOLERANCE) for path in sorted(allowed_tolerance_fields)
        }
    git = git_identity(root)
    payload: dict[str, Any] = {
        "golden_schema_version": GOLDEN_SCHEMA_VERSION,
        "golden_case_id": case.case_id,
        "golden_version": case.golden_version,
        "identity": {
            "method": case.method,
            "run_type": manifest.get("run_type"),
            "run_mode": manifest.get("run_mode"),
            "software_version": manifest.get("software_version"),
            "git_commit": git["git_commit"],
            "working_tree_dirty": git["working_tree_dirty"],
            "key_production_file_hashes": production_file_hashes(root, list(case.production_files)),
            "run_id": manifest.get("run_id"),
        },
        "input_lineage": {
            "dataset_id": dataset_id,
            "raw_input_hashes": _dir_hashes(project / "data", list(case.raw_input_files)),
            "standard_layer_hashes": _dir_hashes(version_dir, list(case.raw_input_files)),
            "data_version_hash": manifest.get("data_version_hash"),
            "data_version_id": data_version_id,
            "import_batch_id": manifest.get("import_batch_id"),
            "run_config_path": manifest.get("run_config_path"),
            "run_config_hash": manifest.get("run_config_hash"),
            "method_config_hash": manifest.get("method_config_hash"),
            "application_config_hash": manifest.get("application_config_hash"),
        },
        "scenario_reconstruction": {
            "scenario_bank_id": bank_id,
            "random_seed": random_seed,
            "sample_count": sample_count,
            "sample_id_first": 0 if sample_count else None,
            "sample_id_last": (sample_count - 1) if sample_count else None,
            "weather_years": weather_years,
            "weather_year_weights": (weights if bank_index is not None else {}),
            "object_stream_version": str(bank_index.get("object_stream_version")) if bank_index else None,
            "object_ids": (objects if bank_index is not None else {}),
            "timezone": bank_index.get("timezone") if bank_index else None,
        },
        "results": results,
        "tolerances": {"float_fields": tolerances},
        "baseline_governance": {
            "golden_version": case.golden_version,
            "previous_version": case.previous_version,
            "change_reason": case.change_reason,
            "change_summary": case.change_summary,
            "method_review_status": case.method_review_status,
            "method_review_evidence_refs": list(case.method_review_evidence_refs),
            "created_at": datetime.now(UTC).isoformat(),
        },
    }
    return payload


def _validated_golden(payload: dict[str, Any], context: str) -> dict[str, Any]:
    """Full document validation shared by load and write: an illegal
    golden can never be loaded, written or compared."""
    if not isinstance(payload, dict):
        raise GoldenError("GOLDEN_DOCUMENT_INVALID", f"{context}: golden must be an object")
    errors = validate_golden_document(payload)
    if errors:
        raise GoldenError(
            "GOLDEN_DOCUMENT_INVALID",
            f"{context}: document validation failed: {'; '.join(errors)}",
        )
    return payload


def load_golden(path: Path | str) -> dict[str, Any]:
    return _validated_golden(_read_json(Path(path), "golden manifest"), "load_golden")


def write_golden(payload: dict[str, Any], path: Path | str) -> None:
    _validated_golden(payload, "write_golden")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if target.is_file() and target.read_bytes() != data:
        raise GoldenError(
            "GOLDEN_OVERWRITE_FORBIDDEN",
            f"refusing to overwrite existing golden {target}; update requires a governed "
            "version replacement (never one-click snapshot refresh)",
        )
    target.write_bytes(data)
