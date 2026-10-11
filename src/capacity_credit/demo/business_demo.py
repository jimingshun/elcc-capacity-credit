"""T24-B business demonstration orchestration (thin; first remediation).

Single entry: run_demo(config_path, output_dir, samples=None).

Pipeline (all formal production calls, no second implementations):
  synthetic teaching input builder (committed, deterministic formulas)
  -> import_project -> approve_and_freeze -> scenario_build
  -> run_elcc_pipeline (base + candidate + search in ONE formal run)
  -> write_machine_outputs (T23-A seven artifacts; search trace ALWAYS
     published - the formal trace/result statuses now agree)
  -> export_report (T24-A Excel/HTML/manifest)
  -> business-analysis.md rendered from the SHARED ReportView
  -> artifact-manifest.json (demo-level inventory with full identity)
  -> run-instructions README.

Teaching scenario (non-degenerate, auditable formulas - documented in
benchmarks/business-demo/t24b-wind-elcc-teaching/README.md):
  L(h)  = 90 + 28*cos(2*pi*((h mod 24)-18)/24) + 8*cos(2*pi*(h-4380)/8760)
  cf(h) = max(0, 0.40 + 0.25*cos(2*pi*((h mod 24)-18)/24)
                   - 0.15*cos(2*pi*(h-4380)/8760))
  units = 3 x 60 MW CCGT, FOR=0.05; wind installed 40 MW.
  capacity_factor == available_generation_mw / installed_capacity_mw
  per hour (the formal import validates this exactly).

Artifact directory contract (user-facing, final): input/,
machine-results/, report.xlsx, report.html, report_export_manifest.json,
business-analysis.md, artifact-manifest.json, README.md. The internal
formal project workspace lives under tmp/business-demo-work/ and is
NEVER mixed into the artifact directory. Existing output behaviour:
empty -> fresh run; complete+identity-consistent -> controlled replay
without recomputation; partial or identity-different -> stable
DEMO_OUTPUT_EXISTS_DIFFERENT; nothing is ever auto-deleted.

Numbers inside business-analysis.md come ONLY from the validated
machine results / shared ReportView; presentation formatting (thousand
separators, percent) never touches raw values.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from jinja2 import Environment, FileSystemLoader

from capacity_credit.application.p1_workflow import (
    approve_and_freeze,
    import_project,
    scenario_build,
)
from capacity_credit.contracts.elcc_config import ElccApplicationConfig
from capacity_credit.reporting.export_service import export_report
from capacity_credit.reporting.machine import write_machine_outputs
from capacity_credit.reporting.report_model import ReportView, load_and_validate

DEMO_SCHEMA_VERSION = "2.0"

_TOP_LEVEL_FILES = (
    "report.xlsx",
    "report.html",
    "report_export_manifest.json",
    "business-analysis.md",
    "README.md",
)
_TOP_LEVEL_CONTRACT = frozenset(
    {
        "input",
        "machine-results",
        "report.xlsx",
        "report.html",
        "report_export_manifest.json",
        "business-analysis.md",
        "artifact-manifest.json",
        "README.md",
    }
)
_MACHINE_OUTPUT_FILES = (
    "run_summary.json",
    "capacity_credit_results.csv",
    "adequacy_metrics.csv",
    "risk_events.parquet",
    "search_trace.csv",
    "warnings.json",
    "reproducibility_manifest.json",
)
_REPO_ROOT = Path(__file__).resolve().parents[3]


class DemoError(ValueError):
    """Stable demo orchestration error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DemoStopped(Exception):
    """Raised at a SAFE STAGE BOUNDARY when a control request asked for
    a stop BEFORE the non-interruptible ELCC compute stage begins. The
    partial output directory is preserved untouched (never deleted,
    never overwritten); no business analysis or artifact manifest is
    published for a stopped demo."""

    code = "DEMO_STOPPED_AT_STAGE_BOUNDARY"

    def __init__(self, *, stage: str) -> None:
        super().__init__(f"demo stopped at safe stage boundary: {stage}")
        self.stage = stage


# T25 stage table: stages are announced to the progress projection; a
# stop request is honoured ONLY at boundaries before the ELCC compute
# stage (index 5). Once the non-checkpointed ELCC internal computation
# has started (or later), a stop request CANNOT be honoured - the
# stable diagnostic WORKER_SAFE_STOP_UNSUPPORTED_AT_CURRENT_STAGE is
# recorded and the demo runs to full completion (never force-killed).
_DEMO_STAGES = (
    "read_config",
    "build_teaching_input",
    "import_project",
    "approve_and_freeze",
    "scenario_build",
    "elcc_pipeline",
    "machine_outputs",
    "report_export",
    "publish_manifest",
)
_DEMO_ELCC_STAGE_INDEX = _DEMO_STAGES.index("elcc_pipeline")
_WORKER_SAFE_STOP_UNSUPPORTED = "WORKER_SAFE_STOP_UNSUPPORTED_AT_CURRENT_STAGE"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git_rev_parse_head() -> str:
    """Unambiguous repository HEAD (empty string when unavailable)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=_REPO_ROOT,
            check=False,
        )
        if result.returncode == 0:
            head = result.stdout.strip()
            if len(head) == 40 and all(char in "0123456789abcdef" for char in head):
                return head
    except Exception:  # noqa: BLE001, S110 - best-effort lookup
        pass
    return ""


def _git_execution_surface_dirty() -> bool:
    """True when the execution surface (production code, demo config,
    schemas, AND pyproject.toml - the single dependency fact source the
    bundle locks are derived from) has uncommitted changes: a run
    recorded against a stale HEAD would not be reproducible from that
    commit. This is THE one git path list - never copied elsewhere."""
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "--",
                "src",
                "schemas",
                "benchmarks/business-demo",
                "pyproject.toml",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=_REPO_ROOT,
            check=False,
        )
        return result.returncode != 0 or bool(result.stdout.strip())
    except Exception:  # noqa: BLE001 - best-effort check
        return True


def source_identity() -> dict[str, str]:
    """THE shared software-identity gate (T24-B demo runs AND T24-C
    rerun_bundle creation both call this one function - there is no
    second git path list anywhere): an unambiguous git HEAD is required
    AND the controlled execution surface must be CLEAN - otherwise the
    recorded software_commit would not contain the code/config/schema
    that produced the artifacts (a stale HEAD must never masquerade as
    the actual software identity). The check runs BEFORE any
    computation, user-artifact or package-file write and fails with the
    stable DEMO_SOURCE_IDENTITY_DIRTY code. Tests patch this entry
    point directly (never by dirtying the working tree)."""
    head = _git_rev_parse_head()
    if not head:
        raise DemoError(
            "DEMO_SOURCE_IDENTITY_DIRTY",
            "cannot resolve an unambiguous git HEAD in the repository",
        )
    if _git_execution_surface_dirty():
        raise DemoError(
            "DEMO_SOURCE_IDENTITY_DIRTY",
            "T24-B execution surface (src, schemas, benchmarks/business-demo) has "
            "uncommitted changes; refusing to record a stale HEAD as software identity",
        )
    return {"head": head}


# demo-run internal alias: run_demo and the demo tests use this name;
# the T24-C bundle layer calls source_identity() - one implementation.
_source_identity = source_identity


# ----------------------------------------------------------------------
# T26-A run-center launch contract: THE formal teaching-case resolution
# ----------------------------------------------------------------------


class TeachingCaseDefinition(TypedDict):
    """One FORMAL teaching case (read-only resolution result): the case
    identity, a business display name, the resolved config path, the
    config's current SHA-256 (recomputed on every call - content drift is
    never laundered) and the shared ReportView use-boundary code."""

    case_id: str
    display_name: str
    config_path: str
    config_sha256: str
    use_boundary_code: str


# THE formal teaching-case registry (case_id -> (display_name, config
# path)). The config path is anchored at the module installation location
# (_REPO_ROOT) - NEVER at the process cwd; T26-C packaging may later serve
# the same contract from bundled resources without changing the signature.
_TEACHING_CASE_REGISTRY: dict[str, tuple[str, Path]] = {
    "t24b-wind-elcc-teaching": (
        "T24-B 发电侧新增风电资源 ELCC 教学示范",
        _REPO_ROOT
        / "benchmarks"
        / "business-demo"
        / "t24b-wind-elcc-teaching"
        / "demo-config.yaml",
    ),
}

# the shared ReportView use-boundary code for teaching data (see
# reporting/report_model.py: G0_teaching -> "g0_teaching")
_TEACHING_USE_BOUNDARY_CODE = "g0_teaching"

# THE canonical T26-A teaching-task identity recipe version
_TEACHING_TASK_CONTRACT_VERSION = "t26a-teaching-demo-v1"


def compute_teaching_task_identity(
    case_id: str, config_sha256: str, source_software_commit: str
) -> str:
    """THE single canonical T26-A teaching-task identity recipe (used by
    BOTH demo_service at launch-planning time and the worker execution
    gate): the full SHA-256 of the deterministic JSON payload (UTF-8,
    sort_keys; NO timestamps, requested_by, pid, session, project or
    random values)."""
    payload = {
        "contract_version": _TEACHING_TASK_CONTRACT_VERSION,
        "case_id": case_id,
        "config_sha256": config_sha256,
        "source_software_commit": source_software_commit,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_output_path_safe(out: Path) -> None:
    """Fail-closed output path safety at the ACTUAL write boundary: the
    output directory and every EXISTING ancestor component must be REAL
    directories (a junction, symlink, other reparse point or a
    file-instead-of-directory is DEMO_OUTPUT_PATH_UNSAFE). Uses THE
    shared worker path check (no divergent path rules); never
    resolve-follows, deletes or rebuilds a suspicious path."""
    from capacity_credit.application.worker import unsafe_path_component

    for part in (out, *out.parents):
        reason = unsafe_path_component(part, directory=True)
        if reason is not None:
            raise DemoError(
                "DEMO_OUTPUT_PATH_UNSAFE",
                f"unsafe output path component ({reason}): {part}",
            )


def resolve_teaching_case(case_id: str) -> TeachingCaseDefinition:
    """Resolve ONE formal teaching case (READ-ONLY: never creates, modifies
    or deletes any file). An unknown case is stably refused
    (DEMO_CASE_INVALID); the config is parsed through the EXISTING
    _read_config (DEMO_CONFIG_MISSING / DEMO_CONFIG_INVALID - no second
    config parser) and must bind to the case identity
    (scenario_name == case_id); the SHA-256 is recomputed on every call."""
    entry = _TEACHING_CASE_REGISTRY.get(case_id)
    if entry is None:
        raise DemoError("DEMO_CASE_INVALID", f"unknown teaching case: {case_id!r}")
    display_name, config_path = entry
    # single-read: the SAME bytes feed the parse and the reported hash
    config, config_bytes = _read_config_bytes(config_path)
    if config.get("scenario_name") != case_id:
        raise DemoError(
            "DEMO_CONFIG_INVALID",
            f"demo config scenario_name does not match teaching case {case_id!r}",
        )
    return TeachingCaseDefinition(
        case_id=case_id,
        display_name=display_name,
        config_path=str(config_path),
        config_sha256=_sha256_bytes(config_bytes),
        use_boundary_code=_TEACHING_USE_BOUNDARY_CODE,
    )


def _read_config_bytes(config_path: Path) -> tuple[dict[str, Any], bytes]:
    """Read the demo config ONCE: the SAME bytes feed the YAML parse and
    the SHA-256 (no two-read window). Missing -> DEMO_CONFIG_MISSING;
    unreadable (OSError), invalid UTF-8, invalid YAML or a non-mapping
    payload -> DEMO_CONFIG_INVALID (never an uncontrolled exception)."""
    import yaml

    if not config_path.is_file():
        raise DemoError("DEMO_CONFIG_MISSING", f"demo config not found: {config_path}")
    try:
        data = config_path.read_bytes()
    except OSError as exc:
        raise DemoError("DEMO_CONFIG_INVALID", f"demo config unreadable: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DemoError(
            "DEMO_CONFIG_INVALID", f"demo config is not valid UTF-8: {exc}"
        ) from exc
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DemoError("DEMO_CONFIG_INVALID", f"demo config YAML invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise DemoError("DEMO_CONFIG_INVALID", "demo config must be a mapping")
    return payload, data


def _read_config(config_path: Path) -> dict[str, Any]:
    """Parse the demo config (delegates to the single-read entry)."""
    config, _data = _read_config_bytes(config_path)
    return config


def _build_teaching_input(data_dir: Path, config: dict[str, Any]) -> dict[str, str]:
    """Deterministic synthetic teaching input with NON-CONSTANT,
    auditable load and wind time series (formulas above; parameters from
    the committed demo config; all content explicitly teaching).
    Column layouts follow the record-based teaching format consumed by
    the formal import chain; the formal renewable validation enforces
    capacity_factor == available_generation_mw / installed per hour."""
    load_mean = float(config.get("load_mean_mw", 90.0))
    load_daily_amp = float(config.get("load_daily_amp_mw", 28.0))
    load_seasonal_amp = float(config.get("load_seasonal_amp_mw", 8.0))
    wind_installed = float(config.get("wind_installed_capacity_mw", 40.0))
    wind_cf_mean = float(config.get("wind_cf_mean", 0.40))
    wind_cf_daily_amp = float(config.get("wind_cf_daily_amp", 0.25))
    wind_cf_seasonal_amp = float(config.get("wind_cf_seasonal_amp", 0.15))
    unit_count = int(config.get("thermal_unit_count", 3))
    unit_capacity = float(config.get("thermal_unit_capacity_mw", 60.0))
    for_rate = float(config.get("thermal_for_rate", 0.05))
    year = int(config.get("weather_year", 2025))
    hours = 8760
    import pandas as pd

    start = pd.Timestamp(year, 1, 1, tz="Asia/Shanghai")
    timestamps = [str(start + pd.Timedelta(hours=h)) for h in range(hours)]
    loads: list[float] = []
    cfs: list[float] = []
    for h in range(hours):
        day_phase = 2.0 * math.pi * ((h % 24) - 18) / 24.0
        season_phase = 2.0 * math.pi * (h - 4380) / 8760.0
        load = (
            load_mean
            + load_daily_amp * math.cos(day_phase)
            + load_seasonal_amp * math.cos(season_phase)
        )
        cf = max(
            0.0,
            wind_cf_mean
            + wind_cf_daily_amp * math.cos(day_phase)
            - wind_cf_seasonal_amp * math.cos(season_phase),
        )
        loads.append(load)
        cfs.append(cf)
    # design sanity: low-risk band exists (hours below one unit) and no
    # certain-deficit hour (max load below the fleet capacity) - the
    # formal engine then yields a NON-degenerate base system
    if not (min(loads) < unit_capacity and max(loads) < unit_count * unit_capacity):
        raise DemoError(
            "DEMO_SCENARIO_DEGENERATE",
            f"teaching load design out of bounds: min={min(loads):.1f} "
            f"max={max(loads):.1f} vs {unit_count}x{unit_capacity:g} MW fleet",
        )
    if max(cfs) > 1.0:
        raise DemoError("DEMO_SCENARIO_DEGENERATE", "wind capacity factor exceeds 1")

    rows: dict[str, list[list[Any]]] = {
        "load_hourly.csv": [
            [
                "record_id",
                "region_id",
                "scenario_id",
                "weather_year",
                "timestamp",
                "load_mw",
                "load_basis",
                "is_forecast",
                "source_id",
                "data_status",
                "quality_flag",
            ]
        ],
        "renewable_hourly.csv": [
            [
                "record_id",
                "resource_group_id",
                "scenario_id",
                "weather_year",
                "timestamp",
                "generation_basis",
                "available_generation_mw",
                "actual_generation_mw",
                "capacity_factor",
                "installed_capacity_mw",
                "source_id",
            ]
        ],
        "intertie_hourly.csv": [
            [
                "record_id",
                "intertie_id",
                "scenario_id",
                "timestamp",
                "net_import_mw",
                "scheduled_import_mw",
                "scheduled_export_mw",
                "source_id",
            ]
        ],
    }
    for h in range(hours):
        rows["load_hourly.csv"].append(
            [
                f"L{year}_{h}",
                "REGION_A",
                "BASE_2025",
                str(year),
                timestamps[h],
                f"{loads[h]:.1f}",
                "measured",
                "false",
                "teaching_synthetic_t24b",
                "complete",
                "good",
            ]
        )
        available = cfs[h] * wind_installed
        rows["renewable_hourly.csv"].append(
            [
                f"R{year}_WIND_PROVINCE_{h}",
                "WIND_PROVINCE",
                "BASE_2025",
                str(year),
                timestamps[h],
                "available",
                f"{available:.6f}",
                f"{0.8 * available:.6f}",
                f"{cfs[h]:.8f}",
                f"{wind_installed:.1f}",
                "teaching_synthetic_t24b",
            ]
        )
        rows["intertie_hourly.csv"].append(
            [
                f"I{year}_{h}",
                "TIE_01",
                "BASE_2025",
                timestamps[h],
                "0.0",
                "0.0",
                "0.0",
                "teaching_synthetic_t24b",
            ]
        )
    thermal_rows: list[list[Any]] = [
        [
            "record_id",
            "unit_id",
            "plant_id",
            "plant_name",
            "unit_name",
            "technology_code",
            "technology_label_raw",
            "capacity_mw",
            "capacity_basis",
            "region_id",
            "source_id",
        ],
    ]
    reliability_map: list[list[Any]] = [
        [
            "record_id",
            "unit_id",
            "reliability_class_id",
            "mapping_method",
            "confidence",
            "source_id",
        ],
    ]
    for index in range(unit_count):
        unit_id = f"UNIT_{chr(65 + index)}"
        thermal_rows.append(
            [
                f"TU{index + 1}",
                unit_id,
                "PLANT_1",
                "teaching plant",
                f"unit {chr(97 + index)}",
                "CCGT",
                "CCGT",
                f"{unit_capacity:.1f}",
                "nameplate",
                "REGION_A",
                "teaching_synthetic_t24b",
            ]
        )
        reliability_map.append(
            [
                f"TUM{index + 1}",
                unit_id,
                "THERMAL_G1",
                "explicit",
                "high",
                "teaching_synthetic_t24b",
            ]
        )
    rows["thermal_units.csv"] = thermal_rows
    rows["thermal_reliability_classes.csv"] = [
        [
            "record_id",
            "reliability_class_id",
            "class_name",
            "model_type",
            "authoritative_parameter_set",
            "for_rate",
            "source_id",
        ],
        [
            "TRC1",
            "THERMAL_G1",
            "teaching_ccgt",
            "sequential_two_state",
            "for_only",
            f"{for_rate:g}",
            "teaching_synthetic_t24b",
        ],
    ]
    rows["thermal_unit_reliability_map.csv"] = reliability_map
    rows["renewable_capacities.csv"] = [
        [
            "record_id",
            "resource_group_id",
            "technology",
            "region_id",
            "year",
            "installed_capacity_mw",
            "capacity_basis",
            "source_id",
        ],
        [
            f"RC{year}_WIND_PROVINCE",
            "WIND_PROVINCE",
            "wind_onshore",
            "REGION_A",
            str(year),
            f"{wind_installed:.1f}",
            "nameplate",
            "teaching_synthetic_t24b",
        ],
    ]
    rows["interties.csv"] = [
        [
            "record_id",
            "intertie_id",
            "intertie_name",
            "intertie_type",
            "rated_capacity_mw",
            "normal_import_limit_mw",
            "normal_export_limit_mw",
            "availability_model",
            "source_id",
        ],
        [
            "IT1",
            "TIE_01",
            "teaching tie",
            "ac",
            "500.0",
            "500.0",
            "500.0",
            "fixed_available",
            "teaching_synthetic_t24b",
        ],
    ]
    rows["weather_year_weight_sets.csv"] = [
        [
            "record_id",
            "weight_set_id",
            "weather_year",
            "weight",
            "source_id",
            "data_status",
            "note",
        ],
        [
            f"WY{year}",
            "w1",
            str(year),
            "1.0",
            "teaching_synthetic_t24b",
            "assumed",
            "teaching equal weight",
        ],
    ]
    data_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for name, table in rows.items():
        text = "".join(",".join(str(v) for v in row) + "\n" for row in table)
        data = text.encode("utf-8")
        (data_dir / name).write_bytes(data)
        hashes[name] = _sha256_bytes(data)
    return hashes


def _load_elcc_config(
    config: dict[str, Any], scenario_bank_id: str, seed: int
) -> ElccApplicationConfig:
    payload = config.get("elcc_config")
    if not isinstance(payload, dict):
        raise DemoError("DEMO_CONFIG_INVALID", "demo config missing elcc_config mapping")
    payload = json.loads(json.dumps(payload))  # deep copy
    payload.setdefault("run_config", {})["random_seed"] = seed
    payload["run_config"]["scenario_bank_id"] = scenario_bank_id
    return ElccApplicationConfig.model_validate(payload)


def _write_business_analysis(
    view: ReportView,
    output_dir: Path,
    meta: dict[str, Any],
) -> Path:
    """Render business-analysis.md from the SHARED ReportView (numbers
    come exclusively from the validated machine results; only
    presentation formatting happens in the template)."""
    template_dir = Path(__file__).resolve().parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=False)

    def _thousands(value: Any) -> str:
        if value is None:
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if abs(number - round(number)) < 1e-9:
            return f"{number:,.0f}"
        return f"{number:,.2f}".rstrip("0").rstrip(".")

    def _num_text(value: Any) -> str:
        if value is None:
            return ""
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            return str(value)

    def _percent(value: Any) -> str:
        if value is None:
            return ""
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(value)

    env.filters["thousands"] = _thousands
    env.filters["num"] = _num_text
    env.filters["pct"] = _percent
    template = env.get_template("business-analysis.md.j2")
    path = output_dir / "business-analysis.md"
    path.write_text(template.render(view=view, meta=meta), encoding="utf-8")
    return path


def _collect_artifacts(output_dir: Path) -> list[dict[str, Any]]:
    """Inventory the FINAL user-facing artifacts: top-level files plus
    input/ and machine-results/ subtrees (the internal project work area
    never lives in the output directory)."""
    entries: list[dict[str, Any]] = []
    for name in _TOP_LEVEL_FILES:
        path = output_dir / name
        if path.is_file():
            entries.append(
                {
                    "path": name,
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    for sub in ("input", "machine-results"):
        base = output_dir / sub
        if base.is_dir():
            for child in sorted(base.rglob("*")):
                if child.is_file():
                    rel = str(child.relative_to(output_dir)).replace("\\", "/")
                    entries.append(
                        {
                            "path": rel,
                            "size_bytes": child.stat().st_size,
                            "sha256": _sha256_file(child),
                        }
                    )
    return entries


def _display_path(path: Path) -> str:
    """Repository-relative POSIX path when possible (PowerShell-friendly
    for the reproduction command); absolute otherwise. Quoted with double
    quotes when the path contains spaces."""
    try:
        relative = path.resolve().relative_to(_REPO_ROOT.resolve())
        text = relative.as_posix()
    except ValueError:
        text = str(path.resolve())
    return f'"{text}"' if " " in text else text


def validate_result_root(
    out: Path,
    *,
    config_sha256: str | None = None,
    expected_software_commit: str | None = None,
) -> dict[str, Any]:
    """SHARED validation of a COMPLETE, identity-consistent demo result
    root (no computation, no writes, nothing deleted) - THE single
    source-result gate, reused by the T24-C bundle layer via the same
    function (never a second approximate validator).

    The ACTUAL directory set must equal the controlled manifest set
    EXACTLY: extra files/directories, duplicate / absolute / ".." paths
    and link/reparse points are stable DEMO_OUTPUT_EXISTS_DIFFERENT.
    The T23-A machine results are re-validated through the SHARED
    load_and_validate gate, and the manifest identity / capacity values
    are cross-checked against the shared ReportView (never trusted on
    their own). When ``config_sha256`` is given (demo replay) the frozen
    demo-config must also match the request; otherwise it is validated
    against the manifest itself. When ``expected_software_commit`` is
    given (a T26-A bound replay) the manifest run_identity.software_commit
    must also equal the bound commit - old artifacts are never trusted
    over the binding."""
    from capacity_credit.application.worker import unsafe_path_component
    from capacity_credit.reporting.report_model import (
        ReportExportError,
        load_and_validate,
    )

    for _part in (out, *out.parents):
        _reason = unsafe_path_component(_part, directory=True)
        if _reason is not None:
            raise DemoError(
                "DEMO_OUTPUT_PATH_UNSAFE",
                f"unsafe output root component ({_reason}): {_part}",
            )
    _stack = [out]
    while _stack:
        _current = _stack.pop()
        for _entry in sorted(_current.iterdir()):
            # THE shared check covers EVERY reparse type (symlink,
            # junction, any FILE_ATTRIBUTE_REPARSE_POINT) - never just
            # is_symlink()/is_junction()
            _leaf_reason = unsafe_path_component(_entry, directory=None)
            if _leaf_reason is not None:
                raise DemoError(
                    "DEMO_OUTPUT_PATH_UNSAFE",
                    f"unsafe output component ({_leaf_reason}): {_entry}",
                )
            if _entry.is_dir():
                _stack.append(_entry)

    def _reject(reason: str) -> None:
        raise DemoError(
            "DEMO_OUTPUT_EXISTS_DIFFERENT",
            f"existing output directory cannot be replayed ({reason}): {out}",
        )

    # 1) top-level exact set
    top = {entry.name for entry in out.iterdir()}
    if top != set(_TOP_LEVEL_CONTRACT):
        _reject(f"top-level set differs: {sorted(top ^ set(_TOP_LEVEL_CONTRACT))}")
    for name in _MACHINE_OUTPUT_FILES:
        if not (out / "machine-results" / name).is_file():
            _reject(f"missing machine artifact {name}")

    def _require_structure(value: Any, label: str, kind: type) -> None:
        """Minimal local structure check: every nested field is read
        ONLY after its container is validated - a malformed manifest
        structure is a stable DEMO_OUTPUT_EXISTS_DIFFERENT, never a
        leaked AttributeError/TypeError/KeyError/ValueError."""
        if not isinstance(value, kind):
            expected = "an object" if kind is dict else "a list"
            _reject(f"{label} must be {expected}")

    # the artifact-manifest.json leaf must be a REAL file BEFORE its
    # content is ever read (a link is DEMO_OUTPUT_PATH_UNSAFE, never
    # followed) - THE shared worker path check, no divergent rule
    from capacity_credit.application.worker import unsafe_path_component

    manifest_leaf_reason = unsafe_path_component(
        out / "artifact-manifest.json", directory=False
    )
    if manifest_leaf_reason is not None:
        raise DemoError(
            "DEMO_OUTPUT_PATH_UNSAFE",
            f"artifact-manifest.json is not a real file "
            f"({manifest_leaf_reason}): {out / 'artifact-manifest.json'}",
        )
    # 2) manifest STRUCTURE FIRST (root -> artifacts -> elements ->
    #    input_identity -> input_files -> run_identity -> results);
    #    only then schema version / config identity / paths / hashes /
    #    shared validation / business values
    try:
        manifest = json.loads((out / "artifact-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _reject(f"unreadable artifact-manifest.json: {exc}")
    _require_structure(manifest, "manifest", dict)
    _require_structure(manifest.get("artifacts"), "artifacts", list)
    for index, entry in enumerate(manifest["artifacts"]):
        if not isinstance(entry, dict):
            _reject(f"artifacts[{index}] must be an object")
    _require_structure(manifest.get("input_identity"), "input_identity", dict)
    _require_structure(
        manifest["input_identity"].get("input_files"), "input_identity.input_files", dict
    )
    _require_structure(manifest.get("run_identity"), "run_identity", dict)
    _require_structure(manifest.get("results"), "results", dict)
    # schema version and frozen-config identity (nested access is safe
    # now: every container is structure-validated above)
    if manifest.get("artifact_manifest_schema_version") != DEMO_SCHEMA_VERSION:
        _reject("artifact manifest schema differs")
    # the recorded config hash is read once here for BOTH the binding
    # cross-check and the later closed-loop comparison
    recorded_config_sha = manifest["input_identity"].get("demo_config_sha256")
    _verify_manifest_binding_identity(
        manifest,
        config_sha256=config_sha256,
        expected_software_commit=expected_software_commit,
    )
    # 3) manifest path safety: unique, relative, no "..", no backslashes
    artifact_paths: list[str] = []
    for entry in manifest.get("artifacts", []):
        raw = str(entry.get("path", ""))
        if raw in artifact_paths:
            _reject(f"duplicate manifest path: {raw}")
        artifact_paths.append(raw)
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or "\\" in raw:
            _reject(f"unsafe manifest path: {raw}")
    # 4) exact file/dir set: actual tree == manifest set + manifest file
    expected_files = set(artifact_paths) | {"artifact-manifest.json"}
    actual_files = {
        str(entry.relative_to(out)).replace("\\", "/")
        for entry in out.rglob("*")
        if entry.is_file()
    }
    if actual_files != expected_files:
        extra = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        _reject(f"directory set differs (extra={extra} missing={missing})")
    expected_dirs = {str(Path(name).parent).replace("\\", "/") for name in expected_files} - {"."}
    actual_dirs = {
        str(entry.relative_to(out)).replace("\\", "/") for entry in out.rglob("*") if entry.is_dir()
    }
    if actual_dirs != expected_dirs:
        _reject(f"directory set differs: {sorted(actual_dirs ^ expected_dirs)}")
    # 5) (the link/reparse sweep now runs FIRST, before any read - see
    #    the step-0 walk above; no second rglob-based sweep here)
    # 6) every manifest artifact recomputed (path/size/SHA-256)
    for entry in manifest.get("artifacts", []):
        path = out / str(entry.get("path", ""))
        if not path.is_file() or path.stat().st_size != entry.get("size_bytes"):
            _reject(f"artifact changed or missing: {entry.get('path')}")
        if _sha256_file(path) != entry.get("sha256"):
            _reject(f"artifact hash differs: {entry.get('path')}")
    # 7) input_identity CLOSED LOOP: the actual input/ file set, the
    #    input_identity.input_files key set and the artifact-manifest
    #    input/* path set must be EXACTLY equal; every declared hash must
    #    be a valid SHA-256 matching the actual file; the frozen
    #    demo-config hash must agree with the request config AND the
    #    artifact record (never relies on file existence alone)
    input_identity = manifest.get("input_identity")
    if not isinstance(input_identity, dict):
        _reject("input_identity must be an object")
    input_files = input_identity.get("input_files")
    if not isinstance(input_files, dict):
        _reject("input_files must be an object")
    actual_input = {
        "input/" + str(entry.relative_to(out / "input")).replace("\\", "/")
        for entry in (out / "input").rglob("*")
        if entry.is_file()
    }
    declared_input = set(input_files.keys())
    artifact_input = {raw for raw in artifact_paths if raw.startswith("input/")}
    if actual_input != declared_input or declared_input != artifact_input:
        _reject(
            f"input_identity sets differ "
            f"(actual={len(actual_input ^ declared_input)} "
            f"declared={len(declared_input ^ artifact_input)} "
            f"artifacts={len(actual_input ^ artifact_input)})"
        )
    for key, value in input_files.items():
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            _reject(f"invalid sha256 declared for input file: {key}")
        key_path = Path(str(key))
        if key_path.is_absolute() or ".." in key_path.parts or "\\" in str(key):
            _reject(f"unsafe input path: {key}")
        actual_path = out / "input" / key_path.relative_to(Path("input"))
        if not actual_path.is_file():
            _reject(f"declared input file missing: {key}")
        if _sha256_file(actual_path) != value:
            _reject(f"input file hash differs: {key}")
    # frozen demo-config binding: actual bytes hash == manifest
    # demo_config_sha256 == artifact record (== request config hash when
    # one is supplied by the demo replay entry)
    expected_config_sha = config_sha256 if config_sha256 is not None else recorded_config_sha
    config_artifact_hash = next(
        (
            entry.get("sha256")
            for entry in manifest.get("artifacts", [])
            if entry.get("path") == "input/demo-config.yaml"
        ),
        None,
    )
    if (
        _sha256_file(out / "input" / "demo-config.yaml") != expected_config_sha
        or recorded_config_sha != expected_config_sha
        or config_artifact_hash != expected_config_sha
    ):
        _reject("frozen demo-config identity differs")
    # 8) SHARED T23-A validation (hash + cross-file identity + semantics)
    try:
        view = load_and_validate(out / "machine-results")
    except ReportExportError as exc:
        _reject(f"shared machine-result validation failed: {exc.code}")
    # 9) manifest identity vs the shared ReportView
    run_identity = manifest.get("run_identity", {})
    if str(view.run_id) != str(run_identity.get("run_id", "")):
        _reject("manifest run_id disagrees with the shared ReportView")
    if str(view.result_id) != str(run_identity.get("result_id", "")):
        _reject("manifest result_id disagrees with the shared ReportView")
    if view.method != str(run_identity.get("method", "")):
        _reject("manifest method disagrees with the shared ReportView")
    # 10) REQUIRED result fields: results must be an object carrying BOTH
    #     finite numbers, and each MUST equal the shared ReportView value
    #     (no conditional "compare only when present" fallback)
    results = manifest.get("results")
    if not isinstance(results, dict):
        _reject("results must be an object")
    required_results = ("accredited_capacity_mw", "capacity_credit_fraction")
    for field in required_results:
        value = results.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _reject(f"results.{field} must be a finite number")
        if not math.isfinite(float(value)):
            _reject(f"results.{field} must be a finite number")
    view_accredited = view.accredited_capacity_mw
    view_fraction = view.capacity_credit_fraction
    if (
        view_accredited is None
        or abs(float(results["accredited_capacity_mw"]) - float(view_accredited)) > 1e-9
    ):
        _reject("manifest accredited_capacity_mw disagrees with the shared ReportView")
    if (
        view_fraction is None
        or abs(float(results["capacity_credit_fraction"]) - float(view_fraction)) > 1e-9
    ):
        _reject("manifest capacity_credit_fraction disagrees with the shared ReportView")
    return {
        "run_id": view.run_id,
        "result_id": view.result_id,
        "output_dir": str(out.resolve()),
        "use_boundary_code": view.use_boundary_code,
        "accredited_capacity_mw": view.accredited_capacity_mw,
        "capacity_credit_fraction": view.capacity_credit_fraction,
        "search_trace_published": True,
        "demo_replayed": True,
        "run_replayed": False,
        "report_replayed": False,
        "run_reused": True,
        "report_reused": True,
    }


def _verify_manifest_binding_identity(
    manifest: dict[str, Any],
    *,
    config_sha256: str | None,
    expected_software_commit: str | None,
) -> None:
    """THE manifest-level binding identity cross-check (called by
    validate_result_root AFTER the manifest structure is validated): the
    recorded demo-config hash must equal the request hash, and a BOUND
    replay's recorded run_identity.software_commit must equal the bound
    commit - old artifacts are never trusted over the binding. Any
    mismatch is the stable DEMO_OUTPUT_EXISTS_DIFFERENT (never recompute,
    never overwrite)."""
    if config_sha256 is not None:
        recorded_config_sha = manifest["input_identity"].get("demo_config_sha256")
        if recorded_config_sha != config_sha256:
            raise DemoError(
                "DEMO_OUTPUT_EXISTS_DIFFERENT", "demo config identity differs"
            )
    if expected_software_commit is not None:
        recorded_commit = manifest["run_identity"].get("software_commit")
        if recorded_commit != expected_software_commit:
            raise DemoError(
                "DEMO_OUTPUT_EXISTS_DIFFERENT", "software commit identity differs"
            )


def _replay_existing(
    out: Path,
    config_sha256: str,
    *,
    expected_software_commit: str | None = None,
) -> dict[str, Any]:
    """Controlled artifact-level replay entry: full shared result-root
    validation, then REUSE the existing run/report artifacts (no formal
    run or report export replay happens). A BOUND replay additionally
    cross-checks the manifest run_identity.software_commit against the
    bound commit (never trusting old artifacts over it)."""
    return validate_result_root(
        out,
        config_sha256=config_sha256,
        expected_software_commit=expected_software_commit,
    )


def _demo_stage_gate(
    stage_index: int,
    *,
    stop_check: Any,
    noted: list[str],
) -> None:
    """Consult the stop seam at a stage boundary. BEFORE the
    non-checkpointed ELCC compute stage a stop is SAFE: raise
    :class:`DemoStopped` (partial output preserved). At or after it the
    stage cannot be interrupted - record the stable diagnostic and run
    to full completion (never force-kill, never fake a stop)."""
    if stop_check is None:
        return
    request = stop_check()
    if not request:
        return
    if stage_index > _DEMO_ELCC_STAGE_INDEX:
        if _WORKER_SAFE_STOP_UNSUPPORTED not in noted:
            noted.append(_WORKER_SAFE_STOP_UNSUPPORTED)
        return
    raise DemoStopped(stage=_DEMO_STAGES[stage_index])


def run_demo(
    config_path: Path | str,
    output_dir: Path | str,
    *,
    samples: int | None = None,
    progress: Any = None,
    stop_check: Any = None,
    expected_config_sha256: str | None = None,
    expected_software_commit: str | None = None,
) -> dict[str, Any]:
    """Run (or controlled-replay) the full T24-B wind-ELCC teaching
    demonstration. Returns the demo summary dict with explicit
    demo/run/report replay flags.

    T25 hooks (optional, never required by the CLI/demo entry):
    ``progress`` receives read-only stage projections
    (``phase=demo``/``current_subtask=<stage>``/``completed_work``/
    ``total_work``); ``stop_check`` is consulted at stage boundaries
    only - safe before the ELCC compute stage, unsupported after it
    (stable diagnostic, never a forced kill).

    T26-A consumption-point binding (optional; absent = legacy T25
    behavior): the expected config hash and software commit bind the run
    to the task identity - the config is read ONCE and the SAME bytes
    feed the SHA-256 check, the decode and the parse (no two-read
    window), and the software HEAD is re-verified at the actual
    consumption point; a drift is the stable DEMO_CONFIG_CHANGED /
    DEMO_COMMIT_CHANGED BEFORE any compute or artifact write, never a
    compute on different bytes."""
    noted: list[str] = []

    def _stage(index: int) -> None:
        if progress is not None:
            progress(
                {
                    "phase": "demo",
                    "current_subtask": _DEMO_STAGES[index],
                    "completed_work": index,
                    "total_work": len(_DEMO_STAGES),
                }
            )
        _demo_stage_gate(index, stop_check=stop_check, noted=noted)

    _stage(0)
    # single-read: the SAME bytes feed the parse and the identity hash
    config, config_bytes = _read_config_bytes(Path(config_path))
    config_sha256 = _sha256_bytes(config_bytes)
    # consumption-point binding: the bound config hash is verified against
    # the SAME bytes that were parsed (never a second read)
    if expected_config_sha256 is not None and config_sha256 != expected_config_sha256:
        raise DemoError(
            "DEMO_CONFIG_CHANGED",
            "the demo config bytes differ from the bound task identity hash",
        )
    out = Path(output_dir)
    # boundary (a): output path safety BEFORE the first read-through or
    # replay of any existing artifact (a swapped junction/reparse point
    # is fail-closed)
    _assert_output_path_safe(out)
    if out.exists() and any(out.iterdir()):
        # a BOUND replay never skips the identity closure: current config
        # bytes (verified above), the current HEAD and the expected commit
        # must agree BEFORE the existing artifacts are trusted (an unbound
        # replay keeps the legacy behavior)
        if expected_software_commit is not None:
            replay_identity = _source_identity()
            if replay_identity["head"] != expected_software_commit:
                raise DemoError(
                    "DEMO_COMMIT_CHANGED",
                    "the software HEAD differs from the bound task identity "
                    "commit (replay refused before trusting old artifacts)",
                )
        return _replay_existing(
            out, config_sha256, expected_software_commit=expected_software_commit
        )
    # strict software identity BEFORE any computation or user-artifact
    # write: a stale HEAD must never be recorded as the actual identity
    _stage(1)
    identity = _source_identity()
    # consumption-point binding: the bound software commit is re-verified
    # against the formal identity gate result
    if (
        expected_software_commit is not None
        and identity["head"] != expected_software_commit
    ):
        raise DemoError(
            "DEMO_COMMIT_CHANGED",
            "the software HEAD differs from the bound task identity commit",
        )
    # boundary (b): re-check AFTER the identity gate and BEFORE mkdir - a
    # check-then-swap between boundary (a) and here is still caught
    _assert_output_path_safe(out)
    out.mkdir(parents=True, exist_ok=True)
    # boundary (c): re-check AFTER mkdir and BEFORE _build_teaching_input
    # and any first artifact write (a post-mkdir swap is still caught)
    _assert_output_path_safe(out)

    demo_samples = int(samples if samples is not None else config.get("samples", 30))
    seed = int(config.get("root_seed", 7))
    scenario_name = str(config.get("scenario_name", "t24b-wind-elcc-teaching"))
    dataset_id = str(config.get("dataset_id", "DS_T24B_WIND_ELCC"))
    data_version_id = str(config.get("data_version_id", "V_T24B_2025"))
    created_by = str(config.get("created_by", "demo_t24b"))
    request_id = str(config.get("request_id", "run-t24b"))
    software_version = str(config.get("software_version", "0.1.0"))
    software_commit = identity["head"]

    # internal formal project workspace: NEVER inside the artifact dir
    work_root = _REPO_ROOT / "tmp" / "business-demo-work" / scenario_name
    token = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    project_dir = work_root / token
    input_hashes = _build_teaching_input(project_dir / "data", config)

    # user-facing input snapshot: data + frozen config + input README
    input_snapshot = out / "input"
    for path in (project_dir / "data").glob("*.csv"):
        dest = input_snapshot / "data" / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
    (input_snapshot / "demo-config.yaml").write_bytes(config_bytes)
    (input_snapshot / "README.md").write_text(
        "\n".join(
            [
                "# 教学输入快照说明（input/）",
                "",
                "- data/：9 张记录式教学输入 CSV（source_id=teaching_synthetic_t24b）；",
                "- demo-config.yaml：本次运行的冻结配置快照（与 benchmarks/business-demo/t24b-wind-elcc-teaching/demo-config.yaml 逐字节一致，见 artifact-manifest.json input_identity.demo_config_sha256）；",
                "- 时序公式与参数：见 benchmarks/business-demo/t24b-wind-elcc-teaching/README.md；",
                f"- 固定种子 {seed}、样本 {demo_samples}、teaching 模式（G0_teaching 使用边界）。",
                "",
                "输入为确定性合成的教学数据，不代表任何真实省份；不得作为正式省级测算输入。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    input_files: dict[str, str] = {}
    for path in sorted(input_snapshot.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(input_snapshot)).replace("\\", "/")
            input_files[f"input/{rel}"] = _sha256_file(path)

    _stage(2)
    imported = import_project(
        project_dir=project_dir,
        dataset_id=dataset_id,
        data_version_id=data_version_id,
        created_by=created_by,
    )
    _stage(3)
    approve_and_freeze(
        project_dir=project_dir,
        import_batch_id=imported["import_batch_id"],
        request_id=request_id,
        reviewed_by=str(config.get("reviewed_by", "demo_t24b_reviewer")),
    )
    _stage(4)
    bank = scenario_build(
        project_dir=project_dir,
        import_batch_id=imported["import_batch_id"],
        data_version_id=imported["data_version_id"],
        root_seed=seed,
        timezone="Asia/Shanghai",
        samples=demo_samples,
        request_id="scn-t24b",
    )

    _stage(5)
    from capacity_credit.application.elcc_workflow import run_elcc_pipeline

    app_config = _load_elcc_config(config, bank["scenario_bank_id"], seed)
    projection = run_elcc_pipeline(
        project_dir=project_dir,
        import_batch_id=imported["import_batch_id"],
        data_version_id=imported["data_version_id"],
        request_id=request_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        app_config=app_config,
    )
    run_id = str(projection["run_id"])
    run_dir = project_dir / "runs" / run_id
    if not run_dir.is_dir():
        raise DemoError("DEMO_RUN_DIR_MISSING", f"formal run directory missing: {run_dir}")

    machine_out = out / "machine-results"
    run_manifest: dict[str, Any] = {}
    run_manifest_path = run_dir / "run_manifest.json"
    if run_manifest_path.is_file():
        run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    reproducibility_context = {
        "scenario_bank_id": bank["scenario_bank_id"],
        "source_run_id": run_manifest.get("source_run_id"),
        "inputs_hash": run_manifest.get("data_version_hash") or "",
        "config_hash": run_manifest.get("run_config_hash") or "",
        "software_version": software_version,
        "random_seed": seed,
        "model_ready_data": True,
        "data_source_records": [
            {
                "source_id": "teaching_synthetic_t24b",
                "kind": "teaching",
                "hash": _sha256_bytes("".join(sorted(input_hashes.values())).encode("utf-8")),
            }
        ],
        "rerun_capability": "deterministic_same_input",
        "missing_reproducibility_inputs": [],
    }
    # search trace: ALWAYS published through the formal writer - the
    # formal ELCC publish path now records the FINAL result status in
    # the trace, so the strict trace/result binding never rejects it;
    # a write failure propagates (no trace-less fallback).
    _stage(6)
    trace_path = run_dir / "search_trace.json"
    write_machine_outputs(
        result_path=run_dir / "method_result.json",
        output_dir=machine_out,
        search_trace_path=trace_path,
        reproducibility_context=reproducibility_context,
    )

    _stage(7)
    export_summary = export_report(machine_out, out)
    view = load_and_validate(machine_out)
    repro_command = (
        "elcc-tool demo run --config "
        + _display_path(Path(config_path))
        + " --output-dir "
        + _display_path(out)
    )
    meta = {
        "scenario_name": scenario_name,
        "teaching": True,
        "dataset_id": dataset_id,
        "data_version_id": data_version_id,
        "root_seed": seed,
        "samples": demo_samples,
        "load_mean_mw": float(config.get("load_mean_mw", 90.0)),
        "load_daily_amp_mw": float(config.get("load_daily_amp_mw", 28.0)),
        "load_seasonal_amp_mw": float(config.get("load_seasonal_amp_mw", 8.0)),
        "wind_installed_capacity_mw": float(config.get("wind_installed_capacity_mw", 40.0)),
        "wind_cf_mean": float(config.get("wind_cf_mean", 0.40)),
        "wind_cf_daily_amp": float(config.get("wind_cf_daily_amp", 0.25)),
        "wind_cf_seasonal_amp": float(config.get("wind_cf_seasonal_amp", 0.15)),
        "thermal_unit_count": int(config.get("thermal_unit_count", 3)),
        "thermal_unit_capacity_mw": float(config.get("thermal_unit_capacity_mw", 60.0)),
        "thermal_for_rate": float(config.get("thermal_for_rate", 0.05)),
        "search_trace_published": True,
        "software_version": software_version,
        "software_commit": software_commit,
        "repro_command": repro_command,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "demo_schema_version": DEMO_SCHEMA_VERSION,
    }
    _write_business_analysis(view, out, meta)

    instructions = [
        "# " + scenario_name + " 成果物说明（run-instructions）",
        "",
        "本目录为 T24-B 教学示范的最终成果物，由以下正式命令生成（与 business-analysis.md 一致）：",
        "",
        "powershell 命令（Windows）:",
        "    " + repro_command,
        "",
        "成果物：input/（输入快照：数据 + 冻结 demo-config.yaml + 输入说明）、machine-results/（T23-A 七项机器结果，含 search_trace.csv）、report.xlsx、report.html、report_export_manifest.json、business-analysis.md（中文业务分析）、artifact-manifest.json（清单与哈希）、本 README。",
        "",
        "目录行为：空目录 → 全新运行；完整且身份一致的既有成果 → 受控重放（不重新计算，demo_replayed=true、run_replayed=false、report_replayed=false、run_reused=true、report_reused=true）；部分或身份不一致 → 稳定报错 DEMO_OUTPUT_EXISTS_DIFFERENT；不会删除既有成果。",
        "",
        "绝对路径：" + str(out.resolve()),
        "",
        "使用边界：" + (view.use_boundary_warning or "内部研发试算"),
        "",
        "不得将本教学示范结果作为正式省级容量信用结论或发布结论。",
        "",
    ]
    (out / "README.md").write_text("\n".join(instructions), encoding="utf-8")

    _stage(8)
    entries = _collect_artifacts(out)
    report_replayed = bool(export_summary.get("replayed", False))
    artifact_manifest = {
        "artifact_manifest_schema_version": DEMO_SCHEMA_VERSION,
        "scenario_name": scenario_name,
        "teaching_demo": True,
        "input_identity": {
            "dataset_id": dataset_id,
            "data_version_id": data_version_id,
            "demo_config_sha256": config_sha256,
            "input_files": input_files,
        },
        "run_identity": {
            "run_id": run_id,
            "result_id": projection.get("result_id"),
            "method": "elcc",
            "root_seed": seed,
            "samples": demo_samples,
            "software_version": software_version,
            "software_commit": software_commit,
            "data_version_hash": run_manifest.get("data_version_hash") or "",
            "run_config_hash": run_manifest.get("run_config_hash") or "",
        },
        "replay_identity": {
            "demo_replayed": False,
            "run_replayed": False,
            "report_replayed": report_replayed,
            "run_reused": False,
            "report_reused": False,
        },
        "results": {
            "accredited_capacity_mw": view.accredited_capacity_mw,
            "capacity_credit_fraction": view.capacity_credit_fraction,
        },
        "artifacts": entries,
        "generated_at_utc": meta["generated_at_utc"],
        "use_boundary_code": view.use_boundary_code,
        "use_boundary_warning": view.use_boundary_warning,
    }
    (out / "artifact-manifest.json").write_text(
        json.dumps(artifact_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return {
        "run_id": run_id,
        "result_id": projection.get("result_id"),
        "output_dir": str(out.resolve()),
        "use_boundary_code": view.use_boundary_code,
        "accredited_capacity_mw": view.accredited_capacity_mw,
        "capacity_credit_fraction": view.capacity_credit_fraction,
        "search_trace_published": True,
        "demo_replayed": False,
        "run_replayed": False,
        "report_replayed": report_replayed,
        "run_reused": False,
        "report_reused": False,
        "stop_diagnostics": noted,
    }


__all__ = [
    "DEMO_SCHEMA_VERSION",
    "DemoError",
    "TeachingCaseDefinition",
    "compute_teaching_task_identity",
    "resolve_teaching_case",
    "run_demo",
    "source_identity",
    "validate_result_root",
]
