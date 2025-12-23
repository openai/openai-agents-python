# MCP Streamable HTTP Remote Example

Python port of the JS `examples/mcp/streamable-http-example.ts`. It connects to a
remote MCP server over the Streamable HTTP transport (`https://gitmcp.io/openai/codex`)
and lets the agent use those tools.

Run it with:

```bash
uv run python examples/mcp/streamable_http_remote_example/main.py
```

Prerequisites:

- `OPENAI_API_KEY` set for the model calls.
