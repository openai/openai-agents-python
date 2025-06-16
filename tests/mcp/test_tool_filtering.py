import pytest

from .helpers import FakeMCPServer


class FilterableFakeMCPServer(FakeMCPServer):
    """Extended FakeMCPServer that supports tool filtering"""

    def __init__(self, tools=None, allowed_tools=None, excluded_tools=None, server_name=None):
        super().__init__(tools)
        self.allowed_tools = allowed_tools
        self.excluded_tools = excluded_tools
        self._server_name = server_name

    async def list_tools(self):
        tools = await super().list_tools()

        # Apply filtering logic similar to _MCPServerWithClientSession
        filtered_tools = tools
        if self.allowed_tools is not None:
            filtered_tools = [t for t in filtered_tools if t.name in self.allowed_tools]
        if self.excluded_tools is not None:
            filtered_tools = [t for t in filtered_tools if t.name not in self.excluded_tools]
        return filtered_tools

    @property
    def name(self) -> str:
        return self._server_name or "filterable_fake_server"


@pytest.mark.asyncio
async def test_server_allowed_tools():
    """Test that server-level allowed_tools filters tools correctly"""
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})

    # Set allowed_tools to only include tool1 and tool2
    server.allowed_tools = ["tool1", "tool2"]

    # Get tools and verify filtering
    tools = await server.list_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}


@pytest.mark.asyncio
async def test_server_excluded_tools():
    """Test that server-level excluded_tools filters tools correctly"""
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})

    # Set excluded_tools to exclude tool3
    server.excluded_tools = ["tool3"]

    # Get tools and verify filtering
    tools = await server.list_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}


@pytest.mark.asyncio
async def test_server_both_filters():
    """Test that server-level allowed_tools and excluded_tools work together correctly"""
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})
    server.add_tool("tool4", {})

    # Set both filters
    server.allowed_tools = ["tool1", "tool2", "tool3"]
    server.excluded_tools = ["tool3"]

    # Get tools and verify filtering (allowed_tools applied first, then excluded_tools)
    tools = await server.list_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}
