# Runner Lifecycle

Use this reference for changes to `Runner`, turn accounting, guardrails, hooks, handoffs, interruptions, cancellation, or streaming and non-streaming behavior.

## Turn Boundary

A turn is one logical model invocation plus processing of that response. Tool execution, handoff resolution, session persistence, interruption resume, and retries inside that logical invocation do not independently consume turns.

- Increment the turn counter exactly once when the run loop starts a logical model turn. Transport or provider retries inside `get_new_response()` remain part of that turn.
- A handoff changes the current agent, but the next turn begins only when the new agent invokes a model.
- Resuming `NextStepInterruption` continues the paused turn. Resolve stored approvals and tool work before deciding whether another model call is needed.
- Preserve `max_turns` and the current turn in `RunState`; resume must not reset the budget or charge a turn twice.

## Guardrail Ordering

- Input guardrails belong to the starting agent and run only for the initial user input. Do not rerun them after handoffs or when resuming an interruption.
- Sequential input guardrails must finish before model-side effects begin. Parallel input guardrails may overlap the model call, so a tripwire or exception must cancel and await the in-flight model task and sibling guardrail tasks.
- Tool input guardrails run before the approved tool side effect. Tool output guardrails run after local execution and before the output is accepted into the next step.
- Output guardrails run only after a candidate final output exists. Streaming must await them and preserve the same tripwire and exception behavior as non-streaming execution before declaring completion.
- Guardrail results are observable run state. Preserve them across handoffs, error handlers, streamed completion, and `RunState` round-trips.

## Step State Machine

`SingleStepResult.next_step` is the control boundary after one model response and its local side effects:

| Step | Meaning |
|---|---|
| `NextStepRunAgain` | Continue with the current agent and make another model call |
| `NextStepHandoff` | Switch the current agent, emit the transition, then continue |
| `NextStepFinalOutput` | A final candidate exists; finish terminal hooks, output guardrails, persistence, and result construction |
| `NextStepInterruption` | Persist enough processed state to resume pending approvals without rerunning completed work |

Do not bypass this state machine with path-local completion logic. New terminal or pausable behavior must define non-streaming, streaming, session, tracing, and serialized-resume semantics.

## Streaming Parity and Cancellation

- Streaming and non-streaming paths must produce equivalent final output, generated items, current agent, usage, guardrail results, session history, and interruption state for the same model behavior.
- Raw transport events may differ, but semantic `RunItemStreamEvent` and `AgentUpdatedStreamEvent` emission must follow the same processed items and agent transitions used by the non-streaming result.
- `stream_events()` is the stream driver's cleanup boundary. Keep consuming it until exhaustion after normal completion or `cancel()`, or explicitly close the async iterator; merely breaking after the last visible token does not prove session writes, guardrails, compaction, sandbox cleanup, usage, or terminal errors have settled.
- Immediate cancellation marks the result complete and requests task cancellation. `after_turn` cancellation leaves the current model/tool turn running so it can persist state and usage before the next turn. Preserve this distinction instead of treating both modes as queue shutdown.
- Terminal run-loop, guardrail, and max-turn errors must be surfaced from `stream_events()` after the required queued events are handled. Preserve `run_loop_exception` as a diagnostic view of the background task, not as a replacement completion primitive.
- `task.cancel()` is a request, not cleanup completion. Await cancelled tasks when their `finally` blocks, exceptions, or owned resources affect run correctness.
- Keep lifecycle hooks aligned across both paths, especially model start/end, handoff, tool start/end, and final-output hooks.

## Review Checklist

1. Identify which turn and which agent own the behavior.
2. Trace every `NextStep` outcome, including interruption resume.
3. Compare streaming and non-streaming side effects and terminal ordering.
4. Test guardrail tripwires and exceptions in sequential and parallel modes when relevant.
5. Verify normal exhaustion, explicit iterator close, immediate cancellation, and after-turn cancellation leave the documented result and owned resources in a coherent state.

## Sources

- `src/agents/run.py`
- `src/agents/run_internal/run_loop.py`
- `src/agents/run_internal/run_steps.py`
- `src/agents/run_internal/turn_preparation.py`
- `src/agents/run_internal/turn_resolution.py`
- `src/agents/run_internal/guardrails.py`
- `tests/test_agent_runner.py`
- `tests/test_agent_runner_streamed.py`
- `tests/test_cancel_streaming.py`
- `tests/test_guardrails.py`
- `tests/test_run_state.py`
- `docs/streaming.md`
- `docs/results.md`
