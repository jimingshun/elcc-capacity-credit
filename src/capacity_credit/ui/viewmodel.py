"""T26-A stateless UI view-model layer (the thin UI facade).

Formal application services return facts -> these PURE projections turn
them into page models. The view-model:

- only groups, formats and projects data returned by the formal services;
- keeps job state, run lifecycle, result_status and control-request
  status strictly separate (never inferred from each other);
- gates the recovery action strictly by the formal recovery_state;
- maps stable error codes to "business explanation + original code"
  (the original code is never swallowed);
- never writes files, never reads project/job/run directories directly,
  never calls compute kernels/solvers/engines, never calls write
  operations (create_job/launch/stop/resume/recover), never imports
  Streamlit and never keeps business state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypedDict

from capacity_credit.application.p1_workflow import ProjectSummary
from capacity_credit.application.run_service import RunSummary
from capacity_credit.application.worker import JobSummary

# st.caption / st.error render Markdown: a raw backslash before one of
# these characters is consumed as an escape sequence, so a Windows path
# like ...tool\.worktrees would DISPLAY as ...tool.worktrees (a wrong
# path). Display text carrying paths or raw reasons is escaped first.
_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<])")


def markdown_safe_text(text: str) -> str:
    """The markdown-escaped DISPLAY form of one text (pure projection).

    The escaping exists only so the rendered caption/error shows the
    text verbatim; it never changes the stored reason or the machine
    result (the verbatim reason stays in the error object/viewmodel)."""
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", text)


class StatusText(TypedDict):
    """Business wording plus a display tone; the machine state is always
    carried separately on the row (never merged)."""

    label: str
    tone: str


_JOB_STATE_TEXT: dict[str, StatusText] = {
    "queued": {"label": "排队中", "tone": "neutral"},
    "running": {"label": "运行中", "tone": "info"},
    "completed": {"label": "已完成", "tone": "success"},
    "failed": {"label": "失败", "tone": "error"},
    "cancelled": {"label": "已安全停止", "tone": "warning"},
    # a blocked job is a single-flight conflict - NEVER a failed run
    "blocked": {"label": "已有相同任务在运行（单飞阻断）", "tone": "warning"},
}

_RUN_LIFECYCLE_TEXT: dict[str, StatusText] = {
    "created": {"label": "已创建", "tone": "neutral"},
    "queued": {"label": "排队中", "tone": "neutral"},
    "running": {"label": "运行中", "tone": "info"},
    "stopping": {"label": "正在安全停止", "tone": "warning"},
    "completed": {"label": "已完成", "tone": "success"},
    "failed": {"label": "失败", "tone": "error"},
    "cancelled": {"label": "已取消", "tone": "warning"},
}

_REQUEST_STATUS_TEXT: dict[str, StatusText] = {
    "pending": {"label": "待处理", "tone": "neutral"},
    "accepted": {"label": "已受理", "tone": "info"},
    "completed": {"label": "已完成", "tone": "success"},
    "rejected": {"label": "已拒绝", "tone": "error"},
}

_RESULT_STATUS_TEXT: dict[str, StatusText] = {
    "valid": {"label": "有效", "tone": "success"},
    "valid_with_warning": {"label": "有效（有警告）", "tone": "warning"},
    "low_resolution": {"label": "低分辨率结果", "tone": "warning"},
    "not_converged": {"label": "未收敛", "tone": "warning"},
    "invalid_method": {"label": "方法无效", "tone": "error"},
    "invalid_data": {"label": "数据无效", "tone": "error"},
}

# stable worker/control-plane error codes -> business explanation (the
# original code is always preserved alongside)
_ERROR_EXPLANATIONS: dict[str, str] = {
    "WORKER_RESUME_OWNER_ACTIVE": "已有相同任务的运行正在进行，恢复被拒绝",
    "WORKER_RESUME_STATE_NOT_ALLOWED": "当前运行状态不允许恢复",
    "WORKER_RESUME_CHECKPOINT_MISSING": "没有可用于恢复的检查点",
    "WORKER_RESUME_IDENTITY_MISMATCH": "恢复身份与冻结记录不一致",
    "WORKER_RESUME_UNSUPPORTED_FOR_TASK": "该任务类型不支持恢复",
    "WORKER_RUN_IN_PROGRESS": "已有相同任务在运行",
    "WORKER_LAUNCH_FAILED": "任务启动失败",
    "WORKER_TERMINAL_INTENT_MISSING": "缺少终态恢复意图记录，自动恢复不可用，需人工证据处置",
    "WORKER_TERMINAL_INTENT_INVALID": "终态恢复意图记录无效，自动恢复不可用，需人工证据处置",
    "WORKER_TERMINAL_INTENT_PERSIST_FAILED": "终态意图写入失败，自动恢复不可用，需人工证据处置",
    "WORKER_TERMINAL_STATE_CONFLICT": "恢复目标与既有记录冲突",
    "WORKER_TERMINAL_PUBLISH_FAILED": "终态发布持久化失败",
    "WORKER_TERMINAL_INTENT_MARKER_UPDATE_FAILED": "终态标记更新失败（终态本身已成立）",
    "WORKER_CONTROL_GATE_BUSY": "控制面正忙，请稍后重试",
    "CONTROL_REQUEST_IO_BUSY": "控制请求文件正被占用，请稍后重试",
    "CONTROL_REQUEST_ACCESS_DENIED": "控制请求文件访问被拒绝",
    "CONTROL_REQUEST_INVALID": "控制请求记录损坏或违反合同",
    "CONTROL_REQUEST_NOT_FOUND": "控制请求不存在",
}


class ProjectRow(ProjectSummary):
    """ProjectSummary + derived display fields (read-only projection)."""

    status_label: str
    status_tone: str
    can_open: bool


class JobRow(JobSummary):
    """JobSummary + derived display fields (read-only projection).

    Display-only: write-action visibility is NEVER derived from the
    summary alone - that is the separate job_actions projection, which
    requires the formal worker_status evidence."""

    state_label: str
    state_tone: str
    diagnostic_label: str | None
    can_open: bool


class JobActions(TypedDict):
    """Write-action visibility derived ONLY from the formal worker_status
    evidence (never from the summary alone)."""

    can_stop: bool
    can_resume: bool


class RunRow(RunSummary):
    """RunSummary + derived display fields (read-only projection)."""

    lifecycle_label: str
    lifecycle_tone: str
    result_label: str
    can_open: bool


class RequestRow(TypedDict):
    """A control-request row (its own status, never a job state). A
    corrupt entry (the ControlRequestStore invalid shape) is surfaced
    EXPLICITLY: invalid=True, the evidence path preserved, never
    normalised into an ordinary request."""

    request_id: str
    action: str
    request_status: str
    status_label: str
    status_tone: str
    requested_by: str
    requested_at: str
    invalid: bool
    invalid_reason: str | None
    evidence_path: str | None


class RecoveryAction(TypedDict):
    """The terminal-recovery action, gated strictly by the formal
    recovery_state."""

    visible: bool
    diagnostic_code: str | None
    note: str


class ErrorView(TypedDict):
    """Business explanation PLUS the original stable error code."""

    message: str
    code: str


def _text(mapping: dict[str, StatusText], value: str, fallback_label: str) -> StatusText:
    known = mapping.get(value)
    if known is not None:
        return known
    return {"label": f"{fallback_label}：{value}", "tone": "neutral"}


def project_row(summary: ProjectSummary) -> ProjectRow:
    """Project one ProjectSummary into a read-only ProjectRow (the input
    is never mutated; the output is a fresh dict)."""
    invalid = bool(summary["invalid"])
    if invalid:
        status: StatusText = {"label": "条目损坏", "tone": "error"}
    elif summary["frozen"]:
        status = {"label": "数据已冻结", "tone": "success"}
    elif (
        summary["latest_batch_state"] is None
        and summary["updated_at"] is None
    ):
        # a REGISTERED project without any import batch (V1-T2-A): it is
        # 待导入 - never "损坏" and never "未冻结" (there is no frozen
        # data to speak of yet)
        status = {"label": "待导入", "tone": "neutral"}
    else:
        status = {"label": "数据未冻结", "tone": "warning"}
    return ProjectRow(
        **summary,
        status_label=status["label"],
        status_tone=status["tone"],
        can_open=not invalid,
    )


def job_row(summary: JobSummary) -> JobRow:
    """Project one JobSummary into a read-only JobRow (the input is never
    mutated). The blocked state keeps its single-flight semantics and is
    never mapped to a run failure."""
    invalid = bool(summary["invalid"])
    state = str(summary["state"])
    status = _text(_JOB_STATE_TEXT, state, "任务状态")
    diagnostic_code = summary["diagnostic_code"]
    diagnostic_label = (
        _ERROR_EXPLANATIONS.get(str(diagnostic_code)) if diagnostic_code else None
    )
    return JobRow(
        **summary,
        state_label=status["label"],
        state_tone=status["tone"],
        diagnostic_label=diagnostic_label,
        can_open=not invalid,
    )


def run_row(summary: RunSummary) -> RunRow:
    """Project one RunSummary into a read-only RunRow (the input is never
    mutated). result_status is displayed only when a formal legal result
    exists; a missing result is never shown as valid."""
    invalid = bool(summary["invalid"])
    lifecycle = str(summary["lifecycle_status"])
    lifecycle_text = _text(_RUN_LIFECYCLE_TEXT, lifecycle, "运行状态")
    result_status = summary["result_status"]
    if invalid:
        result_label = "条目损坏"
    elif result_status is None:
        result_label = "尚未产生正式结果"
    else:
        result_label = _text(_RESULT_STATUS_TEXT, result_status, "结果状态")["label"]
    return RunRow(
        **summary,
        lifecycle_label=lifecycle_text["label"],
        lifecycle_tone=lifecycle_text["tone"],
        result_label=result_label,
        can_open=not invalid,
    )


def request_row(payload: dict[str, Any]) -> RequestRow:
    """Project one control-request record into its OWN status row (never
    merged into a job or run state). A corrupt entry keeps its
    request_id and evidence path, is marked invalid with an honest
    reason, and is never laundered into a legal-looking ordinary
    request."""
    if payload.get("invalid"):
        evidence_path = payload.get("path")
        return RequestRow(
            request_id=str(payload.get("request_id", "")),
            action="",
            request_status="",
            status_label="控制请求记录损坏",
            status_tone="error",
            requested_by="",
            requested_at="",
            invalid=True,
            invalid_reason="控制请求记录损坏或违反合同（原始证据保留，不修改不删除）",
            evidence_path=str(evidence_path) if evidence_path else None,
        )
    status = _text(_REQUEST_STATUS_TEXT, str(payload.get("request_status", "")), "请求状态")
    return RequestRow(
        request_id=str(payload.get("request_id", "")),
        action=str(payload.get("action", "")),
        request_status=str(payload.get("request_status", "")),
        status_label=status["label"],
        status_tone=status["tone"],
        requested_by=str(payload.get("requested_by", "")),
        requested_at=str(payload.get("requested_at", "")),
        invalid=False,
        invalid_reason=None,
        evidence_path=None,
    )


def job_actions(
    summary: JobSummary, worker_status: Mapping[str, Any] | None
) -> JobActions:
    """Derive write-action visibility ONLY from the formal worker_status
    evidence (never from the summary alone): the status must belong to
    THIS job (job_id identity), and a missing/invalid evidence base is
    conservatively False.

    can_resume requires ALL of: a valid adequacy job still running, a
    non-empty run_id, identity-matched status, run lifecycle running, a
    checkpoint present, and neither the execution nor the task lock
    held. can_stop requires a valid running job, identity-matched
    status, no corrupt control request and no already-open stop request.
    The UI gate is never authoritative - the formal stop_job /
    resume_job services re-check on click."""
    no = JobActions(can_stop=False, can_resume=False)
    if summary["invalid"]:
        return no
    if worker_status is None:
        return no
    status_job = worker_status.get("job")
    if not isinstance(status_job, dict) or status_job.get("job_id") != summary["job_id"]:
        return no
    state = str(summary["state"])
    # can_stop: running job + STRUCTURALLY COMPLETE control-request
    # evidence (fail-closed: a missing/None/non-list control_requests, a
    # non-dict or incomplete entry, an invalid entry or an already-open
    # stop request all conservatively block - evidence gaps are never
    # degraded to an empty list and damaged entries are never skipped)
    requests_raw = worker_status.get("control_requests")
    requests_complete = isinstance(requests_raw, list)
    requests: list[Any] = requests_raw if isinstance(requests_raw, list) else []
    if requests_complete:
        for entry in requests:
            if not isinstance(entry, dict):
                requests_complete = False
                break
            if entry.get("invalid"):
                requests_complete = False
                break
            if not isinstance(entry.get("action"), str) or not isinstance(
                entry.get("request_status"), str
            ):
                requests_complete = False
                break
    open_or_corrupt = not requests_complete
    if requests_complete:
        for entry in requests:
            if entry.get("action") == "stop_safely" and entry.get("request_status") in (
                "pending",
                "accepted",
            ):
                open_or_corrupt = True
                break
    can_stop = state == "running" and not open_or_corrupt
    # can_resume: the full formal evidence chain
    lock_refs = worker_status.get("lock_refs")
    can_resume = (
        str(summary["task"]) == "adequacy"
        and state == "running"
        and isinstance(summary["run_id"], str)
        and bool(summary["run_id"])
        and worker_status.get("run_lifecycle_status") == "running"
        and worker_status.get("checkpoint_present") is True
        and isinstance(lock_refs, dict)
        and lock_refs.get("execution_held") is False
        and lock_refs.get("task_held") is False
    )
    return JobActions(can_stop=can_stop, can_resume=can_resume)


def recovery_action(
    recovery_state: str | None, diagnostic_code: str | None
) -> RecoveryAction:
    """The terminal-recovery action is visible ONLY when the formal
    recovery_state is 'available'; invalid keeps the stable diagnostic
    code with an honest manual-handling note."""
    if recovery_state == "available":
        return RecoveryAction(visible=True, diagnostic_code=None, note="可恢复终态发布")
    if recovery_state == "invalid":
        return RecoveryAction(
            visible=False,
            diagnostic_code=diagnostic_code,
            note="终态意图记录无效，自动恢复不可用，需人工证据处置",
        )
    if recovery_state == "not_required":
        return RecoveryAction(visible=False, diagnostic_code=None, note="无需恢复")
    return RecoveryAction(visible=False, diagnostic_code=None, note="缺少终态意图记录")


def error_view(code: str, message: str | None = None) -> ErrorView:
    """Map a stable error code to 'business explanation + original code';
    an unregistered code is never swallowed or disguised as success."""
    explanation = _ERROR_EXPLANATIONS.get(code, "操作未完成")
    if message:
        explanation = f"{explanation}（{message}）"
    return ErrorView(message=explanation, code=code)


class ProjectWorkbenchCard(ProjectSummary):
    """ProjectSummary + display fields for the workbench card (read-only
    projection; identical shape to ProjectRow, kept as a separate name
    for the page semantic)."""

    status_label: str
    status_tone: str
    can_open: bool


class DataPageModel(TypedDict):
    """The 02_data read-only page model (read-only projection)."""

    selected: bool
    project_dir: str | None
    name: str | None
    data_grade: str | None
    frozen_label: str
    latest_batch_label: str | None
    updated_at: str | None
    status_label: str
    status_tone: str
    invalid: bool
    invalid_reason: str | None
    empty_note: str | None


def project_workbench_card(summary: ProjectSummary) -> ProjectWorkbenchCard:
    """Project one ProjectSummary into a workbench card (pure projection;
    the input is never mutated)."""
    row = project_row(summary)
    return ProjectWorkbenchCard(
        **summary,
        status_label=row["status_label"],
        status_tone=row["status_tone"],
        can_open=row["can_open"],
    )


def next_step_advice(
    selected_dir: str | None, cards: list[ProjectWorkbenchCard]
) -> str:
    """The next-step advice derives ONLY from formal ProjectSummary
    fields: a missing selection asks to select+open; a frozen project
    states the frozen fact and points to the NEXT preparation checks
    (never infers scenario/run/result readiness); a not-yet-frozen
    project points to data preparation; an invalid entry asks for manual
    evidence handling."""
    card = (
        next((c for c in cards if c["project_dir"] == selected_dir), None)
        if selected_dir is not None
        else None
    )
    if card is None:
        return "选择并打开项目"
    if card["invalid"]:
        return f"该项目条目损坏（{card['invalid_reason']}），需人工证据处置"
    if card["frozen"]:
        return "数据已冻结；请继续检查后续准备状态"
    if (
        card["latest_batch_state"] is None
        and card["updated_at"] is None
    ):
        return "待导入：请下载输入模板、填写数据，并在数据页上传校验"
    return "完成数据准备：核验数据并完成审批冻结"


def data_page_model(
    project: ProjectSummary | None, requested_dir: str | None = None
) -> DataPageModel:
    """Project the selected project into the read-only data page model,
    distinguishing the four selection states:

    - no selection (project None, requested_dir None): the honest empty
      state '尚未选择项目';
    - a stale selection (project None but a requested_dir exists): the
      entry is gone from THIS call's formal list - '当前项目不存在或已移除',
      never laundered into '尚未选择项目';
    - an invalid selection: selected=True + invalid=True with the raw
      invalid_reason; the corrupt entry's frozen=False sentinel is NEVER
      shown as a definite '未冻结' (frozen_label stays '未知') and its
      data_grade/latest_batch_state/updated_at are never packaged as
      reliable business facts;
    - a valid selection: the formal data_grade/frozen/latest_batch_state/
      updated_at verbatim (frozen is '已冻结'/'未冻结' only for explicit
      bools)."""
    if project is None:
        stale = requested_dir is not None
        return DataPageModel(
            selected=False,
            project_dir=None,
            name=None,
            data_grade=None,
            frozen_label="未知",
            latest_batch_label=None,
            updated_at=None,
            status_label="当前项目不存在或已移除" if stale else "尚未选择项目",
            status_tone="warning" if stale else "neutral",
            invalid=False,
            invalid_reason=None,
            empty_note="当前项目不存在或已移除" if stale else "尚未选择项目",
        )
    invalid = bool(project["invalid"])
    if invalid:
        # a corrupt entry is surfaced EXPLICITLY: identity and the raw
        # damage reason are preserved, while every business fact stays
        # UNKNOWN - the entry's sentinel values are never laundered
        return DataPageModel(
            selected=True,
            project_dir=project["project_dir"],
            name=project["name"],
            data_grade=None,
            frozen_label="未知",
            latest_batch_label=None,
            updated_at=None,
            status_label="条目损坏",
            status_tone="error",
            invalid=True,
            invalid_reason=project["invalid_reason"],
            empty_note=None,
        )
    if project["frozen"]:
        status_label = "数据已冻结"
        status_tone = "success"
    else:
        status_label = "数据未冻结"
        status_tone = "warning"
    frozen_value = project["frozen"]
    if frozen_value is True:
        frozen_label = "已冻结"
    elif frozen_value is False:
        frozen_label = "未冻结"
    else:
        frozen_label = "未知"
    return DataPageModel(
        selected=True,
        project_dir=project["project_dir"],
        name=project["name"],
        data_grade=project["data_grade"],
        frozen_label=frozen_label,
        latest_batch_label=project["latest_batch_state"],
        updated_at=project["updated_at"],
        status_label=status_label,
        status_tone=status_tone,
        invalid=False,
        invalid_reason=None,
        empty_note=None,
    )


def select_project(
    cards: list[ProjectWorkbenchCard], selected_dir: str | None
) -> ProjectWorkbenchCard | None:
    """Select a project ONLY from THIS call's formal list: an exact
    project_dir match on a valid, openable card returns a fresh copy;
    '..', absolute paths, names outside the list, sibling directories
    and invalid entries are all rejected (None)."""
    if selected_dir is None:
        return None
    for card in cards:
        if card["project_dir"] == selected_dir and card["can_open"] and not card["invalid"]:
            return ProjectWorkbenchCard(**card)
    return None


def find_project(
    cards: list[ProjectWorkbenchCard], selected_dir: str | None
) -> ProjectWorkbenchCard | None:
    """The READ-ONLY lookup (never the open-action gate): an exact
    project_dir match on THIS call's formal list returns a fresh copy,
    INCLUDING an invalid entry (so a damaged entry can be displayed
    honestly instead of being filtered into '尚未选择项目'). '..',
    absolute paths, empty strings and names outside the list all return
    None; the input list is never mutated. Opening a project still goes
    through select_project, which keeps rejecting invalid entries."""
    if selected_dir is None:
        return None
    for card in cards:
        if card["project_dir"] == selected_dir:
            return ProjectWorkbenchCard(**card)
    return None


# ===== V1-T1-A: workspace entry-error and empty-state projections =====

# the facade's display kinds -> stable business titles (a display
# mapping, never a new persistent diagnostic-code family)
_WORKSPACE_ERROR_TITLES: dict[str, str] = {
    "invalid_input": "工作区路径无效",
    "unavailable": "工作区无法读取",
}

_EMPTY_WORKSPACE_NOTE = (
    "尚无项目。请在上方“新建项目”创建项目（创建后显示为待导入）；"
    "打开项目后可在“数据”页下载模板、上传并校验输入表。"
)


class WorkspaceEntryError(TypedDict):
    """The honest workspace entry-error projection for the pages:
    ok=False, a stable business title, the facade's reason preserved
    VERBATIM, and a restart advice that names the real command."""

    ok: bool
    title: str
    detail: str
    advice: str


def workspace_error_view(kind: str, message: str) -> WorkspaceEntryError:
    """Project the facade's entry error (kind + preserved reason) into
    the page display state: the kind maps to a stable business title
    (an unknown kind stays honest with a neutral title and keeps the
    raw detail - never swallowed, never faked as a known workspace
    problem class); the advice always names the restart command. Pure
    projection: the inputs are never mutated, repeated calls are
    identical."""
    title = _WORKSPACE_ERROR_TITLES.get(kind)
    if title is None:
        title = f"工作区状态异常（{kind}）"
    return WorkspaceEntryError(
        ok=False,
        title=title,
        detail=str(message),
        advice="请检查工作区路径后，用 elcc-tool ui --workspace <工作区路径> 重新启动；"
        "本页面不会修改工作区。",
    )


def empty_workspace_note() -> str:
    """The honest empty-workspace note: an explicit empty state plus the
    fact that project creation/import arrives in a later package (the
    shell never auto-creates a teaching project and never shows a fake
    clickable entry)."""
    return _EMPTY_WORKSPACE_NOTE


__all__ = [
    "DataPageModel",
    "ErrorView",
    "JobActions",
    "JobRow",
    "ProjectRow",
    "ProjectWorkbenchCard",
    "RecoveryAction",
    "RequestRow",
    "RunRow",
    "StatusText",
    "WorkspaceEntryError",
    "bank_preparation_view",
    "calculation_common_rows",
    "calculation_options_view",
    "calculation_result_view",
    "cleanup_text",
    "data_confirm_state_label",
    "data_grade_label",
    "data_page_model",
    "empty_workspace_note",
    "error_view",
    "find_project",
    "format_beijing_time",
    "job_actions",
    "job_row",
    "metric_rows",
    "next_step_advice",
    "project_row",
    "project_workbench_card",
    "recovery_action",
    "request_row",
    "result_status_view",
    "run_row",
    "run_row_label",
    "scenario_bank_label",
    "select_project",
    "workspace_error_view",
]


# ===== V1-T1-C calculation projections =====

_CLEANUP_TEXT: dict[str, str] = {
    "pending": "资源释放确认中",
    "released": "资源已释放",
    "uncertain": "资源释放未确认（状态不确定）",
    "not_applicable": "无工作区锁资源需释放",
    "unconfirmed": "无法确认",
}

_SOC_MODE_LABELS: dict[str, str] = {
    "weather_cycle_warmup": "气象年预热（weather_cycle_warmup）",
    "stochastic_sample_burnin": "随机样本预热（stochastic_sample_burnin）",
    "not_applicable": "不适用",
}
_SOC_BOUNDARY_LABELS: dict[str, str] = {
    "carry_over": "跨年承接（carry_over）",
    "reinitialize_with_configured_mode": "按配置重初始化",
    "not_applicable": "不适用",
}
_CHRONOLOGY_LABELS: dict[str, str] = {
    "independent_weather_years": "独立气象年",
    "continuous_multiyear": "连续多年",
}

# T4-A remediation (review Q3): the unit text is DERIVED from the
# canonical domain source (domain.risk_units) - the UI keeps only the
# Chinese rendering of the machine unit strings, never a second unit
# table.
_T4A_METRIC_NAMES: dict[str, str] = {
    "lole_days_per_year": "LOLE 期望缺电天数",
    "lolh_hours_per_year": "LOLH 期望缺电小时数",
    "eue_mwh_per_year": "EUE 期望缺供电量",
    "neue_ppm": "NEUE 缺供电量占比",
}

_T4A_UNIT_ZH: dict[str, str] = {
    "days/year": "天/年",
    "hours/year": "小时/年",
    "mwh/year": "MWh/年",
    "ppm": "ppm",
}


def _metric_label(metric_value: str) -> str:
    """The display label with the unit taken from domain.risk_units."""
    from capacity_credit.contracts.enums import RiskMetric
    from capacity_credit.domain.risk_units import risk_metric_unit

    name = _T4A_METRIC_NAMES.get(metric_value, metric_value)
    try:
        unit = risk_metric_unit(RiskMetric(metric_value))
    except Exception:  # noqa: BLE001 - an unknown metric keeps its name
        return name
    return f"{name}（{_T4A_UNIT_ZH.get(unit, unit)}）"


_METRIC_LABELS: dict[str, str] = {
    metric_value: _metric_label(metric_value)
    for metric_value in (
        "lole_days_per_year",
        "lolh_hours_per_year",
        "eue_mwh_per_year",
        "neue_ppm",
    )
}


def result_status_view(result_status: str | None) -> StatusText:
    """The typed result-status projection; unknown values stay honest."""
    if result_status is None:
        return {"label": "未发布", "tone": "neutral"}
    text = _RESULT_STATUS_TEXT.get(result_status)
    if text is None:
        return {"label": f"未知状态（{result_status}）", "tone": "neutral"}
    return {"label": text["label"], "tone": text["tone"]}


def cleanup_text(cleanup: str | None) -> str:
    """The typed cleanup projection (never parsed from note strings)."""
    if cleanup is None:
        return _CLEANUP_TEXT["unconfirmed"]
    return _CLEANUP_TEXT.get(cleanup, f"未知清理状态（{cleanup}）")


def metric_rows(metrics: dict[str, Any] | None) -> list[tuple[str, Any]]:
    """The core adequacy metrics WITH their established units, in a
    stable order; only metrics that actually exist are shown."""
    if not metrics:
        return []
    rows: list[tuple[str, Any]] = []
    for key, label in _METRIC_LABELS.items():
        if key in metrics and metrics[key] is not None:
            rows.append((label, metrics[key]))
    return rows


def calculation_options_view(options: dict[str, Any]) -> dict[str, Any]:
    """The page-facing view of get_run_options: runnable gate, reasons,
    selectable data refs / banks / weight sets / scenarios, the
    capability limits, the wired methods (with honest availability
    notes) and the frozen ELCC candidate groups - all verbatim from the
    facade, never inferred."""
    return {
        "runnable": bool(options.get("runnable")),
        "not_runnable_reasons": list(options.get("not_runnable_reasons") or []),
        "data_refs": list(options.get("data_refs") or []),
        "scenario_banks": list(options.get("scenario_banks") or []),
        "weight_set_ids": list(options.get("weight_set_ids") or []),
        # B3: the declared per-year weights of each selectable weight set
        # (read from the frozen copy; a set without readable rows stays
        # absent and the page falls back to the id-only display)
        "weight_sets": dict(options.get("weight_sets") or {}),
        "scenario_ids": list(options.get("scenario_ids") or []),
        "capability_limits": dict(options.get("capability_limits") or {}),
        "methods": list(options.get("methods") or []),
        "method_notes": dict(options.get("method_notes") or {}),
        "elcc_candidates": list(options.get("elcc_candidates") or []),
        "elcc_growth_modes": list(options.get("elcc_growth_modes") or []),
    }


def weight_set_display(options: dict[str, Any], weight_set_id: str) -> str:
    """B2/B3: the human reading of one declared weight set - the FINAL
    per-year percentages actually used (the frozen contract requires
    the weights to sum to 1, so percentages ARE the declared values);
    a set without readable rows stays an honest id."""
    weight_sets = dict(options.get("weight_sets") or {})
    years = weight_sets.get(weight_set_id)
    if not years:
        return weight_set_id
    if isinstance(years, dict):
        items = sorted(years.items())
        body = "，".join(
            f"{year}年占 {float(weight) * 100:.1f}%" for year, weight in items
        )
        return f"{weight_set_id}（{body}）"
    return weight_set_id


def _mw_or_unresolved(value: Any) -> str:
    """An MW number formatted for display; an unresolved search stays
    honestly unresolved (never a fabricated MW value)."""
    if value is None:
        return "未解析（搜索未闭合）"
    return f"{float(value):,.1f} MW"


_T4A_DATA_GRADE_LABELS: dict[str, str] = {
    "G0_teaching": "示例数据（G0 教学）",
    "G1_preliminary": "G1 初步",
    "G2_research": "G2 研究",
    "G3_rule_support": "G3 规则支持",
}

_T4A_INTERPOLATION_LABELS: dict[str, str] = {
    "chord": "弦插值（chord）",
    "not_applicable": "不适用",
}

_T4A_METHOD_LABELS: dict[str, str] = {
    "adequacy": "系统充裕度评估",
    "elcc": "ELCC 有效承载容量评估",
    "efc": "EFC 等效完美容量评估",
    "demand_target_capacity": "目标可靠容量测算",
}

_T4A_METHOD_READING: dict[str, str] = {
    "adequacy": (
        "基础风险指标反映基准系统在所选场景银行与冻结数据下的风险水平，"
        "数值越大风险越高；不涉及任何候选资源。"
    ),
    "elcc": (
        "有效容量是等风险意义下候选资源可承载的容量：加入候选后系统风险"
        "回到基准水平所能支持的最大容量；等风险搜索区间是该容量的当前"
        "证据边界，不等于精确值。"
    ),
    "efc": (
        "等效完美容量是与候选资源产生相同系统风险的 100% 可靠完美容量大小；"
        "等风险搜索区间是该等效值的当前证据边界。"
    ),
}


def soc_block_visible(soc_rows: list[tuple[str, str]]) -> bool:
    """A06: the storage/chronology block is for REAL storage semantics -
    a not-applicable trio with the default independent-year chronology
    is not dressed up as a storage result (chronology still reaches the
    interpretation block for every method)."""
    default_semantics = ("—", "不适用", "独立气象年")
    for _label, value in soc_rows or []:
        text = str(value or "").strip()
        if text and text not in default_semantics:
            return True
    return False


def result_interpretation_rows(
    result_view: dict[str, Any]
) -> list[tuple[str, str]]:
    """A deterministic explanation template over the projection ONLY
    (what was measured, input scope, settings, how to read it, what
    stays uncertain, traceability). No policy advice, no causal claims,
    no invented statistics; an evidence failure or an unpublished result
    has NO interpretation at all.

    R02 (night-4): a warning tone NEVER reads as 未收敛 by itself - the
    raw result_status separates 确实未收敛 from 有效但有警告（收敛已满足）."""
    if result_view.get("evidence_error") or not result_view.get("result_id"):
        return []
    method = str(result_view.get("method") or "")
    capacity = dict(result_view.get("capacity_view") or {})
    common = dict(result_view.get("common_view") or {})
    parts: list[tuple[str, str]] = []
    scope = [_T4A_METHOD_LABELS.get(method, method or "未知方法")]
    if result_view.get("scenario_bank_id"):
        scope.append(f"模拟样本集 {str(result_view['scenario_bank_id'])[:8]}…")
    sample_count = common.get("sample_count")
    if sample_count is None:
        sample_count = capacity.get("sample_count")
    if sample_count is not None:
        scope.append(f"{sample_count} 样本")
    parts.append(("本次测算", "｜".join(scope)))
    parts.append(("结果状态", str(result_view.get("result_status_label") or "—")))
    reading = _T4A_METHOD_READING.get(method)
    if reading:
        parts.append(("怎样理解", reading))
    uncertainty: list[str] = []
    raw_status = result_view.get("result_status")
    if raw_status == "not_converged":
        uncertainty.append(
            "本结果未收敛：数值未满足收敛要求，只是当前证据下的诚实读数，"
            "不构成最终结论。"
        )
    elif result_view.get("result_status_tone") == "warning":
        uncertainty.append(
            "计算已完成，结果有效但携带警告（例如数据等级或缺少置信区间提示）："
            "收敛要求已满足，具体见结果诊断。"
        )
    convergence = common.get("risk_convergence") or None
    if capacity.get("search_convergence") and capacity["search_convergence"] not in (
        "converged",
        "not_applicable",
    ):
        uncertainty.append(
            f"搜索收敛：{capacity['search_convergence']}（有效容量为当前证据读数）。"
        )
    if convergence and convergence not in ("converged",) and raw_status != "not_converged":
        uncertainty.append(f"风险收敛：{convergence}。")
    diagnostics = common.get("diagnostics")
    if diagnostics is None:
        diagnostics = capacity.get("diagnostics") or []
    if diagnostics:
        uncertainty.append(f"结果携带 {len(diagnostics)} 条诊断（见下方逐条原文）。")
    if uncertainty:
        parts.append(("需要注意", " ".join(uncertainty)))
    parts.append(
        (
            "结果追溯",
            f"运行 {result_view.get('run_ref')}｜结果 {result_view.get('result_id')}"
            f"｜制品清单 {result_view.get('artifact_manifest_id') or '—'}",
        )
    )
    return parts


def calculation_capacity_rows(capacity: dict[str, Any]) -> list[tuple[str, str]]:
    """The ELCC/EFC capacity card rows, verbatim from the MethodResult
    projection: the candidate identity, the frozen reference capacity,
    the accredited capacity (or its honest unresolved state), the credit
    fraction, the data grade, the interpolation method and the
    search/convergence boundaries. The equal-risk SEARCH bracket and the
    STATISTICAL risk-estimate confidence interval are separately named
    with their sources. An unresolved bracket is SHOWN, never smoothed
    into a precise-looking number."""
    if not capacity:
        return []
    rows: list[tuple[str, str]] = []
    portfolio = capacity.get("resource_or_portfolio_id")
    if portfolio:
        rows.append(("候选资源/组合", str(portfolio)))
    if capacity.get("reference_capacity_mw") is not None:
        rows.append(
            ("参考容量（冻结解析）", _mw_or_unresolved(capacity.get("reference_capacity_mw")))
        )
    rows.append(("有效容量", _mw_or_unresolved(capacity.get("accredited_capacity_mw"))))
    fraction = capacity.get("capacity_credit_fraction")
    if fraction is not None:
        rows.append(("容量信用率", f"{float(fraction) * 100:.2f}%"))
    # T4-A (A06): the effective data grade of the result, verbatim
    grade = capacity.get("effective_data_grade")
    if grade:
        rows.append(
            ("数据等级", _T4A_DATA_GRADE_LABELS.get(str(grade), str(grade)))
        )
    # T4-A (A07): the interpolation method has its own NAME (never
    # blended into the intervals below)
    interpolation = capacity.get("interpolation_method")
    if interpolation:
        rows.append(
            (
                "插值方式",
                _T4A_INTERPOLATION_LABELS.get(str(interpolation), str(interpolation)),
            )
        )
    low = capacity.get("search_bracket_low_mw")
    high = capacity.get("search_bracket_high_mw")
    if low is not None or high is not None:
        bracket_text = (
            f"等风险搜索区间 [{_mw_or_unresolved(low)}，"
            f"{_mw_or_unresolved(high)}]"
        )
        low_risk = capacity.get("search_bracket_low_risk")
        high_risk = capacity.get("search_bracket_high_risk")
        if low_risk is not None and high_risk is not None:
            bracket_text += f"（区间端点风险 {low_risk} / {high_risk}）"
        rows.append(("搜索区间", bracket_text))
    sample_count = capacity.get("sample_count")
    if sample_count is not None:
        rows.append(
            (
                "样本与收敛",
                (
                    f"{sample_count} 样本"
                    f"（风险收敛：{capacity.get('risk_convergence')}；"
                    f"搜索收敛：{capacity.get('search_convergence')}）"
                ),
            )
        )
    if capacity.get("lower_ci") is not None or capacity.get("upper_ci") is not None:
        # T4-A (A07): the STATISTICAL risk-estimate CI names its metric
        # and confidence level - a different object from the search
        # bracket above
        metric = str(capacity.get("primary_risk_metric") or "")
        metric_label = _METRIC_LABELS.get(metric, metric or "主风险指标")
        rows.append(
            (
                "风险估计置信区间",
                (
                    f"[{capacity.get('lower_ci')}，{capacity.get('upper_ci')}]"
                    f"｜{metric_label}｜置信水平 {capacity.get('confidence_level')}"
                ),
            )
        )
    return rows


def calculation_common_rows(common: dict[str, Any]) -> list[tuple[str, str]]:
    """R06 (night-4): the SHARED result rows every method shows - data
    grade, sample/convergence reading and the primary metric identity -
    verbatim from the projection. Capacity-only fields stay in the
    capacity rows; an not-applicable search convergence (adequacy) is
    shown honestly as 不适用, never dropped and never read as a value."""
    if not common:
        return []
    rows: list[tuple[str, str]] = []
    grade = common.get("effective_data_grade")
    if grade:
        rows.append(
            ("数据等级", _T4A_DATA_GRADE_LABELS.get(str(grade), str(grade)))
        )
    sample_count = common.get("sample_count")
    risk_convergence = common.get("risk_convergence")
    search_convergence = common.get("search_convergence")
    if sample_count is not None or risk_convergence:
        text = f"{sample_count if sample_count is not None else '—'} 样本"
        text += f"（风险收敛：{risk_convergence or '—'}"
        text += f"；搜索收敛：{_SEARCH_CONVERGENCE_ZH.get(str(search_convergence), search_convergence) if search_convergence else '—'}）"
        rows.append(("样本与收敛", text))
    metric = str(common.get("primary_risk_metric") or "")
    if metric:
        rows.append(("主要风险指标", _METRIC_LABELS.get(metric, metric)))
    return rows


_CRITERIA_LABELS: dict[str, str] = {
    "min_samples_met": "样本量达到最低要求",
    "precision_met": "主指标区间精度达标",
    "nonzero_samples_met": "非零风险样本足够",
    "recent_batches_stable": "最近批次均值稳定",
}


def convergence_criteria_rows(common: dict[str, Any]) -> list[tuple[str, str]]:
    """D2/D3 (C-night): expand the single 未收敛 word into the run's OWN
    per-criterion evidence, verbatim from ``convergence_evidence.json``
    (the publisher's record of the shared T08 gate). A criterion is
    rendered 满足/未满足/无法判断 - 无法判断 (no evidence file, old
    runs) NEVER guesses. Zero observed events get the honest D3 wording
    (never 无风险, never a fabricated precision claim)."""
    rows: list[tuple[str, str]] = []
    evidence = common.get("convergence_evidence")
    if not isinstance(evidence, dict):
        rows.append(
            (
                "收敛判据明细",
                (
                    "无法判断：本结果没有判据级证据（旧版本运行）。"
                    "只能显示样本数与最终收敛状态；重新计算可获得逐判据明细。"
                ),
            )
        )
        return rows
    criteria = dict(evidence.get("criteria") or {})
    measured = dict(evidence.get("measured") or {})
    thresholds = dict(evidence.get("thresholds") or {})
    metric = str(evidence.get("primary_risk_metric") or "")
    metric_label = _METRIC_LABELS.get(metric, metric or "主风险指标")

    for key in (
        "min_samples_met",
        "nonzero_samples_met",
        "precision_met",
        "recent_batches_stable",
    ):
        label = _CRITERIA_LABELS.get(key, key)
        value = criteria.get(key)
        if value is True:
            rows.append((label, "满足"))
        elif value is False:
            rows.append((label, "未满足"))
        else:
            rows.append((label, "无法判断"))

    sample_count = measured.get("sample_count")
    min_samples = thresholds.get("min_samples")
    nonzero = measured.get("nonzero_sample_count")
    min_nonzero = thresholds.get("min_nonzero_samples")
    parts: list[str] = []
    if sample_count is not None:
        parts.append(
            f"本次使用 {sample_count} 个样本"
            + (
                f"（最低要求 {min_samples}）"
                if min_samples is not None
                else ""
            )
        )
    if nonzero is not None:
        parts.append(
            f"观察到缺供的样本 {nonzero} 个"
            + (
                f"（判据要求 ≥{min_nonzero}）"
                if min_nonzero is not None
                else ""
            )
        )
    mean = measured.get("primary_mean")
    half_width = measured.get("interval_half_width")
    relative = measured.get("relative_half_width")
    if mean is not None and half_width is not None:
        precision = f"{metric_label}均值 {_format_number(mean)}，判据区间半宽 {_format_number(half_width)}"
        if relative is not None:
            tolerance = thresholds.get("relative_half_width_tolerance")
            precision += (
                f"（相对半宽 {float(relative) * 100:.2f}%，"
                f"要求 ≤{float(tolerance) * 100:.1f}%）"
                if tolerance is not None
                else ""
            )
        else:
            absolute = thresholds.get("absolute_half_width_tolerance")
            precision += (
                "（均值为0或接近0，按绝对误差判据判定，不计算不稳定的相对误差）"
            )
            if absolute is not None:
                precision += f"：绝对半宽要求 ≤{absolute}"
        parts.append(precision)
    if parts:
        rows.append(("判据读数", "；".join(parts) + "。"))

    rows.append(("实际停止原因", _convergence_stop_reason(evidence)))
    rows.append(("对应的下一步", _convergence_next_step(evidence)))
    return rows


def _format_number(value: Any) -> str:
    number = float(value)
    if number == int(number) and abs(number) < 1e6:
        return f"{int(number):,}"
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def _convergence_stop_reason(evidence: dict[str, Any]) -> str:
    criteria = dict(evidence.get("criteria") or {})
    measured = dict(evidence.get("measured") or {})
    risk_conv = str(evidence.get("risk_convergence") or "")
    nonzero = measured.get("nonzero_sample_count")
    if risk_conv == "converged":
        base = "四项判据全部满足，计算收敛"
        if measured.get("nonzero_sample_count") == 0:
            # cannot happen with the shared gate (nonzero criterion), but
            # never fabricate - stay verbatim
            return base
        return base + "。注意：这是主风险指标的收敛，不代表另外三项指标达到相同精度。"
    if not criteria.get("nonzero_samples_met"):
        if nonzero == 0:
            return (
                "本次模拟样本中没有观察到任何缺供事件（全部样本的风险指标为0），"
                "非零风险样本判据不满足；计算在未满足判据的情况下停止，不能当作已收敛。"
            )
        return (
            f"观察到缺供的样本偏少（{nonzero} 个，低于判据要求），"
            "统计证据不足，计算在未满足判据的情况下停止。"
        )
    if not criteria.get("min_samples_met"):
        measured_sample = measured.get("sample_count")
        min_samples = (evidence.get("thresholds") or {}).get("min_samples")
        return (
            f"样本量未达到最低要求（{measured_sample}/{min_samples}），"
            "样本预算不足，计算在未满足判据的情况下停止。"
        )
    if not criteria.get("precision_met"):
        return (
            "主风险指标的区间精度未达到要求（估计波动过大），"
            "计算在未满足判据的情况下停止。"
        )
    if not criteria.get("recent_batches_stable"):
        return "最近批次均值尚不稳定，计算在未满足判据的情况下停止。"
    return "部分判据未满足，计算停止；具体见上方逐判据读数。"


def _convergence_next_step(evidence: dict[str, Any]) -> str:
    criteria = dict(evidence.get("criteria") or {})
    measured = dict(evidence.get("measured") or {})
    risk_conv = str(evidence.get("risk_convergence") or "")
    if risk_conv == "converged":
        return (
            "可结合结果状态直接阅读本结果；仍需注意收敛不等于供电达标，"
            "结论边界见下方说明。"
        )
    if not criteria.get("nonzero_samples_met"):
        if measured.get("nonzero_sample_count") == 0:
            return (
                "数值0按实际结果显示；这不能推断系统无风险，也不能给出统计精度。"
                "可结合业务经验判断当前裕度；如需统计上的风险区间，需要能观察到"
                "缺供事件的评估条件或更多样本，但增加样本不保证一定满足精度要求。"
            )
        return "可准备更大的模拟样本集（更多样本数）后重新计算，以积累更多缺供样本。"
    if not criteria.get("min_samples_met"):
        return (
            "当前样本量不足：生成更大样本数的模拟样本集（或使用包含更多样本的"
            "样本集）后重新计算。"
        )
    if not criteria.get("precision_met") or not criteria.get("recent_batches_stable"):
        return "可增加模拟样本数量后重新计算，让估计更稳定。"
    return "请参考逐判据读数定位未满足的判据后，调整样本设置重新计算。"


def metric_interpretation_rows(metrics: dict[str, Any] | None) -> list[tuple[str, str]]:
    """E2: one business explanation per REPORTED metric - only metrics
    that actually exist on the result are explained, and each
    explanation carries the metric-boundary wording required by the
    methodology. The texts come from the reporting layer's canonical
    business-notes source (the SAME wording the published report
    renders - never a second drifting copy)."""
    if not metrics:
        return []
    from capacity_credit.reporting.business_notes import (
        BUSINESS_METRIC_EXPLANATIONS as _CANONICAL_EXPLANATIONS,
    )

    rows: list[tuple[str, str]] = []
    for key, label in _METRIC_LABELS.items():
        if key in metrics and metrics[key] is not None:
            explanation = _CANONICAL_EXPLANATIONS.get(key)
            if explanation:
                rows.append((label, explanation))
    return rows


def result_boundary_rows() -> list[tuple[str, str]]:
    """E3: the fixed conclusion boundaries shown with every result -
    what this reading can and cannot support (no compliance verdicts
    without an applicable standard, no seasonal/peak claims without
    hourly evidence, no counterfactual benefit claims without a
    comparison run)."""
    return [
        (
            "结果边界",
            (
                "收敛只说明估计达到了当前设定的精度要求，不等于供电达标；"
                "本工具未内置任何考核标准，不在无适用标准时判定“合格/不合格”，"
                "也不自动套用其他地区阈值。"
            ),
        ),
        (
            "结论边界",
            (
                "本结果不包含季节、高峰或持续缺电事件分析（那需要逐时证据的"
                "专门核查）；没有对照试算时，也不能给出储能、新能源或联络线"
                "能降低多少风险的结论。"
            ),
        ),
    ]


_SEARCH_CONVERGENCE_ZH: dict[str, str] = {
    "converged": "已收敛",
    "not_converged": "未收敛",
    "not_applicable": "不适用",
    "unresolved": "未解析",
}


def calculation_result_view(projection: dict[str, Any]) -> dict[str, Any]:
    """The page-facing view of get_run_projection: formal run identity,
    lifecycle and result statuses (typed projections), core metrics
    with units, the SHARED rows every method shows (R06), the ELCC/EFC
    capacity rows (verbatim, honest about unresolved searches), and any
    honest evidence failure."""
    status = result_status_view(projection.get("result_status"))
    soc = dict(projection.get("soc_modes") or {})
    soc_rows = [
        ("SOC 初始化模式", _SOC_MODE_LABELS.get(
            soc.get("storage_soc_initialization_mode"),
            soc.get("storage_soc_initialization_mode") or "—",
        )),
        ("SOC 年边界", _SOC_BOUNDARY_LABELS.get(
            soc.get("soc_year_boundary_mode"),
            soc.get("soc_year_boundary_mode") or "—",
        )),
        ("时间衔接", _CHRONOLOGY_LABELS.get(
            soc.get("chronology_mode"),
            soc.get("chronology_mode") or "—",
        )),
    ]
    method = projection.get("method")
    capacity_rows: list[tuple[str, str]] = []
    if method in ("elcc", "efc"):
        capacity_rows = calculation_capacity_rows(
            dict(projection.get("capacity_view") or {})
        )
    common = dict(projection.get("common_view") or {})
    return {
        "run_ref": projection.get("run_ref"),
        "lifecycle_status": projection.get("lifecycle_status"),
        "result_id": projection.get("result_id"),
        "result_status": projection.get("result_status"),
        "result_status_label": status["label"],
        "result_status_tone": status["tone"],
        "method": method,
        "scenario_bank_id": projection.get("scenario_bank_id"),
        "metric_rows": metric_rows(projection.get("metrics")),
        "metric_interpretation_rows": metric_interpretation_rows(
            projection.get("metrics")
        ),
        "soc_rows": soc_rows,
        "capacity_rows": capacity_rows,
        "capacity_view": dict(projection.get("capacity_view") or {}),
        "common_view": common,
        "common_rows": calculation_common_rows(common),
        "convergence_criteria_rows": convergence_criteria_rows(common),
        "boundary_rows": result_boundary_rows(),
        "artifact_manifest_id": projection.get("artifact_manifest_id"),
        "evidence_error": projection.get("evidence_error"),
    }


# ===== V1-T2-B upload submission projections =====

_IMPORT_STATUS_TEXT: dict[str, dict[str, str]] = {
    "parse_error": {
        "label": "无法受理（未注册批次）",
        "tone": "error",
        "note": "本次上传无法进入原导入注册合同；不存在任何批次。请修正后重新提交。",
    },
    "validation_failed": {
        "label": "校验未通过（已按原状态注册失败批次）",
        "tone": "error",
        "note": "已生成可追溯的失败批次（状态：已阻断）；未批准、未冻结、不可计算。"
                "修正数据后重新提交将形成新批次，不会覆盖旧版本。",
    },
    "passed": {
        "label": "校验通过（已注册导入批次）",
        "tone": "success",
        "note": "已生成可追溯导入批次；该批次未批准、未冻结、尚不可计算——"
                "批准与冻结需在确认页明确操作。",
    },
}

# per-code actionable filling hints (display only - never a second
# diagnostic family); unknown codes keep an honest generic hint
_IMPORT_ADVICE: dict[str, str] = {
    "DQ01_FILE_MISSING": "缺少必需表：请下载对应模板、填写后一并上传。",
    "DQ01_FILE_EMPTY": "表为空（只有表头或0行）：请填写数据行；声明不包含的资源不提供该表即可。",
    "DQ01_FILE_OVERSIZE": "文件超过本入口大小上限：请拆分文件或联系管理员。",
    "DQ02_ENCODING_UNKNOWN": "无法识别编码：请将文件另存为 UTF-8（Excel：CSV UTF-8）后重试。",
    "DQ02_CSV_UNREADABLE": "CSV 结构无法解析：请检查是否存在未闭合引号、二进制内容或损坏文件。",
    "DQ03_MISSING_REQUIRED_COLUMNS": "缺少必需列：请对照所下载模板的表头核对列名与顺序。",
    "DQ05_UNKNOWN_COLUMN_PRESERVED": "模板外的未知列已原样保留，不影响导入；请确认其是否需要。",
    "DQ05_COMPOSITION_CONFLICT": "与项目资源构成声明冲突：请更正项目构成声明，或移除该表。",
    "DQ05_COMPOSITION_UNCONFIRMED": "资源构成尚未确认：请先在项目页明确储能/水电构成。",
    "DATA_TIME_DUPLICATE": "存在重复时间戳：请排查 timestamp 列的重复行（预览之外也会被检出）。",
    "DATA_TIME_GAP": "小时轴不连续（缺时段/多时段）：请补齐缺失时段；系统不会补零出数。",
    "DQ19_TIME_MISALIGNED": "跨表时间轴与负荷不一致：请核对各表 timestamp 与时段范围。",
    "DQ07_DUPLICATE_PRIMARY_KEY": "主键重复：请检查业务编号列（如 unit_id/timestamp 组合）。",
    "DQ20_GRADE_UNDETERMINED": "没有完整同步的气象年：请检查负荷/新能源表的年份与时间轴。",
    "DQ29_GRADE_ABOVE_MAX": "声明等级超过数据可支持上限：请调低声明数据等级。",
    "DQ21_WEIGHT_INVALID": "气象年权重非法：请检查权重表（同集合权重和应为1）。",
    "DATA_ID_ORPHAN": "存在引用了不存在对象的记录：请核对接表业务编号（如 hydro_id/intertie_id）。",
}


def import_issue_rows(issues: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The page-facing issue rows: the original code/severity/table/
    locator/message are preserved verbatim and an actionable hint is
    ATTACHED for display; nothing is dropped, merged or renumbered."""
    rows: list[dict[str, Any]] = []
    for issue in issues or []:
        code = str(issue.get("code", ""))
        rows.append(
            {
                "severity": str(issue.get("severity", "")),
                "code": code,
                "table": str(issue.get("table", "") or "—"),
                "locator": str(issue.get("locator", "") or "—"),
                "message": markdown_safe_text(str(issue.get("message", ""))),
                "advice": _IMPORT_ADVICE.get(code, "请根据原因修正后重新上传该表。"),
            }
        )
    return rows


def upload_preview(table_key: str, file_name: str, data: bytes, max_rows: int = 5) -> dict[str, Any]:
    """The bounded pre-submit preview of one uploaded CSV: an honest
    total row count (the FULL file is read - the display is capped, the
    count is not a subset claim) plus the first rows for display.
    Parse failures are honest preview states, not submission verdicts."""
    import io as _io

    import pandas as pd

    from capacity_credit.data.io import detect_encoding

    empty: dict[str, Any] = {
        "ok": False, "summary": "", "columns": [], "head": [], "total_rows": None,
    }
    try:
        encoding = detect_encoding(data)
    except Exception:  # noqa: BLE001 - preview stays honest
        encoding = "unknown"
    if encoding == "unknown":
        return {
            **empty,
            "summary": f"{file_name}：无法识别文件编码（读取器支持 utf-8-sig/utf-8/gb18030）",
        }
    try:
        frame = pd.read_csv(_io.BytesIO(data), encoding=encoding)
    except Exception as exc:  # noqa: BLE001 - preview stays honest
        return {**empty, "summary": f"{file_name}：无法解析（{exc}）"}
    head = [
        [("" if pd.isna(v) else str(v))[:60] for v in row]
        for row in frame.head(max_rows).itertuples(index=False, name=None)
    ]
    shown = min(max_rows, len(frame))
    return {
        "ok": True,
        "summary": (
            f"{file_name}：共 {len(frame)} 行数据（预览前 {shown} 行；"
            "校验覆盖整个文件，超出预览范围的行同样会被检查）"
        ),
        "columns": [str(c) for c in frame.columns],
        "head": head,
        "total_rows": len(frame),
    }


def import_submission_view(result: dict[str, Any]) -> dict[str, Any]:
    """The page-facing view of the upload submission projection: an
    honest status label, the never-approved/never-frozen note, the
    batch reference when one exists, and the full issue rows."""
    status_key = str(result.get("status", ""))
    text = _IMPORT_STATUS_TEXT.get(status_key)
    if text is None:
        text = {
            "label": f"未知状态（{status_key or '空'}）",
            "tone": "neutral",
            "note": "未识别的提交状态；请核对日志。",
        }
    return {
        "status": status_key,
        "status_label": text["label"],
        "status_tone": text["tone"],
        "honest_note": text["note"],
        "import_batch_id": result.get("import_batch_id"),
        "data_version_id": result.get("data_version_id"),
        "reused": bool(result.get("reused")),
        "validation_status": result.get("validation_status"),
        "declared_data_grade": result.get("declared_data_grade"),
        "issue_rows": import_issue_rows(result.get("issues")),
        "row_counts": dict(result.get("row_counts") or {}),
        "previews": dict(result.get("previews") or {}),
    }


# ===== night-4: business wording helpers (R01/§7 user-facing facts) =====


def data_grade_label(grade: Any) -> str:
    """The business wording of one data grade (G0 reads as 示例数据;
    other grades map to the data contract's honest levels - never
    upgraded to 已认证/正式可用). Unknown/None stays 未知 or verbatim."""
    if grade is None:
        return "未知"
    return _T4A_DATA_GRADE_LABELS.get(str(grade), str(grade))


def format_beijing_time(value: Any) -> str:
    """A human-readable Beijing wall-clock rendering of one recorded
    time (ISO string or datetime; UTC offsets honoured). Internal
    records stay untouched - this is DISPLAY only. Missing/unparsable
    input stays 未知, never a fabricated time."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    if value is None:
        return "未知"
    try:
        if isinstance(value, str):
            parsed = _dt.fromisoformat(value)
        elif isinstance(value, _dt):
            parsed = value
        else:
            return "未知"
        beijing = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        return beijing.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "未知"


def data_confirm_state_label(
    validation_passed: Any, approved: Any, frozen: Any
) -> str:
    """The user wording of the DATA CONFIRM state, derived ONLY from the
    actual recorded facts (never merged into one backend state):
    frozen = confirmed; approved-but-not-frozen = the version save did
    not succeed (retry); validated = awaiting the user's confirmation
    review; otherwise awaiting the check. Unknown evidence stays
    unknown."""
    if frozen is True:
        return "本次测算数据已确认"
    if approved is True and frozen is False:
        return "数据审核已通过，测算版本尚未保存成功，请重试"
    if validation_passed is True and approved is not True:
        return "待核对"
    if frozen is False and approved is False and validation_passed is False:
        return "待检查"
    return "未知"


def scenario_bank_label(
    bank: dict[str, Any], current_batch_id: str | None = None
) -> str:
    """One sample-set row for a dropdown: sample count, seed and (when
    recorded) the Beijing preparation time. The 64-char internal bank id
    is NEVER the primary label; identity stays available via the short
    prefix (uniqueness is kept by attaching it, the full id is not
    shown). A bank first built under a DIFFERENT data version carries an
    honest provenance note - sample sets are random-stream layers whose
    content is shape-derived, so cross-version reuse is legitimate and
    the run gates validate seed/structure."""
    binding = bank.get("source_binding")
    created = (
        format_beijing_time(binding.get("created_at"))
        if isinstance(binding, dict)
        else "未知"
    )
    samples = bank.get("sample_count", "未知")
    seed = bank.get("root_seed", "未知")
    label = f"样本集（{samples} 样本｜随机数种子 {seed}｜生成时间 {created}）"
    if (
        current_batch_id is not None
        and isinstance(binding, dict)
        and binding.get("import_batch_id")
        and binding["import_batch_id"] != current_batch_id
    ):
        label += "（首次构建于其他数据版本）"
    return label


def bank_preparation_view(
    options: dict[str, Any], selected_batch_id: str | None
) -> dict[str, Any]:
    """R01: the sample-set (scenario bank) preparation view for ONE
    selected frozen data version. Preparing a set for a NEW frozen
    version stays available even when the project already has banks for
    an older version: banks bound to the selected version are listed,
    legacy banks without a recorded binding are counted honestly
    (构建来源未记录) and never claimed to belong to the selected
    version. Nothing here creates, rewrites or reads raw business
    files - the rows come from the facade's options verbatim."""
    frozen = [
        d
        for d in options.get("data_refs") or []
        if d.get("frozen") and d.get("approved")
    ]
    banks_for_version: list[dict[str, Any]] = []
    other_banks: list[dict[str, Any]] = []
    unbound = 0
    for bank in options.get("scenario_banks") or []:
        binding = bank.get("source_binding")
        if not isinstance(binding, dict) or not binding.get("import_batch_id"):
            unbound += 1
            continue
        row = {
            "scenario_bank_id": str(bank.get("scenario_bank_id") or ""),
            "label": scenario_bank_label(bank, selected_batch_id),
            "sample_count": bank.get("sample_count"),
            "root_seed": bank.get("root_seed"),
            "weather_years": list(bank.get("weather_years") or []),
        }
        if binding.get("import_batch_id") == selected_batch_id:
            banks_for_version.append(row)
        else:
            other_banks.append(row)
    return {
        "can_prepare": bool(frozen),
        "banks_for_version": banks_for_version,
        "other_banks": other_banks,
        "unbound_banks": unbound,
    }

def run_row_label(row: dict[str, Any]) -> str:
    """R09: one READABLE history row (method, honest state, Beijing
    time) - never a bare run UUID as the primary label. Failed and
    unpublished runs are listed as such, never dropped and never
    laundered into success."""
    method_raw = str(row.get("method") or "")
    method = {
        "adequacy": "充裕度",
        "elcc": "ELCC",
        "efc": "EFC",
    }.get(method_raw, "未知方法")
    lifecycle = str(row.get("lifecycle_status") or "")
    published = bool(row.get("published"))
    if lifecycle == "running":
        state = "正在计算"
    elif lifecycle == "failed":
        state = "计算失败"
    elif lifecycle == "cancelled" or lifecycle == "stopping":
        state = "已中断"
    elif published and lifecycle == "completed":
        state = "计算完成"
    elif published:
        state = "有结果（生命周期未知）"
    else:
        state = "未发布结果"
    return f"{method}｜{state}｜{format_beijing_time(row.get('created_at'))}"
