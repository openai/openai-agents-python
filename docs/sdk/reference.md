# API reference

Use this reference to look up supported calls, defaults and value types. `*` marks keyword-only arguments. Type parameters describe application context and final output. The linked task guides explain setup, behavior and limits.

Unless a section gives another import path, import the named types and functions from `agents`.

## Agent and one-shot runs

### Agent

An Agent declares instructions, tools and final output. Its constructor does not start the runtime. A Session captures the declaration when it opens. See [configuration](configuration.md).

```text
Agent(*, instructions: str, name=None, tools=None, model=None,
      model_settings=None, provider=None, output_type=None,
      programmatic_tool_calling=False)

agent.validate() -> None
agent.with_output_type(output_type) -> Agent
await agent.run(input, *, context=None, on_interaction=None,
                provider=None, model=None, model_settings=None, environment=None,
                tools=None, before_step=None, state=None, return_state=False,
                parent_trace=None, trace_options=None) -> Completed
agent.run_sync(input, *, context=None, on_interaction=None,
               provider=None, model=None, model_settings=None, environment=None,
               tools=None, before_step=None, state=None, return_state=False,
               parent_trace=None, trace_options=None) -> Completed
agent.session(*, context=None, state=None, storage=None, resume=None, provider=None, model=None,
              model_settings=None, environment=None, tools=None,
              instruction_files=(), reconciled_calls=(),
              parent_trace=None, trace_options=None)
              -> async context manager[Session]
```

The call options follow these rules:

- `instructions` and `input` are required. `input=None` is allowed only when a handler resumes a supported pending manual checkpoint.
- `on_interaction` returns the exact request's `reply(value)`, directly or through an awaitable. Sending the reply itself and returning `None` is invalid. See [manual requests](approvals.md#answer-interactions-during-a-one-shot-run).
- `return_state` must be a bool. `True` captures state after successful completion. A capture failure fails the run. Otherwise, `Completed.state` defaults to `None`.
- `output_type=None` or bare `str` returns text. `with_output_type(...)` creates a variant. There is no one-shot `output_type` argument. See [structured output](configuration.md#structured-output) for formats, validation and restore limits.

`validate()` returns `None` for a valid declaration or raises its first `TypeError` or `ValueError`. It opens no Session, reads no credentials and connects to no services. Context, environment, overrides and native model support still require opening-time checks. It produces no validation report.

Restore uses the checkpoint's model and settings. Any non-`None` opening `model` or `model_settings`, including `{}`, raises `ValueError`. Idle updates can change them after restore, except with `NativeAgents`. See [save and restore](state.md#save-and-restore-a-portable-checkpoint).

## Sessions and turns

### Session

A Session owns a native conversation, opened with `agent.session()` in an async context. See [conversations and state](sessions.md).

`session.codex_account` exposes the saved Codex account metadata when the Session uses `CodexLogin`. See [`CodexAccount`](#codexaccount).

#### Submission and control

| Call | Result |
| --- | --- |
| `await session.submit(input, *, output_type=None, tools=None, before_step=None, if_busy="reject", writable_roots=None, parent_trace=None, trace_options=None)` | Return a root Turn before completion. Busy handling defaults to rejection. `"queue"` adds first-in, first-out work. `"steer"` targets the captured busy root or starts a new root when idle, without overrides. On a supported local or Linux WSS executor, use [additional write roots](execution.md#replace-write-roots-between-turns) to replace grants for new work. |
| `session.rate_limits()` | Latest available `ApiRateLimits` snapshot. See [API-key limits](configuration.md#read-api-key-rate-limits). |
| `session.events(*, after=0, include_execution_details=False)` | Observe the Session until it ends, using the retained global observation sequence. Execution arguments and results require `include_execution_details=True` for a trusted reader. See [streaming](sessions.md#stream-observations). |
| `session.interactions()` | Receive live requests, separate from passive observations. |
| `await session.pending_interactions()` | Snapshot tuple of pending live requests. Inspection leaves them available for reply. |
| `await session.cancel_all(*, mode="force")` | Stop and join roots, descendants and queued work. See [interruption](sessions.md#interrupt-work). |
| `await session.update(*, model=None, model_settings=None, instructions=None, tools=None)` | Change one idle category. Tool updates replace admitted Python or manual names or MCP server/raw-tool pairs and set defaults. Temporary selections use the last accepted bindings. See [Session updates](sessions.md#update-tools-instructions-and-model-settings). |

`session.active_turn` is the current root `Turn[Any]`, or `None`. `session.restored_turn` retains a root restored at a pending manual boundary, even after later submissions. It is `None` for a new Session or idle restore. Both use `Any` because roots can override the Session's output type.

#### History and saved state

| Call | Result |
| --- | --- |
| `await session.compact(*, summary=None)` | Compact an idle Session. A nonempty supplied summary replaces model context without a model request and preserves canonical history. See [compaction limits](state.md#compact-conversation-context). |
| `await session.save_state()` | Capture an idle conversation without retiring it. |
| `await session.fork()` | Return independent `SessionState`, leaving the source usable. Workspace files are not copied. See [save and restore](state.md#save-and-restore-a-portable-checkpoint) for storage, descendant and programmatic-state limits. |
| `await session.suspend()` | Capture a supported manual boundary and retire the Session. See [manual suspension](state.md#save-a-settled-manual-request). |
| `await session.checkpoint_status(*, kind)` | Return `CheckpointStatus` with `status` (`"ready"`, `"not_ready"`, or `"unknown"`) and `reasons` for `kind="idle"` or `"manual_wait"`. It reserves nothing. Capture checks readiness again. |
| `session.items(*, subagent_id=None)` | Async iterator over a snapshot of recorded items, including provider call and output rows and [typed execution completions](sessions.md#inspect-completed-execution). |
| `session.turns(*, subagent_id=None)` | Async iterator over recorded turns and any [settings receipts](sessions.md#check-applied-instructions-and-settings). |
| `session.subagents()` | Async iterator over captured Subagent records. |

History and descendant iterators have no pagination arguments.

#### Planning and capabilities

| Call | Result |
| --- | --- |
| `await session.checklist(*, subagent_id=None)` | Latest root or descendant `ChecklistState`, or `None` before an update. Readback grants no planning tools. |
| `await session.goal()` | Actual `NativeGoal` data, or `None`, on a local Session opened with `NativeGoals`. Codex owns status and accounting. |
| `session.capabilities.get(feature)` | `FeatureSupport` with `status` (`"supported"`, `"limited"`, or `"unsupported"`), `reason` and `scope`. |
| `session.capabilities.require(feature, allow_limited=False)` | Raise `CapabilityError` for unsupported or, by default, limited coverage. |

`session.capabilities.websockets_allowed` reports provider permission, not negotiated transport. Capability reports describe configuration support. They grant no permissions and do not establish checkpoint readiness. See [conversations and state](sessions.md).

### before_step

The synchronous or asynchronous callback receives root `Turn` positionally and `step_index` by keyword. It returns `None` to keep the turn's tool selection, or a sequence of selectors for that preparation.

`step_index` counts preparations from zero. Cancellation can end a preparation before a model request. Selectors obey [tool-selection limits](tools.md#replace-tools-between-turns). Programmatic entry points are `ToolRef("exec", "functions")` and `ToolRef("wait", "functions")`. See the [per-step example](sessions.md#update-tools-instructions-and-model-settings).

### Turn

A root Turn's `get_result()` follows accepted continuations. A captured child Turn represents one exact native turn and does not follow later child turns. Child events or a new Subagent snapshot can identify a successor.

| Call | Result |
| --- | --- |
| `await turn.get_result()` | Stable result, unchanged by later submissions or Session closure. Cancelling the wait leaves work running. |
| `turn.events(*, after=0, include_execution_details=False)` | Observations for this Turn. Execution arguments and results require `include_execution_details=True` for a trusted reader. |
| `await turn.wait_for_request_or_result()` | `Completed` or `PendingRequests`. Its `requests` tuple contains this Turn's live requests without claiming ownership. See the [reply loop](approvals.md#wait-for-a-request-or-result). |
| `await turn.wait_for_attention()` | `Completed`, `PendingRequests`, or `TurnNeedsAttention`. See [supervisory waits](sessions.md#wait-for-a-decision-or-a-result). |
| `await turn.steer(input)` | Steer the captured active root or child turn. It cannot target another turn or revive a closed child. |
| `await turn.steer_exact(input, *, expected_turn_id)` | Steer the exact native root turn and return a `SteeringReceipt` upon acceptance. See [strict steering](sessions.md#steer-one-exact-root-turn). |
| `await turn.cancel(*, mode="force")` | Stop the root or withdraw it from the queue. Return `None` after cleanup. Children use `Subagent.try_cancel`, which returns a boolean; see [interruption](sessions.md#interrupt-work) for its confirmation and cleanup limits. |

Turn calls use the Session's opening event loop. After `run_sync()`, that loop has closed, so use `Completed` directly. IDs alone grant no control. See [Turn lifetime](sessions.md).

### Subagent

A `Subagent` record contains a captured descendant's identity, status, `is_live` and optional observed `turn`. A snapshot can become stale.

`await child.start(input)` checks that the child is still the same live instance and returns a `Turn[str]` only while the child is idle. To steer an active child turn, use `await child.turn.steer(input)`. It cannot create or resume a closed child. Manual replies use live requests, not input to this method. See [native children](delegation.md#native-children).

## Results and usage

### Completed

A `Completed` result contains `turn`, `final_output`, `usage`, optional `state` and `repair_attempts`. `turn` is the exact root or child `Turn` that produced the result. `state` is populated only when a one-shot run requests `return_state=True`.

Usage is a cumulative thread snapshot or `None`, not a per-turn cost. Root usage excludes children.

`repair_attempts` is a tuple of `RecordedTurn` receipts for opted-in prompted-output corrections; it is empty when no repair ran. Their per-turn usage is separate from `Completed.usage`, which still describes the original thread. Missing usage remains `None`. The final output may come from a repair without rewriting the original native history. See [structured output configuration](configuration.md).

### TokenUsageInfo and TokenUsage

Import these immutable data classes from `agents.usage`. `Completed.usage` is a `TokenUsageInfo` or `None`. `TokenUsageInfo` contains `total_token_usage: TokenUsage`, `last_token_usage: TokenUsage` and `model_context_window: int | None`.

`TokenUsage` contains integer fields `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` and `total_tokens`. Cached and reasoning counts are already included in the totals.

These values describe native thread snapshots. They do not report per-submission costs or billing records. See [usage readback and accounting limits](observability.md#read-usage-without-double-counting).

## Errors

### InteractionRequired

The run needs an application decision. A supported one-shot run using Python or manual tools can raise this error with a portable checkpoint in `state`. Unsupported automatic-save combinations have `state=None`. Always check it. Resuming a pending checkpoint without a handler retains the supplied checkpoint in `state`. See [manual continuation](approvals.md#continue-a-one-shot-manual-request) for the limits and resume call.

### NativeError

`NativeError` reports a native execution, parsing or lifecycle failure. An error does not prove that no effect occurred or that earlier effects were rolled back.

| Field | Meaning |
| --- | --- |
| `status_code` | Available provider HTTP status, or `None`. |
| `code` | Available upstream provider error code, or `None`. |
| `request_id` | Available provider request ID, or `None`. |
| `category` | Normalized native failure category, or `None`. |
| `effects` | `"not_started"`, `"unknown"`, or `"recorded"` for the exact failed operation. The default is `"unknown"`. |

Missing or discarded metadata is `None`. Messages and metadata may contain provider-supplied information. `effects="unknown"` does not establish that replay is safe. Even `"not_started"` describes only this failed operation, not earlier actions.

### Lifecycle errors

| Exception | Meaning |
| --- | --- |
| `SessionBusyError` | An idle-only start was requested while the Session owned active work. |
| `CapabilityError` | The requested operation is outside the Session's admitted capability. |
| `StaleTurnError` | The captured Turn cannot control the target or its current work. |
| `InteractionExpiredError` | The captured request no longer accepts a reply. |
| `TurnStoppedError` | The root Turn stopped and its owned cleanup finished. This is the work's result. Cancelling a wait for the result does not produce this error. Stopping the work does not promise rollback. |

## Saved conversations

### SessionState

Import from `agents.state`. Serialize with `to_json()` and load with `SessionState.from_json()` using trusted application storage. Restore requires the same pinned Codex revision that saved the checkpoint. Serializing it to JSON does not make it compatible with a different revision. The read-only `instructions` accessor derives accepted instructions from the stored history. It does not establish trust in their source. See [save and restore](state.md#save-and-restore-a-portable-checkpoint).

### LocalStorage

Import `LocalStorage` from `agents` and construct it with a `pathlib.Path`, for example `LocalStorage(Path("conversations"))`. Pass the profile as `agent.session(storage=profile)`. Keep `session.id` in application storage.

After closing the Session, reopen the conversation with `agent.session(storage=profile, resume=saved_id)` using the original profile directory and current tools, context and provider. Only one native writer can own the conversation at a time.

`resume` requires `storage`. Neither option can be used with portable `state`. A Session using local storage cannot export portable state through `save_state()`, `suspend()` or `fork()`.

An unknown call outcome raises `LocalRecoveryRequired`. Verify the effects of unresolved root calls, then supply `reconciled_calls` to reopen the original conversation. An active child whose runtime cannot be restored still blocks reopening. See [local recovery](state.md#recover-after-interruption).

Import `LocalRecoveryRequired` from `agents`. The exception carries `calls`, `turns`, and `items`. Each `UncertainCall` has a `reconcile(outcome=..., result=...)` method that carries over the original thread, turn, and call IDs. Set `outcome` to `"applied"` or `"not_applied"` after you verify the effect. Supply text for `result`. If you saved a confirmation in a journal, reconstruct it with `ReconciledCall` using the original IDs, outcome, and result.

## Tools and connection data

### function_tool

The decorator declares a Python function without starting a runtime. Input annotations are required; the return annotation is optional. A supplied return annotation still validates output, including resolved `T` from a synchronous function annotated `Awaitable[T]` or `Coroutine[Any, Any, T]`:

```text
@function_tool
@function_tool(name=None, description=None, callback_timeout=None,
               before=None, after=None, defer_loading=False)
```

Names and descriptions default to the function declaration. `callback_timeout=None` sets no callback body deadline. `defer_loading=True` lets supported models discover selected schemas when needed; other models receive them directly. It does not defer MCP connection startup or admit tools outside the selection. See [Python tools](tools.md#python-functions) and [notifications](tools.md#tool-notifications).

### FunctionTool

An immutable declaration with or without a registered Python callback. `function_tool` creates one from a Python function. `before` and `after` are flat callback fields; see [Python functions](tools.md#python-functions). A declaration without a callback uses manual results and cannot use callback settings:

```text
FunctionTool(*, name: str, description: str, input_type: type, output_type: type,
             defer_loading=False)
request = tool.match(event)
```

In a one-shot handler, return `request.reply(value)`. In an explicit Session, use `await request.respond(value)`. See [manual results](approvals.md#manual-tool-results).

### Tool

Use `Tool[Application]` to annotate collections that mix supported tool declarations.

### ToolRef

`ToolRef(name, namespace=None)` identifies a tool already available in the Session. Use `python_tool.ref` for a Python or manual function and `server.tool(raw_name)` for an MCP leaf. A selector uses the accepted binding. It does not replace a callback or reconnect a server. See [idle replacement and selection](tools.md#replace-tools-between-turns).

### ToolOutput

`ToolOutput.text(value)`, `ToolOutput.image(image_url)` and `ToolOutput.error(message)` construct native tool results. For mixed content, pass a list of `ToolOutputText` and `ToolOutputImage` items to `ToolOutput`. Images must be inline base64 image data URLs; remote URLs, file IDs and local paths are unsupported. `ToolOutput.error(...)` returns an explicit failed tool result (`success=False`), which differs from raising a Python exception. See [tool values](tools.md#inspect-or-change-tool-values).

### ToolContext

`ToolContext` supplies application `context`, native `submission` identity and `call_id` through a function's first parameter. The context parameter is absent from the model-visible schema. See [application context](tools.md#application-context).

`original_arguments` contains the received JSON values. `effective_arguments` contains the validated Python values, including accepted replacements. The `after` callback sees the arguments accepted for execution; validation can change their Python types without changing the received JSON.

## Manual results and interactions

### ActionRequested

An `ActionRequested` request carries typed arguments and permission to reply to a pending manual call. `request.reply(value)` prepares a candidate reply without sending it or preventing another reply. A direct Session caller uses `await request.respond(value)`. If output validation fails, the live request still accepts another reply.

### ActionReply

The one-shot handler returns the matching request's candidate reply. The SDK checks the reply's identity, validates its value and delivers it. An invalid reply fails the run and closes its Session.

### ApprovalRequest and ElicitationRequest

Both types carry a native request's `description`. For `ApprovalRequest`, return `request.reply("allow_once")` or `request.reply("deny")` from a one-shot handler, or call `await request.respond(decision)` in an explicit Session.

For an `ElicitationRequest` form, inspect `request.schema` for its fields and supported constraints. Return `request.reply(values)` from a one-shot handler, or call `await request.respond(values)` in an explicit Session. Decline with `request.reply(None)` or `await request.decline()`, respectively.

A URL request has an empty schema, plus `url` and `elicitation_id`. Return `request.reply_url_accept()` from a one-shot handler or use `await request.accept_url()` in an explicit Session to consent. Decline with `request.reply(None)` or `await request.decline()`.

In an explicit Session, after consent, `await request.wait_url_completion(timeout=30)` waits for the issuing server's completion notification. Keep the Session open while waiting. Consent does not establish completion, and a server notification does not independently verify the result.

A timeout expires the interaction and raises `TimeoutError`. Cancelling the waiter stops only that wait. A Session-scoped URL request has no turn or submission ID. The SDK never visits its URL. See [native interactions and their limits](approvals.md#answer-interactions-during-a-one-shot-run).

## MCP connections

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

`location="environment"` runs stdio on the Session's Environment. Neither MCP descriptor accepts `interceptor`. See [native MCP](mcp.md#connect-an-http-server) and [result policies](mcp.md#mcp-result-policies).

For both descriptors, `transform_result_timeout=None` uses a 30-second deadline; it does not disable the deadline. An explicit value must be in `(0, 3600]` seconds and requires `transform_result`. This deadline is separate from tool execution and approval. Where supported, `defer_loading=True` controls when the model sees selected schemas. It does not delay connection startup.

### MCPCallContext

Supplies context to MCP `before` and `approval`. `context.call.call_id` is `None` when native supplies no provider call ID.

### MCPResultContext

Supplies application context, native submission identity, `call_id`, `server_name` and `tool_name` to an MCP result policy.

## Execution environments

### Environment

Configures a local SDK executor or an application-owned WSS executor. Pass it to `agent.session()` or a one-shot run, outside the Agent's tools.

These calls accept an `EnvironmentSource`: either a borrowed `Environment` descriptor or an async context manager that yields one. See [managed executor lifetimes](execution.md#manage-the-executor-lifetime) for acquisition, cleanup and restore rules.

```text
Environment(*, url: str, cwd: str, tools=(), headers={}, connect_timeout=None,
            capability_directories=None, approval=None, transform_arguments=None,
            transform_result=None, policy_timeout=30, network=None, writable_roots=None)
Environment.local(*, cwd, tools=(), writable_roots=(), network=None,
                  capability_directories=None, approval=None, transform_arguments=None,
                  transform_result=None, policy_timeout=30) -> Environment
```

Environment options have these limits:

- `tools` selects any combination of `exec_command`, `write_stdin` and `apply_patch`, defaulting to none. `write_stdin` requires `exec_command`.
- `approval` returns bool or `StopTurn`, without a transform deadline. It sees original arguments. Denial skips `transform_arguments`. `policy_timeout` bounds only transforms.
- Result transforms support default-mode direct calls and generated calls with `Agent(programmatic_tool_calling=True)`. See [executor policies](execution.md#executor-policies).
- `capability_directories` contains absolute paths or `file:` URIs on the selected executor. `None` keeps native discovery defaults. An empty sequence selects no discovery roots.
- `network` accepts immutable `NetworkPolicy` or a same-field dictionary. Explicit policies require `"exec_command"` and a compatible Linux or macOS managed-proxy executor. Windows rejects them in this revision. The Python controller may run on another OS. See [network and local socket access](execution.md#network-and-local-socket-access).

Use [`Environment.local()`](execution.md#local-execution) on supported Linux and macOS hosts. `writable_roots` lists existing writable directories in addition to `cwd`. For WSS, pass a non-`None` sequence to enable the strict write policy. Use `writable_roots=[]` to grant writes only in `cwd`. A macOS WSS Environment keeps the roots you choose when opening the Session. See [per-turn write roots](execution.md#replace-write-roots-between-turns) for policy changes and restore.

For a Windows x64 job-local named pipe, omit `network` and `writable_roots`. `cwd` is the provisioner's canonical workspace path. See [Windows named pipes in an offline job](execution.md#windows-named-pipes-in-an-offline-job) and [remote execution](execution.md#remote-execution).

### NetworkPolicy

Configures the managed network policy for native commands:

```text
NetworkPolicy(*, allowed_domains=(), denied_domains=(),
              allow_all_unix_sockets=False, allow_local_binding=False)
```

The policy captures domain lists as immutable values. Denied domains take precedence over allowed domains. On a macOS executor, an empty policy grants no network destination or Unix socket access. On a Linux executor, the SDK rejects any explicit policy with `allow_all_unix_sockets=False` before a command starts. Omitting `Environment.network` or setting it to `None` preserves the default executor behavior.

On Linux and macOS executors, `allow_all_unix_sockets=True` grants access to every reachable Unix socket. Use this grant only on an executor private to the job. Keep every socket the job does not need out of reach. The SDK rejects grants for individual socket paths.

`allow_local_binding=True` enables local network access and changes local proxy routing. On a macOS executor, this setting can allow direct traffic to any reachable IP address on destination port 53. This direct traffic bypasses the domain rules, even when the payload is not DNS. If the job needs stricter limits, the application must enforce them with network isolation outside the executor.

The settings apply only to native commands. They do not apply to model requests, Python functions or MCP clients. Every explicit policy disables sandbox escalation. Restore requires the same normalized policy from current application code. See [network and local socket access](execution.md#network-and-local-socket-access) for platform and isolation limits.

### ExecutorContext

Supplies application context, the owning Python Session and Agent, and the actual native executor call identity. `agent_role` identifies a native child role, or is `None` for the root. `model` identifies the executing model when Codex supplies it. Direct calls set `call_id`. Programmatic calls set identities for the code cell and nested call. Those fields grant no execution authority.

## Model connections

### NativeProvider

```text
NativeProvider(*, base_url=None, api_key=None, supports_websockets=None, auth=None)
await provider.aclose() -> None
```

Use the [provider configuration guide](configuration.md#provider-connection) for connection defaults, WebSocket selection and provider lifetime. Custom endpoints require Responses support. Close every Session using the provider before calling `aclose()`.

### CodexLogin

```text
CodexLogin(*, codex_home: str | Path | None = None, account_id: str | None = None)
```

Import `CodexLogin` from `agents` and pass it as `NativeProvider(auth=CodexLogin())`. `codex_home` selects the existing file-backed Codex ChatGPT login directory; omission uses standard `CODEX_HOME` discovery. `account_id` requires the expected workspace ID. The native runtime owns token refresh for the same user and workspace. See [experimental Codex login reuse](configuration.md#reuse-a-codex-login-experimental) for setup and limits. The public beta and general availability of the Agents SDK may ship without this feature.

### CodexAccount

`session.codex_account` returns a `CodexAccount` with `account_id` and `plan_type`, or `None` in API-key mode. Import `CodexAccount` from `agents`. `account_id` identifies the saved workspace and is `None` for personal logins without a workspace ID. `plan_type` comes from saved login metadata. See [account identity, refresh, and usage](configuration.md#account-identity-refresh-and-usage).

### ModelSettings

Supports `reasoning_effort`, `reasoning_summary` and `service_tier`. Omitted settings keep native defaults. `service_tier=None` clears that override.

## Tracing

### TraceContext and trace options

`TraceContext(*, traceparent, tracestate=None)` holds validated W3C trace headers. `to_headers()` returns a new header mapping. Import `TraceContext` from `agents`. Pass it or a mapping containing `traceparent` and optional `tracestate` as `parent_trace` when opening a Session, running an Agent, or submitting a new root Turn. See [trace correlation](observability.md#continue-a-trace-across-workers) for inheritance and restore rules.

Import `TraceOptions` from `agents.telemetry` for the `trace_options` argument. These options control native tracing linkage and policy. A local `TraceOptions.parent` uses an SDK `SpanContext`; a `TraceContext` carries only W3C headers between workers. Neither type grants tool or workspace access.

## Native tools and delegation

### WebSearch

`WebSearch(mode="cached", route="standalone")` enables native web search at Session opening. `mode` also accepts `"live"`; `route` also accepts `"hosted"` for the provider-hosted protocol. The provider must support the chosen route. Omission disables both routes. The declaration is fixed for the opening, and explicit selections must retain it. It does not grant executor network access. See [the tools guide](tools.md).

### Checklist and NativeGoals

Put `Checklist()` in the opening tools to enable native checklist updates, then read them with `await session.checklist()`. Put `NativeGoals()` in the opening tools of a `LocalStorage` Session to enable native goals, then inspect `await session.goal()`. These declarations grant separate tools. Redeclare `NativeGoals()` when reopening a local conversation. Saved data alone grants no tools. Explicit selections must retain fixed planning declarations. Reopening does not itself start work. See [current checklists and goals](sessions.md#current-checklists-and-goals).

### Saved checklist and goal data

`agents.state` contains the value types used by older format 2 and 3 checkpoints. They preserve saved data. Restoring that data grants no planning tools and starts no work. See [retained checkpoint data](state.md#inspect-retained-planning-data).

### NativeAgents

Enables Codex to delegate work to subagents. Include it in `Agent(tools=[...])` or the opening `agent.session(tools=[...])`. Use an explicit compatible model and a Session `Environment` with `exec_command`, `write_stdin` and `apply_patch` selected.

The optional `approval(context, call)` callback returns `bool` or `Awaitable[bool]` to allow or deny native collaboration calls. `interactions=True` enables supported child approvals, MCP forms and URL requests; it defaults to `False`. See [native children](delegation.md#native-children), [child interaction limits](delegation.md#shared-tools-and-permissions) and [native tool decisions](delegation.md#native-tool-decisions).

### NativeRole

A `NativeRole` adds instructions and optional compatible model and reasoning settings. Built-in role names cannot be replaced.

### ChildInvocation

A `ChildInvocation` identifies a live native child callback without granting permission to wait for or cancel it. Read a `Subagent.turn` to wait for observed child work.

### NativeToolCall

A `NativeToolCall` is the native call received by `NativeAgents.approval`. Its input and result may contain sensitive data. See [native tool decisions](delegation.md#native-tool-decisions).

## Typed input

### UserInput and UserMessage

`UserInput` is `str | UserMessage`. `UserMessage` is a dictionary with `role="user"` and string or ordered content. See [typed input](sessions.md#typed-input).

### InputText

A dictionary with `type="input_text"` and `text`.

### InputImage

A dictionary with `type="input_image"` and an `image_url` containing base64 image data as a URL, with optional `detail="auto"`.
