# Agent Definition and Run Context

Use this reference for changes to public `Agent` fields, cloning, dynamic instructions, enabled tools or handoffs, output schemas, `RunContextWrapper`, `ToolContext`, usage aggregation, or the distinction between a public agent and an internal prepared clone.

## Public Definition and Cloning

- Exported `Agent` and `AgentBase` dataclass field order is a positional compatibility boundary. Append optional fields where possible and test old positional construction when the order changes.
- `Agent.__post_init__()` is the eager boundary for invalid field categories such as names, tools, handoffs, hooks, model settings, output types, and tool-use behavior. Dynamic callbacks are validated when invoked because their result depends on the current run.
- `Agent.clone()` uses `dataclasses.replace()` and is shallow. Mutable fields and contained tool, handoff, hook, and provider objects remain shared unless the caller explicitly supplies replacements.
- When `clone(model=...)` replaces a model whose settings still equal the old model's implicit defaults, recompute the new model's implicit defaults. Preserve explicitly customized `model_settings` instead of silently resetting them.

## Per-Turn Resolution

- Resolve dynamic instructions with the current context and public agent for each model turn. Enforce the documented two-argument callable shape and await async results.
- Evaluate callable `FunctionTool.is_enabled` and `Handoff.is_enabled` against the current run context. Do not cache a prior run's enabled set on the reusable agent.
- Use one resolved tool and handoff view for model exposure, reserved-name and collision checks, local dispatch, tracing, and Realtime session updates. Re-resolving independently at those surfaces can expose one set and execute another.
- An internal prepared agent may add bound tools, instructions, or sampling settings, but hooks, `ToolContext.agent`, handoff callbacks, and public results should identify the public agent unless an internal identity is explicitly part of the contract.
- The effective output schema belongs to the agent and model call that produced the candidate output. A handoff can change the final output type, so do not assume the starting agent's schema when parsing or typing the final result.

## Context Ownership

- Every agent, tool, handoff, guardrail, and lifecycle hook in one run must agree on the same application context type. The context object is local runtime state and is never added to model input automatically.
- A normal `ToolContext.from_agent_context()` shares the underlying application object, usage accumulator, and approval mapping with its parent while adding call-scoped fields such as call ID, namespace, arguments, and conversation history.
- Nested `Agent.as_tool()` execution has a separate run loop, approval scope, and resumable tool state. On the normal function-tool path it still shares the application object and usage accumulator, while `tool_input` belongs to the nested wrapper and must not overwrite the parent's scoped value.
- Sharing the application object is not the same as sharing every wrapper field. Add explicit application-level isolation when nested mutation is unsafe, and do not reuse parent approval decisions for nested calls merely because the tool name or call ID looks similar.
- Context serialization is a separate durability decision. Read [RunState schema and resume boundary](runstate-schema.md) before persisting custom context objects, approvals, usage, or nested tool input.

## Usage Accounting

- `RunContextWrapper.usage` is the run-wide mutable accumulator. Add each model response exactly once across streaming, non-streaming, retries, nested runs, handoffs, and resume paths.
- Preserve authoritative `request_usage_entries` when combining usage. Do not synthesize a second per-request entry from aggregate totals when the provider or retry layer already supplied request-level records.
- Retry accounting may include failed attempts with no token totals. Keep request count, aggregate tokens, request-level entries, and trace span usage internally consistent without inventing provider token data.
- Streamed usage remains incomplete until terminal chunks and the stream driver finish. Do not finalize billing, result summaries, or usage-bearing spans from the last visible text delta alone.

## Review Checklist

1. Test direct construction and clone behavior without mutating shared caller-owned objects.
2. Resolve dynamic instructions, tools, and handoffs through the same public agent and current context used for dispatch.
3. Verify handoff and internal prepared-agent paths expose the intended public identity and effective output schema.
4. Test nested agent tools for shared application state and isolated scoped metadata.
5. Compare aggregate and per-request usage after streaming, retries, handoffs, interruption resume, and nested runs.

## Sources

- `docs/agents.md`
- `docs/context.md`
- `docs/results.md`
- `src/agents/agent.py`
- `src/agents/run_context.py`
- `src/agents/tool_context.py`
- `src/agents/usage.py`
- `src/agents/run_internal/turn_preparation.py`
- `src/agents/run_internal/run_loop.py`
- `tests/test_agent_config.py`
- `tests/test_agent_clone_shallow_copy.py`
- `tests/test_agent_as_tool.py`
- `tests/test_usage.py`
