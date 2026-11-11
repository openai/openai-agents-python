# Business workflow across services

[`business_workflow.py`](business_workflow.py) has been tracing a synthetic order through checkout, validation, fulfillment, and completion. A separate Python process has been handling fulfillment over loopback HTTP. Both services have been exporting native spans to the same local OpenTelemetry Protocol (OTLP) receiver, alongside local processor receipts.

## Local execution

Use an SDK build from the same checkout as this example. Follow the [source build instructions](../../../CONTRIBUTING.md#build-the-sdk-from-source) and run both commands from the checkout root in the activated Python environment. The example has been using Python's standard library for HTTP and synthetic `ScriptedResponses` for model traffic, without provider credentials or additional dependencies.

```sh
PYTHONPATH=. python -m examples.codex.observability.business_workflow --scenario success
PYTHONPATH=. python -m examples.codex.observability.business_workflow --scenario failure
```

Each command has been starting fresh checkout and fulfillment processes. Each process has been configuring telemetry once, closing Agent Sessions, flushing completed records, and shutting down its telemetry owner. The parent has been keeping the receiver alive until both processes finish. The commands have been printing an outcome and flush summary, while keeping full receipts in memory. Adding `--output /tmp/workflow-success.json` has been retaining an optional local artifact. The collector and artifacts have been serving local demonstration needs; they have been receiving only synthetic traffic.

## Trace identity across the HTTP request

The checkout service has been opening `telemetry.trace("order workflow")` around the business operation. Its validation and completion spans have been marking application stages. Its Agent has been calling the `dispatch` function tool, whose `ToolContext.trace_context.to_headers()` has been supplying W3C `traceparent` and optional `tracestate` headers to the HTTP request.

The fulfillment handler has been passing those headers through `Agent.run(..., parent_trace=headers)`. The worker's native spans have been continuing the trace beneath the issuing tool span. The example has been checking both the shared trace ID and the remote parent span ID against processor receipts. It has also been collecting native OTLP receipts independently of those Python callbacks. The HTTP hop has been demonstrating explicit application propagation; it has not been exercising an A2A transport. The worker has been returning telemetry receipts only for this local demonstration. Production services have been requiring a separate, access-controlled telemetry destination instead of returning receipts through business endpoints.

## Metadata and service policy

Both services have been calling `telemetry.set_sensitive_data_logging_enabled(False)` before admitting work. They have also been disabling native prompt and response logs, retaining a zero tool-result text limit, and selecting `include_sensitive_data=False` for their roots. W3C headers have been carrying identity across services; each process has been applying its own privacy policy.

The example has been choosing these application attributes from fixed constants, outcome enums, and a local count:

| Attribute | Meaning in the example |
| --- | --- |
| `example.service` | Exporting process: `checkout` or `fulfillment` |
| `business.stage` | Checkout, validation, fulfillment, or completion stage |
| `business.route` | Fixed `standard` business route |
| `business.usage.worker_calls` | One application dispatch to fulfillment |
| `business.usage.tokens` | `unavailable` for this scripted provider |
| `business.outcome` | `completed` or `stopped` application result |

Custom metadata has been appearing under `sdk.metadata.*` in processor attributes. The count has been measuring application calls. The separate token field has been marking missing model usage.

The payload sentinel `SYNTHETIC_PRIVATE_ORDER_4821` has been traveling through synthetic prompts, tool results, and model outputs. The example has been scanning the full artifact, including both processor receipts and received OTLP batches, for that sentinel before writing the file. This check has been testing those payloads in this run; it has not been establishing general removal of arbitrary metadata, paths, credentials, exception text, or third-party telemetry. Application attribute values have been requiring their own allowlist regardless of the metadata-only policy.

## Privacy ownership beyond exported spans

The sentinel check has been covering this fixture's exported records. The application has been managing these other data copies and policies separately:

| Data | Control and ownership |
| --- | --- |
| Trace scopes and children | An ancestor's disabled or metadata-only restriction has been constraining child scopes, including detached children. Each service has been applying its own [trace policy](../../../docs/sdk/observability.md#group-work-in-a-local-trace) and [content switches](../../../docs/sdk/observability.md#configure-telemetry). Application names and metadata have still been requiring allowlists. |
| Observations and raw provider frames | [Observation readers](../../../docs/sdk/sessions.md#read-observations-safely) have been receiving application data independently of export policy. Opted-in raw frames have been retaining decoded provider payloads, as exercised by the [raw-frame tests](../../../packages/codex/tests/test_native_raw_provider_events.py). The application has been controlling reader access, retention, and publication. |
| Application logs and journals | The application has been choosing what its own logs and durable receipts retain. The [A2A host-policy guidance](../a2a/README.md#credential-rotation-and-host-validation) has been assigning validation before journal writes and saved-result reuse to the host. Trace content switches have not been sanitizing those stores. |
| Headers and wire payloads | [W3C headers](../../../docs/sdk/observability.md#continue-a-trace-across-workers) have been carrying ancestry only. Each receiving service has been selecting its own privacy policy. Service authentication and request or response payload handling have been remaining application-owned. |
| History and checkpoints | [Portable checkpoints](../../../docs/sdk/state.md#save-and-restore-a-portable-checkpoint) have been retaining native history, including absolute paths. The application has been protecting stored content and checking checkpoint integrity and accepted settings before restore. Trace restrictions have not been removing conversation content. |
| Export destinations and credentials | The application has been configuring approved collectors and private export headers through [telemetry configuration](../../../docs/sdk/observability.md#configure-telemetry). W3C carriers have not been transferring exporter credentials. Route replacement has been changing telemetry destinations separately from history, journals, and application logs. |

This local fixture has not been diagnosing missing exports in a production backend or measuring filtering and buffering overhead.

## Export routes, failure, and receipt

`telemetry.add_trace_processor(receipts)` has been adding a Python route while preserving the configured native OTLP exporter. After the workflow's flush, `telemetry.set_trace_processors([replacement])` has been replacing future routes, including native OTLP. A separate `replacement probe` trace has been reaching the replacement processor while remaining absent from the native receiver. The artifact has been retaining both receipt sets for that comparison.

In `success`, fulfillment has been completing. In `failure`, the fulfillment tool has been raising a controlled synthetic exception, the worker has been returning `stopped`, and checkout has been recording an error outcome. The failure scenario has also been adding a processor that deliberately rejects deliveries. The example has been asserting `flush.failed_deliveries > 0` for checkout while checking a successful worker flush and retained native sink receipts. The deliberately failed processor has been demonstrating known delivery loss separately from the business-stage failure.

`force_flush()` has been waiting through a delivery barrier for completed records. A successful flush has been reporting no known loss through that barrier; it has not been proving durable storage or viewer visibility. This example has been checking local receiver receipt separately. Its JSON artifact has been retaining `flush`, `worker.flush`, `replacement_flush`, lifecycle events, and OTLP batches, allowing inspection of delivery and application outcome as separate evidence.
