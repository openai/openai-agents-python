# Realtime Tracing Architecture

Use this reference when reviewing or implementing Realtime tracing behavior, especially claims that `RealtimeSession` should emit the same trace hierarchy as `Runner`.

## Two Separate Tracing Systems

Realtime integrations involve two independent tracing paths:

| Path | Owner | Configuration | Result |
|---|---|---|---|
| Realtime API server tracing | Realtime API | `"auto"`, `workflow_name`, `group_id`, and `metadata` | The server creates a Realtime session trace in the Traces Dashboard. |
| Agents SDK client tracing | Agents SDK tracing provider | `trace()`, `agent_span()`, and other SDK span factories | The SDK exports locally created traces and spans through its tracing processor. |

The current Python SDK has no mapping from an Agents SDK client `trace_id`, `span_id`, or parent context into `RealtimeModelTracingConfig` or the model's `session.update`. A server-created Realtime trace is therefore not attached as a child of an SDK-created trace or span by this implementation. Likewise, adding an SDK `agent_span()` around `RealtimeSession` does not make server-side trace contents children of that span.

If both paths are enabled, the dashboard can contain two separate traces. A shared `group_id` can make them easier to filter and correlate, but it does not merge them or create a parent-child relationship.

## Current Python SDK Behavior

- `RealtimeModelTracingConfig` exposes only `workflow_name`, `group_id`, and `metadata` in `src/agents/realtime/config.py`.
- `OpenAIRealtimeWebSocketModel` defaults the Realtime tracing configuration to `"auto"` when the caller does not provide one.
- After receiving `session.created`, the model sends the tracing configuration through a `session.update` event.
- `RealtimeRunConfig.tracing_disabled` prevents the SDK from enabling Realtime tracing for that session.

Verify these paths in `src/agents/realtime/openai_realtime.py` and `src/agents/realtime/session.py`; do not rely on old issue descriptions because Realtime tracing support has changed over time.

## Maintainer Constraints

1. Identify whether the behavior belongs to the Realtime API's server trace or an Agents SDK client trace created with `trace()`.
2. A client-side agent span does not repair missing server tracing and does not create the unified hierarchy produced by `Runner`.
3. The current Python SDK cannot place server-created Realtime spans under an SDK-created trace or span because it does not carry client trace parentage through the Realtime tracing configuration. Recheck the live protocol with `$openai-knowledge` before treating that implementation gap as permanent.
4. Use the server trace for Realtime model activity. Use a shared `group_id` or metadata when correlation with a client trace is required.
5. Parallel SDK spans need an explicit product and maintenance contract covering the dual-trace user experience, async task context, handoff parenting, failure cleanup, and the client-only operations represented by those spans.
6. A client trace becoming non-empty is not evidence that Realtime server activity has been captured or parented correctly.

## Sources

- `src/agents/realtime/config.py`
- `src/agents/realtime/openai_realtime.py`
- `src/agents/realtime/session.py`

Recheck the official API reference with `$openai-knowledge` before changing this guidance or implementing new protocol behavior.
