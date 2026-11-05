# Keep large MCP reports outside model context

[mcp_reports.py](mcp_reports.py) shows one pattern using the existing `HttpMCP.transform_result` hook:

1. Receive a text report from an MCP tool.
2. Keep a small result inline, or store the full report and return a preview with a reference.
3. Retrieve the original in application code before the Session closes.

The hook runs before the result reaches the model and conversation history. Storage belongs to the application; this recipe adds no SDK API.

Install the matching SDK preview wheel, then run from the source checkout:

```sh
MCP_URL=http://localhost:8000/mcp python -m examples.recipes.mcp_reports
```

This command makes live model calls and needs provider credentials. Use a trusted MCP endpoint with a `report` tool accepting `{ "key": "43" }`. Successful results must contain exactly one text block, no non-null `structuredContent`, and no declared tool `outputSchema`. Media, multiple blocks, and structured results are deliberately outside this example. Peer errors pass through unchanged.

## What the example keeps

Successful results up to 4096 bytes stay inline. Larger results receive a preview of up to 500 characters and a UUID reference. Result sizes are measured as UTF-8 JSON, including metadata; metadata that prevents a replacement from fitting causes an error before storage. These are example limits, not SDK token or transport limits.

The store holds at most four originals, each at most 64 KiB: at most 256 KiB of report JSON per Session. A full store rejects another oversized report without evicting earlier ones.

Supply an authenticated owner from application code to `report_session(url, owner)`. The command uses a synthetic identity. Both `reports.references(owner)` and `reports.read(owner, reference)` require that same object identity; a UUID alone does not grant access. Reads return fresh copies, and the command prints only the final answer and retrieved byte counts.

Leaving the context manager closes the Session and joins its callbacks before clearing the store, including on failure. Retrieve reports inside that scope. References are process-local and cannot be restored from a conversation checkpoint. An explicitly supplied provider remains application-owned.

Cancellation can occur after retention but before native acceptance. A stored report does not prove that the turn accepted its preview; context-manager cleanup clears it either way. Transforming a result cannot undo the remote operation, so do not replay it merely because a turn failed.
