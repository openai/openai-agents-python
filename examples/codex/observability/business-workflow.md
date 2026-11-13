# Business workflow across services

[`business_workflow.py`](business_workflow.py) traces a synthetic order through checkout, validation, fulfillment, and completion. A separate Python process handles fulfillment over loopback HTTP. Both services export native spans to the same local OpenTelemetry Protocol (OTLP) receiver, alongside local processor receipts.

## Local execution

Use an SDK build from the same checkout as this example. Follow the [source build instructions](../../../CONTRIBUTING.md#build-the-sdk-from-source) and run both commands from the checkout root in the activated Python environment. The example uses Python's standard library for HTTP and synthetic `ScriptedResponses` for model traffic, without provider credentials or additional dependencies.

```sh
PYTHONPATH=. python -m examples.codex.observability.business_workflow --scenario success
PYTHONPATH=. python -m examples.codex.observability.business_workflow --scenario failure
```

Each command starts fresh checkout and fulfillment processes. Each process configures telemetry once. It closes Agent Sessions, flushes completed records, and shuts down its telemetry owner before exit. The parent keeps the receiver alive until both processes finish. The commands print an outcome and flush summary, while keeping full receipts in memory. Adding `--output /tmp/workflow-success.json` retains an optional local artifact. The collector and artifacts serve local demonstration needs; they receive only synthetic traffic.

## Trace identity across the HTTP request

The checkout service opens `telemetry.trace("order workflow")` around the business operation. Its validation and completion spans mark application stages. Its Agent calls the `dispatch` function tool, whose `ToolContext.trace_context.to_headers()` supplies W3C `traceparent` and optional `tracestate` headers to the HTTP request.

The fulfillment handler passes those headers through `Agent.run(..., parent_trace=headers)`. The worker's native spans continue the trace beneath the issuing tool span. The example checks both the shared trace ID and the remote parent span ID against processor receipts. It also collects native OTLP receipts independently of those Python callbacks. The HTTP hop demonstrates explicit application propagation; it does not exercise an A2A transport. The worker returns telemetry receipts only for this local demonstration. Production services require a separate, access-controlled telemetry destination instead of returning receipts through business endpoints.

## Metadata and service policy

The `stages` receipt keeps an ordered record of authorized routing, specialist tool work, validation, and in-memory delivery. Each stage carries its outcome and elapsed nanoseconds. The failed scenario records rejected validation and suppressed delivery. The successful scenario keeps one public outcome in `delivered`.

The `supervisory` receipts map typed stops to the fixed span categories `cancelled` and `run_budget_exceeded`. The cancellation probe joins local tool cleanup before recording the stage. The zero-request budget probe stops before any provider send or tool effect. Both probes check the categories in ended-span receipts.

The `usage` receipt keeps reported totals, observed completions, and the number of completions without usage. It also keeps a separately labeled fixture estimate. Worker tokens are unavailable.

Both services call `telemetry.set_sensitive_data_logging_enabled(False)` before admitting work. They also disable native prompt and response logs, set the tool-result text limit to zero, and select `include_sensitive_data=False` for their roots. W3C headers carry identity across services; each process applies its own privacy policy.

The example chooses these application attributes from fixed constants, outcome enums, and a local count:

| Attribute | Meaning in the example |
| --- | --- |
| `example.service` | Exporting process: `checkout` or `fulfillment` |
| `business.stage` | Checkout, validation, fulfillment, or completion stage |
| `business.route` | Fixed `standard` business route |
| `business.usage.worker_calls` | One application dispatch to fulfillment |
| `business.usage.tokens` | `unavailable` for this scripted provider |
| `business.outcome` | `completed` or `stopped` application result |

Custom metadata appears under `sdk.metadata.*` in processor attributes. The count measures application calls. The separate token field marks missing model usage.

The payload sentinel `SYNTHETIC_PRIVATE_ORDER_4821` travels through synthetic prompts, tool results, and model outputs. The example scans the full artifact, including both processor receipts and received OTLP batches, for that sentinel before writing the file. This check tests those payloads in this run; it does not establish general removal of arbitrary metadata, paths, credentials, exception text, or third-party telemetry. Application attribute values require their own allowlist regardless of the metadata-only policy.

## Privacy ownership beyond exported spans

The sentinel check covers this fixture's exported records. The application manages these other data copies and policies separately:

| Data | Control and ownership |
| --- | --- |
| Trace scopes and children | An ancestor's disabled or metadata-only restriction constrains child scopes, including detached children. Each service applies its own [trace policy](../../../docs/sdk/observability.md#group-work-in-a-local-trace) and [content switches](../../../docs/sdk/observability.md#configure-telemetry). Application names and metadata still require allowlists. |
| Observations and raw provider frames | [Observation readers](../../../docs/sdk/streaming.md#read-observations-safely) receive application data independently of export policy. Opted-in raw frames retain decoded provider payloads, as exercised by the [raw-frame tests](../../../packages/codex/tests/test_native_raw_provider_events.py). The application controls reader access, retention, and publication. |
| Application logs and journals | The application chooses what its own logs and durable receipts retain. The [A2A host-policy guidance](../a2a/README.md#credential-rotation-and-host-validation) assigns validation before journal writes and saved-result reuse to the host. Trace content switches do not sanitize those stores. |
| Headers and wire payloads | [W3C headers](../../../docs/sdk/observability.md#continue-a-trace-across-workers) carry ancestry only. Each receiving service selects its own privacy policy. Service authentication and request or response payload handling remain application-owned. |
| History and checkpoints | [Portable checkpoints](../../../docs/sdk/state.md#save-and-restore-a-portable-checkpoint) retain native history, including absolute paths. The application protects stored content and checks checkpoint integrity and accepted settings before restore. Trace restrictions do not remove conversation content. |
| Export destinations and credentials | The application configures approved collectors and private export headers through [telemetry configuration](../../../docs/sdk/observability.md#configure-telemetry). W3C carriers do not transfer exporter credentials. Route replacement changes telemetry destinations separately from history, journals, and application logs. |

This local fixture does not diagnose missing exports in a production backend or measure filtering and buffering overhead.

## Export routes, failure, and receipt

`telemetry.add_trace_processor(receipts)` adds a Python route while preserving the configured native OTLP exporter. After the workflow's flush, `telemetry.set_trace_processors([replacement])` replaces future routes, including native OTLP. A separate `replacement probe` trace reaches the replacement processor while remaining absent from the native receiver. The artifact retains both receipt sets for that comparison.

In `success`, fulfillment completes. In `failure`, the fulfillment tool raises a controlled synthetic exception, the worker returns `stopped`, and checkout records an error outcome. The failure scenario also adds a processor that deliberately rejects deliveries. The example asserts `flush.failed_deliveries > 0` for checkout while checking a successful worker flush and retained native sink receipts. The deliberately failed processor demonstrates known delivery loss separately from the business-stage failure.

`force_flush()` waits through a delivery barrier for completed records. A successful flush reports no known loss through that barrier; it does not prove durable storage or viewer visibility. This example checks local receiver receipt separately. Its JSON artifact retains `flush`, `worker.flush`, `replacement_flush`, lifecycle events, and OTLP batches, allowing inspection of delivery and application outcome as separate evidence.

## Diagnosing missing trace records

Comparing records for one trace ID separates four questions. The local processor shows creation and ancestry. `force_flush()` shows known queue drops and failed deliveries. A receiver receipt shows transport arrival. A separate lookup in the destination establishes whether the trace is stored and visible.

The native OpenAI exporter requires a known native root descriptor. The local exporter fixture records an externally parented child in a separate processor while reporting failed delivery and no matching trace at the loopback ingest endpoint. The same exporter delivers a locally rooted trace. [Exporter fixture](../../../packages/codex/tests/test_native_trace_exporter.py)
