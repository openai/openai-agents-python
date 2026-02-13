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
    parent_span: Span[Any],
) -> None:
    """Run guardrails concurrently and stream results into the queue."""
    queue = streamed_result._input_guardrail_queue

    guardrail_tasks = [
        asyncio.create_task(run_single_input_guardrail(agent, guardrail, input, context))
        for guardrail in guardrails
    ]
    guardrail_results = []
    try:
        for done in asyncio.as_completed(guardrail_tasks):
            result = await done
            if result.output.tripwire_triggered:
                for t in guardrail_tasks:
                    t.cancel()
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


async def run_input_guardrails(
    agent: Agent[Any],
    guardrails: list[InputGuardrail[TContext]],
    input: str | list[TResponseInputItem],
    context: RunContextWrapper[TContext],
) -> list[InputGuardrailResult]:
    """Run input guardrails concurrently and raise on tripwires."""
    if not guardrails:
        return []

    guardrail_tasks = [
        asyncio.create_task(run_single_input_guardrail(agent, guardrail, input, context))
        for guardrail in guardrails
    ]

    guardrail_results: list[InputGuardrailResult] = []

    for done in asyncio.as_completed(guardrail_tasks):
        result = await done
        if result.output.tripwire_triggered:
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
        guardrail_results.append(result)

    return guardrail_results


async def run_output_guardrails(
    guardrails: list[OutputGuardrail[TContext]],
    agent: Agent[TContext],
    agent_output: Any,
    context: RunContextWrapper[TContext],
) -> list[OutputGuardrailResult]:
    """Run output guardrails in parallel and raise on tripwires."""
    if not guardrails:
        return []

    guardrail_tasks = [
        asyncio.create_task(run_single_output_guardrail(guardrail, agent, agent_output, context))
        for guardrail in guardrails
    ]

    guardrail_results: list[OutputGuardrailResult] = []

    for done in asyncio.as_completed(guardrail_tasks):
        result = await done
        if result.output.tripwire_triggered:
            for t in guardrail_tasks:
                t.cancel()
            _error_tracing.attach_error_to_current_span(
                SpanError(
                    message="Guardrail tripwire triggered",
                    data={"guardrail": result.guardrail.get_name()},
                )
            )
            raise OutputGuardrailTripwireTriggered(result)
        guardrail_results.append(result)

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
