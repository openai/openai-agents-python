# Streaming and events

Observations are events that report text and progress while a Session runs. A slow reader can miss events, and observation records cannot answer pending requests. Use completed results to read accepted output and recorded history to inspect the conversation. Use [live interactions](approvals.md) when your application needs to answer a request.

| Task | Start here |
| --- | --- |
| Display text while a Turn runs | [Stream observations](#stream-observations) |
| Reconnect a reader or handle missing events | [Observation retention](#read-observations-safely) and [reader cursors](#reconnect-an-observer) |
| Inspect tool and command outcomes | [Tool events](#interpret-tool-events) and [completed execution](#inspect-completed-execution) |
| Compare provider responses with the SDK result | [Response attempts](#inspect-response-attempts) |
| Check the instructions and settings used for sampling | [Settings receipts](#check-applied-instructions-and-settings) |

First complete the [SDK setup](index.md#before-you-begin), including model credentials. Examples with `await` or `async with` are fragments for an async function unless marked as complete scripts. Fragments using `agent` assume an Agent configured by your application. Follow each feature's wheel or development-build notice.

## Stream observations

`turn.events()` observes the captured Turn's work, including its owned continuations. Run the observer alongside `Turn.get_result()`. The following complete script prints observed text while waiting for the result, then stops its observer. Save it as `stream.py` and run `python stream.py` with the interpreter and credentials from SDK setup:

```python
import asyncio
from contextlib import suppress

from agents import Agent, Turn
from agents.observation import OutputTextDelta


async def print_text(turn: Turn[str]) -> None:
    async for event in turn.events():
        if isinstance(event, OutputTextDelta):
            print(event.delta, end="", flush=True)


async def main() -> None:
    agent = Agent(instructions="Answer briefly.")
    async with agent.session() as session:
        turn = await session.submit("Explain the label atlas.")
        observer = asyncio.create_task(print_text(turn))
        try:
            result = await turn.get_result()
        finally:
            observer.cancel()
            with suppress(asyncio.CancelledError):
                await observer
        print()
        print("Final answer:", result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

The script prints text deltas as they arrive, then prints the final answer after the turn finishes. The final answer can repeat text already printed from deltas.

### Read observations safely

Use `session.events()` to observe the whole Session until it ends. Session-wide lifecycle and gap records do not identify a submission.

The default observation buffer keeps only a limited number of recent events. A slow reader receives `EventGap` when older records have been dropped. Use observations to display progress, and use completed results and recorded history to check accepted output.

Matching [development builds](installation.md#use-development-branch-examples) accept `observation_retention="bounded"` (the default) or `observation_retention="all"`. The `0.24.0-alpha.7` wheel does not accept this keyword. Use full retention when a reader needs to start after work finishes:

```python
async with agent.session(observation_retention="all") as session:
    turn = await session.submit("Explain the result.")
    result = await turn.get_result()
    async for event in turn.events(after=0):
        print(event.type, event.sequence)
    print(result.final_output)
```

Full retention keeps every emitted observation in memory while references to the Session remain reachable, so memory use grows with event payloads. It cannot recover records that the runtime never supplied. Text deltas also arrive before final output validation, so displayed text can precede a failed result.

The same development-build option supports reading observations after an asynchronous one-shot run. Keep the run and reader on the same event loop:

```python
result = await agent.run("Explain the result.", observation_retention="all")
async for event in result.turn.events():
    print(event.type, event.sequence)
print(result.final_output)
```

`run_sync()` returns a `Completed` result whose `final_output`, `thread_usage`, and optional `state` remain available after return. Its Turn belongs to the event loop used for the run. To read observations, use an asynchronous run on the same loop as the reader; do not move a `run_sync()` Turn into a new `asyncio.run()` call.

`TurnFinished` marks the end of one native step. Accepted steering can start a continuation after that step. Use `await turn.get_result()` to wait for the captured root and its continuations, including when observations have been dropped.

### Reconnect an observer

Record `event.sequence` to resume an observer with `events(after=last_seen)`. The sequence belongs to one live Session and its in-memory buffer. It is not a durable restart token. Reopening from a profile or checkpoint starts a fresh buffer and sequence. Checkpoints do not store the retention choice, so select it for each new Session. Recorded history retains earlier conversation records.

This async helper reads one record, then disconnects only its reader. Call it again with the returned sequence to resume on the same open `session`:

```python
from contextlib import aclosing

from agents import Session
from agents.observation import EventGap, OutputTextDelta


async def read_one(session: Session, last_seen: int = 0) -> int:
    async with aclosing(session.events(after=last_seen)) as events:
        event = await anext(events, None)
        if event is None:
            return last_seen
        if isinstance(event, EventGap):
            items = [item async for item in session.items()]
            print("Recovered history:", items)
        elif isinstance(event, OutputTextDelta):
            print(event.delta, end="", flush=True)
        return event.sequence
```

Use the recovered history to refresh your application's view after a gap; the missing observations cannot be replayed. Choose what history your application may display or log.

An existing `turn.events()` iterator ends when its owned steps settle. Keep reading `session.events()` to observe later background completions. To replay a late completion still in the buffer, open a fresh Turn cursor with `turn.events(after=last_seen)`. The detailed execution-completion observations require `0.24.0-alpha.7` or later; see [completed execution](#inspect-completed-execution).

## Inspect observations and retained history

### Provider frames and text identity

Codex parses the provider stream before the SDK receives events and can discard malformed frames. Its text events do not retain the provider's wire item ID. `OutputTextDelta.item_id` identifies Codex's active native item but does not authenticate the wire ID. You cannot use observations to validate raw frames.

Providers must send each text delta before its output item closes. Codex handles provider events that arrive out of order.

### Reply to live requests

Use `session.interactions()` for requests that your application can answer. These requests are separate from passive observations. [Reply to the exact live request](approvals.md#reply-to-live-manual-requests), or use `turn.wait_for_request_or_result()` to wait for a captured root's requests or completion.

For requests that must survive closing the Session, [save a settled manual request](state.md#save-a-settled-manual-request).

### Identify child lifecycle records

`SubagentLifecycle` records describe Session-wide child changes and do not identify a submission. Before interacting with a child, [read its current lifecycle state](delegation.md#observe-child-results).

### Interpret tool events

`ToolStarted` and `ToolFinished` cover declared Python tools and HTTP or stdio MCP tools on the root and its descendants. They do not cover native built-in tools. `session.events()` includes root and child work. Filter tool records by submission identity.

The records contain `family` (`"function"` or `"mcp"`), submission identity, native turn and call IDs, and the model-visible name and namespace. They omit arguments, results, and private error text.

Event timing depends on the tool family:

- For a Python function, `ToolStarted` means native code accepted the call, before argument validation. `ToolFinished` follows native reply acceptance or local cleanup.
- For MCP, `ToolStarted` means native handling began, possibly before approval or server execution. `ToolFinished` reports `"completed"` for success or `"failed"` for an error, including `isError: true`. Result policies run before this end event.

An MCP result policy can edit the output body, but cannot clear a server's `isError: true` flag. The model receives the accepted body, but `ToolFinished` still reports the failure. A failed tool can still lead to a successful final answer. Check the tool status and submission outcome separately.

Cancelling an observer leaves tool work running. Native cancellation may produce no MCP end event. Use `await turn.get_result()` for the exact turn's final outcome. Check your application records to learn which effects occurred before an error or cancellation.

### Inspect completed execution

By default, `ExecutionItemCompleted` from `agents.observation` reports the kind, status, and IDs of a completed command, patch, or MCP call in `event.item`.

For a trusted reader, pass `include_execution_details=True` to `session.events()` or `turn.events()`. Command details include `argv`, `cwd`, and `exit_code`. Patch details include the changes. MCP details include the arguments and accepted result.

Match the event item's native `thread_id`, `turn_id`, and `call_id` to the typed completion from `session.items()`. If another row uses the event item's `id`, the typed history item can have a different `id` as its history cursor.

`session.items()` retains provider `function_call` and `function_call_output` rows alongside typed execution items. After an SDK upgrade, the same retained history can yield additional provider rows. After an `EventGap` or a reopen, read the retained items. Pass `subagent_id` to read a child's history.

Execution items carry a call ID and a status when the runtime records them. Command records can include the original `argv`, working directory, exit code, and output. Patch records can include the changes and output. MCP records can include arguments and the result or error. Core can shorten command output and MCP results in history. An interrupted call can lack a completion item. When a field or completion is missing, treat that evidence as unknown and check your application records before assuming the outcome.

### Inspect response attempts

Retained response facts and the `ModelRequestDispatching` and `ModelResponseLinked` observations require `0.24.0-alpha.7` or later. Use a [matching preview wheel](installation.md) for this recipe.

`session.turns()` can include `RecordedTurn.responses`, a list of retained `RecordedResponse` occurrences. A provider can report `status="completed"` while `get_result()` fails because the SDK rejects its output. Inspect both outcomes. Save the following complete script as `response_diagnostics.py` and run it with the interpreter and credentials from [SDK setup](index.md#before-you-begin):

```python
import asyncio

from agents import Agent, NativeError, Turn
from agents.observation import ModelRequestDispatching, ModelResponseLinked


async def main() -> None:
    dispatches = {}
    links = {}

    async def observe(turn: Turn[str]) -> None:
        async for event in turn.events():
            if isinstance(event, ModelRequestDispatching):
                dispatches[event.request_id] = event
            elif isinstance(event, ModelResponseLinked):
                key = (event.thread_id, event.turn_id, event.response_observation_id)
                links[key] = event.request_id

    agent = Agent(instructions="Answer briefly.")
    async with agent.session() as session:
        turn = await session.submit("Reply with the word atlas.")
        observer = asyncio.create_task(observe(turn))
        try:
            await turn.get_result()
            print("SDK result completed")
        except NativeError as error:
            print("SDK result failed:", error.category, error.response_integrity_reason)
        await observer

        async for recorded_turn in session.turns():
            for response in recorded_turn.get("responses", []):
                key = (response["thread_id"], response["turn_id"], response["id"])
                request_id = links.get(key)
                dispatch = dispatches.get(request_id)
                print(
                    {
                        "occurrence_id": response["id"],
                        "provider_response_id": response["response_id"],
                        "provider_status": response.get("status"),
                        "incomplete_reason": response.get("incomplete_reason"),
                        "usage_source": response["usage_source"],
                        "usage": response["usage"],
                        "request_id": request_id,
                        "dispatched_model": dispatch.model if dispatch else None,
                    }
                )


if __name__ == "__main__":
    asyncio.run(main())
```

Interpret the records as follows:

- `id` identifies a retained occurrence. `response_id` comes from the provider and can repeat across occurrences. Join `ModelResponseLinked.response_observation_id` to the occurrence `id`, retaining its thread and turn identity.
- `usage_source="received"` preserves counters the provider supplied. Missing counters stay unknown; a reported zero is zero. `"native_normalized"` describes older accounting records and does not prove which counters arrived from the provider.
- `status` and `incomplete_reason` are optional provider terminal facts. Older records can omit them, and older turns can omit `responses` entirely. Missing response data does not prove that no model request occurred.

`ModelRequestDispatching` describes resolved root sampling fields before entering the transport client. Its model, reasoning settings, service tier, wire format, and transport help compare client dispatch with a [settings receipt](#check-applied-instructions-and-settings). A `None` wire field means the client omitted it; the server's default remains unknown. Transport retries share a `request_id`; later client invocations have new IDs.

Dispatch can precede encoding failure or any socket write. It does not prove provider receipt, success, or billing. The observations cover root sampling, including separate output-correction roots with their parent submission link. They exclude native children, compaction, warmup, and background or reviewer requests. Restored manual work emits fresh events; earlier dispatch events are not replayed.

Event readers follow the selected retention policy. The runtime can omit source records. Cancellation or retirement can suppress a link even when history retains the response. A missing join stays unknown. Retained responses do not guarantee complete attempt coverage and are not a billing ledger. This example prints identifiers and counters; apply your application's data policy before logging them.

## Diagnose response integrity failures

The read-only `NativeError.response_integrity_reason` and its public `ResponseIntegrityReason` type are available in `0.24.0-alpha.7` or later. Use a [matching preview wheel](installation.md) to inspect this field. It identifies the captured cause of a supported-output failure, even when several failures share the same generic error message or category:

| Reason | Meaning |
| --- | --- |
| `"undecodable_output_item"` | The response contained an output item the runtime could not decode as supported output. |
| `"provider_owned_tool_receipt"` | The provider supplied a tool-result receipt that belongs to the runtime's tool execution. |
| `"unfinished_assistant_item"` | The response ended with an assistant item still unfinished. |
| `"missing_supported_terminal_output"` | The response ended without the supported terminal output needed to complete the work. |

The value can be `None` when no cause was captured, including restored or ambiguously attributed errors. It does not replace `category` or provider metadata. A reason does not prove that earlier tools had no effects: an already-started tool can finish before the SDK reports the failure. Check history and application receipts before deciding whether to submit new work.

See [response-attempt diagnostics](#inspect-response-attempts) to compare retained provider status with the SDK's result.

## Check applied instructions and settings

Read `recorded_turn.get("receipt")` from `session.turns()` to inspect the settings selected for the turn's latest recorded model sampling attempt. The receipt can include:

- The model, reasoning effort, Codex revision and source tree, and whether user configuration applied.
- The executor and approval policy, including the environment ID, transport, working directory, explicit write roots, and network settings.
- Loaded instruction file paths, source categories, and SHA-256 hashes, plus the hash of the developer instructions.

Live Sessions and trusted `LocalStorage` report these details for each executor the step selected. On `LocalStorage` reopen, the SDK compares the full native policy for a local executor or a strict WSS Environment. An explicitly selected legacy WSS Environment follows the [relocation and model-only reopen rules](state.md#restore-executor-policy).

Interpret `receipt["executors"]` as follows:

- `[]` means the observed step selected no executor.
- After portable restore, `None` means the checkpoint withheld executor policy details. Other recorded settings remain available.

Use the file hash to check the version of a method file the runtime loaded. The receipt describes the runtime's settings. The provider can still reject or route the request. To verify later tool reads or external effects, use their records separately. Treat missing receipts or fields as unknown.
