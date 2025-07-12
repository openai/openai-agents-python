# Caching Example

This example show how to integrate tools and prompts caching using a Streamable HTTP server in [server.py](server.py).

Run the example via:

```
uv run python examples/mcp/caching/main.py
```

## Details

The example uses the `MCPServerStreamableHttp` class from `agents.mcp`. The server runs in a sub-process at `https://localhost:8000/mcp`.
