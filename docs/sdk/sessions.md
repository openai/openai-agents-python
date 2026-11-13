# Conversations

<a id="run-and-observe-conversations"></a>

<a id="conversations-events-and-state"></a>

Open a Session to keep a conversation across prompts. Each `session.submit()` returns a Turn that tracks the submitted work, including continuations after accepted steering. Use the Turn to wait for a result, observe progress, or stop that work. Submit the next prompt in the same Session to keep the conversation history.

Open the Session with an async context manager. Closing the Session stops its remaining work and waits for callback and native cleanup.

| Task | Start here |
| --- | --- |
| Send a follow-up while work is active | [Queue or steer active work](#queue-or-steer-active-work) |
| Show text as it arrives | [Streaming and events](streaming.md) |
| Wait for application input | [Wait for a decision or a result](#wait-for-a-decision-or-a-result) |
| Stop work and wait for cleanup | [Interrupt work](#interrupt-work) |
| Change the tools, instructions, or model | [Update the Session](#update-tools-instructions-and-model-settings) |
| Continue after closing the Session | [Save a checkpoint or use local storage](state.md#choose-how-to-store-a-conversation) |

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

In your activated SDK environment, run `python conversation.py` from the directory where you saved the script. The answer should recall `atlas`. The model chooses the wording.

Later examples with `await` or `async with` are fragments for an async function unless marked as complete scripts. Use the same installed SDK and credentials, and follow the version notices when opening other pages.

## Typed input

Pass a string or one `UserMessage` dictionary. Its `role` is `"user"`; its `content` is a string or a nonempty ordered sequence of content dictionaries. `InputText` supplies text, and `InputImage` supplies an image data URL. These types use the Responses field names:

```python
from agents import InputText, UserMessage

text: InputText = {"type": "input_text", "text": "Explain the label atlas."}
message: UserMessage = {"role": "user", "content": [text]}
result = await agent.run(message)
```

An `InputImage` uses `type="input_image"` and `image_url`. For an inline image, supply a base64 image data URL; Codex decodes and prepares it. With the alpha.7 wheel, omit `detail` or use `"auto"`. A local path is not an image URL.

Do not pass manual results to `Session.submit()`. Reply to the exact live `ActionRequested` with `await request.respond(value)`, using the declared output type. If validation fails, the request remains available, so you can retry while it remains live. Request identity keeps root and child replies separate even if native call IDs match.

### Images and files in development builds

The extended media types require a [build from matching development source](installation.md#use-development-branch-examples). The alpha.7 wheel does not include them.

Development builds accept these additional content sources:

| Content type | Source fields | Detail |
| --- | --- | --- |
| `InputImage` | `image_url` with an absolute HTTP(S) URL or base64 image data URL. | Omitted, `None`, `"auto"`, `"low"`, `"high"`, or `"original"`, subject to provider and model support for the request representation and detail value. |
| `InputImageFile` | `file_id` with a provider-owned image ID. | The same detail values as `InputImage`. |
| `InputFile` | Exactly one of `file_id`, `file_data`, or `file_url`. | `file_data` is canonical base64, optionally in a base64 data URL. `file_url` must use HTTPS. `filename` is optional. |

Import these types from `agents`. A provider ID is an opaque reference, never a host file path. The SDK does not upload a local file for you. Use a provider connection and model that support the chosen source. The native runtime rejects unsupported combinations of request representation, model, and detail rather than silently changing them. For example, inline `detail="low"` is unsupported on models that use detail to size image input.

HTTP(S) image URLs and image file IDs require API-key authentication and a compatible standard Responses model. Select `Agent.model` explicitly; the current default uses Responses Lite. `CodexLogin` and Responses Lite require inline image data. The native runtime checks the current model and detail before accepting the image; see [image reference limits](tools.md#return-text-images-and-tool-errors). Local filesystem paths are unsupported.

This fragment sends a provider-owned file ID through your configured `agent`. Replace the fictional ID with one available to the provider and account serving the request:

```python
from agents import InputFile, UserMessage

attachment: InputFile = {"type": "input_file", "file_id": "your-file-id", "filename": "report.pdf"}
message: UserMessage = {
    "role": "user",
    "content": [{"type": "input_text", "text": "Summarize this report."}, attachment],
}
result = await agent.run(message)
```

Keep source URLs, embedded file data, and IDs out of routine logs. Accepted media can remain in conversation history and saved state.

## Submit and wait for work

`submit()` returns a handle to the root `Turn`. Use `await turn.get_result()` in the event loop where you opened the Session. The call waits for that root's work and its native continuations.

For new work, returning from `submit()` acknowledges local registration, before native admission or any model request. The handle's `submission_id` is immediately available for correlation. Its native `turn_id` starts as `None` and becomes available after admission; observe `TurnStarted` before relying on a native turn ID. These rules also apply to the first root in an idle Session.

Keep application receipts if acknowledgments must survive a process restart. Neither ID reconstructs a live Turn or grants permission to control it.

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

To withdraw queued work, cancel its handle. This async fragment assumes `session` has an active root waiting for an application reply, so that root stays active while the fragment runs:

```python
from agents import TurnStoppedError

queued = await session.submit("Review the next document.", if_busy="queue")
print("Application correlation:", queued.submission_id)
await queued.cancel()
try:
    await queued.get_result()
except TurnStoppedError:
    print("Cancelled before native admission:", queued.turn_id is None)
    print("Native observations:", [event async for event in queued.events()])
```

Work withdrawn before admission has no native turn or history record, and its Turn observer can finish without events. Cancelling only a result reader leaves queued work registered.

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

A `SteeringReceipt` confirms that the exact native turn accepted the input. The model may finish before it reads that input. Strict steering never starts a continuation.

Keep one steering mode per native root turn. Cancelling the wait can leave acceptance unknown. To submit new work after a stale target, make a separate application decision.

For child work, see [native child turns](#native-child-turns).

### Wait for a decision or a result

Use `await turn.wait_for_attention()` for requests owned by that Turn or a settled outcome. A Turn wait excludes genuinely Session-scoped requests, such as a URL interaction without Turn IDs. Use `agent.session(on_interaction=handler)` for general handling, or own a `session.interactions()` task and propagate that task's failures while awaiting results. Ordinary tool activity stays on the observation stream.

| Return value | What the application receives |
| --- | --- |
| `Completed` | The turn's final output and available usage. |
| `PendingRequests` | Live requests owned by this root. Respond to each exact request. Use `session.interactions()` to include child requests. |
| `TurnNeedsAttention` | A settled interruption, lost connection, blocking quota, temporary rate limit, or SDK run-budget stop (`reason="budget_exceeded"`). The `reason` distinguishes these outcomes. |

Other failures still raise. If a connection is lost, check retained history and your application's records of tool effects before continuing. To set a deadline, [bound the wait and stop owned work](troubleshooting.md#bound-a-wait-and-stop-owned-work).

## Stream observations

Read [Streaming and events](streaming.md) to display text and progress while waiting for a Turn's result. Observations are separate from [live requests](approvals.md) that your application can answer.

<a id="read-observations-safely"></a>
<a id="reconnect-an-observer"></a>

Follow [observation retention and cursor lifetimes](streaming.md#read-observations-safely) when reconnecting a reader or reading after work finishes.

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

<a id="provider-frames-and-text-identity"></a>
<a id="reply-to-live-requests"></a>
<a id="identify-child-lifecycle-records"></a>
<a id="interpret-tool-events"></a>
<a id="inspect-completed-execution"></a>

Inspect [observation identities, tool events, and recorded execution](streaming.md#inspect-observations-and-retained-history). Reply through the [live interaction API](approvals.md#reply-to-live-manual-requests).

<a id="inspect-response-attempts"></a>

Compare [retained response attempts with live request observations](streaming.md#inspect-response-attempts) when diagnosing model dispatch or provider outcomes.

## Failures and recovery

`NativeError` exposes available provider metadata as `status_code`, `code`, and `request_id`. Missing or discarded values remain `None`. `category` is the normalized native failure category, which differs from the provider's `code`. Messages and metadata can contain provider-supplied information, so choose what your application logs.

`error.effects` describes only the exact failed operation. Its value is `"not_started"`, `"recorded"`, or `"unknown"`. It does not describe earlier tool effects or promise to undo them. An unknown outcome does not mean that retrying is safe. Before deciding how to recover, inspect canonical history and your application's records of external effects.

<a id="diagnose-response-integrity-failures"></a>

For supported-output failures, inspect the [response integrity reason](streaming.md#diagnose-response-integrity-failures) together with recorded provider responses.

### Handle lifecycle errors

| Exception | Meaning |
| --- | --- |
| `SessionBusyError` | An operation requires an idle Session, but work is active. Wait for the owned Turn, or explicitly queue or steer work where supported. |
| `StaleTurnError` | The captured Turn cannot control its former owner or current work. Read current handles instead of redirecting the old one. |
| `InteractionExpiredError` | A request no longer carries reply authority. Inspect current live requests. |
| `TurnStoppedError` | The root stopped and its owned cleanup finished. Earlier effects remain possible. |
| `CapabilityError` | The operation requires capabilities that this Session has not been granted. |
| `LocalRecoveryRequired` | Local history contains recorded calls with unknown outcomes. Check [local recovery](state.md#recover-after-interruption) before reopening. |

These types are public imports from `agents`. `InteractionRequired` can carry a supported manual checkpoint; see [manual continuation](approvals.md#continue-a-one-shot-manual-request).

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

In `0.24.0-alpha.7` or later, explicit selections can omit native capabilities admitted at opening. `tools=[]` disables all model tools for that operation. Select the captured Environment or `NativeAgents` declaration, or admitted `ToolRef` values, to include executor or collaboration tools. Changing the selection does not close resources or stop already running children or processes. [Per-step selection](#select-tools-before-each-model-step) can narrow the operation's selection further.

### Update instructions or model settings

While idle, use separate calls for each category:

- `await session.update(instructions="Answer in Spanish.")` changes application instructions. An empty string clears them.
- `await session.update(model=..., model_settings=...)` changes the model or settings.
- `await session.update(tools=[...])` changes tools. All children must be closed first.

`None` leaves a category unchanged. Updates apply only to this Session. Sessions opened with `NativeAgents` cannot change instructions or model settings.

The update waits for native acceptance. If you cancel the call, it waits until the update finishes. An accepted change remains applied. If the SDK cannot confirm whether the update took effect, the Session rejects further work. Exit its async context to finish cleanup, then open a new Session.

For instruction matching and trust during restore, see [store and trust checkpoint data](state.md#store-and-trust-checkpoint-data).

### Hand off the conversation to another Agent

Matching [development builds](installation.md#use-development-branch-examples) support `Agent.handoffs` to continue the same Session conversation under another configured Agent. The `0.24.0-alpha.7` wheel does not include handoffs. Use [native children](delegation.md) when tasks need independent turns in a shared workspace.

Declare the target Agent before opening the Session, then switch Agents while the Session is idle:

```python
from agents import Agent

billing = Agent(
    name="billing",
    model="gpt-5.5",
    instructions="Answer billing questions using the conversation so far.",
)
triage = Agent(
    name="triage",
    model="gpt-5.5",
    instructions="Record the account label and answer briefly.",
    handoffs=[billing],
)
async with triage.session() as session:
    first = await session.submit("My account label is atlas.")
    await first.get_result()
    await session.update(agent=billing)
    assert await session.get_active_agent() is billing
    second = await session.submit("What account label did I give you?")
    print((await second.get_result()).final_output)
```

When the Session opens, the SDK captures the Agents reachable through `handoffs` and exposes tools that let the model select declared targets. `get_active_agent()` returns the original Agent object currently selected in native history. To switch from application code, pass a captured Agent to a separate idle `update(agent=...)` call. A new Agent object with the same name cannot replace that captured object.

Agents in the handoff graph share the Session provider, programmatic-tool-calling mode, and native runtime grants. The active Agent supplies its plain text, native structured, or prompted output contract. Structured output can use object or scalar schemas. A per-turn output override keeps its contract across handoffs. Realtime handoffs require a shared output mode and, for prompted output, a shared schema.

Each target supplies its captured instructions, model settings, callbacks, and default tools. An explicit tool selection keeps the original operation's tool limit across handoffs. Omitting the selection uses the destination's defaults; `tools=[]` disables model tools.

To restore a portable checkpoint, open it through the original graph root, even if another Agent was active when you saved it. Supply a matching graph with current callbacks and tool bindings. Saved names do not restore callbacks or grant tools. The restored active Agent is the corresponding object in the graph you supplied.

## Load trusted instruction files

Pass `instruction_files=[Path("method.md")]` to `agent.session()` to load trusted UTF-8 files as part of the application instructions. The SDK reads these paths on the application host. Choose them in trusted code. When restoring a portable checkpoint, supply matching instructions and file content.

Matching source builds also support `discover_agents_md=True` to load guidance from `AGENTS.md` files. Follow the [source setup](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/packages/codex/MAINTAINING.md#build-from-this-source) to install the matching native extension. Discovery defaults to `False`.

With discovery enabled, Codex loads user guidance from `CODEX_HOME` on the application host, defaulting to `~/.codex`. If you select an `Environment`, Codex then reads project guidance from the nearest Git root down to the Environment's working directory, within the executor's read policy. Without a Git root, Codex checks only that working directory. Without an Environment, Codex loads only user guidance.

At each location, Codex checks `AGENTS.override.md` before `AGENTS.md`. More specific project files take priority over broader guidance. Application instructions, including explicit `instruction_files`, retain developer priority. Enable discovery only for trusted files and parent directories because Codex treats discovered content as instructions. This option does not load user configuration.

For an async application using a trusted, existing workspace at `/jobs/worker/work` and a configured model provider:

```python
from pathlib import Path

from agents import Agent, Environment

agent = Agent(instructions="Work in the assigned workspace.")
environment = Environment.local(
    cwd=Path("/jobs/worker/work"),
    tools=("exec_command",),
)
async with agent.session(
    environment=environment,
    discover_agents_md=True,
) as session:
    turn = await session.submit("Describe the project layout.")
    print((await turn.get_result()).final_output)
    state = await session.save_state()

# Renewing the opt-in for this opening, including restore or storage resume.
async with agent.session(
    environment=environment,
    state=state,
    discover_agents_md=True,
) as restored:
    turn = await restored.submit("Continue with the same project guidance.")
    print((await turn.get_result()).final_output)
```

Enable discovery separately for each Session, including checkpoint restore with `state=` and storage reopen with `storage=` and `resume=`. Omitting the option disables discovery for the new Session, although saved history can still contain earlier instructions. See [local execution](execution.md#local-execution) for workspace and sandbox prerequisites.

<a id="check-applied-instructions-and-settings"></a>

Read [settings receipts](streaming.md#check-applied-instructions-and-settings) to inspect the model, instructions, and executor policy selected for a recorded sampling attempt.

## Select tools before each model step

To select tools before each model step, pass `before_step` to `submit()`, `agent.run()`, or `agent.run_sync()`. The callback receives the root `Turn` as its positional argument and `step_index` as a keyword argument. The callback may be synchronous or asynchronous.

- Return `None` to use the operation's tool selection.
- Return a sequence to narrow the admitted tools.
- Return `StopTurn(...)` from `agents` to stop the root before its next model request. After cleanup, `get_result()` raises `TurnStoppedError`; independent children can continue.

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

- It cannot add tools excluded by the operation, including MCP tools, or install or replace bindings.
- In `0.24.0-alpha.7` or later, it can omit admitted native capabilities for that preparation, including executor, collaboration, search, and planning tools.
- With programmatic tool calling, the SDK includes the `exec` and `wait` helpers for a nonempty selection. An empty selection disables the helpers and all tool calls for that preparation.

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

After closing the Session, [reopen the local conversation](state.md#reopen-a-conversation-from-local-storage) with `agent.session(storage=profile, resume=saved_id)` and declare `NativeGoals()` again. Reopening the Session and reading the goal do not start a turn or resume goal work automatically. `NativeGoals` does not enable a checklist. In `0.24.0-alpha.7` or later, explicit operation and step selections can omit a planning declaration to disable its tools for that operation or preparation. Saved planning data alone grants neither tool.

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

## Retry incomplete cleanup

Use `async with agent.session()` as the normal Session lifetime. If cleanup fails before all resources retire, the original error can retain its remaining cleanup owner. Use `get_cleanup` from `agents` to find that owner:

```python
from agents import get_cleanup

cleanup = get_cleanup(error)
if cleanup is not None:
    await cleanup.aclose()
```

Retain that owner until cleanup succeeds. If another attempt fails, retain both the primary error and the owner for a later retry. The public `aclose()` method retries complete cleanup. For an admitted Session, `aclose()` retries native retirement. It then joins the same Environment task that entered the original manager. For failed opening, the owner is the existing NativeProvider. Cleanup preserves the primary failure; linked cleanup errors remain available through its exception context.

In `0.24.0-alpha.7` or later, a failed or cancelled `CustomStorage.reconcile()` can retain a `SessionStoreCleanup` owner. Its `aclose()` retries only release of the original storage lease. It never records another result or runs the tool. Concurrent cleanup calls join the same release attempt; calls after successful release do nothing. The reconciliation result may already be recorded when release fails, so retry cleanup through this owner before reopening the Session.

[`get_cleanup(error)`](reference.md#get_cleanup) also follows exception contexts. On Python 3.10, a Task boundary can replace a `CancelledError` while retaining the original error in its context. The helper returns the existing owner and does not wrap or replace the cancellation error.

A retained Session also exposes idempotent `await session.aclose()`. Successful repeated calls do nothing. An interrupted waiter still joins owned cleanup before cancellation propagates. After a failed `run_sync()`, host code outside an event loop can capture `cleanup = get_cleanup(error)`. Call `asyncio.run(cleanup.aclose())` only when `cleanup is not None`. The cleanup owner keeps and drives the original run loop until its remaining resources retire. Do not discard a failed cleanup owner or retry the business operation before checking its effects.
