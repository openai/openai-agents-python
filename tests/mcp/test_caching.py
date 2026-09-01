import asyncio
from unittest.mock import AsyncMock, call, patch

import pytest
from mcp.types import PaginatedRequestParams

from agents import Agent
from agents.mcp import MCPServerStdio
from agents.run_context import RunContextWrapper

from .helpers import DummyStreamsContextManager, tee
from .model_compat import ListToolsResult, Tool as MCPTool


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_server_caching_works(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Test that if we turn caching on, the list of tools is cached and not fetched from the server
    on each call to `list_tools()`.
    """
    server = MCPServerStdio(
        params={
            "command": tee,
        },
        cache_tools_list=True,
    )

    tools = [
        MCPTool(name="tool1", inputSchema={}),
        MCPTool(name="tool2", inputSchema={}),
    ]

    mock_list_tools.return_value = ListToolsResult(tools=tools)

    async with server:
        # Create test context and agent
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")

        # Call list_tools() multiple times
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools

        assert mock_list_tools.call_count == 1, "list_tools() should have been called once"

        # Call list_tools() again, should return the cached value
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools

        assert mock_list_tools.call_count == 1, "list_tools() should not have been called again"

        # Invalidate the cache and call list_tools() again
        server.invalidate_tools_cache()
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools

        assert mock_list_tools.call_count == 2, "list_tools() should be called again"

        # Without invalidating the cache, calling list_tools() again should return the cached value
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_cache_invalidation_during_refresh_is_preserved(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    request_count = 0

    async def list_tools():
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return ListToolsResult(
                tools=[MCPTool(name="initial", inputSchema={})],
            )
        if request_count == 2:
            refresh_started.set()
            await release_refresh.wait()
            return ListToolsResult(
                tools=[
                    MCPTool(
                        name="before-second-invalidation",
                        inputSchema={},
                    ),
                ],
            )
        return ListToolsResult(
            tools=[
                MCPTool(
                    name="after-second-invalidation",
                    inputSchema={},
                ),
            ],
        )

    mock_list_tools.side_effect = list_tools
    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
    )

    async with server:
        initial = await server.list_tools()
        assert [tool.name for tool in initial] == ["initial"]

        server.invalidate_tools_cache()
        refresh_task = asyncio.create_task(server.list_tools())
        try:
            await asyncio.wait_for(refresh_started.wait(), timeout=1)

            server.invalidate_tools_cache()
            release_refresh.set()
            refreshed = await asyncio.wait_for(refresh_task, timeout=1)
        finally:
            release_refresh.set()
            if not refresh_task.done():
                refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)

        assert [tool.name for tool in refreshed] == [
            "before-second-invalidation",
        ]
        assert [tool.name for tool in (server.cached_tools or [])] == [
            "initial",
        ]

        latest = await server.list_tools()
        assert [tool.name for tool in latest] == [
            "after-second-invalidation",
        ]
        assert [tool.name for tool in (server.cached_tools or [])] == [
            "after-second-invalidation",
        ]
        assert request_count == 3


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_paginated_tools_are_cached_before_filtering(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    first_page_tool = MCPTool(name="first_page_tool", inputSchema={})
    second_page_tool = MCPTool(name="second_page_tool", inputSchema={})
    mock_list_tools.side_effect = [
        ListToolsResult(tools=[first_page_tool], nextCursor=""),
        ListToolsResult(tools=[second_page_tool]),
    ]
    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
        tool_filter={"allowed_tool_names": ["second_page_tool"]},
    )

    async with server:
        filtered_tools = await server.list_tools()
        cached_tools = server.cached_tools
        filtered_tools_again = await server.list_tools()

    assert filtered_tools == [second_page_tool]
    assert filtered_tools_again == [second_page_tool]
    assert cached_tools == [first_page_tool, second_page_tool]
    assert mock_list_tools.await_args_list == [
        call(),
        call(params=PaginatedRequestParams(cursor="")),
    ]


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_list_tools_does_not_expose_the_tools_cache(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Mutating the list returned by `list_tools()` must not corrupt the server's cache."""
    server = MCPServerStdio(params={"command": tee}, cache_tools_list=True)
    mock_list_tools.return_value = ListToolsResult(
        tools=[MCPTool(name="tool1", inputSchema={}), MCPTool(name="tool2", inputSchema={})]
    )

    async with server:
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")

        returned = await server.list_tools(run_context, agent)
        assert returned is not server.cached_tools
        returned.pop()

        assert [tool.name for tool in await server.list_tools(run_context, agent)] == [
            "tool1",
            "tool2",
        ]
        assert mock_list_tools.call_count == 1, "the cache should still be serving both tools"


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_list_tools_does_not_expose_the_cache_with_a_no_op_static_filter(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """A static filter that sets neither key passes the cached list straight through."""
    server = MCPServerStdio(params={"command": tee}, cache_tools_list=True, tool_filter={})
    mock_list_tools.return_value = ListToolsResult(
        tools=[MCPTool(name="tool1", inputSchema={}), MCPTool(name="tool2", inputSchema={})]
    )

    async with server:
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")

        returned = await server.list_tools(run_context, agent)
        returned.clear()

        assert [tool.name for tool in await server.list_tools(run_context, agent)] == [
            "tool1",
            "tool2",
        ]
        assert mock_list_tools.call_count == 1


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_cached_tools_returns_a_snapshot(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """`cached_tools` must not hand out the live cache: mutating it must not leak into listings."""
    server = MCPServerStdio(params={"command": tee}, cache_tools_list=True)
    mock_list_tools.return_value = ListToolsResult(
        tools=[
            MCPTool(
                name="tool1",
                inputSchema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            ),
            MCPTool(name="tool2", inputSchema={}),
        ]
    )

    async with server:
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")
        await server.list_tools(run_context, agent)

        snapshot = server.cached_tools
        assert snapshot is not None
        snapshot.append(MCPTool(name="injected", inputSchema={}))
        snapshot[0].description = "mutated"
        snapshot[0].input_schema["required"] = []

        later_cached = server.cached_tools
        later_listed = await server.list_tools(run_context, agent)
        assert [tool.name for tool in (later_cached or [])] == ["tool1", "tool2"]
        assert [tool.name for tool in later_listed] == ["tool1", "tool2"]
        assert (later_cached or [])[0].description is None
        assert later_listed[0].description is None
        assert (later_cached or [])[0].input_schema.get("required") == ["q"]
        assert later_listed[0].input_schema.get("required") == ["q"]


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_list_tools_snapshots_tool_objects(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Mutating a returned tool must not corrupt the cached tool or its schema."""
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    server = MCPServerStdio(params={"command": tee}, cache_tools_list=True)
    mock_list_tools.return_value = ListToolsResult(
        tools=[MCPTool(name="tool1", inputSchema=schema)]
    )

    async with server:
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")

        returned = await server.list_tools(run_context, agent)
        cached = server.cached_tools
        assert cached is not None
        assert returned[0] is not cached[0]
        assert returned[0].input_schema is not cached[0].input_schema

        returned[0].input_schema["required"] = []
        returned[0].description = "mutated"

        later = await server.list_tools(run_context, agent)
        assert later[0].description is None
        assert later[0].input_schema.get("required") == ["q"]
        assert (server.cached_tools or [])[0].input_schema.get("required") == ["q"]
        assert mock_list_tools.call_count == 1


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.call_tool", new_callable=AsyncMock)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_list_tools_mutation_cannot_bypass_required_parameter_validation(
    mock_list_tools: AsyncMock,
    mock_call_tool: AsyncMock,
    mock_initialize: AsyncMock,
    mock_stdio_client,
):
    """Clearing required fields on a returned tool must not skip call-time validation."""
    from mcp.types import CallToolResult, TextContent

    from agents.exceptions import UserError

    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    server = MCPServerStdio(params={"command": tee}, cache_tools_list=True)
    mock_list_tools.return_value = ListToolsResult(
        tools=[MCPTool(name="tool1", inputSchema=schema)]
    )
    mock_call_tool.return_value = CallToolResult(content=[TextContent(type="text", text="ok")])

    async with server:
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")
        returned = await server.list_tools(run_context, agent)
        returned[0].input_schema["required"] = []

        with pytest.raises(UserError, match="missing required parameters: q"):
            await server.call_tool("tool1", {})
        assert mock_call_tool.call_count == 0


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.call_tool", new_callable=AsyncMock)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_dynamic_filter_mutation_cannot_corrupt_cached_tool_schemas(
    mock_list_tools: AsyncMock,
    mock_call_tool: AsyncMock,
    mock_initialize: AsyncMock,
    mock_stdio_client,
):
    """A callable filter that mutates nested schemas must not affect later listings or calls."""
    from mcp.types import CallToolResult, TextContent

    from agents.exceptions import UserError

    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }

    def mutating_filter(_context, tool: MCPTool) -> bool:
        tool.input_schema["required"] = []
        tool.description = "mutated"
        return True

    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
        tool_filter=mutating_filter,
    )
    mock_list_tools.return_value = ListToolsResult(
        tools=[MCPTool(name="tool1", inputSchema=schema)]
    )
    mock_call_tool.return_value = CallToolResult(content=[TextContent(type="text", text="ok")])

    async with server:
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")
        first = await server.list_tools(run_context, agent)
        later = await server.list_tools(run_context, agent)
        cached = server.cached_tools

        assert first[0].input_schema.get("required") == ["q"]
        assert later[0].input_schema.get("required") == ["q"]
        assert cached is not None
        assert cached[0].input_schema.get("required") == ["q"]
        assert first[0].description is None
        assert later[0].description is None
        assert cached[0].description is None

        with pytest.raises(UserError, match="missing required parameters: q"):
            await server.call_tool("tool1", {})
        assert mock_call_tool.call_count == 0


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_cached_tools_is_none_before_the_first_list(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """The snapshot must preserve the `None` sentinel rather than reporting an empty cache."""
    server = MCPServerStdio(params={"command": tee}, cache_tools_list=True)
    mock_list_tools.return_value = ListToolsResult(tools=[])

    async with server:
        assert server.cached_tools is None
