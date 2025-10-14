from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, cast, get_args

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
    NextStepRunAgain,
    QueueCompleteSentinel,
    RunImpl,
    SingleStepResult,
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
from .handoffs import Handoff, HandoffInputFilter, handoff
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
from .run_context import RunContextWrapper, TContext
from .stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
    StreamEvent,
)
from .tool import Tool
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
    previous_response_id modes."""

    conversation_id: str | None = None
    previous_response_id: str | None = None
    sent_items: set[int] = field(default_factory=set)
    server_items: set[int] = field(default_factory=set)

    def track_server_items(self, model_response: ModelResponse) -> None:
        for output_item in model_response.output:
            self.server_items.add(id(output_item))

        # Update previous_response_id only when using previous_response_id
        if (
            self.conversation_id is None
            and self.previous_response_id is not None
            and model_response.response_id is not None
        ):
            self.previous_response_id = model_response.response_id

    def prepare_input(
        self,
        original_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
    ) -> list[TResponseInputItem]:
        input_items: list[TResponseInputItem] = []

        # On first call (when there are no generated items yet), include the original input
        if not generated_items:
            input_items.extend(ItemHelpers.input_to_new_input_list(original_input))

        # Process generated_items, skip items already sent or from server
        for item in generated_items:
            raw_item_id = id(item.raw_item)

            if raw_item_id in self.sent_items or raw_item_id in self.server_items:
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

    conversation_id: NotRequired[str | None]
    """The ID of the stored conversation, if any."""

    session: NotRequired[Session | None]
    """The session for the run."""


class Runner:
    @classmethod
    async def run(
        cls,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem],
        *,
        context: TContext | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: RunHooks[TContext] | None = None,
        run_config: RunConfig | None = None,
        previous_response_id: str | None = None,
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
            conversation_id=conversation_id,
            session=session,
        )

    @classmethod
    def run_sync(
        cls,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem],
        *,
        context: TContext | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: RunHooks[TContext] | None = None,
        run_config: RunConfig | None = None,
        previous_response_id: str | None = None,
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
        )

    @classmethod
    def run_streamed(
        cls,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem],
        context: TContext | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: RunHooks[TContext] | None = None,
        run_config: RunConfig | None = None,
        previous_response_id: str | None = None,
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
        input: str | list[TResponseInputItem],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResult:
        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = cast(RunHooks[TContext], self._validate_run_hooks(kwargs.get("hooks")))
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
        conversation_id = kwargs.get("conversation_id")
        session = kwargs.get("session")
        if run_config is None:
            run_config = RunConfig()

        if conversation_id is not None or previous_response_id is not None:
            server_conversation_tracker = _ServerConversationTracker(
                conversation_id=conversation_id, previous_response_id=previous_response_id
            )
        else:
            server_conversation_tracker = None

        # Keep original user input separate from session-prepared input
        original_user_input = input
        prepared_input = await self._prepare_input_with_session(
            input, session, run_config.session_input_callback
        )

        tool_use_tracker = AgentToolUseTracker()

        with TraceCtxManager(
            workflow_name=run_config.workflow_name,
            trace_id=run_config.trace_id,
            group_id=run_config.group_id,
            metadata=run_config.trace_metadata,
            disabled=run_config.tracing_disabled,
        ):
            current_turn = 0
            original_input: str | list[TResponseInputItem] = _copy_str_or_list(prepared_input)
            generated_items: list[RunItem] = []
            model_responses: list[ModelResponse] = []

            context_wrapper: RunContextWrapper[TContext] = RunContextWrapper(
                context=context,  # type: ignore
            )

            input_guardrail_results: list[InputGuardrailResult] = []
            tool_input_guardrail_results: list[ToolInputGuardrailResult] = []
            tool_output_guardrail_results: list[ToolOutputGuardrailResult] = []

            current_span: Span[AgentSpanData] | None = None
            current_agent = starting_agent
            should_run_agent_start_hooks = True

            # save only the new user input to the session, not the combined history
            await self._save_result_to_session(session, original_user_input, [])

            try:
                while True:
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

                    logger.debug(
                        f"Running agent {current_agent.name} (turn {current_turn})",
                    )

                    if current_turn == 1:
                        input_guardrail_results, turn_result = await asyncio.gather(
                            self._run_input_guardrails(
                                starting_agent,
                                starting_agent.input_guardrails
                                + (run_config.input_guardrails or []),
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
                            ),
                        )
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

                    if isinstance(turn_result.next_step, NextStepFinalOutput):
                        output_guardrail_results = await self._run_output_guardrails(
                            current_agent.output_guardrails + (run_config.output_guardrails or []),
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
                        )
                        if not any(
                            guardrail_result.output.tripwire_triggered
                            for guardrail_result in input_guardrail_results
                        ):
                            await self._save_result_to_session(
                                session, [], turn_result.new_step_items
                            )

                        return result
                    elif isinstance(turn_result.next_step, NextStepHandoff):
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
        input: str | list[TResponseInputItem],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResult:
        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = kwargs.get("hooks")
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
        conversation_id = kwargs.get("conversation_id")
        session = kwargs.get("session")

        return asyncio.get_event_loop().run_until_complete(
            self.run(
                starting_agent,
                input,
                session=session,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
            )
        )

    def run_streamed(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResultStreaming:
        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = cast(RunHooks[TContext], self._validate_run_hooks(kwargs.get("hooks")))
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
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
        context_wrapper: RunContextWrapper[TContext] = RunContextWrapper(
            context=context  # type: ignore
        )

        streamed_result = RunResultStreaming(
            input=_copy_str_or_list(input),
            new_items=[],
            current_agent=starting_agent,
            raw_responses=[],
            final_output=None,
            is_complete=False,
            current_turn=0,
            max_turns=max_turns,
            input_guardrail_results=[],
            output_guardrail_results=[],
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
            _current_agent_output_schema=output_schema,
            trace=new_trace,
            context_wrapper=context_wrapper,
        )

        # Kick off the actual agent loop in the background and return the streamed result object.
        streamed_result._run_impl_task = asyncio.create_task(
            self._start_streaming(
                starting_input=input,
                streamed_result=streamed_result,
                starting_agent=starting_agent,
                max_turns=max_turns,
                hooks=hooks,
                context_wrapper=context_wrapper,
                run_config=run_config,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                session=session,
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
        except Exception:
            for t in guardrail_tasks:
                t.cancel()
            raise

        streamed_result.input_guardrail_results = guardrail_results

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
        conversation_id: str | None,
        session: Session | None,
    ):
        if streamed_result.trace:
            streamed_result.trace.start(mark_as_current=True)

        current_span: Span[AgentSpanData] | None = None
        current_agent = starting_agent
        current_turn = 0
        should_run_agent_start_hooks = True
        tool_use_tracker = AgentToolUseTracker()

        if conversation_id is not None or previous_response_id is not None:
            server_conversation_tracker = _ServerConversationTracker(
                conversation_id=conversation_id, previous_response_id=previous_response_id
            )
        else:
            server_conversation_tracker = None

        streamed_result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=current_agent))

        try:
            # Prepare input with session if enabled
            prepared_input = await AgentRunner._prepare_input_with_session(
                starting_input, session, run_config.session_input_callback
            )

            # Update the streamed result with the prepared input
            streamed_result.input = prepared_input

            await AgentRunner._save_result_to_session(session, starting_input, [])

            while True:
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
                    # Run the input guardrails in the background and put the results on the queue
                    streamed_result._input_guardrails_task = asyncio.create_task(
                        cls._run_input_guardrails_with_queue(
                            starting_agent,
                            starting_agent.input_guardrails + (run_config.input_guardrails or []),
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
                        # Note: Non-streaming path doesn't save handoff turns immediately,
                        # but streaming needs to for graceful cancellation support
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
            if current_span:
                current_span.finish(reset_current=True)
            if streamed_result.trace:
                streamed_result.trace.finish(reset_current=True)

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
            input = server_conversation_tracker.prepare_input(
                streamed_result.input, streamed_result.new_items
            )
        else:
            input = ItemHelpers.input_to_new_input_list(streamed_result.input)
            input.extend([item.to_input_item() for item in streamed_result.new_items])

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
                    call_id: str | None = getattr(
                        output_item, "call_id", getattr(output_item, "id", None)
                    )

                    if call_id and call_id not in emitted_tool_call_ids:
                        emitted_tool_call_ids.add(call_id)

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
            input = server_conversation_tracker.prepare_input(original_input, generated_items)
        else:
            input = ItemHelpers.input_to_new_input_list(original_input)
            input.extend([generated_item.to_input_item() for generated_item in generated_items])

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

        # Convert input to list format
        new_input_list = ItemHelpers.input_to_new_input_list(input)

        if session_input_callback is None:
            return history + new_input_list
        elif callable(session_input_callback):
            res = session_input_callback(history, new_input_list)
            if inspect.isawaitable(res):
                return await res
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
_TOOL_CALL_TYPES: tuple[type, ...] = get_args(ToolCallItemTypes)


def _copy_str_or_list(input: str | list[TResponseInputItem]) -> str | list[TResponseInputItem]:
    if isinstance(input, str):
        return input
    return input.copy()
