"""V1-T1-A/T1-B public workbench facade (the single application entry).

T1-A: the SINGLE service entry for the local workbench pages and the
CLI - it validates the explicit workspace root once per construction,
keeps the normalized root as a read-only reference, and delegates every
project query to the FORMAL p1_workflow.list_projects (never a second
directory scanner, never recomputed business state).

T1-B: calculation admission flows through start_calculation into the
workspace-SHARED LocalRuntime (one active task per workspace, per
process), and get_execution_snapshot returns independent read-only
projections. Validation always precedes admission.

Hard boundaries (V1-T1-A):

- constructing the facade never creates threads, lock files, business
  directories or run objects, and never writes anything;
- invalid roots are entry errors (invalid_input / unavailable), NEVER
  an empty project list;
- a project reference is a single path component re-validated on every
  query BEFORE any content is read: absolute paths, traversal,
  separators, drive colons and NTFS stream syntax are rejected, and a
  junction/symlink entry (a Windows reparse point, not just
  is_symlink) is never followed;
- None means ONLY "a legal reference that currently does not exist"; a
  corrupt project keeps its invalid summary; illegal input raises;
- the light-weight WorkbenchError carries a display kind
  (invalid_input / unavailable) and the preserved reason - it is not a
  persistent diagnostic-code family and never reuses WORKER/P1 codes.
"""

from __future__ import annotations

import json
import math
import os
import stat as _stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from capacity_credit.application import p1_workflow
from capacity_credit.application.atomic_io import (
    hash_payload,
    safe_filename_component,
)
from capacity_credit.application.import_service import (
    ImportService,
    ServiceError,
)
from capacity_credit.application.local_runtime import (
    ExecutionSnapshot,
    LocalRuntimeError,
    StartHandle,
    get_shared_runtime,
)
from capacity_credit.application.p1_workflow import ProjectSummary
from capacity_credit.application.result_service import ResultServiceError
from capacity_credit.application.run_service import RunService
from capacity_credit.scenarios.scenario_bank import open_scenario_bank

_WORKBENCH_INPUT = "invalid_input"
_WORKBENCH_UNAVAILABLE = "unavailable"

# the run modes the wired adequacy chain accepts from the workbench
_RUN_MODES = ("formal", "teaching")


def _resolve_software_commit() -> str:
    """The REAL source-tree git commit for run manifests (V1-T2-C identity
    wiring, replacing the 'workbench-local' placeholder): resolves the
    package location to its checkout/worktree root and asks git for HEAD.
    A tree without usable git metadata returns an HONEST marker that
    never pretends to be a commit SHA."""
    import subprocess

    try:
        # <root>/src/capacity_credit/application/this.py -> the checkout
        # (or linked-worktree) root that owns the .git metadata
        package_root = Path(__file__).resolve().parents[3]
    except (OSError, ValueError):
        return "unresolved-source-identity"
    if not (package_root / ".git").exists():
        return "source-tree-without-git"
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(package_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (
        subprocess.SubprocessError,
        OSError,
        ValueError,
    ):  # identity stays honest on any resolver failure
        return "unresolved-source-identity"
    commit = completed.stdout.strip()
    return commit or "unresolved-source-identity"

# the double-separator prefixes that mark a UNC/network drive (built
# from os.sep so the source carries no path-separator literals)
_UNC_MARKERS = (os.sep * 2, (os.altsep or os.sep) * 2)


class WorkbenchError(ValueError):
    """A light-weight workbench entry error (display kind + reason).

    kind is a display category, not a persistent diagnostic code:

    - invalid_input: the workspace path or project reference is
      illegal (missing, not a directory, unsupported network drive, or
      an illegal reference component);
    - unavailable: the workspace exists with a legal shape but cannot
      actually be read/enumerated (e.g. access denied).

    The original reason is always preserved in message; nothing is
    swallowed and success is never faked.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _reject_unc_root(candidate: Path) -> None:
    """UNC / network-drive roots are outside this package's supported
    environment: rejected BEFORE any filesystem access, with an honest
    unsupported-environment message (never a missing-directory
    message)."""
    drive = candidate.drive or ""
    if drive[:2] in _UNC_MARKERS:
        raise WorkbenchError(
            _WORKBENCH_INPUT,
            f"不支持UNC/网络驱动器作为本包工作区（网络盘不在T1-A已验证支持范围）：{candidate}",
        )


def _validate_root(workspace_root: Path | str) -> Path:
    """THE single workspace-root validation (shared by the CLI, the
    direct page entry and every page query): normalize once, require an
    existing local directory that is ACTUALLY readable/enumerable, and
    classify failures honestly. Nothing is created, no write probe is
    used, and readable is never claimed to be writable."""
    raw = Path(workspace_root)
    # an empty/blank path would silently mean the cwd - never an
    # explicit workspace. The raw INPUT TEXT is examined, because
    # Path('') normalizes to '.' and Path construction must not precede
    # this guard (a blank string would otherwise resolve the current
    # directory; independent-review counterexample).
    raw_text = (
        workspace_root if isinstance(workspace_root, str) else os.fspath(workspace_root)
    )
    if not str(raw_text).strip():
        raise WorkbenchError(
            _WORKBENCH_INPUT,
            "工作区必须显式提供：空路径不会回退到当前目录",
        )
    _reject_unc_root(raw)
    try:
        resolved = raw.resolve()  # link/reparse roots normalize to the real root
    except OSError as exc:
        raise WorkbenchError(
            _WORKBENCH_UNAVAILABLE, f"工作区路径无法解析：{exc}"
        ) from None
    try:
        st = os.stat(resolved)
    except FileNotFoundError:
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"工作区不存在：{resolved}"
        ) from None
    except NotADirectoryError:
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"工作区路径不是目录（路径中包含文件）：{resolved}"
        ) from None
    except PermissionError as exc:
        raise WorkbenchError(
            _WORKBENCH_UNAVAILABLE, f"工作区拒绝访问：{resolved}（{exc}）"
        ) from None
    except OSError as exc:
        raise WorkbenchError(
            _WORKBENCH_UNAVAILABLE, f"工作区无法读取：{resolved}（{exc}）"
        ) from None
    if not _stat.S_ISDIR(st.st_mode):
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"工作区不是目录：{resolved}"
        )
    try:
        with os.scandir(resolved) as entries:
            for _ in entries:
                break  # an empty directory is legal; one successful
                # enumeration proves actual readability
    except PermissionError as exc:
        raise WorkbenchError(
            _WORKBENCH_UNAVAILABLE, f"工作区拒绝访问：{resolved}（{exc}）"
        ) from None
    except OSError as exc:
        raise WorkbenchError(
            _WORKBENCH_UNAVAILABLE, f"工作区无法读取：{resolved}（{exc}）"
        ) from None
    return resolved


def _validate_project_ref(project_ref: str) -> str:
    """THE single project-reference validation: reuse the established
    safe-filename-component rule so a reference is exactly ONE legal
    path component (absolute paths, traversal, separators, drive
    colons, NTFS stream syntax and NUL are all rejected)."""
    if not isinstance(project_ref, str):
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"非法项目引用（必须为字符串）：{project_ref!r}"
        )
    try:
        safe_filename_component(project_ref)
    except ValueError as exc:
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"非法项目引用：{exc}"
        ) from None
    return project_ref


class WorkbenchService:
    """The read-only public facade for one explicit workspace."""

    def __init__(self, workspace_root: Path | str) -> None:
        """Validate the explicit root and establish the read-only
        reference. No thread, lock file, business directory or run
        object is ever created here."""
        self._root = _validate_root(workspace_root)

    @property
    def workspace_root(self) -> Path:
        """The validated, normalized workspace root (display only; the
        facade never persists user configuration)."""
        return self._root

    def list_projects(self) -> list[ProjectSummary]:
        """The formal project list via p1_workflow.list_projects,
        returned as INDEPENDENT ProjectSummary copies (the original
        data-grade / freeze / batch / invalid semantics are preserved
        verbatim; the facade never recomputes business state).

        The workspace root is RE-VALIDATED on EVERY query at this
        boundary: a facade constructed earlier must never answer a
        vanished/unreadable root with an empty list (the formal query's
        missing-root contract is its own; the entry-level honesty is
        the facade's)."""
        root = _validate_root(self._root)
        try:
            summaries = p1_workflow.list_projects(root)
        except p1_workflow.P1WorkflowError as exc:
            # a corrupt project registry is an honest entry error at the
            # facade boundary (pages project WorkbenchError uniformly)
            raise WorkbenchError(
                _WORKBENCH_UNAVAILABLE, str(exc)
            ) from None
        copies: list[ProjectSummary] = []
        for summary in summaries:
            # an explicit key-by-key fresh dict: every field of the
            # formal summary is copied verbatim, never shared or
            # recomputed
            copies.append(
                ProjectSummary(
                    project_dir=summary["project_dir"],
                    name=summary["name"],
                    data_grade=summary["data_grade"],
                    frozen=summary["frozen"],
                    latest_batch_state=summary["latest_batch_state"],
                    created_at=summary.get("created_at"),
                    updated_at=summary["updated_at"],
                    invalid=summary["invalid"],
                    invalid_reason=summary["invalid_reason"],
                )
            )
        return copies

    def get_project_summary(self, project_ref: str) -> ProjectSummary | None:
        """The formal summary for one single-component project
        reference, re-validated on EVERY call:

        - an illegal reference raises invalid_input BEFORE any content
          is read;
        - the workspace root is re-validated first (a vanished root is
          an entry error, never None and never an empty list);
        - a legal reference that currently does not exist (missing, a
          plain file, or a reparse-point entry) is None - metadata-only
          checks, no external content is ever read for it;
        - a corrupt entry keeps its invalid summary (never None);
        - the result is an independent copy from the CURRENT formal
          list (ownership is proven by this very enumeration)."""
        _validate_project_ref(project_ref)
        root = _validate_root(self._root)
        entry = root / project_ref
        try:
            os.lstat(entry)  # metadata only: never follows a link
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkbenchError(
                _WORKBENCH_UNAVAILABLE, f"项目条目不可读取：{entry}（{exc}）"
            ) from None
        if p1_workflow.is_reparse_point(entry):
            return None  # a junction/symlink entry is not an owned project
        if not entry.is_dir():
            return None  # a plain file is not a project
        for summary in self.list_projects():
            if summary["project_dir"] == project_ref:
                return summary
        return None

    def start_calculation(
        self,
        intent_id: str,
        project_ref: str,
        data_ref: str,
        method: str,
        config: Mapping[str, Any],
        *,
        runner: Callable[[Any, str], Any] | None = None,
        intent_is_new: bool = False,
        research_point: bool = False,
    ) -> StartHandle:
        """THE single calculation entry (V1-T1-B/T1-C): validate first,
        then admit through the workspace-shared LocalRuntime, and return
        immediately (the handle carries the request ref and whatever
        run ref exists - None until the formal workflow creates one).

        - workspace re-validated at this boundary (a vanished root is
          an entry error, never a silent admission);
        - project_ref re-validated as ONE legal component AND an owned,
          non-corrupt project;
        - config must be a Mapping (the identity comes from the
          established canonical hash rule - hash_payload);
        - ``method="adequacy"`` is WIRED: without runner injection the
          BUILT-IN production runner dispatches the real
          run_adequacy_pipeline (formal RunService identity, frozen
          gates, workspace execution lock, artifact publication). Any
          other method is honestly method_unavailable; caller-injected
          runners remain an INTERNAL test seam only - the production
          page never passes one;
        - data capability gates run BEFORE admission: the data ref must
          be an approved+frozen batch of this project; storage-containing
          data dispatches the RESOURCE adequacy chain (run-config 1.2,
          V1-T3-C) - the plain 1.0/1.1 chain never silently absorbs a
          storage fleet;
        - intent_is_new is the CALLER's explicit declaration that this
          intent id is genuinely new (the UI 'run again' action). The
          default False means: an intent the runtime can no longer
          confirm (history evicted) is refused as intent_unconfirmed -
          the public entry can NEVER silently re-execute a forgotten
          intent. THE contract T1-C must keep: the calculation page
          passes intent_is_new=True only on a user-initiated fresh
          submission, never on retries/re-submissions of an existing
          intent (the same intent replays the same formal run)."""
        root = _validate_root(self._root)
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "intent_id 必须为非空字符串")
        if not isinstance(data_ref, str) or not data_ref.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "data_ref 必须为非空字符串")
        if not isinstance(method, str) or not method.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "method 必须为非空字符串")
        if not isinstance(config, Mapping):
            raise WorkbenchError(
                _WORKBENCH_INPUT, "config 必须为既有配置对象（Mapping），不接受任意标量"
            )
        summary = self.get_project_summary(project_ref)
        if summary is None:
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"项目不存在或不是本工作区的正式项目：{project_ref}"
            )
        if summary["invalid"]:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"项目条目损坏，不能进入计算：{project_ref}（{summary['invalid_reason']}）",
            )
        # P-A guards: identity-level, so they apply to injected test
        # runners too - a completed project never runs a second business
        # measurement, and the type lock holds at the facade.
        # P0 (2026-09-20 user ruling): the type lock protects the FORMAL
        # result identity, not research probes - a calibration batch
        # point run (an adequacy research sub-run that can never bind as
        # the formal result: formal-mode binding requires a passed
        # seed-replication gate such a run never carries) passes with
        # the caller's explicit research_point declaration. The
        # completion guard is NEVER exempted; the declaration is
        # opt-in at the call site and never inferred.
        self._guard_not_completed(project_ref, "发起测算")
        if not research_point:
            self._guard_measurement_type_lock(project_ref, method)
        data_version_id: str | None = None
        if runner is None:
            # the REAL dispatch path: the built-in runner exists only
            # for the wired method (parameter error first), then the
            # data capability gates run BEFORE admission with zero side
            # effects - the data ref must be an approved+frozen batch of
            # this project. Test-injected runners bypass the real
            # pipeline and its data gates (internal seam only).
            if method not in ("adequacy", "elcc", "efc", "demand_target_capacity"):
                raise LocalRuntimeError(
                    "method_unavailable",
                    f"计算方法 {method!r} 尚未接入；本包仅开放充裕度/ELCC/EFC/目标可靠容量",
                )
            if method == "demand_target_capacity":
                # Step 2 Stage A（需求类设计 §3.1）：本项目无自有数据——
                # data_ref 指向 SOURCE 充裕度项目的冻结批次，来源引用在
                # config.source；门＝来源项目存在＋类型 adequacy（无类型
                # 旧项目经下方运行发布核验放行）＋既有冻结门对来源生效＋
                # 来源运行已发布（result_link 闭环，与投影同一读法）
                source_cfg = (config or {}).get("source") or {}
                source_ref = str(source_cfg.get("project_ref") or "")
                source_run = str(source_cfg.get("run_ref") or "")
                if not source_ref or not source_run:
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        "config.source 需携带来源充裕度项目的 project_ref 与 run_ref"
                        "（目标可靠容量测算站在已完成的充裕度评估之上）",
                    )
                if self.get_project_summary(source_ref) is None:
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        f"来源项目不存在：{source_ref}",
                    )
                try:
                    source_mt = self.get_project_measurement_type(source_ref)
                except WorkbenchError:
                    # 无注册表项的旧项目＝无类型锁；下方运行发布核验兜底
                    source_mt = None
                if source_mt is not None and source_mt != "adequacy":
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        f"来源项目类型必须是充裕度评估（adequacy），当前为 {source_mt}",
                    )
                data_version_id, _storage_detected = self._resolve_frozen_data(
                    root, source_ref, data_ref
                )
                # 资源链真实消费储能（与 EFC 同链），无需 ELCC 式拒绝；
                # 置 False 仅为让下方 elcc/efc 分派链对该方法安全跳过
                storage_detected = False
                source_run_dir = (
                    root / source_ref / "runs" / safe_filename_component(source_run)
                )
                source_link = source_run_dir / "result_link.json"
                if not source_run_dir.is_dir() or not source_link.is_file():
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        f"来源运行不存在：{source_ref} / {source_run}",
                    )
                from capacity_credit.application.result_service import (
                    read_result_link as _read_src_link,
                )

                try:
                    src_link = _read_src_link(source_link)
                except Exception as exc:  # noqa: BLE001 - honest gate
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        f"来源运行结果链不可读：{exc}",
                    ) from None
                if src_link.result_id is None:
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        f"来源运行尚未发布正式结果：{source_run}",
                    )
                runner = self._make_target_capacity_runner(
                    root=root,
                    project_ref=project_ref,
                    source_ref=source_ref,
                    import_batch_id=data_ref,
                    data_version_id=data_version_id,
                    intent_id=intent_id,
                    config=dict(config),
                )
            else:
                data_version_id, storage_detected = self._resolve_frozen_data(
                    root, project_ref, data_ref
                )
            if method in ("elcc", "efc"):
                if method == "elcc" and storage_detected:
                    # night-3 G3: the ELCC evaluation engine is the plain
                    # adequacy chain, which does NOT consume a base
                    # storage fleet (probed 2026-09-09: storage and
                    # no-storage projects give bit-identical base risks).
                    # Refuse BEFORE admission instead of silently
                    # ignoring the fleet; storage data stays runnable
                    # through adequacy (resource chain) and EFC (resource
                    # chain, which really dispatches the storage).
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        "冻结数据包含储能，而本版ELCC评估链（普通充裕度引擎）不消费基准储能；"
                        "为避免静默忽略储能，已拒绝本次ELCC提交且未创建任何运行。"
                        "含储能基准请选择充裕度或EFC（均经资源链真实消费储能）。",
                    )
                # V1-T3-D/E: the typed method-config payload IS the
                # config; the workflow validates and freezes it (its own
                # run_config carries run_mode) before create_run
                runner = self._make_method_workflow_runner(
                    method=method,
                    root=root,
                    project_ref=project_ref,
                    import_batch_id=data_ref,
                    data_version_id=data_version_id,
                    intent_id=intent_id,
                    config=dict(config),
                )
            elif method == "demand_target_capacity":
                pass  # runner 已在上方来源门分支构建（勿落充裕度链）
            elif storage_detected:
                # V1-T3-C: storage-containing frozen data dispatches the
                # RESOURCE adequacy chain (run-config 1.2) - the storage
                # fleet never silently degrades to the plain chain
                runner = self._make_resource_adequacy_runner(
                    root=root,
                    runtime=get_shared_runtime(root),
                    project_ref=project_ref,
                    import_batch_id=data_ref,
                    data_version_id=data_version_id,
                    intent_id=intent_id,
                    config=dict(config),
                )
            else:
                runner = self._make_adequacy_runner(
                    root=root,
                    runtime=get_shared_runtime(root),
                    project_ref=project_ref,
                    import_batch_id=data_ref,
                    data_version_id=data_version_id,
                    intent_id=intent_id,
                    config=dict(config),
                )
        try:
            params_hash = hash_payload(
                {
                    "project_ref": project_ref,
                    "data_ref": data_ref,
                    "method": method,
                    "config": dict(config),
                }
            )
        except ValueError as exc:
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"config 含不可进入身份哈希的内容：{exc}"
            ) from None
        runtime = get_shared_runtime(root)
        request_ref = runtime.start(
            intent_id=intent_id,
            params={
                "project_ref": project_ref,
                "data_ref": data_ref,
                "method": method,
                "config": dict(config),
            },
            params_hash=params_hash,
            runner=runner,
            allow_unknown=intent_is_new,
        )
        return StartHandle(request_ref=request_ref, run_ref=None)

    def _make_resource_adequacy_runner(
        self,
        *,
        root: Path,
        runtime: Any,
        project_ref: str,
        import_batch_id: str,
        data_version_id: str,
        intent_id: str,
        config: dict[str, Any],
    ) -> Callable[[Any, str], Any]:
        """THE storage-chain runner (V1-T3-C): builds the run-config 1.2
        from the SAME user-visible selections plus the bank facts
        (samples = the bank's sample count; the convergence section
        carries the SHARED engine defaults, recorded in the hashed
        config), then dispatches the formal resource adequacy run. The
        plain-chain 1.0 build is untouched."""
        scenario_bank_id = config.get("scenario_bank_id")
        root_seed = config.get("root_seed")
        primary_risk_metric = config.get("primary_risk_metric")
        weight_set_id = config.get("weight_set_id")
        scenario_id = config.get("scenario_id")
        run_mode = config.get("run_mode", "formal")
        if run_mode not in _RUN_MODES:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"config.run_mode 必须为 {'/'.join(_RUN_MODES)}，收到 {run_mode!r}",
            )
        if not isinstance(scenario_bank_id, str) or not scenario_bank_id.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "config.scenario_bank_id 必须为非空字符串")
        if (
            not isinstance(root_seed, int)
            or isinstance(root_seed, (bool, float))
        ):
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                "config.root_seed 必须为整数（来自所选场景银行，页面只读回显）",
            )
        if not isinstance(primary_risk_metric, str) or not primary_risk_metric.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "config.primary_risk_metric 必须为非空字符串")
        if not isinstance(weight_set_id, str) or not weight_set_id.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "config.weight_set_id 必须为非空字符串")

        from capacity_credit.engine.convergence import default_config

        shared = default_config()
        convergence_section = {
            "batch_size": shared.batch_size,
            "min_samples": shared.min_samples,
            "max_samples": shared.max_samples,
            "confidence_level": shared.confidence_level,
            "relative_half_width_tolerance": shared.relative_half_width_tolerance,
            "absolute_half_width_tolerance": shared.absolute_half_width_tolerance,
            "near_zero_threshold": shared.near_zero_threshold,
            "min_nonzero_samples": shared.min_nonzero_samples,
            "recent_batch_window": shared.recent_batch_window,
            "recent_batch_stability_tolerance": shared.recent_batch_stability_tolerance,
            "use_student_t": shared.use_student_t,
            "normal_interval_min_samples": shared.normal_interval_min_samples,
            "primary_risk_metric": primary_risk_metric,
            "provisional_defaults": shared.provisional_defaults,
        }
        from capacity_credit.scenarios.scenario_bank import open_scenario_bank

        try:
            bank = open_scenario_bank(
                root / project_ref / "artifacts" / "scenario_banks" / scenario_bank_id
            )
            samples = int(bank.index["sample_count"])
        except Exception as exc:  # noqa: BLE001 - ScenarioBankError/OSError -> honest
            # entry refusal BEFORE admission (no admission, no side effects)
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"所选场景银行不可用：{exc}"
            ) from None

        from capacity_credit.contracts.runs import RunConfig12

        cfg12 = RunConfig12.model_validate(
            {
                "run_config_schema_version": "1.2",
                "run_mode": run_mode,
                "run_type": "adequacy",
                "method_profile": "adequacy_sequential",
                "primary_risk_metric": primary_risk_metric,
                "weather_year_weight_set_id": weight_set_id,
                "chronology_mode": "independent_weather_years",
                "storage_soc_initialization_mode": "weather_cycle_warmup",
                "soc_year_boundary_mode": "reinitialize_with_configured_mode",
                "capacity_value_type": "not_applicable",
                "allocation_type": "none",
                "random_seed": root_seed,
                "scenario_bank_id": scenario_bank_id,
                "scenario_id": scenario_id,
                "adequacy_config": {
                    "samples": samples,
                    "storage_soc": {"initialization_mode": "weather_cycle_warmup"},
                    "convergence": convergence_section,
                },
            }
        )

        from capacity_credit import __version__

        def resource_runner(observe: Any, request_ref: str) -> Any:
            return p1_workflow.run_resource_adequacy_formal(
                project_dir=root / project_ref,
                import_batch_id=import_batch_id,
                data_version_id=data_version_id,
                run_config=cfg12,
                request_id=intent_id,  # the intent IS the formal request
                created_by="local-workbench",
                software_version=str(__version__),
                software_commit=_resolve_software_commit(),
                observe=(
                    lambda completed, total: observe(
                        "resource_dispatch", "sample", completed, total
                    )
                ),
                attach_run=lambda run_id: runtime.attach_run_ref(
                    request_ref, str(run_id)
                ),
            )

        return resource_runner


    def _make_method_workflow_runner(
        self,
        *,
        method: str,
        root: Path,
        project_ref: str,
        import_batch_id: str,
        data_version_id: str,
        intent_id: str,
        config: dict[str, Any],
    ) -> Callable[[Any, str], Any]:
        """THE elcc/efc runner (V1-T3-D/E): the config IS the typed
        method application-config payload - validated and frozen by the
        ORIGINAL workflow BEFORE create_run (its own run_config carries
        the run mode, search/growth/candidate sections enter
        method_config_hash / application_config_hash). Admission, the
        workspace execution lock and the formal identity are identical
        to the adequacy chains. V1-T3-D seam 3: the formal run id
        attaches right after create_run (BEFORE the first long
        computation) and the REAL pipeline boundaries (base series,
        search trials) flow through the runtime observer - never a
        fabricated progress event, never a changed search."""
        from capacity_credit import __version__
        from capacity_credit.application.efc_workflow import run_efc_pipeline
        from capacity_credit.application.elcc_workflow import run_elcc_pipeline

        pipeline = run_elcc_pipeline if method == "elcc" else run_efc_pipeline

        def method_runner(observe: Any, request_ref: str) -> Any:
            projection = pipeline(
                project_dir=root / project_ref,
                import_batch_id=import_batch_id,
                data_version_id=data_version_id,
                request_id=intent_id,  # the intent IS the formal request
                created_by="local-workbench",
                software_version=str(__version__),
                software_commit=_resolve_software_commit(),
                app_config=config,
                observe=observe,
                attach_run=lambda run_id: get_shared_runtime(
                    root
                ).attach_run_ref(request_ref, str(run_id)),
            )
            return projection

        return method_runner

    def _make_target_capacity_runner(
        self,
        *,
        root: Path,
        project_ref: str,
        source_ref: str,
        import_batch_id: str,
        data_version_id: str,
        intent_id: str,
        config: dict[str, Any],
    ) -> Callable[[Any, str], Any]:
        """THE demand_target_capacity runner (Step 2 Stage A): runs are
        recorded under THIS project; the frozen tables / scenario bank /
        risk baseline come READ-ONLY from the SOURCE adequacy project
        (the source gates ran before admission). Admission, the workspace
        execution lock and the formal identity are identical to the
        other method chains."""
        from capacity_credit import __version__
        from capacity_credit.application.target_capacity_workflow import (
            run_target_capacity_pipeline,
        )

        def tcap_runner(observe: Any, request_ref: str) -> Any:
            return run_target_capacity_pipeline(
                project_dir=root / project_ref,
                source_project_dir=root / source_ref,
                source_import_batch_id=import_batch_id,
                source_data_version_id=data_version_id,
                request_id=intent_id,
                created_by="local-workbench",
                software_version=str(__version__),
                software_commit=_resolve_software_commit(),
                app_config=dict(config),
                observe=observe,
                attach_run=lambda run_id: get_shared_runtime(
                    root
                ).attach_run_ref(request_ref, str(run_id)),
            )

        return tcap_runner

    def get_target_capacity_sources(self) -> dict[str, Any]:
        """Step 2 Stage A：来源选择器数据（只读）——全部 reliability 项目里
        已完成且已发布的充裕度运行（类型 adequacy 或无类型旧项目，如实
        列出；不做任何计算）。"""
        root = _validate_root(self._root)
        del root  # 逐项目经运行事实判定（教学/旧项目可能无注册表类型项）
        sources: list[dict[str, Any]] = []
        for project in self.list_projects():
            ref = str(project["project_dir"])
            # 过滤纯以运行事实为准：method=adequacy（run_config 权威）＋
            # completed＋published——成本项目不可能有充裕度运行；elcc/efc
            # 项目被 method 过滤自然排除；无类型旧项目如实纳入
            try:
                runs = self.list_run_summaries(ref)
            except WorkbenchError:
                continue
            for run in runs or []:
                if str(run.get("method") or "") != "adequacy":
                    continue
                if run.get("lifecycle_status") != "completed" or not run.get(
                    "published"
                ):
                    continue
                # 发起目标容量测算需要 data_ref＝来源运行实际消费的冻结批次
                # （run manifest 权威记录；不可读时如实 None，UI 置灰该来源）
                import_batch_id: str | None = None
                scenario_bank_id: str | None = None
                random_seed: int | None = None
                try:
                    src_run_dir = (
                        self._root
                        / ref
                        / "runs"
                        / safe_filename_component(str(run.get("run_ref") or ""))
                    )
                    manifest_path = src_run_dir / "run_manifest.json"
                    if manifest_path.is_file():
                        import json as _json

                        import_batch_id = str(
                            _json.loads(manifest_path.read_text(encoding="utf-8")).get(
                                "import_batch_id"
                            )
                            or ""
                        ) or None
                    config_path = src_run_dir / "run_config.yaml"
                    if config_path.is_file():
                        import yaml as _yaml

                        src_cfg = _yaml.safe_load(
                            config_path.read_text(encoding="utf-8")
                        )
                        if isinstance(src_cfg, dict):
                            scenario_bank_id = (
                                str(src_cfg.get("scenario_bank_id") or "") or None
                            )
                            seed = src_cfg.get("random_seed")
                            random_seed = int(seed) if seed is not None else None
                except (OSError, ValueError):
                    import_batch_id = None
                sources.append(
                    {
                        "project_ref": ref,
                        "project_name": str(project.get("name") or ref),
                        "run_ref": str(run.get("run_ref") or ""),
                        "created_at": run.get("created_at"),
                        "import_batch_id": import_batch_id,
                        "scenario_bank_id": scenario_bank_id,
                        "random_seed": random_seed,
                    }
                )
        return {"sources": sources}

    @staticmethod
    def _storage_in_frozen_copy(import_service: Any, data_ref: str) -> bool:
        """Whether the FROZEN COPY of this batch contains storage units
        (the frozen copy is the pipeline's actual input and the only
        authoritative source after freeze - the pre-freeze worksheet is
        never consulted)."""
        files = import_service.frozen_files(data_ref)
        storage_path = files.get("data/storage_units.csv")
        if storage_path is None or not storage_path.is_file():
            return False
        import csv

        with storage_path.open(encoding="utf-8", newline="") as handle:
            return len(list(csv.reader(handle))) > 1

    def _resolve_frozen_data(
        self, root: Path, project_ref: str, data_ref: str
    ) -> tuple[str, bool]:
        """The data capability gate (BEFORE admission): the data ref
        (an import batch id) must belong to this project and be
        approved+FROZEN; returns the approved data version id and
        whether the frozen data contains storage resources (plain
        adequacy must refuse storage-containing data instead of
        silently ignoring the storage fleet)."""
        project_dir = root / project_ref
        import_service = ImportService(project_dir)
        try:
            state = import_service.current_state(data_ref)
        except Exception as exc:  # ServiceError and contract errors alike
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"数据引用无效或不属于本项目：{data_ref}（{exc}）",
            ) from None
        gates = (
            (state.validation_status == "passed", "校验未通过"),
            (state.import_review_status == "approved", "导入审批未通过"),
            (state.data_version_status == "frozen", "数据版本未冻结"),
        )
        failures = [reason for ok, reason in gates if not ok]
        if failures:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"数据 {data_ref} 不满足可运行门：{'；'.join(failures)}"
                "（须先经既有审批与冻结流程，本包不提供上传审批页面）",
            )
        # V1-T2-C (C05): the FROZEN content is integrity-verified at this
        # gate - a tampered/corrupt frozen set is refused BEFORE any
        # admission or computation side effect
        try:
            import_service.verify_frozen_integrity(data_ref)
        except ServiceError as exc:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"冻结数据完整性校验失败，已拒绝计算：{exc.message}（{exc.code}）",
            ) from None
        storage_detected = self._storage_in_frozen_copy(
            import_service, data_ref
        )
        return state.data_version_id, storage_detected

    def _make_adequacy_runner(
        self,
        *,
        root: Path,
        runtime: Any,
        project_ref: str,
        import_batch_id: str,
        data_version_id: str,
        intent_id: str,
        config: dict[str, Any],
    ) -> Callable[[Any, str], Any]:
        """THE built-in production runner for the wired adequacy method
        (internal to the facade - the production page never passes a
        runner): dispatches the REAL run_adequacy_pipeline with the
        formal RunService identity, forwards REAL stage/batch events
        into the runtime observer, and attaches the resolved formal
        run_id through the ownership-checked seam the moment it
        exists."""
        scenario_bank_id = config.get("scenario_bank_id")
        root_seed = config.get("root_seed")
        primary_risk_metric = config.get("primary_risk_metric")
        weight_set_id = config.get("weight_set_id")
        scenario_id = config.get("scenario_id")
        declared_convergence = config.get("convergence")
        if declared_convergence is not None and not isinstance(
            declared_convergence, Mapping
        ):
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                "config.convergence 必须为映射（声明的收敛档案）或省略",
            )
        if not isinstance(scenario_bank_id, str) or not scenario_bank_id.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "config.scenario_bank_id 必须为非空字符串")
        run_mode = config.get("run_mode", "formal")
        if run_mode not in _RUN_MODES:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"config.run_mode 必须为 {'/'.join(_RUN_MODES)}，收到 {run_mode!r}",
            )
        if (
            not isinstance(root_seed, int)
            or isinstance(root_seed, bool)
            or isinstance(root_seed, float)
        ):
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                "config.root_seed 必须为整数（来自所选场景银行，页面只读回显）",
            )
        if not isinstance(primary_risk_metric, str) or not primary_risk_metric.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "config.primary_risk_metric 必须为非空字符串")
        if not isinstance(weight_set_id, str) or not weight_set_id.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "config.weight_set_id 必须为非空字符串")
        if scenario_id is not None and (
            not isinstance(scenario_id, str) or not scenario_id.strip()
        ):
            raise WorkbenchError(_WORKBENCH_INPUT, "config.scenario_id 必须为非空字符串或省略")

        from capacity_credit import __version__

        def adequacy_runner(observe: Any, request_ref: str) -> Any:
            attached = False

            def progress(info: dict[str, Any]) -> None:
                nonlocal attached
                run_id = info.get("run_id")
                if run_id and not attached:
                    attached = True
                    runtime.attach_run_ref(request_ref, str(run_id))
                if "completed_batches" in info:
                    observe(
                        str(info.get("phase", "adequacy")),
                        str(info.get("current_subtask", "batch")),
                        info.get("completed_batches"),
                        info.get("total_batches_bound"),
                    )
                else:
                    observe(
                        str(info.get("phase", "adequacy")),
                        str(info.get("current_subtask", "")),
                        None,
                        None,
                    )

            return p1_workflow.run_adequacy_pipeline(
                project_dir=root / project_ref,
                import_batch_id=import_batch_id,
                data_version_id=data_version_id,
                scenario_bank_id=scenario_bank_id,
                request_id=intent_id,  # the intent IS the formal request
                created_by="local-workbench",
                software_version=str(__version__),
                software_commit=_resolve_software_commit(),
                root_seed=int(root_seed),
                primary_risk_metric=primary_risk_metric,
                weight_set_id=weight_set_id,
                scenario_id=scenario_id,
                run_mode=str(run_mode),
                declared_convergence=declared_convergence,
                progress=progress,
            )

        return adequacy_runner

    def get_run_options(self, project_ref: str) -> dict[str, Any]:
        """What THIS package can actually run for one project (the page
        renders ONLY these): the wired methods (adequacy / elcc / efc
        since V1-T3-E), the approved+frozen data refs, the scenario
        banks with their read-only seeds, the declared weight sets and
        scenarios, the frozen ELCC candidate groups with their constant
        installed capacity, the capability limits and the per-method
        storage notes. Purely read-only."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        project_dir = root / project_ref
        if not project_dir.is_dir():
            raise WorkbenchError(_WORKBENCH_INPUT, f"项目不存在：{project_ref}")
        import_service = ImportService(project_dir)
        data_refs: list[dict[str, Any]] = []
        imports_dir = project_dir / "imports"
        if imports_dir.is_dir():
            for entry in sorted(imports_dir.iterdir()):
                if entry.name.startswith(".") or entry.name.startswith("_"):
                    continue
                if p1_workflow.is_reparse_point(entry) or not entry.is_dir():
                    continue
                try:
                    state = import_service.current_state(entry.name)
                except Exception:  # noqa: BLE001 - a corrupt batch is skipped
                    continue
                data_refs.append(
                    {
                        "import_batch_id": entry.name,
                        "data_version_id": state.data_version_id,
                        "frozen": state.data_version_status == "frozen",
                        "approved": state.import_review_status == "approved",
                        "validation_passed": state.validation_status == "passed",
                    }
                )
        banks: list[dict[str, Any]] = []
        banks_dir = project_dir / "artifacts" / "scenario_banks"
        if banks_dir.is_dir():
            for entry in sorted(banks_dir.iterdir()):
                if entry.name.startswith(".") or p1_workflow.is_reparse_point(entry):
                    continue
                if not entry.is_dir():
                    continue
                try:
                    bank = open_scenario_bank(entry)
                    binding = bank.index.get("source_binding")
                    banks.append(
                        {
                            "scenario_bank_id": bank.bank_id,
                            "root_seed": int(bank.index["root_seed"]),
                            "sample_count": int(bank.index["sample_count"]),
                            # A2: the weather years the bank ACTUALLY
                            # simulates (read-only bank facts)
                            "weather_years": [
                                int(y)
                                for y in bank.index.get("weather_years")
                                or []
                            ],
                            # R01: WHICH frozen batch/version prepared this
                            # bank (post-hash index bookkeeping; None for a
                            # legacy bank - never inferred)
                            "source_binding": (
                                dict(binding) if isinstance(binding, dict) else None
                            ),
                        }
                    )
                except Exception:  # noqa: BLE001 - a corrupt bank is skipped
                    continue
        weight_set_ids: list[str] = []
        weight_sets: dict[str, dict[int, float]] = {}
        scenario_ids: list[str] = []
        storage_detected = False
        # V1-T3-D seam 1: the frozen ELCC candidate groups (id ->
        # per-year installed capacities read from the FROZEN copy; the
        # run-time resolution refuses non-constant capacities, so the
        # option carries the constant value only when it IS constant)
        elcc_candidate_caps: dict[str, dict[str, Any]] = {}
        frozen_refs = [d for d in data_refs if d["frozen"] and d["approved"]]
        # V1-T2-C: the weight/scenario sets come from the FROZEN COPY of
        # the runnable batches (the upload flow never writes project
        # data/ tables); a legacy project with on-disk data/ tables
        # keeps the old discovery source as a fallback.
        for d in frozen_refs:
            try:
                files = import_service.frozen_files(d["import_batch_id"])
            except Exception:  # noqa: BLE001, S112 - an unreadable frozen
                continue  # set is refused at the run gates anyway
            weights_table = files.get("data/weather_year_weight_sets.csv")
            if weights_table is not None and weights_table.is_file():
                import csv

                with weights_table.open(encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        wid = str(row.get("weight_set_id") or "").strip()
                        if wid and wid not in weight_set_ids:
                            weight_set_ids.append(wid)
                        # B3: keep the declared per-year weights of each
                        # set so the page can show the FINAL percentages
                        # (read-only facts from the same frozen copy)
                        year_raw = str(row.get("weather_year") or "").strip()
                        raw_weight = row.get("weight")
                        if wid and year_raw and raw_weight is not None:
                            try:
                                weight_sets.setdefault(wid, {})[int(year_raw)] = float(
                                    raw_weight
                                )
                            except (TypeError, ValueError):
                                continue  # refused at the run gates anyway
            scenarios_table = files.get("data/scenarios.csv")
            if scenarios_table is not None and scenarios_table.is_file():
                import csv

                with scenarios_table.open(encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        sid = str(row.get("scenario_id") or "").strip()
                        if sid and sid not in scenario_ids:
                            scenario_ids.append(sid)
            caps_table = files.get("data/renewable_capacities.csv")
            if caps_table is not None and caps_table.is_file():
                import csv

                with caps_table.open(encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        group = str(row.get("resource_group_id") or "").strip()
                        year = str(row.get("year") or "").strip()
                        raw_mw = row.get("installed_capacity_mw")
                        if not group or raw_mw is None or str(raw_mw).strip() == "":
                            continue
                        try:
                            mw = float(raw_mw)
                        except (TypeError, ValueError):
                            continue  # a corrupt row is refused at the run gates
                        entry = elcc_candidate_caps.setdefault(
                            group, {"values": set(), "years": set()}
                        )
                        entry["values"].add(mw)
                        if year:
                            entry["years"].add(year)
        if not weight_set_ids:
            weights_table = project_dir / "data" / "weather_year_weight_sets.csv"
            if weights_table.is_file():
                import csv

                with weights_table.open(encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        wid = str(row.get("weight_set_id") or "").strip()
                        if wid and wid not in weight_set_ids:
                            weight_set_ids.append(wid)
        if not scenario_ids:
            scenarios_table = project_dir / "data" / "scenarios.csv"
            if scenarios_table.is_file():
                import csv

                with scenarios_table.open(encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        sid = str(row.get("scenario_id") or "").strip()
                        if sid and sid not in scenario_ids:
                            scenario_ids.append(sid)
        for d in frozen_refs:
            try:
                if self._storage_in_frozen_copy(import_service, d["import_batch_id"]):
                    storage_detected = True
                    break
            except Exception:  # noqa: BLE001 - an unreadable frozen set is
                break  # treated as capability-unknown -> conservative refusal
        limits = {
            "method": "adequacy",
            # V1-T3-C: storage-containing data IS supported through the
            # resource adequacy chain (run-config 1.2)
            "storage_supported": True,
            "storage_detected": storage_detected,
            "reason": None,
        }
        # night-3 G3: per-method storage semantics - the page and the
        # user guide render THESE so a user can tell an honest refusal
        # from a silent ignore (never a blanket "not open" note)
        method_notes: dict[str, str] = {}
        if storage_detected:
            if len(frozen_refs) <= 1:
                method_notes["elcc"] = (
                    "本项目的冻结数据包含储能：ELCC不可用（本版ELCC评估链"
                    "不消费基准储能，提交将被拒绝而非静默忽略）"
                )
            else:
                method_notes["elcc"] = (
                    "本项目部分冻结数据包含储能：ELCC仅可对不含储能的批次"
                    "运行（对含储能批次提交将被拒绝而非静默忽略）"
                )
            method_notes["efc"] = (
                "含储能数据：EFC经资源链真实消费储能"
                "（配置未给storage_soc时按默认weather_cycle_warmup派储，进资源运行身份）"
            )
        runnable = bool(frozen_refs) and bool(banks)
        reasons: list[str] = []
        if not frozen_refs:
            reasons.append("缺少已审批且冻结的数据（须先经既有导入/审批/冻结流程）")
        if not banks:
            reasons.append("尚未生成模拟样本集：请在本页上方“模拟样本集”处先生成")
        elcc_candidates = [
            {
                "resource_group_id": group,
                "installed_capacity_mw": (
                    next(iter(info["values"]))
                    if len(info["values"]) == 1
                    else None
                ),
                "capacity_constant_across_years": len(info["values"]) == 1,
                "weather_years": sorted(info["years"]),
            }
            for group, info in sorted(elcc_candidate_caps.items())
        ]
        return {
            "project_ref": project_ref,
            # V1-T3-E: efc joins the wired set (its user flow is
            # implemented and tested; the EFC form exposes the original
            # EfcApplicationConfig fields only)
            "methods": ("adequacy", "elcc", "efc", "demand_target_capacity"),
            "method_notes": method_notes,
            "elcc_candidates": elcc_candidates,
            "elcc_growth_modes": ("fixed_mw", "scaling"),
            "efc_upper_bound_sources": (
                "nameplate_capacity",
                "declared_mw",
            ),
            "data_refs": data_refs,
            "scenario_banks": banks,
            "weight_set_ids": weight_set_ids,
            "weight_sets": weight_sets,
            "scenario_ids": scenario_ids,
            "capability_limits": limits,
            "runnable": runnable,
            "not_runnable_reasons": reasons,
        }

    def list_runs(
        self, project_ref: str, published_only: bool = False
    ) -> list[str]:
        """The EXISTING formal runs of one project (oldest first): the
        session-loss query path - after a browser refresh the page can
        still find and inspect prior formal runs without any persistent
        request database (the runs directory IS the record).
        ``published_only`` keeps only runs whose MethodResult was
        actually published."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        runs_dir = root / project_ref / "runs"
        if not runs_dir.is_dir():
            return []
        runs: list[str] = []
        for entry in sorted(runs_dir.iterdir(), key=lambda e: e.stat().st_mtime):
            if entry.name.startswith(".") or p1_workflow.is_reparse_point(entry):
                continue
            if not entry.is_dir() or not (entry / "run_manifest.json").is_file():
                continue
            if published_only and not (entry / "method_result.json").is_file():
                continue
            runs.append(entry.name)
        return runs

    def list_run_summaries(
        self, project_ref: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Lightweight READ-ONLY run rows for history / recent displays
        (R09): newest first, every run with a manifest - published,
        failed or interrupted alike - with its identity, kind, lifecycle
        and publication facts. Rows NEVER trigger a computation and
        never rewrite state; unreadable fields stay honest None (a
        failed run is listed as failed, never dropped and never
        laundered into success). Opening one run still goes through the
        fully evidence-checked get_run_projection."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        runs_dir = root / project_ref / "runs"
        if not runs_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for entry in sorted(
            runs_dir.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True
        ):
            if entry.name.startswith(".") or p1_workflow.is_reparse_point(entry):
                continue
            if not entry.is_dir() or not (entry / "run_manifest.json").is_file():
                continue
            lifecycle: str | None = None
            try:
                run_service = RunService(
                    root / project_ref, ImportService(root / project_ref)
                )
                lifecycle = str(
                    run_service.run_state(entry.name)["lifecycle_status"]
                )
            except Exception:  # noqa: BLE001 - honest unknown lifecycle
                lifecycle = None
            method: str | None = None
            config_path = entry / "run_config.yaml"
            if config_path.is_file():
                try:
                    import yaml

                    config = yaml.safe_load(
                        config_path.read_text(encoding="utf-8")
                    )
                    if isinstance(config, dict):
                        raw_type = config.get("run_type")
                        method = str(raw_type) if raw_type else None
                except Exception:  # noqa: BLE001 - honest unknown kind
                    method = None
            created_at: str | None = None
            try:
                created_at = datetime.fromtimestamp(
                    entry.stat().st_mtime, tz=UTC
                ).isoformat()
            except (OSError, ValueError):
                created_at = None
            rows.append(
                {
                    "run_ref": entry.name,
                    "method": method,
                    "lifecycle_status": lifecycle,
                    "published": (entry / "method_result.json").is_file(),
                    "created_at": created_at,
                }
            )
            if limit is not None and len(rows) >= limit:
                break
        return rows

    def get_run_projection(self, project_ref: str, run_ref: str) -> dict[str, Any]:
        """The formal run projection read back from the FORMAL artifacts
        (RunService lifecycle state + MethodResult + artifact manifest):
        the page renders THIS, never preset text and never a re-read of
        raw business JSON beyond the established services. Read or
        evidence failures are carried honestly in ``evidence_error``."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(run_ref)
        project_dir = root / project_ref
        run_dir = project_dir / "runs" / run_ref
        if not run_dir.is_dir():
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"正式运行不存在：{run_ref}"
            )
        evidence_error: str | None = None
        lifecycle_status: str | None = None
        try:
            run_service = RunService(
                project_dir, ImportService(project_dir)
            )
            lifecycle_status = str(
                run_service.run_state(run_ref)["lifecycle_status"]
            )
        except Exception as exc:  # noqa: BLE001 - honest evidence failure
            evidence_error = f"运行生命周期状态不可读：{exc}"
        result_id: str | None = None
        result_status: str | None = None
        metrics: dict[str, Any] | None = None
        method: str | None = None
        scenario_bank_id: str | None = None
        manifest_id: str | None = None
        # runs WITHOUT a published/readable MethodResult (failed,
        # interrupted, corrupt evidence) project honestly: the SOC
        # modes and the capacity view are unknown, never a crash
        soc_modes: dict[str, str] | None = None
        capacity_view: dict[str, Any] | None = None
        common_view: dict[str, Any] | None = None
        result_path = run_dir / "method_result.json"
        link_path = run_dir / "result_link.json"
        manifest_path = run_dir / "artifact_manifest.json"
        if not (
            result_path.is_file()
            or link_path.is_file()
            or manifest_path.is_file()
        ):
            evidence_error = evidence_error or "结果证据缺失（未发布MethodResult）"
        else:
            # night-3 G1: the result identity loop closes BEFORE any
            # value is extracted - run_ref -> result_link ->
            # method_result run/result ids -> artifact_manifest
            # (expected ids + content hash), all through the EXISTING
            # result_service readers (the same loop p1_workflow
            # run_projection enforces on the adequacy chain). A broken
            # link/id/hash is evidence corruption and withholds EVERY
            # value; an absent/null link is the honest unpublished
            # state (the workflows write the null-id link before
            # computing and the id-bearing link last). No second
            # validator lives in the facade.
            try:
                from capacity_credit.application.result_service import (
                    read_artifact_manifest,
                    read_method_result,
                    read_result_link,
                )

                if not link_path.is_file():
                    raise ResultServiceError(
                        "RESULT_LINK_MISSING", "结果未发布（缺少result_link）"
                    )
                link = read_result_link(link_path)
                if str(link.run_id) != run_ref:
                    raise ResultServiceError(
                        "RESULT_LINK_RUN_MISMATCH",
                        "结果证据损坏（link与运行身份错绑）",
                    )
                if link.result_id is None or link.artifact_manifest_id is None:
                    raise ResultServiceError(
                        "RESULT_NOT_PUBLISHED", "结果未发布（运行无已发布结果）"
                    )
                if not result_path.is_file():
                    raise ResultServiceError(
                        "RESULT_MISSING", "结果证据损坏（link指向的结果文件缺失）"
                    )
                if not manifest_path.is_file():
                    raise ResultServiceError(
                        "ARTIFACT_MANIFEST_MISSING", "结果证据损坏（artifact清单缺失）"
                    )
                result = read_method_result(result_path)
                if str(result.run_id) != run_ref:
                    raise ResultServiceError(
                        "RESULT_RUN_MISMATCH", "结果证据损坏（结果run_id与运行身份错绑）"
                    )
                if str(result.result_id) != str(link.result_id):
                    raise ResultServiceError(
                        "RESULT_ID_MISMATCH", "结果证据损坏（结果result_id与link错绑）"
                    )
                manifest = read_artifact_manifest(
                    manifest_path,
                    result_path=result_path,
                    expected_run_id=run_ref,
                    expected_result_id=str(link.result_id),
                    expected_manifest_id=str(link.artifact_manifest_id),
                )
                # the loop is CLOSED - only now are display values
                # extracted (identical to the pre-G1 verbatim mapping)
                result_id = str(result.result_id)
                result_status = str(result.result_status.value)
                method = str(result.method.value)
                scenario_bank_id = (
                    str(result.scenario_bank_id)
                    if result.scenario_bank_id is not None
                    else None
                )
                # V1-T3-C: the REAL storage/chronology modes reach the UI
                soc_modes = {
                    "storage_soc_initialization_mode": str(
                        result.storage_soc_initialization_mode.value
                    ),
                    "soc_year_boundary_mode": str(result.soc_year_boundary_mode.value),
                    "chronology_mode": str(result.chronology_mode.value),
                }
                if result.risk_metrics is not None and result.risk_metrics.base is not None:
                    base = result.risk_metrics.base
                    # the pydantic contract is the established projection
                    # rule - model_dump gives JSON-safe native values
                    metrics = dict(base.model_dump())
                if method in ("elcc", "efc"):
                    # V1-T3-D: the capacity/search semantics of the ELCC/
                    # EFC MethodResult reach the UI VERBATIM - an
                    # unresolved search stays None (never a fabricated MW)
                    capacity_view = {
                        "capacity_value_type": str(
                            result.capacity_value_type.value
                        ),
                        "resource_or_portfolio_id": result.resource_or_portfolio_id,
                        "reference_capacity_mw": result.reference_capacity_mw,
                        "accredited_capacity_mw": result.accredited_capacity_mw,
                        "capacity_credit_fraction": result.capacity_credit_fraction,
                        "base_risk": result.base_risk,
                        "candidate_risk": result.candidate_risk,
                        "matched_risk": result.matched_risk,
                        "risk_difference": result.risk_difference,
                        "search_bracket_low_mw": result.search_bracket_low_mw,
                        "search_bracket_high_mw": result.search_bracket_high_mw,
                        "search_bracket_low_risk": result.search_bracket_low_risk,
                        "search_bracket_high_risk": result.search_bracket_high_risk,
                        "interpolation_method": str(
                            result.interpolation_method.value
                        ),
                        "search_convergence": str(result.search_convergence.value),
                        "risk_convergence": str(result.risk_convergence.value),
                        "sample_count": result.sample_count,
                        "confidence_level": result.confidence_level,
                        "lower_ci": result.lower_ci,
                        "upper_ci": result.upper_ci,
                        # R3-B3: the four scenario variables travel to the
                        # UI verbatim (None on legacy results)
                        "scenario_birth_certificate": (
                            result.scenario_birth_certificate.model_dump()
                            if result.scenario_birth_certificate is not None
                            else None
                        ),
                        # MR-01: the EFC de-rating identity reaches the UI
                        # without a second business-JSON read
                        "delivery_year": result.delivery_year,
                        "auction_round": result.auction_round,
                        "criterion_mode": (
                            str(result.criterion_mode)
                            if getattr(result, "criterion_mode", None) is not None
                            else None
                        ),
                        "technology_class": result.technology_class,
                        "effective_data_grade": str(
                            result.effective_data_grade.value
                        ),
                        # T4-A (A06/A07): the metric identity the CI
                        # belongs to, the bracket risk sources and the
                        # verbatim diagnostics reach the UI without a
                        # second business-JSON read
                        "primary_risk_metric": str(
                            result.primary_risk_metric.value
                        ),
                        "diagnostics": [
                            diagnostic.model_dump()
                            for diagnostic in result.diagnostics
                        ],
                    }
                # R06 (night-4): the SHARED result facts every method
                # shows - data grade, samples, convergence and the
                # verbatim diagnostics are NOT capacity-only fields
                if method == "demand_target_capacity":
                    # Step 2 Stage A：目标容量语义进投影（与 ELCC/EFC 同一
                    # capacity_view 透传通道；点值 None＝未收敛不给数）。
                    # Q-D4：adjust_load 口径下
                    # target_capacity_mw＝目标负荷水平（λ×峰荷），
                    # load_scale_factor＝λ——页面/引用方据此区分语义
                    capacity_view = {
                        "solver_caliber": result.solver_caliber,
                        "target_risk_value": result.target_risk_value,
                        "target_capacity_mw": result.accredited_capacity_mw,
                        "load_scale_factor": result.load_scale_factor,
                        "current_risk": result.base_risk,
                        "bracket_low_mw": result.search_bracket_low_mw,
                        "bracket_high_mw": result.search_bracket_high_mw,
                        "bracket_low_risk": result.search_bracket_low_risk,
                        "bracket_high_risk": result.search_bracket_high_risk,
                        "source_project_ref": result.source_project_ref,
                        "source_run_ref": result.source_run_ref,
                    }
                common_view = {
                    "effective_data_grade": str(
                        result.effective_data_grade.value
                    ),
                    "sample_count": result.sample_count,
                    "risk_convergence": str(result.risk_convergence.value),
                    "search_convergence": str(
                        result.search_convergence.value
                    ),
                    "primary_risk_metric": str(
                        result.primary_risk_metric.value
                    ),
                    "diagnostics": [
                        diagnostic.model_dump()
                        for diagnostic in result.diagnostics
                    ],
                    # D2: the run's own convergence evidence (criteria
                    # booleans + measured precision), read from the
                    # run dir written at publish time; a missing or
                    # unreadable file stays an explicit None (the
                    # honest 无法判断 tri-state), never a guess
                    "convergence_evidence": self._read_convergence_evidence(
                        run_dir
                    ),
                }
                manifest_id = (
                    str(manifest.artifact_manifest_id)
                    if manifest.artifact_manifest_id is not None
                    else None
                )
            except Exception as exc:  # noqa: BLE001 - corrupt evidence shown
                detail = (
                    exc.message
                    if isinstance(exc, ResultServiceError)
                    else str(exc)
                )
                evidence_error = (
                    (evidence_error or "") + f"结果证据不可确认：{detail}"
                ).strip("；")
        return {
            "run_ref": run_ref,
            "project_ref": project_ref,
            "lifecycle_status": lifecycle_status,
            "result_id": result_id,
            "result_status": result_status,
            "method": method,
            "scenario_bank_id": scenario_bank_id,
            "metrics": metrics,
            "artifact_manifest_id": manifest_id,
            "evidence_error": evidence_error,
            "soc_modes": soc_modes,
            "capacity_view": capacity_view,
            "common_view": common_view,
        }

    def export_analysis_report(
        self, project_ref: str, run_ref: str
    ) -> dict[str, Any]:
        """V1-T4-B: the ONE report entry for the pages and history -
        project/run refs IN, verified Excel+HTML artifacts OUT. The same
        result identity loop as get_run_projection gates the export (an
        unpublished or corrupted result exports NOTHING); the T23
        machine outputs are prepared idempotently through the EXISTING
        write_machine_outputs (the bare run directory is never handed to
        export_report), and the full report set is published by the
        EXISTING export_report (manifest last, same-identity replay,
        immutable semantics). Output locations are service-resolved
        under the workspace ``reports/`` area - the frontend never
        chooses input or output paths. This NEVER touches the run, its
        lifecycle or any calculation artifact."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(run_ref)
        run_dir = root / project_ref / "runs" / run_ref
        if not run_dir.is_dir():
            raise WorkbenchError(_WORKBENCH_INPUT, f"正式运行不存在：{run_ref}")
        projection = self.get_run_projection(project_ref, run_ref)
        if projection["evidence_error"] or not projection["result_id"]:
            reason = projection["evidence_error"] or "结果未发布"
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"结果不可导出：{reason}"
            )
        report_root = root / "reports" / project_ref / run_ref
        machine_dir = report_root / "machine_outputs"
        export_dir = report_root / "export"
        self._prepare_machine_outputs(run_dir, machine_dir)
        from capacity_credit.reporting.export_service import export_report
        from capacity_credit.reporting.report_model import ReportExportError

        try:
            summary = export_report(
                machine_dir,
                export_dir,
                business_notes=self._build_business_notes(run_dir),
            )
        except ReportExportError as exc:
            # the report layer's stable refusal (incomplete/conflict/
            # replay mismatch) stays a display-kind error at the facade;
            # the published set is never rewritten and the run's facts
            # are untouched
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"报告导出被拒绝：{exc.code}：{exc.message}",
            ) from None
        return {
            "run_ref": run_ref,
            "project_ref": project_ref,
            "machine_dir": str(machine_dir),
            "export": summary,
        }

    def get_run_report_view(self, project_ref: str, run_ref: str) -> dict[str, Any]:
        """P-EFC UI wiring: the READ-ONLY report projection the pages need
        for the search-trace table and the risk-event (gap) view - built
        through the SAME identity loop and the SAME idempotent machine
        output preparation as the export, so the page never opens a
        second channel to raw business artifacts. An unpublished or
        broken result refuses honestly (no empty table masquerading as
        "no events")."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(run_ref)
        run_dir = root / project_ref / "runs" / run_ref
        if not run_dir.is_dir():
            raise WorkbenchError(_WORKBENCH_INPUT, f"正式运行不存在：{run_ref}")
        projection = self.get_run_projection(project_ref, run_ref)
        if projection["evidence_error"] or not projection["result_id"]:
            reason = projection["evidence_error"] or "结果未发布"
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"结果不可读取报告视图：{reason}"
            )
        machine_dir = root / "reports" / project_ref / run_ref / "machine_outputs"
        self._prepare_machine_outputs(run_dir, machine_dir)
        from capacity_credit.reporting.report_model import (
            ReportExportError,
            load_and_validate,
        )

        try:
            view = load_and_validate(machine_dir)
        except ReportExportError as exc:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"报告视图不可构建：{exc.code}：{exc.message}",
            ) from None

        def table(tv: Any) -> dict[str, Any] | None:
            if tv is None:
                return None
            return {
                "source": str(tv.source),
                "total_rows": int(tv.total_rows),
                "displayed_rows": int(tv.displayed_rows),
                "rows": [dict(row) for row in tv.rows],
                "note": str(tv.note or ""),
            }

        return {
            "run_id": str(view.run_id),
            "result_id": str(view.result_id),
            "result_status": str(view.result_status),
            "method": str(view.method),
            "primary_risk_metric": str(view.primary_risk_metric),
            "primary_risk_metric_unit": str(view.primary_risk_metric_unit),
            "effective_data_grade": str(view.effective_data_grade),
            "risk_convergence": str(view.risk_convergence),
            "search_convergence": str(view.search_convergence),
            "is_diagnostic": bool(view.is_diagnostic),
            "use_boundary_code": str(view.use_boundary_code),
            "use_boundary_warning": str(view.use_boundary_warning),
            "random_seed": self._read_run_config_seed(run_dir),
            "search_trace": table(view.search_trace),
            "risk_events": table(view.risk_events),
            # Q-D1 (2026-10-10 ruling): loss-of-load duration aggregation
            # (per-year + cross-year weighted with the KPI weight set);
            # None/available=False stays honest on old machine outputs
            "risk_event_duration_summary": view.risk_event_duration_summary,
            # W2: per-weather-year risk metrics (None on runs without
            # the sidecar - the page renders an honest absence)
            "weather_year_metrics": table(view.weather_year_metrics),
            "conclusion_card": [dict(c) for c in view.conclusion_card],
            "capacity_row": (
                dict(view.capacity_row) if view.capacity_row else None
            ),
        }

    def _prepare_machine_outputs(
        self, run_dir: Path, machine_dir: Path
    ) -> None:
        """Idempotent, bounded T23 preparation for ONE run (night-4
        R04/R05 rules):
        - an existing COMPLETE machine dir is reused ONLY when it
          belongs to THIS run (its run_summary run_id binds to the run
          directory) - a foreign but self-consistent dir is refused,
          never republished as this run's report;
        - a PARTIAL dir left by THIS writer's own failed attempt (only
          known machine-output names inside) is safe to clear and
          rebuild - the retry never touches the run or its results;
        - any UNKNOWN (foreign/tampered) content is a conflict that is
          never overwritten;
        - a missing dir is written through the EXISTING writer with
          honestly-sourced reproducibility context (whatever cannot be
          verified stays honestly listed as missing - never a fabricated
          full-rerun claim)."""
        from capacity_credit.reporting.machine import (
            MACHINE_OUTPUT_FILES,
            WEATHER_YEAR_METRICS_FILE,
        )
        from capacity_credit.reporting.report_model import load_and_validate

        if machine_dir.is_dir():
            present = {
                entry.name
                for entry in machine_dir.iterdir()
                if entry.is_file()
            }
            # the writer's own in-progress marker is bookkeeping, never
            # "unknown foreign content"; the W2 eighth artifact is a
            # known machine-output name on runs that left a sidecar
            known_names = set(MACHINE_OUTPUT_FILES) | {
                ".write_in_progress",
                WEATHER_YEAR_METRICS_FILE,
            }
            if present - known_names:
                raise WorkbenchError(
                    _WORKBENCH_INPUT,
                    "既有报告机器产物不完整或损坏，已拒绝导出（不静默覆盖）："
                    f"{machine_dir}",
                )
            if set(MACHINE_OUTPUT_FILES).issubset(present):
                load_and_validate(machine_dir)  # reuse only what verifies
                summary_path = machine_dir / "run_summary.json"
                try:
                    summary = json.loads(
                        summary_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    summary = {}
                bound_run = str(summary.get("run_id") or "")
                if bound_run != run_dir.name:
                    raise WorkbenchError(
                        _WORKBENCH_INPUT,
                        "报告机器产物与运行身份不符：该目录属于运行 "
                        f"{bound_run[:8] if bound_run else '(未知)'}…，"
                        f"而非 {run_dir.name[:8]}…；已拒绝导出（不覆盖他人产物）",
                    )
                return
            # R05: a PARTIAL dir is rebuilt ONLY when it is recognizably
            # THIS writer's own failed attempt (marker present). A
            # partial dir WITHOUT the marker is external tampering or
            # unknown provenance - refused, never overwritten.
            if not (machine_dir / ".write_in_progress").is_file():
                raise WorkbenchError(
                    _WORKBENCH_INPUT,
                    "既有报告机器产物不完整，且无法识别为本程序未完成的写入"
                    "（缺少写入标记），已拒绝导出（不覆盖来历不明的目录）："
                    f"{machine_dir}",
                )
            import shutil

            shutil.rmtree(machine_dir)
        manifest_path = run_dir / "run_manifest.json"
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except ValueError:
                manifest = {}
        reproducibility_context: dict[str, Any] = {}
        if manifest.get("data_version_hash"):
            reproducibility_context["inputs_hash"] = manifest[
                "data_version_hash"
            ]
        if manifest.get("run_config_hash"):
            reproducibility_context["config_hash"] = manifest[
                "run_config_hash"
            ]
        if manifest.get("software_version"):
            reproducibility_context["software_version"] = manifest[
                "software_version"
            ]
        seed = self._read_run_config_seed(run_dir)
        if seed is not None:
            reproducibility_context["random_seed"] = seed
        # model_ready_data / data_source_records are NOT claimed: the
        # reproducibility manifest lists them as missing and records
        # rerun_capability=verify_only (never a fabricated full_rerun)
        trace_path = run_dir / "search_trace.json"
        from capacity_credit.reporting.machine import (
            risk_event_rows_from_deficit_events,
            write_machine_outputs,
        )

        # R3-B1: a run's deficit-event evidence (written by the
        # adequacy/ELCC publication chains) supplies the report layer's
        # risk_events slot - previously ALWAYS "missing" (events were
        # computed then discarded). Missing/corrupt evidence stays an
        # honest "missing", never a fabricated event.
        risk_events: list[dict[str, Any]] | None = None
        evidence_path = run_dir / "deficit_events.json"
        if evidence_path.is_file():
            try:
                payload = json.loads(
                    evidence_path.read_text(encoding="utf-8")
                )
                raw_events = payload.get("events")
                if isinstance(raw_events, list) and raw_events:
                    risk_events = risk_event_rows_from_deficit_events(
                        raw_events
                    )
            except (OSError, ValueError):
                risk_events = None

        # W4/MR-03: a bound seed-replication summary feeds the report's
        # review block; missing/corrupt stays an honest absence
        from capacity_credit.application.result_service import (
            read_seed_replication_summary,
        )

        replication_summary = read_seed_replication_summary(run_dir)

        # W2 (weather-year plan section 3): the run's per-year metric
        # sidecar (written by the adequacy publication chain) feeds the
        # OPTIONAL eighth machine artifact; missing/corrupt stays an
        # honest absence - old runs keep the seven fixed artifacts
        weather_year_rows: list[dict[str, Any]] | None = None
        weather_path = run_dir / "weather_year_metrics.json"
        if weather_path.is_file():
            try:
                payload = json.loads(weather_path.read_text(encoding="utf-8"))
                rows = payload.get("rows")
                if isinstance(rows, list) and rows:
                    weather_year_rows = rows
            except (OSError, ValueError):
                weather_year_rows = None

        write_machine_outputs(
            run_dir / "method_result.json",
            machine_dir,
            search_trace_path=(
                trace_path if trace_path.is_file() else None
            ),
            risk_events=risk_events,
            seed_replication_summary=replication_summary,
            reproducibility_context=reproducibility_context,
            weather_year_metrics=weather_year_rows,
        )

    def _read_run_config_seed(self, run_dir: Path) -> int | None:
        """The declared random seed from the run's OWN frozen config
        (existing file, existing parser; a missing/invalid seed is an
        honest None, never a guess)."""
        import yaml

        config_path = run_dir / "run_config.yaml"
        if not config_path.is_file():
            return None
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return None
        if not isinstance(config, dict):
            return None
        seed = config.get("random_seed")
        return int(seed) if isinstance(seed, int) and not isinstance(seed, bool) else None

    @staticmethod
    def _report_downloads_unavailable(
        reason: str, output_dir: str | None = None
    ) -> dict[str, Any]:
        """THE single honest unavailable shape for the download read."""
        return {
            "available": False,
            "reason": reason,
            "output_dir": output_dir,
            "result_status": None,
            "files": [],
        }

    def get_bank_sample_preview(
        self, project_ref: str, scenario_bank_id: str, sample_count: int = 2
    ) -> dict[str, Any]:
        """A3 (C-night): a READ-ONLY peek into an EXISTING scenario bank
        via the existing bank reader - a few REAL samples' per-object
        available hours (unit: hours in that weather year), clearly
        labeled with the sample number and weather year. This is what
        the bank actually stores (per-hour availability fractions); no
        resampling, no re-computation, no invented curves."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        bank_path = (
            root / project_ref / "artifacts" / "scenario_banks" / scenario_bank_id
        )
        try:
            bank = open_scenario_bank(bank_path)
        except Exception as exc:  # noqa: BLE001 - ScenarioBankError/OSError
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"模拟样本集不可读取：{exc}"
            ) from None
        sample_count = max(1, min(int(sample_count), 5))
        total = int(bank.index["sample_count"])
        sample_ids = list(range(min(sample_count, total)))
        year = min(int(y) for y in bank.index["weather_years"])
        hours = None
        objects: list[dict[str, Any]] = []
        for entry in bank.index["objects"]:
            object_id = str(entry["object_id"])
            fractions = bank.read_object_array(object_id, weather_year=year)
            # the stored array is (samples, hours-in-year): the HOUR
            # dimension is axis 1 - the preview denominator is the year's
            # hour count, never the bank's sample count
            if fractions.ndim != 2:
                raise WorkbenchError(
                    _WORKBENCH_INPUT,
                    f"样本数组形状异常（{fractions.shape}），无法预览",
                )
            hours = int(fractions.shape[1]) if hours is None else hours
            objects.append(
                {
                    "object_id": object_id,
                    "capacity_mw": float(entry["capacity_mw"]),
                    "available_hours": [
                        float(np.asarray(fractions[sample], dtype=float).sum())
                        for sample in sample_ids
                    ],
                }
            )
        return {
            "scenario_bank_id": bank.bank_id,
            "weather_year": year,
            "total_samples": total,
            "previewed_samples": len(sample_ids),
            "hours_in_year": hours,
            "sample_ids": sample_ids,
            "objects": objects,
        }

    def _build_business_notes(self, run_dir: Path) -> dict[str, Any]:
        """C-night/F: the deterministic business-reading payload for ONE
        run, built ONLY from that run's own audited artifacts - the run
        manifest, its frozen run config, its convergence-evidence
        record, the formal MethodResult, its scenario-bank index and
        the PINNED data-version copy under ``versions/`` (NEVER the
        project's latest state: an old run is explained by its own
        inputs). Every number is a verbatim or aggregate fact; missing
        facts are stated as missing, never filled from elsewhere."""
        from capacity_credit.reporting import business_notes as bn

        manifest = self._read_json_file(run_dir / "run_manifest.json") or {}
        run_config = self._read_yaml_file(run_dir / "run_config.yaml") or {}
        result = self._read_json_file(run_dir / "method_result.json") or {}
        evidence = self._read_convergence_evidence(run_dir)

        result_status = str(result.get("result_status") or "未知")
        metrics = dict(
            (result.get("risk_metrics") or {}).get("base") or {}
        )
        sample_count = result.get("sample_count")

        # scope facts from the run's OWN bank + weight set
        bank_id = str(manifest.get("scenario_bank_id") or "")
        bank_index = (
            self._read_json_file(
                run_dir.parent.parent
                / "artifacts"
                / "scenario_banks"
                / bank_id
                / "index.json"
            )
            if bank_id
            else {}
        ) or {}
        weight_set_id = str(
            run_config.get("weather_year_weight_set_id") or ""
        )
        weight_share = self._weight_share_text(
            run_dir.parent.parent, bank_id, weight_set_id, manifest
        )
        seed = run_config.get("random_seed")
        scope_paragraphs = [
            (
                f"评估对象：项目数据集 {manifest.get('dataset_id') or '—'}，"
                f"数据版本 {str(manifest.get('data_version_id') or '—')[:16]}…"
                "（数据用途等级见结果页标注）。"
            ),
            (
                f"模拟范围：气象年 {(bank_index.get('weather_years') or ['—'])}，"
                f"权重集 {weight_set_id or '—'}（{weight_share}）；"
                f"随机种子 {seed if seed is not None else '—'}；"
                f"准备并使用的随机样本 {bank_index.get('sample_count', sample_count) if sample_count is None else sample_count} 个。"
                "模拟样本不是未来天气预报，也不是已发生的停电记录。"
            ),
        ]
        scope_table = self._frozen_input_summary(
            run_dir.parent.parent, manifest
        )
        method_paragraphs = [
            (
                "工具在既定系统输入下生成多组设备可用状态，逐小时比较供给和需求，"
                "再汇总年度缺电天数、小时数及电量；随后按四项统计判据判断估计"
                "是否满足本次精度要求。"
            ),
            (
                "抽样收敛与容量搜索收敛（如适用）分别判断；计算耗时与数据准备"
                "耗时不在本报告统计范围内。"
            ),
        ]
        improvement_note = (
            "本次为单一基准测算，没有对照试算。如需评估改善措施"
            "（新增电源、储能、支援或可靠性改造），需要在相同评估口径下"
            "补充对照方案并重新计算；本报告不直接给出所需新增容量。"
        )
        appendix_rows = [
            ["来源运行", run_dir.name],
            ["结果标识", str(result.get("result_id") or "—")],
            ["结果状态", result_status],
            ["场景银行", bank_id[:16] + "…" if bank_id else "—"],
            [
                "数据版本哈希",
                str(manifest.get("data_version_hash") or "—"),
            ],
            [
                "软件版本/提交",
                (
                    f"{manifest.get('software_version', '—')} / "
                    f"{manifest.get('software_commit', '—')}"
                ),
            ],
            [
                "收敛判据证据",
                "已随运行记录（convergence_evidence.json）"
                if evidence is not None
                else "本运行无判据级证据（旧版本运行）",
            ],
        ]
        status_business = bn.RESULT_STATUS_BUSINESS.get(
            result_status,
            f"结果状态：{result_status}。",
        )
        if evidence is None:
            status_business += "（本结果没有判据级收敛证据，明细无法判断。）"
        return bn.build_notes_from_run(
            method=str(result.get("method") or ""),
            method_label={
                "adequacy": "系统充裕度评估",
                "elcc": "ELCC 有效承载容量评估",
                "efc": "EFC 等效完美容量评估",
            }.get(str(result.get("method") or ""), "充裕度评估"),
            result_status=result_status,
            result_status_business=status_business,
            metrics=metrics,
            convergence_evidence=evidence,
            scope_paragraphs=scope_paragraphs,
            scope_table=scope_table,
            method_paragraphs=method_paragraphs,
            improvement_note=improvement_note,
            appendix_rows=appendix_rows,
        )

    def _read_json_file(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _read_yaml_file(self, path: Path) -> dict[str, Any] | None:
        import yaml

        if not path.is_file():
            return None
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return None
        return payload if isinstance(payload, dict) else None

    def _weight_share_text(
        self,
        project_dir: Path,
        bank_id: str,
        weight_set_id: str,
        manifest: dict[str, Any],
    ) -> str:
        """The FINAL per-year weight shares from the run's OWN pinned
        data version (the declared weights sum to 1, so they ARE the
        percentages); an unreadable set is stated as missing."""
        if not weight_set_id:
            return "未声明"
        dataset = str(manifest.get("dataset_id") or "")
        version = str(manifest.get("data_version_id") or "")
        table = (
            project_dir
            / "versions"
            / dataset
            / version
            / "data"
            / "weather_year_weight_sets.csv"
        )
        if not table.is_file():
            return "权重明细不可读"
        shares: dict[str, float] = {}
        import csv as _csv

        try:
            with table.open(encoding="utf-8", newline="") as handle:
                for row in _csv.DictReader(handle):
                    if str(row.get("weight_set_id") or "").strip() != weight_set_id:
                        continue
                    year = str(row.get("weather_year") or "").strip()
                    raw = row.get("weight")
                    if year and raw is not None:
                        shares[year] = float(raw)
        except (OSError, TypeError, ValueError):
            return "权重明细不可读"
        if not shares:
            return "权重明细不可读"
        return "，".join(
            f"{year}年占 {share * 100:.1f}%"
            for year, share in sorted(shares.items())
        )

    def _frozen_input_summary(
        self, project_dir: Path, manifest: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A small, bounded summary of the run's PINNED frozen input
        tables (count/min/max/mean facts only - never a re-simulation);
        absent tables are listed as 未纳入."""
        dataset = str(manifest.get("dataset_id") or "")
        version = str(manifest.get("data_version_id") or "")
        data_dir = project_dir / "versions" / dataset / version / "data"

        def read(name: str) -> list[dict[str, str]]:
            path = data_dir / name
            if not path.is_file():
                return []
            import csv as _csv

            try:
                with path.open(encoding="utf-8", newline="") as handle:
                    return list(_csv.DictReader(handle))
            except OSError:
                return []

        rows_out: list[list[str]] = []
        loads = read("load_hourly.csv")
        if loads:
            values = []
            for row in loads:
                try:
                    values.append(float(row.get("load_mw")))
                except (TypeError, ValueError):
                    continue
            if values:
                hours = len(values)
                rows_out.append(
                    [
                        "逐小时负荷",
                        (
                            f"均值 {sum(values) / hours:,.1f} MW，"
                            f"最小 {min(values):,.1f} MW，最大 {max(values):,.1f} MW，"
                            f"共 {hours:,} 小时"
                        ),
                    ]
                )
        units = read("thermal_units.csv")
        if units:
            total = 0.0
            count = 0
            for row in units:
                try:
                    total += float(row.get("capacity_mw"))
                    count += 1
                except (TypeError, ValueError):
                    continue
            rows_out.append(
                [f"常规机组（{count} 台）", f"总容量 {total:,.1f} MW"]
            )
        classes = read("thermal_reliability_classes.csv")
        if classes:
            parts = []
            for row in classes:
                cid = str(row.get("reliability_class_id") or "").strip()
                rate = row.get("for_rate")
                if cid and rate is not None:
                    parts.append(f"{cid}={rate}")
            if parts:
                rows_out.append(["强迫停运率（按类别）", "；".join(parts)])
        renewables = read("renewable_capacities.csv")
        if renewables:
            groups: dict[str, float] = {}
            for row in renewables:
                group = str(row.get("resource_group_id") or "").strip()
                try:
                    mw = float(row.get("installed_capacity_mw"))
                except (TypeError, ValueError):
                    continue
                groups[group] = max(mw, groups.get(group, 0.0))
            if groups:
                total = sum(groups.values())
                rows_out.append(
                    [
                        f"新能源（{len(groups)} 组）",
                        f"总装机 {total:,.1f} MW",
                    ]
                )
        storage = read("storage_units.csv")
        rows_out.append(
            [
                "储能",
                f"{len(storage)} 台（纳入资源充裕度链）"
                if storage
                else "未纳入",
            ]
        )
        interties = read("interties.csv")
        rows_out.append(
            ["联络线", f"{len(interties)} 条" if interties else "未纳入"]
        )
        if not rows_out:
            return None
        return {
            "columns": ["输入项", "本运行输入（只读事实）"],
            "rows": rows_out,
        }

    def _read_convergence_evidence(
        self, run_dir: Path
    ) -> dict[str, Any] | None:
        """D2: read one run's ``convergence_evidence.json`` (written by
        the publisher from the audited gate objects). ANY problem -
        missing file (old runs), unreadable content, wrong shape -
        returns None: the page then states the criteria as 无法判断
        instead of guessing; a corrupt file is NEVER presented as
        evidence."""
        path = run_dir / "convergence_evidence.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("criteria"), dict) or not isinstance(
            payload.get("measured"), dict
        ):
            return None
        return payload

    def get_report_downloads(
        self, project_ref: str, run_ref: str
    ) -> dict[str, Any]:
        """V1-T4-B + night-4 R04: the VERIFIED download set for one
        run's published report (the pages render only this - they never
        construct paths or read business files). The read re-checks the
        CURRENT result identity loop (a broken result never lets old
        download bytes masquerade as this run's trusted report), re-binds
        the manifest to the requested run AND result, REQUIRES the fixed
        full Excel+HTML set (a manifest that lost an entry never reads
        as complete), survives legal-JSON wrong shapes honestly, and
        verifies every file's fresh bytes against the manifest hash. No
        export is triggered here (history/queries stay read-only; the
        explicit export action retries the report)."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(run_ref)
        export_dir = root / "reports" / project_ref / run_ref / "export"
        manifest_path = export_dir / "report_export_manifest.json"
        if not manifest_path.is_file():
            return self._report_downloads_unavailable("尚未导出或清单缺失")
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            return self._report_downloads_unavailable(f"报告清单不可读：{exc}")
        if not isinstance(manifest, dict):
            return self._report_downloads_unavailable(
                "报告清单形状无效（不是对象）"
            )
        # R04: the CURRENT result identity loop must still close before
        # any historical byte is served as this run's report
        try:
            projection = self.get_run_projection(project_ref, run_ref)
        except WorkbenchError as exc:
            return self._report_downloads_unavailable(
                f"运行不可读：{exc.message}"
            )
        if projection.get("evidence_error") or not projection.get("result_id"):
            detail = projection.get("evidence_error") or "结果未发布"
            return self._report_downloads_unavailable(
                f"当前结果证据不可确认，历史报告不再作为本运行的可信结果提供：{detail}"
            )
        # review Q1 remediation + R04: the manifest is re-bound to the
        # requested run AND its current result id
        source_run = str(manifest.get("source_run_id") or "")
        if source_run != run_ref:
            return self._report_downloads_unavailable(
                "报告清单与运行身份不符：清单属于运行 "
                f"{source_run[:8] if source_run else '(空)'}…，"
                f"而非 {run_ref[:8]}…"
            )
        source_result = str(manifest.get("source_result_id") or "")
        if source_result != str(projection.get("result_id")):
            return self._report_downloads_unavailable(
                "报告清单与当前结果身份不符（结果证据已变化）；请重新生成报告"
            )
        outputs = manifest.get("outputs")
        if isinstance(outputs, dict):
            outputs = list(outputs.values())
        if not isinstance(outputs, list) or any(
            not isinstance(entry, dict) for entry in outputs
        ):
            return self._report_downloads_unavailable(
                "报告清单outputs形状无效"
            )
        import hashlib as _hashlib

        files: list[dict[str, Any]] = []
        present_names: set[str] = set()
        for entry in outputs:
            name = str(entry.get("name") or "")
            present_names.add(name)
            try:
                path = export_dir / safe_filename_component(name)
            except ValueError:
                files.append(
                    {
                        "name": name,
                        "mime": None,
                        "available": False,
                        "reason": "清单中的文件名非法",
                        "data": None,
                    }
                )
                continue
            record: dict[str, Any] = {
                "name": name,
                "mime": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                    if name.endswith(".xlsx")
                    else "text/html"
                ),
                "available": False,
                "reason": None,
                "data": None,
            }
            if not path.is_file():
                record["reason"] = "文件缺失"
            else:
                data = path.read_bytes()
                if _hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                    record["reason"] = "与发布清单哈希不一致"
                else:
                    record["available"] = True
                    record["data"] = data
                    record["size"] = entry.get("size")
            files.append(record)
        # R04: the fixed full set is REQUIRED - a manifest that lost an
        # entry never reads as a complete set (the per-file records stay
        # attached so the problem detail remains visible)
        missing_required = [
            name for name in ("report.xlsx", "report.html")
            if name not in present_names
        ]
        if missing_required:
            return {
                "available": False,
                "reason": (
                    "发布集合不完整：清单缺少 "
                    + "、".join(missing_required)
                ),
                "output_dir": str(export_dir),
                "result_status": manifest.get("result_status"),
                "files": files,
            }
        if not files:
            return self._report_downloads_unavailable("发布清单没有任何文件")
        return {
            "available": all(f["available"] for f in files),
            "reason": None,
            "output_dir": str(export_dir),
            "result_status": manifest.get("result_status"),
            "files": files,
        }

    def create_project(
        self,
        *,
        request_id: str,
        name: str,
        description: str,
        resource_composition: dict[str, str] | None = None,
        project_kind: str = "reliability",
        measurement_type: str | None = None,
        object_name: str | None = None,
        province: str | None = None,
        base_year: int | None = None,
    ) -> dict[str, Any]:
        """Create one registered project (V1-T2-A; night-5 cost domain)
        via the project service: workspace validated first, business
        fields validated by the service, atomic registry write.
        Idempotent per request_id; same name under a new request is a
        distinct project; a cost project takes NO resource composition.
        R1: a project may carry a locked measurement_type (created AS
        one measurement); omitting it keeps a legacy domain-guarded
        project. Nothing import-related is fabricated (the project is
        待导入)."""
        root = _validate_root(self._root)
        from capacity_credit.application.project_service import create_project

        return create_project(
            workspace_root=root,
            request_id=request_id,
            name=name,
            description=description,
            resource_composition=resource_composition,
            project_kind=project_kind,
            measurement_type=measurement_type,
            object_name=object_name,
            province=province,
            base_year=base_year,
        )

    def get_project_measurement_type(self, project_ref: str) -> str | None:
        """The LOCKED measurement type of one project (None for legacy
        entries without the field - domain-guarded only)."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import (
            measurement_type_of,
            read_registry,
        )

        try:
            registry = read_registry(root)
        except Exception as exc:  # noqa: BLE001 - ProjectServiceError
            raise WorkbenchError(
                _WORKBENCH_UNAVAILABLE, f"项目登记不可读取：{exc}"
            ) from None
        for entry in registry["projects"]:
            if entry.get("project_ref") == project_ref:
                return measurement_type_of(entry)
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"项目不存在或不是本工作区的正式项目：{project_ref}"
        )

    def get_project_archived(self, project_ref: str) -> bool | None:
        """The archived flag of one registry entry (None = legacy/no
        field)."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import read_registry

        try:
            registry = read_registry(root)
        except Exception as exc:  # noqa: BLE001 - ProjectServiceError
            raise WorkbenchError(
                _WORKBENCH_UNAVAILABLE, f"项目登记不可读取：{exc}"
            ) from None
        for entry in registry["projects"]:
            if entry.get("project_ref") == project_ref:
                value = entry.get("archived")
                return bool(value) if value is not None else None
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"项目不存在或不是本工作区的正式项目：{project_ref}"
        )

    def set_project_archived(
        self, project_ref: str, *, archived: bool
    ) -> dict[str, Any]:
        """Soft archive / restore (R5b): registry flag only - the
        project directory and its runs are never touched."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import (
            set_project_archived as _set_archived,
        )

        return _set_archived(root, project_ref, archived=archived)

    def set_resource_composition(
        self, project_ref: str, resource_composition: Any
    ) -> dict[str, Any]:
        """Post-creation update of the storage/hydro declaration (user
        ruling 2026-09-17: the creation form only asks for the name;
        the declaration moved to the data-input step). Registry entry
        only; completed projects and the cost domain refuse honestly.
        ProjectServiceError is mapped to WorkbenchError so the kind
        (e.g. project_completed) survives to the HTTP boundary."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import (
            ProjectServiceError,
            set_resource_composition as _set_composition,
        )

        try:
            return _set_composition(root, project_ref, resource_composition)
        except ProjectServiceError as exc:
            raise WorkbenchError(exc.kind, exc.message) from None

    # ---- P-A: one project, one formal result, copy-as-new -----------

    def get_formal_result(self, project_ref: str) -> dict[str, Any] | None:
        """THE unique formal-result binding of one project (None when
        unbound). A LEGACY project that predates the registry (a plain
        T24-style directory with imports) has no binding - readers get
        None, not an error; admission already validated existence."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import (
            ProjectServiceError,
            get_formal_result as _get_formal,
        )

        try:
            return _get_formal(root, project_ref)
        except ProjectServiceError as exc:
            if exc.kind == "invalid_input":
                return None
            raise WorkbenchError(exc.kind, exc.message) from None

    def bind_formal_result_from_run(
        self, project_ref: str, *, domain: str, run_ref: str
    ) -> dict[str, Any]:
        """Bind a run as THE formal result after validating the run
        record itself (P-A §3.3): cost runs must be completed with a
        result; reliability runs must be completed, published, evidence
        clean and NOT unconverged/invalid. Idempotent for the same run;
        an already-bound project never rebinds."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import (
            ProjectServiceError,
            bind_formal_result as _bind,
        )

        result_sha: str | None = None
        if domain == "cost":
            from capacity_credit.application import cost_service

            try:
                record = cost_service.read_cost_run(
                    root / project_ref, run_ref
                )
            except cost_service.CostServiceError as exc:
                raise WorkbenchError(exc.kind, exc.message) from None
            manifest = record.get("manifest") or {}
            if (
                manifest.get("status") != "completed"
                or not isinstance(record.get("result"), dict)
            ):
                raise WorkbenchError(
                    "invalid_input",
                    f"运行 {run_ref} 未完成或无有效结果，不能绑定为正式结果",
                )
            result_sha = (
                str(manifest.get("result_sha256"))
                if manifest.get("result_sha256")
                else None
            )
        elif domain == "reliability":
            projection = self.get_run_projection(project_ref, run_ref)
            valid_states = {"valid", "valid_with_warning", "low_resolution"}
            if (
                projection.get("lifecycle_status") != "completed"
                or not projection.get("result_id")
                or str(projection.get("result_status") or "")
                not in valid_states
            ):
                raise WorkbenchError(
                    "invalid_input",
                    f"运行 {run_ref} 未完成、未发布或未收敛/无效，"
                    "不能绑定为正式结果（未收敛不是正式结论）",
                )
            # W4/MR-03: a FORMAL reliability result must carry a PASSED
            # seed-replication gate bound to its run dir (多种子稳健性
            # 复核，方法说明第五章(三) / 第八章风险5) - teaching and
            # research chains are exempt by adjudication and keep the
            # honest SEED_REPLICATION_GATE_MISSING disclosure instead
            run_dir = root / project_ref / "runs" / run_ref
            run_config = run_dir / "run_config.yaml"
            run_mode_value = ""
            if run_config.is_file():
                import yaml

                try:
                    config_payload = yaml.safe_load(
                        run_config.read_text(encoding="utf-8")
                    )
                except yaml.YAMLError:
                    config_payload = None
                if isinstance(config_payload, dict):
                    run_mode_value = str(config_payload.get("run_mode") or "")
            if run_mode_value == "formal":
                from capacity_credit.application.result_service import (
                    read_seed_replication_summary,
                )

                summary = read_seed_replication_summary(run_dir)
                if summary is None or not summary.get("gate", {}).get("passed"):
                    raise WorkbenchError(
                        "seed_replication_gate_failed",
                        "正式可靠性结果必须绑定通过的 seed_replication 门"
                        "（多种子稳健性复核）：本运行目录没有通过的复核汇总。"
                        "请先运行 seed_replication 批次（仅换 root_seed 的完整"
                        "复核，默认 20 组）并把通过的 summary 写入本运行目录；"
                        "教学/研究链路豁免本门。",
                    )
        else:
            raise WorkbenchError(
                "invalid_input", f"未知结果域 {domain!r}"
            )
        try:
            return _bind(
                root,
                project_ref,
                domain=domain,
                run_ref=run_ref,
                result_sha256=result_sha,
            )
        except ProjectServiceError as exc:
            raise WorkbenchError(exc.kind, exc.message) from None

    def copy_project(
        self,
        source_project_ref: str,
        *,
        request_id: str,
        source_run_ref: str | None = None,
    ) -> dict[str, Any]:
        """Copy-as-new-project (P-A §3.5): a NEW project with the
        source's type and business fields plus the source provenance;
        the source is untouched. When the source has a formal result its
        run_ref prefills the copy's form by default."""
        root = _validate_root(self._root)
        _validate_project_ref(source_project_ref)
        from capacity_credit.application.project_service import (
            ProjectServiceError,
            copy_project as _copy,
        )

        if source_run_ref is None:
            formal = self.get_formal_result(source_project_ref)
            if formal:
                source_run_ref = str(formal.get("run_ref"))
        try:
            return _copy(
                root,
                source_project_ref=source_project_ref,
                request_id=request_id,
                source_run_ref=source_run_ref,
            )
        except ProjectServiceError as exc:
            raise WorkbenchError(exc.kind, exc.message) from None

    def _guard_not_completed(self, project_ref: str, action: str) -> None:
        """P-A completion guard: a project with a formal result refuses
        any further business measurement (copy-as-new is the only
        re-measurement path)."""
        formal = self.get_formal_result(project_ref)
        if formal:
            raise WorkbenchError(
                "project_completed",
                "该项目已完成一次业务测算（正式结果已绑定 "
                f"{formal.get('domain')}／{str(formal.get('run_ref'))[:8]}…），"
                f"不能再次{action}。修改参数重新测算请使用“复制为新项目”，"
                "来源与血缘将完整保留。",
            )

    def _guard_measurement_type_lock(
        self, project_ref: str, requested_kind: str
    ) -> None:
        """P-A: the FACADE enforces the creation-time type lock (the
        HTTP layer's check is not the only gate). A LEGACY project
        without a registry entry stays governed by the domain guard
        only (the R1 rule - old projects are never retro-typed)."""
        try:
            locked = self.get_project_measurement_type(project_ref)
        except WorkbenchError as exc:
            if exc.kind != "invalid_input":
                raise
            locked = None
        if locked is not None and locked != requested_kind:
            raise WorkbenchError(
                "invalid_input",
                f"该项目已锁定为 {locked} 测算类型，不能执行 "
                f"{requested_kind}。请新建对应类型的测算项目。",
            )

    def get_declared_composition(self, project_ref: str) -> dict[str, str]:
        """The DECLARED resource composition of one registered project
        (from the registry entry); a legacy project with imports (not
        registry-created) has no declaration - honest empty dict."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import read_registry

        registry = read_registry(root)
        for entry in registry["projects"]:
            if entry.get("project_ref") == project_ref:
                composition = entry.get("resource_composition")
                return dict(composition) if isinstance(composition, dict) else {}
        return {}

    def get_input_templates(self, project_ref: str) -> dict[str, Any]:
        """The template slots for one project's declared composition
        (public required slots always; optional slots follow the
        three-state declaration). Read-only."""
        _validate_root(self._root)
        composition = self.get_declared_composition(project_ref)
        from capacity_credit.data.template_catalog import (
            templates_for_composition,
        )

        return templates_for_composition(composition)

    # ------------------------------------------------------------------
    # night-5: the cost-domain use cases (thin dispatch over the SAME
    # LocalRuntime admission and the SAME workspace; the cost run
    # records live under project/cost_runs/ via application.cost_service)
    # ------------------------------------------------------------------

    def get_project_kind(self, project_ref: str) -> str:
        """The registered project domain (reliability/cost); legacy
        entries read as reliability. An unknown project is honest
        invalid_input."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.project_service import (
            project_kind_of,
            read_registry,
        )

        try:
            registry = read_registry(root)
        except Exception as exc:  # noqa: BLE001 - ProjectServiceError
            raise WorkbenchError(
                _WORKBENCH_UNAVAILABLE, f"项目登记不可读取：{exc}"
            ) from None
        for entry in registry["projects"]:
            if entry.get("project_ref") == project_ref:
                return project_kind_of(entry)
        raise WorkbenchError(
            _WORKBENCH_INPUT, f"项目不存在或不是本工作区的正式项目：{project_ref}"
        )

    def start_cost_calculation(
        self,
        *,
        intent_id: str,
        project_ref: str,
        cost_kind: str,
        payload: Mapping[str, Any],
        intent_is_new: bool = False,
    ) -> StartHandle:
        """THE single cost-calculation entry (night-5): validate first
        (project exists and is a COST project, kind delivered, payload
        complete) - all BEFORE admission with zero side effects - then
        admit through the SAME workspace-shared LocalRuntime. The
        deterministic computation runs synchronously in the runtime
        thread and publishes the cost-run record; no sampling, no
        convergence display."""
        from capacity_credit.application import cost_service

        root = _validate_root(self._root)
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "intent_id 必须为非空字符串")
        if cost_kind not in cost_service._COST_COMPUTERS:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"成本测算类型 {cost_kind!r} 尚未交付，不能发起计算",
            )
        summary = self.get_project_summary(project_ref)
        if summary is None:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"项目不存在或不是本工作区的正式项目：{project_ref}",
            )
        if summary["invalid"]:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"项目条目损坏，不能进入计算：{project_ref}（{summary['invalid_reason']}）",
            )
        if self.get_project_kind(project_ref) != "cost":
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                "成本测算需要成本项目：请为成本功能创建成本项目"
                "（可靠性项目不适用于成本测算）",
            )
        # P-A: a completed project never runs a second business
        # measurement, and the creation-time type lock is enforced at
        # the facade (not only at the HTTP boundary)
        self._guard_not_completed(project_ref, "发起成本测算")
        self._guard_measurement_type_lock(project_ref, cost_kind)
        payload_dict = dict(payload)
        if cost_kind == cost_service.COST_KIND_NET_CONE:
            # the internal Gross binding resolves EXPLICITLY against the
            # referenced run BEFORE validation/admission: a damaged or
            # mismatched source refuses here (never a silent latest-file
            # switch)
            try:
                payload_dict = cost_service.resolve_gross_reference(
                    root / project_ref, payload_dict
                )
            except cost_service.CostServiceError as exc:
                raise WorkbenchError(exc.kind, exc.message) from None
        try:
            cost_service.validate_cost_payload(cost_kind, payload_dict)
        except cost_service.CostServiceError as exc:
            raise WorkbenchError(exc.kind, exc.message) from None
        from capacity_credit import __version__

        try:
            params_hash = hash_payload(
                {
                    "project_ref": project_ref,
                    "cost_kind": cost_kind,
                    "payload": payload_dict,
                }
            )
        except ValueError as exc:
            raise WorkbenchError(
                _WORKBENCH_INPUT, f"成本输入含不可进入身份哈希的内容：{exc}"
            ) from None
        project_dir = root / project_ref
        runner = cost_service.make_cost_runner(
            project_dir=project_dir,
            cost_kind=cost_kind,
            payload=payload_dict,
            software_version=str(__version__),
            software_commit=_resolve_software_commit(),
        )
        runtime = get_shared_runtime(root)
        request_ref = runtime.start(
            intent_id=intent_id,
            params={
                "project_ref": project_ref,
                "cost_kind": cost_kind,
                "payload": payload_dict,
            },
            params_hash=params_hash,
            runner=runner,
            allow_unknown=intent_is_new,
        )
        return StartHandle(request_ref=request_ref, run_ref=request_ref)

    def list_coefficient_summaries(self, project_ref: str) -> list[dict[str, Any]]:
        """The read-only coefficient-freeze listing for one project
        (Step 3 S0): zero or one row (a project binds at most ONE formal
        coefficient); a corrupt freeze becomes an honest invalid
        summary instead of a silent gap."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.capacity_coefficient_freeze_service import (
            read_coefficient_freeze,
        )

        record = read_coefficient_freeze(root / project_ref)
        if record is None:
            return []
        if record.get("invalid"):
            return [
                {
                    "freeze_ref": record.get("freeze_ref"),
                    "invalid": True,
                    "invalid_reason": record.get("invalid_reason"),
                }
            ]
        manifest = record["manifest"]
        return [
            {
                "freeze_ref": manifest.get("freeze_ref"),
                "coefficient": manifest.get("coefficient"),
                "formula_version": manifest.get("formula_version"),
                "caliber": manifest.get("caliber"),
                "target_year": manifest.get("target_year"),
                "confirmer": manifest.get("confirmer"),
                "created_at": manifest.get("frozen_at"),
                "invalid": False,
            }
        ]

    def list_settlement_summaries(self, project_ref: str) -> list[dict[str, Any]]:
        """The read-only settlement annual-summary listing for one
        project (Step 3 S3 收尾): zero or one row (a settlement year
        binds ONE immutable annual summary); a corrupt record becomes
        an honest invalid summary instead of a silent gap."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.settlement_annual_service import (
            read_annual_summary_record,
        )

        record = read_annual_summary_record(root / project_ref)
        if record is None:
            return []
        if record.get("invalid"):
            return [
                {
                    "annual_ref": None,
                    "invalid": True,
                    "invalid_reason": record.get("invalid_reason"),
                }
            ]
        annual = record["annual"]
        return [
            {
                "annual_ref": f"annual-{annual.get('settlement_year')}",
                "settlement_year": annual.get("settlement_year"),
                "total_paid": annual.get("total_paid"),
                "total_deduction": annual.get("total_deduction"),
                "coefficient": annual.get("coefficient"),
                "formula_version": annual.get("formula_version"),
                "published_at": annual.get("published_at"),
                "invalid": False,
            }
        ]

    def list_market_summaries(self, project_ref: str) -> list[dict[str, Any]]:
        """The read-only market-clearing listing for one project (Q-M6
        2026-10-11): zero or one row (a market scenario binds at most ONE
        frozen clearing); a corrupt record becomes an honest invalid
        summary instead of a silent gap."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application.market_service import read_clearing_result

        record = read_clearing_result(root / project_ref)
        if record is None:
            return []
        if record.get("invalid"):
            return [
                {
                    "clearing_ref": None,
                    "invalid": True,
                    "invalid_reason": record.get("invalid_reason"),
                }
            ]
        manifest = record["manifest"]
        return [
            {
                "clearing_ref": manifest.get("clearing_ref"),
                "rule_version": manifest.get("rule_version"),
                "result_status": manifest.get("result_status"),
                "clearing_price": manifest.get("clearing_price"),
                "procured_mw": manifest.get("procured_mw"),
                "shortfall_mw": manifest.get("shortfall_mw"),
                "target_mw": manifest.get("target_mw"),
                "frozen_at": manifest.get("frozen_at"),
                "invalid": False,
            }
        ]

    def list_cost_run_summaries(self, project_ref: str) -> list[dict[str, Any]]:
        """The read-only cost-run listing for one project (honest
        invalid entries; deterministic order)."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application import cost_service

        return cost_service.list_cost_runs(root / project_ref)

    def get_cost_run_projection(self, project_ref: str, run_ref: str) -> dict[str, Any]:
        """One cost-run record projected for the pages: manifest facts
        plus the raw domain result (the page renders the projection;
        it never reads run files itself)."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application import cost_service

        try:
            record = cost_service.read_cost_run(root / project_ref, run_ref)
        except cost_service.CostServiceError as exc:
            raise WorkbenchError(exc.kind, exc.message) from None
        return record

    def export_cost_report(self, project_ref: str, run_ref: str) -> dict[str, Any]:
        """Publish the cost business report (Excel projection) for one
        completed cost run through the SAME reporting discipline
        (manifest last, hash verification, immutable, same-identity
        replay). Never touches the run record or the input."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(run_ref)
        from capacity_credit.application import cost_service
        from capacity_credit.reporting.cost_report import (
            CostReportError,
            export_cost_report as render_cost_report,
        )

        try:
            record = cost_service.read_cost_run(root / project_ref, run_ref)
        except cost_service.CostServiceError as exc:
            raise WorkbenchError(exc.kind, exc.message) from None
        manifest = record["manifest"]
        if manifest.get("status") != "completed" or record["result"] is None:
            raise WorkbenchError(
                _WORKBENCH_INPUT, "成本运行未完成，不能导出报告"
            )
        export_dir = root / "reports" / "cost" / project_ref / run_ref / "export"
        try:
            return render_cost_report(
                manifest=manifest,
                input_payload=record["input"],
                result=record["result"],
                output_dir=export_dir,
            )
        except CostReportError as exc:
            raise WorkbenchError(_WORKBENCH_INPUT, str(exc)) from None

    def get_cost_report_downloads(self, project_ref: str, run_ref: str) -> dict[str, Any]:
        """The verified download set for one cost run's published
        report (hash re-checked against the manifest; no export is
        triggered here)."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(run_ref)
        import hashlib

        export_dir = root / "reports" / "cost" / project_ref / run_ref / "export"
        manifest_path = export_dir / "cost_report_manifest.json"
        if not manifest_path.is_file():
            return {"available": False, "reason": "尚未导出报告", "files": []}
        import json

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = []
            for entry in manifest.get("files", []):
                name = str(entry.get("name", ""))
                path = export_dir / name
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest != entry.get("sha256"):
                    raise ValueError(f"文件哈希不一致：{name}")
                files.append(
                    {
                        "name": name,
                        "bytes": data,
                        "sha256": digest,
                        "content_type": str(
                            entry.get("content_type", "application/octet-stream")
                        ),
                    }
                )
        except (OSError, ValueError) as exc:
            return {"available": False, "reason": str(exc), "files": []}
        return {"available": True, "reason": None, "files": files}

    def import_inputs(
        self,
        *,
        project_ref: str,
        files: Mapping[str, tuple[str, bytes]],
        declared_grade: str,
        created_by: str,
        declared_total_capacity_mw: float | None = None,
        ledger_total_tolerance_percent: float = 1.0,
    ) -> dict[str, Any]:
        """THE single upload chain (V1-T2-B): the ACTUAL uploaded bytes
        go through the controlled staging, the EXISTING readers, the
        EXISTING quality validator and the EXISTING register_import of
        the selected project; the result is an honest projection whose
        three states stay separate (parse_error with no batch /
        validation_failed registered as failed / passed registered as
        validated). A registered upload is NEVER an approval or a
        freeze, and the display file names never become paths.

        - workspace and project reference re-validated at this boundary;
        - a corrupt project cannot receive uploads;
        - the DECLARED resource composition (registry) is enforced
          against the actually uploaded tables - conflicts block, no
          table is ever silently dropped;
        - unknown slots raise invalid_input BEFORE any processing."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        summary = self.get_project_summary(project_ref)
        if summary is None:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"项目不存在或不是本工作区的正式项目：{project_ref}",
            )
        if summary["invalid"]:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"项目条目损坏，不能接收上传：{project_ref}（{summary['invalid_reason']}）",
            )
        from capacity_credit.application.import_submission import (
            submit_upload_import,
        )

        try:
            return submit_upload_import(
                project_dir=root / project_ref,
                files=dict(files),
                declared_grade=declared_grade,
                created_by=created_by,
                declared_composition=self.get_declared_composition(project_ref),
                declared_total_capacity_mw=declared_total_capacity_mw,
                ledger_total_tolerance_percent=ledger_total_tolerance_percent,
            )
        except WorkbenchError:
            raise
        except ValueError as exc:
            # UploadRejection and any value-level refusal keep their
            # honest message at the invalid_input boundary
            raise WorkbenchError(_WORKBENCH_INPUT, str(exc)) from None

    def get_template_download(self, table_key: str) -> tuple[str, bytes]:
        """The downloadable fillable CSV for one stable table key:
        (file_name, utf-8-sig bytes). Read-only - downloading never
        creates batches, approvals, freezes or runs."""
        _validate_root(self._root)
        from capacity_credit.data.template_catalog import render_csv_bytes

        return render_csv_bytes(table_key)

    def get_weather_year_coverage(
        self,
        project_ref: str,
        *,
        import_batch_id: str,
        preview_equal_weights: bool = False,
    ) -> dict[str, Any]:
        """W1 (three-slice plan 2026-09-19): the honest weather-year
        coverage projection for ONE frozen batch - which years the
        load/renewable tables actually cover, hour by hour, which years
        are complete, the registered weight sets, and (on request) an
        equal-weight preview computed by the SAME ``default_equal_weights``
        generator the import chain validates - the frontend never
        invents 1/N.

        Zero methodology change: this projects what the frozen tables
        carry. ``declared_years`` is the union of years seen in the
        mandatory load table (the load curve is the weather-year
        bottleneck by contract); a year whose renewable rows are absent
        or short is ``complete=false``, never silently complete.
        """
        import csv

        from capacity_credit.data.validation import default_equal_weights

        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(import_batch_id)
        project_dir = root / project_ref
        import_service = ImportService(project_dir)
        frozen = import_service.frozen_files(import_batch_id)

        def _years_and_hours(rel_path: str) -> dict[int, int]:
            path = frozen.get(rel_path)
            if path is None:
                return {}
            counts: dict[int, int] = {}
            with open(path, encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    raw = (row.get("weather_year") or "").strip()
                    if not raw:
                        continue
                    try:
                        year = int(float(raw))
                    except ValueError:
                        continue
                    counts[year] = counts.get(year, 0) + 1
            return counts

        load_hours = _years_and_hours("data/load_hourly.csv")
        renewable_hours = _years_and_hours("data/renewable_hourly.csv")
        declared_years = sorted(load_hours)
        from capacity_credit.domain.time_axis import expected_hours_for_year

        # W3 (three-slice plan section 4.1): the year card's stress-test
        # row state needs the ROLE of each year - a read-only projection
        # of the year_roles carried by the scenario banks BOUND to this
        # batch (never guessed at the UI layer). No bound bank -> None
        # (honest absence); banks disagreeing -> "mixed" (the run's own
        # config stays the authority). Legacy banks without a recorded
        # source binding never leak their roles into this batch.
        year_roles: dict[int, set[str]] = {}
        banks_dir = project_dir / "artifacts" / "scenario_banks"
        if banks_dir.is_dir():
            from capacity_credit.scenarios.scenario_bank import (
                open_scenario_bank,
            )

            for entry in sorted(banks_dir.iterdir()):
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                try:
                    bank = open_scenario_bank(entry)
                except Exception:  # noqa: BLE001 - a corrupt bank is skipped
                    continue
                binding = bank.index.get("source_binding")
                if not isinstance(binding, dict) or str(
                    binding.get("import_batch_id")
                ) != str(import_batch_id):
                    continue
                roles = bank.index.get("year_roles")
                if not isinstance(roles, dict):
                    continue
                for raw_year, raw_role in roles.items():
                    try:
                        role_year = int(raw_year)
                    except (TypeError, ValueError):
                        continue
                    year_roles.setdefault(role_year, set()).add(
                        str(raw_role)
                    )

        def _projected_role(year: int) -> str | None:
            roles = year_roles.get(year)
            if not roles:
                return None
            if len(roles) > 1:
                return "mixed"
            return next(iter(roles))

        years = []
        for year in declared_years:
            expected = expected_hours_for_year(year)
            load_count = load_hours.get(year, 0)
            renewable_count = renewable_hours.get(year, 0)
            years.append(
                {
                    "year": year,
                    "load_hours": load_count,
                    "renewable_hours": renewable_count,
                    "expected_hours": expected,
                    "complete": (
                        load_count == expected and renewable_count == expected
                    ),
                    "role": _projected_role(year),
                }
            )
        weight_sets: list[dict[str, Any]] = []
        weights_path = frozen.get("data/weather_year_weight_sets.csv")
        if weights_path is not None:
            grouped: dict[str, list[tuple[int, float]]] = {}
            with open(weights_path, encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    set_id = (row.get("weight_set_id") or "").strip()
                    if not set_id:
                        continue
                    try:
                        year = int(float(row.get("weather_year") or ""))
                        weight = float(row.get("weight") or 0.0)
                    except ValueError:
                        continue
                    grouped.setdefault(set_id, []).append((year, weight))
            for set_id, entries in sorted(grouped.items()):
                total = sum(weight for _, weight in entries)
                weight_sets.append(
                    {
                        "weight_set_id": set_id,
                        "years": sorted(year for year, _ in entries),
                        "normalized": abs(total - 1.0) <= 1e-9,
                        # W3 (section 4.2): the per-year weight VALUES the
                        # share sub-card renders (「2023年 占 20%」) and sums
                        # for the 100% hint - the frontend never re-reads
                        # the CSV nor invents 1/N
                        "weights": {
                            year: weight for year, weight in entries
                        },
                    }
                )
        result: dict[str, Any] = {
            "import_batch_id": import_batch_id,
            "declared_years": declared_years,
            "years": years,
            "weight_sets": weight_sets,
        }
        if preview_equal_weights:
            result["equal_weight_preview"] = default_equal_weights(
                declared_years
            ).to_dict("records")
        return result

    def get_run_seasonal_metrics(
        self,
        project_ref: str,
        run_ref: str,
        *,
        heating_months: Any,
        import_batch_id: str,
        weight_set_id: str,
        sample_count: int,
    ) -> dict[str, Any]:
        """SL-01 wiring: one completed run's SEGMENT metrics (heating /
        non-heating LOLE days per year), computed from the run's OWN
        persisted deficit-event evidence with the T06 aggregation
        (per-year sample mean, then year weights).

        Honesty rules: the projection PREFERS the run's own
        ``seasonal_dates.json`` (per-sample monthly deficit-date counts
        computed at publication from the FULL untruncated event set).
        Older runs without it fall back to the capped deficit_events
        evidence - and a TRUNCATED evidence refuses, because capped
        events cannot honestly feed a seasonal calibration round. A
        missing/illegal weight set, or a weight year the run never
        simulated, refuses. The annual metric the run already carries
        is never touched; this projection ONLY adds the segments."""
        from capacity_credit.application import calibration_service

        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(run_ref)
        safe_filename_component(import_batch_id)
        run_dir = root / project_ref / "runs" / run_ref
        sample_count: int | None = None
        counts: dict[tuple[str, int], dict[str, int]] = {}
        seasonal_path = run_dir / "seasonal_dates.json"
        if seasonal_path.is_file():
            try:
                payload = json.loads(
                    seasonal_path.read_text(encoding="utf-8")
                )
                sample_count = int(payload.get("sample_count") or 0)
                for key, by_month in (
                    payload.get("counts") or {}
                ).items():
                    sample, _, year = str(key).rpartition("|")
                    segment_counts = {"heating": 0, "non_heating": 0}
                    for month, value in by_month.items():
                        segment = (
                            "heating"
                            if int(month) in set(heating_months)
                            else "non_heating"
                        )
                        segment_counts[segment] += int(value)
                    counts[(sample, int(year))] = segment_counts
            except (OSError, ValueError) as exc:
                raise WorkbenchError(
                    _WORKBENCH_INPUT,
                    f"季节日期证据不可读：{exc}",
                ) from None
        else:
            evidence_path = run_dir / "deficit_events.json"
            if not evidence_path.is_file():
                raise WorkbenchError(
                    _WORKBENCH_INPUT,
                    f"运行 {run_ref} 没有缺电事件证据"
                    "（deficit_events.json）：季节归属无从计算",
                )
            try:
                payload = json.loads(
                    evidence_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise WorkbenchError(
                    _WORKBENCH_INPUT,
                    f"缺电事件证据不可读：{exc}",
                ) from None
            if payload.get("truncated"):
                raise WorkbenchError(
                    _WORKBENCH_INPUT,
                    "缺电事件证据被截断（max_events）：截断后的事件无法"
                    "诚实支撑季节归属，请增大单轮模拟精度或减少事件后"
                    "重测该档",
                )
            events = payload.get("events") or []
            counts = calibration_service.seasonal_segment_days(
                events, heating_months=heating_months
            )
            sample_count = int(payload.get("sample_count") or 0)
        import_service = ImportService(root / project_ref)
        frozen = import_service.frozen_files(import_batch_id)
        weights_path = frozen.get("data/weather_year_weight_sets.csv")
        if weights_path is None:
            raise WorkbenchError(
                _WORKBENCH_INPUT, "冻结数据缺少气象年权重表"
            )
        import csv

        weights: dict[int, float] = {}
        with open(weights_path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if (row.get("weight_set_id") or "").strip() != str(
                    weight_set_id
                ):
                    continue
                try:
                    year = int(float(row.get("weather_year") or ""))
                    weight = float(row.get("weight") or 0.0)
                except ValueError:
                    continue
                weights[year] = weight
        if not weights:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"冻结权重表中没有权重集 {weight_set_id!r}",
            )
        if not counts:
            # a ZERO-LOSS run: every sample/year pair has zero deficit
            # days in both segments - an honest all-zero projection,
            # never an aggregate refusal
            aggregated = {"heating": 0.0, "non_heating": 0.0}
        else:
            try:
                aggregated = (
                    calibration_service.seasonal_aggregate_segment_lole(
                        counts,
                        weights,
                        samples_by_year={
                            year: sample_count for year in weights
                        },
                    )
                )
            except calibration_service.CalibrationError as exc:
                raise WorkbenchError(exc.kind, exc.message) from None
        return {
            "run_ref": run_ref,
            "heating": {"lole_days_per_year": aggregated["heating"]},
            "non_heating": {"lole_days_per_year": aggregated["non_heating"]},
            "events_used": payload.get("event_count"),
            "truncated": False,
            "weights": weights,
        }

    def compute_seasonal_split(
        self,
        project_ref: str,
        run_ref: str,
        *,
        annual_target: float,
        heating_months: Any,
        import_batch_id: str,
        weight_set_id: str,
        sample_count: int,
    ) -> dict[str, Any]:
        """SL-01 signed default split (draft section 7 item 2): cut the
        annual target along the BASELINE run's weighted deficit-day
        distribution (basis=baseline_risk_share, recorded). A
        zero-loss baseline has no risk to split - the honest refusal
        (the zero-loss calibration case, again). The pair ALWAYS sums
        back to the annual target, so the batch-creation annual
        consistency check passes by construction."""
        metrics = self.get_run_seasonal_metrics(
            project_ref,
            run_ref,
            heating_months=heating_months,
            import_batch_id=import_batch_id,
            weight_set_id=weight_set_id,
            sample_count=sample_count,
        )
        heating = float(metrics["heating"]["lole_days_per_year"])
        non = float(metrics["non_heating"]["lole_days_per_year"])
        total = heating + non
        if total <= 0:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                "基线运行无任何缺电日（零损失）：季节拆解没有可分配的风险，"
                "拒绝发明拆解比例",
            )
        target = float(annual_target)
        if not math.isfinite(target) or target <= 0:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"年度目标必须为大于0的有限数，收到 {annual_target!r}",
            )
        heating_share = heating / total
        return {
            "basis": "baseline_risk_share",
            "annual_target": target,
            "shares": {
                "heating": heating_share,
                "non_heating": 1.0 - heating_share,
            },
            "heating_target": target * heating_share,
            "non_heating_target": target * (1.0 - heating_share),
        }

    def get_import_summary(
        self, project_ref: str, import_batch_id: str
    ) -> dict[str, Any]:
        """The read-only confirmation projection for one import batch
        (V1-T2-C): the formal state verbatim, the immutable grade
        anchor, the registered file inventory with hashes, and a
        ``summary_hash`` that binds EXACTLY this batch identity - the
        confirmation action must present the same hash or be refused
        (the user confirms a version, not a moving target)."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        safe_filename_component(import_batch_id)
        project_dir = root / project_ref
        import_service = ImportService(project_dir)
        state = import_service.current_state(import_batch_id)
        file_entries: list[dict[str, Any]] = []
        evidence_error: str | None = None
        try:
            manifest = import_service.verify_registered_integrity(import_batch_id)
            file_entries = [
                {
                    "relative_path": entry["relative_path"],
                    "size_bytes": entry["size_bytes"],
                    "sha256": entry["sha256"],
                }
                for entry in manifest.get("files", [])
            ]
        except ServiceError as exc:
            # the registered content no longer matches its manifest: the
            # confirmation cannot bind such a batch - an honest error, no
            # hash is offered for confirmation
            evidence_error = f"批次内容完整性校验失败：{exc.message}（{exc.code}）"
        grades = dict(state.grades or {})
        if evidence_error is not None:
            summary_hash = None
        else:
            summary_hash = hash_payload(
                {
                    "import_batch_id": import_batch_id,
                    "dataset_id": state.dataset_id,
                    "data_version_id": state.data_version_id,
                    "data_version_hash": state.data_version_hash,
                    "grades": grades,
                    "files": file_entries,
                }
            )
        return {
            "import_batch_id": import_batch_id,
            "dataset_id": state.dataset_id,
            "data_version_id": state.data_version_id,
            "data_version_hash": state.data_version_hash,
            "validation_status": state.validation_status,
            "data_version_status": state.data_version_status,
            "import_review_status": state.import_review_status,
            "approved_data_version_id": state.approved_data_version_id,
            "grades": grades,
            "files": file_entries,
            "summary_hash": summary_hash,
            "evidence_error": evidence_error,
        }

    def derive_data_version(
        self,
        project_ref: str,
        *,
        baseline_batch_id: str,
        rule_id: str,
        params: Mapping[str, Any],
        declared_grade: str,
    ) -> dict[str, Any]:
        """B1 slice 2: create ONE derived data version from a FROZEN
        baseline using a user-confirmed derivation rule, and register it
        through the EXISTING import chain.

        It deliberately STOPS at 待确认: DR-01 requires a human to approve
        and freeze every derived point, so this never approves, never
        freezes and never fabricates a data version. The derivation
        record (rule id/version, params, changed slots, formula, the
        DR-07 attestation and the DR-05 penetration caliber) is returned
        for the batch object and the report to carry as the assumption
        register.
        """
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        from capacity_credit.application import derivation_service
        from capacity_credit.application.p1_workflow import _TABLE_FILES

        project_dir = root / project_ref
        import_service = ImportService(project_dir)
        try:
            frozen = import_service.frozen_files(baseline_batch_id)
        except Exception as exc:  # noqa: BLE001 - honest entry refusal
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"基线批次不可读（须为已冻结批次）：{exc}",
            ) from None
        files: dict[str, bytes] = {}
        for slot, rel in _TABLE_FILES.items():
            path = frozen.get(rel)
            if path is None:
                continue
            files[slot] = Path(path).read_bytes()
        if not files:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"基线批次 {baseline_batch_id} 没有可读的冻结表",
            )
        try:
            derived, record = derivation_service.derive(
                rule_id, files, dict(params)
            )
        except derivation_service.DerivationError as exc:
            raise WorkbenchError(exc.kind, exc.message) from None
        result = self.import_inputs(
            project_ref=project_ref,
            files={
                slot: (f"{slot}.csv", data) for slot, data in derived.items()
            },
            declared_grade=declared_grade,
            created_by=(
                f"derivation:{record['rule_id']}@{record['rule_version']}"
            ),
        )
        return {
            "import_result": result,
            "derivation": record,
            "awaiting_confirmation": True,
            "baseline_batch_id": baseline_batch_id,
        }

    def confirm_and_freeze(
        self,
        *,
        project_ref: str,
        import_batch_id: str,
        request_id: str,
        reviewed_by: str,
        review_comment: str,
        confirmed_summary_hash: str,
    ) -> dict[str, Any]:
        """The confirmation action (V1-T2-C): the user confirmed the
        summary bound to ``confirmed_summary_hash``; the CURRENT batch
        identity is re-computed and must match, otherwise the stale
        confirmation is refused (never an approval of a changed batch).
        The ORIGINAL approve and freeze services run in order; a freeze
        failure after a successful approval returns the honest partial
        state (approved, NOT frozen) - never a fabricated rollback.
        Idempotent per request_id through the original services."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        if not isinstance(request_id, str) or not request_id.strip():
            raise WorkbenchError(_WORKBENCH_INPUT, "request_id 必须为非空字符串")
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            raise WorkbenchError(
                _WORKBENCH_INPUT, "审批人（reviewed_by）必须为非空文本"
            )
        if not isinstance(review_comment, str) or not review_comment.strip():
            raise WorkbenchError(
                _WORKBENCH_INPUT, "审批说明（review_comment）必须为非空文本"
            )
        summary = self.get_import_summary(project_ref, import_batch_id)
        if summary.get("evidence_error") or summary["summary_hash"] is None:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                "批次内容完整性校验失败，不能按当前摘要批准："
                f"{summary.get('evidence_error')}",
            )
        if summary["summary_hash"] != confirmed_summary_hash:
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                "确认摘要与当前批次身份不一致：批次在确认后已变化，"
                "请重新核对后再批准（不会按旧摘要批准）",
            )
        project_dir = root / project_ref
        import_service = ImportService(project_dir)
        try:
            import_service.approve(
                request_id=request_id,
                import_batch_id=import_batch_id,
                reviewed_by=reviewed_by,
                review_comment=review_comment,
            )
        except ServiceError as exc:
            if exc.code == "GATE_INVALID_STATE" and "approved" in exc.message:
                # DR-01a/b: a batch that auto-approved (e.g. G0 teaching
                # data) is already in the right state - proceed to freeze
                # instead of refusing. Other gate failures still refuse.
                pass
            else:
                raise WorkbenchError(
                    _WORKBENCH_INPUT, f"批准失败：{exc.message}（{exc.code}）"
                ) from None
        try:
            frozen = import_service.freeze(
                request_id=f"{request_id}-freeze",
                import_batch_id=import_batch_id,
            )
        except ServiceError as exc:
            return {
                "approved": True,
                "frozen": False,
                "import_batch_id": import_batch_id,
                "data_version_id": summary["data_version_id"],
                "data_version_hash": summary["data_version_hash"],
                "freeze_error": f"{exc.message}（{exc.code}）",
            }
        # the freeze contract itself verified integrity before publishing;
        # read the frozen facts back once more for the honest projection
        return {
            "approved": True,
            "frozen": True,
            "import_batch_id": import_batch_id,
            "data_version_id": frozen.data_version_id,
            "data_version_hash": frozen.data_version_hash,
            "freeze_error": None,
        }

    def prepare_scenario_bank(
        self,
        *,
        project_ref: str,
        import_batch_id: str,
        request_id: str,
        root_seed: int,
        samples: int = 30,
        weight_set_id: str = "w1",
        timezone: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        """The scenario-bank preparation seam (V1-T2-C): the EXISTING
        p1_workflow.scenario_build runs against the frozen data (its own
        frozen gates and idempotent request). The user never handles
        internal directory paths - the bank lands in the project's
        established artifacts location and shows up in get_run_options."""
        root = _validate_root(self._root)
        _validate_project_ref(project_ref)
        if not isinstance(root_seed, int) or isinstance(root_seed, bool):
            raise WorkbenchError(
                _WORKBENCH_INPUT, "根种子必须为整数（由用户显式选择）"
            )
        if not isinstance(samples, int) or isinstance(samples, bool) or samples <= 0:
            raise WorkbenchError(_WORKBENCH_INPUT, "样本数必须为正整数")
        data_version_id: str | None = None
        try:
            state = ImportService(root / project_ref).current_state(import_batch_id)
            data_version_id = state.data_version_id
        except Exception as exc:  # noqa: BLE001 - ServiceError and contract
            # errors alike become the honest invalid_input boundary
            raise WorkbenchError(
                _WORKBENCH_INPUT,
                f"数据引用无效或不属于本项目：{import_batch_id}（{exc}）",
            ) from None
        from capacity_credit.application.p1_workflow import scenario_build

        result = scenario_build(
            project_dir=root / project_ref,
            import_batch_id=import_batch_id,
            data_version_id=data_version_id,
            root_seed=root_seed,
            timezone=timezone,
            samples=samples,
            weight_set_id=weight_set_id,
            request_id=request_id,
        )
        return {
            "scenario_bank_id": result["scenario_bank_id"],
            "weather_years": result["weather_years"],
            "root_seed": result["root_seed"],
        }

    def get_execution_snapshot(self, request_ref: str) -> ExecutionSnapshot:
        """The independent, read-only execution projection for one
        request ref (the shared per-workspace runtime's snapshot; memory
        projections only - never a business state machine, never a
        modifiable internal dict)."""
        return get_shared_runtime(self._root).snapshot(request_ref)


__all__ = [
    "ExecutionSnapshot",
    "StartHandle",
    "WorkbenchError",
    "WorkbenchService",
]
