# Keep large MCP reports outside model context

[mcp_reports.py](mcp_reports.py) uses the native MCP client's `HttpMCP.transform_result` callback. The application retains complete oversized successful results and returns bounded previews with private references. Native Core owns the MCP connection and accepted history. The example adds no SDK storage service or borrowed-client adapter.

Install the matching SDK preview wheel, then run from the source checkout:

```sh
MCP_URL=http://localhost:8000/mcp python -m examples.recipes.mcp_reports
```

This command makes live model calls and needs the normal provider credentials. The endpoint must advertise a raw tool named `report` accepting `{ "key": "43" }`, without a declared `outputSchema`. This recipe does not preserve an arbitrary peer output-schema contract. A suitable synthetic result is a text block containing a long report, optionally with `structuredContent` containing report fields. Use an endpoint the application trusts. The example does not follow result URLs or interpret content as filesystem paths. The command prints the final answer and retrieved byte counts, without printing full retained reports.

## Bounds and retrieval

Native MCP normally projects a non-null `structuredContent` value instead of ordinary content, which can hide media. This recipe removes that field from successful replacements and puts its JSON in the first existing text slot, or adds one text fallback if necessary. Existing text slots keep their order, type and other fields. The original structured data remains in the retained original when the result is oversized.

The example measures the normalized text blocks as serialized UTF-8 JSON. Results over 4096 bytes receive an opaque UUID reference. Text slots then contain excerpts, the original byte count and the reference, including an excerpt of selected structured data. Each excerpt contains at most 32 Unicode characters. The example checks the replacement byte bound before retaining the original. Small successful results keep their full text and selected structured data inline. Peer errors pass through without normalization or retention.

Non-text blocks pass through unchanged. They are outside the 4096-byte bound, including any media that native MCP represents as text during model conversion. The example does not decode media, fetch resources or qualify audio/video processing. It accepts at most eight original content blocks. Storage holds at most four complete originals, at most 64 KiB each and 128 KiB total, measured as UTF-8 JSON. Complete originals include media and metadata. Storage rejection fails the turn without publishing a new reference or evicting existing references. These are example policies rather than SDK transport or token limits.

`report_session(url, owner, provider=...)` yields the native Session and its application store. Supply the authenticated owner from trusted application code; the command uses a synthetic identity. Both `reports.references(owner)` and `reports.read(owner, reference)` require that same object identity. A UUID alone does not grant access. Retrieval returns a fresh copy of the full original, including its protected media and metadata. Wrong owners and missing references fail explicitly.

References remain available after a successful turn while the context manager is open. Its exit first closes the Session and joins callbacks, then clears the store, including when the body fails. References are process-local and expire at that boundary. They cannot be restored from a conversation checkpoint. Use one context manager per Session and retrieve required reports before leaving it. An explicitly supplied provider remains application-owned.

The transform performs no awaits between checking bounds, retaining the original and returning the replacement. Cancellation can still occur after application retention but before native acceptance; a retained record alone does not prove the native result was accepted. Session exit clears such records too. Result transformation cannot undo the remote MCP operation. Do not replay an operation merely because the turn failed.
