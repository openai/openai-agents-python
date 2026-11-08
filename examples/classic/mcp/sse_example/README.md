# MCP SSE Example

This repository example targets MCP Python SDK v2 and is intended to run with the repository's locked development environment. The Agents SDK client itself supports both MCP v1 and v2.

This example uses a local SSE server in [server.py](server.py).

Run the example via:

```
uv run python examples/classic/mcp/sse_example/main.py
```

## Details

The example uses the `MCPServerSse` class from `agents.classic.mcp`. The script picks an open localhost port and starts the server at `http://127.0.0.1:<port>/sse`. Set `SSE_PORT` and `SSE_HOST` to override the port and bind address.
