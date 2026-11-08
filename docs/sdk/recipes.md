# Find a recipe by task

Install the [SDK preview wheel](installation.md) and use examples from the matching release. Most recipes use scripted model responses and need no API key. They test application behavior, not live model quality.

## Runnable task examples

Follow the [example setup](installation.md#examples-and-upgrades) to download the matching source archive and open its root directory. From the development checkout's repository root, run a module with `PYTHONPATH=. python -m examples.codex.recipes.NAME`, replacing `NAME` with a name below. Follow each source file's setup notes.

### Test application logic without a model

These examples use scripted responses and need no API key. Tools still execute, so replace dependencies that have real effects before simulating.

| Module | What it checks |
| --- | --- |
| [`typed_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/typed_tools.py) | Validates nested Python input and returns typed results as JSON. Python enforces bounds the native schema can omit. Invalid input prevents a write; invalid output cannot undo one. |
| [`inspect_requests`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/inspect_requests.py) | Prints native declarations, original arguments, arguments after preparation, and the model-visible result. Scripted selection does not measure prompt quality. |
| [`prepared_request`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/prepared_request.py) | Edits the next sampling request after a tool result while retaining execution policies. Uses APIs added after alpha.6; follow the [matching-source setup](installation.md#examples-and-upgrades) and [request-editing limits](configuration.md#edit-the-next-sampling-request). |
| [`concurrent_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/concurrent_tools.py) | Two Sessions share one quota unit through an event-loop lock. The quota does not work across threads or processes. |
| [`safe_effects`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/safe_effects.py) | A fake writer records one write for duplicate calls. Deduplication lasts only in memory. |
| [`specialist_agents`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/specialist_agents.py) | A function tool calls a separate Agent with explicit catalog context and tools, then returns a typed summary. This nested Session is not a native child for tracking or checkpoints. Python closures can still share permissions and resources. |
| [`application_memory`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/application_memory.py) | Saves, recalls, and deletes a user's SQLite fact across new Sessions. The example removes its temporary database on exit. Your application owns retention and expiry. |

For example, `PYTHONPATH=. python -m examples.codex.recipes.concurrent_tools` prints `Two Sessions shared one unit without overspending.` Its assertions check the remaining quota and tool results, not just the scripted answer.

### Try a live model

These examples require `OPENAI_API_KEY`, make model requests, and can incur charges.

| Module | What it shows |
| --- | --- |
| [`local_mcp`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/local_mcp.py) | A trusted standard-library stdio server returns the PAPER catalog price. Select the raw tool name `lookup`. The native client cleans up the connection and process. Read-only metadata does not restrict the server's host permissions. |
| [`image_input`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/image_input.py) | Sends a synthetic PNG as inline data and prints a typed answer. Local paths, PDFs, and remote URLs are unsupported. Model confidence is not a calibrated probability. |
| [`retrieval`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/retrieval.py) | Searches tenant-scoped text and checks returned source IDs against retrieved IDs. A matching ID does not prove that the source supports the answer. |
| [`programmatic_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/programmatic_tools.py) | Generated JavaScript aggregates regional sales and prints tool calls and history. Pending code cells and their runtime state need an open Session. |

## Run supervised workers

This example uses APIs added after `0.24.0-alpha.6`. Follow the [setup for examples from the development branch](installation.md#examples-and-upgrades) before running it.

The [supervised worker example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/supervised_worker/README.md) runs two local processes with separate workspaces and native histories. Each worker owns its Session. A status client can disconnect, then read the same job and its accepted result from application storage.

For an API-key worker that needs different write grants on different turns, use a supported local executor or Linux WSS executor. With macOS WSS, set the worker's write roots in its Environment before opening the Session. See [executor write roots](execution.md#replace-write-roots-between-turns).

The worker checks the actual artifact and the retained command result before accepting a draft. It can request revisions in the same conversation. An explicit publish action uses the accepted record to write a local marker once. The example also shows how to reconcile a simulated handoff after a crash and how to measure session startup, first tool result, turn completion, and revision times separately.

## Start a local browser application

The [browser starter](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/web_starter/README.md) combines tools, human approval, a typed result, and an authorized artifact download. It uses pinned aiohttp and a browser client. A FastAPI adaptation must preserve its identity, request, streaming, and cleanup rules.

Run this example only on a private local POSIX host. Anyone with local access can choose either synthetic identity. Shared deployment requires real authentication.

### Install and run

Install the matching preview wheel and pinned dependencies, then build the browser client as the starter's README describes. The state directory's parent must exist; the service creates the private directory with `0700` permissions.

Run from the repository root:

```sh
PYTHONPATH=. python -m examples.codex.web_starter --directory /tmp/agents-web-state --port 8080
```

Open `http://127.0.0.1:8080`, choose `alice`, send the request, and approve publication to get a download. The default mode uses scripted Responses and needs no API key.

### Understand recovery and retention

The starter separates display recovery from work cancellation:

- **Disconnect reader** stops the browser reader. **Stop work** asks the worker to cancel work and wait for cleanup.
- Reload and `EventGap` recovery use authorized application snapshots, not replayed SDK observations.
- After restart, unfinished receipts become `unknown`. The service does not retry their effects automatically.
- Access to reports, checkpoints, and artifacts expires after five minutes. Stored data remains until the application deletes it.

See the starter's README for work limits, cancellation deadlines, MCP credentials, and external-worker commands.

## Adapt a recipe to your application

Before replacing synthetic dependencies with application services, check the relevant API limits, approval rules, and data lifetimes.

### Check feature limits

Use these guides for the rules behind each recipe:

| Task | Guide |
| --- | --- |
| Save or resume a conversation | [Storage choices](state.md#choose-how-to-store-a-conversation) and [capture readiness](state.md#check-support-and-capture-readiness) |
| Stream to a browser or stop work | [Stream observations](sessions.md#stream-observations) and [cancel joined work](sessions.md#interrupt-work) |
| Validate tool inputs and results | [Python tools](tools.md#python-functions), [argument inspection](tools.md#inspect-python-policy-arguments), and [structured output](configuration.md#structured-output) |
| Edit the next model request | [Prepared sampling requests](configuration.md#edit-the-next-sampling-request), available in a matching development build |
| Call tools from generated JavaScript | [Programmatic tool calling](configuration.md#programmatic-tool-calling) |
| Run an MCP server locally | [Start a stdio server](mcp.md#start-a-stdio-server) |
| Manage conversation context and application memory | [History and compaction](state.md#compact-conversation-context) |
| Delegate to another agent | [Native children](delegation.md#native-children) |

For PDFs or document search, extract text in application code and retrieve authorized excerpts. The SDK cannot accept PDFs or arbitrary files directly or provide authoritative native citation spans. Images require inline image data.

### Human in the loop: approve the exact request

Use [manual tool results](approvals.md#manual-tool-results) when your application or a person supplies a tool answer. Use [application approval](approvals.md#ask-for-application-approval) to decide whether an executor call may run.

Before accepting a reply, verify that the authenticated caller can answer that exact pending request. A timeout or cancellation does not undo completed effects.

### Rate limits and token budget

Read the [API-key rate-limit snapshot](configuration.md#read-api-key-rate-limits) and set your own limits on accepted and queued work. Use [token usage](observability.md#read-usage-without-double-counting) to report consumption and [deadlines](troubleshooting.md#bound-a-wait-and-stop-owned-work) to stop work. Use [`RunBudget`](observability.md#limit-a-runs-model-requests-and-usage) to stop later sampling admissions. Enforce any stricter token or cost limits in your application.

### Keep each data store's lifetime explicit

Choose retention, access, and deletion rules for each store:

| Store | Lifetime and limits |
| --- | --- |
| Model conversation context | The native Session owns it. `compact(summary=...)` replaces context and preserves canonical history. `compact()` asks the model for a summary and lets the runtime replace history. Neither selectively deletes facts. See [compaction](state.md#compact-conversation-context). |
| Public projected history | A filtered view of native history, with no separate retention policy or general selective deletion. It is not a complete UI transcript. See [history and context](state.md#compact-conversation-context). |
| Portable checkpoint | Trusted application storage can retain old facts. Restoring requires current configuration. Deleting a database fact does not rewrite checkpoints. |
| Application memory | The application controls storage, access, expiry, and deletion. The recipe stores one fact per user but has no identity provider or automatic expiry. |
| Workspace artifacts | Application or executor storage controls files. Checkpoints do not copy or delete them. |
| Collector data | The chosen telemetry service controls retention and deletion. Closing a Session or deleting a memory fact does not delete exported data. |

The [example index](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/README.md) has more protocol and deployment examples. Keep their exact client pins and stated deployment limits.
