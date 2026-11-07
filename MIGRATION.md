# Run v0 agents with the native SDK

Use the experimental `agents.migration.SDKRunner` to run a supported Classic Agent through the native Codex loop. Keep its existing Model, function tools, and application context. The selected Classic Model still makes model requests; Codex decides when to request another response and execute the declared tools.

This guide applies to the source on `feat/codex-harness`. `SDKRunner` is not included in the previously published `v0.24.0-alpha.6` preview wheels or the v0 `openai-agents` package on PyPI. Follow the [contributor setup](CONTRIBUTING.md) to build this source, then install its Classic dependencies from the repository root:

```sh
python -m pip install '.[classic]'
```

The same distribution contains both APIs and requires the matching native runtime. See [Classic installation](CLASSIC.md#install-from-the-matching-source) for dependency extras and platform constraints.

## Allow the private loopback connection

The adapter passes model responses to the native loop through a private local connection. On macOS and Windows, add `127.0.0.1` to `NO_PROXY` before starting your application, because the native transport can load system proxy settings. On other platforms, this is required when any `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` variable, including a lowercase variant, is set to a nonempty value.

When a bypass is required, define `NO_PROXY`, `no_proxy`, or both. Every defined variable must contain the exact comma-separated entry `127.0.0.1`. Preserve your existing entries. The adapter requires this explicit entry; it does not interpret wildcard or subnet exclusions. Keep proxy and bypass variables unchanged during a run. An incompatible configuration fails before provider resolution or model calls. SDKRunner does not change your proxy environment.

## Choose a migration step

| Your goal | API | Execution |
| --- | --- | --- |
| Upgrade the package while preserving existing v0 behavior | `agents.classic.Agent` and `agents.classic.Runner` | Original Python loop |
| Move a supported v0 workflow to native execution | `agents.classic.Agent` and `agents.migration.SDKRunner` | Native loop with your Classic Model and tools |
| Use the native API directly | `agents.Agent` and its `run` or `session` methods | Native loop with native declarations |

`agents.classic` retains the v0.24.0 compatibility baseline and later fixes from its pinned main commit. Start with the [Classic import migration](CLASSIC.md#change-imports-before-changing-behavior) and test your application there. Libraries that import the old root `agents` API internally also need to select Classic.

## Replace the runner for one supported workflow

Keep the Agent and tools together in the Classic namespace. For a workflow with an explicit Model object, change the runner call:

```python
# Before: existing v0 or Classic execution.
from agents.classic import Runner

result = await Runner.run(agent, "Greet Ada.", context=app_context)
```

```python
# After: native execution of the same supported Agent.
from agents.migration import SDKRunner

result = await SDKRunner.run(agent, "Greet Ada.", context=app_context)
print(result.final_output)
```

These fragments assume you already created `agent` and `app_context`. Do not pass a Classic Agent directly to the native `agents.Agent` API or mix native tools into the Classic Agent.

Select a Model explicitly, either on `agent.model` or with the per-run `model` argument. A non-`None` run argument takes precedence over the Agent. If the selected value is a name rather than a Model object, pass its Classic ModelProvider through `model_provider=provider`. You can reuse the provider from your Classic `RunConfig`. An explicit Model object does not use a provider; omit `model_provider` in that case. For example, with your existing OpenAI credentials:

```python
from agents.classic import OpenAIProvider

provider = OpenAIProvider()
try:
    result = await SDKRunner.run(agent, "Greet Ada.", model_provider=provider)
finally:
    await provider.aclose()
```

Pass a `ModelSettings` object or dictionary as `model_settings` to override the Agent settings through Classic `ModelSettings.resolve`. If the Agent still uses implicit settings, defaults follow the selected model. Explicit settings and application edits are preserved before applying the run override. The adapter validates supplied settings before provider lookup and effective settings before model execution.

The adapter does not choose a provider or change your model credentials. It borrows the Model and ModelProvider. Your application remains responsible for their resources, including any client you supply.

Run the [complete scripted example](examples/codex/migration.py) from the matching source tree:

```sh
python examples/codex/migration.py
```

It needs no model credentials and makes no external model requests. Its scripted Model requests the existing Classic `greet` tool, the native loop executes that tool with application context, and the next response supplies the final answer. The example prints `Hello, Ada!` and the native session ID. It verifies the execution path, not model quality.

## Check the supported features

| Feature | `SDKRunner` support |
| --- | --- |
| One base Classic Agent | Supported; Agent subclasses are unsupported |
| Static instructions and string input | Supported |
| Explicit Classic Model, or model name plus explicit ModelProvider | Supported within the response limits below |
| Eager Classic function tools with text results | Supported; the original tool validates arguments and invokes its callback |
| Application context | Supported; pass `context=...` |
| Text final output | Supported |
| Model request limit | `max_turns=10` by default; a positive integer counts Model calls |
| Ordinary model tuning | Passed to the selected Model within the supported settings contract |
| Runner-managed retries, timeouts, or forced tool choices | Unsupported; `tool_choice` must be `None` or `"auto"` |
| Structured final output and Agent/tool guardrails | Unsupported |
| Dynamic instructions, prompts, lifecycle hooks, or tool enablement | Unsupported |
| Static tool approval | Supported with `needs_approval=True` and an explicit async `on_approval` callback |
| Dynamic approval predicates, tool timeouts, deferred loading, or namespaces | Unsupported |
| Handoffs and agents used as tools | Unsupported |
| Custom tool-use behavior | Unsupported; keep `tool_use_behavior="run_llm_again"` and `reset_tool_choice=True` |
| MCP, hosted tools, sandbox tools, voice, or realtime | Unsupported |
| Model streaming | Pass `model_invocation="stream"` to call `Model.stream_response`; the default `"response"` calls `Model.get_response` |
| Live raw Model events | In stream mode, pass a synchronous `on_model_event` observer; these events are provisional |
| Buffered semantic events | Use `SDKRunner.start()` inside async code; these events remain buffered in either Model invocation mode |
| Synchronous execution to completion | Unsupported |
| Native persistence | Idle portable state, `LocalStorage`, and root-only `CustomStorage` |
| Classic Session stores | Pass `session` to read and append settled, uncompacted history. `OpenAIResponsesCompactionSession` works as a store. Use `agents.classic.Runner` for automatic compaction or opaque compacted history. |
| Raw conversation history input, Classic RunState, or same-turn continuation | Unsupported |

Unsupported Agent, tool, and model settings fail before provider resolution or model/tool execution. Fix the reported configuration or keep that workflow on `agents.classic.Runner`. Do not remove a guardrail or approval policy merely to make a workflow eligible.

Tool callbacks receive the application context, cumulative usage, original tool call and raw argument string, and active Agent. `ToolContext.turn_input` contains the input supplied to `Model.get_response` for the response that requested the tool. The context does not carry a Classic `RunConfig` or approval state.

Ordinary `ModelSettings`, such as temperature, maximum output tokens, reasoning, verbosity, and provider response storage, pass to your selected Model. The adapter rejects `retry`, `timeout`, `context_management`, `extra_query`, `extra_body`, `extra_headers`, and `extra_args` when they are not `None`. These settings can require runner behavior or replace request fields that the adapter must control.

The adapter validates each Model response before delivering it to native execution. A custom Model must return completed text messages, function calls, or both; reasoning records may accompany them. Unsupported output fails the run; earlier Model calls and tool effects may already have occurred.

Response items with `provider_data` are unsupported. Providers can use this field for replay data, such as Gemini thought signatures or provider-native thinking blocks, that the native loop cannot preserve. The adapter rejects the whole response before any of its tools run. Keep workflows that require this metadata on `agents.classic.Runner`.

Keep `OpenAIChatCompletionsModel` workflows on Classic, including plain text runs: that adapter adds `provider_data` to its responses for provider provenance. Standard Responses reasoning fields, including `encrypted_content`, remain supported.

The private transport allows at most 4 MiB for each native request body and each encoded response event sequence. Stream invocation also limits the cumulative encoded Model events to 4 MiB. This is a transport limit, not a model token allowance. Keep larger workflows on Classic or port them to the native API.

## Save and reopen a native conversation

Without persistence options, each call starts a fresh native conversation. Use the same persistence mode when you save a conversation and when you reopen it.

| Mode | Save | Reopen |
| --- | --- | --- |
| Portable | Pass `return_state=True` and keep `result.native_result.state` | Pass the idle `SessionState` as `state` |
| Native local | Pass `storage=LocalStorage(path)` and keep `result.native_result.turn.session_id` | Pass the original profile as `storage` and the saved ID as `resume` |
| Native custom | Pass `storage=CustomStorage(backend)` and keep the same Session ID | Pass storage backed by the original canonical store and the saved ID as `resume` |

Save a portable checkpoint after a completed turn. Restore it with your current Agent.

```python
from agents import SessionState
from agents.migration import SDKRunner

first = await SDKRunner.run(agent, "Greet Ada.", return_state=True)
checkpoint = first.native_result.state
assert checkpoint is not None
saved_json = checkpoint.to_json()

second = await SDKRunner.run(
    current_agent,
    "Now greet Grace.",
    state=SessionState.from_json(saved_json),
    return_state=True,
)
```

Keep the profile and Session ID together when you use native local storage.

```python
from pathlib import Path

from agents import LocalStorage
from agents.migration import SDKRunner

storage = LocalStorage(Path("native-profile"))
first = await SDKRunner.run(agent, "Greet Ada.", storage=storage)
second = await SDKRunner.run(
    current_agent, "Now greet Grace.", storage=storage, resume=first.native_result.turn.session_id
)
```

A successful reopen preserves the native Session ID and recorded history. Use the current Agent and application bindings for each run, including the selected Model, context, and tool callbacks. For a named Model, pass the current provider. For portable restore, use a checkpoint from the runner with matching instructions and tool declarations. Verify checkpoint integrity before restoring. Keep checkpoint contents and storage profiles private.

The native history keeps recorded item identities. The Model receives the projected native request and may see only part of that history after compaction. A Model response that reuses a recorded function call ID fails before any tool in that response runs.

Use `return_state` only without storage. Portable state must be idle and free of pending actions or recovery. `CustomStorage` supports root conversations with automatic function tools and follows the backend's durability contract.

SDKRunner submits only the root turn. After reopening a compatible local root, use a separate native Session to read completed child history with `Session.items(subagent_id=...)`. A reopened root that requires automatic compaction before sampling fails at the native request preparation boundary.

If local storage reports an unknown tool outcome, check your application records. Pass verified `ReconciledCall` records as `reconciled_calls` when reopening the original profile. For custom storage, follow the backend's recovery contract. Use `CustomStorage.reconcile` to record a verified result before reopening. Keep the conversation closed until you can verify what happened. See the [native storage and recovery guide](docs/sdk/state.md#recover-after-interruption).

## Continue a completed Classic Session

Pass your existing `agents.classic.Session` to `SDKRunner.run()` or `SDKRunner.start()`. The selected Model receives the stored messages, reasoning, and complete function call pairs in order. Your new text follows that history. The native loop starts a fresh turn and executes tools for that turn only.

A declared `SessionSettings.limit` bounds the stored history that SDKRunner sends to the Model. SDKRunner checks the complete store snapshot first and reserves every stored function call ID. It then takes the latest items in order and omits any function output whose call falls outside that window. With a limit of zero or less, the Model receives only the new input. If the snapshot reaches SDKRunner's read bound, the run stops before calling the Model.

```python
from agents.migration import SDKRunner

result = await SDKRunner.run(
    agent, "Continue where we left off.", session=existing_session, context=app_context
)
print(result.final_output)
```

Choose one persistence route per invocation. With `session`, omit `state`, `storage`, and `resume`, leave `reconciled_calls` empty, and keep `return_state=False`.

SDKRunner waits for the turn and native cleanup to succeed. It then appends the new user input, eligible Model items, and matching function results in one call. Both the result and any buffered events wait for the write to succeed. The result's `input` includes the imported history and new user input. Its `new_items` contains only the items produced in this invocation.

For `OpenAIConversationsSession`, SDKRunner follows Classic Runner's rules for item metadata. On replay, it strips `id` and `provider_data` from historical assistant messages. On append, it strips top-level `id` fields from messages and function items, preserves `call_id` values, and skips reasoning items that have neither a server ID nor encrypted content.

SDKRunner uses `OpenAIResponsesCompactionSession` to read and append settled, uncompacted history. `agents.classic.Runner` handles automatic Responses compaction. Opaque compaction records can hide historical function call IDs, so SDKRunner rejects them even outside the Model's retrieval window. Use `agents.classic.Runner` to continue an already compacted conversation.

Reopen the same store with the same session ID after restarting your application. Use different IDs for separate conversations. Your application owns the store and closes it according to the store's lifecycle. Run one request at a time for each conversation, or coordinate concurrent requests in your application. The Classic Session protocol reads and appends in separate operations.

### Finish pending Classic work before moving it

SDKRunner rejects incomplete function calls, unmatched outputs, and unknown effect records before invoking the Model. Finish a pending Classic `RunState` with its original Classic Runner. If a tool may already have run, verify and reconcile its outcome before continuing. A new Model response must use fresh function call IDs. Reusing a stored ID stops the run before any tool in that response executes.

If you cancel during an append, SDKRunner waits for the write to settle. It then raises cancellation. A store may commit a write before reporting an error. SDKRunner makes one append attempt. After a write error or cancellation, check the store and any tool effects before retrying.

## Approve a static tool call

Set `needs_approval=True` on a supported Classic function tool and pass an async `on_approval` callback. The callback receives the Classic tool context for the exact call. That context includes the original arguments and the input supplied to the Model. Return `Allow()` to dispatch the tool or `Deny(reason)` to record a tool failure the Model can read.

```python
from agents import Allow, Deny
from agents.migration import SDKRunner


async def approve(call_context):
    if await app_policy.permits(call_context):
        return Allow()
    return Deny("The application denied this call.")


result = await SDKRunner.run(agent_with_approval_tool, "Greet Ada.", on_approval=approve)
```

The run waits for the callback and keeps the Model selected at the start of that invocation. Portable capture happens after the turn completes. Cancellation invalidates approval for that turn, so a late allow cannot start the tool.

## Read the result and handle failures

`SDKRunner.run()` returns an `SDKRunResult` after the owned native Session closes. Both `SDKRunResult` and `SDKRunHandle` are available from `agents.migration`.

| Field | Value |
| --- | --- |
| `final_output` | Final answer as text |
| `last_agent` | The original Classic Agent |
| `input` | The SDK input for this invocation |
| `context_wrapper` | The Classic `RunContextWrapper`. Its `.context` holds your typed application context or `None`. Its `.usage` holds cumulative Classic `Usage` for this run. |
| `new_items` | Classic `RunItem` values from this invocation, in observed order |
| `raw_responses` | Detached Classic `ModelResponse` snapshots from the selected Model |
| `last_response_id` | The last Model response's ID, or `None` if that response has no ID |
| `interruptions` | An empty list after a successful run, including a run that handled live approval |
| `native_result` | The completed native result, with `turn.session_id`, `turn.turn_id`, and any requested portable `state` |

`result.to_input_list()` exports a detached list of Responses input items. The list starts with this invocation's SDK input and includes its new items in order. Both `mode="preserve_all"` and `mode="normalized"` return the same items. Model items keep their original IDs when supplied. Confirmed tool outputs keep the original call ID and exact text, including empty strings.

Generated native receipt IDs and history from earlier native turns stay with the native Session. Use the persistence options above to reopen that conversation. Use `agents.classic.Runner` for Classic `RunState`, cold approval, or provider continuation IDs.

### Consume buffered events

Call `SDKRunner.start()` inside an async function. It starts a task with the same options as `SDKRunner.run()`, including `on_approval`, `session`, and the native persistence options.

```python
from agents.migration import SDKRunner

stream = SDKRunner.start(agent, "Greet Ada.", context=app_context)
async for event in stream.replay_events():
    if event.type == "run_item_stream_event":
        print(event.name)

result = await stream.get_result()
print(result.final_output)
```

The iterator waits for the run to close its native Session and finish any Classic Session append. It first yields an `agent_updated_stream_event` for the starting agent. Completed reasoning, message, tool call, and confirmed tool output events follow in `result.new_items` order. The iterator then ends. You can also call `replay_events()` on a settled `SDKRunResult`. To observe live raw Model events and token deltas, use the explicit stream invocation and observer described below.

The iterator yields no events while `on_approval` waits for a decision. If the Model completes the turn after a denial, the iterator includes the recorded denial as a `tool_output` event.

If the run fails or is cancelled, `replay_events()` raises before yielding any events. `get_result()` also raises. Call `stream.cancel()` and await `stream.get_result()` to join cleanup. Cancelling a task waiting for either method also cancels the run and joins cleanup.

### Select streaming on your Model

Pass `model_invocation="stream"` to `SDKRunner.run()` or `SDKRunner.start()`. The adapter calls the selected Model's `stream_response` method. It does not switch providers or fall back to `get_response` if streaming fails. The default remains `model_invocation="response"`, including for `start()`.

An optional synchronous `on_model_event` callback receives detached raw Model events as they arrive, including failure events. Keep this callback short. An exception from the callback fails the run. These events are provisional: a text delta or completed Model event does not prove that the native turn, tool execution, storage, or cleanup succeeded. Await the run result for that guarantee. The callback does not change the buffered semantic events returned by `replay_events()`.

The first successful `response.completed` event ends consumption. Its response can omit `status` or set it to `completed`; explicit failure states or error details are rejected. The adapter also rejects a stream that ends without a completed response. It awaits the iterator's `aclose`, when available, before delivering the validated response to native execution. No tool from that response runs unless validation and iterator cleanup succeed. The adapter does not read trailing events or wait for transport EOF.

Cancellation joins iterator cleanup, including response transport closure for the built-in `OpenAIResponsesModel`. A custom Model must await its cleanup before its iterator closes. Put resource cleanup in `finally` or `aclose`; ordinary code after the terminal yield may not run. A Model that suppresses cancellation or whose cleanup does not finish can delay the run's cancellation. The application still owns the Model and provider.

The [application Model example](examples/codex/migration_models.py) uses a named ModelProvider, a per-run model selection, and a real native tool loop without external model requests:

```sh
python examples/codex/migration_models.py
python examples/codex/migration_models.py --stream
```

This integration differs from `NativeProvider(base_url=...)`. A NativeProvider configures the endpoint and credentials used by the native transport. `SDKRunner` instead calls your application Model object, which owns its provider interaction. Neither route replaces native execution permissions or tool approval policies. Python SDKRunner still rejects handoffs and agents used as tools; use Classic Runner for those workflows. JavaScript SDKRunner has its own documented child and handoff subset. Each adapter retains its language's Classic precedence: Python run model/settings override the Agent, while JavaScript run model/settings supply defaults that explicit Agent values override.

### Handle failures and cancellation

`max_turns` limits model requests, not individual tool calls or wall-clock time. Native request and stream retries are disabled for the adapter. The selected Model's underlying client can still apply its own transport retry policy. The adapter does not add another retry loop.

Classic Runner traces are not preserved. The adapter calls the selected Model with `ModelTracing.DISABLED`.

To cancel, cancel the task awaiting `SDKRunner.run` and await that task's completion. The runner closes its response endpoint and native Session. During cancellation or failure, it waits for every started tool invocation to finish, including asynchronous tools. Runner cancellation does not cancel those tool invocations. A tool that never finishes can prevent cleanup from completing. A Model that suppresses cancellation can also delay cleanup.

If native cleanup fails, import `get_cleanup` from `agents` and retain `cleanup = get_cleanup(error)` in your error handler. If `cleanup is not None`, await `cleanup.aclose()` and retain that owner if the retry fails. The helper finds the existing Session or NativeProvider through the error and its exception context. After admission, the Session also closes the migration-owned native provider; do not close that provider separately. The adapter still leaves your application Model and its provider open. `agents.migration.get_cleanup` re-exports the same helper.

Use `get_cleanup(error)` instead of reading `error.cleanup` directly. On Python 3.10, a Task boundary can replace a `CancelledError` and leave the owner on an error in its context. Cancellation still propagates; the helper does not wrap or replace the error.

Your application remains responsible for background work that its callbacks start independently. Cancellation and failure do not undo tool effects. Reconcile those effects before restarting a failed workflow.

## Move a workflow fully to the native API

For native streaming, manual tool requests, or child execution, port that workflow's declarations to `agents.Agent` and native tools. Follow the [native quickstart](docs/sdk/index.md), [tool guide](docs/sdk/tools.md), and [session guide](docs/sdk/sessions.md) from your installed SDK version.

Test model inputs, tool effects, output handling, and failure behavior at each migration step. You can keep some workflows on Classic while others use `SDKRunner` or native declarations. Choose the runner explicitly for each workflow; the adapter never falls back to Classic after starting native execution.
