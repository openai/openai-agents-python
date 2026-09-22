from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import PaginatedRequestParams, Prompt

from agents import Agent, Runner, UserError
from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from agents.testing import ScriptedModel

from .model_compat import ListPromptsResult, ListToolsResult, Tool


def make_server(transport: str, **kwargs: Any):
    if transport == "stdio":
        return MCPServerStdio(params={"command": "unused"}, **kwargs)
    if transport == "sse":
        return MCPServerSse(params={"url": "https://example.com/mcp"}, **kwargs)
    return MCPServerStreamableHttp(params={"url": "https://example.com/mcp"}, **kwargs)


def list_page(method: str, index: int, *, last: bool = False, empty: bool = False):
    cursor = None if last else f"private-cursor-{index}"
    if method == "list_tools":
        return ListToolsResult(
            tools=[] if empty else [Tool(name=f"tool_{index}", inputSchema={})],
            nextCursor=cursor,
        )
    return ListPromptsResult(
        prompts=[] if empty else [Prompt(name=f"prompt_{index}")],
        nextCursor=cursor,
        _meta={"page": index},
    )


@pytest.mark.parametrize("transport", ["stdio", "sse", "http"])
@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_page_limit_rejected_before_connect(transport: str, limit: int):
    with pytest.raises(ValueError, match="max_list_pages must be a positive integer or None"):
        make_server(transport, max_list_pages=limit)


@pytest.mark.parametrize("limit", [True, 1.5])
def test_page_limit_requires_an_integer(limit: Any):
    with pytest.raises(TypeError, match="max_list_pages must be a positive integer or None"):
        make_server("stdio", max_list_pages=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "sse", "http"])
@pytest.mark.parametrize("method", ["list_tools", "list_prompts"])
@pytest.mark.parametrize("empty", [False, True])
async def test_fresh_cursors_stop_at_page_limit(transport: str, method: str, empty: bool):
    server = make_server(
        transport, max_list_pages=2, max_retry_attempts=2, retry_backoff_seconds_base=0
    )
    session = MagicMock()
    request = AsyncMock(side_effect=[list_page(method, i, empty=empty) for i in range(4)])
    setattr(session, method, request)
    server.session = session

    with pytest.raises(UserError, match="exceeded max_list_pages"):
        await getattr(server, method)()

    assert request.await_count == 2
    assert server.cached_tools is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list_tools", "list_prompts"])
@pytest.mark.parametrize("limit", [1, 2, None])
async def test_complete_listing_at_page_boundary(method: str, limit: int | None):
    server = make_server("stdio", max_list_pages=limit, cache_tools_list=True)
    page_count = limit if limit is not None else 4
    session = MagicMock()
    request = AsyncMock(
        side_effect=[list_page(method, i, last=i == page_count - 1) for i in range(page_count)]
    )
    setattr(session, method, request)
    server.session = session

    result = await getattr(server, method)()

    prefix = "tool" if method == "list_tools" else "prompt"
    entries = result if method == "list_tools" else result.prompts
    assert [entry.name for entry in entries] == [f"{prefix}_{i}" for i in range(page_count)]
    assert request.await_count == page_count
    if method == "list_tools":
        assert await server.list_tools() == result
        assert request.await_count == page_count
    else:
        assert result.meta == {"page": 0}


@pytest.mark.asyncio
async def test_failed_refresh_does_not_replace_cache_and_next_call_starts_fresh():
    server = make_server("http", max_list_pages=1, cache_tools_list=True)
    session = MagicMock()
    session.list_tools = AsyncMock(
        side_effect=[
            list_page("list_tools", 0, last=True),
            list_page("list_tools", 1),
            list_page("list_tools", 2, last=True),
        ]
    )
    server.session = session
    original = await server.list_tools()
    server.invalidate_tools_cache()

    with pytest.raises(UserError, match="exceeded max_list_pages"):
        await server.list_tools()

    assert server.cached_tools == original
    refreshed = await server.list_tools()
    assert [tool.name for tool in refreshed] == ["tool_2"]
    assert await server.list_tools() == refreshed
    assert session.list_tools.await_count == 3
    assert all(not call.kwargs for call in session.list_tools.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_successful_page_budget_survives_failed_page_retry(complete: bool):
    server = make_server(
        "stdio", max_list_pages=2, max_retry_attempts=2, retry_backoff_seconds_base=0
    )
    session = MagicMock()
    session.list_tools = AsyncMock(
        side_effect=[
            list_page("list_tools", 0),
            RuntimeError("transient failure"),
            list_page("list_tools", 1, last=complete),
            list_page("list_tools", 2, last=True),
        ]
    )
    server.session = session

    if complete:
        assert [tool.name for tool in await server.list_tools()] == ["tool_0", "tool_1"]
    else:
        with pytest.raises(UserError, match="exceeded max_list_pages"):
            await server.list_tools()
        assert server.cached_tools is None

    assert session.list_tools.await_count == 3
    assert session.list_tools.await_args_list[1].kwargs == {
        "params": PaginatedRequestParams(cursor="private-cursor-0")
    }
    assert session.list_tools.await_args_list[2] == session.list_tools.await_args_list[1]


@pytest.mark.asyncio
async def test_runner_stops_during_tool_enumeration_before_model_execution():
    server = make_server("stdio", max_list_pages=1)
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=list_page("list_tools", 0))
    server.session = session
    model = ScriptedModel()
    model.get_response = AsyncMock()

    with pytest.raises(UserError, match="exceeded max_list_pages"):
        await Runner.run(Agent(name="test", model=model, mcp_servers=[server]), "hello")

    model.get_response.assert_not_awaited()
    assert session.list_tools.await_count == 1
    assert server.cached_tools is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list_tools", "list_prompts"])
async def test_cancellation_with_page_budget_preserves_cancellation(method: str):
    server = make_server("http", max_list_pages=2)
    session = MagicMock()
    request = AsyncMock(side_effect=[list_page(method, 0), asyncio.CancelledError()])
    setattr(session, method, request)
    server.session = session

    with pytest.raises(asyncio.CancelledError):
        await getattr(server, method)()

    assert request.await_count == 2
    assert server.cached_tools is None
