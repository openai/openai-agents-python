# MCP SSE Remote Example

Python port of the JS `examples/mcp/sse-example.ts`. It connects to a remote MCP
server over SSE (`https://gitmcp.io/openai/codex`) and lets the agent use those tools.

Run it with:

```bash
uv run python examples/mcp/sse_remote_example/main.py
```

Prerequisites:

- `OPENAI_API_KEY` set for the model calls.
