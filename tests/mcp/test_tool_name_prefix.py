"""Tests for the per-server ``tool_name_prefix`` option.

The prefix lets users disambiguate tools that share a name across MCP servers (for example,
``create_issue`` on both a GitHub and a Linear MCP server) without renaming the underlying
tools on the upstream servers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import ListToolsResult, Tool as MCPTool

from agents import Agent, FunctionTool
from agents.exceptions import UserError
from agents.mcp import MCPServerStdio, MCPUtil
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext

from .helpers import DummyStreamsContextManager, FakeMCPServer, tee


def _agent() -> Agent:
    return Agent(name="test_agent", instructions="Test agent")


def _ctx() -> RunContextWrapper:
    return RunContextWrapper(context=None)


@pytest.mark.asyncio
async def test_two_servers_with_different_prefixes_avoid_collision():
    """Two servers exposing the same tool name can co-exist when each picks a distinct prefix."""
    github = FakeMCPServer(server_name="github", tool_name_prefix="gh")
    github.add_tool("create_issue", {})
    github.add_tool("list_issues", {})

    linear = FakeMCPServer(server_name="linear", tool_name_prefix="ln")
    linear.add_tool("create_issue", {})
    linear.add_tool("update_issue", {})

    tools = await MCPUtil.get_all_function_tools([github, linear], False, _ctx(), _agent())

    tool_names = [tool.name for tool in tools]
    assert tool_names == [
        "gh_create_issue",
        "gh_list_issues",
        "ln_create_issue",
        "ln_update_issue",
    ]


@pytest.mark.asyncio
async def test_call_tool_strips_prefix_before_dispatching_upstream():
    """`call_tool` must hand the original (unprefixed) name to the upstream server."""
    github = FakeMCPServer(server_name="github", tool_name_prefix="gh")
    github.add_tool("create_issue", {})

    linear = FakeMCPServer(server_name="linear", tool_name_prefix="ln")
    linear.add_tool("create_issue", {})

    tools = await MCPUtil.get_all_function_tools([github, linear], False, _ctx(), _agent())

    gh_tool, ln_tool = tools
    assert isinstance(gh_tool, FunctionTool)
    assert isinstance(ln_tool, FunctionTool)
    assert gh_tool.name == "gh_create_issue"
    assert ln_tool.name == "ln_create_issue"

    await gh_tool.on_invoke_tool(
        ToolContext(
            context=None,
            tool_name=gh_tool.name,
            tool_call_id="call_gh",
            tool_arguments="{}",
        ),
        "{}",
    )
    await ln_tool.on_invoke_tool(
        ToolContext(
            context=None,
            tool_name=ln_tool.name,
            tool_call_id="call_ln",
            tool_arguments="{}",
        ),
        "{}",
    )

    # The fake servers record the upstream tool name that they were asked to invoke. Each
    # server should only see calls to the original (unprefixed) name on its own side.
    assert github.tool_calls == ["create_issue"]
    assert linear.tool_calls == ["create_issue"]


@pytest.mark.asyncio
async def test_prefix_exceeding_length_limit_raises_user_error():
    """`list_tools()` rejects prefixes that would push a tool name past the 64-char limit."""
    long_prefix = "p" * 60
    server = FakeMCPServer(server_name="long", tool_name_prefix=long_prefix)
    server.add_tool("create_issue", {})  # 60 + 1 + 12 = 73 chars after prefixing.

    with pytest.raises(UserError) as exc_info:
        await server.list_tools(_ctx(), _agent())

    assert "exceeds the 64-character limit" in str(exc_info.value)
    assert "tool_name_prefix=" in str(exc_info.value)


@pytest.mark.asyncio
async def test_prefix_composes_with_tool_filter():
    """The static allow/block list must continue to match the upstream tool name."""
    server = FakeMCPServer(
        server_name="github",
        tool_filter={"allowed_tool_names": ["create_issue"]},
        tool_name_prefix="gh",
    )
    server.add_tool("create_issue", {})
    server.add_tool("list_issues", {})

    tools = await server.list_tools(_ctx(), _agent())
    assert [tool.name for tool in tools] == ["gh_create_issue"]


@pytest.mark.asyncio
async def test_prefix_does_not_mutate_cached_upstream_tools():
    """The internal cache must keep the original upstream names so retries hit the right tool."""
    server = FakeMCPServer(server_name="github", tool_name_prefix="gh")
    server.add_tool("create_issue", {})

    # The first listing applies the prefix to a copy; the source list keeps the original name.
    tools = await server.list_tools(_ctx(), _agent())
    assert [tool.name for tool in tools] == ["gh_create_issue"]
    assert [tool.name for tool in server.tools] == ["create_issue"]


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
@patch("mcp.client.session.ClientSession.call_tool", new_callable=AsyncMock)
async def test_stdio_server_applies_prefix_and_strips_on_call(
    mock_call_tool: AsyncMock,
    mock_list_tools: AsyncMock,
    mock_initialize: AsyncMock,
    mock_stdio_client,
):
    """End-to-end check on the real MCPServerStdio: prefix in `list_tools`, strip in `call_tool`."""
    mock_list_tools.return_value = ListToolsResult(
        tools=[MCPTool(name="create_issue", inputSchema={})]
    )
    mock_call_tool.return_value = None

    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
        tool_name_prefix="gh",
    )

    async with server:
        tools = await server.list_tools(_ctx(), _agent())
        assert [tool.name for tool in tools] == ["gh_create_issue"]

        # Caller passes the prefixed name; upstream sees the original.
        await server.call_tool("gh_create_issue", {"title": "Bug"})
        assert mock_call_tool.call_args.args[0] == "create_issue"

        # If a caller still passes the upstream name directly, the strip is a no-op.
        await server.call_tool("create_issue", {"title": "Bug"})
        assert mock_call_tool.call_args.args[0] == "create_issue"

        # Caching keeps upstream names so re-listing returns the same prefixed view.
        tools = await server.list_tools(_ctx(), _agent())
        assert [tool.name for tool in tools] == ["gh_create_issue"]
        assert mock_list_tools.call_count == 1
