"""Private state for one exact native submission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ._lifecycle import _error_contexts, _retain_cleanup_error
from .usage import _UsageCollector

if TYPE_CHECKING:
    from ._model_retry import _ModelFailure
    from ._model_run import _ModelRun
    from ._trace_context import TraceContext
    from .agent import Agent
    from .guardrails import GuardrailRecord
    from .history import _InputStoragePolicy
    from .input import UserInput
    from .manual import _NativeQuestion
    from .mcp import _MCP
    from .observation import ToolStarted
    from .prepared_request import _PrepareRequest
    from .retry import RetryPolicy
    from .schema import _PreparedOutput
    from .session import (
        Completed,
        _BeforeStep,
        _CallbackState,
        _NativeCallback,
        _RunBudget,
        _ToolCompletionProposal,
    )
    from .state import ApplicationToolOutput
    from .telemetry import _CapturedTrace
    from .tools import ToolRef
    from .turn import Turn, _SubmissionRequestAttempts


def _future() -> asyncio.Future[Any]:
    future = asyncio.get_running_loop().create_future()
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return future


@dataclass
class _Submission:
    output: _PreparedOutput
    run_usage: _UsageCollector = field(default_factory=_UsageCollector)
    model_run_usage: _UsageCollector = field(default_factory=_UsageCollector, repr=False)
    submission_request_attempts: _SubmissionRequestAttempts | None = None
    observation_after: int = 0
    accepted: asyncio.Future[Turn[Any]] = field(default_factory=_future)
    completion: asyncio.Future[Any] = field(default_factory=_future)
    terminal: asyncio.Future[Any] = field(default_factory=_future)
    failure: asyncio.Future[Any] = field(default_factory=_future)
    model_failure: _ModelFailure | None = field(default=None, repr=False)
    model_run: _ModelRun | None = field(default=None, repr=False)
    handle: Turn[Any] | None = None
    issued: Turn[Any] | None = None
    task: asyncio.Task[None] | None = None
    callbacks: dict[asyncio.Task[None], _NativeCallback | None] = field(default_factory=dict)
    function_admission: asyncio.Semaphore | None = None
    tool_completion: _ToolCompletionProposal | None = field(default=None, repr=False)
    terminal_observations: dict[str, ToolStarted] = field(default_factory=dict, repr=False)
    pending_application_outputs: dict[str, ApplicationToolOutput] = field(
        default_factory=dict, repr=False
    )
    completion_continuations: int = 0
    guardrail_results: list[GuardrailRecord] = field(default_factory=list)
    guardrail_input: UserInput | None = field(default=None, repr=False)
    original_input: UserInput | None = field(default=None, repr=False)
    input_guardrails_passed: bool | None = None
    input_storage_policy: tuple[_InputStoragePolicy, UserInput] | None = field(
        default=None, repr=False
    )
    input_guardrails_started: bool = False
    input_guardrails_task: asyncio.Task[None] | None = field(default=None, repr=False)
    calls: set[str] = field(default_factory=set)
    mcp_calls: dict[str, str] = field(default_factory=dict)
    mcp_names: dict[tuple[str, str], ToolRef] = field(default_factory=dict, repr=False)
    mcp_call_names: dict[str, ToolRef] = field(default_factory=dict, repr=False)
    step_mcp: tuple[_MCP[Any], ...] | None = field(default=None, repr=False)
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
    repair_attempt_count: int = 0
    # A one-element tuple distinguishes an accepted None from no accepted value.
    accepted_output: tuple[Any] | None = None
    before_step: _BeforeStep[Any] | None = None
    prepare_request: _PrepareRequest | None = None
    preparation_index: int = 0
    step_index: int = 0
    budget: _RunBudget | None = None
    model_override: str | None = None
    model_settings_override: dict[str, Any] | None = None
    retry_policy_override: RetryPolicy | None = field(default=None, repr=False)
    raise_on_unknown_tool: bool = False
    operation_slots: tuple[int, ...] | None = None
    operation_mcp: tuple[_MCP[Any], ...] | None = None
    operation_refs: tuple[ToolRef, ...] | None = None
    writable_roots: tuple[str, ...] | None = None
    parent_trace: TraceContext | None = field(default=None, repr=False)
    tracing: _CapturedTrace | None = field(default=None, repr=False)
    realtime_tool: tuple[str, str, tuple[str, ...], tuple[str, ...]] | None = None
    output_override: bool = False
    operation_declarations: tuple[Any, ...] | None = None
    operation_agent: Agent[Any, Any] | None = None
    operation_declaration_source: dict[str, Any] | None = None

    def add_callback(
        self, task: asyncio.Task[None], receipt: _NativeCallback | None = None
    ) -> None:
        self.callbacks[task] = receipt
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
