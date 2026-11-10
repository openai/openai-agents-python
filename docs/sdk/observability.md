# Observe usage and diagnose agent work

Read token usage from completed results and configure telemetry to trace work across agents and workers. When a run fails, compare telemetry with recorded history and your application's effect records before deciding whether to retry. For live text and passive events, follow [Session observation streaming](sessions.md#stream-observations).

## Read usage without double counting

After a successful [one-shot run or Turn](concepts.md), inspect the optional `Completed.thread_usage` snapshot. This fragment assumes `result` is the `Completed` value returned by `await agent.run(...)` or `await turn.get_result()`:

```python
# result is the Completed value returned by agent.run() or turn.get_result().
usage = result.thread_usage
if usage is None:
    print("No usage was reported.")
else:
    print("Thread total:", usage.total_token_usage.total_tokens)
    print("Latest reported response:", usage.last_token_usage.total_tokens)
    print("Model context window:", usage.model_context_window)
```

Read usage with these accounting rules:

- `total_token_usage` is cumulative for the native thread, including restored history. Do not add successive snapshots as separate costs.
- `last_token_usage` may cover only one response from a Turn that needed several. A snapshot may repeat when no new usage arrives.
- Cached and reasoning tokens are already included in totals. Do not add them again.
- Root usage excludes child threads. [Prompted-output repairs](configuration.md#structured-output) have separate records.

Both `total_token_usage` and `last_token_usage` expose the fields listed in [usage types](reference.md#tokenusageinfo-and-tokenusage). `model_context_window` can be `None`. These counts are not billing records.

For a live display, replace the snapshot with `UsageUpdated.usage` from `agents.observation`, which can be `None`. Do not add event counts. Because observations can drop, read the final result after completion.

## Limit a run's model requests and usage

`RunBudget` requires [`0.24.0-alpha.7` or later](installation.md). Pass `budget=RunBudget(...)` to `agent.run()`, `agent.run_sync()`, or `session.submit()`. This async fragment assumes a configured `agent`:

```python
from agents import RunBudget

result = await agent.run("Summarize the supplied text.", budget=RunBudget(max_requests=3))
```

Set at least one integer limit from `0` through `2**53 - 1`: `max_requests`, `max_tokens`, or `max_estimated_cost_micros`. A zero limit blocks the first model request. Once a limit is reached, the SDK blocks the next sampling admission. An already admitted response can exceed token or cost limits and still complete successfully, so these limits do not cap actual charges.

`max_requests` counts root sampling admissions, including prompted-output repairs. It excludes transport retries, compaction requests, and child requests. Token and cost limits use reported response completions on the active root, including reported internal requests. They exclude child responses and cumulative thread snapshots.

For a cost limit, supply a synchronous `estimate_cost_micros(usage)` callback. It receives `TokenUsage` and returns a nonnegative integer estimate in millionths of a US dollar, or `None` for unknown cost. Estimates can differ from billed charges. With the default `missing_usage="stop"`, missing usage or an unknown cost estimate blocks the next admission. Use `missing_usage="continue"` only if continuing with known values meets your application's requirements. A request-only budget needs neither usage nor an estimator.

A blocked admission raises `NativeError` with code `"run_budget_exceeded"` or `"run_budget_usage_unavailable"`. An invalid or failed estimator produces `"run_budget_invalid_estimate"`. Inspect earlier tool effects before starting another run. [`wait_for_attention()`](sessions.md#wait-for-a-decision-or-a-result) reports a budget stop as `reason="budget_exceeded"`.

Each queued submission has its own budget. Steering and live manual replies keep the active run's budget. A budgeted active run cannot suspend or restore from a pending checkpoint; one-shot manual tools need `on_interaction`. After completion, an idle checkpoint can save the result and start a new run with a fresh budget.

## Configure telemetry

Configure native telemetry through `agents.telemetry` before opening the first Session in the process. Call `await configure(...)` once. After all Sessions close, call `await shutdown()` to flush and shut down native telemetry. Your application still owns Session cleanup and any Python OpenTelemetry exporters it creates.

If native initialization fails after claiming the tracing subscriber, start a fresh process. Retrying configuration in that process is unsupported.

Place this fragment inside your application's async entry point, before any Session opens. Import `Agent` from `agents` and supply an existing `agent`, such as `Agent(instructions="Answer briefly.")`. Replace the endpoint with your collector's trace endpoint:

```python
from agents import telemetry

await telemetry.configure(
    traces=telemetry.OtlpHttp(endpoint="http://127.0.0.1:4318/v1/traces"),
    environment="dev",
)
try:
    result = await agent.run("Say hello.")
finally:
    await telemetry.shutdown()
```

Exporter settings are:

- `traces`, `logs` and `metrics` accept `OtlpHttp(...)`, `OtlpGrpc(...)` or `None`. Each defaults to `None`, disabling that signal, including native metrics.
- `OtlpHttp` defaults to `protocol="binary"` and also accepts `"json"`. Supply a collector endpoint and optional string-to-string `headers` mapping.
- Both exporters accept `tls=OtelTlsConfig(...)` with `ca_certificate`, `client_certificate` and `client_private_key` paths. Relative paths resolve against the application's current directory.
- `span_attributes` adds application-selected string attributes.

Content defaults are `log_user_prompt=False`, `log_agent_responses=False` and `tool_result_max_bytes=0`. The switches control only the named native records. They do not sanitize all telemetry. The byte limit bounds only result text. Select exporters and controls for the data your collector may receive.

## Group work in a local trace

Local workflow scopes, `TraceOptions`, and custom processors require `0.24.0-alpha.7` or later. Install a [matching preview wheel](installation.md). Run each complete telemetry example below in a fresh Python process because telemetry configuration and shutdown apply to the process.

Use `telemetry.trace()` for a workflow and `telemetry.span()` for application work inside it. These are synchronous context managers used inside either synchronous or asynchronous application code. This complete script groups two synthetic Agent runs and an application span. It checks local context without configuring an exporter. To send the spans to a collector, add the exporter from [telemetry configuration](#configure-telemetry).

```python
import asyncio

from agents import Agent, telemetry
from agents.testing import ScriptedResponses, text


async def main() -> None:
    await telemetry.configure()
    try:
        with telemetry.trace("synthetic-workflow") as workflow:
            with telemetry.span("prepare-input"):
                prompt = "Return the synthetic label."
            assert telemetry.current_context() == workflow.context
            async with ScriptedResponses([text("First")], [text("Second")]) as peer:
                agent = Agent(instructions="Answer briefly.", provider=peer.provider)
                first = await agent.run(prompt)
                second = await agent.run(prompt)
                print(first.final_output, second.final_output)
        assert telemetry.current_context() is None
    finally:
        await telemetry.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
```

Expected final-answer output is `First Second`. The scripted peer uses a local HTTP server and no real model key. The current scope is task-local. Enter and exit a scope in the same task; each scope can be entered once. Ending a scope restores the previous context immediately. Already admitted work retains its captured context through cleanup.

A new workflow defaults to metadata-only tracing. Use `TraceOptions(disabled=True)` on a workflow or operation to disable its tracing. Use `include_sensitive_data` only when the destination may receive that content. An ancestor's disabled or metadata-only restrictions remain in effect: a child cannot relax them, and `detached=True` removes parent linkage without clearing restrictions. These per-workflow controls supplement the process-level [content switches](#configure-telemetry), which do not sanitize all telemetry.

For `agent.session(trace_options=...)`, `agent.run(trace_options=...)`, or `session.submit(trace_options=...)`, an explicit `TraceOptions.parent` selects a local `SpanContext`. Otherwise the current SDK scope supplies the parent, followed by the Session default. The Session default and current owners still constrain tracing policy when another parent is selected. Submission captures context before queueing; steering cannot change an admitted Turn's policy. Use fresh tracing options on restore because local context is not part of saved conversation state.

`SpanContext` is process-local and can outlive the recording scope through captured work. Do not serialize it or send it to another process. W3C [trace headers](#continue-a-trace-across-workers) transfer ancestry only. They do not transfer local workflow policy or exporter credentials. `TraceOptions` can also set a trace ID, group ID, application metadata, and private per-trace export headers through `TraceExportCredentials`. Keep credentials out of metadata and header carriers.

## Original trace metadata

Matching source builds have been returning copies of metadata from the owning trace root through `SpanContext.trace_metadata` and `TraceScope.trace_metadata`.

Scope: the owning native root, including child scopes and retained contexts after scope exit. Fresh dictionaries per read; local copy edits without changes to the native root. Contexts without retained root metadata: `None`.

Example fragment, after the application's `telemetry.configure(...)` call:

```python
from agents import telemetry

with telemetry.trace("Job", options={"metadata": {"tenant": "example"}}) as root:
    with telemetry.span("Step") as child:
        context = child.context
        assert child.trace_metadata == {"tenant": "example"}

snapshot = dict(context.trace_metadata or {})
snapshot["tenant"] = "local copy"
assert root.trace_metadata == {"tenant": "example"}
```

Process-local metadata, separate from W3C ancestry headers.

## Send native traces to an application sink

This section uses the same [matching preview wheel](installation.md) as local trace scopes. Supply an object with `on_trace_event(event)` through `telemetry.configure(processors=[...])` before opening Sessions. A processor can receive native trace records without an OTLP collector. The interface is separate from classic SDK trace processors.

This complete script prints synthetic lifecycle records and a bounded delivery result:

```python
import asyncio

from agents import telemetry


class ConsoleProcessor:
    def on_trace_event(self, event: telemetry.TraceLifecycleEvent) -> None:
        print(event["lifecycle"], event["name"], event["outcome"])


async def main() -> None:
    await telemetry.configure(processors=[ConsoleProcessor()])
    try:
        with telemetry.trace("synthetic-job"):
            with telemetry.span("synthetic-step"):
                pass
        delivery = await telemetry.force_flush(timeout_ms=5000)
        print(delivery.status, delivery.dropped_records, delivery.failed_deliveries)
    finally:
        await telemetry.shutdown(timeout_ms=5000)


if __name__ == "__main__":
    asyncio.run(main())
```

Events are immutable snapshots. `lifecycle` is `"start"` or `"end"`; events include trace/span identities, nanosecond timestamps as decimal strings, attributes, outcome, and `dropped_attributes_count`. `end_time_unix_nanos` is `None` until a span ends. Keep processor output and metadata synthetic or apply your application's data policy.

A callback may be synchronous or asynchronous. It runs on a private worker loop with a one-second delivery budget. Up to 16 workers can remain live, including stalled removed routes. A stalled processor can lose later events; failures affect delivery accounting rather than the Agent's result. Callbacks must cooperate to return because the SDK cannot forcibly stop Python code. Do not call `force_flush()` or `shutdown()` inside a processor callback.

After configuration, `add_trace_processor(processor)` adds a route and preserves the selected native exporter. `set_trace_processors(processors)` replaces future trace routes, including that exporter. Removed routes can still finish queued deliveries. At most 64 current and retired route descriptors are retained; flush before adding routes if that bound is reached.

`force_flush(timeout_ms=...)` covers completed records through a barrier within one total budget. `FlushResult.status` is `"success"`, `"partial"`, `"timeout"`, or `"failure"`; `dropped_records` and `failed_deliveries` report known loss, and `barrier` identifies the covered sequence. Success means exporter calls completed without known loss. It does not prove durable storage or viewer visibility. Unfinished spans and future records are excluded.

Both `force_flush` and an explicit shutdown budget accept integer milliseconds from 1 through 60000. Flush defaults to 10000 milliseconds. `shutdown(timeout_ms=None)` uses the runtime's default shutdown behavior. End application scopes and close Sessions before shutdown. Shutdown retires telemetry for the process, so use flush for periodic delivery checks while the worker continues.

## Continue a trace across workers

`TraceContext` and `parent_trace` require `0.24.0-alpha.7` or later. Install a [matching preview wheel](installation.md) to use these APIs and the example below.

Pass `parent_trace=TraceContext(traceparent=...)` or a W3C header mapping to `agent.run()`, `agent.run_sync()`, or `agent.session()`. Import `TraceContext` from `agents`. A Session uses this parent for startup and as the default for new root Turns and restored manual continuations. Pass a fresh parent when restoring state; saved conversation state does not retain an active trace context.

`session.submit(parent_trace=...)` overrides the Session parent for the new root and its automatic continuations. Omission and `None` inherit the Session parent. Queued work captures the parent when submitted. Steering cannot change an admitted Turn's parent.

A Python tool's `ToolContext.trace_context` supplies the issuing native span's optional W3C context. Use `to_headers()` to pass that context to another worker or an application HTTP client. Keep credentials and private content out of trace headers. This context does not create a Python span or retain local tracing policy. Use `agents.telemetry.TraceOptions` for native tracing policy and an SDK `SpanContext` as its local parent.

The synthetic [trace-context example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/observability/trace_context.py) connects a parent, a Python tool, and a second worker through a local collector. It requires no model key or collector account. Follow the [example-project setup](installation.md#examples-and-upgrades), then run `python -m examples.codex.observability.trace_context` from the repository root containing `examples/`.

## Diagnose a trace with history and application receipts

### Run the local walkthrough

The synthetic [trace walkthrough](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/observability/trace_walkthrough.py) starts a local OTLP JSON receiver and prints a receipt summary and viewer URL. It needs no collector account or model key. Keep receiver output synthetic. See the [observability examples](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/observability/README.md) for local exercises and the history reader.

First follow the [example-project setup](installation.md#examples-and-upgrades) to obtain the source files matching your installed wheel. From the repository root containing `examples/`, run `python -m examples.codex.observability.trace_walkthrough --scenario tool --view --output synthetic-trace.json`. Other scenarios are `tool-error`, `validation` and `provider`.

### Compare evidence

Connect application receipts, spans and history using `example.run_id`, span `traceId`, Session identity and `conversation.id` where those identifiers are present. Compare application outcomes with telemetry and the [history reader](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/observability/history_reader.py), which joins tool calls and results by turn and call identity. Missing timestamps and usage stay missing.

| Evidence | What it can show | What it does not prove |
| --- | --- | --- |
| Application receipt around `get_result()` | Settled output, raised error and application effect records | Native logs need not contain a separate record for a Python callback exception or output-validation failure. |
| `session.turns()` | Recorded turn status, failure, and available usage and timestamps | Missing usage or timestamps are not zero. Recorded turn usage differs from cumulative live thread snapshots. |
| `session.items()` messages and reasoning | Supported user and assistant text, user inline images, and retained reasoning summaries | Developer and system messages, unsupported content and unidentified records may be absent. This is not a full UI transcript or hidden reasoning. |
| Recorded function calls and results | Normalized function and custom calls and supported native tool records, joined by turn and call ID | `error=None` can accompany a returned tool error. Inspect the output and application outcome. Cancellation can leave incomplete pairs. |
| Recorded web search | Recorded query, action and text fields | These fields cannot reconstruct authoritative source records or citation spans. |
| Collector export | Received native spans and logs and configured telemetry fields | Export does not prove coverage of all Python failures, undo effects or define collector retention. |

Inspect the tool result and the application's outcome together. A tool may return an error value and let the turn complete, or raise an exception that fails the turn. Final-output validation can also fail after a tool effect. Each case needs a different recovery decision because turn completion alone does not describe the tool's outcome.

Closing the Session does not delete data already exported to the collector.
