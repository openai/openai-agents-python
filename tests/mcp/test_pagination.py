from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import (
    AnyUrl,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    Prompt,
    Resource,
    ResourceTemplate,
    Tool,
)

from agents.mcp.server import MCPServerStreamableHttp


@pytest.fixture
def server():
    return MCPServerStreamableHttp({"url": "http://localhost"})


@pytest.mark.asyncio
async def test_list_tools_pagination_and_loop_guard(server: MCPServerStreamableHttp):
    mock_session = MagicMock()
    page1 = ListToolsResult(
        tools=[
            Tool(name="tool_1", description="", inputSchema={"type": "object", "properties": {}})
        ],
        nextCursor="tok_loop",
    )
    # The session repeatedly returns the same cursor
    mock_session.list_tools = AsyncMock(return_value=page1)
    server.session = mock_session

    result = await server.list_tools()

    # The result should contain the tool twice because the loop breaks on the second identical fetch
    assert len(result) == 2
    assert result[0].name == "tool_1"


@pytest.mark.asyncio
async def test_list_prompts_pagination_and_loop_guard(server: MCPServerStreamableHttp):
    mock_session = MagicMock()
    page1 = ListPromptsResult(
        prompts=[Prompt(name="prompt_1", description="")], nextCursor="tok_loop"
    )
    mock_session.list_prompts = AsyncMock(return_value=page1)
    server.session = mock_session

    result = await server.list_prompts()

    assert len(result.prompts) == 2
    assert result.prompts[0].name == "prompt_1"


@pytest.mark.asyncio
async def test_list_resources_pagination_and_loop_guard(server: MCPServerStreamableHttp):
    mock_session = MagicMock()
    page1 = ListResourcesResult(
        resources=[Resource(uri=AnyUrl("file:///1"), name="res_1", mimeType="text/plain")],
        nextCursor="tok_loop",
    )
    mock_session.list_resources = AsyncMock(return_value=page1)
    server.session = mock_session

    result = await server.list_resources()

    assert len(result.resources) == 2
    assert result.resources[0].name == "res_1"


@pytest.mark.asyncio
async def test_list_resource_templates_pagination_and_loop_guard(server: MCPServerStreamableHttp):
    mock_session = MagicMock()
    page1 = ListResourceTemplatesResult(
        resourceTemplates=[ResourceTemplate(uriTemplate="file:///{path}", name="temp_1")],
        nextCursor="tok_loop",
    )
    mock_session.list_resource_templates = AsyncMock(return_value=page1)
    server.session = mock_session

    result = await server.list_resource_templates()

    assert len(result.resourceTemplates) == 2
    assert result.resourceTemplates[0].name == "temp_1"
