# Find a recipe by task

Install the [SDK preview wheel](installation.md), then run these recipes from the matching release's example files. Most recipes use local scripted model responses and need no API key. These recipes test application behavior. They do not measure live model quality.

## Find the rule before changing code

Use the recipes below for conversation memory, parallel tools and concurrency, image attachments, and citations. Image attachments require inline image data. Arbitrary file input is unsupported.

| Search or task | Rule and example |
| --- | --- |
| Save, resume, persistence, restore | [Storage choices](sessions.md#choose-how-to-store-a-conversation) and [capture readiness](sessions.md#check-support-and-capture-readiness). A checkpoint does not save workspace files or supply current credentials. |
| Stream to a browser, stop, disconnect, missing events | [Stream observations](sessions.md#stream-observations) and [cancel joined work](sessions.md#interrupt-work). `EventGap` means the observer missed events. Public history exposes selected conversation items and is not a full UI transcript. |
| FastAPI or another Python web service | The [browser starter](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/web_starter/README.md) uses pinned aiohttp and a browser client. If you adapt the example to FastAPI, preserve its rules for identity, requests, streaming, and cleanup. |
| Typed tools, JSON tool results, input constraints | [Python tools](tools.md#python-functions), [argument inspection](tools.md#inspect-python-policy-arguments) and [structured output](configuration.md#structured-output). Python validation and the native model schema have different limits. |
| Code mode, programmatic tool calling | [Programmatic tool calling](configuration.md#programmatic-tool-calling). Codex uses V8 JavaScript to call selected tools. A checkpoint cannot transfer pending code cells. |
| MCP on my machine | [Native MCP](tools.md#native-mcp). Tool selection does not sandbox server startup. |
| RAG, PDF, citations, document search | Extract text in application code and retrieve authorized excerpts. This interface cannot accept PDFs or arbitrary files directly, or provide authoritative citation spans from the native runtime. |
| Memory, forget a fact, retention | [History and compaction](sessions.md#compact-and-inspect-history). Your application owns its memory store and defines its deletion rules. Deleting a fact there does not delete it from old checkpoints or telemetry. |
| Multi-agent, handoff, specialist | [Native children](tools.md#native-children) share tools and context. Use separately configured Agents when tasks need different tool access. |

## Human in the loop: approve the exact request

Use [manual tool results](tools.md#manual-tool-results) when an application or person supplies a tool answer. Use [application approval](tools.md#ask-for-application-approval) to decide whether a selected executor call may run. Before accepting a reply, verify that the authenticated caller can answer that exact pending request. A timeout or cancellation does not undo completed effects.

## Rate limits and token budget

Limit the work your application accepts and queues separately from provider rate limits. Read [usage and deadlines](troubleshooting.md) before choosing a token budget. Usage reports consumption, and cancellation asks work owned by the caller to stop. Neither enforces a strict token or cost limit. If you need a strict limit, enforce it outside the SDK.

## Runnable task examples

Use `python -m examples.recipes.NAME` for the modules below. Replace `NAME` with the name in the table. The synthetic modules run without a model API key. Live modules require `OPENAI_API_KEY`, make model requests and can incur charges. Follow each source file's setup notes.

| Module and source | Run mode | Observable result and limit |
| --- | --- | --- |
| [`typed_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/typed_tools.py) | Synthetic | Validates nested input as a Python value and sends typed results to the model as JSON. Python enforces numeric bounds that the native schema can omit. Invalid input prevents the write. Invalid output cannot undo a completed write. |
| [`inspect_requests`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/inspect_requests.py) | Synthetic | Prints native function declarations, original arguments, arguments after preparation, and the result sent to the model. Scripted selection does not measure prompt quality. |
| [`concurrent_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/concurrent_tools.py) | Synthetic | Two Sessions share one quota unit without overspending. Every writer uses the same event-loop lock. The quota does not work across threads or processes. |
| [`safe_effects`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/safe_effects.py) | Synthetic | A fake writer records one write for duplicate calls. Scripted models still execute tools, so replace dependencies that have real effects before simulating. The sample deduplicates only in memory. |
| [`specialist_agents`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/specialist_agents.py) | Synthetic | A function tool awaits a separate Agent with its own catalog context and tools, then returns a typed summary. Native child tracking does not include this nested Session, and checkpoints do not treat it as a native child. Python closures can still share application permissions and resources. |
| [`application_memory`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/application_memory.py) | Synthetic | Saves, recalls, and deletes a user's SQLite fact across new Sessions. The example removes its temporary database on exit. Your application must retain the database and enforce expiry if needed. |
| [`local_mcp`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/local_mcp.py) | Live | A trusted standard-library stdio server returns the PAPER catalog price. Select the raw tool name `lookup`. The native client cleans up the connection and process. Read-only metadata does not restrict the server's host permissions. |
| [`image_input`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/image_input.py) | Live | Creates a synthetic PNG, sends inline image data, and prints the final typed answer. Native input cannot accept a local path, PDF, or remote URL. The model supplies the confidence value, which is not a calibrated probability. |
| [`retrieval`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/retrieval.py) | Live | Searches a small tenant-scoped text corpus and validates returned source IDs against actual retrieved IDs. A matching source ID does not prove that the source supports the answer. |
| [`programmatic_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/programmatic_tools.py) | Live | Uses generated JavaScript to aggregate regional sales and prints tool calls and history. Pending code cells and their runtime state need an open Session. |

For example, `python -m examples.recipes.concurrent_tools` prints `Two Sessions shared one unit without overspending.` The recipe's assertions check the remaining quota and returned tool results, rather than relying on scripted final text.

## Start a local browser application

The [complete browser application](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/web_starter/README.md) combines native tools, human approval, a final typed result, and an authorized artifact download. Install the matching preview wheel and the example's pinned dependencies, then build its browser client as that README describes. Run from the repository root:

```sh
python -m examples.web_starter --directory /tmp/agents-web-state --port 8080
```

Open `http://127.0.0.1:8080`, choose synthetic user `alice`, send the request and approve publication to obtain the download. By default, the example uses scripted Responses and needs no API key. The state directory's parent must exist. The service creates a private directory with `0700` permissions. This starter supports one local POSIX process and synthetic identities. Anyone with local access can choose either identity. A shared deployment requires real application authentication.

Disconnecting stops the browser reader. **Stop** asks the owner to cancel the work and wait for it to finish. Reload and `EventGap` recovery use authorized application snapshots instead of replaying SDK observations. After a restart, unfinished receipts become `unknown`. The service does not retry their effects automatically. Access to reports, checkpoints, and artifacts expires after five minutes. Stored data and receipts remain until the application deletes them. See the example for limits on accepted work, cancellation deadlines, current MCP credentials, and commands to start an external worker.

## Keep each data store's lifetime explicit

| Store | Owner and lifetime | Deletion or resume limit |
| --- | --- | --- |
| Model conversation context | The native Session. Compaction changes the model context. | `compact(summary=...)` uses your summary and preserves prior canonical history. `compact()` asks the model for a summary and lets the native runtime replace the history. Neither offers selective fact deletion. See [compaction](sessions.md#compact-and-inspect-history). |
| Public projected history | A view of native stored history, read through SDK iterators | This view has no independent retention policy. It is not a complete UI transcript and exposes no general selective-history deletion. See [history and context](sessions.md#compact-and-inspect-history). |
| Portable checkpoint | Trusted application storage | Can retain old facts independently of application memory. Restoring requires the current configuration. Deleting a database fact does not rewrite saved checkpoints. |
| Application memory | Application database and authenticated user scope | The application defines storage, expiry, deletion and access. The recipe keeps one fact per user and has no identity provider or automatic expiry. |
| Workspace artifacts | Storage managed by the application or executor | Checkpoints do not copy or delete files. Export and retention need separate controls. |
| Collector data | Application-selected telemetry service | The collector defines retention and deletion rules. Closing a Session or deleting a memory fact does not delete exported data. |

The [example index](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/README.md) also includes protocol and deployment examples. Use the examples' exact client pins and synthetic local identities only within their stated deployment limits.
