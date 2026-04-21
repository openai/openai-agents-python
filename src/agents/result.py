from __future__ import annotations

import abc
import asyncio
import copy
import weakref
from collections.abc import AsyncIterator
from dataclasses import InitVar, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from .agent import Agent
from .agent_output import AgentOutputSchemaBase
from .exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    RunErrorDetails,
)
from .guardrail import InputGuardrailResult, OutputGuardrailResult
from .items import (
    ItemHelpers,
    ModelResponse,
    RunItem,
    ToolApprovalItem,
    TResponseInputItem,
)
from .logger import logger
from .run_context import RunContextWrapper
from .run_internal.items import run_items_to_input_items
from .run_internal.run_steps import (
    NextStepInterruption,
    ProcessedResponse,
    QueueCompleteSentinel,
)
from .run_state import RunState
from .stream_events import StreamEvent
from .tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from .tracing import Trace
from .tracing.traces import TraceState
from .util._pretty_print import (
    pretty_print_result,
    pretty_print_run_result_streaming,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .sandbox.session.base_sandbox_session import BaseSandboxSession

T = TypeVar("T")


@dataclass(frozen=True)
class AgentToolInvocation:
    """Immutable metadata about a nested agent-tool invocation."""

    tool_name: str
    """The nested tool name exposed to the model."""

    tool_call_id: str
    """The tool call ID for the nested invocation."""

    tool_arguments: str
    """The raw JSON arguments for the nested invocation."""


def _populate_state_from_result(
    state: RunState[Any],
    result: RunResultBase,
    *,
    current_turn: int,
    last_processed_response: ProcessedResponse | None,
    current_turn_persisted_item_count: int,
    tool_use_tracker_snapshot: dict[str, list[str]],
    conversation_id: str | None = None,
    previous_response_id: str | None = None,
    auto_previous_response_id: bool = False,
) -> RunState[Any]:
    """Populate a RunState with common fields from a RunResult."""
    state._current_agent = result.last_agent
    model_input_items = getattr(result, "_model_input_items", None)
    if isinstance(model_input_items, list):
        state._generated_items = list(model_input_items)
    else:
        state._generated_items = result.new_items
    state._session_items = list(result.new_items)
    state._model_responses = result.raw_responses
    state._input_guardrail_results = result.input_guardrail_results
    state._output_guardrail_results = result.output_guardrail_results
    state._tool_input_guardrail_results = result.tool_input_guardrail_results
    state._tool_output_guardrail_results = result.tool_output_guardrail_results
    state._last_processed_response = last_processed_response
    state._current_turn = current_turn
    state._current_turn_persisted_item_count = current_turn_persisted_item_count
    state.set_tool_use_tracker_snapshot(tool_use_tracker_snapshot)
    state._conversation_id = conversation_id
    state._previous_response_id = previous_response_id
    state._auto_previous_response_id = auto_previous_response_id
    source_state = getattr(result, "_state", None)
    if isinstance(source_state, RunState):
        state._generated_prompt_cache_key = source_state._generated_prompt_cache_key
    else:
        state._generated_prompt_cache_key = getattr(result, "_generated_prompt_cache_key", None)
    state._reasoning_item_id_policy = getattr(result, "_reasoning_item_id_policy", None)

    interruptions = list(getattr(result, "interruptions", []))
    if interruptions:
        state._current_step = NextStepInterruption(interruptions=interruptions)

    trace_state = getattr(result, "_trace_state", None)
    if trace_state is None:
        trace_state = TraceState.from_trace(getattr(result, "trace", None))
    state._trace_state = copy.deepcopy(trace_state) if trace_state else None
    sandbox_resume_state = getattr(result, "_sandbox_resume_state", None)
    if isinstance(sandbox_resume_state, dict):
        state._sandbox = copy.deepcopy(sandbox_resume_state)
    else:
        state._sandbox = None

    return state


ToInputListMode = Literal["preserve_all", "normalized"]


def _input_items_for_result(
    result: RunResultBase,
    *,
    mode: ToInputListMode,
    reasoning_item_id_policy: Literal["preserve", "omit"] | None,
) -> list[TResponseInputItem]:
    """Return input items for the requested result view.

    ``preserve_all`` keeps the full converted history from ``new_items``. ``normalized`` returns
    the canonical continuation input when handoff filtering rewrote model history, otherwise it
    falls back to the same converted history.
    """
    session_items = run_items_to_input_items(result.new_items, reasoning_item_id_policy)
    if mode == "preserve_all":
        return session_items
    if mode != "normalized":
        raise ValueError(f"Unsupported to_input_list mode: {mode}")
    if not getattr(result, "_replay_from_model_input_items", False):
        # Most runs never rewrite continuation history, so normalized stays identical to the
        # historical preserve-all view unless the runner explicitly marked a divergence.
        return session_items

    model_input_items = getattr(result, "_model_input_items", None)
    if not isinstance(model_input_items, list):
        return session_items

    # When the runner marks a divergence, generated_items already reflect the continuation input
    # chosen for the next local run after applying handoff/input filtering.
    return run_items_to_input_items(model_input_items, reasoning_item_id_policy)


def _starting_agent_for_state(result: RunResultBase) -> Agent[Any]:
    """Return the root agent graph that should seed RunState identity resolution."""
    state = getattr(result, "_state", None)
    starting_agent = getattr(state, "_starting_agent", None)
    if isinstance(starting_agent, Agent):
        return starting_agent

    stored_starting_agent = getattr(result, "_starting_agent_for_state", None)
    if isinstance(stored_starting_agent, Agent):
        return stored_starting_agent

    return result.last_agent


@dataclass
class RunResultBase(abc.ABC):
    input: str | list[TResponseInputItem]
    """The original input items i.e. the items before run() was called. This may be a mutated
    version of the input, if there are handoff input filters that mutate the input.
    """

    new_items: list[RunItem]
    """The new items generated during the agent run. These include things like new messages, tool
    calls and their outputs, etc.
    """

    raw_responses: list[ModelResponse]
    """The raw LLM responses generated by the model during the agent run."""

    final_output: Any
    """The output of the last agent."""

    input_guardrail_results: list[InputGuardrailResult]
    """Guardrail results for the input messages."""

    output_guardrail_results: list[OutputGuardrailResult]
    """Guardrail results for the final output of the agent."""

    tool_input_guardrail_results: list[ToolInputGuardrailResult]
    """Tool input guardrail results from all tools executed during the run."""

    tool_output_guardrail_results: list[ToolOutputGuardrailResult]
    """Tool output guardrail results from all tools executed during the run."""

    context_wrapper: RunContextWrapper[Any]
    """The context wrapper for the agent run."""

    _trace_state: TraceState | None = field(default=None, init=False, repr=False)
    """Serialized trace metadata captured during the run."""
    _replay_from_model_input_items: bool = field(default=False, init=False, repr=False)
    """Whether replay helpers should prefer `_model_input_items` over `new_items`.

    This is only set when the runner preserved extra session history items that should not be
    replayed into the next local run, such as nested handoff history or filtered handoff input.
    """
    _sandbox_resume_state: dict[str, object] | None = field(default=None, init=False, repr=False)
    """Serialized sandbox session state captured during the run."""
    _sandbox_session: BaseSandboxSession | None = field(default=None, init=False, repr=False)
    """Live sandbox session attached to this run result when sandbox execution is enabled."""
    _starting_agent_for_state: Agent[Any] | None = field(default=None, init=False, repr=False)
    """Root agent graph used when converting the result back into RunState."""
    _generated_prompt_cache_key: str | None = field(default=None, init=False, repr=False)
    """SDK-generated prompt cache key captured during the run."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        # RunResult objects are runtime values; schema generation should treat them as instances
        # instead of recursively traversing internal dataclass annotations.
        return core_schema.is_instance_schema(cls)

    @property
    @abc.abstractmethod
    def last_agent(self) -> Agent[Any]:
        """The last agent that was run."""

    def release_agents(self, *, release_new_items: bool = True) -> None:
        """
        Release strong references to agents held by this result. After calling this method,
        accessing `item.agent` or `last_agent` may return `None` if the agent has been garbage
        collected. Callers can use this when they are done inspecting the result and want to
        eagerly drop any associated agent graph.
        """
        if release_new_items:
            for item in self.new_items:
                release = getattr(item, "release_agent", None)
                if callable(release):
                    release()
        self._release_last_agent_reference()

    def __del__(self) -> None:
        try:
            # Fall back to releasing agents automatically in case the caller never invoked
            # `release_agents()` explicitly so GC of the RunResult drops the last strong reference.
            # We pass `release_new_items=False` so RunItems that the user intentionally keeps
            # continue exposing their originating agent until that agent itself is collected.
            self.release_agents(release_new_items=False)
        except Exception:
            # Avoid raising from __del__.
            pass

    @abc.abstractmethod
    def _release_last_agent_reference(self) -> None:
        """Release stored agent reference specific to the concrete result type."""

    def final_output_as(self, cls: type[T], raise_if_incorrect_type: bool = False) -> T:
        """A convenience method to cast the final output to a specific type. By default, the cast
        is only for the typechecker. If you set `raise_if_incorrect_type` to True, we'll raise a
        TypeError if the final output is not of the given type.

        Args:
            cls: The type to cast the final output to.
            raise_if_incorrect_type: If True, we'll raise a TypeError if the final output is not of
                the given type.

        Returns:
            The final output casted to the given type.
        """
        if raise_if_incorrect_type and not isinstance(self.final_output, cls):
            raise TypeError(f"Final output is not of type {cls.__name__}")

        return cast(T, self.final_output)

    def to_input_list(
        self,
        *,
        mode: ToInputListMode = "preserve_all",
    ) -> list[TResponseInputItem]:
        """Create an input-item view of this run.

        ``mode="preserve_all"`` keeps the historical behavior of converting ``new_items`` into a
        full plain-item history. ``mode="normalized"`` prefers the canonical continuation input
        when handoff filtering rewrote model history, while remaining identical for ordinary runs.
        """
        original_items: list[TResponseInputItem] = ItemHelpers.input_to_new_input_list(self.input)
        reasoning_item_id_policy = getattr(self, "_reasoning_item_id_policy", None)
        replay_items = _input_items_for_result(
            self,
            mode=mode,
            reasoning_item_id_policy=reasoning_item_id_policy,
        )
        return original_items + replay_items

    @property
    def agent_tool_invocation(self) -> AgentToolInvocation | None:
        """Immutable metadata for results produced by `Agent.as_tool()`.

        Returns `None` for ordinary top-level runs.
        """
        from .tool_context import ToolContext

        if not isinstance(self.context_wrapper, ToolContext):
            return None

        return AgentToolInvocation(
            tool_name=self.context_wrapper.tool_name,
            tool_call_id=self.context_wrapper.tool_call_id,
            tool_arguments=self.context_wrapper.tool_arguments,
        )

    @property
    def last_response_id(self) -> str | None:
        """Convenience method to get the response ID of the last model response."""
        if not self.raw_responses:
            return None

        return self.raw_responses[-1].response_id


@dataclass
class RunResult(RunResultBase):
    _last_agent: Agent[Any]
    _last_agent_ref: weakref.ReferenceType[Agent[Any]] | None = field(
        init=False,
        repr=False,
        default=None,
    )
    _last_processed_response: ProcessedResponse | None = field(default=None, repr=False)
    """The last processed model response. This is needed for resuming from interruptions."""
    _tool_use_tracker_snapshot: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _current_turn_persisted_item_count: int = 0
    """Number of items from new_items already persisted to session for the
    current turn."""
    _current_turn: int = 0
    """The current turn number. This is preserved when converting to RunState."""
    _model_input_items: list[RunItem] = field(default_factory=list, repr=False)
    """Filtered items used to build model input when resuming runs."""
    _original_input: str | list[TResponseInputItem] | None = field(default=None, repr=False)
    """The original input for the current run segment.
    This is updated when handoffs or resume logic replace the input history, and used by to_state()
    to preserve the correct originalInput when serializing state."""
    _conversation_id: str | None = field(default=None, repr=False)
    """Conversation identifier for server-managed runs."""
    _previous_response_id: str | None = field(default=None, repr=False)
    """Response identifier returned by the server for the last turn."""
    _auto_previous_response_id: bool = field(default=False, repr=False)
    """Whether automatic previous response tracking was enabled."""
    _reasoning_item_id_policy: Literal["preserve", "omit"] | None = field(
        default=None, init=False, repr=False
    )
    """How reasoning IDs should be represented when converting to input history."""
    max_turns: int = 10
    """The maximum number of turns allowed for this run."""
    interruptions: list[ToolApprovalItem] = field(default_factory=list)
    """Pending tool approval requests (interruptions) for this run."""

    def __post_init__(self) -> None:
        self._last_agent_ref = weakref.ref(self._last_agent)

    @property
    def last_agent(self) -> Agent[Any]:
        """The last agent that was run."""
        agent = cast("Agent[Any] | None", self.__dict__.get("_last_agent"))
        if agent is not None:
            return agent
        if self._last_agent_ref:
            agent = self._last_agent_ref()
            if agent is not None:
                return agent
        raise AgentsException("Last agent reference is no longer available.")

    def _release_last_agent_reference(self) -> None:
        agent = cast("Agent[Any] | None", self.__dict__.get("_last_agent"))
        if agent is None:
            return
        self._last_agent_ref = weakref.ref(agent)
        # Preserve dataclass field so repr/asdict continue to succeed.
        self.__dict__["_last_agent"] = None

    def to_state(self) -> RunState[Any]:
        """Create a RunState from this result to resume execution.

        This is useful when the run was interrupted (e.g., for tool approval). You can
        approve or reject the tool calls on the returned state, then pass it back to
        `Runner.run()` to continue execution.

        Returns:
            A RunState that can be used to resume the run.

        Example:
            ```python
            # Run agent until it needs approval
            result = await Runner.run(agent, "Use the delete_file tool")

            if result.interruptions:
                # Approve the tool call
                state = result.to_state()
                state.approve(result.interruptions[0])

                # Resume the run
                result = await Runner.run(agent, state)
            ```
        """
        # Create a RunState from the current result
        original_input_for_state = getattr(self, "_original_input", None)
        state = RunState(
            context=self.context_wrapper,
            original_input=original_input_for_state
            if original_input_for_state is not None
            else self.input,
            starting_agent=_starting_agent_for_state(self),
            max_turns=self.max_turns,
        )

        return _populate_state_from_result(
            state,
            self,
            current_turn=self._current_turn,
            last_processed_response=self._last_processed_response,
            current_turn_persisted_item_count=self._current_turn_persisted_item_count,
            tool_use_tracker_snapshot=self._tool_use_tracker_snapshot,
            conversation_id=self._conversation_id,
            previous_response_id=self._previous_response_id,
            auto_previous_response_id=self._auto_previous_response_id,
        )

    def __str__(self) -> str:
        return pretty_print_result(self)


@dataclass
class RunResultStreaming(RunResultBase):
    """The result of an agent run in streaming mode. You can use the `stream_events` method to
    receive semantic events as they are generated.

    The streaming method will raise:
    - A MaxTurnsExceeded exception if the agent exceeds the max_turns limit.
    - A GuardrailTripwireTriggered exception if a guardrail is tripped.
    """

    current_agent: Agent[Any]
    """The current agent that is running."""

    current_turn: int
    """The current turn number."""

    max_turns: int
    """The maximum number of turns the agent can run for."""

    final_output: Any
    """The final output of the agent. This is None until the agent has finished running."""

    _current_agent_output_schema: AgentOutputSchemaBase | None = field(repr=False)

    trace: Trace | None = field(repr=False)

    is_complete: bool = False
    """Whether the agent has finished running."""

    _current_agent_ref: weakref.ReferenceType[Agent[Any]] | None = field(
        init=False,
        repr=False,
        default=None,
    )

    _model_input_items: list[RunItem] = field(default_factory=list, repr=False)
    """Filtered items used to build model input between streaming turns."""

    # Queues that the background run_loop writes to
    _event_queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel] = field(
        default_factory=asyncio.Queue, repr=False
    )
    _input_guardrail_queue: asyncio.Queue[InputGuardrailResult] = field(
        default_factory=asyncio.Queue, repr=False
    )

    # Store the asyncio tasks that we're waiting on
    run_loop_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _input_guardrails_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _triggered_input_guardrail_result: InputGuardrailResult | None = field(default=None, repr=False)
    _output_guardrails_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _stored_exception: Exception | None = field(default=None, repr=False)
    _cancel_mode: Literal["none", "immediate", "after_turn"] = field(default="none", repr=False)
    _last_processed_response: ProcessedResponse | None = field(default=None, repr=False)
    """The last processed model response. This is needed for resuming from interruptions."""
    interruptions: list[ToolApprovalItem] = field(default_factory=list)
    """Pending tool approval requests (interruptions) for this run."""
    _waiting_on_event_queue: bool = field(default=False, repr=False)

    _current_turn_persisted_item_count: int = 0
    """Number of items from new_items already persisted to session for the
    current turn."""

    _stream_input_persisted: bool = False
    """Whether the input has been persisted to the session. Prevents double-saving."""

    _original_input_for_persistence: list[TResponseInputItem] | None = None
    """Original turn input before session history was merged, used for
    persistence (matches JS sessionInputOriginalSnapshot)."""

    _max_turns_handled: bool = field(default=False, repr=False)

    _original_input: str | list[TResponseInputItem] | None = field(default=None, repr=False)
    """The original input from the first turn. Unlike `input`, this is never updated during the run.
    Used by to_state() to preserve the correct originalInput when serializing state."""
    _tool_use_tracker_snapshot: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _state: Any = field(default=None, repr=False)
    """Internal reference to the RunState for streaming results."""
    _conversation_id: str | None = field(default=None, repr=False)
    """Conversation identifier for server-managed runs."""
    _previous_response_id: str | None = field(default=None, repr=False)
    """Response identifier returned by the server for the last turn."""
    _auto_previous_response_id: bool = field(default=False, repr=False)
    """Whether automatic previous response tracking was enabled."""
    _reasoning_item_id_policy: Literal["preserve", "omit"] | None = field(
        default=None, init=False, repr=False
    )
    """How reasoning IDs should be represented when converting to input history."""
    _run_impl_task: InitVar[asyncio.Task[Any] | None] = None
    _sandbox_cleanup: Callable[[], Awaitable[None]] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _sandbox_cleanup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _sandbox_cleanup_callback_registered: bool = field(default=False, init=False, repr=False)

    def __post_init__(self, _run_impl_task: asyncio.Task[Any] | None) -> None:
        self._current_agent_ref = weakref.ref(self.current_agent)
        # Store the original input at creation time (it will be set via input field)
        if self._original_input is None:
            self._original_input = self.input
        # Compatibility shim: accept legacy `_run_impl_task` constructor keyword.
        if self.run_loop_task is None and _run_impl_task is not None:
            self.run_loop_task = _run_impl_task

    @property
    def last_agent(self) -> Agent[Any]:
        """The last agent that was run. Updates as the agent run progresses, so the true last agent
        is only available after the agent run is complete.
        """
        agent = cast("Agent[Any] | None", self.__dict__.get("current_agent"))
        if agent is not None:
            return agent
        if self._current_agent_ref:
            agent = self._current_agent_ref()
            if agent is not None:
                return agent
        raise AgentsException("Last agent reference is no longer available.")

    def _release_last_agent_reference(self) -> None:
        agent = cast("Agent[Any] | None", self.__dict__.get("current_agent"))
        if agent is None:
            return
        self._current_agent_ref = weakref.ref(agent)
        # Preserve dataclass field so repr/asdict continue to succeed.
        self.__dict__["current_agent"] = None

    async def _run_sandbox_cleanup(self) -> None:
        sandbox_cleanup = self._sandbox_cleanup
        if sandbox_cleanup is None:
            return

        task = self._sandbox_cleanup_task
        if task is None:

            async def _cleanup_once() -> None:
                try:
                    await sandbox_cleanup()
                except Exception as error:
                    logger.warning(
                        "Failed to clean up sandbox resources after streamed run: %s", error
                    )

            task = asyncio.create_task(_cleanup_once())
            self._sandbox_cleanup_task = task

        await task

    def ensure_sandbox_cleanup_on_completion(self) -> None:
        if (
            self._sandbox_cleanup is None
            or self.run_loop_task is None
            or self._sandbox_cleanup_callback_registered
        ):
            return

        original_task = self.run_loop_task
        self._sandbox_cleanup_callback_registered = True
        original_task.add_done_callback(
            lambda _task: asyncio.create_task(self._run_sandbox_cleanup())
        )

        async def _await_run_and_cleanup() -> Any:
            try:
                result = await original_task
            except asyncio.CancelledError:
                if not original_task.done():
                    original_task.cancel()
                raise
            except Exception:
                await self._run_sandbox_cleanup()
                raise

            await self._run_sandbox_cleanup()
            return result

        self.run_loop_task = asyncio.create_task(_await_run_and_cleanup())

    @property
    def run_loop_exception(self) -> BaseException | None:
        """The exception raised by the background run loop, if any.

        When the run loop fails before producing stream events (for example during early
        sandbox initialisation), the exception may not be re-raised through
        :meth:`stream_events`. This property gives callers a reliable way to check for
        silent failures after consuming the stream:

        .. code-block:: python

            result = Runner.run_streamed(agent, "hello")
            async for event in result.stream_events():
                pass
            if result.run_loop_exception:
                raise result.run_loop_exception

        Returns ``None`` if the run loop completed without error, has not yet finished,
        or was cancelled.
        """
        task = self.run_loop_task
        if task is None or not task.done() or task.cancelled():
            return None
        return task.exception()

    def cancel(self, mode: Literal["immediate", "after_turn"] = "immediate") -> None:
        """Cancel the streaming run.

        Args:
            mode: Cancellation strategy:
                - "immediate": Stop immediately, cancel all tasks, clear queues (default)
                - "after_turn": Complete current turn gracefully before stopping
                    * Allows LLM response to finish
                    * Executes pending tool calls
                    * Saves session state properly
                    * Tracks usage accurately
                    * Stops before next turn begins

        Example:
            ```python
            result = Runner.run_streamed(agent, "Task", session=session)

            async for event in result.stream_events():
                if user_interrupted():
                    result.cancel(mode="after_turn")  # Graceful
                    # result.cancel()  # Immediate (default)
            ```

        Note: After calling cancel(), you should continue consuming stream_events()
        to allow the cancellation to complete properly.
        """
        # Store the cancel mode for the background task to check
        self._cancel_mode = mode

        if mode == "immediate":
            # Existing behavior - immediate shutdown
            self._cleanup_tasks()  # Cancel all running tasks
            self.is_complete = True  # Mark the run as complete to stop event streaming

            while not self._input_guardrail_queue.empty():
                self._input_guardrail_queue.get_nowait()

            # Unblock any streamers waiting on the event queue.
            self._event_queue.put_nowait(QueueCompleteSentinel())
            if not self._waiting_on_event_queue:
                self._drain_event_queue()

        elif mode == "after_turn":
            # Soft cancel - just set the flag
            # The streaming loop will check this and stop gracefully
            # Don't call _cleanup_tasks() or clear queues yet
            pass

    async def stream_events(self) -> AsyncIterator[StreamEvent]:
        """Stream deltas for new items as they are generated. We're using the types from the
        OpenAI Responses API, so these are semantic events: each event has a `type` field that
        describes the type of the event, along with the data for that event.

        This will raise:
        - A MaxTurnsExceeded exception if the agent exceeds the max_turns limit.
        - A GuardrailTripwireTriggered exception if a guardrail is tripped.
        """
        cancelled = False
        try:
            while True:
                self._check_errors()
                should_drain_queued_events = isinstance(self._stored_exception, MaxTurnsExceeded)
                if self._stored_exception and (
                    not should_drain_queued_events or self._event_queue.empty()
                ):
                    logger.debug("Breaking due to stored exception")
                    self.is_complete = True
                    break

                if self.is_complete and self._event_queue.empty():
                    break

                try:
                    self._waiting_on_event_queue = True
                    item = await self._event_queue.get()
                except asyncio.CancelledError:
                    cancelled = True
                    self.cancel()
                    raise
                finally:
                    self._waiting_on_event_queue = False

                if isinstance(item, QueueCompleteSentinel):
                    # Await input guardrails if they are still running, so late
                    # exceptions are captured.
                    await self._await_task_safely(self._input_guardrails_task)

                    self._event_queue.task_done()

                    # Check for errors, in case the queue was completed
                    # due to an exception
                    self._check_errors()
                    break

                yield item
                self._event_queue.task_done()
        finally:
            try:
                if cancelled:
                    # Cancellation should return promptly, so avoid waiting on long-running tasks.
                    # Tasks have already been cancelled above.
                    self._cleanup_tasks()
                else:
                    # Ensure main execution completes before cleanup to avoid race conditions
                    # with session operations.
                    await self._await_task_safely(self.run_loop_task)
                    # Re-check for exceptions now that the run loop has fully settled.
                    # _await_task_safely swallows exceptions; without this call, a run-loop
                    # failure that races past the sentinel (e.g. early sandbox failures) would
                    # be silently lost instead of surfaced via _stored_exception.
                    self._check_errors()
                    # Safely terminate all background tasks after main execution has finished.
                    self._cleanup_tasks()

                if not cancelled:
                    await self._run_sandbox_cleanup()
            finally:
                # Allow any pending callbacks (e.g., cancellation handlers) to enqueue their
                # completion sentinels before we clear the queues for observability.
                await asyncio.sleep(0)

                # Drain queues so callers observing internal state see them empty after completion.
                self._drain_event_queue()
                self._drain_input_guardrail_queue()

        if self._stored_exception:
            raise self._stored_exception

    def _create_error_details(self) -> RunErrorDetails:
        """Return a `RunErrorDetails` object considering the current attributes of the class."""
        return RunErrorDetails(
            input=self.input,
            new_items=self.new_items,
            raw_responses=self.raw_responses,
            last_agent=self.current_agent,
            context_wrapper=self.context_wrapper,
            input_guardrail_results=self.input_guardrail_results,
            output_guardrail_results=self.output_guardrail_results,
        )

    def _check_errors(self):
        if self.current_turn > self.max_turns and not self._max_turns_handled:
            max_turns_exc = MaxTurnsExceeded(f"Max turns ({self.max_turns}) exceeded")
            max_turns_exc.run_data = self._create_error_details()
            self._stored_exception = max_turns_exc

        # Fetch all the completed guardrail results from the queue and raise if needed
        while not self._input_guardrail_queue.empty():
            guardrail_result = self._input_guardrail_queue.get_nowait()
            if guardrail_result.output.tripwire_triggered:
                tripwire_exc = InputGuardrailTripwireTriggered(guardrail_result)
                tripwire_exc.run_data = self._create_error_details()
                self._stored_exception = tripwire_exc

        # Check the tasks for any exceptions
        if self.run_loop_task and self.run_loop_task.done():
            if not self.run_loop_task.cancelled():
                run_impl_exc = self.run_loop_task.exception()
                if run_impl_exc and isinstance(run_impl_exc, Exception):
                    if isinstance(run_impl_exc, AgentsException) and run_impl_exc.run_data is None:
                        run_impl_exc.run_data = self._create_error_details()
                    self._stored_exception = run_impl_exc

        if self._input_guardrails_task and self._input_guardrails_task.done():
            if not self._input_guardrails_task.cancelled():
                in_guard_exc = self._input_guardrails_task.exception()
                if in_guard_exc and isinstance(in_guard_exc, Exception):
                    if isinstance(in_guard_exc, AgentsException) and in_guard_exc.run_data is None:
                        in_guard_exc.run_data = self._create_error_details()
                    self._stored_exception = in_guard_exc

        if self._output_guardrails_task and self._output_guardrails_task.done():
            if not self._output_guardrails_task.cancelled():
                out_guard_exc = self._output_guardrails_task.exception()
                if out_guard_exc and isinstance(out_guard_exc, Exception):
                    if (
                        isinstance(out_guard_exc, AgentsException)
                        and out_guard_exc.run_data is None
                    ):
                        out_guard_exc.run_data = self._create_error_details()
                    self._stored_exception = out_guard_exc

    def _cleanup_tasks(self):
        if self.run_loop_task and not self.run_loop_task.done():
            self.run_loop_task.cancel()

        if self._input_guardrails_task and not self._input_guardrails_task.done():
            self._input_guardrails_task.cancel()

        if self._output_guardrails_task and not self._output_guardrails_task.done():
            self._output_guardrails_task.cancel()

    def __str__(self) -> str:
        return pretty_print_run_result_streaming(self)

    async def _await_task_safely(self, task: asyncio.Task[Any] | None) -> None:
        """Await a task if present, ignoring cancellation and storing exceptions elsewhere.

        This ensures we do not lose late guardrail exceptions while not surfacing
        CancelledError to callers of stream_events.
        """
        if task and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                # Task was cancelled (e.g., due to result.cancel()). Nothing to do here.
                pass
            except Exception:
                # The exception will be surfaced via _check_errors() if needed.
                pass

    def _drain_event_queue(self) -> None:
        """Remove any pending items from the event queue and mark them done."""
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
                self._event_queue.task_done()
            except asyncio.QueueEmpty:
                break
            except ValueError:
                # task_done called too many times; nothing more to drain.
                break

    def _drain_input_guardrail_queue(self) -> None:
        """Remove any pending items from the input guardrail queue."""
        while not self._input_guardrail_queue.empty():
            try:
                self._input_guardrail_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def to_state(self) -> RunState[Any]:
        """Create a RunState from this streaming result to resume execution.

        This is useful when the run was interrupted (e.g., for tool approval). You can
        approve or reject the tool calls on the returned state, then pass it back to
        `Runner.run_streamed()` to continue execution.

        Returns:
            A RunState that can be used to resume the run.

        Example:
            ```python
            # Run agent until it needs approval
            result = Runner.run_streamed(agent, "Use the delete_file tool")
            async for event in result.stream_events():
                pass

            if result.interruptions:
                # Approve the tool call
                state = result.to_state()
                state.approve(result.interruptions[0])

                # Resume the run
                result = Runner.run_streamed(agent, state)
                async for event in result.stream_events():
                    pass
            ```
        """
        # Create a RunState from the current result
        # Use _original_input (updated on handoffs/resume when input history changes).
        # This avoids serializing a mutated view of input history.
        state = RunState(
            context=self.context_wrapper,
            original_input=self._original_input if self._original_input is not None else self.input,
            starting_agent=_starting_agent_for_state(self),
            max_turns=self.max_turns,
        )

        return _populate_state_from_result(
            state,
            self,
            current_turn=self.current_turn,
            last_processed_response=self._last_processed_response,
            current_turn_persisted_item_count=self._current_turn_persisted_item_count,
            tool_use_tracker_snapshot=self._tool_use_tracker_snapshot,
            conversation_id=self._conversation_id,
            previous_response_id=self._previous_response_id,
            auto_previous_response_id=self._auto_previous_response_id,
        )
