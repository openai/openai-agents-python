"""One native conversation and exact per-submission ownership."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import sys
import weakref
from collections import deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    Sequence,
)
from concurrent.futures import Future
from contextlib import AbstractAsyncContextManager, contextmanager, suppress
from contextvars import ContextVar, copy_context
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from os import PathLike
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any, Generic, Literal, Protocol, TypeAlias, TypeVar, cast, get_args, overload
from uuid import uuid4

from ._compat import cancel_once
from ._events import SessionItem
from ._function_tools import FunctionTool, Tool, ToolContext
from ._history import project_history
from ._native_async import _NativeCompletionError
from ._projection import _error_code, project_execution_completion
from ._trace_context import TraceContext, _native_trace_context, _trace_context
from .agent import (
    Agent,
    CodexAccount,
    ContextT,
    ModelSettings,
    NativeProvider,
    OutputT,
    _copy_model_settings,
    _SyncRun,
)
from .canonical_storage import CustomStorage
from .executor import Environment, EnvironmentSource, ExecutorContext, _writable_roots
from .history import RecordedTurn
from .input import UserInput, _native_input
from .manual import (
    _FORM_MODES,
    _URL_ACCEPT,
    ActionReply,
    ActionRequested,
    ElicitationRequest,
    Interaction,
    VerificationRequest,
    _CapturedManual,
    _NativeQuestion,
    _question,
)
from .mcp import (
    _MCP,
    ChatGPTAppsMCP,
    HostedVerificationMCP,
    HttpMCP,
    MCPResultContext,
    StdioMCP,
    _ready,
    _result_policies,
    _tool_names,
)
from .native_agents import (
    _COLLABORATION_TOOLS,
    ChildInvocation,
    NativeAgents,
)
from .native_interception import (
    Allow,
    Deny,
    ReplaceArguments,
    StopTurn,
    _CellPolicyOwner,
    _has_interceptors,
    _NativeInterception,
)
from .observation import (
    ApiRateLimitsUpdated,
    ContextCompacted,
    EventStream,
    ExecutionItemCompleted,
    OutputTextDelta,
    SubagentLifecycle,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
    _ObservationBuffer,
    _request_observation,
)
from .rate_limits import ApiRateLimits, _api_rate_limits
from .recovery import LocalRecoveryRequired, ReconciledCall, UncertainCall
from .schema import OutputSpec, OutputValidationError, _prepare_spec, _PreparedOutput
from .state import ChecklistState, CheckpointStatus, LocalStorage, NativeGoal, SessionState
from .subagents import Subagent, _subagent
from .telemetry import (
    SpanContext,
    TraceOptions,
    TraceScope,
    _activate,
    _capture,
    _CapturedTrace,
    _clear_current,
    _create_task,
    _owned_span,
)
from .tools import (
    Checklist,
    NativeGoals,
    ToolCallInfo,
    ToolOutput,
    ToolRef,
    WebSearch,
    _CapturedFunction,
    _ToolCatalog,
)
from .turn import PendingRequests, SteeringReceipt, Turn, _child_turn, _root_turn
from .turn_receipt import _load_instruction_files, _redact_checkpoint_executor_receipts
from .usage import TokenUsageInfo, _token_usage_info

_MutationResultT = TypeVar("_MutationResultT")
_ResultT = TypeVar("_ResultT")
_TurnT = TypeVar("_TurnT")
_SelectionContextT_contra = TypeVar("_SelectionContextT_contra", contravariant=True)
_NATIVE_HOME_CLOSE_TIMEOUT = 30.0
_IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST = hashlib.sha256(b"implicit-remote-workspace").hexdigest()


def _native_model_settings(model: str | None, settings: ModelSettings | None) -> dict[str, Any]:
    if model is not None and not isinstance(model, str):
        raise TypeError("model must be a string or None")
    result = {
        {"reasoning_effort": "effort", "reasoning_summary": "summary"}.get(key, key): value
        for key, value in _copy_model_settings(settings).items()
    }
    if model is not None:
        result["model"] = model
    return result


def _manual_restore_model_settings(state: SessionState) -> tuple[str, dict[str, Any]]:
    """Read only accepted model fields; connection and tool authority stay current."""
    metadata = state.root_metadata
    model = metadata.get("model") if metadata is not None else None
    captured = None
    for row in reversed(state.history):
        payload = row.get("payload")
        if (
            row.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "thread_settings_applied"
            and payload.get("thread_id") == state.source_session_id
        ):
            captured = payload.get("thread_settings")
            break
    if (
        not isinstance(model, str)
        or not model.strip()
        or not isinstance(captured, dict)
        or captured.get("model") != model
    ):
        raise ValueError("Manual checkpoint has invalid accepted model settings")
    settings: dict[str, Any] = {"model": model}
    for source, target in (
        ("reasoning_effort", "effort"),
        ("reasoning_summary", "summary"),
        ("service_tier", "service_tier"),
    ):
        value = captured.get(source)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Manual checkpoint has invalid accepted model settings")
        # Absent effort/summary keep the native isolated configuration defaults.
        # The existing settings operation accepts an explicit null service tier.
        if value is not None or target == "service_tier":
            settings[target] = value
    return model, settings


class SessionBusyError(RuntimeError):
    """An idle-only start was requested while the Session owns active work."""


class CapabilityError(ValueError):
    """The requested operation is outside this Session's admitted capability."""


class StaleTurnError(RuntimeError):
    """This Turn cannot control the requested owner or its current work."""


class InteractionExpiredError(RuntimeError):
    """The captured interaction no longer has reply authority."""


class TurnStoppedError(RuntimeError):
    """The root Turn stopped and its owned cleanup completed.

    This is the work's result, not cancellation of a task waiting for it.
    It does not promise rollback of earlier effects.
    """


@dataclass(frozen=True)
class FeatureSupport:
    """Availability for this configuration, never a readiness or effect receipt."""

    status: Literal["supported", "limited", "unsupported"]
    reason: str
    scope: str


FeatureName: TypeAlias = Literal[
    "native_output",
    "prompted_output",
    "manual_suspend",
    "native_output_manual_restore",
    "tool_selection",
    "function_policy",
    "mcp_before_policy",
    "mcp_result_policy",
    "environment_before_policy",
    "environment_code_result_policy",
    "native_agents",
    "remote_skills",
    "checklist",
]


class Capabilities:
    """Live support for the current admitted configuration.

    Output features describe modes available to new root Turns, not the last
    output mode. Tool reports use the last acknowledged roster, never a candidate
    being published. They do not reserve the right to mutate or checkpoint.
    """

    def __init__(self, session: Session[Any, Any]) -> None:
        self._session = session

    @property
    def websockets_allowed(self) -> bool:
        """Whether this provider permits WebSockets, not the negotiated transport."""
        self._session._check_open()
        return self._session._websockets_enabled

    def get(self, feature: FeatureName) -> FeatureSupport:
        """Describe actual coverage without performing admission, discovery or I/O."""
        if feature not in get_args(FeatureName):
            raise ValueError("Unknown feature name")
        s = self._session
        s._check_open()
        if feature == "native_output":
            return FeatureSupport(
                "supported",
                "Schema-constrained output for root Turns",
                "Settled manual restore requires a matching output declaration; "
                "native child Turns remain plain text",
            )
        if feature == "prompted_output":
            if s._storage is not None:
                return FeatureSupport(
                    "limited",
                    "LocalStorage supports prompted output with max_retries=0",
                    "Use an ephemeral Session for nonzero retries",
                )
            return FeatureSupport(
                "supported",
                "Prompt guidance with optional bounded tool-free correction",
                "New root Turns and their automatic successors",
            )
        if feature == "native_output_manual_restore":
            return FeatureSupport(
                "supported",
                "Settled manual restore preserves the captured native schema",
                "Same root Turn and matching output declaration; capture readiness is separate",
            )
        if feature == "manual_suspend":
            if s._storage is not None:
                return FeatureSupport(
                    "unsupported",
                    "LocalStorage cannot export SessionState",
                    "Use the existing local storage profile",
                )
            if not s._catalog.manual_slots():
                return FeatureSupport("unsupported", "No active manual function", "Current roster")
            return FeatureSupport(
                "limited",
                "Settled manual Turns can be suspended with their output contract",
                "Settled native values are retained; active code and root before_step "
                "callbacks cannot suspend",
            )
        if feature == "tool_selection":
            if s._native_agents is not None:
                return FeatureSupport(
                    "limited",
                    "Root and native children select tools before each model step",
                    "Opening and operation ceilings apply; "
                    "live native children limit tool replacement",
                )
            return FeatureSupport(
                "supported",
                "Root may select admitted tools before each model step",
                "Opening and operation ceilings apply to every tool family",
            )
        if feature == "function_policy":
            if any(isinstance(tool, _CapturedFunction) for tool in s._bound_tools):
                return FeatureSupport(
                    "supported",
                    "Opening Python callbacks support controls",
                    "Before/after on selected callbacks only, including native children",
                )
            return FeatureSupport(
                "unsupported",
                "No Python callback captured at opening",
                "Callback-less functions cannot carry policies",
            )
        if feature == "mcp_before_policy":
            if not s._mcp_tools:
                return FeatureSupport("unsupported", "No selected MCP tools", "Current roster")
            return FeatureSupport(
                "supported",
                "Selected MCP tools support before/approval",
                "HTTP or stdio; direct or code; current acknowledged roster",
            )
        if feature == "mcp_result_policy":
            if not s._mcp_tools:
                return FeatureSupport("unsupported", "No selected MCP tools", "Current roster")
            return FeatureSupport(
                "supported",
                "Selected MCP tools support result policies",
                "HTTP or stdio; current acknowledged roster",
            )
        environment = s._executor
        if feature == "environment_before_policy":
            if environment is None or not environment.tools:
                return FeatureSupport(
                    "unsupported", "No selected executor tools", "Current Environment"
                )
            return FeatureSupport(
                "supported",
                "Selected executor tools support before/approval",
                "Current Environment; root and admitted native children",
            )
        if feature == "environment_code_result_policy":
            if environment is None or not environment.tools:
                return FeatureSupport(
                    "unsupported", "No selected executor tools", "Current Environment"
                )
            return FeatureSupport(
                "supported",
                "Controls selected executor results before native consumers",
                "Direct and code calls; raw output events withheld; effects remain",
            )
        if feature == "native_agents":
            if s._native_agents is None:
                return FeatureSupport(
                    "unsupported",
                    "NativeAgents was not granted at opening",
                    "Historical or independent child Sessions grant no native children",
                )
            return FeatureSupport(
                "supported",
                "NativeAgents and its Environment were admitted",
                "Model and instructions stay fixed; owned prompts require interactions=True",
            )
        if feature == "remote_skills":
            if environment is None:
                return FeatureSupport("unsupported", "No remote Environment", "No host fallback")
            if not environment.capability_directories:
                return FeatureSupport(
                    "unsupported",
                    "No remote discovery roots selected",
                    "No host, saved or source roots",
                )
            return FeatureSupport(
                "supported",
                "Destination remote discovery roots were selected",
                "Discovery only, not filesystem authority or proof a skill exists",
            )
        assert feature == "checklist" and s._opening_agent is not None
        tools = s._opening_agent.tools
        opening = tools
        if any(isinstance(tool, Checklist) for tool in opening):
            return FeatureSupport(
                "supported",
                "update_plan was granted at this opening",
                "Root and admitted native children have separate readback",
            )
        return FeatureSupport(
            "unsupported",
            "Checklist was not granted at this opening",
            "Historical readback is permitted; it grants no updates",
        )

    def require(self, feature: FeatureName, *, allow_limited: bool = False) -> None:
        """Reject unsupported coverage and, by default, limited coverage."""
        support = self.get(feature)
        if support.status == "unsupported" or support.status == "limited" and not allow_limited:
            raise CapabilityError(f"{feature}: {support.reason}. Scope: {support.scope}")


ResponseIntegrityReason: TypeAlias = Literal[
    "undecodable_output_item",
    "provider_owned_tool_receipt",
    "unfinished_assistant_item",
    "missing_supported_terminal_output",
]


class NativeError(RuntimeError):
    """The native conversation could not complete the submitted operation.

    ``status_code``, ``code`` and ``request_id`` contain available provider
    metadata. ``code`` is the upstream error code; ``category`` is the normalized
    native failure category. Missing or discarded values remain ``None``.
    Messages and metadata may contain provider-supplied information.
    effects describes only the failed operation, never earlier actions.
    unknown forbids assuming replay is safe.
    """

    _effects: Literal["not_started", "unknown", "recorded"] = "unknown"
    _interrupted: bool = False

    def __init__(
        self,
        *args: object,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        category: str | None = None,
    ) -> None:
        super().__init__(*args)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.category = category
        self._response_integrity_reason: ResponseIntegrityReason | None = None

    @property
    def response_integrity_reason(self) -> ResponseIntegrityReason | None:
        """Captured model-output cause, when available for this selected failure.

        Restored or ambiguously owned errors can leave this unknown.
        A reason does not establish that earlier operations had no effects.
        """
        return self._response_integrity_reason

    @property
    def effects(self) -> Literal["not_started", "unknown", "recorded"]:
        """Effect outcome proved for this exact failure, otherwise unknown."""
        return self._effects


def _model_failure_error(message: dict[str, Any]) -> NativeError:
    """Construct once from the captured native notice, never a later terminal."""
    error = NativeError(message["message"])
    reason = message.get("response_integrity_reason")
    if isinstance(reason, str) and reason in get_args(ResponseIntegrityReason):
        error._response_integrity_reason = cast(ResponseIntegrityReason, reason)
    return error


def _native_error(error: dict[str, Any], message: str | None = None) -> NativeError:
    metadata = error.get("_sdk_provider_error") or {}
    result = NativeError(
        error["message"] if message is None else message,
        status_code=metadata.get("status_code"),
        code=metadata.get("code"),
        request_id=metadata.get("request_id"),
        category=_error_code(error),
    )
    reason = error.get("_sdk_response_integrity_reason")
    if isinstance(reason, str) and reason in get_args(ResponseIntegrityReason):
        result._response_integrity_reason = cast(ResponseIntegrityReason, reason)
    return result


class InteractionRequired(NativeError):
    """The operation needs an application decision.

    state is a receipt on a Turn joined by Session.suspend()
    or DeferredTools.prepare(), after validating its native manual continuation.
    A one-shot pending manual restore without a handler returns its input state.
    Other interaction errors have state=None.
    Save a receipt, not this exception or its traceback.
    """

    state: SessionState | None = None


class _ManualBoundaryLost(RuntimeError):
    """No settled manual boundary remains at the pre-seal check."""


@dataclass(frozen=True)
class Completed(Generic[OutputT]):
    """One completed root or captured child turn.

    Usage is None when the native runtime has no usage information. It is a
    cumulative thread snapshot, not a per-submission cost. It remains valid
    after the Session closes or later submissions complete. A child result
    retains its exact Turn[str] and the latest usage reported by that
    child thread before the captured turn completed. Missing child usage is
    None; root results do not aggregate child usage.

    repair_attempts retains the real RecordedTurn receipts for opted-in
    prompted-output correction. Their per-turn usage is separate from usage,
    which still reports only the original thread. Missing usage remains None.
    final_output may be derived from a repair; native task history stays intact.
    state is populated only on Agent.run/run_sync with return_state=True.
    Existing native checkpoint restrictions still apply.
    """

    turn: Turn[OutputT]
    final_output: OutputT
    usage: TokenUsageInfo | None = None
    state: SessionState | None = field(default=None, repr=False)
    repair_attempts: tuple[RecordedTurn, ...] = ()


def _future() -> asyncio.Future[Any]:
    future = asyncio.get_running_loop().create_future()
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return future


@dataclass
class _Submission:
    output: _PreparedOutput
    accepted: asyncio.Future[Any] = field(default_factory=_future)
    completion: asyncio.Future[Any] = field(default_factory=_future)
    terminal: asyncio.Future[Any] = field(default_factory=_future)
    failure: asyncio.Future[Any] = field(default_factory=_future)
    handle: Turn[Any] | None = None
    issued: Turn[Any] | None = None
    task: asyncio.Task[None] | None = None
    callbacks: set[asyncio.Task[None]] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    mcp_calls: dict[str, str] = field(default_factory=dict)
    stopping: bool = False
    cleanup_error: BaseException | None = None
    deferred_close: tuple[bool, BaseException | None, Completed[Any] | None] | None = None
    manual_inputs: dict[int, Any] = field(default_factory=dict)
    questions: dict[int, _NativeQuestion] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    restored_pending: bool = False
    retire_manual: bool = False
    graceful_requested: bool = False
    unsampled_steering: bool = False
    continued_from: str | None = None
    last_assistant_had_blank_final: bool = False
    policy_stop: Literal["requested", "expired"] | None = None
    repairing: bool = False
    before_step: _BeforeStep[Any] | None = None
    step_index: int = 0
    operation_slots: tuple[int, ...] | None = None
    operation_mcp: tuple[_MCP[Any], ...] | None = None
    operation_refs: tuple[ToolRef, ...] | None = None
    writable_roots: tuple[str, ...] | None = None
    parent_trace: TraceContext | None = field(default=None, repr=False)
    tracing: _CapturedTrace | None = field(default=None, repr=False)

    def add_callback(self, task: asyncio.Task[None]) -> None:
        self.callbacks.add(task)
        task.add_done_callback(self._callback_finished)

    def _callback_finished(self, task: asyncio.Task[None]) -> None:
        self.changed.set()

    async def wait_for_callbacks(self) -> None:
        # Admission remains open while this submission owns the turn. A
        # returned callback may cause another call after any earlier snapshot.
        while pending := tuple(task for task in self.callbacks if not task.done()):
            await asyncio.wait((*pending, self.failure), return_when=asyncio.FIRST_COMPLETED)
            if self.failure.done():
                raise self.failure.result()
        if self.failure.done():
            raise self.failure.result()

    def retain_cleanup_error(self: _CallbackState, error: BaseException) -> None:
        if self.cleanup_error is None:
            self.cleanup_error = error
        else:
            _retain_cleanup_error(
                self.cleanup_error,
                error,
                _error_contexts(self.cleanup_error.__context__),
            )


@dataclass
class _ChildCallbacks:
    """Application callbacks for one native child incarnation and turn."""

    handle: ChildInvocation
    request_keys: list[str] = field(default_factory=list)
    native_instance: int | None = None
    callbacks: set[asyncio.Task[None]] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    terminal: asyncio.Future[Any] = field(default_factory=_future)
    observed: asyncio.Future[Any] = field(default_factory=_future)
    failure: asyncio.Future[Any] = field(default_factory=_future)
    completion: asyncio.Future[Any] = field(default_factory=_future)
    task: asyncio.Task[None] | None = None
    continuation: asyncio.Future[None] | None = None
    stopping: bool = False
    policy_stop: Literal["requested", "expired"] | None = None
    cleanup_error: BaseException | None = None
    questions: dict[int, _NativeQuestion] = field(default_factory=dict)
    manual_inputs: dict[int, Any] = field(default_factory=dict)
    step_index: int = 0
    # Retain observed calls only to retire the owning turn on rejection.

    retain_cleanup_error = _Submission.retain_cleanup_error

    def add_callback(self, task: asyncio.Task[None]) -> None:
        self.callbacks.add(task)

    @property
    def busy(self) -> bool:
        return not self.completion.done() or (
            self.continuation is not None and not self.continuation.done()
        )

    def stop_continuation(self) -> None:
        # An all-graph stop also revokes an ACK still in transit for this
        # completed owner. Ordinary callback stopping does not carry that right.
        if self.continuation is None:
            self.continuation = _future()
            self.continuation.set_result(None)


_CallbackState = _Submission | _ChildCallbacks
_InteractionHandler = Callable[[Interaction], ActionReply[Any] | Awaitable[ActionReply[Any]]]


class _BeforeStep(Protocol[_SelectionContextT_contra]):
    def __call__(
        self, turn: Turn[Any], /, *, step_index: int
    ) -> (
        Sequence[
            ToolRef
            | Tool[_SelectionContextT_contra]
            | Environment[_SelectionContextT_contra]
            | NativeAgents
        ]
        | None
        | Awaitable[
            Sequence[
                ToolRef
                | Tool[_SelectionContextT_contra]
                | Environment[_SelectionContextT_contra]
                | NativeAgents
            ]
            | None
        ]
    ): ...


@dataclass
class _Invocation:
    session: object
    active: bool = True
    # Keep enclosing guards live even in tasks copied from this context.
    parent: _Invocation | None = field(default_factory=lambda: _invocation.get(), repr=False)


_invocation: ContextVar[_Invocation | None] = ContextVar("native_agent_invocation", default=None)
_CODEX_RESPONSES_BASE_URL = "https://chatgpt.com/backend-api/codex"


@dataclass(repr=False)
class _RestoreDeclaration:
    agent: Agent[Any, Any]
    declaration: Agent[Any, Any]
    api_key: str | None
    output: _PreparedOutput
    state: SessionState = field(init=False)
    owner: asyncio.Task[Any] | None = None


_restore_declaration: ContextVar[_RestoreDeclaration | None] = ContextVar(
    "native_restore_declaration", default=None
)


@contextmanager
def _private_task(
    work: Coroutine[Any, Any, _ResultT],
) -> Iterator[tuple[asyncio.Task[None], Future[_ResultT]]]:
    # Asyncio may format a finished Task before its caller can map a private outcome.
    outcome: Future[_ResultT] = Future()
    started = False

    async def run() -> None:
        nonlocal started
        started = True
        try:
            outcome.set_result(await work)
        except BaseException as error:
            outcome.set_exception(error)

    owner = run()
    try:
        yield asyncio.create_task(owner), outcome
    finally:
        if not started:
            owner.close()
            work.close()


async def _join(task: asyncio.Future[Any], *, outcome: Future[Any] | None = None) -> Any:
    """Join owned cleanup before propagating cancellation of its passive waiter."""
    cancelled = None
    try:
        while True:
            try:
                value = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                if task.done() and task.cancelled():
                    task.result()
                cancelled = cancelled or error
                if task.done():
                    value = task.result()
                    break
        if outcome is not None:
            value = outcome.result()
    except BaseException as error:
        if cancelled is None:
            raise
        _retain_cleanup_error(cancelled, error, _error_contexts(cancelled.__context__))
        raise cancelled  # noqa: B904 - keep the caller's explicit cause and owned cleanup contexts
    if cancelled is not None:
        raise cancelled
    return value


def _error_contexts(error: BaseException | None) -> tuple[BaseException, ...]:
    contexts: list[BaseException] = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        contexts.append(error)
        error = error.__context__
    return tuple(contexts)


def _retain_cleanup_error(
    primary: BaseException,
    cleanup_error: BaseException,
    previous: tuple[BaseException, ...],
) -> None:
    """Retain each distinct context without replacing explicit causes."""
    contexts: list[BaseException] = []
    seen = {id(primary)}
    for error in _error_contexts(cleanup_error) + _error_contexts(primary.__context__) + previous:
        if id(error) not in seen:
            seen.add(id(error))
            contexts.append(error)
    context: BaseException | None = None
    for error in reversed(contexts):
        error.__context__ = context
        context = error
    primary.__context__ = context


async def _close_after_error(primary: BaseException, close: Callable[[], Awaitable[None]]) -> None:
    """Join close and retain its failures before exception transport can replace context."""
    previous = _error_contexts(primary.__context__)

    async def capture() -> tuple[BaseException, tuple[BaseException, ...]] | None:
        # Eager task factories must also leave the caller's active error handler.
        await asyncio.sleep(0)
        try:
            await close()
        except BaseException as error:  # retain every cleanup failure
            return error, _error_contexts(error.__context__)
        return None

    pending = asyncio.create_task(capture())
    try:
        await _join(pending)
    except asyncio.CancelledError:
        # The first failure wins; _join has finished the owned close task.
        pass
    captured = pending.result()
    if captured is not None:
        cleanup_error, contexts = captured
        _retain_cleanup_error(primary, cleanup_error, contexts + previous)


class _OpeningOwner:
    """Join Python publication; exact native slots remain registered by the lease."""

    def __init__(self) -> None:
        self.lease: Any = None
        self._environment_reservation: Any = None
        self.api_key: str | None = None

    async def acquire(self, operation: Any) -> Any:
        pending = asyncio.ensure_future(operation)
        try:
            return await asyncio.shield(pending)
        except BaseException:
            with suppress(BaseException):
                await _join(pending)
            raise

    def disown(self, session: Session[Any, Any]) -> None:
        self.lease.adopt()
        session._model_api_key = self.api_key
        self.api_key = None

    async def _close_orphan(self) -> None:
        await _finish_native_home_cleanup(self.lease.close_orphan)
        self.lease = None

    def defer_environment(self, environment: _EnvironmentLease) -> None:
        if self._environment_reservation is not None:
            self._environment_reservation.orphan()
        environment.detach_borrower()

    async def fail_environment(self, environment: _EnvironmentLease) -> None:
        # Native resources can still be using the remote executor. A failed
        # home deletion is not such a user and must not strand its manager.
        if self.lease is not None:
            await self.lease.close_orphan_resources()
        try:
            try:
                await _release_environment(environment)
            except BaseException as error:
                if self.lease is not None:
                    await _close_after_error(error, self._close_orphan)
                raise
            else:
                if self.lease is not None:
                    await self._close_orphan()
        finally:
            if self._environment_reservation is not None:
                self._environment_reservation.release()
                self._environment_reservation = None

    async def fail(
        self, session: Any, transport: Any, environment: _EnvironmentLease | None
    ) -> None:
        failure: BaseException | None = None
        state: _Submission | None = None
        if session is not None:
            candidates = [getattr(session, name, None) for name in ("_reader", "_mcp_reader")]
            state = session._active
            if state is not None:
                candidates.append(state.task)
            workers = [worker for worker in candidates if worker is not None]
            for worker in workers:
                cancel_once(worker)
            interception = getattr(session, "_interception", None)
            closing = [interception.close()] if interception is not None else []
            # Retire every armed worker before releasing this unpublished owner.
            outcomes = await asyncio.gather(*workers, *closing, return_exceptions=True)
            if state is not None and state.cleanup_error is not None:
                outcomes.append(state.cleanup_error)
            for error in outcomes:
                if isinstance(error, BaseException) and not isinstance(
                    error, asyncio.CancelledError
                ):
                    if failure is None:
                        failure = error
                    else:
                        _retain_cleanup_error(failure, error, _error_contexts(failure.__context__))
        if environment is not None:
            # Native orphan retirement also releases its Provider reservation.
            # Keep this opening reserved through the admitted manager's exit.
            # Acquire before orphaning so concurrent aclose never sees a gap.
            self._environment_reservation = transport.acquire()
        self.lease.orphan()
        cleanup = (
            self.lease.close_orphan_resources if environment is not None else self._close_orphan
        )
        try:
            if failure is not None:
                await _close_after_error(failure, cleanup)
                raise failure
            await cleanup()
        finally:
            if state is not None and state.tracing is not None:
                state.tracing.close(failure)
                state.tracing = None


async def _finish_native_home_cleanup(cleanup: Callable[[], Awaitable[None] | None]) -> None:
    # Core's Windows stdio termination returns after requesting process exit.
    # The process may still hold its cwd in this private home. Native cleanup
    # retains the directory/lease after failure, so retry actual removal only
    # for that sharing violation. Match Core's bounded close window.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _NATIVE_HOME_CLOSE_TIMEOUT
    first_error: RuntimeError | None = None
    while True:
        try:
            pending = cleanup()
            if pending is not None:
                await pending
            return
        except RuntimeError as error:
            if (
                sys.platform != "win32"
                or not str(error).endswith("(os error 32)")
                or "; additional cleanup failure:" in str(error)
            ):
                raise
            if first_error is None:
                first_error = error
        if loop.time() >= deadline:
            raise first_error
        await asyncio.sleep(0.01)


async def _call_callback(
    callback: Callable[..., Any], admit: Callable[[], bool], /, *args: Any, **kwargs: Any
) -> Any:
    with _owned_span("callback", "callback"):
        return await _call_callback_body(callback, admit, *args, **kwargs)


async def _call_callback_body(
    callback: Callable[..., Any],
    admit: Callable[[], bool],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run Python in its appropriate executor and retain ownership until exit."""
    if inspect.iscoroutinefunction(callback):
        return await callback(*args, **kwargs)
    loop = asyncio.get_running_loop()

    async def admission() -> bool:
        # The asyncio and native ownership check belongs to the application's loop.
        return admit()

    def invoke_if_admitted() -> Any:
        if asyncio.run_coroutine_threadsafe(admission(), loop).result():
            return callback(*args, **kwargs)
        return None

    worker = loop.run_in_executor(None, copy_context().run, invoke_if_admitted)
    cancelled = None
    while not worker.done():
        try:
            # Waiting must not cancel a running worker. A cancelled shield can
            # also log the raw callback exception before its owner sanitizes it.
            await asyncio.wait((worker,))
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
    value = worker.result()
    if cancelled is not None:
        # A synchronous wrapper may return a coroutine. Cancellation must
        # not start that body after the callback's authority has expired.
        if inspect.iscoroutine(value):
            value.close()
        raise cancelled
    if inspect.isawaitable(value):
        try:
            allowed = admit()
        except BaseException:
            if inspect.iscoroutine(value):
                value.close()
            raise
        if not allowed:
            # Declined work must not start the coroutine's body on the way out.
            if inspect.iscoroutine(value):
                value.close()
            return None
        return await value
    return value


def _fixed_tool_refs(
    tools: Sequence[Tool[Any]],
    programmatic_tool_calling: bool,
    environment: Environment[Any] | None,
    native_agents: NativeAgents | None,
) -> set[ToolRef]:
    """Derive selectors only from native grants captured for this opening."""
    from .native_agents import _COLLABORATION_TOOLS

    fixed: set[ToolRef] = set()
    for tool in tools:
        if isinstance(tool, Checklist):
            fixed.add(ToolRef("update_plan", "functions"))
        elif isinstance(tool, NativeGoals):
            fixed.update(
                ToolRef(n, "functions") for n in ("create_goal", "get_goal", "update_goal")
            )
        elif isinstance(tool, WebSearch):
            fixed.add(
                ToolRef("run", "web")
                if tool.route == "standalone"
                else ToolRef("web_search", "functions")
            )
    if programmatic_tool_calling:
        fixed.update((ToolRef("exec", "functions"), ToolRef("wait", "functions")))
    if environment is not None:
        fixed.update(ToolRef(name, "functions") for name in environment.tools)
    if native_agents is not None:
        fixed.update(ToolRef(name, "collaboration") for name in _COLLABORATION_TOOLS)
    return fixed


def _split_tools(
    tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents],
    programmatic_tool_calling: bool = False,
    *,
    available: Sequence[Tool[ContextT]] | None = None,
    environment: Environment[Any] | None = None,
    native_agents: NativeAgents | None = None,
    ceiling: Sequence[Tool[ContextT]] | None = None,
    rebind: bool = False,
) -> tuple[tuple[FunctionTool[Any, Any, Any], ...], tuple[_MCP[Any], ...], NativeAgents | None]:
    opening = ceiling if ceiling is not None else tools if available is None else available
    admitted: dict[ToolRef, FunctionTool[Any, Any, Any] | HttpMCP[Any] | StdioMCP[Any]] = {}
    mcp_raw_names: dict[ToolRef, str] = {}
    mcp_pairs: set[tuple[str, str]] = set()
    fixed: set[ToolRef] = set()
    retained: set[ToolRef] = set()
    if available is not None:
        fixed = _fixed_tool_refs(
            ceiling if ceiling is not None else available,
            programmatic_tool_calling,
            environment,
            native_agents,
        )
        for declaration in opening:
            if isinstance(declaration, FunctionTool):
                current = next(
                    item
                    for item in available
                    if isinstance(item, FunctionTool) and item.name == declaration.name
                )
                admitted[declaration.ref] = current
            elif isinstance(declaration, (StdioMCP, HttpMCP)):
                current_server = next(
                    item
                    for item in available
                    if isinstance(item, (StdioMCP, HttpMCP)) and item.name == declaration.name
                )
                for raw in declaration.tools:
                    ref = declaration.tool(raw)
                    admitted[ref] = current_server
                    mcp_raw_names[ref] = raw
                    mcp_pairs.add((declaration.name, raw))
    python_tools: list[FunctionTool[Any, Any, Any]] = []
    mcp_tools: dict[str, _MCP[Any]] = {}
    has_checklist = any(isinstance(item, Checklist) for item in opening)
    has_goals = any(isinstance(item, NativeGoals) for item in opening)
    if sum(isinstance(item, WebSearch) for item in opening) > 1:
        raise ValueError("Declare WebSearch only once per Session")
    for item in tools:
        tool = item
        if available is not None and isinstance(tool, (Environment, NativeAgents)):
            if tool is not environment and tool is not native_agents:
                raise CapabilityError("Operation tools must use captured opening bindings")
            refs = _fixed_tool_refs(
                (),
                False,
                tool if isinstance(tool, Environment) else None,
                tool if isinstance(tool, NativeAgents) else None,
            )
            if retained.intersection(refs):
                raise CapabilityError("Operation tools must select distinct fixed capabilities")
            retained.update(refs)
            continue
        elif isinstance(tool, ToolRef):
            if tool in fixed:
                if tool in retained:
                    raise CapabilityError("Operation tools must select distinct fixed capabilities")
                retained.add(tool)
                continue
            if tool not in admitted:
                raise CapabilityError(
                    f"Tool {tool!r} must have been allowed when the Session opened"
                )
            resolved = admitted[tool]
            if isinstance(resolved, FunctionTool):
                tool = resolved
            else:
                raw = mcp_raw_names[tool]
                tool = replace(resolved, tools=(raw,))
        elif available is not None:
            if isinstance(tool, (HostedVerificationMCP, ChatGPTAppsMCP)) and not any(
                tool is original for original in (*opening, *available)
            ):
                raise ValueError("Apps registration is fixed; use a separate Session")
            if not rebind and not any(tool is original for original in (*opening, *available)):
                raise CapabilityError("Operation tools must use captured opening bindings")
            if isinstance(tool, FunctionTool):
                if tool.ref not in admitted or not isinstance(admitted[tool.ref], FunctionTool):
                    raise CapabilityError(
                        "Replacement tool names must have been allowed when the Session opened"
                    )
            elif isinstance(tool, (HttpMCP, StdioMCP)):
                if any((tool.name, raw) not in mcp_pairs for raw in tool.tools):
                    raise CapabilityError(
                        "Replacement MCP names must have been allowed when the Session opened"
                    )
                if (
                    isinstance(tool, StdioMCP)
                    and tool.location == "host"
                    and not any(
                        isinstance(item, StdioMCP) and item.location == "host" for item in opening
                    )
                ):
                    raise CapabilityError("Host stdio MCP must be declared when the Session opens")
            if not rebind or not isinstance(tool, (FunctionTool, HttpMCP, StdioMCP)):
                if not any(tool is original for original in (*opening, *available)):
                    raise CapabilityError("Operation tools must use captured opening bindings")
                if isinstance(tool, FunctionTool):
                    tool = admitted[tool.ref]
                elif isinstance(tool, (HttpMCP, StdioMCP)):
                    # An opening object is a selector, never authority to restore
                    # its old endpoint or credentials after an acknowledged update.
                    resolved = admitted[tool.tool(tool.tools[0])]
                    assert isinstance(resolved, (HttpMCP, StdioMCP))
                    tool = replace(resolved, tools=tool.tools)
        if isinstance(tool, FunctionTool):
            if has_checklist and tool.name == "update_plan":
                raise ValueError("Checklist reserves the tool name update_plan")
            if has_goals and tool.name in ("create_goal", "get_goal", "update_goal"):
                raise ValueError("NativeGoals reserves create_goal, get_goal and update_goal")
            if programmatic_tool_calling and tool.name in ("exec", "wait"):
                raise ValueError("programmatic_tool_calling reserves the tool names exec and wait")
            if available is not None and any(
                previous.name == tool.name for previous in python_tools
            ):
                raise CapabilityError("Operation tools must select distinct captured slots")
            python_tools.append(tool)
        elif isinstance(tool, (StdioMCP, HttpMCP)):
            previous = mcp_tools.get(tool.name)
            if previous is not None:
                if available is None or replace(tool, tools=previous.tools) != previous:
                    raise ValueError("MCP server aliases have colliding native namespaces")
                if set(previous.tools).intersection(tool.tools):
                    raise CapabilityError("Operation tools must select distinct MCP leaves")
                tool = replace(tool, tools=(*previous.tools, *tool.tools))
            mcp_tools[tool.name] = tool
        elif isinstance(tool, NativeAgents):
            if native_agents is not None:
                raise ValueError("A Session supports exactly one NativeAgents declaration")
            native_agents = tool
        elif isinstance(tool, (Checklist, NativeGoals, WebSearch)):
            if available is not None:
                refs = _fixed_tool_refs((tool,), False, None, None)
                if retained.intersection(refs):
                    raise CapabilityError("Operation tools must select distinct fixed capabilities")
                retained.update(refs)
        else:
            raise TypeError(
                "tools must use FunctionTool, function_tool, StdioMCP, HttpMCP, "
                "Checklist, NativeGoals, WebSearch or NativeAgents"
            )
    return tuple(python_tools), tuple(mcp_tools.values()), native_agents


def _selected_tool_refs(
    selection: Sequence[Any],
    python: Sequence[FunctionTool[Any, Any, Any]],
    mcp: Sequence[_MCP[Any]],
    *,
    programmatic_tool_calling: bool,
    environment: Environment[Any] | None,
    native_agents: NativeAgents | None,
) -> tuple[ToolRef, ...]:
    """Normalize admitted declarations through the same identities as dispatch."""
    refs = {tool.ref for tool in python}
    refs.update(server.tool(name) for server in mcp for name in server.tools)
    refs.update(
        item
        for item in selection
        if isinstance(item, ToolRef)
        and not (item.namespace == "functions" and ToolRef(item.name) in refs)
    )
    refs.update(
        _fixed_tool_refs(
            selection,
            False,
            environment if any(item is environment for item in selection) else None,
            native_agents if any(item is native_agents for item in selection) else None,
        )
    )
    # Execution helpers remain an implementation detail of an admitted nonempty
    # selection. They grant no leaves and disappear with the empty selection.
    if programmatic_tool_calling and any(
        ref.namespace != "functions" or ref.name not in {"exec", "wait"} for ref in refs
    ):
        refs.update((ToolRef("exec", "functions"), ToolRef("wait", "functions")))
    # Preserve the acknowledged Python declaration order. Fixed identities are
    # unordered capabilities and follow the declared Python/MCP leaves.
    ordered = [tool.ref for tool in python]
    ordered.extend(server.tool(name) for server in mcp for name in server.tools)
    ordered.extend(
        sorted(refs.difference(ordered), key=lambda ref: (ref.namespace or "", ref.name))
    )
    return tuple(ordered)


def _native_tool_refs(refs: Sequence[ToolRef]) -> str:
    return json.dumps([{"name": ref.name, "namespace": ref.namespace} for ref in refs])


def _capture_tools(
    generation: int,
    tools: tuple[FunctionTool[Any, Any, Any], ...],
    context: ContextT | None,
) -> _ToolCatalog:
    return _ToolCatalog(
        generation,
        tuple(tool._capture(context) for tool in tools),
    )


def _same_opening_tools(left: Sequence[Tool[Any]], right: Sequence[Tool[Any]]) -> bool:
    return len(left) == len(right) and all(a is b for a, b in zip(left, right, strict=True))


def _mcp_config(
    servers: tuple[_MCP[Any], ...],
    environment: Environment[Any] | None,
    available: tuple[_MCP[Any], ...],
    *,
    programmatic_tool_calling: bool,
) -> str:
    active = {server.name: server for server in servers}
    config: dict[str, dict[str, object]] = {}
    for declared in available:
        selected = active.get(declared.name)
        value = (selected if selected is not None else declared)._config(environment)
        if not programmatic_tool_calling:
            value["omit_tools_from"] = ["code_mode"]
        if selected is None:
            value.update(enabled=False, required=False)
        config[declared.name] = value
    return json.dumps(config)


def _check_mcp_receipt(
    raw: str,
    servers: tuple[_MCP[Any], ...],
    available: tuple[_MCP[Any], ...],
    generation: int,
) -> None:
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        raise NativeError("Invalid native MCP publication receipt") from None
    if not _ready(receipt, servers, available):
        result = receipt.get("result") if isinstance(receipt, dict) else None
        reports = result.get("servers") if isinstance(result, dict) else None
        if isinstance(reports, dict):
            for server in servers:
                report = reports.get(server.name)
                prefix = f"Native MCP server {server.name!r}: "
                if not isinstance(report, dict) or (
                    report.get("accepted") is not True
                    or report.get("ready") is not True
                    or report.get("status") != "connected"
                ):
                    raise NativeError(prefix + "connection not ready")
                if report.get("listing_error") is not False:
                    raise NativeError(prefix + "tool listing unavailable")
                listed = report.get("tools")
                if isinstance(listed, list):
                    missing = [name for name in server.tools if name not in listed]
                    if missing:
                        raise NativeError(
                            prefix + "selected tools unavailable: " + ", ".join(map(repr, missing))
                        )
                else:
                    raise NativeError(prefix + "tool listing unavailable")
        raise NativeError("Native MCP publication did not confirm every selected tool ready")
    if receipt["result"].get("policy_generation") != generation:
        raise NativeError("Native MCP publication generation did not match")


def _check_checkpoint(state: SessionState, instructions: str) -> None:
    """Accept idle or settled manual native state without rewriting history."""
    for unsupported, component in (
        (state.managed_tool_specs is None, "missing native tool declarations"),
        (bool(state.interrupted_thread_ids), "interrupted native work"),
        (bool(state.pending_actions), "pending tool decisions"),
    ):
        if unsupported:
            raise ValueError(f"state must contain an idle native Agent session tree: {component}")
    native = importlib.import_module("agents._native")
    native._check_session_instructions(json.dumps(state.history), instructions)


class _EnvironmentLease:
    """One task holds the supplied manager from entry until native retirement."""

    def __init__(self, source: AbstractAsyncContextManager[Environment[Any]]) -> None:
        loop = asyncio.get_running_loop()
        self._ready: asyncio.Future[Environment[Any]] = loop.create_future()
        self._retired: asyncio.Future[None] = loop.create_future()
        self._borrower = asyncio.current_task()
        self._sync_run: _SyncRun | None = None
        self._woke_borrower = False
        self.interrupted = False
        self._owner = _create_task(self._hold(source))

    async def _hold(self, source: AbstractAsyncContextManager[Environment[Any]]) -> None:
        # Manager timeouts and task groups cancel this task, distinct from
        # cancellation of the borrowing application. Entry and exit stay together.
        async with source as environment:
            self._ready.set_result(environment)
            try:
                await asyncio.shield(self._retired)
            except BaseException as error:
                # A Source ending early must not release an executor in native use.
                # Wake only its own borrower and wait for the existing opening/close
                # path to join native work. Never join the Source from native close.
                self.interrupted = True
                if self._borrower is not None:
                    self._woke_borrower = self._borrower.cancel(self)
                await _close_after_error(error, lambda: _join(self._retired))
                raise

    async def acquire(self) -> Environment[Any]:
        try:
            await asyncio.wait((self._owner, self._ready), return_when=asyncio.FIRST_COMPLETED)
            if self._owner.done():
                await self._owner
            return self._ready.result()
        except BaseException as error:
            # Before successful entry, the source owns rollback. Cancellation must
            # join that entry and must never start native work from a late result.
            if not self._ready.done():
                cancel_once(self._owner)
            await _close_after_error(error, lambda: _release_environment(self))
            self.raise_if_interrupted(error)
            raise

    def raise_if_interrupted(self, error: BaseException) -> None:
        if self.interrupted and isinstance(error, asyncio.CancelledError) and error.args == (self,):
            if error.__context__ is not None:
                raise error.__context__ from None
            raise RuntimeError("Environment lease ended before Session retirement") from None

    def detach_borrower(self) -> None:
        if self._woke_borrower:
            self._woke_borrower = False
            if sys.version_info >= (3, 11):
                assert self._borrower is not None
                self._borrower.uncancel()
        self._borrower = None


async def _release_environment(lease: _EnvironmentLease) -> None:
    # Only signal after native retirement; the Source owns normal exception policy
    # inside its manager. It cannot suppress an error in the separate Session body.
    if not lease._owner.done() and lease._owner.get_loop() is not asyncio.get_running_loop():
        raise RuntimeError("Close the retained Environment on the event loop where it opened")
    if not lease._retired.done():
        lease._retired.set_result(None)
    try:
        await _join(lease._owner)
    finally:
        lease.detach_borrower()


def _validate_storage_opening(
    storage: LocalStorage | CustomStorage | None,
    state: SessionState | None,
    resume: str | None,
    reconciled_calls: Sequence[ReconciledCall],
) -> tuple[ReconciledCall, ...]:
    if storage is not None and not isinstance(storage, (LocalStorage, CustomStorage)):
        raise TypeError("storage must be LocalStorage or CustomStorage")
    if state is not None and (storage is not None or resume is not None):
        raise ValueError("Portable state cannot be combined with native local storage or resume")
    if resume is not None:
        if storage is None:
            raise ValueError("resume requires the original LocalStorage profile")
        if not isinstance(resume, str) or not resume:
            raise TypeError("resume must be a nonempty native Session ID")
    reconciliation = tuple(reconciled_calls)
    if any(not isinstance(receipt, ReconciledCall) for receipt in reconciliation):
        raise TypeError("reconciled_calls must contain ReconciledCall records")
    if reconciliation and (resume is None or not isinstance(storage, LocalStorage)):
        raise ValueError("reconciled_calls requires a LocalStorage resume")
    return reconciliation


def _configure_storage_runtime(
    runtime: dict[str, Any],
    storage: LocalStorage | CustomStorage | None,
    resume: str | None,
    goals: bool,
) -> None:
    if isinstance(storage, CustomStorage):
        runtime["ephemeral"] = False
        if resume is not None:
            runtime["_sdk_resume_thread_id"] = resume
    elif storage is not None:
        runtime.update(native_thread_store=str(storage.path), ephemeral=False)
        if resume is not None:
            runtime["_sdk_resume_thread_id"] = resume
        if goals:
            runtime["native_goal_store"] = str(storage.path)


class Session(Generic[ContextT, OutputT]):
    """Own a native thread, its readers, and at most one active submission.

    Open and use it in one event loop with Agent.session(). Context exit
    closes the native work.
    Waiting is passive. Cancelling a waiter leaves the submitted work running.
    """

    @classmethod
    async def _open(
        cls,
        agent: Agent[ContextT, OutputT],
        *,
        context: ContextT | None = None,
        state: SessionState | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        tool_selection: Sequence[Tool[ContextT]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        _captured_api_key: str | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        _tracing_context: str | None = None,
        _expected_codex_login_binding: str | None = None,
    ) -> Session[ContextT, OutputT]:
        parent_trace = _trace_context(parent_trace)
        reconciliation = _validate_storage_opening(storage, state, resume, reconciled_calls)
        if state is not None and (model is not None or model_settings is not None):
            raise ValueError(
                "Opening model and model_settings overrides cannot be used with state; "
                "use update(model=...) after the restored Session is idle"
            )
        policy_agent = agent
        selected = _restore_declaration.get()
        if (
            selected is not None
            and selected.owner is not None
            and selected.owner is asyncio.current_task()
            and selected.agent is agent
            and (selected.state is state or selected.state == state)
        ):
            agent = selected.declaration
            if tool_selection is not None and not _same_opening_tools(tool_selection, agent.tools):
                raise ValueError(
                    "DeferredTools.start restore requires its captured opening tools; "
                    "omit tools or use prepare and restore explicitly for new bindings"
                )
        else:
            selected = None
            # Capture the declaration before native opening can suspend. Keep
            # application hooks and executor policy bound to the original Agent.
            agent = Agent[ContextT, OutputT](
                instructions=agent.instructions,
                name=agent.name,
                tools=agent.tools if tool_selection is None else tool_selection,
                model=agent.model if model is None else model,
                model_settings=cast(
                    ModelSettings, {**agent.model_settings, **_copy_model_settings(model_settings)}
                ),
                provider=agent.provider if provider is None else provider,
                output_type=cast(OutputSpec[OutputT], agent.output_type),
                programmatic_tool_calling=agent.programmatic_tool_calling,
            )
        codex_login = agent.provider.auth if agent.provider is not None else None
        programmatic_tool_calling = agent.programmatic_tool_calling
        instructions, instruction_receipts = _load_instruction_files(
            agent.instructions, instruction_files
        )
        if not isinstance(programmatic_tool_calling, bool):
            raise TypeError("programmatic_tool_calling must be a bool")
        checkpoint = None
        manual_restore = False
        continuation = None
        operation_slots = None
        restored_operation_refs = None
        restored_model_settings = None
        restored_defaults = None
        if state is not None:
            if not isinstance(state, SessionState):
                raise TypeError("state must be a SessionState")
            # Capture caller-owned data before the first await. The native
            # importer validates identities, revision, and current tool specs.
            saved = SessionState.from_json(state.to_json())
            _check_checkpoint(saved, instructions)
            if (saved.codex_login_binding is not None) != (codex_login is not None):
                raise ValueError("Checkpoint and Session must use the same authentication mode")
            # The binding still consumes its original idle/manual format.
            # Validate the SDK envelope before giving Core the native version.
            payload = json.loads(saved.to_json())
            payload.pop("codex_login_binding", None)
            if saved.format_version in (4, 5, 6, 7):
                payload["format_version"] = saved.format_version - 2
            payload.pop("operation_tools", None)
            payload.pop("repair_turns", None)
            payload.pop("repair_items", None)
            if "_sdk_default_tools" in saved.agent_options:
                restored_defaults = tuple(
                    ToolRef(**item) for item in saved.agent_options["_sdk_default_tools"]
                )
                payload["agent_options"].pop("_sdk_default_tools")
            if saved.format_version in (4, 6):
                # Idle selector names are SDK input, not native grants.
                payload.pop("step_mcp_tools", None)
            checkpoint = json.dumps(payload)
            manual_restore = saved.native_continuation is not None
            continuation = saved.native_continuation
            if manual_restore:
                restored_model_settings = _manual_restore_model_settings(saved)
        if isinstance(storage, LocalStorage) and programmatic_tool_calling:
            raise ValueError(
                "LocalStorage does not support code mode: nested tool identities "
                "do not yet have durable intent and result receipts"
            )
        tools, mcp_tools, native_agents = _split_tools(agent.tools, programmatic_tool_calling)
        available = agent.tools
        checklist = any(isinstance(tool, Checklist) for tool in available)
        goals = any(isinstance(tool, NativeGoals) for tool in available)
        if isinstance(storage, CustomStorage) and (
            native_agents is not None
            or goals
            or programmatic_tool_calling
            or checklist
            or mcp_tools
            or environment is not None
            or any(isinstance(tool, WebSearch) for tool in available)
            or any(tool._bind is None for tool in tools)
        ):
            raise ValueError(
                "CustomStorage preview does not support children, goals, checklist, "
                "code mode, manual tools, native tools or environments"
            )
        if goals and storage is None:
            raise ValueError("NativeGoals requires an explicit LocalStorage profile")
        web_search = next((tool for tool in available if isinstance(tool, WebSearch)), None)
        allowed_tools, allowed_mcp = tools, mcp_tools
        deferred_schemas = any(tool.defer_loading for tool in allowed_tools) or any(
            server.defer_loading for server in allowed_mcp
        )
        if state is not None and state.descendants and native_agents is None:
            raise ValueError("Restoring child state requires current NativeAgents")
        if native_agents is not None:
            if not agent.model:
                raise ValueError("NativeAgents requires an explicit compatible model")
            if environment is None or (
                isinstance(environment, Environment)
                and set(environment.tools) != {"exec_command", "write_stdin", "apply_patch"}
            ):
                raise ValueError(
                    "NativeAgents requires an Environment with all three execution tools"
                )
        output = _prepare_spec(agent.output_type)
        mcp_names = _tool_names(allowed_mcp)
        model = agent.model
        catalog = _capture_tools(0, tools, context)
        bound_tools = catalog.tools
        native = importlib.import_module("agents._native")
        native._validate_python_tool_declarations(catalog.native_specs())
        if (
            state is not None
            and saved.managed_tool_specs is not None
            and (native_agents is not None or not manual_restore)
        ):
            # The saved idle or native child generation may be a subset.
            # Rebuild its import specs from current bindings in saved order. Native still
            # compares full schemas, modes and manual slots, so unknown or
            # duplicate saved declarations cannot become trusted grants.
            names = [spec.get("name") for spec in saved.managed_tool_specs]
            catalog = _ToolCatalog(
                0,
                tuple(
                    sorted(
                        (tool for tool in bound_tools if tool.name in names),
                        key=lambda tool: names.index(tool.name),
                    )
                ),
            )
        # Carry the owned root's exact contract through this checked restore.
        # The opening default still applies to the next explicit request.
        restored_output = selected.output if selected is not None and manual_restore else output
        settings = _native_model_settings(model, agent.model_settings)
        if restored_model_settings is not None:
            model, settings = restored_model_settings
        native_specs = catalog.native_specs()
        manual_slots = catalog.manual_slots()
        if manual_restore:
            expected_schema = restored_output.schema if restored_output.mode == "native" else None
            if continuation is None or continuation.output_schema != expected_schema:
                raise ValueError("Checkpoint native output schema does not match the Agent")
            if expected_schema is not None and saved.output_fingerprint is None:
                raise ValueError("Checkpoint predates the requested native output contract")
        if state is not None:
            if saved.output_fingerprint is None:
                # Before output contracts, only idle native/plain states and
                # settled plain manual states were supported. Idle starts a
                # new turn: no schema for its next output was ever persisted.
                if restored_output.mode == "prompted":
                    raise ValueError("Checkpoint predates the requested output contract")
            elif saved.output_fingerprint != restored_output.fingerprint:
                raise ValueError(
                    "Checkpoint output contract does not match the Agent; "
                    "use with_output_type with the saved turn's output type"
                )
        if os.environ.get("CODEX_SANDBOX") == "seatbelt" and any(
            os.environ.get(name)
            for name in (
                "HTTP_PROXY",
                "http_proxy",
                "HTTPS_PROXY",
                "https_proxy",
                "ALL_PROXY",
                "all_proxy",
            )
        ):
            raise ValueError(
                "The pinned Core cannot honor HTTP proxy settings in seatbelt mode. "
                "Run outside that sandboxed environment or remove unused proxy settings."
            )
        if manual_restore:
            # A restore already owns work. Check current selected Python and MCP
            # bindings before entering even a managed destination Environment.
            # Full roster/fixed-grant validation follows once it is available.
            for selected_tool in tools:
                if saved.operation_tools is None or selected_tool.ref in saved.operation_tools:
                    selected_tool._check_context(context)
            for server in mcp_tools:
                if saved.operation_tools is None or any(
                    server.tool(name) in saved.operation_tools for name in server.tools
                ):
                    server._check_context(context)
        lease = None
        owned_provider = False
        opening = _OpeningOwner()
        if environment is not None and not isinstance(environment, Environment):
            if not isinstance(environment, AbstractAsyncContextManager):
                raise TypeError("environment must be an Environment or async context manager")
            lease = _EnvironmentLease(environment)
            environment = await lease.acquire()
        try:
            executor = environment
            if (lease is not None or executor is not None) and not isinstance(
                executor, Environment
            ):
                raise TypeError("environment must be an Environment")
            if state is not None:
                current_policy = executor._network_policy_digest() if executor is not None else None
                if saved.network_policy_digest != current_policy:
                    raise ValueError(
                        "Checkpoint and Environment must agree on the command network policy"
                    )
                if (
                    saved.executor_policy_digest is None
                    and executor is not None
                    and (executor._is_local or executor.writable_roots is not None)
                ):
                    raise ValueError(
                        "Checkpoint and Environment must agree on the applied executor policy"
                    )
            if native_agents is not None and (
                executor is None
                or set(executor.tools) != {"exec_command", "write_stdin", "apply_patch"}
            ):
                raise ValueError(
                    "NativeAgents requires an Environment with all three execution tools"
                )
            if restored_defaults is not None:
                # Resolve only against destination declarations, without acquiring
                # MCP connections or replacing the active manual roster.
                _split_tools(
                    restored_defaults,
                    programmatic_tool_calling,
                    available=available,
                    environment=executor,
                    native_agents=native_agents,
                )
            if manual_restore and saved.operation_tools is not None:
                # Select current bindings only; state never provides callbacks,
                # credentials or executor authority.
                selected_python, mcp_tools, _ = _split_tools(
                    saved.operation_tools,
                    programmatic_tool_calling,
                    available=available,
                    environment=executor,
                    native_agents=native_agents,
                )
                restored_operation_refs = _selected_tool_refs(
                    saved.operation_tools,
                    selected_python,
                    mcp_tools,
                    programmatic_tool_calling=programmatic_tool_calling,
                    environment=executor,
                    native_agents=native_agents,
                )
                if set(restored_operation_refs) != set(saved.operation_tools):
                    raise ValueError("Saved operation does not describe a canonical tool subset")
                available_slots = {tool.name: slot for slot, tool in enumerate(catalog.tools)}
                if any(tool.name not in available_slots for tool in selected_python):
                    raise ValueError("Saved operation names tools outside its native generation")
                operation_slots = tuple(available_slots[tool.name] for tool in selected_python)
                assert continuation is not None
                selected_names = {tool.name for tool in selected_python}
                if any(
                    pending.tool not in selected_names for pending in continuation.pending_manual
                ):
                    raise ValueError("Saved operation does not admit its pending manual tools")
            # Capture bindings at opening; acquire only a promoted operation's
            # selected servers. A manual restore already owns admitted work.
            if not manual_restore:
                mcp_tools = ()
            for server in mcp_tools:
                server._check_context(context)
            mcp_config = _mcp_config(
                mcp_tools,
                executor,
                allowed_mcp,
                programmatic_tool_calling=programmatic_tool_calling,
            )
            if executor is not None:
                executor._check_context(context)
            home: TemporaryDirectory[str] | None = None
            runtime: dict[str, Any] = {
                "strict_config": True,
                "loader": {"ignore_user_config": True, "ignore_project_config": True},
                "base_instructions": (
                    "You are an AI agent. Follow the application instructions and use "
                    "the tools provided to complete the user's request accurately."
                ),
            }

            if codex_login is not None:
                runtime["_sdk_codex_login"] = {
                    "codex_home": (
                        None if codex_login.codex_home is None else str(codex_login.codex_home)
                    ),
                    "account_id": codex_login.account_id,
                    "expected_binding": (
                        saved.codex_login_binding
                        if state is not None
                        else _expected_codex_login_binding
                    ),
                }
            _configure_storage_runtime(runtime, storage, resume, goals)
            if executor is not None:
                native_environment = executor._to_native()
                native_environment["local_mcp"] = any(
                    isinstance(server, StdioMCP) and server.location == "host"
                    for server in allowed_mcp
                )
                runtime["environment"] = native_environment
                runtime["sandbox_mode"] = "read-only"
            if programmatic_tool_calling:
                helpers = Path(__file__).parent / "bin"
                runtime["code_mode_host_exe"] = str(
                    helpers
                    / (
                        "codex-code-mode-host.exe"
                        if sys.platform == "win32"
                        else "codex-code-mode-host"
                    )
                )
            if (executor is not None and executor._is_local) or any(
                isinstance(server, StdioMCP) and server.location == "host" for server in allowed_mcp
            ):
                # Provision the helper for an explicit local executor or host MCP.
                helpers = Path(__file__).parent / "bin"
                runtime["codex_self_exe"] = str(
                    helpers
                    / ("codex-sdk-runtime.exe" if sys.platform == "win32" else "codex-sdk-runtime")
                )
                if sys.platform == "linux":
                    runtime["codex_linux_sandbox_exe"] = str(helpers / "codex-linux-sandbox")
            # Allocate only after local admission. An omitted provider belongs to
            # this Session alone; the captured declaration keeps None for restore.
            provider = agent.provider
            owned_provider = provider is None
            if provider is None:
                provider = NativeProvider()
            try:
                native, transport, provider_lease = provider._acquire_lease()
                opening.lease = provider_lease
                session = None
                engine = None
                thread = None
                provider_id = (
                    "native_agent_codex_login" if codex_login is not None else "native_agent"
                )
                config = {
                    "include_permissions_instructions": executor is not None,
                    "include_environment_context": executor is not None,
                    "model_provider": provider_id,
                    f"model_providers.{provider_id}": {
                        "name": "OpenAI" if codex_login is not None else "Native agent provider",
                        "base_url": (
                            _CODEX_RESPONSES_BASE_URL
                            if codex_login is not None
                            else provider.base_url or "https://api.openai.com/v1"
                        ),
                        "wire_api": "responses",
                        "requires_openai_auth": True,
                        "supports_websockets": provider._websockets_enabled,
                        # Both native web protocols require a compatible Responses
                        # provider. The declaration admits that provider capability.
                        "supports_standalone_web_search": (
                            web_search is not None and web_search.route == "standalone"
                        ),
                    },
                    "agents.enabled": False,
                    "features.goals": goals,
                    "tools.update_plan.enabled": checklist,
                    "mcp_servers": {},
                    "features.non_prefixed_mcp_tool_names": False,
                    "features.code_mode": {
                        "enabled": programmatic_tool_calling,
                        "direct_only_tool_namespaces": (
                            ["collaboration"]
                            if programmatic_tool_calling
                            else [
                                "functions",
                                *sorted(
                                    {
                                        name["namespace"]
                                        for server in allowed_mcp
                                        if not server.defer_loading
                                        for name in _tool_names((server,))
                                    }
                                ),
                                *(
                                    ["collaboration"]
                                    if native_agents is not None or deferred_schemas
                                    else []
                                ),
                            ]
                        ),
                    },
                    "features.code_mode_interrupt": programmatic_tool_calling,
                    "web_search": web_search.mode if web_search is not None else "disabled",
                    "features.standalone_web_search": (
                        web_search is not None and web_search.route == "standalone"
                    ),
                }
                from . import telemetry

                if telemetry._engine_options is not None:
                    config.update(json.loads(telemetry._engine_options))
                if programmatic_tool_calling:
                    config["features.code_mode_host"] = {
                        "enabled": True,
                        "disable_in_process_fallback": True,
                    }
                if model is not None:
                    config["model"] = model
                if native_agents is not None:
                    runtime["_native_agents"] = True
                    # Core enables the owned route only for non-root child creation.
                    runtime["approval_policy"] = "never"
                    config.update(
                        {
                            "agents.enabled": True,
                            "features.multi_agent_v2": {
                                "subagent_developer_instructions": instructions,
                                "enabled": True,
                                "tool_namespace": "collaboration",
                                "expose_spawn_agent_model_overrides": False,
                                **(
                                    {
                                        "max_concurrent_threads_per_session": (
                                            native_agents.max_children + 1
                                        )
                                    }
                                    if native_agents.max_children is not None
                                    else {}
                                ),
                            },
                            "features.view_image": False,
                            "features.image_generation": False,
                            "features.current_time_reminder": False,
                            "features.sleep_tool": False,
                            "features.request_permissions_tool": False,
                            "features.token_budget": False,
                            "features.deferred_executor": False,
                            "features.apps": False,
                            "features.plugins": False,
                            "features.memory_tool": False,
                            "features.tool_suggest": False,
                            "features.send_message_to_user_async": False,
                            "tools.experimental_request_user_input.enabled": False,
                        }
                    )
                if codex_login is not None:
                    opening.api_key = None
                elif selected is not None:
                    opening.api_key = selected.api_key
                elif _captured_api_key is not None:
                    opening.api_key = _captured_api_key
                elif provider.api_key is None:
                    opening.api_key = native._read_openai_api_key_from_env()
                else:
                    opening.api_key = provider.api_key
                hosted_verification = next(
                    (server for server in allowed_mcp if type(server) is HostedVerificationMCP),
                    None,
                )
                chatgpt_apps = next(
                    (server for server in allowed_mcp if type(server) is ChatGPTAppsMCP), None
                )
                if hosted_verification is not None and chatgpt_apps is not None:
                    raise ValueError("Select one explicit Apps registration per Session")
                options = {
                    **(
                        {"parent_trace": parent_trace.to_headers()}
                        if parent_trace is not None
                        else {}
                    ),
                    "config": config,
                    **(
                        {
                            "_sdk_chatgpt_apps": {
                                "access_token": chatgpt_apps.access_token,
                                "account_id": chatgpt_apps.account_id,
                            }
                        }
                        if chatgpt_apps is not None
                        else {}
                    ),
                    **(
                        {"_sdk_hosted_verification": hosted_verification._config()}
                        if hosted_verification is not None
                        else {}
                    ),
                    "runtime": runtime,
                    "client_mcp_extensions": {
                        "openai/form": {},
                        "openai/elicitation": {
                            "form": {},
                            **(
                                {"userVerification": {}}
                                if hosted_verification is not None or chatgpt_apps is not None
                                else {}
                            ),
                        },
                        **(
                            {"openai/sdk-child-prompts": {}}
                            if native_agents is not None and native_agents.interactions
                            else {}
                        ),
                    },
                    **({"_sdk_resume_thread_id": resume} if resume is not None else {}),
                    "_application_instructions": instructions,
                    "_sdk_instruction_files": instruction_receipts,
                    "_manual_tool_slots": manual_slots,
                    **(
                        {
                            "_native_agent_roles": [
                                role.name for role in native_agents.additional_roles
                            ],
                            "_native_agent_max_depth": native_agents.max_depth,
                        }
                        if native_agents is not None
                        else {}
                    ),
                    "tool_policy": {
                        "allowed_tools": [
                            {
                                "name": entry.name,
                                "namespace": "functions",
                            }
                            for entry in allowed_tools
                        ]
                        + mcp_names
                        + (
                            [{"name": "run", "namespace": "web"}]
                            if web_search is not None and web_search.route == "standalone"
                            else [{"name": "web_search", "namespace": "functions"}]
                            if web_search is not None
                            else []
                        )
                        + (
                            [{"name": "tool_search", "namespace": "functions"}]
                            if deferred_schemas
                            else []
                        )
                        + ([{"name": "update_plan", "namespace": "functions"}] if checklist else [])
                        + (
                            [
                                {"name": name, "namespace": "functions"}
                                for name in ("create_goal", "get_goal", "update_goal")
                            ]
                            if goals
                            else []
                        )
                        + (
                            [
                                {"name": name, "namespace": "collaboration"}
                                for name in _COLLABORATION_TOOLS
                            ]
                            if native_agents is not None
                            else []
                        )
                        + (
                            [{"name": name, "namespace": "functions"} for name in executor.tools]
                            if executor is not None
                            else []
                        )
                        + (
                            [{"name": name, "namespace": "functions"} for name in ("exec", "wait")]
                            if programmatic_tool_calling
                            else []
                        ),
                        **(
                            {
                                "require_managed_sandbox": True,
                                "require_unified_exec": True,
                                "expose_additional_permissions": False,
                            }
                            if executor is not None
                            else {}
                        ),
                    },
                }
                # Every conversation uses Core for model execution and history.
                home = provider_lease.home()
                assert home is not None
                workspace = Path(home.name) / "workspace"
                runtime.update(codex_home=home.name, cwd=str(workspace))
                # Recreate storage for retained checkpoint data only. Saved data
                # never enables model-facing planning or goal tools.
                if state is not None and (
                    state.root_goal is not None
                    or any(child.goal is not None for child in state.descendants)
                ):
                    runtime["native_goal_store"] = home.name
                workspace.mkdir(exist_ok=True)
                if native_agents is not None:
                    for role in native_agents.additional_roles:
                        role_path = Path(home.name) / ("role-" + role.name + ".toml")
                        role_fields = {"developer_instructions": role.instructions}
                        if role.model is not None:
                            role_fields["model"] = role.model
                        if role.model_settings and "reasoning_effort" in role.model_settings:
                            role_fields["model_reasoning_effort"] = role.model_settings[
                                "reasoning_effort"
                            ]
                        role_path.write_text(
                            "".join(
                                key + " = " + json.dumps(value, ensure_ascii=False) + "\n"
                                for key, value in role_fields.items()
                            ),
                            encoding="utf-8",
                        )
                        config["agents." + role.name] = {
                            "description": role.description,
                            "config_file": str(role_path),
                        }
                create_engine = (
                    native.create_checkpoint_engine
                    if checkpoint is not None
                    else native.create_engine
                )
                engine = await opening.acquire(
                    create_engine(
                        json.dumps(config),
                        json.dumps(runtime),
                        # Preserve the captured environment value in API-key mode.
                        api_key=None if codex_login is not None else (opening.api_key or ""),
                        provider_lease=provider_lease,
                        **(
                            {"canonical_store": storage.backend}
                            if isinstance(storage, CustomStorage)
                            else {}
                        ),
                        **(
                            {
                                "initial_checkpoint": checkpoint,
                                "initial_specs_json": native_specs,
                                "initial_options_json": json.dumps(options),
                            }
                            if checkpoint is not None
                            else {}
                        ),
                    )
                )

                if selected is None and provider.api_key is not None:
                    # Match Rust's whitespace after native UTF-8 validation.
                    opening.api_key = re.sub(
                        r"^[^\S\x1c-\x1f]+|[^\S\x1c-\x1f]+\Z", "", provider.api_key
                    )

                thread = await opening.acquire(
                    engine.start_replaceable_thread(json.dumps(options), native_specs)
                    if checkpoint is None
                    else engine.restore_checkpoint(checkpoint, json.dumps(options), native_specs)
                )
                session = cls(
                    engine,
                    thread,
                    home,
                    catalog,
                    mcp_tools,
                    context,
                    output,
                    instructions,
                    _start_readers=False,
                )
                session._trace_context = _tracing_context
                session._hosted_verification = hosted_verification or chatgpt_apps
                session._chatgpt_apps = chatgpt_apps
                if codex_login is not None:
                    account_id, plan_type, binding = engine._codex_account
                    session._codex_account = CodexAccount(
                        account_id=account_id, plan_type=plan_type
                    )
                    session._codex_login_binding = binding
                    # Application-supplied compaction opens a replacement engine.
                    runtime["_sdk_codex_login"]["expected_binding"] = binding
                session._bound_tools = bound_tools
                session._agent = policy_agent
                session._opening_agent = agent
                session._bindings = agent.tools
                session._executor = executor
                if executor is not None and executor.writable_roots is not None:
                    session._writable_roots = _writable_roots(executor.writable_roots)
                session._native_agents = native_agents
                session._storage = storage
                if checkpoint is not None:
                    session._repair_turns = saved.repair_turns
                    session._repair_items = saved.repair_items
                    recorded_policy = saved.executor_policy_digest
                    if (
                        recorded_policy == _IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST
                        and restored_operation_refs is not None
                        and executor is None
                    ):
                        # Older checkpoints can carry an implicit policy even
                        # when the saved subset resolves without an executor.
                        recorded_policy = None
                    if recorded_policy is not None and (
                        recorded_policy != await session._executor_policy_digest()
                    ):
                        raise ValueError(
                            "Checkpoint and Environment must agree on the applied executor policy"
                        )
                if restored_defaults is not None:
                    session._default_tools = restored_defaults
                if (
                    checkpoint is not None
                    and not manual_restore
                    and (catalog.tools != bound_tools or saved.step_mcp_tools is not None)
                ):
                    # The acknowledged idle catalog is the restored default.
                    # Other current opening declarations remain explicitly selectable.
                    restored_fixed_refs: list[ToolRef] = sorted(
                        _fixed_tool_refs(
                            agent.tools, programmatic_tool_calling, executor, native_agents
                        ),
                        key=lambda ref: (ref.namespace or "", ref.name),
                    )
                    restored_mcp_refs = [
                        server.tool(name) for server in allowed_mcp for name in server.tools
                    ]
                    if saved.step_mcp_tools is not None:
                        current_mcp_refs = {
                            (server.name, name): server.tool(name)
                            for server in allowed_mcp
                            for name in server.tools
                        }
                        saved_mcp_pairs = [
                            (item.server, item.name) for item in saved.step_mcp_tools
                        ]
                        if len(set(saved_mcp_pairs)) != len(saved_mcp_pairs) or any(
                            pair not in current_mcp_refs for pair in saved_mcp_pairs
                        ):
                            raise ValueError(
                                "Saved idle MCP tools must name distinct current declarations"
                            )
                        restored_mcp_refs = [current_mcp_refs[pair] for pair in saved_mcp_pairs]
                    if restored_defaults is None:
                        # Older checkpoints lack exact persistent fixed selectors.
                        session._default_tools = (
                            *(ToolRef(tool.name, tool.namespace) for tool in catalog.tools),
                            *restored_mcp_refs,
                            *restored_fixed_refs,
                        )
                if resume is not None:
                    # The writer is held. Read the saved graph, including
                    # cold descendants, before publishing current tools.
                    descendants = json.loads(
                        await session._publish_startup(thread.call("list_subagents"))
                    )
                    recorded: dict[str, list[RecordedTurn]] = {}
                    recorded_items: list[SessionItem] = []
                    unknown: list[UncertainCall] = []
                    for subagent_id in [None, *(child["id"] for child in descendants)]:
                        history = json.loads(
                            await session._publish_startup(thread.read_thread_history(subagent_id))
                        )
                        turns, items = project_history(
                            history, session.id, subagent_id, uncertain_calls=unknown
                        )
                        thread_id = subagent_id if subagent_id is not None else session.id
                        recorded[thread_id] = turns
                        recorded_items.extend(items)
                    if any(call.thread_id != session.id for call in unknown):
                        raise RuntimeError(
                            "Native local history has an unknown tool outcome in a descendant; "
                            "local recovery cannot reconcile descendant calls"
                        )
                    if unknown and not reconciliation:
                        raise LocalRecoveryRequired(
                            tuple(unknown),
                            tuple(turn for turns in recorded.values() for turn in turns),
                            tuple(recorded_items),
                        )
                    for child in descendants:
                        if child["status"] == "closed":
                            continue
                        # An open edge can outlive settled native work. Parent
                        # turns inherited at fork do not prove child completion.
                        parent_ids = {turn["id"] for turn in recorded[child["parent_agent_id"]]}
                        own_turns = [
                            turn for turn in recorded[child["id"]] if turn["id"] not in parent_ids
                        ]
                        if not own_turns or any(
                            turn["status"] == "in_progress" for turn in own_turns
                        ):
                            raise RuntimeError(
                                "Native local history has an active descendant; "
                                "its runtime cannot be restored"
                            )
                    if isinstance(storage, LocalStorage):
                        missing = {(call.thread_id, call.turn_id, call.call_id) for call in unknown}
                        supplied = {
                            (call.thread_id, call.turn_id, call.call_id) for call in reconciliation
                        }
                        prior = {
                            (session.id, item["turn_id"], item["call_id"])
                            for item in recorded_items
                            if item["type"] == "function_call_output" and "reconciliation" in item
                        }
                        if (
                            len(supplied) != len(reconciliation)
                            or not missing <= supplied <= missing | prior
                        ):
                            raise ValueError("Reconciliation must cover every unresolved call once")
                        await session._publish_startup(
                            thread._reconcile_local_calls(
                                json.dumps([asdict(call) for call in reconciliation])
                            )
                        )
                if (
                    web_search is not None
                    and web_search.route == "hosted"
                    and (checkpoint is not None or "model" not in settings)
                ):
                    # Without a destination model, the restored model owns
                    # admission. Validate it before publishing any startup
                    # settings or instructions to an addressable local thread.
                    # Explicit model updates validate natively before writing.
                    await session._publish_startup(thread._validate_hosted_search_model())
                if checkpoint is None and settings:
                    if (
                        await session._publish_startup(
                            thread._update_model_settings(json.dumps(settings))
                        )
                        is not True
                    ):
                        raise NativeError("Native model update acknowledgement did not match")
                if checkpoint is None:
                    if (
                        await session._publish_startup(
                            thread._update_instructions(
                                instructions, json.dumps(instruction_receipts)
                            )
                            if instruction_receipts
                            else thread._update_instructions(instructions)
                        )
                        is not True
                    ):
                        raise NativeError("Native instruction update acknowledgement did not match")
                if allowed_mcp:
                    receipt = await session._publish_startup(
                        thread._replace_mcp_servers(
                            mcp_config,
                            _result_policies(mcp_tools, catalog.generation),
                        ),
                    )
                    _check_mcp_receipt(receipt, mcp_tools, allowed_mcp, catalog.generation)
                session._provider_lease = provider_lease
                session._parent_trace = parent_trace
                session._native_options = json.dumps(options)
                if owned_provider:
                    session._owned_provider = provider
                session._websockets_enabled = provider._websockets_enabled
                if manual_restore:
                    assert continuation is not None and checkpoint is not None
                    session._manual_source_digest = hashlib.sha256(
                        saved.to_json().encode()
                    ).hexdigest()
                    active = _Submission(
                        restored_output,
                        restored_pending=True,
                        parent_trace=parent_trace,
                        operation_slots=operation_slots,
                        operation_mcp=mcp_tools,
                        operation_refs=restored_operation_refs,
                    )
                    handle = _root_turn(
                        session, continuation.turn_id, uuid4().hex, active.completion
                    )
                    session._last_turn = handle
                    session._restored_turn = handle
                    active.handle = handle
                    session._register_request_observation(active, handle)
                    active.accepted.set_result(handle)
                    session._active = active
                    thread._select_steps(
                        restored_operation_refs is not None,
                        _native_tool_refs(restored_operation_refs)
                        if restored_operation_refs is not None
                        else None,
                        native_agents is not None and native_agents.before_step is not None,
                    )
                    thread._prepare_manual_restore()
                    active.tracing = _capture(fallback=_tracing_context)
                    session._validate_pending_manual(active)
                    session._observations.append(
                        TurnStarted(
                            session_id=session.id,
                            turn_id=continuation.turn_id,
                            submission_id=handle.submission_id,
                        )
                    )
                    active.task = _create_task(session._execute(active, [], admitted=True))
                session._start_readers()
                opening.disown(session)
                session._environment_lease = lease
                return session
            except BaseException as error:
                if isinstance(error, UnicodeEncodeError) and error.object is opening.api_key:
                    # Keep only the character or range shown by the native conversion error.
                    error.object = (
                        "*" * error.start + error.object[error.start]
                        if error.end == error.start + 1
                        else "*" * error.end
                    )
                    error.args = (
                        error.encoding,
                        error.object,
                        error.start,
                        error.end,
                        error.reason,
                    )
                # Keep native validation order and the missing-environment diagnostic.
                if (
                    opening.api_key is None
                    and isinstance(error, RuntimeError)
                    and error.args == ("api_key must not be empty",)
                ):
                    error.args = ("Provide api_key or set OPENAI_API_KEY",)
                opening.api_key = None
                if opening.lease is not None:
                    await _close_after_error(error, lambda: opening.fail(session, transport, lease))
                raise

        except BaseException as error:
            if lease is not None:
                await _close_after_error(error, lambda: opening.fail_environment(lease))
                if opening.lease is not None and not lease._retired.done():
                    # Only the native join itself can keep an admitted manager
                    # pending. Expose its existing owner to supported aclose.
                    assert provider is not None
                    provider._defer_opening(opening, lease)
            if owned_provider:
                assert provider is not None
                await _close_after_error(error, provider.aclose)
            if lease is not None:
                try:
                    lease.raise_if_interrupted(error)
                except BaseException as interrupted:
                    # The manager's own error is the one the caller receives.
                    error = interrupted
                    raise
                finally:
                    if (
                        owned_provider
                        and provider is not None
                        and not vars(provider).get("_closed")
                    ):
                        # The manager may have exited while orphan home cleanup
                        # still needs the implicit provider's supported aclose().
                        cast(Any, error).provider = provider
            raise

    def __init__(
        self,
        engine: Any,
        thread: Any,
        home: TemporaryDirectory[str] | None,
        catalog: _ToolCatalog,
        mcp_tools: tuple[_MCP[Any], ...],
        context: ContextT | None,
        output: _PreparedOutput,
        instructions: str,
        *,
        _start_readers: bool = True,
    ) -> None:
        self._trace_context: str | None = None
        self._provider_lease: Any = None
        self._storage: LocalStorage | CustomStorage | None = None
        self._environment_lease: _EnvironmentLease | None = None
        self._owned_provider: NativeProvider | None = None
        self._agent: Agent[ContextT, OutputT] | None = None
        self._opening_agent: Agent[ContextT, OutputT] | None = None
        self._model_api_key: str | None = None
        self._parent_trace: TraceContext | None = None
        self._codex_account: CodexAccount | None = None
        self._codex_login_binding: str | None = None
        self._native_options: str | None = None
        self._websockets_enabled = False
        self._engine = engine
        self._native = thread
        self._home = home
        self._catalog = catalog
        # MCP publication keeps this exact captured tuple; rebinding an idle
        # Python catalog creates another. No second set of bindings is made.
        self._bound_tools = catalog.tools
        self._bindings: Sequence[Tool[ContextT]] = ()
        self._mcp_tools = mcp_tools
        self._hosted_verification = next(
            (
                server
                for server in mcp_tools
                if type(server) in (HostedVerificationMCP, ChatGPTAppsMCP)
            ),
            None,
        )
        self._chatgpt_apps = next(
            (server for server in mcp_tools if type(server) is ChatGPTAppsMCP), None
        )
        self._default_tools: (
            tuple[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents, ...] | None
        ) = None
        self._executor: Environment[Any] | None = None
        self._writable_roots: tuple[str, ...] | None = None
        self._executor_termination_attempted = False
        self._native_agents: NativeAgents | None = None
        self._interception: _NativeInterception | None = None
        self._context = context
        self._default_output = output
        self._instructions = instructions
        self._id: str = thread.id
        self._owner = object()
        self._loop = asyncio.get_running_loop()
        self._url_completions: dict[tuple[str, str], ElicitationRequest] = {}
        self._url_completion_lock = asyncio.Lock()
        self._active: _Submission | None = None
        self._last_turn: Turn[Any] | None = None
        self._repair_turns: list[RecordedTurn] = []
        self._repair_receipts_incomplete = False
        self._repair_items: list[SessionItem] = []
        self._restored_turn: Turn[OutputT] | None = None
        self._child_callbacks: dict[tuple[str, str, str], _ChildCallbacks] = {}
        self._child_admissions: set[asyncio.Task[Turn[str]]] = set()
        self._pending: deque[tuple[_Submission, list[dict[str, Any]]]] = deque()
        self._graph_interrupts: set[asyncio.Task[None]] = set()
        self._root_interrupts: set[asyncio.Task[None]] = set()
        self._mutation: asyncio.Task[Any] | None = None
        self._checkpoint_task: asyncio.Future[None] | None = None
        self._sealed_checkpoint: str | None = None
        self._manual_source_digest: str | None = None
        self._sealing = False
        self._closing = False
        self._closed = False
        self._resources_retired = False
        self._broken = False
        self._startup_failure: asyncio.Future[BaseException] | None = None
        self._fail_unhandled_questions = False
        self._close_task: asyncio.Task[None] | None = None
        self._observations = _ObservationBuffer(self.id)
        self._api_rate_limit_snapshot = ApiRateLimits()
        self._rate_limit_parent: Session[Any, Any] | None = None
        self._request_observation_routes: weakref.WeakValueDictionary[str, _Submission] = (
            weakref.WeakValueDictionary()
        )
        self._observations._callback_check = weakref.WeakMethod(self._check_callback_operation)
        # The sole native reader owns questions during opening publication.
        self._reader = _create_task(self._read_events())
        self._mcp_reader: asyncio.Task[None] | None = None
        if _start_readers:
            self._start_readers()

    def _start_readers(self) -> None:
        self._start_interception()
        if any(server.transform_result is not None for server in self._mcp_tools):
            self._mcp_reader = _create_task(self._read_mcp_results())

    def _start_interception(self) -> None:
        if self._interception is not None:
            self._interception.refresh()
        elif _has_interceptors(self):
            self._interception = _NativeInterception(self)
            with _clear_current():
                self._interception.start()

    @property
    def id(self) -> str:
        return self._id

    @property
    def codex_account(self) -> CodexAccount | None:
        """Read the saved Codex account and plan type selected at opening."""
        return self._codex_account

    def events(self, *, after: int = 0, include_execution_details: bool = False) -> EventStream:
        """Observe all turns until this Session ends.

        after is a retained global sequence, with zero replaying the retained
        suffix. Gaps report unavailable observations. Use items() to recover
        recorded completions, including completions after a turn settles.
        Use interactions() for live requests that need an answer.
        Set include_execution_details=True for a trusted reader that needs
        command, patch, and MCP payloads. The default includes their IDs and status.
        """
        self._check_loop()
        self._check_callback_operation("observe its own Session")
        return EventStream(
            self._observations, after=after, _include_execution_details=include_execution_details
        )

    async def pending_interactions(self) -> tuple[Interaction, ...]:
        """Read fresh, still-live requests from this Session and its children."""
        self._check_open()
        self._check_callback_operation("read interactions from its own Session")
        seen: set[int] = set()
        requests: list[Interaction] = []
        while (request := self._manual_request(None, seen)) is not None:
            seen.add(request._invocation)
            requests.append(request)
        requests.extend(cast("Iterator[Interaction]", self._pending_questions()))
        return tuple(requests)

    def _pending_questions(self) -> Iterator[_NativeQuestion]:
        for state in (self._active, *self._child_callbacks.values()):
            if (
                state is not None
                and not state.stopping
                and not state.failure.done()
                and not state.terminal.done()
            ):
                for question in state.questions.values():
                    if isinstance(question, VerificationRequest) and (
                        question._registration is not self._hosted_verification
                        or question.subagent_id is None
                        and not self._native._check_elicitation_pending(
                            question.server_name, question._message["id"]
                        )
                    ):
                        continue
                    yield question

    def interactions(self) -> AsyncIterator[Interaction]:
        """Yield each still-pending native request once for this reader.

        Requests come from the live registry, independently of event retention.
        Closing or cancelling a reader does not consume grants or stop a turn.
        """
        self._check_open()
        self._check_callback_operation("read interactions from its own Session")
        return self._interactions()

    async def _interactions(self) -> AsyncIterator[Interaction]:
        seen: set[int] = set()
        seen_questions: set[int] = set()
        while True:
            self._check_loop()
            self._check_callback_operation("read interactions from its own Session")
            signal = self._observations.changed
            request = self._manual_request(None, seen)
            if request is not None:
                seen.add(request._invocation)
                yield request
            else:
                questions = {id(question): question for question in self._pending_questions()}
                seen_questions.intersection_update(questions)
                pending = next((key for key in questions if key not in seen_questions), None)
                if pending is not None:
                    seen_questions.add(pending)
                    yield cast(Interaction, questions[pending])
                elif self._observations.closed:
                    return
                else:
                    await signal.wait()

    async def _wait_url_completion(self, question: ElicitationRequest, timeout: float) -> None:
        self._check_loop()
        if self._closing or self._closed or self._broken or self._sealed_checkpoint is not None:
            raise InteractionExpiredError("URL request is expired")
        self._check_callback_operation("wait for URL completion")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("URL completion timeout must be positive and finite")
        future = question._url_completion
        if question._session() is not self or future is None:
            raise InteractionExpiredError("URL request is expired")
        try:
            completed = await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            # Event delivery and the timer may both become ready in one loop turn.
            if future.done():
                if future.result():
                    return
                raise InteractionExpiredError("URL request is expired") from None
            key = (question._message["server_name"], question._message["id"])
            if self._url_completions.get(key) is question and not future.done():
                self._settle_url_completion(question, False)
                # Own cancellation through its receipt, even if this waiter is cancelled.
                await _join(asyncio.create_task(self._cancel_url_completion(question)))
            raise TimeoutError from None
        if not completed:
            raise InteractionExpiredError("URL request is expired")

    async def _cancel_url_completion(self, question: ElicitationRequest) -> None:
        if question.subagent_id is not None:
            await self._native._cancel_child_url_completion(question._message["_sdk_question_id"])
        else:
            await self._native.call(
                "cancel_url_completion",
                json.dumps(
                    {
                        "server_name": question._message["server_name"],
                        "request_id": question._message["id"],
                        "decision": "cancel",
                        "content": None,
                        "meta": None,
                    }
                ),
            )

    def _settle_url_completion(self, question: ElicitationRequest, completed: bool) -> None:
        question._release_tracing()
        key = (question._message["server_name"], question._message["id"])
        if self._url_completions.get(key) is question:
            del self._url_completions[key]
        future = question._url_completion
        if future is not None and not future.done():
            future.set_result(completed)
        if not completed:
            for state in (self._active, *self._child_callbacks.values()):
                if state is not None and state.questions.get(id(question)) is question:
                    del state.questions[id(question)]
            self._observations.notify()

    async def _probe_child_url_completion(
        self, question: ElicitationRequest, *, retire: bool = False
    ) -> bool | None:
        # Serialize native receipt consumption with publishing its Python result.
        async with self._url_completion_lock:
            future = question._url_completion
            if future is not None and future.done():
                return future.result()
            try:
                status = await self._native._child_url_completion_status(
                    question._message["_sdk_question_id"]
                )
            except RuntimeError:
                if not retire:
                    raise
                status = False
            if status is None and retire:
                status = False
            if status is not None:
                self._settle_url_completion(question, status)
            return cast(bool | None, status)

    async def _retire_url_completions(self) -> None:
        for question in tuple(self._url_completions.values()):
            if question.subagent_id is not None:
                await self._probe_child_url_completion(question, retire=True)
            else:
                self._settle_url_completion(question, False)

    def _check_question(self, question: _NativeQuestion) -> _CallbackState:
        self._check_loop()
        if (
            self._closing
            or self._closed
            or self._broken
            or (self._environment_lease is not None and self._environment_lease.interrupted)
            or self._sealed_checkpoint is not None
        ):
            raise InteractionExpiredError("Native request is expired or already answered")
        scope = _invocation.get()
        if scope is not None and scope.active:
            raise RuntimeError("A callback must return its reply; it cannot respond directly")
        state = (
            self._active
            if question.subagent_id is None
            else next(
                (
                    owner
                    for owner in self._child_callbacks.values()
                    if owner.handle.child_id == question.subagent_id
                    and owner.handle.submission_id == question.submission_id
                    and owner.questions.get(id(question)) is question
                ),
                None,
            )
        )
        if (
            state is None
            or state.handle is None
            or state.stopping
            or state.failure.done()
            or state.terminal.done()
            or question.submission_id not in (None, state.handle.submission_id)
            or state.questions.get(id(question)) is not question
        ):
            raise InteractionExpiredError("Native request is expired or already answered")
        if isinstance(question, VerificationRequest) and (
            question._registration is not self._hosted_verification
            or question.subagent_id is None
            and not self._native._check_elicitation_pending(
                question.server_name, question._message["id"]
            )
        ):
            raise InteractionExpiredError("Verification request is expired")
        self._check_open()
        return state

    async def _respond_question(
        self, question: _NativeQuestion, operation: str, encoded: str
    ) -> None:
        state = self._check_question(question)
        if question.subagent_id is not None:
            if not await self._native._child_question_pending(
                question._message["_sdk_question_id"]
            ):
                raise InteractionExpiredError("Native request is expired or already answered")
            state = self._check_question(question)
        # Consume only after encoding/validation and before the enqueue await.
        # Do not restore on uncertain cancellation: native may already have it.
        del state.questions[id(question)]
        question._release_tracing()
        self._observations.notify()

        async def enqueue() -> None:
            try:
                if question.subagent_id is None:
                    await self._native.call(operation, encoded)
                elif not await self._native._respond_child_question(
                    question._message["_sdk_question_id"], operation, encoded
                ):
                    raise InteractionExpiredError("Native request is expired or already answered")
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if state.stopping:
                    state.retain_cleanup_error(error)
                raise

        # Own the actual enqueue before yielding. Retirement must finish it
        # before this Session can admit another root turn with the same call ID.
        # Capture its result privately as for other callback-owned operations.
        with _private_task(enqueue()) as (task, outcome):
            state.add_callback(task)
            await _join(task, outcome=outcome)

    def _check_open(self) -> None:
        self._check_loop()
        if (
            self._closing
            or self._closed
            or (self._environment_lease is not None and self._environment_lease.interrupted)
        ):
            raise RuntimeError("Session is closed")
        if self._sealing:
            raise RuntimeError("Session is sealing a manual checkpoint")
        if self._sealed_checkpoint is not None:
            raise RuntimeError("Session was sealed for a manual checkpoint")
        if self._broken:
            raise RuntimeError("Session cannot continue after an unconfirmed native operation")

    def _check_owner(self, submission: Turn[Any]) -> None:
        self._check_loop()
        if submission._owner is not self._owner:
            raise StaleTurnError("Submission belongs to a different Session instance")

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Use Session in the event loop where it opened")

    def _check_callback_operation(self, operation: str) -> None:
        invocation = _invocation.get()
        while invocation is not None:
            if invocation.active and invocation.session is self:
                raise RuntimeError(f"A tool callback cannot {operation}")
            invocation = invocation.parent

    def _check_graph_admission(self) -> None:
        if self._graph_interrupts:
            raise RuntimeError("Session is interrupting its graph")

    def _check_root_admission(self) -> None:
        if self._root_interrupts:
            raise RuntimeError("Session is finishing root interruption")

    @property
    def capabilities(self) -> Capabilities:
        """Support in this admitted configuration; checkpoint readiness is separate."""
        self._check_open()
        return Capabilities(self)

    @property
    def active_turn(self) -> Turn[Any] | None:
        """The current root, type-erased because each root may override output."""
        self._check_loop()
        if self._active is not None and self._last_turn is not None:
            if self._owned_submission(self._last_turn) is self._active:
                return self._last_turn
        return None

    def rate_limits(self) -> ApiRateLimits:
        """Read the latest available limits reported by this API-key provider.

        This includes requests that correct prompted output.
        Missing values are unknown. This snapshot cannot determine account
        plan limits, credit balances or whether the provider will admit work.
        """
        self._check_loop()
        return self._api_rate_limit_snapshot

    @property
    def _strict_executor_writes(self) -> bool:
        return self._executor is not None and (
            self._executor._is_local or self._writable_roots is not None
        )

    async def _executor_policy_digest(self) -> str | None:
        if self._executor is None:
            return None
        if self._strict_executor_writes:
            # The mode also disables escalation and permission requests.
            policy = "strict-writes:" + await self._native._executor_policies()
        else:
            # An implicit WSS workspace can move on restore. Keep the write
            # mode, and let the separate network digest check network authority.
            return _IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST
        return hashlib.sha256(policy.encode("utf-8")).hexdigest()

    @property
    def restored_turn(self) -> Turn[Any] | None:
        """The initially restored pending root, even after another turn starts.

        None for a new Session or an idle restore. This is type-erased like
        active_turn: an owned restore can retain a root output that differs
        from the Session's next-turn default. The captured output contract is
        verified before new manual grants are issued.
        """
        self._check_loop()
        return self._restored_turn

    @overload
    async def submit(
        self,
        input: UserInput,
        *,
        output_type: None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        if_busy: Literal["reject", "queue"] = "reject",
        writable_roots: Sequence[str | PathLike[str]] | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
    ) -> Turn[OutputT]: ...

    @overload
    async def submit(
        self,
        input: UserInput,
        *,
        output_type: OutputSpec[_TurnT],
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        if_busy: Literal["reject", "queue"] = "reject",
        writable_roots: Sequence[str | PathLike[str]] | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
    ) -> Turn[_TurnT]: ...

    @overload
    async def submit(
        self,
        input: UserInput,
        *,
        if_busy: Literal["steer"],
        output_type: None = None,
        tools: None = None,
        before_step: None = None,
        writable_roots: None = None,
        parent_trace: None = None,
        trace_options: None = None,
    ) -> Turn[Any]: ...

    async def submit(
        self,
        input: UserInput,
        *,
        output_type: OutputSpec[Any] | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        if_busy: Literal["reject", "queue", "steer"] = "reject",
        writable_roots: Sequence[str | PathLike[str]] | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
    ) -> Turn[Any]:
        """Acknowledge a new root locally, or steer the captured active root.

        parent_trace overrides the Session parent for this new root and its
        automatic continuations. None and omission inherit the Session parent.
        Queued work captures its carrier now. Steering cannot change a parent.

        Omission inherits the Agent's output. Bare str selects plain text.
        PromptedOutput adds JSON guidance to this root's user input only.
        tools=None uses this Session's last accepted update, or opening defaults. An explicit
        sequence selects only already bound tools; [] permits no model tools. A new
        submit rejects busy work by default; if_busy="queue" registers FIFO new work.
        if_busy="steer" disallows overrides and awaits native acceptance for
        the captured active root. Use Turn.steer_exact to target one native root turn.
        New work returns before native admission; await Turn.get_result() for completion.
        before_step(turn, *, step_index) may narrow admitted tools before
        sampling. Return None to use the captured operation default, or a
        sequence of admitted declarations and ToolRefs.
        step_index counts preparations in that Turn from zero: a preparation
        can be cancelled before any model request.
        Keep this Turn to wait with the chosen type, steer or cancel exactly it.

        trace_options overrides linkage and adds native tracing restrictions.
        Explicit parent takes precedence over ambient context, then Session
        defaults. Disabled/content restrictions from every owner remain sticky.
        Capture occurs before queueing and remains alive through settled cleanup.
        Steering cannot change an admitted turn's tracing policy.

        For local executors and Linux WSS executors, writable_roots replaces
        the additional write grants starting with this turn. An empty sequence
        clears those grants. macOS WSS executors keep the opening write roots
        for the whole Session.
        None keeps the current policy. The working directory remains writable,
        and the network policy stays fixed. Changing roots requires a strict
        Environment and an idle graph.
        Selection cannot install new declarations, endpoints or credentials.
        Each native step retains its immutable selected router for
        direct, deferred-search and generated-code calls.
        A static subset can be suspended and restored under the existing native
        checkpoint conditions, against current opening declarations.
        A before_step callback cannot be recovered from portable state, so its
        operation refuses suspension before consuming live requests.
        """
        try:
            self._check_open()
            parent_trace = _trace_context(parent_trace)
            self._check_callback_operation("start work in its own Session")
            self._check_graph_admission()
            self._check_root_admission()
            if if_busy not in ("reject", "queue", "steer"):
                raise ValueError("if_busy must be 'reject', 'queue', or 'steer'")
            if if_busy == "steer" and (
                output_type is not None
                or tools is not None
                or before_step is not None
                or writable_roots is not None
                or trace_options is not None
                or parent_trace is not None
            ):
                raise TypeError("if_busy='steer' cannot override the new turn's settings")
            if self._mutation is not None or self._checkpoint_task is not None:
                raise SessionBusyError("Session is changing or saving its state")
            if if_busy == "steer" and self._active is not None:
                captured = self.active_turn
                if captured is None:
                    raise SessionBusyError("Cannot steer private Session work")
                await self._steer(captured, input)
                return captured
            output = self._default_output if output_type is None else _prepare_spec(output_type)
            parts = _native_input(input)
            roots = None
            if writable_roots is not None:
                if self._executor is None:
                    raise CapabilityError("Writable roots require a Session Environment")
                if self._native_agents is not None:
                    raise CapabilityError("Writable roots cannot change with NativeAgents")
                roots = _writable_roots(writable_roots)
            if output.mode == "prompted":
                parts.append(
                    {
                        "type": "text",
                        "text": "Return only JSON that matches this output schema: "
                        + json.dumps(output.schema, sort_keys=True, separators=(",", ":"))
                        + "\nUse this format only for this request, including its automatic "
                        "continuations and tool results. This output format instruction "
                        "does not apply to later requests.",
                    }
                )
            return await self._start(
                parts,
                output,
                tools=tools,
                before_step=before_step,
                enqueue=if_busy != "reject",
                writable_roots=roots,
                parent_trace=parent_trace,
                trace_options=trace_options,
            )
        finally:
            del trace_options

    async def _steer(
        self, turn: Turn[Any], input: UserInput, *, expected_turn_id: str | None = None
    ) -> SteeringReceipt | None:
        self._check_owner(turn)
        self._check_open()
        self._check_callback_operation("steer its own Session")
        self._check_graph_admission()
        if turn.child_id is not None:
            if expected_turn_id is not None:
                raise ValueError("Exact steering requires a root Turn")
            assert turn.instance_id is not None
            await self._submit_child_input(
                turn.child_id, turn.instance_id, turn._native.incarnation, input, turn
            )
            return None
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        if expected_turn_id is None and any(state.issued is turn for state, _ in self._pending):
            raise RuntimeError("Cannot steer queued work")
        parts = _native_input(input)
        while True:
            state = self._owned_submission(turn)
            if state is None or (
                expected_turn_id is not None
                and (
                    state.handle is None
                    or state.handle.turn_id != expected_turn_id
                    or state.terminal.done()
                )
            ):
                raise StaleTurnError("Turn is no longer active")
            if state.stopping or state.failure.done() or state.restored_pending or state.repairing:
                raise RuntimeError("Current turn cannot accept steering input")
            handle = await asyncio.shield(state.accepted)
            if self._owned_submission(turn) is not state:
                continue
            if state.stopping or state.failure.done() or state.restored_pending or state.repairing:
                raise RuntimeError("Current turn cannot accept steering input")
            # Revalidation above can wait for an owned successor before sending.
            # After sending, surface Core's receipt without retry or retarget.
            request: dict[str, Any] = {
                "mode": "steer",
                "expected_turn_id": handle.turn_id,
                "input": parts,
            }
            if expected_turn_id is not None:
                request["exact_steering"] = True
            captured = _CapturedTrace(None, None)
            if state.tracing is not None and state.tracing.context is not None:
                # Steering belongs to the admitted root, even if the caller's
                # ambient workflow changed. Retain it through native acceptance.
                with _activate(state.tracing.context):
                    captured = _capture()
                request["tracing_context"] = captured.context
            try:
                encoded = json.dumps(request)
                try:
                    response = await self._native._submit(encoded)
                except RuntimeError as exc:
                    if type(exc) is not RuntimeError or exc.args != (
                        "Native completion has no matching admission receipt",
                    ):
                        raise
                    raise StaleTurnError(
                        "Native Turn no longer accepts this steering input"
                    ) from exc
            finally:
                captured.close()
            receipt = json.loads(response)
            if receipt["status"] == "not_submitted":
                raise StaleTurnError("Native Turn no longer accepts this steering input")
            if receipt["status"] != "steered" or receipt["turn_id"] != handle.turn_id:
                raise NativeError(f"Native input was not submitted: {receipt.get('reason')}")
            if expected_turn_id is not None:
                input_id = receipt.get("input_id")
                if not isinstance(input_id, str) or not input_id:
                    raise NativeError("Native steering acceptance did not include an input ID")
                return SteeringReceipt(self.id, expected_turn_id, handle.submission_id, input_id)
            return None

    async def _start_child(self, child: Subagent, input: UserInput) -> Turn[str]:
        if child._session is not self:
            raise ValueError("Subagent belongs to another Session")
        if child.status != "active" or child._instance_id is None or child._incarnation is None:
            raise ValueError("Subagent is not a live native incarnation")
        return await self._submit_child_input(
            child.id, child._instance_id, child._incarnation, input, None
        )

    async def _submit_child_input(
        self,
        child_id: str,
        instance_id: int,
        incarnation: str,
        input: UserInput,
        turn: Turn[str] | None,
    ) -> Turn[str]:
        self._check_open()
        self._check_callback_operation("control a child of its own Session")
        self._check_graph_admission()
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        pending = _create_task(
            self._admit_child_input(child_id, instance_id, incarnation, input, turn)
        )
        self._child_admissions.add(pending)
        pending.add_done_callback(self._child_admission_finished)
        return cast(Turn[str], await _join(pending))

    async def _admit_child_input(
        self,
        child_id: str,
        instance_id: int,
        incarnation: str,
        input: UserInput,
        turn: Turn[str] | None,
    ) -> Turn[str]:
        # Recheck after taking task ownership: a stop can begin before this
        # admission has called native code. A caller's cancellation cannot
        # orphan an input Core has already accepted.
        self._check_open()
        self._check_graph_admission()
        encoded = json.dumps({"input": _native_input(input)})
        try:
            receipt = await self._native._submit_child(
                child_id, instance_id, encoded, None if turn is None else turn._native
            )
        except RuntimeError as exc:
            # Only the binding's captured-turn admission checks establish
            # staleness. In particular a busy fresh start, bad input, lost
            # capability, or provider error remains its original failure.
            if turn is not None and type(exc) is RuntimeError and len(exc.args) == 1:
                message = exc.args[0]
                if message in (
                    "Native child instance changed or is no longer live",
                    "Native child turn is no longer current",
                    "Native child is no longer live",
                    "Native child turn ownership changed",
                ) or (
                    isinstance(message, str)
                    and message.startswith("Native child steer was not submitted: ")
                ):
                    raise StaleTurnError("Native child Turn no longer accepts steering") from exc
            raise
        state = self._child_callback_state(child_id, incarnation, receipt.turn_id)
        if self._closing or self._broken or self._graph_interrupts:
            state.stop_continuation()
        elif state.continuation is None:
            driver = _create_task(self._continue_child_after_settlement(state, receipt))
            state.continuation = driver
            driver.add_done_callback(self._child_continuation_finished)
        return _child_turn(self, receipt)

    def _child_admission_finished(self, task: asyncio.Task[Turn[str]]) -> None:
        self._child_admissions.discard(task)
        if not task.cancelled():
            task.exception()
        self._finish_failed_observations()

    async def _continue_child_after_settlement(self, state: _ChildCallbacks, receipt: Any) -> None:
        await asyncio.shield(state.completion)
        if (
            self._closing
            or self._broken
            or self._graph_interrupts
            or state.failure.done()
            or state.cleanup_error is not None
        ):
            return
        terminal = state.terminal.result() if state.terminal.done() else None
        if terminal is None or terminal.get("_sdk_child_turn_failed") is not False:
            return
        successor = await self._native._continue_child_turn(receipt)
        if successor is not None:
            # Transfer ownership before ending this driver. This TurnStarted
            # comes from an actual native admission, even if the reader lags.
            self._child_callback_state(
                state.handle.child_id, state.handle.instance_id, successor.turn_id
            )

    def _child_continuation_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            # Never put a later driver error on the old child's callback result.
            # Close joins and reports this Session-owned operation's exception.
            self._broken = True
        self._finish_failed_observations()

    def _manual_request(
        self,
        submission_id: str | None,
        seen: set[int],
        *,
        _call_id: str | None = None,
    ) -> ActionRequested[Any, Any] | None:
        state: _CallbackState | None = self._active
        if self._closing:
            seen.clear()
            return None
        scope = _Invocation(self)
        token = _invocation.set(scope)
        try:
            pending = self._native._pending_manual_tools()
            live_invocations = {invocation for invocation, _, _ in pending}
            seen.intersection_update(live_invocations)
            for owner in [self._active, *self._child_callbacks.values()]:
                if owner is not None:
                    for invocation in tuple(owner.manual_inputs):
                        if invocation not in live_invocations:
                            del owner.manual_inputs[invocation]
            for invocation, grant, raw in pending:
                if invocation in seen:
                    continue
                message = json.loads(raw)["msg"]
                if _call_id is not None and message["callId"] != _call_id:
                    continue
                marker = message["_sdk_python_tool"]
                if not self._native._check_manual_tool(grant):
                    continue
                if marker["thread"] == self.id:
                    state = self._active
                else:
                    state = self._child_callback_state(
                        marker["thread"], marker["instance"], message["turnId"]
                    )
                    if invocation not in state.manual_inputs:
                        self._child_callback_state(
                            marker["thread"], marker["instance"], message["turnId"], message
                        )
                if (
                    state is None
                    or state.handle is None
                    or state.stopping
                    or state.failure.done()
                    or (submission_id is not None and state.handle.submission_id != submission_id)
                ):
                    continue
                handle = state.handle
                if message["turnId"] != handle.turn_id:
                    continue
                if len(raw.encode("utf-8")) > 65_536:
                    raise NativeError("Manual tool request exceeds the live action size limit")
                if marker["generation"] != self._catalog.generation:
                    raise NativeError("Manual tool request belongs to an expired catalog")
                entry = self._catalog.tools[marker["slot"]]
                if (
                    not isinstance(entry, _CapturedManual)
                    or entry.name != message["tool"]
                    or entry.namespace != message.get("namespace")
                ):
                    raise NativeError("Manual tool request does not match its declaration")
                if not self._native._check_manual_tool(grant):
                    state.manual_inputs.pop(invocation, None)
                    continue
                if self._fail_unhandled_questions:
                    state.failure.set_result(
                        InteractionRequired(
                            "Manual tools require on_interaction or Agent.session()"
                        )
                    )
                    return None
                tracing_context = message.get("tracing_context")
                if (
                    tracing_context is None
                    and isinstance(state, _Submission)
                    and state.restored_pending
                    and state.tracing is not None
                ):
                    tracing_context = state.tracing.context
                if invocation not in state.manual_inputs:
                    with (
                        _clear_current(),
                        _owned_span("manual input validation", "validation", tracing_context),
                    ):
                        arguments = entry.input_adapter.validate_python(message["arguments"])
                    if (
                        state.stopping
                        or state.failure.done()
                        or self._closing
                        or not self._native._check_manual_tool(grant)
                    ):
                        continue
                    state.manual_inputs[invocation] = arguments
                # Cache validation, never grant authority. Every cursor receives
                # its own value and we check the native grant on every exposure.
                arguments = deepcopy(state.manual_inputs[invocation])
                if (
                    state.stopping
                    or state.failure.done()
                    or self._closing
                    or not self._native._check_manual_tool(grant)
                ):
                    state.manual_inputs.pop(invocation, None)
                    continue
                return entry.request(
                    grant,
                    invocation,
                    marker["slot"],
                    self,
                    handle,
                    message["callId"],
                    arguments,
                    message["_sdk_code_origin"],
                    model=message.get("_sdk_model"),
                    tracing_context=tracing_context,
                )
        except BaseException:
            # Do not retain validator frames or private native arguments.
            if state is not None and not state.stopping and not state.failure.done():
                state.failure.set_result(
                    NativeError("Manual tool arguments could not be delivered")
                )
            return None
        finally:
            scope.active = False
            _invocation.reset(token)
        return None

    def _validate_pending_manual(self, state: _Submission) -> bool:
        """Validate every live manual grant, also when no observer is reading."""
        assert state.handle is not None
        seen: set[int] = set()
        while request := self._manual_request(state.handle.submission_id, seen):
            seen.add(request._invocation)
        return bool(seen) and not state.failure.done()

    async def _manual_ready(self, state: _Submission, *, probe: bool = False) -> bool:
        if (
            self._active is not state
            or state.handle is None
            or state.stopping
            or state.failure.done()
            or state.terminal.done()
            or any(not callback.done() for callback in state.callbacks)
        ):
            return False
        # Core persisted the full response sample before publishing its event.
        # This check also rejects any pending non-manual native operation.
        try:
            ready = await self._native._manual_ready()
        except RuntimeError:
            if not probe:
                raise
            raise NativeError("Native checkpoint readiness could not be verified") from None
        if not ready:
            return False
        return self._validate_pending_manual(state)

    async def _wait_manual_boundary(
        self, state: _Submission, *, require_request: bool = False
    ) -> bool:
        while (
            self._active is state
            and not state.stopping
            and not state.failure.done()
            and not state.terminal.done()
            and not self._closing
        ):
            # Subscribe before probing so sample finalization and callback
            # completion cannot leave this waiter asleep after the boundary.
            state.changed.clear()
            native_progress = self._native._manual_progress()
            if require_request and (
                state.handle is None
                or not self._validate_pending_manual(state)
                or self._child_admissions
                or any(not child.completion.done() for child in self._child_callbacks.values())
            ):
                return False
            if await self._manual_ready(state):
                return True
            changed = asyncio.create_task(state.changed.wait())
            finished = asyncio.ensure_future(self._native._wait_manual_progress(native_progress))
            try:
                done, _ = await asyncio.wait(
                    (changed, finished, state.failure, state.terminal),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for completed in done:
                    completed.result()
            finally:
                cancel_once(changed)
                cancel_once(finished)
                await asyncio.gather(changed, finished, return_exceptions=True)
        return False

    async def _respond(
        self, request: ActionRequested[Any, Any], result: Any, *, expected_json: str | None = None
    ) -> Turn[Any]:
        self._check_loop()
        if (
            self._closing
            or self._closed
            or self._broken
            or (self._environment_lease is not None and self._environment_lease.interrupted)
            or self._sealed_checkpoint is not None
        ):
            raise InteractionExpiredError(
                "Manual tool reply is foreign, expired or already delivered"
            )
        invocation = _invocation.get()
        if invocation is not None and invocation.active:
            raise RuntimeError("A callback must return its reply; it cannot respond directly")
        state: _CallbackState | None = (
            self._active
            if request.subagent_id is None
            else next(
                (
                    child
                    for child in self._child_callbacks.values()
                    if child.handle.child_id == request.subagent_id
                    and child.handle.submission_id == request.submission_id
                ),
                None,
            )
        )
        if (
            state is None
            or state.handle is None
            or state.stopping
            or state.failure.done()
            or request.session_id != self.id
            or request.submission_id != state.handle.submission_id
            or not self._native._check_manual_tool(request._grant)
        ):
            raise InteractionExpiredError(
                "Manual tool reply is foreign, expired or already delivered"
            )
        entry = self._catalog.tools[request._slot]
        if not isinstance(entry, _CapturedManual) or entry.declaration is not request._declaration:
            raise InteractionExpiredError("Manual tool reply belongs to a different declaration")
        self._check_open()
        if isinstance(state, _ChildCallbacks):
            # Native manual grants can reach a cursor before the event reader
            # observes this exact child turn. A still-live grant must wait for
            # that receipt before we can return its Turn.
            if state.native_instance is None:
                await asyncio.wait(
                    (state.observed, state.terminal, state.failure),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            native = await self._native._child_turn(state.handle.child_id)
            if (
                self._closing
                or self._closed
                or self._broken
                or (self._environment_lease is not None and self._environment_lease.interrupted)
                or self._sealed_checkpoint is not None
                or state.stopping
                or state.terminal.done()
                or state.failure.done()
                or not self._native._check_manual_tool(request._grant)
                or native is None
                or native.turn_id != state.handle.turn_id
                or native.incarnation != state.handle.instance_id
                or (
                    state.native_instance is not None
                    and native.instance_id != state.native_instance
                )
            ):
                raise InteractionExpiredError("Manual child tool reply is expired")
            entry = self._catalog.tools[request._slot]
            if (
                not isinstance(entry, _CapturedManual)
                or entry.declaration is not request._declaration
            ):
                raise InteractionExpiredError(
                    "Manual tool reply belongs to a different declaration"
                )
            handle: Turn[Any] = _child_turn(self, native)
        else:
            assert state.handle is not None
            handle = state.handle
        scope = _Invocation(self)
        token = _invocation.set(scope)
        output: str | None = None
        try:
            with (
                _clear_current(),
                _owned_span("manual output validation", "validation", request._tracing_context),
            ):
                value = entry.output_adapter.validate_python(result)
                if not self._native._check_manual_tool(request._grant):
                    raise InteractionExpiredError("Manual tool reply expired during validation")
                text = (
                    value
                    if isinstance(value, str)
                    else entry.output_adapter.dump_json(
                        value, warnings="error" if expected_json is not None else True
                    ).decode()
                )
                if expected_json is not None and text != expected_json:
                    raise ValueError("Manual tool result differs from its verified projection")
                response = value if isinstance(value, ToolOutput) else ToolOutput.text(text)
                response._validate()
                output = json.dumps(response.to_native())
        except Exception:
            pass
        finally:
            scope.active = False
            _invocation.reset(token)
        if output is None:
            if not self._native._check_manual_tool(request._grant):
                raise InteractionExpiredError("Manual tool reply expired during validation")
            raise ValueError("Manual tool result failed validation")

        async def deliver_and_continue() -> Turn[Any]:
            if (
                state.stopping
                or state.failure.done()
                or self._closing
                or self._closed
                or self._broken
                or (self._environment_lease is not None and self._environment_lease.interrupted)
                or self._sealed_checkpoint is not None
                or not await self._native._respond_manual_tool(request._grant, output)
            ):
                raise InteractionExpiredError("Manual tool reply is expired or already delivered")
            self._observations.notify()
            if isinstance(state, _Submission) and state.restored_pending:
                submission = state
                assert submission.handle is not None
                observation_route = submission.handle.submission_id

                async def continue_restored_turn() -> None:
                    try:
                        # Core may already have queued recovery when this callback
                        # is cancelled. Retain its mutation ownership through the
                        # admission acknowledgement before retirement can pass it.
                        context = (
                            submission.tracing.context if submission.tracing is not None else None
                        )
                        tracing_args = {}
                        if context is not None:
                            tracing_args["tracing_context"] = context
                        if submission.parent_trace is not None:
                            tracing_args["trace_json"] = json.dumps(
                                submission.parent_trace.to_headers()
                            )
                        operation = self._native._continue_manual(observation_route, **tracing_args)
                        with _private_task(operation) as (admitting, admitted):
                            receipt = json.loads(await _join(admitting, outcome=admitted))
                        if receipt["status"] == "pending":
                            return
                        if receipt["status"] == "not_pending":
                            # A concurrently submitted last reply already recovered
                            # this frame. Its owned task validates the receipt.
                            submission.restored_pending = False
                            return
                        if receipt["status"] != "started" or receipt["turn_id"] != request.turn_id:
                            raise NativeError("Native manual continuation was not resumed")
                        submission.restored_pending = False
                    except BaseException as error:
                        if not submission.stopping and not submission.failure.done():
                            submission.failure.set_result(error)
                        raise
                    finally:
                        submission.changed.set()

                continuation = _create_task(continue_restored_turn())
                submission.add_callback(continuation)
                await _join(continuation)
            if isinstance(state, _Submission):
                state.changed.set()
            return handle

        with _clear_current(), _private_task(deliver_and_continue()) as (delivery, delivered):
            state.add_callback(delivery)
            return cast(Turn[Any], await _join(delivery, outcome=delivered))

    async def compact(self, *, summary: str | None = None) -> None:
        """Compact native conversation context and wait for owned settlement.

        This idle-only operation uses Core's compaction task. It does not submit
        a user message or decode Agent output. Cancelling this call stops and
        joins its native work. Session close also waits for that cleanup.
        Cancellation does not undo history changes Core has already committed.

        With summary, replace model context with the supplied nonempty text,
        preserving current instructions and the original canonical history.
        This does not call the model or create a Turn. It requires an exclusively
        idle root without child or background work. Local profiles cannot be
        copied into this private context. Programmatic tool calling is supported
        only while full native history proves that no code VM was used. Used or
        previously compacted programmatic sessions must use compact() instead.
        Used code state remains subject to this restriction while dormant.
        Unresolved tool effects are not discarded. Current bindings and the Session's workspace stay
        owned by this Session. Cancellation joins this publication before new
        work may start; a published summary is not rolled back.
        """
        self._check_callback_operation("compact its own Session")
        if summary is not None:
            self._check_idle_update()
            if not isinstance(summary, str):
                raise TypeError("summary must be a string")
            if not summary.strip():
                raise ValueError("summary must not be empty")
            self._check_portable_capture()
            if self._child_admissions or any(
                child.busy for child in self._child_callbacks.values()
            ):
                raise RuntimeError("Cannot compact while native child callbacks remain active")
            await self._join_mutation(asyncio.create_task(self._compact_context(summary)))
            return
        handle = await self._start(None, self._default_output)
        try:
            assert handle._completion is not None
            await asyncio.shield(handle._completion)
        except asyncio.CancelledError as error:
            if self._active is not None and self._active.handle is handle:
                await _close_after_error(error, lambda: self._cancel(handle))
            raise

    async def _compact_context(self, summary: str) -> None:
        assert self._native_options is not None
        assert self._opening_agent is not None
        native = importlib.import_module("agents._native")
        change = await self._native._begin_context_change()
        engine = None
        thread = None
        retiring = False
        try:
            current = json.loads(change.context())
            retained = [
                index
                for index, item in enumerate(current["items"])
                if item.get("role") in ("system", "developer")
            ]
            # Core constructs the checkpoint from original native envelopes,
            # rejects unknown/unpaired effects, and retains review provenance.
            checkpoint = change.prepare(summary, retained)
            saved = json.loads(checkpoint)
            code = saved.get("root_code_mode")
            if code is not None and (
                code["next_cell_id"] != 1 or code["stored_values"] or code["completed_cells"]
            ):
                raise RuntimeError("Supplied compaction requires verified unused programmatic code")
            if self._opening_agent.programmatic_tool_calling:
                # Ignore only the compaction record just prepared by this
                # transaction. An older compacted context cannot establish the
                # complete VM history. Inspect canonical records, not just the
                # visible context, since rolled-back turns may have used code.
                history = saved["history"][:-1]
                for row in history:
                    item = row["payload"]
                    if row["type"] == "compacted" or (
                        row["type"] == "response_item"
                        and item.get("type") in ("custom_tool_call", "function_call")
                        and item.get("name") in ("exec", "wait")
                        and item.get("namespace") in (None, "", "functions")
                    ):
                        raise RuntimeError(
                            "Supplied compaction requires verified unused programmatic code"
                        )
            options = json.loads(self._native_options)
            options["_application_instructions"] = self._instructions
            options["_manual_tool_slots"] = self._catalog.manual_slots()
            current_options = json.loads(change.options())
            options["_sdk_instruction_files"] = current_options["instruction_files"]
            applied_roots = current_options["executor_writable_roots"]
            if applied_roots is not None:
                options["runtime"]["environment"]["writable_roots"] = applied_roots
            opening = self._opening_agent.tools
            engine = await native.create_checkpoint_engine(
                json.dumps(options["config"]),
                json.dumps(options["runtime"]),
                api_key=(
                    None if self._codex_login_binding is not None else (self._model_api_key or "")
                ),
                provider_lease=self._provider_lease,
                **(
                    {
                        "initial_checkpoint": checkpoint,
                        "initial_specs_json": self._catalog.native_specs(),
                        "initial_options_json": json.dumps(options),
                    }
                    if self._native_agents is not None
                    else {}
                ),
            )
            thread = await engine.restore_checkpoint(
                checkpoint,
                json.dumps(options),
                self._catalog.native_specs(),
            )
            await change.validate_candidate(thread)
            available = opening
            _, allowed_mcp, _ = _split_tools(available)
            if allowed_mcp:
                receipt = await self._publish_startup(
                    thread._replace_mcp_servers(
                        _mcp_config(
                            self._mcp_tools,
                            self._executor,
                            allowed_mcp,
                            programmatic_tool_calling=self._opening_agent.programmatic_tool_calling,
                        ),
                        _result_policies(self._mcp_tools, self._catalog.generation),
                    ),
                    candidate=thread,
                )
                _check_mcp_receipt(receipt, self._mcp_tools, allowed_mcp, self._catalog.generation)
            # From this point, retirement cannot roll back. Keep the old reader
            # attached until its own native EOF; do not end the Session stream.
            retiring = True
            self._sealing = True
            if self._interception is not None:
                await self._interception.close()
                self._interception = None
            if self._mcp_reader is not None:
                cancel_once(self._mcp_reader)
                await self._mcp_reader
                self._mcp_reader = None
            await change.retire()
            await self._reader
            # Release the context lease before clearing the provider-held owner.
            change.release()
            await self._engine.close()
            self._engine = engine
            self._native = thread
            engine = thread = None
            self._reader = _create_task(self._read_events())
            self._sealing = False
            self._start_readers()
        except BaseException as error:
            if retiring:
                self._broken = True
            if thread is not None:
                await _close_after_error(error, thread.close)
            if engine is not None:
                await _close_after_error(error, engine.close)
            raise
        finally:
            self._sealing = False
            change.release()

    async def _run_with_interactions(
        self,
        submission: Turn[OutputT],
        handler: _InteractionHandler | None,
        *,
        return_state: bool = False,
        pause_manual: Sequence[FunctionTool[Any, Any, Any]] = (),
    ) -> Completed[OutputT]:
        """Own live replies and Session shutdown for a one-shot run."""
        failure: asyncio.Future[BaseException] = self._loop.create_future()
        callbacks: list[asyncio.Task[None]] = []
        stopping = False

        async def answer(
            event: Interaction, owner: _CallbackState, tracing_owner: SpanContext | None
        ) -> None:
            skipped = False

            def admitted() -> bool:
                nonlocal skipped
                allowed = (
                    not stopping
                    and not self._closing
                    and not owner.stopping
                    and not owner.failure.done()
                    and not owner.terminal.done()
                    and (
                        self._native._check_manual_tool(event._grant)
                        if isinstance(event, ActionRequested)
                        else owner.questions.get(id(event)) is event
                    )
                )
                if allowed and isinstance(event, VerificationRequest):
                    allowed = event._registration is self._hosted_verification and (
                        event.subagent_id is not None
                        or self._native._check_elicitation_pending(
                            event.server_name, event._message["id"]
                        )
                    )
                # _call_callback can refuse entry before a queued worker or
                # its returned awaitable starts. That is not a handler result.
                if not allowed:
                    skipped = True
                return allowed

            async def invoke_body() -> None:
                try:
                    assert handler is not None
                    if not admitted():
                        return
                    if event.subagent_id is not None and not isinstance(event, ActionRequested):
                        if (
                            not await self._native._child_question_pending(
                                event._message["_sdk_question_id"]
                            )
                            or not admitted()
                        ):
                            return
                    scope = _Invocation(self)
                    token = _invocation.set(scope)
                    try:
                        if isinstance(event, VerificationRequest):
                            provider_failed = False
                            answer = None
                            try:
                                answer = await _call_callback(handler, admitted, event)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                provider_failed = True
                            if provider_failed:
                                raise InteractionRequired("Verification provider failed")
                        else:
                            answer = await _call_callback(handler, admitted, event)
                        # The handler's authority ends here. Keep this inactive
                        # frame above any enclosing callback while the SDK
                        # delivers; validators still retain the parent guards.
                        scope.active = False
                        if skipped:
                            return
                        if not isinstance(answer, ActionReply) or answer._request is not event:
                            raise TypeError(
                                "on_interaction must return a reply to the interaction it received"
                            )
                        # Another cursor may have answered while the handler ran.
                        if not admitted():
                            return
                        if isinstance(event, ElicitationRequest) and answer._value is _URL_ACCEPT:
                            await event.accept_url()
                        elif (
                            isinstance(event, (ElicitationRequest, VerificationRequest))
                            and answer._value is None
                        ):
                            await event.decline()
                        else:
                            try:
                                await event.respond(answer._value)
                            except InteractionExpiredError:
                                # A native owner may retire while SDK delivery awaits its receipt.
                                if not isinstance(owner, _ChildCallbacks):
                                    raise
                                if isinstance(event, ActionRequested):
                                    if self._native._check_manual_tool(event._grant):
                                        raise
                                elif await self._native._child_question_pending(
                                    event._message["_sdk_question_id"]
                                ):
                                    raise
                    finally:
                        scope.active = False
                        _invocation.reset(token)
                except BaseException as error:
                    if isinstance(error, asyncio.CancelledError) and (
                        stopping or self._closing or owner.stopping or owner.terminal.done()
                    ):
                        return
                    if not failure.done():
                        failure.set_result(error)
                    if not owner.stopping and not owner.failure.done():
                        owner.failure.set_result(error)
                    elif not isinstance(error, asyncio.CancelledError):
                        owner.retain_cleanup_error(error)

            async def invoke() -> None:
                context = (
                    event._tracing_context
                    if isinstance(event, ActionRequested)
                    else tracing_owner._handle
                    if tracing_owner is not None
                    else None
                )
                if context is None and isinstance(owner, _Submission) and owner.tracing is not None:
                    context = owner.tracing.context
                with (
                    _clear_current(),
                    _owned_span("interaction callback", "callback", context) as tracing,
                ):
                    await invoke_body()
                    if tracing is not None and owner.failure.done():
                        tracing.set_outcome(status="error", category="callback_error")

            pending = asyncio.create_task(invoke())
            try:
                # The owner also waits for this callback. Watch its native
                # terminal directly so an independently answered grant cannot
                # strand a handler that is still waiting for application input.
                await asyncio.wait((pending, owner.terminal), return_when=asyncio.FIRST_COMPLETED)
            finally:
                cancel_once(pending)
                await _join(pending)

        async def consume() -> None:
            async for event in self.interactions():
                if stopping:
                    return
                owner: _CallbackState | None = (
                    self._active
                    if event.subagent_id is None
                    else next(
                        (
                            child
                            for child in self._child_callbacks.values()
                            if child.handle.child_id == event.subagent_id
                            and child.handle.submission_id == event.submission_id
                        ),
                        None,
                    )
                )
                if (
                    owner is None
                    or owner.handle is None
                    or (
                        owner.handle.submission_id != event.submission_id
                        and not (
                            isinstance(owner, _Submission)
                            and isinstance(event, (ElicitationRequest, VerificationRequest))
                            and event.submission_id is None
                            and owner.questions.get(id(event)) is event
                        )
                    )
                    or owner.stopping
                    or owner.terminal.done()
                ):
                    continue
                if handler is None:
                    if (
                        not isinstance(event, ActionRequested)
                        or event.subagent_id is not None
                        or not any(tool.match(event) is not None for tool in pause_manual)
                    ):
                        raise InteractionRequired(
                            "Native interaction needs on_interaction or Agent.session()"
                        )

                    def check_pending() -> None:
                        # Check the whole settled sample, not only the first
                        # request this cursor happened to observe.
                        seen: set[int] = set()
                        while (pending := self._manual_request(None, seen)) is not None:
                            seen.add(pending._invocation)
                            if pending.subagent_id is not None or not any(
                                tool.match(pending) is not None for tool in pause_manual
                            ):
                                raise InteractionRequired(
                                    "Native interaction needs on_interaction or Agent.session()"
                                )

                    try:
                        await self._suspend(before_seal=check_pending)
                    except _ManualBoundaryLost:
                        if owner.failure.done():
                            # Let the Turn join callbacks and report its primary
                            # failure, rather than this aborted capture attempt.
                            return
                        # A live cursor may have consumed this grant while
                        # automatic callbacks were settling. Real capture
                        # errors and lost boundaries with live grants still fail.
                        if self._native._check_manual_tool(event._grant):
                            raise
                        continue
                    return
                tracing_owner = event._tracing if isinstance(event, _NativeQuestion) else None
                task = asyncio.create_task(answer(event, owner, tracing_owner))
                callbacks.append(task)
                owner.add_callback(task)

        reader = asyncio.create_task(consume())
        waiter = asyncio.create_task(submission.get_result())

        async def close() -> None:
            nonlocal stopping
            stopping = True
            cancel_once(reader)
            try:
                # Session owns native interruption and callback cleanup. Close
                # it before draining the reader and remaining handler tasks.
                await self._aclose()
            finally:
                for task in callbacks:
                    cancel_once(task)
                cancel_once(waiter)
                await asyncio.gather(reader, waiter, *callbacks, return_exceptions=True)
            if failure.done():
                raise failure.result()

        try:
            await asyncio.wait((waiter, reader, failure), return_when=asyncio.FIRST_COMPLETED)
            if failure.done():
                raise failure.result()
            if reader.done():
                await reader
            result = await waiter
            if return_state:
                result = replace(result, state=await self.save_state())
        except BaseException as error:
            await _close_after_error(error, close)
            raise
        else:
            await _join(asyncio.create_task(close()))
            return result

    async def _start(
        self,
        input: list[dict[str, Any]] | None,
        output: _PreparedOutput,
        *,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        enqueue: bool = False,
        writable_roots: tuple[str, ...] | None = None,
        parent_trace: TraceContext | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
    ) -> Turn[Any]:
        """Own one native task; None selects Core's compaction operation."""
        try:
            self._check_open()
            self._check_graph_admission()
            self._check_root_admission()
            if (
                self._mutation is not None
                or (self._active is not None or self._pending)
                and (not enqueue or input is None)
                or self._active is not None
                and self.active_turn is None
            ):
                raise SessionBusyError("Session already has an active submission or mutation")
            if self._checkpoint_task is not None:
                raise RuntimeError("Session is saving a checkpoint")
            if output.max_retries and self._storage is not None:
                raise CapabilityError(
                    "LocalStorage cannot retain prompted-output repair receipts; "
                    "use a separate ephemeral Session or max_retries=0"
                )
            if before_step is not None and not callable(before_step):
                raise TypeError("before_step must be callable")
            if tools is None:
                tools = self._default_tools
            slots = None
            mcp = None
            operation_refs = None
            if input is not None:
                assert self._opening_agent is not None
                opening = self._opening_agent.tools
                python, mcp, _ = _split_tools(
                    opening if tools is None else tools,
                    self._opening_agent.programmatic_tool_calling,
                    available=None if tools is None else self._bindings,
                    ceiling=opening,
                    environment=self._executor,
                    native_agents=None if tools is None else self._native_agents,
                )
                if tools is not None:
                    operation_refs = _selected_tool_refs(
                        tools,
                        python,
                        mcp,
                        programmatic_tool_calling=self._opening_agent.programmatic_tool_calling,
                        environment=self._executor,
                        native_agents=self._native_agents,
                    )
                for selected_tool in python:
                    selected_tool._check_context(self._context)
                for server in mcp:
                    server._check_context(self._context)
                if tools is not None or before_step is not None:
                    indices = []
                    for tool in python:
                        slot = next(
                            (
                                i
                                for i, captured in enumerate(self._bound_tools)
                                if captured.name == tool.name
                            ),
                            None,
                        )
                        if slot is None or slot in indices:
                            raise CapabilityError(
                                "Operation tools must select distinct captured slots"
                            )
                        indices.append(slot)
                    slots = tuple(indices)
            state = _Submission(
                output,
                before_step=before_step,
                operation_slots=slots,
                operation_mcp=mcp,
                operation_refs=operation_refs,
                writable_roots=writable_roots,
                parent_trace=parent_trace if parent_trace is not None else self._parent_trace,
                tracing=_capture(trace_options, self._trace_context),
            )
            if input is not None:
                issued = _root_turn(self, None, uuid4().hex, state.completion)
                state.issued = issued
                self._pending.append((state, input))
                self._promote()
                if state.task is None and state.completion.done():
                    state.completion.result()
                return issued
            self._active = state
            startup_error = self._start_execution(state, input)
            if startup_error is not None:
                self._promote()
                raise startup_error
            try:
                return await asyncio.shield(state.accepted)
            except asyncio.CancelledError as error:
                await _close_after_error(error, self._aclose)
                raise
        finally:
            del trace_options

    def _start_execution(
        self, state: _Submission, input: list[dict[str, Any]] | None
    ) -> BaseException | None:
        try:
            state.task = _create_task(self._execute(state, input))
        except BaseException as error:
            tracing, state.tracing = state.tracing, None
            if tracing is not None:
                try:
                    tracing.close(error)
                except BaseException as cleanup:
                    _retain_cleanup_error(error, cleanup, _error_contexts(error.__context__))
            # No execution body ran. Retire only host admission, preserving
            # the factory exception rather than cancelling native work.
            if self._active is state:
                self._active = None
            if not state.accepted.done():
                state.accepted.set_exception(error)
            if not state.completion.done():
                state.completion.set_exception(error)
            self._observations.notify()
            return error
        return None

    def _discard_pending(self, error: BaseException) -> None:
        while self._pending:
            state, _ = self._pending.popleft()
            if state.tracing is not None:
                state.tracing.close(error, cancelled=isinstance(error, TurnStoppedError))
            state.accepted.set_exception(error)
            state.completion.set_exception(error)
        self._observations.notify()

    def _promote(self) -> None:
        """Claim only on logical release. This is not a second execution driver."""
        if (
            self._broken
            or self._closed
            or self._closing
            or (self._environment_lease is not None and self._environment_lease.interrupted)
        ):
            self._discard_pending(NativeError("Session ended before queued work was admitted"))
        while (
            not self._broken
            and not self._closed
            and not self._closing
            and not (self._environment_lease is not None and self._environment_lease.interrupted)
            and self._active is None
            and self._mutation is None
            and self._checkpoint_task is None
            and not self._graph_interrupts
            and not self._root_interrupts
            and self._pending
        ):
            state, parts = self._pending.popleft()
            self._active = state
            self._last_turn = state.issued
            self._start_execution(state, parts)

    def _check_idle_update(self) -> None:
        self._check_open()
        self._check_callback_operation("update its own Session")
        self._check_graph_admission()
        self._check_root_admission()
        if (
            self._active is not None
            or self._pending
            or self._mutation is not None
            or self._checkpoint_task is not None
        ):
            raise RuntimeError("Cannot update settings while the Session is busy")

    @overload
    async def update(
        self,
        *,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        instructions: None = None,
        tools: None = None,
    ) -> None: ...

    @overload
    async def update(
        self,
        *,
        instructions: str | None = None,
        model: None = None,
        model_settings: None = None,
        tools: None = None,
    ) -> None: ...

    @overload
    async def update(
        self,
        *,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents]
        | None = None,
        model: None = None,
        model_settings: None = None,
        instructions: None = None,
    ) -> None: ...

    async def update(
        self,
        *,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        instructions: str | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents]
        | None = None,
    ) -> None:
        """Update one idle category: tools, model/settings, or instructions.

        None leaves a category unchanged; empty settings change nothing.
        An empty instruction string clears instructions. tools sets the default
        subset for later submits and may redefine Python/manual names and MCP
        server/tool pairs admitted at opening. Explicit submit and before_step
        objects select opening or current declarations; refs use the latest
        accepted bindings. Neither route may install a fresh declaration.
        Fixed grants and host-stdio startup authority remain as opened.
        Cancellation joins publication; accepted changes stay applied.
        """
        self._check_idle_update()
        settings = _native_model_settings(model, model_settings)
        if tools is not None:
            if instructions is not None or settings:
                raise ValueError("Update tools separately from instructions and model settings")
            assert self._opening_agent is not None
            selection = tuple(tools)
            opening = self._opening_agent.tools
            # Retain captured executor and collaboration grants in update
            # defaults. Explicit submissions still validate their whole roster.
            implicit = _fixed_tool_refs(
                (),
                self._opening_agent.programmatic_tool_calling,
                None if any(tool is self._executor for tool in selection) else self._executor,
                (
                    None
                    if any(tool is self._native_agents for tool in selection)
                    else self._native_agents
                ),
            )
            selected_refs = {tool for tool in selection if isinstance(tool, ToolRef)}
            missing_refs: list[ToolRef] = sorted(
                implicit - selected_refs, key=lambda ref: (ref.namespace or "", ref.name)
            )
            selection += tuple(missing_refs)
            python, mcp, _ = _split_tools(
                selection,
                self._opening_agent.programmatic_tool_calling,
                available=self._bindings,
                ceiling=opening,
                rebind=True,
                environment=self._executor,
                native_agents=self._native_agents,
            )
            for tool in python:
                tool._check_context(self._context)
            for server in mcp:
                server._check_context(self._context)
            allowed_python, allowed_mcp, _ = _split_tools(opening)
            if any(tool.defer_loading for tool in python) and not (
                any(tool.defer_loading for tool in allowed_python)
                or any(server.defer_loading for server in allowed_mcp)
            ):
                raise CapabilityError("Python schema discovery must be granted at Session opening")
            opening_mcp_modes = {server.name: server.defer_loading for server in allowed_mcp}
            if any(server.defer_loading != opening_mcp_modes[server.name] for server in mcp):
                raise CapabilityError("MCP schema discovery mode must match Session opening")
            # MCP defaults use validated leaf selectors, not declaration identities
            # which a same-alias replacement can supersede during publication.
            defaults = tuple(
                ref
                for tool in selection
                for ref in (
                    tuple(tool.tool(raw) for raw in tool.tools)
                    if isinstance(tool, (HttpMCP, StdioMCP))
                    else (tool,)
                )
            )
            # Explicit idle declarations replace bindings; refs only select.
            # Preserve every omitted opening slot and alias for later operations.
            supplied_python = {
                tool.name: tool for tool in selection if isinstance(tool, FunctionTool)
            }
            supplied_mcp = tuple(
                tool for tool in selection if isinstance(tool, (HttpMCP, StdioMCP))
            )
            bindings: list[Tool[ContextT]] = []
            for bound in self._bindings:
                if isinstance(bound, FunctionTool):
                    bindings.append(supplied_python.get(bound.name, bound))
                elif isinstance(bound, (HttpMCP, StdioMCP)):
                    if not any(server.name == bound.name for server in supplied_mcp):
                        bindings.append(bound)
                else:
                    bindings.append(bound)
            # A validated alias can contain several disjoint descriptor identities.
            # Replace its old identities once; keep all current ones selectable.
            bindings.extend(supplied_mcp)
            selected = tuple(
                supplied_python[bound.name]._capture(self._context)
                if bound.name in supplied_python
                else bound
                for bound in self._bound_tools
            )
            bound_tools = selected
            if self._native_agents is not None:
                names = {tool.name for tool in python}
                selected = tuple(tool for tool in selected if tool.name in names)
            candidate = _ToolCatalog(self._catalog.generation + 1, selected)

            async def publish() -> None:
                await self._replace_tools(candidate, mcp, allowed_mcp, candidate.native_specs())
                self._bound_tools = bound_tools
                self._bindings = tuple(bindings)
                self._default_tools = defaults

            await self._join_mutation(asyncio.create_task(publish()))
            return
        if instructions is not None:
            if not isinstance(instructions, str):
                raise TypeError("instructions must be a string or None")
            if settings:
                raise ValueError("Update instructions separately from model settings")
        if self._native_agents is not None and (settings or instructions is not None):
            raise CapabilityError(
                "NativeAgents settings and instructions cannot change after the Session opens"
            )
        if instructions is not None:
            await self._join_mutation(asyncio.create_task(self._update_instructions(instructions)))
        elif settings:
            await self._join_mutation(
                asyncio.create_task(self._update_model_settings(json.dumps(settings)))
            )

    async def _update_model_settings(self, encoded: str) -> None:
        try:
            acknowledgement = await self._native._update_model_settings(encoded)
        except BaseException as error:
            if not isinstance(error, ValueError):
                self._broken = True
            raise
        if acknowledgement is not True:
            self._broken = True
            raise NativeError("Native model update acknowledgement did not match")

    async def _update_instructions(self, instructions: str) -> None:
        try:
            acknowledgement = await self._native._update_instructions(instructions)
        except BaseException as error:
            if not isinstance(error, ValueError):
                self._broken = True
            raise
        if acknowledgement is not True:
            self._broken = True
            raise NativeError("Native instruction update acknowledgement did not match")
        self._instructions = instructions

    async def _join_mutation(self, pending: asyncio.Task[_MutationResultT]) -> _MutationResultT:
        self._mutation = pending
        try:
            return cast(_MutationResultT, await _join(pending))
        except BaseException:
            if not pending.done() or pending.cancelled():
                self._broken = True
            raise
        finally:
            if pending.done():
                self._mutation = None
            if self._broken and not self._closing:
                self._finish_failed_observations()

    async def _publish_startup(self, operation: Any, *, candidate: Any = None) -> Any:
        """Reject no-Turn prompts and join any outstanding native publication."""
        native = self._native if candidate is None else candidate
        watcher: asyncio.Task[None] | None = None
        witness: asyncio.Future[Any]
        if candidate is None:
            self._startup_failure = self._loop.create_future()
            witness = self._startup_failure
        else:

            async def reject_candidate_questions() -> None:
                while True:
                    event = json.loads(await native._next_session_event())
                    if event is None:
                        raise NativeError("Compaction candidate ended during MCP publication")
                    if event.get("msg", {}).get("type") in {
                        "exec_approval_request",
                        "apply_patch_approval_request",
                        "elicitation_request",
                        "request_user_input",
                        "request_permissions",
                    }:
                        raise InteractionRequired(
                            "Native startup requires interaction; "
                            "supply startup configuration before opening"
                        )

            watcher = asyncio.create_task(reject_candidate_questions())
            witness = watcher
        pending = asyncio.ensure_future(operation)
        try:
            await asyncio.wait((pending, witness), return_when=asyncio.FIRST_COMPLETED)
            if witness.done():
                if watcher is not None:
                    await watcher
                else:
                    raise cast(BaseException, witness.result())
            return await _join(pending)
        except BaseException as error:
            # A settled native rejection may leave an opened Session usable.
            # Only retire native work that still needs to be interrupted and joined.
            if not pending.done():

                async def retire() -> None:
                    # The receipt owns and joins cancellation of the native
                    # mutation; close must not wait behind its startup prompt.
                    cancel_once(pending)
                    try:
                        await native.close()
                    finally:
                        await asyncio.gather(pending, return_exceptions=True)

                await _close_after_error(error, retire)
            raise
        finally:
            if watcher is None:
                self._startup_failure = None
            else:
                cancel_once(watcher)
                await asyncio.gather(watcher, return_exceptions=True)

    async def _replace_tools(
        self,
        candidate: _ToolCatalog,
        mcp_tools: tuple[_MCP[Any], ...],
        allowed_mcp: tuple[_MCP[Any], ...],
        specs: str,
    ) -> None:
        if isinstance(self._storage, CustomStorage) and candidate.manual_slots():
            raise ValueError("CustomStorage preview does not support manual tools")
        assert self._opening_agent is not None
        if self._native_agents is not None:
            children = json.loads(await self._native.call("list_subagents"))
            if self._child_admissions or any(
                child.busy for child in self._child_callbacks.values()
            ):
                raise RuntimeError("Cannot replace tools while native children remain open")
            for child in children:
                if child["status"] == "active" and (
                    candidate.tools != self._catalog.tools or child["runtime_loaded"]
                ):
                    # Native startup copies the catalog before the event reader
                    # registers an incarnation. Only actually cold records have
                    # no native catalog, even if Python bindings still match.
                    raise RuntimeError("Cannot replace tools while native children remain open")
        mcp_published = False
        if allowed_mcp:
            try:
                receipt = await self._publish_startup(
                    self._native._replace_mcp_servers(
                        _mcp_config(
                            mcp_tools,
                            self._executor,
                            allowed_mcp,
                            programmatic_tool_calling=self._opening_agent.programmatic_tool_calling,
                        ),
                        _result_policies(mcp_tools, candidate.generation),
                    )
                )
            except BaseException as error:
                # MCP ValueErrors reject before publication. Other failures can
                # hide an accepted roster, unlike Python's RuntimeError contract.
                if not isinstance(error, ValueError):
                    self._broken = True
                raise
            try:
                _check_mcp_receipt(receipt, mcp_tools, allowed_mcp, candidate.generation)
            except NativeError:
                self._broken = True
                raise
            mcp_published = True
        try:
            manual_slots = candidate.manual_slots()
            acknowledgement = await (
                self._native._replace_tools(candidate.generation, specs, manual_slots)
                if manual_slots
                else self._native._replace_tools(candidate.generation, specs)
            )
        except BaseException as error:
            # Binding RuntimeErrors reject before publication. Transport failures
            # and cancellation can hide an accepted generation, so fence those.
            if (
                mcp_published
                or not isinstance(error, RuntimeError)
                or isinstance(error, _NativeCompletionError)
            ):
                self._broken = True
            raise
        if type(acknowledgement) is not int or acknowledgement != candidate.generation:
            self._broken = True
            raise NativeError("Native tool replacement acknowledgement did not match")
        # The binding publishes only after validation and its native idle check.
        # Keep Python definitions unchanged until that exact generation is accepted.
        self._catalog = candidate
        self._mcp_tools = mcp_tools
        self._start_interception()
        if self._mcp_reader is None and any(s.transform_result is not None for s in mcp_tools):
            self._mcp_reader = _create_task(self._read_mcp_results())

    def _owned_submission(self, turn: Turn[Any]) -> _Submission | None:
        """Find only the active native owner or admission reserved by this Turn."""
        state = self._active
        if state is not None and turn.child_id is None:
            for step in turn._steps():
                if (
                    state.issued is step
                    or state.handle is step
                    or step._continuation is state.accepted
                ):
                    return state
        return None

    async def _wait_continuations(self, handle: Turn[_TurnT]) -> Completed[_TurnT]:
        root = handle
        while True:
            assert handle._completion is not None
            result = await asyncio.shield(handle._completion)
            successor = handle._continuation
            if successor is None:
                # Native usage is a cumulative thread snapshot, including
                # previous native continuations. Never add snapshots together.
                return result if handle is root else replace(result, turn=root)
            handle = await asyncio.shield(successor)

    async def _wait(self, submission: Turn[_TurnT]) -> Completed[_TurnT]:
        """Join this captured root future or exact native child result."""
        self._check_owner(submission)
        if submission.child_id is not None:
            self._check_callback_operation("wait on a child of its own Session")
            native_error = None
            output, usage = "", "null"
            try:
                output, usage = cast(tuple[str, str], await submission._native.wait())
            except RuntimeError as error:
                native_error = NativeError(str(error))
            for state in self._callbacks_for_child(submission):
                await asyncio.shield(state.completion)
                if state.failure.done():
                    raise state.failure.result()
                if state.cleanup_error is not None:
                    raise NativeError("Child callback cleanup failed") from state.cleanup_error
                if native_error is not None and state.terminal.done():
                    terminal = state.terminal.result()
                    if terminal is not None:
                        if terminal.get("error"):
                            native_error = _native_error(terminal["error"], str(native_error))
                        elif terminal["type"] == "turn_aborted" and (
                            terminal.get("reason") == "interrupted"
                        ):
                            native_error._interrupted = True
            if native_error is not None:
                raise native_error
            return cast(
                Completed[_TurnT],
                Completed(submission, cast(_TurnT, output), _token_usage_info(json.loads(usage))),
            )
        if self._owned_submission(submission) is not None or any(
            state.issued is submission for state, _ in self._pending
        ):
            self._check_callback_operation("wait on its own turn")
        assert submission._completion is not None
        result = submission._result
        if result is None:
            result = _create_task(self._wait_continuations(submission))
            # A cancelled passive waiter need not be present at settlement.
            result.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            object.__setattr__(submission, "_result", result)
        return await asyncio.shield(result)

    async def _wait_for_request_or_result(
        self, submission: Turn[_TurnT]
    ) -> Completed[_TurnT] | PendingRequests[_TurnT]:
        self._check_owner(submission)
        if (
            submission.child_id is None
            and self._owned_submission(submission) is None
            and not any(state.issued is submission for state, _ in self._pending)
        ):
            return await self._wait(submission)
        self._check_callback_operation("wait for requests from its own Session")
        if self._closing or self._closed:
            return await self._wait(submission)

        # This task joins only the captured Turn. Cancelling the private waiter
        # has the same passive semantics as Turn.result, including on child turns.
        completion = _create_task(self._wait(submission))
        try:
            while True:
                signal = self._observations.changed
                if completion.done():
                    return completion.result()
                if submission._result is not None and submission._result.done():
                    return await self._wait(submission)
                seen: set[int] = set()
                requests: list[Interaction] = []
                state = self._owned_submission(submission)
                owner = state.handle if state is not None else None
                owner_id = owner.submission_id if owner is not None else submission.submission_id
                while (request := self._manual_request(owner_id, seen)) is not None:
                    seen.add(request._invocation)
                    requests.append(request)
                if (
                    state is not None
                    and not state.stopping
                    and not state.failure.done()
                    and not state.terminal.done()
                ):
                    requests.extend(
                        cast(Interaction, question)
                        for question in state.questions.values()
                        if question.submission_id == owner_id
                    )
                if requests:
                    owners = (
                        self._callbacks_for_child(submission)
                        if submission.child_id is not None
                        else (() if state is None else (state,))
                    )
                    if any(owner.stopping or owner.failure.done() for owner in owners):
                        return await self._wait(submission)
                    try:
                        # Replies use the same Session admission gate. A seal
                        # can be refused, so wait for it to reopen or complete
                        # this Turn instead of treating refusal as terminal.
                        self._check_open()
                    except RuntimeError:
                        pass
                    else:
                        return PendingRequests(submission, tuple(requests))
                # Capture before reading the registries. The signal is just a
                # wakeup: dropped or replayed observations never carry grants.
                changed = asyncio.create_task(signal.wait())
                try:
                    await asyncio.wait((completion, changed), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
        finally:
            completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)

    async def cancel_all(
        self,
        *,
        mode: Literal["graceful", "force"] = "force",
    ) -> None:
        """Stop and join this whole Session's graph work, callbacks and cells.

        Use only when the caller owns this entire scope. Graceful lets admitted
        work reach its next boundary; a pending manual request still needs a
        response or a force/close escalation. Cancelling this caller does not
        withdraw a stop. Effects outside the Session are not rolled back.
        """
        self._check_open()
        self._check_callback_operation("interrupt its own Session")
        if mode not in ("graceful", "force"):
            raise ValueError("mode must be 'graceful' or 'force'")
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        state = self._active
        if state is not None and state.restored_pending and mode == "graceful":
            raise RuntimeError("Graceful interruption requires a recovered manual turn")
        self._discard_pending(TurnStoppedError("Queued work stopped by cancel_all"))
        accepted: asyncio.Future[None] = self._loop.create_future()
        # Retrieve failures even if the caller is cancelled before acceptance.
        accepted.add_done_callback(lambda result: result.exception())
        pending = asyncio.create_task(self._interrupt_all(mode, state, accepted))
        await self._join_interruption(pending, self._graph_interrupts, accepted)

    async def _cancel_turn(self, turn: Turn[Any], mode: Literal["graceful", "force"]) -> None:
        self._check_owner(turn)
        self._check_callback_operation("interrupt its own Session")
        if turn.child_id is not None:
            raise CapabilityError("Use Subagent.try_cancel for a child Turn")
        if mode not in ("graceful", "force"):
            raise ValueError("mode must be 'graceful' or 'force'")
        for entry in self._pending:
            queued, _ = entry
            if queued.issued is turn:
                self._pending.remove(entry)
                error = TurnStoppedError("Queued work cancelled before admission")
                if queued.tracing is not None:
                    queued.tracing.close(error, cancelled=True)
                queued.accepted.set_exception(error)
                queued.completion.set_exception(error)
                self._observations.notify()
                return
        state = self._owned_submission(turn)
        # A settled or superseded root owns neither the new turn nor its cells.
        if state is None:
            return
        self._check_open()
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        if state.restored_pending and mode == "graceful":
            raise RuntimeError("Graceful interruption requires a recovered manual turn")
        accepted: asyncio.Future[None] = self._loop.create_future()
        accepted.add_done_callback(lambda result: result.exception())
        pending = asyncio.create_task(self._interrupt_root(mode, state, accepted))
        await self._join_interruption(pending, self._root_interrupts, accepted)
        if mode == "graceful" and state.handle is not None:
            # Joined cleanup owns the queue fence, not the captured Turn's
            # ordinary result. Keep that result on this caller and the Turn.
            # An unreceived owned stop has no admitted result to report here.
            if state.failure.done():
                raise state.failure.result()
            completion_error = state.completion.exception()
            if completion_error is not None and not isinstance(
                completion_error, (asyncio.CancelledError, TurnStoppedError)
            ):
                raise completion_error

    async def _join_interruption(
        self,
        pending: asyncio.Task[None],
        owners: set[asyncio.Task[None]],
        accepted: asyncio.Future[None],
    ) -> None:
        # Own the gate before yielding: cleanup must not reach a newer turn.
        owners.add(pending)

        def finished(task: asyncio.Task[None]) -> None:
            error = asyncio.CancelledError() if task.cancelled() else task.exception()
            if not accepted.done():
                if error is None:
                    accepted.set_result(None)
                else:
                    accepted.set_exception(error)
            # Retain failed owners so Session exit reports background failures.
            if error is None:
                owners.discard(task)
                self._promote()
            else:
                self._discard_pending(
                    NativeError("Session interruption failed before queued admission")
                )

        pending.add_done_callback(finished)
        await asyncio.shield(accepted)
        await _join(pending)

    async def _try_cancel_child(self, child: Subagent, turn: Turn[str]) -> bool:
        self._check_owner(turn)
        self._check_open()
        self._check_callback_operation("interrupt a child of its own Session")
        if (
            child._session is not self
            or not child.is_live
            or turn.child_id != child.id
            or turn.instance_id != child._instance_id
            or turn._native.incarnation != child._incarnation
        ):
            raise StaleTurnError("Turn does not belong to this captured child incarnation")
        # Keep cleanup owned when the passive caller is cancelled. Native
        # validates the exact captured turn and joins only its retained cells.
        return cast(bool, await _join(asyncio.create_task(self._cancel_child_and_join(turn))))

    async def _cancel_child_and_join(self, turn: Turn[str]) -> bool:
        confirmed = await self._native._cancel_child_turn(turn._native)
        if confirmed is False:
            return False
        if self._closing:
            # Native released its operation lease before returning. Close may
            # already have swept the owners and stopped delivering child events.
            # Join that cleanup instead of creating an owner after its sweep.
            assert self._close_task is not None
            try:
                await asyncio.shield(self._close_task)
            except BaseException:
                pass
            else:
                if confirmed is True:
                    return True
        else:
            assert turn.child_id is not None and turn.turn_id is not None
            key = (turn.child_id, turn._native.incarnation, turn.turn_id)
            if self._broken and key not in self._child_callbacks:
                # The failed reader already swept its owners. A new owner has
                # no remaining event source; existing owners still need joining.
                raise NativeError("Child cancellation cleanup failed") from None
            state = self._child_callback_state(
                turn.child_id, turn._native.incarnation, turn.turn_id
            )
            # Seal admission before yielding. The native terminal reader may have
            # already done this; cancelling a task twice must not break its cleanup.
            state.stopping = True
            for callback in state.callbacks:
                cancel_once(callback)
            await asyncio.shield(state.completion)
            if state.cleanup_error is None and confirmed is True:
                return True
        # Leave any raw close exception handler before raising, so neither cause
        # nor context exposes private application cleanup errors to this waiter.
        raise NativeError("Child cancellation cleanup failed") from None

    async def _interrupt_root(
        self,
        mode: Literal["graceful", "force"],
        state: _Submission | None,
        accepted: asyncio.Future[None],
    ) -> None:
        errors: list[BaseException] = []
        if state is not None:
            try:
                handle = (
                    self._last_turn
                    if mode == "force"
                    and self._last_turn is not None
                    and self._owned_submission(self._last_turn) is state
                    else await asyncio.shield(state.accepted)
                )
                if mode == "force":
                    await self._cancel(handle, accepted=accepted)
                else:
                    if (
                        self._active is state
                        and not state.stopping
                        and not state.failure.done()
                        and self._last_turn is not None
                        and self._owned_submission(self._last_turn) is state
                    ):
                        # Only the exact captured regular turn may be marked.
                        # Compaction has no regular sample boundary.
                        state.graceful_requested = True
                        await self._native._request_graceful_interrupt(handle.turn_id)
                    accepted.set_result(None)
                    assert state.task is not None
                    await state.task
                    if state.cleanup_error is not None:
                        raise NativeError(
                            "Session interruption failed during owned cleanup"
                        ) from state.cleanup_error
            except BaseException as error:
                if (
                    mode == "graceful"
                    and (state.issued is not None or state.continued_from is not None)
                    and state.handle is None
                    and state.stopping
                    and isinstance(error, TurnStoppedError)
                    and isinstance(error.__cause__, asyncio.CancelledError)
                ):
                    # The captured unreceived root was already stopped and
                    # joined. Still run this owner's native cleanup below.
                    if not accepted.done():
                        accepted.set_result(None)
                else:
                    if not accepted.done():
                        accepted.set_exception(error)
                    errors.append(error)
                    # A failed graceful marker must not sweep cells while its turn
                    # can still start more. Join cancellation of the exact captured
                    # handle first; _cancel never targets a successor.
                    assert state.task is not None
                    if not state.task.done() and state.handle is not None:
                        try:
                            await self._cancel(state.handle)
                        except BaseException as cleanup_error:
                            errors.append(cleanup_error)
        else:
            accepted.set_result(None)
        # Python's broken/closing/sealed flags do not prove host cleanup.
        # Native selects this root's exact Control or confirms its close joined.
        # No root can start until this owner exits and no Op is sent here.
        try:
            await self._native._interrupt_code_mode_cells()
        except BaseException as error:
            self._broken = True
            errors.append(error)
            # Joining Session._aclose here would wait on this same owner.
            # Close just native work; Session exit joins remaining owners.
            await _close_after_error(error, self._native.close)
        finally:
            if self._interception is not None:
                try:
                    await self._interception.join_cell_callbacks()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            if self._broken and not self._closing:
                self._finish_failed_observations()
        if errors:
            # Task cancellation discards its exception context on gather. A
            # cancelled initial admission must not hide a real cleanup failure.
            first = next(
                (error for error in errors if not isinstance(error, asyncio.CancelledError)),
                errors[0],
            )
            for remaining_error in errors:
                if remaining_error is not first:
                    _retain_cleanup_error(
                        first, remaining_error, _error_contexts(first.__context__)
                    )
            raise first

    def _interrupt_all(
        self,
        mode: Literal["graceful", "force"],
        state: _Submission | None,
        accepted: asyncio.Future[None],
    ) -> Coroutine[Any, Any, None]:
        errors: list[BaseException] = []
        previous_children = set(self._child_callbacks)
        admissions = tuple(self._child_admissions)
        children = {key: child for key, child in self._child_callbacks.items() if child.busy}
        for child in self._child_callbacks.values():
            child.stop_continuation()
        native_stop: Awaitable[None] | None = None
        native_error: BaseException | None = None
        if state is None or not state.restored_pending:
            try:
                # Register the native fence before Python yields to manual
                # replies, automatic callbacks or another admitted start.
                native_stop = self._native._interrupt_graph(mode)
                if mode == "force":
                    # Native has revoked these grants. Revoke Python admission
                    # before publishing acceptance, including returning workers.
                    # Leave an unaccepted input to the existing receipt owner.
                    if state is not None and state.handle is not None and not state.stopping:
                        state.stopping = True
                        assert state.task is not None
                        if not state.failure.done():
                            cancel_once(state.task)
                    for child in children.values():
                        child.stopping = True
                        for callback in child.callbacks:
                            cancel_once(callback)
            except BaseException as error:
                native_error = error

        async def settle() -> None:
            admission_stopped = False
            if native_error is not None:
                accepted.set_exception(native_error)
            elif state is None or not state.restored_pending:
                accepted.set_result(None)
            if state is not None:
                try:
                    if (
                        mode == "force"
                        and state.handle is None
                        and not state.restored_pending
                        and self._last_turn is not None
                        and self._owned_submission(self._last_turn) is state
                    ):
                        # The graph fence above owns children; the captured
                        # root owns its own and its continuation's admission.
                        await self._cancel(self._last_turn)
                    else:
                        handle = await asyncio.shield(state.accepted)
                        if state.restored_pending:
                            # Keep unrecovered-checkpoint retirement unchanged.
                            await self._cancel(handle, accepted=accepted)
                            return
                except BaseException as error:
                    if (
                        mode == "graceful"
                        and (state.issued is not None or state.continued_from is not None)
                        and state.handle is None
                        and state.stopping
                        and isinstance(error, TurnStoppedError)
                        and isinstance(error.__cause__, asyncio.CancelledError)
                    ):
                        # _execute chose this owned cancellation before joining
                        # retirement. A reader ended by that retirement must
                        # not replace the completed admission outcome.
                        admission_stopped = True
                        if not accepted.done():
                            accepted.set_result(None)
                    else:
                        if not accepted.done():
                            accepted.set_exception(error)
                        errors.append(error)
                if mode == "graceful":
                    state.graceful_requested = True
            native_failed = False
            try:
                try:
                    if native_error is not None:
                        raise native_error
                    await (
                        native_stop
                        if native_stop is not None
                        else self._native._interrupt_graph(mode)
                    )
                finally:
                    # Even a failed bridge cannot retire an accepted input
                    # before its receipt transfers to the exact child owner.
                    if admissions:
                        await asyncio.gather(*admissions, return_exceptions=True)
                if admissions:
                    # Explicit child input can acquire native admission after
                    # the first successful sweep. Stop any such new work.
                    await self._native._interrupt_graph(mode)
            except BaseException as error:
                native_failed = True
                errors.append(error)
                self._broken = True
                try:
                    await self._native.close()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            # Native joined the graph stop (or its failure-close). Join the
            # actual root-cell callbacks too, including already-settled roots.
            if self._interception is not None:
                try:
                    await self._interception.join_cell_callbacks()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            # Capture owners born during native settlement, including owners whose
            # callbacks failed before the stop returned. Historical failures do not
            # belong to this interruption.
            children.update(
                (key, child)
                for key, child in self._child_callbacks.items()
                if key not in previous_children or child.busy
            )
            if mode == "force" or native_failed:
                if state is not None and not state.stopping:
                    assert state.task is not None
                    state.stopping = True
                    cancel_once(state.task)
                for child in children.values():
                    child.stopping = True
                    for callback in child.callbacks:
                        cancel_once(callback)
                    if native_failed and not child.terminal.done():
                        # Native retirement failed or closed the source. Retire the
                        # Python owner too, while retaining the interruption error.
                        child.terminal.set_result(None)
            if state is not None:
                assert state.task is not None
                try:
                    await state.task
                except BaseException as error:
                    errors.append(error)
                if state.cleanup_error is not None:
                    errors.append(state.cleanup_error)
                if mode == "graceful":
                    if state.failure.done() and not admission_stopped:
                        errors.append(state.failure.result())
                    if state.completion.done() and not state.completion.cancelled():
                        completion_error = state.completion.exception()
                        if completion_error is not None and not isinstance(
                            completion_error, (asyncio.CancelledError, TurnStoppedError)
                        ):
                            errors.append(completion_error)
            await asyncio.gather(*(asyncio.shield(child.completion) for child in children.values()))
            continuation_results = await asyncio.gather(
                *(
                    child.continuation
                    for child in children.values()
                    if child.continuation is not None
                ),
                return_exceptions=True,
            )
            errors.extend(
                result for result in continuation_results if isinstance(result, BaseException)
            )
            for child in children.values():
                if child.cleanup_error is not None:
                    errors.append(child.cleanup_error)
                if mode == "graceful" and child.failure.done():
                    errors.append(child.failure.result())
            if errors:
                first = next(
                    (error for error in errors if not isinstance(error, asyncio.CancelledError)),
                    errors[0],
                )
                for remaining_error in errors:
                    if remaining_error is not first:
                        _retain_cleanup_error(
                            first, remaining_error, _error_contexts(first.__context__)
                        )
                raise first

        return settle()

    async def _cancel(
        self, submission: Turn[Any], *, accepted: asyncio.Future[None] | None = None
    ) -> None:
        """Stop the root turn owned by this Session and join callback cleanup."""
        self._check_callback_operation("interrupt its own Session")
        self._check_owner(submission)
        state = self._owned_submission(submission)
        if state is None:
            if accepted is not None:
                accepted.set_result(None)
            return
        assert state.task is not None
        should_cancel = not state.stopping and not state.failure.done()
        if should_cancel and state.handle is not None:
            # Callback cleanup can finish before Core handles the interrupt.
            # The public root may now own a later native continuation.
            assert state.handle.turn_id is not None
            self._native._request_sampling_stop(state.handle.turn_id)
        # A final manual reply can clear restored_pending while native recovery
        # is in flight. Keep ownership of the terminal retirement already chosen.
        state.retire_manual = state.retire_manual or state.restored_pending
        state.stopping = True
        if should_cancel:
            cancel_once(state.task)
        if accepted is not None:
            accepted.set_result(None)

        async def finish_cancellation() -> None:
            assert state.task is not None
            await state.task
            if state.cleanup_error is not None:
                raise NativeError(
                    "Session cancellation failed during owned cleanup"
                ) from state.cleanup_error
            if self._broken and state.handle is None and not state.completion.cancelled():
                await asyncio.wait((state.completion,))
                error = state.completion.exception()
                if error is not None and not isinstance(
                    error, (asyncio.CancelledError, TurnStoppedError)
                ):
                    # Unknown publication must also fail interruption and fence
                    # the queue. A known refusal remains on the issued Turn's
                    # result; it did not invalidate other callers' queued work.
                    raise error

        await _join(asyncio.create_task(finish_cancellation()))

    async def _execute(
        self, state: _Submission, input: list[dict[str, Any]] | None, *, admitted: bool = False
    ) -> None:
        outcome = None
        error = None
        confirmed_rejection = False
        delegate_close = False
        usage_request: asyncio.Future[Any] | None = None
        try:
            if admitted:
                handle = state.handle
                assert handle is not None
            else:
                if input is not None and state.continued_from is None:
                    assert self._opening_agent is not None
                    opening = self._opening_agent.tools
                    available = opening
                    _, allowed_mcp, _ = _split_tools(available)
                    selected = self._bound_tools
                    if self._native_agents is not None and state.operation_slots is not None:
                        selected = tuple(self._bound_tools[slot] for slot in state.operation_slots)
                    if state.operation_mcp is not None and (
                        bool(allowed_mcp)
                        and state.operation_mcp != self._mcp_tools
                        or selected != self._catalog.tools
                    ):
                        # Children inherit the confirmed generation. Root-only
                        # step slots cannot restrict an independently owned child.
                        candidate = _ToolCatalog(self._catalog.generation + 1, selected)
                        publication = asyncio.create_task(
                            self._replace_tools(
                                candidate,
                                state.operation_mcp,
                                allowed_mcp,
                                candidate.native_specs(),
                            )
                        )
                        try:
                            await _join(publication)
                        except asyncio.CancelledError:
                            # No input was submitted: finish this catalog receipt
                            # before releasing the root or retiring its resources.
                            confirmed_rejection = not self._broken
                            if not publication.cancelled():
                                publication.result()
                            raise
                        except (ValueError, RuntimeError):
                            # Publication fences every uncertain receipt. Its
                            # unfenced rejection happened before any mutation;
                            # do not retire unrelated live children for it.
                            confirmed_rejection = not self._broken
                            raise
                    if self._native_agents is not None and state.operation_slots is not None:
                        state.operation_slots = tuple(range(len(selected)))
                self._native._select_steps(
                    input is not None
                    and (state.before_step is not None or state.operation_slots is not None),
                    _native_tool_refs(state.operation_refs)
                    if state.operation_refs is not None
                    else None,
                    self._native_agents is not None and self._native_agents.before_step is not None,
                )
                if input is None:
                    tracing_args = {}
                    if state.parent_trace is not None:
                        tracing_args["trace_json"] = json.dumps(state.parent_trace.to_headers())
                    receipt = json.loads(
                        await self._native._compact(state.tracing.context, **tracing_args)
                        if state.tracing is not None and state.tracing.context is not None
                        else await self._native._compact(**tracing_args)
                    )
                else:
                    if self._environment_lease is not None and self._environment_lease.interrupted:
                        # Publication has settled; neither root nor continuation issued input.
                        confirmed_rejection = True
                        raise NativeError("Session ended before queued work was admitted")
                    request: dict[str, Any] = {
                        "mode": "continue_if_idle" if state.continued_from else "start_if_idle",
                        "input": input,
                    }
                    if state.parent_trace is not None:
                        request["trace"] = state.parent_trace.to_headers()
                    if state.issued is None:
                        state.issued = _root_turn(self, None, uuid4().hex, state.completion)
                    route_id = self._register_request_observation(state, state.issued)
                    request["observation_submission_id"] = route_id
                    if state.tracing is not None and state.tracing.context is not None:
                        request["tracing_context"] = state.tracing.context
                    if state.continued_from is not None:
                        request["expected_previous_turn_id"] = state.continued_from
                    if state.output.mode == "native":
                        request["start"] = {"final_output_json_schema": state.output.schema}
                    if state.writable_roots is not None:
                        request["writable_roots"] = state.writable_roots
                    receipt = json.loads(await self._native._submit(json.dumps(request)))
                if receipt["status"] != "started":
                    confirmed_rejection = receipt["status"] == "not_submitted"
                    reason = receipt["reason"]
                    problem = NativeError(
                        reason.get("message") or f"Native input was not submitted: {reason['type']}"
                    )
                    if confirmed_rejection:
                        problem._effects = "not_started"
                    raise problem
                if state.writable_roots is not None:
                    self._writable_roots = state.writable_roots
                if (
                    input is not None
                    and self._environment_lease is not None
                    and self._environment_lease.interrupted
                ):
                    # Input was admitted, so retire native ownership as uncertain.
                    # Do not publish acceptance to readers which dispatch callbacks.
                    raise NativeError("Session ended during native admission")
                handle = state.issued
                if handle is None:
                    handle = _root_turn(self, receipt["turn_id"], uuid4().hex, state.completion)
                else:
                    assert handle.turn_id is None
                    object.__setattr__(handle, "turn_id", receipt["turn_id"])
                if input is not None and state.continued_from is None:
                    self._last_turn = handle
                state.handle = handle
                assert handle.turn_id is not None
                self._observations.append(
                    TurnStarted(
                        session_id=self.id,
                        turn_id=handle.turn_id,
                        submission_id=handle.submission_id,
                    )
                )
                state.accepted.set_result(handle)
            await asyncio.wait((state.terminal, state.failure), return_when=asyncio.FIRST_COMPLETED)
            if state.failure.done():
                raise state.failure.result()
            terminal = state.terminal.result()
            if terminal["type"] == "turn_aborted":
                raise asyncio.CancelledError()
            if terminal.get("error"):
                raise _native_error(terminal["error"])
            if input is not None:
                # Terminal fixes this turn's native usage. Start the read while
                # draining callbacks: newly admitted work must not block the
                # final read on which it may itself depend.
                usage_request = asyncio.ensure_future(self._native.call("token_usage_info"))
            await state.wait_for_callbacks()
            if input is not None:
                text = (
                    ""
                    if state.last_assistant_had_blank_final
                    else terminal.get("last_agent_message") or ""
                )
                # Preserve single validation and its established error ordering
                # unless correction is explicitly enabled. A correction can
                # start only after all original callbacks and usage have settled.
                if not state.unsampled_steering and not state.output.max_retries:
                    output = self._validate_traced_output(state, text)
                assert usage_request is not None
                await asyncio.wait(
                    (usage_request, state.failure), return_when=asyncio.FIRST_COMPLETED
                )
                # Native terminal does not seal callbacks on still-owned cells.
                # Drain work admitted during usage before committing an outcome.
                await state.wait_for_callbacks()
                usage = _token_usage_info(json.loads(usage_request.result()))
                if not state.unsampled_steering and state.output.max_retries:
                    if state.cleanup_error is not None:
                        raise state.cleanup_error
                    invalid = False
                    try:
                        output = self._validate_traced_output(state, text)
                    except OutputValidationError:
                        invalid = True
                    if invalid:
                        # Leave the validation exception context before running
                        # other work or surfacing a different native failure.
                        state.repairing = True
                        output = await self._repair_output(state, text)
                        # A yielded original cell may finish a tool while the
                        # separate correction was running. It still belongs to
                        # this Turn; neither skip it nor hide its failure behind
                        # a valid correction.
                        await state.wait_for_callbacks()
                        if state.cleanup_error is not None:
                            raise state.cleanup_error
                if not state.unsampled_steering:
                    outcome = Completed(
                        handle,
                        output,
                        usage,
                        repair_attempts=tuple(
                            deepcopy(t)
                            for t in self._repair_turns
                            if "parent_turn_id" in t and t["parent_turn_id"] == handle.turn_id
                        ),
                    )
        except BaseException as caught:  # all exits settle owned work
            error = caught
        finally:
            state.stopping = True
            # A pending selector blocks Core before it can settle an interrupt.
            # Notify callbacks now; retain this owner until their exit below.
            for callback in state.callbacks:
                cancel_once(callback)
            try:
                try:
                    if (
                        state.policy_stop
                        or state.retire_manual
                        or (not state.terminal.done() and not confirmed_rejection)
                    ):
                        if (
                            state.retire_manual
                            or state.restored_pending
                            or state.handle is None
                            or self._reader.done()
                        ):
                            self._broken = True
                            if state.retire_manual or state.restored_pending:
                                await self._native._prepare_manual_retirement()
                            if (
                                (state.retire_manual or state.restored_pending)
                                and self._executor is not None
                                and not self._executor_termination_attempted
                            ):
                                self._executor_termination_attempted = True
                                await self._native._terminate_executor_processes()
                            # Session close owns an ordinary uncertain root. It
                            # joins its reserved graph stop before shutting down
                            # the thread and still reports cleanup failures.
                            delegate_close = (
                                self._closing
                                and state.handle is None
                                and not state.retire_manual
                                and not state.restored_pending
                                and not self._reader.done()
                            )
                            if not delegate_close:
                                await self._native.close()
                            if state.retire_manual:
                                # Native close ends the graph event source, so
                                # this reader joins every possible child birth.
                                await self._reader
                        else:
                            # This controller is the sole input producer. Hold its
                            # active slot through native settlement and callback exit.
                            if input is None:
                                # Only this owner can hold a queued compaction.
                                await self._native._interrupt_session(
                                    state.handle.turn_id, queued_compaction=True
                                )
                            else:
                                await self._native._interrupt_session(state.handle.turn_id)
                            await asyncio.wait(
                                (state.terminal, self._reader),
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if not state.terminal.done():
                                self._broken = True
                                await self._native.close()
                        if state.policy_stop and not self._broken:
                            # A yielded cell can outlive its turn terminal.
                            # Keep this active slot until native root cells join.
                            await self._native._interrupt_code_mode_cells(stopped_only=True)
                    elif (
                        state.graceful_requested
                        and state.handle is not None
                        and state.terminal.done()
                        and state.terminal.result()["type"] == "turn_aborted"
                    ):
                        # TurnAborted precedes Core's final abort cleanup. Keep
                        # this active slot until its exact named receipt is idle.
                        await self._native._finish_graceful_interrupt(state.handle.turn_id)
                except BaseException as interruption_error:
                    self._broken = True
                    await _close_after_error(interruption_error, self._native.close)
                    raise
                finally:
                    try:
                        if usage_request is not None:
                            # Retain native-read ownership even on interruption
                            # failure. The stop fences first; this join prevents
                            # another turn from entering before the read retires.
                            await _join(usage_request)
                    finally:
                        for callback in state.callbacks:
                            cancel_once(callback)
                        await asyncio.gather(*state.callbacks, return_exceptions=True)
                        if (
                            self._interception is not None
                            and state.handle is not None
                            and (error is not None or state.policy_stop or state.retire_manual)
                        ):
                            await self._interception.join_cell_callbacks(state.handle)
                        if state.retire_manual:
                            for child in self._child_callbacks.values():
                                child.stopping = True
                                for callback in child.callbacks:
                                    cancel_once(callback)
                                if not child.terminal.done():
                                    child.terminal.set_result(None)
                            await asyncio.gather(
                                *(child.completion for child in self._child_callbacks.values())
                            )
                            for child in self._child_callbacks.values():
                                if child.cleanup_error is not None:
                                    state.retain_cleanup_error(child.cleanup_error)
            except BaseException as cleanup_error:  # preserve ownership failure
                self._broken = True
                state.retain_cleanup_error(cleanup_error)
                if error is None:
                    error = cleanup_error
            finally:
                if delegate_close:
                    # The execution task can finish so Session close can join
                    # the graph and thread. Consumers wait for their outcome.
                    state.deferred_close = (input is not None, error, outcome)
                else:
                    self._settle_submission(state, input is not None, error, outcome)

    @staticmethod
    def _retire_questions(state: _CallbackState) -> None:
        for question in state.questions.values():
            question._release_tracing()
        state.questions.clear()

    def _settle_submission(
        self,
        state: _Submission,
        has_input: bool,
        error: BaseException | None,
        outcome: Completed[Any] | None,
    ) -> None:
        continuation: _Submission | None = None
        if state.cleanup_error is not None:
            if error is None:
                error = state.cleanup_error
            else:
                _retain_cleanup_error(
                    error,
                    state.cleanup_error,
                    _error_contexts(error.__context__),
                )
        if error is None and has_input and state.unsampled_steering:
            if (
                not self._closing
                and not self._broken
                and not state.graceful_requested
                and not self._graph_interrupts
                and not self._root_interrupts
            ):
                assert state.handle is not None
                continuation = _Submission(
                    state.output,
                    continued_from=state.handle.turn_id,
                    before_step=state.before_step,
                    step_index=state.step_index,
                    operation_slots=state.operation_slots,
                    operation_mcp=state.operation_mcp,
                    operation_refs=state.operation_refs,
                    parent_trace=state.parent_trace,
                    tracing=state.tracing,
                )
                state.tracing = None
                object.__setattr__(state.handle, "_continuation", continuation.accepted)
                self._active = continuation
            else:
                error = NativeError("Accepted steering input remains in history after interruption")
        if state.tracing is not None:
            state.tracing.close(
                error,
                cancelled=isinstance(error, TurnStoppedError),
                category="validation_error"
                if isinstance(error, OutputValidationError)
                else "provider_error"
                if isinstance(error, NativeError)
                else None,
            )
            state.tracing = None
        self._retire_questions(state)
        state.manual_inputs.clear()
        if self._active is state:
            # Failed admission/publication may have retired the native
            # thread already. Always settle the accepted/completion
            # futures with the original error before releasing its slot.
            if not self._broken:
                self._native._select_steps(False)
            self._active = None
        # Issued roots also own their unreceived continuations.
        # A raw CancelledError as a shared result loses its cleanup
        # context and looks like cancellation of an admission or result
        # reader. Settle both with the same owned outcome.
        if (
            state.issued is not None or state.handle is not None or state.continued_from is not None
        ) and (
            isinstance(error, asyncio.CancelledError)
            or state.policy_stop == "requested"
            and state.failure.done()
            and error is state.failure.result()
        ):
            if state.cleanup_error is None:
                stopped = TurnStoppedError(
                    "Turn stopped by policy" if state.policy_stop else "Turn stopped"
                )
                # Distinguish an actual owned cancellation from a
                # callback raising TurnStoppedError itself.
                stopped.__cause__ = error
                error = stopped
            else:
                failure = NativeError("Turn cleanup failed")
                failure.__cause__ = error
                error = failure
        if not state.accepted.done():
            if isinstance(error, asyncio.CancelledError):
                state.accepted.cancel()
            else:
                state.accepted.set_exception(
                    error if error is not None else NativeError("Native input was not accepted")
                )
        if state.handle is not None:
            assert state.handle.turn_id is not None
            self._observations.append(
                TurnFinished(
                    session_id=self.id,
                    turn_id=state.handle.turn_id,
                    submission_id=state.handle.submission_id,
                    status="cancelled"
                    if isinstance(error, (asyncio.CancelledError, TurnStoppedError))
                    else "failed"
                    if error is not None
                    else "completed",
                    error_type=type(error).__name__ if error is not None else None,
                )
            )
        if error is not None:
            state.completion.set_exception(error)
        else:
            state.completion.set_result(outcome)
        if state.handle is None:
            # No native TurnFinished exists for refused admission. Readers
            # must recheck the settled owned continuation without a new event.
            self._observations.notify()
        if continuation is not None:
            self._start_execution(continuation, [])
        self._finish_failed_observations()
        self._promote()

    def _validate_traced_output(self, state: _Submission, text: str) -> Any:
        context = state.tracing.context if state.tracing is not None else None
        with _owned_span("output validation", "validation", context):
            return state.output.validate(text)

    async def _repair_output(self, state: _Submission, text: str) -> Any:
        """Correct only invalid final text under the still-active root owner."""
        handle = state.handle
        assert handle is not None and handle.turn_id is not None and self._opening_agent is not None
        provider = self._opening_agent.provider or self._owned_provider
        assert provider is not None
        assert self._model_api_key is not None or self._codex_login_binding is not None
        history = json.loads(await self._native.read_thread_history(None))
        captured = None
        for row in reversed(history):
            payload = row.get("payload")
            if (
                row.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "thread_settings_applied"
                and payload.get("thread_id") == self.id
            ):
                captured = payload.get("thread_settings")
                break
        if not isinstance(captured, dict) or not isinstance(captured.get("model"), str):
            raise NativeError("Native history does not contain accepted model settings for repair")
        settings = cast(
            ModelSettings,
            {
                key: captured[key]
                for key in ("reasoning_effort", "reasoning_summary", "service_tier")
                if captured.get(key) is not None
            },
        )
        declaration = Agent(
            instructions="Correct the supplied JSON to match its schema. Return only JSON.",
            provider=provider,
            model=captured["model"],
            model_settings=settings,
        )
        for _ in range(state.output.max_retries):
            # The original native turn has already settled. Its owner can
            # still request a graceful stop before another correction starts.
            if state.graceful_requested:
                raise asyncio.CancelledError()
            if state.failure.done():
                raise state.failure.result()
            repair = await Session._open(
                declaration,
                _captured_api_key=self._model_api_key,
                _expected_codex_login_binding=self._codex_login_binding,
                parent_trace=state.parent_trace,
            )
            repair._trace_context = state.tracing.context if state.tracing is not None else None
            repair._observations.repair_parent = (self._observations, handle.submission_id)
            repair._rate_limit_parent = self
            try:
                if state.graceful_requested:
                    raise asyncio.CancelledError()
                if state.failure.done():
                    raise state.failure.result()
                # No repair work can have started before this local check.
                # _start reaches its identical check without yielding.
                repair._check_open()
                rejected = False
                try:
                    try:
                        attempt = await repair._start(
                            _native_input(
                                json.dumps({"schema": state.output.schema, "invalid_output": text})
                            ),
                            _prepare_spec(str),
                        )
                    except NativeError as error:
                        rejected = error._effects == "not_started"
                        raise
                    repair_result = asyncio.create_task(attempt.get_result())
                    try:
                        await asyncio.wait(
                            (repair_result, state.failure), return_when=asyncio.FIRST_COMPLETED
                        )
                        if state.failure.done():
                            raise state.failure.result()
                        result = repair_result.result()
                    except BaseException as error:
                        # Waiting is passive. This producer, not the waiter,
                        # owns cancellation and joins only its exact attempt.
                        await _close_after_error(error, attempt.cancel)
                        raise
                    finally:
                        cancel_once(repair_result)
                        await asyncio.gather(repair_result, return_exceptions=True)
                finally:
                    if not rejected:
                        if repair._broken:
                            self._repair_receipts_incomplete = True
                        else:
                            try:
                                recorded = json.loads(
                                    await repair._native.read_thread_history(None)
                                )
                                turns, items = project_history(recorded, repair.id)
                                for turn in turns:
                                    turn["parent_session_id"] = self.id
                                    turn["parent_turn_id"] = handle.turn_id
                                self._repair_turns.extend(turns)
                                self._repair_items.extend(items)
                            except BaseException:
                                # Preserve uncertainty even if a later repair succeeds.
                                self._repair_receipts_incomplete = True
                                raise
            except BaseException as error:
                await _close_after_error(error, repair._exit_scope)
                raise
            else:
                await repair._exit_scope()
            text = result.final_output
            try:
                return self._validate_traced_output(state, text)
            except OutputValidationError:
                pass
        # Never expose validator inputs through an attached exception context.
        raise OutputValidationError(text)

    def _finish_failed_observations(self) -> None:
        """Close a failed stream after every already-known owner has settled."""
        if (
            self._broken
            and not self._closing
            and self._sealed_checkpoint is None
            and (self._active is None or self._active.completion.done())
            and not self._child_admissions
            and all(not child.busy for child in self._child_callbacks.values())
        ):
            self._observations.finish("failed")

    def _register_request_observation(self, state: _Submission, handle: Turn[Any]) -> str:
        """Capture a fresh, nonpersistent route for this exact root handle."""
        route_id = handle.submission_id
        self._request_observation_routes[route_id] = state
        state.completion.add_done_callback(
            lambda _done: self._request_observation_routes.pop(route_id, None)
        )
        return route_id

    def _observe_request_metadata(self, event: dict[str, Any]) -> None:
        """Route passive metadata only through its original submitted handle."""
        message = event["msg"]
        route_id = message.get("submission_id")
        route = (
            self._request_observation_routes.get(route_id) if isinstance(route_id, str) else None
        )
        if route is not None and not route.completion.done() and not route.stopping:
            observation = _request_observation(
                message.get("observation"), session_id=self.id, submission_id=route_id
            )
            captured = route.handle or route.issued
            if (
                observation is not None
                and captured is not None
                and observation.thread_id == self.id
                and observation.turn_id == event.get("id")
                and captured.turn_id in (None, observation.turn_id)
            ):
                self._observations.append(observation)

    def _observe_progress(
        self, event: dict[str, Any], handle: Turn[Any] | ChildInvocation, thread_id: str
    ) -> None:
        assert handle.turn_id is not None
        message = event["msg"]
        kind = message["type"]
        if kind == "token_count" and event["id"] == handle.turn_id:
            if "api_rate_limits" in message:
                snapshot = _api_rate_limits(message["api_rate_limits"])
                if snapshot is not None:
                    self._observations.append(
                        ApiRateLimitsUpdated(
                            session_id=self.id,
                            turn_id=handle.turn_id,
                            submission_id=handle.submission_id,
                            snapshot=snapshot,
                        )
                    )
                if message.get("info") is None and message.get("rate_limits") is None:
                    return
            self._observations.append(
                UsageUpdated(
                    session_id=self.id,
                    turn_id=handle.turn_id,
                    submission_id=handle.submission_id,
                    usage=_token_usage_info(message.get("info")),
                )
            )
        elif message.get("thread_id") == thread_id and message.get("turn_id") == handle.turn_id:
            if kind == "item_completed" and self._observe_execution(
                message,
                thread_id=thread_id,
                turn_id=handle.turn_id,
                submission_id=handle.submission_id,
            ):
                return
            elif kind == "agent_message_content_delta":
                self._observations.append(
                    OutputTextDelta(
                        session_id=self.id,
                        turn_id=handle.turn_id,
                        submission_id=handle.submission_id,
                        item_id=message["item_id"],
                        delta=message["delta"],
                    )
                )
            elif kind == "item_completed" and message["item"]["type"] == "ContextCompaction":
                self._observations.append(
                    ContextCompacted(
                        session_id=self.id,
                        turn_id=handle.turn_id,
                        submission_id=handle.submission_id,
                        item_id=message["item"]["id"],
                    )
                )

    def _observe_execution(
        self, message: dict[str, Any], *, thread_id: str, turn_id: str, submission_id: str | None
    ) -> bool:
        item = project_execution_completion(message, thread_id=thread_id, turn_id=turn_id)
        if item is None:
            return False
        if submission_id is None:
            # Canonical history still has the completion after the stream loses its owner.
            self._observations._mark_gap()
            return True
        self._observations.append(
            ExecutionItemCompleted(
                session_id=self.id,
                turn_id=turn_id,
                submission_id=submission_id,
                item=item,
            )
        )
        return True

    def _observe_mcp(self, event: dict[str, Any], state: _CallbackState) -> None:
        handle = state.handle
        assert handle is not None
        message = event["msg"]
        kind = message["type"]
        if (
            kind not in {"mcp_tool_call_begin", "mcp_tool_call_end"}
            or message.get("turn_id") != handle.turn_id
            or (event.get("subagent_id"), event.get("subagent_incarnation"))
            != (
                (state.handle.child_id, state.handle.instance_id)
                if isinstance(state, _ChildCallbacks)
                else (None, None)
            )
        ):
            return
        assert handle.turn_id is not None
        invocation = message["invocation"]
        for server in self._mcp_tools:
            if server.name != invocation["server"] or invocation["tool"] not in server.tools:
                continue
            selected = _tool_names((server,))[server.tools.index(invocation["tool"])]
            if kind == "mcp_tool_call_begin":
                if isinstance(state, _Submission):
                    state.mcp_calls[message["call_id"]] = server.name
                self._observations.append(
                    ToolStarted(
                        session_id=self.id,
                        submission_id=handle.submission_id,
                        turn_id=handle.turn_id,
                        call_id=message["call_id"],
                        name=selected["name"],
                        namespace=selected["namespace"],
                        family="mcp",
                    )
                )
            else:
                if isinstance(state, _Submission):
                    state.mcp_calls.pop(message["call_id"], None)
                result = message["result"].get("Ok")
                self._observations.append(
                    ToolFinished(
                        session_id=self.id,
                        submission_id=handle.submission_id,
                        turn_id=handle.turn_id,
                        call_id=message["call_id"],
                        name=selected["name"],
                        namespace=selected["namespace"],
                        family="mcp",
                        status=(
                            "completed"
                            if isinstance(result, dict) and result.get("isError") is not True
                            else "failed"
                        ),
                    )
                )
            return

    def _child_callback_state(
        self,
        child_id: str,
        instance: str,
        turn_id: str,
        request: dict[str, Any] | None = None,
    ) -> _ChildCallbacks:
        if self._native_agents is None or not child_id or not instance or not turn_id:
            raise NativeError("Native child callback has no declared owner")
        key = (child_id, instance, turn_id)
        state = self._child_callbacks.get(key)
        if state is None:
            state = _ChildCallbacks(ChildInvocation(self.id, child_id, instance, turn_id))
            self._child_callbacks[key] = state
            self._observations.append(
                TurnStarted(
                    session_id=self.id,
                    turn_id=turn_id,
                    submission_id=state.handle.submission_id,
                )
            )
            state.task = _create_task(self._settle_child_callbacks(state))
        if request is not None and not state.stopping and not state.completion.done():
            # Retain every native identity until settlement, without retaining
            # tool arguments or results. A later completed call cannot stand in
            # for an earlier call that still owns a pending native response.
            fields = (
                ("nonce", "thread_id", "turn_id", "call_id")
                if "nonce" in request
                else ("_sdk_python_tool", "turnId", "callId")
            )
            state.request_keys.append(json.dumps({name: request[name] for name in fields}))
        return state

    async def _stop_child_callback_owner(self, state: _ChildCallbacks) -> None:
        state.stopping = True
        if self._closing:
            return
        try:
            stopped = False
            for request in state.request_keys:
                # Pending input can prevent interruption. Retire every original
                # response owner even when Core must preserve that queued input.
                stopped = await self._native._interrupt_child_callback(request) or stopped
            # Interrupting a live callback can precede the separately delivered
            # incarnation receipt. A policy stop still needs it to retire cells.
            if (
                state.native_instance is None
                and not self._broken
                and (not stopped or state.policy_stop)
            ):
                await asyncio.wait(
                    (state.observed, state.terminal), return_when=asyncio.FIRST_COMPLETED
                )
            if self._closing or stopped or state.terminal.done():
                return
            native = await self._native._child_turn(state.handle.child_id)
            if (
                native is not None
                and native.turn_id == state.handle.turn_id
                and native.instance_id == state.native_instance
            ):
                if await self._native._cancel_child_turn(native) is None:
                    raise NativeError("Child cancellation cleanup failed")
        finally:
            if state.policy_stop and not self._closing:
                if state.native_instance is None:
                    raise NativeError("Stopped child has no native incarnation receipt")
                # A native terminal does not end yielded cells. Retire only
                # this stopped turn, even when a successor has already started.
                await self._native._interrupt_code_mode_cells(
                    stopped_only=True,
                    child=(state.handle.child_id, state.native_instance, state.handle.turn_id),
                )

    async def _settle_child_callbacks(self, state: _ChildCallbacks) -> None:
        try:
            await asyncio.wait((state.terminal, state.failure), return_when=asyncio.FIRST_COMPLETED)
            self._retire_questions(state)
            self._observations.notify()
            if state.failure.done():
                await self._stop_child_callback_owner(state)
                for task in state.callbacks:
                    cancel_once(task)
            # Background native cells may publish another Environment callback
            # while an admitted function hook finishes after turn completion.
            # Keep joining newly registered owners before sealing admission.
            while pending := tuple(task for task in state.callbacks if not task.done()):
                joined = asyncio.gather(*pending, return_exceptions=True)
                if not state.failure.done():
                    await asyncio.wait((joined, state.failure), return_when=asyncio.FIRST_COMPLETED)
                    if state.failure.done():
                        await self._stop_child_callback_owner(state)
                        for task in state.callbacks:
                            cancel_once(task)
                await joined
            # Rejection can finish a native child before its conditional
            # interrupt reaches Python. Join the accepted decision receipt
            # before reporting the child settled or releasing Session close.
            # No await separates the last empty check from closing admission.
            state.stopping = True
        except BaseException as error:
            if state.policy_stop:
                # No failed stopped-cell retirement proves its effects ended.
                # Fence new work before releasing the child settlement owner;
                # Session close still owns graph and sibling cleanup.
                self._broken = True
            state.retain_cleanup_error(error)
            state.stopping = True
            for task in state.callbacks:
                cancel_once(task)
            await asyncio.gather(*state.callbacks, return_exceptions=True)
        finally:
            state.stopping = True
            state.request_keys.clear()
            state.callbacks.clear()
            state.manual_inputs.clear()
            if not state.completion.done():
                terminal = state.terminal.result() if state.terminal.done() else None
                failure = state.failure.result() if state.failure.done() else state.cleanup_error
                if state.policy_stop and failure is not None and state.cleanup_error is not None:
                    _retain_cleanup_error(
                        failure, state.cleanup_error, _error_contexts(failure.__context__)
                    )
                status: Literal["completed", "failed", "cancelled"]
                error_type = None
                if failure is not None:
                    status, error_type = "failed", type(failure).__name__
                elif terminal is None or terminal["type"] in {"turn_aborted", "shutdown_complete"}:
                    status, error_type = "cancelled", "CancelledError"
                elif terminal.get("_sdk_child_turn_failed") is False:
                    status = "completed"
                else:
                    # Only the exact native terminal receipt can authorize a
                    # successful result; unresolved native tools may fail even
                    # when Core's message has no error.
                    status, error_type = "failed", "NativeError"
                self._observations.append(
                    TurnFinished(
                        session_id=self.id,
                        turn_id=state.handle.turn_id,
                        submission_id=state.handle.submission_id,
                        status=status,
                        error_type=error_type,
                    )
                )
                state.completion.set_result(None)
            self._finish_failed_observations()

    async def _expire_child_question(
        self, state: _ChildCallbacks, question: _NativeQuestion
    ) -> None:
        # This worker belongs to the existing child callback owner. Native state
        # remains authoritative if expiry races a response or this observation.
        completion = question._url_completion if isinstance(question, ElicitationRequest) else None
        try:
            while (
                (
                    state.questions.get(id(question)) is question
                    or completion is not None
                    and not completion.done()
                )
                and not state.stopping
                and not state.terminal.done()
                and not self._closing
            ):
                if completion is not None and not completion.done():
                    assert isinstance(question, ElicitationRequest)
                    status = await self._probe_child_url_completion(question)
                    if status is not None:
                        return
                elif not await self._native._child_question_pending(
                    question._message["_sdk_question_id"]
                ):
                    return
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if not self._closing and not state.stopping and not state.failure.done():
                state.failure.set_result(error)
        finally:
            state.questions.pop(id(question), None)
            question._release_tracing()
            if completion is not None and not completion.done():
                assert isinstance(question, ElicitationRequest)
                await self._probe_child_url_completion(question, retire=True)
            self._observations.notify()

    async def _read_child_event(self, event: dict[str, Any]) -> None:
        if self._closing:
            return
        message = event["msg"]
        kind = message["type"]
        incarnation = event.get("subagent_incarnation")
        turn_id = message.get("turn_id", message.get("turnId"))
        if kind in {"task_complete", "turn_complete", "turn_aborted", "token_count"}:
            turn_id = turn_id or event["id"]
        if not turn_id and kind == "elicitation_request":
            # Only the Core token is owned. A same-server call does not
            # identify the originating turn or authorize its retirement.
            # Session.close joins this reader-owned negative enqueue.
            await self._native.respond_to_subagent(
                event["subagent_id"],
                event["subagent_instance_id"],
                "resolve_elicitation",
                json.dumps(
                    {
                        "server_name": message["server_name"],
                        "request_id": message["id"],
                        "decision": "decline",
                        "content": None,
                        "meta": None,
                    }
                ),
            )
            return
        if incarnation is None or kind == "shutdown_complete":
            for state in self._child_callbacks.values():
                if state.handle.child_id == event["subagent_id"] and (
                    state.handle.instance_id == incarnation
                    if incarnation is not None
                    else state.native_instance is not None
                    and state.native_instance == event.get("subagent_instance_id")
                ):
                    if event.get("subagent_instance_id") is not None:
                        state.native_instance = event["subagent_instance_id"]
                    state.stopping = True
                    for task in state.callbacks:
                        cancel_once(task)
                    if not state.terminal.done():
                        state.terminal.set_result(message)
            return
        if not turn_id:
            return
        if kind == "token_count":
            # Rate-limit counts need not belong to a live turn. They must not
            # create a never-settling owner without native admission.
            current = self._child_callbacks.get((event["subagent_id"], incarnation, turn_id))
            if current is None:
                return
            state = current
        else:
            state = self._child_callback_state(event["subagent_id"], incarnation, turn_id)
        if state.completion.done():
            if kind == "item_completed":
                self._observe_execution(
                    message,
                    thread_id=event["subagent_id"],
                    turn_id=turn_id,
                    submission_id=state.handle.submission_id,
                )
            return
        state.native_instance = event["subagent_instance_id"]
        if not state.observed.done():
            state.observed.set_result(None)
        self._observe_mcp(event, state)
        self._observe_progress(event, state.handle, event["subagent_id"])
        if kind in {"task_complete", "turn_complete", "turn_aborted"}:
            policy_failure = message.get("_sdk_policy_failure")
            if policy_failure is not None and not state.failure.done():
                state.policy_stop = "expired"
                state.failure.set_result(NativeError(policy_failure))
            if kind == "turn_aborted":
                state.stopping = True
                for task in state.callbacks:
                    cancel_once(task)
            if not state.terminal.done():
                state.terminal.set_result(
                    {**message, "_sdk_child_turn_failed": event.get("subagent_turn_failed")}
                )
        elif (
            kind
            in {
                "exec_approval_request",
                "apply_patch_approval_request",
                "elicitation_request",
            }
            and not state.stopping
            and not state.failure.done()
        ):
            try:
                if not message.get("_sdk_question_id"):
                    raise InteractionExpiredError("Native child request expired before delivery")
                if self._fail_unhandled_questions:
                    raise InteractionRequired(
                        "Native interaction needs on_interaction or Agent.session()"
                    )
                question = _question(self, message, state.handle)
                state.questions[id(question)] = question
                self._observations.notify()
                state.add_callback(
                    asyncio.create_task(self._expire_child_question(state, question))
                )
            except InteractionExpiredError:
                # Capture races source expiry; retain the original native outcome.
                pass
            except InteractionRequired as error:
                state.failure.set_result(error)
            except RuntimeError:
                if (
                    self._closing
                    or state.stopping
                    or state.terminal.done()
                    or not await self._native._child_question_pending(message["_sdk_question_id"])
                ):
                    return
                raise
        elif (
            kind
            in {
                "request_user_input",
                "request_permissions",
            }
            and not state.failure.done()
        ):
            state.failure.set_result(InteractionRequired("Native child requires interaction"))

    async def _select_step(self, state: _CallbackState, message: dict[str, Any]) -> None:
        with _owned_span("step callback", "callback", message.get("tracing_context")) as tracing:
            await self._select_step_body(state, message)
            if tracing is not None and state.failure.done():
                tracing.set_outcome(status="error", category="callback_error")

    async def _select_step_body(self, state: _CallbackState, message: dict[str, Any]) -> None:
        try:
            self._native._check_step(message["step_id"], message["thread"], message["instance"])
        except RuntimeError:
            return
        selected_json = None
        guard = _Invocation(self)
        token = _invocation.set(guard)
        try:
            if state.stopping or state.failure.done():
                return
            assert state.handle is not None
            if message["generation"] != self._catalog.generation:
                raise RuntimeError("Step selection belongs to an expired generation")
            index = state.step_index
            state.step_index += 1
            child = isinstance(state, _ChildCallbacks)
            callback = (
                self._native_agents.before_step
                if child and self._native_agents is not None
                else state.before_step
                if isinstance(state, _Submission)
                else None
            )

            def active() -> bool:
                if (
                    state.stopping
                    or state.failure.done()
                    or isinstance(state, _Submission)
                    and self._active is not state
                    or isinstance(state, _ChildCallbacks)
                    and (state.terminal.done() or state.completion.done())
                ):
                    return False
                try:
                    self._native._check_step(
                        message["step_id"], message["thread"], message["instance"]
                    )
                except RuntimeError:
                    return False
                return True

            selection = None
            if not active():
                return
            if callback is not None:
                selection = await _call_callback(
                    callback,
                    active,
                    state.handle if child else self.active_turn or state.handle,
                    step_index=index,
                )
            if not active():
                return
            defaults = {
                ToolRef(item["name"], item["namespace"] or "functions")
                for item in (
                    message["defaults"]
                    if message["defaults"] is not None
                    else message["candidates"]
                )
            }
            if selection is None:
                refs = defaults
            else:
                assert self._opening_agent is not None
                python, mcp, _ = _split_tools(
                    selection,
                    self._opening_agent.programmatic_tool_calling,
                    available=self._bindings,
                    ceiling=self._opening_agent.tools,
                    environment=self._executor,
                    native_agents=self._native_agents,
                )
                refs = set(
                    _selected_tool_refs(
                        selection,
                        python,
                        mcp,
                        programmatic_tool_calling=self._opening_agent.programmatic_tool_calling,
                        environment=self._executor,
                        native_agents=self._native_agents,
                    )
                )
                refs = {ToolRef(ref.name, ref.namespace or "functions") for ref in refs}
                if not refs <= defaults:
                    raise CapabilityError(
                        "Step tools must select distinct admitted operation tools"
                    )
            candidates = {
                ToolRef(item["name"], item["namespace"] or "functions")
                for item in message["candidates"]
            }
            # Search is an existing discovery helper over selected leaves. It
            # cannot restore an excluded runtime or a cached leaf from another step.
            search = ToolRef("tool_search", "functions")
            if refs and search in candidates:
                refs.add(search)
            selected_json = _native_tool_refs(tuple(refs & candidates))
        except BaseException as error:
            if not state.failure.done():
                state.failure.set_result(error)
        finally:
            guard.active = False
            _invocation.reset(token)
            try:
                self._native._reply_step(
                    message["step_id"], selected_json, message["thread"], message["instance"]
                )
            except RuntimeError:
                # Cancellation can retire this exact native nonce first.
                # A child's native nonce can retire before its terminal receipt.
                # Settlement owns that outcome; callback errors are retained above.
                if (
                    not isinstance(state, _ChildCallbacks)
                    and not state.stopping
                    and not state.failure.done()
                ):
                    state.failure.set_result(NativeError("Step selection expired"))

    async def _read_events(self) -> None:
        try:
            while True:
                raw_event = await self._native._next_session_event()
                event = json.loads(raw_event)
                if event is None:
                    break
                message = event.get("msg", {})
                if message.get("type") == "token_count" and "api_rate_limits" in message:
                    snapshot = _api_rate_limits(message["api_rate_limits"])
                    if snapshot is not None:
                        self._api_rate_limit_snapshot = snapshot
                        if self._rate_limit_parent is not None:
                            self._rate_limit_parent._api_rate_limit_snapshot = snapshot
                if message.get("type") == "_sdk_request_metadata":
                    route_id = message.get("submission_id")
                    route = (
                        self._request_observation_routes.get(route_id)
                        if isinstance(route_id, str)
                        else None
                    )
                    if route is not None and not route.accepted.done():
                        # Sampling has reached dispatch; reuse the same admission
                        # wait as ordinary output without consulting _active.
                        await asyncio.wait((route.accepted,))
                    self._observe_request_metadata(event)
                    continue
                if message.get("type") == "elicitation_status":
                    key = (message["server_name"], message["request_id"])
                    url_question = self._url_completions.get(key)
                    if url_question is not None and (
                        url_question.subagent_id == event.get("subagent_id")
                        and (
                            url_question.subagent_id is None
                            or url_question._message["_sdk_child_incarnation"]
                            == event.get("subagent_incarnation")
                        )
                    ):
                        self._settle_url_completion(url_question, message["completed"] is True)
                    continue
                if message.get("type") == "sdk_subagent_lifecycle":
                    self._observations.append(
                        SubagentLifecycle(
                            session_id=self.id,
                            subagent_id=message["subagent_id"],
                            parent_agent_id=message["parent_agent_id"],
                            status=message["status"],
                            kind=message["kind"],
                        )
                    )
                    continue
                if (
                    message.get("type") == "_sdk_model_generation_failed"
                    and event.get("subagent_id") is not None
                ):
                    if not self._closing:
                        child = self._child_callback_state(
                            event["subagent_id"],
                            event["subagent_incarnation"],
                            message["turn_id"],
                        )
                        if not child.completion.done() and not child.failure.done():
                            child.policy_stop = (
                                "expired" if message.get("policy_stop") is True else None
                            )
                            child.failure.set_result(_model_failure_error(message))
                    continue
                if message.get("type") == "_sdk_select_step" and message["thread"] != self.id:
                    try:
                        self._native._check_step(
                            message["step_id"], message["thread"], message["instance"]
                        )
                    except RuntimeError:
                        continue
                    child = self._child_callback_state(
                        message["thread"], message["instance"], message["turn_id"]
                    )
                    child.add_callback(asyncio.create_task(self._select_step(child, message)))
                    continue
                marker = message.get("_sdk_python_tool")
                if marker is not None and marker["thread"] != self.id:
                    if self._closing:
                        continue
                    request = json.dumps(message)
                    try:
                        self._native._check_python_tool(request)
                    except RuntimeError:
                        continue  # A cancelled or retired invocation grants no callback authority.
                    child = self._child_callback_state(
                        marker["thread"],
                        marker["instance"],
                        message["turnId"],
                        deepcopy(message),
                    )
                    if child.stopping or child.failure.done():
                        await cast(Any, self._native)._interrupt_child_callback(request)
                        continue
                    if message.get("_sdk_manual") is True:
                        self._manual_request(
                            child.handle.submission_id, set(), _call_id=message["callId"]
                        )
                        self._observations.notify()
                        continue
                    call_id = message["callId"]
                    if call_id in child.calls:
                        if not child.failure.done():
                            child.failure.set_result(
                                NativeError("Native child reused a tool call ID")
                            )
                        continue
                    child.calls.add(call_id)
                    child.callbacks.add(
                        asyncio.create_task(self._invoke_tool(child, deepcopy(message)))
                    )
                    continue
                if event.get("subagent_id") is not None:
                    await self._read_child_event(event)
                    continue
                state = self._active
                if (
                    message.get("type") == "item_completed"
                    and message.get("thread_id") == self.id
                    and isinstance(late_turn_id := message.get("turn_id"), str)
                    and (
                        state is None
                        or state.handle is None
                        or state.handle.turn_id != late_turn_id
                    )
                ):
                    started = self._observations.turn_started(late_turn_id)
                    if self._observe_execution(
                        message,
                        thread_id=self.id,
                        turn_id=late_turn_id,
                        submission_id=started.submission_id if started is not None else None,
                    ):
                        continue
                if self._startup_failure is not None and (state is None or state.handle is None):
                    if not self._startup_failure.done() and message.get("type") in {
                        "exec_approval_request",
                        "apply_patch_approval_request",
                        "elicitation_request",
                        "request_user_input",
                        "request_permissions",
                    }:
                        self._startup_failure.set_result(
                            InteractionRequired(
                                "Native startup requires interaction; "
                                "supply startup configuration before opening"
                            )
                        )
                    continue
                if state is None or (
                    (state.stopping or state.terminal.done() or state.failure.done())
                    and message.get("type") == "elicitation_request"
                    and not message.get("turn_id", message.get("turnId"))
                    and message["request"].get("mode")
                    in _FORM_MODES | {"url", "openai/userVerification"}
                ):
                    if (
                        not self._closing
                        and message.get("type") == "elicitation_request"
                        and not message.get("turn_id", message.get("turnId"))
                        and message["request"].get("mode")
                        in _FORM_MODES | {"url", "openai/userVerification"}
                    ):
                        # No turn owns an idle background question. Resolve
                        # its native token; never manufacture a public grant.
                        await self._native.call(
                            "resolve_elicitation",
                            json.dumps(
                                {
                                    "server_name": message["server_name"],
                                    "request_id": message["id"],
                                    "decision": "decline",
                                    "content": None,
                                    "meta": None,
                                }
                            ),
                        )
                    continue
                # Opening observations can outlive the publication that sent
                # them. Do not block the sole reader before MCP admission.
                # Retain assistant items: a real submission may emit its final
                # text before the native receipt reaches Python.
                if state.handle is None:
                    if message["type"] in {
                        "session_configured",
                        "warning",
                        "mcp_startup_update",
                        "mcp_startup_complete",
                    } or (
                        message["type"] == "raw_response_item"
                        and (
                            message["item"].get("type") != "message"
                            or message["item"].get("role") != "assistant"
                        )
                    ):
                        continue
                await asyncio.wait((state.accepted,))
                state.changed.set()
                handle = state.handle
                if handle is None:
                    continue
                message = event["msg"]
                kind = message["type"]
                turn_id = message.get("turn_id", message.get("turnId"))
                if kind in {"task_complete", "turn_complete", "turn_aborted"}:
                    turn_id = turn_id or event["id"]
                if turn_id and turn_id != handle.turn_id:
                    continue
                # Observe delivered MCP ends before settled-policy failure bookkeeping.
                self._observe_mcp(event, state)
                if kind in {"task_complete", "turn_complete", "turn_aborted"}:
                    policy_failure = message.get("_sdk_policy_failure")
                    if policy_failure is not None and not state.failure.done():
                        state.policy_stop = "expired"
                        state.failure.set_result(NativeError(policy_failure))
                    # An MCP reader exists only on the initial Core route.
                    if (
                        self._mcp_reader is not None
                        and cast(Any, self._native)._take_native_mcp_result_failure(handle.turn_id)
                        and not state.failure.done()
                    ):
                        state.failure.set_result(NativeError("Native MCP result policy failed"))
                    # A background URL has Session scope. Resolve its native
                    # token before this incidental root releases its active slot.
                    unanswered = [
                        question
                        for question in state.questions.values()
                        if not self._closing
                        and isinstance(question, ElicitationRequest)
                        and question.submission_id is None
                        and question._message["request"].get("mode") == "url"
                    ]
                    for pending_url in unanswered:
                        del state.questions[id(pending_url)]
                        pending_url._release_tracing()
                    for pending_url in unanswered:
                        await self._native.call(
                            "resolve_elicitation",
                            json.dumps(
                                {
                                    "server_name": pending_url._message["server_name"],
                                    "request_id": pending_url._message["id"],
                                    "decision": "decline",
                                    "content": None,
                                    "meta": None,
                                }
                            ),
                        )
                    if not state.terminal.done():
                        state.unsampled_steering = event.get("sdk_unsampled_steering") is True
                        state.terminal.set_result(message)
                elif kind == "_sdk_graceful_interrupt_failed":
                    if not state.stopping and not state.failure.done():
                        state.failure.set_result(NativeError("Native graceful interruption failed"))
                elif kind == "_sdk_model_generation_failed":
                    if not state.stopping and not state.failure.done():
                        state.policy_stop = (
                            "expired" if message.get("policy_stop") is True else None
                        )
                        state.failure.set_result(_model_failure_error(message))
                elif kind == "_sdk_select_step":
                    task = asyncio.create_task(self._select_step(state, message))
                    state.add_callback(task)
                elif kind == "token_count" and event["id"] == handle.turn_id:
                    self._observe_progress(event, handle, self.id)
                elif (
                    self._mcp_reader is not None
                    and cast(Any, self._native)._has_settled_native_mcp_result_failure(
                        handle.turn_id
                    )
                    and not state.failure.done()
                ):
                    state.failure.set_result(NativeError("Native MCP result policy failed"))
                elif kind == "raw_response_item" and event["id"] == handle.turn_id:
                    item = message["item"]
                    if item.get("type") == "message" and item.get("role") == "assistant":
                        texts = [
                            part["text"]
                            for part in item["content"]
                            if part["type"] in {"input_text", "output_text"}
                        ]
                        # Match Rust's trim; Python also strips U+001C through U+001F.
                        blank = bool(texts) and all(
                            not text.strip()
                            and not any(separator in text for separator in "\x1c\x1d\x1e\x1f")
                            for text in texts
                        )
                        # Unknown-phase blank tails keep the previous final answer.
                        if not blank or item.get("phase") == "final_answer":
                            state.last_assistant_had_blank_final = blank
                elif kind in {"agent_message_content_delta", "item_completed"}:
                    self._observe_progress(event, handle, self.id)
                elif kind == "dynamic_tool_call_request" and not state.stopping:
                    if message.get("_sdk_manual") is True:
                        # Input validation cannot depend on a consumer. A
                        # cursor may also validate first, under the same grant.
                        self._manual_request(
                            handle.submission_id, set(), _call_id=message["callId"]
                        )
                        self._observations.notify()
                        continue
                    call_id = message["callId"]
                    if call_id in state.calls:
                        state.stopping = True
                        if not state.failure.done():
                            state.failure.set_result(
                                NativeError(
                                    f"Native tool call ID {call_id!r} "
                                    "was reused within this submission"
                                )
                            )
                        continue
                    state.calls.add(call_id)
                    task = asyncio.create_task(self._invoke_tool(state, deepcopy(message)))
                    state.add_callback(task)
                elif (
                    kind
                    in {
                        "elicitation_request",
                        "exec_approval_request",
                        "apply_patch_approval_request",
                    }
                    and not state.stopping
                    and not state.failure.done()
                ):
                    if (
                        kind == "elicitation_request"
                        and not turn_id
                        and message["request"].get("mode")
                        in _FORM_MODES | {"openai/userVerification"}
                        and message["server_name"] not in state.mcp_calls.values()
                    ):
                        # Server-origin forms have no turn ID. Do not give an
                        # unrelated Python turn authority over a delayed form.
                        await self._native.call(
                            "resolve_elicitation",
                            json.dumps(
                                {
                                    "server_name": message["server_name"],
                                    "request_id": message["id"],
                                    "decision": "decline",
                                    "content": None,
                                    "meta": None,
                                }
                            ),
                        )
                        continue
                    try:
                        if self._fail_unhandled_questions:
                            raise InteractionRequired(
                                "Native interaction needs on_interaction or Agent.session()"
                            )
                        assert state.handle is not None
                        question = _question(self, message, state.handle)
                    except InteractionRequired as error:
                        state.failure.set_result(error)
                    else:
                        state.questions[id(question)] = question
                        self._observations.notify()
                elif (
                    kind
                    in {
                        "request_user_input",
                        "request_permissions",
                    }
                    and not state.failure.done()
                ):
                    state.failure.set_result(
                        InteractionRequired(
                            "This native interaction cannot be answered by the current Session API"
                        )
                    )
        except BaseException as error:  # report reader failure to its owner
            if self._startup_failure is not None and not self._startup_failure.done():
                self._startup_failure.set_result(error)
            state = self._active
            if (
                not self._sealing
                and state is not None
                and not state.terminal.done()
                and not state.failure.done()
            ):
                state.failure.set_result(error)
        finally:
            await self._retire_url_completions()
            if not self._sealing and self._sealed_checkpoint is None:
                self._broken = True
            state = self._active
            if (
                not self._sealing
                and state is not None
                and not state.terminal.done()
                and not state.failure.done()
            ):
                state.failure.set_result(
                    NativeError(
                        "Native event stream ended before settlement", category="connection_failed"
                    )
                )
            if self._native_agents is not None:
                cast(Any, self._native)._finish_child_event_reader()
                for child in self._child_callbacks.values():
                    if not child.observed.done():
                        child.observed.set_result(None)
                    if not child.completion.done() and not child.failure.done():
                        child.failure.set_result(
                            NativeError(
                                "Native child event stream ended", category="connection_failed"
                            )
                        )
            self._finish_failed_observations()

    async def _read_mcp_results(self) -> None:
        try:
            while True:
                raw = await self._native._next_native_mcp_result()
                notice = json.loads(raw)
                if notice is None:
                    return
                nonce = notice["nonce"]
                if not self._native._handoff_native_mcp_result(nonce):
                    self._native._release_native_mcp_result(nonce)
                    continue
                dispatched = False
                try:
                    if self._closing:
                        continue
                    state: _CallbackState | None
                    if notice["thread_id"] != self.id:
                        if not self._native._check_native_mcp_result(nonce):
                            continue
                        state = self._child_callback_state(
                            notice["thread_id"],
                            notice["instance"],
                            notice["turn_id"],
                            deepcopy(notice),
                        )
                    else:
                        state = self._active
                    if state is None:
                        continue
                    if isinstance(state, _Submission):
                        await asyncio.wait((state.accepted,))
                    if isinstance(state, _ChildCallbacks) and (
                        state.stopping or state.failure.done()
                    ):
                        await self._native._interrupt_child_callback(json.dumps(notice))
                        continue
                    handle = state.handle
                    if (
                        state.stopping
                        or handle is None
                        or notice["turn_id"] != handle.turn_id
                        or notice["generation"] != self._catalog.generation
                    ):
                        continue
                    task = asyncio.create_task(self._invoke_mcp_policy(state, notice))

                    def finished(_task: asyncio.Task[None], nonce: int = nonce) -> None:
                        # Own storage even if cancellation prevents coroutine entry.
                        self._native._release_native_mcp_result(nonce)

                    task.add_done_callback(finished)
                    state.add_callback(task)
                    dispatched = True
                finally:
                    if not dispatched:
                        self._native._release_native_mcp_result(nonce)
        except BaseException as error:
            state = self._active
            if not self._closing and state is not None and not state.failure.done():
                state.failure.set_result(error)
            if not self._closing:
                for child in self._child_callbacks.values():
                    if not child.completion.done() and not child.failure.done():
                        child.failure.set_result(NativeError("Native MCP result reader failed"))
        finally:
            self._native._abandon_native_mcp_result_read()

    async def _invoke_executor_policy(
        self,
        state: _CallbackState | None,
        nonce: int,
        request: dict[str, Any],
        *,
        cell_owner: _CellPolicyOwner | None = None,
    ) -> None:
        with _owned_span(
            "executor callback", "callback", request.get("tracing_context")
        ) as tracing:
            await self._invoke_executor_policy_body(
                state, nonce, request, cell_owner=cell_owner, tracing=tracing
            )

    async def _invoke_executor_policy_body(
        self,
        state: _CallbackState | None,
        nonce: int,
        request: dict[str, Any],
        *,
        cell_owner: _CellPolicyOwner | None = None,
        tracing: TraceScope | None = None,
    ) -> None:
        scope = _Invocation(self)
        token = _invocation.set(scope)
        expired = False

        def admitted() -> bool:
            if expired:
                return False
            if cell_owner is not None:
                return (
                    not self._closing
                    and not self._broken
                    and self._interception is not None
                    and self._interception.owns_cell(cell_owner)
                    and cell_owner.matches(request)
                    and self._native._check_native_interception(nonce)
                )
            assert state is not None
            return (
                not self._closing
                and (isinstance(state, _ChildCallbacks) or self._active is state)
                and state.handle is not None
                and state.handle.turn_id == request["turn_id"]
                and not state.stopping
                and not state.failure.done()
                and self._native._check_native_interception(nonce)
            )

        def fail_original(error: BaseException) -> None:
            if tracing is not None:
                cancelled = isinstance(error, asyncio.CancelledError)
                tracing.set_outcome(
                    status="cancelled" if cancelled else "error",
                    category="cancelled" if cancelled else "callback_error",
                )
            target = state
            if cell_owner is not None:
                target = self._active
                if (
                    target is None
                    or target.handle is not cell_owner.turn
                    or target.stopping
                    or target.terminal.done()
                ):
                    return
            if target is not None and not target.failure.done():
                if cell_owner is None and request["route"] == "direct":
                    # Fence this exact native decision before Python retires its owner.
                    self._native._hold_native_interception_for_stop(nonce)
                target.failure.set_result(error)

        async def stop_owner() -> None:
            if cell_owner is None:
                assert state is not None
                await self._stop_policy_turn(state, nonce)
                return
            target = self._active
            if (
                target is not None
                and target.handle is cell_owner.turn
                and not target.stopping
                and not target.failure.done()
                and not target.terminal.done()
            ):
                # Keep the decision held until the original active root stops.
                # Ending its cell first could let Core sample another response.
                await self._stop_policy_turn(target, nonce)
                return
            if not self._native._hold_native_interception_for_stop(nonce):
                return
            # The FFI captures the already-held original native owner now,
            # before cancellation/retirement can remove its pending nonce.
            cleanup: asyncio.Future[Any] | None = None
            try:
                stop_task = asyncio.ensure_future(
                    self._native._finish_native_interception_stop(nonce)
                )
                cleanup = stop_task
                target = self._active
                if (
                    target is not None
                    and target.handle is cell_owner.turn
                    and not target.terminal.done()
                ):
                    target.policy_stop = "requested"
                    fail_original(NativeError("Turn stopped by policy"))
                await _join(stop_task)
            finally:
                # Setup can fail synchronously. _join can also rethrow waiter
                # cancellation with an ACK failure chained underneath it.
                if cleanup is None or (
                    cleanup.done() and (cleanup.cancelled() or cleanup.exception() is not None)
                ):
                    assert self._interception is not None
                    self._interception.cell_cleanup_failed()

        try:
            if isinstance(state, _Submission):
                await asyncio.wait((state.accepted,))
            if not admitted() or not self._native._handoff_native_interception(nonce):
                return
            executor = cell_owner.executor if cell_owner is not None else self._executor
            handle = cell_owner.turn if cell_owner is not None else state.handle if state else None
            assert executor is not None and handle is not None
            before = request["phase"] == "before"
            callback = executor.transform_arguments if before else executor.transform_result
            decision: dict[str, Any] = {"action": "continue"}
            if callback is not None or (before and executor.approval is not None):
                agent = cell_owner.agent if cell_owner is not None else self._agent
                assert agent is not None
                context = ExecutorContext(
                    cell_owner.context if cell_owner is not None else self._context,
                    handle,
                    request["tool_name"]["name"],
                    request["thread_id"],
                    request["turn_id"],
                    request["outer_call_id"],
                    request["cell_id"],
                    request["runtime_tool_call_id"],
                    request["call_id"],
                    session=self,
                    agent=agent,
                    agent_role=request.get("agent_role"),
                    model=request.get("model"),
                )
                supplied = request["input" if before else "result"]
                if before and request["tool_kind"] == "function_json":
                    supplied = json.loads(supplied)
                    if not isinstance(supplied, dict):
                        raise ValueError("Executor function arguments must be an object")
                if before and executor.approval is not None:
                    approved = await _call_callback(
                        executor.approval, admitted, context, deepcopy(supplied)
                    )
                    if not admitted():
                        return
                    if isinstance(approved, StopTurn) or (
                        approved is False and request["route"] == "code_mode"
                    ):
                        await stop_owner()
                        return
                    if type(approved) is not bool:
                        raise TypeError("Environment approval must return a bool")
                    if not approved:
                        if not self._native._resolve_native_interception(
                            nonce, '{"action":"deny"}'
                        ):
                            raise NativeError("Executor policy decision expired")
                        return
                if callback is not None:
                    # Approval owns its own waiting. Only the transform gets
                    # the configured policy deadline.
                    loop = asyncio.get_running_loop()
                    cutoff = loop.time() + executor.policy_timeout
                    callback_task = asyncio.current_task()
                    assert callback_task is not None

                    def expire_transform(cancel_body: bool = True) -> None:
                        nonlocal expired
                        if not admitted():
                            return
                        # A completed root has no failure future to revoke this
                        # callback. Timeout is terminal for the exact decision.
                        expired = True
                        if not before:
                            self._native._expire_native_interception_result(nonce)
                            target = state if cell_owner is None else self._active
                            if target is not None and target.handle is handle:
                                target.policy_stop = "expired"
                        fail_original(NativeError("Executor policy failed"))
                        self._native._resolve_native_interception(nonce, '{"action":"deny"}')
                        if cancel_body:
                            cancel_once(callback_task)

                    timer = loop.call_at(cutoff, expire_transform)
                    try:
                        # An in-place mutation is not a native replacement on Allow.
                        value = await _call_callback(
                            callback, admitted, context, deepcopy(supplied) if before else supplied
                        )
                    finally:
                        timer.cancel()
                        # Sync code or a cancellation-suppressing callback may
                        # finish after cutoff but before timer dispatch.
                        if loop.time() >= cutoff:
                            expire_transform(cancel_body=False)
                    if not admitted():
                        return
                    if before and isinstance(value, StopTurn):
                        await stop_owner()
                        return
                    if before:
                        if not isinstance(value, Allow):
                            # Only actual SDK decisions carry authority. Raw JSON
                            # stays data, even if it contains an "action" field.
                            supplied = (
                                value.arguments if isinstance(value, ReplaceArguments) else value
                            )
                            supplied = json.loads(json.dumps(supplied, allow_nan=False))
                            if (
                                request["tool_kind"] == "function_json"
                                and not isinstance(supplied, dict)
                                or request["tool_kind"] == "freeform"
                                and not isinstance(supplied, str)
                            ):
                                raise TypeError("Invalid native replacement arguments")
                            decision = {"action": "replace", "value": supplied}
                    else:
                        decision = {"action": "replace", "value": value}
            if admitted():
                reply = json.dumps(decision, allow_nan=False)
                if not self._native._resolve_native_interception(nonce, reply):
                    raise NativeError("Executor policy decision expired")
        except BaseException as error:
            if admitted():
                fail_original(
                    asyncio.CancelledError()
                    if isinstance(error, asyncio.CancelledError)
                    else NativeError("Executor policy failed")
                )
            elif not isinstance(error, asyncio.CancelledError):
                # Native revocation can precede Python's stopping flag.
                # Keep rejected input/results out of both history and errors.
                if tracing is not None:
                    tracing.set_outcome(status="error", category="callback_error")
                if state is not None:
                    state.retain_cleanup_error(NativeError("Executor policy cleanup failed"))
                elif cell_owner is not None:
                    cell_owner.cleanup_failed = True
        finally:
            # Returning the payload does not retire an unanswered decision.
            # Resolve failed or no-longer-admitted work before releasing it;
            # an already accepted decision makes this an exact-nonce no-op.
            self._native._resolve_native_interception(nonce, '{"action":"deny"}')
            scope.active = False
            _invocation.reset(token)

    async def _stop_policy_turn(self, state: _CallbackState, nonce: int) -> None:
        """Hold the admitted native decision while its exact owner retires."""
        if not self._native._hold_native_interception_for_stop(nonce):
            return
        state.policy_stop = "requested"
        # All Python callback/worker admission runs on this loop. Seal it
        # before yielding after the native turn's atomic effect fence.
        state.stopping = True
        # Do not retain the application's decision or its private reason.
        state.failure.set_result(NativeError("Turn stopped by policy"))
        # The owner cancels callbacks after joined native retirement. Terminal
        # alone precedes that receipt; completion would require joining ourselves.
        await self._loop.create_future()

    async def _invoke_mcp_policy(self, state: _CallbackState, notice: dict[str, Any]) -> None:
        with _owned_span("MCP callback", "callback", notice.get("tracing_context")) as tracing:
            await self._invoke_mcp_policy_body(state, notice)
            if tracing is not None and state.failure.done():
                tracing.set_outcome(status="error", category="callback_error")

    async def _invoke_mcp_policy_body(self, state: _CallbackState, notice: dict[str, Any]) -> None:
        nonce = notice["nonce"]
        scope = _Invocation(self)
        token = _invocation.set(scope)
        try:
            server = next(
                (
                    s
                    for s in self._mcp_tools
                    if s.name == notice["server_name"] and notice["tool_name"] in s.tools
                ),
                None,
            )
            if server is None or server.transform_result is None:
                raise NativeError("Native MCP result does not match its captured declaration")
            assert state.handle is not None

            def admitted() -> bool:
                return (
                    not self._closing
                    and not state.stopping
                    and not state.failure.done()
                    and self._native._check_native_mcp_result(nonce)
                )

            if not admitted():
                return
            context = MCPResultContext(
                self._context,
                state.handle,
                notice["call_id"],
                notice["server_name"],
                notice["tool_name"],
            )
            value = await _call_callback(
                server.transform_result, admitted, context, notice["result"]
            )
            if not admitted():
                return
            reply = json.dumps(value)
            accepted = self._native._resolve_native_mcp_result(
                nonce, notice["turn_id"], notice["call_id"], reply
            )
            if not accepted and not state.stopping:
                raise NativeError("Native MCP result policy failed")
        except BaseException as error:
            if not state.stopping and not state.failure.done():
                # Callback or result errors may contain the unaccepted peer data.
                state.failure.set_result(NativeError("Native MCP result policy failed"))
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(NativeError("Native MCP result policy cleanup failed"))
        finally:
            scope.active = False
            _invocation.reset(token)

    def _admit_tool(self, state: _CallbackState, request: str) -> bool:
        """Recheck authority before the invocation starts another Python step."""
        if state.stopping or state.failure.done() or self._closing:
            return False
        self._native._check_python_tool(request)
        return True

    async def _invoke_tool(self, state: _CallbackState, message: dict[str, Any]) -> None:
        with _owned_span("tool callback", "callback", message.get("tracing_context")) as tracing:
            await self._invoke_tool_body(state, message, tracing)

    async def _invoke_tool_body(
        self, state: _CallbackState, message: dict[str, Any], tracing: TraceScope | None
    ) -> None:
        scope = _Invocation(self)
        token = _invocation.set(scope)
        started: ToolStarted | None = None
        status: Literal["completed", "failed", "cancelled"] = "cancelled"
        try:
            request = json.dumps(message)
            marker = message["_sdk_python_tool"]
            thread_id = state.handle.child_id if isinstance(state, _ChildCallbacks) else self.id
            if marker["thread"] != thread_id or marker["generation"] != self._catalog.generation:
                raise NativeError("Native tool request belongs to an expired catalog")
            entry = self._catalog.tools[marker["slot"]]
            if not isinstance(entry, _CapturedFunction):
                raise NativeError("Native callback does not match a function declaration")
            if message["tool"] != entry.name or message.get("namespace") != entry.namespace:
                raise NativeError("Native tool request does not match its captured declaration")
            # Pydantic validators are application code too. Admit the original
            # native request before argument validation can execute that code.
            if not self._admit_tool(state, request):
                return
            assert state.handle is not None and state.handle.turn_id is not None
            code_origin = message["_sdk_code_origin"]
            started = ToolStarted(
                session_id=self.id,
                turn_id=state.handle.turn_id,
                submission_id=state.handle.submission_id,
                call_id=message["callId"],
                name=entry.name,
                namespace=entry.namespace,
            )
            self._observations.append(started)
            parsed = entry._parse_arguments(message["arguments"])
            kwargs = entry._arguments(parsed)
            raw_arguments = message["arguments"]
            if isinstance(raw_arguments, str):
                raw_arguments = json.loads(raw_arguments)
            context = ToolContext(
                entry.context,
                state.handle,
                message["callId"],
                MappingProxyType(deepcopy(raw_arguments)),
                MappingProxyType(deepcopy(kwargs)),
                trace_context=_native_trace_context(message.get("_sdk_trace")),
                call=ToolCallInfo(
                    tool=ToolRef(entry.name, entry.namespace),
                    session_id=self.id,
                    turn_id=state.handle.turn_id,
                    call_id=message["callId"] if code_origin is None else None,
                    child_id=state.handle.child_id if isinstance(state, _ChildCallbacks) else None,
                    child_incarnation=(
                        state.handle.instance_id if isinstance(state, _ChildCallbacks) else None
                    ),
                    role=None,
                    model=message.get("_sdk_model"),
                    route="direct" if code_origin is None else "code",
                    cell_id=code_origin["cell_id"] if code_origin is not None else None,
                    nested_call_id=(
                        code_origin["nested_call_id"] if code_origin is not None else None
                    ),
                ),
            )
            if entry.before is not None:
                decision = await _call_callback(
                    entry.before, lambda: self._admit_tool(state, request), context
                )
                if not self._admit_tool(state, request):
                    return
                if isinstance(decision, Deny):
                    # A local denial never invokes the body or the after hook.
                    denial = ToolOutput.error(decision.reason)
                    denial._validate()
                    reply = json.dumps({"request": message, "response": denial.to_native()})
                    if self._admit_tool(state, request):
                        accepted = await self._native._respond_python_tool(reply)
                        if not accepted and not state.stopping:
                            raise NativeError("Native tool denial was not accepted")
                        if accepted:
                            status = "failed"
                    return
                if isinstance(decision, ReplaceArguments):
                    parsed = entry._parse_arguments(decision.arguments)
                    kwargs = entry._arguments(parsed)
                    context = replace(
                        context, effective_arguments=MappingProxyType(deepcopy(kwargs))
                    )
                elif not isinstance(decision, Allow):
                    raise TypeError("Function before must return Allow, Deny or ReplaceArguments")
                # A returned decision may alias SDK-owned input. Do not retain it
                # across output validation.
                del decision
            args: tuple[ToolContext[ContextT | None], ...] = ()
            parameter = entry.context_parameter
            if parameter is not None:
                args = (context,)
            if not self._admit_tool(state, request):
                return
            duration = entry.callback_timeout
            if duration is None:
                value = await _call_callback(
                    entry.callback,
                    lambda: self._admit_tool(state, request),
                    *args,
                    **kwargs,
                )
            else:
                loop = asyncio.get_running_loop()
                cutoff = loop.time() + duration
                callback_task = asyncio.current_task()
                assert callback_task is not None
                timeout_error = TimeoutError(f"Tool {entry.name!r} exceeded its callback timeout")

                def expire_callback(cancel_body: bool = True) -> None:
                    if self._closing or state.stopping or state.failure.done():
                        return
                    # Every stop owner joins this same failure and cancellation.
                    state.failure.set_result(timeout_error)
                    if cancel_body:
                        cancel_once(callback_task)

                timer = loop.call_at(cutoff, expire_callback)
                try:
                    value = await _call_callback(
                        entry.callback,
                        lambda: self._admit_tool(state, request),
                        *args,
                        **kwargs,
                    )
                finally:
                    timer.cancel()
                    # A busy callback can cross the cutoff before timer dispatch.
                    if loop.time() >= cutoff:
                        expire_callback(cancel_body=False)
            if not self._admit_tool(state, request):
                return
            output: ToolOutput | None = None
            try:
                value = entry.output_adapter.validate_python(value)
                if isinstance(value, ToolOutput):
                    output = value
                else:
                    wire = entry.output_adapter.dump_json(value, warnings="error").decode()
                    # Declared strings retain raw text; other results keep JSON wire semantics.
                    text = json.loads(wire) if isinstance(value, str) else wire
                    output = ToolOutput.text(text if isinstance(text, str) else wire)
                    del wire, text
                output._validate()
            except Exception:
                output = None
            if output is None:
                # End ownership of the original admission request and every
                # invocation data reference without altering caller-owned objects.
                del request, message, raw_arguments, parsed, kwargs, context, args, value
                raise ValueError("Tool result failed validation")
            if entry.after is not None:
                output = await _call_callback(
                    entry.after, lambda: self._admit_tool(state, request), context, output
                )
                if not self._admit_tool(state, request):
                    return
                if not isinstance(output, ToolOutput):
                    raise TypeError("Function after must return ToolOutput")
                try:
                    output._validate()
                except Exception:
                    output = None
                if output is None:
                    # The hook may have mutated the original callback result.
                    del request, message, raw_arguments, parsed, kwargs, context, args, value
                    raise ValueError("Tool result failed validation")
            reply = json.dumps({"request": message, "response": output.to_native()})
            if not self._admit_tool(state, request):
                return
            accepted = await self._native._respond_python_tool(reply)
            if not accepted and not state.stopping:
                raise NativeError("Native tool result was not accepted")
            if accepted:
                status = "completed" if output.success else "failed"
        except BaseException as error:  # report every callback exit to its owner
            status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            if not state.stopping and not state.failure.done():
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            if tracing is not None:
                tracing.set_outcome(
                    status="ok"
                    if status == "completed"
                    else "cancelled"
                    if status == "cancelled"
                    else "error",
                    category=None
                    if status == "completed"
                    else "cancelled"
                    if status == "cancelled"
                    else "tool_error",
                )
            try:
                if started is not None:
                    self._observations.append(
                        ToolFinished(
                            session_id=self.id,
                            turn_id=started.turn_id,
                            submission_id=started.submission_id,
                            call_id=started.call_id,
                            name=started.name,
                            namespace=started.namespace,
                            status=status,
                        )
                    )
            finally:
                scope.active = False
                _invocation.reset(token)

    async def items(self, *, subagent_id: str | None = None) -> AsyncIterator[SessionItem]:
        """Iterate a snapshot of recorded items, including inherited child history."""
        self._check_open()
        history = json.loads(await self._native.read_thread_history(subagent_id))
        turns, items = project_history(history, self.id, subagent_id)
        if subagent_id is None:
            original_ids = {turn["id"] for turn in turns}
            repair_parents = {
                turn["id"]: turn["parent_turn_id"]
                for turn in self._repair_turns
                if "parent_turn_id" in turn and turn["parent_turn_id"] in original_ids
            }
            by_parent: dict[str, list[SessionItem]] = {}
            for item in self._repair_items:
                parent = repair_parents.get(item["turn_id"])
                if parent is not None:
                    by_parent.setdefault(parent, []).append(item)
            # Leave all native first-seen order intact, including items whose
            # turn has no projected terminal. Insert receipts at their owner.
            last_item = {item["turn_id"]: index for index, item in enumerate(items)}
            merged: list[SessionItem] = []
            for index, item in enumerate(items):
                merged.append(item)
                if last_item[item["turn_id"]] == index:
                    merged.extend(deepcopy(by_parent.pop(item["turn_id"], [])))
            # A captured parent can lack projected items. Its actual repair
            # receipts still belong in this snapshot.
            for remaining in by_parent.values():
                merged.extend(deepcopy(remaining))
            items = merged
        for item in items:
            yield item

    async def subagents(self) -> AsyncIterator[Subagent]:
        """Iterate current descendant records without resuming native work."""
        self._check_open()
        data = json.loads(await self._native.call("list_subagents"))
        data.sort(key=lambda child: (child["opened_at"], child["id"]))
        for child in data:
            snapshot = await self._native._child_instance(child["id"])
            if snapshot is None:
                yield _subagent(self, child, None, None, None)
            else:
                instance, incarnation, native = snapshot
                turn = None if native is None else _child_turn(self, native)
                yield _subagent(self, child, turn, instance, incarnation)

    def _callbacks_for_child(self, handle: Turn[Any]) -> list[_ChildCallbacks]:
        return [
            state
            for state in self._child_callbacks.values()
            if state.handle.submission_id == handle.submission_id
        ]

    async def turns(self, *, subagent_id: str | None = None) -> AsyncIterator[RecordedTurn]:
        """Iterate a snapshot of recorded turns, including restored terminal outcomes."""
        self._check_open()
        history = json.loads(await self._native.read_thread_history(subagent_id))
        turns, _ = project_history(history, self.id, subagent_id)
        if subagent_id is None:
            by_parent: dict[str, list[RecordedTurn]] = {}
            for repair in self._repair_turns:
                if "parent_turn_id" in repair:
                    by_parent.setdefault(repair["parent_turn_id"], []).append(repair)
            merged: list[RecordedTurn] = []
            for turn in turns:
                merged.append(turn)
                merged.extend(deepcopy(by_parent.get(turn["id"], [])))
            turns = merged
        for turn in turns:
            yield turn

    async def checklist(self, *, subagent_id: str | None = None) -> ChecklistState | None:
        """Read the root or native descendant's latest checklist, or None before an update.

        Reads never grant permission to update the list. Restored data is
        readable when the destination Agent does not declare Checklist.
        As with items() and turns(), the ID must belong to this Session's graph.
        """
        self._check_open()
        value = await self._native.read_checklist(subagent_id)
        return None if value == "null" else ChecklistState.model_validate_json(value)

    async def goal(self) -> NativeGoal | None:
        """Read actual Core goal data on a local Session opened with NativeGoals.

        Core owns its status, token and time accounting. Reads do not resume
        goal work, enable Checklist or grant tools to a restored Session.
        """
        self._check_open()
        value = await self._native.read_goal()
        return cast(NativeGoal | None, json.loads(value))

    async def save_state(self) -> SessionState:
        """Snapshot an idle session without retiring it.

        Active turns and manual requests must finish before a snapshot. Use
        suspend() to retire a session at a supported manual-tool boundary.
        Settled native stored values and terminal cell observations survive
        restoration without rerunning cells. Active code cells are not captured.
        """
        return await self._save_state()

    async def _save_state(self, *, probe: bool = False) -> SessionState:
        self._check_open()
        self._check_portable_capture()
        self._check_callback_operation("save its own Session")
        if self._active is not None:
            raise RuntimeError(
                "save_state requires an idle Session; use suspend for pending manual tools"
            )
        return await self._checkpoint(None, probe=probe)

    async def fork(self) -> SessionState:
        """Copy an idle native conversation for an independently owned Session.

        Open the result with Agent.session(state=...). The source remains
        usable; subsequent source turns are not part of the fork. The new
        Agent supplies current tools, credentials, context, and environment.
        LocalStorage profiles, unsettled work and conversations with native
        descendants cannot be copied this way. Use save_state() for an idle
        conversation snapshot that includes settled descendants.
        """
        self._check_open()
        self._check_portable_capture()
        self._check_callback_operation("fork its own Session")
        if self._active is not None:
            raise RuntimeError("fork requires an idle Session")
        return await self._checkpoint(None, fork=True)

    async def checkpoint_status(self, *, kind: Literal["idle", "manual_wait"]) -> CheckpointStatus:
        """Check actual capture admission without reserving a checkpoint.

        idle validates the same native copy that save_state validates.
        manual_wait probes settled manual admission without sealing or waiting
        for active automatic work. A later save_state or suspend checks again.
        """
        self._check_loop()
        self._check_callback_operation("inspect checkpoints of its own Session")
        if kind not in ("idle", "manual_wait"):
            raise ValueError("kind must be 'idle' or 'manual_wait'")
        try:
            self._check_portable_capture()
            if kind == "idle":
                await self._save_state(probe=True)
            else:
                active = self._active
                self._check_manual_capture(active)
                assert active is not None
                self._check_checkpoint_admission(active)
                if not await self._manual_ready(active, probe=True):
                    return CheckpointStatus("not_ready", ("No settled pending manual boundary",))
        except NativeError:
            return CheckpointStatus(
                "unknown", ("Native checkpoint readiness could not be verified",)
            )
        except (RuntimeError, ValueError) as error:
            return CheckpointStatus("not_ready", (str(error),))
        return CheckpointStatus("ready", ())

    async def suspend(self) -> SessionState:
        """Stop and export a pending manual-tool boundary for a new Session.

        Wait for the pending request's sample and callbacks to settle, then
        retire the original session. A repeated suspend returns the same state.
        No active automatic work or unfinished child work is checkpointed.
        Settled native stored values and terminal cell observations are retained.
        Active code cells and root before_step callbacks cannot be captured.
        """
        self._check_loop()
        self._check_callback_operation("suspend its own Session")
        if self._sealed_checkpoint is not None and not self._closing and not self._closed:
            return SessionState.from_json(self._sealed_checkpoint)
        return await self._suspend()

    async def _suspend(self, *, before_seal: Callable[[], None] | None = None) -> SessionState:
        self._check_portable_capture()
        active = self._active
        self._check_manual_capture(active)
        assert active is not None
        # Waiting precedes checkpoint ownership: cancellation or close can
        # still stop a callback that has not reached a safe sealing boundary.
        if self._child_admissions or any(
            not child.completion.done() for child in self._child_callbacks.values()
        ):
            raise RuntimeError("Cannot save while native child work or callbacks remain active")
        if not await self._wait_manual_boundary(active, require_request=True):
            raise _ManualBoundaryLost("The active submission is not a settled manual boundary")
        self._check_open()
        return await self._checkpoint(active, before_seal=before_seal)

    def _check_portable_capture(self) -> None:
        if self._pending:
            raise RuntimeError("Cannot checkpoint a Session with queued work")
        if self._storage is not None:
            raise CapabilityError(
                "Native local storage cannot export portable SessionState; "
                "retain Session.id and reopen from its LocalStorage profile"
            )
        if self._repair_receipts_incomplete:
            raise CapabilityError("Cannot export an incomplete prompted-output repair receipt")
        if self._active is not None and self._active.repairing:
            raise CapabilityError("Cannot checkpoint while prompted-output repair is active")

    def _check_manual_capture(self, active: _Submission | None) -> None:
        self._check_open()
        if active is None:
            raise RuntimeError("suspend requires a pending manual-tool boundary")
        if active.before_step is not None:
            raise CapabilityError(
                "Cannot suspend an operation with before_step: "
                "portable restore cannot recover its callback"
            )

    def _check_checkpoint_admission(self, active: _Submission | None) -> None:
        self._check_open()
        if self._pending:
            raise RuntimeError("Cannot checkpoint a Session with queued work")
        self._check_graph_admission()
        self._check_root_admission()
        if self._active is not active:
            raise RuntimeError("Session is no longer at the requested idle or manual boundary")
        if self._mutation is not None:
            raise RuntimeError("Cannot save a checkpoint during a Session mutation")
        if self._checkpoint_task is not None:
            raise RuntimeError("Session is saving a checkpoint")
        if self._child_admissions or any(
            not child.completion.done() for child in self._child_callbacks.values()
        ):
            raise RuntimeError("Cannot save while native child work or callbacks remain active")

    async def _checkpoint(
        self,
        active: _Submission | None,
        *,
        before_seal: Callable[[], None] | None = None,
        fork: bool = False,
        probe: bool = False,
    ) -> SessionState:
        while True:
            self._check_checkpoint_admission(active)
            continuations = tuple(
                child.continuation
                for child in self._child_callbacks.values()
                if child.continuation is not None
            )
            if all(continuation.done() for continuation in continuations):
                for continuation in continuations:
                    continuation.result()
                break
            # These checks belong to the Session. Passive preparation must
            # neither cancel them nor hold checkpoint ownership across close.
            await asyncio.gather(*(asyncio.shield(task) for task in continuations))
        with _private_task(self._capture_checkpoint(active, before_seal, fork, probe=probe)) as (
            pending,
            outcome,
        ):
            self._checkpoint_task = pending
            try:
                if active is None and not fork:
                    # This owner cancels a read-only export and joins its native receipt.
                    await pending
                    return SessionState.from_json(outcome.result())
                return SessionState.from_json(await _join(pending, outcome=outcome))
            finally:
                self._checkpoint_task = None

    async def _capture_checkpoint(
        self,
        active: _Submission | None,
        before_seal: Callable[[], None] | None,
        fork: bool,
        *,
        probe: bool = False,
    ) -> str:
        active_uses_executor = active is not None and (
            active.operation_refs is None
            or not _fixed_tool_refs((), False, self._executor, self._native_agents).isdisjoint(
                active.operation_refs
            )
            or any(
                isinstance(server, StdioMCP) and server.location == "environment"
                for server in active.operation_mcp or ()
            )
        )
        executor_policy_digest = None
        if (
            active_uses_executor
            or self._strict_executor_writes
            or (self._executor is not None and self._executor.network is not None)
        ):
            executor_policy_digest = await self._executor_policy_digest()
        if active is not None:
            if not await self._manual_ready(active):
                raise _ManualBoundaryLost("The active submission is not a settled manual boundary")
            if before_seal is not None:
                before_seal()
            # Block replies and suppress expected ShutdownComplete/EOF while
            # Core decides whether it can stop the original writer. Readers
            # must keep dispatching until native capture confirms suspension.
            self._sealing = True
        native_sealed = False
        pre_seal_refused = False
        try:
            if active is None:
                idle_mcp = self._mcp_tools
                if not probe:
                    assert self._opening_agent is not None
                    opening = self._opening_agent.tools
                    if self._default_tools is None:
                        # A temporary child roster is not a persistent default.
                        # Leave unselected MCP connections untouched on idle save.
                        selected = self._bound_tools
                        mcp = self._mcp_tools
                    else:
                        python, mcp, _ = _split_tools(
                            self._default_tools,
                            self._opening_agent.programmatic_tool_calling,
                            available=self._bindings,
                            ceiling=opening,
                            environment=self._executor,
                            native_agents=self._native_agents,
                        )
                        bound = {tool.name: tool for tool in self._bound_tools}
                        selected = tuple(bound[tool.name] for tool in python)
                    idle_mcp = mcp
                    if selected != self._catalog.tools:
                        _, allowed_mcp, _ = _split_tools(opening)
                        candidate = _ToolCatalog(self._catalog.generation + 1, selected)
                        # Export the default Python catalog without reacquiring
                        # MCP defaults. Keep the live roster's policy generation.
                        # Catalog publication must settle before export cancellation.
                        with _private_task(
                            self._replace_tools(
                                candidate, self._mcp_tools, allowed_mcp, candidate.native_specs()
                            )
                        ) as (publishing, published):
                            await _join(publishing, outcome=published)
                try:
                    encoded = await self._native._export_checkpoint()
                except RuntimeError as error:
                    if fork:
                        raise RuntimeError(
                            "fork requires settled source and descendant code cells: " + str(error)
                        ) from error
                    if not probe:
                        raise
                    raise NativeError("Native checkpoint readiness could not be verified") from None
                payload = json.loads(encoded)
                if fork:
                    if payload["descendants"]:
                        raise RuntimeError("fork cannot copy conversations with native descendants")
                    # Export admission rejects active children, background work
                    # and unknown effects before a second writer can open. Use
                    # Core's native fork history and fresh ID, never replay jobs.
                    source = payload
                    copied = await self._engine.fork_thread(
                        self.id,
                        # The temporary writer only copies settled history.
                        # Discovery belongs to the current destination on open,
                        # not to this passive capture or the source roster.
                        options_json=json.dumps({"config": {"mcp_servers": {}}}),
                        specs_json=self._catalog.native_specs(),
                    )
                    try:
                        payload = json.loads(await copied._export_checkpoint())
                    except BaseException as error:
                        await _close_after_error(
                            error, lambda: copied.close(discard_checkpoint_import=True)
                        )
                        raise
                    else:
                        await copied.close(discard_checkpoint_import=True)
                    # Core copies native history, but retained planning records
                    # live outside that history. Carry only settled root data,
                    # under Core's new identity; never transfer grants or work.
                    for field in ("root_checklist", "root_goal"):
                        record = source.get(field)
                        if record is not None:
                            payload[field] = {
                                **record,
                                "thread_id": payload["source_session_id"],
                            }
            else:
                if active.output.mode == "native":
                    encoded = await self._native._seal_manual(json.dumps(active.output.schema))
                else:
                    encoded = await self._native._seal_manual()
                payload = json.loads(encoded)
            native_sealed = active is not None
            if active is not None:
                if payload is None:
                    pre_seal_refused = True
                    raise RuntimeError("The active submission is not a settled manual boundary")
                active.stopping = True
                self._model_api_key = None
            # Current launch options are authority. Saved options may hold MCP
            # endpoints or literal credentials.
            payload["agent_options"] = {}
            if self._codex_login_binding is not None:
                payload["codex_login_binding"] = self._codex_login_binding
                # Core adds creator identity to its live history. Portable
                # snapshots need only the binding to validate a fresh login.
                for captured in (payload, *payload["descendants"]):
                    for item in captured["history"]:
                        if item["type"] == "session_meta":
                            item["payload"].pop("creator_account_id", None)
                            item["payload"].pop("creator_user_id", None)
            if active is None:
                # Do not persist a temporary operation as an idle default.
                # Only selectors cross restore, never endpoints or credentials.
                payload["step_mcp_tools"] = (
                    [
                        {"server": server.name, "name": name}
                        for server in idle_mcp
                        for name in server.tools
                    ]
                    if self._default_tools is not None
                    else None
                )
            if self._default_tools is not None:
                assert self._opening_agent is not None
                python, mcp, _ = _split_tools(
                    self._default_tools,
                    self._opening_agent.programmatic_tool_calling,
                    available=self._bindings,
                    ceiling=self._opening_agent.tools,
                    environment=self._executor,
                    native_agents=self._native_agents,
                )
                default_refs = _selected_tool_refs(
                    self._default_tools,
                    python,
                    mcp,
                    programmatic_tool_calling=self._opening_agent.programmatic_tool_calling,
                    environment=self._executor,
                    native_agents=self._native_agents,
                )
                payload["agent_options"]["_sdk_default_tools"] = [
                    {"name": ref.name, "namespace": ref.namespace} for ref in default_refs
                ]
            output = self._default_output if active is None else active.output
            # Native formats 2/3 become SDK formats 4/5 when fingerprinted.
            payload["format_version"] += 2
            payload["output_version"] = 1
            payload["output_fingerprint"] = output.fingerprint
            payload["repair_turns"] = [
                {**turn, "parent_session_id": payload["source_session_id"]}
                for turn in self._repair_turns
            ]
            payload["repair_items"] = self._repair_items
            if active is not None and active.operation_refs is not None:
                payload["operation_tools"] = [
                    {"name": ref.name, "namespace": ref.namespace} for ref in active.operation_refs
                ]
            payload["network_policy_digest"] = (
                self._executor._network_policy_digest() if self._executor is not None else None
            )
            payload["executor_policy_digest"] = executor_policy_digest
            _redact_checkpoint_executor_receipts(payload)
            state = SessionState.from_json(json.dumps(payload))
            _check_checkpoint(state, self._instructions)
            raw = state.to_json()
            if active is not None:
                if state.format_version not in (5, 7):
                    raise NativeError("Native manual settlement omitted its continuation")
                self._sealed_checkpoint = raw
                # The native writer has already stopped. Do not send Interrupt.
                if not active.terminal.done():
                    active.terminal.set_result({"type": "manual_checkpoint"})
                interaction = InteractionRequired("Manual tool replies require a restored Session")
                interaction.state = state
                if not active.failure.done():
                    active.failure.set_result(interaction)
            return raw
        except BaseException as error:
            if active is not None:
                if (
                    pre_seal_refused or (active.restored_pending and not native_sealed)
                ) and not self._reader.done():
                    # A restored frame does not suspend a running Core writer.
                    # Native keeps its live receipts until the entire reseal
                    # snapshot is ready. A fresh refusal also retains its writer.
                    raise error from None
                error = (
                    NativeError("Native event stream ended before settlement")
                    if pre_seal_refused
                    else error
                )
                # Without a known live writer and reader, end the handle and
                # close the native owner.
                active.stopping = True
                self._broken = True
                if not active.terminal.done():
                    active.terminal.set_result({"type": "manual_checkpoint"})
                if not active.failure.done():
                    active.failure.set_result(error)
            raise error from None
        finally:
            self._sealing = False
            self._observations.notify()

    async def _aclose(self) -> None:
        """Stop new work and join the native runtime, reader, and callbacks.

        Agent.session owns this cleanup through its async context manager.
        New input stays disabled until the owners finish closing.
        For remote executors, close attempts termination of known processes.
        Acknowledgment does not prove physical exit or that remote effects ended.
        A failed live termination attempt is reported as the connection closes.
        After native shutdown, Core has only scheduled best-effort termination;
        cleanup preserves any original shutdown error without claiming an ACK.
        """
        self._check_loop()
        self._check_callback_operation("close its own Session")
        if self._close_task is None or (
            not self._closed
            and self._close_task.done()
            and (self._close_task.cancelled() or self._close_task.exception() is not None)
        ):
            self._closing = True
            self._discard_pending(TurnStoppedError("Session closed before queued admission"))
            self._close_task = _create_task(self._close())
        await _join(self._close_task)

    async def _exit_scope(self) -> None:
        # A Session may retire native work early from another task. Application
        # lease exit belongs to the scope that entered it, after native work joins.
        try:
            await self._aclose()
        except BaseException as error:
            if self._resources_retired and self._environment_lease is not None:
                await _close_after_error(error, self._release_provider_lease)
            raise
        if self._environment_lease is not None:
            await self._release_provider_lease()

    async def _close(self) -> None:
        try:
            if not self._resources_retired:
                await self._close_session()
        except BaseException as error:
            if self._resources_retired and self._environment_lease is None:
                await _close_after_error(error, self._release_provider_lease)
            else:
                self._closed = False
            raise
        if self._environment_lease is None:
            await self._release_provider_lease()

    async def _release_provider_lease(self) -> None:
        if not self._resources_retired:
            raise RuntimeError("Session resources have not retired")
        self._closed = False
        if self._environment_lease is not None:
            lease, self._environment_lease = self._environment_lease, None
            try:
                await _release_environment(lease)
            except BaseException as error:
                await _close_after_error(error, self._retire_provider)
                raise
        await _join(asyncio.create_task(self._retire_provider()))

    async def _retire_provider(self) -> None:
        if self._provider_lease is not None:
            self._provider_lease.release()
            self._provider_lease = None
        if self._owned_provider is not None:
            await self._owned_provider.aclose()
            self._owned_provider = None
        self._model_api_key = None
        self._native_options = None
        self._parent_trace = None
        self._closed = True

    async def _close_session(self) -> None:
        await self._retire_url_completions()
        # Capture before cancelling native work. Done callbacks remove owners
        # from the gates while shutdown runs; close must still join their results.
        interrupts = (*self._graph_interrupts, *self._root_interrupts)
        state = self._active
        previous_children = set(self._child_callbacks)
        admissions = tuple(self._child_admissions)
        children = {
            key: child
            for key, child in self._child_callbacks.items()
            if child.busy or (child.policy_stop and child.cleanup_error is not None)
        }
        for child in self._child_callbacks.values():
            child.stop_continuation()
        errors: list[BaseException] = []
        native_stop: asyncio.Future[None] | None = None
        graph_stopped = False
        try:
            engine_closed = False
            try:
                if self._mutation is not None:
                    await asyncio.gather(self._mutation, return_exceptions=True)
                if self._checkpoint_task is not None:
                    await asyncio.gather(self._checkpoint_task, return_exceptions=True)
                if state is None or not state.restored_pending:
                    try:
                        # Reserve the graph fence synchronously, before closing
                        # interception releases any native result to Core.
                        native_stop = asyncio.ensure_future(self._native._interrupt_graph("force"))
                    except BaseException as error:
                        errors.append(error)
                try:
                    if state is not None:
                        assert state.task is not None
                        should_cancel = not state.stopping and (
                            state.handle is None or not state.failure.done()
                        )
                        state.stopping = True
                        if should_cancel:
                            cancel_once(state.task)
                        # The owner interrupts native sampling before joining
                        # callbacks. Revoking their decisions earlier can let
                        # Core continue sampling while close waits for them.
                        await state.task
                        if state.cleanup_error is not None:
                            raise state.cleanup_error
                finally:
                    if self._interception is not None:
                        await self._interception.close()
            finally:
                try:
                    try:
                        try:
                            if native_stop is not None:
                                await native_stop
                        finally:
                            # Do not close the native reader under an accepted
                            # admission, including stop-reservation failures.
                            if admissions:
                                await asyncio.gather(*admissions, return_exceptions=True)
                        if native_stop is not None and admissions:
                            await self._native._interrupt_graph("force")
                        graph_stopped = native_stop is not None
                    except BaseException as error:
                        errors.append(error)
                    # Include children first observed during close, without
                    # replaying already-completed historical failures.
                    children.update(
                        (key, child)
                        for key, child in self._child_callbacks.items()
                        if key not in previous_children or child.busy
                    )
                    for child in self._child_callbacks.values():
                        child.stopping = True
                        if not child.observed.done():
                            child.observed.set_result(None)
                        for task in child.callbacks:
                            cancel_once(task)
                        if not child.terminal.done():
                            child.terminal.set_result(None)
                    await asyncio.gather(
                        *(child.completion for child in self._child_callbacks.values()),
                        return_exceptions=True,
                    )
                    continuation_results = await asyncio.gather(
                        *(
                            child.continuation
                            for child in self._child_callbacks.values()
                            if child.continuation is not None
                        ),
                        return_exceptions=True,
                    )
                    errors.extend(
                        result
                        for result in continuation_results
                        if isinstance(result, BaseException)
                    )
                    for child in children.values():
                        if child.cleanup_error is not None:
                            errors.append(child.cleanup_error)
                    if self._mcp_reader is not None:
                        cancel_once(self._mcp_reader)
                        await self._mcp_reader
                    # Successful force settlement releases graceful stops blocked
                    # on live/manual work. Join their exact owners while native
                    # receipts can still be processed. Failed force still needs
                    # the existing close-first recovery and retained-error join.
                    if graph_stopped and (state is None or state.deferred_close is None):
                        await asyncio.gather(*interrupts, return_exceptions=True)
                    try:
                        if self._executor is not None and not self._executor_termination_attempted:
                            self._executor_termination_attempted = True
                            await self._native._terminate_executor_processes()
                    except BaseException as error:
                        await _close_after_error(error, self._native.close)
                        if state is not None and state.deferred_close is not None:
                            state.retain_cleanup_error(error)
                        raise
                    try:
                        await self._native.close()
                    except BaseException as error:
                        if state is not None and state.deferred_close is not None:
                            state.retain_cleanup_error(error)
                        raise
                finally:
                    # Settle before joining interrupts or the reader: they may
                    # themselves be waiting on this unreceived admission.
                    if state is not None and state.deferred_close is not None:
                        has_input, turn_error, outcome = state.deferred_close
                        state.deferred_close = None
                        self._settle_submission(state, has_input, turn_error, outcome)
                    results = await asyncio.gather(*interrupts, return_exceptions=True)
                    for result in results:
                        # Closing an unadmitted root may cancel its accepted future.
                        # Preserve real ownership/termination failures from both sets.
                        if isinstance(result, BaseException) and not isinstance(
                            result, asyncio.CancelledError
                        ):
                            errors.append(result)
                    try:
                        await self._engine.close()
                        engine_closed = True
                    finally:
                        # A failed shutdown leaves the native event source live.
                        # Retire its existing read without waiting for a new event.
                        if not engine_closed:
                            cancel_once(self._reader)
                        try:
                            await self._reader
                        finally:
                            self._observations.finish("closed")
                            if engine_closed:
                                if self._home is not None:
                                    await _finish_native_home_cleanup(self._home.cleanup)
                                self._resources_retired = (
                                    self._reader.done()
                                    and (self._mcp_reader is None or self._mcp_reader.done())
                                    and (
                                        self._interception is None
                                        or self._interception.reader is None
                                        or self._interception.reader.done()
                                    )
                                )

        except BaseException as error:
            # Keep the original close failure primary, then its stop-owner errors.
            errors.insert(0, error)
        if errors:
            first = errors[0]
            for remaining_error in errors[1:]:
                if remaining_error is not first:
                    _retain_cleanup_error(
                        first, remaining_error, _error_contexts(first.__context__)
                    )
            raise first
