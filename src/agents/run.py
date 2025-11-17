from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Union, cast, get_args, get_origin

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseOutputItemDoneEvent,
)
from openai.types.responses.response_prompt_param import (
    ResponsePromptParam,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from typing_extensions import NotRequired, TypedDict, Unpack

from ._run_impl import (
    AgentToolUseTracker,
    NextStepFinalOutput,
    NextStepHandoff,
    NextStepInterruption,
    NextStepRunAgain,
    QueueCompleteSentinel,
    RunImpl,
    SingleStepResult,
    ToolRunFunction,
    TraceCtxManager,
    get_model_tracing_impl,
)
from .agent import Agent
from .agent_output import AgentOutputSchema, AgentOutputSchemaBase
from .exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    OutputGuardrailTripwireTriggered,
    RunErrorDetails,
    UserError,
)
from .guardrail import (
    InputGuardrail,
    InputGuardrailResult,
    OutputGuardrail,
    OutputGuardrailResult,
)
from .handoffs import Handoff, HandoffHistoryMapper, HandoffInputFilter, handoff
from .items import (
    HandoffCallItem,
    ItemHelpers,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolCallItem,
    ToolCallItemTypes,
    TResponseInputItem,
)
from .lifecycle import AgentHooksBase, RunHooks, RunHooksBase
from .logger import logger
from .memory import Session, SessionInputCallback
from .model_settings import ModelSettings
from .models.interface import Model, ModelProvider
from .models.multi_provider import MultiProvider
from .result import RunResult, RunResultStreaming
from .run_context import AgentHookContext, RunContextWrapper, TContext
from .run_state import RunState, _normalize_field_names
from .stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
    StreamEvent,
)
from .tool import Tool, dispose_resolved_computers
from .tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from .tracing import Span, SpanError, agent_span, get_current_trace, trace
from .tracing.span_data import AgentSpanData
from .usage import Usage
from .util import _coro, _error_tracing
from .util._types import MaybeAwaitable

DEFAULT_MAX_TURNS = 10

DEFAULT_AGENT_RUNNER: AgentRunner = None  # type: ignore
# the value is set at the end of the module


def set_default_agent_runner(runner: AgentRunner | None) -> None:
    """
    WARNING: this class is experimental and not part of the public API
    It should not be used directly.
    """
    global DEFAULT_AGENT_RUNNER
    DEFAULT_AGENT_RUNNER = runner or AgentRunner()


def get_default_agent_runner() -> AgentRunner:
    """
    WARNING: this class is experimental and not part of the public API
    It should not be used directly.
    """
    global DEFAULT_AGENT_RUNNER
    return DEFAULT_AGENT_RUNNER


def _default_trace_include_sensitive_data() -> bool:
    """Returns the default value for trace_include_sensitive_data based on environment variable."""
    val = os.getenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "true")
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ModelInputData:
    """Container for the data that will be sent to the model."""

    input: list[TResponseInputItem]
    instructions: str | None


@dataclass
class CallModelData(Generic[TContext]):
    """Data passed to `RunConfig.call_model_input_filter` prior to model call."""

    model_data: ModelInputData
    agent: Agent[TContext]
    context: TContext | None


@dataclass
class _ServerConversationTracker:
    """Tracks server-side conversation state for either conversation_id or
    previous_response_id modes.

    Note: When auto_previous_response_id=True is used, response chaining is enabled
    automatically for the first turn, even when there's no actual previous response ID yet.
    """

    conversation_id: str | None = None
    previous_response_id: str | None = None
    auto_previous_response_id: bool = False
    sent_items: set[int] = field(default_factory=set)
    server_items: set[int] = field(default_factory=set)

    def track_server_items(self, model_response: ModelResponse) -> None:
        for output_item in model_response.output:
            self.server_items.add(id(output_item))

        # Update previous_response_id when using previous_response_id mode or auto mode
        if (
            self.conversation_id is None
            and (self.previous_response_id is not None or self.auto_previous_response_id)
            and model_response.response_id is not None
        ):
            self.previous_response_id = model_response.response_id

    def prepare_input(
        self,
        original_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
        model_responses: list[ModelResponse] | None = None,
    ) -> list[TResponseInputItem]:
        input_items: list[TResponseInputItem] = []

        # On first call (when there are no generated items yet), include the original input
        if not generated_items:
            input_items.extend(ItemHelpers.input_to_new_input_list(original_input))

        # First, collect call_ids from tool_call_output_item items
        # (completed tool calls with outputs) and build a map of
        # call_id -> tool_call_item for quick lookup
        completed_tool_call_ids: set[str] = set()
        tool_call_items_by_id: dict[str, RunItem] = {}

        # Also look for tool calls in model responses (they might have been sent in previous turns)
        tool_call_items_from_responses: dict[str, Any] = {}
        if model_responses:
            for response in model_responses:
                for output_item in response.output:
                    # Check if this is a tool call item
                    if isinstance(output_item, dict):
                        item_type = output_item.get("type")
                        call_id = output_item.get("call_id")
                    elif hasattr(output_item, "type") and hasattr(output_item, "call_id"):
                        item_type = output_item.type
                        call_id = output_item.call_id
                    else:
                        continue

                    if item_type == "function_call" and call_id:
                        tool_call_items_from_responses[call_id] = output_item

        for item in generated_items:
            if item.type == "tool_call_output_item":
                # Extract call_id from the output item
                raw_item = item.raw_item
                if isinstance(raw_item, dict):
                    call_id = raw_item.get("call_id")
                elif hasattr(raw_item, "call_id"):
                    call_id = raw_item.call_id
                else:
                    call_id = None
                if call_id and isinstance(call_id, str):
                    completed_tool_call_ids.add(call_id)
            elif item.type == "tool_call_item":
                # Extract call_id from the tool call item and store it for later lookup
                tool_call_raw_item: Any = item.raw_item
                if isinstance(tool_call_raw_item, dict):
                    call_id = tool_call_raw_item.get("call_id")
                elif hasattr(tool_call_raw_item, "call_id"):
                    call_id = tool_call_raw_item.call_id
                else:
                    call_id = None
                if call_id and isinstance(call_id, str):
                    tool_call_items_by_id[call_id] = item

        # Process generated_items, skip items already sent or from server
        for item in generated_items:
            raw_item_id = id(item.raw_item)

            if raw_item_id in self.sent_items or raw_item_id in self.server_items:
                continue

            # Skip tool_approval_item items - they're metadata about pending approvals
            if item.type == "tool_approval_item":
                continue

            # For tool_call_item items, only include them if there's a
            # corresponding tool_call_output_item (i.e., the tool has been
            # executed and has an output)
            if item.type == "tool_call_item":
                # Extract call_id from the tool call item
                tool_call_item_raw: Any = item.raw_item
                if isinstance(tool_call_item_raw, dict):
                    call_id = tool_call_item_raw.get("call_id")
                elif hasattr(tool_call_item_raw, "call_id"):
                    call_id = tool_call_item_raw.call_id
                else:
                    call_id = None

                # Only include if there's a matching tool_call_output_item
                if call_id and isinstance(call_id, str) and call_id in completed_tool_call_ids:
                    input_items.append(item.to_input_item())
                    self.sent_items.add(raw_item_id)
                continue

            # For tool_call_output_item items, also include the corresponding tool_call_item
            # even if it's already in sent_items (API requires both)
            if item.type == "tool_call_output_item":
                raw_item = item.raw_item
                if isinstance(raw_item, dict):
                    call_id = raw_item.get("call_id")
                elif hasattr(raw_item, "call_id"):
                    call_id = raw_item.call_id
                else:
                    call_id = None

                # Track which item IDs have been added to avoid duplicates
                # Include the corresponding tool_call_item if it exists and hasn't been added yet
                # First check in generatedItems, then in model responses
                if call_id and isinstance(call_id, str):
                    if call_id in tool_call_items_by_id:
                        tool_call_item = tool_call_items_by_id[call_id]
                        tool_call_raw_item_id = id(tool_call_item.raw_item)
                        # Include even if already sent (API requires both call and output)
                        if tool_call_raw_item_id not in self.server_items:
                            tool_call_input_item = tool_call_item.to_input_item()
                            # Check if this item has already been added (by ID)
                            if isinstance(tool_call_input_item, dict):
                                tool_call_item_id = tool_call_input_item.get("id")
                            else:
                                tool_call_item_id = getattr(tool_call_input_item, "id", None)
                            # Only add if not already in input_items (check by ID)
                            if tool_call_item_id:
                                already_added = any(
                                    (
                                        isinstance(existing_item, dict)
                                        and existing_item.get("id") == tool_call_item_id
                                    )
                                    or (
                                        hasattr(existing_item, "id")
                                        and getattr(existing_item, "id", None) == tool_call_item_id
                                    )
                                    for existing_item in input_items
                                )
                                if not already_added:
                                    input_items.append(tool_call_input_item)
                            else:
                                input_items.append(tool_call_input_item)
                    elif call_id in tool_call_items_from_responses:
                        # Tool call is in model responses (was sent in previous turn)
                        tool_call_from_response = tool_call_items_from_responses[call_id]
                        # Normalize field names from JSON (camelCase) to Python (snake_case)
                        if isinstance(tool_call_from_response, dict):
                            normalized_tool_call = _normalize_field_names(tool_call_from_response)
                            tool_call_item_id_raw = normalized_tool_call.get("id")
                            tool_call_item_id = (
                                tool_call_item_id_raw
                                if isinstance(tool_call_item_id_raw, str)
                                else None
                            )
                        else:
                            # It's already a Pydantic model, convert to dict
                            normalized_tool_call = (
                                tool_call_from_response.model_dump(exclude_unset=True)
                                if hasattr(tool_call_from_response, "model_dump")
                                else tool_call_from_response
                            )
                            tool_call_item_id = (
                                getattr(tool_call_from_response, "id", None)
                                if hasattr(tool_call_from_response, "id")
                                else (
                                    normalized_tool_call.get("id")
                                    if isinstance(normalized_tool_call, dict)
                                    else None
                                )
                            )
                            if not isinstance(tool_call_item_id, str):
                                tool_call_item_id = None
                        # Only add if not already in input_items (check by ID)
                        if tool_call_item_id:
                            already_added = any(
                                (
                                    isinstance(existing_item, dict)
                                    and existing_item.get("id") == tool_call_item_id
                                )
                                or (
                                    hasattr(existing_item, "id")
                                    and getattr(existing_item, "id", None) == tool_call_item_id
                                )
                                for existing_item in input_items
                            )
                            if not already_added:
                                input_items.append(normalized_tool_call)  # type: ignore[arg-type]
                        else:
                            input_items.append(normalized_tool_call)  # type: ignore[arg-type]

                # Include the tool_call_output_item (check for duplicates by ID)
                output_input_item = item.to_input_item()
                if isinstance(output_input_item, dict):
                    output_item_id = output_input_item.get("id")
                else:
                    output_item_id = getattr(output_input_item, "id", None)
                if output_item_id:
                    already_added = any(
                        (
                            isinstance(existing_item, dict)
                            and existing_item.get("id") == output_item_id
                        )
                        or (
                            hasattr(existing_item, "id")
                            and getattr(existing_item, "id", None) == output_item_id
                        )
                        for existing_item in input_items
                    )
                    if not already_added:
                        input_items.append(output_input_item)
                        self.sent_items.add(raw_item_id)
                else:
                    input_items.append(output_input_item)
                    self.sent_items.add(raw_item_id)
                continue

            input_items.append(item.to_input_item())
            self.sent_items.add(raw_item_id)

        return input_items


# Type alias for the optional input filter callback
CallModelInputFilter = Callable[[CallModelData[Any]], MaybeAwaitable[ModelInputData]]


@dataclass
class RunConfig:
    """Configures settings for the entire agent run."""

    model: str | Model | None = None
    """The model to use for the entire agent run. If set, will override the model set on every
    agent. The model_provider passed in below must be able to resolve this model name.
    """

    model_provider: ModelProvider = field(default_factory=MultiProvider)
    """The model provider to use when looking up string model names. Defaults to OpenAI."""

    model_settings: ModelSettings | None = None
    """Configure global model settings. Any non-null values will override the agent-specific model
    settings.
    """

    handoff_input_filter: HandoffInputFilter | None = None
    """A global input filter to apply to all handoffs. If `Handoff.input_filter` is set, then that
    will take precedence. The input filter allows you to edit the inputs that are sent to the new
    agent. See the documentation in `Handoff.input_filter` for more details.
    """

    nest_handoff_history: bool = True
    """Wrap prior run history in a single assistant message before handing off when no custom
    input filter is set. Set to False to preserve the raw transcript behavior from previous
    releases.
    """

    handoff_history_mapper: HandoffHistoryMapper | None = None
    """Optional function that receives the normalized transcript (history + handoff items) and
    returns the input history that should be passed to the next agent. When left as `None`, the
    runner collapses the transcript into a single assistant message. This function only runs when
    `nest_handoff_history` is True.
    """

    input_guardrails: list[InputGuardrail[Any]] | None = None
    """A list of input guardrails to run on the initial run input."""

    output_guardrails: list[OutputGuardrail[Any]] | None = None
    """A list of output guardrails to run on the final output of the run."""

    tracing_disabled: bool = False
    """Whether tracing is disabled for the agent run. If disabled, we will not trace the agent run.
    """

    trace_include_sensitive_data: bool = field(
        default_factory=_default_trace_include_sensitive_data
    )
    """Whether we include potentially sensitive data (for example: inputs/outputs of tool calls or
    LLM generations) in traces. If False, we'll still create spans for these events, but the
    sensitive data will not be included.
    """

    workflow_name: str = "Agent workflow"
    """The name of the run, used for tracing. Should be a logical name for the run, like
    "Code generation workflow" or "Customer support agent".
    """

    trace_id: str | None = None
    """A custom trace ID to use for tracing. If not provided, we will generate a new trace ID."""

    group_id: str | None = None
    """
    A grouping identifier to use for tracing, to link multiple traces from the same conversation
    or process. For example, you might use a chat thread ID.
    """

    trace_metadata: dict[str, Any] | None = None
    """
    An optional dictionary of additional metadata to include with the trace.
    """

    session_input_callback: SessionInputCallback | None = None
    """Defines how to handle session history when new input is provided.
    - `None` (default): The new input is appended to the session history.
    - `SessionInputCallback`: A custom function that receives the history and new input, and
      returns the desired combined list of items.
    """

    call_model_input_filter: CallModelInputFilter | None = None
    """
    Optional callback that is invoked immediately before calling the model. It receives the current
    agent, context and the model input (instructions and input items), and must return a possibly
    modified `ModelInputData` to use for the model call.

    This allows you to edit the input sent to the model e.g. to stay within a token limit.
    For example, you can use this to add a system prompt to the input.
    """


class RunOptions(TypedDict, Generic[TContext]):
    """Arguments for ``AgentRunner`` methods."""

    context: NotRequired[TContext | None]
    """The context for the run."""

    max_turns: NotRequired[int]
    """The maximum number of turns to run for."""

    hooks: NotRequired[RunHooks[TContext] | None]
    """Lifecycle hooks for the run."""

    run_config: NotRequired[RunConfig | None]
    """Run configuration."""

    previous_response_id: NotRequired[str | None]
    """The ID of the previous response, if any."""

    auto_previous_response_id: NotRequired[bool]
    """Enable automatic response chaining for the first turn."""

    conversation_id: NotRequired[str | None]
    """The ID of the stored conversation, if any."""

    session: NotRequired[Session | None]
    """The session for the run."""


class Runner:
    @classmethod
    async def run(
        cls,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        *,
        context: TContext | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: RunHooks[TContext] | None = None,
        run_config: RunConfig | None = None,
        previous_response_id: str | None = None,
        auto_previous_response_id: bool = False,
        conversation_id: str | None = None,
        session: Session | None = None,
    ) -> RunResult:
        """
        Run a workflow starting at the given agent.

        The agent will run in a loop until a final output is generated. The loop runs like so:

          1. The agent is invoked with the given input.
          2. If there is a final output (i.e. the agent produces something of type
             `agent.output_type`), the loop terminates.
          3. If there's a handoff, we run the loop again, with the new agent.
          4. Else, we run tool calls (if any), and re-run the loop.

        In two cases, the agent may raise an exception:

          1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised.
          2. If a guardrail tripwire is triggered, a GuardrailTripwireTriggered
             exception is raised.

        Note:
            Only the first agent's input guardrails are run.

        Args:
            starting_agent: The starting agent to run.
            input: The initial input to the agent. You can pass a single string for a
                user message, or a list of input items.
            context: The context to run the agent with.
            max_turns: The maximum number of turns to run the agent for. A turn is
                defined as one AI invocation (including any tool calls that might occur).
            hooks: An object that receives callbacks on various lifecycle events.
            run_config: Global settings for the entire agent run.
            previous_response_id: The ID of the previous response. If using OpenAI
                models via the Responses API, this allows you to skip passing in input
                from the previous turn.
            conversation_id: The conversation ID
                (https://platform.openai.com/docs/guides/conversation-state?api-mode=responses).
                If provided, the conversation will be used to read and write items.
                Every agent will have access to the conversation history so far,
                and its output items will be written to the conversation.
                We recommend only using this if you are exclusively using OpenAI models;
                other model providers don't write to the Conversation object,
                so you'll end up having partial conversations stored.
            session: A session for automatic conversation history management.

        Returns:
            A run result containing all the inputs, guardrail results and the output of
            the last agent. Agents may perform handoffs, so we don't know the specific
            type of the output.
        """

        runner = DEFAULT_AGENT_RUNNER
        return await runner.run(
            starting_agent,
            input,
            context=context,
            max_turns=max_turns,
            hooks=hooks,
            run_config=run_config,
            previous_response_id=previous_response_id,
            auto_previous_response_id=auto_previous_response_id,
            conversation_id=conversation_id,
            session=session,
        )

    @classmethod
    def run_sync(
        cls,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        *,
        context: TContext | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: RunHooks[TContext] | None = None,
        run_config: RunConfig | None = None,
        previous_response_id: str | None = None,
        auto_previous_response_id: bool = False,
        conversation_id: str | None = None,
        session: Session | None = None,
    ) -> RunResult:
        """
        Run a workflow synchronously, starting at the given agent.

        Note:
            This just wraps the `run` method, so it will not work if there's already an
            event loop (e.g. inside an async function, or in a Jupyter notebook or async
            context like FastAPI). For those cases, use the `run` method instead.

        The agent will run in a loop until a final output is generated. The loop runs:

          1. The agent is invoked with the given input.
          2. If there is a final output (i.e. the agent produces something of type
             `agent.output_type`), the loop terminates.
          3. If there's a handoff, we run the loop again, with the new agent.
          4. Else, we run tool calls (if any), and re-run the loop.

        In two cases, the agent may raise an exception:

          1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised.
          2. If a guardrail tripwire is triggered, a GuardrailTripwireTriggered
             exception is raised.

        Note:
            Only the first agent's input guardrails are run.

        Args:
            starting_agent: The starting agent to run.
            input: The initial input to the agent. You can pass a single string for a
                user message, or a list of input items.
            context: The context to run the agent with.
            max_turns: The maximum number of turns to run the agent for. A turn is
                defined as one AI invocation (including any tool calls that might occur).
            hooks: An object that receives callbacks on various lifecycle events.
            run_config: Global settings for the entire agent run.
            previous_response_id: The ID of the previous response, if using OpenAI
                models via the Responses API, this allows you to skip passing in input
                from the previous turn.
            conversation_id: The ID of the stored conversation, if any.
            session: A session for automatic conversation history management.

        Returns:
            A run result containing all the inputs, guardrail results and the output of
            the last agent. Agents may perform handoffs, so we don't know the specific
            type of the output.
        """

        runner = DEFAULT_AGENT_RUNNER
        return runner.run_sync(
            starting_agent,
            input,
            context=context,
            max_turns=max_turns,
            hooks=hooks,
            run_config=run_config,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            session=session,
            auto_previous_response_id=auto_previous_response_id,
        )

    @classmethod
    def run_streamed(
        cls,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        context: TContext | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: RunHooks[TContext] | None = None,
        run_config: RunConfig | None = None,
        previous_response_id: str | None = None,
        auto_previous_response_id: bool = False,
        conversation_id: str | None = None,
        session: Session | None = None,
    ) -> RunResultStreaming:
        """
        Run a workflow starting at the given agent in streaming mode.

        The returned result object contains a method you can use to stream semantic
        events as they are generated.

        The agent will run in a loop until a final output is generated. The loop runs like so:

          1. The agent is invoked with the given input.
          2. If there is a final output (i.e. the agent produces something of type
             `agent.output_type`), the loop terminates.
          3. If there's a handoff, we run the loop again, with the new agent.
          4. Else, we run tool calls (if any), and re-run the loop.

        In two cases, the agent may raise an exception:

          1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised.
          2. If a guardrail tripwire is triggered, a GuardrailTripwireTriggered
             exception is raised.

        Note:
            Only the first agent's input guardrails are run.

        Args:
            starting_agent: The starting agent to run.
            input: The initial input to the agent. You can pass a single string for a
                user message, or a list of input items.
            context: The context to run the agent with.
            max_turns: The maximum number of turns to run the agent for. A turn is
                defined as one AI invocation (including any tool calls that might occur).
            hooks: An object that receives callbacks on various lifecycle events.
            run_config: Global settings for the entire agent run.
            previous_response_id: The ID of the previous response, if using OpenAI
                models via the Responses API, this allows you to skip passing in input
                from the previous turn.
            conversation_id: The ID of the stored conversation, if any.
            session: A session for automatic conversation history management.

        Returns:
            A result object that contains data about the run, as well as a method to
            stream events.
        """

        runner = DEFAULT_AGENT_RUNNER
        return runner.run_streamed(
            starting_agent,
            input,
            context=context,
            max_turns=max_turns,
            hooks=hooks,
            run_config=run_config,
            previous_response_id=previous_response_id,
            auto_previous_response_id=auto_previous_response_id,
            conversation_id=conversation_id,
            session=session,
        )


class AgentRunner:
    """
    WARNING: this class is experimental and not part of the public API
    It should not be used directly or subclassed.
    """

    async def run(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResult:
        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = cast(RunHooks[TContext], self._validate_run_hooks(kwargs.get("hooks")))
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
        auto_previous_response_id = kwargs.get("auto_previous_response_id", False)
        conversation_id = kwargs.get("conversation_id")
        session = kwargs.get("session")

        if run_config is None:
            run_config = RunConfig()

        # Check if we're resuming from a RunState
        is_resumed_state = isinstance(input, RunState)
        run_state: RunState[TContext] | None = None
        prepared_input: str | list[TResponseInputItem]

        if is_resumed_state:
            # Resuming from a saved state
            run_state = cast(RunState[TContext], input)
            original_user_input = run_state._original_input

            if isinstance(run_state._original_input, list):
                prepared_input = self._merge_provider_data_in_items(run_state._original_input)
            else:
                prepared_input = run_state._original_input

            # Override context with the state's context if not provided
            if context is None and run_state._context is not None:
                context = run_state._context.context
        else:
            # Keep original user input separate from session-prepared input
            raw_input = cast(Union[str, list[TResponseInputItem]], input)
            original_user_input = raw_input
            prepared_input = await self._prepare_input_with_session(
                raw_input, session, run_config.session_input_callback
            )

        # Check whether to enable OpenAI server-managed conversation
        if (
            conversation_id is not None
            or previous_response_id is not None
            or auto_previous_response_id
        ):
            server_conversation_tracker = _ServerConversationTracker(
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
        else:
            server_conversation_tracker = None

        # Prime the server conversation tracker from state if resuming
        if server_conversation_tracker is not None and is_resumed_state and run_state is not None:
            for response in run_state._model_responses:
                server_conversation_tracker.track_server_items(response)

        # Always create a fresh tool_use_tracker
        # (it's rebuilt from the run state if needed during execution)
        tool_use_tracker = AgentToolUseTracker()

        with TraceCtxManager(
            workflow_name=run_config.workflow_name,
            trace_id=run_config.trace_id,
            group_id=run_config.group_id,
            metadata=run_config.trace_metadata,
            disabled=run_config.tracing_disabled,
        ):
            if is_resumed_state and run_state is not None:
                # Restore state from RunState
                current_turn = run_state._current_turn
                original_input = run_state._original_input
                generated_items = run_state._generated_items
                model_responses = run_state._model_responses
                # Cast to the correct type since we know this is TContext
                context_wrapper = cast(RunContextWrapper[TContext], run_state._context)
            else:
                # Fresh run
                current_turn = 0
                original_input = _copy_str_or_list(prepared_input)
                generated_items = []
                model_responses = []
                context_wrapper = RunContextWrapper(
                    context=context,  # type: ignore
                )

            input_guardrail_results: list[InputGuardrailResult] = []
            tool_input_guardrail_results: list[ToolInputGuardrailResult] = []
            tool_output_guardrail_results: list[ToolOutputGuardrailResult] = []

            current_span: Span[AgentSpanData] | None = None
            current_agent = starting_agent
            should_run_agent_start_hooks = True

            # save only the new user input to the session, not the combined history
            # Skip saving if resuming from state - input is already in session
            if not is_resumed_state:
                await self._save_result_to_session(session, original_user_input, [])

            # If resuming from an interrupted state, execute approved tools first
            if is_resumed_state and run_state is not None and run_state._current_step is not None:
                if isinstance(run_state._current_step, NextStepInterruption):
                    # Track items before executing approved tools
                    items_before_execution = len(generated_items)

                    # We're resuming from an interruption - execute approved tools
                    await self._execute_approved_tools(
                        agent=current_agent,
                        interruptions=run_state._current_step.interruptions,
                        context_wrapper=context_wrapper,
                        generated_items=generated_items,
                        run_config=run_config,
                        hooks=hooks,
                    )

                    # Save the newly executed tool outputs to the session
                    new_tool_outputs: list[RunItem] = [
                        item
                        for item in generated_items[items_before_execution:]
                        if item.type == "tool_call_output_item"
                    ]
                    if new_tool_outputs and session is not None:
                        await self._save_result_to_session(session, [], new_tool_outputs)

                    # Clear the current step since we've handled it
                    run_state._current_step = None

            try:
                while True:
                    all_tools = await AgentRunner._get_all_tools(current_agent, context_wrapper)
                    await RunImpl.initialize_computer_tools(
                        tools=all_tools, context_wrapper=context_wrapper
                    )

                    # Start an agent span if we don't have one. This span is ended if the current
                    # agent changes, or if the agent loop ends.
                    if current_span is None:
                        handoff_names = [
                            h.agent_name
                            for h in await AgentRunner._get_handoffs(current_agent, context_wrapper)
                        ]
                        if output_schema := AgentRunner._get_output_schema(current_agent):
                            output_type_name = output_schema.name()
                        else:
                            output_type_name = "str"

                        current_span = agent_span(
                            name=current_agent.name,
                            handoffs=handoff_names,
                            output_type=output_type_name,
                        )
                        current_span.start(mark_as_current=True)
                        current_span.span_data.tools = [t.name for t in all_tools]

                    current_turn += 1
                    if current_turn > max_turns:
                        _error_tracing.attach_error_to_span(
                            current_span,
                            SpanError(
                                message="Max turns exceeded",
                                data={"max_turns": max_turns},
                            ),
                        )
                        raise MaxTurnsExceeded(f"Max turns ({max_turns}) exceeded")

                    logger.debug(
                        f"Running agent {current_agent.name} (turn {current_turn})",
                    )

                    if current_turn == 1:
                        # Separate guardrails based on execution mode.
                        all_input_guardrails = starting_agent.input_guardrails + (
                            run_config.input_guardrails or []
                        )
                        sequential_guardrails = [
                            g for g in all_input_guardrails if not g.run_in_parallel
                        ]
                        parallel_guardrails = [g for g in all_input_guardrails if g.run_in_parallel]

                        # Run blocking guardrails first, before agent starts.
                        # (will raise exception if tripwire triggered).
                        sequential_results = []
                        if sequential_guardrails:
                            sequential_results = await self._run_input_guardrails(
                                starting_agent,
                                sequential_guardrails,
                                _copy_str_or_list(prepared_input),
                                context_wrapper,
                            )

                        # Run parallel guardrails + agent together.
                        input_guardrail_results, turn_result = await asyncio.gather(
                            self._run_input_guardrails(
                                starting_agent,
                                parallel_guardrails,
                                _copy_str_or_list(prepared_input),
                                context_wrapper,
                            ),
                            self._run_single_turn(
                                agent=current_agent,
                                all_tools=all_tools,
                                original_input=original_input,
                                generated_items=generated_items,
                                hooks=hooks,
                                context_wrapper=context_wrapper,
                                run_config=run_config,
                                should_run_agent_start_hooks=should_run_agent_start_hooks,
                                tool_use_tracker=tool_use_tracker,
                                server_conversation_tracker=server_conversation_tracker,
                                model_responses=model_responses,
                            ),
                        )

                        # Combine sequential and parallel results.
                        input_guardrail_results = sequential_results + input_guardrail_results
                    else:
                        turn_result = await self._run_single_turn(
                            agent=current_agent,
                            all_tools=all_tools,
                            original_input=original_input,
                            generated_items=generated_items,
                            hooks=hooks,
                            context_wrapper=context_wrapper,
                            run_config=run_config,
                            should_run_agent_start_hooks=should_run_agent_start_hooks,
                            tool_use_tracker=tool_use_tracker,
                            server_conversation_tracker=server_conversation_tracker,
                            model_responses=model_responses,
                        )
                    should_run_agent_start_hooks = False

                    model_responses.append(turn_result.model_response)
                    original_input = turn_result.original_input
                    generated_items = turn_result.generated_items

                    if server_conversation_tracker is not None:
                        server_conversation_tracker.track_server_items(turn_result.model_response)

                    # Collect tool guardrail results from this turn
                    tool_input_guardrail_results.extend(turn_result.tool_input_guardrail_results)
                    tool_output_guardrail_results.extend(turn_result.tool_output_guardrail_results)

                    try:
                        if isinstance(turn_result.next_step, NextStepFinalOutput):
                            output_guardrail_results = await self._run_output_guardrails(
                                current_agent.output_guardrails
                                + (run_config.output_guardrails or []),
                                current_agent,
                                turn_result.next_step.output,
                                context_wrapper,
                            )
                            result = RunResult(
                                input=original_input,
                                new_items=generated_items,
                                raw_responses=model_responses,
                                final_output=turn_result.next_step.output,
                                _last_agent=current_agent,
                                input_guardrail_results=input_guardrail_results,
                                output_guardrail_results=output_guardrail_results,
                                tool_input_guardrail_results=tool_input_guardrail_results,
                                tool_output_guardrail_results=tool_output_guardrail_results,
                                context_wrapper=context_wrapper,
                                interruptions=[],
                            )
                            if not any(
                                guardrail_result.output.tripwire_triggered
                                for guardrail_result in input_guardrail_results
                            ):
                                await self._save_result_to_session(
                                    session, [], turn_result.new_step_items
                                )
                            return result
                        elif isinstance(turn_result.next_step, NextStepInterruption):
                            # Tool approval is needed - return a result with interruptions
                            result = RunResult(
                                input=original_input,
                                new_items=generated_items,
                                raw_responses=model_responses,
                                final_output=None,
                                _last_agent=current_agent,
                                input_guardrail_results=input_guardrail_results,
                                output_guardrail_results=[],
                                tool_input_guardrail_results=tool_input_guardrail_results,
                                tool_output_guardrail_results=tool_output_guardrail_results,
                                context_wrapper=context_wrapper,
                                interruptions=turn_result.next_step.interruptions,
                                _last_processed_response=turn_result.processed_response,
                            )
                            return result
                        elif isinstance(turn_result.next_step, NextStepHandoff):
                            # Save the conversation to session if enabled (before handoff)
                            if session is not None:
                                if not any(
                                    guardrail_result.output.tripwire_triggered
                                    for guardrail_result in input_guardrail_results
                                ):
                                    await self._save_result_to_session(
                                        session, [], turn_result.new_step_items
                                    )
                            current_agent = cast(Agent[TContext], turn_result.next_step.new_agent)
                            current_span.finish(reset_current=True)
                            current_span = None
                            should_run_agent_start_hooks = True
                        elif isinstance(turn_result.next_step, NextStepRunAgain):
                            if not any(
                                guardrail_result.output.tripwire_triggered
                                for guardrail_result in input_guardrail_results
                            ):
                                await self._save_result_to_session(
                                    session, [], turn_result.new_step_items
                                )
                        else:
                            raise AgentsException(
                                f"Unknown next step type: {type(turn_result.next_step)}"
                            )
                    finally:
                        # RunImpl.execute_tools_and_side_effects returns a SingleStepResult that
                        # stores direct references to the `pre_step_items` and `new_step_items`
                        # lists it manages internally. Clear them here so the next turn does not
                        # hold on to items from previous turns and to avoid leaking agent refs.
                        turn_result.pre_step_items.clear()
                        turn_result.new_step_items.clear()
            except AgentsException as exc:
                exc.run_data = RunErrorDetails(
                    input=original_input,
                    new_items=generated_items,
                    raw_responses=model_responses,
                    last_agent=current_agent,
                    context_wrapper=context_wrapper,
                    input_guardrail_results=input_guardrail_results,
                    output_guardrail_results=[],
                )
                raise
            finally:
                try:
                    await dispose_resolved_computers(run_context=context_wrapper)
                except Exception as error:
                    logger.warning("Failed to dispose computers after run: %s", error)
                if current_span:
                    current_span.finish(reset_current=True)

    def run_sync(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResult:
        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = kwargs.get("hooks")
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
        auto_previous_response_id = kwargs.get("auto_previous_response_id", False)
        conversation_id = kwargs.get("conversation_id")
        session = kwargs.get("session")

        # Python 3.14 stopped implicitly wiring up a default event loop
        # when synchronous code touches asyncio APIs for the first time.
        # Several of our synchronous entry points (for example the Redis/SQLAlchemy session helpers)
        # construct asyncio primitives like asyncio.Lock during __init__,
        # which binds them to whatever loop happens to be the thread's default at that moment.
        # To keep those locks usable we must ensure that run_sync reuses that same default loop
        # instead of hopping over to a brand-new asyncio.run() loop.
        try:
            already_running_loop = asyncio.get_running_loop()
        except RuntimeError:
            already_running_loop = None

        if already_running_loop is not None:
            # This method is only expected to run when no loop is already active.
            # (Each thread has its own default loop; concurrent sync runs should happen on
            # different threads. In a single thread use the async API to interleave work.)
            raise RuntimeError(
                "AgentRunner.run_sync() cannot be called when an event loop is already running."
            )

        policy = asyncio.get_event_loop_policy()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                default_loop = policy.get_event_loop()
            except RuntimeError:
                default_loop = policy.new_event_loop()
                policy.set_event_loop(default_loop)

        # We intentionally leave the default loop open even if we had to create one above. Session
        # instances and other helpers stash loop-bound primitives between calls and expect to find
        # the same default loop every time run_sync is invoked on this thread.
        # Schedule the async run on the default loop so that we can manage cancellation explicitly.
        task = default_loop.create_task(
            self.run(
                starting_agent,
                input,
                session=session,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
                conversation_id=conversation_id,
            )
        )

        try:
            # Drive the coroutine to completion, harvesting the final RunResult.
            return default_loop.run_until_complete(task)
        except BaseException:
            # If the sync caller aborts (KeyboardInterrupt, etc.), make sure the scheduled task
            # does not linger on the shared loop by cancelling it and waiting for completion.
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    default_loop.run_until_complete(task)
            raise
        finally:
            if not default_loop.is_closed():
                # The loop stays open for subsequent runs, but we still need to flush any pending
                # async generators so their cleanup code executes promptly.
                with contextlib.suppress(RuntimeError):
                    default_loop.run_until_complete(default_loop.shutdown_asyncgens())

    def run_streamed(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResultStreaming:
        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = cast(RunHooks[TContext], self._validate_run_hooks(kwargs.get("hooks")))
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
        auto_previous_response_id = kwargs.get("auto_previous_response_id", False)
        conversation_id = kwargs.get("conversation_id")
        session = kwargs.get("session")

        if run_config is None:
            run_config = RunConfig()

        # If there's already a trace, we don't create a new one. In addition, we can't end the
        # trace here, because the actual work is done in `stream_events` and this method ends
        # before that.
        new_trace = (
            None
            if get_current_trace()
            else trace(
                workflow_name=run_config.workflow_name,
                trace_id=run_config.trace_id,
                group_id=run_config.group_id,
                metadata=run_config.trace_metadata,
                disabled=run_config.tracing_disabled,
            )
        )

        output_schema = AgentRunner._get_output_schema(starting_agent)

        # Handle RunState input
        is_resumed_state = isinstance(input, RunState)
        run_state: RunState[TContext] | None = None
        input_for_result: str | list[TResponseInputItem]

        if is_resumed_state:
            run_state = cast(RunState[TContext], input)

            if isinstance(run_state._original_input, list):
                input_for_result = AgentRunner._merge_provider_data_in_items(
                    run_state._original_input
                )
            else:
                input_for_result = run_state._original_input

            # Use context from RunState if not provided
            if context is None and run_state._context is not None:
                context = run_state._context.context
            # Use context wrapper from RunState
            context_wrapper = cast(RunContextWrapper[TContext], run_state._context)
        else:
            input_for_result = cast(Union[str, list[TResponseInputItem]], input)
            context_wrapper = RunContextWrapper(context=context)  # type: ignore

        streamed_result = RunResultStreaming(
            input=_copy_str_or_list(input_for_result),
            new_items=run_state._generated_items if run_state else [],
            current_agent=starting_agent,
            raw_responses=run_state._model_responses if run_state else [],
            final_output=None,
            is_complete=False,
            current_turn=run_state._current_turn if run_state else 0,
            max_turns=max_turns,
            input_guardrail_results=[],
            output_guardrail_results=[],
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
            _current_agent_output_schema=output_schema,
            trace=new_trace,
            context_wrapper=context_wrapper,
            interruptions=[],
        )

        # Kick off the actual agent loop in the background and return the streamed result object.
        streamed_result._run_impl_task = asyncio.create_task(
            self._start_streaming(
                starting_input=input_for_result,
                streamed_result=streamed_result,
                starting_agent=starting_agent,
                max_turns=max_turns,
                hooks=hooks,
                context_wrapper=context_wrapper,
                run_config=run_config,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
                conversation_id=conversation_id,
                session=session,
                run_state=run_state,
            )
        )
        return streamed_result

    @staticmethod
    def _validate_run_hooks(
        hooks: RunHooksBase[Any, Agent[Any]] | AgentHooksBase[Any, Agent[Any]] | Any | None,
    ) -> RunHooks[Any]:
        if hooks is None:
            return RunHooks[Any]()
        input_hook_type = type(hooks).__name__
        if isinstance(hooks, AgentHooksBase):
            raise TypeError(
                "Run hooks must be instances of RunHooks. "
                f"Received agent-scoped hooks ({input_hook_type}). "
                "Attach AgentHooks to an Agent via Agent(..., hooks=...)."
            )
        if not isinstance(hooks, RunHooksBase):
            raise TypeError(f"Run hooks must be instances of RunHooks. Received {input_hook_type}.")
        return hooks

    @classmethod
    async def _maybe_filter_model_input(
        cls,
        *,
        agent: Agent[TContext],
        run_config: RunConfig,
        context_wrapper: RunContextWrapper[TContext],
        input_items: list[TResponseInputItem],
        system_instructions: str | None,
    ) -> ModelInputData:
        """Apply optional call_model_input_filter to modify model input.

        Returns a `ModelInputData` that will be sent to the model.
        """
        effective_instructions = system_instructions
        effective_input: list[TResponseInputItem] = input_items

        if run_config.call_model_input_filter is None:
            return ModelInputData(input=effective_input, instructions=effective_instructions)

        try:
            model_input = ModelInputData(
                input=effective_input.copy(),
                instructions=effective_instructions,
            )
            filter_payload: CallModelData[TContext] = CallModelData(
                model_data=model_input,
                agent=agent,
                context=context_wrapper.context,
            )
            maybe_updated = run_config.call_model_input_filter(filter_payload)
            updated = await maybe_updated if inspect.isawaitable(maybe_updated) else maybe_updated
            if not isinstance(updated, ModelInputData):
                raise UserError("call_model_input_filter must return a ModelInputData instance")
            return updated
        except Exception as e:
            _error_tracing.attach_error_to_current_span(
                SpanError(message="Error in call_model_input_filter", data={"error": str(e)})
            )
            raise

    @classmethod
    async def _run_input_guardrails_with_queue(
        cls,
        agent: Agent[Any],
        guardrails: list[InputGuardrail[TContext]],
        input: str | list[TResponseInputItem],
        context: RunContextWrapper[TContext],
        streamed_result: RunResultStreaming,
        parent_span: Span[Any],
    ):
        queue = streamed_result._input_guardrail_queue

        # We'll run the guardrails and push them onto the queue as they complete
        guardrail_tasks = [
            asyncio.create_task(
                RunImpl.run_single_input_guardrail(agent, guardrail, input, context)
            )
            for guardrail in guardrails
        ]
        guardrail_results = []
        try:
            for done in asyncio.as_completed(guardrail_tasks):
                result = await done
                if result.output.tripwire_triggered:
                    # Cancel all remaining guardrail tasks if a tripwire is triggered.
                    for t in guardrail_tasks:
                        t.cancel()
                    # Wait for cancellations to propagate by awaiting the cancelled tasks.
                    await asyncio.gather(*guardrail_tasks, return_exceptions=True)
                    _error_tracing.attach_error_to_span(
                        parent_span,
                        SpanError(
                            message="Guardrail tripwire triggered",
                            data={
                                "guardrail": result.guardrail.get_name(),
                                "type": "input_guardrail",
                            },
                        ),
                    )
                    queue.put_nowait(result)
                    guardrail_results.append(result)
                    break
                queue.put_nowait(result)
                guardrail_results.append(result)
        except Exception:
            for t in guardrail_tasks:
                t.cancel()
            raise

        streamed_result.input_guardrail_results = (
            streamed_result.input_guardrail_results + guardrail_results
        )

    @classmethod
    async def _start_streaming(
        cls,
        starting_input: str | list[TResponseInputItem],
        streamed_result: RunResultStreaming,
        starting_agent: Agent[TContext],
        max_turns: int,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        previous_response_id: str | None,
        auto_previous_response_id: bool,
        conversation_id: str | None,
        session: Session | None,
        run_state: RunState[TContext] | None = None,
    ):
        if streamed_result.trace:
            streamed_result.trace.start(mark_as_current=True)

        current_span: Span[AgentSpanData] | None = None
        current_agent = starting_agent
        current_turn = 0
        should_run_agent_start_hooks = True
        tool_use_tracker = AgentToolUseTracker()

        # Check whether to enable OpenAI server-managed conversation
        if (
            conversation_id is not None
            or previous_response_id is not None
            or auto_previous_response_id
        ):
            server_conversation_tracker = _ServerConversationTracker(
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
        else:
            server_conversation_tracker = None

        # Prime the server conversation tracker from state if resuming
        if server_conversation_tracker is not None and run_state is not None:
            for response in run_state._model_responses:
                server_conversation_tracker.track_server_items(response)

        streamed_result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=current_agent))

        try:
            # Prepare input with session if enabled (skip if resuming from state)
            if run_state is None:
                prepared_input = await AgentRunner._prepare_input_with_session(
                    starting_input, session, run_config.session_input_callback
                )

                # Update the streamed result with the prepared input
                streamed_result.input = prepared_input

                await AgentRunner._save_result_to_session(session, starting_input, [])
            else:
                # When resuming, starting_input is already prepared from RunState
                prepared_input = starting_input

            # If resuming from an interrupted state, execute approved tools first
            if run_state is not None and run_state._current_step is not None:
                if isinstance(run_state._current_step, NextStepInterruption):
                    # Track items before executing approved tools
                    items_before_execution = len(streamed_result.new_items)

                    # We're resuming from an interruption - execute approved tools
                    await cls._execute_approved_tools_static(
                        agent=current_agent,
                        interruptions=run_state._current_step.interruptions,
                        context_wrapper=context_wrapper,
                        generated_items=streamed_result.new_items,
                        run_config=run_config,
                        hooks=hooks,
                    )

                    # Save the newly executed tool outputs to the session
                    new_tool_outputs: list[RunItem] = [
                        item
                        for item in streamed_result.new_items[items_before_execution:]
                        if item.type == "tool_call_output_item"
                    ]
                    if new_tool_outputs and session is not None:
                        await cls._save_result_to_session(session, [], new_tool_outputs)

                    # Clear the current step since we've handled it
                    run_state._current_step = None

            while True:
                # Check for soft cancel before starting new turn
                if streamed_result._cancel_mode == "after_turn":
                    streamed_result.is_complete = True
                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    break

                if streamed_result.is_complete:
                    break

                all_tools = await cls._get_all_tools(current_agent, context_wrapper)
                await RunImpl.initialize_computer_tools(
                    tools=all_tools, context_wrapper=context_wrapper
                )

                # Start an agent span if we don't have one. This span is ended if the current
                # agent changes, or if the agent loop ends.
                if current_span is None:
                    handoff_names = [
                        h.agent_name
                        for h in await cls._get_handoffs(current_agent, context_wrapper)
                    ]
                    if output_schema := cls._get_output_schema(current_agent):
                        output_type_name = output_schema.name()
                    else:
                        output_type_name = "str"

                    current_span = agent_span(
                        name=current_agent.name,
                        handoffs=handoff_names,
                        output_type=output_type_name,
                    )
                    current_span.start(mark_as_current=True)
                    tool_names = [t.name for t in all_tools]
                    current_span.span_data.tools = tool_names
                current_turn += 1
                streamed_result.current_turn = current_turn

                if current_turn > max_turns:
                    _error_tracing.attach_error_to_span(
                        current_span,
                        SpanError(
                            message="Max turns exceeded",
                            data={"max_turns": max_turns},
                        ),
                    )
                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    break

                if current_turn == 1:
                    # Separate guardrails based on execution mode.
                    all_input_guardrails = starting_agent.input_guardrails + (
                        run_config.input_guardrails or []
                    )
                    sequential_guardrails = [
                        g for g in all_input_guardrails if not g.run_in_parallel
                    ]
                    parallel_guardrails = [g for g in all_input_guardrails if g.run_in_parallel]

                    # Run sequential guardrails first.
                    if sequential_guardrails:
                        await cls._run_input_guardrails_with_queue(
                            starting_agent,
                            sequential_guardrails,
                            ItemHelpers.input_to_new_input_list(prepared_input),
                            context_wrapper,
                            streamed_result,
                            current_span,
                        )
                        # Check if any blocking guardrail triggered and raise before starting agent.
                        for result in streamed_result.input_guardrail_results:
                            if result.output.tripwire_triggered:
                                streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                                raise InputGuardrailTripwireTriggered(result)

                    # Run parallel guardrails in background.
                    streamed_result._input_guardrails_task = asyncio.create_task(
                        cls._run_input_guardrails_with_queue(
                            starting_agent,
                            parallel_guardrails,
                            ItemHelpers.input_to_new_input_list(prepared_input),
                            context_wrapper,
                            streamed_result,
                            current_span,
                        )
                    )
                try:
                    turn_result = await cls._run_single_turn_streamed(
                        streamed_result,
                        current_agent,
                        hooks,
                        context_wrapper,
                        run_config,
                        should_run_agent_start_hooks,
                        tool_use_tracker,
                        all_tools,
                        server_conversation_tracker,
                    )
                    should_run_agent_start_hooks = False

                    streamed_result.raw_responses = streamed_result.raw_responses + [
                        turn_result.model_response
                    ]
                    streamed_result.input = turn_result.original_input
                    streamed_result.new_items = turn_result.generated_items

                    if server_conversation_tracker is not None:
                        server_conversation_tracker.track_server_items(turn_result.model_response)

                    if isinstance(turn_result.next_step, NextStepHandoff):
                        # Save the conversation to session if enabled (before handoff)
                        # Streaming needs to save for graceful cancellation support
                        if session is not None:
                            should_skip_session_save = (
                                await AgentRunner._input_guardrail_tripwire_triggered_for_stream(
                                    streamed_result
                                )
                            )
                            if should_skip_session_save is False:
                                await AgentRunner._save_result_to_session(
                                    session, [], turn_result.new_step_items
                                )

                        current_agent = turn_result.next_step.new_agent
                        current_span.finish(reset_current=True)
                        current_span = None
                        should_run_agent_start_hooks = True
                        streamed_result._event_queue.put_nowait(
                            AgentUpdatedStreamEvent(new_agent=current_agent)
                        )

                        # Check for soft cancel after handoff
                        if streamed_result._cancel_mode == "after_turn":  # type: ignore[comparison-overlap]
                            streamed_result.is_complete = True
                            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                            break
                    elif isinstance(turn_result.next_step, NextStepFinalOutput):
                        streamed_result._output_guardrails_task = asyncio.create_task(
                            cls._run_output_guardrails(
                                current_agent.output_guardrails
                                + (run_config.output_guardrails or []),
                                current_agent,
                                turn_result.next_step.output,
                                context_wrapper,
                            )
                        )

                        try:
                            output_guardrail_results = await streamed_result._output_guardrails_task
                        except Exception:
                            # Exceptions will be checked in the stream_events loop
                            output_guardrail_results = []

                        streamed_result.output_guardrail_results = output_guardrail_results
                        streamed_result.final_output = turn_result.next_step.output
                        streamed_result.is_complete = True

                        # Save the conversation to session if enabled
                        if session is not None:
                            should_skip_session_save = (
                                await AgentRunner._input_guardrail_tripwire_triggered_for_stream(
                                    streamed_result
                                )
                            )
                            if should_skip_session_save is False:
                                await AgentRunner._save_result_to_session(
                                    session, [], turn_result.new_step_items
                                )

                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    elif isinstance(turn_result.next_step, NextStepInterruption):
                        # Tool approval is needed - complete the stream with interruptions
                        streamed_result.interruptions = turn_result.next_step.interruptions
                        streamed_result._last_processed_response = turn_result.processed_response
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    elif isinstance(turn_result.next_step, NextStepRunAgain):
                        if session is not None:
                            should_skip_session_save = (
                                await AgentRunner._input_guardrail_tripwire_triggered_for_stream(
                                    streamed_result
                                )
                            )
                            if should_skip_session_save is False:
                                await AgentRunner._save_result_to_session(
                                    session, [], turn_result.new_step_items
                                )

                        # Check for soft cancel after turn completion
                        if streamed_result._cancel_mode == "after_turn":  # type: ignore[comparison-overlap]
                            streamed_result.is_complete = True
                            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                            break
                except AgentsException as exc:
                    streamed_result.is_complete = True
                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    exc.run_data = RunErrorDetails(
                        input=streamed_result.input,
                        new_items=streamed_result.new_items,
                        raw_responses=streamed_result.raw_responses,
                        last_agent=current_agent,
                        context_wrapper=context_wrapper,
                        input_guardrail_results=streamed_result.input_guardrail_results,
                        output_guardrail_results=streamed_result.output_guardrail_results,
                    )
                    raise
                except Exception as e:
                    if current_span:
                        _error_tracing.attach_error_to_span(
                            current_span,
                            SpanError(
                                message="Error in agent run",
                                data={"error": str(e)},
                            ),
                        )
                    streamed_result.is_complete = True
                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    raise

            streamed_result.is_complete = True
        finally:
            if streamed_result._input_guardrails_task:
                try:
                    await AgentRunner._input_guardrail_tripwire_triggered_for_stream(
                        streamed_result
                    )
                except Exception as e:
                    logger.debug(
                        f"Error in streamed_result finalize for agent {current_agent.name} - {e}"
                    )
            try:
                await dispose_resolved_computers(run_context=context_wrapper)
            except Exception as error:
                logger.warning("Failed to dispose computers after streamed run: %s", error)
            if current_span:
                current_span.finish(reset_current=True)
            if streamed_result.trace:
                streamed_result.trace.finish(reset_current=True)

            # Ensure QueueCompleteSentinel is always put in the queue when the stream ends,
            # even if an exception occurs before the inner try/except block (e.g., in
            # _save_result_to_session at the beginning). Without this, stream_events()
            # would hang forever waiting for more items.
            if not streamed_result.is_complete:
                streamed_result.is_complete = True
                streamed_result._event_queue.put_nowait(QueueCompleteSentinel())

    @classmethod
    async def _run_single_turn_streamed(
        cls,
        streamed_result: RunResultStreaming,
        agent: Agent[TContext],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        should_run_agent_start_hooks: bool,
        tool_use_tracker: AgentToolUseTracker,
        all_tools: list[Tool],
        server_conversation_tracker: _ServerConversationTracker | None = None,
    ) -> SingleStepResult:
        emitted_tool_call_ids: set[str] = set()
        emitted_reasoning_item_ids: set[str] = set()

        if should_run_agent_start_hooks:
            agent_hook_context = AgentHookContext(
                context=context_wrapper.context,
                usage=context_wrapper.usage,
                turn_input=ItemHelpers.input_to_new_input_list(streamed_result.input),
            )
            await asyncio.gather(
                hooks.on_agent_start(agent_hook_context, agent),
                (
                    agent.hooks.on_start(agent_hook_context, agent)
                    if agent.hooks
                    else _coro.noop_coroutine()
                ),
            )

        output_schema = cls._get_output_schema(agent)

        streamed_result.current_agent = agent
        streamed_result._current_agent_output_schema = output_schema

        system_prompt, prompt_config = await asyncio.gather(
            agent.get_system_prompt(context_wrapper),
            agent.get_prompt(context_wrapper),
        )

        handoffs = await cls._get_handoffs(agent, context_wrapper)
        model = cls._get_model(agent, run_config)
        model_settings = agent.model_settings.resolve(run_config.model_settings)
        model_settings = RunImpl.maybe_reset_tool_choice(agent, tool_use_tracker, model_settings)

        final_response: ModelResponse | None = None

        if server_conversation_tracker is not None:
            input = server_conversation_tracker.prepare_input(
                streamed_result.input, streamed_result.new_items, streamed_result.raw_responses
            )
        else:
            # Filter out tool_approval_item items and include all other items
            input = ItemHelpers.input_to_new_input_list(streamed_result.input)
            for item in streamed_result.new_items:
                if item.type == "tool_approval_item":
                    # Skip tool_approval_item items - they're metadata about pending
                    # approvals and shouldn't be sent to the API
                    continue
                # Include all other items
                input_item = item.to_input_item()
                input.append(input_item)

        input = cls._merge_provider_data_in_items(input)

        # THIS IS THE RESOLVED CONFLICT BLOCK
        filtered = await cls._maybe_filter_model_input(
            agent=agent,
            run_config=run_config,
            context_wrapper=context_wrapper,
            input_items=input,
            system_instructions=system_prompt,
        )

        # Call hook just before the model is invoked, with the correct system_prompt.
        await asyncio.gather(
            hooks.on_llm_start(context_wrapper, agent, filtered.instructions, filtered.input),
            (
                agent.hooks.on_llm_start(
                    context_wrapper, agent, filtered.instructions, filtered.input
                )
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        previous_response_id = (
            server_conversation_tracker.previous_response_id
            if server_conversation_tracker
            and server_conversation_tracker.previous_response_id is not None
            else None
        )
        conversation_id = (
            server_conversation_tracker.conversation_id if server_conversation_tracker else None
        )

        # 1. Stream the output events
        async for event in model.stream_response(
            filtered.instructions,
            filtered.input,
            model_settings,
            all_tools,
            output_schema,
            handoffs,
            get_model_tracing_impl(
                run_config.tracing_disabled, run_config.trace_include_sensitive_data
            ),
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt_config,
        ):
            # Emit the raw event ASAP
            streamed_result._event_queue.put_nowait(RawResponsesStreamEvent(data=event))

            if isinstance(event, ResponseCompletedEvent):
                usage = (
                    Usage(
                        requests=1,
                        input_tokens=event.response.usage.input_tokens,
                        output_tokens=event.response.usage.output_tokens,
                        total_tokens=event.response.usage.total_tokens,
                        input_tokens_details=event.response.usage.input_tokens_details,
                        output_tokens_details=event.response.usage.output_tokens_details,
                    )
                    if event.response.usage
                    else Usage()
                )
                final_response = ModelResponse(
                    output=event.response.output,
                    usage=usage,
                    response_id=event.response.id,
                )
                context_wrapper.usage.add(usage)

            if isinstance(event, ResponseOutputItemDoneEvent):
                output_item = event.item

                if isinstance(output_item, _TOOL_CALL_TYPES):
                    output_call_id: str | None = getattr(
                        output_item, "call_id", getattr(output_item, "id", None)
                    )

                    if (
                        output_call_id
                        and isinstance(output_call_id, str)
                        and output_call_id not in emitted_tool_call_ids
                    ):
                        emitted_tool_call_ids.add(output_call_id)

                        tool_item = ToolCallItem(
                            raw_item=cast(ToolCallItemTypes, output_item),
                            agent=agent,
                        )
                        streamed_result._event_queue.put_nowait(
                            RunItemStreamEvent(item=tool_item, name="tool_called")
                        )

                elif isinstance(output_item, ResponseReasoningItem):
                    reasoning_id: str | None = getattr(output_item, "id", None)

                    if reasoning_id and reasoning_id not in emitted_reasoning_item_ids:
                        emitted_reasoning_item_ids.add(reasoning_id)

                        reasoning_item = ReasoningItem(raw_item=output_item, agent=agent)
                        streamed_result._event_queue.put_nowait(
                            RunItemStreamEvent(item=reasoning_item, name="reasoning_item_created")
                        )

        # Call hook just after the model response is finalized.
        if final_response is not None:
            await asyncio.gather(
                (
                    agent.hooks.on_llm_end(context_wrapper, agent, final_response)
                    if agent.hooks
                    else _coro.noop_coroutine()
                ),
                hooks.on_llm_end(context_wrapper, agent, final_response),
            )

        # 2. At this point, the streaming is complete for this turn of the agent loop.
        if not final_response:
            raise ModelBehaviorError("Model did not produce a final response!")

        # 3. Now, we can process the turn as we do in the non-streaming case
        single_step_result = await cls._get_single_step_result_from_response(
            agent=agent,
            original_input=streamed_result.input,
            pre_step_items=streamed_result.new_items,
            new_response=final_response,
            output_schema=output_schema,
            all_tools=all_tools,
            handoffs=handoffs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_use_tracker=tool_use_tracker,
            event_queue=streamed_result._event_queue,
        )

        import dataclasses as _dc

        # Filter out items that have already been sent to avoid duplicates
        items_to_filter = single_step_result.new_step_items

        if emitted_tool_call_ids:
            # Filter out tool call items that were already emitted during streaming
            items_to_filter = [
                item
                for item in items_to_filter
                if not (
                    isinstance(item, ToolCallItem)
                    and (
                        call_id := getattr(
                            item.raw_item, "call_id", getattr(item.raw_item, "id", None)
                        )
                    )
                    and call_id in emitted_tool_call_ids
                )
            ]

        if emitted_reasoning_item_ids:
            # Filter out reasoning items that were already emitted during streaming
            items_to_filter = [
                item
                for item in items_to_filter
                if not (
                    isinstance(item, ReasoningItem)
                    and (reasoning_id := getattr(item.raw_item, "id", None))
                    and reasoning_id in emitted_reasoning_item_ids
                )
            ]

        # Filter out HandoffCallItem to avoid duplicates (already sent earlier)
        items_to_filter = [
            item for item in items_to_filter if not isinstance(item, HandoffCallItem)
        ]

        # Create filtered result and send to queue
        filtered_result = _dc.replace(single_step_result, new_step_items=items_to_filter)
        RunImpl.stream_step_result_to_queue(filtered_result, streamed_result._event_queue)
        return single_step_result

    async def _execute_approved_tools(
        self,
        *,
        agent: Agent[TContext],
        interruptions: list[Any],  # list[RunItem] but avoid circular import
        context_wrapper: RunContextWrapper[TContext],
        generated_items: list[Any],  # list[RunItem]
        run_config: RunConfig,
        hooks: RunHooks[TContext],
    ) -> None:
        """Execute tools that have been approved after an interruption (instance method version).

        This is a thin wrapper around the classmethod version for use in non-streaming mode.
        """
        await AgentRunner._execute_approved_tools_static(
            agent=agent,
            interruptions=interruptions,
            context_wrapper=context_wrapper,
            generated_items=generated_items,
            run_config=run_config,
            hooks=hooks,
        )

    @classmethod
    async def _execute_approved_tools_static(
        cls,
        *,
        agent: Agent[TContext],
        interruptions: list[Any],  # list[RunItem] but avoid circular import
        context_wrapper: RunContextWrapper[TContext],
        generated_items: list[Any],  # list[RunItem]
        run_config: RunConfig,
        hooks: RunHooks[TContext],
    ) -> None:
        """Execute tools that have been approved after an interruption (classmethod version)."""
        from .items import ToolApprovalItem, ToolCallOutputItem

        tool_runs: list[ToolRunFunction] = []

        # Find all tools from the agent
        all_tools = await AgentRunner._get_all_tools(agent, context_wrapper)
        tool_map = {tool.name: tool for tool in all_tools}

        for interruption in interruptions:
            if not isinstance(interruption, ToolApprovalItem):
                continue

            tool_call = interruption.raw_item
            tool_name = tool_call.name

            # Check if this tool was approved
            approval_status = context_wrapper.is_tool_approved(tool_name, tool_call.call_id)
            if approval_status is not True:
                # Not approved or rejected - add rejection message
                if approval_status is False:
                    output = "Tool execution was not approved."
                else:
                    output = "Tool approval status unclear."

                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            # Tool was approved - find it and prepare for execution
            tool = tool_map.get(tool_name)
            if tool is None:
                # Tool not found - add error output
                output = f"Tool '{tool_name}' not found."
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            # Only function tools can be executed via ToolRunFunction
            from .tool import FunctionTool

            if not isinstance(tool, FunctionTool):
                output = f"Tool '{tool_name}' is not a function tool."
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            tool_runs.append(ToolRunFunction(function_tool=tool, tool_call=tool_call))

        # Execute approved tools
        if tool_runs:
            (
                function_results,
                tool_input_guardrail_results,
                tool_output_guardrail_results,
            ) = await RunImpl.execute_function_tool_calls(
                agent=agent,
                tool_runs=tool_runs,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            )

            # Add tool outputs to generated_items
            for result in function_results:
                generated_items.append(result.run_item)

    @classmethod
    async def _run_single_turn(
        cls,
        *,
        agent: Agent[TContext],
        all_tools: list[Tool],
        original_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        should_run_agent_start_hooks: bool,
        tool_use_tracker: AgentToolUseTracker,
        server_conversation_tracker: _ServerConversationTracker | None = None,
        model_responses: list[ModelResponse] | None = None,
    ) -> SingleStepResult:
        # Ensure we run the hooks before anything else
        if should_run_agent_start_hooks:
            agent_hook_context = AgentHookContext(
                context=context_wrapper.context,
                usage=context_wrapper.usage,
                turn_input=ItemHelpers.input_to_new_input_list(original_input),
            )
            await asyncio.gather(
                hooks.on_agent_start(agent_hook_context, agent),
                (
                    agent.hooks.on_start(agent_hook_context, agent)
                    if agent.hooks
                    else _coro.noop_coroutine()
                ),
            )

        system_prompt, prompt_config = await asyncio.gather(
            agent.get_system_prompt(context_wrapper),
            agent.get_prompt(context_wrapper),
        )

        output_schema = cls._get_output_schema(agent)
        handoffs = await cls._get_handoffs(agent, context_wrapper)
        if server_conversation_tracker is not None:
            input = server_conversation_tracker.prepare_input(
                original_input, generated_items, model_responses
            )
        else:
            # Filter out tool_approval_item items and include all other items
            input = ItemHelpers.input_to_new_input_list(original_input)
            for generated_item in generated_items:
                if generated_item.type == "tool_approval_item":
                    # Skip tool_approval_item items - they're metadata about pending
                    # approvals and shouldn't be sent to the API
                    continue
                # Include all other items
                input_item = generated_item.to_input_item()
                input.append(input_item)

        input = cls._merge_provider_data_in_items(input)

        new_response = await cls._get_new_response(
            agent,
            system_prompt,
            input,
            output_schema,
            all_tools,
            handoffs,
            hooks,
            context_wrapper,
            run_config,
            tool_use_tracker,
            server_conversation_tracker,
            prompt_config,
        )

        return await cls._get_single_step_result_from_response(
            agent=agent,
            original_input=original_input,
            pre_step_items=generated_items,
            new_response=new_response,
            output_schema=output_schema,
            all_tools=all_tools,
            handoffs=handoffs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_use_tracker=tool_use_tracker,
        )

    @classmethod
    async def _get_single_step_result_from_response(
        cls,
        *,
        agent: Agent[TContext],
        all_tools: list[Tool],
        original_input: str | list[TResponseInputItem],
        pre_step_items: list[RunItem],
        new_response: ModelResponse,
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        tool_use_tracker: AgentToolUseTracker,
        event_queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel] | None = None,
    ) -> SingleStepResult:
        processed_response = RunImpl.process_model_response(
            agent=agent,
            all_tools=all_tools,
            response=new_response,
            output_schema=output_schema,
            handoffs=handoffs,
        )

        tool_use_tracker.add_tool_use(agent, processed_response.tools_used)

        # Send handoff items immediately for streaming, but avoid duplicates
        if event_queue is not None and processed_response.new_items:
            handoff_items = [
                item for item in processed_response.new_items if isinstance(item, HandoffCallItem)
            ]
            if handoff_items:
                RunImpl.stream_step_items_to_queue(cast(list[RunItem], handoff_items), event_queue)

        return await RunImpl.execute_tools_and_side_effects(
            agent=agent,
            original_input=original_input,
            pre_step_items=pre_step_items,
            new_response=new_response,
            processed_response=processed_response,
            output_schema=output_schema,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
        )

    @classmethod
    async def _get_single_step_result_from_streamed_response(
        cls,
        *,
        agent: Agent[TContext],
        all_tools: list[Tool],
        streamed_result: RunResultStreaming,
        new_response: ModelResponse,
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        tool_use_tracker: AgentToolUseTracker,
    ) -> SingleStepResult:
        original_input = streamed_result.input
        pre_step_items = streamed_result.new_items
        event_queue = streamed_result._event_queue

        processed_response = RunImpl.process_model_response(
            agent=agent,
            all_tools=all_tools,
            response=new_response,
            output_schema=output_schema,
            handoffs=handoffs,
        )
        new_items_processed_response = processed_response.new_items
        tool_use_tracker.add_tool_use(agent, processed_response.tools_used)
        RunImpl.stream_step_items_to_queue(new_items_processed_response, event_queue)

        single_step_result = await RunImpl.execute_tools_and_side_effects(
            agent=agent,
            original_input=original_input,
            pre_step_items=pre_step_items,
            new_response=new_response,
            processed_response=processed_response,
            output_schema=output_schema,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
        )
        new_step_items = [
            item
            for item in single_step_result.new_step_items
            if item not in new_items_processed_response
        ]
        RunImpl.stream_step_items_to_queue(new_step_items, event_queue)

        return single_step_result

    @classmethod
    async def _run_input_guardrails(
        cls,
        agent: Agent[Any],
        guardrails: list[InputGuardrail[TContext]],
        input: str | list[TResponseInputItem],
        context: RunContextWrapper[TContext],
    ) -> list[InputGuardrailResult]:
        if not guardrails:
            return []

        guardrail_tasks = [
            asyncio.create_task(
                RunImpl.run_single_input_guardrail(agent, guardrail, input, context)
            )
            for guardrail in guardrails
        ]

        guardrail_results = []

        for done in asyncio.as_completed(guardrail_tasks):
            result = await done
            if result.output.tripwire_triggered:
                # Cancel all guardrail tasks if a tripwire is triggered.
                for t in guardrail_tasks:
                    t.cancel()
                # Wait for cancellations to propagate by awaiting the cancelled tasks.
                await asyncio.gather(*guardrail_tasks, return_exceptions=True)
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Guardrail tripwire triggered",
                        data={"guardrail": result.guardrail.get_name()},
                    )
                )
                raise InputGuardrailTripwireTriggered(result)
            else:
                guardrail_results.append(result)

        return guardrail_results

    @classmethod
    async def _run_output_guardrails(
        cls,
        guardrails: list[OutputGuardrail[TContext]],
        agent: Agent[TContext],
        agent_output: Any,
        context: RunContextWrapper[TContext],
    ) -> list[OutputGuardrailResult]:
        if not guardrails:
            return []

        guardrail_tasks = [
            asyncio.create_task(
                RunImpl.run_single_output_guardrail(guardrail, agent, agent_output, context)
            )
            for guardrail in guardrails
        ]

        guardrail_results = []

        for done in asyncio.as_completed(guardrail_tasks):
            result = await done
            if result.output.tripwire_triggered:
                # Cancel all guardrail tasks if a tripwire is triggered.
                for t in guardrail_tasks:
                    t.cancel()
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Guardrail tripwire triggered",
                        data={"guardrail": result.guardrail.get_name()},
                    )
                )
                raise OutputGuardrailTripwireTriggered(result)
            else:
                guardrail_results.append(result)

        return guardrail_results

    @classmethod
    async def _get_new_response(
        cls,
        agent: Agent[TContext],
        system_prompt: str | None,
        input: list[TResponseInputItem],
        output_schema: AgentOutputSchemaBase | None,
        all_tools: list[Tool],
        handoffs: list[Handoff],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        tool_use_tracker: AgentToolUseTracker,
        server_conversation_tracker: _ServerConversationTracker | None,
        prompt_config: ResponsePromptParam | None,
    ) -> ModelResponse:
        # Allow user to modify model input right before the call, if configured
        filtered = await cls._maybe_filter_model_input(
            agent=agent,
            run_config=run_config,
            context_wrapper=context_wrapper,
            input_items=input,
            system_instructions=system_prompt,
        )

        model = cls._get_model(agent, run_config)
        model_settings = agent.model_settings.resolve(run_config.model_settings)
        model_settings = RunImpl.maybe_reset_tool_choice(agent, tool_use_tracker, model_settings)

        # If we have run hooks, or if the agent has hooks, we need to call them before the LLM call
        await asyncio.gather(
            hooks.on_llm_start(context_wrapper, agent, filtered.instructions, filtered.input),
            (
                agent.hooks.on_llm_start(
                    context_wrapper,
                    agent,
                    filtered.instructions,  # Use filtered instructions
                    filtered.input,  # Use filtered input
                )
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        previous_response_id = (
            server_conversation_tracker.previous_response_id
            if server_conversation_tracker
            and server_conversation_tracker.previous_response_id is not None
            else None
        )
        conversation_id = (
            server_conversation_tracker.conversation_id if server_conversation_tracker else None
        )

        new_response = await model.get_response(
            system_instructions=filtered.instructions,
            input=filtered.input,
            model_settings=model_settings,
            tools=all_tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=get_model_tracing_impl(
                run_config.tracing_disabled, run_config.trace_include_sensitive_data
            ),
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt_config,
        )

        context_wrapper.usage.add(new_response.usage)

        # If we have run hooks, or if the agent has hooks, we need to call them after the LLM call
        await asyncio.gather(
            (
                agent.hooks.on_llm_end(context_wrapper, agent, new_response)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
            hooks.on_llm_end(context_wrapper, agent, new_response),
        )

        return new_response

    @classmethod
    def _get_output_schema(cls, agent: Agent[Any]) -> AgentOutputSchemaBase | None:
        if agent.output_type is None or agent.output_type is str:
            return None
        elif isinstance(agent.output_type, AgentOutputSchemaBase):
            return agent.output_type

        return AgentOutputSchema(agent.output_type)

    @classmethod
    async def _get_handoffs(
        cls, agent: Agent[Any], context_wrapper: RunContextWrapper[Any]
    ) -> list[Handoff]:
        handoffs = []
        for handoff_item in agent.handoffs:
            if isinstance(handoff_item, Handoff):
                handoffs.append(handoff_item)
            elif isinstance(handoff_item, Agent):
                handoffs.append(handoff(handoff_item))

        async def _check_handoff_enabled(handoff_obj: Handoff) -> bool:
            attr = handoff_obj.is_enabled
            if isinstance(attr, bool):
                return attr
            res = attr(context_wrapper, agent)
            if inspect.isawaitable(res):
                return bool(await res)
            return bool(res)

        results = await asyncio.gather(*(_check_handoff_enabled(h) for h in handoffs))
        enabled: list[Handoff] = [h for h, ok in zip(handoffs, results) if ok]
        return enabled

    @classmethod
    async def _get_all_tools(
        cls, agent: Agent[Any], context_wrapper: RunContextWrapper[Any]
    ) -> list[Tool]:
        return await agent.get_all_tools(context_wrapper)

    @classmethod
    def _get_model(cls, agent: Agent[Any], run_config: RunConfig) -> Model:
        if isinstance(run_config.model, Model):
            return run_config.model
        elif isinstance(run_config.model, str):
            return run_config.model_provider.get_model(run_config.model)
        elif isinstance(agent.model, Model):
            return agent.model

        return run_config.model_provider.get_model(agent.model)

    @classmethod
    def _merge_provider_data_in_items(
        cls, items: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        """Remove providerData fields from items."""
        result = []
        for item in items:
            if isinstance(item, dict):
                merged_item = dict(item)
                # Pop both possible keys (providerData and provider_data)
                provider_data = merged_item.pop("providerData", None)
                if provider_data is None:
                    provider_data = merged_item.pop("provider_data", None)
                # Merge contents if providerData exists and is a dict
                if isinstance(provider_data, dict):
                    # Merge provider_data contents, with existing fields taking precedence
                    for key, value in provider_data.items():
                        if key not in merged_item:
                            merged_item[key] = value
                result.append(cast(TResponseInputItem, merged_item))
            else:
                result.append(item)
        return result

    @classmethod
    async def _prepare_input_with_session(
        cls,
        input: str | list[TResponseInputItem],
        session: Session | None,
        session_input_callback: SessionInputCallback | None,
    ) -> str | list[TResponseInputItem]:
        """Prepare input by combining it with session history if enabled."""
        if session is None:
            return input

        # If the user doesn't specify an input callback and pass a list as input
        if isinstance(input, list) and not session_input_callback:
            raise UserError(
                "When using session memory, list inputs require a "
                "`RunConfig.session_input_callback` to define how they should be merged "
                "with the conversation history. If you don't want to use a callback, "
                "provide your input as a string instead, or disable session memory "
                "(session=None) and pass a list to manage the history manually."
            )

        # Get previous conversation history
        history = await session.get_items()
        history = cls._merge_provider_data_in_items(history)

        # Convert input to list format
        new_input_list = ItemHelpers.input_to_new_input_list(input)

        if session_input_callback is None:
            return history + new_input_list
        elif callable(session_input_callback):
            res = session_input_callback(history, new_input_list)
            if inspect.isawaitable(res):
                res = await res
            if isinstance(res, list):
                res = cls._merge_provider_data_in_items(res)
            return res
        else:
            raise UserError(
                f"Invalid `session_input_callback` value: {session_input_callback}. "
                "Choose between `None` or a custom callable function."
            )

    @classmethod
    async def _save_result_to_session(
        cls,
        session: Session | None,
        original_input: str | list[TResponseInputItem],
        new_items: list[RunItem],
    ) -> None:
        """
        Save the conversation turn to session.
        It does not account for any filtering or modification performed by
        `RunConfig.session_input_callback`.
        """
        if session is None:
            return

        # Convert original input to list format if needed
        input_list = ItemHelpers.input_to_new_input_list(original_input)

        # Convert new items to input format
        new_items_as_input = [item.to_input_item() for item in new_items]

        # Save all items from this turn
        items_to_save = input_list + new_items_as_input
        await session.add_items(items_to_save)

    @staticmethod
    async def _input_guardrail_tripwire_triggered_for_stream(
        streamed_result: RunResultStreaming,
    ) -> bool:
        """Return True if any input guardrail triggered during a streamed run."""

        task = streamed_result._input_guardrails_task
        if task is None:
            return False

        if not task.done():
            await task

        return any(
            guardrail_result.output.tripwire_triggered
            for guardrail_result in streamed_result.input_guardrail_results
        )


DEFAULT_AGENT_RUNNER = AgentRunner()


def _get_tool_call_types() -> tuple[type, ...]:
    normalized_types: list[type] = []
    for type_hint in get_args(ToolCallItemTypes):
        origin = get_origin(type_hint)
        candidate = origin or type_hint
        if isinstance(candidate, type):
            normalized_types.append(candidate)
    return tuple(normalized_types)


_TOOL_CALL_TYPES: tuple[type, ...] = _get_tool_call_types()


def _copy_str_or_list(input: str | list[TResponseInputItem]) -> str | list[TResponseInputItem]:
    if isinstance(input, str):
        return input
    return input.copy()
