# Keep large tool results out of the conversation

Run the example from this source checkout. It uses a scripted model and a local MCP server over stdio. No API key is needed.

```sh
uv run python examples/mcp/bounded_tool_output/main.py
```

Both inspection reports exceed the example's 128-byte text budget. The next model request, SDK history and caller receive warning counts of 20 and 8 with random references. The example prints each saved summary and checks the first page privately.

## Choose what to share

[main.py](main.py) uses two routes.

- `reports.site_tool()` binds `inspect_site` to the private store. The function checks access before reading the report, applies the budget and checks access again before returning. Any output editor receives the result after those checks.
- For SDK-discovered MCP tools, each `ReportServer` applies the policy to `read.report` by raw tool name, regardless of the server's display name. Its `call_tool` method checks `isError` and sees every block. It replaces large successful reports before SDK conversion, custom data extraction and function span output capture. Other tools and ordinary `MCPServerStdio` instances keep their usual behavior.

For a nonempty structured object, the wrapper uses its JSON in place of the fallback text. It resolves the sample `{"ticket": "site"}` by awaiting a local report. Runner calls `authorize_call` with the run context before sending the request. Direct calls to `server.call_tool()` skip this access check. Never let a server pick another user's record or an arbitrary file or URL.

At the tool boundary, the budget covers the sum of UTF-8 text bytes per result, including the reference. Smaller reports pass through in full. For large reports, the summary shares only the count of literal `WARN:` markers. Choose facts safe to disclose for your data. The first text slot holds the replacement and the other text slots stay empty. A structured result without a text slot gets one.

## Retrieve privately

Create one `Reports` instance per run. Load the owner's nonblank string ID from a trusted application record. The store compares IDs exactly. Pass only the authenticated requester as the run context. Never accept either identity from tool arguments or a reference. The reader uses only summaries and references issued by this store. It ignores tool output, including short reports, errors and text that looks like a reference. Without an authorized saved report, it prints a fixed message.

After the run, inside the `closing` block, application code can read the first page of each saved text part. Keep these pages out of tool results and model input.

```python
for reference, summary, part_count in reports.saved(requester):
    for part in range(part_count):
        first_page = reports.fetch(requester, reference, part=part)
```

`saved()` returns an empty tuple for an empty or closed store or a different requester. Images do not count as text parts. A structured report has one JSON part. Each `fetch()` checks access again and returns up to 64 characters. To read the next page, advance `offset` by the page length.

The originals stay in process memory outside SDK context. Leaving the `closing` block revokes every reference, including on failure or cancellation. Saved conversations cannot restore the originals. This example has no storage quota or durable store.

## Limits

- Selected successes support text and images. Images retain their contents and positions. Other media stop the run. The budget excludes images, block metadata and serialization overhead. Use this policy only for tools that accept a text replacement without a required output schema.
- Unselected results and MCP errors pass through, even when large. The local function converts source-read exceptions into a fixed unavailable status before SDK publication; it preserves cancellation. Other selected-operation exceptions and invalid summaries stop the run. Cancelling the run during preparation withholds the pending output. It cannot undo a remote action or stop counting already underway in a worker thread.
- This example disables tracing. Keep tool-data logging disabled and check logs, exceptions and other observers separately. Small reports, images and errors can still contain sensitive data.
- The recipe covers SDK-executed tools. It excludes native tools and provider-hosted MCP. Editing later requests cannot retract data already received by a provider.


## Source attribution

This recipe has been reusing PR #6120 at `4c0fcf035faa12d34480329af49c6f634ddcc276`. The integration has been adding fixed local source-error handling and tests without changing the original ownership and media limits.
