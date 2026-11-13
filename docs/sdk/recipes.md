<a id="find-a-recipe-by-task"></a>

# Task recipes

Choose a small recipe for the behavior you want to add. Each recipe includes synthetic data and a runnable entry point. Use the [full recipe catalog](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/README.md) for additional examples, dependencies, and expected results.

## Run a recipe

Install the [SDK preview wheel](installation.md), download the [matching source archive](installation.md#examples-and-upgrades), and activate your SDK environment. From the archive root, try a Python-tool recipe:

```sh
python -m examples.codex.recipes.typed_tools
```

Expect a JSON receipt for SKU `synthetic` with `accepted` equal to `2`. The native loop calls a Python function and validates its output. For another recipe, replace `typed_tools` with a module name from the tables below.

**Scripted** recipes use a local model-service fixture and need no API key. Keep `127.0.0.1` reachable directly or through your proxy bypass settings. Python tools still execute; replace dependencies that have real effects before testing. **Live** recipes need `OPENAI_API_KEY` and can incur model charges.

The SDK build column distinguishes recipes in the alpha.7 archive from development-only recipes. Development-only recipes need a [matching source build](installation.md#use-development-branch-examples) and files from that checkout.

<a id="runnable-task-examples"></a>
<a id="test-application-logic-without-a-model"></a>
<a id="try-a-live-model"></a>

## Validate tools and control effects

| Module | Run | SDK build | What you can learn |
| --- | --- | --- | --- |
| [typed_tools](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/typed_tools.py) | Scripted | Alpha.7 or later | Validate nested Python inputs and typed results. Python enforces bounds that the native schema can omit. |
| [inspect_requests](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/inspect_requests.py) | Scripted | Alpha.7 or later | Compare tool declarations, original arguments, effective arguments, and the model-visible result. |
| [prepared_request](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/prepared_request.py) | Scripted | Alpha.7 or later | Edit the next sampling request while retaining tool validation and execution policies. |
| [concurrent_tools](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/concurrent_tools.py) | Scripted | Alpha.7 or later | Share one quota unit across two overlapping Sessions with an event-loop lock. |
| [safe_effects](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/safe_effects.py) | Scripted | Alpha.7 or later | Use a fake writer to record one effect for duplicate calls. Deduplication lasts only in memory. |
| [accepted_output](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/accepted-output.md) | Scripted | Development only | Keep rejected drafts private and publish only application-checked values. |

Invalid input can prevent a write. Invalid output cannot undo one. The concurrency and deduplication examples coordinate within one process; they do not provide distributed quotas or durable idempotency. Scripted selection checks application behavior, not model quality.

## Add memory, retrieval, and specialists

| Module | Run | SDK build | What you can learn |
| --- | --- | --- | --- |
| [application_memory](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/application_memory.py) | Scripted | Alpha.7 or later | Save, recall, and delete one user's SQLite fact across fresh Sessions. The temporary database is removed on exit. |
| [specialist_agents](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/specialist_agents.py) | Scripted | Alpha.7 or later | Call a separately configured Agent from a Python tool and return its typed summary. |
| [retrieval](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/retrieval.py) | Live | Alpha.7 or later | Search tenant-scoped text and check returned source IDs against retrieved IDs. |
| [image_input](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/image_input.py) | Live | Alpha.7 or later | Send a synthetic inline PNG and return a typed answer. |
| [programmatic_tools](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/programmatic_tools.py) | Live | Alpha.7 or later | Use generated JavaScript to aggregate Python tool results. |

A separately configured specialist uses its own Session. It does not inherit native child tracking or checkpoint ownership, and Python closures can still share application resources. See [native children](delegation.md) when you need a shared executor workspace.

For document search, select authorized sources in application code. A returned source ID establishes membership in the retrieved set, not whether the source supports the answer. The SDK does not provide authoritative native citation spans. See [typed input](sessions.md#typed-input) for image and file inputs, including development-only forms. Model confidence is not a calibrated probability.

The small memory recipe teaches application-owned retention. The [packaged memory example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/packaged_memory.py) demonstrates a reusable memory helper and requires a matching development build. Neither conversation history nor a memory helper replaces your application's access, expiry, and deletion rules.

## Connect MCP servers and inspect transport retries

Start with [local_mcp](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/local_mcp.py), available in alpha.7 or later. It launches the included standard-library stdio server and asks a live model for the price of `PAPER`. Select the raw tool name `lookup`; the synthetic catalog price is 5 dollars. The Session owns connection and process cleanup. The server still runs with the host's permissions.

The [MCP and transport recipes](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/README.md#connect-mcp-servers-and-inspect-transport-retries) also cover reading server resources and prompts, and observing retries through an application-owned model client. Follow each recipe's build and server requirements.

## Run supervised workers

Use the [supervised worker guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/supervised_worker/README.md) for a complete application with two local workers, separate workspaces, durable job status, and checked artifacts. It requires a matching development build, including native helpers from that revision. Each worker owns its Session; a status client can disconnect and later read the same job and accepted result.

The guide covers setup, artifact validation, revision requests, and recovery after an interrupted handoff. Check [executor write-root support](execution.md#replace-write-roots-between-turns) if an API-key worker needs different write permissions on later turns.

## Start a local browser application

The [browser starter guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/web_starter/README.md) combines tools, human approval, typed output, and authorized artifact downloads. It uses aiohttp and a browser client. Its default scripted mode needs no API key.

<a id="install-and-run"></a>

Follow that guide's installation and run commands with their pinned dependencies. Use a private local POSIX host: anyone with local access can choose either synthetic identity. Shared deployment requires real authentication. A FastAPI adaptation must preserve the starter's identity, request, streaming, and cleanup rules.

<a id="understand-recovery-and-retention"></a>

The starter distinguishes reader disconnection from work cancellation and uses application snapshots for display recovery. After a restart, it marks unfinished effects as unknown instead of retrying them automatically. Its guide explains access expiry, stored-data retention, and cleanup.

## Adapt a recipe to your application

Before replacing synthetic dependencies with application services, check the relevant API limits, approval rules, and data lifetimes.

### Check feature limits

| Task | Guide |
| --- | --- |
| Save or resume a conversation | [Storage choices](state.md#choose-how-to-store-a-conversation) and [capture readiness](state.md#check-support-and-capture-readiness) |
| Stream to a browser or stop work | [Streaming and events](streaming.md) and [work cancellation](sessions.md#interrupt-work) |
| Validate tool inputs and results | [Python tools](tools.md#python-functions), [argument inspection](tools.md#inspect-python-policy-arguments), and [structured output](outputs.md) |
| Edit the next model request | [Prepared sampling requests](configuration.md#edit-the-next-sampling-request) |
| Call tools from generated JavaScript | [Programmatic tool calling](configuration.md#programmatic-tool-calling) |
| Run an MCP server locally | [Start a stdio server](mcp.md#start-a-stdio-server) |
| Reduce conversation context | [History and compaction](state.md#compact-conversation-context) |
| Retain facts across independent conversations | [Application memory recipe](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/application_memory.py) and [data lifetimes](#keep-each-data-stores-lifetime-explicit) |

### Human in the loop: approve the exact request

Use [manual tool results](approvals.md#manual-tool-results) when your application or a person supplies a tool answer. Use [application approval](approvals.md#ask-for-application-approval) to decide whether an executor call may run. Before accepting a reply, verify that the authenticated caller can answer that exact pending request. A timeout or cancellation does not undo completed effects.

### Rate limits and token budget

Read the [API-key rate-limit snapshot](models.md#read-api-key-rate-limits) and set your own limits on accepted and queued work. Use [token usage](observability.md#read-usage-without-double-counting) to report consumption and [deadlines](troubleshooting.md#bound-a-wait-and-stop-owned-work) to stop work. Use [`RunBudget`](observability.md#limit-a-runs-model-requests-and-usage) to stop later sampling admissions. Enforce any stricter token or cost limits in your application.

### Keep each data store's lifetime explicit

Choose retention, access, and deletion rules for each store:

| Store | Lifetime and limits |
| --- | --- |
| Model conversation context | The native Session owns it. `compact(summary=...)` replaces context and preserves canonical history. `compact()` asks the model for a summary and lets the runtime replace history. Neither selectively deletes facts. See [compaction](state.md#compact-conversation-context). |
| Public projected history | A filtered view of native history, with no separate retention policy or general selective deletion. It is not a complete UI transcript. |
| Portable checkpoint | Trusted application storage can retain old facts. Restoring requires current configuration. Deleting a database fact does not rewrite checkpoints. |
| Application memory | The application controls storage, access, expiry, and deletion. The recipe stores one fact per user but has no identity provider or automatic expiry. |
| Workspace artifacts | Application or executor storage controls files. Checkpoints do not copy or delete them. |
| Collector data | The chosen telemetry service controls retention and deletion. Closing a Session or deleting a memory fact does not delete exported data. |

The [example index](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/README.md) has more protocol and deployment examples. Keep their exact client pins and stated deployment limits.
