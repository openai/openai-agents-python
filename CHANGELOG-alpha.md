# Preview changelog

Release notes for previews of the OpenAI Agents SDK. Versions alpha.5 and earlier were distributed through the earlier preview program; their release tags are not hosted in this repository.

## 0.24.0-alpha.6 (Unreleased)

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
