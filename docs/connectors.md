# Connectors

Connectors package reusable tool surfaces for an [`Agent`][agents.Agent]. They do not add a
separate runtime. A connector resolves to existing SDK primitives:

- [`Tool`][agents.tool.Tool] instances, including function tools and hosted tools.
- Local MCP servers that the SDK already knows how to expose as function tools.
- Metadata and coarse policy labels that callers can use for approval, routing, or sandbox choices.

Use connectors when you want to mount a named bundle of tools or package-provided MCP servers on
one or more agents without manually copying every tool and server into each agent definition.

## Installed plugin registries

Use [`ConnectorRegistry`][agents.connectors.ConnectorRegistry] when your application already has
installed plugin records from a Unified Plugins-style directory, marketplace, or workspace plugin
service. The SDK does not fetch those records itself; the registry is an adapter over records that
Codex, ChatGPT, your control plane, or tests have already provided.

A registry record can point at a mounted local package, hosted app connector IDs, or both. Loading
the record still produces a normal [`Connector`][agents.connectors.Connector].

```python
from agents import Agent, Connector, ConnectorRegistry


registry = ConnectorRegistry.from_plugin_records(
    [
        {
            "id": "plugin_orders",
            "name": "orders",
            "package_path": "./orders-plugin",
        },
        {
            "id": "plugin_calendar",
            "name": "calendar",
            "apps": {
                "calendar": {
                    "id": "connector_googlecalendar",
                }
            },
        },
    ]
)

orders = Connector.from_installed_plugin("plugin_orders", registry)
calendar = Connector.from_installed_plugin(
    "plugin_calendar",
    registry,
    authorization={"calendar": "conn_calendar_access_token"},
    hosted_mcp_require_approval="always",
)

agent = Agent(
    name="Assistant",
    instructions="Use installed connector plugins when needed.",
    connectors=[orders, calendar],
)
```

This keeps package discovery, marketplace installation, workspace sharing, admin policy, and cloud
sync outside the SDK runtime while giving those systems a stable place to hand installed plugin
records to the SDK.

## SDK tool connectors

Use [`Connector.from_tools()`][agents.connectors.Connector.from_tools] when your integration is
implemented directly in Python.

```python
from agents import Agent, Connector, function_tool


@function_tool
def apply_discount(amount: float, percentage: float) -> str:
    return f"discount={amount * percentage / 100:.2f}"


pricing = Connector.from_tools(
    "pricing",
    [apply_discount],
    description="Pricing helpers implemented directly in Python.",
    policy_labels={"read_only"},
)

agent = Agent(
    name="Assistant",
    instructions="Use pricing tools when needed.",
    connectors=[pricing],
)
```

Connector tools are combined with the agent's normal `tools` list when the SDK prepares available
tools for a run.

## Package connectors

Use [`Connector.from_package()`][agents.connectors.Connector.from_package] to load a connector from a
shared package layout. The initial package bridge supports:

- `.codex-plugin/plugin.json` as the package manifest.
- `.mcp.json` for local or remote MCP server definitions referenced by `mcpServers`.
- Optional `.app.json` entries referenced by `apps` for hosted connector IDs.

For packages with local MCP servers, connect the servers before running the agent. The connector
still mounts through `Agent(connectors=[...])`; do not add the same MCP servers again through
`Agent(mcp_servers=[...])`.

```python
from agents import Agent, Connector
from agents.mcp import MCPServerManager


orders = Connector.from_package("./orders-plugin")

async with MCPServerManager(orders.mcp_servers, strict=True):
    agent = Agent(
        name="Assistant",
        instructions="Use order tools when needed.",
        connectors=[orders],
        mcp_config={"include_server_in_tool_names": True},
    )
```

If a package declares hosted app connectors, pass an authorization source to
[`Connector.from_package()`][agents.connectors.Connector.from_package]. The authorization can be one
token string, a mapping keyed by app name or connector ID, or a callback.

```python
calendar = Connector.from_package(
    "./workspace-plugin",
    authorization={"calendar": "conn_calendar_access_token"},
    hosted_mcp_require_approval="always",
)
```

## Hosted connectors

Use [`Connector.from_hosted_connector()`][agents.connectors.Connector.from_hosted_connector] when
you already know the hosted connector ID and want the Responses API hosted MCP integration to call
it.

```python
import os

from agents import Agent, Connector


calendar = Connector.from_hosted_connector(
    "calendar",
    connector_id="connector_googlecalendar",
    authorization=os.environ["GOOGLE_CALENDAR_AUTHORIZATION"],
    server_label="google_calendar",
    require_approval="never",
)

agent = Agent(
    name="Assistant",
    instructions="Use calendar tools when needed.",
    connectors=[calendar],
)
```

Hosted connector tools are represented as [`HostedMCPTool`][agents.tool.HostedMCPTool] instances.
They are sent to the Responses API like other hosted tools.

## End-to-end demo

See `examples/connectors/package_demo.py` for a deterministic demo that needs no API key. It builds
a direct Python tool connector, creates a temporary plugin-style MCP package, loads that package
through a `ConnectorRegistry` record, mounts the resolved connector on an agent, invokes the
discovered tools, and inspects a hosted connector config loaded from a registry record.

Run it with:

```bash
uv run --frozen python examples/connectors/package_demo.py --verify
```
