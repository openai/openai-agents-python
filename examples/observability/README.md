# Debug a run with local telemetry

Run a synthetic task, inspect its native traces and logs, and compare the application receipt with its recorded history.

## Set up the walkthrough

Install the [SDK preview wheel](../../docs/sdk/installation.md), activate your Python environment, and use the example files from the same release. Run the commands below from the root of that release's source archive. The receiver uses the Python standard library and needs no additional packages or API key.

Keep these files local. Native telemetry may include paths, tool metadata, and other details even when you disable prompt and response text. This receiver has no authentication, storage service or production hardening. Use synthetic data only.

## Run and inspect a trace

```sh
python -m examples.observability.trace_walkthrough --scenario tool-error --view --output synthetic-trace.json
```

The command starts a loopback OpenTelemetry Protocol (OTLP) HTTP JSON receiver, configures native traces and logs, runs the synthetic task, closes its Session, and flushes telemetry.

Open the printed viewer URL while the command waits for Enter. The page shows received payloads. The JSON file also includes the application receipt. For this scenario, expect `outcome` set to `completed` and a `lookup` result containing `Synthetic catalog unavailable`. The artifact also contains received telemetry batches. Press Enter to close the viewer and receiver.

## Find related records

1. Search the viewer for the printed `run_id` to find records for the whole example process.
2. In `resourceSpans[].scopeSpans[].spans[]`, find a `run_turn` span whose `attributes` include `example.run_id`. Its `traceId` links spans from that native operation. Startup spans often have different trace IDs.
3. In `resourceLogs[].scopeLogs[].logRecords[]`, find `event.name=codex.api_request` and `conversation.id` equal to the receipt's `session_id`. A log's `traceId`, when present, links to its native span.

The receipt's `turn_id` identifies the submitted turn after it settles. Not every log has a trace ID or every span a session ID.

## Compare failure scenarios

Run each scenario in a **new process**. Configure native telemetry only once per process. These commands require no provider credentials:

```sh
python -m examples.observability.trace_walkthrough --scenario tool --output raised.json
python -m examples.observability.trace_walkthrough --scenario validation --output validation.json
python -m examples.observability.trace_walkthrough --scenario provider --output provider.json
```

| Scenario | Application receipt | What the received records establish |
| --- | --- | --- |
| `tool-error` | The callback returns `ToolOutput.error`; the model receives its text and the turn completes. | API requests return 200. Public history can show a completed function result with `error=null`; that does not establish tool success. |
| `tool` | The real Python callback raises; `get_result()` raises and the receipt records `tool_callback_exception`. | The model request returned 200. A provider success does not establish callback success. |
| `validation` | `PromptedOutput(Report)` rejects the final JSON value; the receipt records `output_validation_failure`. | The model request returned 200. Native telemetry does not establish Python output validity. |
| `provider` | A local synthetic provider returns HTTP 401; the receipt records `provider_failure`, `status_code=401`, `category=authentication_error` and `code=invalid_api_key`. | API-request log records contain HTTP 401. This exercises the provider failure path without contacting a real provider. |

In the checked source build, traces included `run_turn`, `traceId`, `spanId`, parent links, start/end times and the custom run attribute. API-request logs included the conversation ID, HTTP status, attempt and duration fields.

The raised callback and output-validation runs did not emit dedicated Python failure log records. Their native span status did not reliably identify Python failures. Use settled results, exception types and application receipts for those distinctions. These fields describe observations from this build. They do not define a stable telemetry schema or establish native retry defaults.

## Check delivery

If the command reports no receipt, check the exporter endpoint and loopback proxy bypass. A successful SDK run alone does not prove telemetry arrived. The process keeps the receiver alive until `telemetry.shutdown()` flushes. Closing the receiver first can lose batches.

## Continue a parent trace into another worker

The [`trace_context.py`](trace_context.py) example requires APIs added after `0.24.0-alpha.6`. Follow the [setup for examples from the development branch](../../docs/sdk/installation.md#examples-and-upgrades), then run:

```sh
python -m examples.observability.trace_context
```

The example supplies an explicit `TraceContext` to the first run. Its Python tool passes `ToolContext.trace_context.to_headers()` to a second Agent through `parent_trace`. Both runs use synthetic model responses and a local collector, so no API key is needed. The command prints `Done` and the number of received native spans; the count can vary.

The application passes trace headers explicitly across the worker boundary. Configure telemetry once before opening Sessions, and call `telemetry.shutdown()` after the Sessions close so the collector can receive the final spans.

## Read supported tool history

[`history_reader.py`](history_reader.py) uses public `session.turns()` and `session.items()` while a settled Session remains open. The walkthrough stores its result under `receipt.history`.

The reader pairs each function output with the most recent preceding call that has the same `(turn_id, call_id)`. It uses each pair once. It leaves an unpaired output's name and arguments null. It does not turn recorded output text into a success classification.

The projection filters the native engine's canonical history. Each iterator takes one snapshot. Call both after the turn settles so they read the same settled state.

Older turn timestamps and unavailable usage remain null. Null usage means the token count is unavailable. The synthetic peer supplies no usage in this example. Use `subagent_id` with the Session iterators when reading a particular child; this small reader shows the root only.

| Recorded content | Projection and limits |
| --- | --- |
| User and assistant messages | Supported text and user inline images. System/developer messages and unsupported content parts are omitted. |
| Reasoning | Recorded summary fields; no promise of private reasoning text. |
| Function/custom calls and results | Calls normalize to `function_call` and results to `function_call_output`; inspect output and error as recorded. Missing identities or turn IDs can exclude records. |
| Native tool records | Supported native items can include MCP, command execution, file changes and web search. The reader's `item_types` lists what arrived; `function_results` covers Python/custom function records only. |
| Turn metadata | Recorded status, nullable timestamps, error and nullable usage. A completed native turn does not establish valid Python final output. |
| Unknown records and unsupported item types | Omitted. History does not reconstruct approvals, all passive events, callback effects or authoritative web citation spans. |

Keep a separate application ledger when you need receipts for effects or application error categories. Observations are bounded and may have gaps, and history omits some records. Neither replaces that ledger.
