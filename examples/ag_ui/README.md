# AG-UI passive Turn stream

This is application example code, not an SDK integration or namespace. It targets the Python event/SSE contract in `ag-ui-protocol==0.1.9`, using its real `ag_ui.core.Event` models and `ag_ui.encoder.EventEncoder`. It is not a claim of compatibility with later AG-UI versions, every browser client, or the entire protocol.

This example requires the SDK from this source revision. It uses `Turn.get_result()`, which the `v0.24.0-alpha.5` wheel linked for the older examples does not provide. Install an SDK wheel built from this revision, then install the example dependency in the application's environment:

```sh
python -m pip install -r examples/ag_ui/requirements.txt
```

The only extra direct dependency is the pinned AG-UI protocol implementation, so the example does not copy its schema or write its own SSE encoder. Its Pydantic dependency is already used by the SDK. Protocol concepts and Python SDK are documented by [AG-UI](https://docs.ag-ui.com/concepts/events) and the [AG-UI Python event and encoder docs](https://github.com/ag-ui-protocol/ag-ui/tree/main/docs/sdk/python). Those pages can change; the installed `0.1.9` `ag_ui/core/events.py`, `types.py`, and `encoder/encoder.py` define the actual tested contract.

Use `stream_turn` inside the worker's existing Session scope. Pass that open Session explicitly to authorize identity verification. For example:

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

`turn_cancelled` projects the SDK outcome, not proof that an owner stopped this Turn. A callback can forward `TurnStoppedError` from another stopped Turn, including its existing exception cause. The example preserves that classification without inferring who requested the stop or its original Turn. It does not distinguish those callback failures from actual stops.

A gap before the retained start of this Turn can belong entirely to earlier Turns. A missing start or a gap after the start ends the UI view with `resync_required`; it does not stop the worker. Displayed text items close even on failure or a gap, but their text is not asserted complete. An authorized application can choose a fresh display from `session.items()` or `session.turns()`; this recipe cannot reconstruct lost tokens. It provides no cursor, tool-body mapping, model-step mapping, interrupts, resume protocol, or browser-owned durable worker.

A completed-only text response has no observed native text item ID. For nonempty plain text, the example checks the recorded final item ID in the owning Session against text already displayed from Turn events. It limits the check to that Turn's last observed native step and its child, if any. If the final ID or full text was not displayed, or identity verification is unavailable, the example ends with `resync_required`. This check never renders, reconstructs, or replays history; an identical earlier commentary string cannot stand in for the missing final item. It does not invent an ID or stringify structured outputs. For child Turns, it uses the matching cancelled terminal only after `get_result()` joins settlement; other child callback errors report `turn_failed`.

Pass the actual output declaration for the captured Turn. If the worker supplied an `output_type` override to `session.submit`, pass that override; otherwise pass the opening Agent's `output_type`. Only `None` and bare `str` select plain text. `PromptedOutput(str)` and `NativeOutput(str)` produce decoded strings from JSON, so they must not be tested for missing plain-text deltas. Structured completion remains an exact Turn success; the example neither decodes UI text nor invents a replacement text item. `Turn` does not expose an output-mode getter, so the application retains its own declaration.

Closing or cancelling `stream_turn` closes only its passive reader. The Session must remain owned elsewhere if work should outlive a disconnected response. If the application instead places the Session itself inside a response generator, closing that generator also closes the work it owns.
