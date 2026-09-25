from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

import pytest

import agents._debug as _debug
from agents import Agent, RunConfig, RunContextWrapper, Runner, UserError
from agents.decorators import tool
from agents.testing import ScriptedModel
from agents.tool import default_tool_error_function

from .test_responses import get_function_tool_call, get_text_message
from .testing_processor import SPAN_PROCESSOR_TESTING

SENTINEL = "synthetic-private-tool-detail"
GENERIC_ERROR = "An error occurred while running the tool. Please try again."


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("sensitive_tracing", [False, True])
@pytest.mark.parametrize("failure", ["sync", "async", "cancel"])
async def test_default_failure_keeps_exception_out_of_model_history_and_traces(
    streamed: bool,
    sensitive_tracing: bool,
    failure: Literal["sync", "async", "cancel"],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raise_error() -> str:
        try:
            raise RuntimeError(f"cause-{SENTINEL}")
        except RuntimeError as cause:
            error = ValueError(SENTINEL)
            if hasattr(error, "add_note"):
                error.add_note(f"note-{SENTINEL}")
            raise error from cause

    async def raise_async_error() -> str:
        if failure == "cancel":
            raise asyncio.CancelledError(SENTINEL)
        return raise_error()

    failing_tool = tool(raise_error if failure == "sync" else raise_async_error)
    model = ScriptedModel(
        [
            [get_function_tool_call(failing_tool.name, "{}", call_id="failure-call")],
            [get_text_message("recovered")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[failing_tool])
    config = RunConfig(trace_include_sensitive_data=sensitive_tracing)
    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        if streamed:
            result = Runner.run_streamed(agent, "start", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, "start", run_config=config)

    assert result.final_output == "recovered"
    next_input = model.calls[-1].input
    assert isinstance(next_input, list)
    outputs = [item for item in next_input if item.get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["output"] == GENERIC_ERROR
    assert SENTINEL not in json.dumps(next_input)
    assert SENTINEL not in json.dumps(result.to_input_list())
    spans = [span.export() for span in SPAN_PROCESSOR_TESTING.get_ordered_spans()]
    assert spans
    assert SENTINEL not in json.dumps(spans)
    for record in caplog.records:
        assert SENTINEL not in repr(record.__dict__)
        assert record.exc_info is None
        assert record.exc_text is None
        assert SENTINEL not in logging.Formatter().format(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["custom", "propagate", "explicit-default"])
async def test_explicit_failure_policies_remain_available(
    policy: Literal["custom", "propagate", "explicit-default"],
) -> None:
    received: list[Exception] = []

    def custom_error(context: RunContextWrapper[Any], error: Exception) -> str:
        received.append(error)
        return "Application-approved feedback"

    @tool(
        failure_error_function=(
            custom_error
            if policy == "custom"
            else default_tool_error_function
            if policy == "explicit-default"
            else None
        )
    )
    async def failing_tool() -> str:
        raise ValueError(SENTINEL)

    model = ScriptedModel(
        [
            [get_function_tool_call("failing_tool", "{}")],
            [get_text_message("recovered")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[failing_tool])
    if policy == "propagate":
        with pytest.raises(UserError, match=SENTINEL) as exc_info:
            await Runner.run(agent, "start")
        assert isinstance(exc_info.value.__cause__, ValueError)
        return

    result = await Runner.run(agent, "start")
    assert result.final_output == "recovered"
    next_input = model.calls[-1].input
    assert isinstance(next_input, list)
    output = next(item for item in next_input if item.get("type") == "function_call_output")
    if policy == "custom":
        assert output["output"] == "Application-approved feedback"
        assert len(received) == 1
        assert str(received[0]) == SENTINEL
    else:
        assert output["output"] == GENERIC_ERROR
        spans = [span.export() for span in SPAN_PROCESSOR_TESTING.get_ordered_spans()]
        assert SENTINEL not in json.dumps(spans)


@pytest.mark.asyncio
async def test_default_failure_does_not_format_the_exception() -> None:
    class UnprintableError(Exception):
        def __str__(self) -> str:
            raise AssertionError("Exception text must not be inspected")

    @tool
    async def failing_tool() -> str:
        raise UnprintableError()

    model = ScriptedModel(
        [[get_function_tool_call("failing_tool", "{}")], [get_text_message("recovered")]]
    )
    result = await Runner.run(Agent(name="test", model=model, tools=[failing_tool]), "start")
    assert result.final_output == "recovered"


@pytest.mark.asyncio
async def test_explicit_local_diagnostics_keep_exception_detail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

    @tool
    async def failing_tool() -> str:
        raise ValueError(SENTINEL)

    model = ScriptedModel(
        [[get_function_tool_call("failing_tool", "{}")], [get_text_message("recovered")]]
    )
    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        result = await Runner.run(Agent(name="test", model=model, tools=[failing_tool]), "start")
    assert result.final_output == "recovered"
    assert SENTINEL in caplog.text
    assert any(record.exc_info is not None for record in caplog.records)
    assert SENTINEL not in json.dumps(model.calls[-1].input)
    assert SENTINEL not in json.dumps(
        [span.export() for span in SPAN_PROCESSOR_TESTING.get_ordered_spans()]
    )
