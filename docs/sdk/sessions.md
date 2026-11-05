# Run and observe conversations

<a id="conversations-events-and-state"></a>

A Session owns one native conversation. Submit each prompt as a Turn, wait for its result, and submit the next prompt in the same Session to keep the conversation history. Open the Session with an async context manager so its callbacks and native work close together.

To continue after closing the Session, [save a checkpoint or use local storage](state.md#choose-how-to-store-a-conversation).

First complete the [SDK setup](index.md#before-you-begin), including model credentials. Save this complete script as `conversation.py`:

```python
import asyncio

from agents import Agent


async def main() -> None:
    agent = Agent(instructions="Answer briefly.")
    async with agent.session() as session:
        first = await session.submit("Remember the label atlas.")
        await first.get_result()
        second = await session.submit("What label did I give you?")
        result = await second.get_result()
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

Run `.venv/bin/python conversation.py` on Linux/macOS or `.\.venv\Scripts\python.exe conversation.py` on Windows. The script prints an answer recalling `atlas`; the model chooses the wording.

Later examples with `await` or `async with` are fragments for an async function unless marked as complete scripts. Use the same installed SDK and credentials, and follow the version notices when opening other pages.

## Submit and wait for work

`submit()` returns a handle to the root `Turn`. Use `await turn.get_result()` in the event loop where you opened the Session. The call waits for that root's work and its native continuations.

The handle has these lifetime rules:

- A later independent submission cannot change its result. You can read the result again after later work starts or the Session closes.
- A Turn cannot move to another event loop.
- Cancelling a reader leaves the root running. Closing the Session waits for cleanup.

### Queue or steer active work

If root work is active, choose how to submit a new prompt:

- The default raises `SessionBusyError`.
- `await session.submit(prompt, if_busy="queue")` queues work in the order submitted.
- `if_busy="steer"` steers the active root and returns the same Turn after native acceptance. Refused steering is not queued.

You can also use `await turn.steer(prompt)` for a captured root. Accepted steering may start a native continuation after a model step ends. `await turn.get_result()` waits for that continuation too. Do not resubmit input the root has retained.

#### Steer one exact root turn

Use `turn.steer_exact()` when a supervisor must target the native root turn it observed. Take the ID from `TurnStarted` on that root's event stream. The fragment below observes a captured `turn`.

```python
from contextlib import aclosing

from agents import StaleTurnError
from agents.observation import TurnStarted

async with aclosing(turn.events()) as events:
    async for event in events:
        if isinstance(event, TurnStarted):
            try:
                receipt = await turn.steer_exact(
                    "Keep the output in the assigned workspace.",
                    expected_turn_id=event.turn_id,
                )
            except StaleTurnError:
                # The observed native turn is no longer active.
                break
            print(receipt.turn_id, receipt.input_id)
            break
```

A `SteeringReceipt` confirms that the exact native turn accepted the input. The model may finish before it reads that input. Strict steering never starts a continuation, and cancelling the wait can leave acceptance unknown. Keep one steering mode per native root turn. To submit new work after a stale target, make a separate application decision.

For child work, see [native child turns](#native-child-turns).

### Wait for a decision or a result

Use `await turn.wait_for_attention()` when the application needs to act only on live requests or a settled outcome. Ordinary tool activity stays on the observation stream.

| Return value | What the application receives |
| --- | --- |
| `Completed` | The turn's final output and available usage. |
| `PendingRequests` | Live requests owned by this root. Respond to each exact request. Use `session.interactions()` to include child requests. |
| `TurnNeedsAttention` | A settled interruption, lost connection, blocking quota, or temporary rate limit. The `reason` distinguishes these outcomes. |

Other failures still raise. If a connection is lost, check retained history and your application's records of tool effects before continuing. To set a deadline, [bound the wait and stop owned work](troubleshooting.md#bound-a-wait-and-stop-owned-work).

## Stream observations

`turn.events()` observes the captured Turn's work, including its owned continuations. Run the observer alongside `Turn.get_result()`. The following complete script prints observed text while waiting for the result, then stops its observer. Save it as `stream.py` and run it with the same interpreter and credentials as `conversation.py`:

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

Observations are passive, and the SDK retains a limited number. A slow reader can receive `EventGap` after the SDK drops records. Text deltas can differ from final output. Use the completed result and canonical history as the source of truth.

`TurnFinished` marks the end of one native step. Accepted steering can start a continuation after that step. Use `await turn.get_result()` to wait for the captured root and its continuations, including when observations have been dropped.

## Interrupt work

### Stop a root or child

To stop a captured root, use `await turn.cancel(mode="force")`. The call stops that root's callbacks and cells and waits for their cleanup. If the root has already finished or a later root has replaced it, the call does nothing. It cannot stop a later root. Native children can continue independently.

A child Turn cannot use this root method. Use `await child.try_cancel(turn)` with the captured child Turn. A return value of `True` confirms native interruption and completion of SDK-owned callback and cell cleanup. A return value of `False` means the SDK could not confirm native interruption. Cancelling this waiter still waits for cleanup. Noncooperative callbacks can delay the return.

### Stop the whole Session

If you own the whole Session, use `await session.cancel_all(mode="force")` to stop root and descendant work and wait for cleanup. Use `mode="graceful"` on either `turn.cancel()` or `session.cancel_all()` to let accepted work reach its next boundary. A pending manual request may still need a reply. Force cancellation or Session closure can end that wait.

Cancellation does not undo completed effects. Closing the Session waits for the native cleanup it owns and reports cleanup failures. A root result does not prove that independent children or all background work have finished.

## Native child turns

A root Turn and a child Turn have separate lifetimes. Waiting for the root does not wait for independent children, and a captured child handle cannot control the root. Read [child lifecycle and results](delegation.md#observe-child-results) before steering, cancelling, or resuming a child.

## Observation details

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

## Failures and recovery

`NativeError` exposes available provider metadata as `status_code`, `code`, and `request_id`. Missing or discarded values remain `None`. `category` is the normalized native failure category, which differs from the provider's `code`. Messages and metadata can contain provider-supplied information, so choose what your application logs.

`error.effects` describes only the exact failed operation. Its value is `"not_started"`, `"recorded"`, or `"unknown"`. It does not describe earlier tool effects or promise to undo them. An unknown outcome does not mean that retrying is safe. Before deciding how to recover, inspect canonical history and your application's records of external effects.

| Exception | Meaning |
| --- | --- |
| `SessionBusyError` | An operation requires an idle Session, but work is active. Wait for the owned Turn, or explicitly queue or steer work where supported. |
| `StaleTurnError` | The captured Turn cannot control its former owner or current work. Read current handles instead of redirecting the old one. |
| `InteractionExpiredError` | A request no longer carries reply authority. Inspect current live requests. |
| `TurnStoppedError` | The root stopped and its owned cleanup finished. Earlier effects remain possible. |
| `CapabilityError` | The operation requires capabilities that this Session has not been granted. |
| `LocalRecoveryRequired` | Local history contains recorded calls with unknown outcomes. Check [local recovery](state.md#recover-after-interruption) before reopening. |

These types are public imports from `agents`. `InteractionRequired` can carry a supported manual checkpoint; see [manual continuation](approvals.md#continue-a-one-shot-manual-request).

## Typed input

Pass a string or one `UserMessage` dictionary. Its `role` is `"user"`; its `content` is a string or a nonempty ordered sequence of `InputText` and `InputImage` dictionaries. These types use the Responses field names:

```python
from agents import InputText, UserMessage

text: InputText = {"type": "input_text", "text": "Explain the label atlas."}
message: UserMessage = {"role": "user", "content": [text]}
result = await agent.run(message)
```

An `InputImage` uses `type="input_image"` and an `image_url` containing a base64 image data URL. Omit `detail` or set it to `"auto"`; other detail values, remote URLs, file IDs and local paths are unsupported. Codex decodes and prepares the image.

Do not pass manual results to `Session.submit()`. Reply to the exact live `ActionRequested` with `await request.respond(value)`, using the declared output type. If validation fails, the request remains available, so you can retry while it remains live. Request identity keeps root and child replies separate even if native call IDs match.

## Update tools, instructions and model settings

Declare the available tools when you open the Session. By default, the Session uses the Agent's tools. `agent.session(tools=[...])` replaces that opening tool list for this Session. Include `NativeAgents()` in the list if this Session needs subagents.

### Update default tools

While the Session is idle, use `await session.update(tools=[...])` to set the default tools for later turns. You can also redefine Python or manual tools with opening names, or replace MCP declarations for opening server and raw-tool pairs.

Later submissions select tools as follows:

- `submit(prompt)` and `submit(prompt, tools=None)` use the last accepted default.
- `submit(prompt, tools=[...])` can select tools admitted at opening even if absent from that default. It uses their last accepted bindings and does not change the next turn's default.

For example, after opening with `[lookup, summarize]`:

```python
await session.update(tools=[lookup])
first = await session.submit("Summarize the note.", tools=[summarize])
await first.get_result()
second = await session.submit("Look up its source.")
result = await second.get_result()
```

Updates cannot add new names or MCP pairs. Open a new Session for those. Temporary turn selections cannot install bindings. Selecting an original descriptor uses its last accepted replacement. See [idle replacement](tools.md#replace-tools-between-turns).

Explicit selections must retain fixed native capabilities. `tools=[]` fails if it would remove one. [Per-step selection](#select-tools-before-each-model-step) has additional limits.

### Update instructions or model settings

While idle, use separate calls for each category:

- `await session.update(instructions="Answer in Spanish.")` changes application instructions. An empty string clears them.
- `await session.update(model=..., model_settings=...)` changes the model or settings.
- `await session.update(tools=[...])` changes tools. All children must be closed first.

`None` leaves a category unchanged. Updates apply only to this Session. Sessions opened with `NativeAgents` cannot change instructions or model settings.

The update waits for native acceptance. If you cancel the call, it waits until the update finishes. An accepted change remains applied. If the SDK cannot confirm whether the update took effect, the Session rejects further work. Exit its async context to finish cleanup, then open a new Session.

For instruction matching and trust during restore, see [store and trust checkpoint data](state.md#store-and-trust-checkpoint-data).

### Check applied instructions and settings

Pass `instruction_files=[Path("method.md")]` to `agent.session()` to load trusted UTF-8 files as part of the application instructions. The SDK reads these paths on the application host. Choose them in trusted code. When restoring a portable checkpoint, supply matching instructions and file content.

Read `recorded_turn.get("receipt")` from `session.turns()` to inspect the settings the runtime selected for the turn's latest recorded model sampling attempt. The receipt can identify the model, reasoning effort, Codex revision and source tree, whether user configuration applied, and the selected executor and approval policy. The executor entry shows the selected environment ID, transport, working directory, explicit write roots and network settings. The receipt lists loaded instruction file paths, source categories and SHA-256 hashes, along with the hash of the developer instructions.

Live Sessions and trusted `LocalStorage` report these details for each executor the step selected. When the latest recorded receipt lists a local executor or a WSS Environment with explicit write roots, the SDK compares its full native policy on `LocalStorage` reopen. An implicit WSS Environment follows the [relocation and model-only reopen rules](state.md#restore-executor-policy). In `receipt["executors"]`, `[]` means the observed step selected no executor. After portable restore, `None` means the checkpoint withheld executor policy details. The other recorded settings remain available.

Use the file hash to check the version of a method file the runtime loaded. The receipt describes the runtime's settings. The provider can still reject or route the request. To verify later tool reads or external effects, use their records separately. Treat missing receipts or fields as unknown.

### Select tools before each model step

To select tools before each model step, pass `before_step` to `submit()`, `agent.run()`, or `agent.run_sync()`. The callback receives the root `Turn` as its positional argument and `step_index` as a keyword argument. The callback may be synchronous or asynchronous.

- Return `None` to use the operation's tool selection.
- Return a sequence to narrow the admitted tools.

`step_index` starts at zero and counts preparations in that Turn. Cancellation can stop a preparation before it sends a model request.

This example opens a Session with only one Python tool. It permits that tool for the first preparation and no tools for later preparations:

```python
from agents import Agent, Turn, function_tool


@function_tool
async def lookup(key: str) -> str:
    return f"Saved label: {key}"


def choose_tools(turn: Turn[str], *, step_index: int):
    return [lookup] if step_index == 0 else []


agent = Agent(instructions="Use lookup once, then answer.", tools=[lookup])
async with agent.session() as session:
    turn = await session.submit("Look up atlas.", before_step=choose_tools)
    result = await turn.get_result()
```

A selector has these limits:

- It cannot add excluded tools, install or replace bindings, or change the operation's individual MCP tools.
- It must retain each fixed native capability, including the opening Environment, `NativeAgents`, `Checklist`, `NativeGoals`, and other fixed declarations when present.
- With programmatic tool calling, each explicit selection must include `ToolRef("exec", "functions")` and `ToolRef("wait", "functions")` from `agents`.

An operation with `before_step` cannot be suspended because portable state does not retain its callback.

## Current checklists and goals

Declare `Checklist()` to enable native `update_plan`. Read the current root list with `await session.checklist()` or a descendant's list with `await session.checklist(subagent_id=child_id)`. These calls return `None` before the first update. After an update, the returned `ChecklistState` contains the native steps and statuses:

```python
from agents import Agent, Checklist

agent = Agent(instructions="Keep a checklist for this task.", tools=[Checklist()])
async with agent.session() as session:
    turn = await session.submit("Plan and explain a small documentation review.")
    await turn.get_result()
    checklist = await session.checklist()
```

Declare `NativeGoals()` separately to enable native `create_goal`, `get_goal` and `update_goal`. Goals require an explicit `LocalStorage` profile. Codex owns the goal's status, token use and elapsed-time accounting. Inspect the current goal with `await session.goal()`; it returns `None` before a goal exists:

```python
from pathlib import Path

from agents import Agent, LocalStorage, NativeGoals

profile = LocalStorage(Path("conversations"))
agent = Agent(instructions="Track the requested task as a goal.", tools=[NativeGoals()])
async with agent.session(storage=profile) as session:
    saved_id = session.id
    turn = await session.submit("Create a goal to review this document, then complete the review.")
    await turn.get_result()
    goal = await session.goal()
```

After closing the Session, [reopen the local conversation](state.md#reopen-a-conversation-from-local-storage) with `agent.session(storage=profile, resume=saved_id)` and declare `NativeGoals()` again. Reopening the Session and reading the goal do not start a turn or resume goal work automatically. `NativeGoals` does not enable a checklist. Explicit tool selections for an operation or step must retain each granted planning declaration. Saved planning data alone grants neither tool.

## Save, restore, or compact a conversation

<a id="choose-how-to-store-a-conversation"></a>

[Choose conversation storage](state.md#choose-how-to-store-a-conversation).

<a id="reopen-a-conversation-from-local-storage"></a>
<a id="recover-after-interruption"></a>

[Reopen a local conversation](state.md#reopen-a-conversation-from-local-storage) or [reconcile an interrupted call](state.md#recover-after-interruption).

<a id="compact-save-and-restore"></a>
<a id="supply-restore-configuration"></a>
<a id="retain-or-update-the-saved-model"></a>
<a id="use-one-shot-runs"></a>
<a id="restore-executor-policy"></a>
<a id="network-policy-and-offline-executors"></a>
<a id="store-and-trust-checkpoint-data"></a>
<a id="check-support-and-capture-readiness"></a>

[Save and restore a portable checkpoint](state.md#save-and-restore-a-portable-checkpoint), [match restore configuration](state.md#supply-restore-configuration), or [check capture readiness](state.md#check-support-and-capture-readiness).

<a id="compact-and-inspect-history"></a>
<a id="supply-a-trusted-summary"></a>
<a id="read-compaction-and-history-records"></a>
<a id="fork-a-conversation"></a>

[Compact conversation context](state.md#compact-conversation-context), [inspect history](state.md#inspect-recorded-items-and-turns), or [fork a conversation](state.md#fork-a-conversation).

<a id="retained-checkpoint-data"></a>
<a id="saving-a-settled-manual-request"></a>
<a id="check-manual-checkpoint-support"></a>
<a id="suspend-and-restore"></a>
<a id="resume-children-and-one-shot-runs"></a>
<a id="protect-saved-data-and-effects"></a>

[Save and restore pending manual requests](state.md#save-a-settled-manual-request) or [inspect retained planning data](state.md#inspect-retained-planning-data).
