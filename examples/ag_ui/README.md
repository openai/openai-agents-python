# AG-UI passive Turn stream

This application example uses the Python events and server-sent event (SSE) format in `ag-ui-protocol==0.1.9`. It uses `ag_ui.core.Event` models and `ag_ui.encoder.EventEncoder`. It adds no SDK integration or namespace. Compatibility covers only the subset below, not later AG-UI versions, every browser client, or the entire protocol.

Install the [SDK preview wheel](../../docs/sdk/installation.md) and use example files from the same release. Then install the example dependency in your activated Python environment:

```sh
python -m pip install -r examples/ag_ui/requirements.txt
```

The pinned AG-UI protocol package is the only extra direct dependency. The example uses its schema and SSE encoder. The SDK already uses Pydantic, which AG-UI also requires. See [AG-UI](https://docs.ag-ui.com/concepts/events) and the [AG-UI Python event and encoder docs](https://github.com/ag-ui-protocol/ag-ui/tree/main/docs/sdk/python). Those pages can change. The installed `0.1.9` files `ag_ui/core/events.py`, `types.py`, and `encoder/encoder.py` define the tested behavior.

Use `stream_turn` inside the worker's existing Session scope. Pass the open Session so the example can verify item identities. For example:

```python
from contextlib import aclosing

from examples.ag_ui.stream import stream_turn

# The application's worker owns agent, session and turn.
selected_output = agent.output_type
turn = await session.submit("Summarize the result.")
async with aclosing(stream_turn(turn, session=session, output_type=selected_output)) as frames:
    async for frame in frames:
        await send_to_ui(frame)  # The application supplies its own HTTP response.
result = await turn.get_result()
```

Use `Content-Type: text/event-stream` (also returned by `EventEncoder().get_content_type()`). The encoder yields Unicode strings; an HTTP framework that expects bytes can UTF-8 encode them at the response boundary. Do not append a Vercel header or `[DONE]` marker. Authentication, authorization, frontend rendering and disconnect handling belong to the application.

## Supported subset

| Wire event | Meaning and IDs |
| --- | --- |
| `RUN_STARTED` | Begins this fresh UI view of an issued Turn. `threadId` is its Session or native child ID; `runId` is its exact `submission_id`. An ID never grants permission to resume or control the Turn. |
| `TEXT_MESSAGE_START/CONTENT/END` | Uses each real native `OutputTextDelta.item_id` as `messageId`. Empty deltas are omitted, because this pinned schema forbids them. |
| `CUSTOM` with `name="sdk.tool_progress"` | Only the passive tool type, call ID, name, namespace, family and optional status. No arguments, results, approval object or credentials. |
| `RUN_FINISHED` | Only after this exact logical Turn's `get_result()` completes successfully. Same thread and run IDs as its start. |
| `RUN_ERROR` | Fixed messages only. `turn_failed` for failure; `turn_cancelled` for an SDK stop-class outcome; `resync_required` when observations are incomplete or its retained start is missing. In 0.1.9, `RUN_ERROR` has no ID fields and there is no abort event. |

`turn_cancelled` reports the SDK outcome. It does not prove that an owner stopped this Turn. A callback can forward `TurnStoppedError` from another stopped Turn, including its existing exception cause. The example keeps that classification without inferring who requested the stop or which Turn originally stopped. It does not distinguish those callback failures from actual stops.

A gap before the retained start of this Turn can belong entirely to earlier Turns. A missing start or a gap after the start ends the UI view with `resync_required`; it does not stop the worker. The example closes displayed text items even on failure or a gap. Their text may be incomplete. An authorized application can rebuild the display from `session.items()` or `session.turns()`. This recipe cannot reconstruct lost tokens. It provides no cursor, tool-body mapping, model-step mapping, interrupts, resume protocol, or browser-owned durable worker.

When a text response arrives only at completion, the example has not observed its native text item ID. For nonempty plain text, the example checks the recorded final item ID in the owning Session against text already displayed from Turn events. The check covers only that Turn's last observed native step and its child, if any. If the final ID or full text was not displayed, or identity verification is unavailable, the example ends with `resync_required`. This check never renders, reconstructs, or replays history. An identical earlier commentary string cannot replace the missing final item. It does not invent an ID or stringify structured outputs. For child Turns, the example uses the matching cancelled terminal event only after `get_result()` finishes waiting for settlement. Other child callback errors report `turn_failed`.

Pass the actual output declaration for the captured Turn. If the worker supplied an `output_type` override to `session.submit`, pass that override; otherwise pass the opening Agent's `output_type`. Only `None` and bare `str` select plain text. `PromptedOutput(str)` and `NativeOutput(str)` produce decoded strings from JSON, so they must not be tested for missing plain-text deltas. A successful structured completion still counts as success for that exact Turn. The example does not decode UI text or invent a replacement text item. `Turn` does not expose an output-mode getter, so the application retains its own declaration.

Closing or cancelling `stream_turn` closes only its passive reader. To let work continue after a response disconnects, keep the Session open outside the response generator. If the application instead places the Session itself inside a response generator, closing that generator also closes the work it owns.
