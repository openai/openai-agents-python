# Preview changelog

Release notes for previews of the OpenAI Agents SDK. Versions alpha.5 and earlier were distributed through the earlier preview program; their release tags are not hosted in this repository.

## Unreleased

## 0.24.0-alpha.7 (2026-11-08)

### Highlights

- **Use Classic workflows alongside native execution.** Import the retained v0 runtime through `agents.classic`. This alpha also changes executor write defaults; review the upgrade actions before opening existing environments.
- **Keep conversations in your own storage.** Add durable root Sessions with `CustomStorage`, and reconcile uncertain tool effects before resuming work.
- **Add voice and hosted tools to native agents.** Connect speech workflows to a Session, or use `FileSearch`, `CodeInterpreter`, and `ImageGeneration` through compatible Responses providers.
- **Control context, completion, and usage.** Transform model input, retain selected context during compaction, review proposed answers, and limit observed root usage.

### Upgrading from 0.24.0-alpha.6

- **Review Environment write policies.** New Environments default to `write_policy="strict"`, which requires a compatible Linux or macOS executor. The working directory remains writable; use `writable_roots` for additional directories. To retain the previous remote policy, including on Windows executors, set `write_policy="legacy"` and leave `writable_roots=None`. Local execution requires strict mode. When restoring legacy remote checkpoints or profiles, select legacy explicitly; reopening does not upgrade their policy. See the [executor policy and restore rules](https://github.com/openai/openai-agents-python/blob/4c1190046163c09e4f67ab7254f22bea60b14894/docs/sdk/state.md#restore-executor-policy).
- **Check implicit reasoning settings.** The bundled model catalog changes the default reasoning effort from `low` to `medium` for `gpt-6-astra`, `gpt-6.1-sol`, and `gpt-5.6-sol`. Set `model_settings={"reasoning_effort": "low"}` explicitly if you need the previous effort. This describes bundled metadata; an explicit setting or provider model metadata can select another value. (#5517)
- **Rename MCP approval modes.** Replace `approval_mode="auto"` with `"native"`, and `"approve"` with `"preapproved"`. The old values are rejected; the new names preserve their respective approval behavior.
- **Update child step callbacks.** `NativeAgents.before_step` now receives one `ChildStepContext`, with `context`, `child`, and `step_index`, instead of separate child and step-index arguments. Root `before_step` callbacks keep their existing signature.
- **Handle MCP forms and URLs separately.** `ElicitationRequest` is now the union `FormRequest | UrlRequest`. Use `FormRequest.reply()` or `respond()` for forms; use `UrlRequest.reply_url_accept()` or `accept_url()` for URL consent.

### Added

- **Application-owned conversation storage.** `SessionStore`, `SQLiteSessionStore`, and `CustomStorage` persist root history and automatic Python tool results. This initial profile excludes native tools, Environments, children, planning tools, generated-code execution, manual tools, and portable checkpoints. `SQLiteSessionStore` requires one host and a local POSIX filesystem. Both local profiles and custom stores can report uncertain root calls for explicit reconciliation; neither automatically replays those effects. (#5514, #5502)
- **Provider-hosted tools and storage.** Add `FileSearch`, `CodeInterpreter`, and `ImageGeneration`, Code Interpreter caller policies, and provider-managed storage. Hosted tools require an API-key provider and a compatible standard Responses model; they exclude Responses Lite and Codex login. Hosted web search adds domain and location controls and retains citations. (#5576, [hosted-tool guide](https://github.com/openai/openai-agents-python/blob/4c1190046163c09e4f67ab7254f22bea60b14894/docs/sdk/tools.md#provider-hosted-tools))
- **Context and completion hooks.** `Agent.transform_context` changes sampled input without changing stored history. `before_compact` and selected-item retention control compaction. `before_complete` can accept, return `ContinueTurn`, or stop a proposed answer; rejected text may already have streamed and remains in history. Advanced `prepare_request` editing supports a restricted HTTP profile. See the [hook contracts and limits](https://github.com/openai/openai-agents-python/blob/4c1190046163c09e4f67ab7254f22bea60b14894/docs/sdk/configuration.md#choose-a-model-context-hook).
- **Observed usage budgets.** `RunBudget` limits root request admissions, reported tokens, or estimated cost before the next request. An admitted request can exceed a limit. Children are excluded; missing usage stops further admission by default. Active budgeted operations cannot use portable checkpoints. (#5555)
- **MCP resources, prompts, and reconnection.** Use `Session.list_mcp_resources()`, `list_mcp_resource_templates()`, `read_mcp_resource()`, `list_mcp_prompts()`, and `get_mcp_prompt()` with a selected HTTP or stdio server while the Session is idle. Retrieved data does not enter model context automatically. Scoped tool selection and explicit reconnection preserve control over admitted tools without replaying uncertain calls. (#5597, #5604, #5556)
- **Local execution and project search.** `Environment.local()` runs supported executor tools locally. On Linux, `LocalStorageMount(source=path)` grants read-only access to an existing canonical directory; source and destination must match. `project_glob` and `project_grep` search beneath the Environment's fixed working directory without granting command execution. (#5501, #5575, #5574)
- **Integration and supervision.** Add native A2A client and server adapters through the `a2a` extra, explicit `TraceContext`, workflow tracing controls, API-key rate-limit snapshots, turn receipts, exact-turn steering, and attention waits. Classic integrations use the `classic` and `classic-*` extras; Classic and native objects and checkpoints remain separate.
- **Speech with native history and policies.** `agents.voice.NativeSessionVoiceWorkflow` uses a caller-owned text-output Session with the speech pipeline. It speaks accepted final text after completion review and output validation. The application handles interactions and Session lifetime. Install the `voice` extra; Realtime agents remain under `agents.classic.realtime`. (#5596)

### Changed

- **Choose function-tool timeout handling.** `timeout_behavior="error_as_result"` returns a timeout error to the model, with optional `timeout_error_function` formatting. Raising remains the default. (#5605)
- **Optional buffered Responses.** `NativeProvider(response_mode="buffered")` reads each complete HTTP JSON response before emitting model output. Select a standard Responses model explicitly; the default model uses Responses Lite. Buffered mode requires API-key authentication and excludes WebSockets, native children, and Codex login. Streaming remains the default. (#5589)
- **Experimental saved-login reuse.** `NativeProvider(auth=CodexLogin())` can use a saved Codex login over HTTP. Omit `api_key` and `base_url`. This experimental feature may change or be removed; see the [authentication guidance and limits](https://github.com/openai/openai-agents-python/blob/4c1190046163c09e4f67ab7254f22bea60b14894/docs/sdk/configuration.md#reuse-a-codex-login-experimental).

### Fixed

- Keep Classic mount credential files private during creation and replacement, and bound Classic sandbox archive extraction.
- Stop deferred work and automatic compaction after cancellation. Retain cleanup owners so callers can retry unfinished release of resources without repeating reconciliation or tool effects.
- Stop sampling retries after terminal provider authentication failures. Report typed output-failure reasons and captured context limits, and preserve cache-write token counts.
- Retain provider response observations and execution details under their configured policies. Reduce native startup and event-reading overhead.

## 0.24.0-alpha.6 (2026-11-03)

Changes since `0.24.0-alpha.5`. This preview expands conversation control, state recovery, and tool policies, and includes public API changes.

### Added

- Control submitted work through a captured `Turn`, with `get_result()`, passive `events()`, manual request handling, steering, and cancellation. Queue prompts with `Session.submit(..., if_busy="queue")`, or steer the active Turn with `if_busy="steer"`.
- Add `LocalStorage` to reopen conversations from a local profile, and extend portable `SessionState` capture and recovery. Add idle forks, caller-supplied compaction summaries, and checks for feature support and checkpoint readiness. `LocalStorage` profiles cannot export portable checkpoints.
- Add `NativeOutput` for native strict output and `PromptedOutput` for JSON validated in Python. Optional `max_retries` performs bounded corrections without tools or replaying the task. `LocalStorage` requires `max_retries=0`.
- Handle live manual tool requests and resume supported pending requests from checkpoints. Add experimental external-job recovery through `agents.experimental.deferred` and an optional MCP Tasks adapter.
- Select tools for each submission or model step within the Session's opening permissions. Add native web search, native child checklists, and native goals for local-storage Sessions.
- Validate Python tool return annotations and add tool policies for inspecting arguments and results. Extend native MCP support with argument replacement, result transforms, and application-managed form and URL interactions over supported HTTP, stdio, and model WebSocket paths.
- Add `NetworkPolicy` for native commands on supported Linux and macOS executors, subject to each platform's network and Unix socket limits. Manage remote executor lifetimes through an `Environment` supplied when opening a Session or running an Agent.
- Enable native child approvals and form requests with `NativeAgents(interactions=True)`. Children continue to share the Session's tools and application context.
- Configure native OpenTelemetry export and inspect typed observations. `NativeError` now exposes available provider status codes, error codes, request IDs, and failure categories.
- Test real SDK sessions and Python tools against local scripted responses with `agents.testing.ScriptedResponses`, without a live model or API key.

### API changes

- Use `Agent.run()` or `Agent.run_sync()` for one-shot calls; the redundant `Runner` aliases have been removed.
- Replace `Turn.wait()` and `Session.wait()` with `Turn.get_result()` on the captured Turn.
- Replace `Session.interrupt()` with `Turn.cancel()` for one root Turn or `Session.cancel_all()` for the whole Session.
- Rename `Subagent.submit()` to `Subagent.start()`. Unify Python callbacks and manual tools under `FunctionTool`; remove the separate `ManualTool` declaration.
- Agent declarations are immutable. Create a new Agent to change defaults, use `with_output_type()` for an output-type variant, or use supported idle Session updates for an open conversation.
- Tool selection and Session updates cannot expand the permissions captured when the Session opened. Restored Sessions use current application tools, credentials, and policies.

### Fixed

- Keep each captured Turn's result stable across accepted steering, native continuations, and later submissions. Preserve one-shot manual checkpoints, matching interaction replies, and output-repair state across supported save and restore boundaries.
- Improve native child cancellation and Session cleanup. Stop native work when result policies expire, and preserve policy failure reasons when reporting root and child results.
- Reject malformed Python tool result envelopes and preserve server errors through MCP result transforms. Support nested OpenAI extension forms and HTTP telemetry export with client certificates.

### Documentation and examples

- Move the preview source to the public repository. Refocus installation guidance on prebuilt wheels and move SDK build instructions to `CONTRIBUTING.md`.
- Add examples for browser applications, AG-UI streaming, deferred jobs, evaluation, observability, and common application tasks. Remove migration guides from the preview documentation.

## 0.24.0-alpha.5 (2026-10-21)

This alpha includes API and lifecycle changes. Read the [Sessions guide](docs/sdk/sessions.md) and [MCP guidance](docs/sdk/tools.md#native-mcp) when upgrading from alpha.4.

- `Session.interrupt()` now returns when the interruption request is accepted. Session exit joins the remaining owned cleanup and reports failures.
- Preserve accepted steering that arrives after the last model request by starting a successor turn after successful completion. `Turn.wait()` keeps its exact result; `Session.wait()` and one-shot Agent helpers follow automatic root continuations. Observe child continuations through Session events or fresh descendant snapshots.
- Remove `HttpMCP.bearer_token_env_var`. Read credentials in application code and pass `bearer_token`, for example from `os.environ`.
- Add `ExecutorContext.agent_role` and `model` for policies that need the actual native child role and executing model.
- Reject incomplete or unsupported model output exposed by the pinned Codex parser before reporting successful root or child completion. The parser can omit an undecodable refusal after commentary; that response can still report success. Preserve exact-turn output, settled manual checkpoint receipts and owned cleanup across interruption.
- Update current installation commands, wheel names and guides for alpha.5. Keep the pinned Codex revision unchanged.

## 0.24.0-alpha.4 (2026-10-21)

This alpha changes the public API. Follow the [Agent and Session guide](docs/sdk/index.md) and use the examples shipped with this release.

- Run one answer with `Agent.run()` or `Agent.run_sync()`, or open an `Agent.session()` for multiple turns. `Runner.run()` and `Runner.run_sync()` remain convenience aliases.
- Declare typed Python functions, application context, structured output, native MCP tools, manual tools, executor policies and native children through the smaller public API.
- Observe a Session's events, wait for individual submissions, interrupt work, compact history, and save or restore settled state.
- Improve callback deadlines, checkpoint settlement and native resource cleanup, including Windows handle ownership. Keep the pinned Codex revision unchanged.
- Update installation commands, SDK guides and example links to the qualified alpha.4 wheels. Remove installation instructions for extras that this API no longer provides.

## 0.24.0-alpha.3 (2026-10-16)

- Keep native continuation bindings separate from SDK conversation history with `NativeSession`, `SQLiteNativeSession`, and `RunConfig.native_session`. Existing `SQLiteSession` usage remains supported.
- Add source-checkout commands to back up, restore, and purge SQLite native session storage. Stop all writers before backup or restore. Restore requires the original absolute paths and the matching native revision; cross-build migration is not supported.
- Add an inventory tutorial covering streaming, MCP, specialist agents, history, approvals, and a service endpoint. Clarify MCP results and nested stream callbacks.

## 0.24.0-alpha.2

Changes since v0.24.0a1:

- Fix cleanup after cancellation and failed native checkpoint imports.
- Keep imported conversation history and distinct tool calls when restoring session state.
- Preserve approved work and the active agent when resuming handoffs and nested agents.
- Keep session history consistent during compaction, and include compaction in model usage and timeout handling.
- Bound exec-server connection and command cleanup. Report interrupted command outcomes without automatically replaying them.
- Preserve binary WebSocket frames and response headers across the native worker process, and stop retaining unused session events.
- Reduce bundled native executables and enforce wheel-size limits.
- Add a resumable sandbox example with bounded MCP output, file edits, approval, and restart.
- Restore files, safe links, and executable modes in fresh exec-server workspaces after restart.
- Type checkers now recognize existing `async with` and `aclose()` usage for streaming results. Clarify stream cleanup and text deltas, MCP errors and approvals, and resume guidance.
