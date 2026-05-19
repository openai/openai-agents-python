from __future__ import annotations

import json
from typing import Any, cast

import pytest

from agents import (
    Agent,
    Connector,
    ConnectorRegistry,
    HostedConnectorAuthorization,
    HostedMCPTool,
    RunContextWrapper,
    ToolSearchTool,
    UserError,
    function_tool,
)
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from tests.mcp.helpers import FakeMCPServer


def test_hosted_connector_authorization_is_exported() -> None:
    authorization: HostedConnectorAuthorization = "conn_123"

    assert authorization == "conn_123"


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


def test_connector_from_package_loads_app_manifest_with_authorization_mapping(tmp_path) -> None:
    plugin_dir = tmp_path / "workspace"
    plugin_config_dir = plugin_dir / ".codex-plugin"
    plugin_config_dir.mkdir(parents=True)
    (plugin_config_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "workspace",
                "description": "Workspace connectors.",
                "apps": "./.app.json",
            }
        )
    )
    (plugin_dir / ".app.json").write_text(
        json.dumps(
            {
                "apps": {
                    "calendar": {
                        "id": "connector_googlecalendar",
                    }
                }
            }
        )
    )

    connector = Connector.from_package(
        plugin_dir,
        authorization={"calendar": "conn_calendar"},
        hosted_mcp_require_approval="always",
    )

    assert connector.policy_labels == {"network"}
    assert len(connector.tools) == 1
    tool = connector.tools[0]
    assert isinstance(tool, HostedMCPTool)
    tool_config = cast(dict[str, Any], tool.tool_config)
    assert tool_config["type"] == "mcp"
    assert tool_config["server_label"] == "calendar"
    assert tool_config["connector_id"] == "connector_googlecalendar"
    assert tool_config["authorization"] == "conn_calendar"
    assert tool_config["require_approval"] == "always"


def test_connector_from_package_skips_app_manifest_without_authorization(tmp_path) -> None:
    plugin_dir = tmp_path / "workspace"
    plugin_config_dir = plugin_dir / ".codex-plugin"
    plugin_config_dir.mkdir(parents=True)
    (plugin_config_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "workspace",
                "description": "Workspace connectors.",
                "apps": "./.app.json",
            }
        )
    )
    (plugin_dir / ".app.json").write_text(
        json.dumps(
            {
                "apps": {
                    "calendar": {
                        "id": "connector_googlecalendar",
                    }
                }
            }
        )
    )

    connector = Connector.from_package(plugin_dir)

    assert connector.tools == []
    assert connector.policy_labels == set()


def test_connector_registry_loads_installed_plugin_package(tmp_path) -> None:
    plugin_dir = tmp_path / "orders"
    plugin_config_dir = plugin_dir / ".codex-plugin"
    plugin_config_dir.mkdir(parents=True)
    (plugin_config_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "orders",
                "version": "1.0.0",
                "description": "Order lookup plugin.",
                "mcpServers": "./.mcp.json",
            }
        )
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "orders": {
                        "command": "python",
                        "args": ["server.py"],
                        "cwd": ".",
                    }
                }
            }
        )
    )

    registry = ConnectorRegistry.from_plugin_records(
        [
            {
                "id": "plugin_orders",
                "name": "orders",
                "package_path": str(plugin_dir),
                "source": "unified_plugins",
            }
        ]
    )
    connector = Connector.from_installed_plugin("plugin_orders", registry)

    assert [plugin.id for plugin in registry.list_plugins()] == ["plugin_orders"]
    assert connector.name == "orders"
    assert connector.description == "Order lookup plugin."
    assert connector.metadata["unified_plugin"]["id"] == "plugin_orders"
    assert connector.metadata["unified_plugin"]["source"] == "unified_plugins"
    assert connector.policy_labels == {"local_execution"}
    assert len(connector.mcp_servers) == 1
    server = connector.mcp_servers[0]
    assert isinstance(server, MCPServerStdio)
    assert str(server.params.cwd) == str(plugin_dir)


def test_connector_registry_loads_hosted_app_connector_record() -> None:
    registry = ConnectorRegistry.from_plugin_records(
        [
            {
                "id": "plugin_workspace",
                "name": "workspace",
                "description": "Workspace apps.",
                "apps": {
                    "calendar": {
                        "id": "connector_googlecalendar",
                    }
                },
            }
        ]
    )

    connector = Connector.from_installed_plugin(
        "workspace",
        registry,
        authorization={"calendar": "conn_calendar"},
        hosted_mcp_require_approval="always",
    )

    assert connector.name == "workspace"
    assert connector.description == "Workspace apps."
    assert connector.policy_labels == {"network"}
    assert len(connector.tools) == 1
    tool = connector.tools[0]
    assert isinstance(tool, HostedMCPTool)
    tool_config = cast(dict[str, Any], tool.tool_config)
    assert tool_config["type"] == "mcp"
    assert tool_config["server_label"] == "calendar"
    assert tool_config["connector_id"] == "connector_googlecalendar"
    assert tool_config["authorization"] == "conn_calendar"
    assert tool_config["require_approval"] == "always"


def test_connector_registry_skips_hosted_apps_without_authorization() -> None:
    registry = ConnectorRegistry.from_plugin_records(
        [
            {
                "id": "plugin_workspace",
                "name": "workspace",
                "apps": {
                    "calendar": {
                        "id": "connector_googlecalendar",
                    }
                },
            }
        ]
    )

    connector = Connector.from_installed_plugin("plugin_workspace", registry)

    assert connector.tools == []
    assert connector.policy_labels == set()


def test_agent_rejects_invalid_connectors() -> None:
    with pytest.raises(TypeError, match="Agent connectors must be a list"):
        Agent(name="assistant", connectors=cast(Any, ()))

    with pytest.raises(TypeError, match="Agent connectors must contain Connector instances"):
        Agent(name="assistant", connectors=cast(Any, [object()]))


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
