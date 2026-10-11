"""T24-C reproduction bundles - SINGLE shared implementation.

Every bundle type decision, file inventory, hash check, path-safety
check, integrity check and rerun-eligibility decision lives HERE. CLI,
Excel/HTML reports and the business demo only call this shared service -
no second bundle implementation exists anywhere.

Package contract (T24-C round-1 rectification - NO second schema):

- The package-level manifest ``artifact-manifest.json`` IS the existing
  formal ``ArtifactManifest`` (artifact-manifest 1.0, pydantic-validated,
  result-bound with run_id/result_id) - the unmodelled
  ``bundle-manifest/1.0`` dict is gone. The directory bundle is declared
  through exactly ONE ``ArtifactEntry`` whose ``artifact_type`` is the
  existing ``ArtifactType.VERIFY_BUNDLE`` / ``ArtifactType.RERUN_BUNDLE``
  and whose ``relative_path`` is the child descriptor ``bundle.json``.
- ``bundle.json`` is the child file manifest (per-file path / size /
  SHA-256 plus rerun metadata). It is FORMALlY BOUND by the package
  ArtifactManifest through that path + SHA-256 entry: editing ANY
  descriptor field (bundle_type, rerun flags, confirmation, command,
  commits) breaks the recorded hash and is stably rejected.
- Bundle type is derived from the manifest entry artifact_type and
  cross-checked against the descriptor - editing JSON can never promote
  a verify_bundle into a rerun_bundle; rerun eligibility is ALWAYS
  re-derived from the real packaged files, never trusted from any
  manifest boolean.
- ``source_artifact_manifest`` (the source run's demo artifact manifest)
  is carried verbatim; the source run identity (run_id / result_id /
  method / report_mapping_version / software_commit) is cross-checked
  against it, run_summary.json and the reproducibility manifest. The
  packaging HEAD is recorded separately as ``packager_software_commit``
  and NEVER overwrites ``source_software_commit``.
- Source validation at create time is the SHARED
  ``capacity_credit.demo.business_demo.validate_result_root`` gate (same
  function the demo replay uses) - never a second approximate validator.
- verify_bundle carries machine results + reports + config/input
  HASHES only - NEVER the input/data model-ready body - and states that
  it cannot independently rerun. rerun_bundle additionally carries the
  allowlisted model-ready input files and a VERIFIED dependency lock
  file (path + SHA-256 + CONTENT gate) under dependencies/.
- Rerun reachability + clean surface (round-2/3): a rerun_bundle is
  only created when the SHARED business_demo.source_identity gate
  passes (resolvable HEAD, no tracked/untracked changes on the
  controlled execution surface src / schemas / benchmarks/business-demo
  - BUNDLE_SOFTWARE_IDENTITY_DIRTY otherwise) AND the source run's
  software commit EQUALS that HEAD - the only safe proof that
  bundles.py and the ``report bundle rerun`` entry existed in the
  recorded source commit AND the executing code IS that commit.
  Historical results stay verifiable as verify_bundle (rerun_eligible
  stays false). The recorded rerun_command must bind --config to the
  package's frozen input/demo-config.yaml - the very file rerun_bundle
  executes - so the recorded command and the executed path can never
  diverge.
- Dependency locks (round-2): the only supported format is
  dependencies/requirements-lock.txt with ``package==exact_version``
  lines covering every required dependency (PEP 503 name
  normalization, no ranges/wildcards/duplicates). create validates the
  content against the CURRENT installed versions; verify re-parses the
  packaged lock against the descriptor-FROZEN versions - the SHA check
  is kept but never replaces the content gate.
- Input collection is a DECLARED ALLOWLIST from the validated source
  artifact-manifest input_identity - never a recursive directory copy.
  Sensitive names (.env, keys, credentials, logs, caches, tmp, hidden,
  archives) are rejected even if someone registers them, and source
  symlink/junction/escape paths are stably rejected.

verify_bundle operations are pure inspection (manifest, exact file set,
sizes, SHA-256, identity, schema/bundle type, path safety, metadata) -
they NEVER run ELCC/EFC/reliability computation and NEVER call the
formal compute runner. rerun_bundle delegates ONLY to the existing
formal demo workflow (capacity_credit.demo.business_demo.run_demo) and
writes to a NEW isolated output directory, and only when the CURRENT
execution commit equals the bundle's source_software_commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from importlib import metadata as _metadata
from pathlib import Path
from typing import Any

from capacity_credit.contracts.artifacts import ArtifactEntry, ArtifactManifest
from capacity_credit.contracts.enums import ArtifactType, ConfidentialityLevel, RedactionStatus
from capacity_credit.reporting.report_model import (
    SUPPORTED_REPORT_MAPPING_VERSIONS,
)

BUNDLE_DESCRIPTOR_SCHEMA = "bundle-descriptor/1.0"
BUNDLE_DESCRIPTOR_NAME = "bundle.json"
PACKAGE_MANIFEST_NAME = "artifact-manifest.json"
SOURCE_MANIFEST_NAME = "source-artifact-manifest.json"
ALLOWED_BUNDLE_TYPES = frozenset({"verify_bundle", "rerun_bundle"})
_BUNDLE_TYPE_TO_ARTIFACT_TYPE = {
    "verify_bundle": ArtifactType.VERIFY_BUNDLE,
    "rerun_bundle": ArtifactType.RERUN_BUNDLE,
}
_BUNDLE_TOP_LEVEL = frozenset(
    {
        "machine-results",
        "report.xlsx",
        "report.html",
        "report_export_manifest.json",
        "input",
        BUNDLE_DESCRIPTOR_NAME,
        PACKAGE_MANIFEST_NAME,
        SOURCE_MANIFEST_NAME,
        "dependencies",
    }
)
_BUNDLE_MACHINE_FILES = frozenset(
    {
        "run_summary.json",
        "capacity_credit_results.csv",
        "adequacy_metrics.csv",
        "risk_events.parquet",
        "search_trace.csv",
        "warnings.json",
        "reproducibility_manifest.json",
    }
)
_BUNDLE_REPORT_FILES = frozenset({"report.xlsx", "report.html", "report_export_manifest.json"})
# Dependency fact source: DERIVED from the project's formal metadata
# (pyproject.toml project.dependencies + the report extra) - there is
# NO hand-maintained package list in this module (round-3), so the lock
# validation set can never drift from the declared project dependencies.
_PYPROJECT_PATH = Path(__file__).resolve().parents[3] / "pyproject.toml"
_VERIFY_NOT_RERUNNABLE_REASON = (
    "verify_bundle 只携带输入哈希、配置与复现元数据，不携带 input/data "
    "模型就绪数据本体，不能独立完整重跑（rerun_bundle 才具备完整复现条件）"
)
# THE formal rerun entry shape - arbitrary non-empty strings are rejected.
# The recorded command MUST bind the config to the PACKAGE's frozen
# input/demo-config.yaml (the very file rerun_bundle executes) - a
# formally-shaped command pointing outside the bundle is rejected.
_RERUN_COMMAND_PATTERN = re.compile(
    r'^elcc-tool demo run --config (?P<config>"[^"]+"|[^\s"]+) --output-dir (?:"[^"]+"|[^\s"]+)$'
)
_RERUN_PACKAGE_CONFIG = "input/demo-config.yaml"
# Dependency locks: the ONLY supported format is requirements-lock.txt
# with package==exact_version lines covering every required dependency.
_DEPENDENCY_LOCK_NAME = "requirements-lock.txt"
_LOCK_LINE_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.+!]*)$"
)
# Sensitive names: hidden entries, credential material, forbidden
# working directories, dangerous suffixes/archive payloads and marker
# substrings are checked on EVERY path segment (never only the final
# file name) - case-insensitively so Windows case cannot bypass.
_SENSITIVE_SEGMENT_NAMES = frozenset(
    {
        "secret",
        "secrets",
        "credentials",
        "credential",
        "keys",
        "tokens",
        "cache",
        "tmp",
        "temp",
        "logs",
    }
)
_SENSITIVE_EXACT_NAMES = frozenset(
    {".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "htpasswd"}
)
_SENSITIVE_SUFFIXES = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".pem",
    ".crt",
    ".log",
    ".tmp",
    ".cache",
    ".pyc",
    ".swp",
    ".bak",
    ".zip",
    ".gz",
    ".tgz",
    ".7z",
    ".rar",
    ".tar",
)
_SENSITIVE_MARKERS = ("secret", "password", "credential", "apikey", "api_key", "token")
_MEDIA_TYPES = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".parquet": "application/vnd.apache.parquet",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


class BundleError(ValueError):
    """Stable bundle error with a machine code (repository error-code
    style: BUNDLE_<REASON>)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    return bool(hasattr(os.path, "isjunction") and os.path.isjunction(path))


def _require_structure(value: Any, label: str, kind: type) -> None:
    if not isinstance(value, kind):
        expected = "an object" if kind is dict else "a list"
        raise BundleError("BUNDLE_MANIFEST_INVALID", f"{label} must be {expected}")


def _safe_relative_path(raw: str) -> str:
    """Normalize and validate ONE bundle-relative path: POSIX form, no
    absolute paths, no ".." traversal, no backslashes, no drive
    letters, no empty segments."""
    if not isinstance(raw, str) or not raw:
        raise BundleError("BUNDLE_UNSAFE_PATH", f"empty or non-string path: {raw!r}")
    if raw.startswith("/") or "\\" in raw:
        raise BundleError("BUNDLE_UNSAFE_PATH", f"unsafe path form: {raw!r}")
    normalized = raw.replace("\\", "/")
    if ":" in normalized.split("/")[0]:
        raise BundleError("BUNDLE_UNSAFE_PATH", f"drive letter in path: {raw!r}")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BundleError("BUNDLE_UNSAFE_PATH", f"unsafe path segments: {raw!r}")
    return normalized


def _is_sensitive_rel(rel: str) -> bool:
    """THE shared sensitive-path gate (source input allowlist, dependency
    lock paths and bundle verification all use this one function):
    sensitive checks run on EVERY "/" segment of the relative path -
    hidden entries, credential/secret/keys/tokens names, forbidden
    working directories (cache/tmp/temp/logs), dangerous suffixes and
    marker substrings. Lower-cased so Windows case cannot bypass."""
    for segment in rel.split("/"):
        seg = segment.lower()
        if seg.startswith("."):
            return True
        if seg in _SENSITIVE_SEGMENT_NAMES or seg in _SENSITIVE_EXACT_NAMES:
            return True
        if any(seg.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
            return True
        if any(marker in seg for marker in _SENSITIVE_MARKERS):
            return True
    return False


def _git_head() -> str:
    """Packaging-time HEAD for verify_bundle packaging ONLY (a pure
    rev-parse - the clean-surface gate lives in the SHARED
    business_demo.source_identity and is enforced for rerun_bundle
    creation)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            head = result.stdout.strip()
            if len(head) == 40 and all(char in "0123456789abcdef" for char in head):
                return head
    except Exception:  # noqa: BLE001, S110 - best-effort identity lookup
        pass
    return ""


def _execution_identity() -> dict[str, str]:
    """SHARED rerun-tool software identity (business_demo.source_identity
    - the SAME function a fresh demo run uses; no second git path list
    exists in this module): resolvable HEAD + clean controlled execution
    surface (src, schemas, benchmarks/business-demo). A dirty surface is
    a stable BUNDLE_SOFTWARE_IDENTITY_DIRTY - the executing code would
    not be the committed rerun entry the bundle freezes."""
    from capacity_credit.demo.business_demo import DemoError, source_identity

    try:
        return source_identity()
    except DemoError as exc:
        raise BundleError(
            "BUNDLE_SOFTWARE_IDENTITY_DIRTY",
            f"rerun tool execution surface is not a clean committed identity: {exc.message}",
        ) from exc


def _required_dependency_names() -> tuple[str, ...]:
    """THE single dependency fact source: pyproject.toml
    ``project.dependencies`` plus ``project.optional-dependencies.report``
    (the report reproduction toolchain), PEP 503 normalized, order
    preserved, deduplicated."""
    import tomllib

    try:
        data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_DEPENDENCY_LOCK_INVALID", f"project metadata unreadable: {exc}"
        ) from exc
    project = data.get("project", {})
    requirements = [str(item) for item in project.get("dependencies") or []]
    requirements += [
        str(item) for item in (project.get("optional-dependencies") or {}).get("report") or []
    ]
    names: list[str] = []
    for requirement in requirements:
        name = re.split(r"[\s<>=!~;\[]", requirement.strip(), maxsplit=1)[0]
        if not name:
            raise BundleError(
                "BUNDLE_DEPENDENCY_LOCK_INVALID",
                f"unparsable project dependency requirement: {requirement!r}",
            )
        normalized = _normalize_dist_name(name)
        if normalized not in names:
            names.append(normalized)
    if not names:
        raise BundleError(
            "BUNDLE_DEPENDENCY_LOCK_INVALID", "project metadata declares no dependencies"
        )
    return tuple(names)


def _dependency_versions() -> dict[str, str]:
    """Installed version of EVERY dependency derived from the project
    metadata (never a hand list); a missing package is "unknown" and
    blocks rerun_bundle creation."""
    versions: dict[str, str] = {}
    for name in _required_dependency_names():
        try:
            versions[name] = str(_metadata.version(name))
        except _metadata.PackageNotFoundError:
            versions[name] = "unknown"
    return versions


def _read_identity(source: Path) -> dict[str, Any]:
    """run/result identity from the T23-A run_summary.json (the same
    source the report layer validates)."""
    summary_path = source / "machine-results" / "run_summary.json"
    if not summary_path.is_file():
        raise BundleError("BUNDLE_SOURCE_INCOMPLETE", "machine-results/run_summary.json missing")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_SOURCE_INCOMPLETE", f"run_summary.json unreadable: {exc}"
        ) from exc
    return {
        "run_id": str(summary.get("run_id", "")),
        "result_id": str(summary.get("result_id", "")),
        "method": str(summary.get("method", "")),
        "report_mapping_version": str(summary.get("report_mapping_version", "")),
    }


def _valid_commit(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def _read_source_artifact_manifest(source: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The source run's demo artifact-manifest.json: the authority for
    the declared input allowlist and the SOURCE software identity."""
    path = source / PACKAGE_MANIFEST_NAME
    if not path.is_file():
        raise BundleError(
            "BUNDLE_SOURCE_INCOMPLETE",
            "source artifact-manifest.json missing (a bundle source must be a "
            "complete validated result root)",
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_SOURCE_INVALID", f"source artifact-manifest.json unreadable: {exc}"
        ) from exc
    _require_structure(manifest, "source artifact manifest", dict)
    _require_structure(manifest.get("run_identity"), "run_identity", dict)
    _require_structure(manifest.get("input_identity"), "input_identity", dict)
    _require_structure(
        manifest["input_identity"].get("input_files"), "input_identity.input_files", dict
    )
    run_identity = manifest["run_identity"]
    if not _valid_commit(run_identity.get("software_commit")):
        raise BundleError(
            "BUNDLE_IDENTITY_MISMATCH",
            "source run_identity.software_commit missing or not a 40-hex commit",
        )
    return manifest, run_identity


def _validate_source_root(source: Path) -> None:
    """SHARED source result-root validation (the same business_demo
    gate used for controlled replay) - create_bundle never packages an
    unvalidated root."""
    from capacity_credit.demo.business_demo import DemoError, validate_result_root

    try:
        validate_result_root(source)
    except DemoError as exc:
        raise BundleError(
            "BUNDLE_SOURCE_INVALID", f"source result root invalid: {exc.message}"
        ) from exc


def _check_inner_report_manifest(result_root: Path) -> None:
    """report_export_manifest.json vs the ACTUAL files: every recorded
    output hash (report.xlsx / report.html) and every recorded machine
    input artifact hash must match - a stale inner report manifest can
    never be packaged or verified."""
    manifest_path = result_root / "report_export_manifest.json"
    if not manifest_path.is_file():
        raise BundleError("BUNDLE_SOURCE_INCOMPLETE", "report_export_manifest.json missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_INNER_MANIFEST_MISMATCH", f"report_export_manifest.json unreadable: {exc}"
        ) from exc
    _require_structure(manifest, "report export manifest", dict)
    _require_structure(manifest.get("outputs"), "report export manifest outputs", list)
    _require_structure(
        manifest.get("input_artifacts"), "report export manifest input_artifacts", list
    )
    for item in manifest["outputs"]:
        name = str(item.get("name", ""))
        path = result_root / name
        if not path.is_file():
            raise BundleError("BUNDLE_SOURCE_INCOMPLETE", f"declared report output missing: {name}")
        data = path.read_bytes()
        if item.get("sha256") != _sha256_bytes(data) or item.get("size") != len(data):
            raise BundleError(
                "BUNDLE_INNER_MANIFEST_MISMATCH",
                f"report_export_manifest output hash/size stale: {name}",
            )
    for item in manifest["input_artifacts"]:
        name = str(item.get("name", ""))
        path = result_root / "machine-results" / name
        if not path.is_file():
            raise BundleError(
                "BUNDLE_SOURCE_INCOMPLETE", f"declared machine artifact missing: {name}"
            )
        if item.get("sha256") != _sha256_file(path) or item.get("size") != path.stat().st_size:
            raise BundleError(
                "BUNDLE_INNER_MANIFEST_MISMATCH",
                f"report_export_manifest input hash/size stale: {name}",
            )


def _collect_input_entries(
    source: Path,
    declared_input: dict[str, Any],
    *,
    include_data: bool,
) -> list[tuple[Path, str]]:
    """DECLARED ALLOWLIST collection from the validated source
    artifact-manifest input_identity - never a recursive copy. Every
    candidate is checked for sensitive names, link/reparse points and
    path escape BEFORE any byte is copied."""
    entries: list[tuple[Path, str]] = []
    source_resolved = source.resolve()
    for key in sorted(declared_input):
        if not isinstance(key, str) or not key.startswith("input/"):
            raise BundleError("BUNDLE_SOURCE_INVALID", f"invalid declared input key: {key!r}")
        if not isinstance(declared_input[key], str) or len(declared_input[key]) != 64:
            raise BundleError("BUNDLE_SOURCE_INVALID", f"invalid declared input hash: {key}")
        rel = _safe_relative_path(key)
        if _is_sensitive_rel(rel):
            raise BundleError(
                "BUNDLE_SENSITIVE_INPUT_REJECTED",
                f"declared input is a sensitive/unacceptable file: {rel}",
            )
        src_path = source / rel
        if not src_path.is_file():
            raise BundleError("BUNDLE_SOURCE_INCOMPLETE", f"declared input file missing: {rel}")
        if _is_link(src_path):
            raise BundleError("BUNDLE_UNSAFE_PATH", f"source input is a symlink/junction: {rel}")
        if not src_path.resolve().is_relative_to(source_resolved):
            raise BundleError("BUNDLE_UNSAFE_PATH", f"source input escapes the source root: {rel}")
        if not include_data and rel.startswith("input/data/"):
            # verify_bundle: model-ready data body stays OUT; only
            # hashes (carried in the descriptor input_files) remain
            continue
        entries.append((src_path, rel))
    return entries


def _derive_redaction_status(source: Path) -> str:
    """Redaction status from EVIDENCE: an explicit
    redaction_status in the reproducibility manifest, or the teaching
    synthetic data record. Never hardcoded for arbitrary sources."""
    repro_path = source / "machine-results" / "reproducibility_manifest.json"
    try:
        repro = json.loads(repro_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_SOURCE_INCOMPLETE", f"reproducibility manifest unreadable: {exc}"
        ) from exc
    declared = repro.get("redaction_status")
    if declared is not None:
        allowed = {status.value for status in RedactionStatus}
        if declared not in allowed:
            raise BundleError(
                "BUNDLE_CONFIDENTIALITY_BLOCKED",
                f"reproducibility manifest declares invalid redaction_status: {declared!r}",
            )
        return str(declared)
    records = repro.get("data_source_records") or []
    if records and all(str(record.get("kind")) == "teaching" for record in records):
        return RedactionStatus.NOT_REQUIRED.value
    raise BundleError(
        "BUNDLE_CONFIDENTIALITY_BLOCKED",
        "redaction status cannot be derived from source evidence; an explicit "
        "redaction_status is required for non-teaching data",
    )


def _rerun_command_valid(command: Any) -> bool:
    """Formal shape AND package-config binding: the --config argument
    must be the package's frozen input/demo-config.yaml (the exact file
    rerun_bundle executes), never a path outside the bundle."""
    if not isinstance(command, str):
        return False
    match = _RERUN_COMMAND_PATTERN.match(command.strip())
    if match is None:
        return False
    return match.group("config").strip('"') == _RERUN_PACKAGE_CONFIG


def _normalize_dist_name(name: str) -> str:
    """Python distribution-name normalization (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_dependency_lock(text: str) -> dict[str, str]:
    """Parse requirements-lock.txt content: every non-empty, non-comment
    line must be ``package==exact_version`` (no >=, ~=, ranges,
    wildcards or empty versions; no duplicate normalized names)."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LOCK_LINE_PATTERN.match(stripped)
        if match is None:
            raise BundleError(
                "BUNDLE_DEPENDENCY_LOCK_INVALID",
                f"lock line must be package==exact_version: {stripped!r}",
            )
        name = _normalize_dist_name(match.group("name"))
        if name in parsed:
            raise BundleError(
                "BUNDLE_DEPENDENCY_LOCK_INVALID",
                f"lock declares a duplicate package: {name}",
            )
        parsed[name] = match.group("version")
    if not parsed:
        raise BundleError("BUNDLE_DEPENDENCY_LOCK_INVALID", "dependency lock is empty")
    return parsed


def _validate_lock_against(lock_path: Path, expected: dict[str, str]) -> None:
    """Content gate (the SHA check NEVER replaces it): the lock must pin
    every required dependency to EXACTLY the expected version - a
    missing or differing required entry is a stable INVALID/MISMATCH.
    ``expected`` keys come from the project-metadata-derived dependency
    set (already PEP 503 normalized)."""
    parsed = _parse_dependency_lock(lock_path.read_text(encoding="utf-8"))
    for key, want in expected.items():
        if want == "unknown":
            raise BundleError(
                "BUNDLE_DEPENDENCY_LOCK_INVALID",
                f"no installed version identity available for required dependency: {key}",
            )
        if key not in parsed:
            raise BundleError(
                "BUNDLE_DEPENDENCY_LOCK_INVALID",
                f"dependency lock is missing required package: {key}",
            )
        if parsed[key] != want:
            raise BundleError(
                "BUNDLE_DEPENDENCY_LOCK_MISMATCH",
                f"dependency lock version differs for {key}: lock={parsed[key]} expected={want}",
            )


def _core_rerun_conditions(
    payload_root: Path,
    *,
    rerun_command: Any,
    confirmed: Any,
    confirmed_by: Any,
    confidentiality: Any,
    source_commit: Any,
) -> list[str]:
    """PURE IN-PACKAGE rerun conditions from the REAL files under
    payload_root (a source root at create time, the packaged bundle at
    verify time): config/seed, model-ready data, command shape, commit
    identity, confidentiality, confirmation. NO environment probing
    happens here - the verifying machine's installed versions are
    irrelevant to verify_bundle (round-4): create freezes the CURRENT
    environment via the lock content gate, and rerun_bundle compares
    the CURRENT environment against the frozen versions explicitly.
    The dependency-lock condition itself is checked by the caller."""
    missing: list[str] = []
    input_dir = payload_root / "input"
    config_path = input_dir / "demo-config.yaml"
    if not config_path.is_file():
        missing.append("scenario/seed reconstruction (input/demo-config.yaml)")
    else:
        try:
            import yaml

            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - unreadable config is a missing condition
            payload = {}
        if not (payload.get("root_seed") is not None and payload.get("samples") is not None):
            missing.append("scenario/seed reconstruction (root_seed+samples in demo-config.yaml)")
    data_dir = input_dir / "data"
    if not data_dir.is_dir() or not any(p.is_file() for p in data_dir.iterdir()):
        missing.append("model-ready input snapshot (input/data/)")
    if not _rerun_command_valid(rerun_command):
        missing.append(
            "formal rerun command binding the PACKAGE config "
            "(elcc-tool demo run --config input/demo-config.yaml --output-dir ...)"
        )
    if not _valid_commit(source_commit):
        missing.append("source software commit identity")
    if confidentiality not in {level.value for level in ConfidentialityLevel}:
        missing.append("allowed confidentiality level")
    if confirmed is not True or not (isinstance(confirmed_by, str) and confirmed_by.strip()):
        missing.append("explicit confirmation (confirmed=true with a confirmed_by identity)")
    return missing


def _check_dependency_lock(
    payload_root: Path, lock_entry: Any, frozen_dependencies: Any
) -> str | None:
    """None when the verified lock condition holds; a missing-condition
    text otherwise. lock_entry = {"path": "dependencies/requirements-lock.txt",
    "sha256": ...}. The SHA check is retained but NEVER replaces the
    content gate: the packaged lock is re-parsed and every required
    dependency must match the descriptor-FROZEN dependency versions
    (the verifying machine's installed versions are irrelevant)."""
    if not isinstance(lock_entry, dict):
        return f"verified dependency lock file (dependencies/{_DEPENDENCY_LOCK_NAME} + SHA-256)"
    raw = lock_entry.get("path")
    try:
        rel = _safe_relative_path(str(raw))
    except BundleError:
        return "verified dependency lock file (unsafe lock path)"
    if rel != f"dependencies/{_DEPENDENCY_LOCK_NAME}" or _is_sensitive_rel(rel):
        return (
            "verified dependency lock file (lock must be dependencies/"
            + _DEPENDENCY_LOCK_NAME
            + ")"
        )
    path = payload_root / rel
    if not path.is_file():
        return f"verified dependency lock file missing in package: {rel}"
    if _sha256_file(path) != lock_entry.get("sha256"):
        return f"dependency lock file hash differs: {rel}"
    if not isinstance(frozen_dependencies, dict) or not frozen_dependencies:
        return "verified dependency lock file (descriptor dependency versions missing)"
    _validate_lock_against(path, dict(frozen_dependencies))
    return None


def _media_type(rel: str) -> str:
    return _MEDIA_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def _publish_bytes_atomic(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _load_descriptor(bundle: Path) -> dict[str, Any]:
    path = bundle / BUNDLE_DESCRIPTOR_NAME
    if not path.is_file():
        raise BundleError("BUNDLE_SOURCE_INCOMPLETE", f"{BUNDLE_DESCRIPTOR_NAME} missing")
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_MANIFEST_INVALID", f"{BUNDLE_DESCRIPTOR_NAME} unreadable: {exc}"
        ) from exc
    _require_structure(descriptor, "bundle descriptor", dict)
    _require_structure(descriptor.get("files"), "bundle descriptor files", list)
    for index, entry in enumerate(descriptor["files"]):
        if not isinstance(entry, dict):
            raise BundleError("BUNDLE_MANIFEST_INVALID", f"files[{index}] must be an object")
    _require_structure(descriptor.get("rerun"), "bundle descriptor rerun", dict)
    return dict(descriptor)


def create_bundle(
    input_dir: Path | str,
    output_dir: Path | str,
    bundle_type: str,
    *,
    confirm: bool = False,
    confirm_by: str = "",
    rerun_command: str | None = None,
    confidentiality: str = "internal",
    dependency_lock: Path | str | None = None,
) -> dict[str, Any]:
    """Create a reproduction bundle from a COMPLETE validated result
    root. The source is validated through the SHARED demo result-root
    gate BEFORE any copy; the package ArtifactManifest (artifact-manifest
    1.0) is published LAST with an atomic write and any failure removes
    the partial output (never a complete-looking package)."""
    source = Path(input_dir).resolve()
    out = Path(output_dir).resolve()
    if bundle_type not in ALLOWED_BUNDLE_TYPES:
        raise BundleError(
            "BUNDLE_TYPE_INVALID", f"bundle_type must be one of {sorted(ALLOWED_BUNDLE_TYPES)}"
        )
    if out.exists() and any(out.iterdir()):
        raise BundleError(
            "BUNDLE_OUTPUT_CONFLICT", f"output directory must be empty or absent: {out}"
        )
    if confidentiality not in {level.value for level in ConfidentialityLevel}:
        raise BundleError(
            "BUNDLE_CONFIDENTIALITY_BLOCKED",
            f"confidentiality must be one of {sorted(level.value for level in ConfidentialityLevel)}",
        )
    if confidentiality == ConfidentialityLevel.RESTRICTED.value and not confirm:
        raise BundleError(
            "BUNDLE_CONFIDENTIALITY_BLOCKED",
            "restricted data requires explicit confirmation (--confirm with --confirm-by)",
        )
    out.mkdir(parents=True, exist_ok=True)
    try:
        # 1) SHARED source validation (exact set, hashes, identity,
        #    input closed loop) - before ANY byte is copied
        _validate_source_root(source)
        source_manifest, run_identity = _read_source_artifact_manifest(source)
        source_commit = str(run_identity.get("software_commit"))
        identity = _read_identity(source)
        if identity["report_mapping_version"] not in SUPPORTED_REPORT_MAPPING_VERSIONS:
            raise BundleError(
                "BUNDLE_IDENTITY_MISMATCH",
                f"unsupported report_mapping_version in source: {identity['report_mapping_version']}",
            )
        if (
            str(run_identity.get("run_id", "")) != identity["run_id"]
            or str(run_identity.get("result_id", "")) != identity["result_id"]
            or str(run_identity.get("method", "")) != identity["method"]
        ):
            raise BundleError(
                "BUNDLE_IDENTITY_MISMATCH",
                "source artifact-manifest run_identity disagrees with run_summary.json",
            )
        # 2) inner report manifest must be FRESH against actual files
        _check_inner_report_manifest(source)
        # 3) confidentiality / redaction evidence
        redaction_status = _derive_redaction_status(source)
        if (
            confidentiality == ConfidentialityLevel.PUBLIC_SANITIZED.value
            and redaction_status != RedactionStatus.COMPLETED.value
        ):
            raise BundleError(
                "BUNDLE_CONFIDENTIALITY_BLOCKED",
                "public_sanitized requires completed redaction evidence in the source",
            )
        # 4) declared allowlist collection
        declared_input = source_manifest["input_identity"]["input_files"]
        include_data = bundle_type == "rerun_bundle"
        payload: list[tuple[Path, str]] = []
        for name in sorted(_BUNDLE_MACHINE_FILES):
            path = source / "machine-results" / name
            if not path.is_file():
                raise BundleError("BUNDLE_SOURCE_INCOMPLETE", f"machine artifact missing: {name}")
            payload.append((path, f"machine-results/{name}"))
        for name in sorted(_BUNDLE_REPORT_FILES):
            path = source / name
            if not path.is_file():
                raise BundleError("BUNDLE_SOURCE_INCOMPLETE", f"report artifact missing: {name}")
            payload.append((path, name))
        payload.extend(_collect_input_entries(source, declared_input, include_data=include_data))
        # 5) rerun conditions re-derived from the SOURCE files
        lock_rel: str | None = None
        lock_sha: str | None = None
        lock_path: Path | None = None
        if dependency_lock is not None:
            lock_path = Path(dependency_lock).resolve()
            if not lock_path.is_file():
                raise BundleError(
                    "BUNDLE_RERUN_CONDITIONS_MISSING", f"dependency lock file missing: {lock_path}"
                )
            lock_rel = f"dependencies/{_safe_relative_path(lock_path.name)}"
            if lock_rel != f"dependencies/{_DEPENDENCY_LOCK_NAME}" or _is_sensitive_rel(lock_rel):
                raise BundleError(
                    "BUNDLE_DEPENDENCY_LOCK_INVALID",
                    "the only supported dependency lock is dependencies/"
                    f"{_DEPENDENCY_LOCK_NAME}; got: {lock_path.name}",
                )
            lock_sha = _sha256_file(lock_path)
        # verify_bundle packaging keeps the plain rev-parse packager
        # identity (historical verification is NOT gated on a clean
        # surface); rerun_bundle overrides it with the shared
        # clean-surface identity head below
        packager_commit: str | None = _git_head() or None
        # ONE environment snapshot per entry (round-4): create reads the
        # installed versions exactly once and reuses the same dict for
        # the lock content gate AND the descriptor freeze
        frozen_versions = _dependency_versions()
        missing = _core_rerun_conditions(
            source,
            rerun_command=rerun_command,
            confirmed=confirm,
            confirmed_by=confirm_by,
            confidentiality=confidentiality,
            source_commit=source_commit,
        )
        if bundle_type == "rerun_bundle":
            # REACHABILITY + CLEAN-SURFACE PROOF: the rerun entry
            # (bundles.py + the report bundle rerun command) must exist
            # in the very commit recorded as source_software_commit, and
            # the executing code must BE that commit. The SHARED
            # business_demo.source_identity gate (resolvable HEAD + no
            # tracked/untracked changes on the controlled execution
            # surface) runs FIRST - before any package byte is written.
            current_identity = _execution_identity()
            current_head = current_identity["head"]
            if current_head != source_commit:
                raise BundleError(
                    "BUNDLE_SOFTWARE_COMMIT_MISMATCH",
                    "source_software_commit "
                    f"({source_commit}) is not the current committed HEAD "
                    f"({current_head}); the rerun entry is not proven reachable "
                    "from the source commit - package this result as a "
                    "verify_bundle or rerun from a result generated at this commit",
                )
            packager_commit = current_head
            if not confirm:
                raise BundleError(
                    "BUNDLE_CONFIRMATION_REQUIRED",
                    "creating a rerun_bundle requires explicit confirmation (--confirm)",
                )
            if lock_rel is None or lock_sha is None:
                missing.append(
                    f"verified dependency lock file (dependencies/{_DEPENDENCY_LOCK_NAME} + SHA-256)"
                )
            if missing:
                raise BundleError(
                    "BUNDLE_RERUN_CONDITIONS_MISSING",
                    f"rerun_bundle conditions missing: {', '.join(missing)}",
                )
            # SHA is kept, but the lock CONTENT must pin every required
            # dependency to the currently installed exact version
            source_lock = lock_path
            if source_lock is None:
                raise BundleError(
                    "BUNDLE_RERUN_CONDITIONS_MISSING",
                    f"verified dependency lock file (dependencies/{_DEPENDENCY_LOCK_NAME} + SHA-256)",
                )
            _validate_lock_against(source_lock, frozen_versions)
        # 6) copy payload (source artifact manifest verbatim + lock)
        copied: list[dict[str, Any]] = []
        for src_path, rel in payload:
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = src_path.read_bytes()
            dest.write_bytes(data)
            copied.append({"path": rel, "size_bytes": len(data), "sha256": _sha256_bytes(data)})
        source_manifest_data = (source / PACKAGE_MANIFEST_NAME).read_bytes()
        (out / SOURCE_MANIFEST_NAME).write_bytes(source_manifest_data)
        copied.append(
            {
                "path": SOURCE_MANIFEST_NAME,
                "size_bytes": len(source_manifest_data),
                "sha256": _sha256_bytes(source_manifest_data),
            }
        )
        if bundle_type == "rerun_bundle" and lock_rel and lock_sha is not None:
            lock_data = Path(dependency_lock).read_bytes()  # type: ignore[arg-type]
            (out / lock_rel).parent.mkdir(parents=True, exist_ok=True)
            (out / lock_rel).write_bytes(lock_data)
            copied.append({"path": lock_rel, "size_bytes": len(lock_data), "sha256": lock_sha})
        # 7) child descriptor (bound by the package manifest hash)
        created_at = datetime.now(UTC).isoformat()
        manifest_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"reproduction-bundle:{identity['run_id']}:{identity['result_id']}:{bundle_type}",
        )
        verify_command = f"elcc-tool report bundle verify --bundle-dir {out}"
        if bundle_type == "verify_bundle":
            rerun_block: dict[str, Any] = {
                "rerun_eligible": False,
                "can_independently_rerun": False,
                "not_eligible_reasons": [_VERIFY_NOT_RERUNNABLE_REASON],
                "rerun_command": None,
                "explicit_confirmation": {
                    "confirmed": False,
                    "confirmed_by": "",
                    "confirmed_at": None,
                },
                "dependency_lock": None,
                "dependencies": dict(frozen_versions),
            }
        else:
            rerun_block = {
                "rerun_eligible": True,
                "can_independently_rerun": True,
                "not_eligible_reasons": [],
                "rerun_command": rerun_command,
                "explicit_confirmation": {
                    "confirmed": bool(confirm),
                    "confirmed_by": confirm_by,
                    "confirmed_at": created_at,
                },
                "dependency_lock": {"path": lock_rel, "sha256": lock_sha},
                "dependencies": dict(frozen_versions),
            }
        descriptor: dict[str, Any] = {
            "bundle_descriptor_schema": BUNDLE_DESCRIPTOR_SCHEMA,
            "bundle_type": bundle_type,
            "created_at": created_at,
            "created_by": confirm_by or "unknown",
            "package_artifact_manifest_id": str(manifest_id),
            "run_id": identity["run_id"],
            "result_id": identity["result_id"],
            "method": identity["method"],
            "report_mapping_version": identity["report_mapping_version"],
            "source_software_commit": source_commit,
            "packager_software_commit": packager_commit,
            "software_version": _software_version_of(source),
            "confidentiality_level": confidentiality,
            "redaction_status": redaction_status,
            "verify_command": verify_command,
            "source_artifact_manifest": {
                "path": SOURCE_MANIFEST_NAME,
                "sha256": _sha256_bytes(source_manifest_data),
            },
            "input_files": dict(declared_input),
            "files": copied,
            "rerun": rerun_block,
        }
        descriptor_bytes = (
            json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _publish_bytes_atomic(out / BUNDLE_DESCRIPTOR_NAME, descriptor_bytes)
        # 8) package ArtifactManifest (formal 1.0 contract) LAST - one
        #    ArtifactEntry binds bundle.json by path + SHA-256
        package_manifest = ArtifactManifest(
            artifact_schema_version="1.0",
            artifact_manifest_id=manifest_id,
            run_id=uuid.UUID(identity["run_id"]),
            result_id=uuid.UUID(identity["result_id"]),
            report_mapping_version=str(identity["report_mapping_version"]),
            confidentiality_level=ConfidentialityLevel(confidentiality),
            redaction_status=RedactionStatus(redaction_status),
            generated_by=confirm_by or "capacity_credit.reporting.bundles",
            generated_at=datetime.now(UTC),
            artifacts=[
                ArtifactEntry(
                    artifact_type=_BUNDLE_TYPE_TO_ARTIFACT_TYPE[bundle_type],
                    relative_path=BUNDLE_DESCRIPTOR_NAME,
                    sha256=_sha256_bytes(descriptor_bytes),
                    media_type="application/json",
                    producer_service="capacity_credit.reporting.bundles",
                    schema_or_template_version=BUNDLE_DESCRIPTOR_SCHEMA,
                )
            ],
        )
        if identity["report_mapping_version"] not in SUPPORTED_REPORT_MAPPING_VERSIONS:
            raise BundleError(
                "BUNDLE_IDENTITY_MISMATCH",
                f"unsupported report_mapping_version in source: {identity['report_mapping_version']}",
            )
        _publish_bytes_atomic(
            out / PACKAGE_MANIFEST_NAME,
            (
                json.dumps(package_manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
                + "\n"
            ).encode("utf-8"),
        )
    except Exception:
        # never leave a complete-looking package behind on failure
        shutil.rmtree(out, ignore_errors=True)
        raise
    return {
        "bundle_type": bundle_type,
        "output_dir": str(out),
        "run_id": identity["run_id"],
        "result_id": identity["result_id"],
        "rerun_eligible": bundle_type == "rerun_bundle",
        "source_software_commit": source_commit,
        "packager_software_commit": descriptor["packager_software_commit"],
        "file_count": len(copied) + 2,
    }


def _software_version_of(source: Path) -> str:
    try:
        manifest_path = source / "machine-results" / "reproducibility_manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = payload.get("software_version")
        if version:
            return str(version)
    except (OSError, ValueError):
        pass
    return "unknown"


def verify_bundle(bundle_dir: Path | str) -> dict[str, Any]:
    """Pure-inspection verification (NEVER runs computation): package
    ArtifactManifest (pydantic 1.0) -> descriptor binding -> path safety
    -> exact file set -> sizes + SHA-256 -> shared machine-result
    validation -> inner report manifest -> identity chain (run_summary /
    reproducibility / source artifact manifest / commits) -> bundle type
    & scope -> re-derived rerun conditions -> required metadata."""
    bundle = Path(bundle_dir).resolve()
    if not bundle.is_dir():
        raise BundleError("BUNDLE_SOURCE_INCOMPLETE", f"bundle directory missing: {bundle}")
    manifest_path = bundle / PACKAGE_MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError("BUNDLE_SOURCE_INCOMPLETE", f"{PACKAGE_MANIFEST_NAME} missing")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_MANIFEST_INVALID", f"{PACKAGE_MANIFEST_NAME} unreadable: {exc}"
        ) from exc
    try:
        manifest = ArtifactManifest.model_validate(payload)
    except Exception as exc:  # any contract violation maps to a stable code
        raise BundleError(
            "BUNDLE_MANIFEST_INVALID", f"package ArtifactManifest invalid: {exc}"
        ) from exc
    if len(manifest.artifacts) != 1:
        raise BundleError(
            "BUNDLE_MANIFEST_INVALID",
            "a reproduction package declares exactly one bundle artifact entry",
        )
    entry = manifest.artifacts[0]
    if entry.artifact_type not in (
        ArtifactType.VERIFY_BUNDLE,
        ArtifactType.RERUN_BUNDLE,
    ):
        raise BundleError(
            "BUNDLE_TYPE_INVALID", f"bundle artifact_type invalid: {entry.artifact_type}"
        )
    if entry.relative_path != BUNDLE_DESCRIPTOR_NAME:
        raise BundleError(
            "BUNDLE_MANIFEST_INVALID",
            f"the bundle artifact entry must bind {BUNDLE_DESCRIPTOR_NAME}",
        )
    bundle_type = entry.artifact_type.value

    descriptor = _load_descriptor(bundle)
    if descriptor.get("bundle_descriptor_schema") != BUNDLE_DESCRIPTOR_SCHEMA:
        raise BundleError("BUNDLE_MANIFEST_INVALID", "bundle descriptor schema differs")
    if descriptor.get("bundle_type") != bundle_type:
        raise BundleError(
            "BUNDLE_TYPE_INVALID",
            "descriptor bundle_type disagrees with the package manifest entry - "
            "editing JSON can never change the bundle type",
        )
    if descriptor.get("package_artifact_manifest_id") != str(manifest.artifact_manifest_id):
        raise BundleError(
            "BUNDLE_MANIFEST_INVALID",
            "descriptor package_artifact_manifest_id does not bind this package manifest",
        )

    # path safety FIRST: unsafe/undeclared shapes are rejected before
    # any hash comparison or filesystem access
    declared_paths: set[str] = set()
    for file_entry in descriptor["files"]:
        raw = str(file_entry.get("path", ""))
        rel = _safe_relative_path(raw)
        if _is_sensitive_rel(rel):
            raise BundleError("BUNDLE_SENSITIVE_INPUT_REJECTED", f"sensitive file in bundle: {rel}")
        if not (bundle / rel).resolve().is_relative_to(bundle.resolve()):
            raise BundleError("BUNDLE_UNSAFE_PATH", f"path escapes the bundle: {raw}")
        declared_paths.add(rel)
    expected = declared_paths | {BUNDLE_DESCRIPTOR_NAME, PACKAGE_MANIFEST_NAME}
    actual = {
        str(item.relative_to(bundle)).replace("\\", "/")
        for item in bundle.rglob("*")
        if item.is_file()
    }
    if actual != expected:
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise BundleError(
            "BUNDLE_FILE_SET_MISMATCH",
            f"bundle file set differs (extra={extra} missing={missing})",
        )

    # size + SHA-256 over the WHOLE package: payload via the descriptor,
    # the descriptor itself via the package manifest entry hash
    for file_entry in descriptor["files"]:
        raw = str(file_entry.get("path", ""))
        rel = _safe_relative_path(raw)
        path = bundle / rel
        if not path.is_file():
            raise BundleError("BUNDLE_FILE_SET_MISMATCH", f"declared file missing: {rel}")
        if _is_link(path):
            raise BundleError("BUNDLE_UNSAFE_PATH", f"link/reparse point in bundle: {rel}")
        if path.stat().st_size != file_entry.get("size_bytes"):
            raise BundleError("BUNDLE_HASH_MISMATCH", f"size differs for {rel}")
        if _sha256_file(path) != file_entry.get("sha256"):
            raise BundleError("BUNDLE_HASH_MISMATCH", f"hash differs for {rel}")
    descriptor_path = bundle / BUNDLE_DESCRIPTOR_NAME
    if _sha256_file(descriptor_path) != entry.sha256:
        raise BundleError(
            "BUNDLE_HASH_MISMATCH",
            f"{BUNDLE_DESCRIPTOR_NAME} was modified after packaging "
            "(any descriptor edit breaks its package-manifest binding)",
        )
    source_manifest_entry = next(
        (item for item in descriptor["files"] if str(item.get("path", "")) == SOURCE_MANIFEST_NAME),
        None,
    )
    if source_manifest_entry is None:
        raise BundleError(
            "BUNDLE_FILE_SET_MISMATCH", f"{SOURCE_MANIFEST_NAME} missing from the descriptor"
        )

    # identity chain: run_summary vs package manifest vs source
    # artifact manifest vs recorded commits
    identity = _read_identity(bundle)
    if (
        str(manifest.run_id) != identity["run_id"]
        or str(manifest.result_id) != identity["result_id"]
    ):
        raise BundleError(
            "BUNDLE_IDENTITY_MISMATCH",
            "package manifest run/result_id disagrees with run_summary.json",
        )
    try:
        source_manifest = json.loads((bundle / SOURCE_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            "BUNDLE_MANIFEST_INVALID", f"{SOURCE_MANIFEST_NAME} unreadable: {exc}"
        ) from exc
    _require_structure(source_manifest, "source artifact manifest", dict)
    _require_structure(source_manifest.get("run_identity"), "run_identity", dict)
    _require_structure(
        source_manifest["input_identity"].get("input_files")
        if isinstance(source_manifest.get("input_identity"), dict)
        else None,
        "input_identity.input_files",
        dict,
    )
    run_identity = source_manifest["run_identity"]
    if (
        str(run_identity.get("run_id", "")) != identity["run_id"]
        or str(run_identity.get("result_id", "")) != identity["result_id"]
        or str(run_identity.get("method", "")) != identity["method"]
        or identity["method"] != str(descriptor.get("method", ""))
        or identity["report_mapping_version"] != str(descriptor.get("report_mapping_version", ""))
    ):
        raise BundleError("BUNDLE_IDENTITY_MISMATCH", "identity chain disagrees across the package")
    source_commit = str(descriptor.get("source_software_commit", ""))
    if not _valid_commit(source_commit):
        raise BundleError("BUNDLE_IDENTITY_MISMATCH", "source_software_commit missing or invalid")
    if source_commit != str(run_identity.get("software_commit", "")):
        raise BundleError(
            "BUNDLE_IDENTITY_MISMATCH",
            "source_software_commit disagrees with the carried source artifact manifest - "
            "the recorded run identity cannot be edited",
        )
    declared_input = source_manifest["input_identity"]["input_files"]
    if descriptor.get("input_files") != declared_input:
        raise BundleError(
            "BUNDLE_IDENTITY_MISMATCH",
            "descriptor input_files disagree with the source artifact manifest input identity",
        )

    # SHARED machine-result validation inside the bundle (hashes +
    # cross-file identity + semantics - catches a stale
    # reproducibility manifest) and the inner report manifest
    from capacity_credit.reporting.report_model import ReportExportError, load_and_validate

    try:
        load_and_validate(bundle / "machine-results")
    except ReportExportError as exc:
        code = (
            "BUNDLE_SOURCE_INCOMPLETE"
            if exc.code == "REPORT_SOURCE_INCOMPLETE"
            else "BUNDLE_INNER_MANIFEST_MISMATCH"
        )
        raise BundleError(
            code, f"bundle machine-results failed shared validation: {exc.code}: {exc.message}"
        ) from exc
    _check_inner_report_manifest(bundle)

    # bundle type scope: verify_bundle NEVER carries the model-ready
    # data body and NEVER claims independent rerun capability
    rerun_block = descriptor["rerun"]
    if bundle_type == "verify_bundle":
        data_paths = [rel for rel in declared_paths if rel.startswith("input/data/")]
        if data_paths:
            raise BundleError(
                "BUNDLE_VERIFY_SCOPE_VIOLATION",
                f"verify_bundle must not carry the model-ready input body: {sorted(data_paths)}",
            )
        if (
            rerun_block.get("rerun_eligible") is not False
            or rerun_block.get("can_independently_rerun") is not False
            or not rerun_block.get("not_eligible_reasons")
        ):
            raise BundleError(
                "BUNDLE_TYPE_INVALID",
                "verify_bundle must state rerun_eligible=false with the "
                "cannot-independently-rerun reason",
            )
        rerun_eligible = False
        not_rerunable_reason = "; ".join(str(r) for r in rerun_block["not_eligible_reasons"])
    else:
        # rerun eligibility RE-DERIVED from the packaged files - the
        # recorded boolean is never trusted
        missing_conditions = _core_rerun_conditions(
            bundle,
            rerun_command=rerun_block.get("rerun_command"),
            confirmed=rerun_block.get("explicit_confirmation", {}).get("confirmed"),
            confirmed_by=rerun_block.get("explicit_confirmation", {}).get("confirmed_by"),
            confidentiality=descriptor.get("confidentiality_level"),
            source_commit=source_commit,
        )
        # a rerun_bundle was only ever creatable when the packaging HEAD
        # equaled the source commit - keep that relationship frozen and
        # re-checked (packager identity must be a valid commit)
        packager_commit = descriptor.get("packager_software_commit")
        if not _valid_commit(packager_commit) or packager_commit != source_commit:
            raise BundleError(
                "BUNDLE_SOFTWARE_COMMIT_MISMATCH",
                "rerun_bundle identity relationship broken: "
                f"source_software_commit={source_commit} "
                f"packager_software_commit={packager_commit}",
            )
        lock_condition = _check_dependency_lock(
            bundle, rerun_block.get("dependency_lock"), rerun_block.get("dependencies")
        )
        if lock_condition:
            missing_conditions.append(lock_condition)
        if missing_conditions:
            raise BundleError(
                "BUNDLE_RERUN_CONDITIONS_MISSING",
                f"rerun conditions missing: {', '.join(missing_conditions)}",
            )
        rerun_eligible = True
        not_rerunable_reason = ""

    # required metadata
    for field in ("created_at", "verify_command", "confidentiality_level", "redaction_status"):
        if not descriptor.get(field):
            raise BundleError(
                "BUNDLE_MANIFEST_INVALID", f"required descriptor metadata missing: {field}"
            )
    if descriptor.get("confidentiality_level") not in {
        level.value for level in ConfidentialityLevel
    }:
        raise BundleError("BUNDLE_MANIFEST_INVALID", "invalid confidentiality_level")
    if descriptor.get("redaction_status") not in {status.value for status in RedactionStatus}:
        raise BundleError("BUNDLE_MANIFEST_INVALID", "invalid redaction_status")
    if descriptor.get("source_artifact_manifest", {}).get("sha256") != source_manifest_entry.get(
        "sha256"
    ):
        raise BundleError("BUNDLE_HASH_MISMATCH", "source artifact manifest binding differs")

    return {
        "ok": True,
        "bundle_type": bundle_type,
        "run_id": identity["run_id"],
        "result_id": identity["result_id"],
        "source_software_commit": source_commit,
        "packager_software_commit": descriptor.get("packager_software_commit"),
        "rerun_eligible": rerun_eligible,
        "can_independently_rerun": rerun_eligible,
        "not_rerunable_reason": not_rerunable_reason,
        "confidentiality_level": descriptor.get("confidentiality_level"),
        "file_count": len(declared_paths) + 2,
        "bundle_dir": str(bundle),
    }


def rerun_bundle(bundle_dir: Path | str, output_dir: Path | str) -> dict[str, Any]:
    """Rerun a rerun_bundle through the EXISTING formal demo workflow
    (never a second calculation path). Gates, all BEFORE any output
    directory is created, any runner call or any computation:
    in-package verify -> shared clean software-identity gate
    (business_demo.source_identity) -> HEAD == source_software_commit
    -> CURRENT environment dependency versions == descriptor-frozen
    versions (BUNDLE_DEPENDENCY_ENVIRONMENT_INCOMPLETE/_MISMATCH).
    No automatic install/upgrade, no checkout/reset; output goes to a
    NEW isolated directory and existing different results are never
    overwritten."""
    bundle = Path(bundle_dir).resolve()
    out = Path(output_dir).resolve()
    verified = verify_bundle(bundle)
    if verified["bundle_type"] != "rerun_bundle":
        raise BundleError(
            "BUNDLE_RERUN_CONDITIONS_MISSING",
            "this bundle is a verify_bundle; rerun requires a rerun_bundle with full conditions",
        )
    if out.exists() and any(out.iterdir()):
        raise BundleError("BUNDLE_OUTPUT_CONFLICT", f"rerun output must be empty or absent: {out}")
    descriptor = _load_descriptor(bundle)
    # shared clean identity gate + commit relationship (one snapshot)
    current_identity = _execution_identity()
    current_commit = current_identity["head"]
    if current_commit != str(descriptor.get("source_software_commit", "")):
        raise BundleError(
            "BUNDLE_SOFTWARE_COMMIT_MISMATCH",
            "current execution commit differs from the bundle source_software_commit "
            f"(current={current_commit} "
            f"source={descriptor.get('source_software_commit')}); recover the source "
            "commit in a controlled environment before rerunning",
        )
    # CURRENT environment gate (one version snapshot): every frozen
    # dependency must be installed at exactly the frozen version
    frozen_dependencies = descriptor["rerun"].get("dependencies")
    if not isinstance(frozen_dependencies, dict) or not frozen_dependencies:
        raise BundleError(
            "BUNDLE_DEPENDENCY_ENVIRONMENT_INCOMPLETE",
            "the bundle descriptor freezes no dependency versions",
        )
    current_versions = _dependency_versions()
    for name, frozen in frozen_dependencies.items():
        current = current_versions.get(name)
        if current is None or current == "unknown":
            raise BundleError(
                "BUNDLE_DEPENDENCY_ENVIRONMENT_INCOMPLETE",
                f"required dependency not installed in the rerun environment: {name} "
                f"(frozen=={frozen})",
            )
        if current != frozen:
            raise BundleError(
                "BUNDLE_DEPENDENCY_ENVIRONMENT_MISMATCH",
                f"rerun environment version differs for {name}: "
                f"environment=={current} frozen=={frozen}",
            )
    out.mkdir(parents=True, exist_ok=True)

    # THE only sanctioned rerun entry: the existing formal demo workflow
    from capacity_credit.demo.business_demo import run_demo

    summary = run_demo(bundle / "input" / "demo-config.yaml", out)
    return {
        "run_id": summary["run_id"],
        "result_id": summary["result_id"],
        "output_dir": str(out),
        "source_bundle": str(bundle),
        "source_software_commit": descriptor.get("source_software_commit"),
    }


__all__ = [
    "ALLOWED_BUNDLE_TYPES",
    "BUNDLE_DESCRIPTOR_NAME",
    "BUNDLE_DESCRIPTOR_SCHEMA",
    "PACKAGE_MANIFEST_NAME",
    "BundleError",
    "create_bundle",
    "rerun_bundle",
    "verify_bundle",
]
