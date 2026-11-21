# Keep large tool results out of the conversation

Run the example from this source checkout. It uses a scripted model and a local MCP server over stdio. No API key is needed.

```sh
uv run python examples/mcp/bounded_tool_output/main.py
```

Both inspection reports exceed the example's 128-byte text budget. The next model request, SDK history and caller receive warning counts of 20 and 8 with random references. The application fetches a page of each original report without adding it to the conversation.

## Choose what to share

[main.py](main.py) uses two routes.

- `reports.site_tool()` binds `inspect_site` to the private store. The function checks access before reading the report, applies the budget and checks access again before returning. Any output editor receives the result after those checks.
- For SDK-discovered MCP tools, `ReportServer.call_tool` awaits the peer and selects successful `read.report` calls on the configured `Inspection reports` server. At this point the wrapper can still check `isError` and see every block. It replaces large reports before SDK conversion, custom data extraction and function span output capture.

For a nonempty structured object, the wrapper uses its JSON in place of the fallback text. It resolves the sample `{"ticket": "site"}` by awaiting a local report. The SDK calls `authorize_call` with the run context before sending the request. Never let a server pick another user's record or an arbitrary file or URL.

At the tool boundary, the budget covers the sum of UTF-8 text bytes per result, including the reference. Smaller reports pass through in full. For large reports, the summary shares only the count of literal `WARN:` markers. Choose facts safe to disclose for your data. The first text slot holds the replacement and the other text slots stay empty. A structured result without a text slot gets one.

## Retrieve privately

Create one `Reports` instance per run. Load the owner from a trusted application record. Pass only the authenticated requester as the run context. Never accept either identity from tool arguments or a reference. Call `reports.fetch(requester, reference, part=0, offset=0)` in application code. Each page contains up to 64 characters. Advance the offset by the page length, or select another text part. Structured reports have one JSON part.

The originals stay in process memory outside SDK context. Leaving the `closing` block revokes every reference, including on failure or cancellation. Saved conversations cannot restore the originals. This example has no storage quota or durable store.

## Limits

- Selected successes support text and images. Images retain their contents and positions. Other media stop the run. The budget excludes images, block metadata and serialization overhead. Use this policy only for tools that accept a text replacement without a required output schema.
- Unselected results and MCP errors pass through, even when large. Exceptions from selected operations and invalid summaries stop the run. Cancelling the run during preparation withholds the pending output. It cannot undo a remote action or stop counting already underway in a worker thread.
- This example disables tracing. Keep tool-data logging disabled and check logs, exceptions and other observers separately. Small reports, images and errors can still contain sensitive data.
- The recipe covers SDK-executed tools. It excludes native tools and provider-hosted MCP. Editing later requests cannot retract data already received by a provider.
