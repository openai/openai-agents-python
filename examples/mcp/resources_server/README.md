# MCP Resources Server Example
This example shows the absolute basics of working with an MCP resources server by discovering what resources exist and reading one of them.

The local MCP Resources Server is defined in [server.py](server.py).

Run the example via:

```
uv run python examples/mcp/resources_server/main.py
```

## What the code does

The example uses the `MCPServerStreamableHttp` class from `agents.mcp`. The server runs in a sub-process at `http://localhost:8000/mcp` and provides resources that can be exposed to agents.
The example demonstrates three main functions:

1. **`list_resources`** - Lists all available resources in the MCP server.
2. **`list_resource_templates`** - Lists all available resources templates in the MCP server.
3. **`read_resource`** - Read a specific resource from the MCP server.
