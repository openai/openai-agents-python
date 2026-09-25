"""Safe SDK-generated errors at the asynchronous Realtime event boundary."""

import asyncio
import logging

import pytest

from agents import _debug
from agents.decorators import tool
from agents.realtime import RealtimeAgent, RealtimeSession
from agents.realtime.events import RealtimeError, RealtimeSessionEvent, RealtimeToolEnd
from agents.realtime.model_events import RealtimeModelToolCallEvent
from agents.realtime.model_inputs import RealtimeModelSendToolOutput
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


class UnprintableToolError(RuntimeError):
    def __str__(self) -> str:
        raise AssertionError("Exception text must not be inspected")

    def __repr__(self) -> str:
        raise AssertionError("Exception representation must not be inspected")


@pytest.mark.parametrize(
    ("send_failure", "redact_model", "redact_tool", "unprintable"),
    [
        (send_failure, redact_model, redact_tool, False)
        for send_failure in (False, True)
        for redact_model, redact_tool in (
            (True, True),
            (False, True),
            (True, False),
            (False, False),
        )
    ]
    + [(False, True, True, True)],
)
@pytest.mark.asyncio
async def test_async_tool_failure_events_are_safe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    send_failure: bool,
    redact_model: bool,
    redact_tool: bool,
    unprintable: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redact_model)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redact_tool)
    caplog.set_level(logging.WARNING, logger="openai.agents")
    secret = "synthetic-exception-secret"
    cause_secret = "synthetic-cause-secret"
    argument = "synthetic-argument-secret"
    output = "synthetic-output-secret"
    failure = UnprintableToolError(secret) if unprintable else RuntimeError(secret)
    failure.__cause__ = ValueError(cause_secret)
    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    @tool(failure_error_function=None)
    async def lookup(value: str) -> str:
        nonlocal invocations
        invocations += 1
        assert value == argument
        started.set()
        await release.wait()
        if not send_failure:
            raise failure
        return output

    model = ScriptedRealtimeModel(
        steps=(
            [
                RealtimeStep(expect=RealtimeModelSendToolOutput, error=failure),
                RealtimeStep(expect=RealtimeModelSendToolOutput),
            ]
            if send_failure
            else []
        ),
    )
    events: list[RealtimeSessionEvent] = []
    error_received = asyncio.Event()
    output_received = asyncio.Event()
    waiting_for_failure = asyncio.Event()

    async def consume(session: RealtimeSession) -> None:
        async for event in session:
            events.append(event)
            if event.type == "tool_start":
                waiting_for_failure.set()
            elif isinstance(event, RealtimeError):
                error_received.set()
            elif isinstance(event, RealtimeToolEnd):
                output_received.set()

    call = RealtimeModelToolCallEvent(
        name="lookup", call_id="synthetic-call-secret", arguments=f'{{"value":"{argument}"}}'
    )
    async with RealtimeSession(model, RealtimeAgent(name="test", tools=[lookup]), None) as session:
        consumer = asyncio.create_task(consume(session))
        try:
            await model.emit(call)
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.wait_for(waiting_for_failure.wait(), timeout=2)
            release.set()
            await asyncio.wait_for(error_received.wait(), timeout=2)
            errors = [event for event in events if isinstance(event, RealtimeError)]
            expected = (
                "Tool output send failed; cached output will be retried"
                if send_failure
                else "Tool call task failed"
            )
            assert [event.error for event in errors] == [{"message": expected}]
            assert all(
                value not in str(errors[0].error)
                for value in (secret, cause_secret, argument, output, call.call_id)
            )

            records = [
                record
                for record in caplog.records
                if record.getMessage().startswith(
                    "Realtime tool output send failed"
                    if send_failure
                    else "Realtime tool call task failed"
                )
            ]
            assert len(records) == 1
            record = records[0]
            if redact_tool:
                assert record.exc_info is None
                assert record.exc_text is None
                assert not any(
                    isinstance(value, BaseException) for value in record.__dict__.values()
                )
                rendered = logging.Formatter().format(record)
                for value in (secret, cause_secret, argument, output, call.call_id):
                    assert value not in rendered
                    assert value not in repr(record.__dict__)
            else:
                assert record.exc_info is not None
                assert secret in logging.Formatter().format(record)
                assert cause_secret in logging.Formatter().format(record)

            if send_failure:
                await model.emit(call)
                await asyncio.wait_for(output_received.wait(), timeout=2)
                assert invocations == 1
                assert [
                    event.output
                    for event in model.sent_events
                    if isinstance(event, RealtimeModelSendToolOutput)
                ] == [output, output]
                assert [event.output for event in events if isinstance(event, RealtimeToolEnd)] == [
                    output
                ]
                assert session._stored_exception is None
                assert not session._pending_tool_outputs
                model.assert_complete()
            else:
                with pytest.raises(RuntimeError) as raised:
                    await asyncio.wait_for(consumer, timeout=2)
                assert raised.value is failure
                assert raised.value.__cause__ is failure.__cause__
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
