# SDK maintainer reference map

Use this map to find the owner of an affected SDK boundary, then inspect the current source and focused tests. The [SDK guide](../../docs/sdk/index.md) is the user-facing contract; the [native maintainer guide](../../packages/codex/MAINTAINING.md) owns native build and lifecycle guidance.

| Boundary | Source and relevant guide |
| --- | --- |
| Agent declarations and native provider settings | `src/agents/agent.py`; [configuration](../../docs/sdk/configuration.md) |
| Session ownership, submissions and cleanup | `src/agents/session.py`; [sessions](../../docs/sdk/sessions.md) |
| Python function schemas, callbacks and typed context | `src/agents/_function_tools.py`, `src/agents/tools.py`; [tools](../../docs/sdk/tools.md) |
| Manual tools and live replies | `src/agents/manual.py`; [manual tools](../../docs/sdk/approvals.md#manual-tool-results) |
| Passive observations and accepted history | `src/agents/observation.py`, `src/agents/_projection.py`; [observation details](../../docs/sdk/sessions.md#observation-details) |
| MCP, remote executors and native children | `src/agents/mcp.py`, `src/agents/executor.py`, `src/agents/native_agents.py`; [MCP](../../docs/sdk/tools.md#native-mcp) and [remote execution](../../docs/sdk/execution.md#remote-execution) |
| Checkpoints and retained goal/checklist data | `src/agents/state.py`, `packages/codex/rust/checkpoint.rs`; [save and restore](../../docs/sdk/state.md#save-and-restore-a-portable-checkpoint) |
| Embedded Codex and PyO3 bindings | `packages/codex/rust/`; [native maintainer guide](../../packages/codex/MAINTAINING.md) |

Codex owns model requests, execution admission and canonical history. Python supplies declarations, validates callback inputs and results, and owns the Session's async integration. Restore binds current application permissions, callbacks and credentials; passive observations are not authority over work. Use `tests/native_fixtures.py` and the [native test guide](../../tests/README.md) to verify relevant behavior without live provider calls.

During issue and PR reviews, treat this directory as read-only background and validate claims against the reviewed source, tests and release boundary. Update the map during explicit maintenance work when ownership changes.
