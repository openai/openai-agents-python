from __future__ import annotations

import asyncio
from typing import Any

from ..agent import Agent
from ..exceptions import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered
from ..guardrail import (
    InputGuardrail,
    InputGuardrailResult,
    OutputGuardrail,
    OutputGuardrailResult,
)
from ..items import TResponseInputItem
from ..result import RunResultStreaming
from ..run_context import RunContextWrapper, TContext
from ..tracing import Span, SpanError, guardrail_span
from ..util import _error_tracing
from .run_steps import QueueCompleteSentinel

__all__ = [
    "run_single_input_guardrail",
    "run_single_output_guardrail",
    "run_input_guardrails_with_queue",
    "run_input_guardrails",
    "run_output_guardrails",
    "input_guardrail_tripwire_triggered_for_stream",
]


async def run_single_input_guardrail(
    agent: Agent[Any],
    guardrail: InputGuardrail[TContext],
    input: str | list[TResponseInputItem],
    context: RunContextWrapper[TContext],
) -> InputGuardrailResult:
    with guardrail_span(guardrail.get_name()) as span_guardrail:
        result = await guardrail.run(agent, input, context)
        span_guardrail.span_data.triggered = result.output.tripwire_triggered
        return result


async def run_single_output_guardrail(
    guardrail: OutputGuardrail[TContext],
    agent: Agent[Any],
    agent_output: Any,
    context: RunContextWrapper[TContext],
) -> OutputGuardrailResult:
    with guardrail_span(guardrail.get_name()) as span_guardrail:
        result = await guardrail.run(agent=agent, agent_output=agent_output, context=context)
        span_guardrail.span_data.triggered = result.output.tripwire_triggered
        return result


async def run_input_guardrails_with_queue(
    agent: Agent[Any],
    guardrails: list[InputGuardrail[TContext]],
    input: str | list[TResponseInputItem],
    context: RunContextWrapper[TContext],
    streamed_result: RunResultStreaming,
    parent_span: Span[Any] | None,
) -> None:
    """Run guardrails concurrently and stream results into the queue."""
    queue = streamed_result._input_guardrail_queue

    guardrail_tasks = [
        asyncio.create_task(run_single_input_guardrail(agent, guardrail, input, context))
        for guardrail in guardrails
    ]
    try:
        for done in asyncio.as_completed(guardrail_tasks):
            result = await done
            # Publish into the runner-owned accumulator as each guardrail completes, so no exit
            # path can omit results that already finished. This mirrors how the non-streamed
            # `run_input_guardrails` records into its caller-owned sink.
            streamed_result.input_guardrail_results = streamed_result.input_guardrail_results + [
                result
            ]
            if result.output.tripwire_triggered:
                streamed_result._triggered_input_guardrail_result = result
                queue.put_nowait(result)
                for t in guardrail_tasks:
                    t.cancel()
                await asyncio.gather(*guardrail_tasks, return_exceptions=True)
                span_error = SpanError(
                    message="Guardrail tripwire triggered",
                    data={
                        "guardrail": result.guardrail.get_name(),
                        "type": "input_guardrail",
                    },
                )
                if parent_span is not None:
                    _error_tracing.attach_error_to_span(parent_span, span_error)
                else:
                    # Early first-turn streamed guardrails can run before the agent span exists.
                    _error_tracing.attach_error_to_current_span(span_error)
                break
            queue.put_nowait(result)
    except BaseException as error:
        for t in guardrail_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*guardrail_tasks, return_exceptions=True)
        if (
            isinstance(error, Exception)
            and asyncio.current_task() is streamed_result._input_guardrails_task
            and not streamed_result.is_complete
        ):
            if streamed_result.run_loop_task and not streamed_result.run_loop_task.done():
                streamed_result.run_loop_task.cancel()
            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
        raise


async def run_input_guardrails(
    agent: Agent[Any],
    guardrails: list[InputGuardrail[TContext]],
    input: str | list[TResponseInputItem],
    context: RunContextWrapper[TContext],
    results_sink: list[InputGuardrailResult] | None = None,
) -> list[InputGuardrailResult]:
    """Run input guardrails concurrently and raise on tripwires.

    Results are recorded into ``results_sink`` as each guardrail completes, including the
    tripping result, so callers can report them even when this function raises. The streamed
    path publishes the same results through `RunResultStreaming.input_guardrail_results`.
    """
    if not guardrails:
        return []

    guardrail_tasks = [
        asyncio.create_task(run_single_input_guardrail(agent, guardrail, input, context))
        for guardrail in guardrails
    ]

    guardrail_results: list[InputGuardrailResult] = []

    def record(result: InputGuardrailResult) -> None:
        guardrail_results.append(result)
        if results_sink is not None:
            results_sink.append(result)

    try:
        for done in asyncio.as_completed(guardrail_tasks):
            result = await done
            if result.output.tripwire_triggered:
                record(result)
                for t in guardrail_tasks:
                    t.cancel()
                await asyncio.gather(*guardrail_tasks, return_exceptions=True)
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Guardrail tripwire triggered",
                        data={"guardrail": result.guardrail.get_name()},
                    )
                )
                raise InputGuardrailTripwireTriggered(result)
            record(result)
    except BaseException:
        # On any error (including a guardrail raising or the caller being cancelled),
        # cancel and await siblings so they don't leak past this function's return.
        for t in guardrail_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*guardrail_tasks, return_exceptions=True)
        raise

    return guardrail_results


async def run_output_guardrails(
    guardrails: list[OutputGuardrail[TContext]],
    agent: Agent[TContext],
    agent_output: Any,
    context: RunContextWrapper[TContext],
    results_sink: list[OutputGuardrailResult] | None = None,
) -> list[OutputGuardrailResult]:
    """Run output guardrails in parallel and raise on tripwires.

    Results are recorded into ``results_sink`` as each guardrail completes, including the
    tripping result, so callers can report them even when this function raises. This mirrors
    `run_input_guardrails`.
    """
    if not guardrails:
        return []

    guardrail_tasks = [
        asyncio.create_task(run_single_output_guardrail(guardrail, agent, agent_output, context))
        for guardrail in guardrails
    ]

    guardrail_results: list[OutputGuardrailResult] = []

    def record(result: OutputGuardrailResult) -> None:
        guardrail_results.append(result)
        if results_sink is not None:
            results_sink.append(result)

    try:
        for done in asyncio.as_completed(guardrail_tasks):
            result = await done
            if result.output.tripwire_triggered:
                record(result)
                for t in guardrail_tasks:
                    t.cancel()
                await asyncio.gather(*guardrail_tasks, return_exceptions=True)
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Guardrail tripwire triggered",
                        data={"guardrail": result.guardrail.get_name()},
                    )
                )
                raise OutputGuardrailTripwireTriggered(result)
            record(result)
    except BaseException:
        # On any error (including a guardrail raising or the caller being cancelled),
        # cancel and await siblings so they don't leak past this function's return.
        for t in guardrail_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*guardrail_tasks, return_exceptions=True)
        raise

    return guardrail_results


async def input_guardrail_tripwire_triggered_for_stream(
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
