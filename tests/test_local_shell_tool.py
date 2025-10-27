"""Tests for local shell tool execution.

These confirm that LocalShellAction.execute forwards the command to the executor
and that Runner.run executes local shell calls and records their outputs.
"""

from typing import Any, cast

import pytest
from openai.types.responses import ResponseOutputText
from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction

from agents import (
    Agent,
    LocalShellCommandRequest,
    LocalShellTool,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
)
from agents._run_impl import LocalShellAction, ToolRunLocalShellCall
from agents.items import ToolCallOutputItem

from .fake_model import FakeModel
from .test_responses import get_text_message


class RecordingLocalShellExecutor:
    """A `LocalShellTool` executor that records the requests it receives."""

    def __init__(self, output: str = "shell output") -> None:
        self.output = output
        self.calls: list[LocalShellCommandRequest] = []

    def __call__(self, request: LocalShellCommandRequest) -> str:
        self.calls.append(request)
        return self.output


@pytest.mark.asyncio
async def test_local_shell_action_execute_invokes_executor() -> None:
    executor = RecordingLocalShellExecutor(output="test output")
    tool = LocalShellTool(executor=executor)

    action = LocalShellCallAction(
        command=["bash", "-c", "ls"],
        env={"TEST": "value"},
        type="exec",
        timeout_ms=5000,
        working_directory="/tmp",
    )
    tool_call = LocalShellCall(
        id="lsh_123",
        action=action,
        call_id="call_456",
        status="completed",
        type="local_shell_call",
    )

    tool_run = ToolRunLocalShellCall(tool_call=tool_call, local_shell_tool=tool)
    agent = Agent(name="test_agent", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    output_item = await LocalShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert len(executor.calls) == 1
    request = executor.calls[0]
    assert isinstance(request, LocalShellCommandRequest)
    assert request.ctx_wrapper is context_wrapper
    assert request.data is tool_call
    assert request.data.action.command == ["bash", "-c", "ls"]
    assert request.data.action.env == {"TEST": "value"}
    assert request.data.action.timeout_ms == 5000
    assert request.data.action.working_directory == "/tmp"

    assert isinstance(output_item, ToolCallOutputItem)
    assert output_item.agent is agent
    assert output_item.output == "test output"

    raw_item = output_item.raw_item
    assert isinstance(raw_item, dict)
    raw = cast(dict[str, Any], raw_item)
    assert raw["type"] == "local_shell_call_output"
    assert raw["call_id"] == "call_456"
    assert raw["output"] == "test output"


@pytest.mark.asyncio
async def test_runner_executes_local_shell_calls() -> None:
    executor = RecordingLocalShellExecutor(output="shell result")
    tool = LocalShellTool(executor=executor)

    model = FakeModel()
    agent = Agent(name="shell-agent", model=model, tools=[tool])

    action = LocalShellCallAction(
        command=["bash", "-c", "echo shell"],
        env={},
        type="exec",
        timeout_ms=1000,
        working_directory="/tmp",
    )
    local_shell_call = LocalShellCall(
        id="lsh_test",
        action=action,
        call_id="call_local_shell",
        status="completed",
        type="local_shell_call",
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("running shell"), local_shell_call],
            [get_text_message("shell complete")],
        ]
    )

    result = await Runner.run(agent, input="please run shell")

    assert len(executor.calls) == 1
    request = executor.calls[0]
    assert isinstance(request, LocalShellCommandRequest)
    assert request.data is local_shell_call

    items = result.new_items
    assert len(items) == 4

    message_before = items[0]
    assert message_before.type == "message_output_item"
    first_content = message_before.raw_item.content[0]
    assert isinstance(first_content, ResponseOutputText)
    assert first_content.text == "running shell"

    tool_call_item = items[1]
    assert tool_call_item.type == "tool_call_item"
    assert tool_call_item.raw_item is local_shell_call

    local_shell_output = items[2]
    assert isinstance(local_shell_output, ToolCallOutputItem)
    assert local_shell_output.raw_item.get("type") == "local_shell_call_output"
    assert local_shell_output.output == "shell result"

    message_after = items[3]
    assert message_after.type == "message_output_item"
    last_content = message_after.raw_item.content[0]
    assert isinstance(last_content, ResponseOutputText)
    assert last_content.text == "shell complete"

    assert result.final_output == "shell complete"
    assert len(result.raw_responses) == 2
