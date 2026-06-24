# Model and Provider Boundaries

Use this reference for changes to model resolution, `ModelSettings`, provider adapters, Responses versus Chat Completions behavior, request conversion, streaming terminal events, transport reuse, or model retries.

## Core Boundary

The run loop depends on the `Model` interface, not on one provider's request or response schema.

- `Model.get_response()` returns a normalized `ModelResponse`.
- `Model.stream_response()` yields normalized response stream events while preserving provider payloads needed by public raw-event consumers.
- `ModelProvider.get_model()` resolves names to model implementations and owns provider-level caches or connections.
- `Model.close()` and `ModelProvider.aclose()` release persistent transport resources when an implementation owns them.

Provider adapters own request construction, provider feature validation, terminal event interpretation, usage conversion, and translation into SDK item shapes. Keep provider-specific branching out of the core run loop unless it represents a shared SDK contract.

## Model and Settings Resolution

- An explicit `RunConfig.model` overrides the agent model. A model instance is used directly; a model name is resolved through the configured `ModelProvider`.
- Implicit default settings must follow the resolved model name, including when a run-level model name replaces the agent default.
- Resolve agent settings with run-level settings by overlaying non-`None` values. Preserve the documented merge behavior for structured fields such as `extra_args` and retry settings.
- Do not pass provider request extras into tracing by default. `ModelSettings.to_traceable_dict()` is the boundary for settings considered safe and meaningful in traces.

## Capability Ownership

Do not infer that a feature available in one adapter is supported by every `Model` implementation.

- Responses-specific features include server-managed response chaining, conversation-aware request fields, tool namespaces, deferred tool loading, tool search, response includes, compaction, and Responses websocket transport.
- Chat Completions generally requires client-managed replay and adapter conversion of Responses-compatible SDK items. Unsupported server-state or tool features should be rejected or explicitly ignored according to the adapter's documented validation mode.
- Realtime has its own session protocol, event model, and server tracing. Do not route Realtime behavior through the standard Responses or Chat Completions assumptions.
- Third-party model adapters may preserve only the shared `Model` contract. New provider-specific fields need an explicit conversion and fallback policy.

Validate capabilities at the adapter boundary where the resolved model and complete request are known. Avoid public flags that appear accepted by the SDK but are silently dropped before the provider request.

## Provider Data and Terminal Semantics

- Preserve provider-supplied string IDs, request IDs, usage, and opaque provider data when the public SDK contract exposes them.
- Normalize provider objects and mapping payloads without relying on truthiness for valid empty or zero values.
- A transport stream ending is not automatically a successful model response. Responses `failed` and `incomplete` terminals, explicit error events, and a missing terminal payload must produce the documented failure behavior in both HTTP and websocket paths.
- Keep semantically equivalent HTTP, websocket, streaming, and non-streaming paths aligned on final `ModelResponse`, errors, request IDs, and usage.

## Transport Resource Ownership

- Persistent Responses websocket models are loop-bound resources. Cache reusable websocket model instances by running event loop and model name; do not share one connection or `asyncio.Lock` across loops.
- Use weak loop ownership so an unused cache does not keep a closed event loop alive. When a live connection itself pins a closed loop, prune it with synchronous abort and state clearing rather than awaiting work on that closed loop.
- A provider that caches persistent models must make `aclose()` close every unique cached model and clear its caches. Close on a still-running owner loop when possible; do not drive an inactive foreign loop inside `asyncio.to_thread()`.
- A model used without a running loop cannot safely join the loop-scoped websocket cache. Preserve the non-reuse fallback rather than attaching it to an arbitrary global loop.
- Connection reuse ends after protocol errors, pre-terminal disconnects, cancellation that invalidates framing, or explicit close. Clear connection and loop-bound lock state together so a later request cannot reuse half-closed transport state.

## Retry and Replay Safety

- Provider retry advice can describe retryability, delay, and replay safety; the runner must not replace provider-specific evidence with a generic status-code assumption.
- Requests that use server-managed conversation state or may have produced side effects are not automatically replay-safe. A retry policy must account for whether the provider could have accepted the previous attempt.
- Retry conversion and error handlers must preserve the original exception semantics and avoid leaking sensitive request payloads through chaining, logs, traces, or provider error objects.

## Review Checklist

1. Identify which adapter owns the feature and how unsupported adapters behave.
2. Verify model and implicit-settings resolution when run config overrides the agent.
3. Compare HTTP/websocket and streaming/non-streaming terminal behavior when applicable.
4. Preserve request IDs, usage, provider data, and error semantics through normalization.
5. Prove retries are safe for the request's state ownership and side effects.
6. Test transport reuse, cross-loop access, closed-loop pruning, and provider shutdown when persistent connections are involved.

## Sources

- `src/agents/models/interface.py`
- `src/agents/model_settings.py`
- `src/agents/run_internal/turn_preparation.py`
- `src/agents/models/openai_responses.py`
- `src/agents/models/openai_chatcompletions.py`
- `src/agents/models/multi_provider.py`
- `src/agents/models/_response_terminal.py`
- `src/agents/run_internal/model_retry.py`
- `tests/models/`
- `tests/test_config.py`
