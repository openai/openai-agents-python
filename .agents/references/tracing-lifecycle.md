# Tracing Lifecycle

Use this reference for changes to SDK trace or span context, processors, export, flush, shutdown, resumed trace state, or sensitive-data handling. Read [Realtime tracing architecture](realtime-tracing.md) before applying these client-side rules to Realtime server traces.

## Context and Parenting

- The current trace and span are held in `ContextVar` state. Async tasks inherit a snapshot when created; later changes in a child task do not rewrite the parent task's context.
- A context token must be reset in the context that created it. Start and finish ownership cannot be transferred between tasks without an explicit context boundary.
- A no-op trace or span cannot be a real parent. Propagate no-op behavior instead of exporting children with the sentinel `no-op` trace or span ID.
- Span factories should inherit trace metadata needed by processors, but they must not mutate the trace's caller-owned metadata mapping.

## Run and Resume Ownership

- A runner-created trace encloses run-loop-owned guardrails, model calls, tool execution, handoffs, session persistence, and error handling. Do not assume every completion callback or resource cleanup runs before trace finish; place newly traced cleanup explicitly inside the trace lifetime or create a deliberate separate trace/span context.
- An existing caller trace remains caller-owned. `Runner` may create child spans but must not finish or flush the caller's trace.
- `RunState` stores enough trace metadata to continue an interrupted run. Resume may reattach only when the trace ID was previously started in the process and the effective workflow name, group ID, metadata, and tracing key identity still match.
- Reattachment must not emit a duplicate trace-start event. If the saved state cannot prove a compatible live trace, create a normal trace according to the current run configuration instead of pretending to resume the old context.
- Tracing API keys are omitted from serialized `RunState` by default. A hash can verify that the caller supplied the same explicit key without persisting the secret; raw key persistence is opt-in.

## Processor and Export Isolation

- Trace processors are observability extensions and must not change application success. Catch processor callback, exporter, flush, and shutdown failures and report them as non-fatal.
- The default batch worker starts lazily on first queued item to avoid import-time thread and fork hazards. Keep top-level imports free of worker creation and shutdown-handler duplication.
- An exporter exception must not kill the batch worker and strand future traces. Drop or report the failed batch according to policy, then keep the worker usable.
- `flush_traces()` waits for queued and in-flight export work, so callers should invoke it after the trace closes when they require immediate delivery. It is not a substitute for finishing a partially built trace.
- Shutdown is best effort and deadline-aware. It should request exporter shutdown, interrupt retry backoff, drain within the remaining deadline, and return without changing the process exit code when an exporter blocks or a backend remains unavailable.
- Keep `TraceProvider.force_flush()` and `shutdown()` defaulting to no-ops for compatibility with custom providers that predate these lifecycle methods.

## Data Boundaries

- `trace_include_sensitive_data=False` controls captured span payload fields; it does not automatically sanitize exception objects, chaining, tracebacks, logs, or telemetry created elsewhere.
- Redaction must cover `__cause__`, `__context__`, formatter failures, and model-visible error conversion when an original exception carries tool arguments or provider payloads. `raise ... from None` changes display, not object retention.
- The OpenAI trace exporter owns ingest-specific payload sanitization such as field-size limits and supported usage keys. Custom processors should continue receiving the SDK's normal trace data unless their contract says otherwise.
- Per-run tracing keys, organization, and project routing must stay attached to the trace or exported item that selected them; do not let mutable global exporter state reroute an already-created trace.

## Review Checklist

1. Identify which task and context own each trace and span start, finish, and token reset.
2. Test success, exception, cancellation, interruption, serialized resume, full stream exhaustion, and explicit stream close.
3. Verify processor and exporter failures remain non-fatal and do not kill later export work.
4. Test flush and shutdown with queued work, in-flight export, retry backoff, and a blocking exporter.
5. Audit sensitive data through span payloads, exception chains, logs, and serialized state.

## Sources

- `docs/tracing.md`
- `src/agents/tracing/context.py`
- `src/agents/tracing/scope.py`
- `src/agents/tracing/traces.py`
- `src/agents/tracing/spans.py`
- `src/agents/tracing/provider.py`
- `src/agents/tracing/processors.py`
- `src/agents/tracing/setup.py`
- `src/agents/run_state.py`
- `tests/test_trace_processor.py`
- `tests/test_tracing.py`
- `tests/test_run_state.py`
- `tests/tracing/test_import_side_effects.py`
