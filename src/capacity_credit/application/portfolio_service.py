"""Portfolio analysis application service (T19 round-2, P4).

A SINGLE application entry for combination / marginal / penetration
analysis on top of the existing EFC pipeline (never a second reliability
engine or a second result writer):

- creates INDEPENDENT member / portfolio / marginal / penetration EFC
  runs through the production ``run_efc_pipeline``;
- creates a FORMAL PORTFOLIO coordination parent run
  (``RunType.PORTFOLIO``) with the correct created -> queued -> running ->
  completed lifecycle, and records the child runs' parent/child lineage;
- closes the accounting with the PURE ``solvers.portfolio`` combiner;
- persists portfolio_summary.json / penetration_sequence.json /
  governance_allocation.json / portfolio_analysis_spec.json into the
  PORTFOLIO run directory and registers them in that run's
  artifact-manifest-1.1 COORDINATION manifest.

The portfolio analysis is identified by a stable ``analysis_spec_hash``
(computed from the normalized business specification) that:
  - is embedded in the coordination-parent request identity and every
    child request identity (same spec -> same idempotency key; a changed
    spec -> a different identity, never reusing the old results);
  - is written into portfolio_analysis_spec.json and registered in the
    coordination manifest;
  - is re-computed and cross-checked in the read-back loop.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capacity_credit.application.atomic_io import atomic_write_object, safe_filename_component
from capacity_credit.application.coordination_run_service import (
    P_COORDINATION_RUN_IN_PROGRESS,
    P_COORDINATION_RUN_INVALID,
    P_COORDINATION_RUN_STOPPING,
    CoordinationAction,
    CoordinationRunServiceError,
    analysis_summary_entry,
    complete_coordination_run,
    create_coordination_run,
    fail_coordination_run,
    write_coordination_manifest11,
)
from capacity_credit.application.efc_workflow import run_efc_pipeline
from capacity_credit.application.import_service import ImportService
from capacity_credit.application.result_service import read_method_result_any
from capacity_credit.application.run_service import RunService
from capacity_credit.contracts.results import MethodResult
from capacity_credit.solvers.portfolio import (
    GovernanceAllocation,
    MarginalCredit,
    MemberCredit,
    PenetrationPoint,
    PenetrationSequence,
    PortfolioSummary,
    combine_portfolio,
    marginal_credit_from_result,
    member_credit_from_result,
    penetration_sequence,
)
from capacity_credit.solvers.portfolio import (
    governance_allocation as build_governance_allocation,
)

PORTFOLIO_SUMMARY_FILE = "portfolio_summary.json"
PENETRATION_SEQUENCE_FILE = "penetration_sequence.json"
GOVERNANCE_ALLOCATION_FILE = "governance_allocation.json"
PORTFOLIO_SPEC_FILE = "portfolio_analysis_spec.json"


class PortfolioServiceError(ValueError):
    """Stable portfolio-service error with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


P_PORTFOLIO_SERVICE_INVALID = "P_PORTFOLIO_SERVICE_INVALID"
P_PORTFOLIO_SERVICE_BASE_FAILED = "P_PORTFOLIO_SERVICE_BASE_FAILED"
P_PORTFOLIO_SPEC_HASH_MISMATCH = "P_PORTFOLIO_SPEC_HASH_MISMATCH"


@dataclass(frozen=True)
class PortfolioMemberSpec:
    member_id: str
    candidate_ids: tuple[str, ...]
    nominal_capacity_mw: float


@dataclass(frozen=True)
class MarginalSpec:
    member_id: str
    candidate_ids: tuple[str, ...]
    baseline_portfolio_version: str


@dataclass(frozen=True)
class PenetrationSpec:
    point_id: str
    nominal_capacity_mw: float
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioAnalysisSpec:
    """The normalized business specification whose hash identifies the
    portfolio analysis (same spec -> idempotent; changed spec -> a new
    identity, never reusing the old results)."""

    members: tuple[PortfolioMemberSpec, ...]
    portfolio_candidate_ids: tuple[str, ...]
    nominal_capacity_mw: float
    marginal: MarginalSpec | None = None
    governance_rule_id: str | None = None
    governance_allocations: tuple[tuple[str, float | None], ...] = ()
    penetration_points: tuple[PenetrationSpec, ...] = ()
    method_profile: str = "efc_teaching"
    import_batch_id: str = ""
    data_version_id: str = ""
    scenario_bank_id: str = ""
    software_commit: str = ""
    base_app_config_hash: str = ""


@dataclass(frozen=True)
class PortfolioAnalysisResult:
    portfolio_run_id: str
    member_run_ids: dict[str, str]
    portfolio_efc_run_id: str
    marginal_run_id: str | None
    summary: PortfolioSummary
    penetration: PenetrationSequence | None
    spec_hash: str
    manifest_path: Path


def _spec_to_dict(spec: PortfolioAnalysisSpec) -> dict[str, Any]:
    return {
        "members": [
            {
                "member_id": m.member_id,
                "candidate_ids": list(m.candidate_ids),
                "nominal_capacity_mw": m.nominal_capacity_mw,
            }
            for m in spec.members
        ],
        "portfolio_candidate_ids": list(spec.portfolio_candidate_ids),
        "nominal_capacity_mw": spec.nominal_capacity_mw,
        "marginal": (
            {
                "member_id": spec.marginal.member_id,
                "candidate_ids": list(spec.marginal.candidate_ids),
                "baseline_portfolio_version": spec.marginal.baseline_portfolio_version,
            }
            if spec.marginal
            else None
        ),
        "governance_rule_id": spec.governance_rule_id,
        "governance_allocations": list(spec.governance_allocations),
        "penetration_points": [
            {
                "point_id": p.point_id,
                "nominal_capacity_mw": p.nominal_capacity_mw,
                "candidate_ids": list(p.candidate_ids),
            }
            for p in spec.penetration_points
        ],
        "method_profile": spec.method_profile,
        "import_batch_id": spec.import_batch_id,
        "data_version_id": spec.data_version_id,
        "scenario_bank_id": spec.scenario_bank_id,
        "software_commit": spec.software_commit,
        "base_app_config_hash": spec.base_app_config_hash,
    }


def analysis_spec_hash(spec: PortfolioAnalysisSpec) -> str:
    """Stable identity of the portfolio analysis specification (canonical
    JSON -> sha256)."""
    payload = json.dumps(
        _spec_to_dict(spec), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _portfolio_summary_from_payload(payload: dict[str, Any]) -> PortfolioSummary:
    """Reconstruct a PortfolioSummary from the persisted
    portfolio_summary.json payload (replay read-back)."""
    return PortfolioSummary(
        portfolio_member_ids=tuple(payload["portfolio_member_ids"]),
        portfolio_run_id=payload["portfolio_run_id"],
        portfolio_capacity_mw=payload["portfolio_capacity_mw"],
        portfolio_credit_fraction=payload["portfolio_credit_fraction"],
        portfolio_nominal_capacity_mw=payload["portfolio_nominal_capacity_mw"],
        sum_of_individuals_mw=payload["sum_of_individuals_mw"],
        arithmetic_difference_mw=payload["arithmetic_difference_mw"],
        diagnostic_complete=payload["diagnostic_complete"],
        member_credits=tuple(
            MemberCredit(
                member_id=mc["member_id"],
                run_id=mc["run_id"],
                capacity_credit_mw=mc["capacity_credit_mw"],
                capacity_credit_fraction=mc["capacity_credit_fraction"],
                nominal_capacity_mw=mc["nominal_capacity_mw"],
                result_status=mc["result_status"],
                risk_convergence=mc.get("risk_convergence", ""),
            )
            for mc in payload["member_credits"]
        ),
        marginal_credits=tuple(
            MarginalCredit(
                member_id=m["member_id"],
                run_id=m["run_id"],
                baseline_portfolio_version=m["baseline_portfolio_version"],
                incremental_capacity_mw=m["incremental_capacity_mw"],
                marginal_credit_mw=m["marginal_credit_mw"],
                marginal_credit_fraction=m["marginal_credit_fraction"],
                result_status=m["result_status"],
            )
            for m in payload["marginal_credits"]
        ),
        allocation=(
            GovernanceAllocation(
                allocation_rule_id=payload["allocation"]["allocation_rule_id"],
                member_allocations=tuple(payload["allocation"]["member_allocations"]),
            )
            if payload["allocation"] is not None
            else None
        ),
        capacity_credit_type=payload["capacity_credit_type"],
    )


def _efc_child_config(
    base_app_config: dict[str, Any],
    candidate_ids: list[str],
    *,
    capacity_value_type: str,
    baseline: str | None,
    parent_run_id: str,
    method_profile: str,
) -> dict[str, Any]:
    config = copy.deepcopy(base_app_config)
    single = candidate_ids[0] if len(candidate_ids) == 1 else None
    config["run_config"]["parent_run_ids"] = [parent_run_id]
    config["run_config"]["capacity_value_type"] = capacity_value_type
    config["run_config"]["baseline_portfolio_version"] = baseline
    config["run_config"]["method_profile"] = method_profile
    config["candidate"] = {
        "resource_or_portfolio_id": "+".join(candidate_ids),
        "resource_group_id": single,
        "resource_group_ids": candidate_ids if len(candidate_ids) > 1 else None,
        "reference_capacity_mw": None,
    }
    return config


def _result_from_run(project: Path, run_id: str) -> MethodResult:
    run_dir = project / "runs" / safe_filename_component(run_id)
    result = read_method_result_any(run_dir / "method_result.json")
    if not isinstance(result, MethodResult):
        raise PortfolioServiceError(
            P_PORTFOLIO_SERVICE_INVALID, "portfolio child run did not produce a MethodResult"
        )
    return result


def run_portfolio_analysis(
    *,
    project_dir: Path | str,
    import_batch_id: str,
    data_version_id: str,
    request_id: str,
    created_by: str,
    software_version: str,
    software_commit: str,
    base_app_config: dict[str, Any],
    members: list[PortfolioMemberSpec],
    portfolio_candidate_ids: list[str],
    nominal_capacity_mw: float,
    marginal: MarginalSpec | None = None,
    governance_rule_id: str | None = None,
    governance_allocations: tuple[tuple[str, float | None], ...] = (),
    penetration_points: list[PenetrationSpec] | None = None,
    method_profile: str = "efc_teaching",
) -> PortfolioAnalysisResult:
    """Run the full portfolio analysis through the PRODUCTION pipeline with
    a stable analysis-spec identity."""
    project = Path(project_dir)
    base_app_config = dict(base_app_config)
    import_service = ImportService(project)
    run_service = RunService(project, import_service)
    scenario_bank_id = base_app_config["run_config"]["scenario_bank_id"]
    base_app_config_hash = hashlib.sha256(
        json.dumps(
            base_app_config, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    spec = PortfolioAnalysisSpec(
        members=tuple(members),
        portfolio_candidate_ids=tuple(portfolio_candidate_ids),
        nominal_capacity_mw=nominal_capacity_mw,
        marginal=marginal,
        governance_rule_id=governance_rule_id,
        governance_allocations=tuple(governance_allocations),
        penetration_points=tuple(penetration_points or []),
        method_profile=method_profile,
        import_batch_id=import_batch_id,
        data_version_id=data_version_id,
        scenario_bank_id=scenario_bank_id,
        software_commit=software_commit,
        base_app_config_hash=base_app_config_hash,
    )
    spec_hash = analysis_spec_hash(spec)

    parent_run_id = ""
    parent_run_dir = project / "runs"
    expected_artifacts = [PORTFOLIO_SUMMARY_FILE, PORTFOLIO_SPEC_FILE]
    if penetration_points:
        expected_artifacts.append(PENETRATION_SEQUENCE_FILE)
    if governance_rule_id is not None:
        expected_artifacts.append(GOVERNANCE_ALLOCATION_FILE)
    try:
        start = create_coordination_run(
            project=project,
            run_service=run_service,
            import_batch_id=import_batch_id,
            created_by=created_by,
            software_version=software_version,
            software_commit=software_commit,
            run_type="portfolio",
            run_config={
                **base_app_config["run_config"],
                "run_type": "portfolio",
                "source_run_id": None,
                "parent_run_ids": [],
                "method_config": None,
            },
            request_id=f"{request_id}-{spec_hash}-portfolio-parent",
            scenario_bank_id=scenario_bank_id,
            expected_artifacts=tuple(expected_artifacts),
        )
    except CoordinationRunServiceError as exc:
        if exc.code in (
            P_COORDINATION_RUN_IN_PROGRESS,
            P_COORDINATION_RUN_STOPPING,
            P_COORDINATION_RUN_INVALID,
        ):
            # Stable pre-execution codes: re-raised unchanged - they must
            # NEVER reach fail_coordination_run, so a racing second request
            # can never mark the first owner's parent run failed.
            raise
        raise PortfolioServiceError(
            P_PORTFOLIO_SERVICE_BASE_FAILED,
            f"failed to create portfolio coordination run: {exc}",
        ) from exc
    parent_run_id = start.run_id
    parent_run_dir = start.run_dir
    try:
        # The execution claim (held since create_coordination_run) covers the
        # WHOLE cycle: recovery decision -> child scheduling / computation ->
        # artifact + manifest publish -> completed/failed -> release. It is
        # released in the finally below.
        if start.action in (CoordinationAction.REPLAY_COMPLETED, CoordinationAction.FINALIZE_EXISTING):
            # Read-back path: a completed (REPLAY) or fully-published-but-not-
            # completed (FINALIZE) coordination run. Return the ORIGINAL run_id /
            # manifest id / artifact paths and reconstruct the summary from the
            # persisted files. NOTHING is recomputed or rewritten; FINALIZE only
            # adds the completed transition after the business read-back checks.
            payload_path = parent_run_dir / PORTFOLIO_SUMMARY_FILE
            try:
                persisted_spec = json.loads(
                    (parent_run_dir / PORTFOLIO_SPEC_FILE).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise PortfolioServiceError(
                    P_PORTFOLIO_SPEC_HASH_MISMATCH,
                    f"replay: portfolio spec read-back failed: {exc}",
                ) from exc
            if (
                not isinstance(persisted_spec, dict)
                or persisted_spec.get("analysis_spec_hash") != spec_hash
            ):
                raise PortfolioServiceError(
                    P_PORTFOLIO_SPEC_HASH_MISMATCH,
                    "replay: persisted portfolio_analysis_spec.json does not match the "
                    "requested analysis spec",
                )
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PortfolioServiceError(
                    P_PORTFOLIO_SERVICE_BASE_FAILED,
                    f"replay: portfolio_summary.json read-back failed: {exc}",
                ) from exc
            summary = _portfolio_summary_from_payload(payload)
            replay_penetration: PenetrationSequence | None = None
            pen_path = parent_run_dir / PENETRATION_SEQUENCE_FILE
            if pen_path.is_file():
                pen_payload = json.loads(pen_path.read_text(encoding="utf-8"))
                replay_penetration = PenetrationSequence(
                    points=tuple(PenetrationPoint(**point) for point in pen_payload["points"]),
                    scaling_description=pen_payload["scaling_description"],
                )
            replay_member_run_ids = {mc.member_id: mc.run_id for mc in summary.member_credits}
            replay_marginal_run_id = (
                summary.marginal_credits[0].run_id if summary.marginal_credits else None
            )
            if start.action == CoordinationAction.FINALIZE_EXISTING:
                # The artifacts + a valid matching manifest already exist; only
                # the completed transition is added (nothing is rewritten).
                complete_coordination_run(
                    run_service=run_service, run_id=parent_run_id, request_id=request_id
                )
            return PortfolioAnalysisResult(
                portfolio_run_id=parent_run_id,
                member_run_ids=replay_member_run_ids,
                portfolio_efc_run_id=summary.portfolio_run_id,
                marginal_run_id=replay_marginal_run_id,
                summary=summary,
                penetration=replay_penetration,
                spec_hash=spec_hash,
                manifest_path=start.manifest_path or (parent_run_dir / "artifact_manifest.json"),
            )

        member_run_ids: dict[str, str] = {}
        member_credits: list[MemberCredit] = []
        try:
            for member in members:
                child = _efc_child_config(
                    base_app_config,
                    list(member.candidate_ids),
                    capacity_value_type="average",
                    baseline=None,
                    parent_run_id=parent_run_id,
                    method_profile=method_profile,
                )
                projection = run_efc_pipeline(
                    project_dir=project,
                    import_batch_id=import_batch_id,
                    data_version_id=data_version_id,
                    request_id=f"{request_id}-{spec_hash}-member-{member.member_id}",
                    created_by=created_by,
                    software_version=software_version,
                    software_commit=software_commit,
                    app_config=child,
                )
                run_id = str(projection["run_id"])
                member_run_ids[member.member_id] = run_id
                member_credits.append(
                    member_credit_from_result(
                        member.member_id, _result_from_run(project, run_id), member.nominal_capacity_mw
                    )
                )

            portfolio_config = _efc_child_config(
                base_app_config,
                portfolio_candidate_ids,
                capacity_value_type="portfolio",
                baseline=None,
                parent_run_id=parent_run_id,
                method_profile=method_profile,
            )
            portfolio_projection = run_efc_pipeline(
                project_dir=project,
                import_batch_id=import_batch_id,
                data_version_id=data_version_id,
                request_id=f"{request_id}-{spec_hash}-portfolio",
                created_by=created_by,
                software_version=software_version,
                software_commit=software_commit,
                app_config=portfolio_config,
            )
            portfolio_run_id = str(portfolio_projection["run_id"])
            portfolio_result = _result_from_run(project, portfolio_run_id)

            marginal_run_id: str | None = None
            marginal_credits: list[MarginalCredit] = []
            if marginal is not None:
                marginal_config = _efc_child_config(
                    base_app_config,
                    list(marginal.candidate_ids),
                    capacity_value_type="marginal",
                    baseline=marginal.baseline_portfolio_version,
                    parent_run_id=parent_run_id,
                    method_profile=method_profile,
                )
                marginal_projection = run_efc_pipeline(
                    project_dir=project,
                    import_batch_id=import_batch_id,
                    data_version_id=data_version_id,
                    request_id=f"{request_id}-{spec_hash}-marginal",
                    created_by=created_by,
                    software_version=software_version,
                    software_commit=software_commit,
                    app_config=marginal_config,
                )
                marginal_run_id = str(marginal_projection["run_id"])
                marginal_credits.append(
                    marginal_credit_from_result(
                        marginal.member_id, _result_from_run(project, marginal_run_id)
                    )
                )

            allocation: GovernanceAllocation | None = None
            if governance_rule_id is not None:
                allocation = build_governance_allocation(governance_rule_id, governance_allocations)

            summary = combine_portfolio(
                portfolio_result=portfolio_result,
                member_credits=member_credits,
                nominal_capacity_mw=nominal_capacity_mw,
                marginal_credits=marginal_credits,
                allocation=allocation,
            )

            penetration: PenetrationSequence | None = None
            if penetration_points:
                sequence_points: list[PenetrationPoint] = []
                for point_spec in penetration_points:
                    point_projection = run_efc_pipeline(
                        project_dir=project,
                        import_batch_id=import_batch_id,
                        data_version_id=data_version_id,
                        request_id=f"{request_id}-{spec_hash}-pen-{point_spec.point_id}",
                        created_by=created_by,
                        software_version=software_version,
                        software_commit=software_commit,
                        app_config=_efc_child_config(
                            base_app_config,
                            list(point_spec.candidate_ids),
                            capacity_value_type=(
                                "portfolio" if len(point_spec.candidate_ids) > 1 else "average"
                            ),
                            baseline=None,
                            parent_run_id=parent_run_id,
                            method_profile=method_profile,
                        ),
                    )
                    point_run_id = str(point_projection["run_id"])
                    point_result = _result_from_run(project, point_run_id)
                    sequence_points.append(
                        PenetrationPoint(
                            point_id=point_spec.point_id,
                            nominal_capacity_mw=point_spec.nominal_capacity_mw,
                            run_id=point_run_id,
                            total_credit_mw=point_result.accredited_capacity_mw,
                            result_status=point_result.result_status.value,
                        )
                    )
                penetration = penetration_sequence(
                    sequence_points, scaling_description="declared nominal capacity per point (P4)"
                )
        except BaseException as exc:  # noqa: BLE001 - fail the coordination run
            fail_coordination_run(
                run_service=run_service,
                run_id=parent_run_id,
                request_id=request_id,
                error_code=P_PORTFOLIO_SERVICE_BASE_FAILED,
                message=f"portfolio computation failed: {exc}",
            )

        # Persist coordination artifacts + the spec, register in the manifest.
        summary_payload = {
            "portfolio_run_id": summary.portfolio_run_id,
            "portfolio_member_ids": list(summary.portfolio_member_ids),
            "portfolio_capacity_mw": summary.portfolio_capacity_mw,
            "portfolio_credit_fraction": summary.portfolio_credit_fraction,
            "portfolio_nominal_capacity_mw": summary.portfolio_nominal_capacity_mw,
            "sum_of_individuals_mw": summary.sum_of_individuals_mw,
            "arithmetic_difference_mw": summary.arithmetic_difference_mw,
            "diagnostic_complete": summary.diagnostic_complete,
            "member_credits": [
                {
                    "member_id": mc.member_id,
                    "run_id": mc.run_id,
                    "capacity_credit_mw": mc.capacity_credit_mw,
                    "capacity_credit_fraction": mc.capacity_credit_fraction,
                    "nominal_capacity_mw": mc.nominal_capacity_mw,
                    "result_status": mc.result_status,
                    "risk_convergence": mc.risk_convergence,
                }
                for mc in summary.member_credits
            ],
            "marginal_credits": [
                {
                    "member_id": m.member_id,
                    "run_id": m.run_id,
                    "baseline_portfolio_version": m.baseline_portfolio_version,
                    "incremental_capacity_mw": m.incremental_capacity_mw,
                    "marginal_credit_mw": m.marginal_credit_mw,
                    "marginal_credit_fraction": m.marginal_credit_fraction,
                    "result_status": m.result_status,
                }
                for m in summary.marginal_credits
            ],
            "allocation": (
                {
                    "allocation_rule_id": summary.allocation.allocation_rule_id,
                    "member_allocations": list(summary.allocation.member_allocations),
                }
                if summary.allocation is not None
                else None
            ),
            "capacity_credit_type": summary.capacity_credit_type,
        }
        manifest_files = [PORTFOLIO_SUMMARY_FILE, PORTFOLIO_SPEC_FILE]
        try:
            atomic_write_object(
                parent_run_dir / PORTFOLIO_SUMMARY_FILE, summary_payload, immutable=True
            )
            atomic_write_object(
                parent_run_dir / PORTFOLIO_SPEC_FILE,
                {"analysis_spec_hash": spec_hash, "analysis_spec": _spec_to_dict(spec)},
                immutable=True,
            )
            if penetration is not None:
                atomic_write_object(
                    parent_run_dir / PENETRATION_SEQUENCE_FILE, penetration.to_dict(), immutable=True
                )
                manifest_files.append(PENETRATION_SEQUENCE_FILE)
            if allocation is not None:
                atomic_write_object(
                    parent_run_dir / GOVERNANCE_ALLOCATION_FILE,
                    allocation.__dict__,
                    immutable=True,
                )
                manifest_files.append(GOVERNANCE_ALLOCATION_FILE)
            entries = [
                analysis_summary_entry(
                    parent_run_dir, name, "capacity_credit.application.portfolio_service"
                )
                for name in manifest_files
            ]
            manifest_id = str(uuid.uuid4())
            manifest_path = write_coordination_manifest11(
                run_dir=parent_run_dir,
                run_id=parent_run_id,
                run_type="portfolio",
                manifest_id=manifest_id,
                created_by=created_by,
                entries=entries,
            )
        except BaseException as exc:  # noqa: BLE001 - no completed on failed publish
            fail_coordination_run(
                run_service=run_service,
                run_id=parent_run_id,
                request_id=request_id,
                error_code=P_PORTFOLIO_SERVICE_BASE_FAILED,
                message=f"portfolio artifact publish failed: {exc}",
            )

        # Read-back loop: recompute the spec hash from the persisted spec and
        # cross-check it; only a fully published + verified coordination run
        # may complete.
        try:
            persisted_doc = json.loads(
                (parent_run_dir / PORTFOLIO_SPEC_FILE).read_text(encoding="utf-8")
            )
            if (
                not isinstance(persisted_doc, dict)
                or "analysis_spec_hash" not in persisted_doc
                or "analysis_spec" not in persisted_doc
            ):
                raise PortfolioServiceError(
                    P_PORTFOLIO_SPEC_HASH_MISMATCH,
                    "portfolio_analysis_spec.json must be a {hash, spec} wrapper",
                )
            recomputed = hashlib.sha256(
                json.dumps(
                    persisted_doc["analysis_spec"],
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if recomputed != spec_hash or persisted_doc["analysis_spec_hash"] != spec_hash:
                raise PortfolioServiceError(
                    P_PORTFOLIO_SPEC_HASH_MISMATCH,
                    "persisted portfolio_analysis_spec.json does not match the requested analysis spec",
                )
            complete_coordination_run(
                run_service=run_service,
                run_id=parent_run_id,
                request_id=request_id,
            )
        except BaseException as exc:  # noqa: BLE001 - no completed on failed verification
            fail_coordination_run(
                run_service=run_service,
                run_id=parent_run_id,
                request_id=request_id,
                error_code=P_PORTFOLIO_SERVICE_BASE_FAILED,
                message=f"portfolio verification failed: {exc}",
            )

        return PortfolioAnalysisResult(
            portfolio_run_id=parent_run_id,
            member_run_ids=member_run_ids,
            portfolio_efc_run_id=portfolio_run_id,
            marginal_run_id=marginal_run_id,
            summary=summary,
            penetration=penetration,
            spec_hash=spec_hash,
            manifest_path=manifest_path,
        )
    finally:
        start.claim.release()



__all__ = [
    "GOVERNANCE_ALLOCATION_FILE",
    "PENETRATION_SEQUENCE_FILE",
    "PORTFOLIO_SPEC_FILE",
    "PORTFOLIO_SUMMARY_FILE",
    "P_PORTFOLIO_SERVICE_BASE_FAILED",
    "P_PORTFOLIO_SERVICE_INVALID",
    "P_PORTFOLIO_SPEC_HASH_MISMATCH",
    "MarginalSpec",
    "PenetrationSpec",
    "PortfolioAnalysisResult",
    "PortfolioAnalysisSpec",
    "PortfolioMemberSpec",
    "PortfolioServiceError",
    "analysis_spec_hash",
    "run_portfolio_analysis",
]
