from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses as _dc
import inspect
import json
import os
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Union, cast, get_args, get_origin

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
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
    ToolApprovalItem,
    ToolCallItem,
    ToolCallItemTypes,
    ToolCallOutputItem,
    TResponseInputItem,
    normalize_function_call_output_payload,
)
from .lifecycle import AgentHooksBase, RunHooks, RunHooksBase
from .logger import logger
from .memory import Session, SessionInputCallback
from .memory.openai_conversations_session import OpenAIConversationsSession
from .model_settings import ModelSettings
from .models.interface import Model, ModelProvider
from .models.multi_provider import MultiProvider
from .result import RunResult, RunResultStreaming
from .run_context import RunContextWrapper, TContext
from .run_state import RunState, _build_agent_map, _normalize_field_names
from .stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
    StreamEvent,
)
from .tool import FunctionTool, Tool
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
    server_item_ids: set[str] = field(default_factory=set)
    server_tool_call_ids: set[str] = field(default_factory=set)
    sent_item_fingerprints: set[str] = field(default_factory=set)
    sent_initial_input: bool = False
    remaining_initial_input: list[TResponseInputItem] | None = None

    def __post_init__(self):
        import traceback

        stack = "".join(traceback.format_stack()[-5:-1])
        logger.error(
            "[SCT-CREATED] Created _ServerConversationTracker for "
            f"conv_id={self.conversation_id}, prev_resp_id={self.previous_response_id}. "
            f"Stack:\n{stack}"
        )

    def prime_from_state(
        self,
        *,
        original_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
        model_responses: list[ModelResponse],
        session_items: list[TResponseInputItem] | None = None,
    ) -> None:
        if self.sent_initial_input:
            return

        # Normalize items before marking by fingerprint to match what prepare_input will receive
        # This ensures fingerprints match between prime_from_state and prepare_input
        normalized_input = original_input
        if isinstance(original_input, list):
            # Normalize first (converts protocol to API format, normalizes field names)
            normalized = AgentRunner._normalize_input_items(original_input)
            # Filter incomplete function calls after normalization
            normalized_input = AgentRunner._filter_incomplete_function_calls(normalized)

        for item in ItemHelpers.input_to_new_input_list(normalized_input):
            if item is None:
                continue
            self.sent_items.add(id(item))
            # Also mark by server ID if available (for items that come from server
            # with new object IDs)
            item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
            if isinstance(item_id, str):
                self.server_item_ids.add(item_id)
            # Also mark by fingerprint to filter out items even if they're new Python
            # objects. Use normalized items so fingerprints match what prepare_input
            # will receive.
            if isinstance(item, dict):
                try:
                    fp = json.dumps(item, sort_keys=True)
                    self.sent_item_fingerprints.add(fp)
                except Exception:
                    pass

        self.sent_initial_input = True
        self.remaining_initial_input = None

        latest_response = model_responses[-1] if model_responses else None
        for response in model_responses:
            for output_item in response.output:
                if output_item is None:
                    continue
                self.server_items.add(id(output_item))
                item_id = (
                    output_item.get("id")
                    if isinstance(output_item, dict)
                    else getattr(output_item, "id", None)
                )
                if isinstance(item_id, str):
                    self.server_item_ids.add(item_id)
                call_id = (
                    output_item.get("call_id")
                    if isinstance(output_item, dict)
                    else getattr(output_item, "call_id", None)
                )
                has_output_payload = isinstance(output_item, dict) and "output" in output_item
                has_output_payload = has_output_payload or hasattr(output_item, "output")
                if isinstance(call_id, str) and has_output_payload:
                    self.server_tool_call_ids.add(call_id)

        if self.conversation_id is None and latest_response and latest_response.response_id:
            self.previous_response_id = latest_response.response_id

        if session_items:
            for item in session_items:
                item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                if isinstance(item_id, str):
                    self.server_item_ids.add(item_id)
                call_id = (
                    item.get("call_id") or item.get("callId")
                    if isinstance(item, dict)
                    else getattr(item, "call_id", None)
                )
                has_output = isinstance(item, dict) and "output" in item
                has_output = has_output or hasattr(item, "output")
                if isinstance(call_id, str) and has_output:
                    self.server_tool_call_ids.add(call_id)
                # Also mark by fingerprint to filter out items even if they're new
                # Python objects. This ensures items already in the conversation
                # are filtered correctly when resuming.
                if isinstance(item, dict):
                    try:
                        fp = json.dumps(item, sort_keys=True)
                        self.sent_item_fingerprints.add(fp)
                    except Exception:
                        pass

        for item in generated_items:  # type: ignore[assignment]
            # Cast to RunItem since generated_items is typed as list[RunItem]
            run_item: RunItem = cast(RunItem, item)
            raw_item = run_item.raw_item
            if raw_item is None:
                continue
            raw_item_id = id(raw_item)
            # Only mark as sent if already in server_items
            if raw_item_id in self.server_items:
                self.sent_items.add(raw_item_id)
            # Always mark by fingerprint to filter out items even if they're new Python objects
            # This ensures items already in the conversation are filtered correctly
            if isinstance(raw_item, dict):
                try:
                    fp = json.dumps(raw_item, sort_keys=True)
                    self.sent_item_fingerprints.add(fp)
                except Exception:
                    pass
            # Also mark by server ID if available
            item_id = (
                raw_item.get("id") if isinstance(raw_item, dict) else getattr(raw_item, "id", None)
            )
            if isinstance(item_id, str):
                self.server_item_ids.add(item_id)
            # Mark tool call IDs for function call outputs
            call_id = (
                raw_item.get("call_id")
                if isinstance(raw_item, dict)
                else getattr(raw_item, "call_id", None)
            )
            has_output_payload = isinstance(raw_item, dict) and "output" in raw_item
            has_output_payload = has_output_payload or hasattr(raw_item, "output")
            if isinstance(call_id, str) and has_output_payload:
                self.server_tool_call_ids.add(call_id)

    def track_server_items(self, model_response: ModelResponse | None) -> None:
        if model_response is None:
            return

        # Collect fingerprints of items echoed by the server to filter remaining_initial_input
        server_item_fingerprints: set[str] = set()
        for output_item in model_response.output:
            if output_item is None:
                continue
            self.server_items.add(id(output_item))
            item_id = (
                output_item.get("id")
                if isinstance(output_item, dict)
                else getattr(output_item, "id", None)
            )
            if isinstance(item_id, str):
                self.server_item_ids.add(item_id)
            call_id = (
                output_item.get("call_id")
                if isinstance(output_item, dict)
                else getattr(output_item, "call_id", None)
            )
            has_output_payload = isinstance(output_item, dict) and "output" in output_item
            has_output_payload = has_output_payload or hasattr(output_item, "output")
            if isinstance(call_id, str) and has_output_payload:
                self.server_tool_call_ids.add(call_id)
            # Also mark by fingerprint to filter out items even if they're new Python objects
            # This ensures items echoed by the server are filtered correctly in prepare_input
            if isinstance(output_item, dict):
                try:
                    fp = json.dumps(output_item, sort_keys=True)
                    self.sent_item_fingerprints.add(fp)
                    server_item_fingerprints.add(fp)
                except Exception:
                    pass

        # Filter remaining_initial_input if items match server items by fingerprint
        # This ensures items echoed by the server are removed from remaining_initial_input
        # Match JS: markInputAsSent filters remainingInitialInput based on what was delivered
        if self.remaining_initial_input and server_item_fingerprints:
            remaining: list[TResponseInputItem] = []
            for pending in self.remaining_initial_input:
                if isinstance(pending, dict):
                    try:
                        serialized = json.dumps(pending, sort_keys=True)
                        if serialized in server_item_fingerprints:
                            continue
                    except Exception:
                        pass
                remaining.append(pending)
            self.remaining_initial_input = remaining or None

        # Update previous_response_id when using previous_response_id mode or auto mode
        if (
            self.conversation_id is None
            and (self.previous_response_id is not None or self.auto_previous_response_id)
            and model_response.response_id is not None
        ):
            self.previous_response_id = model_response.response_id

    def mark_input_as_sent(self, items: Sequence[TResponseInputItem]) -> None:
        if not items:
            return

        delivered_ids: set[int] = set()
        for item in items:
            if item is None:
                continue
            delivered_ids.add(id(item))
            self.sent_items.add(id(item))
            if isinstance(item, dict):
                try:
                    fp = json.dumps(item, sort_keys=True)
                    self.sent_item_fingerprints.add(fp)
                except Exception:
                    pass

        if not self.remaining_initial_input:
            return

        # Prefer object identity, but also fall back to content comparison to handle
        # cases where filtering produces cloned dicts. Mirrors JS intent (drop initial
        # items once delivered) while being resilient to Python-side copies.
        delivered_by_content: set[str] = set()
        for item in items:
            if isinstance(item, dict):
                try:
                    delivered_by_content.add(json.dumps(item, sort_keys=True))
                except Exception:
                    continue

        remaining: list[TResponseInputItem] = []
        for pending in self.remaining_initial_input:
            if id(pending) in delivered_ids:
                continue
            if isinstance(pending, dict):
                try:
                    serialized = json.dumps(pending, sort_keys=True)
                    if serialized in delivered_by_content:
                        continue
                except Exception:
                    pass
            remaining.append(pending)

        # Only set to None if empty after filtering
        # Don't unconditionally set to None for server-managed conversations
        # markInputAsSent filters remainingInitialInput based on what was delivered
        self.remaining_initial_input = remaining or None

    def rewind_input(self, items: Sequence[TResponseInputItem]) -> None:
        """
        Rewind previously marked inputs so they can be resent (e.g., after a conversation lock).
        """
        if not items:
            return

        rewind_items: list[TResponseInputItem] = []
        for item in items:
            if item is None:
                continue
            rewind_items.append(item)
            self.sent_items.discard(id(item))

            if isinstance(item, dict):
                try:
                    fp = json.dumps(item, sort_keys=True)
                    self.sent_item_fingerprints.discard(fp)
                except Exception:
                    pass

        if not rewind_items:
            return

        logger.debug("Queued %d items to resend after conversation retry", len(rewind_items))
        existing = self.remaining_initial_input or []
        self.remaining_initial_input = rewind_items + existing

    def prepare_input(
        self,
        original_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
        model_responses: list[ModelResponse] | None = None,
    ) -> list[TResponseInputItem]:
        input_items: list[TResponseInputItem] = []

        if not self.sent_initial_input:
            initial_items = ItemHelpers.input_to_new_input_list(original_input)
            # Add all initial items without filtering
            # Filtering happens via markInputAsSent after items are sent to the API
            input_items.extend(initial_items)
            # Always set remaining_initial_input to filtered initial items
            # markInputAsSent will filter it later based on what was actually sent
            filtered_initials = []
            for item in initial_items:
                if item is None or isinstance(item, (str, bytes)):
                    continue
                filtered_initials.append(item)
            self.remaining_initial_input = filtered_initials or None
            self.sent_initial_input = True
        elif self.remaining_initial_input:
            input_items.extend(self.remaining_initial_input)

        for item in generated_items:  # type: ignore[assignment]
            # Cast to RunItem since generated_items is typed as list[RunItem]
            run_item: RunItem = cast(RunItem, item)
            if run_item.type == "tool_approval_item":
                continue

            raw_item = run_item.raw_item
            if raw_item is None:
                continue

            item_id = (
                raw_item.get("id") if isinstance(raw_item, dict) else getattr(raw_item, "id", None)
            )
            if isinstance(item_id, str) and item_id in self.server_item_ids:
                continue

            call_id = (
                raw_item.get("call_id")
                if isinstance(raw_item, dict)
                else getattr(raw_item, "call_id", None)
            )
            has_output_payload = isinstance(raw_item, dict) and "output" in raw_item
            has_output_payload = has_output_payload or hasattr(raw_item, "output")
            if (
                isinstance(call_id, str)
                and has_output_payload
                and call_id in self.server_tool_call_ids
            ):
                continue

            raw_item_id = id(raw_item)
            if raw_item_id in self.sent_items or raw_item_id in self.server_items:
                continue

            input_items.append(cast(TResponseInputItem, raw_item))

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

        # If the caller supplies a session and a list input without a
        # session_input_callback, raise. This mirrors JS validation and prevents
        # ambiguous history handling.
        if (
            session is not None
            and not isinstance(input, RunState)
            and isinstance(input, list)
            and run_config.session_input_callback is None
        ):
            raise UserError(
                "list inputs require a `RunConfig.session_input_callback` when used with a "
                "session to manage the history manually."
            )

        # Check if we're resuming from a RunState
        is_resumed_state = isinstance(input, RunState)
        run_state: RunState[TContext] | None = None
        starting_input = input if not is_resumed_state else None
        original_user_input: str | list[TResponseInputItem] | None = None
        # Track session input items for persistence.
        # When resuming from state, this should be [] since input items were already saved
        # in the previous run before the state was saved.
        session_input_items_for_persistence: list[TResponseInputItem] | None = (
            [] if (session is not None and is_resumed_state) else None
        )

        if is_resumed_state:
            # Resuming from a saved state
            run_state = cast(RunState[TContext], input)
            # When resuming, use the original_input from state.
            # primeFromState will mark items as sent so prepareInput skips them
            starting_input = run_state._original_input
            # When resuming, use the original_input from state.
            # primeFromState will mark items as sent so prepareInput skips them
            original_user_input = _copy_str_or_list(run_state._original_input)
            # Normalize items to remove top-level providerData and convert protocol to API format
            # Then filter incomplete function calls to ensure API compatibility
            if isinstance(original_user_input, list):
                # Normalize first (converts protocol format to API format, normalizes field names)
                normalized = AgentRunner._normalize_input_items(original_user_input)
                # Filter incomplete function calls after normalization
                # This ensures consistent field names (call_id vs callId) for matching
                prepared_input: str | list[TResponseInputItem] = (
                    AgentRunner._filter_incomplete_function_calls(normalized)
                )
            else:
                prepared_input = original_user_input

            # Override context with the state's context if not provided
            if context is None and run_state._context is not None:
                context = run_state._context.context
        else:
            # Keep original user input separate from session-prepared input
            raw_input = cast(Union[str, list[TResponseInputItem]], input)
            original_user_input = raw_input

            # Match JS: serverManagesConversation is ONLY based on
            # conversationId/previousResponseId. Sessions remain usable
            # alongside server-managed conversations (e.g., OpenAIConversationsSession)
            # so callers can reuse callbacks, resume-from-state logic, and other
            # helpers without duplicating remote history, so persistence is gated
            # on serverManagesConversation.
            server_manages_conversation = (
                conversation_id is not None or previous_response_id is not None
            )

            if server_manages_conversation:
                prepared_input, _ = await self._prepare_input_with_session(
                    raw_input,
                    session,
                    run_config.session_input_callback,
                    include_history_in_prepared_input=False,
                    preserve_dropped_new_items=True,
                )
                # For state serialization, mirror JS behavior: keep only the
                # turn input, not merged history.
                original_input_for_state = raw_input
                session_input_items_for_persistence = []
            else:
                # When server doesn't manage conversation, use full history for both
                (
                    prepared_input,
                    session_input_items_for_persistence,
                ) = await self._prepare_input_with_session(
                    raw_input,
                    session,
                    run_config.session_input_callback,
                )
                original_input_for_state = prepared_input

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

        if server_conversation_tracker is not None and is_resumed_state and run_state is not None:
            session_items: list[TResponseInputItem] | None = None
            if session is not None:
                try:
                    session_items = await session.get_items()
                except Exception:
                    session_items = None
            server_conversation_tracker.prime_from_state(
                original_input=run_state._original_input,
                generated_items=run_state._generated_items,
                model_responses=run_state._model_responses,
                session_items=session_items,
            )

        # Always create a fresh tool_use_tracker
        # (it's rebuilt from the run state if needed during execution)
        tool_use_tracker = AgentToolUseTracker()
        if is_resumed_state and run_state is not None:
            self._hydrate_tool_use_tracker(tool_use_tracker, run_state, starting_agent)

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
                # Normalize original_input: remove top-level providerData,
                # convert protocol to API format, then filter incomplete function calls
                raw_original_input = run_state._original_input
                if isinstance(raw_original_input, list):
                    # Normalize first (converts protocol to API format, normalizes field names)
                    normalized = AgentRunner._normalize_input_items(raw_original_input)
                    # Filter incomplete function calls after normalization
                    # This ensures consistent field names (call_id vs callId) for matching
                    original_input: str | list[TResponseInputItem] = (
                        AgentRunner._filter_incomplete_function_calls(normalized)
                    )
                else:
                    original_input = raw_original_input
                generated_items = run_state._generated_items
                model_responses = run_state._model_responses
                if (
                    run_state._current_turn_persisted_item_count == 0
                    and generated_items
                    and server_conversation_tracker is None
                ):
                    run_state._current_turn_persisted_item_count = len(generated_items)
                # Cast to the correct type since we know this is TContext
                context_wrapper = cast(RunContextWrapper[TContext], run_state._context)
            else:
                # Fresh run
                current_turn = 0
                original_input = _copy_str_or_list(original_input_for_state)
                generated_items = []
                model_responses = []
                context_wrapper = RunContextWrapper(
                    context=context,  # type: ignore
                )
                # Create RunState for fresh runs to track persisted item count
                # This ensures counter is properly maintained across streaming iterations
                run_state = RunState(
                    context=context_wrapper,
                    original_input=original_input,
                    starting_agent=starting_agent,
                    max_turns=max_turns,
                )

            pending_server_items: list[RunItem] | None = None
            input_guardrail_results: list[InputGuardrailResult] = []
            tool_input_guardrail_results: list[ToolInputGuardrailResult] = []
            tool_output_guardrail_results: list[ToolOutputGuardrailResult] = []

            current_span: Span[AgentSpanData] | None = None
            # When resuming from state, use the current agent from the state (which may be different
            # from starting_agent if a handoff occurred). Otherwise use starting_agent.
            if is_resumed_state and run_state is not None and run_state._current_agent is not None:
                current_agent = run_state._current_agent
            else:
                current_agent = starting_agent
            should_run_agent_start_hooks = True

            # CRITICAL: Do not save input items here in blocking mode.
            # Input and output items are saved together at the end of the run.
            # Skip saving if resuming from state or if the server manages the
            # conversation. Store original_user_input for later saving with
            # output items. When resuming, session_input_items_for_persistence is []
            # so there are no input items to save.
            if (
                not is_resumed_state
                and server_conversation_tracker is None
                and original_user_input is not None
                and session_input_items_for_persistence is None
            ):
                # Store input items to save later with output items.
                # Only set this if we haven't already set it (e.g., when server
                # manages conversation, it's already []).
                session_input_items_for_persistence = ItemHelpers.input_to_new_input_list(
                    original_user_input
                )

            if (
                session is not None
                and server_conversation_tracker is None
                and session_input_items_for_persistence
            ):
                await self._save_result_to_session(
                    session, session_input_items_for_persistence, [], run_state
                )
                # Prevent double-saving later; the initial input has been persisted.
                session_input_items_for_persistence = []

            try:
                while True:
                    resuming_turn = is_resumed_state
                    # Check if we're resuming from an interrupted state
                    # (matching TypeScript behavior). We check
                    # run_state._current_step every iteration, not just when
                    # is_resumed_state is True.
                    if run_state is not None and run_state._current_step is not None:
                        if isinstance(run_state._current_step, NextStepInterruption):
                            logger.debug("Continuing from interruption")
                            if (
                                not run_state._model_responses
                                or not run_state._last_processed_response
                            ):
                                raise UserError("No model response found in previous state")

                            turn_result = await RunImpl.resolve_interrupted_turn(
                                agent=current_agent,
                                original_input=original_input,
                                original_pre_step_items=generated_items,
                                new_response=run_state._model_responses[-1],
                                processed_response=run_state._last_processed_response,
                                hooks=hooks,
                                context_wrapper=context_wrapper,
                                run_config=run_config,
                                run_state=run_state,
                            )

                            if run_state._last_processed_response is not None:
                                tool_use_tracker.add_tool_use(
                                    current_agent,
                                    run_state._last_processed_response.tools_used,
                                )

                            pending_approval_items: list[ToolApprovalItem] = []
                            if isinstance(run_state._current_step, NextStepInterruption):
                                # Filter to only ToolApprovalItem instances
                                pending_approval_items = [
                                    item
                                    for item in run_state._current_step.interruptions
                                    if isinstance(item, ToolApprovalItem)
                                ]

                            rewind_count = 0
                            if pending_approval_items:

                                def _get_approval_identity(
                                    approval: ToolApprovalItem,
                                ) -> str | None:
                                    raw_item = approval.raw_item
                                    if isinstance(raw_item, dict):
                                        if raw_item.get("type") == "function_call" and raw_item.get(
                                            "callId"
                                        ):
                                            return f"function_call:{raw_item['callId']}"
                                        call_id = (
                                            raw_item.get("callId")
                                            or raw_item.get("call_id")
                                            or raw_item.get("id")
                                        )
                                        if call_id:
                                            return f"{raw_item.get('type', 'unknown')}:{call_id}"
                                        item_id = raw_item.get("id")
                                        if item_id:
                                            return f"{raw_item.get('type', 'unknown')}:{item_id}"
                                    elif isinstance(raw_item, ResponseFunctionToolCall):
                                        if raw_item.call_id:
                                            return f"function_call:{raw_item.call_id}"
                                    return None

                                pending_identities = set()
                                for approval in pending_approval_items:
                                    identity = _get_approval_identity(approval)
                                    if identity:
                                        pending_identities.add(identity)

                                if pending_identities:
                                    for item in reversed(run_state._generated_items):
                                        if not isinstance(item, ToolApprovalItem):
                                            continue
                                        identity = _get_approval_identity(item)
                                        if not identity or identity not in pending_identities:
                                            continue
                                        rewind_count += 1
                                        pending_identities.discard(identity)
                                        if not pending_identities:
                                            break

                            if rewind_count > 0:
                                run_state._current_turn_persisted_item_count = max(
                                    0,
                                    run_state._current_turn_persisted_item_count - rewind_count,
                                )

                            # Update state from turn result
                            # Assign without type annotation to avoid redefinition error
                            original_input = turn_result.original_input
                            generated_items = turn_result.generated_items
                            run_state._original_input = _copy_str_or_list(original_input)
                            run_state._generated_items = generated_items
                            # Type assertion: next_step can be various types, but we assign it
                            run_state._current_step = turn_result.next_step  # type: ignore[assignment]

                            # Persist newly produced items (e.g., tool outputs) from the resumed
                            # interruption before continuing the turn so they aren't dropped on
                            # the next iteration.
                            if (
                                session is not None
                                and server_conversation_tracker is None
                                and turn_result.new_step_items
                            ):
                                persisted_before_partial = (
                                    run_state._current_turn_persisted_item_count
                                    if run_state is not None
                                    else 0
                                )
                                await self._save_result_to_session(
                                    session, [], turn_result.new_step_items, None
                                )
                                if run_state is not None:
                                    run_state._current_turn_persisted_item_count = (
                                        persisted_before_partial + len(turn_result.new_step_items)
                                    )

                            # Handle the next step
                            if isinstance(turn_result.next_step, NextStepInterruption):
                                # Still in an interruption - return result to avoid infinite loop
                                # Ensure starting_input is not None and not RunState
                                interruption_result_input: str | list[TResponseInputItem] = (
                                    starting_input
                                    if starting_input is not None
                                    and not isinstance(starting_input, RunState)
                                    else ""
                                )
                                result = RunResult(
                                    input=interruption_result_input,
                                    new_items=generated_items,
                                    raw_responses=model_responses,
                                    final_output=None,
                                    _last_agent=current_agent,
                                    input_guardrail_results=input_guardrail_results,
                                    output_guardrail_results=[],
                                    tool_input_guardrail_results=(
                                        turn_result.tool_input_guardrail_results
                                    ),
                                    tool_output_guardrail_results=(
                                        turn_result.tool_output_guardrail_results
                                    ),
                                    context_wrapper=context_wrapper,
                                    interruptions=turn_result.next_step.interruptions,
                                    _tool_use_tracker_snapshot=self._serialize_tool_use_tracker(
                                        tool_use_tracker
                                    ),
                                )
                                result._original_input = _copy_str_or_list(original_input)
                                return result

                            # If continuing from interruption with next_step_run_again,
                            # continue the loop.
                            if isinstance(turn_result.next_step, NextStepRunAgain):
                                continue

                            # Handle other next step types (handoff, final output) in
                            # the normal flow below. For now, treat as if we got this
                            # from _run_single_turn.
                            model_responses.append(turn_result.model_response)
                            tool_input_guardrail_results.extend(
                                turn_result.tool_input_guardrail_results
                            )
                            tool_output_guardrail_results.extend(
                                turn_result.tool_output_guardrail_results
                            )

                            # Process the next step
                            if isinstance(turn_result.next_step, NextStepFinalOutput):
                                output_guardrail_results = await self._run_output_guardrails(
                                    current_agent.output_guardrails
                                    + (run_config.output_guardrails or []),
                                    current_agent,
                                    turn_result.next_step.output,
                                    context_wrapper,
                                )
                                result = RunResult(
                                    input=turn_result.original_input,
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
                                    _tool_use_tracker_snapshot=self._serialize_tool_use_tracker(
                                        tool_use_tracker
                                    ),
                                )
                                if server_conversation_tracker is None:
                                    # Save both input and output items together at the end.
                                    # When resuming from state, session_input_items_for_save
                                    # is [] since input items were already saved before the state
                                    # was saved.
                                    input_items_for_save_1: list[TResponseInputItem] = (
                                        session_input_items_for_persistence
                                        if session_input_items_for_persistence is not None
                                        else []
                                    )
                                    await self._save_result_to_session(
                                        session, input_items_for_save_1, generated_items, run_state
                                    )
                                result._original_input = _copy_str_or_list(original_input)
                                return result
                            elif isinstance(turn_result.next_step, NextStepHandoff):
                                current_agent = cast(
                                    Agent[TContext], turn_result.next_step.new_agent
                                )
                                # Assign without type annotation to avoid redefinition error
                                starting_input = turn_result.original_input
                                original_input = turn_result.original_input
                                if current_span is not None:
                                    current_span.finish(reset_current=True)
                                current_span = None
                                should_run_agent_start_hooks = True
                                continue

                            # If we get here, it's a NextStepRunAgain, so continue the loop
                            continue

                    # Normal flow: if we don't have a current step, treat this as a new run
                    if run_state is not None:
                        if run_state._current_step is None:
                            run_state._current_step = NextStepRunAgain()  # type: ignore[assignment]
                    all_tools = await AgentRunner._get_all_tools(current_agent, context_wrapper)

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

                    if (
                        run_state is not None
                        and not resuming_turn
                        and not isinstance(run_state._current_step, NextStepRunAgain)
                    ):
                        run_state._current_turn_persisted_item_count = 0

                    logger.debug("Running agent %s (turn %s)", current_agent.name, current_turn)

                    items_for_model = (
                        pending_server_items
                        if server_conversation_tracker is not None and pending_server_items
                        else generated_items
                    )

                    if current_turn <= 1:
                        # Separate guardrails based on execution mode.
                        all_input_guardrails = starting_agent.input_guardrails + (
                            run_config.input_guardrails or []
                        )
                        sequential_guardrails = [
                            g for g in all_input_guardrails if not g.run_in_parallel
                        ]
                        parallel_guardrails = [g for g in all_input_guardrails if g.run_in_parallel]

                        # Run blocking guardrails first, before agent starts.
                        try:
                            sequential_results = []
                            if sequential_guardrails:
                                sequential_results = await self._run_input_guardrails(
                                    starting_agent,
                                    sequential_guardrails,
                                    _copy_str_or_list(prepared_input),
                                    context_wrapper,
                                )
                        except InputGuardrailTripwireTriggered:
                            if session is not None and server_conversation_tracker is None:
                                if session_input_items_for_persistence is None and (
                                    original_user_input is not None
                                ):
                                    session_input_items_for_persistence = (
                                        ItemHelpers.input_to_new_input_list(original_user_input)
                                    )
                                input_items_for_save: list[TResponseInputItem] = (
                                    session_input_items_for_persistence
                                    if session_input_items_for_persistence is not None
                                    else []
                                )
                                await self._save_result_to_session(
                                    session, input_items_for_save, [], run_state
                                )
                            raise

                        # Run the agent turn and parallel guardrails concurrently when configured.
                        parallel_results: list[InputGuardrailResult] = []
                        parallel_guardrail_task: asyncio.Task[list[InputGuardrailResult]] | None = (
                            None
                        )
                        model_task: asyncio.Task[SingleStepResult] | None = None

                        if parallel_guardrails:
                            parallel_guardrail_task = asyncio.create_task(
                                self._run_input_guardrails(
                                    starting_agent,
                                    parallel_guardrails,
                                    _copy_str_or_list(prepared_input),
                                    context_wrapper,
                                )
                            )

                        # Kick off model call
                        # Ensure starting_input is the correct type (not RunState or None)
                        starting_input_for_turn: str | list[TResponseInputItem] = (
                            starting_input
                            if starting_input is not None
                            and not isinstance(starting_input, RunState)
                            else ""
                        )
                        model_task = asyncio.create_task(
                            self._run_single_turn(
                                agent=current_agent,
                                all_tools=all_tools,
                                original_input=original_input,
                                starting_input=starting_input_for_turn,
                                generated_items=items_for_model,
                                hooks=hooks,
                                context_wrapper=context_wrapper,
                                run_config=run_config,
                                should_run_agent_start_hooks=should_run_agent_start_hooks,
                                tool_use_tracker=tool_use_tracker,
                                server_conversation_tracker=server_conversation_tracker,
                                model_responses=model_responses,
                                session=session,
                                session_items_to_rewind=session_input_items_for_persistence
                                if not is_resumed_state and server_conversation_tracker is None
                                else None,
                            )
                        )

                        if parallel_guardrail_task:
                            done, pending = await asyncio.wait(
                                {parallel_guardrail_task, model_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )

                            if parallel_guardrail_task in done:
                                try:
                                    parallel_results = parallel_guardrail_task.result()
                                except InputGuardrailTripwireTriggered:
                                    model_task.cancel()
                                    await asyncio.gather(model_task, return_exceptions=True)
                                    if session is not None and server_conversation_tracker is None:
                                        if session_input_items_for_persistence is None and (
                                            original_user_input is not None
                                        ):
                                            session_input_items_for_persistence = (
                                                ItemHelpers.input_to_new_input_list(
                                                    original_user_input
                                                )
                                            )
                                        input_items_for_save_guardrail: list[TResponseInputItem] = (
                                            session_input_items_for_persistence
                                            if session_input_items_for_persistence is not None
                                            else []
                                        )
                                        await self._save_result_to_session(
                                            session, input_items_for_save_guardrail, [], run_state
                                        )
                                    raise
                                turn_result = await model_task
                            else:
                                # Model finished first; await guardrails afterwards.
                                turn_result = await model_task
                                try:
                                    parallel_results = await parallel_guardrail_task
                                except InputGuardrailTripwireTriggered:
                                    if session is not None and server_conversation_tracker is None:
                                        if session_input_items_for_persistence is None and (
                                            original_user_input is not None
                                        ):
                                            session_input_items_for_persistence = (
                                                ItemHelpers.input_to_new_input_list(
                                                    original_user_input
                                                )
                                            )
                                        input_items_for_save_guardrail2: list[
                                            TResponseInputItem
                                        ] = (
                                            session_input_items_for_persistence
                                            if session_input_items_for_persistence is not None
                                            else []
                                        )
                                        await self._save_result_to_session(
                                            session, input_items_for_save_guardrail2, [], run_state
                                        )
                                    raise
                        else:
                            turn_result = await model_task

                        # Combine sequential and parallel results before proceeding.
                        input_guardrail_results = sequential_results + parallel_results
                    else:
                        # Ensure starting_input is the correct type (not RunState or None)
                        starting_input_for_turn2: str | list[TResponseInputItem] = (
                            starting_input
                            if starting_input is not None
                            and not isinstance(starting_input, RunState)
                            else ""
                        )
                        turn_result = await self._run_single_turn(
                            agent=current_agent,
                            all_tools=all_tools,
                            original_input=original_input,
                            starting_input=starting_input_for_turn2,
                            generated_items=items_for_model,
                            hooks=hooks,
                            context_wrapper=context_wrapper,
                            run_config=run_config,
                            should_run_agent_start_hooks=should_run_agent_start_hooks,
                            tool_use_tracker=tool_use_tracker,
                            server_conversation_tracker=server_conversation_tracker,
                            model_responses=model_responses,
                            session=session,
                            session_items_to_rewind=session_input_items_for_persistence
                            if not is_resumed_state and server_conversation_tracker is None
                            else None,
                        )

                    # Start hooks should only run on the first turn unless reset by a handoff.
                    should_run_agent_start_hooks = False

                    # Update shared state after each turn.
                    model_responses.append(turn_result.model_response)
                    original_input = turn_result.original_input
                    generated_items = turn_result.generated_items
                    if server_conversation_tracker is not None:
                        pending_server_items = list(turn_result.new_step_items)

                    if server_conversation_tracker is not None:
                        server_conversation_tracker.track_server_items(turn_result.model_response)

                    # Collect tool guardrail results from this turn
                    tool_input_guardrail_results.extend(turn_result.tool_input_guardrail_results)
                    tool_output_guardrail_results.extend(turn_result.tool_output_guardrail_results)

                    items_to_save_turn = list(turn_result.new_step_items)
                    if not isinstance(turn_result.next_step, NextStepInterruption):
                        # When resuming a turn we have already persisted the tool_call items;
                        # avoid writing them again. For fresh turns we still need to persist them.
                        if (
                            is_resumed_state
                            and run_state
                            and run_state._current_turn_persisted_item_count > 0
                        ):
                            items_to_save_turn = [
                                item for item in items_to_save_turn if item.type != "tool_call_item"
                            ]
                        if server_conversation_tracker is None and session is not None:
                            output_call_ids = {
                                item.raw_item.get("call_id")
                                if isinstance(item.raw_item, dict)
                                else getattr(item.raw_item, "call_id", None)
                                for item in turn_result.new_step_items
                                if item.type == "tool_call_output_item"
                            }
                            for item in generated_items:
                                if item.type != "tool_call_item":
                                    continue
                                call_id = (
                                    item.raw_item.get("call_id")
                                    if isinstance(item.raw_item, dict)
                                    else getattr(item.raw_item, "call_id", None)
                                )
                                if (
                                    call_id in output_call_ids
                                    and item not in items_to_save_turn
                                    and not (
                                        run_state
                                        and run_state._current_turn_persisted_item_count > 0
                                    )
                                ):
                                    items_to_save_turn.append(item)
                            if items_to_save_turn:
                                logger.debug(
                                    "Persisting turn items (types=%s)",
                                    [item.type for item in items_to_save_turn],
                                )
                                if is_resumed_state and run_state is not None:
                                    await self._save_result_to_session(
                                        session, [], items_to_save_turn, None
                                    )
                                    run_state._current_turn_persisted_item_count += len(
                                        items_to_save_turn
                                    )
                                else:
                                    await self._save_result_to_session(
                                        session, [], items_to_save_turn, run_state
                                    )

                    # After the first resumed turn, treat subsequent turns as fresh
                    # so counters and input saving behave normally.
                    is_resumed_state = False

                    try:
                        if isinstance(turn_result.next_step, NextStepFinalOutput):
                            output_guardrail_results = await self._run_output_guardrails(
                                current_agent.output_guardrails
                                + (run_config.output_guardrails or []),
                                current_agent,
                                turn_result.next_step.output,
                                context_wrapper,
                            )

                            # Ensure starting_input is not None and not RunState
                            final_output_result_input: str | list[TResponseInputItem] = (
                                starting_input
                                if starting_input is not None
                                and not isinstance(starting_input, RunState)
                                else ""
                            )
                            result = RunResult(
                                input=final_output_result_input,
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
                                _tool_use_tracker_snapshot=self._serialize_tool_use_tracker(
                                    tool_use_tracker
                                ),
                            )
                            if run_state is not None:
                                result._current_turn_persisted_item_count = (
                                    run_state._current_turn_persisted_item_count
                                )
                            result._original_input = _copy_str_or_list(original_input)
                            return result
                        elif isinstance(turn_result.next_step, NextStepInterruption):
                            # Tool approval is needed - return a result with interruptions
                            if session is not None and server_conversation_tracker is None:
                                if not any(
                                    guardrail_result.output.tripwire_triggered
                                    for guardrail_result in input_guardrail_results
                                ):
                                    # Filter out tool_approval_item items -
                                    # they shouldn't be saved to session.
                                    # Save both input and output items together at the end.
                                    # When resuming from state, session_input_items_for_persistence
                                    # is [] since input items were already saved before the state
                                    # was saved.
                                    input_items_for_save_interruption: list[TResponseInputItem] = (
                                        session_input_items_for_persistence
                                        if session_input_items_for_persistence is not None
                                        else []
                                    )
                                    await self._save_result_to_session(
                                        session,
                                        input_items_for_save_interruption,
                                        generated_items,
                                        run_state,
                                    )
                            # Ensure starting_input is not None and not RunState
                            interruption_result_input2: str | list[TResponseInputItem] = (
                                starting_input
                                if starting_input is not None
                                and not isinstance(starting_input, RunState)
                                else ""
                            )
                            result = RunResult(
                                input=interruption_result_input2,
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
                                _tool_use_tracker_snapshot=self._serialize_tool_use_tracker(
                                    tool_use_tracker
                                ),
                            )
                            if run_state is not None:
                                result._current_turn_persisted_item_count = (
                                    run_state._current_turn_persisted_item_count
                                )
                            result._original_input = _copy_str_or_list(original_input)
                            return result
                        elif isinstance(turn_result.next_step, NextStepHandoff):
                            current_agent = cast(Agent[TContext], turn_result.next_step.new_agent)
                            # Next agent starts with the nested/filtered input.
                            # Assign without type annotation to avoid redefinition error
                            starting_input = turn_result.original_input
                            original_input = turn_result.original_input
                            current_span.finish(reset_current=True)
                            current_span = None
                            should_run_agent_start_hooks = True
                        elif isinstance(turn_result.next_step, NextStepRunAgain):
                            continue
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

        # If the caller supplies a session and a list input without a
        # session_input_callback, raise early to match blocking behavior.
        if (
            session is not None
            and not isinstance(input, RunState)
            and isinstance(input, list)
            and run_config.session_input_callback is None
        ):
            raise UserError(
                "list inputs require a `RunConfig.session_input_callback` when used with a "
                "session to manage the history manually."
            )

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
        starting_input = input if not is_resumed_state else None

        if is_resumed_state:
            run_state = cast(RunState[TContext], input)
            # When resuming, use the original_input from state.
            # primeFromState will mark items as sent so prepareInput skips them
            starting_input = run_state._original_input
            current_step_type: str | int | None = None
            if run_state._current_step:
                if isinstance(run_state._current_step, NextStepInterruption):
                    current_step_type = "next_step_interruption"
                elif isinstance(run_state._current_step, NextStepHandoff):
                    current_step_type = "next_step_handoff"
                elif isinstance(run_state._current_step, NextStepFinalOutput):
                    current_step_type = "next_step_final_output"
                elif isinstance(run_state._current_step, NextStepRunAgain):
                    current_step_type = "next_step_run_again"
                else:
                    current_step_type = type(run_state._current_step).__name__
            # Log detailed information about generated_items
            generated_items_details = []
            for idx, item in enumerate(run_state._generated_items):
                item_info = {
                    "index": idx,
                    "type": item.type,
                }
                if hasattr(item, "raw_item") and isinstance(item.raw_item, dict):
                    raw_type = item.raw_item.get("type")
                    name = item.raw_item.get("name")
                    call_id = item.raw_item.get("call_id") or item.raw_item.get("callId")
                    item_info["raw_type"] = raw_type  # type: ignore[assignment]
                    item_info["name"] = name  # type: ignore[assignment]
                    item_info["call_id"] = call_id  # type: ignore[assignment]
                    if item.type == "tool_call_output_item":
                        output_str = str(item.raw_item.get("output", ""))[:100]
                        item_info["output"] = output_str  # type: ignore[assignment]  # First 100 chars
                generated_items_details.append(item_info)

            logger.debug(
                "Resuming from RunState in run_streaming()",
                extra={
                    "current_turn": run_state._current_turn,
                    "current_agent": run_state._current_agent.name
                    if run_state._current_agent
                    else None,
                    "generated_items_count": len(run_state._generated_items),
                    "generated_items_types": [item.type for item in run_state._generated_items],
                    "generated_items_details": generated_items_details,
                    "current_step_type": current_step_type,
                },
            )
            # When resuming, use the original_input from state.
            # primeFromState will mark items as sent so prepareInput skips them
            raw_input_for_result = run_state._original_input
            if isinstance(raw_input_for_result, list):
                input_for_result = AgentRunner._normalize_input_items(raw_input_for_result)
            else:
                input_for_result = raw_input_for_result
            # Use context from RunState if not provided
            if context is None and run_state._context is not None:
                context = run_state._context.context
            # Use context wrapper from RunState
            context_wrapper = cast(RunContextWrapper[TContext], run_state._context)
        else:
            # input is already str | list[TResponseInputItem] when not RunState
            # Reuse input_for_result variable from outer scope
            input_for_result = cast(Union[str, list[TResponseInputItem]], input)
            context_wrapper = RunContextWrapper(context=context)  # type: ignore
            # input_for_state is the same as input_for_result here
            input_for_state = input_for_result
            run_state = RunState(
                context=context_wrapper,
                original_input=_copy_str_or_list(input_for_state),
                starting_agent=starting_agent,
                max_turns=max_turns,
            )

        # Ensure starting_input is not None and not RunState
        streamed_input: str | list[TResponseInputItem] = (
            starting_input
            if starting_input is not None and not isinstance(starting_input, RunState)
            else ""
        )
        streamed_result = RunResultStreaming(
            input=_copy_str_or_list(streamed_input),
            # When resuming from RunState, use generated_items from state.
            # primeFromState will mark items as sent so prepareInput skips them
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
            # When resuming from RunState, use the persisted counter from the
            # saved state. This ensures we don't re-save items that were already
            # persisted before the interruption. CRITICAL: When resuming from
            # a cross-language state (e.g., from another SDK implementation),
            # the counter might be 0 or incorrect. In this case, all items in
            # generated_items were already saved, so set the counter to the length
            # of generated_items to prevent duplication. For Python-to-Python
            # resumes, the counter should already be correct, so we use it as-is.
            _current_turn_persisted_item_count=(
                (
                    len(run_state._generated_items)
                    if run_state._generated_items
                    else 0
                    if run_state._current_turn_persisted_item_count == 0
                    and run_state._generated_items
                    else run_state._current_turn_persisted_item_count
                )
                if run_state
                else 0
            ),
            # When resuming from RunState, preserve the original input from the state
            # This ensures originalInput in serialized state reflects the first turn's input
            _original_input=(
                _copy_str_or_list(run_state._original_input)
                if run_state and run_state._original_input is not None
                else _copy_str_or_list(streamed_input)
            ),
        )
        # Store run_state in streamed_result._state so it's accessible throughout streaming
        # Now that we create run_state for both fresh and resumed runs, always set it
        streamed_result._state = run_state
        if run_state is not None:
            streamed_result._tool_use_tracker_snapshot = run_state.get_tool_use_tracker_snapshot()

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
                is_resumed_state=is_resumed_state,
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

        def _sanitize_for_logging(value: Any) -> Any:
            if isinstance(value, dict):
                sanitized: dict[str, Any] = {}
                for key, val in value.items():
                    sanitized[key] = _sanitize_for_logging(val)
                return sanitized
            if isinstance(value, list):
                return [_sanitize_for_logging(v) for v in value]
            if isinstance(value, str) and len(value) > 200:
                return value[:200] + "...(truncated)"
            return value

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
        *,
        is_resumed_state: bool = False,
    ):
        if streamed_result.trace:
            streamed_result.trace.start(mark_as_current=True)

        # CRITICAL: Create server_conversation_tracker as early as possible to prevent
        # items from being saved when the server manages the conversation.
        # Match JS: serverManagesConversation is determined early and used consistently.
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

        if run_state is None:
            run_state = RunState(
                context=context_wrapper,
                original_input=_copy_str_or_list(starting_input),
                starting_agent=starting_agent,
                max_turns=max_turns,
            )
            streamed_result._state = run_state
        elif streamed_result._state is None:
            streamed_result._state = run_state

        current_span: Span[AgentSpanData] | None = None
        # When resuming from state, use the current agent from the state (which may be different
        # from starting_agent if a handoff occurred). Otherwise use starting_agent.
        if run_state is not None and run_state._current_agent is not None:
            current_agent = run_state._current_agent
        else:
            current_agent = starting_agent
        # Initialize current_turn from run_state if resuming, otherwise start at 0
        # This is set earlier at StreamedRunResult creation, but we need to ensure it's correct here
        if run_state is not None:
            current_turn = run_state._current_turn
        else:
            current_turn = 0
        should_run_agent_start_hooks = True
        tool_use_tracker = AgentToolUseTracker()
        if run_state is not None:
            cls._hydrate_tool_use_tracker(tool_use_tracker, run_state, starting_agent)

        pending_server_items: list[RunItem] | None = None

        # server_conversation_tracker was created above (moved earlier to
        # prevent duplicate saves).

        # Prime the server conversation tracker from state if resuming
        if is_resumed_state and server_conversation_tracker is not None and run_state is not None:
            session_items: list[TResponseInputItem] | None = None
            if session is not None:
                try:
                    session_items = await session.get_items()
                except Exception:
                    session_items = None
            # Call prime_from_state to mark initial input as sent.
            # This prevents the original input from being sent again when resuming
            server_conversation_tracker.prime_from_state(
                original_input=run_state._original_input,
                generated_items=run_state._generated_items,
                model_responses=run_state._model_responses,
                session_items=session_items,
            )

        streamed_result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=current_agent))

        try:
            # Prepare input with session if enabled. When resuming from a
            # RunState, use the RunState's original_input directly (which
            # already contains the full conversation history). The session is
            # used for persistence, not for input preparation when resuming.
            if is_resumed_state and run_state is not None:
                # Resuming from state - normalize items to remove top-level
                # providerData and filter incomplete function_call pairs. Don't
                # merge with session history because the RunState's
                # original_input already contains the full conversation history.
                if isinstance(starting_input, list):
                    # Normalize field names first (camelCase -> snake_case) to ensure
                    # consistent field names for filtering
                    normalized_input = AgentRunner._normalize_input_items(starting_input)
                    # Filter incomplete function_call pairs after normalizing
                    filtered = AgentRunner._filter_incomplete_function_calls(normalized_input)
                    prepared_input: str | list[TResponseInputItem] = filtered
                else:
                    prepared_input = starting_input
                # Update streamed_result.input to match prepared_input when
                # resuming. prepareInput will skip items marked as sent by
                # primeFromState.
                streamed_result.input = prepared_input
                # streamed_result._original_input is already set to
                # run_state._original_input earlier. Don't set
                # _original_input_for_persistence when resuming - input already
                # in session.
                streamed_result._original_input_for_persistence = []
                # Mark as persisted when resuming - input is already in session,
                # prevent fallback save.
                streamed_result._stream_input_persisted = True
            else:
                # Fresh run - prepare input with session history
                # Match JS: serverManagesConversation is ONLY based on
                # conversationId/previousResponseId. Sessions remain usable
                # alongside server-managed conversations (e.g.,
                # OpenAIConversationsSession) so callers can reuse callbacks,
                # resume-from-state logic, and other helpers without duplicating
                # remote history, so persistence is gated on
                # serverManagesConversation. CRITICAL:
                # server_conversation_tracker is now created earlier so we can
                # use it directly to determine if server manages conversation.
                # Match JS: serverManagesConversation is determined early and
                # used consistently.
                server_manages_conversation = server_conversation_tracker is not None
                if server_manages_conversation:
                    # When server manages conversation, don't merge with session
                    # history. The server conversation tracker's prepare_input
                    # will handle everything. Match JS: result.input remains the
                    # original input, prepareInput handles preparation.
                    (
                        prepared_input,
                        session_items_snapshot,
                    ) = await AgentRunner._prepare_input_with_session(
                        starting_input,
                        session,
                        run_config.session_input_callback,
                        include_history_in_prepared_input=False,
                        preserve_dropped_new_items=True,
                    )
                    # CRITICAL: Don't overwrite streamed_result.input when the
                    # server manages conversation. prepare_input expects the
                    # original input, not the prepared input. streamed_result.input
                    # is already set to starting_input and _original_input earlier.
                else:
                    (
                        prepared_input,
                        session_items_snapshot,
                    ) = await AgentRunner._prepare_input_with_session(
                        starting_input,
                        session,
                        run_config.session_input_callback,
                    )
                    # Update streamed result with prepared input (only when
                    # server doesn't manage conversation).
                    streamed_result.input = prepared_input
                    streamed_result._original_input = _copy_str_or_list(prepared_input)

                # Store original input for persistence (match JS:
                # sessionInputOriginalSnapshot). This is the new user input
                # before session history was merged. When serverManagesConversation
                # is True, don't set items for persistence.
                if server_manages_conversation:
                    # Server manages conversation - don't save input items
                    # locally. They're already being saved by the server.
                    streamed_result._original_input_for_persistence = []
                    streamed_result._stream_input_persisted = True
                else:
                    streamed_result._original_input_for_persistence = session_items_snapshot

                # Save only the new user input to the session, not the combined
                # history. Skip saving if server manages conversation
                # (conversationId/previousResponseId provided).
                # For fresh runs we mark as persisted to prevent the
                # fallback save from firing; set the flag before any potential
                # save. In streaming mode, we save input right before handing it to the
                # model.

            while True:
                # Check for interruption at the start of the loop
                if (
                    is_resumed_state
                    and run_state is not None
                    and run_state._current_step is not None
                ):
                    if isinstance(run_state._current_step, NextStepInterruption):
                        # We're resuming from an interruption - resolve it.
                        # In streaming mode, we process the last model response
                        # and call resolveTurnAfterModelResponse which handles the interruption
                        if not run_state._model_responses or not run_state._last_processed_response:
                            from .exceptions import UserError

                            raise UserError("No model response found in previous state")

                        # Get the last model response
                        last_model_response = run_state._model_responses[-1]

                        from ._run_impl import RunImpl

                        turn_result = await RunImpl.resolve_interrupted_turn(
                            agent=current_agent,
                            original_input=run_state._original_input,
                            original_pre_step_items=run_state._generated_items,
                            new_response=last_model_response,
                            processed_response=run_state._last_processed_response,
                            hooks=hooks,
                            context_wrapper=context_wrapper,
                            run_config=run_config,
                            run_state=run_state,
                        )

                        tool_use_tracker.add_tool_use(
                            current_agent, run_state._last_processed_response.tools_used
                        )
                        streamed_result._tool_use_tracker_snapshot = (
                            AgentRunner._serialize_tool_use_tracker(tool_use_tracker)
                        )

                        # Calculate rewind count for approval items.
                        # Approval items were persisted when the interruption was raised,
                        # so we need to rewind the counter to ensure tool outputs are saved
                        pending_approval_items = run_state._current_step.interruptions
                        rewind_count = 0
                        if pending_approval_items:
                            # Get approval identities for matching
                            def get_approval_identity(approval: ToolApprovalItem) -> str | None:
                                raw_item = approval.raw_item
                                if isinstance(raw_item, dict):
                                    if raw_item.get("type") == "function_call" and raw_item.get(
                                        "callId"
                                    ):
                                        return f"function_call:{raw_item['callId']}"
                                    call_id = (
                                        raw_item.get("callId")
                                        or raw_item.get("call_id")
                                        or raw_item.get("id")
                                    )
                                    if call_id:
                                        return f"{raw_item.get('type', 'unknown')}:{call_id}"
                                    item_id = raw_item.get("id")
                                    if item_id:
                                        return f"{raw_item.get('type', 'unknown')}:{item_id}"
                                elif isinstance(raw_item, ResponseFunctionToolCall):
                                    if raw_item.call_id:
                                        return f"function_call:{raw_item.call_id}"
                                return None

                            pending_approval_identities = set()
                            for approval in pending_approval_items:
                                # Type guard: ensure approval is ToolApprovalItem
                                if isinstance(approval, ToolApprovalItem):
                                    identity = get_approval_identity(approval)
                                    if identity:
                                        pending_approval_identities.add(identity)

                            if pending_approval_identities:
                                # Count approval items from the end of original_pre_step_items
                                # that match pending approval identities
                                for item in reversed(run_state._generated_items):
                                    if not isinstance(item, ToolApprovalItem):
                                        continue

                                    identity = get_approval_identity(item)
                                    if not identity:
                                        continue

                                    if identity not in pending_approval_identities:
                                        continue

                                    rewind_count += 1
                                    pending_approval_identities.discard(identity)

                                    if not pending_approval_identities:
                                        break

                        # Apply rewind to counter. The rewind reduces the counter
                        # to account for approval items that were saved but need
                        # to be re-saved with their tool outputs.
                        if rewind_count > 0:
                            streamed_result._current_turn_persisted_item_count = max(
                                0,
                                streamed_result._current_turn_persisted_item_count - rewind_count,
                            )

                        streamed_result.input = turn_result.original_input
                        streamed_result._original_input = _copy_str_or_list(
                            turn_result.original_input
                        )
                        # newItems includes all generated items. Set new_items to include all
                        # items (original + new); the counter will skip the
                        # original items when saving.
                        streamed_result.new_items = turn_result.generated_items
                        # Update run_state._generated_items to match
                        run_state._original_input = _copy_str_or_list(turn_result.original_input)
                        run_state._generated_items = turn_result.generated_items
                        run_state._current_step = turn_result.next_step  # type: ignore[assignment]
                        # CRITICAL: When resuming from a cross-language state
                        # (e.g., from another SDK implementation), the counter
                        # might be incorrect after rewind. Keep it in sync with
                        # run_state.
                        run_state._current_turn_persisted_item_count = (
                            streamed_result._current_turn_persisted_item_count
                        )

                        # Stream the new items
                        RunImpl.stream_step_items_to_queue(
                            turn_result.new_step_items, streamed_result._event_queue
                        )

                        if isinstance(turn_result.next_step, NextStepInterruption):
                            # Still in an interruption - save and return
                            # Always update counter (even for server-managed
                            # conversations) for resume tracking.

                            if session is not None and server_conversation_tracker is None:
                                guardrail_tripwire = (
                                    AgentRunner._input_guardrail_tripwire_triggered_for_stream
                                )
                                should_skip_session_save = await guardrail_tripwire(streamed_result)
                                if should_skip_session_save is False:
                                    await AgentRunner._save_result_to_session(
                                        session,
                                        [],
                                        streamed_result.new_items,
                                        streamed_result._state,
                                    )
                                    streamed_result._current_turn_persisted_item_count = (
                                        streamed_result._state._current_turn_persisted_item_count
                                    )
                            streamed_result.interruptions = turn_result.next_step.interruptions
                            streamed_result._last_processed_response = (
                                run_state._last_processed_response
                            )
                            streamed_result.is_complete = True
                            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                            break

                        # Handle the next step type (similar to after _run_single_turn_streamed)
                        if isinstance(turn_result.next_step, NextStepHandoff):
                            current_agent = turn_result.next_step.new_agent
                            if current_span:
                                current_span.finish(reset_current=True)
                            current_span = None
                            should_run_agent_start_hooks = True
                            streamed_result._event_queue.put_nowait(
                                AgentUpdatedStreamEvent(new_agent=current_agent)
                            )
                            run_state._current_step = NextStepRunAgain()  # type: ignore[assignment]
                            continue
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
                                output_guardrail_results = (
                                    await streamed_result._output_guardrails_task
                                )
                            except Exception:
                                output_guardrail_results = []

                            streamed_result.output_guardrail_results = output_guardrail_results
                            streamed_result.final_output = turn_result.next_step.output
                            streamed_result.is_complete = True

                            if session is not None and server_conversation_tracker is None:
                                guardrail_tripwire = (
                                    AgentRunner._input_guardrail_tripwire_triggered_for_stream
                                )
                                should_skip_session_save = await guardrail_tripwire(streamed_result)
                                if should_skip_session_save is False:
                                    await AgentRunner._save_result_to_session(
                                        session,
                                        [],
                                        streamed_result.new_items,
                                        streamed_result._state,
                                    )
                                    streamed_result._current_turn_persisted_item_count = (
                                        streamed_result._state._current_turn_persisted_item_count
                                    )

                            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                            break
                        elif isinstance(turn_result.next_step, NextStepRunAgain):
                            run_state._current_step = NextStepRunAgain()  # type: ignore[assignment]
                            continue

                        # Clear the current step since we've handled it
                        run_state._current_step = None

                # Check for soft cancel before starting new turn
                if streamed_result._cancel_mode == "after_turn":
                    streamed_result.is_complete = True
                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    break

                if streamed_result.is_complete:
                    break

                all_tools = await cls._get_all_tools(current_agent, context_wrapper)

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
                # Only increment turn and reset counter if we're starting a new turn,
                # not if we're continuing from an interruption (which would have
                # _last_model_response set). We check _last_model_response which
                # corresponds to the last model response from the serialized state.
                last_model_response_check: ModelResponse | None = None
                if run_state is not None:
                    # Get the last model response from _model_responses
                    # (corresponds to _lastTurnResponse)
                    if run_state._model_responses:
                        last_model_response_check = run_state._model_responses[-1]

                # Only increment turn and reset counter if we're starting a new turn,
                # not if we're continuing from an interruption (which would have
                # _last_model_response set).
                # if (!state._lastTurnResponse) { state._currentTurn++;
                # state._currentTurnPersistedItemCount = 0; }
                # When resuming, don't increment turn or reset counter - use values from saved state
                if run_state is None or last_model_response_check is None:
                    # Starting a new turn - increment turn and reset counter
                    current_turn += 1
                    streamed_result.current_turn = current_turn
                    streamed_result._current_turn_persisted_item_count = 0
                    if run_state:
                        run_state._current_turn_persisted_item_count = 0
                else:
                    # Resuming from an interruption - don't increment turn or reset counter
                    # TypeScript doesn't increment turn when resuming, it just continues
                    # The turn and counter are already set from saved state at
                    # StreamedRunResult creation. No need to modify them here.
                    pass

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
                    logger.debug(
                        f"[DEBUG] Starting turn {current_turn}, current_agent={current_agent.name}"
                    )
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
                        pending_server_items=pending_server_items,
                        session=session,
                    )
                    logger.debug(
                        "[DEBUG] Turn %s complete, next_step type=%s",
                        current_turn,
                        type(turn_result.next_step).__name__,
                    )
                    should_run_agent_start_hooks = False
                    streamed_result._tool_use_tracker_snapshot = cls._serialize_tool_use_tracker(
                        tool_use_tracker
                    )

                    streamed_result.raw_responses = streamed_result.raw_responses + [
                        turn_result.model_response
                    ]
                    streamed_result.input = turn_result.original_input
                    streamed_result.new_items = turn_result.generated_items
                    if server_conversation_tracker is not None:
                        pending_server_items = list(turn_result.new_step_items)
                    # Reset counter when next_step_run_again to ensure all items
                    # are saved again for the next iteration
                    if isinstance(turn_result.next_step, NextStepRunAgain):
                        streamed_result._current_turn_persisted_item_count = 0
                        if run_state:
                            run_state._current_turn_persisted_item_count = 0

                    if server_conversation_tracker is not None:
                        server_conversation_tracker.track_server_items(turn_result.model_response)

                    if isinstance(turn_result.next_step, NextStepHandoff):
                        current_agent = turn_result.next_step.new_agent
                        current_span.finish(reset_current=True)
                        current_span = None
                        should_run_agent_start_hooks = True
                        streamed_result._event_queue.put_nowait(
                            AgentUpdatedStreamEvent(new_agent=current_agent)
                        )
                        if streamed_result._state is not None:
                            streamed_result._state._current_step = NextStepRunAgain()

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

                        if session is not None and server_conversation_tracker is None:
                            should_skip_session_save = (
                                await AgentRunner._input_guardrail_tripwire_triggered_for_stream(
                                    streamed_result
                                )
                            )
                            if should_skip_session_save is False:
                                await AgentRunner._save_result_to_session(
                                    session, [], streamed_result.new_items, streamed_result._state
                                )
                                streamed_result._current_turn_persisted_item_count = (
                                    streamed_result._state._current_turn_persisted_item_count
                                )

                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        break
                    elif isinstance(turn_result.next_step, NextStepInterruption):
                        # Tool approval is needed - complete the stream with interruptions
                        if session is not None and server_conversation_tracker is None:
                            should_skip_session_save = (
                                await AgentRunner._input_guardrail_tripwire_triggered_for_stream(
                                    streamed_result
                                )
                            )
                            if should_skip_session_save is False:
                                await AgentRunner._save_result_to_session(
                                    session, [], streamed_result.new_items, streamed_result._state
                                )
                                streamed_result._current_turn_persisted_item_count = (
                                    streamed_result._state._current_turn_persisted_item_count
                                )
                        streamed_result.interruptions = turn_result.next_step.interruptions
                        streamed_result._last_processed_response = turn_result.processed_response
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        break
                    elif isinstance(turn_result.next_step, NextStepRunAgain):
                        if streamed_result._state is not None:
                            streamed_result._state._current_step = NextStepRunAgain()
                        # Check for soft cancel after turn completion
                        if streamed_result._cancel_mode == "after_turn":  # type: ignore[comparison-overlap]
                            streamed_result.is_complete = True
                            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                            break
                except Exception as e:
                    # Handle exceptions from _run_single_turn_streamed
                    if current_span and not isinstance(e, ModelBehaviorError):
                        _error_tracing.attach_error_to_span(
                            current_span,
                            SpanError(
                                message="Error in agent run",
                                data={"error": str(e)},
                            ),
                        )
                    raise
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
            if current_span and not isinstance(e, ModelBehaviorError):
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
        else:
            streamed_result.is_complete = True

        finally:
            # Finalize guardrails and tracing regardless of loop outcome.
            if streamed_result._input_guardrails_task:
                try:
                    triggered = await AgentRunner._input_guardrail_tripwire_triggered_for_stream(
                        streamed_result
                    )
                    if triggered:
                        first_trigger = next(
                            (
                                result
                                for result in streamed_result.input_guardrail_results
                                if result.output.tripwire_triggered
                            ),
                            None,
                        )
                        if first_trigger is not None:
                            raise InputGuardrailTripwireTriggered(first_trigger)
                except Exception as e:
                    logger.debug(
                        f"Error in streamed_result finalize for agent {current_agent.name} - {e}"
                    )
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
        session: Session | None = None,
        session_items_to_rewind: list[TResponseInputItem] | None = None,
        pending_server_items: list[RunItem] | None = None,
    ) -> SingleStepResult:
        emitted_tool_call_ids: set[str] = set()
        emitted_reasoning_item_ids: set[str] = set()

        if should_run_agent_start_hooks:
            await asyncio.gather(
                hooks.on_agent_start(context_wrapper, agent),
                (
                    agent.hooks.on_start(context_wrapper, agent)
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
            # Store original input before prepare_input for mark_input_as_sent
            # Match JS: markInputAsSent receives sourceItems (original items before filtering)
            original_input_for_tracking = ItemHelpers.input_to_new_input_list(streamed_result.input)
            # Also include generated items for tracking
            items_for_input = (
                pending_server_items if pending_server_items else streamed_result.new_items
            )
            for item in items_for_input:
                if item.type == "tool_approval_item":
                    continue
                input_item = item.to_input_item()
                original_input_for_tracking.append(input_item)

            input = server_conversation_tracker.prepare_input(
                streamed_result.input, items_for_input, streamed_result.raw_responses
            )
            logger.debug(
                "[DEBUG-STREAM] prepare_input returned %s items; remaining_initial_input=%s",
                len(input),
                len(server_conversation_tracker.remaining_initial_input)
                if server_conversation_tracker.remaining_initial_input
                else 0,
            )
            logger.debug(f"[DEBUG-STREAM] input item ids: {[id(i) for i in input]}")
            if server_conversation_tracker.remaining_initial_input:
                logger.debug(
                    "[DEBUG-STREAM] remaining_initial_input item ids: %s",
                    [id(i) for i in server_conversation_tracker.remaining_initial_input],
                )
        else:
            # Filter out tool_approval_item items and include all other items
            input = ItemHelpers.input_to_new_input_list(streamed_result.input)
            for item in streamed_result.new_items:
                if item.type == "tool_approval_item":
                    continue
                input_item = item.to_input_item()
                input.append(input_item)

        # Normalize input items to strip providerData/provider_data and normalize fields/types
        if isinstance(input, list):
            input = cls._normalize_input_items(input)
            # Deduplicate by id to avoid re-sending identical items across resumes
            input = cls._deduplicate_items_by_id(input)
            # Deduplicate by id to avoid sending the same item twice when resuming
            # from state that may contain duplicate generated items.
            input = cls._deduplicate_items_by_id(input)

        # THIS IS THE RESOLVED CONFLICT BLOCK
        filtered = await cls._maybe_filter_model_input(
            agent=agent,
            run_config=run_config,
            context_wrapper=context_wrapper,
            input_items=input,
            system_instructions=system_prompt,
        )
        if isinstance(filtered.input, list):
            filtered.input = cls._deduplicate_items_by_id(filtered.input)
        if server_conversation_tracker is not None:
            logger.debug(f"[DEBUG-STREAM] filtered.input has {len(filtered.input)} items")
            logger.debug(
                f"[DEBUG-STREAM] filtered.input item ids: {[id(i) for i in filtered.input]}"
            )
            # markInputAsSent receives sourceItems (original items before filtering),
            # not the filtered items, so object identity matching works correctly.
            server_conversation_tracker.mark_input_as_sent(original_input_for_tracking)
            # markInputAsSent filters remaining_initial_input based on what was delivered.
            # It will set it to None if it becomes empty.
        if not filtered.input and server_conversation_tracker is None:
            raise RuntimeError("Prepared model input is empty")

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

        # Persist input right before handing to model. This is the PRIMARY save point
        # for input items in streaming mode.
        # Only save if:
        # 1. We have items to persist (_original_input_for_persistence)
        # 2. Server doesn't manage conversation (server_conversation_tracker is None)
        # 3. Session is available
        # 4. Input hasn't been persisted yet (_stream_input_persisted is False)
        # CRITICAL: When server_conversation_tracker is not None, do not save input
        # items because the server manages the conversation and will save them automatically.
        if (
            not streamed_result._stream_input_persisted
            and session is not None
            and server_conversation_tracker is None
            and streamed_result._original_input_for_persistence
            and len(streamed_result._original_input_for_persistence) > 0
        ):
            # Set flag BEFORE saving to prevent race conditions
            streamed_result._stream_input_persisted = True
            input_items_to_save = [
                AgentRunner._ensure_api_input_item(item)
                for item in ItemHelpers.input_to_new_input_list(
                    streamed_result._original_input_for_persistence
                )
            ]
            if input_items_to_save:
                logger.warning(
                    "[SAVE-INPUT] Saving %s input items to session before model call. "
                    "Turn=%s, items=%s",
                    len(input_items_to_save),
                    streamed_result.current_turn,
                    [
                        item.get("type", "unknown")
                        if isinstance(item, dict)
                        else getattr(item, "type", "unknown")
                        for item in input_items_to_save[:3]
                    ],
                )
                await session.add_items(input_items_to_save)
                logger.warning(
                    f"[SAVE-INPUT-COMPLETE] Saved {len(input_items_to_save)} input items"
                )
            # CRITICAL: Do NOT update _current_turn_persisted_item_count when
            # saving input items. The counter only tracks items from newItems
            # (generated items), not input items.

        previous_response_id = (
            server_conversation_tracker.previous_response_id
            if server_conversation_tracker
            and server_conversation_tracker.previous_response_id is not None
            else None
        )
        conversation_id = (
            server_conversation_tracker.conversation_id if server_conversation_tracker else None
        )
        if conversation_id:
            logger.debug("Using conversation_id=%s", conversation_id)
        else:
            logger.debug("No conversation_id available for request")

        # 1. Stream the output events (with conversation lock retries)
        from openai import BadRequestError

        max_stream_retries = 3
        for attempt in range(max_stream_retries):
            try:
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
                                    RunItemStreamEvent(
                                        item=reasoning_item, name="reasoning_item_created"
                                    )
                                )
                break
            except BadRequestError as exc:
                if (
                    getattr(exc, "code", "") != "conversation_locked"
                    or attempt == max_stream_retries - 1
                ):
                    raise
                wait_time = 1.0 * (2**attempt)
                logger.debug(
                    "Conversation locked during streaming, retrying in %ss (attempt %s/%s)",
                    wait_time,
                    attempt + 1,
                    max_stream_retries,
                )
                await asyncio.sleep(wait_time)
                # Only rewind the items that were actually saved to the session,
                # not the full prepared input. Use
                # _original_input_for_persistence if available (new items only),
                # otherwise fall back to filtered.input.
                items_to_rewind = (
                    session_items_to_rewind
                    if session_items_to_rewind
                    else (
                        streamed_result._original_input_for_persistence
                        if hasattr(streamed_result, "_original_input_for_persistence")
                        and streamed_result._original_input_for_persistence
                        else filtered.input
                    )
                )
                await AgentRunner._rewind_session_items(
                    session, items_to_rewind, server_conversation_tracker
                )
                if server_conversation_tracker is not None:
                    server_conversation_tracker.rewind_input(filtered.input)
                final_response = None
                emitted_tool_call_ids.clear()
                emitted_reasoning_item_ids.clear()

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

        # Match JS: track server items immediately after getting final response,
        # before processing. This ensures that items echoed by the server are
        # tracked before the next turn's prepare_input.
        if server_conversation_tracker is not None:
            server_conversation_tracker.track_server_items(final_response)

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
        tool_runs: list[ToolRunFunction] = []

        # Find all tools from the agent
        all_tools = await AgentRunner._get_all_tools(agent, context_wrapper)
        tool_map = {tool.name: tool for tool in all_tools}

        for interruption in interruptions:
            if not isinstance(interruption, ToolApprovalItem):
                continue

            tool_call = interruption.raw_item
            # Use ToolApprovalItem's name property which handles different raw_item types
            tool_name = interruption.name
            if not tool_name:
                # Create a minimal ResponseFunctionToolCall for error output
                error_tool_call = ResponseFunctionToolCall(
                    type="function_call",
                    name="unknown",
                    call_id="unknown",
                    status="completed",
                    arguments="{}",
                )
                output = "Tool approval item missing tool name."
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(error_tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            # Extract call_id - function tools have call_id, hosted tools have id
            call_id: str | None = None
            if isinstance(tool_call, dict):
                call_id = tool_call.get("callId") or tool_call.get("call_id") or tool_call.get("id")
            elif hasattr(tool_call, "call_id"):
                call_id = tool_call.call_id
            elif hasattr(tool_call, "id"):
                call_id = tool_call.id

            if not call_id:
                # Create a minimal ResponseFunctionToolCall for error output
                error_tool_call = ResponseFunctionToolCall(
                    type="function_call",
                    name=tool_name,
                    call_id="unknown",
                    status="completed",
                    arguments="{}",
                )
                output = "Tool approval item missing call ID."
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(error_tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            # Check if this tool was approved
            approval_status = context_wrapper.is_tool_approved(tool_name, call_id)
            if approval_status is not True:
                # Not approved or rejected - add rejection message
                if approval_status is False:
                    output = "Tool execution was not approved."
                else:
                    output = "Tool approval status unclear."

                # Only function tools can create proper tool_call_output_item
                error_tool_call = (
                    tool_call
                    if isinstance(tool_call, ResponseFunctionToolCall)
                    else ResponseFunctionToolCall(
                        type="function_call",
                        name=tool_name,
                        call_id=call_id or "unknown",
                        status="completed",
                        arguments="{}",
                    )
                )
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(error_tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            # Tool was approved - find it and prepare for execution
            tool = tool_map.get(tool_name)
            if tool is None:
                # Tool not found - add error output
                # Only function tools can create proper tool_call_output_item
                error_tool_call = (
                    tool_call
                    if isinstance(tool_call, ResponseFunctionToolCall)
                    else ResponseFunctionToolCall(
                        type="function_call",
                        name=tool_name,
                        call_id=call_id or "unknown",
                        status="completed",
                        arguments="{}",
                    )
                )
                output = f"Tool '{tool_name}' not found."
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(error_tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            # Only function tools can be executed via ToolRunFunction
            if not isinstance(tool, FunctionTool):
                # Only function tools can create proper tool_call_output_item
                error_tool_call = (
                    tool_call
                    if isinstance(tool_call, ResponseFunctionToolCall)
                    else ResponseFunctionToolCall(
                        type="function_call",
                        name=tool_name,
                        call_id=call_id or "unknown",
                        status="completed",
                        arguments="{}",
                    )
                )
                output = f"Tool '{tool_name}' is not a function tool."
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(error_tool_call, output),
                    agent=agent,
                )
                generated_items.append(output_item)
                continue

            # Only function tools can be executed - ensure tool_call is ResponseFunctionToolCall
            if not isinstance(tool_call, ResponseFunctionToolCall):
                output = (
                    f"Tool '{tool_name}' approval item has invalid raw_item type for execution."
                )
                error_tool_call = ResponseFunctionToolCall(
                    type="function_call",
                    name=tool_name,
                    call_id=call_id or "unknown",
                    status="completed",
                    arguments="{}",
                )
                output_item = ToolCallOutputItem(
                    output=output,
                    raw_item=ItemHelpers.tool_call_output_item(error_tool_call, output),
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
        starting_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        should_run_agent_start_hooks: bool,
        tool_use_tracker: AgentToolUseTracker,
        server_conversation_tracker: _ServerConversationTracker | None = None,
        model_responses: list[ModelResponse] | None = None,
        session: Session | None = None,
        session_items_to_rewind: list[TResponseInputItem] | None = None,
    ) -> SingleStepResult:
        # Ensure we run the hooks before anything else
        if should_run_agent_start_hooks:
            await asyncio.gather(
                hooks.on_agent_start(context_wrapper, agent),
                (
                    agent.hooks.on_start(context_wrapper, agent)
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
            # Concatenate original_input and generated_items (excluding tool_approval_item)
            input = ItemHelpers.input_to_new_input_list(original_input)
            for generated_item in generated_items:
                if generated_item.type == "tool_approval_item":
                    continue
                input_item = generated_item.to_input_item()
                if isinstance(input, list):
                    input.append(input_item)
                else:
                    input = [input, input_item]

        # Normalize input items to strip providerData/provider_data and normalize fields/types
        if isinstance(input, list):
            input = cls._normalize_input_items(input)

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
            session=session,
            session_items_to_rewind=session_items_to_rewind,
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
        # When resuming from a RunState, items from streamed_result.new_items were already saved
        # to the session, so we should start with empty pre_step_items to avoid duplicates.
        # pre_step_items should only include items from the current run.
        pre_step_items: list[RunItem] = []
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
        session: Session | None = None,
        session_items_to_rewind: list[TResponseInputItem] | None = None,
    ) -> ModelResponse:
        # Allow user to modify model input right before the call, if configured
        filtered = await cls._maybe_filter_model_input(
            agent=agent,
            run_config=run_config,
            context_wrapper=context_wrapper,
            input_items=input,
            system_instructions=system_prompt,
        )
        if isinstance(filtered.input, list):
            filtered.input = cls._deduplicate_items_by_id(filtered.input)

        if server_conversation_tracker is not None:
            # markInputAsSent receives sourceItems (original items before filtering),
            # not the filtered items, so object identity matching works correctly.
            server_conversation_tracker.mark_input_as_sent(input)

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
        if conversation_id:
            logger.debug("Using conversation_id=%s", conversation_id)
        else:
            logger.debug("No conversation_id available for request")

        # Debug: log what we're sending to the API
        try:
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
        except Exception as exc:
            # Retry on transient conversation locks to mirror JS resilience.
            from openai import BadRequestError

            if (
                isinstance(exc, BadRequestError)
                and getattr(exc, "code", "") == "conversation_locked"
            ):
                # Retry with exponential backoff: 1s, 2s, 4s
                max_retries = 3
                last_exception = exc
                for attempt in range(max_retries):
                    wait_time = 1.0 * (2**attempt)
                    logger.debug(
                        "Conversation locked, retrying in %ss (attempt %s/%s)",
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    # Only rewind the items that were actually saved to the
                    # session, not the full prepared input.
                    items_to_rewind = (
                        session_items_to_rewind if session_items_to_rewind else filtered.input
                    )
                    await cls._rewind_session_items(
                        session, items_to_rewind, server_conversation_tracker
                    )
                    if server_conversation_tracker is not None:
                        server_conversation_tracker.rewind_input(filtered.input)
                    try:
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
                        break  # Success, exit retry loop
                    except BadRequestError as retry_exc:
                        last_exception = retry_exc
                        if (
                            getattr(retry_exc, "code", "") == "conversation_locked"
                            and attempt < max_retries - 1
                        ):
                            continue  # Try again
                        else:
                            raise  # Re-raise if not conversation_locked or out of retries
                else:
                    # All retries exhausted
                    logger.error(
                        "Conversation locked after all retries; filtered.input=%s", filtered.input
                    )
                    raise last_exception
            else:
                logger.error("Error getting response; filtered.input=%s", filtered.input)
                raise

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

    @staticmethod
    def _filter_incomplete_function_calls(
        items: list[TResponseInputItem],
    ) -> list[TResponseInputItem]:
        """Filter out function_call items that don't have corresponding function_call_output.

        The OpenAI API requires every function_call in an assistant message to have a
        corresponding function_call_output (tool message). This function ensures only
        complete pairs are included to prevent API errors.

        IMPORTANT: This only filters incomplete function_call items. All other items
        (messages, complete function_call pairs, etc.) are preserved to maintain
        conversation history integrity.

        Args:
            items: List of input items to filter

        Returns:
            Filtered list with only complete function_call pairs. All non-function_call
            items and complete function_call pairs are preserved.
        """
        # First pass: collect call_ids from function_call_output/function_call_result items
        completed_call_ids: set[str] = set()
        for item in items:
            if isinstance(item, dict):
                item_type = item.get("type")
                # Handle both API format (function_call_output) and
                # protocol format (function_call_result)
                if item_type in ("function_call_output", "function_call_result"):
                    call_id = item.get("call_id") or item.get("callId")
                    if call_id and isinstance(call_id, str):
                        completed_call_ids.add(call_id)

        # Second pass: only include function_call items that have corresponding outputs
        filtered: list[TResponseInputItem] = []
        for item in items:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "function_call":
                    call_id = item.get("call_id") or item.get("callId")
                    # Only include if there's a corresponding
                    # function_call_output/function_call_result
                    if call_id and call_id in completed_call_ids:
                        filtered.append(item)
                else:
                    # Include all non-function_call items
                    filtered.append(item)
            else:
                # Include non-dict items as-is
                filtered.append(item)

        return filtered

    @staticmethod
    def _normalize_input_items(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
        """Normalize input items by removing top-level providerData/provider_data
        and normalizing field names (callId -> call_id).

        The OpenAI API doesn't accept providerData at the top level of input items.
        providerData should only be in content where it belongs. This function removes
        top-level providerData while preserving it in content.

        Also normalizes field names from camelCase (callId) to snake_case (call_id)
        to match API expectations.

        Normalizes item types: converts 'function_call_result' to 'function_call_output'
        to match API expectations.

        Args:
            items: List of input items to normalize

        Returns:
            Normalized list of input items
        """

        def _coerce_to_dict(value: TResponseInputItem) -> dict[str, Any] | None:
            if isinstance(value, dict):
                return dict(value)
            if hasattr(value, "model_dump"):
                try:
                    return cast(dict[str, Any], value.model_dump(exclude_unset=True))
                except Exception:
                    return None
            return None

        normalized: list[TResponseInputItem] = []
        for item in items:
            coerced = _coerce_to_dict(item)
            if coerced is None:
                normalized.append(item)
                continue

            normalized_item = dict(coerced)
            normalized_item.pop("providerData", None)
            normalized_item.pop("provider_data", None)
            item_type = normalized_item.get("type")
            if item_type == "function_call_result":
                normalized_item["type"] = "function_call_output"
                item_type = "function_call_output"
            if item_type == "function_call_output":
                normalized_item.pop("name", None)
                normalized_item.pop("status", None)
                normalized_item = normalize_function_call_output_payload(normalized_item)
            normalized_item = _normalize_field_names(normalized_item)
            normalized.append(cast(TResponseInputItem, normalized_item))
        return normalized

    @staticmethod
    def _ensure_api_input_item(item: TResponseInputItem) -> TResponseInputItem:
        """Ensure item is in API format (function_call_output, snake_case fields)."""

        def _coerce_dict(value: TResponseInputItem) -> dict[str, Any] | None:
            if isinstance(value, dict):
                return dict(value)
            if hasattr(value, "model_dump"):
                try:
                    return cast(dict[str, Any], value.model_dump(exclude_unset=True))
                except Exception:
                    return None
            return None

        coerced = _coerce_dict(item)
        if coerced is None:
            return item

        normalized = dict(coerced)
        item_type = normalized.get("type")
        if item_type == "function_call_result":
            normalized["type"] = "function_call_output"
            normalized.pop("name", None)
            normalized.pop("status", None)

        if normalized.get("type") == "function_call_output":
            normalized = normalize_function_call_output_payload(normalized)
        return cast(TResponseInputItem, normalized)

    @classmethod
    async def _prepare_input_with_session(
        cls,
        input: str | list[TResponseInputItem],
        session: Session | None,
        session_input_callback: SessionInputCallback | None,
        *,
        include_history_in_prepared_input: bool = True,
        preserve_dropped_new_items: bool = False,
    ) -> tuple[str | list[TResponseInputItem], list[TResponseInputItem]]:
        """Prepare input by combining it with session history if enabled."""

        if session is None:
            # No session -> nothing to persist separately
            return input, []

        # Convert protocol format items from session to API format.
        history = await session.get_items()
        converted_history = [cls._ensure_api_input_item(item) for item in history]

        # Convert input to list format (new turn items only)
        new_input_list = [
            cls._ensure_api_input_item(item) for item in ItemHelpers.input_to_new_input_list(input)
        ]

        # If include_history_in_prepared_input is False (e.g., server manages conversation),
        # don't call the callback - just use the new input directly
        if session_input_callback is None or not include_history_in_prepared_input:
            prepared_items_raw: list[TResponseInputItem] = (
                converted_history + new_input_list
                if include_history_in_prepared_input
                else list(new_input_list)
            )
            appended_items = list(new_input_list)
        else:
            history_for_callback = copy.deepcopy(converted_history)
            new_items_for_callback = copy.deepcopy(new_input_list)
            combined = session_input_callback(history_for_callback, new_items_for_callback)
            if inspect.isawaitable(combined):
                combined = await combined
            if not isinstance(combined, list):
                raise UserError("Session input callback must return a list of input items.")

            def session_item_key(item: Any) -> str:
                try:
                    if hasattr(item, "model_dump"):
                        payload = item.model_dump(exclude_unset=True)
                    elif isinstance(item, dict):
                        payload = item
                    else:
                        payload = cls._ensure_api_input_item(item)
                    return json.dumps(payload, sort_keys=True, default=str)
                except Exception:
                    return repr(item)

            def build_reference_map(items: Sequence[Any]) -> dict[str, list[Any]]:
                refs: dict[str, list[Any]] = {}
                for item in items:
                    key = session_item_key(item)
                    refs.setdefault(key, []).append(item)
                return refs

            def consume_reference(ref_map: dict[str, list[Any]], key: str, candidate: Any) -> bool:
                candidates = ref_map.get(key)
                if not candidates:
                    return False
                for idx, existing in enumerate(candidates):
                    if existing is candidate:
                        candidates.pop(idx)
                        if not candidates:
                            ref_map.pop(key, None)
                        return True
                return False

            def build_frequency_map(items: Sequence[Any]) -> dict[str, int]:
                freq: dict[str, int] = {}
                for item in items:
                    key = session_item_key(item)
                    freq[key] = freq.get(key, 0) + 1
                return freq

            history_refs = build_reference_map(history_for_callback)
            new_refs = build_reference_map(new_items_for_callback)
            history_counts = build_frequency_map(history_for_callback)
            new_counts = build_frequency_map(new_items_for_callback)

            appended: list[Any] = []
            for item in combined:
                key = session_item_key(item)
                if consume_reference(new_refs, key, item):
                    new_counts[key] = max(new_counts.get(key, 0) - 1, 0)
                    appended.append(item)
                    continue
                if consume_reference(history_refs, key, item):
                    history_counts[key] = max(history_counts.get(key, 0) - 1, 0)
                    continue
                if history_counts.get(key, 0) > 0:
                    history_counts[key] = history_counts.get(key, 0) - 1
                    continue
                if new_counts.get(key, 0) > 0:
                    new_counts[key] = new_counts.get(key, 0) - 1
                    appended.append(item)
                    continue
                appended.append(item)

            appended_items = [cls._ensure_api_input_item(item) for item in appended]

            if include_history_in_prepared_input:
                prepared_items_raw = combined
            elif appended_items:
                prepared_items_raw = appended_items
            else:
                prepared_items_raw = new_items_for_callback if preserve_dropped_new_items else []

        # Filter incomplete function_call pairs before normalizing
        prepared_as_inputs = [cls._ensure_api_input_item(item) for item in prepared_items_raw]
        filtered = cls._filter_incomplete_function_calls(prepared_as_inputs)

        # Normalize items to remove top-level providerData and deduplicate by ID
        normalized = cls._normalize_input_items(filtered)
        deduplicated = cls._deduplicate_items_by_id(normalized)

        return deduplicated, [cls._ensure_api_input_item(item) for item in appended_items]

    @classmethod
    async def _save_result_to_session(
        cls,
        session: Session | None,
        original_input: str | list[TResponseInputItem],
        new_items: list[RunItem],
        run_state: RunState | None = None,
    ) -> None:
        """
        Save the conversation turn to session.
        It does not account for any filtering or modification performed by
        `RunConfig.session_input_callback`.

        Uses _currentTurnPersistedItemCount to prevent duplicate saves during
        streaming execution.
        """
        already_persisted = run_state._current_turn_persisted_item_count if run_state else 0

        if session is None:
            return

        # If we're resuming a turn and only passing a subset of items (e.g.,
        # post-approval outputs), the persisted counter from the earlier partial
        # save can exceed the new items being saved. In that case, reset the
        # baseline so the new items are still written.
        # Only persist items that haven't been saved yet for this turn
        if already_persisted >= len(new_items):
            new_run_items = list(new_items)
        else:
            new_run_items = new_items[already_persisted:]
        # If the counter skipped past tool outputs (e.g., resuming after approval),
        # make sure those outputs are still persisted.
        if run_state and new_items and new_run_items:
            missing_outputs = [
                item
                for item in new_items
                if item.type == "tool_call_output_item" and item not in new_run_items
            ]
            if missing_outputs:
                new_run_items = missing_outputs + new_run_items

        # In streaming mode, this function saves ONLY output items from new_items,
        # never input items (input items were saved earlier).
        # In blocking mode, this function saves both input and output items.
        # In streaming mode this function is called with original_input=[]
        # because input items were saved earlier. If new_items is not empty,
        # we're in streaming mode and must not save input here. Only save input
        # items in blocking mode when new_items is empty.
        input_list = []
        if original_input:
            input_list = [
                cls._ensure_api_input_item(item)
                for item in ItemHelpers.input_to_new_input_list(original_input)
            ]

        # Filter out tool_approval_item items before converting to input format
        items_to_convert = [item for item in new_run_items if item.type != "tool_approval_item"]

        # Convert new items to input format
        # item.to_input_item() converts RunItem to AgentInputItem format
        new_items_as_input: list[TResponseInputItem] = [
            cls._ensure_api_input_item(item.to_input_item()) for item in items_to_convert
        ]

        # In streaming mode: only output items are saved (input_list is [] because
        # original_input is [] in streaming).
        # In blocking mode: both input and output items are saved.
        items_to_save = input_list + new_items_as_input
        items_to_save = cls._deduplicate_items_by_id(items_to_save)

        # Avoid reusing provider-assigned IDs when saving to OpenAIConversationsSession.
        # FakeModel produces fixed ids; letting the service assign ids prevents
        # "Item already in conversation" errors when resuming across processes.
        if isinstance(session, OpenAIConversationsSession) and items_to_save:
            sanitized: list[TResponseInputItem] = []
            for item in items_to_save:
                if isinstance(item, dict) and "id" in item:
                    clean_item = dict(item)
                    clean_item.pop("id", None)
                    sanitized.append(cast(TResponseInputItem, clean_item))
                else:
                    sanitized.append(item)
            items_to_save = sanitized

        if len(items_to_save) == 0:
            # Update counter even if nothing to save
            if run_state:
                run_state._current_turn_persisted_item_count = already_persisted + len(
                    new_run_items
                )
            return

        await session.add_items(items_to_save)

        # Update counter after successful save
        if run_state:
            run_state._current_turn_persisted_item_count = already_persisted + len(new_run_items)

    @staticmethod
    async def _rewind_session_items(
        session: Session | None,
        items: Sequence[TResponseInputItem],
        server_tracker: _ServerConversationTracker | None = None,
    ) -> None:
        """
        Best-effort helper to remove the most recently persisted items from a session.
        Used when a conversation lock forces us to retry the same turn so we don't end
        up duplicating user inputs.
        """
        if session is None or not items:
            return

        pop_item = getattr(session, "pop_item", None)
        if not callable(pop_item):
            return

        target_serializations: list[str] = []
        for item in items:
            serialized = AgentRunner._serialize_item_for_matching(item)
            if serialized:
                target_serializations.append(serialized)

        if not target_serializations:
            return

        logger.debug(
            "Rewinding session items due to conversation retry (targets=%d)",
            len(target_serializations),
        )

        # DEBUG: Log what we're trying to match
        for i, target in enumerate(target_serializations):
            logger.error("[REWIND-DEBUG] Target %d (first 300 chars): %s", i, target[:300])

        snapshot_serializations = target_serializations.copy()

        remaining = target_serializations.copy()

        while remaining:
            try:
                result = pop_item()
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                logger.warning("Failed to rewind session item: %s", exc)
                break
            else:
                if result is None:
                    break

                popped_serialized = AgentRunner._serialize_item_for_matching(result)

                # DEBUG: Log detailed matching information
                logger.error("[REWIND-DEBUG] Popped item type: %s", type(result).__name__)
                if popped_serialized:
                    logger.error(
                        "[REWIND-DEBUG] Popped serialized (first 300 chars): %s",
                        popped_serialized[:300],
                    )
                else:
                    logger.error("[REWIND-DEBUG] Popped serialized: None")

                logger.error("[REWIND-DEBUG] Number of remaining targets: %d", len(remaining))
                if remaining and popped_serialized:
                    logger.error(
                        "[REWIND-DEBUG] First target (first 300 chars): %s", remaining[0][:300]
                    )
                    logger.error("[REWIND-DEBUG] Match found: %s", popped_serialized in remaining)
                    # Show character-by-character comparison if close match
                    if len(remaining) > 0:
                        first_target = remaining[0]
                        if abs(len(first_target) - len(popped_serialized)) < 50:
                            logger.error(
                                "[REWIND-DEBUG] Length comparison - popped: %d, target: %d",
                                len(popped_serialized),
                                len(first_target),
                            )

                if popped_serialized and popped_serialized in remaining:
                    remaining.remove(popped_serialized)

        if remaining:
            logger.warning(
                "Unable to fully rewind session; %d items still unmatched after retry",
                len(remaining),
            )
        else:
            await AgentRunner._wait_for_session_cleanup(session, snapshot_serializations)

        if session is None or server_tracker is None:
            return

        # After removing the intended inputs, peel off any additional items (e.g., partial model
        # outputs) that may have landed on the conversation during the failed attempt.
        try:
            latest_items = await session.get_items(limit=1)
        except Exception as exc:
            logger.debug("Failed to peek session items while rewinding: %s", exc)
            return

        if not latest_items:
            return

        latest_id = latest_items[0].get("id")
        if isinstance(latest_id, str) and latest_id in server_tracker.server_item_ids:
            return

        logger.debug("Stripping stray conversation items until we reach a known server item")
        while True:
            try:
                result = pop_item()
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                logger.warning("Failed to strip stray session item: %s", exc)
                break

            if result is None:
                break

            stripped_id = (
                result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
            )
            if isinstance(stripped_id, str) and stripped_id in server_tracker.server_item_ids:
                break

    @staticmethod
    def _deduplicate_items_by_id(
        items: Sequence[TResponseInputItem],
    ) -> list[TResponseInputItem]:
        """Remove duplicate items based on their IDs while preserving order."""
        seen_keys: set[str] = set()
        deduplicated: list[TResponseInputItem] = []
        for item in items:
            serialized = AgentRunner._serialize_item_for_matching(item) or repr(item)
            if serialized in seen_keys:
                continue
            seen_keys.add(serialized)
            deduplicated.append(item)
        return deduplicated

    @staticmethod
    def _serialize_item_for_matching(item: Any) -> str | None:
        """
        Normalize input items (dicts, pydantic models, etc.) into a JSON string we can use
        for lightweight equality checks when rewinding session items.
        """
        if item is None:
            return None

        try:
            if hasattr(item, "model_dump"):
                payload = item.model_dump(exclude_unset=True)
            elif isinstance(item, dict):
                payload = item
            else:
                payload = AgentRunner._ensure_api_input_item(item)

            return json.dumps(payload, sort_keys=True, default=str)
        except Exception:
            return None

    @staticmethod
    async def _wait_for_session_cleanup(
        session: Session | None, serialized_targets: Sequence[str], *, max_attempts: int = 5
    ) -> None:
        if session is None or not serialized_targets:
            return

        window = len(serialized_targets) + 2

        for attempt in range(max_attempts):
            try:
                tail_items = await session.get_items(limit=window)
            except Exception as exc:
                logger.debug("Failed to verify session cleanup (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(0.1 * (attempt + 1))
                continue

            serialized_tail: set[str] = set()
            for item in tail_items:
                serialized = AgentRunner._serialize_item_for_matching(item)
                if serialized:
                    serialized_tail.add(serialized)

            if not any(serial in serialized_tail for serial in serialized_targets):
                return

            await asyncio.sleep(0.1 * (attempt + 1))

        logger.debug(
            "Session cleanup verification exhausted attempts; targets may still linger temporarily"
        )

    @staticmethod
    async def _maybe_get_openai_conversation_id(session: Session | None) -> str | None:
        """
        Best-effort helper to ensure we have a conversation_id when using
        OpenAIConversationsSession. This allows the Responses API to reuse
        server-side history even when no new input items are being sent.
        """
        if session is None:
            return None

        get_session_id = getattr(session, "_get_session_id", None)
        if not callable(get_session_id):
            return None

        try:
            session_id = get_session_id()
            if session_id is None:
                return None
            resolved_id = await session_id if inspect.isawaitable(session_id) else session_id
            return str(resolved_id) if resolved_id is not None else None
        except Exception as exc:  # pragma: no cover
            logger.debug("Failed to resolve OpenAI conversation id from session: %s", exc)
            return None

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

    @staticmethod
    def _serialize_tool_use_tracker(
        tool_use_tracker: AgentToolUseTracker,
    ) -> dict[str, list[str]]:
        """Convert the AgentToolUseTracker into a serializable snapshot."""
        snapshot: dict[str, list[str]] = {}
        for agent, tool_names in tool_use_tracker.agent_to_tools:
            snapshot[agent.name] = list(tool_names)
        return snapshot

    @staticmethod
    def _hydrate_tool_use_tracker(
        tool_use_tracker: AgentToolUseTracker,
        run_state: RunState[Any],
        starting_agent: Agent[Any],
    ) -> None:
        """Seed a fresh AgentToolUseTracker using the snapshot stored on the RunState."""
        snapshot = run_state.get_tool_use_tracker_snapshot()
        if not snapshot:
            return
        agent_map = _build_agent_map(starting_agent)
        for agent_name, tool_names in snapshot.items():
            agent = agent_map.get(agent_name)
            if agent is None:
                continue
            tool_use_tracker.add_tool_use(agent, list(tool_names))


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
