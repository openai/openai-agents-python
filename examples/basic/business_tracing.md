# Trace a business workflow without recording model or tool payloads

[Run the offline example](business_tracing.py) from this checkout with the development dependencies installed (`make sync`). No API key, collector or network calls.

```sh
uv run --no-sync python examples/basic/business_tracing.py
```

Expected output — one delivery to an in-memory outbox, five successful application stages and a count of spans received in memory. The scripted classifier supplies synthetic usage. The delivery count comes from the outbox, independently of telemetry.

## Limit capture across nested runs

For a single run, use `RunConfig(tracing={"operation_detail": "metadata"})`. An explicit child config can change that policy. To prevent nested Runner calls and agents used as tools from enabling automatic model and tool payload capture, wrap the outer run.

```python
from agents import Runner
from agents.tracing import metadata_only_run_tracing


async def run_private(agent, request):
    with metadata_only_run_tracing():
        return await Runner.run(agent, request)
```

For streaming, start `Runner.run_streamed` and consume `result.stream_events()` inside the scope. Each run in the scope uses metadata detail and `trace_include_sensitive_data=False`. Disabled tracing stays disabled. The scope leaves caller configs and normal behavior outside it unchanged.

New asyncio tasks inherit the context at creation. Leaving the scope restores the previous setting in the current task. Enter a fresh scope on independent resume, in an existing worker task or in another service.

**Still private** — run results, saved state, logs and exceptions. Keep sensitive values out of names, IDs, custom spans and `trace_metadata`. The scope does not sanitize those fields or third-party instrumentation.

## Connect application stages

- **Service startup** — choose the OpenTelemetry provider, sampler, exporter, endpoint and retention policy for each service. The standalone example uses `set_trace_processors([bridge])` once for exclusive export to memory. To keep existing SDK destinations, use `add_trace_processor(bridge)`. Both settings affect the whole process. Drain active runs before changing destinations.
- **Service boundary** — the example passes only W3C `traceparent` from ingress to routing. Accept it only from trusted callers. Keep credentials, baggage and capture settings out of propagation. Each receiver chooses its own capture policy.
- **Stages** — routing, specialist work, validation and delivery have fixed span names and outcomes. Use span start and end times for latency. Put business attributes directly on application spans. On every enclosing span, disable both exception recording and automatic exception status descriptions. The example sets a fixed error status and re-raises the original exception.
- **Direct usage** — with direct Responses, `ModelSettings(preserve_raw_usage=True)` preserves available provider usage per response. The specialist span counts only entries in this run's `result.raw_responses`. Separate nested runs and attempts absent from that list are excluded. `app.direct_usage.available` is true when the list is nonempty and every entry supplies a nonnegative integer total. Only then does the example record `app.direct_usage.total_tokens`, including a measured zero. Never export the raw payload. Token counts alone do not establish charges.
- **Shutdown** — after finishing spans, call `flush_traces()` and flush the caller-owned OpenTelemetry provider separately. Shut down the bridge and provider when the service stops.

## Monitor export loss

The example uses a synchronous processor and counts spans in its local receiver. For a queued OTLP exporter, monitor each boundary separately.

| Boundary | What to measure |
| --- | --- |
| Queue | In OpenTelemetry Python SDK 1.45, enable `OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED=true` and pass a configured `meter_provider` to `BatchSpanProcessor`. Alert on `otel.sdk.processor.span.processed` with `error.type=queue_full`. These spans never reached the exporter. |
| Export attempts | Count spans submitted per batch and spans in failed batches. Account for retries when comparing attempts with unique spans. A successful flush or export counter alone cannot establish acceptance. |
| Receiver | Check accepted and rejected counts, including [OTLP partial success](https://opentelemetry.io/docs/specs/otlp/#partial-success) and `rejected_spans`. Reconcile sampled trace and span IDs with receiver receipts where available. Verify durable storage separately. |

Example with no retries — three sampled spans, one queue drop, two sent, one rejected and one accepted. Keep delivery and other business outcomes independent of export success.
