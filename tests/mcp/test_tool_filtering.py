import pytest

from agents import Agent
from agents.mcp import MCPUtil

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


@pytest.mark.asyncio
async def test_agent_allowed_tools():
    """Test that agent-level allowed_tools filters tools correctly"""
    server1 = FilterableFakeMCPServer(server_name="server1")
    server1.add_tool("tool1", {})
    server1.add_tool("tool2", {})

    server2 = FilterableFakeMCPServer(server_name="server2")
    server2.add_tool("tool3", {})
    server2.add_tool("tool4", {})

    # Create agent with allowed_tools in mcp_config
    agent = Agent(
        name="test_agent",
        mcp_servers=[server1, server2],
        mcp_config={
            "allowed_tools": {
                "server1": ["tool1"],
                "server2": ["tool3"],
            }
        }
    )

    # Get tools and verify filtering
    tools = await agent.get_mcp_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool3"}


@pytest.mark.asyncio
async def test_agent_excluded_tools():
    """Test that agent-level excluded_tools filters tools correctly"""
    server1 = FilterableFakeMCPServer(server_name="server1")
    server1.add_tool("tool1", {})
    server1.add_tool("tool2", {})

    server2 = FilterableFakeMCPServer(server_name="server2")
    server2.add_tool("tool3", {})
    server2.add_tool("tool4", {})

    # Create agent with excluded_tools in mcp_config
    agent = Agent(
        name="test_agent",
        mcp_servers=[server1, server2],
        mcp_config={
            "excluded_tools": {
                "server1": ["tool2"],
                "server2": ["tool4"],
            }
        }
    )

    # Get tools and verify filtering
    tools = await agent.get_mcp_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool3"}


@pytest.mark.asyncio
async def test_combined_filtering():
    """Test that server-level and agent-level filtering work together correctly"""
    # Server with its own filtering
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})
    server.add_tool("tool4", {})
    server.allowed_tools = ["tool1", "tool2", "tool3"]  # Server only exposes these

    # Agent with additional filtering
    agent = Agent(
        name="test_agent",
        mcp_servers=[server],
        mcp_config={
            "excluded_tools": {
                "test_server": ["tool3"],  # Agent excludes this one
            }
        }
    )

    # Get tools and verify filtering
    tools = await agent.get_mcp_tools()
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}


@pytest.mark.asyncio
async def test_util_direct_filtering():
    """Test MCPUtil.get_all_function_tools with filtering parameters"""
    server1 = FilterableFakeMCPServer(server_name="server1")
    server1.add_tool("tool1", {})
    server1.add_tool("tool2", {})

    server2 = FilterableFakeMCPServer(server_name="server2")
    server2.add_tool("tool3", {})
    server2.add_tool("tool4", {})

    # Test direct filtering through MCPUtil
    allowed_tools_map = {"server1": ["tool1"]}
    excluded_tools_map = {"server2": ["tool4"]}

    tools = await MCPUtil.get_all_function_tools(
        [server1, server2],
        convert_schemas_to_strict=False,
        allowed_tools_map=allowed_tools_map,
        excluded_tools_map=excluded_tools_map
    )

    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool3"}


@pytest.mark.asyncio
async def test_filtering_priority():
    """Test that server-level filtering takes priority over agent-level filtering"""
    # Server only exposes tool1 and tool2
    server = FilterableFakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})
    server.allowed_tools = ["tool1", "tool2"]

    # Agent tries to allow tool3 (which server doesn't expose)
    agent = Agent(
        name="test_agent",
        mcp_servers=[server],
        mcp_config={
            "allowed_tools": {
                "test_server": ["tool2", "tool3"],  # tool3 isn't available from server
            }
        }
    )

    # Get tools and verify filtering
    tools = await agent.get_mcp_tools()
    assert len(tools) == 1
    assert tools[0].name == "tool2"  # Only tool2 passes both filters
