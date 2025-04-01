import json

import pytest
from pydantic import BaseModel

from agents import Agent, ModelBehaviorError, Runner, UserError

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
