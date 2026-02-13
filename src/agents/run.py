from __future__ import annotations

import asyncio
import contextlib
import warnings
from typing import Union, cast

from typing_extensions import Unpack

from . import _debug
from .agent import Agent
from .exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    RunErrorDetails,
    UserError,
)
from .guardrail import (
    InputGuardrailResult,
)
from .items import (
    ItemHelpers,
    RunItem,
    TResponseInputItem,
)
from .lifecycle import RunHooks
from .logger import logger
from .memory import Session
from .result import RunResult, RunResultStreaming
from .run_config import (
    DEFAULT_MAX_TURNS,
    CallModelData,
    CallModelInputFilter,
    ModelInputData,
    RunConfig,
    RunOptions,
    ToolErrorFormatter,
    ToolErrorFormatterArgs,
)
from .run_context import RunContextWrapper, TContext
from .run_error_handlers import RunErrorHandlers
from .run_internal.agent_runner_helpers import (
    append_model_response_if_new,
    apply_resumed_conversation_settings,
    build_interruption_result,
    build_resumed_stream_debug_extra,
    ensure_context_wrapper,
    finalize_conversation_tracking,
    input_guardrails_triggered,
    resolve_processed_response,
    resolve_resumed_context,
    resolve_trace_settings,
    save_turn_items_if_needed,
    should_cancel_parallel_model_task_on_input_guardrail_trip,
    update_run_state_for_interruption,
    validate_session_conversation_settings,
)
from .run_internal.approvals import approvals_from_step
from .run_internal.error_handlers import (
    build_run_error_data,
    create_message_output_item,
    format_final_output_text,
    resolve_run_error_handler_result,
    validate_handler_final_output,
)
from .run_internal.items import (
    copy_input_items,
    normalize_resumed_input,
)
from .run_internal.oai_conversation import OpenAIServerConversationTracker
from .run_internal.run_loop import (
    get_all_tools,
    get_handoffs,
    get_output_schema,
    initialize_computer_tools,
    resolve_interrupted_turn,
    run_final_output_hooks,
    run_input_guardrails,
    run_output_guardrails,
    run_single_turn,
    start_streaming,
    validate_run_hooks,
)
from .run_internal.run_steps import (
    NextStepFinalOutput,
    NextStepHandoff,
    NextStepInterruption,
    NextStepRunAgain,
)
from .run_internal.session_persistence import (
    persist_session_items_for_guardrail_trip,
    prepare_input_with_session,
    resumed_turn_items,
    save_result_to_session,
    save_resumed_turn_items,
    session_items_for_turn,
    update_run_state_after_resume,
)
from .run_internal.tool_use_tracker import (
    AgentToolUseTracker,
    hydrate_tool_use_tracker,
    serialize_tool_use_tracker,
)
from .run_state import RunState
from .tool import dispose_resolved_computers
from .tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from .tracing import Span, SpanError, agent_span, get_current_trace, trace
from .tracing.context import TraceCtxManager
from .tracing.span_data import AgentSpanData
from .util import _error_tracing

DEFAULT_AGENT_RUNNER: AgentRunner = None  # type: ignore
# the value is set at the end of the module

__all__ = [
    "AgentRunner",
    "Runner",
    "RunConfig",
    "RunOptions",
    "RunState",
    "RunContextWrapper",
    "ModelInputData",
    "CallModelData",
    "CallModelInputFilter",
    "ToolErrorFormatter",
    "ToolErrorFormatterArgs",
    "DEFAULT_MAX_TURNS",
    "set_default_agent_runner",
    "get_default_agent_runner",
]


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
        error_handlers: RunErrorHandlers[TContext] | None = None,
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

          1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised unless handled.
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
            error_handlers: Error handlers keyed by error kind. Currently supports max_turns.
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
            error_handlers=error_handlers,
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
        error_handlers: RunErrorHandlers[TContext] | None = None,
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

          1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised unless handled.
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
            error_handlers: Error handlers keyed by error kind. Currently supports max_turns.
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
            error_handlers=error_handlers,
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
        *,
        error_handlers: RunErrorHandlers[TContext] | None = None,
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

          1. If the max_turns is exceeded, a MaxTurnsExceeded exception is raised unless handled.
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
            error_handlers: Error handlers keyed by error kind. Currently supports max_turns.
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
            error_handlers=error_handlers,
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
        hooks = cast(RunHooks[TContext], validate_run_hooks(kwargs.get("hooks")))
        run_config = kwargs.get("run_config")
        error_handlers = kwargs.get("error_handlers")
        previous_response_id = kwargs.get("previous_response_id")
        auto_previous_response_id = kwargs.get("auto_previous_response_id", False)
        conversation_id = kwargs.get("conversation_id")
        session = kwargs.get("session")

        if run_config is None:
            run_config = RunConfig()

        is_resumed_state = isinstance(input, RunState)
        run_state: RunState[TContext] | None = None
        starting_input = input if not is_resumed_state else None
        original_user_input: str | list[TResponseInputItem] | None = None
        session_input_items_for_persistence: list[TResponseInputItem] | None = (
            [] if (session is not None and is_resumed_state) else None
        )
        # Track the most recent input batch we persisted so conversation-lock retries can rewind
        # exactly those items (and not the full history).
        last_saved_input_snapshot_for_rewind: list[TResponseInputItem] | None = None

        if is_resumed_state:
            run_state = cast(RunState[TContext], input)
            (
                conversation_id,
                previous_response_id,
                auto_previous_response_id,
            ) = apply_resumed_conversation_settings(
                run_state=run_state,
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
            validate_session_conversation_settings(
                session,
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
            starting_input = run_state._original_input
            original_user_input = copy_input_items(run_state._original_input)
            prepared_input = normalize_resumed_input(original_user_input)

            context_wrapper = resolve_resumed_context(
                run_state=run_state,
                context=context,
            )
            context = context_wrapper.context

            max_turns = run_state._max_turns
        else:
            raw_input = cast(Union[str, list[TResponseInputItem]], input)
            original_user_input = raw_input

            validate_session_conversation_settings(
                session,
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )

            server_manages_conversation = (
                conversation_id is not None
                or previous_response_id is not None
                or auto_previous_response_id
            )

            if server_manages_conversation:
                prepared_input, _ = await prepare_input_with_session(
                    raw_input,
                    session,
                    run_config.session_input_callback,
                    run_config.session_settings,
                    include_history_in_prepared_input=False,
                    preserve_dropped_new_items=True,
                )
                original_input_for_state = raw_input
                session_input_items_for_persistence = []
            else:
                (
                    prepared_input,
                    session_input_items_for_persistence,
                ) = await prepare_input_with_session(
                    raw_input,
                    session,
                    run_config.session_input_callback,
                    run_config.session_settings,
                )
                original_input_for_state = prepared_input

        # Check whether to enable OpenAI server-managed conversation
        if (
            conversation_id is not None
            or previous_response_id is not None
            or auto_previous_response_id
        ):
            server_conversation_tracker = OpenAIServerConversationTracker(
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
        else:
            server_conversation_tracker = None
        session_persistence_enabled = session is not None and server_conversation_tracker is None

        if server_conversation_tracker is not None and is_resumed_state and run_state is not None:
            session_input_items: list[TResponseInputItem] | None = None
            if session is not None:
                try:
                    session_input_items = await session.get_items()
                except Exception:
                    session_input_items = None
            server_conversation_tracker.hydrate_from_state(
                original_input=run_state._original_input,
                generated_items=run_state._generated_items,
                model_responses=run_state._model_responses,
                session_items=session_input_items,
            )

        tool_use_tracker = AgentToolUseTracker()
        if is_resumed_state and run_state is not None:
            hydrate_tool_use_tracker(tool_use_tracker, run_state, starting_agent)

        (
            trace_workflow_name,
            trace_id,
            trace_group_id,
            trace_metadata,
            trace_config,
        ) = resolve_trace_settings(run_state=run_state, run_config=run_config)

        with TraceCtxManager(
            workflow_name=trace_workflow_name,
            trace_id=trace_id,
            group_id=trace_group_id,
            metadata=trace_metadata,
            tracing=trace_config,
            disabled=run_config.tracing_disabled,
        ):
            if is_resumed_state and run_state is not None:
                run_state.set_trace(get_current_trace())
                current_turn = run_state._current_turn
                raw_original_input = run_state._original_input
                original_input = normalize_resumed_input(raw_original_input)
                generated_items = run_state._generated_items
                session_items = list(run_state._session_items)
                model_responses = run_state._model_responses
                # Cast to the correct type since we know this is TContext
                context_wrapper = cast(RunContextWrapper[TContext], run_state._context)
            else:
                current_turn = 0
                original_input = copy_input_items(original_input_for_state)
                generated_items = []
                session_items = []
                model_responses = []
                context_wrapper = ensure_context_wrapper(context)
                run_state = RunState(
                    context=context_wrapper,
                    original_input=original_input,
                    starting_agent=starting_agent,
                    max_turns=max_turns,
                    conversation_id=conversation_id,
                    previous_response_id=previous_response_id,
                    auto_previous_response_id=auto_previous_response_id,
                )
                run_state.set_trace(get_current_trace())

            pending_server_items: list[RunItem] | None = None
            input_guardrail_results: list[InputGuardrailResult] = (
                list(run_state._input_guardrail_results) if run_state is not None else []
            )
            tool_input_guardrail_results: list[ToolInputGuardrailResult] = (
                list(getattr(run_state, "_tool_input_guardrail_results", []))
                if run_state is not None
                else []
            )
            tool_output_guardrail_results: list[ToolOutputGuardrailResult] = (
                list(getattr(run_state, "_tool_output_guardrail_results", []))
                if run_state is not None
                else []
            )

            current_span: Span[AgentSpanData] | None = None
            if is_resumed_state and run_state is not None and run_state._current_agent is not None:
                current_agent = run_state._current_agent
            else:
                current_agent = starting_agent
            should_run_agent_start_hooks = True
            store_setting = current_agent.model_settings.resolve(run_config.model_settings).store

            if (
                not is_resumed_state
                and session_persistence_enabled
                and original_user_input is not None
                and session_input_items_for_persistence is None
            ):
                session_input_items_for_persistence = ItemHelpers.input_to_new_input_list(
                    original_user_input
                )

            if session_persistence_enabled and session_input_items_for_persistence:
                # Capture the exact input saved so it can be rewound on conversation lock retries.
                last_saved_input_snapshot_for_rewind = list(session_input_items_for_persistence)
                await save_result_to_session(
                    session,
                    session_input_items_for_persistence,
                    [],
                    run_state,
                    store=store_setting,
                )
                session_input_items_for_persistence = []

            try:
                while True:
                    resuming_turn = is_resumed_state
                    normalized_starting_input: str | list[TResponseInputItem] = (
                        starting_input
                        if starting_input is not None and not isinstance(starting_input, RunState)
                        else ""
                    )
                    store_setting = current_agent.model_settings.resolve(
                        run_config.model_settings
                    ).store
                    if run_state is not None and run_state._current_step is not None:
                        if isinstance(run_state._current_step, NextStepInterruption):
                            logger.debug("Continuing from interruption")
                            if (
                                not run_state._model_responses
                                or not run_state._last_processed_response
                            ):
                                raise UserError("No model response found in previous state")

                            turn_result = await resolve_interrupted_turn(
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

                            original_input = turn_result.original_input
                            generated_items, turn_session_items = resumed_turn_items(turn_result)
                            session_items.extend(turn_session_items)
                            if run_state is not None:
                                update_run_state_after_resume(
                                    run_state,
                                    turn_result=turn_result,
                                    generated_items=generated_items,
                                    session_items=session_items,
                                )

                            if (
                                session_persistence_enabled
                                and turn_result.new_step_items
                                and run_state is not None
                            ):
                                run_state._current_turn_persisted_item_count = (
                                    await save_resumed_turn_items(
                                        session=session,
                                        items=turn_session_items,
                                        persisted_count=(
                                            run_state._current_turn_persisted_item_count
                                        ),
                                        response_id=turn_result.model_response.response_id,
                                        store=store_setting,
                                    )
                                )

                            # After the resumed turn, treat subsequent turns as fresh so
                            # counters and input saving behave normally.
                            is_resumed_state = False

                            if isinstance(turn_result.next_step, NextStepInterruption):
                                interruption_result_input: str | list[TResponseInputItem] = (
                                    original_input
                                )
                                append_model_response_if_new(
                                    model_responses, turn_result.model_response
                                )
                                processed_response_for_state = resolve_processed_response(
                                    run_state=run_state,
                                    processed_response=turn_result.processed_response,
                                )
                                if run_state is not None:
                                    update_run_state_for_interruption(
                                        run_state=run_state,
                                        model_responses=model_responses,
                                        processed_response=processed_response_for_state,
                                        generated_items=generated_items,
                                        session_items=session_items,
                                        current_turn=current_turn,
                                        next_step=turn_result.next_step,
                                    )
                                result = build_interruption_result(
                                    result_input=interruption_result_input,
                                    session_items=session_items,
                                    model_responses=model_responses,
                                    current_agent=current_agent,
                                    input_guardrail_results=input_guardrail_results,
                                    tool_input_guardrail_results=(
                                        turn_result.tool_input_guardrail_results
                                    ),
                                    tool_output_guardrail_results=(
                                        turn_result.tool_output_guardrail_results
                                    ),
                                    context_wrapper=context_wrapper,
                                    interruptions=approvals_from_step(turn_result.next_step),
                                    processed_response=processed_response_for_state,
                                    tool_use_tracker=tool_use_tracker,
                                    max_turns=max_turns,
                                    current_turn=current_turn,
                                    generated_items=generated_items,
                                    run_state=run_state,
                                    original_input=original_input,
                                )
                                return finalize_conversation_tracking(
                                    result,
                                    server_conversation_tracker=server_conversation_tracker,
                                    run_state=run_state,
                                )

                            if isinstance(turn_result.next_step, NextStepRunAgain):
                                continue

                            append_model_response_if_new(
                                model_responses, turn_result.model_response
                            )
                            tool_input_guardrail_results.extend(
                                turn_result.tool_input_guardrail_results
                            )
                            tool_output_guardrail_results.extend(
                                turn_result.tool_output_guardrail_results
                            )

                            if isinstance(turn_result.next_step, NextStepFinalOutput):
                                output_guardrail_results = await run_output_guardrails(
                                    current_agent.output_guardrails
                                    + (run_config.output_guardrails or []),
                                    current_agent,
                                    turn_result.next_step.output,
                                    context_wrapper,
                                )
                                current_step = getattr(run_state, "_current_step", None)
                                approvals_from_state = approvals_from_step(current_step)
                                result = RunResult(
                                    input=turn_result.original_input,
                                    new_items=session_items,
                                    raw_responses=model_responses,
                                    final_output=turn_result.next_step.output,
                                    _last_agent=current_agent,
                                    input_guardrail_results=input_guardrail_results,
                                    output_guardrail_results=output_guardrail_results,
                                    tool_input_guardrail_results=tool_input_guardrail_results,
                                    tool_output_guardrail_results=tool_output_guardrail_results,
                                    context_wrapper=context_wrapper,
                                    interruptions=approvals_from_state,
                                    _tool_use_tracker_snapshot=serialize_tool_use_tracker(
                                        tool_use_tracker
                                    ),
                                    max_turns=max_turns,
                                )
                                result._current_turn = current_turn
                                result._model_input_items = list(generated_items)
                                if run_state is not None:
                                    result._trace_state = run_state._trace_state
                                if session_persistence_enabled:
                                    input_items_for_save_1: list[TResponseInputItem] = (
                                        session_input_items_for_persistence
                                        if session_input_items_for_persistence is not None
                                        else []
                                    )
                                    await save_result_to_session(
                                        session,
                                        input_items_for_save_1,
                                        session_items_for_turn(turn_result),
                                        run_state,
                                        response_id=turn_result.model_response.response_id,
                                        store=store_setting,
                                    )
                                result._original_input = copy_input_items(original_input)
                                return finalize_conversation_tracking(
                                    result,
                                    server_conversation_tracker=server_conversation_tracker,
                                    run_state=run_state,
                                )
                            elif isinstance(turn_result.next_step, NextStepHandoff):
                                current_agent = cast(
                                    Agent[TContext], turn_result.next_step.new_agent
                                )
                                if run_state is not None:
                                    run_state._current_agent = current_agent
                                starting_input = turn_result.original_input
                                original_input = turn_result.original_input
                                if current_span is not None:
                                    current_span.finish(reset_current=True)
                                current_span = None
                                should_run_agent_start_hooks = True
                                continue

                            continue

                    if run_state is not None:
                        if run_state._current_step is None:
                            run_state._current_step = NextStepRunAgain()  # type: ignore[assignment]
                    all_tools = await get_all_tools(current_agent, context_wrapper)
                    await initialize_computer_tools(
                        tools=all_tools, context_wrapper=context_wrapper
                    )

                    if current_span is None:
                        handoff_names = [
                            h.agent_name for h in await get_handoffs(current_agent, context_wrapper)
                        ]
                        if output_schema := get_output_schema(current_agent):
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
                        max_turns_error = MaxTurnsExceeded(f"Max turns ({max_turns}) exceeded")
                        run_error_data = build_run_error_data(
                            input=original_input,
                            new_items=session_items,
                            raw_responses=model_responses,
                            last_agent=current_agent,
                        )
                        handler_result = await resolve_run_error_handler_result(
                            error_handlers=error_handlers,
                            error=max_turns_error,
                            context_wrapper=context_wrapper,
                            run_data=run_error_data,
                        )
                        if handler_result is None:
                            raise max_turns_error

                        validated_output = validate_handler_final_output(
                            current_agent, handler_result.final_output
                        )
                        output_text = format_final_output_text(current_agent, validated_output)
                        synthesized_item = create_message_output_item(current_agent, output_text)
                        include_in_history = handler_result.include_in_history
                        if include_in_history:
                            generated_items.append(synthesized_item)
                            session_items.append(synthesized_item)

                        await run_final_output_hooks(
                            current_agent,
                            hooks,
                            context_wrapper,
                            validated_output,
                        )
                        output_guardrail_results = await run_output_guardrails(
                            current_agent.output_guardrails + (run_config.output_guardrails or []),
                            current_agent,
                            validated_output,
                            context_wrapper,
                        )
                        current_step = getattr(run_state, "_current_step", None)
                        approvals_from_state = approvals_from_step(current_step)
                        result = RunResult(
                            input=original_input,
                            new_items=session_items,
                            raw_responses=model_responses,
                            final_output=validated_output,
                            _last_agent=current_agent,
                            input_guardrail_results=input_guardrail_results,
                            output_guardrail_results=output_guardrail_results,
                            tool_input_guardrail_results=tool_input_guardrail_results,
                            tool_output_guardrail_results=tool_output_guardrail_results,
                            context_wrapper=context_wrapper,
                            interruptions=approvals_from_state,
                            _tool_use_tracker_snapshot=serialize_tool_use_tracker(tool_use_tracker),
                            max_turns=max_turns,
                        )
                        result._current_turn = max_turns
                        result._model_input_items = list(generated_items)
                        if run_state is not None:
                            result._trace_state = run_state._trace_state
                        if session_persistence_enabled and include_in_history:
                            handler_input_items_for_save: list[TResponseInputItem] = (
                                session_input_items_for_persistence
                                if session_input_items_for_persistence is not None
                                else []
                            )
                            await save_result_to_session(
                                session,
                                handler_input_items_for_save,
                                [synthesized_item],
                                run_state,
                                response_id=None,
                                store=store_setting,
                            )
                        result._original_input = copy_input_items(original_input)
                        return finalize_conversation_tracking(
                            result,
                            server_conversation_tracker=server_conversation_tracker,
                            run_state=run_state,
                        )

                    if run_state is not None and not resuming_turn:
                        run_state._current_turn_persisted_item_count = 0

                    logger.debug("Running agent %s (turn %s)", current_agent.name, current_turn)

                    if session_persistence_enabled:
                        try:
                            last_saved_input_snapshot_for_rewind = (
                                ItemHelpers.input_to_new_input_list(original_input)
                            )
                        except Exception:
                            last_saved_input_snapshot_for_rewind = None

                    items_for_model = (
                        pending_server_items
                        if server_conversation_tracker is not None and pending_server_items
                        else generated_items
                    )

                    if current_turn <= 1:
                        all_input_guardrails = starting_agent.input_guardrails + (
                            run_config.input_guardrails or []
                        )
                        sequential_guardrails = [
                            g for g in all_input_guardrails if not g.run_in_parallel
                        ]
                        parallel_guardrails = [g for g in all_input_guardrails if g.run_in_parallel]

                        try:
                            sequential_results = []
                            if sequential_guardrails:
                                sequential_results = await run_input_guardrails(
                                    starting_agent,
                                    sequential_guardrails,
                                    copy_input_items(prepared_input),
                                    context_wrapper,
                                )
                        except InputGuardrailTripwireTriggered:
                            session_input_items_for_persistence = (
                                await persist_session_items_for_guardrail_trip(
                                    session,
                                    server_conversation_tracker,
                                    session_input_items_for_persistence,
                                    original_user_input,
                                    run_state,
                                    store=store_setting,
                                )
                            )
                            raise

                        parallel_results: list[InputGuardrailResult] = []
                        model_task = asyncio.create_task(
                            run_single_turn(
                                agent=current_agent,
                                all_tools=all_tools,
                                original_input=original_input,
                                generated_items=items_for_model,
                                hooks=hooks,
                                context_wrapper=context_wrapper,
                                run_config=run_config,
                                should_run_agent_start_hooks=should_run_agent_start_hooks,
                                tool_use_tracker=tool_use_tracker,
                                server_conversation_tracker=server_conversation_tracker,
                                session=session,
                                session_items_to_rewind=(
                                    last_saved_input_snapshot_for_rewind
                                    if not is_resumed_state and session_persistence_enabled
                                    else None
                                ),
                            )
                        )

                        if parallel_guardrails:
                            try:
                                parallel_results, turn_result = await asyncio.gather(
                                    run_input_guardrails(
                                        starting_agent,
                                        parallel_guardrails,
                                        copy_input_items(prepared_input),
                                        context_wrapper,
                                    ),
                                    model_task,
                                )
                            except InputGuardrailTripwireTriggered:
                                if should_cancel_parallel_model_task_on_input_guardrail_trip():
                                    if not model_task.done():
                                        model_task.cancel()
                                    await asyncio.gather(model_task, return_exceptions=True)
                                session_input_items_for_persistence = (
                                    await persist_session_items_for_guardrail_trip(
                                        session,
                                        server_conversation_tracker,
                                        session_input_items_for_persistence,
                                        original_user_input,
                                        run_state,
                                        store=store_setting,
                                    )
                                )
                                raise
                        else:
                            turn_result = await model_task

                        input_guardrail_results.extend(sequential_results)
                        input_guardrail_results.extend(parallel_results)
                    else:
                        turn_result = await run_single_turn(
                            agent=current_agent,
                            all_tools=all_tools,
                            original_input=original_input,
                            generated_items=items_for_model,
                            hooks=hooks,
                            context_wrapper=context_wrapper,
                            run_config=run_config,
                            should_run_agent_start_hooks=should_run_agent_start_hooks,
                            tool_use_tracker=tool_use_tracker,
                            server_conversation_tracker=server_conversation_tracker,
                            session=session,
                            session_items_to_rewind=(
                                last_saved_input_snapshot_for_rewind
                                if not is_resumed_state and session_persistence_enabled
                                else None
                            ),
                        )

                    # Start hooks should only run on the first turn unless reset by a handoff.
                    last_saved_input_snapshot_for_rewind = None
                    should_run_agent_start_hooks = False

                    model_responses.append(turn_result.model_response)
                    original_input = turn_result.original_input
                    # For model input, use new_step_items (filtered on handoffs).
                    generated_items = turn_result.pre_step_items + turn_result.new_step_items
                    # Accumulate unfiltered items for observability.
                    turn_session_items = session_items_for_turn(turn_result)
                    session_items.extend(turn_session_items)
                    if server_conversation_tracker is not None:
                        pending_server_items = list(turn_result.new_step_items)
                        server_conversation_tracker.track_server_items(turn_result.model_response)

                    tool_input_guardrail_results.extend(turn_result.tool_input_guardrail_results)
                    tool_output_guardrail_results.extend(turn_result.tool_output_guardrail_results)

                    items_to_save_turn = list(turn_session_items)
                    if not isinstance(turn_result.next_step, NextStepInterruption):
                        # When resuming a turn we have already persisted the tool_call items;
                        if (
                            is_resumed_state
                            and run_state
                            and run_state._current_turn_persisted_item_count > 0
                        ):
                            items_to_save_turn = [
                                item for item in items_to_save_turn if item.type != "tool_call_item"
                            ]
                        if session_persistence_enabled:
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
                                    saved_count = await save_result_to_session(
                                        session,
                                        [],
                                        items_to_save_turn,
                                        None,
                                        response_id=turn_result.model_response.response_id,
                                        store=store_setting,
                                    )
                                    run_state._current_turn_persisted_item_count += saved_count
                                else:
                                    await save_result_to_session(
                                        session,
                                        [],
                                        items_to_save_turn,
                                        run_state,
                                        response_id=turn_result.model_response.response_id,
                                        store=store_setting,
                                    )

                    # After the first resumed turn, treat subsequent turns as fresh
                    # so counters and input saving behave normally.
                    is_resumed_state = False

                    try:
                        if isinstance(turn_result.next_step, NextStepFinalOutput):
                            output_guardrail_results = await run_output_guardrails(
                                current_agent.output_guardrails
                                + (run_config.output_guardrails or []),
                                current_agent,
                                turn_result.next_step.output,
                                context_wrapper,
                            )

                            # Ensure starting_input is not None and not RunState
                            final_output_result_input: str | list[TResponseInputItem] = (
                                normalized_starting_input
                            )
                            result = RunResult(
                                input=final_output_result_input,
                                new_items=session_items,
                                raw_responses=model_responses,
                                final_output=turn_result.next_step.output,
                                _last_agent=current_agent,
                                input_guardrail_results=input_guardrail_results,
                                output_guardrail_results=output_guardrail_results,
                                tool_input_guardrail_results=tool_input_guardrail_results,
                                tool_output_guardrail_results=tool_output_guardrail_results,
                                context_wrapper=context_wrapper,
                                interruptions=[],
                                _tool_use_tracker_snapshot=serialize_tool_use_tracker(
                                    tool_use_tracker
                                ),
                                max_turns=max_turns,
                            )
                            result._current_turn = current_turn
                            result._model_input_items = list(generated_items)
                            if run_state is not None:
                                result._current_turn_persisted_item_count = (
                                    run_state._current_turn_persisted_item_count
                                )
                            await save_turn_items_if_needed(
                                session=session,
                                run_state=run_state,
                                session_persistence_enabled=session_persistence_enabled,
                                input_guardrail_results=input_guardrail_results,
                                items=session_items_for_turn(turn_result),
                                response_id=turn_result.model_response.response_id,
                                store=store_setting,
                            )
                            result._original_input = copy_input_items(original_input)
                            return finalize_conversation_tracking(
                                result,
                                server_conversation_tracker=server_conversation_tracker,
                                run_state=run_state,
                            )
                        elif isinstance(turn_result.next_step, NextStepInterruption):
                            if session_persistence_enabled:
                                if not input_guardrails_triggered(input_guardrail_results):
                                    # Persist session items but skip approval placeholders.
                                    input_items_for_save_interruption: list[TResponseInputItem] = (
                                        session_input_items_for_persistence
                                        if session_input_items_for_persistence is not None
                                        else []
                                    )
                                    await save_result_to_session(
                                        session,
                                        input_items_for_save_interruption,
                                        session_items_for_turn(turn_result),
                                        run_state,
                                        response_id=turn_result.model_response.response_id,
                                        store=store_setting,
                                    )
                            append_model_response_if_new(
                                model_responses, turn_result.model_response
                            )
                            processed_response_for_state = resolve_processed_response(
                                run_state=run_state,
                                processed_response=turn_result.processed_response,
                            )
                            if run_state is not None:
                                update_run_state_for_interruption(
                                    run_state=run_state,
                                    model_responses=model_responses,
                                    processed_response=processed_response_for_state,
                                    generated_items=generated_items,
                                    session_items=session_items,
                                    current_turn=current_turn,
                                    next_step=turn_result.next_step,
                                )
                            # Ensure starting_input is not None and not RunState
                            interruption_result_input2: str | list[TResponseInputItem] = (
                                normalized_starting_input
                            )
                            result = build_interruption_result(
                                result_input=interruption_result_input2,
                                session_items=session_items,
                                model_responses=model_responses,
                                current_agent=current_agent,
                                input_guardrail_results=input_guardrail_results,
                                tool_input_guardrail_results=tool_input_guardrail_results,
                                tool_output_guardrail_results=tool_output_guardrail_results,
                                context_wrapper=context_wrapper,
                                interruptions=approvals_from_step(turn_result.next_step),
                                processed_response=processed_response_for_state,
                                tool_use_tracker=tool_use_tracker,
                                max_turns=max_turns,
                                current_turn=current_turn,
                                generated_items=generated_items,
                                run_state=run_state,
                                original_input=original_input,
                            )
                            return finalize_conversation_tracking(
                                result,
                                server_conversation_tracker=server_conversation_tracker,
                                run_state=run_state,
                            )
                        elif isinstance(turn_result.next_step, NextStepHandoff):
                            current_agent = cast(Agent[TContext], turn_result.next_step.new_agent)
                            if run_state is not None:
                                run_state._current_agent = current_agent
                            # Next agent starts with the nested/filtered input.
                            # Assign without type annotation to avoid redefinition error
                            starting_input = turn_result.original_input
                            original_input = turn_result.original_input
                            current_span.finish(reset_current=True)
                            current_span = None
                            should_run_agent_start_hooks = True
                        elif isinstance(turn_result.next_step, NextStepRunAgain):
                            await save_turn_items_if_needed(
                                session=session,
                                run_state=run_state,
                                session_persistence_enabled=session_persistence_enabled,
                                input_guardrail_results=input_guardrail_results,
                                items=session_items_for_turn(turn_result),
                                response_id=turn_result.model_response.response_id,
                                store=store_setting,
                            )
                            continue
                        else:
                            raise AgentsException(
                                f"Unknown next step type: {type(turn_result.next_step)}"
                            )
                    finally:
                        # execute_tools_and_side_effects returns a SingleStepResult that
                        # stores direct references to the `pre_step_items` and `new_step_items`
                        # lists it manages internally. Clear them here so the next turn does not
                        # hold on to items from previous turns and to avoid leaking agent refs.
                        turn_result.pre_step_items.clear()
                        turn_result.new_step_items.clear()
            except AgentsException as exc:
                exc.run_data = RunErrorDetails(
                    input=original_input,
                    new_items=session_items,
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
        error_handlers = kwargs.get("error_handlers")
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
                error_handlers=error_handlers,
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
        hooks = cast(RunHooks[TContext], validate_run_hooks(kwargs.get("hooks")))
        run_config = kwargs.get("run_config")
        error_handlers = kwargs.get("error_handlers")
        previous_response_id = kwargs.get("previous_response_id")
        auto_previous_response_id = kwargs.get("auto_previous_response_id", False)
        conversation_id = kwargs.get("conversation_id")
        session = kwargs.get("session")

        if run_config is None:
            run_config = RunConfig()

        # Handle RunState input
        is_resumed_state = isinstance(input, RunState)
        run_state: RunState[TContext] | None = None
        input_for_result: str | list[TResponseInputItem]
        starting_input = input if not is_resumed_state else None

        if is_resumed_state:
            run_state = cast(RunState[TContext], input)
            (
                conversation_id,
                previous_response_id,
                auto_previous_response_id,
            ) = apply_resumed_conversation_settings(
                run_state=run_state,
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
            validate_session_conversation_settings(
                session,
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
            # When resuming, use the original_input from state.
            # primeFromState will mark items as sent so prepareInput skips them
            starting_input = run_state._original_input

            logger.debug(
                "Resuming from RunState in run_streaming()",
                extra=build_resumed_stream_debug_extra(
                    run_state,
                    include_tool_output=not _debug.DONT_LOG_TOOL_DATA,
                ),
            )
            # When resuming, use the original_input from state.
            # primeFromState will mark items as sent so prepareInput skips them
            raw_input_for_result = run_state._original_input
            input_for_result = normalize_resumed_input(raw_input_for_result)
            # Use context from RunState if not provided, otherwise override it.
            context_wrapper = resolve_resumed_context(
                run_state=run_state,
                context=context,
            )
            context = context_wrapper.context

            # Override max_turns with the state's max_turns to preserve it across resumption
            max_turns = run_state._max_turns

        else:
            # input is already str | list[TResponseInputItem] when not RunState
            # Reuse input_for_result variable from outer scope
            input_for_result = cast(Union[str, list[TResponseInputItem]], input)
            validate_session_conversation_settings(
                session,
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )
            context_wrapper = ensure_context_wrapper(context)
            # input_for_state is the same as input_for_result here
            input_for_state = input_for_result
            run_state = RunState(
                context=context_wrapper,
                original_input=copy_input_items(input_for_state),
                starting_agent=starting_agent,
                max_turns=max_turns,
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
            )

        (
            trace_workflow_name,
            trace_id,
            trace_group_id,
            trace_metadata,
            trace_config,
        ) = resolve_trace_settings(run_state=run_state, run_config=run_config)

        # If there's already a trace, we don't create a new one. In addition, we can't end the
        # trace here, because the actual work is done in `stream_events` and this method ends
        # before that.
        new_trace = (
            None
            if get_current_trace()
            else trace(
                workflow_name=trace_workflow_name,
                trace_id=trace_id,
                group_id=trace_group_id,
                metadata=trace_metadata,
                tracing=trace_config,
                disabled=run_config.tracing_disabled,
            )
        )
        if run_state is not None:
            run_state.set_trace(new_trace or get_current_trace())

        schema_agent = (
            run_state._current_agent if run_state and run_state._current_agent else starting_agent
        )
        output_schema = get_output_schema(schema_agent)

        streamed_input: str | list[TResponseInputItem] = (
            starting_input
            if starting_input is not None and not isinstance(starting_input, RunState)
            else ""
        )
        streamed_result = RunResultStreaming(
            input=copy_input_items(streamed_input),
            # When resuming from RunState, use session_items from state.
            # primeFromState will mark items as sent so prepareInput skips them
            new_items=run_state._session_items if run_state else [],
            current_agent=schema_agent,
            raw_responses=run_state._model_responses if run_state else [],
            final_output=None,
            is_complete=False,
            current_turn=run_state._current_turn if run_state else 0,
            max_turns=max_turns,
            input_guardrail_results=(list(run_state._input_guardrail_results) if run_state else []),
            output_guardrail_results=(
                list(run_state._output_guardrail_results) if run_state else []
            ),
            tool_input_guardrail_results=(
                list(getattr(run_state, "_tool_input_guardrail_results", [])) if run_state else []
            ),
            tool_output_guardrail_results=(
                list(getattr(run_state, "_tool_output_guardrail_results", [])) if run_state else []
            ),
            _current_agent_output_schema=output_schema,
            trace=new_trace,
            context_wrapper=context_wrapper,
            interruptions=[],
            # Preserve persisted-count from state to avoid re-saving items when resuming.
            # If a cross-SDK state omits the counter, fall back to len(generated_items)
            # to avoid duplication.
            _current_turn_persisted_item_count=(
                run_state._current_turn_persisted_item_count if run_state else 0
            ),
            # When resuming from RunState, preserve the original input from the state
            # This ensures originalInput in serialized state reflects the first turn's input
            _original_input=(
                copy_input_items(run_state._original_input)
                if run_state and run_state._original_input is not None
                else copy_input_items(streamed_input)
            ),
        )
        streamed_result._model_input_items = (
            list(run_state._generated_items) if run_state is not None else []
        )
        if run_state is not None:
            streamed_result._trace_state = run_state._trace_state
        # Store run_state in streamed_result._state so it's accessible throughout streaming
        # Now that we create run_state for both fresh and resumed runs, always set it
        streamed_result._conversation_id = conversation_id
        streamed_result._previous_response_id = previous_response_id
        streamed_result._auto_previous_response_id = auto_previous_response_id
        streamed_result._state = run_state
        if run_state is not None:
            streamed_result._tool_use_tracker_snapshot = run_state.get_tool_use_tracker_snapshot()

        # Kick off the actual agent loop in the background and return the streamed result object.
        streamed_result.run_loop_task = asyncio.create_task(
            start_streaming(
                starting_input=input_for_result,
                streamed_result=streamed_result,
                starting_agent=starting_agent,
                max_turns=max_turns,
                hooks=hooks,
                context_wrapper=context_wrapper,
                run_config=run_config,
                error_handlers=error_handlers,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
                conversation_id=conversation_id,
                session=session,
                run_state=run_state,
                is_resumed_state=is_resumed_state,
            )
        )
        return streamed_result


DEFAULT_AGENT_RUNNER = AgentRunner()
