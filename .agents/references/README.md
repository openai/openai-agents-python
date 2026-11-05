# SDK maintainer reference map

Use this map to find the owner of an affected SDK boundary, then inspect the current source and focused tests. The [SDK guide](../../docs/sdk/index.md) is the user-facing contract; the [native maintainer guide](../../packages/codex/MAINTAINING.md) owns native build and lifecycle guidance.

| Boundary | Source and relevant guide |
| --- | --- |
| Retained v0 API | `src/agents/classic/`; [Classic contract](../../CLASSIC.md) |
| Public imports and runtime boundary | `src/agents/__init__.py` and sibling facade modules; [runtime ownership](../../packages/codex/MAINTAINING.md#runtime-ownership) |
| Agent declarations and native provider settings | `src/agents/_runtime/agent.py`; [configuration](../../docs/sdk/configuration.md) |
| Prepared model requests | `src/agents/_runtime/prepared_request.py`; [runtime ownership](../../packages/codex/MAINTAINING.md#runtime-ownership) |
| Session ownership, submissions and cleanup | `src/agents/_runtime/session.py`; [sessions](../../docs/sdk/sessions.md) |
| Python function schemas, callbacks and typed context | `src/agents/_runtime/_function_tools.py`, `src/agents/_runtime/tools.py`; [tools](../../docs/sdk/tools.md) |
| Manual tools and live replies | `src/agents/_runtime/manual.py`; [manual tools](../../docs/sdk/approvals.md#manual-tool-results) |
| Passive observations and accepted history | `src/agents/_runtime/observation.py`, `src/agents/_runtime/_projection.py`; [observation details](../../docs/sdk/sessions.md#observation-details) |
| MCP connections and result policies | `src/agents/_runtime/mcp.py`; [native MCP](../../docs/sdk/tools.md#native-mcp) |
| Local and remote executor policies | `src/agents/_runtime/executor.py`; [execution](../../docs/sdk/execution.md) |
| Native child declarations and controls | `src/agents/_runtime/native_agents.py`, `src/agents/_runtime/subagents.py`; [delegation](../../docs/sdk/delegation.md) |
| Checkpoints and retained goal/checklist data | `src/agents/_runtime/state.py`, `packages/codex/rust/checkpoint.rs`; [save and restore](../../docs/sdk/state.md#save-and-restore-a-portable-checkpoint) |
| Embedded Codex and PyO3 bindings | `packages/codex/rust/`; [native maintainer guide](../../packages/codex/MAINTAINING.md) |

For the native API, Codex owns model requests, execution admission and canonical history. Python supplies declarations, validates callback inputs and results, and owns the Session's async integration. Restore binds current application permissions, callbacks and credentials; passive observations are not authority over work. Use `tests/native_fixtures.py` and the [native test guide](../../tests/README.md) to verify relevant behavior without live provider calls.

During issue and PR reviews, treat this directory as read-only background and validate claims against the reviewed source, tests and release boundary. Update the map during explicit maintenance work when ownership changes.
