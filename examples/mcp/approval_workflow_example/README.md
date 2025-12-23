# MCP Approval Workflow Example

This example demonstrates how to use the approval workflow with non-hosted MCP server implementations, with approval callbacks.

Run the example via:

```
uv run python examples/mcp/approval_workflow_example/main.py
```

## Details

The example uses the `MCPServerSse` class from `agents.mcp`, but this also works with `MCPServerStdio` and `MCPServerStreamableHttp`. The server runs in a sub-process at `https://localhost:8000/sse`.
