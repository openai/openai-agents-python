import json
from typing import Any

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    ModelBehaviorError,
    RunContextWrapper,
    Runner,
    UserError,
    default_tool_error_function,
    handoff,
)
from agents.exceptions import AgentsException

from ..fake_model import FakeModel
from ..test_responses import get_function_tool_call, get_text_message
from .helpers import FakeMCPServer


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_calls_mcp_tool(streaming: bool):
    """Test that the runner calls an MCP tool when the model produces a tool call."""
    server = FakeMCPServer()
    server.add_tool("test_tool_1", {})
    server.add_tool("test_tool_2", {})
    server.add_tool("test_tool_3", {})
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_tool_2", "")],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    if streaming:
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, input="user_message")

    assert server.tool_calls == ["test_tool_2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_asserts_when_mcp_tool_not_found(streaming: bool):
    """Test that the runner asserts when an MCP tool is not found."""
    server = FakeMCPServer()
    server.add_tool("test_tool_1", {})
    server.add_tool("test_tool_2", {})
    server.add_tool("test_tool_3", {})
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_tool_doesnt_exist", "")],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    with pytest.raises(ModelBehaviorError):
        if streaming:
            result = Runner.run_streamed(agent, input="user_message")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, input="user_message")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_works_with_multiple_mcp_servers(streaming: bool):
    """Test that the runner works with multiple MCP servers."""
    server1 = FakeMCPServer()
    server1.add_tool("test_tool_1", {})

    server2 = FakeMCPServer()
    server2.add_tool("test_tool_2", {})
    server2.add_tool("test_tool_3", {})

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server1, server2],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_tool_2", "")],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    if streaming:
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, input="user_message")

    assert server1.tool_calls == []
    assert server2.tool_calls == ["test_tool_2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_errors_when_mcp_tools_clash(streaming: bool):
    """Test that the runner errors when multiple servers have the same tool name."""
    server1 = FakeMCPServer()
    server1.add_tool("test_tool_1", {})
    server1.add_tool("test_tool_2", {})

    server2 = FakeMCPServer()
    server2.add_tool("test_tool_2", {})
    server2.add_tool("test_tool_3", {})

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server1, server2],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_tool_3", "")],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    with pytest.raises(UserError):
        if streaming:
            result = Runner.run_streamed(agent, input="user_message")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, input="user_message")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_can_call_server_prefixed_mcp_tool_names(streaming: bool):
    server1 = FakeMCPServer(server_name="docs")
    server1.add_tool("search", {})

    server2 = FakeMCPServer(server_name="calendar")
    server2.add_tool("search", {})

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server1, server2],
        mcp_config={"include_server_in_tool_names": True},
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("a_message"), get_function_tool_call("mcp_calendar__search", "")],
            [get_text_message("done")],
        ]
    )

    if streaming:
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, input="user_message")

    assert server1.tool_calls == []
    assert server2.tool_calls == ["search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_prefixed_mcp_tool_names_do_not_collide_with_agent_tools(streaming: bool):
    server1 = FakeMCPServer(server_name="docs")
    server1.add_tool("search", {})

    server2 = FakeMCPServer(server_name="calendar")
    server2.add_tool("search", {})

    local_tool_calls: list[str] = []

    async def invoke_local_tool(context: Any, input_json: str) -> str:
        local_tool_calls.append(input_json)
        return "local"

    local_tool = FunctionTool(
        name="mcp_calendar__search",
        description="Local tool that intentionally collides with the natural MCP prefix.",
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=invoke_local_tool,
    )

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[local_tool],
        mcp_servers=[server1, server2],
        mcp_config={"include_server_in_tool_names": True},
    )

    mcp_tools = await agent.get_mcp_tools(RunContextWrapper(context=None))
    calendar_search_tool_name = next(
        tool.name
        for tool in mcp_tools
        if getattr(getattr(tool, "_tool_origin", None), "mcp_server_name", None) == "calendar"
    )
    assert calendar_search_tool_name != "mcp_calendar__search"
    assert calendar_search_tool_name.startswith("mcp_calendar__search_")

    model.add_multiple_turn_outputs(
        [
            [get_text_message("a_message"), get_function_tool_call(calendar_search_tool_name, "")],
            [get_text_message("done")],
        ]
    )

    if streaming:
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, input="user_message")

    assert local_tool_calls == []
    assert server1.tool_calls == []
    assert server2.tool_calls == ["search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_prefixed_mcp_tool_names_do_not_collide_with_handoffs(streaming: bool):
    server = FakeMCPServer(server_name="calendar")
    server.add_tool("search", {})

    target_model = FakeModel()
    target_agent = Agent(name="calendar_agent", model=target_model)
    target_model.add_multiple_turn_outputs([[get_text_message("handoff target")]])

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        handoffs=[handoff(target_agent, tool_name_override="mcp_calendar__search")],
        mcp_servers=[server],
        mcp_config={"include_server_in_tool_names": True},
    )

    mcp_tools = await agent.get_mcp_tools(RunContextWrapper(context=None))
    assert len(mcp_tools) == 1
    calendar_search_tool_name = mcp_tools[0].name
    assert calendar_search_tool_name != "mcp_calendar__search"
    assert calendar_search_tool_name.startswith("mcp_calendar__search_")

    model.add_multiple_turn_outputs(
        [
            [get_text_message("a_message"), get_function_tool_call(calendar_search_tool_name, "")],
            [get_text_message("done")],
        ]
    )

    if streaming:
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, input="user_message")

    assert server.tool_calls == ["search"]
    assert target_model.first_turn_args is None


class Foo(BaseModel):
    bar: str
    baz: int


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_calls_mcp_tool_with_args(streaming: bool):
    """Test that the runner calls an MCP tool when the model produces a tool call."""
    server = FakeMCPServer()
    await server.connect()
    server.add_tool("test_tool_1", {})
    server.add_tool("test_tool_2", Foo.model_json_schema())
    server.add_tool("test_tool_3", {})
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server],
    )

    json_args = json.dumps(Foo(bar="baz", baz=1).model_dump())

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_tool_2", json_args)],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    if streaming:
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, input="user_message")

    assert server.tool_calls == ["test_tool_2"]
    assert server.tool_results == [f"result_test_tool_2_{json_args}"]

    await server.cleanup()


class CrashingFakeMCPServer(FakeMCPServer):
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object] | None,
        meta: dict[str, object] | None = None,
    ):
        raise Exception("Crash!")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_runner_emits_mcp_error_tool_call_output_item(streaming: bool):
    """Runner should emit tool_call_output_item with failure output when MCP tool raises."""
    server = CrashingFakeMCPServer()
    server.add_tool("crashing_tool", {})

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("a_message"), get_function_tool_call("crashing_tool", "{}")],
            [get_text_message("done")],
        ]
    )

    if streaming:
        streamed_result = Runner.run_streamed(agent, input="user_message")
        async for _ in streamed_result.stream_events():
            pass
        tool_output_items = [
            item for item in streamed_result.new_items if item.type == "tool_call_output_item"
        ]
        assert streamed_result.final_output == "done"
    else:
        non_streamed_result = await Runner.run(agent, input="user_message")
        tool_output_items = [
            item for item in non_streamed_result.new_items if item.type == "tool_call_output_item"
        ]
        assert non_streamed_result.final_output == "done"

    assert tool_output_items, "Expected tool_call_output_item for MCP failure"
    wrapped_error = AgentsException(
        "Error invoking MCP tool crashing_tool on server 'fake_mcp_server': Crash!"
    )
    expected_error_message = default_tool_error_function(
        RunContextWrapper(context=None),
        wrapped_error,
    )
    assert tool_output_items[0].output == expected_error_message
