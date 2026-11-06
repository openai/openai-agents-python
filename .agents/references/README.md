# SDK maintainer reference map

Use this map to find the owner of an affected SDK boundary, then inspect the current source and focused tests. The [SDK guide](../../docs/sdk/index.md) is the user-facing contract; the [native maintainer guide](../../packages/codex/MAINTAINING.md) owns native build and lifecycle guidance.

| Boundary | Source and relevant guide |
| --- | --- |
| Retained v0 API | `src/agents/classic/`; [Classic contract](../../CLASSIC.md) |
| Public imports and runtime boundary | `src/agents/__init__.py` and sibling facade modules; [runtime ownership](../../packages/codex/MAINTAINING.md#runtime-ownership) |
| Agent declarations and native provider settings | `packages/codex/src/_agents_codex/agent.py`; [configuration](../../docs/sdk/configuration.md) |
| Prepared model requests | `packages/codex/src/_agents_codex/prepared_request.py`; [runtime ownership](../../packages/codex/MAINTAINING.md#runtime-ownership) |
| Session ownership, submissions and cleanup | `packages/codex/src/_agents_codex/session.py`; [sessions](../../docs/sdk/sessions.md) |
| Python function schemas, callbacks and typed context | `packages/codex/src/_agents_codex/_function_tools.py`, `packages/codex/src/_agents_codex/tools.py`; [tools](../../docs/sdk/tools.md) |
| Manual tools and live replies | `packages/codex/src/_agents_codex/manual.py`; [manual tools](../../docs/sdk/approvals.md#manual-tool-results) |
| Passive observations and accepted history | `packages/codex/src/_agents_codex/observation.py`, `packages/codex/src/_agents_codex/_projection.py`; [observation details](../../docs/sdk/sessions.md#observation-details) |
| MCP connections and result policies | `packages/codex/src/_agents_codex/mcp.py`; [native MCP](../../docs/sdk/tools.md#native-mcp) |
| Local and remote executor policies | `packages/codex/src/_agents_codex/executor.py`; [execution](../../docs/sdk/execution.md) |
| Native child declarations and controls | `packages/codex/src/_agents_codex/native_agents.py`, `packages/codex/src/_agents_codex/subagents.py`; [delegation](../../docs/sdk/delegation.md) |
| Checkpoints and retained goal/checklist data | `packages/codex/src/_agents_codex/state.py`, `packages/codex/rust/checkpoint.rs`; [save and restore](../../docs/sdk/state.md#save-and-restore-a-portable-checkpoint) |
| Embedded Codex and PyO3 bindings | `packages/codex/rust/`; [native maintainer guide](../../packages/codex/MAINTAINING.md) |

For the native API, Codex owns model requests, execution admission and canonical history. Python supplies declarations, validates callback inputs and results, and owns the Session's async integration. Restore binds current application permissions, callbacks and credentials; passive observations are not authority over work. Use `packages/codex/tests/native_fixtures.py` and the [native test guide](../../packages/codex/tests/README.md) to verify relevant behavior without live provider calls.

During issue and PR reviews, treat this directory as read-only background and validate claims against the reviewed source, tests and release boundary. Update the map during explicit maintenance work when ownership changes.
