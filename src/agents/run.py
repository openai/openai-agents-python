from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any, cast

from openai.types.responses import ResponseCompletedEvent

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
)
from .guardrail import InputGuardrail, InputGuardrailResult, OutputGuardrail, OutputGuardrailResult
from .handoffs import Handoff, HandoffInputFilter, handoff
from .items import ItemHelpers, ModelResponse, RunItem, TResponseInputItem
from .lifecycle import RunHooks
from .logger import logger
from .model_settings import ModelSettings
from .models.interface import Model, ModelProvider
from .models.multi_provider import MultiProvider
from .result import RunResult, RunResultStreaming
from .run_context import RunContextWrapper, TContext
from .stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent
from .tool import Tool
from .tracing import Span, SpanError, agent_span, get_current_trace, trace
from .tracing.span_data import AgentSpanData
from .usage import Usage
from .util import _coro, _error_tracing

DEFAULT_MAX_TURNS = 10


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

    trace_include_sensitive_data: bool = True
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
    ) -> RunResult:
        """Run a workflow starting at the given agent. The agent will run in a loop until a final
        output is generated. The loop runs like so:
        1. The agent is invoked with the given input.
        2. If there is a final output (i.e. the agent produces something of type
            `agent.output_type`, the loop terminates.
        3. If there's a handoff, we run the loop again, with the new agent.
        4. Else, we run tool calls (if any), and re-run the loop.

        In two cases, the agent may raise an exception:
        1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised.
        2. If a guardrail tripwire is triggered, a GuardrailTripwireTriggered exception is raised.

        Note that only the first agent's input guardrails are run.

        Args:
            starting_agent: The starting agent to run.
            input: The initial input to the agent. You can pass a single string for a user message,
                or a list of input items.
            context: The context to run the agent with.
            max_turns: The maximum number of turns to run the agent for. A turn is defined as one
                AI invocation (including any tool calls that might occur).
            hooks: An object that receives callbacks on various lifecycle events.
            run_config: Global settings for the entire agent run.
            previous_response_id: The ID of the previous response, if using OpenAI models via the
                Responses API, this allows you to skip passing in input from the previous turn.

        Returns:
            A run result containing all the inputs, guardrail results and the output of the last
            agent. Agents may perform handoffs, so we don't know the specific type of the output.
        """
        if hooks is None:
            hooks = RunHooks[Any]()
        if run_config is None:
            run_config = RunConfig()

        tool_use_tracker = AgentToolUseTracker()

        with TraceCtxManager(
            workflow_name=run_config.workflow_name,
            trace_id=run_config.trace_id,
            group_id=run_config.group_id,
            metadata=run_config.trace_metadata,
            disabled=run_config.tracing_disabled,
        ):
            current_turn = 0
            original_input: str | list[TResponseInputItem] = copy.deepcopy(input)
            generated_items: list[RunItem] = []
            model_responses: list[ModelResponse] = []

            context_wrapper: RunContextWrapper[TContext] = RunContextWrapper(
                context=context,  # type: ignore
            )

            input_guardrail_results: list[InputGuardrailResult] = []

            current_span: Span[AgentSpanData] | None = None
            current_agent = starting_agent
            should_run_agent_start_hooks = True

            try:
                while True:
                    # Start an agent span if we do not have one. This span ends when the current
                    # agent changes or when the agent loop ends.
                    if current_span is None:
                        handoff_names = [h.agent_name for h in cls._get_handoffs(current_agent)]
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

                        all_tools = await cls._get_all_tools(current_agent)
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
                            cls._run_input_guardrails(
                                starting_agent,
                                starting_agent.input_guardrails
                                + (run_config.input_guardrails or []),
                                copy.deepcopy(input),
                                context_wrapper,
                            ),
                            cls._run_single_turn(
                                agent=current_agent,
                                all_tools=all_tools,
                                original_input=original_input,
                                generated_items=generated_items,
                                hooks=hooks,
                                context_wrapper=context_wrapper,
                                run_config=run_config,
                                should_run_agent_start_hooks=should_run_agent_start_hooks,
                                tool_use_tracker=tool_use_tracker,
                                previous_response_id=previous_response_id,
                                current_turn=current_turn,
                            ),
                        )
                    else:
                        turn_result = await cls._run_single_turn(
                            agent=current_agent,
                            all_tools=all_tools,
                            original_input=original_input,
                            generated_items=generated_items,
                            hooks=hooks,
                            context_wrapper=context_wrapper,
                            run_config=run_config,
                            should_run_agent_start_hooks=should_run_agent_start_hooks,
                            tool_use_tracker=tool_use_tracker,
                            previous_response_id=previous_response_id,
                            current_turn=current_turn,
                        )
                    should_run_agent_start_hooks = False

                    model_responses.append(turn_result.model_response)
                    original_input = turn_result.original_input
                    generated_items = turn_result.generated_items

                    if isinstance(turn_result.next_step, NextStepFinalOutput):
                        output_guardrail_results = await cls._run_output_guardrails(
                            current_agent.output_guardrails + (run_config.output_guardrails or []),
                            current_agent,
                            turn_result.next_step.output,
                            context_wrapper,
                        )
                        return RunResult(
                            input=original_input,
                            new_items=generated_items,
                            raw_responses=model_responses,
                            final_output=turn_result.next_step.output,
                            _last_agent=current_agent,
                            input_guardrail_results=input_guardrail_results,
                            output_guardrail_results=output_guardrail_results,
                            context_wrapper=context_wrapper,
                        )
                    elif isinstance(turn_result.next_step, NextStepHandoff):
                        current_agent = cast(Agent[TContext], turn_result.next_step.new_agent)
                        current_span.finish(reset_current=True)
                        current_span = None
                        should_run_agent_start_hooks = True
                    elif isinstance(turn_result.next_step, NextStepRunAgain):
                        pass
                    else:
                        raise AgentsException(
                            f"Unknown next step type: {type(turn_result.next_step)}"
                        )
            finally:
                if current_span:
                    current_span.finish(reset_current=True)

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
    ) -> RunResult:
        """Run a workflow synchronously, starting at the given agent. Note that this just wraps the
        `run` method, so it will not work if there's already an event loop (e.g. inside an async
        function, or in a Jupyter notebook or async context like FastAPI). For those cases, use
        the `run` method instead.

        The agent will run in a loop until a final output is generated. The loop runs like so:
        1. The agent is invoked with the given input.
        2. If there is a final output (i.e. the agent produces something of type
            `agent.output_type`, the loop terminates.
        3. If there's a handoff, we run the loop again, with the new agent.
        4. Else, we run tool calls (if any), and re-run the loop.

        In two cases, the agent may raise an exception:
        1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised.
        2. If a guardrail tripwire is triggered, a GuardrailTripwireTriggered exception is raised.

        Note that only the first agent's input guardrails are run.

        Args:
            starting_agent: The starting agent to run.
            input: The initial input to the agent. You can pass a single string for a user message,
                or a list of input items.
            context: The context to run the agent with.
            max_turns: The maximum number of turns to run the agent for. A turn is defined as one
                AI invocation (including any tool calls that might occur).
            hooks: An object that receives callbacks on various lifecycle events.
            run_config: Global settings for the entire agent run.
            previous_response_id: The ID of the previous response, if using OpenAI models via the
                Responses API, this allows you to skip passing in input from the previous turn.

        Returns:
            A run result containing all the inputs, guardrail results and the output of the last
            agent. Agents may perform handoffs, so we don't know the specific type of the output.
        """
        return asyncio.get_event_loop().run_until_complete(
            cls.run(
                starting_agent,
                input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
            )
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
    ) -> RunResultStreaming:
        """Run a workflow starting at the given agent in streaming mode. The returned result object
        contains a method you can use to stream semantic events as they are generated.

        The agent will run in a loop until a final output is generated. The loop runs like so:
        1. The agent is invoked with the given input.
        2. If there is a final output (i.e. the agent produces something of type
            `agent.output_type`, the loop terminates.
        3. If there's a handoff, we run the loop again, with the new agent.
        4. Else, we run tool calls (if any), and re-run the loop.

        In two cases, the agent may raise an exception:
        1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised.
        2. If a guardrail tripwire is triggered, a GuardrailTripwireTriggered exception is raised.

        Note that only the first agent's input guardrails are run.

        Args:
            starting_agent: The starting agent to run.
            input: The initial input to the agent. You can pass a single string for a user message,
                or a list of input items.
            context: The context to run the agent with.
            max_turns: The maximum number of turns to run the agent for. A turn is defined as one
                AI invocation (including any tool calls that might occur).
            hooks: An object that receives callbacks on various lifecycle events.
            run_config: Global settings for the entire agent run.
            previous_response_id: The ID of the previous response, if using OpenAI models via the
                Responses API, this allows you to skip passing in input from the previous turn.
        Returns:
            A result object that contains data about the run, as well as a method to stream events.
        """
        if hooks is None:
            hooks = RunHooks[Any]()
        if run_config is None:
            run_config = RunConfig()

        # If there is already a trace, we do not create a new one. In addition, we cannot end the
        # trace here because the actual work is done in `stream_events` and this method ends before that.
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

        output_schema = cls._get_output_schema(starting_agent)
        context_wrapper: RunContextWrapper[TContext] = RunContextWrapper(
            context=context  # type: ignore
        )

        streamed_result = RunResultStreaming(
            input=copy.deepcopy(input),
            new_items=[],
            current_agent=starting_agent,
            raw_responses=[],
            final_output=None,
            is_complete=False,
            current_turn=0,
            max_turns=max_turns,
            input_guardrail_results=[],
            output_guardrail_results=[],
            _current_agent_output_schema=output_schema,
            trace=new_trace,
            context_wrapper=context_wrapper,
        )

        # Kick off the actual agent loop in the background and return the streamed result object.
        streamed_result._run_impl_task = asyncio.create_task(
            cls._run_streamed_impl(
                starting_input=input,
                streamed_result=streamed_result,
                starting_agent=starting_agent,
                max_turns=max_turns,
                hooks=hooks,
                context_wrapper=context_wrapper,
                run_config=run_config,
                previous_response_id=previous_response_id,
            )
        )
        return streamed_result

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

        # We run the guardrails and push them onto the queue as they complete.
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
    async def _run_streamed_impl(
        cls,
        starting_input: str | list[TResponseInputItem],
        streamed_result: RunResultStreaming,
        starting_agent: Agent[TContext],
        max_turns: int,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        previous_response_id: str | None,
    ):
        if streamed_result.trace:
            streamed_result.trace.start(mark_as_current=True)

        current_span: Span[AgentSpanData] | None = None
        current_agent = starting_agent
        current_turn = 0
        should_run_agent_start_hooks = True
        tool_use_tracker = AgentToolUseTracker()

        streamed_result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=current_agent))

        try:
            while True:
                if streamed_result.is_complete:
                    break

                # Start an agent span if we do not have one. This span ends when the current
                # agent changes or when the agent loop ends.
                if current_span is None:
                    handoff_names = [h.agent_name for h in cls._get_handoffs(current_agent)]
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

                    all_tools = await cls._get_all_tools(current_agent)
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
                    # Run the input guardrails in the background and put the results on the queue.
                    streamed_result._input_guardrails_task = asyncio.create_task(
                        cls._run_input_guardrails_with_queue(
                            starting_agent,
                            starting_agent.input_guardrails + (run_config.input_guardrails or []),
                            copy.deepcopy(ItemHelpers.input_to_new_input_list(starting_input)),
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
                        previous_response_id,
                        current_turn=current_turn, # Pass current_turn
                    )
                    should_run_agent_start_hooks = False

                    streamed_result.raw_responses = streamed_result.raw_responses + [
                        turn_result.model_response
                    ]
                    streamed_result.input = turn_result.original_input
                    streamed_result.new_items = turn_result.generated_items

                    if isinstance(turn_result.next_step, NextStepHandoff):
                        current_agent = turn_result.next_step.new_agent
                        current_span.finish(reset_current=True)
                        current_span = None
                        should_run_agent_start_hooks = True
                        streamed_result._event_queue.put_nowait(
                            AgentUpdatedStreamEvent(new_agent=current_agent)
                        )
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
                            # Exceptions will be checked in the stream_events loop.
                            output_guardrail_results = []

                        streamed_result.output_guardrail_results = output_guardrail_results
                        streamed_result.final_output = turn_result.next_step.output
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    elif isinstance(turn_result.next_step, NextStepRunAgain):
                        pass
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
        previous_response_id: str | None,
        current_turn: int, # Added current_turn
    ) -> SingleStepResult:
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

        system_prompt = await agent.get_system_prompt(context_wrapper)

        handoffs = cls._get_handoffs(agent)
        model = cls._get_model(agent, run_config)
        model_settings = agent.model_settings.resolve(run_config.model_settings)
        model_settings = RunImpl.maybe_reset_tool_choice(agent, tool_use_tracker, model_settings)

        final_response: ModelResponse | None = None

        # --- Memory loading and input preparation for streaming START ---
        memory = agent.memory
        if current_turn == 1: # Consistent with non-streaming _run_single_turn
            memory.load()

        history_messages_from_memory = memory.get_messages()

        generated_item_messages: list[TResponseInputItem] = [
            item.to_input_item() for item in streamed_result.new_items
        ]
        # For streaming, `streamed_result.input` holds the initial input to the *entire run*.
        # New inputs for a specific turn are not explicitly separated in the same way as `original_input`
        # in the non-streaming version. The conversation history is built up in `streamed_result.new_items`
        # and the initial input.
        # We need to ensure the `combined_input` logic is consistent.
        # `streamed_result.input` is the initial input that started the *whole run*.
        # `streamed_result.new_items` are items from previous turns of this run.

        # If current_turn is 1, the input to the model should be the `streamed_result.input` (original input for the run)
        # plus any history from memory.
        # If current_turn > 1, the `streamed_result.new_items` contain the conversation from previous turns of this run.

        combined_input_for_model: list[TResponseInputItem] = []
        combined_input_for_model.extend(history_messages_from_memory) # type: ignore[arg-type]

        if current_turn == 1:
            # The first turn uses the initial input that started the run.
            # (ItemHelpers.input_to_new_input_list can handle if it's already a list or a string)
            initial_run_input_list = ItemHelpers.input_to_new_input_list(streamed_result.input)
            combined_input_for_model.extend(initial_run_input_list)
            # `streamed_result.new_items` should be empty on turn 1 before model call,
            # but if it weren't, it would be messages from previous turns.
            # The current logic in `_run_streamed_impl` updates `streamed_result.new_items` *after* the turn.
        else:
            # Subsequent turns use messages from `streamed_result.new_items` which accumulate turn by turn.
            # These items already include user messages and assistant responses from previous turns of this run.
            combined_input_for_model.extend(generated_item_messages)


        # The `current_turn_new_input_list_for_memory_saving` is what *this specific turn* adds as new "user" input.
        # In streaming, this is tricky because there isn't a direct "input for this turn" passed around
        # after the very first input. The "user" messages are often part of `new_items` if they were added.
        # For consistency with how `_get_single_step_result_from_response` saves to memory,
        # we need to identify the "new user input" for *this* turn.
        # However, `_run_single_turn_streamed` doesn't receive `original_input` like `_run_single_turn`.
        # The `streamed_result.input` is the *initial* input to the whole chain.
        # `streamed_result.new_items` are all previous items.
        # This means the "new input for this turn" concept is different.
        # The model is called with the cumulative history.

        # Let's assume for now that memory saving in `_get_single_step_result_from_response`
        # will correctly use its `current_turn_new_input_list` parameter.
        # The challenge is what to pass as `current_turn_new_input_list` from here.
        # For streaming, the "new input" is implicitly part of the stream if it's a user message.
        # The current design of `_get_single_step_result_from_response` expects `current_turn_new_input_list`
        # to be specifically the *new user messages* for the current turn.
        # In streaming, the "new user message" that triggers a turn (after the first) isn't explicitly passed.
        # It's assumed to be the last message in `streamed_result.new_items` if it's from the user.

        # This part of the plan might need re-evaluation for streaming, as the input flow is different.
        # For now, let's focus on getting the `combined_input_for_model` correct.
        # --- Memory loading and input preparation for streaming END ---


        # 1. Stream the output events.
        async for event in model.stream_response(
            system_prompt,
            combined_input_for_model, # Use the carefully constructed combined input
            model_settings,
            all_tools,
            output_schema,
            handoffs,
            get_model_tracing_impl(
                run_config.tracing_disabled, run_config.trace_include_sensitive_data
            ),
            previous_response_id=previous_response_id,
        ):
            if isinstance(event, ResponseCompletedEvent):
                usage = (
                    Usage(
                        requests=1,
                        input_tokens=event.response.usage.input_tokens,
                        output_tokens=event.response.usage.output_tokens,
                        total_tokens=event.response.usage.total_tokens,
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

            streamed_result._event_queue.put_nowait(RawResponsesStreamEvent(data=event))

        # 2. At this point, the streaming is complete for this turn of the agent loop.
        if not final_response:
            raise ModelBehaviorError("Model did not produce a final response!")

        # 3. Now we can process the turn as we do in the non-streaming case.
        # For saving to memory, `_get_single_step_result_from_response` needs `current_turn_new_input_list`.
        # In the streaming context, after the first turn, "new user input" is not explicitly provided to `_run_single_turn_streamed`.
        # The model reacts to the accumulated history.
        # If the last message in `combined_input_for_model` (before this turn's LLM call) was from the 'user',
        # that could be considered the "new input" for memory saving purposes.
        # This is a divergence from the non-streaming flow that needs careful handling.

        # Let's assume, for now, that `streamed_result.input` (which is the initial input to the whole run)
        # is the closest analogue to `original_input` for the *first* turn memory saving.
        # For subsequent turns, there isn't a direct equivalent of "new input for this specific turn"
        # being passed to `_run_single_turn_streamed`.

        # The `original_input` parameter for `_get_single_step_result_from_response`
        # is used by memory saving logic as `current_turn_new_input_list`.
        # We need to construct this appropriately.
        # If current_turn == 1, it's ItemHelpers.input_to_new_input_list(streamed_result.input)
        # If current_turn > 1, what should it be? The model has already seen previous items.
        # The memory saving should only save the *actual new messages* of this turn.
        # User messages in streaming (after first) are typically added via user interaction with the stream.
        # This function doesn't see that "new" user message directly.

        # Given the subtask is to integrate memory saving by calling _get_single_step_result_from_response,
        # we must provide what it expects.
        # The simplest approach for now, to make it runnable, is to pass an empty list
        # for `current_turn_new_input_list` if it's not the first turn, acknowledging this is a gap
        # in how new user inputs are captured for memory in streaming turns after the first.
        # Or, we can assume `streamed_result.new_items` contains the new user message if it was added before this call.

        current_turn_new_input_list_for_memory: list[TResponseInputItem]
        if current_turn == 1:
            current_turn_new_input_list_for_memory = ItemHelpers.input_to_new_input_list(streamed_result.input)
        else:
            # In streaming, subsequent "user" inputs are part of the history that `model.stream_response` gets.
            # They would be in `streamed_result.new_items` if added by the streaming handler.
            # The `_get_single_step_result_from_response` expects only the *newest* user messages for *this* turn.
            # This is a conceptual mismatch for streaming after turn 1.
            # For now, let's assume no *new* user messages are being introduced to this function for turns > 1,
            # as they are already part of the history.
            # This means memory might not capture "user" messages correctly after turn 1 in streaming.
            # This needs to be addressed in how user messages are added to `streamed_result.new_items`
            # and then identified for memory saving.
            # A pragmatic choice: if the *last* item in `generated_item_messages` (inputs to LLM for this turn, from previous turns)
            # was from a user, maybe that's the "new" input. This is heuristic.
            # For now, to fulfill the signature, and noting this limitation:
            current_turn_new_input_list_for_memory = [] # Placeholder for turns > 1


        single_step_result = await cls._get_single_step_result_from_response(
            agent=agent,
            original_input=streamed_result.input, # This is the input to the entire run.
            current_turn_new_input_list=current_turn_new_input_list_for_memory, #This is what's saved as "user input for the turn"
            pre_step_items=streamed_result.new_items, # Items from previous turns of this run
            new_response=final_response,
            output_schema=output_schema,
            all_tools=all_tools,
            handoffs=handoffs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_use_tracker=tool_use_tracker,
        )

        RunImpl.stream_step_result_to_queue(single_step_result, streamed_result._event_queue)
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
        previous_response_id: str | None,
        current_turn: int,
    ) -> SingleStepResult:
        # Ensure we run the hooks before anything else.
        if should_run_agent_start_hooks:
            await asyncio.gather(
                hooks.on_agent_start(context_wrapper, agent),
                (
                    agent.hooks.on_start(context_wrapper, agent)
                    if agent.hooks
                    else _coro.noop_coroutine()
                ),
            )

        system_prompt = await agent.get_system_prompt(context_wrapper)

        output_schema = cls._get_output_schema(agent)
        handoffs = cls._get_handoffs(agent)

        # --- Memory loading and input preparation START ---
        memory = agent.memory
        if current_turn == 1:
            # This ensures that for agents with persistent memory (like FileStorageMemory),
            # the history is loaded at the beginning of their first turn in a multi-agent
            # conversation or a resumed conversation. Agent.__post_init__ also calls load(),
            # so this call here is specifically for reloading if the agent instance is reused
            # across multiple Runner.run calls or if the underlying store was modified externally.
            memory.load()

        # 1. Messages from memory (history from previous runs/sessions)
        history_messages_from_memory = memory.get_messages()

        # 2. `generated_items` are from previous turns within the *current* `Runner.run` call.
        generated_item_messages: list[TResponseInputItem] = [
            generated_item.to_input_item() for generated_item in generated_items
        ]

        # 3. `original_input` is the new message(s) for the *current* turn of the *current* `Runner.run` call.
        # This can be a single string or a list of TResponseInputItem.
        current_turn_new_input_list: list[TResponseInputItem] = ItemHelpers.input_to_new_input_list(original_input)

        # Combine all message sources in chronological order:
        # memory_messages + generated_item_messages + original_input_messages
        combined_input: list[TResponseInputItem] = []
        combined_input.extend(history_messages_from_memory)  # type: ignore[arg-type]
        combined_input.extend(generated_item_messages)
        combined_input.extend(current_turn_new_input_list)
        # --- Memory loading and input preparation END ---

        new_response = await cls._get_new_response(
            agent,
            system_prompt,
            combined_input,  # Use the combined list
            output_schema,
            all_tools,
            handoffs,
            context_wrapper,
            run_config,
            tool_use_tracker,
            previous_response_id,
            # current_turn is passed through to the memory system
            current_turn=current_turn,
        )

        return await cls._get_single_step_result_from_response(
            agent=agent,
            original_input=original_input, # This is the original input to _run_single_turn
            current_turn_new_input_list=current_turn_new_input_list, # Pass the processed new inputs
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
        current_turn_new_input_list: list[TResponseInputItem], # Added parameter
    ) -> SingleStepResult:
        processed_response = RunImpl.process_model_response(
            agent=agent,
            all_tools=all_tools,
            response=new_response,
            output_schema=output_schema,
            handoffs=handoffs,
        )

        tool_use_tracker.add_tool_use(agent, processed_response.tools_used)

        # --- Memory saving START ---
        memory = agent.memory

        # 1. Add the new user messages for this turn to memory
        # current_turn_new_input_list contains only the fresh inputs for this turn
        for user_message_item in current_turn_new_input_list:
            # Ensure content is a string, though TResponseInputItem content should already be.
            memory.add(role=user_message_item["role"], content=str(user_message_item["content"]))

        # 2. Add the assistant's new messages from this turn to memory
        #    These are the direct textual/object outputs from the LLM, before tool execution.
        from .items import MessageOutputItem # Local import to avoid circular dependency issues at module level

        for item in processed_response.new_items:
            if isinstance(item, MessageOutputItem):
                # item.raw_item is ResponseOutputMessage
                # item.raw_item.content is a list of content parts (e.g., TextPart, ImageURLPart)
                # We'll concatenate text from all TextPart instances.
                text_content = ""
                if item.raw_item.content: # Content can sometimes be None for certain roles/tool_calls
                    for part in item.raw_item.content:
                        if hasattr(part, "text") and isinstance(part.text, str): # Check if it's a TextPart or similar
                            text_content += part.text
                        # Note: Other content part types (like images) are currently ignored for memory.
                        # If structured JSON is part of a TextPart, it will be saved as a string.
                
                # Only add if there's actual text content or if it's a tool_call request
                # (which might have None content but important role/tool_call_id).
                # For memory, typically we want to store textual conversation.
                # Tool call requests themselves (role='assistant', tool_calls=[...]) are important context.
                # If item.raw_item.tool_calls is not None, it means it's an assistant message requesting tool calls.
                # The content of such messages might be empty or just instructive text.
                if text_content or item.raw_item.tool_calls:
                    # If there are tool_calls, the content might be None or some wrapper text.
                    # We prioritize text_content if available, otherwise save the fact of tool_call.
                    # For simplicity, AgentMemory expects string content.
                    # If tool_calls are present, we might want to serialize them or just save the text part.
                    # Current memory.add expects string.
                    # Let's stick to saving the textual part of the message.
                    # If the message was *only* a tool call request with no text part,
                    # item.raw_item.content might be empty or None.
                    # The role is item.raw_item.role, which is typically 'assistant'
                    memory.add(role=item.raw_item.role, content=text_content)


        # 3. Add tool call results (role='tool') to memory
        #    These are added to `generated_items` after tool execution in `RunImpl.execute_tools_and_side_effects`
        #    The current structure adds LLM response first, then tools are executed.
        #    The tool results will be part of `generated_items` for the *next* turn's `combined_input`.
        #    So, we don't need to explicitly add tool results here as they will be captured
        #    when they become part of `combined_input` in a subsequent `_run_single_turn` call,
        #    and then passed as `current_turn_new_input_list` to this function.
        #    However, the problem description implies saving the "interaction", which includes tool responses *for this turn*.
        #    Let's look at `RunImpl.execute_tools_and_side_effects` - it returns `next_step_items`.
        #    These `next_step_items` will contain `ToolResultItem`s.
        #
        #    Re-evaluating: The task is to save "current turn's interaction".
        #    `processed_response.new_items` has the assistant's direct output (text, tool_code).
        #    The actual tool *results* are generated *after* this point, inside `execute_tools_and_side_effects`.
        #    This means we can't save tool *results* here. They will be saved in the *next* turn
        #    when they form part of that turn's `current_turn_new_input_list` (if they were user-like messages)
        #    or if we explicitly add items of role 'tool' from `generated_items` at the start of `_run_single_turn`.

        #    Let's stick to the current subtask: save user input and assistant response.
        #    Tool calls (requests) are part of assistant response. Tool results are separate.

        memory.save()
        # --- Memory saving END ---

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
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        tool_use_tracker: AgentToolUseTracker,
        previous_response_id: str | None,
    ) -> ModelResponse:
        model = cls._get_model(agent, run_config)
        model_settings = agent.model_settings.resolve(run_config.model_settings)
        model_settings = RunImpl.maybe_reset_tool_choice(agent, tool_use_tracker, model_settings)

        new_response = await model.get_response(
            system_instructions=system_prompt,
            input=input,
            model_settings=model_settings,
            tools=all_tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=get_model_tracing_impl(
                run_config.tracing_disabled, run_config.trace_include_sensitive_data
            ),
            previous_response_id=previous_response_id,
        )

        context_wrapper.usage.add(new_response.usage)

        return new_response

    @classmethod
    def _get_output_schema(cls, agent: Agent[Any]) -> AgentOutputSchemaBase | None:
        if agent.output_type is None or agent.output_type is str:
            return None
        elif isinstance(agent.output_type, AgentOutputSchemaBase):
            return agent.output_type

        return AgentOutputSchema(agent.output_type)

    @classmethod
    def _get_handoffs(cls, agent: Agent[Any]) -> list[Handoff]:
        handoffs = []
        for handoff_item in agent.handoffs:
            if isinstance(handoff_item, Handoff):
                handoffs.append(handoff_item)
            elif isinstance(handoff_item, Agent):
                handoffs.append(handoff(handoff_item))
        return handoffs

    @classmethod
    async def _get_all_tools(cls, agent: Agent[Any]) -> list[Tool]:
        return await agent.get_all_tools()

    @classmethod
    def _get_model(cls, agent: Agent[Any], run_config: RunConfig) -> Model:
        if isinstance(run_config.model, Model):
            return run_config.model
        elif isinstance(run_config.model, str):
            return run_config.model_provider.get_model(run_config.model)
        elif isinstance(agent.model, Model):
            return agent.model

        return run_config.model_provider.get_model(agent.model)
