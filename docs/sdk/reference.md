# API reference

> **Version:** These guides describe the v1 SDK on `feat/codex-harness`. Build the SDK from the same commit as these guides. See [installation and version compatibility](versions.md) for v0 and v1.

These signatures show the supported calls and defaults. `*` marks keyword-only arguments. Type parameters describe application context and final output. Follow each guide link for behavior and limits.

## Agent and one-shot runs

### Agent

Declares instructions, tools and final output. Construction starts no runtime. A Session captures the declaration when it opens. See [configuration](configuration.md).

```text
Agent(*, instructions: str, name=None, tools=None, model=None,
      model_settings=None, provider=None, output_type=None,
      programmatic_tool_calling=False)

agent.validate() -> None
agent.with_output_type(output_type) -> Agent
await agent.run(input, *, context=None, on_interaction=None,
                provider=None, model=None, model_settings=None, environment=None,
                tools=None, before_step=None, state=None, return_state=False) -> Completed
agent.run_sync(input, *, context=None, on_interaction=None,
               provider=None, model=None, model_settings=None, environment=None,
               tools=None, before_step=None, state=None, return_state=False) -> Completed
agent.session(*, context=None, state=None, storage=None, resume=None, provider=None, model=None,
              model_settings=None, environment=None, tools=None)
              -> async context manager[Session]
```

`instructions` and `input` are required. Use `input=None` only to resume a supported pending manual checkpoint with a handler. `on_interaction` returns the exact request's `reply(value)`, directly or through an awaitable. It must not respond itself and return `None`. `return_state=True` captures successful completion. `return_state` must be a bool. By default, `Completed.state` is `None`. A capture failure fails the run. See [manual requests](tools.md#answer-interactions-during-a-one-shot-run).

`validate()` checks the declaration without opening a Session, reading credentials or connecting to services. It returns `None` on success and raises the first existing `TypeError` or `ValueError` on an invalid declaration. Context, environment, overrides and native model support still need checks when a Session opens. There is no validation report to inspect.

`output_type=None` or bare `str` returns text. For native and prompted structured output, including `OutputValidationError` handling and restore limits, see [configuration](configuration.md#structured-output). Use `with_output_type(...)` to create an Agent variant for a different output type. There is no one-shot `output_type` argument.

When restoring `state`, the checkpoint's model and model settings take precedence over the Agent declaration. A non-`None` opening `model` or `model_settings`, including an empty settings dictionary, raises `ValueError`. Restore first, then update an idle Session where supported. Sessions opened with `NativeAgents` keep their model settings fixed. See [save and restore](sessions.md#compact-save-and-restore).

## Sessions and turns

### Session

Owns a native conversation. Open it with `agent.session()` in an async context. See [conversations and state](sessions.md).

| Call | Result |
| --- | --- |
| `await session.submit(input, *, output_type=None, tools=None, before_step=None, if_busy="reject")` | Return an issued root Turn before completion. Busy defaults to rejection; `"queue"` registers FIFO work. `"steer"` steers the captured root when busy and starts a new root when idle. Use `Turn.steer()` to target only a specific Turn. Steering disallows overrides. |
| `session.events(*, after=0)` | Observe the whole Session until it ends. |
| `session.interactions()` | Receive live requests, separate from passive observations. |
| `await session.cancel_all(*, mode="force")` | Stop and join root and descendant work, including queued work. See [interruption](sessions.md#interrupt-work). |
| `await session.compact(*, summary=None)` | Compact an idle Session. A nonempty supplied summary replaces model context without a model request and preserves canonical history. See [compaction limits](sessions.md#compact-and-inspect-history). |
| `await session.save_state()` | Capture an idle conversation without retiring it. |
| `await session.fork()` | Return `SessionState` for an independent conversation; the source remains usable. This does not copy workspace files. See [save and restore](sessions.md#compact-save-and-restore) for storage, descendant and programmatic-state limits. |
| `await session.suspend()` | Capture a supported manual boundary and retire this Session. |
| `await session.update(*, model=None, model_settings=None, instructions=None, tools=None)` | Change one idle category per call. Tool updates can replace admitted Python/manual names or MCP server/raw-tool pairs and set the default. Temporary turn selections use the last accepted bindings. See [Session updates](sessions.md#update-tools-instructions-and-model-settings). |
| `session.items(*, subagent_id=None)` | Async iterator over a snapshot of recorded items. |
| `session.turns(*, subagent_id=None)` | Async iterator over a snapshot of recorded turns. |
| `session.subagents()` | Async iterator over captured Subagent records. |
| `await session.checklist(*, subagent_id=None)` | Read the root or a descendant's latest `ChecklistState`, or `None` before an update. Readback grants no planning tools. |
| `await session.goal()` | Read the actual `NativeGoal` data, or `None`, on a local Session opened with `NativeGoals`. Codex owns its status and accounting. |
| `await session.pending_interactions()` | Return a snapshot tuple of currently pending live requests. Inspecting it does not consume their reply authority. |
| `await session.checkpoint_status(*, kind)` | Inspect current capture readiness for `kind="idle"` or `"manual_wait"`. Returns `CheckpointStatus` with `status` (`"ready"`, `"not_ready"`, or `"unknown"`) and `reasons`. It reserves no checkpoint; capture checks again. |

`events(after=...)` uses a retained global observation sequence. History and descendant iterators have no pagination arguments. See [streaming](sessions.md#stream-observations) and [manual suspension](sessions.md#saving-a-settled-manual-request).

`session.active_turn` is the current root `Turn[Any]`, or `None` when there is no active root. `session.restored_turn` retains the root restored at a pending manual boundary, even after another turn starts. It is `None` for a new Session or an idle restore. Both properties erase the output type because a root can override the Session's output contract.

`session.capabilities.get(feature)` returns `FeatureSupport` with `status` (`"supported"`, `"limited"`, or `"unsupported"`), `reason` and `scope`. `session.capabilities.require(feature, allow_limited=False)` raises `CapabilityError` for unsupported or, by default, limited coverage. `session.capabilities.websockets_allowed` reports provider permission to use WebSockets, not the negotiated transport. These reports describe configuration support; they grant no authority and do not establish checkpoint readiness. See [conversations and state](sessions.md).

### before_step

The callback receives the root `Turn` as a positional argument and `step_index` as a keyword-only argument. It can be synchronous or asynchronous. Return `None` to keep that turn's tool selection, or a sequence of selectors for this preparation. The zero-based counter counts preparations, which can be cancelled before a model request occurs. Selectors must obey the [tool-selection limits](tools.md#idle-replacement). For programmatic tool calling, the native entry points are `ToolRef("exec", "functions")` and `ToolRef("wait", "functions")`. See the [per-step example](sessions.md#update-tools-instructions-and-model-settings).

### Turn

`await turn.get_result()` follows an issued root Turn through its accepted continuations. A captured child Turn instead represents one exact native turn; its result does not follow later child turns. Observe child events or fetch a new Subagent snapshot to find a successor. Use the event loop where the Session opened. The captured result remains stable after later submissions and Session closure. After `run_sync()`, use `Completed` directly; its event loop has closed. Cancelling a result reader leaves work running.

`turn.events(*, after=0)` observes that Turn. `await turn.wait_for_request_or_result()` returns `Completed` or `PendingRequests`, whose `requests` tuple contains this Turn's live requests. It does not claim request ownership; see the [reply loop](sessions.md#observation-details). `await turn.steer(prompt)` targets the captured active root or the captured active child turn; it cannot retarget another turn or revive a closed child. `await turn.cancel(*, mode="force")` stops that root or withdraws it if queued. Child cancellation uses `Subagent.try_cancel`; see [interruption](sessions.md#interrupt-work) for its boolean result and cleanup limits. IDs alone never recreate authority. See [Turn lifetime](sessions.md#conversations-events-and-state).

### Completed

Contains `turn`, `final_output`, `usage`, optional `state` and `repair_attempts`. `turn` is the exact root or child `Turn` that produced the result. Usage is a cumulative thread snapshot or `None`, not a per-turn cost; root usage does not aggregate child usage. `state` is populated only when a one-shot run requests `return_state=True`.

`repair_attempts` is a tuple of `RecordedTurn` receipts for opted-in prompted-output corrections; it is empty when no repair ran. Their per-turn usage is separate from `Completed.usage`, which still describes the original thread. Missing usage remains `None`. The final output may come from a repair without rewriting the original native history. See [structured output configuration](configuration.md).

### TokenUsageInfo and TokenUsage

Import these immutable data classes from `agents.usage`. `Completed.usage` is a `TokenUsageInfo` or `None`. `TokenUsageInfo` contains `total_token_usage: TokenUsage`, `last_token_usage: TokenUsage` and `model_context_window: int | None`.

`TokenUsage` contains integer fields `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` and `total_tokens`. Cached and reasoning counts are subsets, not additions to the total. These are native thread snapshots, not per-submission costs or billing records. See [usage readback and accounting limits](troubleshooting.md#read-usage-without-double-counting).

### Subagent

A captured descendant's identity, status, `is_live` and optional observed `turn`. A snapshot can become stale. `await child.start(input)` validates the live incarnation and returns a `Turn[str]` only while the child is idle. To steer an active child turn, use `await child.turn.steer(input)`. It cannot create or resume a closed child. Manual replies use live requests, not input to this method. See [native children](tools.md#native-children).

### InteractionRequired

The run needs an application decision. A supported plain Python/manual one-shot run can raise this with a portable checkpoint in `state`. Unsupported automatic-save combinations have `state=None`; always check it. Resuming a pending checkpoint without a handler retains the supplied checkpoint in `state`. See [manual continuation](tools.md#continue-a-one-shot-manual-request) for the limits and resume call.

### NativeError

Reports native execution, parsing or lifecycle failure. An error does not prove that no effect occurred or that earlier effects were rolled back.

| Field | Meaning |
| --- | --- |
| `status_code` | Available provider HTTP status, or `None`. |
| `code` | Available upstream provider error code, or `None`. |
| `request_id` | Available provider request ID, or `None`. |
| `category` | Normalized native failure category, or `None`. |
| `effects` | `"not_started"`, `"unknown"`, or `"recorded"` for the exact failed operation. The default is `"unknown"`. |

Missing or discarded metadata remains `None`. Messages and metadata may contain provider-supplied information. `effects="unknown"` does not establish that replay is safe. Even `"not_started"` describes only this failed operation, not earlier actions.

### Lifecycle errors

| Exception | Meaning |
| --- | --- |
| `SessionBusyError` | An idle-only start was requested while the Session owned active work. |
| `CapabilityError` | The requested operation is outside the Session's admitted capability. |
| `StaleTurnError` | The captured Turn cannot control the requested owner or its current work. |
| `InteractionExpiredError` | The captured request no longer has reply authority. |
| `TurnStoppedError` | The root Turn stopped and its owned cleanup finished. This is the work's result, not cancellation of a reader waiting for it, and does not promise rollback. |

### SessionState

Import from `agents.state`. Serialize with `to_json()` and load with `SessionState.from_json()` using trusted application storage. Restore requires the same pinned Codex revision that saved the checkpoint; JSON serialization does not make it compatible with a different revision. The read-only `instructions` accessor derives accepted instructions from the stored history; it does not establish trust in their source. See [save and restore](sessions.md#compact-save-and-restore).

### LocalStorage

Import `LocalStorage` from `agents` and construct it with a `pathlib.Path`, for example `LocalStorage(Path("conversations"))`. Pass the profile as `agent.session(storage=profile)`. Keep `session.id` in application storage. After closing the Session, reopen the conversation with `agent.session(storage=profile, resume=saved_id)` using the original profile directory and current tools, context and provider. Only one native writer can own the conversation at a time.

`resume` requires `storage`. Neither can be combined with portable `state`. A Session using local storage cannot export portable state through `save_state()`, `suspend()` or `fork()`. Reopening fails if saved history contains a function call with an unknown outcome or an active child whose runtime cannot be restored. It does not retry those tools automatically. See [local recovery limits](sessions.md#reopen-a-conversation-from-local-storage).

## Tools and connection data

### function_tool

Declares a Python function without starting a runtime. Input annotations are required; the return annotation is optional. A supplied return annotation still validates output, including resolved `T` from a synchronous function annotated `Awaitable[T]` or `Coroutine[Any, Any, T]`:

```text
@function_tool
@function_tool(name=None, description=None, callback_timeout=None,
               before=None, after=None, defer_loading=False)
```

Names and descriptions default to the function declaration. `callback_timeout=None` sets no callback body deadline. `defer_loading=True` lets supported models discover selected schemas when needed; other models receive them directly. It does not defer MCP connection startup or admit tools outside the selection. See [Python tools](tools.md#python-functions) and [notifications](tools.md#tool-notifications).

### FunctionTool

An immutable declaration with or without a registered Python callback. `function_tool` creates one from a Python function. `before` and `after` are flat callback fields; see [Python functions](tools.md#python-functions). A declaration without a callback supplies manual results and cannot use callback settings:

```text
FunctionTool(*, name: str, description: str, input_type: type, output_type: type,
             defer_loading=False)
request = tool.match(event)
```

In a one-shot handler, return `request.reply(value)`. In an explicit Session, use `await request.respond(value)`. See [manual results](tools.md#manual-tool-results).

### Tool

Use `Tool[Application]` to annotate collections that mix supported tool declarations.

### ToolRef

`ToolRef(name, namespace=None)` identifies a tool already admitted to the Session. Use `python_tool.ref` for a Python or manual function and `server.tool(raw_name)` for an MCP leaf. A selector uses the accepted binding; it does not replace a callback or reconnect a server. See [idle replacement and selection](tools.md#idle-replacement).

### ToolOutput

`ToolOutput.text(value)`, `ToolOutput.image(image_url)` and `ToolOutput.error(message)` construct native tool results. For mixed content, pass a list of `ToolOutputText` and `ToolOutputImage` items to `ToolOutput`. Images must be inline base64 image data URLs; remote URLs, file IDs and local paths are unsupported. `ToolOutput.error(...)` returns an explicit failed tool result (`success=False`), which differs from raising a Python exception. See [tool values](tools.md#inspect-or-change-tool-values).

### ToolContext

Injects application `context`, native `submission` identity and `call_id` as a function's first parameter. The context parameter is absent from the model-visible schema. See [application context](tools.md#application-context).

`original_arguments` contains the received JSON values. `effective_arguments` contains the validated Python values, including accepted replacements. The `after` callback sees the arguments accepted for execution; validation can change their Python types without changing the received JSON.

### ActionRequested

Carries typed arguments and live authority for a pending manual call. `request.reply(value)` packages a candidate reply without consuming that authority. A direct Session caller uses `await request.respond(value)`. If output validation fails, that live request remains available for retry.

### ActionReply

The one-shot handler returns the matching request's candidate reply. The SDK checks its identity, validates and delivers it. An invalid reply fails the run and closes its Session.

### ApprovalRequest and ElicitationRequest

Both types carry a native request's `description`. For `ApprovalRequest`, return `request.reply("allow_once")` or `request.reply("deny")` from a one-shot handler, or call `await request.respond(decision)` in an explicit Session.

For an `ElicitationRequest` form, inspect `request.schema` for its fields and supported constraints. Return `request.reply(values)` from a one-shot handler, or call `await request.respond(values)` in an explicit Session. Decline with `request.reply(None)` or `await request.decline()`, respectively. A URL request has an empty schema and supports only refusal through these APIs. A Session-scoped URL request has no turn or submission ID; the SDK never visits its URL. Native replies acknowledge enqueueing only, not acceptance or completion. See [native interactions and their limits](tools.md#answer-interactions-during-a-one-shot-run).

### HttpMCP

Declares selected tools on a Streamable HTTP server:

```text
HttpMCP(*, name: str, tools: Sequence[str], url: str,
        approval_mode="auto", startup_timeout=10, execution_timeout=60,
        transform_result=None, transform_result_timeout=None,
        before=None, approval=None, defer_loading=False, bearer_token=None)
```

### StdioMCP

Declares selected tools on a server process:

```text
StdioMCP(*, name: str, tools: Sequence[str], command: str,
         args=(), env_vars=(), cwd=None, location="host",
         approval_mode="auto", startup_timeout=10, execution_timeout=60,
         transform_result=None, transform_result_timeout=None,
         before=None, approval=None, defer_loading=False)
```

`location="environment"` runs stdio on the Session's Environment. Neither MCP descriptor accepts `interceptor`. See [native MCP](tools.md#native-mcp) and [result policies](tools.md#mcp-result-policies).

For both descriptors, `transform_result_timeout=None` uses a 30-second deadline; it does not disable the deadline. An explicit value must be in `(0, 3600]` seconds and requires `transform_result`. This deadline is separate from tool execution and approval. `defer_loading=True` controls selected schema presentation when supported, not connection acquisition.

### MCPCallContext

Supplies context to MCP `before` and `approval`. `context.call.call_id` is `None` when native supplies no provider call ID.

### MCPResultContext

Supplies application context, native submission identity, `call_id`, `server_name` and `tool_name` to an MCP result policy.

### Environment

Connects the Session to an application-owned executor. Pass it to `agent.session()` or a one-shot run, outside the Agent's tools.

These calls accept an `EnvironmentSource`: either a borrowed `Environment` descriptor or an async context manager that yields one. See [managed executor lifetimes](tools.md#manage-the-executor-lifetime) for acquisition, cleanup and restore rules.

The `network` argument shown below requires the matching v1 source build. The historical private alpha.5 wheel does not support it.

```text
Environment(*, url: str, cwd: str, tools=(), headers={}, connect_timeout=None,
            approval=None, transform_arguments=None, transform_result=None,
            policy_timeout=30, capability_directories=None, network=None)
```

It selects `exec_command`, `write_stdin` and/or `apply_patch`; `write_stdin` requires `exec_command`. The default selects none. `approval` returns a bool or `StopTurn` and has no transform deadline. Approval sees original arguments before `transform_arguments`; denial never invokes the transform. `policy_timeout` bounds only transforms. Result transforms support direct executor calls in the default mode and calls from generated code with `Agent(programmatic_tool_calling=True)`. See [remote execution](tools.md#remote-execution) and [executor policies](tools.md#executor-policies).

`capability_directories` accepts absolute paths or `file:` URIs on the remote executor, not the Python controller. `None` keeps the native discovery defaults; an empty sequence selects no discovery roots.

`network` accepts an immutable `NetworkPolicy` or a dictionary with the same fields. An explicit policy requires `"exec_command"` in `tools` and a compatible Linux or macOS managed-proxy executor. Windows executors reject explicit policies in this revision. The Python controller can run on a different OS. See [network and local socket access](tools.md#network-and-local-socket-access).

On a Windows x64 executor, a job-local named pipe uses `network=None`. Pass the canonical workspace path supplied by the Windows provisioner as `cwd`. [Windows named pipes in an offline job](tools.md#windows-named-pipes-in-an-offline-job)

### NetworkPolicy

**Unreleased.** Configures the managed network policy for native commands:

```text
NetworkPolicy(*, allowed_domains=(), denied_domains=(),
              allow_all_unix_sockets=False, allow_local_binding=False)
```

The policy captures domain lists as immutable values. Denied domains take precedence over allowed domains. On a macOS executor, an empty policy grants no network destination or Unix socket access. On a Linux executor, the SDK rejects any explicit policy with `allow_all_unix_sockets=False` before a command starts. Omitting `Environment.network` or setting it to `None` preserves the default executor behavior.

On Linux and macOS executors, `allow_all_unix_sockets=True` grants access to every reachable Unix socket. Use this grant only on an executor private to the job. Keep every socket the job does not need out of reach. The SDK rejects grants for individual socket paths.

`allow_local_binding=True` enables local network access and changes local proxy routing. On a macOS executor, this setting can allow direct traffic to any reachable IP address on destination port 53. This direct traffic bypasses the domain rules, even when the payload is not DNS. If the job needs stricter limits, the application must enforce them with network isolation outside the executor.

The settings apply to native commands, not model requests, Python functions or MCP clients. Every explicit policy disables sandbox escalation. Restore requires the same normalized policy from current application code. See [network and local socket access](tools.md#network-and-local-socket-access) for platform and isolation limits.

### ExecutorContext

Supplies application context, the owning Python Session and Agent, and the actual native executor call identity. `agent_role` identifies a native child role, or is `None` for the root. `model` identifies the executing model when Codex supplies it. Direct calls set `call_id`; programmatic calls set cell/nested-call identities. Those fields grant no execution authority.

### NativeProvider

```text
NativeProvider(*, base_url=None, api_key=None, supports_websockets=None)
await provider.aclose() -> None
```

An omitted `base_url` uses `https://api.openai.com/v1`; `OPENAI_BASE_URL` is not read automatically. Custom endpoints must support the Responses protocol. An omitted key reads `OPENAI_API_KEY` when the Session opens. With `supports_websockets=None`, an explicit `base_url` defaults to HTTP; `base_url=None` permits WebSockets. Set `True` to permit WebSockets for a compatible custom provider or `False` to require HTTP. The native runtime selects the final transport when WebSockets are enabled. Close every Session using a custom provider before closing that provider. See [configuration](configuration.md).

### ModelSettings

Supports `reasoning_effort`, `reasoning_summary` and `service_tier`. Omission preserves native defaults; `service_tier=None` clears that override.

### WebSearch

`WebSearch(mode="cached", route="standalone")` enables native web search at Session opening. `mode` also accepts `"live"`; `route` also accepts `"hosted"` for the provider-hosted protocol. The provider must support the chosen route. Omission disables both routes. The declaration is fixed for the opening, and explicit selections must retain it. It does not grant executor network access. See [the tools guide](tools.md).

### Checklist and NativeGoals

Put `Checklist()` in the opening tools to enable native checklist updates, then read them with `await session.checklist()`. Put `NativeGoals()` in the opening tools of a `LocalStorage` Session to enable native goals, then inspect `await session.goal()`. The grants are separate. Redeclare `NativeGoals()` on local reopen; saved data alone grants no tools. Explicit selections must retain fixed planning declarations. Reopening does not itself start work. See [current checklists and goals](sessions.md#current-checklists-and-goals).

### Saved checklist and goal data

`agents.state` contains the value types used by older format 2 and 3 checkpoints.
They preserve saved data; restoring it grants no planning tools and starts no
work. See [retained checkpoint data](sessions.md#retained-checkpoint-data).

### NativeAgents

Enables Codex to delegate work to subagents. Include it in `Agent(tools=[...])` or the opening `agent.session(tools=[...])`. Use an explicit compatible model and a Session `Environment` with `exec_command`, `write_stdin` and `apply_patch` selected. Its optional `approval(context, call)` callback returns `bool` or `Awaitable[bool]` to allow or deny native collaboration calls. See [native children](tools.md#native-children) and [native tool decisions](tools.md#native-tool-decisions).

### NativeRole

Adds instructions and optional compatible model/reasoning settings. Built-in role names cannot be replaced.

### ChildInvocation

Identifies a live native child callback without granting wait or cancellation authority. Read a `Subagent.turn` to wait for observed child work.

### NativeToolCall

The native call received by `NativeAgents.approval`. Its input and result may contain sensitive data. See [native tool decisions](tools.md#native-tool-decisions).

## Typed input

### UserInput and UserMessage

`UserInput` is `str | UserMessage`. `UserMessage` is a dictionary with `role="user"` and string or ordered content. See [typed input](sessions.md#typed-input).

### InputText

A dictionary with `type="input_text"` and `text`.

### InputImage

A dictionary with `type="input_image"` and an `image_url` containing base64 image data as a URL, with optional `detail="auto"`.
