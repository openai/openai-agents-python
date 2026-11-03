# Conversations, events and state

> **Version:** These guides describe the v1 source branch. Build the SDK from the same commit as these guides. See [installation and version compatibility](versions.md) for v0 and v1.

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

`submit()` returns the captured root `Turn`. Use `await turn.get_result()` in the event loop where the Session opened. It waits for that root's accepted work and owned native continuations, including steering. A later independent submission cannot change its result. You can read the same result again after later work starts or the Session closes. A Turn cannot move to another event loop. Cancelling a reader leaves the root running; closing the Session joins cleanup.

Submitting a new prompt while root work is active raises `SessionBusyError` by default. `await session.submit(prompt, if_busy="queue")` registers new work in FIFO order. `if_busy="steer"` steers the active root and returns that same Turn only after a native receipt; it does not queue on refusal. You can also use `await turn.steer(prompt)` for a captured root. Accepted steering may start a native continuation after a model step ends; `await turn.get_result()` follows it. Do not resubmit retained input.

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

The script can print text deltas as they arrive, then prints the settled final answer. Deltas are observations, so the final answer can repeat text already printed.

Use `session.events()` to observe the whole Session; it remains open until that Session ends. Session-wide lifecycle and gap records have no submission identity. Observations are passive and bounded; a slow reader can receive `EventGap` after records are dropped. Text deltas can differ from final output. Use the settled result and canonical history as authority, and use `Turn.get_result()` to detect completion even if an observation was dropped.

## Interrupt work

For the captured root, use `await turn.cancel(mode="force")`. It stops and joins that root's callbacks and cells; a settled or superseded root is a no-op and cannot stop a later root. Native children can continue independently.

A child Turn cannot use this root method. Use `await child.try_cancel(turn)` with its captured child Turn. `True` confirms a native terminal state, but does not promise that Python callbacks or background cells have been joined. `False` means native interruption was not confirmed. For joined cleanup, use whole-Session cancellation or closure when you own that scope.

If you own the whole Session, use `await session.cancel_all(mode="force")` to stop and join root and descendant work. Use `mode="graceful"` on either operation to let admitted work reach its next boundary. A pending manual request may still need a reply; force cancellation or Session closure can end that wait.

Cancellation does not roll back completed effects. Session closure joins its owned native cleanup and reports cleanup failures. A root result is not proof that independent children or all background work have finished.

## Choose how to store a conversation

Choose storage before opening the Session. Start with an ordinary Session for a conversation that only needs to stay open in the current application process.

| Need | Use | What the application retains |
| --- | --- | --- |
| Continue talking while the Session stays open | `async with agent.session()` | The live Session, which owns the conversation until it closes. |
| Save a portable snapshot and restore it in a new Session | `SessionState` from `await session.save_state()` | The checkpoint in trusted storage, serialized with `state.to_json()` when needed. |
| Reopen a native conversation from a local profile directory | [`LocalStorage`](reference.md#localstorage) | The profile directory and `session.id`; reopen with the same profile and `resume=saved_id`. |

Portable checkpoints do not copy workspace files or live processes. A `LocalStorage` Session has one native writer at a time and cannot export portable state with `save_state()`, `suspend()` or `fork()`. Do not combine `storage` or `resume` with portable `state`. Both restore methods require current application tools, context and provider configuration; saved history grants no credentials or permissions.

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

Local storage retains history, but it cannot recover every interrupted conversation. Reopening fails with `RuntimeError` if the saved history contains a function call with an unknown outcome, such as a crash after an external effect but before its result was recorded. Reopening also fails if a saved active child's runtime cannot be restored. No new model request starts in either case.

Preserve the profile and use your application's records to check which external effects occurred before starting another conversation. Repeating `resume` does not resolve an unknown outcome, and the SDK provides no public operation to repair that saved history. Checking external effects does not repair the saved history.

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

Ordinary conversation checkpoints are saved after the Session becomes idle. A checkpoint preserves native history and turn identity. Restore uses the callbacks, application context and provider credentials supplied when opening the new Session, with isolated storage. The saved Python and manual tool definitions and latest accepted application instructions must match those supplied when opening. MCP servers, credentials and selected tools come from the opening Session's `tools=` declarations, which default to the Agent's tools when omitted. These declarations can omit an old MCP server or narrow its tool selection, and set the new Session's tool-name limit. Handles and workspace files do not move to the restored Session. Use `suspend()` for the limited manual-request boundary described below; `save_state()` is idle-only.

Portable restore retains the saved model and accepted model settings for both idle and pending manual checkpoints. A different model or setting on the new Agent does not replace them. With `state`, omit non-`None` opening `model` and `model_settings` arguments; even `model_settings={}` is rejected. To change them, restore first, wait until the Session is idle, then use an update:

```python
async with agent.session(state=state) as restored:
    await restored.update(model="gpt-5.2", model_settings={"reasoning_effort": "low"})
    handle = await restored.submit("Continue with the saved conversation.")
    result = await handle.get_result()
```

This example assumes an idle checkpoint. Resume a pending manual turn before updating. A Session opened with `NativeAgents` cannot change its model or model settings.

`Agent.run()` and `Agent.run_sync()` also accept `state`. Pass ordinary input for an idle checkpoint. To resume a supported pending manual checkpoint, pass `None` and an `on_interaction` handler. Set `return_state=True` to save successful completion in `Completed.state`. `return_state` must be a bool. By default, `Completed.state` is `None`. If saving fails, the run fails. Saving can fail if child work remains unfinished. Live processes do not transfer, and capture does not undo completed effects.

### Network policy and offline executors (unreleased)

Restore matches the normalized `Environment.network` policy even if no command used the network. The checkpoint's policy field stores only a digest of the policy. The current application must supply the matching policy. Saved data cannot grant access. A missing digest matches an omitted Environment or `network=None`, preserving older checkpoints. An explicit empty `NetworkPolicy()` is distinct from no policy. The SDK rejects a mismatch before connecting to the executor. To change the policy, open a new Session without the checkpoint. [Network and local socket access](tools.md#network-and-local-socket-access)

For the Docker example, the authenticated gateway calls `open_exec_stdio` again before attaching to the retained job. That function checks the actual Docker network mode and attached networks before launching the exec-server in the same container. Keep the trusted Docker job record separate from the Session checkpoint. The checkpoint cannot choose the Docker daemon or container. [Docker creation and restore checks](tools.md#docker-creation-and-restore-checks)

To run a Windows named-pipe command after restore, supply the current `Environment` with `network` omitted. A checkpoint does not carry open pipe handles. [Windows named pipes in an offline job](tools.md#windows-named-pipes-in-an-offline-job)

For storage across processes, serialize with `state.to_json()` and load with `SessionState.from_json()` from `agents.state`. The read-only `state.instructions` accessor reads the latest accepted instructions from the checkpoint and validates the stored history. Use it only when trusted application checkpoint storage is the intended instruction source. Construct a fresh Agent for each restore so one conversation cannot change a declaration shared by others.

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

Choose `path` in trusted application storage. Selecting `state.instructions` explicitly trusts the checkpoint as the source for application instructions; it is not independent confirmation that today's application policy still permits those instructions. An application requiring current policy must supply its independently configured instructions instead and accept a mismatch error. Restore always checks the exact match. Native history and `agent_options` must not be edited to supply instructions or credentials.

When restoring an idle checkpoint from an Agent that uses Python or manual tools or typed output, construct the fresh Agent with matching Python and manual tool definitions and output type. Select its current MCP declarations and provider, then pass application context to `agent.session(state=state, context=...)`. Saved text never selects those credentials, callbacks, or permissions. A pending manual turn has a narrower capture rule: see [saving a settled manual request](#saving-a-settled-manual-request).

Keep checkpoints in trusted application storage because their history contains instructions and may contain private application data. Compacted checkpoints require native replacement history with verifiable application instructions. Apart from the settled manual case below, active and unverifiable histories remain outside the restore contract. Idle checkpoints can include native child histories; restoring them requires current `NativeAgents` and matching Python and manual tool definitions. Older goal and checklist data remains inert. Restore creates fresh handles and does not resume interrupted child work automatically. Checkpoints do not back up workspace files.

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

`websockets_allowed` reports provider permission, not the negotiated transport. `checkpoint_status(kind="idle")` checks the current idle capture boundary; use `kind="manual_wait"` for settled manual capture. Its status is `"ready"`, `"not_ready"` or `"unknown"`, with reasons. A capability report does not prove current capture readiness. A readiness probe reserves nothing, and `save_state()` or `suspend()` checks again.

### Compact and inspect history

Use `await session.compact()` while the Session is idle to ask Codex to compact its conversation context. The call returns `None` after native work settles. For this model-generated compaction, Codex owns the summary and history replacement; the Agent's output schema does not apply. Cancelling the call stops and joins its native work. Closing the Session joins the same cleanup. Submission, tool replacement and checkpoint operations cannot run at the same time as compaction. Cancellation does not undo history changes that Codex has already committed.

If the application already has a trusted summary, pass it explicitly:

```python
await session.compact(summary="The user chose the label atlas. No tool work remains.")
```

A supplied summary must be nonempty. It replaces model context without calling a model or creating a Turn, while preserving canonical history and current application instructions. It does not delete audit history. This operation requires a portable, idle root with no native descendants, background work or unresolved tool effects; `LocalStorage` cannot use it. With programmatic tool calling, complete history must prove that no code VM has run. If the programmatic history records code VM execution or has already been compacted, use model-generated `compact()` instead. If cancelled, this call waits until the summary update finishes; an accepted summary stays applied.

Session observations include `ContextCompacted` from `agents.observation`, with
the native item and turn IDs. The pinned Codex history format retains the summary,
completed turn and compacted replacement history, but not that observation's
compaction item ID. `session.items()` iterates recorded summary messages; `session.turns()` iterates the completed compaction turn. The observation also reports native automatic compaction when it occurs during a submitted turn.

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

The source must be idle and portable, with no native descendants, unsettled work or background processes. `LocalStorage` cannot be forked. Use `save_state()` for a settled snapshot that includes descendants. Later source turns do not enter the fork. Neither a fork nor a checkpoint copies workspace files, live handles or credentials; the destination supplies its current bindings under the normal restore checks.

## Native child turns

A completed root does not imply that native children have finished. Read descendants with `async for child in session.subagents()`, then use `await child.turn.get_result()` when `child.turn` is present. This child handle does not inherit root-wide authority. Use `Subagent.start()` only when the child is idle; steer an active turn with `await child.turn.steer(...)`. Its controls are distinct from Session root controls.

A captured child Turn waits only for its exact native turn. Accepted child steering can create a separate successor; the original handle does not follow that successor. Observe `session.events()` or read a fresh `session.subagents()` snapshot to find the child's new Turn. Waiting on the original Turn or rereading an old Subagent snapshot starts no work. Interruption preserves recorded history, but input not yet recorded may be discarded. Read [interrupt work](#interrupt-work) before deciding how to continue after a stop.

See [native children](tools.md#native-children) for declarations and delegation tools.

## Observation details

Codex parses the provider stream before the SDK receives events. It can discard
malformed frames, and its text events do not retain the provider's wire item ID.
`OutputTextDelta.item_id` identifies Codex's active native item. It does not
authenticate the wire item ID, and the observation stream is not a raw-frame
validation interface.
Providers must send each text delta before its output item closes. Codex owns
handling of out-of-order provider events.

Declare a `FunctionTool` without a callback for manual results.
`session.interactions()` delivers live `ActionRequested` and supported native
request values. These requests carry reply authority and are separate from
passive events. They have no replay sequence. Answer an `ActionRequested` with
`await request.respond(value)` while it remains live. You can also use
`await turn.wait_for_request_or_result()` to observe that Turn's pending
requests or completion.

`Turn.wait_for_request_or_result()` returns either `Completed` or `PendingRequests`, both public imports from `agents`. A `PendingRequests` value contains the captured `turn` and a tuple of live `requests`. It is a snapshot, not an exclusive claim. Another consumer can answer a request before your application does. Wait again after answering the current batch to receive later requests or completion.

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

The root Turn's wait includes requests from its owned continuations, but excludes child requests. Use `session.interactions()` or `session.pending_interactions()` to handle requests across the Session and its children. Match manual requests with their `FunctionTool.match()` method; other native request types need their own reply handling. Closing the Session ends outstanding reply authority. A cancelled waiter leaves the Turn running.

`ToolStarted` and `ToolFinished` cover declared Python tools and HTTP or stdio MCP tools on the root and its descendants. `session.events()` includes root and child work. Filter observations using their submission identity.

For `SubagentLifecycle`, `kind="created"` identifies a new spawn. The other kinds are `"active"`, `"closed"` and `"snapshot"`; a snapshot can describe restored history. Its `status` can describe a restored child that is not running. Read a fresh `session.subagents()` snapshot and inspect `is_live` before selecting a child for interaction. These Session-wide records have no submission identity.

`ToolStarted` and `ToolFinished` carry `"function"` or `"mcp"` as `family`, plus submission identity, native turn and call IDs, and the model-visible name and namespace. These tool records omit arguments, results and private error text.

For a Python function, start means native admission before argument validation;
finish follows native reply acceptance or local cleanup. For MCP, start means
native handling began, possibly before approval or server execution. MCP finish
reports Codex's result: `"completed"` for success and `"failed"` for an error,
including a result with `isError: true`. Result policies run before this end event.

An MCP result policy can edit the output body, but cannot clear a server's
`isError: true` flag. The model receives the accepted body, but `ToolFinished` still reports the failure. A failed tool can still lead to a successful final
answer. Read the tool status and submission outcome separately.

Cancelling an observer does not cancel the call. Native cancellation may produce
no MCP end event; use `await turn.get_result()` for the exact turn's final outcome.
An error or cancellation does not prove that no external effect occurred or that
an effect was undone. These tool records do not cover native built-in tools.

## Failures and recovery

`NativeError` exposes available provider metadata as `status_code`, `code` and `request_id`; missing or discarded values remain `None`. `category` is the normalized native failure category, distinct from the provider's `code`. Messages and metadata can contain provider-supplied information, so choose what your application logs.

`error.effects` describes only the exact failed operation: `"not_started"`, `"recorded"` or `"unknown"`. It does not describe earlier tool effects or promise rollback. An unknown outcome does not establish that replay is safe. Inspect canonical history and application effect records before deciding how to recover.

| Exception | Meaning |
| --- | --- |
| `SessionBusyError` | An idle-only operation encountered active work. Wait for the owned Turn or choose an explicit queue/steer operation where supported. |
| `StaleTurnError` | The captured Turn cannot control its former owner or current work. Read current handles rather than retargeting the old one. |
| `InteractionExpiredError` | A request no longer carries reply authority. Inspect current live requests. |
| `TurnStoppedError` | The root stopped and its owned cleanup settled. Earlier effects remain possible. |
| `CapabilityError` | The operation is outside this Session's admitted capabilities. |

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

Manual results are not `Session.submit()` input. Reply to the exact live
`ActionRequested` with `await request.respond(value)`, using the declared
output type. Validation failure does not consume the request, so you can retry
while that request remains live. Request identity keeps root and child replies
separate even if native call IDs coincide.

## Update tools, instructions and model settings

Declare the available tools for the opening Session. By default it uses the Agent's tools; `agent.session(tools=[...])` replaces the opening tools for this Session. Include `NativeAgents()` there if this Session needs subagents.

Use `await session.update(tools=[...])` while idle to set the default for later turns. It can also redefine Python/manual tools with opening names or replace MCP declarations for opening server/raw-tool pairs. `submit(prompt)` and `submit(prompt, tools=None)` use the last accepted default. An explicit `submit(prompt, tools=[...])` can select admitted tools absent from that default. It uses their last accepted bindings and does not change the next turn's default. For example, after opening with `[lookup, summarize]`:

```python
await session.update(tools=[lookup])
first = await session.submit("Summarize the note.", tools=[summarize])
await first.get_result()
second = await session.submit("Look up its source.")
result = await second.get_result()
```

Updates cannot add new names or MCP pairs. Open a new Session for those. Temporary turn and `before_step` selections cannot install new bindings: an original descriptor selects its last accepted replacement. See [idle replacement](tools.md#idle-replacement) for an example. Explicit selections must retain fixed native capabilities; `tools=[]` fails if it would remove one. `before_step` can change Python selection but must retain the operation's MCP leaves and fixed native capabilities. An operation with `before_step` cannot be suspended.

Use `await session.update(instructions="Answer in Spanish.")` while idle to change application instructions. An empty string clears them. Use `await session.update(model=..., model_settings=...)` to change the model or settings. Update tools, instructions, and model/settings in separate calls; `None` leaves a category unchanged. These changes apply to this Session only. Sessions opened with `NativeAgents` cannot change instructions or model settings. Tool updates require all children to have closed.

The update waits for native acceptance. Cancelling it waits for settlement; an accepted change remains applied. If the SDK cannot confirm whether the update took effect, the Session rejects further work. Exit its async context to finish cleanup, then open a new Session.

For restore, the current Agent must match the latest accepted instructions. Use `state.instructions` only when trusted checkpoint storage is your application's instruction source. Otherwise supply current application instructions; a mismatch fails.

For per-model-step selection, pass `before_step` to `submit()`, `agent.run()` or `agent.run_sync()`. The callback receives the root `Turn` as its positional argument and `step_index` as a keyword argument. It may be synchronous or asynchronous. Return `None` to use the operation's tool selection, or return a sequence that narrows the admitted tools. The index starts at zero and counts preparations in that Turn; cancellation can stop a preparation before it sends a model request.

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

A selector cannot add an operation's excluded tools, replace bindings or change its MCP leaves. It must retain each fixed native capability. When programmatic tool calling is enabled, include `ToolRef("exec", "functions")` and `ToolRef("wait", "functions")` from `agents` in each explicit selection. Keep the opening Environment, `NativeAgents`, `Checklist`, `NativeGoals` and other fixed declarations when present. An operation with `before_step` cannot be suspended because portable state does not retain its callback.

## Current checklists and goals

Declare `Checklist()` to enable native `update_plan`. Read the current root list with `await session.checklist()` or a descendant's list with `await session.checklist(subagent_id=child_id)`. Readback returns `None` before the first update. The returned `ChecklistState` contains the native steps and statuses:

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

After closing, reopen with `agent.session(storage=profile, resume=saved_id)` and declare `NativeGoals()` again. Reopening and readback do not start a turn or resume goal work automatically. `NativeGoals` does not enable a checklist. Explicit operation and step selections must retain each granted planning declaration. Saved planning data alone grants neither tool.

## Retained checkpoint data

Older saved Sessions can contain `root_goal`, `root_checklist`, and a `goal` or `checklist` for each descendant. Formats 2 and 3 retain these values across save and restore, including recorded accounting. They are inert checkpoint data. Restoring them does not grant model planning tools, restart a goal, or resume interrupted work. Descendant histories still require current `NativeAgents` and their matching tool declarations.

The checkpoint value types live in `agents.state`. Keep the checkpoint in trusted storage and continue through the regular Session methods.

## Saving a settled manual request

Use `await session.suspend()` when all remaining work consists of validated manual requests. Automatic operations and callbacks must have finished; a callback or cleanup failure prevents the checkpoint. A successful suspend retires the original Session and expires its live requests. Repeating `suspend()` returns the same state. `save_state()` is reserved for an idle Session. Restore with `agent.session(state=state)` and reply to the fresh live requests from that Session.

After a pending manual restore, use `restored.restored_turn` to obtain the initially restored root. It is `None` for fresh Sessions and idle restores, and it remains that same initial handle after later work starts. `session.active_turn` instead returns the current root or `None`. Both accessors erase the output type because individual turns may override it.

`await session.pending_interactions()` returns a fresh snapshot of live requests from the root and its children. It does not claim a request exclusively; another consumer can answer first. Reply through the exact fresh request, and handle expiry if another consumer or Session closure has ended its authority.

Manual checkpoints support HTTP and WebSocket inference with plain-text or supported `PromptedOutput` Agent output. They retain completed tool results, including typed data, inline images and file patch results. An explicit Session can also declare MCP tools, executor tools and native agents. A completed executor call must leave no live background terminal. Pending approvals, unanswered MCP questions and unfinished result policies prevent the save.

Children must have finished their turns and have no live processes or pending decisions. Saving stops any remaining idle child runtimes and retains their histories, planning data and active/closed status. Restore leaves those children unloaded. The model can resume them later through `followup_task`; their completed work is not replayed.

A settled manual turn with `PromptedOutput(Model)` can be suspended and restored when its output contract matches. Restore with the same mode, schema and retry setting; for a per-turn output override, use `agent.with_output_type(...)` with the saved turn's output type. Old checkpoints without an output fingerprint cannot be restored as prompted output. A turn with `before_step` cannot be suspended because its callback is not saved. A repair still in progress also prevents capture.

A pending turn with `NativeOutput(Model)`, or a bare structured type such as `output_type=Model`, cannot be suspended or restored. The pinned runtime cannot resume that turn with its native schema. Keep the Session open and reply there, or save after completion. Idle restore and live typed manual replies remain supported. The Agent's final output limit does not prohibit typed results from manual tools.

Programmatic tool calling may stay enabled when no code cell has run in the root or any child. The history must be complete enough to prove this. Otherwise, the manual save fails before sealing because code cells can retain JavaScript state that a checkpoint cannot restore.

An ordinary plain Python/manual one-shot run can raise `InteractionRequired` with a saved manual checkpoint even without a handler. That automatic save supports fewer combinations than an explicit Session; see [one-shot manual continuation](tools.md#continue-a-one-shot-manual-request). Resume either supported checkpoint with `agent.run(None, state=state, on_interaction=answer)`. Restoring a pending checkpoint without a handler retains the supplied state in `InteractionRequired`. New tools or `before_step` cannot replace an already admitted turn. Calling `turn.get_result()` only waits; it does not save state.

Restore retains the saved model and accepted model settings, the original turn and call IDs, and the order of accepted results. Completed tools are not called again. It uses the current credentials, transport choice, tool declarations, reply types and permissions, including current `NativeAgents` when descendants are present. You can switch between HTTP and WebSockets. The Agent's instructions must match the latest accepted instructions. Restore gives the new Session fresh requests; saved handles and live replies do not transfer.

Saving again retains the checkpoint's goals, checklists and native history. Inspect planning data through the saved state.

Manual checkpoints can contain tool arguments, accepted results and other private application data. Store them with the same controls as ordinary checkpoints. A checkpoint lets another Session continue from accepted history. It does not coordinate concurrent processes or guarantee durable execution. If an effect occurred but its result was never accepted, the checkpoint cannot guarantee that a retry will not repeat that effect.
