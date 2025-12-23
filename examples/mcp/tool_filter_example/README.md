# MCP Tool Filter Example

Python port of the JS `examples/mcp/tool-filter-example.ts`. It shows how to:

- Run the filesystem MCP server locally via `npx`.
- Apply a static tool filter so only specific tools are exposed to the model.
- Observe that blocked tools are not available.
- Enable `require_approval="always"` and auto-approve interruptions in code so the HITL path is exercised.

Run it with:

```bash
uv run python examples/mcp/tool_filter_example/main.py
```

Prerequisites:

- `npx` available on your PATH.
- `OPENAI_API_KEY` set for the model calls.
