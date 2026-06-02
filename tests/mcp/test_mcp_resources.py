"""Tests for MCP server list_resources, list_resource_templates, and read_resource."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import (
    ListResourcesResult,
    ListResourceTemplatesResult,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextResourceContents,
)
from pydantic import AnyUrl

from agents.mcp import MCPServerStreamableHttp


@pytest.fixture
def server():
    return MCPServerStreamableHttp(params={"url": "http://localhost:8000/mcp"})


@pytest.mark.asyncio
async def test_list_resources_raises_when_not_connected(server: MCPServerStreamableHttp):
    """list_resources raises UserError when server has not been connected."""
    from agents.exceptions import UserError

    with pytest.raises(UserError, match="Server not initialized"):
        await server.list_resources()


@pytest.mark.asyncio
async def test_list_resource_templates_raises_when_not_connected(server: MCPServerStreamableHttp):
    """list_resource_templates raises UserError when server has not been connected."""
    from agents.exceptions import UserError

    with pytest.raises(UserError, match="Server not initialized"):
        await server.list_resource_templates()


@pytest.mark.asyncio
async def test_read_resource_raises_when_not_connected(server: MCPServerStreamableHttp):
    """read_resource raises UserError when server has not been connected."""
    from agents.exceptions import UserError

    with pytest.raises(UserError, match="Server not initialized"):
        await server.read_resource("file:///etc/hosts")


@pytest.mark.asyncio
async def test_list_resources_returns_result(server: MCPServerStreamableHttp):
    """list_resources delegates to the underlying MCP session."""
    mock_session = MagicMock()
    expected = ListResourcesResult(
        resources=[
            Resource(uri=AnyUrl("file:///readme.md"), name="readme.md", mimeType="text/markdown"),
        ]
    )
    mock_session.list_resources = AsyncMock(return_value=expected)
    server.session = mock_session

    result = await server.list_resources()

    assert result is expected
    mock_session.list_resources.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_list_resources_forwards_cursor(server: MCPServerStreamableHttp):
    """list_resources forwards the cursor argument for pagination."""
    mock_session = MagicMock()
    page2 = ListResourcesResult(resources=[])
    mock_session.list_resources = AsyncMock(return_value=page2)
    server.session = mock_session

    result = await server.list_resources(cursor="tok_abc")

    assert result is page2
    mock_session.list_resources.assert_awaited_once_with("tok_abc")


@pytest.mark.asyncio
async def test_list_resource_templates_returns_result(server: MCPServerStreamableHttp):
    """list_resource_templates delegates to the underlying MCP session."""
    mock_session = MagicMock()
    expected = ListResourceTemplatesResult(
        resourceTemplates=[
            ResourceTemplate(uriTemplate="file:///{path}", name="file"),
        ]
    )
    mock_session.list_resource_templates = AsyncMock(return_value=expected)
    server.session = mock_session

    result = await server.list_resource_templates()

    assert result is expected
    mock_session.list_resource_templates.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_list_resource_templates_forwards_cursor(server: MCPServerStreamableHttp):
    """list_resource_templates forwards the cursor argument for pagination."""
    mock_session = MagicMock()
    page2 = ListResourceTemplatesResult(resourceTemplates=[])
    mock_session.list_resource_templates = AsyncMock(return_value=page2)
    server.session = mock_session

    result = await server.list_resource_templates(cursor="tok_xyz")

    assert result is page2
    mock_session.list_resource_templates.assert_awaited_once_with("tok_xyz")


@pytest.mark.asyncio
async def test_read_resource_returns_result(server: MCPServerStreamableHttp):
    """read_resource delegates to the underlying MCP session with the given URI."""
    mock_session = MagicMock()
    uri = "file:///readme.md"
    expected = ReadResourceResult(
        contents=[
            TextResourceContents(uri=AnyUrl(uri), text="# Hello", mimeType="text/markdown"),
        ]
    )
    mock_session.read_resource = AsyncMock(return_value=expected)
    server.session = mock_session

    result = await server.read_resource(uri)

    assert result is expected
    mock_session.read_resource.assert_awaited_once_with(AnyUrl(uri))


@pytest.mark.asyncio
async def test_base_methods_raise_not_implemented():
    """Bare MCPServer subclasses that don't override resource methods get NotImplementedError."""
    from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult

    from agents.mcp import MCPServer

    class MinimalServer(MCPServer):
        """Minimal subclass implementing only the truly abstract methods."""

        @property
        def name(self) -> str:
            return "minimal"

        async def connect(self) -> None:
            pass

        async def cleanup(self) -> None:
            pass

        async def list_tools(self, run_context=None, agent=None):
            return []

        async def call_tool(self, tool_name, tool_arguments, run_context=None, agent=None):
            return CallToolResult(content=[])

        async def list_prompts(self):
            return ListPromptsResult(prompts=[])

        async def get_prompt(self, name, arguments=None):
            return GetPromptResult(messages=[])

    s = MinimalServer()

    with pytest.raises(NotImplementedError, match="list_resources"):
        await s.list_resources()

    with pytest.raises(NotImplementedError, match="list_resource_templates"):
        await s.list_resource_templates()

    with pytest.raises(NotImplementedError, match="read_resource"):
        await s.read_resource("file:///test.txt")
