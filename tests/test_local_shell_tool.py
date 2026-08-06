"""Tests for local shell tool execution.

These confirm that LocalShellAction.execute forwards the command to the executor
and that Runner.run executes local shell calls and records their outputs.
"""

import json
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
from agents.items import ToolCallOutputItem
from agents.run_internal.run_loop import LocalShellAction, ToolRunLocalShellCall
from agents.run_state import RunState

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
    assert isinstance(local_shell_output.raw_item, dict)
    assert local_shell_output.raw_item.get("type") == "local_shell_call_output"
    assert local_shell_output.output == "shell result"

    message_after = items[3]
    assert message_after.type == "message_output_item"
    last_content = message_after.raw_item.content[0]
    assert isinstance(last_content, ResponseOutputText)
    assert last_content.text == "shell complete"

    assert result.final_output == "shell complete"
    assert len(result.raw_responses) == 2


def _local_shell_call() -> LocalShellCall:
    return LocalShellCall(
        id="lsh_test",
        action=LocalShellCallAction(
            command=["bash", "-c", "echo shell"],
            env={},
            type="exec",
            timeout_ms=1000,
            working_directory="/tmp",
        ),
        call_id="call_local_shell",
        status="completed",
        type="local_shell_call",
    )


async def _run_with_local_shell(agent: Agent[Any], model: FakeModel) -> Any:
    model.add_multiple_turn_outputs(
        [
            [get_text_message("running shell"), _local_shell_call()],
            [get_text_message("shell complete")],
        ]
    )
    return await Runner.run(agent, input="please run shell")


@pytest.mark.asyncio
async def test_local_shell_output_survives_run_state_roundtrip() -> None:
    """A serialized run that used a local shell tool must keep its shell output on resume."""
    executor = RecordingLocalShellExecutor(output="shell result")
    tool = LocalShellTool(executor=executor)
    model = FakeModel()
    agent = Agent(name="shell-agent", model=model, tools=[tool])

    result = await _run_with_local_shell(agent, model)
    state = result.to_state()
    restored = await RunState.from_json(agent, json.loads(json.dumps(state.to_json())))

    shell_outputs = [
        cast(dict[str, Any], item.raw_item)
        for item in restored._generated_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "local_shell_call_output"
    ]
    assert len(shell_outputs) == 1
    # The runner pairs calls with outputs on call_id, so it has to survive the round trip.
    assert shell_outputs[0]["call_id"] == "call_local_shell"
    assert shell_outputs[0]["output"] == "shell result"


@pytest.mark.asyncio
async def test_resumed_local_shell_run_replays_call_and_output() -> None:
    """Resuming keeps the shell call paired with its output instead of pruning both."""
    executor = RecordingLocalShellExecutor(output="shell result")
    tool = LocalShellTool(executor=executor)
    model = FakeModel()
    agent = Agent(name="shell-agent", model=model, tools=[tool])

    result = await _run_with_local_shell(agent, model)
    serialized = json.loads(json.dumps(result.to_state().to_json()))

    resumed_model = FakeModel()
    resumed_agent = Agent(name="shell-agent", model=resumed_model, tools=[tool])
    resumed_state = await RunState.from_json(resumed_agent, serialized)
    resumed_model.add_multiple_turn_outputs([[get_text_message("resumed")]])
    await Runner.run(resumed_agent, resumed_state)

    replayed = [
        entry
        for entry in (resumed_model.last_turn_args.get("input") or [])
        if isinstance(entry, dict)
    ]
    replayed_types = [entry.get("type") for entry in replayed]
    assert "local_shell_call" in replayed_types
    assert "local_shell_call_output" in replayed_types
    call = next(entry for entry in replayed if entry.get("type") == "local_shell_call")
    output = next(entry for entry in replayed if entry.get("type") == "local_shell_call_output")
    assert call["call_id"] == output["call_id"] == "call_local_shell"


@pytest.mark.asyncio
async def test_api_shaped_local_shell_output_still_restores() -> None:
    """A snapshot whose shell output carries the Responses API `id` keeps all of its fields."""
    executor = RecordingLocalShellExecutor(output="shell result")
    tool = LocalShellTool(executor=executor)
    model = FakeModel()
    agent = Agent(name="shell-agent", model=model, tools=[tool])

    result = await _run_with_local_shell(agent, model)
    serialized = json.loads(json.dumps(result.to_state().to_json()))
    for item in serialized["generated_items"]:
        raw_item = item.get("raw_item")
        if isinstance(raw_item, dict) and raw_item.get("type") == "local_shell_call_output":
            raw_item["id"] = raw_item["call_id"]

    restored = await RunState.from_json(agent, serialized)
    shell_outputs = [
        cast(dict[str, Any], item.raw_item)
        for item in restored._generated_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "local_shell_call_output"
    ]
    assert len(shell_outputs) == 1
    assert shell_outputs[0]["id"] == "call_local_shell"
    assert shell_outputs[0]["call_id"] == "call_local_shell"
