"""Tests for local shell tool execution.

These confirm that LocalShellAction.execute forwards the command to the executor
and that Runner.run executes local shell calls and records their outputs.
"""

import json
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.responses import ResponseOutputText
from openai.types.responses.response_input_param import LocalShellCallOutput
from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction

from agents import (
    Agent,
    LocalShellCommandRequest,
    LocalShellTool,
    OpenAIResponsesModel,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    UserError,
)
from agents.items import ToolCallOutputItem
from agents.run_internal.run_loop import LocalShellAction, ToolRunLocalShellCall
from agents.run_state import RunState

from .fake_model import FakeModel, get_response_obj
from .test_responses import get_text_message


class RecordingLocalShellExecutor:
    """A `LocalShellTool` executor that records the requests it receives."""

    def __init__(self, output: str = "shell output") -> None:
        self.output = output
        self.calls: list[LocalShellCommandRequest] = []

    def __call__(self, request: LocalShellCommandRequest) -> str:
        self.calls.append(request)
        return self.output


async def _create_serialized_local_shell_state() -> tuple[LocalShellTool, dict[str, Any]]:
    tool = LocalShellTool(executor=RecordingLocalShellExecutor(output="shell result"))
    initial_model = FakeModel()
    initial_agent = Agent(name="shell-agent", model=initial_model, tools=[tool])
    local_shell_call = LocalShellCall(
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
    initial_model.add_multiple_turn_outputs(
        [
            [get_text_message("running shell"), local_shell_call],
            [get_text_message("shell complete")],
        ]
    )
    result = await Runner.run(initial_agent, input="please run shell")
    return tool, json.loads(json.dumps(result.to_state().to_json()))


def _create_recording_responses_model() -> tuple[
    OpenAIResponsesModel, list[httpx.Request], httpx.AsyncClient
]:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=get_response_obj([get_text_message("resumed")]).model_dump_json(),
            headers={"content-type": "application/json"},
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=http_client,
    )
    return (
        OpenAIResponsesModel(model="codex-mini-latest", openai_client=client),
        requests,
        http_client,
    )


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


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema_version",
    [None, "1.13"],
    ids=["current", "v0.19.4"],
)
async def test_local_shell_output_survives_run_state_resume(schema_version: str | None) -> None:
    tool, serialized = await _create_serialized_local_shell_state()
    if schema_version is not None:
        serialized["$schemaVersion"] = schema_version

    resumed_model, requests, http_client = _create_recording_responses_model()
    try:
        resumed_agent = Agent(name="shell-agent", model=resumed_model, tools=[tool])
        resumed_state = await RunState.from_json(resumed_agent, serialized)

        shell_outputs = [
            item.raw_item
            for item in resumed_state._generated_items
            if isinstance(item, ToolCallOutputItem)
            and isinstance(item.raw_item, dict)
            and item.raw_item.get("type") == "local_shell_call_output"
        ]
        assert shell_outputs == [
            {
                "type": "local_shell_call_output",
                "call_id": "call_local_shell",
                "output": "shell result",
            }
        ]

        await Runner.run(resumed_agent, resumed_state)
    finally:
        await http_client.aclose()

    assert len(requests) == 1
    request_body = json.loads(requests[0].content)
    replayed = [item for item in request_body["input"] if isinstance(item, dict)]
    replayed_call = next(item for item in replayed if item.get("type") == "local_shell_call")
    replayed_output = next(
        item for item in replayed if item.get("type") == "local_shell_call_output"
    )
    assert replayed_call["call_id"] == replayed_output["call_id"] == "call_local_shell"
    assert "id" not in replayed_output


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema_version",
    [None, "1.13"],
    ids=["current", "v0.19.4"],
)
async def test_run_state_rejects_id_only_local_shell_output(schema_version: str | None) -> None:
    tool, serialized = await _create_serialized_local_shell_state()
    invalid_output = {
        "type": "local_shell_call_output",
        "id": "legacy-only",
        "output": "shell result",
    }
    for item_group in ("generated_items", "session_items"):
        output_items = [
            item
            for item in serialized[item_group]
            if item.get("raw_item", {}).get("type") == "local_shell_call_output"
        ]
        assert len(output_items) == 1
        output_items[0]["raw_item"] = invalid_output.copy()
    if schema_version is not None:
        serialized["$schemaVersion"] = schema_version

    resumed_model, requests, http_client = _create_recording_responses_model()
    resumed_agent = Agent(name="shell-agent", model=resumed_model, tools=[tool])
    try:
        if schema_version is None:
            with pytest.raises(
                UserError,
                match="completed tool invocation does not match a restored tool call and output",
            ) as exc_info:
                await RunState.from_json(resumed_agent, serialized)
            assert "call_local_shell" not in str(exc_info.value)
        else:
            resumed_state = await RunState.from_json(resumed_agent, serialized)
            await Runner.run(resumed_agent, resumed_state)
    finally:
        await http_client.aclose()

    if schema_version is None:
        assert requests == []
    else:
        assert len(requests) == 1
        request_body = json.loads(requests[0].content)
        replayed_types = {
            item.get("type") for item in request_body["input"] if isinstance(item, dict)
        }
        assert "local_shell_call" not in replayed_types
        assert "local_shell_call_output" not in replayed_types


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema_version",
    [None, "1.13"],
    ids=["current", "v0.19.4"],
)
async def test_run_state_preserves_official_local_shell_original_input(
    schema_version: str | None,
) -> None:
    original_input: LocalShellCallOutput = {
        "type": "local_shell_call_output",
        "id": "lsh_output_123",
        "output": "shell result",
    }
    model = FakeModel()
    model.add_multiple_turn_outputs([[get_text_message("complete")]])
    agent = Agent(name="shell-agent", model=model)
    result = await Runner.run(agent, input=[original_input])
    serialized = json.loads(json.dumps(result.to_state().to_json()))
    if schema_version is not None:
        serialized["$schemaVersion"] = schema_version

    restored_state = await RunState.from_json(agent, serialized)
    assert restored_state.to_json()["original_input"] == [original_input]
