import pytest

from agents.mcp import ToolFilterStatic
from .helpers import FakeMCPServer


class FilterableFakeMCPServer(FakeMCPServer):
    """Extended FakeMCPServer that supports tool filtering"""

    def __init__(self, tools=None, tool_filter=None, server_name=None):
        super().__init__(tools)
        self.tool_filter = tool_filter
        self._server_name = server_name

    async def list_tools(self):
        tools = await super().list_tools()

        # Apply filtering logic similar to _MCPServerWithClientSession
        filtered_tools = tools
        if self.tool_filter is not None:
            filtered_tools = self._apply_tool_filter(filtered_tools)
        return filtered_tools

    def _apply_tool_filter(self, tools):
        """Apply the tool filter to the list of tools."""
        if self.tool_filter is None:
            return tools
        
        # Handle static tool filter
        if isinstance(self.tool_filter, dict):
            static_filter: ToolFilterStatic = self.tool_filter
            filtered_tools = tools
            
            # Apply allowed_tool_names filter (whitelist)
            if "allowed_tool_names" in static_filter:
                allowed_names = static_filter["allowed_tool_names"]
                filtered_tools = [t for t in filtered_tools if t.name in allowed_names]
            
            # Apply blocked_tool_names filter (blacklist)
            if "blocked_tool_names" in static_filter:
                blocked_names = static_filter["blocked_tool_names"]
                filtered_tools = [t for t in filtered_tools if t.name not in blocked_names]
            
            return filtered_tools
        
        return tools

    @property
    def name(self) -> str:
        return self._server_name or "filterable_fake_server"


@pytest.mark.asyncio
async def test_server_allowed_tool_names():
    """Test that server-level allowed_tool_names filters tools correctly"""
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})

    # Set tool_filter to only include tool1 and tool2
    server.tool_filter = {"allowed_tool_names": ["tool1", "tool2"]}

    # Get tools and verify filtering
    tools = await server.list_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}


@pytest.mark.asyncio
async def test_server_blocked_tool_names():
    """Test that server-level blocked_tool_names filters tools correctly"""
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})

    # Set tool_filter to exclude tool3
    server.tool_filter = {"blocked_tool_names": ["tool3"]}

    # Get tools and verify filtering
    tools = await server.list_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}


@pytest.mark.asyncio
async def test_server_both_filters():
    """Test that server-level allowed_tool_names and blocked_tool_names work together correctly"""
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})
    server.add_tool("tool4", {})

    # Set both filters
    server.tool_filter = {
        "allowed_tool_names": ["tool1", "tool2", "tool3"],
        "blocked_tool_names": ["tool3"]
    }

    # Get tools and verify filtering (allowed_tool_names applied first, then blocked_tool_names)
    tools = await server.list_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}


@pytest.mark.asyncio
async def test_server_no_filter():
    """Test that when no filter is set, all tools are returned"""
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})

    # No filter set (None)
    server.tool_filter = None

    # Get tools and verify no filtering
    tools = await server.list_tools()
    assert len(tools) == 3
    assert {t.name for t in tools} == {"tool1", "tool2", "tool3"}
