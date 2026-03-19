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
            Resource(uri="file:///readme.md", name="readme.md", mimeType="text/markdown"),
        ]
    )
    mock_session.list_resources = AsyncMock(return_value=expected)
    server.session = mock_session

    result = await server.list_resources()

    assert result is expected
    mock_session.list_resources.assert_awaited_once()


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
    mock_session.list_resource_templates.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_resource_returns_result(server: MCPServerStreamableHttp):
    """read_resource delegates to the underlying MCP session with the given URI."""
    mock_session = MagicMock()
    uri = "file:///readme.md"
    expected = ReadResourceResult(
        contents=[
            TextResourceContents(uri=uri, text="# Hello", mimeType="text/markdown"),
        ]
    )
    mock_session.read_resource = AsyncMock(return_value=expected)
    server.session = mock_session

    result = await server.read_resource(uri)

    assert result is expected
    mock_session.read_resource.assert_awaited_once_with(uri)
