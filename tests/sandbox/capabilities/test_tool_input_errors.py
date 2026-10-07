from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from agents import Agent, RunConfig, Runner, UserError
from agents.sandbox import Manifest
from agents.sandbox.capabilities.tools import (
    ExecCommandArgs,
    ExecCommandTool,
    ViewImageTool,
    WriteStdinTool,
)
from agents.sandbox.errors import InvalidManifestPathError
from agents.testing import ScriptedModel, assistant_message, function_call, scripted_sandbox_session
from tests.testing_processor import SPAN_PROCESSOR_TESTING


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "tool_type, arguments, expected",
    [
        (
            ExecCommandTool,
            {"cmd": "pwd", "workdir": "/outside/private-sentinel"},
            "Invalid workdir.",
        ),
        (ExecCommandTool, {"cmd": ""}, "Invalid tool arguments for cmd."),
        (
            ExecCommandTool,
            {"cmd": "private-sentinel", "max_output_tokens": 0},
            "Invalid tool arguments for max_output_tokens.",
        ),
        (ExecCommandTool, '{"cmd": "private-sentinel"', "Invalid tool arguments."),
        (ViewImageTool, {"path": "/outside/private-sentinel"}, "Invalid path."),
        (
            ViewImageTool,
            {"path": {"private-sentinel": "value"}},
            "Invalid tool arguments for path.",
        ),
        (WriteStdinTool, {"chars": "private-sentinel"}, "Invalid tool arguments for session_id."),
    ],
)
async def test_invalid_model_input_returns_safe_feedback_without_backend_calls(
    tool_type: type[ExecCommandTool] | type[ViewImageTool] | type[WriteStdinTool],
    arguments: Any,
    expected: str,
    streamed: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    tool = tool_type(session=session)
    model = ScriptedModel(
        [
            [function_call(tool.name, arguments, call_id="bad-input")],
            [assistant_message("recovered")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[tool])
    config = RunConfig(trace_include_sensitive_data=False)
    if streamed:
        result = Runner.run_streamed(agent, "go", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "go", run_config=config)

    assert result.final_output == "recovered"
    outputs = [item.output for item in result.new_items if item.type == "tool_call_output_item"]
    assert len(outputs) == 1
    assert str(outputs[0]).startswith(expected)
    assert "private-sentinel" not in str(outputs[0])
    assert model.calls[1].input[-1]["output"] == outputs[0]
    assert session.calls == ()
    model.assert_complete()
    assert "private-sentinel" not in caplog.text
    assert "private-sentinel" not in repr(
        [span.export() for span in SPAN_PROCESSOR_TESTING.get_ordered_spans()]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["runtime", "path", "validation", "cancel"])
async def test_exec_backend_failures_are_not_converted_to_argument_feedback(
    failure_kind: str,
) -> None:
    failure: BaseException
    if failure_kind == "path":
        failure = InvalidManifestPathError(rel="backend-private", reason="escape_root")
    elif failure_kind == "validation":
        with pytest.raises(ValidationError) as captured:
            ExecCommandArgs(cmd="")
        failure = captured.value
    elif failure_kind == "cancel":
        failure = asyncio.CancelledError()
    else:
        failure = RuntimeError("backend failure")

    def fail(_call: Any) -> None:
        raise failure

    session = scripted_sandbox_session([{"method": "exec", "responder": fail}])
    tool = ExecCommandTool(session=session)
    model = ScriptedModel(
        [
            [function_call(tool.name, {"cmd": "pwd"}, call_id="exec")],
            [assistant_message("done")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[tool])
    # Tool-local cancellation is converted by the existing runner, not by input handling.
    if failure_kind == "cancel":
        result = await Runner.run(agent, "go")
        outputs = [item.output for item in result.new_items if item.type == "tool_call_output_item"]
        assert outputs == ["An error occurred while running the tool. Please try again."]
        assert result.final_output == "done"
    else:
        with pytest.raises(UserError) as raised:
            await Runner.run(agent, "go")
        assert raised.value.__cause__ is failure
    assert len(session.calls) == 1
    session.assert_complete()


@pytest.mark.asyncio
async def test_direct_exec_run_still_raises_for_invalid_workdir() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    with pytest.raises(InvalidManifestPathError):
        await ExecCommandTool(session=session).run(ExecCommandArgs(cmd="pwd", workdir="/outside"))
    assert session.calls == ()
