# Find a recipe by task

Install the [SDK preview wheel](installation.md), then run these recipes from the matching release's example files. Most recipes use local scripted model responses and need no real model key. They test application behavior rather than live model quality.

## Find the rule before changing code

Use the recipes below for conversation memory, parallel tools and concurrency, image attachments, and citations. Image attachments require inline image data; arbitrary file input remains unsupported.

| Search or task | Rule and example |
| --- | --- |
| Save, resume, persistence, restore | [Storage choices](sessions.md#choose-how-to-store-a-conversation) and [capture readiness](sessions.md#check-support-and-capture-readiness). A checkpoint does not save workspace files or supply current credentials. |
| Stream to a browser, stop, disconnect, missing events | [Stream observations](sessions.md#stream-observations) and [cancel joined work](sessions.md#interrupt-work). `EventGap` means the cursor lost observations; public history is a filtered projection, not a full UI transcript. |
| FastAPI or another Python web service | The [browser starter](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/web_starter/README.md) uses pinned aiohttp and a browser client. An adaptation to FastAPI must preserve the starter's identity, request, stream and cleanup rules. |
| Typed tools, JSON tool results, input constraints | [Python tools](tools.md#python-functions), [argument inspection](tools.md#inspect-python-policy-arguments) and [structured output](configuration.md#structured-output). Python validation and the native model schema have different limits. |
| Code mode, programmatic tool calling | [Programmatic tool calling](configuration.md#programmatic-tool-calling). Codex uses V8 JavaScript to call selected tools; pending cells do not transfer through a checkpoint. |
| MCP on my machine | [Native MCP](tools.md#native-mcp). Tool selection does not sandbox server startup. |
| RAG, PDF, citations, document search | Extract text in application code and retrieve authorized excerpts. There is no first-class PDF/file input or authoritative native citation-span API in this interface. |
| Memory, forget a fact, retention | [History and compaction](sessions.md#compact-and-inspect-history). Application memory has separate ownership and deletion rules; deleting a fact does not delete it from old checkpoints or telemetry. |
| Multi-agent, handoff, specialist | [Native children](tools.md#native-children) share tools and context. Use separately configured Agents when tasks need different tool access. |

## Human in the loop: approve the exact request

Use [manual tool results](tools.md#manual-tool-results) when an application or person supplies a tool answer. Use [application approval](tools.md#ask-for-application-approval) to decide whether a selected executor call may run. Bind the authenticated caller to the exact current pending request before accepting a reply. A timeout or cancellation does not undo completed effects.

## Rate limits and token budget

Set application admission and pending-work limits separately from provider rate limits. Read [usage and deadlines](troubleshooting.md) before choosing a token budget: usage reports consumption and cooperative cancellation stops owned work, but neither creates a hard token/cost ceiling. Keep an external enforcement boundary when a strict ceiling is required.

## Runnable task examples

Use `python -m examples.recipes.NAME` for the modules below. Replace `NAME` with the name in the table. The synthetic modules run without a model API key. Live modules require `OPENAI_API_KEY`, make model requests and can incur charges. Follow each source file's setup notes.

| Module and source | Run mode | Observable result and limit |
| --- | --- | --- |
| [`typed_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/typed_tools.py) | Synthetic | Nested input becomes a validated Python value and the model receives JSON from typed results. Python enforces numeric bounds that the native schema can omit. Invalid input runs no write; invalid returned data cannot undo a completed write. |
| [`inspect_requests`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/inspect_requests.py) | Synthetic | Prints native function declarations, original/effective arguments and the model-visible result. Scripted selection does not measure prompt quality. |
| [`concurrent_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/concurrent_tools.py) | Synthetic | Two Sessions share one quota unit without overspending. Every writer uses the same event-loop lock; this is not a distributed or cross-thread quota. |
| [`safe_effects`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/safe_effects.py) | Synthetic | An injected fake writer records one effect for duplicate calls. A scripted model still executes tools; replace real effect dependencies before simulating. The sample deduplicates only in memory. |
| [`specialist_agents`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/specialist_agents.py) | Synthetic | A normal function tool awaits a separate Agent with explicit catalog context/tools and returns a typed summary. The nested Session has no native child tracking or checkpoint parity. Python closures can still share application authority. |
| [`application_memory`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/application_memory.py) | Synthetic | Saves, recalls and deletes a user-scoped SQLite fact across fresh Sessions. The temporary sample database is removed on exit; retaining it and enforcing expiry belong to the application. |
| [`local_mcp`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/local_mcp.py) | Live | A trusted standard-library stdio server returns the PAPER catalog price. Select raw name `lookup`; the native client owns connection/process cleanup. Read-only metadata does not restrict the server's host permissions. |
| [`image_input`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/image_input.py) | Live | Creates a synthetic PNG, sends inline image data and prints a settled typed answer. Native input does not accept a local path, PDF or remote URL. The confidence value is model output, not calibrated probability. |
| [`retrieval`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/retrieval.py) | Live | Searches a small tenant-scoped text corpus and validates returned source IDs against actual retrieved IDs. Membership does not prove that the answer follows from those sources. |
| [`programmatic_tools`](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/recipes/programmatic_tools.py) | Live | Uses generated JavaScript to aggregate regional sales and prints call/history evidence. Pending code cells and their VM state need the live Session. |

For example, `python -m examples.recipes.concurrent_tools` prints `Two Sessions shared one unit without overspending.` The recipe's assertions check the remaining quota and returned tool results, rather than relying on scripted final text.

## Start a local browser application

The [complete service/browser starter](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/web_starter/README.md) combines native tools, human approval, a settled typed result and an authorized artifact download. Install the matching preview wheel and the example's pinned dependencies, then build its browser client as that README describes. Run from the repository root:

```sh
python -m examples.web_starter --directory /tmp/agents-web-state --port 8080
```

Open `http://127.0.0.1:8080`, choose synthetic user `alice`, send the request and approve publication to obtain the download. The default path uses scripted Responses and needs no model key. The state directory's parent must exist; the service creates a private `0700` directory. This starter supports one local POSIX process and synthetic identities. Anyone with local access can choose either identity; shared deployment requires real application authentication.

Disconnect stops the browser reader; Stop asks the owner to cancel and join work. Reload and `EventGap` recovery use authorized application snapshots rather than replaying SDK observations. After restart, unfinished receipts become `unknown`; the service does not retry their effects automatically. Report, checkpoint and artifact access expires after five minutes, but stored bytes and receipts remain until the application deletes them. See the example for finite admission, cooperative deadlines, current MCP credentials and external-worker launch commands.

## Keep each data store's lifetime explicit

| Store | Owner and lifetime | Deletion or resume limit |
| --- | --- | --- |
| Model conversation context | The native Session; compaction changes model context | `compact(summary=...)` uses a supplied summary and preserves prior canonical history. `compact()` asks the model for a summary and follows native history-replacement behavior. Neither offers selective fact deletion. See [compaction](sessions.md#compact-and-inspect-history). |
| Public projected history | A view of native stored history, read through SDK iterators | This view has no independent retention policy. It is not a complete UI transcript and exposes no general selective-history deletion. See [history and context](sessions.md#compact-and-inspect-history). |
| Portable checkpoint | Trusted application storage | Can retain old facts independently of application memory. Restore requires current configuration; deleting a database fact does not rewrite saved checkpoints. |
| Application memory | Application database and authenticated user scope | The application defines storage, expiry, deletion and access. The recipe keeps one fact per user and has no identity provider or automatic expiry. |
| Workspace artifacts | Application/executor storage | Checkpoints do not copy or delete files. Export and retention need separate controls. |
| Collector data | Application-selected telemetry service | Retention and deletion are collector policy; closing a Session or deleting a memory fact does not delete exported data. |

The [example index](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/README.md) also includes protocol and deployment examples. Use the examples' exact client pins and synthetic local identities only within their stated deployment limits.
