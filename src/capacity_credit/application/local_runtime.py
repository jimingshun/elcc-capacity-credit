"""V1-T1-B in-process serial runtime (the single-active-task engine).

ONE active task per normalized workspace, per process (architecture
§5): admission happens under a short in-process mutex BEFORE anything
is queued - a busy workspace refuses the second request, it is never
enqueued behind the first. The same intent id re-submitted with the
same effective params returns the SAME request (never a second run);
different params under one intent are rejected. The execution thread
acquires the workspace RunLock (locks/v1-execution, key ``active``)
BEFORE the runner's first side effect; a lock conflict refuses with
zero runner calls. All slot cleanup is ownership-checked - a stale
finalize for an old request can never clear a newer one - and snapshots
are independent frozen copies with a bounded event buffer. Completion
of the executor Future is NOT business completion: without formal run
evidence the projection says exactly that (run_ref stays None).

No persistent queue, no claim/recovery, no cancellation contract, no
worker process - the controlled runner stub is injected for tests only;
unwired production methods must stay explicitly unavailable (T1-C).
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from capacity_credit.application.locks import RunLock

MAX_EVENTS = 100
MAX_INTENT_RECORDS = 128
MAX_EVICTED_REMEMBERED = 256

EXECUTION_LOCK_DIR = "locks/v1-execution"
EXECUTION_LOCK_KEY = "active"

Observer = Callable[[str, str, int | float | None, int | float | None], None]
Runner = Callable[[Observer, str], Any]


class LocalRuntimeError(ValueError):
    """Light-weight runtime admission error (display kind + reason).

    kind: busy / intent_conflict / intent_unconfirmed /
    method_unavailable / invalid_input / closed / close_forbidden. A
    display category, not a persistent diagnostic-code family; never
    reuses WORKER or business codes."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class ExecutionEvent:
    seq: int
    phase: str
    subtask: str = ""
    observed: int | float | None = None
    total: int | float | None = None
    event_time: str = ""


@dataclass(frozen=True)
class ExecutionSnapshot:
    """An independent, read-only projection of one execution request.

    Transient execution/display state ONLY - never a business state
    machine: executor completion does not imply business result
    validity; ``run_ref`` stays None until a formal run identity truly
    exists; unknown/evicted requests are honestly ``unconfirmed``."""

    request_ref: str
    intent_id: str
    run_ref: str | None
    status: str
    phase: str
    subtask: str
    event_seq: int
    observed: int | float | None
    total: int | float | None
    event_time: str
    execution_error: str | None
    evidence_note: str
    cleanup: str
    observer_failures: int
    events: tuple[ExecutionEvent, ...]


@dataclass(frozen=True)
class StartHandle:
    """What start_calculation returns immediately: the request reference
    plus whatever run reference exists (None - never a fabricated
    identity - until the formal workflow creates one)."""

    request_ref: str
    run_ref: str | None = None


@dataclass
class _RequestState:
    request_ref: str
    intent_id: str
    params_hash: str
    run_ref: str | None = None
    status: str = "admitted"
    phase: str = "admitted"
    subtask: str = ""
    event_seq: int = 0
    observed: int | float | None = None
    total: int | float | None = None
    event_time: str = ""
    execution_error: str | None = None
    evidence_note: str = "执行已受理（受控替身路径；尚无业务结果证据）"
    cleanup: str = "pending"
    observer_failures: int = 0
    events: deque[ExecutionEvent] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))


@dataclass
class _IntentRecord:
    request_ref: str
    params_hash: str
    finished: bool


class LocalRuntime:
    """The single-active-task engine for one normalized workspace."""

    def __init__(self, workspace_root: Path | str) -> None:
        self._root = Path(workspace_root).resolve()
        self._mutex = threading.Lock()
        self._slot: _RequestState | None = None
        self._finished_states: dict[str, _RequestState] = {}
        self._intents: OrderedDict[str, _IntentRecord] = OrderedDict()
        self._evicted_intents: deque[str] = deque(maxlen=MAX_EVICTED_REMEMBERED)
        self._forgotten_count = 0
        self._closed = False
        self._shared = False
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="elcc-local-runtime"
        )

    @property
    def workspace_root(self) -> Path:
        """The workspace this runtime is bound to (normalized upstream)."""
        return self._root

    def has_active(self) -> bool:
        with self._mutex:
            return self._slot is not None

    def start(
        self,
        *,
        intent_id: str,
        params: dict[str, Any],
        params_hash: str,
        runner: Runner,
        allow_unknown: bool = False,
    ) -> str:
        """Admit one execution request (all admission decisions happen
        under the short mutex, BEFORE anything is queued):

        - same intent + same params, active: return the SAME request
          (never a second execution);
        - same intent + same params, finished: return the existing
          request (never an automatic re-run);
        - same intent + different params: rejected (intent_conflict);
        - an intent remembered as evicted, OR any UNKNOWN intent after
          this runtime has forgotten at least one intent: refused as
          intent_unconfirmed (never a silent fresh execution pretending
          it never ran - once memory was lost, an unknown id could be an
          old one, and the runtime does not guess);
        - ``allow_unknown=True`` is the CALLER's explicit declaration
          that this intent is genuinely new (the UI 'run again' action);
          the declaration transfers the freshness responsibility to the
          caller contract;
        - busy workspace: refused (busy) without queueing;
        - a closed runtime: refused (closed);
        - a failed submit rolls the reservation back completely."""
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise LocalRuntimeError(
                "invalid_input", "intent_id 必须为非空字符串"
            )
        with self._mutex:
            if self._closed:
                raise LocalRuntimeError(
                    "closed", "运行时已关闭：不接受新请求"
                )
            record = self._intents.get(intent_id)
            if record is not None:
                if record.params_hash != params_hash:
                    raise LocalRuntimeError(
                        "intent_conflict",
                        f"同一意图 {intent_id!r} 已使用不同参数：明确“再次运行”请使用新意图",
                    )
                if record.finished:
                    return record.request_ref
                if self._slot is not None and self._slot.request_ref == record.request_ref:
                    return record.request_ref
            if intent_id in self._evicted_intents:
                raise LocalRuntimeError(
                    "intent_unconfirmed",
                    f"意图 {intent_id!r} 已超出本进程有界记录，无法确认其历史；"
                    "请使用新意图明确再次运行",
                )
            if record is None and self._forgotten_count > 0 and not allow_unknown:
                # ambiguity is real: this runtime has already forgotten
                # intents, so an unknown id might be one of them
                raise LocalRuntimeError(
                    "intent_unconfirmed",
                    f"意图 {intent_id!r} 不在本运行时可确认范围内且已有历史被淘汰，"
                    "无法确认其是否曾执行；新运行请由调用方以显式新意图声明提交",
                )
            if self._slot is not None:
                raise LocalRuntimeError(
                    "busy",
                    "该工作区已有一个活动任务：忙时拒绝，不排队；请等待当前任务结束后再提交",
                )
            state = _RequestState(
                request_ref=str(uuid4()),
                intent_id=intent_id,
                params_hash=params_hash,
            )
            self._intents[intent_id] = _IntentRecord(
                request_ref=state.request_ref, params_hash=params_hash, finished=False
            )
            self._slot = state
            try:
                self._executor.submit(self._execute, state, runner)
            except BaseException:
                # roll the reservation back completely - no phantom active
                # task, no phantom intent record
                self._slot = None
                if self._intents.get(intent_id) is not None and self._intents[
                    intent_id
                ].request_ref == state.request_ref:
                    del self._intents[intent_id]
                raise
            return state.request_ref

    def attach_run_ref(self, request_ref: str, run_ref: str | None) -> None:
        """THE formal-run-identity seam (V1-T1-C): the pipeline resolves
        the RunService run id and the runner attaches it here so the
        snapshot carries the FORMAL identity (never a fabricated one -
        run_ref stays None until the real id exists).

        Ownership-checked per request_ref: a late callback from an old
        request can never pollute a newer one; attaching to a finished
        request updates its finished projection (result queries need the
        formal id after completion)."""
        if not isinstance(run_ref, str) or not run_ref.strip():
            # the seam is only called when a formal identity EXISTS:
            # None/empty attach is a caller contract violation (None
            # stays None by never calling the seam)
            raise LocalRuntimeError(
                "invalid_input", "run_ref 必须为非空字符串的正式运行身份"
            )
        with self._mutex:
            if self._slot is not None and self._slot.request_ref == request_ref:
                self._slot.run_ref = run_ref
                return
            finished = self._finished_states.get(request_ref)
            if finished is not None and finished.request_ref == request_ref:
                finished.run_ref = run_ref
                return
        # unknown ref: a stale attach silently does nothing (the request
        # may have been evicted; it must not pollute anything else)

    def snapshot(self, request_ref: str) -> ExecutionSnapshot:
        """The independent frozen projection for one request ref; an
        unknown ref is honestly ``unconfirmed`` (never 'never executed',
        never a fabricated completed state)."""
        with self._mutex:
            if self._slot is not None and self._slot.request_ref == request_ref:
                return self._copy(self._slot)
            finished = self._finished_states.get(request_ref)
            if finished is not None:
                return self._copy(finished)
        return ExecutionSnapshot(
            request_ref=request_ref,
            intent_id="",
            run_ref=None,
            status="unconfirmed",
            phase="",
            subtask="",
            event_seq=0,
            observed=None,
            total=None,
            event_time="",
            execution_error=None,
            evidence_note="请求不在当前进程的有界记录中，无法确认",
            cleanup="uncertain",
            observer_failures=0,
            events=(),
        )

    def _copy(self, state: _RequestState) -> ExecutionSnapshot:
        return ExecutionSnapshot(
            request_ref=state.request_ref,
            intent_id=state.intent_id,
            run_ref=state.run_ref,
            status=state.status,
            phase=state.phase,
            subtask=state.subtask,
            event_seq=state.event_seq,
            observed=state.observed,
            total=state.total,
            event_time=state.event_time,
            execution_error=state.execution_error,
            evidence_note=state.evidence_note,
            cleanup=state.cleanup,
            observer_failures=state.observer_failures,
            events=tuple(state.events),
        )

    def close(self, wait: bool = True) -> None:
        """The OWNER's lifecycle close (directly-constructed instances;
        registry-shared instances refuse - see close_shared_runtime).

        Idempotent. After close, start() is stably refused (closed) with
        zero runner calls. A running request is NEVER killed and its
        workspace lock is NEVER released early: with wait=True this
        returns only after the in-flight request finished its own
        execution and cleanup (the final projection is the real
        outcome); with wait=False the request still runs to completion
        in its thread - threads cannot be safely killed and no cancelled
        contract exists."""
        with self._mutex:
            if self._closed:
                return
            if self._shared:
                raise LocalRuntimeError(
                    "close_forbidden",
                    "共享运行时由进程所有者管理：页面会话不得关闭；"
                    "所有者请使用close_shared_runtime",
                )
            self._closed = True
        self._executor.shutdown(wait=wait)

    def _publish(
        self,
        state: _RequestState,
        *,
        phase: str,
        subtask: str = "",
        observed: int | float | None = None,
        total: int | float | None = None,
    ) -> None:
        state.event_seq += 1
        state.phase = phase
        state.subtask = subtask
        state.observed = observed
        state.total = total
        state.event_time = datetime.now(UTC).isoformat()
        state.events.append(
            ExecutionEvent(
                seq=state.event_seq,
                phase=phase,
                subtask=subtask,
                observed=observed,
                total=total,
                event_time=state.event_time,
            )
        )

    def _execute(self, state: _RequestState, runner: Runner) -> None:
        """The executor-thread body (max_workers=1 is an implementation
        detail; admission already happened in start()): acquire the
        workspace RunLock BEFORE the runner's first side effect, run the
        runner with an isolated observer, finalize the projection, THEN
        release the slot through the ownership-checked path."""

        def observe(
            phase: str,
            subtask: str = "",
            observed: int | float | None = None,
            total: int | float | None = None,
        ) -> None:
            # observer failures are ISOLATED: never propagate into the
            # runner, never change the execution outcome
            try:
                with self._mutex:
                    self._publish(
                        state,
                        phase=phase,
                        subtask=subtask,
                        observed=observed,
                        total=total,
                    )
            except Exception:
                with self._mutex:
                    state.observer_failures += 1

        try:
            lock = RunLock(self._root / EXECUTION_LOCK_DIR, EXECUTION_LOCK_KEY)
            acquired = lock.acquire()
        except BaseException as exc:
            # a RAISING acquire (read-only workspace, corrupt guard file,
            # metadata write failure) is an honest refusal - never a
            # stranded 'admitted' slot: zero runner calls, the slot is
            # released, the uncertainty stays in the note
            with self._mutex:
                state.status = "lock_refused"
                state.execution_error = f"{type(exc).__name__}: {exc}"
                state.evidence_note = (
                    "工作区执行锁获取失败：本请求未执行、未排队、无任何副作用；"
                    "环境问题（只读工作区/guard文件损坏等）需先解决"
                )
                state.cleanup = "not_applicable"
                self._publish(state, phase="lock_refused")
                self._finalize_locked(state.request_ref)
            return
        if not acquired:
            with self._mutex:
                state.status = "lock_refused"
                state.evidence_note = (
                    "工作区执行锁被其他进程持有：本请求未执行、未排队、无任何副作用"
                )
                state.cleanup = "not_applicable"
                self._publish(state, phase="lock_refused")
                self._finalize_locked(state.request_ref)
            return
        try:
            with self._mutex:
                state.status = "running"
                self._publish(state, phase="running", subtask="runner")
            runner(observe, state.request_ref)
            with self._mutex:
                state.status = "execution_finished"
                state.evidence_note = (
                    "受控替身执行完成；无正式结果证据，不构成业务完成；资源释放尚未确认"
                    + ("；观察器存在失败，进展可能不完整" if state.observer_failures else "")
                )
                self._publish(state, phase="execution_finished")
        except BaseException as exc:  # the runner's own failure
            with self._mutex:
                state.status = "execution_failed"
                state.execution_error = f"{type(exc).__name__}: {exc}"
                # NO premature release claim: cleanup is still pending
                state.evidence_note = "执行异常；资源释放尚未确认；不构成业务成功"
                self._publish(state, phase="execution_failed")
        finally:
            try:
                lock.release()
            except BaseException as exc:
                # cleanup failed: keep the uncertainty, never fake release
                with self._mutex:
                    state.cleanup = "uncertain"
                    state.execution_error = (
                        (state.execution_error or "") + f"；锁释放失败（{exc}），资源状态不确定"
                    ).strip("；")
                    state.evidence_note = "执行结束但清理未确认：资源释放状态不确定"
                    self._publish(state, phase="cleanup_uncertain")
            else:
                with self._mutex:
                    # cleanup is now CONFIRMED - only here may the note
                    # say the resources were released
                    state.cleanup = "released"
                    if "资源释放尚未确认" in state.evidence_note:
                        state.evidence_note = state.evidence_note.replace(
                            "资源释放尚未确认", "资源已释放"
                        )
                    self._publish(state, phase="cleanup_released")
        # ownership-checked slot release: a stale finalize for an older
        # request can never clear a newer request's slot
        self._finalize_execution(state.request_ref)

    def _finalize_execution(self, request_ref: str) -> None:
        """THE single slot-release path (called by the executor thread
        that owns the request, and safely callable late): under the
        mutex it only clears the slot when the slot still belongs to
        THIS request."""
        with self._mutex:
            self._finalize_locked(request_ref)

    def _finalize_locked(self, request_ref: str) -> None:
        slot = self._slot
        if slot is None or slot.request_ref != request_ref:
            return  # a stale cleanup never touches a newer request
        self._finished_states[slot.request_ref] = slot
        while len(self._finished_states) > MAX_INTENT_RECORDS:
            oldest_ref = next(iter(self._finished_states))
            del self._finished_states[oldest_ref]
        self._slot = None
        record = self._intents.get(slot.intent_id)
        if record is not None and record.request_ref == slot.request_ref:
            record.finished = True
            self._intents.move_to_end(slot.intent_id)
            while len(self._intents) > MAX_INTENT_RECORDS:
                evicted_id, evicted_record = next(iter(self._intents.items()))
                if not evicted_record.finished:
                    break  # never evict an active record
                self._intents.popitem(last=False)
                self._evicted_intents.append(evicted_id)
                self._forgotten_count += 1


_RUNTIME_REGISTRY: dict[str, LocalRuntime] = {}
_REGISTRY_MUTEX = threading.Lock()


def get_shared_runtime(workspace_root: Path | str) -> LocalRuntime:
    """THE per-process runtime registry: the SAME LocalRuntime object is
    returned for the same RESOLVED workspace path, so two facade
    instances / UI sessions share one executor (never one per session;
    the key normalization matches the facade's - different spellings of
    one workspace cannot mint a second runtime). A NEW runtime instance
    (e.g. after hot reload) is still bound by the same workspace RunLock
    - it can never silently take over another process's active
    execution. Registry-owned instances refuse session-level close():
    only the process owner may close them via close_shared_runtime."""
    key = str(Path(workspace_root).resolve())
    with _REGISTRY_MUTEX:
        runtime = _RUNTIME_REGISTRY.get(key)
        if runtime is None:
            runtime = LocalRuntime(workspace_root)
            runtime._shared = True
            _RUNTIME_REGISTRY[key] = runtime
        return runtime


def close_shared_runtime(workspace_root: Path | str, wait: bool = True) -> bool:
    """The PROCESS OWNER's close for one shared runtime (never a page
    session): pops the registry entry and closes the runtime. Returns
    True if a runtime was closed, False when none was registered."""
    key = str(Path(workspace_root).resolve())
    with _REGISTRY_MUTEX:
        runtime = _RUNTIME_REGISTRY.pop(key, None)
    if runtime is None:
        return False
    runtime._shared = False
    runtime.close(wait=wait)
    return True


__all__ = [
    "EXECUTION_LOCK_DIR",
    "EXECUTION_LOCK_KEY",
    "ExecutionEvent",
    "ExecutionSnapshot",
    "LocalRuntime",
    "LocalRuntimeError",
    "close_shared_runtime",
    "StartHandle",
    "get_shared_runtime",
]
