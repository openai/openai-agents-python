# MCP SSE Example

This example uses a local SSE server in [server.py](server.py).

Run the example via:

```
uv run python examples/mcp/sse_example/main.py
```

## Details

The example uses the `MCPServerSse` class from `agents.mcp`. The server runs in a sub-process at `https://localhost:8000/sse`.
