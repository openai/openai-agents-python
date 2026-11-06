# Stream a Turn with AG-UI

Use `stream_turn` to convert a worker's Turn observations into AG-UI server-sent event (SSE) frames. The application keeps ownership of the Session, HTTP response, and browser display. Closing the stream releases its passive reader while the worker can continue.

This example uses the event models and encoder in `ag-ui-protocol==0.1.9`. Compatibility covers the [event subset below](#supported-events). Later AG-UI versions and browser clients need separate verification.

## Install the example dependency

Install the [SDK preview wheel](../../../docs/sdk/installation.md) and use example files from the same release. Activate that Python environment, then run this command from the source archive's root directory:

```sh
python -m pip install -r examples/codex/ag_ui/requirements.txt
```

AG-UI is the only extra direct dependency. It supplies the schema and SSE encoder and shares the SDK's Pydantic dependency. The installed `0.1.9` files `ag_ui/core/events.py`, `types.py`, and `encoder/encoder.py` define the tested behavior. The [AG-UI event reference](https://docs.ag-ui.com/concepts/events) and [Python event and encoder docs](https://github.com/ag-ui-protocol/ag-ui/tree/main/docs/sdk/python) may describe a later version.

## Stream from the worker's Session

Place the following excerpt inside your worker's existing Session scope. The application supplies `agent`, the open `session`, and an asynchronous `send_to_ui` function that writes an HTTP response. For a worker launched from a development checkout's repository root, set `PYTHONPATH=.` so the `examples.codex.ag_ui` import resolves. Passing the owning Session lets the example verify native item identities.

Capture the output declaration that the worker uses for this Turn. The excerpt submits without an override, so it passes `agent.output_type`. If you pass an `output_type` override to `session.submit`, pass that override to `stream_turn` instead. `Turn` does not expose an output-mode getter, so the application must retain the declaration.

```python
from contextlib import aclosing

from examples.codex.ag_ui.stream import stream_turn

# The application's worker owns agent, session and turn.
selected_output = agent.output_type
turn = await session.submit("Summarize the result.")
async with aclosing(stream_turn(turn, session=session, output_type=selected_output)) as frames:
    async for frame in frames:
        await send_to_ui(frame)  # The application supplies its own HTTP response.
result = await turn.get_result()
```

A successful stream starts with `RUN_STARTED`, emits any observed text and tool progress, and ends with `RUN_FINISHED` after that exact Turn's `get_result()` succeeds. The example reports a failed Turn or incomplete observations with `RUN_ERROR`. The [event reference](#supported-events) lists the emitted fields and error codes.

Only `None` and bare `str` select plain text. `PromptedOutput(str)` and `NativeOutput(str)` decode strings from JSON, so the example does not check them for missing plain-text deltas. A successful structured completion still counts as success for that exact Turn. The example does not decode UI text or create a replacement text item.

### Set the HTTP response format

Use `Content-Type: text/event-stream`, also returned by `EventEncoder().get_content_type()`. The `ag_ui.encoder.EventEncoder` encodes `ag_ui.core.Event` models as Unicode strings. If your HTTP framework expects bytes, encode those strings as UTF-8 at the response boundary. Do not append a Vercel header or `[DONE]` marker.

The application supplies authentication, authorization, frontend rendering, and disconnect handling. This example does not implement an interrupt or resume protocol, tool-body or model-step mapping, a cursor, or a browser-owned durable worker.

## Keep work alive after a disconnect

Closing or cancelling `stream_turn` closes only its passive reader. Keep the Session open outside the response generator if work must survive a response disconnect. If the response generator owns the Session instead, closing that generator also closes the work it owns.

Each call creates a fresh UI view of the issued Turn. An ID in an event does not authorize the browser to resume or control the Turn.

## Handle incomplete observations

A gap before the retained start of this Turn can belong entirely to earlier Turns. A missing start or a gap after the start ends the UI view with `resync_required`; it does not stop the worker. The example closes displayed text items even on failure or a gap. Their text may be incomplete.

An authorized application can rebuild the display from `session.items()` or `session.turns()`. This recipe cannot reconstruct lost tokens.

When a text response arrives only at completion, the example has not observed its native text item ID. For nonempty plain text, the example:

1. Checks the owning Session's recorded final item ID against text already displayed from Turn events.
2. Limits the check to that Turn's last observed native step and its child, if any.
3. Reports `resync_required` if the final ID or full text was not displayed, or identity verification is unavailable.

The check never renders, reconstructs, or replays history, invents IDs, or stringifies structured outputs. Identical earlier commentary cannot replace the missing final item.

## Interpret failures and cancellation

`turn_failed` reports a failed Turn. `turn_cancelled` reports an SDK stop-class outcome, but does not establish who requested the stop or which Turn originally stopped. A callback can forward `TurnStoppedError` from another stopped Turn, including its existing exception cause. The example preserves that classification and cannot distinguish those callback failures from an owner stopping this Turn.

For child Turns, the example uses the matching cancelled terminal event only after `get_result()` finishes waiting for settlement. Other child callback errors report `turn_failed`.

## Supported events

| Wire event | Meaning and IDs |
| --- | --- |
| `RUN_STARTED` | Begins this fresh UI view of an issued Turn. `threadId` is its Session or native child ID; `runId` is its exact `submission_id`. An ID never grants permission to resume or control the Turn. |
| `TEXT_MESSAGE_START/CONTENT/END` | Uses each real native `OutputTextDelta.item_id` as `messageId`. Empty deltas are omitted, because this pinned schema forbids them. |
| `CUSTOM` with `name="sdk.tool_progress"` | Only the passive tool type, call ID, name, namespace, family and optional status. No arguments, results, approval object or credentials. |
| `RUN_FINISHED` | Only after this exact logical Turn's `get_result()` completes successfully. Same thread and run IDs as its start. |
| `RUN_ERROR` | Fixed messages only. `turn_failed` for failure; `turn_cancelled` for an SDK stop-class outcome; `resync_required` when observations are incomplete or its retained start is missing. In 0.1.9, `RUN_ERROR` has no ID fields and there is no abort event. |
