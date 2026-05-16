from __future__ import annotations

import json
from typing import Any, cast

import pytest

from agents import (
    Agent,
    Connector,
    HostedMCPTool,
    RunContextWrapper,
    ToolSearchTool,
    UserError,
    function_tool,
)
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from tests.mcp.helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_agent_get_all_tools_includes_connector_tools() -> None:
    @function_tool
    def direct_lookup() -> str:
        return "direct"

    @function_tool
    def connector_lookup() -> str:
        return "connector"

    connector = Connector.from_tools("crm", [connector_lookup])
    agent = Agent(name="assistant", tools=[direct_lookup], connectors=[connector])

    tools = await agent.get_all_tools(RunContextWrapper(context=None))

    assert tools == [direct_lookup, connector_lookup]
    assert agent.tools == [direct_lookup]


@pytest.mark.asyncio
async def test_connector_hosted_connector_can_defer_loading_with_tool_search() -> None:
    connector = Connector.from_hosted_connector(
        "slack",
        connector_id="asdk_app_123",
        authorization="conn_456",
        server_label="slack",
        defer_loading=True,
    )
    agent = Agent(name="assistant", tools=[ToolSearchTool()], connectors=[connector])

    tools = await agent.get_all_tools(RunContextWrapper(context=None))

    hosted_tool = next(tool for tool in tools if isinstance(tool, HostedMCPTool))
    assert isinstance(hosted_tool, HostedMCPTool)
    hosted_tool_config = cast(dict[str, Any], hosted_tool.tool_config)
    assert hosted_tool_config["type"] == "mcp"
    assert hosted_tool_config["server_label"] == "slack"
    assert hosted_tool_config["connector_id"] == "asdk_app_123"
    assert hosted_tool_config["authorization"] == "conn_456"
    assert hosted_tool_config["defer_loading"] is True
    assert any(isinstance(tool, ToolSearchTool) for tool in tools)


@pytest.mark.asyncio
async def test_connector_mcp_servers_are_used_by_agent_mcp_tools() -> None:
    server = FakeMCPServer(server_name="calendar")
    server.add_tool("search", {})
    connector = Connector.from_mcp_server("calendar", server)
    agent = Agent(
        name="assistant",
        connectors=[connector],
        mcp_config={"include_server_in_tool_names": True},
    )

    tools = await agent.get_mcp_tools(RunContextWrapper(context=None))

    assert len(tools) == 1
    assert tools[0].name == "mcp_calendar__search"


@pytest.mark.asyncio
async def test_connector_tools_reserve_mcp_tool_names_when_prefixing() -> None:
    @function_tool(name_override="mcp_calendar__search")
    def connector_lookup() -> str:
        return "connector"

    server = FakeMCPServer(server_name="calendar")
    server.add_tool("search", {})
    connector = Connector.from_tools("crm", [connector_lookup])
    agent = Agent(
        name="assistant",
        mcp_servers=[server],
        connectors=[connector],
        mcp_config={"include_server_in_tool_names": True},
    )

    tools = await agent.get_mcp_tools(RunContextWrapper(context=None))

    assert len(tools) == 1
    assert tools[0].name != "mcp_calendar__search"
    assert tools[0].name.startswith("mcp_calendar__search_")


def test_connector_from_package_loads_codex_plugin_mcp_servers(tmp_path) -> None:
    plugin_dir = tmp_path / "computer-use"
    plugin_config_dir = plugin_dir / ".codex-plugin"
    plugin_config_dir.mkdir(parents=True)
    (plugin_config_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "computer-use",
                "version": "1.2.3",
                "description": "Control desktop apps.",
                "mcpServers": "./.mcp.json",
                "interface": {
                    "displayName": "Computer Use",
                    "capabilities": ["Interactive", "Write"],
                },
            }
        )
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "computer-use": {
                        "command": "./ComputerUse",
                        "args": ["mcp"],
                        "cwd": ".",
                    },
                    "docs": {
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer token"},
                    },
                }
            }
        )
    )

    connector = Connector.from_package(plugin_dir)

    assert connector.name == "computer-use"
    assert connector.description == "Control desktop apps."
    assert connector.metadata["version"] == "1.2.3"
    assert connector.metadata["interface"]["displayName"] == "Computer Use"
    assert connector.policy_labels == {"local_execution", "network"}
    assert len(connector.mcp_servers) == 2

    stdio_server = connector.mcp_servers[0]
    assert isinstance(stdio_server, MCPServerStdio)
    assert stdio_server.name == "computer-use"
    assert stdio_server.params.command == "./ComputerUse"
    assert stdio_server.params.args == ["mcp"]
    assert str(stdio_server.params.cwd) == str(plugin_dir)

    http_server = connector.mcp_servers[1]
    assert isinstance(http_server, MCPServerStreamableHttp)
    assert http_server.name == "docs"
    assert http_server.params["url"] == "https://example.com/mcp"
    assert http_server.params["headers"] == {"Authorization": "Bearer token"}


def test_connector_from_package_rejects_paths_outside_package(tmp_path) -> None:
    plugin_dir = tmp_path / "bad-plugin"
    plugin_config_dir = plugin_dir / ".codex-plugin"
    plugin_config_dir.mkdir(parents=True)
    (plugin_config_dir / "plugin.json").write_text(
        json.dumps({"name": "bad-plugin", "mcpServers": "../outside.json"})
    )

    with pytest.raises(UserError, match="must stay inside the connector package"):
        Connector.from_package(plugin_dir)


def test_connector_from_hosted_connector_accepts_extra_tool_config() -> None:
    connector = Connector.from_hosted_connector(
        "github",
        connector_id="asdk_app_789",
        authorization="conn_012",
        server_label="github",
        allowed_tools=["search_issues"],
        require_approval="always",
        tool_config=cast(dict[str, Any], {"custom": "value"}),
    )

    tool = connector.tools[0]
    assert isinstance(tool, HostedMCPTool)
    tool_config = cast(dict[str, Any], tool.tool_config)
    assert tool_config["allowed_tools"] == ["search_issues"]
    assert tool_config["require_approval"] == "always"
    assert tool_config["custom"] == "value"
