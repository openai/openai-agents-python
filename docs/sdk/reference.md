# API reference

Use this reference to look up supported calls, defaults and value types. `*` marks keyword-only arguments. Type parameters describe application context and final output. The linked task guides explain setup, behavior and limits.

Unless a section gives another import path, import the named types and functions from `agents`.

## Agent and one-shot runs

### Agent

An Agent declares instructions, tools and final output. Its constructor does not start the runtime. A Session captures the declaration when it opens. See [configuration](configuration.md).

Matching development builds have also been supporting the `decisions` constructor option and `Agent.decide()` below. The `0.24.0-alpha.7` preview has not been including these APIs. The [typed decision configuration](configuration.md#make-typed-decisions) has been linking matching-source setup and complete examples.

```text
Agent(*, instructions: str, name=None, tools=None, model=None,
      model_settings=None, provider=None, decisions=None, output_type=None,
      programmatic_tool_calling=False, transform_context=None,
      before_compact=None, before_complete=None)

agent.validate() -> None
agent.with_output_type(output_type) -> Agent
await agent.decide(question, *, input, provider=None, choices=None,
                   output_type=None) -> Any
await agent.run(input, *, context=None, on_interaction=None,
                provider=None, model=None, model_settings=None, environment=None,
                tools=None, before_step=None, state=None, return_state=False,
                return_history=False,
                parent_trace=None, trace_options=None, prepare_request=None, budget=None) -> Completed
agent.run_sync(input, *, context=None, on_interaction=None,
               provider=None, model=None, model_settings=None, environment=None,
               tools=None, before_step=None, state=None, return_state=False,
               return_history=False,
               parent_trace=None, trace_options=None, prepare_request=None, budget=None) -> Completed
agent.session(*, context=None, state=None, storage=None, resume=None, provider=None, model=None,
              model_settings=None, environment=None, tools=None,
              instruction_files=(), discover_agents_md=False, reconciled_calls=(),
              parent_trace=None, trace_options=None, on_interaction=None)
              -> async context manager[Session]
```

Matching source builds have been supporting `discover_agents_md=False` on `agent.session()`. With `True`, native discovery has been loading trusted user and project instruction files. The [instruction guide](sessions.md#check-applied-instructions-and-settings) has been covering file selection, priority, and renewed opt-in on restore.

The call options follow these rules:

- `instructions` and `input` are required. `input=None` is allowed only when a handler resumes a supported pending manual checkpoint.
- `on_interaction` returns the exact request's `reply(value)`, directly or through an awaitable. Sending the reply itself and returning `None` is invalid. See [manual requests](approvals.md#answer-interactions-during-a-one-shot-run).
- `return_state` must be a bool. `True` captures state after successful completion. A capture failure fails the run. Otherwise, `Completed.state` defaults to `None`.
- `return_history=True` has been capturing conversation records in `Completed.history` after successful completion. See [original history](state.md#read-and-edit-original-history) for replay into another Session.
- `output_type=None` or bare `str` returns text. `with_output_type(...)` creates a variant. There is no one-shot `output_type` argument. See [structured output](configuration.md#structured-output) for formats, validation and restore limits.

`Agent.decide()` has been returning a boolean by default, a selected value with `choices=...`, or a validated typed value with `output_type=...`. Calls have been requiring explicit `input` and accepting at most one of `choices` and `output_type`. The call's `provider=` has been overriding the Agent's `decisions` provider, independently of the generative connection. These calls have not been starting Agent runs or reading conversation history. The [decision guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/decisions-guide.md) has been explaining callback helpers, provider ownership, errors and usage.

`validate()` returns `None` for a valid declaration or raises its first `TypeError` or `ValueError`. It opens no Session, reads no credentials and connects to no services. Context, environment, overrides and native model support still require opening-time checks. It produces no validation report.

Restore uses the checkpoint's model and settings. Any non-`None` opening `model` or `model_settings`, including `{}`, raises `ValueError`. Idle updates can change them after restore, except with `NativeAgents`. See [save and restore](state.md#save-and-restore-a-portable-checkpoint).

Matching development builds have also been supporting `Agent.handoffs`, `await session.get_active_agent()` and idle `await session.update(agent=target)`. The [Agent handoff guide](sessions.md#hand-off-the-conversation-to-another-agent) has been explaining captured targets, operation tool limits and restore through the original graph root.

## Sessions and turns

### Session

A Session owns a native conversation, opened with `agent.session()` in an async context. See [conversations and state](sessions.md).

`session.codex_account` exposes the saved Codex account metadata when the Session uses `CodexLogin`. See [`CodexAccount`](#codexaccount).

#### Submission and control

| Call | Result |
| --- | --- |
| `await session.submit(input, *, output_type=None, tools=None, before_step=None, if_busy="reject", writable_roots=None, parent_trace=None, trace_options=None, prepare_request=None, budget=None)` | Return a root Turn after local registration, before native admission or completion. Busy handling defaults to rejection. `"queue"` adds first-in, first-out work. `"steer"` targets the captured busy root or starts a new root when idle, without overrides. On a supported local or Linux WSS executor, use [additional write roots](execution.md#replace-write-roots-between-turns) to replace grants for new work. |
| `session.rate_limits()` | Latest available `ApiRateLimits` snapshot. See [API-key limits](configuration.md#read-api-key-rate-limits). |
| `session.events(*, after=0, include_execution_details=False)` | Observe the Session until it ends, using the retained global observation sequence. Execution arguments and results require `include_execution_details=True` for a trusted reader. See [streaming](sessions.md#stream-observations). |
| `session.interactions()` | Receive live requests, separate from passive observations. |
| `await session.respond(reply)` | Deliver a bound reply prepared by an exact live request. Preparing the reply does not reserve its request. See [retained Session handlers](approvals.md#handle-requests-for-a-retained-session). |
| `await session.pending_interactions()` | Snapshot tuple of pending live requests. Inspection leaves them available for reply. |
| `await session.aclose()` | Stop owned work and join cleanup. Retry through this same owner if cleanup fails. See [incomplete cleanup](sessions.md#retry-incomplete-cleanup). |
| `await session.cancel_all(*, mode="force")` | Stop and join roots, descendants and queued work. See [interruption](sessions.md#interrupt-work). |
| `await session.update(*, model=None, model_settings=None, instructions=None, tools=None)` | Change one idle category. Tool updates replace admitted Python or manual names or MCP server/raw-tool pairs and set defaults. Temporary selections use the last accepted bindings. See [Session updates](sessions.md#update-tools-instructions-and-model-settings). |

`session.active_turn` is the current root `Turn[Any]`, or `None`. `session.restored_turn` retains a root restored at a pending manual boundary, even after later submissions. It is `None` for a new Session or idle restore. Both use `Any` because roots can override the Session's output type.

#### History and saved state

| Call | Result |
| --- | --- |
| `await session.history_snapshot(*, limit=None)` | Matching source builds have been returning `CanonicalHistorySnapshot(revision, total, items)` for original JSON records while idle. `limit=0` has been returning only revision and count. See [original history](state.md#read-and-edit-original-history). |
| `await session.transact_history(*, expected_revision, keep, items=())` | Matching source builds have been retaining `keep` original records and replacing the suffix atomically, returning the accepted revision. Stale revisions have been failing before commit. See [transaction limits](state.md#read-and-edit-original-history). |
| `await session.model_context()` | Capture detached current model input while idle. Use the issued `ModelContextSnapshot` to [retain selected originals during compaction](state.md#retain-selected-context-items). |
| `await session.compact(*, summary=None, snapshot=None, retain=None)` | Compact an idle Session. A nonempty supplied summary replaces model context without a model request and preserves canonical history. See [compaction limits](state.md#compact-conversation-context). |
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

### get_cleanup

Import `get_cleanup` from `agents` for native failures and cancellation. `get_cleanup(error: BaseException) -> Session[Any, Any] | NativeProvider | SessionStoreCleanup | None` finds the retained cleanup owner through the error and its exception context. It returns the existing owner, or `None` when it finds no owner. Retain a returned owner until `await cleanup.aclose()` succeeds. Do not close the provider separately while a Session owns cleanup.

In `0.24.0-alpha.7` or later, a failed or cancelled `CustomStorage.reconcile()` can retain a `SessionStoreCleanup` owner, also exported from `agents`. Its `aclose()` retries release of the original storage lease without repeating reconciliation.

Use this helper instead of reading `error.cleanup` directly. On Python 3.10, a Task boundary can replace a `CancelledError` and leave the owner on an error in its context. Cancellation still propagates; the helper does not wrap or replace the error.

### PreparedRequest and prepare_request

Request editing requires `0.24.0-alpha.7` or later; use the [matching preview wheel](installation.md). Import `PreparedRequest` and `PreparedToolPresentation` from `agents`.

`prepare_request(turn, request, *, preparation_index, recovery_attempt)` may return a complete `PreparedRequest`, `None`, or an awaitable of either. The value contains `instructions: str`, `input: list[dict]`, and `tools: list[PreparedToolPresentation]`. Input uses native ResponseItem JSON. A tool presentation contains `namespace`, `name`, `kind`, `strict`, `defer_loading`, `description`, and `parameters`; only the last two can change. Edits affect sampling, leaving canonical history and execution authority unchanged. See [request-editing profiles and lifecycle limits](configuration.md#edit-the-next-sampling-request).

### CompletionContext, ContinueTurn, and CompactionContext

These types require `0.24.0-alpha.7` or later. `Agent.before_complete` receives `CompletionContext` and returns `None`, `ContinueTurn(instructions)`, or `StopTurn(...)`. See [proposed completion checks](configuration.md#check-a-proposed-completion) for callback timing and limits.

`Agent.before_compact` receives `CompactionContext` and returns a nonempty summary string or `None`. See [compaction callbacks](state.md#supply-summaries-through-a-callback) for retained history, fallback, and callback ownership.

### before_step

The synchronous or asynchronous callback receives root `Turn` positionally and `step_index` by keyword. It returns `None` to keep the turn's tool selection, a sequence of selectors for that preparation, or `StopTurn(...)` to stop the root before the next model request.

`step_index` counts preparations from zero. Cancellation can end a preparation before a model request. Selectors obey [tool-selection limits](tools.md#replace-tools-between-turns). With programmatic tool calling, the SDK includes `exec` and `wait` helpers for nonempty selections; an empty selection disables all tools. See the [per-step example](sessions.md#select-tools-before-each-model-step).

### Turn

A root Turn has a stable `submission_id` after local registration. Its `turn_id` can be `None` until native admission produces `TurnStarted`. A withdrawal before admission can leave no native history or events. See [submission acknowledgment](sessions.md#queue-or-steer-active-work).

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

Observation `after` cursors apply only to the same live Session and reset on restore or reopen. A Turn iterator ends at owned settlement; use the Session stream for later background completions. See [observation cursor lifetime](sessions.md#reconnect-an-observer).

Turn calls use the Session's opening event loop. After `run_sync()`, that loop has closed, so use `Completed` directly. IDs alone grant no control. See [Turn lifetime](sessions.md).

### Subagent

A `Subagent` record contains a captured descendant's identity, status, `is_live` and optional observed `turn`. A snapshot can become stale.

`await child.start(input)` checks that the child is still the same live instance and returns a `ChildTurn` only while the child is idle. To steer an active child turn, use `await child.turn.steer(input)`. It cannot create or resume a closed child. Manual replies use live requests, not input to this method. See [native children](delegation.md#native-children).

`ChildTurn` exposes result waits, events, identifiers and ordinary steering. It does not advertise root-only `cancel()` or `steer_exact()`. Use `Subagent.try_cancel(child_turn)` for the weaker native child interruption guarantee. Child results retain the same `ChildTurn` view in `Completed.turn`.

## Results and usage

### Completed

A `Completed` result contains `turn`, `final_output`, `thread_usage`, `repair_attempts`, and optional `state` and `history`. `turn` is the exact root `Turn` or supported `ChildTurn` view that produced the result. One-shot runs have been populating `state` with `return_state=True` and `history` with `return_history=True`.

Usage is a cumulative thread snapshot or `None`, not a per-turn cost. Root usage excludes children.

`repair_attempts` is a tuple of `RecordedTurn` receipts for opted-in prompted-output corrections; it is empty when no repair ran. Their per-turn usage is separate from `Completed.thread_usage`, which still describes the original thread. Missing usage remains `None`. The final output may come from a repair without rewriting the original native history. See [structured output configuration](configuration.md).

### RecordedTurn and RecordedResponse

Import these history value types from `agents.history`. In `0.24.0-alpha.7` or later, a `RecordedTurn` can include `responses`, a list of retained `RecordedResponse` occurrences. Each occurrence has origin `thread_id`/`turn_id`, optional observation `id` and provider `response_id`, `usage_source`, and available usage or terminal facts. `usage_source` is `"received"` or `"native_normalized"`; missing counters are unknown and differ from zero. Optional `status` and `incomplete_reason` retain reported terminal facts. Occurrence `id` differs from a provider `response_id`, which can repeat. Missing response coverage is unknown, and retained responses do not prove SDK acceptance or billing. See [response-attempt inspection](sessions.md#inspect-response-attempts) and the [matching preview wheel](installation.md).

Live `ModelRequestDispatching` and `ModelResponseLinked` observations come from `agents.observation`. They describe resolved sampling fields and best-effort links to retained occurrences. Dispatch can precede encoding failure or zero socket writes; links can be lost. Neither event proves provider receipt or success. Follow the same response-attempt guide for correlation and coverage limits.

### TokenUsageInfo and TokenUsage

Import these immutable data classes from `agents.usage`. `Completed.thread_usage` is a `TokenUsageInfo` or `None`. `TokenUsageInfo` contains `total_token_usage: TokenUsage`, `last_token_usage: TokenUsage` and `model_context_window: int | None`.

`TokenUsage` contains integer fields `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` and `total_tokens`. Cached and reasoning counts are already included in the totals.

These values describe native thread snapshots. They do not report per-submission costs or billing records. See [usage readback and accounting limits](observability.md#read-usage-without-double-counting).

### RunBudget

Import `RunBudget` from `agents` or `agents.usage` in `0.24.0-alpha.7` or later:

```text
RunBudget(max_requests=None, max_tokens=None, max_estimated_cost_micros=None,
          estimate_cost_micros=None, missing_usage="stop")
```

Pass `budget` to a one-shot run or `Session.submit()`. At least one limit is required; each limit is an integer from zero through `2**53 - 1`. The SDK checks limits before the next root sampling admission. An admitted response can overshoot a token or estimated cost limit. See [budget accounting, errors, and checkpoint limits](observability.md#limit-a-runs-model-requests-and-usage).

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
| `response_integrity_reason` | Read-only `ResponseIntegrityReason` or `None` for the captured originating model-output integrity failure. Available in `0.24.0-alpha.7` or later; see [integrity diagnostics](sessions.md#diagnose-response-integrity-failures) and use the [matching preview wheel](installation.md). |
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

In `0.24.0-alpha.7` or later, `LocalStorage` rejects `Agent(programmatic_tool_calling=True)` before opening because nested tool calls lack durable intent/result receipts. Use the [matching preview wheel](installation.md) and [storage support limits](state.md#choose-how-to-store-a-conversation).

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

`ToolOutput.text(value)`, `ToolOutput.image(image_url)` and `ToolOutput.error(message)` construct native tool results. For mixed content, pass a list of `ToolOutputText` and `ToolOutputImage` items to `ToolOutput`. In `0.24.0-alpha.7`, images must be inline base64 image data URLs; remote URLs, file IDs, and local paths are unsupported. `ToolOutput.error(...)` returns an explicit failed tool result (`success=False`), which differs from raising a Python exception. See [tool values](tools.md#inspect-or-change-tool-values).

Matching development builds also support HTTP(S) image URLs, provider image IDs, and optional image `detail` through `ToolOutput.image(...)` or `ToolOutputImage`. Supply exactly one of `image_url` and `file_id`. Optional `detail` accepts `None`, `"low"`, `"high"`, `"auto"`, or `"original"`. HTTP(S) URLs and image file IDs require API-key authentication and a compatible standard Responses model; `CodexLogin` and Responses Lite do not support these references. The helpers do not fetch URLs or upload files. Local filesystem paths are unsupported.

`ToolOutput.file(...)` and `ToolOutputFile` accept exactly one of `file_id`, base64 `file_data`, or HTTPS `file_url`, with optional `filename`. Mixed results can include `ToolOutputFile` items. See [tool-result media](tools.md#return-text-images-and-tool-errors) for source validation, model limits, and examples. These additions are absent from the alpha.7 wheel.

### ToolContext

`ToolContext` supplies application `context`, native `submission` identity and `call_id` through a function's first parameter. The context parameter is absent from the model-visible schema. See [application context](tools.md#application-context).

`original_arguments` contains the received JSON values. `effective_arguments` contains the validated Python values, including accepted replacements. The `after` callback sees the arguments accepted for execution; validation can change their Python types without changing the received JSON.

## Manual results and interactions

### ActionRequested

An `ActionRequested` request carries typed arguments and permission to reply to a pending manual call. `request.reply(value)` prepares a candidate reply without sending it or preventing another reply. A direct Session caller uses `await request.respond(value)`. If output validation fails, the live request still accepts another reply.

### ActionReply

The one-shot handler returns the matching request's candidate reply. The SDK checks the reply's identity, validates its value and delivers it. An invalid reply fails the run and closes its Session.

### ApprovalRequest, FormRequest and UrlRequest

All three request types carry a native request's `description`. For `ApprovalRequest`, return `request.reply("allow_once")` or `request.reply("deny")` from a one-shot handler, or call `await request.respond(decision)` in an explicit Session.

For a `FormRequest`, inspect `request.schema` for its fields and supported constraints. Return `request.reply(values)` from a one-shot handler, or call `await request.respond(values)` in an explicit Session. Decline with `request.reply(None)` or `await request.decline()`, respectively.

Standard forms can use primitive or general schemas, including nested objects and arrays on the general route; see [form schema support](approvals.md#handle-native-approvals-and-forms) for validation limits.

A `UrlRequest` exposes `url` and `elicitation_id`, without form methods or a `schema` field. Return `request.reply_url_accept()` from a one-shot handler or use `await request.accept_url()` in an explicit Session to consent. Decline with `request.reply(None)` or `await request.decline()`.

In an explicit Session, after consent, `await request.wait_url_completion(timeout=30)` waits for the issuing server's completion notification. Keep the Session open while waiting. Consent does not establish completion, and a server notification does not independently verify the result.

A timeout raises `TimeoutError` and stops only that observer. Cancelling the waiter also stops only that wait. Call `await request.cancel_url_completion()` to expire the shared lifecycle; other completion waiters then receive `InteractionExpiredError`. A Session-scoped URL request has no turn or submission ID. The SDK never visits its URL. See [native interactions and their limits](approvals.md#answer-interactions-during-a-one-shot-run).

### VerificationProof and VerificationRequest

These types serve a separately provisioned trusted verification service; SDK support does not establish service eligibility. `VerificationProof(credential_id, signature)` carries the signer's result. Submission requires a nonempty credential ID of at most 1024 UTF-8 bytes and canonical unpadded base64url encoding of 1 through 128 signature bytes.

`VerificationRequest.challenge` contains the exact immutable bytes to sign. `title` is service display text, and `server_name` is the application's registered service alias. A one-shot handler returns `request.reply(proof)` or `request.reply(None)` to decline. An explicit Session uses `await request.respond(proof)` or `await request.decline()`. Enqueue acknowledgment does not establish service acceptance. See [verification challenges](approvals.md#answer-a-verification-challenge).

## MCP connections

### HttpMCP

Declares selected tools on a Streamable HTTP server:

```text
HttpMCP(*, name: str, tools: Sequence[str], url: str,
        approval_mode="native", startup_timeout=10, execution_timeout=60,
        transform_result=None, transform_result_timeout=None,
        before=None, approval=None, defer_loading=False, bearer_token=None)
```

`headers=None` is an additional option in the forthcoming preview and is absent from the `0.24.0-alpha.7` wheel. It captures custom HTTP fields for this MCP connection. Use either an `Authorization` header or `bearer_token`. See [custom MCP headers](mcp.md#connect-an-http-server).

### StdioMCP

Declares selected tools on a server process:

```text
StdioMCP(*, name: str, tools: Sequence[str], command: str,
         args=(), env_vars=(), cwd=None, location="host",
         approval_mode="native", startup_timeout=10, execution_timeout=60,
         transform_result=None, transform_result_timeout=None,
         before=None, approval=None, defer_loading=False)
```

`env=None` is an additional option in the forthcoming preview and is absent from the `0.24.0-alpha.7` wheel. It captures explicit process variables, which override names selected by `env_vars`. See [stdio process configuration](mcp.md#start-a-stdio-server).

`location="environment"` runs stdio on the Session's local or remote Environment. Neither MCP descriptor accepts `interceptor`. See [native MCP](mcp.md#connect-an-http-server) and [result policies](mcp.md#mcp-result-policies).

For both descriptors, `transform_result_timeout=None` uses a 30-second deadline; it does not disable the deadline. An explicit value must be in `(0, 3600]` seconds and requires `transform_result`. This deadline is separate from tool execution and approval. Where supported, `defer_loading=True` controls when the model sees selected schemas. It does not delay connection startup.

### HostedVerificationMCP and ChatGPTAppsMCP

These integrations require separately provisioned service access and credentials; they are not a general service-availability promise. Both use the fixed server name `codex_apps`. `HostedVerificationMCP` accepts the other `HttpMCP` options, including selected tools, trusted service `url`, and optional `bearer_token`. The application supplies its trusted signer and enrollment. Use a new Session to replace the fixed registration.

`ChatGPTAppsMCP` requires selected `tools`, an explicit first-party ChatGPT `access_token`, and its exact `account_id`. Its URL is fixed, and it accepts no `bearer_token` override. Credentials are separate from model-provider authentication. It does not read a saved login or refresh tokens; open a fresh Session to replace credentials. See [service-specific MCP integrations](mcp.md#service-specific-integrations).

### MCPCallContext

Supplies context to MCP `before` and `approval`. `context.call.call_id` is `None` when native supplies no provider call ID. The synchronous or asynchronous `approval(context, call)` returns exactly `bool` or `StopTurn`, without a transform deadline. See [MCP policies](mcp.md#mcp-result-policies).

### MCPResultContext

Supplies application context, native submission identity, `call_id`, `server_name` and `tool_name` to an MCP result policy.

## Execution environments

### Environment

Configures a local SDK executor or an application-owned WSS executor. Pass it to `agent.session()` or a one-shot run, outside the Agent's tools.

These calls accept an `EnvironmentSource`: either a borrowed `Environment` descriptor or an async context manager that yields one. See [managed executor lifetimes](execution.md#manage-the-executor-lifetime) for acquisition, cleanup and restore rules.

```text
Environment(*, url: str, cwd: str, tools=(), headers={}, connect_timeout=None,
            capability_directories=None, approval=None, transform_arguments=None,
            transform_result=None, policy_timeout=30, network=None, writable_roots=None,
            mounts=(), write_policy="strict")
Environment.local(*, cwd, tools=(), writable_roots=(), network=None,
                  capability_directories=None, approval=None, transform_arguments=None,
                  transform_result=None, policy_timeout=30, mounts=(),
                  write_policy="strict") -> Environment
```

`read_only=False` is an additional option on both constructors in the forthcoming preview and is absent from the `0.24.0-alpha.7` wheel. Set it to `True` for a strict read-only workspace, with no `apply_patch`, `NativeAgents`, or additional write roots. Python callbacks and MCP services keep their own permissions. See [read-only execution](execution.md#inspect-a-read-only-workspace).

Environment options have these limits:

- `tools` selects any combination of `exec_command`, `write_stdin`, `apply_patch`, `project_glob` and `project_grep`, defaulting to none. `write_stdin` requires `exec_command`.
- See [executor tool selection](execution.md#select-executor-tools) for project search and its runtime constraints.
- `approval` returns bool or `StopTurn`, without a transform deadline. It sees original arguments. Denial skips `transform_arguments`. `policy_timeout` bounds only transforms.
- Result transforms support default-mode direct calls and generated calls with `Agent(programmatic_tool_calling=True)`. Result control also withholds raw command-output deltas and output fields in controlled execution records. See [executor policies](execution.md#transform-executor-arguments-and-results).
- `capability_directories` contains absolute paths or `file:` URIs on the selected executor. `None` keeps native discovery defaults. An empty sequence selects no discovery roots. See [native skill setup](execution.md#supply-native-skills) for the `skills/NAME/SKILL.md` layout and restore rules.
- `network` accepts immutable `NetworkPolicy` or a same-field dictionary. Explicit policies require `"exec_command"` and a compatible Linux or macOS managed-proxy executor. Windows rejects them in this revision. The Python controller may run on another OS. See [network and local socket access](execution.md#network-and-local-socket-access).

Use [`Environment.local()`](execution.md#local-execution) on supported Linux and macOS hosts. `writable_roots` lists existing writable directories in addition to `cwd`. New local and remote Environments default to `write_policy="strict"`. Omitting roots or using `writable_roots=[]` grants writes in `cwd` only. Remote-only `write_policy="legacy"` retains the previous implicit WSS policy and rejects supplied roots. A macOS WSS Environment keeps the roots you choose when opening the Session. See [per-turn write roots](execution.md#replace-write-roots-between-turns) for policy changes and restore.

For a Windows x64 job-local named pipe, explicitly select `write_policy="legacy"` and omit `network` and `writable_roots`. `cwd` is the provisioner's canonical workspace path. See [Windows named pipes in an offline job](execution.md#windows-named-pipes-in-an-offline-job) and [remote execution](execution.md#remote-execution).

### LocalStorageMount

In `0.24.0-alpha.7` or later, `LocalStorageMount(source, destination=None, strategy="local_bind", read_only=True)` adds an existing same-path directory to a Linux local executor's read-only mounts. Pass `LocalStorageMount(source=path)` in `Environment.local(mounts=[...])`. Destination defaults to source, strategy defaults to `"local_bind"`, and `read_only` must remain `True`. Existing explicit calls remain valid; `read_only=False` is unsupported. See [read-only directory requirements and restore limits](execution.md#add-read-only-directories-on-linux).

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

Use the [provider configuration guide](configuration.md#provider-connection) for connection defaults, WebSocket selection and provider lifetime. The default transport has been requiring Responses support from custom endpoints. Matching source builds have also been accepting `model_backend` with [application models](#application-models). Close every Session using the provider before calling `aclose()`.

### Application models

These APIs have been requiring a [matching source build](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md#build-the-sdk-from-source); the alpha.7 wheel has not been including them. The [application-client guide](configuration.md#use-an-application-owned-model-client) has been providing a complete example and lifecycle limits.

```text
NativeProvider(..., model_backend: NativeModel | ModelProvider | None = None)
ModelRequest(body: dict[str, Any], context: Any, session_id: str, turn_id: str)
ModelResponse(output: list[dict[str, Any]], usage: dict[str, int] | None = None,
              response_id: str = <generated>)
NativeModel.complete(request: ModelRequest) -> ModelResponse | Awaitable[ModelResponse]
ModelProvider.get_model(model_name: str) -> NativeModel
ChatCompletionsModel(client, model=None, *, prompt_cache_key=None)
ResponsesModel(client, model=None, *, prompt_cache_key=None)
LitellmModel(client, model=None, *, prompt_cache_key=None)
AnyLLMModel(client, model=None, *, prompt_cache_key=None)
```

The runtime has been accepting synchronous or asynchronous `complete()` methods. The packaged client adapters have been requiring asynchronous clients and complete responses. `ModelResponse.output` has been using Responses item JSON. `usage` has been accepting nonnegative native token counters: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`, and `total_tokens`. Omitted usage has been representing unknown counts.

### CodexLogin

```text
CodexLogin(*, codex_home: str | Path | None = None, account_id: str | None = None)
```

Import `CodexLogin` from `agents` and pass it as `NativeProvider(auth=CodexLogin())`. `codex_home` selects the existing file-backed Codex ChatGPT login directory; omission uses standard `CODEX_HOME` discovery. `account_id` requires the expected workspace ID. The native runtime owns token refresh for the same user and workspace. See [experimental Codex login reuse](configuration.md#reuse-a-codex-login-experimental) for setup and limits. The public beta and general availability of the Agents SDK may ship without this feature.

### CodexAccount

`session.codex_account` returns a `CodexAccount` with `account_id` and `plan_type`, or `None` in API-key mode. Import `CodexAccount` from `agents`. `account_id` identifies the saved workspace and is `None` for personal logins without a workspace ID. `plan_type` comes from saved login metadata. See [account identity, refresh, and usage](configuration.md#account-identity-refresh-and-usage).

### ModelSettings

Supports `reasoning_effort`, `reasoning_summary`, `service_tier`, and, in `0.24.0-alpha.7` or later, `tool_choice`. Omitted settings keep native defaults. `service_tier=None` clears that override. `tool_choice` accepts `"auto"`, `"none"`, or `"required"`; see [model settings](configuration.md#model-and-session-settings).

Matching development builds also accept a `ToolRef` to require one selected tool with `defer_loading=False`. They add sampling, cache, storage, and model invocation controls. `None` clears these SDK overrides. See [development model controls](configuration.md#additional-controls-in-development-builds) for supported values and provider restrictions. These additions do not establish alpha.7 wheel availability.

## Tracing

### TraceContext and trace options

`TraceContext(*, traceparent, tracestate=None)` holds validated W3C trace headers. `to_headers()` returns a new header mapping. Import `TraceContext` from `agents`. Pass it or a mapping containing `traceparent` and optional `tracestate` as `parent_trace` when opening a Session, running an Agent, or submitting a new root Turn. See [trace correlation](observability.md#continue-a-trace-across-workers) for inheritance and restore rules.

Import `TraceOptions` from `agents.telemetry` for the `trace_options` argument. These options control native tracing linkage and policy. A local `TraceOptions.parent` uses an SDK `SpanContext`; a `TraceContext` carries only W3C headers between workers. Neither type grants tool or workspace access.

### Native workflow scopes and TraceOptions

These APIs require `0.24.0-alpha.7` or later; install a [matching preview wheel](installation.md). Import them from `agents.telemetry`.

```text
trace(name, *, options=None) -> TraceScope
span(name, *, options=None) -> TraceScope
current_context() -> SpanContext | None
TraceOptions(*, parent=None, detached=False, trace_id=None, group_id=None,
             metadata={}, disabled=None, include_sensitive_data=None,
             export_credentials=None)
TraceExportCredentials(*, headers)
```

Use a `TraceScope` as a synchronous context manager, or call `start()` and `end()` in the same task. `context` supplies the local `SpanContext` after entry. `set_outcome(status=..., category=None)` accepts `"ok"`, `"error"`, or `"cancelled"`; category is an optional short ASCII identifier. New workflows default to metadata-only. Disabled and content restrictions are inherited even when linkage is detached. See [local trace scopes](observability.md#group-work-in-a-local-trace) for lifetime and parent selection.

### Native processors and delivery

Processor registration, `force_flush`, and shutdown budgets require `0.24.0-alpha.7` or later. Use the same [matching preview wheel](installation.md) and `agents.telemetry` namespace.

```text
await configure(*, traces=None, logs=None, metrics=None, environment="dev",
                span_attributes=None, log_user_prompt=False,
                log_agent_responses=False, tool_result_max_bytes=0, processors=())
add_trace_processor(processor) -> None
set_trace_processors(processors) -> None
await force_flush(*, timeout_ms=10000) -> FlushResult
await shutdown(*, timeout_ms=None) -> None
```

A processor implements `on_trace_event(event: TraceLifecycleEvent)` synchronously or asynchronously. Add preserves the native exporter; set replaces future routes, including that exporter. `FlushResult` has `status`, `dropped_records`, `failed_deliveries`, and `barrier`. Flush success does not prove storage or viewer visibility. See [custom sinks and delivery limits](observability.md#send-native-traces-to-an-application-sink).

## Native tools and delegation

### WebSearch

`WebSearch(mode="cached", route="standalone", allowed_domains=None, blocked_domains=None, location=None)` enables native web search at Session opening. `mode` also accepts `"live"`; `route` also accepts `"hosted"` for the provider-hosted protocol. The provider must support the chosen route. Omission disables both routes. The declaration is fixed for the opening. In `0.24.0-alpha.7` or later, explicit selections can omit it to disable search for that operation or preparation. Domain filters and approximate location require the hosted route and provider support. It does not grant executor network access. See [the tools guide](tools.md#native-web-search).

### Checklist and NativeGoals

Put `Checklist()` in the opening tools to enable native checklist updates, then read them with `await session.checklist()`. Put `NativeGoals()` in the opening tools of a `LocalStorage` Session to enable native goals, then inspect `await session.goal()`. These declarations grant separate tools. Redeclare `NativeGoals()` when reopening a local conversation. Saved data alone grants no tools. In `0.24.0-alpha.7` or later, explicit selections can omit a planning declaration to disable its tools for that operation or preparation. Reopening does not itself start work. See [current checklists and goals](sessions.md#current-checklists-and-goals).

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

A dictionary with `type="input_image"` and an `image_url` containing base64 image data as a URL, with optional `detail="auto"`, for the original preview profile.

Matching development builds also accept absolute HTTP(S) image URLs and `detail` values of `None`, `"low"`, `"high"`, or `"original"`, subject to native model support. HTTP(S) URLs have the same [route and model limits as image results](tools.md#return-text-images-and-tool-errors). The distinct `InputImageFile` type uses a provider-owned image `file_id`. `InputFile` uses exactly one of `file_id`, `file_data`, or HTTPS `file_url`. These types can appear in `UserMessage.content`; see [development image and file input](sessions.md#images-and-files-in-development-builds) for validation and provider limits. These additions do not establish alpha.7 wheel availability.

## Existing MCP clients

`agents.migration.mcp` provides the independent `MCPNamespace` and `select_mcp_tools` adapter for application-owned classic MCP clients. It returns native function tools; it does not run classic Agents or Models. See [client ownership and policy limits](mcp.md#reuse-an-application-owned-mcp-client).

Import native cleanup recovery as [`agents.get_cleanup`](#get_cleanup).
