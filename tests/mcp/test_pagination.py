from unittest.mock import AsyncMock, MagicMock, call

import pytest
from mcp.types import ListPromptsResult, ListToolsResult, Prompt, Tool

from agents.mcp.server import MCPServerStreamableHttp


@pytest.fixture
def server():
    return MCPServerStreamableHttp({"url": "http://localhost"})


def _tool(name: str) -> Tool:
    return Tool(name=name, description="", inputSchema={"type": "object", "properties": {}})


@pytest.mark.asyncio
async def test_list_tools_accumulates_pages(server: MCPServerStreamableHttp):
    mock_session = MagicMock()
    mock_session.list_tools = AsyncMock(
        side_effect=[
            ListToolsResult(tools=[_tool("tool_1")], nextCursor="page_2"),
            ListToolsResult(tools=[_tool("tool_2")]),
        ]
    )
    server.session = mock_session

    result = await server.list_tools()

    assert [tool.name for tool in result] == ["tool_1", "tool_2"]
    assert mock_session.list_tools.await_args_list == [
        call(cursor=None),
        call(cursor="page_2"),
    ]


@pytest.mark.asyncio
async def test_list_tools_does_not_append_repeated_cursor_page(server: MCPServerStreamableHttp):
    mock_session = MagicMock()
    mock_session.list_tools = AsyncMock(
        side_effect=[
            ListToolsResult(tools=[_tool("tool_1")], nextCursor="tok_loop"),
            ListToolsResult(tools=[_tool("duplicate")], nextCursor="tok_loop"),
        ]
    )
    server.session = mock_session

    result = await server.list_tools()

    assert [tool.name for tool in result] == ["tool_1"]
    assert mock_session.list_tools.await_count == 2


@pytest.mark.asyncio
async def test_list_prompts_accumulates_pages_and_merges_metadata(
    server: MCPServerStreamableHttp,
):
    mock_session = MagicMock()
    mock_session.list_prompts = AsyncMock(
        side_effect=[
            ListPromptsResult(
                prompts=[Prompt(name="prompt_1", description="")],
                nextCursor="page_2",
                _meta={"page_1": True, "shared": "first"},
            ),
            ListPromptsResult(
                prompts=[Prompt(name="prompt_2", description="")],
                _meta={"page_2": True, "shared": "second"},
            ),
        ]
    )
    server.session = mock_session

    result = await server.list_prompts()

    assert [prompt.name for prompt in result.prompts] == ["prompt_1", "prompt_2"]
    assert result.nextCursor is None
    assert result.meta == {"page_1": True, "page_2": True, "shared": "second"}
    assert mock_session.list_prompts.await_args_list == [
        call(cursor=None),
        call(cursor="page_2"),
    ]


@pytest.mark.asyncio
async def test_list_prompts_does_not_append_repeated_cursor_page(
    server: MCPServerStreamableHttp,
):
    mock_session = MagicMock()
    mock_session.list_prompts = AsyncMock(
        side_effect=[
            ListPromptsResult(
                prompts=[Prompt(name="prompt_1", description="")],
                nextCursor="tok_loop",
                _meta={"page": 1},
            ),
            ListPromptsResult(
                prompts=[Prompt(name="duplicate", description="")],
                nextCursor="tok_loop",
                _meta={"page": 2},
            ),
        ]
    )
    server.session = mock_session

    result = await server.list_prompts()

    assert [prompt.name for prompt in result.prompts] == ["prompt_1"]
    assert result.meta == {"page": 1}
    assert mock_session.list_prompts.await_count == 2


@pytest.mark.asyncio
async def test_list_prompts_returns_none_metadata_when_pages_have_none(
    server: MCPServerStreamableHttp,
):
    mock_session = MagicMock()
    mock_session.list_prompts = AsyncMock(
        return_value=ListPromptsResult(prompts=[Prompt(name="prompt", description="")])
    )
    server.session = mock_session

    result = await server.list_prompts()

    assert result.meta is None


@pytest.mark.asyncio
async def test_list_tools_supports_empty_string_next_cursor(server: MCPServerStreamableHttp):
    mock_session = MagicMock()
    mock_session.list_tools = AsyncMock(
        side_effect=[
            ListToolsResult(tools=[_tool("tool_1")], nextCursor=""),
            ListToolsResult(tools=[_tool("tool_2")]),
        ]
    )
    server.session = mock_session

    result = await server.list_tools()

    assert [tool.name for tool in result] == ["tool_1", "tool_2"]
    assert mock_session.list_tools.await_args_list == [
        call(cursor=None),
        call(cursor=""),
    ]
