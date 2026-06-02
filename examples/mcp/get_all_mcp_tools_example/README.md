# MCP get_all_mcp_tools Example

Python port of the JS `examples/mcp/get-all-mcp-tools-example.ts`. It demonstrates:

- Spinning up a local filesystem MCP server via `npx`.
- Prefetching all MCP tools with `MCPUtil.get_all_function_tools`.
- Building an agent that uses those prefetched tools instead of `mcp_servers`.
- Applying a static tool filter and refetching tools.
- Enabling `require_approval="always"` on the server and auto-approving interruptions in code to exercise the HITL path.

Run it with:

```bash
uv run python examples/mcp/get_all_mcp_tools_example/main.py
```

Prerequisites:

- `npx` available on your PATH.
- `OPENAI_API_KEY` set for the model calls.
