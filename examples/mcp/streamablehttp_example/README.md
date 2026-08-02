# MCP Streamable HTTP Example

This repository example targets MCP Python SDK v2 and is intended to run with the repository's locked development environment. The Agents SDK client itself supports both MCP v1 and v2.

This example uses a local Streamable HTTP server in [server.py](server.py).

Run the example via:

```
uv run python examples/mcp/streamablehttp_example/main.py
```

## Details

The example uses the `MCPServerStreamableHttp` class from `agents.mcp`. The script picks an open localhost port automatically (or honors `STREAMABLE_HTTP_PORT` if you set it) and starts the server at `http://<host>:<port>/mcp`. Set `STREAMABLE_HTTP_HOST` if you need a different bind address.
