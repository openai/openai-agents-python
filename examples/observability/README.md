# Debug one synthetic run

These examples require an SDK wheel built from this source revision. The published alpha.5 wheel does not provide all these APIs. Run from the matching source checkout. The receiver uses the Python standard library; it needs no additional packages or API key.

```sh
uv run --no-sync python -m examples.observability.trace_walkthrough --scenario tool-error --view --output synthetic-trace.json
```

The command starts a loopback OTLP HTTP JSON receiver, configures native traces and logs, runs the synthetic task, closes its Session, and flushes telemetry. Open the printed viewer URL while the command waits for Enter. The page shows the actual received payloads. The JSON file also includes the application receipt. Keep these files local: native telemetry may include paths, tool metadata and other details even with prompt and response text switches off. This receiver has no authentication, storage service or production hardening. Use synthetic data only.

Search the viewer for the printed `run_id`. In `resourceSpans[].scopeSpans[].spans[]`, find a `run_turn` span whose `attributes` include `example.run_id`. Its `traceId` links spans from that native operation. Many startup spans have different trace IDs; the run ID selects the whole example process. In `resourceLogs[].scopeLogs[].logRecords[]`, find `event.name=codex.api_request` and `conversation.id` equal to the receipt's `session_id`. The log's `traceId`, when present, links to its native span. The receipt's `turn_id` identifies the submitted turn after it settles. Do not assume every log has a trace ID or every span has a session ID.

Run each scenario in a **new process**. Configure native telemetry only once per process. These commands require no provider credentials:

```sh
uv run --no-sync python -m examples.observability.trace_walkthrough --scenario tool --output raised.json
uv run --no-sync python -m examples.observability.trace_walkthrough --scenario validation --output validation.json
uv run --no-sync python -m examples.observability.trace_walkthrough --scenario provider --output provider.json
```

| Scenario | Application receipt | What the received records establish |
| --- | --- | --- |
| `tool-error` | The callback returns `ToolOutput.error`; the model receives its text and the turn completes. | API requests return 200. Public history can show a completed function result with `error=null`; that does not establish tool success. |
| `tool` | The real Python callback raises; `get_result()` raises and the receipt records `tool_callback_exception`. | The model request returned 200. A provider success does not establish callback success. |
| `validation` | `PromptedOutput(Report)` rejects the final JSON value; the receipt records `output_validation_failure`. | The model request returned 200. Native telemetry does not establish Python output validity. |
| `provider` | A local synthetic provider returns HTTP 401; the receipt records `provider_failure`, `status_code=401`, `category=authentication_error` and `code=invalid_api_key`. | API-request log records contain HTTP 401. This exercises the provider failure path without contacting a real provider. |

In the checked source build, traces included `run_turn`, `traceId`, `spanId`, parent links, start/end times and the custom run attribute. API-request logs included the conversation ID, HTTP status, attempt and duration fields. The raised callback and output-validation runs did not emit dedicated Python failure log records. Their native span status was not a reliable Python failure classifier. Use settled results, exception types and application receipts for those distinctions. These are observed fields for this build, not a stable telemetry schema or a claim about native retry defaults.

If the command reports no receipt, check the exporter endpoint and loopback proxy bypass. A successful SDK run alone does not prove telemetry arrived. The process keeps the receiver alive until `telemetry.shutdown()` flushes; closing the receiver first can lose batches.

# Read supported tool history

[`history_reader.py`](history_reader.py) uses public `session.turns()` and `session.items()` while a settled Session remains open. The walkthrough stores its result under `receipt.history`. The reader joins function outputs to the most recent preceding call with the same `(turn_id, call_id)`, then consumes that pair. It leaves an unpaired output's name and arguments null. It does not turn recorded output text into a success classification.

The projection is a filtered view of canonical native history. Each iterator takes one snapshot; call both after the turn settles to avoid reading different active states. Older turn timestamps and unavailable usage remain null. Null usage means unavailable, not zero tokens. The synthetic peer supplies no usage in this example. Use `subagent_id` with the Session iterators when reading a particular child; this small reader shows the root only.

| Recorded content | Projection and limits |
| --- | --- |
| User and assistant messages | Supported text and user inline images. System/developer messages and unsupported content parts are omitted. |
| Reasoning | Recorded summary fields; no promise of private reasoning text. |
| Function/custom calls and results | Calls normalize to `function_call` and results to `function_call_output`; inspect output and error as recorded. Missing identities or turn IDs can exclude records. |
| Native tool records | Supported native items can include MCP, command execution, file changes and web search. The reader's `item_types` lists what arrived; `function_results` covers Python/custom function records only. |
| Turn metadata | Recorded status, nullable timestamps, error and nullable usage. A completed native turn does not establish valid Python final output. |
| Unknown records and unsupported item types | Omitted. History does not reconstruct approvals, all passive events, callback effects or authoritative web citation spans. |

Keep a separate application ledger when you need effect receipts or business-level error categories. Observations are bounded and may have gaps; history is filtered; neither replaces that ledger.
