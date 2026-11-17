# MCP SSE Example

This repository example targets MCP Python SDK v2. The command below selects MCP v2 for both the client and its local server. The Agents SDK client itself supports both MCP v1 and v2.

This example uses a local SSE server in [server.py](server.py).

Run the example via:

```
uv run --with "mcp>=2.2,<3" python examples/mcp/sse_example/main.py
```

## Details

The example uses the `MCPServerSse` class from `agents.mcp`. The server runs in a sub-process at `https://localhost:8000/sse`.
