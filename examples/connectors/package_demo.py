from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agents import (
    Agent,
    Connector,
    ConnectorRegistry,
    FunctionTool,
    HostedMCPTool,
    RunContextWrapper,
    function_tool,
)
from agents.mcp import MCPServerManager
from agents.tool_context import ToolContext


@function_tool
def apply_discount(amount: float, percentage: float) -> str:
    """Calculate a discount amount."""
    return f"discount={amount * percentage / 100:.2f}"


def build_sdk_tool_connector() -> Connector:
    return Connector.from_tools(
        "pricing",
        [apply_discount],
        description="Pricing tools implemented directly in Python.",
        policy_labels={"read_only"},
    )


def build_hosted_connector() -> Connector:
    registry = ConnectorRegistry.from_plugin_records(
        [
            {
                "id": "plugin_calendar",
                "name": "calendar",
                "description": "Hosted Google Calendar connector shape.",
                "apps": {
                    "calendar": {
                        "id": "connector_googlecalendar",
                    }
                },
            }
        ]
    )
    return Connector.from_installed_plugin(
        "plugin_calendar",
        registry,
        authorization={"calendar": "demo_access_token"},
        hosted_mcp_require_approval="never",
    )


def write_demo_plugin_package(package_root: Path) -> Path:
    plugin_dir = package_root / "orders-plugin"
    plugin_config_dir = plugin_dir / ".codex-plugin"
    plugin_config_dir.mkdir(parents=True)

    (plugin_config_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "orders",
                "version": "0.1.0",
                "description": "Order lookup tools packaged like a shared plugin.",
                "mcpServers": "./.mcp.json",
                "interface": {
                    "displayName": "Orders",
                    "capabilities": ["Read"],
                },
            },
            indent=2,
        )
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "orders": {
                        "command": sys.executable,
                        "args": ["demo_mcp_server.py"],
                        "cwd": ".",
                    }
                }
            },
            indent=2,
        )
    )
    (plugin_dir / "demo_mcp_server.py").write_text(
        "\n".join(
            [
                "from mcp.server.fastmcp import FastMCP",
                "",
                "mcp = FastMCP('Orders connector demo')",
                "",
                "@mcp.tool()",
                "def lookup_order(order_id: str) -> str:",
                "    return f'order {order_id}: fulfilled'",
                "",
                "if __name__ == '__main__':",
                "    mcp.run(transport='stdio')",
                "",
            ]
        )
    )
    return plugin_dir


def build_package_connector(package_root: Path) -> Connector:
    registry = ConnectorRegistry.from_plugin_records(
        [
            {
                "id": "plugin_orders",
                "name": "orders",
                "package_path": str(package_root),
                "source": "unified_plugins_demo",
            }
        ]
    )
    return Connector.from_installed_plugin("plugin_orders", registry)


async def verify_connector_demo() -> dict[str, Any]:
    sdk_connector = build_sdk_tool_connector()
    hosted_connector = build_hosted_connector()

    with TemporaryDirectory(prefix="agents-connectors-demo-") as temp_dir:
        package_dir = write_demo_plugin_package(Path(temp_dir))
        package_connector = build_package_connector(package_dir)

        async with MCPServerManager(
            package_connector.mcp_servers,
            strict=True,
            connect_in_parallel=True,
        ):
            agent = Agent(
                name="Connector demo agent",
                instructions="Use the mounted connector tools when useful.",
                connectors=[
                    sdk_connector,
                    package_connector,
                ],
                mcp_config={"include_server_in_tool_names": True},
            )
            tools = await agent.get_all_tools(RunContextWrapper(context=None))
            tool_names = [tool.name for tool in tools]

            direct_tool = _find_function_tool(tools, "apply_discount")
            mcp_tool = _find_function_tool(tools, "mcp_orders__lookup_order")

            direct_tool_result = await direct_tool.on_invoke_tool(
                _tool_context("apply_discount", '{"amount":100,"percentage":25}'),
                '{"amount":100,"percentage":25}',
            )
            mcp_tool_result = _tool_result_text(
                await mcp_tool.on_invoke_tool(
                    _tool_context("mcp_orders__lookup_order", '{"order_id":"demo_order_1001"}'),
                    '{"order_id":"demo_order_1001"}',
                )
            )

    hosted_tool = _find_hosted_mcp_tool(hosted_connector.tools)

    return {
        "agent_tool_names": tool_names,
        "direct_tool_result": direct_tool_result,
        "mcp_tool_result": mcp_tool_result,
        "package_connector_name": package_connector.name,
        "package_policy_labels": sorted(package_connector.policy_labels),
        "package_registry_source": package_connector.metadata["unified_plugin"]["source"],
        "hosted_connector_label": hosted_tool.tool_config["server_label"],
        "hosted_connector_id": hosted_tool.tool_config["connector_id"],
    }


def _find_function_tool(tools: list[Any], name: str) -> FunctionTool:
    for tool in tools:
        if isinstance(tool, FunctionTool) and tool.name == name:
            return tool
    raise RuntimeError(f"Expected function tool not found: {name}")


def _find_hosted_mcp_tool(tools: list[Any]) -> HostedMCPTool:
    for tool in tools:
        if isinstance(tool, HostedMCPTool):
            return tool
    raise RuntimeError("Expected hosted MCP tool not found.")


def _tool_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str):
            return text
    return json.dumps(result)


def _tool_context(tool_name: str, tool_arguments: str) -> ToolContext[Any]:
    return ToolContext(
        context=None,
        tool_name=tool_name,
        tool_call_id=f"call_{tool_name}",
        tool_arguments=tool_arguments,
    )


def print_summary(summary: dict[str, Any]) -> None:
    print("Connector package demo")
    print("======================")
    print(f"Agent tools: {', '.join(summary['agent_tool_names'])}")
    print(f"Direct tool output: {summary['direct_tool_result']}")
    print(f"MCP tool output: {summary['mcp_tool_result']}")
    print(
        "Package connector: "
        f"{summary['package_connector_name']} "
        f"({', '.join(summary['package_policy_labels'])}, "
        f"{summary['package_registry_source']})"
    )
    print(
        "Hosted connector config: "
        f"{summary['hosted_connector_label']} -> {summary['hosted_connector_id']}"
    )


async def main(*, verify: bool) -> None:
    summary = await verify_connector_demo()
    print_summary(summary)
    if verify:
        expected = {
            "direct_tool_result": "discount=25.00",
            "mcp_tool_result": "order demo_order_1001: fulfilled",
            "hosted_connector_label": "calendar",
        }
        mismatches = {
            key: (summary.get(key), expected_value)
            for key, expected_value in expected.items()
            if summary.get(key) != expected_value
        }
        if mismatches:
            raise RuntimeError(f"Connector demo verification failed: {mismatches}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Demonstrate Agents SDK connector package composition."
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run deterministic checks after printing the demo summary.",
    )
    args = parser.parse_args()
    asyncio.run(main(verify=args.verify))
