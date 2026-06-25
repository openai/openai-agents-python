# Realtime Session Lifecycle

Use this reference for `RealtimeSession` changes involving entry, exit, listeners, connections, background tasks, approvals, handoffs, event iteration, tracing context, or cleanup.

## Resource Ownership

Treat the session as the owner of these resources once they are acquired:

| Resource | Acquisition | Required release or terminal state |
|---|---|---|
| Model listener | `add_listener()` during entry | `remove_listener()` |
| Model connection | `model.connect()` | `model.close()` |
| Event iterators | Waiting on the event queue | Wake or terminate every waiter on close |
| Guardrail tasks | Created during output processing | Complete, or cancel and account for completion |
| Tool-call tasks | Created when `async_tool_calls=True` | Complete, or cancel and account for completion |
| Pending approvals and outputs | Added during tool execution | Resolve, retain for retry, or clear during terminal cleanup |
| Agent and model settings | Updated on handoff or `update_agent()` | Keep runtime state and model configuration aligned |

Do not add a new side effect before a failure point without defining who releases it.

## Entry and Exit

- Python does not call `__aexit__` when `__aenter__` raises. Any listener, connection, task, tracing scope, or other resource acquired before the exception needs explicit failure cleanup.
- Keep construction free of external side effects. Acquire listeners and connections during entry where failures can be handled coherently.
- `close()` and internal cleanup must be idempotent. Repeated close paths should still wake event iterators without closing the model twice.
- Mark the session closed only after the cleanup state is coherent. If model close fails, decide deliberately whether retry is possible and which resources remain owned.

## Async Task and Context Rules

- `asyncio` tasks inherit a snapshot of the creator's context. A background task cannot update the caller task's `ContextVar` state.
- A `ContextVar` token must be reset in the same context that created it. Never pass a token to a different task and assume cleanup can reset it safely.
- Shared session fields can be mutated by the listener path, tool-call tasks, `close()`, `update_agent()`, and handoff handling. Review ordering and races whenever one of those paths changes.
- Calling `task.cancel()` requests cancellation; it does not prove the task has finished its `finally` blocks or released resources. Await cancelled tasks when completion matters, or document and test why dropping them is safe.
- Background-task exceptions must reach a deterministic owner. They must not silently disappear or leave event consumers blocked.

## Agent Transitions

- Handoffs and the public `update_agent()` API are equivalent agent-transition surfaces. Keep their model settings, tool and handoff resolution, emitted events, and tracing metadata aligned unless a difference is intentional and documented.
- Resolve dynamic tools and enabled handoffs once per transition when possible, then reuse the exact resolved values for model settings and metadata.
- With concurrent tool calls, capture the agent snapshot associated with each call. Do not route a call through whichever agent happens to be current when the task eventually runs.

## Guardrails and Response Ordering

- Realtime output guardrails inspect accumulated transcript text at configured debounce thresholds, not each token and not a final `Runner` output object. They emit `guardrail_tripped` instead of raising a normal Runner tripwire exception.
- A tripped output guardrail marks the response interrupted before awaiting transport work, emits one trip event per response, forces response cancellation, and sends safe follow-up input naming the guardrail. Concurrent guardrail tasks must not interrupt or message the same response twice.
- Guardrail callbacks can run after audio has already been buffered or played. Consumers must treat `audio_interrupted` as the signal to stop local playback; text rejection alone cannot retract audio already delivered.
- An exception from one output guardrail is logged and skipped so it does not silently terminate the live session. Exceptions that escape the background guardrail task must become a `RealtimeError` event rather than disappearing.
- Realtime function-tool input guardrails follow the same optional pre-approval and mandatory post-approval ordering as standard function tools, but their rejection is returned through Realtime tool output and events.
- Follow-up `response.create` work triggered by tools, handoffs, or guardrails must respect the active response lifecycle. Wait for `response.done` or the model layer's equivalent gate before starting a conflicting response.

## Failure-Path Tests

Add focused tests for affected phases:

1. Instruction, tool, or handoff resolution fails during entry.
2. Model connection fails after listener registration.
3. A background tool or guardrail task raises or is cancelled.
4. Cleanup runs while event iterators are waiting.
5. `close()` is called repeatedly or from another task.
6. A handoff or `update_agent()` fails partway through model-settings application.
7. Tool output sending fails after local execution and must be retried without running the tool twice.
8. Concurrent guardrail tasks trip once, cancel playback, and do not overlap follow-up responses.

Verify lifecycle changes with the real public path where feasible; helper-only tests are insufficient when task ownership or context propagation determines the result.

## Sources

- `src/agents/realtime/session.py`
- `src/agents/realtime/model.py`
- `src/agents/realtime/openai_realtime.py`
- `tests/realtime/test_session.py`
- `tests/realtime/test_session_exceptions.py`
- `docs/realtime/guide.md`
