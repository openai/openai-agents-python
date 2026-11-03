# Conversations, events and state

A Session owns one native conversation. Open it with an async context manager so its callbacks and native work close together.

First complete the [current-source setup](index.md#before-you-begin), including model credentials. Save this complete script as `conversation.py`:

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

`submit()` returns a handle to the root `Turn`. Use `await turn.get_result()` in the event loop where you opened the Session. The call waits for that root's accepted work and native continuations, including accepted steering. A later independent submission cannot change the result. You can read the result again after later work starts or the Session closes. A Turn cannot move to another event loop. Cancelling a reader leaves the root running. Closing the Session waits for cleanup.

Submitting a new prompt while root work is active raises `SessionBusyError` by default. Use `await session.submit(prompt, if_busy="queue")` to queue work in the order submitted. Use `if_busy="steer"` to steer the active root. This call returns the same Turn after native acceptance. If native code refuses the steering request, the call does not queue it. You can also use `await turn.steer(prompt)` for a captured root. Accepted steering may start a native continuation after a model step ends. `await turn.get_result()` waits for that continuation too. Do not resubmit input the root has retained.

For conversations that delegate work, see [native child turns](#native-child-turns).

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

Use `session.events()` to observe the whole Session. The stream remains open until the Session ends. Session-wide lifecycle and gap records do not identify a submission. Observations are passive, and the SDK retains a limited number. A slow reader can receive `EventGap` after the SDK drops records. Text deltas can differ from final output. Use the completed result and canonical history as the source of truth. Use `Turn.get_result()` to detect completion even if the SDK dropped an observation.

## Interrupt work

To stop a captured root, use `await turn.cancel(mode="force")`. The call stops that root's callbacks and cells and waits for their cleanup. If the root has already finished or a later root has replaced it, the call does nothing. It cannot stop a later root. Native children can continue independently.

A child Turn cannot use this root method. Use `await child.try_cancel(turn)` with the captured child Turn. A return value of `True` confirms that the native turn has ended. It does not guarantee that Python callbacks or background cells have finished cleanup. A return value of `False` means the SDK could not confirm native interruption. To wait for cleanup, cancel or close the whole Session when you own that scope.

If you own the whole Session, use `await session.cancel_all(mode="force")` to stop root and descendant work and wait for cleanup. Use `mode="graceful"` on either `turn.cancel()` or `session.cancel_all()` to let accepted work reach its next boundary. A pending manual request may still need a reply. Force cancellation or Session closure can end that wait.

Cancellation does not undo completed effects. Closing the Session waits for the native cleanup it owns and reports cleanup failures. A root result does not prove that independent children or all background work have finished.

## Choose how to store a conversation

Choose storage before opening the Session. Start with an ordinary Session for a conversation that only needs to stay open in the current application process.

| Need | Use | What the application retains |
| --- | --- | --- |
| Continue talking while the Session stays open | `async with agent.session()` | The live Session, which owns the conversation until it closes. |
| Save a portable snapshot and restore it in a new Session | `SessionState` from `await session.save_state()` | The checkpoint in trusted storage, serialized with `state.to_json()` when needed. |
| Reopen a native conversation from a local profile directory | [`LocalStorage`](reference.md#localstorage) | The profile directory and `session.id`; reopen with the same profile and `resume=saved_id`. |

Portable checkpoints do not copy workspace files or live processes. A `LocalStorage` Session has one native writer at a time. It cannot export portable state with `save_state()`, `suspend()`, or `fork()`. Do not combine `storage` or `resume` with portable `state`. Both restore methods require current application tools, context, and provider configuration. Saved history grants no credentials or permissions.

Use the local profile recipe below to reopen from a directory. For portable `SessionState` snapshots, continue to [compact, save and restore](#compact-save-and-restore).

## Reopen a conversation from local storage

First complete the [current-source setup](index.md#before-you-begin), including model credentials. Save this complete script as `local_conversation.py`:

```python
import asyncio
from pathlib import Path

from agents import Agent, LocalStorage


async def main() -> None:
    profile = LocalStorage(Path("conversations"))
    agent = Agent(instructions="Answer briefly and remember this conversation.")

    async with agent.session(storage=profile) as session:
        turn = await session.submit("Remember the label atlas.")
        await turn.get_result()
        saved_id = session.id

    # The first Session is closed before reopening the same conversation.
    async with agent.session(storage=profile, resume=saved_id) as reopened:
        turn = await reopened.submit("What label did I give you?")
        result = await turn.get_result()
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

Run `.venv/bin/python local_conversation.py` on Linux/macOS or `.\.venv\Scripts\python.exe local_conversation.py` on Windows. The script keeps native conversation data in `conversations` under the current directory and prints an answer recalling `atlas`; the model chooses the wording.

This script demonstrates closing and reopening within one application process. To reopen after a process restart, store `saved_id` in trusted application storage and retain the profile directory. Supply the same directory and current application configuration when reopening. See the [LocalStorage reference](reference.md#localstorage) for configuration, writer and portability constraints.

Local storage retains history, but it cannot recover every interrupted conversation. Reopening fails with `RuntimeError` if the saved history contains a function call with an unknown outcome. For example, the process may have crashed after an external effect but before recording its result. Reopening also fails if a saved active child's runtime cannot be restored. No new model request starts in either case.

Before starting another conversation, preserve the profile and check your application's records to find which external effects occurred. Repeating `resume` does not resolve an unknown outcome. The SDK provides no public operation to repair that saved history, and checking external effects does not repair it.

## Compact, save and restore

Portable checkpoints require the same pinned Codex revision as the SDK that saved them. This applies to idle checkpoints and pending manual continuations. Restore rejects a different revision, even if the checkpoint JSON format is recognized. Keep the matching SDK wheel available for saved conversations you need to resume. Changing the checkpoint's `codex_revision` field does not migrate its contents.

```python
async with agent.session() as session:
    handle = await session.submit("Remember the label atlas.")
    await handle.get_result()
    state = await session.save_state()

# The original Session is now closed.
async with agent.session(state=state) as restored:
    handle = await restored.submit("What label did I give you?")
    result = await handle.get_result()
```

Save ordinary conversation checkpoints after the Session becomes idle. A checkpoint preserves native history and turn identity. Restore uses isolated storage and the callbacks, application context, and provider credentials supplied when you open the new Session. The Python and manual tool definitions and latest accepted application instructions must match the saved values. MCP servers, credentials, and selected tools come from the new Session's `tools=` declarations, which default to the Agent's tools when omitted. These declarations can omit an old MCP server or narrow its tool selection. They also set the new Session's tool-name limit. Handles and workspace files do not move to the restored Session. `save_state()` requires an idle Session. Use `suspend()` for the limited manual-request case described below.

Portable restore retains the saved model and accepted model settings for both idle and pending manual checkpoints. A different model or setting on the new Agent does not replace the saved values. When opening a Session with `state`, omit `model` and `model_settings` or set them to `None`. The SDK rejects any other value, including `model_settings={}`. To change the model or settings, restore first, wait until the Session is idle, then use an update:

```python
async with agent.session(state=state) as restored:
    await restored.update(model="gpt-5.2", model_settings={"reasoning_effort": "low"})
    handle = await restored.submit("Continue with the saved conversation.")
    result = await handle.get_result()
```

This example assumes an idle checkpoint. Resume a pending manual turn before updating. A Session opened with `NativeAgents` cannot change its model or model settings.

`Agent.run()` and `Agent.run_sync()` also accept `state`. Pass ordinary input for an idle checkpoint. To resume a supported pending manual checkpoint, pass `None` and an `on_interaction` handler. Set `return_state=True` to save a checkpoint after successful completion in `Completed.state`. `return_state` must be a bool. By default, `Completed.state` is `None`. If saving fails, the run fails. Unfinished child work can prevent saving. Live processes do not transfer, and saving does not undo completed effects.

### Network policy and offline executors (unreleased)

Restore matches the normalized `Environment.network` policy even if no command used the network. The checkpoint's policy field stores only a digest of the policy. The current application must supply the matching policy. Saved data cannot grant access. A missing digest matches an omitted Environment or `network=None`, preserving older checkpoints. An explicit empty `NetworkPolicy()` is distinct from no policy. The SDK rejects a mismatch before connecting to the executor. To change the policy, open a new Session without the checkpoint. [Network and local socket access](tools.md#network-and-local-socket-access)

For the Docker example, the authenticated gateway calls `open_exec_stdio` again before attaching to the retained job. That function checks the actual Docker network mode and attached networks before launching the exec-server in the same container. Keep the trusted Docker job record separate from the Session checkpoint. The checkpoint cannot choose the Docker daemon or container. [Docker creation and restore checks](tools.md#docker-creation-and-restore-checks)

To run a Windows named-pipe command after restore, supply the current `Environment` with `network` omitted. A checkpoint does not carry open pipe handles. [Windows named pipes in an offline job](tools.md#windows-named-pipes-in-an-offline-job)

To store a checkpoint across processes, serialize it with `state.to_json()` and load it with `SessionState.from_json()` from `agents.state`. The read-only `state.instructions` accessor returns the latest accepted instructions from the checkpoint and validates the stored history. Use it only when your application trusts checkpoint storage as its instruction source. Construct a fresh Agent for each restore so one conversation cannot change a declaration shared by others.

```python
from pathlib import Path

from agents import Agent
from agents.state import SessionState


async def save_conversation(path: Path) -> None:
    agent = Agent(instructions="Answer briefly.")
    async with agent.session() as session:
        handle = await session.submit("Remember the label atlas.")
        await handle.get_result()
        await session.update(instructions="Answer in Spanish.")
        state = await session.save_state()
    path.write_text(state.to_json(), encoding="utf-8")


async def continue_conversation(path: Path) -> str:
    state = SessionState.from_json(path.read_text(encoding="utf-8"))
    agent = Agent(instructions=state.instructions)
    async with agent.session(state=state) as restored:
        handle = await restored.submit("What label did I give you?")
        result = await handle.get_result()
    return result.final_output
```

Choose `path` in trusted application storage. Using `state.instructions` trusts the checkpoint as the source for application instructions. It does not confirm that current application policy still permits those instructions. If your application requires current policy, supply its independently configured instructions and accept an error if they do not match. Restore always requires an exact match. Do not edit native history or `agent_options` to supply instructions or credentials.

When restoring an idle checkpoint, use a fresh Agent with matching Python and manual tool definitions. If the original Agent uses typed output, match its output type too. Select its current MCP declarations and provider, then pass application context to `agent.session(state=state, context=...)`. Saved text never selects those credentials, callbacks, or permissions. A pending manual turn has a narrower capture rule: see [saving a settled manual request](#saving-a-settled-manual-request).

Keep checkpoints in trusted application storage because their history contains instructions and may contain private application data. Compacted checkpoints require native replacement history with verifiable application instructions. Except for the settled manual case below, restore does not support active histories or histories the native runtime cannot verify. Idle checkpoints can include native child histories. Restoring those histories requires current `NativeAgents` and matching Python and manual tool definitions. Older goal and checklist data does not start work or grant tools. Restore creates fresh handles and does not resume interrupted child work automatically. Checkpoints do not back up workspace files.

### Check support and capture readiness

`session.capabilities` describes support in the current accepted configuration. `get(feature)` returns `FeatureSupport` with `status`, `reason` and `scope`. Status is `"supported"`, `"limited"` or `"unsupported"`. `require(feature)` raises `CapabilityError` for unsupported or limited support; pass `allow_limited=True` to accept a limited contract. These checks perform no discovery or model work and grant no permissions.

```python
support = session.capabilities.get("manual_suspend")
print(support.status, support.reason, support.scope)
session.capabilities.require("prompted_output", allow_limited=True)
print(session.capabilities.websockets_allowed)

readiness = await session.checkpoint_status(kind="idle")
print(readiness.status, readiness.reasons)
```

`websockets_allowed` reports whether the provider permits WebSockets. It does not report the negotiated transport. `checkpoint_status(kind="idle")` checks whether the Session can save an idle checkpoint now. Use `kind="manual_wait"` to check whether it can save a settled manual request. The returned status is `"ready"`, `"not_ready"`, or `"unknown"`, with reasons. A capability report does not prove that the Session can save a checkpoint now. A readiness check reserves nothing. `save_state()` and `suspend()` check again.

### Compact and inspect history

While the Session is idle, use `await session.compact()` to ask Codex to compact its conversation context. The call returns `None` after native work finishes. Codex generates the summary and replaces the history. The Agent's output schema does not apply to this summary. Cancelling the call stops its native work and waits for cleanup. Closing the Session waits for the same cleanup. Submission, tool replacement, and checkpoint operations cannot run during compaction. Cancellation does not undo history changes that Codex has already committed.

If the application already has a trusted summary, pass it explicitly:

```python
await session.compact(summary="The user chose the label atlas. No tool work remains.")
```

A supplied summary must be nonempty. Codex uses it to replace model context without calling a model or creating a Turn. The operation preserves canonical history, including audit history, and current application instructions. It requires an idle root that supports portable checkpoints and has no native descendants, background work, or unresolved tool effects. `LocalStorage` does not support this operation. With programmatic tool calling, complete history must prove that no code VM has run. If the programmatic history records code VM execution or has already been compacted, use model-generated `compact()` instead. If you cancel the call, it waits until the summary update finishes. An accepted summary remains applied.

Session observations include `ContextCompacted` from `agents.observation`, with the native item and turn IDs. The pinned Codex history format retains the summary, completed turn, and compacted replacement history. It does not retain the observation's compaction item ID. Use `session.items()` to iterate over recorded summary messages and `session.turns()` to iterate over the completed compaction turn. The observation also reports native automatic compaction during a submitted turn.

Both history methods return async iterators over a snapshot. They have no pagination arguments:

```python
async for item in session.items():
    print(item)
async for recorded_turn in session.turns():
    print(recorded_turn)
```

Pass `subagent_id=child_id` to read a descendant's recorded items or turns, including restored outcomes. These reads do not start native work or create live `Turn` objects.

### Fork a conversation

`await session.fork()` returns state for an independent conversation and leaves the source usable. Restore that state with the current Agent, callbacks, context, provider and Environment:

```python
forked = await session.fork()
async with agent.session(state=forked) as branch:
    turn = await branch.submit("Explore another answer from this point.")
    result = await turn.get_result()
```

The source must be idle and support portable checkpoints, with no native descendants, unfinished work, or background processes. `LocalStorage` cannot be forked. Use `save_state()` for an idle snapshot that includes descendants. Later source turns do not enter the fork. Neither a fork nor a checkpoint copies workspace files, live handles, or credentials. The destination supplies its current bindings, which must pass the normal restore checks.

## Native child turns

A completed root does not imply that native children have finished. Read descendants with `async for child in session.subagents()`, then use `await child.turn.get_result()` when `child.turn` is present. This child handle cannot control the root. Use `Subagent.start()` only when the child is idle. To steer an active child turn, use `await child.turn.steer(...)`. Child controls are separate from Session root controls.

A captured child Turn waits only for its exact native turn. Accepted child steering can create a separate successor turn. The original handle does not wait for that successor. Observe `session.events()` or read a fresh `session.subagents()` snapshot to find the child's new Turn. Waiting on the original Turn or rereading an old Subagent snapshot starts no work. Interruption preserves recorded history, but may discard input that has not yet been recorded. Read [interrupt work](#interrupt-work) before deciding how to continue after a stop.

See [native children](tools.md#native-children) for declarations and delegation tools.

## Observation details

Codex parses the provider stream before the SDK receives events. Codex can discard malformed frames, and its text events do not retain the provider's wire item ID. `OutputTextDelta.item_id` identifies Codex's active native item. It does not authenticate the wire item ID. You cannot use the observation stream to validate raw frames. Providers must send each text delta before its output item closes. Codex handles provider events that arrive out of order.

To supply tool results manually, declare a `FunctionTool` without a callback. `session.interactions()` delivers live `ActionRequested` and supported native request values. These requests let you reply while they remain live. They are separate from passive events and have no replay sequence. Answer an `ActionRequested` with `await request.respond(value)`. You can also use `await turn.wait_for_request_or_result()` to wait for that Turn's pending requests or completion.

`Turn.wait_for_request_or_result()` returns either `Completed` or `PendingRequests`, both public imports from `agents`. A `PendingRequests` value contains the captured `turn` and a tuple of live `requests`. This snapshot does not reserve the requests. Another consumer can answer a request before your application does. After answering the current batch, wait again to receive later requests or completion.

This async fragment declares one manual tool, matches each request to that declaration and supplies a string result:

```python
from pydantic import BaseModel

from agents import (
    Agent,
    Completed,
    FunctionTool,
    InteractionExpiredError,
    PendingRequests,
)


class LookupArgs(BaseModel):
    key: str


lookup = FunctionTool(
    name="lookup",
    description="Read a saved label.",
    input_type=LookupArgs,
    output_type=str,
)
agent = Agent(instructions="Use lookup to read the label.", tools=[lookup])
async with agent.session() as session:
    turn = await session.submit("Look up atlas.")
    while True:
        outcome = await turn.wait_for_request_or_result()
        if isinstance(outcome, Completed):
            print(outcome.final_output)
            break
        assert isinstance(outcome, PendingRequests)
        for request in outcome.requests:
            manual = lookup.match(request)
            if manual is None:
                raise TypeError("Unexpected interaction for this tool selection")
            try:
                await manual.respond(f"Saved label: {manual.arguments.key}")
            except InteractionExpiredError:
                continue
```

The root Turn's wait includes requests from its continuations, but excludes child requests. Use `session.interactions()` or `session.pending_interactions()` to handle requests across the Session and its children. Match manual requests with their `FunctionTool.match()` method. Other native request types need their own reply handling. Closing the Session prevents replies to outstanding requests. Cancelling a waiter leaves the Turn running.

`ToolStarted` and `ToolFinished` cover declared Python tools and HTTP or stdio MCP tools on the root and its descendants. `session.events()` includes root and child work. Filter observations using their submission identity.

For `SubagentLifecycle`, `kind="created"` identifies a newly spawned child. The other kinds are `"active"`, `"closed"`, and `"snapshot"`. A snapshot can describe restored history, and its `status` can describe a restored child that is not running. Before interacting with a child, read a fresh `session.subagents()` snapshot and inspect `is_live`. These Session-wide records do not identify a submission.

`ToolStarted` and `ToolFinished` carry `"function"` or `"mcp"` as `family`, plus submission identity, native turn and call IDs, and the model-visible name and namespace. These tool records omit arguments, results and private error text.

For a Python function, `ToolStarted` means native code accepted the call, before argument validation. `ToolFinished` follows native reply acceptance or local cleanup. For MCP, `ToolStarted` means native handling began, possibly before approval or server execution. `ToolFinished` reports Codex's result with `"completed"` for success or `"failed"` for an error, including a result with `isError: true`. Result policies run before this end event.

An MCP result policy can edit the output body, but cannot clear a server's `isError: true` flag. The model receives the accepted body, but `ToolFinished` still reports the failure. A failed tool can still lead to a successful final answer. Check the tool status and submission outcome separately.

Cancelling an observer does not cancel the tool call. Native cancellation may produce no MCP end event. Use `await turn.get_result()` for the exact turn's final outcome. An error or cancellation does not prove that no external effect occurred or that an effect was undone. These tool records do not cover native built-in tools.

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

These types are public imports from `agents`. `InteractionRequired` can carry a supported manual checkpoint; see [manual continuation](tools.md#continue-a-one-shot-manual-request).

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

While the Session is idle, use `await session.update(tools=[...])` to set the default tools for later turns. You can also redefine Python or manual tools with names declared at opening, or replace MCP declarations for server and raw-tool pairs declared at opening. `submit(prompt)` and `submit(prompt, tools=None)` use the last accepted default. An explicit `submit(prompt, tools=[...])` can select tools admitted at opening even if they are absent from that default. The call uses their last accepted bindings and does not change the next turn's default. For example, after opening with `[lookup, summarize]`:

```python
await session.update(tools=[lookup])
first = await session.submit("Summarize the note.", tools=[summarize])
await first.get_result()
second = await session.submit("Look up its source.")
result = await second.get_result()
```

Updates cannot add new names or MCP pairs. Open a new Session for those. Temporary tool selections for a turn or `before_step` cannot install new bindings. Selecting an original descriptor uses its last accepted replacement. See [idle replacement](tools.md#idle-replacement) for an example. Explicit selections must retain fixed native capabilities. `tools=[]` fails if it would remove one. `before_step` can change the Python tool selection but must retain the operation's individual MCP tools and fixed native capabilities. An operation with `before_step` cannot be suspended.

While the Session is idle, use `await session.update(instructions="Answer in Spanish.")` to change application instructions. An empty string clears them. Use `await session.update(model=..., model_settings=...)` to change the model or settings. Use separate calls to update tools, instructions, and the model or its settings. `None` leaves a category unchanged. These changes apply only to this Session. Sessions opened with `NativeAgents` cannot change instructions or model settings. All children must be closed before you update tools.

The update waits for native acceptance. If you cancel the call, it waits until the update finishes. An accepted change remains applied. If the SDK cannot confirm whether the update took effect, the Session rejects further work. Exit its async context to finish cleanup, then open a new Session.

When restoring, the current Agent's instructions must match the latest accepted instructions. Use `state.instructions` only when trusted checkpoint storage is your application's instruction source. Otherwise, supply current application instructions. The SDK rejects a mismatch.

To select tools before each model step, pass `before_step` to `submit()`, `agent.run()`, or `agent.run_sync()`. The callback receives the root `Turn` as its positional argument and `step_index` as a keyword argument. The callback may be synchronous or asynchronous. Return `None` to use the operation's tool selection, or return a sequence that narrows the admitted tools. The index starts at zero and counts preparations in that Turn. Cancellation can stop a preparation before it sends a model request.

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

A selector cannot add tools excluded from the operation, replace bindings, or change its individual MCP tools. The selector must retain each fixed native capability. When programmatic tool calling is enabled, include `ToolRef("exec", "functions")` and `ToolRef("wait", "functions")` from `agents` in each explicit selection. Keep the opening Environment, `NativeAgents`, `Checklist`, `NativeGoals`, and other fixed declarations when present. An operation with `before_step` cannot be suspended because portable state does not retain its callback.

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

After closing the Session, reopen it with `agent.session(storage=profile, resume=saved_id)` and declare `NativeGoals()` again. Reopening the Session and reading the goal do not start a turn or resume goal work automatically. `NativeGoals` does not enable a checklist. Explicit tool selections for an operation or step must retain each granted planning declaration. Saved planning data alone grants neither tool.

## Retained checkpoint data

Older saved Sessions can contain `root_goal`, `root_checklist`, and a `goal` or `checklist` for each descendant. Formats 2 and 3 retain these values across save and restore, including recorded accounting. Restoring this data does not grant model planning tools, restart a goal, or resume interrupted work. Descendant histories still require current `NativeAgents` and their matching tool declarations.

The checkpoint value types live in `agents.state`. Keep the checkpoint in trusted storage and continue through the regular Session methods.

## Saving a settled manual request

Use `await session.suspend()` when all remaining work consists of validated manual requests. These requests are settled when automatic operations and callbacks have finished, even though the requests still need replies. A callback or cleanup failure prevents the checkpoint. A successful suspend retires the original Session and expires its live requests. Repeating `suspend()` returns the same state. `save_state()` requires an idle Session. Restore with `agent.session(state=state)` and reply to the fresh live requests from that Session.

After restoring a pending manual request, use `restored.restored_turn` to obtain the initially restored root. This accessor returns `None` for fresh Sessions and idle restores. After a pending manual restore, it keeps returning the same initial handle even when later work starts. `session.active_turn` returns the current root or `None`. Neither accessor preserves the output type because individual turns may override it.

`await session.pending_interactions()` returns a fresh snapshot of live requests from the root and its children. The snapshot does not reserve a request. Another consumer can answer first. Reply through the exact fresh request. Handle expiry if another consumer has answered or the Session has closed.

Manual checkpoints support HTTP and WebSocket inference with plain-text or supported `PromptedOutput` Agent output. They retain completed tool results, including typed data, inline images, and file patch results. An explicit Session can also declare MCP tools, executor tools, and native agents. A completed executor call must leave no live background terminal. Pending approvals, unanswered MCP questions, and unfinished result policies prevent saving.

Children must have finished their turns and have no live processes or pending decisions. Saving stops any remaining idle child runtimes. It retains their histories, planning data, and active or closed status. Restore does not load those child runtimes. The model can resume them later through `followup_task` without replaying their completed work.

A settled manual turn with `PromptedOutput(Model)` can be suspended and restored when its output contract matches. Restore with the same mode, schema, and retry setting. For a per-turn output override, use `agent.with_output_type(...)` with the saved turn's output type. Old checkpoints without an output fingerprint cannot be restored as prompted output. A turn with `before_step` cannot be suspended because its callback is not saved. An output repair still in progress also prevents saving.

A pending turn with `NativeOutput(Model)`, or a bare structured type such as `output_type=Model`, cannot be suspended or restored. The pinned runtime cannot resume that turn with its native schema. Keep the Session open and reply there, or save after completion. Idle restore and live typed manual replies remain supported. The Agent's final output limit does not prohibit typed results from manual tools.

Programmatic tool calling may stay enabled if no code cell has run in the root or any child. The history must be complete enough to prove that condition. Otherwise, saving the manual checkpoint fails before the original Session is retired. Code cells can retain JavaScript state that a checkpoint cannot restore.

An ordinary one-shot run with only Python or manual tools and plain-text output can raise `InteractionRequired` with a saved manual checkpoint even without a handler. That automatic save supports fewer combinations than an explicit Session. See [one-shot manual continuation](tools.md#continue-a-one-shot-manual-request). Resume either supported checkpoint with `agent.run(None, state=state, on_interaction=answer)`. Restoring a pending checkpoint without a handler retains the supplied state in `InteractionRequired`. New tools or `before_step` cannot replace an already admitted turn. Calling `turn.get_result()` waits for the result but does not save state.

Restore retains the saved model and accepted model settings, the original turn and call IDs, and the order of accepted results. Restore does not call completed tools again. It uses the current credentials, transport choice, tool declarations, reply types, and permissions, including current `NativeAgents` when descendants are present. You can switch between HTTP and WebSockets. The Agent's instructions must match the latest accepted instructions. Restore gives the new Session fresh requests. Saved handles and live replies do not transfer.

Saving again retains the checkpoint's goals, checklists and native history. Inspect planning data through the saved state.

Manual checkpoints can contain tool arguments, accepted results, and other private application data. Store them with the same controls as ordinary checkpoints. A checkpoint lets another Session continue from accepted history. It does not coordinate concurrent processes or guarantee durable execution. If an effect occurred but its result was never accepted, a retry may repeat that effect.
