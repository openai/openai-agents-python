# Observe usage and diagnose agent work

Read token usage from completed results and configure telemetry to trace work across agents and workers. When a run fails, compare telemetry with recorded history and your application's effect records before deciding whether to retry. For live text and passive events, follow [Session observation streaming](sessions.md#stream-observations).

## Read usage without double counting

After a successful [one-shot run or Turn](concepts.md), inspect the optional `Completed.usage` snapshot. This fragment assumes `result` is the `Completed` value returned by `await agent.run(...)` or `await turn.get_result()`:

```python
# result is the Completed value returned by agent.run() or turn.get_result().
usage = result.usage
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

## Continue a trace across workers

`TraceContext` and `parent_trace` were added after `0.24.0-alpha.6`. Follow the [development-branch setup](installation.md#examples-and-upgrades) to use these APIs and the example below.

Pass `parent_trace=TraceContext(traceparent=...)` or a W3C header mapping to `agent.run()`, `agent.run_sync()`, or `agent.session()`. Import `TraceContext` from `agents`. A Session uses this parent for startup and as the default for new root Turns and restored manual continuations. Pass a fresh parent when restoring state; saved conversation state does not retain an active trace context.

`session.submit(parent_trace=...)` overrides the Session parent for the new root and its automatic continuations. Omission and `None` inherit the Session parent. Queued work captures the parent when submitted. Steering cannot change an admitted Turn's parent.

A Python tool's `ToolContext.trace_context` supplies the issuing native span's optional W3C context. Use `to_headers()` to pass that context to another worker or an application HTTP client. Keep credentials and private content out of trace headers. This context does not create a Python span or retain local tracing policy. Use `agents.telemetry.TraceOptions` for native tracing policy and an SDK `SpanContext` as its local parent.

The synthetic [trace-context example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/trace_context.py) connects a parent, a Python tool, and a second worker through a local collector. It requires no model key or collector account. Follow the [example-project setup](installation.md#examples-and-upgrades), then run `python -m examples.observability.trace_context` from the directory containing `examples/`.

## Diagnose a trace with history and application receipts

### Run the local walkthrough

The synthetic [trace walkthrough](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/trace_walkthrough.py) starts a local OTLP JSON receiver and prints a receipt summary and viewer URL. It needs no collector account or model key. Keep receiver output synthetic. See the [observability examples](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/README.md) for local exercises and the history reader.

First follow the [example-project setup](installation.md#examples-and-upgrades) to obtain the source files matching your installed wheel. From the directory containing `examples/`, run `python -m examples.observability.trace_walkthrough --scenario tool --view --output synthetic-trace.json`. Other scenarios are `tool-error`, `validation` and `provider`.

### Compare evidence

Connect application receipts, spans and history using `example.run_id`, span `traceId`, Session identity and `conversation.id` where those identifiers are present. Compare application outcomes with telemetry and the [history reader](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/observability/history_reader.py), which joins tool calls and results by turn and call identity. Missing timestamps and usage stay missing.

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
