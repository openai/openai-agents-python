"""
Tool filtering tests use FakeMCPServer instead of real MCPServer implementations to avoid
external dependencies (processes, network connections) and ensure fast, reliable unit tests.
FakeMCPServer delegates filtering logic to the real _MCPServerWithClientSession implementation.
"""

import asyncio

import pytest
from mcp import Tool as MCPTool

from agents import Agent
from agents.mcp import ToolFilterContext, create_static_tool_filter
from agents.run_context import RunContextWrapper

from .helpers import FakeMCPServer


def create_test_agent(name: str = "test_agent") -> Agent:
    """Create a test agent for filtering tests."""
    return Agent(name=name, instructions="Test agent")


def create_test_context() -> RunContextWrapper:
    """Create a test run context for filtering tests."""
    return RunContextWrapper(context=None)


# === Static Tool Filtering Tests ===


@pytest.mark.asyncio
async def test_static_tool_filtering():
    """Test all static tool filtering scenarios: allowed, blocked, both, none, etc."""
    server = FakeMCPServer(server_name="test_server")
    server.add_tool("tool1", {})
    server.add_tool("tool2", {})
    server.add_tool("tool3", {})
    server.add_tool("tool4", {})

    # Create test context and agent for all calls
    run_context = create_test_context()
    agent = create_test_agent()

    # Test allowed_tool_names only
    server.tool_filter = {"allowed_tool_names": ["tool1", "tool2"]}
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}

    # Test blocked_tool_names only
    server.tool_filter = {"blocked_tool_names": ["tool3", "tool4"]}
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}

    # Test both filters together (allowed first, then blocked)
    server.tool_filter = {
        "allowed_tool_names": ["tool1", "tool2", "tool3"],
        "blocked_tool_names": ["tool3"],
    }
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"tool1", "tool2"}

    # Test no filter
    server.tool_filter = None
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 4

    # Test helper function
    server.tool_filter = create_static_tool_filter(
        allowed_tool_names=["tool1", "tool2"], blocked_tool_names=["tool2"]
    )
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 1
    assert tools[0].name == "tool1"


# === Dynamic Tool Filtering Core Tests ===


@pytest.mark.asyncio
async def test_dynamic_filter_sync_and_async():
    """Test both synchronous and asynchronous dynamic filters"""
    server = FakeMCPServer(server_name="test_server")
    server.add_tool("allowed_tool", {})
    server.add_tool("blocked_tool", {})
    server.add_tool("restricted_tool", {})

    # Create test context and agent
    run_context = create_test_context()
    agent = create_test_agent()

    # Test sync filter
    def sync_filter(context: ToolFilterContext, tool: MCPTool) -> bool:
        return tool.name.startswith("allowed")

    server.tool_filter = sync_filter
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 1
    assert tools[0].name == "allowed_tool"

    # Test async filter
    async def async_filter(context: ToolFilterContext, tool: MCPTool) -> bool:
        await asyncio.sleep(0.001)  # Simulate async operation
        return "restricted" not in tool.name

    server.tool_filter = async_filter
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"allowed_tool", "blocked_tool"}


@pytest.mark.asyncio
async def test_dynamic_filter_context_handling():
    """Test dynamic filters with context access"""
    server = FakeMCPServer(server_name="test_server")
    server.add_tool("admin_tool", {})
    server.add_tool("user_tool", {})
    server.add_tool("guest_tool", {})

    # Test context-independent filter
    def context_independent_filter(context: ToolFilterContext, tool: MCPTool) -> bool:
        return not tool.name.startswith("admin")

    server.tool_filter = context_independent_filter
    run_context = create_test_context()
    agent = create_test_agent()
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"user_tool", "guest_tool"}

    # Test context-dependent filter (needs context)
    def context_dependent_filter(context: ToolFilterContext, tool: MCPTool) -> bool:
        assert context is not None
        assert context.run_context is not None
        assert context.agent is not None
        assert context.server_name == "test_server"

        # Only admin tools for agents with "admin" in name
        if "admin" in context.agent.name.lower():
            return True
        else:
            return not tool.name.startswith("admin")

    server.tool_filter = context_dependent_filter

    # Should work with context
    run_context = RunContextWrapper(context=None)
    regular_agent = create_test_agent("regular_user")
    tools = await server.list_tools(run_context, regular_agent)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"user_tool", "guest_tool"}

    admin_agent = create_test_agent("admin_user")
    tools = await server.list_tools(run_context, admin_agent)
    assert len(tools) == 3


@pytest.mark.asyncio
async def test_dynamic_filter_error_handling():
    """Test error handling in dynamic filters"""
    server = FakeMCPServer(server_name="test_server")
    server.add_tool("good_tool", {})
    server.add_tool("error_tool", {})
    server.add_tool("another_good_tool", {})

    def error_prone_filter(context: ToolFilterContext, tool: MCPTool) -> bool:
        if tool.name == "error_tool":
            raise ValueError("Simulated filter error")
        return True

    server.tool_filter = error_prone_filter

    # Test with server call
    run_context = create_test_context()
    agent = create_test_agent()
    tools = await server.list_tools(run_context, agent)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"good_tool", "another_good_tool"}


# === Integration Tests ===


@pytest.mark.asyncio
async def test_agent_dynamic_filtering_integration():
    """Test dynamic filtering integration with Agent methods"""
    server = FakeMCPServer()
    server.add_tool("file_read", {"type": "object", "properties": {"path": {"type": "string"}}})
    server.add_tool(
        "file_write",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        },
    )
    server.add_tool(
        "database_query", {"type": "object", "properties": {"query": {"type": "string"}}}
    )
    server.add_tool(
        "network_request", {"type": "object", "properties": {"url": {"type": "string"}}}
    )

    # Role-based filter for comprehensive testing
    async def role_based_filter(context: ToolFilterContext, tool: MCPTool) -> bool:
        # Simulate async permission check
        await asyncio.sleep(0.001)

        agent_name = context.agent.name.lower()
        if "admin" in agent_name:
            return True
        elif "readonly" in agent_name:
            return "read" in tool.name or "query" in tool.name
        else:
            return tool.name.startswith("file_")

    server.tool_filter = role_based_filter

    # Test admin agent
    admin_agent = Agent(name="admin_user", instructions="Admin", mcp_servers=[server])
    run_context = RunContextWrapper(context=None)
    admin_tools = await admin_agent.get_mcp_tools(run_context)
    assert len(admin_tools) == 4

    # Test readonly agent
    readonly_agent = Agent(name="readonly_viewer", instructions="Read-only", mcp_servers=[server])
    readonly_tools = await readonly_agent.get_mcp_tools(run_context)
    assert len(readonly_tools) == 2
    assert {t.name for t in readonly_tools} == {"file_read", "database_query"}

    # Test regular agent
    regular_agent = Agent(name="regular_user", instructions="Regular", mcp_servers=[server])
    regular_tools = await regular_agent.get_mcp_tools(run_context)
    assert len(regular_tools) == 2
    assert {t.name for t in regular_tools} == {"file_read", "file_write"}

    # Test get_all_tools method
    all_tools = await regular_agent.get_all_tools(run_context)
    mcp_tool_names = {
        t.name
        for t in all_tools
        if t.name in {"file_read", "file_write", "database_query", "network_request"}
    }
    assert mcp_tool_names == {"file_read", "file_write"}
