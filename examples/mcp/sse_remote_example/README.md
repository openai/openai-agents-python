# MCP SSE Remote Example

Python port of the JS `examples/mcp/sse-example.ts`. By default it starts the bundled local SSE MCP server and lets the agent use those tools. Set `MCP_SSE_REMOTE_URL` to try a compatible remote SSE server instead.

The bundled server requires MCP Python SDK v2. The client and local server share the interpreter selected by this command:

```bash
uv run --with "mcp>=2.2,<3" python examples/mcp/sse_remote_example/main.py
```

Prerequisites:

- `OPENAI_API_KEY` set for the model calls.
