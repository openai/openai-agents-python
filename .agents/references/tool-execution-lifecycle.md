# Tool Execution Lifecycle

Use this reference for changes to function-tool planning, approvals, tool guardrails, concurrency, cancellation, timeouts, hooks, error conversion, or resumed execution. Read [Tool identity and routing](tool-identity.md) when names, namespaces, lookup keys, or call IDs also change.

## Plan Before Side Effects

`process_model_response()` discovers executable work, but `tool_planning.py` decides which work may run now. Keep discovery, approval partitioning, and invocation as separate phases.

- Fresh and resumed turns need different plans. A resumed interruption must execute unresolved or newly approved work without rediscovering or rerunning completed calls.
- Approval state is authoritative once resolved. Do not call a dynamic `needs_approval` checker again for a call whose status is already approved or rejected.
- Deduplicate by invocation identity before execution while preserving model order for public call and output items. A repeated tool definition is not a repeated call, and a repeated call ID must not execute twice.
- Validate enabled tools and canonical lookup before side effects. A tool disabled after model output or absent from the resolved tool set must follow the configured missing-tool behavior rather than reaching a stale callable.

## Approval and Guardrail Ordering

- Pre-approval input guardrails are an early rejection optimization. They may run before an approval interruption, but input guardrails must run again immediately before invocation because state, policy, or arguments may have changed while approval was pending.
- Rechecking guardrails does not mean rechecking approval. Persisted approval decisions and rejection messages must remain attached to the same tool identity and call ID across `RunState` resume.
- Tool input guardrails finish before the local side effect. Tool output guardrails finish before output becomes accepted run state, model input, or persisted session history.
- The tool guardrail pipeline applies to `FunctionTool` invocation. Handoffs, hosted tools, built-in provider tools, and nested `Agent.as_tool()` runs have separate execution boundaries unless they explicitly opt into equivalent checks.

## Concurrency and Failure Semantics

SDK-side function-tool concurrency is independent of provider-side parallel tool-call generation. The provider controls how many calls appear in one response; `RunConfig.tool_execution.max_function_tool_concurrency` controls how many local function handlers run at once.

- Preserve model order in emitted outputs even when handlers complete out of order.
- Isolate sibling results. A cancelled or failed call must not discard outputs already produced by successful siblings.
- Distinguish cancellation of one tool handler from cancellation of the parent run. Tool-local cancellation can follow the configured tool failure policy; parent cancellation must propagate promptly instead of becoming model-visible tool output.
- `task.cancel()` is not terminal cleanup. On sibling failure, drain cancelled handlers and wait for post-invocation work within the bounded cleanup policy. On parent cancellation, cancel remaining tasks and attach result callbacks so late exceptions are observed without delaying cancellation indefinitely.
- Select and raise failures deterministically when several tasks fail, while still observing secondary failures. Do not let task-set iteration order or eager task execution change the public result.

## Invocation Boundary

- Decorated synchronous Python functions run through `asyncio.to_thread()` so they do not block the event loop. Async function tools run in the event loop and are the only decorated handlers that support SDK timeouts.
- Timeout handling and ordinary exception handling are distinct policies. `timeout_behavior` and `timeout_error_function` own timeout conversion; `failure_error_function=None` means ordinary exceptions propagate instead of becoming model-visible output.
- Tool start/end hooks and function spans surround the actual invocation once per call, including failure and cancellation paths. Do not emit a successful end state before output guardrails complete.
- Per-run resources such as resolved `Computer` implementations must be initialized and disposed by the run that acquired them.
- Nested `Agent.as_tool()` execution owns a nested run loop and nested resumable state. Scope cached nested state by the parent `RunState` and call identity, not only by the reusable agent or tool object.
- `AgentToolUseTracker` records tool use per agent identity. When `reset_tool_choice=True`, reset the effective next-turn tool choice after that agent uses a tool so `required` or a named choice cannot force an accidental loop; do not mutate the agent's declared settings across independent runs.
- Persist and restore the tool-use tracker across interruption and sandbox resume, including graphs with duplicate agent names, so resumed tool-choice behavior matches uninterrupted execution.

## Review Checklist

1. Trace fresh execution, approval interruption, approval rejection, and serialized resume separately.
2. Verify guardrail, approval, hook, trace, invocation, output, and persistence order.
3. Test sequential, bounded-concurrency, sibling failure, tool-local cancellation, and parent cancellation paths.
4. Test default, custom, and disabled failure conversion plus timeout behavior where applicable.
5. Confirm every started task and per-run resource reaches a deterministic terminal state.

## Sources

- `docs/running_agents.md`
- `docs/tools.md`
- `docs/guardrails.md`
- `docs/human_in_the_loop.md`
- `src/agents/run_internal/tool_planning.py`
- `src/agents/run_internal/tool_execution.py`
- `src/agents/tool.py`
- `tests/test_agent_runner.py`
- `tests/test_agent_runner_streamed.py`
- `tests/test_function_tool.py`
- `tests/test_tool_guardrails.py`
- `tests/test_tool_choice_reset.py`
- `tests/test_tool_use_tracker.py`
- `tests/test_run_state.py`
