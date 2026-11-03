# Preview changelog (historical)

These entries describe releases from the earlier private preview. They are historical records, not public releases or installable tags in this repository. Build the current `v1` source using the [maintenance guide](packages/codex/MAINTAINING.md).

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
