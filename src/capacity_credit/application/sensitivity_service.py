"""Sensitivity matrix application service (T19, P4).

An application service that runs a sensitivity matrix over an EXISTING
method pipeline (EFC / ELCC - never a second reliability engine). Each
matrix CELL is an INDEPENDENT run:

- the BASE run is created once and every cell is a child run whose
  ``parent_run_ids`` references the base run id (parent/child lineage);
- each cell changes EXACTLY ONE parameter (a dotted path + an explicit
  value); the service never invents a default value for a missing
  parameter and never changes several parameters in one cell and calls
  it single-factor sensitivity;
- every cell keeps its own ``run_id``; a cell that fails is MARKED
  (status=failed + error code) while the completed cells are preserved
  and the summary never writes 0 for a missing result;
- the matrix is RESUMABLE from a checkpoint: re-running skips cells
  that already have a completed run id.

The cell runs go through the same formal production write/read path as
any other method run (the pipeline writes run_config.yaml / run
manifest / method_result.json / artifact manifest and reads them back).
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import atomic_write_object, safe_filename_component
from capacity_credit.application.p1_workflow import P1WorkflowError


class SensitivityMatrixError(ValueError):
    """Stable sensitivity-service error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


P_SENSITIVITY_EMPTY_CELLS = "P_SENSITIVITY_EMPTY_CELLS"
P_SENSITIVITY_INVALID_PATH = "P_SENSITIVITY_INVALID_PATH"
P_SENSITIVITY_MULTI_PARAM = "P_SENSITIVITY_MULTI_PARAM"
P_SENSITIVITY_MISSING_VALUE = "P_SENSITIVITY_MISSING_VALUE"
P_SENSITIVITY_UNSUPPORTED_RUN_TYPE = "P_SENSITIVITY_UNSUPPORTED_RUN_TYPE"
P_SENSITIVITY_BASE_FAILED = "P_SENSITIVITY_BASE_FAILED"
P_SENSITIVITY_DUPLICATE_CELL = "P_SENSITIVITY_DUPLICATE_CELL"
P_SENSITIVITY_CHECKPOINT_MISMATCH = "P_SENSITIVITY_CHECKPOINT_MISMATCH"

_CHECKPOINT_SCHEMA_VERSION = "1"
SENSITIVITY_MATRIX_FILE = "sensitivity_matrix.json"


def _canonical_json(value: object) -> str:
    """Stable, order-independent canonical serialization for identity
    hashing (never used for pretty output)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class SensitivityCell:
    """One matrix cell = ONE parameter change + its own run.

    ``identity_hash`` is the stable identity of the cell (cell_id +
    parameter_path + canonical value); a changed parameter path or value
    changes the identity, so a checkpoint can never reuse a stale run."""

    cell_id: str
    parameter_path: str
    value: object
    identity_hash: str = ""
    run_id: str | None = None
    status: str = "pending"  # pending | completed | failed
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "parameter_path": self.parameter_path,
            "value": self.value,
            "identity_hash": self.identity_hash,
            "run_id": self.run_id,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def cell_identity_hash(cell: SensitivityCell) -> str:
    """Stable per-cell identity: cell_id + parameter_path + canonical
    value. A parameter change therefore changes the identity."""
    payload = f"{cell.cell_id}|{cell.parameter_path}|{_canonical_json(cell.value)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SensitivityMatrix:
    """The sensitivity matrix result: a base run + per-cell child runs.

    ``sensitivity_analysis_spec_hash`` is the FULL sha256 of the normalized
    SensitivityAnalysisSpec that produced this matrix; it is persisted into
    sensitivity_matrix.json and cross-checked on read-back (a changed spec
    -> a different parent coordination run, never a stale replay)."""

    base_run_id: str
    cells: tuple[SensitivityCell, ...]
    sensitivity_analysis_spec_hash: str = ""

    @property
    def completed_cells(self) -> tuple[SensitivityCell, ...]:
        return tuple(cell for cell in self.cells if cell.status == "completed")

    @property
    def failed_cells(self) -> tuple[SensitivityCell, ...]:
        return tuple(cell for cell in self.cells if cell.status == "failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "base_run_id": self.base_run_id,
            "sensitivity_analysis_spec_hash": self.sensitivity_analysis_spec_hash,
            "completed_count": len(self.completed_cells),
            "failed_count": len(self.failed_cells),
            "cells": [cell.to_dict() for cell in self.cells],
        }


@dataclass(frozen=True)
class SensitivityAnalysisSpec:
    """The NORMALIZED business specification whose FULL sha256 identifies a
    sensitivity analysis: the same spec -> idempotent parent coordination
    run; ANY change to a cell (id/path/value), the base config, the import
    batch, the data version, the scenario bank or the software commit ->
    a NEW identity (a new parent run is created, never a stale replay)."""

    import_batch_id: str
    data_version_id: str
    scenario_bank_id: str
    base_config_hash: str
    software_commit: str
    cells: tuple[tuple[str, str, object, str], ...]  # (cell_id, path, value, identity_hash)


def sensitivity_analysis_spec_hash(spec: SensitivityAnalysisSpec) -> str:
    """Full sha256 identity of a normalized sensitivity spec."""
    payload = _canonical_json(
        {
            "import_batch_id": spec.import_batch_id,
            "data_version_id": spec.data_version_id,
            "scenario_bank_id": spec.scenario_bank_id,
            "base_config_hash": spec.base_config_hash,
            "software_commit": spec.software_commit,
            "cells": [
                {"cell_id": cid, "parameter_path": path, "value": value, "identity_hash": ih}
                for cid, path, value, ih in spec.cells
            ],
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cells_to_spec_cells(cells: Sequence[tuple[str, str, object]]) -> tuple[tuple[str, str, object, str], ...]:
    return tuple(
        (
            cell_id,
            path,
            value,
            cell_identity_hash(SensitivityCell(cell_id=cell_id, parameter_path=path, value=value)),
        )
        for cell_id, path, value in cells
    )


def _validate_cell(cell: SensitivityCell) -> None:
    if not cell.cell_id.strip():
        raise SensitivityMatrixError(P_SENSITIVITY_EMPTY_CELLS, "cell_id must be non-blank")
    path = cell.parameter_path.strip()
    if not path:
        raise SensitivityMatrixError(P_SENSITIVITY_INVALID_PATH, "parameter_path must be non-blank")
    parts = path.split(".")
    if len(parts) < 2 or any(not part.strip() for part in parts):
        raise SensitivityMatrixError(
            P_SENSITIVITY_MULTI_PARAM,
            f"parameter_path must name ONE leaf parameter, got {cell.parameter_path!r}",
        )


def set_parameter(config: dict[str, Any], path: str, value: object) -> dict[str, Any]:
    """Return a NEW config with EXACTLY ONE leaf parameter changed. The
    path is a dotted leaf path (e.g. ``search.capacity_tolerance_mw``);
    the value is explicit - nothing is invented for a missing parameter,
    and an unknown path is rejected. A whole-section replacement is never
    treated as a single-factor sensitivity."""
    updated = copy.deepcopy(config)
    parts = path.split(".")
    if len(parts) < 2 or any(not part.strip() for part in parts):
        raise SensitivityMatrixError(
            P_SENSITIVITY_MULTI_PARAM,
            f"parameter_path must name ONE leaf parameter, got {path!r}",
        )
    node: dict[str, Any] = updated
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise SensitivityMatrixError(
                P_SENSITIVITY_INVALID_PATH,
                f"unknown parameter path {path!r} in the base config",
            )
        node = node[part]
    leaf = parts[-1]
    if leaf not in node:
        raise SensitivityMatrixError(
            P_SENSITIVITY_INVALID_PATH,
            f"unknown parameter path {path!r} in the base config",
        )
    node[leaf] = copy.deepcopy(value)
    return updated


def _child_config(
    base: dict[str, Any], path: str, value: object, base_run_id: str
) -> dict[str, Any]:
    child = set_parameter(base, path, value)
    child["run_config"]["parent_run_ids"] = [base_run_id]
    return child


def _runner_for(run_type: str) -> Callable[..., dict[str, Any]]:
    from capacity_credit.application.efc_workflow import run_efc_pipeline
    from capacity_credit.application.elcc_workflow import run_elcc_pipeline

    if run_type == "efc":
        return run_efc_pipeline
    if run_type == "elcc":
        return run_elcc_pipeline
    raise SensitivityMatrixError(
        P_SENSITIVITY_UNSUPPORTED_RUN_TYPE,
        f"sensitivity service supports run_type=efc/elcc, got {run_type!r}",
    )


def _sensitivity_path(project: Path, base_run_id: str) -> Path:
    # PROJECT-LEVEL checkpoint directory (never the frozen base-run artifact
    # directory; the coordination run publishes its own immutable copy).
    return (
        project
        / "checkpoints"
        / "sensitivity"
        / safe_filename_component(base_run_id)
        / SENSITIVITY_MATRIX_FILE
    )


def _base_run_config(
    run_service: Any, project: Path, base_run_id: str
) -> tuple[Any, dict[str, Any]]:
    """Read the base run's frozen RunConfig + state and verify it exists and
    is completed. Returns (run_config, run_state)."""
    from capacity_credit.contracts.runs import load_run_config

    run_dir = project / "runs" / safe_filename_component(base_run_id)
    config_path = run_dir / "run_config.yaml"
    if not config_path.is_file():
        raise SensitivityMatrixError(
            P_SENSITIVITY_BASE_FAILED,
            f"base sensitivity run {base_run_id} is missing run_config.yaml",
        )
    try:
        config = load_run_config(_yaml_safe_load(config_path.read_text(encoding="utf-8")))
    except Exception as exc:
        raise SensitivityMatrixError(
            P_SENSITIVITY_BASE_FAILED,
            f"base sensitivity run config unreadable: {exc}",
        ) from exc
    state = run_service.run_state(base_run_id)
    return config, state


def _yaml_safe_load(text: str) -> dict[str, Any]:
    import yaml

    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise SensitivityMatrixError(P_SENSITIVITY_BASE_FAILED, "run_config.yaml is not a mapping")
    return payload


def publish_sensitivity_matrix(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    matrix: SensitivityMatrix,
) -> dict[str, Any]:
    """Publish the sensitivity matrix into a FORMAL SENSITIVITY run
    (``RunType.SENSITIVITY``): the coordination-run configuration is DERIVED
    from the base run's frozen config (nothing is hard-coded), the base run
    is verified (exists / completed / consistent import, data version,
    scenario bank), and the matrix artifact is registered in the run's
    artifact-manifest-1.1 COORDINATION manifest."""
    from capacity_credit.application.coordination_run_service import (
        CoordinationAction,
        complete_coordination_run,
        create_coordination_run,
        fail_coordination_run,
    )
    from capacity_credit.application.import_service import ImportService
    from capacity_credit.application.run_service import RunService
    from capacity_credit.contracts.enums import LifecycleStatus

    project = Path(project_dir)
    run_service = RunService(project, ImportService(project))
    base_config, base_state = _base_run_config(run_service, project, matrix.base_run_id)
    if base_state["lifecycle_status"] != LifecycleStatus.COMPLETED.value:
        raise SensitivityMatrixError(
            P_SENSITIVITY_BASE_FAILED,
            f"base sensitivity run {matrix.base_run_id} is not completed",
        )
    # verify import / data / scenario-bank consistency with the request
    base_manifest_path = (
        project / "runs" / safe_filename_component(matrix.base_run_id) / "run_manifest.json"
    )
    if not base_manifest_path.is_file():
        raise SensitivityMatrixError(
            P_SENSITIVITY_BASE_FAILED, "base sensitivity run manifest missing"
        )
    base_manifest = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    if str(base_manifest.get("import_batch_id")) != import_batch_id:
        raise SensitivityMatrixError(
            P_SENSITIVITY_BASE_FAILED, "base run import_batch_id does not match the request"
        )
    if str(base_manifest.get("data_version_id")) != data_version_id:
        raise SensitivityMatrixError(
            P_SENSITIVITY_BASE_FAILED, "base run data_version_id does not match the request"
        )

    scenario_bank_id = base_config.scenario_bank_id
    run_config = {
        "run_config_schema_version": "1.0",
        "run_mode": base_config.run_mode.value,
        "run_type": "sensitivity",
        "source_run_id": uuid.UUID(matrix.base_run_id),
        "parent_run_ids": [],
        "method_profile": base_config.method_profile,
        "primary_risk_metric": base_config.primary_risk_metric.value,
        "weather_year_weight_set_id": base_config.weather_year_weight_set_id,
        "chronology_mode": base_config.chronology_mode.value,
        "storage_soc_initialization_mode": base_config.storage_soc_initialization_mode.value,
        "soc_year_boundary_mode": base_config.soc_year_boundary_mode.value,
        "assessment_anchor": (
            base_config.assessment_anchor.value if base_config.assessment_anchor else None
        ),
        "perturbation_dimension": (
            base_config.perturbation_dimension.value if base_config.perturbation_dimension else None
        ),
        "normalization_basis_mwh_per_mw": base_config.normalization_basis_mwh_per_mw,
        "capacity_value_type": "not_applicable",
        "baseline_portfolio_version": None,
        "allocation_type": "none",
        "allocation_rule_id": None,
        "random_seed": base_config.random_seed,
        "scenario_bank_id": scenario_bank_id,
        "scenario_id": base_config.scenario_id,
        "method_config": None,
    }
    base_config_hash = hashlib.sha256(
        _canonical_json(base_config.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    spec_hash = sensitivity_analysis_spec_hash(
        SensitivityAnalysisSpec(
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            scenario_bank_id=scenario_bank_id,
            base_config_hash=base_config_hash,
            software_commit=software_commit,
            cells=tuple(
                (
                    cell.cell_id,
                    cell.parameter_path,
                    cell.value,
                    cell.identity_hash or cell_identity_hash(cell),
                )
                for cell in matrix.cells
            ),
        )
    )
    start = create_coordination_run(
        project=project,
        run_service=run_service,
        import_batch_id=import_batch_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        run_type="sensitivity",
        run_config=run_config,
        request_id=f"{request_id}-{spec_hash}-sensitivity-parent",
        scenario_bank_id=scenario_bank_id,
        expected_artifacts=(SENSITIVITY_MATRIX_FILE,),
    )
    run_id = start.run_id
    run_dir = start.run_dir
    try:
        # The execution claim (held since create_coordination_run) covers the
        # WHOLE cycle: recovery decision -> artifact publish -> completed /
        # failed transition. It is released in the finally below.
        if start.action in (CoordinationAction.REPLAY_COMPLETED, CoordinationAction.FINALIZE_EXISTING):
            # Read-back path: a completed (REPLAY) or fully-published-but-not-
            # completed (FINALIZE) sensitivity run. Return the ORIGINAL run_id /
            # manifest id / artifact paths; verify the persisted matrix spec
            # identity; FINALIZE only adds the completed transition (nothing is
            # recomputed or rewritten).
            matrix_path = run_dir / SENSITIVITY_MATRIX_FILE
            try:
                persisted = _matrix_from_payload(
                    json.loads(matrix_path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError) as exc:
                raise SensitivityMatrixError(
                    P_SENSITIVITY_BASE_FAILED,
                    f"replay: sensitivity_matrix.json read-back failed: {exc}",
                ) from exc
            if persisted.sensitivity_analysis_spec_hash != spec_hash:
                raise SensitivityMatrixError(
                    P_SENSITIVITY_BASE_FAILED,
                    "replay: persisted sensitivity_matrix.json spec identity does not match "
                    "the requested sensitivity spec",
                )
            if start.action == CoordinationAction.FINALIZE_EXISTING:
                complete_coordination_run(
                    run_service=run_service, run_id=run_id, request_id=request_id
                )
            return {
                "sensitivity_run_id": run_id,
                "manifest_id": str(start.manifest_id) if start.manifest_id else "",
                "matrix_path": str(matrix_path),
                "artifact_manifest_path": str(start.manifest_path or (run_dir / "artifact_manifest.json")),
            }
        matrix = SensitivityMatrix(
            base_run_id=matrix.base_run_id,
            cells=matrix.cells,
            sensitivity_analysis_spec_hash=spec_hash,
        )
        try:
            manifest_path, manifest_id = _publish_sensitivity_matrix_into_run(
                run_dir=run_dir,
                run_id=run_id,
                matrix=matrix,
                created_by=created_by,
                source_run_id=matrix.base_run_id,
            )
            complete_coordination_run(run_service=run_service, run_id=run_id, request_id=request_id)
        except BaseException as exc:
            fail_coordination_run(
                run_service=run_service,
                run_id=run_id,
                request_id=request_id,
                error_code=P_SENSITIVITY_BASE_FAILED,
                message=f"sensitivity matrix publish failed: {exc}",
            )
            raise
        return {
            "sensitivity_run_id": run_id,
            "manifest_id": manifest_id,
            "matrix_path": str(run_dir / SENSITIVITY_MATRIX_FILE),
            "artifact_manifest_path": str(manifest_path),
        }
    finally:
        start.claim.release()


def _publish_sensitivity_matrix_into_run(
    *,
    run_dir: Path,
    run_id: str,
    matrix: SensitivityMatrix,
    created_by: str,
    source_run_id: str,
) -> tuple[Path, str]:
    """The SHARED sensitivity coordination publish step (used by both
    publish_sensitivity_matrix and run_sensitivity_analysis):

    - writes the immutable sensitivity_matrix.json;
    - builds + validates the artifact-manifest-1.1 COORDINATION manifest;
    - atomically publishes it and reads it back.
    Any failure here must be handled by the caller (transition the parent
    run to failed, never completed)."""
    from capacity_credit.application.atomic_io import atomic_write_object
    from capacity_credit.application.coordination_run_service import (
        analysis_summary_entry,
        write_coordination_manifest11,
    )

    matrix_path = run_dir / SENSITIVITY_MATRIX_FILE
    atomic_write_object(matrix_path, matrix.to_dict(), immutable=True)
    manifest_id = str(uuid.uuid4())
    entries = [
        analysis_summary_entry(
            run_dir, SENSITIVITY_MATRIX_FILE, "capacity_credit.application.sensitivity_service"
        )
    ]
    manifest_path = write_coordination_manifest11(
        run_dir=run_dir,
        run_id=run_id,
        run_type="sensitivity",
        manifest_id=manifest_id,
        created_by=created_by,
        entries=entries,
        source_run_id=source_run_id,
    )
    return manifest_path, manifest_id


def _matrix_from_payload(payload: dict[str, Any]) -> SensitivityMatrix:
    """Reconstruct a SensitivityMatrix from the persisted
    sensitivity_matrix.json payload (replay read-back)."""
    return SensitivityMatrix(
        base_run_id=payload["base_run_id"],
        sensitivity_analysis_spec_hash=payload.get("sensitivity_analysis_spec_hash", ""),
        cells=tuple(
            SensitivityCell(
                cell_id=c["cell_id"],
                parameter_path=c["parameter_path"],
                value=c["value"],
                identity_hash=c.get("identity_hash", ""),
                run_id=c.get("run_id"),
                status=c.get("status", "pending"),
                error_code=c.get("error_code"),
                error_message=c.get("error_message"),
            )
            for c in payload["cells"]
        ),
    )


def run_sensitivity_analysis(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    base_config: dict[str, Any],
    request_prefix: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    cells: Sequence[tuple[str, str, object]],
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """High-level sensitivity analysis (single entry):

    1. creates a FORMAL SENSITIVITY coordination parent run
       (created -> queued -> running) BEFORE any computation;
    2. runs the base EFC as a CHILD of the sensitivity run and the cells
       as children of the base (parent/child lineage is verifiable);
    3. persists the checkpoint in the project-level checkpoint directory;
    4. publishes the immutable sensitivity_matrix.json + the
       artifact-manifest-1.1 COORDINATION manifest into the sensitivity run;
    5. running -> completed. Any unhandled error transitions the parent to
        failed (never writes completed)."""
    from capacity_credit.application.coordination_run_service import (
        CoordinationAction,
        complete_coordination_run,
        create_coordination_run,
        fail_coordination_run,
    )
    from capacity_credit.application.import_service import ImportService
    from capacity_credit.application.run_service import RunService

    project = Path(project_dir)
    run_service = RunService(project, ImportService(project))
    base_config = copy.deepcopy(base_config)
    scenario_bank_id = base_config["run_config"]["scenario_bank_id"]
    run_config = {
        **base_config["run_config"],
        "run_type": "sensitivity",
        "source_run_id": None,
        "parent_run_ids": [],
        "method_config": None,
        "capacity_value_type": "not_applicable",
        "baseline_portfolio_version": None,
    }
    spec_hash = sensitivity_analysis_spec_hash(
        SensitivityAnalysisSpec(
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            scenario_bank_id=scenario_bank_id,
            base_config_hash=hashlib.sha256(
                _canonical_json(base_config).encode("utf-8")
            ).hexdigest(),
            software_commit=software_commit,
            cells=_cells_to_spec_cells(cells),
        )
    )
    start = create_coordination_run(
        project=project,
        run_service=run_service,
        import_batch_id=import_batch_id,
        created_by=created_by,
        software_version=software_version,
        software_commit=software_commit,
        run_type="sensitivity",
        run_config=run_config,
        request_id=f"{request_prefix}-{spec_hash}-sensitivity-parent",
        scenario_bank_id=scenario_bank_id,
        expected_artifacts=(SENSITIVITY_MATRIX_FILE,),
    )
    run_id = start.run_id
    run_dir = start.run_dir
    try:
        # The execution claim (held since create_coordination_run) covers the
        # WHOLE cycle: recovery decision -> child scheduling / computation ->
        # artifact + manifest publish -> completed/failed. It is released in
        # the finally below.
        if start.action in (CoordinationAction.REPLAY_COMPLETED, CoordinationAction.FINALIZE_EXISTING):
            # Read-back path: a completed (REPLAY) or fully-published-but-not-
            # completed (FINALIZE) sensitivity analysis. Return the ORIGINAL
            # run_id / manifest id / artifact paths and verify the persisted
            # matrix spec identity; FINALIZE only adds the completed transition
            # (nothing is recomputed or rewritten).
            matrix_path = run_dir / SENSITIVITY_MATRIX_FILE
            try:
                persisted = _matrix_from_payload(json.loads(matrix_path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                raise SensitivityMatrixError(
                    P_SENSITIVITY_BASE_FAILED,
                    f"replay: sensitivity_matrix.json read-back failed: {exc}",
                ) from exc
            if persisted.sensitivity_analysis_spec_hash != spec_hash:
                raise SensitivityMatrixError(
                    P_SENSITIVITY_BASE_FAILED,
                    "replay: persisted sensitivity_matrix.json spec identity does not match "
                    "the requested sensitivity spec",
                )
            if start.action == CoordinationAction.FINALIZE_EXISTING:
                complete_coordination_run(
                    run_service=run_service, run_id=run_id, request_id=request_prefix
                )
            return {
                "sensitivity_run_id": run_id,
                "base_run_id": persisted.base_run_id,
                "matrix_path": str(matrix_path),
                "artifact_manifest_path": str(start.manifest_path or (run_dir / "artifact_manifest.json")),
            }
        try:
            child_base = copy.deepcopy(base_config)
            child_base["run_config"]["parent_run_ids"] = [run_id]
            matrix = run_sensitivity_matrix(
                project_dir=project,
                import_batch_id=import_batch_id,
                data_version_id=data_version_id,
                base_config=child_base,
                request_prefix=request_prefix,
                created_by=created_by,
                software_version=software_version,
                software_commit=software_commit,
                cells=cells,
                runner=runner,
                sensitivity_analysis_spec_hash=spec_hash,
            )
            manifest_path, _manifest_id = _publish_sensitivity_matrix_into_run(
                run_dir=run_dir,
                run_id=run_id,
                matrix=matrix,
                created_by=created_by,
                source_run_id=matrix.base_run_id,
            )
            complete_coordination_run(run_service=run_service, run_id=run_id, request_id=request_prefix)
            return {
                "sensitivity_run_id": run_id,
                "base_run_id": matrix.base_run_id,
                "matrix_path": str(run_dir / SENSITIVITY_MATRIX_FILE),
                "artifact_manifest_path": str(manifest_path),
            }
        except BaseException as exc:
            fail_coordination_run(
                run_service=run_service,
                run_id=run_id,
                request_id=request_prefix,
                error_code=P_SENSITIVITY_BASE_FAILED,
                message=f"sensitivity analysis failed: {exc}",
            )
            raise  # fail_coordination_run already transitioned the parent to failed
    finally:
        start.claim.release()


def _base_execution_hash(
    *,
    import_batch_id: str,
    data_version_id: str,
    scenario_bank_id: str,
    software_commit: str,
    base_config: dict[str, Any],
) -> str:
    """FULL sha256 of the normalized BASE EXECUTION identity.

    Computed from the EFFECTIVE base config (the exact app_config dict that
    will be dispatched - AFTER the formal parent run_id has been injected
    into parent_run_ids), plus import_batch_id / data_version_id /
    scenario_bank_id / software_commit. Any change to any of these forms a
    new base request identity, so the same request_prefix never reuses a
    stale base request (no REQUEST_ID_CONFLICT), and the cell request ids
    (which bind this identity) follow the base identity."""
    payload = _canonical_json(
        {
            "import_batch_id": import_batch_id,
            "data_version_id": data_version_id,
            "scenario_bank_id": scenario_bank_id,
            "software_commit": software_commit,
            "base_config": base_config,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cell_request_id(
    request_prefix: str, base_execution_hash: str, cell: SensitivityCell
) -> str:
    """Identity-scoped cell request id bound to BOTH the full base execution
    identity AND the full cell identity hash: a changed parameter value/path
    (cell identity) OR a changed base execution (base config / import / data
    version / scenario bank / software commit) changes the request id, so a
    stale request can never be replayed (full sha256, never truncated)."""
    identity = cell.identity_hash or cell_identity_hash(cell)
    return f"{request_prefix}-cell-{cell.cell_id}-{base_execution_hash}-{identity}"


def _read_checkpoint(project: Path, base_run_id: str) -> dict[str, Any] | None:
    """Read the checkpoint. A missing / corrupt / version-mismatched file
    returns None (SAFE RECOMPUTE) - it is never silently reused, and the
    already-completed formal child runs are never deleted."""
    path = _sensitivity_path(project, base_run_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION:
        return None
    if payload.get("base_run_id") != base_run_id:
        return None
    if not isinstance(payload.get("cells"), list):
        return None
    return payload


def run_sensitivity_matrix(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    base_config: dict[str, Any],
    request_prefix: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    cells: Sequence[tuple[str, str, object]],  # (cell_id, parameter_path, value)
    runner: Callable[..., dict[str, Any]] | None = None,
    sensitivity_analysis_spec_hash: str = "",
) -> SensitivityMatrix:
    """Run the sensitivity matrix.

    - the base config is run once (``request_prefix``) and every cell is
      a child run of the base (parent_run_ids lineage);
    - one parameter per cell (single-factor; never several at once);
    - completed cells survive a failing cell and the summary never
      zero-fills a missing result;
    - re-running the same matrix resumes: cells that already have a
      completed run id are skipped (checkpoint).
    """
    project = Path(project_dir)
    base_config = copy.deepcopy(base_config)
    run_type = str(base_config["run_config"]["run_type"])
    dispatch = runner or _runner_for(run_type)

    parsed_cells: list[SensitivityCell] = []
    seen_cell_ids: set[str] = set()
    for cell_id, path, value in cells:
        cell = SensitivityCell(cell_id=cell_id, parameter_path=path, value=value)
        _validate_cell(cell)
        if cell.cell_id in seen_cell_ids:
            raise SensitivityMatrixError(
                P_SENSITIVITY_DUPLICATE_CELL,
                f"duplicate sensitivity cell_id {cell.cell_id!r}",
            )
        seen_cell_ids.add(cell.cell_id)
        identity = cell_identity_hash(cell)
        parsed_cells.append(
            SensitivityCell(
                cell_id=cell.cell_id,
                parameter_path=cell.parameter_path,
                value=cell.value,
                identity_hash=identity,
            )
        )
    if not parsed_cells:
        raise SensitivityMatrixError(P_SENSITIVITY_EMPTY_CELLS, "no sensitivity cells provided")

    scenario_bank_id = str(base_config["run_config"]["scenario_bank_id"])
    base_execution_hash = _base_execution_hash(
        import_batch_id=import_batch_id,
        data_version_id=data_version_id,
        scenario_bank_id=scenario_bank_id,
        software_commit=software_commit,
        base_config=base_config,
    )
    base_request = f"{request_prefix}-base-{base_execution_hash}"
    try:
        base_projection = dispatch(
            project_dir=project,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            request_id=base_request,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            app_config=base_config,
        )
    except Exception as exc:
        raise SensitivityMatrixError(
            P_SENSITIVITY_BASE_FAILED,
            f"base sensitivity run failed: {exc}",
        ) from exc
    base_run_id = str(base_projection["run_id"])

    checkpoint = _read_checkpoint(project, base_run_id) or {}
    checkpoint_cells = {c["cell_id"]: c for c in checkpoint.get("cells", [])}

    result_cells: list[SensitivityCell] = []
    for cell in parsed_cells:
        prior = checkpoint_cells.get(cell.cell_id)
        # Reuse ONLY when the full identity matches AND the old cell was
        # completed with a run id; a changed path/value (identity change)
        # never reuses the old run.
        if (
            prior
            and prior.get("status") == "completed"
            and prior.get("run_id")
            and prior.get("identity_hash") == cell.identity_hash
        ):
            result_cells.append(
                SensitivityCell(
                    cell_id=cell.cell_id,
                    parameter_path=cell.parameter_path,
                    value=cell.value,
                    identity_hash=cell.identity_hash,
                    run_id=prior["run_id"],
                    status="completed",
                )
            )
            continue
        child_config = _child_config(base_config, cell.parameter_path, cell.value, base_run_id)
        cell_request = _cell_request_id(request_prefix, base_execution_hash, cell)
        try:
            projection = dispatch(
                project_dir=project,
                import_batch_id=import_batch_id,
                data_version_id=data_version_id,
                request_id=cell_request,
                created_by=created_by,
                software_version=software_version,
                software_commit=software_commit,
                app_config=child_config,
            )
            result_cells.append(
                SensitivityCell(
                    cell_id=cell.cell_id,
                    parameter_path=cell.parameter_path,
                    value=cell.value,
                    identity_hash=cell.identity_hash,
                    run_id=str(projection["run_id"]),
                    status="completed",
                )
            )
        except (P1WorkflowError, ValueError) as exc:
            result_cells.append(
                SensitivityCell(
                    cell_id=cell.cell_id,
                    parameter_path=cell.parameter_path,
                    value=cell.value,
                    identity_hash=cell.identity_hash,
                    status="failed",
                    error_code=getattr(exc, "code", None) or "SENSITIVITY_CELL_FAILED",
                    error_message=str(exc),
                )
            )

    matrix = SensitivityMatrix(
        base_run_id=base_run_id,
        cells=tuple(result_cells),
        sensitivity_analysis_spec_hash=sensitivity_analysis_spec_hash,
    )
    _sensitivity_path(project, base_run_id).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_object(_sensitivity_path(project, base_run_id), matrix.to_dict())
    return matrix


__all__ = [
    "P_SENSITIVITY_BASE_FAILED",
    "P_SENSITIVITY_CHECKPOINT_MISMATCH",
    "P_SENSITIVITY_DUPLICATE_CELL",
    "P_SENSITIVITY_EMPTY_CELLS",
    "P_SENSITIVITY_INVALID_PATH",
    "P_SENSITIVITY_MISSING_VALUE",
    "P_SENSITIVITY_MULTI_PARAM",
    "P_SENSITIVITY_UNSUPPORTED_RUN_TYPE",
    "SENSITIVITY_MATRIX_FILE",
    "SensitivityAnalysisSpec",
    "SensitivityCell",
    "SensitivityMatrix",
    "SensitivityMatrixError",
    "cell_identity_hash",
    "publish_sensitivity_matrix",
    "run_sensitivity_analysis",
    "run_sensitivity_matrix",
    "sensitivity_analysis_spec_hash",
    "set_parameter",
]
