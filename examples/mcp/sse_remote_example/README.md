# MCP SSE Remote Example

Python port of the JS `examples/mcp/sse-example.ts`. By default it starts the
bundled local SSE MCP server and lets the agent use those tools. Set
`MCP_SSE_REMOTE_URL` to try a compatible remote SSE server instead.

Run it with:

```bash
uv run python examples/mcp/sse_remote_example/main.py
```

Prerequisites:

- `OPENAI_API_KEY` set for the model calls.
