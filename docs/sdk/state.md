# Save and restore conversations

Save a conversation when it must survive closing its Session. Use a portable `SessionState` checkpoint, keep the native conversation in a `LocalStorage` profile, or supply an application backend with `CustomStorage`. Each requires your current application configuration when you reopen the conversation.

Complete the [SDK setup](index.md#before-you-begin), including model credentials, before running these examples.

The complete local-storage script below includes its setup. Other examples are fragments for an async function and reuse an `agent` configured by your application. See [run a conversation](sessions.md) for the Session lifecycle.

## Choose how to store a conversation

Choose storage before opening the Session. Start with an ordinary Session for a conversation that only needs to stay open in the current application process.

| Need | Use | What the application retains |
| --- | --- | --- |
| Continue talking while the Session stays open | `async with agent.session()` | The live Session, which owns the conversation until it closes. |
| Save an idle conversation and restore it in a new Session | `SessionState` from `await session.save_state()` | The checkpoint in trusted storage, serialized with `state.to_json()` when needed. |
| Save [supported pending manual requests](#save-a-settled-manual-request) for later replies | `SessionState` from `await session.suspend()` | The checkpoint. Successful suspension retires the original Session and expires its live requests. |
| Reopen a native conversation from a local profile directory | [`LocalStorage`](reference.md#localstorage) | The profile directory and `session.id`; reopen with the same profile and `resume=saved_id`. |
| Store root conversation history in an application-owned backend | `CustomStorage` with a `SessionStore` backend | The backend and `session.id`; reopen with the same backend and `resume=saved_id`. |

The storage choices have these limits:

- Portable checkpoints can retain settled native stored values and terminal code-cell observations. They do not copy workspace files, live processes, or arbitrary JavaScript heap state.
- A `LocalStorage` Session has one native writer at a time and cannot export portable state with `save_state()`, `suspend()`, or `fork()`.
- Do not combine `storage` or `resume` with portable `state`.
- Each restore method requires current application tools, context, and provider configuration. Saved history grants no credentials or permissions.

In development builds after `0.24.0-alpha.6`, `LocalStorage` rejects `programmatic_tool_calling=True` before opening work. Nested code-mode tool calls do not yet have the durable intent and result receipts needed for safe local recovery. Use a nonpersistent Session for code mode, or check the limits for [eligible settled portable checkpoints](#check-manual-checkpoint-support).

`CustomStorage`, also added after alpha.6, supports root Sessions with automatic Python tools. Use a [matching development build](installation.md#examples-and-upgrades). It rejects native tools, environments, child agents, goals, checklists, code mode, and manual tools, and cannot export or restore portable checkpoints.

A custom-store reopen with unresolved root calls raises `LocalRecoveryRequired` before Core starts. Inspect `error.calls`, `error.turns`, and `error.items`; the SDK reads this metadata under the canonical writer lease. This works even when the process stopped before application code recorded the native call IDs. Verify the external outcome, then use `CustomStorage.reconcile()` with those IDs and a truthful result. Keep local-profile `reconciled_calls` separate from custom-store reconciliation.

For the synchronous `SessionStore` contract, persistent SQLite adapter, and recovery of calls with unknown outcomes, see the [canonical storage example](https://github.com/openai/openai-agents-python/tree/feat/codex-harness/examples/codex/canonical_storage). Storage does not guarantee exactly-once external actions.

Use the local profile recipe below to reopen from a directory. For portable `SessionState` snapshots, continue to [save and restore a portable checkpoint](#save-and-restore-a-portable-checkpoint).

## Reopen a conversation from local storage

Keep the profile separate from the executor workspace. The SDK rejects a local working directory or writable root that overlaps the session store. For example, put the worker in `worker/workspace` and keep its session history in `worker/history`.

Save this complete script as `local_conversation.py`:

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

### Recover after interruption

A process can stop after a tool applies an effect and before the SDK records its result. If a retained root tool call has an unknown outcome, an ordinary reopen raises `LocalRecoveryRequired` before any new model request. Inspect `error.calls`, `error.turns`, and `error.items`. Each uncertain call identifies the original thread, turn, and call, along with its name and arguments.

Check each effect against trusted application records or the system that owns the effect. Keep the conversation closed if you cannot confirm an outcome. The SDK supports explicit reconciliation of retained root calls with a missing result. An unresolved child call or an active child whose runtime cannot be restored causes a `RuntimeError` on reopen.

Local history supports root recovery and inspection of settled child history. It does not automatically recover an active child graph or reconcile uncertain child effects. Your application owns external-effect reconciliation and decides whether to start new work. Preserve the refusal when a child outcome remains unknown; do not edit saved history to force reopening.

Suppose `call` is the only unresolved root call. Your application journal confirms that the tool recorded a handoff. Reopen the original profile with a matching result.

```python
confirmation = call.reconcile(
    outcome="applied",
    result="The application journal confirms the handoff.",
)
async with agent.session(
    storage=profile, resume=saved_id, reconciled_calls=[confirmation]
) as session:
    receipts = [
        item
        async for item in session.items()
        if item["type"] == "function_call_output"
        and item["turn_id"] == call.turn_id
        and item["call_id"] == call.call_id
        and item.get("reconciliation") == "applied"
    ]
    if len(receipts) != 1:
        raise RuntimeError("The confirmed result is missing from history.")
    turn = await session.submit("Continue from the confirmed handoff.")
    result = await turn.get_result()
```

Supply one matching confirmation per unresolved call. Use `outcome="not_applied"` only after you verify that the call had no effect and that earlier work cannot still apply it. Include only result text the model may read. The SDK writes the result for the original call without running that call again. If reopening is interrupted, inspect history again before sending more work. The SDK accepts an identical confirmation for a reconciliation it already recorded.

The SDK rejects later reuse of a reconciled call ID. A new call ID can still request the same business effect. Deduplicate effects and publication with a durable application key that identifies the operation across calls and process restarts.

## Save and restore a portable checkpoint

Restore a portable checkpoint with an SDK pinned to the same Codex revision that saved it. The SDK rejects a different revision for both idle checkpoints and pending manual continuations, even when it recognizes the checkpoint JSON format. Keep the matching SDK wheel available for saved conversations you need to resume. Changing the checkpoint's `codex_revision` field does not migrate its contents.

Portable checkpoints retain native history, including platform-specific absolute paths. Restore validates those paths on the destination platform, so moving a checkpoint between operating systems is not guaranteed. Do not rewrite saved paths to bypass validation.

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

### Check checkpoint compatibility

Treat `SessionState` JSON as opaque data for this SDK and language. The current reader retains envelope versions 2 through 7 and rejects unknown versions. Versions 2 and 3 retain legacy idle and manual state; versions 4 and 5 add the output contract; versions 6 and 7 also retain settled CodeMode state. Each format still has its own restore requirements. SDK envelope versions and native checkpoint codec versions are separate. Do not edit the version or `codex_revision` to bypass validation.

The [checkpoint capture provenance](https://github.com/openai/openai-agents-python/tree/feat/codex-harness/packages/codex/tests/fixtures/checkpoint_upgrades) records real idle, settled-manual, and CodeMode captures from a preview source snapshot and its matching native wheel. Retained captures qualify later candidates by replaying accepted history and checking current restore refusals. They do not establish a stable-release upgrade, coverage of every historical preview, cross-language conversion, downgrade support, or arbitrary native-revision portability. Keep the producer SDK available until your candidate passes the relevant continuation tests.

A recognized format does not relax the exact Codex-revision restriction or the current host, tool, output, and executor-policy checks below. Saved data supplies no current credentials, callbacks, or execution authority.

### Supply restore configuration

`save_state()` requires an idle Session and preserves native history and turn identity. Use [`suspend()`](#save-a-settled-manual-request) for supported pending manual requests.

Supply a current Agent and application configuration as follows:

- Match the saved Python and manual tool definitions, latest accepted application instructions, and typed output type when present.
- Supply current callbacks and provider credentials. Pass application context through `agent.session(state=state, context=...)`. Saved text never selects credentials, callbacks, or permissions.
- Select current MCP declarations with `tools=`, or omit it to use the Agent's tools. These declarations provide MCP servers, credentials, and selected tools. They can omit an old server or narrow its tool selection, and they set the new Session's tool-name limit.

Restore creates fresh handles. Supply workspace files separately when the conversation needs them.

#### Retain or update the saved model

Portable restore retains the saved model and accepted model settings for both idle and pending manual checkpoints. A different model or setting on the new Agent does not replace the saved values. When opening a Session with `state`, omit `model` and `model_settings` or set them to `None`. The SDK rejects any other value, including `model_settings={}`.

To change the model or settings, restore first, wait until the Session is idle, then use an update:

```python
async with agent.session(state=state) as restored:
    await restored.update(model="gpt-5.2", model_settings={"reasoning_effort": "low"})
    handle = await restored.submit("Continue with the saved conversation.")
    result = await handle.get_result()
```

This example assumes an idle checkpoint. Resume a pending manual turn before updating. A Session opened with `NativeAgents` cannot change its model or model settings.

#### Use one-shot runs

`Agent.run()` and `Agent.run_sync()` also accept `state`:

- For an idle checkpoint, pass ordinary input.
- For a supported pending manual checkpoint, pass `None` and an `on_interaction` handler.
- To save after successful completion, set `return_state=True`. The checkpoint appears in `Completed.state`, which is `None` by default. `return_state` must be a bool.

If saving fails, the run fails. Unfinished child work can prevent saving. Live processes do not transfer, and saving does not undo completed effects.

### Restore executor policy

#### Portable checkpoints

A portable checkpoint with a local executor or a strict WSS Environment requires the same executor policy on restore. This includes the strict write mode and last applied roots. New Environments default to strict mode; saved policies retain their original mode.

If the saved WSS Environment used the legacy implicit policy, restore can use a different working directory. Explicitly set `write_policy="legacy"` and leave `writable_roots=None` when restoring that policy, including older checkpoints saved before the strict default.

For an idle legacy WSS checkpoint saved with both `writable_roots=None` and `network=None`, restore has two options:

- Supply a WSS Environment with `write_policy="legacy"` and both settings still set to `None`.
- Omit the Environment.

The SDK skips the executor policy digest check for this legacy case. To use `Environment.local(...)` or a strict WSS Environment, open a new Session without this checkpoint.

The SDK rejects detectable declaration or mode mismatches before acquiring an executor. Comparing the complete applied policy can require executor inspection, but must succeed before resumed execution.

#### Local profiles

When reopening with `LocalStorage`, check which executor the latest receipt recorded. For a local executor or a strict WSS Environment, supply the same working directory, last applied write roots, and network policy. Include any replacement made with `submit(writable_roots=...)`.

Strict local-profile resume requires recorded strict-mode evidence as well as the matching applied policy. A missing marker does not prove how older work was confined, and reopening never automatically upgrades that policy.

For saved legacy or unmarked remote history, explicitly supply `write_policy="legacy"` with `writable_roots=None`. You can reopen in a different working directory if the rest of the executor policy matches. You can also omit the Environment to reopen for model-only work, including when the recorded executor used a managed network policy.

### Network policy and offline executors

Restoring a portable checkpoint requires the current application to supply the saved normalized `Environment.network` policy, even if no command used the network. The checkpoint stores only a policy digest and cannot grant access.

- A missing digest matches an omitted Environment or `network=None`, preserving older checkpoints.
- An explicit empty `NetworkPolicy()` differs from no policy.
- The SDK rejects a mismatch before connecting to the executor. To change the policy, open a new Session without the checkpoint.

See [network and local socket access](execution.md#network-and-local-socket-access).

For the [Docker example](execution.md#docker-creation-and-restore-checks), the authenticated gateway calls `open_exec_stdio` before reattaching to the retained job. The function checks Docker network mode and attached networks before launching the exec-server in the same container.

Keep the trusted Docker job record separate from the checkpoint, which cannot choose the Docker daemon or container.

To run a Windows named-pipe command after restore, supply the current `Environment` with `network` omitted. A checkpoint does not carry open pipe handles. [Windows named pipes in an offline job](execution.md#windows-named-pipes-in-an-offline-job)

### Store and trust checkpoint data

To store a checkpoint across processes, serialize it with `state.to_json()` and load it with `SessionState.from_json()` from `agents.state`.

The read-only `state.instructions` accessor returns the latest accepted instructions and validates stored history. Use it only when your application trusts checkpoint storage as its instruction source:

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

Choose `path` in trusted application storage. Using `state.instructions` trusts the saved instructions without confirming that current application policy still permits them.

If your application requires current policy, supply its independently configured instructions. Restore requires an exact match and rejects a mismatch. Do not edit native history or `agent_options` to supply instructions or credentials.

Checkpoint history contains instructions and may contain private application data. Keep it in trusted application storage. Compacted checkpoints require native replacement history with verifiable application instructions. Except for [settled manual requests](#save-a-settled-manual-request), restore does not support active histories or histories the native runtime cannot verify.

Idle checkpoints can include native child histories. Restoring them requires current `NativeAgents` and matching Python and manual tool definitions. Restore does not resume interrupted child work automatically. [Retained planning data](#inspect-retained-planning-data) does not start work or grant tools.

### Check support and capture readiness

`session.capabilities` describes support in the current accepted configuration:

- `get(feature)` returns `FeatureSupport` with `status`, `reason`, and `scope`. Status is `"supported"`, `"limited"`, or `"unsupported"`.
- `require(feature)` raises `CapabilityError` for unsupported or limited support. Pass `allow_limited=True` to accept a limited contract.

These checks perform no discovery or model work and grant no permissions.

```python
support = session.capabilities.get("manual_suspend")
print(support.status, support.reason, support.scope)
session.capabilities.require("prompted_output", allow_limited=True)
print(session.capabilities.websockets_allowed)

readiness = await session.checkpoint_status(kind="idle")
print(readiness.status, readiness.reasons)
```

`websockets_allowed` reports provider permission, not the negotiated transport.

To check whether the Session is ready to save, use `checkpoint_status(kind="idle")` for an idle checkpoint or `kind="manual_wait"` for a settled manual request. The returned status is `"ready"`, `"not_ready"`, or `"unknown"`, with reasons.

Capability support does not prove readiness. A readiness check reserves nothing, and `save_state()` and `suspend()` check again.

## Compact conversation context

While the Session is idle, use `await session.compact()` to ask Codex to compact its conversation context. The call returns `None` after native work finishes. Codex generates the summary and replaces the history. The Agent's output schema does not apply to this summary.

Cancelling the call stops its native work and waits for cleanup. Closing the Session waits for the same cleanup. Submission, tool replacement, and checkpoint operations cannot run during compaction. Cancellation does not undo history changes that Codex has already committed.

### Supply a trusted summary

If the application already has a trusted summary, pass it explicitly:

```python
await session.compact(summary="The user chose the label atlas. No tool work remains.")
```

A supplied summary must be nonempty. Codex uses it to replace model context without calling a model or creating a Turn. The operation preserves canonical history, including audit history, and current application instructions.

This operation has stricter limits than model-generated compaction:

- The root must be idle and support portable checkpoints. `LocalStorage` does not support this operation.
- No native descendants, background work, or unresolved tool effects may remain.
- With programmatic tool calling, complete history must prove that no code VM has run. If the history records code VM execution or has already been compacted, use model-generated `compact()` instead.

Cancelling the call waits for the summary update to finish. An accepted summary remains applied.

### Retain selected context items

Development builds newer than alpha.6 can keep selected original items beside a supplied summary. Capture `snapshot = await session.model_context()` while idle, choose zero-based indices from `snapshot.items`, and pass `summary=...`, `snapshot=snapshot`, and `retain=indices` together to `session.compact()`.

Pass the exact snapshot issued by the same live Session while its context is unchanged. Editing `snapshot.items` does not edit the retained originals. Duplicate indices count once, and selected items keep native order. System and developer items are always retained. Keep both a tool call and its result, or omit both. An empty selection retains no additional items. Capture a new snapshot after context changes or restore. The [supplied-summary limits](#supply-a-trusted-summary) still apply.

### Supply summaries through a callback

`Agent(before_compact=callback)` also requires a [development build newer than alpha.6](installation.md#examples-and-upgrades). The callback runs before automatic or explicit model-based compaction for roots and children. It receives `CompactionContext` from `agents`, with detached active `items`, application `context`, native identity, and `trigger="auto"` or `"manual"`.

Return a nonempty summary string to skip model summarization, or `None` to let Codex generate the summary. Include details your application still needs from discarded assistant and tool messages. Codex retains ownership of compaction and records the summary in history; the summary does not replace application instructions. A supplied `session.compact(summary=...)` bypasses this callback. Returning `None` lets `transform_context` run on the model summarization request.

Async callbacks run on the Session loop; sync callbacks run in a worker. Cancellation joins started callbacks and discards late results. A callback must not mutate its own Session. A raised exception fails compaction instead of falling back to a model summary. Restore uses current callbacks and application context.

## Inspect recorded items and turns

`ContextCompacted` from `agents.observation` reports explicit or native automatic compaction, with native item and turn IDs. The pinned history format retains the summary, completed turn, and replacement history, but not the observation's compaction item ID.

Use `session.items()` to read recorded summary messages and `session.turns()` to read the completed compaction turn.

Both history methods return async iterators over a snapshot. They have no pagination arguments:

```python
async for item in session.items():
    print(item)
async for recorded_turn in session.turns():
    print(recorded_turn)
```

Pass `subagent_id=child_id` to read a descendant's recorded items or turns, including restored outcomes. These reads do not start native work or create live `Turn` objects.

For post-alpha.6 builds, [inspect response attempts](sessions.md#inspect-response-attempts) to interpret retained provider status and counters, and correlate them with live request observations. Provider completion and SDK acceptance are separate outcomes.

## Fork a conversation

`await session.fork()` returns state for an independent conversation and leaves the source usable. Restore that state with the current Agent, callbacks, context, provider and Environment:

```python
forked = await session.fork()
async with agent.session(state=forked) as branch:
    turn = await branch.submit("Explore another answer from this point.")
    result = await turn.get_result()
```

The source must meet these conditions:

- It is idle and supports portable checkpoints. `LocalStorage` cannot be forked.
- It has no native descendants, unfinished work, or background processes. Use `save_state()` for an idle snapshot that includes descendants.

Later source turns do not enter the fork. Like a checkpoint, a fork does not copy workspace files, live handles, or credentials. The destination supplies its current bindings under the [normal restore checks](#supply-restore-configuration).

## Inspect retained planning data

Older saved Sessions can contain `root_goal`, `root_checklist`, and a `goal` or `checklist` for each descendant. Formats 2 and 3 retain these values across save and restore, including recorded accounting. Restoring this data does not grant model planning tools, restart a goal, or resume interrupted work. Descendant histories still require current `NativeAgents` and their matching tool declarations.

The checkpoint value types live in `agents.state`. Keep the checkpoint in trusted storage and continue through the regular Session methods.

## Save a settled manual request

Use `await session.suspend()` when only validated manual requests remain. Here, settled means that all automatic operations and callbacks have finished, even though manual requests still need replies. A callback or cleanup failure prevents saving.

Use `save_state()` only for an idle Session. For requests you can answer while the Session stays open, see [reply to manual requests](approvals.md#reply-to-live-manual-requests).

### Check manual checkpoint support

Manual checkpoints support HTTP and WebSocket inference with plain-text, native strict, or supported `PromptedOutput` Agent output. An explicit Session can also declare MCP tools, executor tools, and native agents. Completed tool results survive restore, including typed data, inline images, and file patch results.

Before suspending, check these limits:

- Executor calls must leave no live background terminal. Pending approvals, unanswered MCP questions, and unfinished result policies prevent saving.
- Children must have finished their turns, with no live processes or pending decisions.
- An operation with `before_step` cannot be suspended.
- An output repair still in progress prevents saving.
- Programmatic tool calling can retain settled native stored values and terminal code-cell observations in the root and children. Active code cells and their continuations cannot be captured. A checkpoint does not preserve arbitrary JavaScript heap state.

For typed output, apply these additional rules:

- `PromptedOutput(Model)` requires the same mode, schema, and retry setting on restore. Old checkpoints without an output fingerprint cannot restore prompted output.
- A settled pending `NativeOutput(Model)` turn, including a bare structured type such as `output_type=Model`, can be suspended. Restore with the same output mode and schema. The checkpoint must contain the captured native schema; a changed or missing contract is rejected before inference.
- The Agent's final output limit does not prohibit typed manual tool results.

For a per-turn output override, use `agent.with_output_type(...)` with the saved turn's output type when restoring.

### Suspend and restore

Follow this sequence:

1. Call `await session.suspend()`. Success retires the original Session and expires its live requests. Repeated calls return the same state.
2. Restore with `agent.session(state=state)` under the [restore configuration rules](#supply-restore-configuration). Supply current credentials, transport, declarations, reply types, and permissions.
3. Get fresh requests with `await session.pending_interactions()` and reply through those requests. The snapshot covers the root and children but reserves nothing. Handle expiry if another consumer answers first or the Session closes.

Restore retains the saved model, accepted model settings, original turn and call IDs, and order of accepted results. You can switch between HTTP and WebSockets. Completed tools do not run again. Saved handles and live replies do not transfer.

To find the restored root, use `restored.restored_turn`. It keeps returning that initial handle after later work starts, and returns `None` for fresh Sessions and idle restores. `session.active_turn` returns the current root or `None`. Neither accessor preserves the output type, because individual turns may override it.

### Resume children and one-shot runs

Saving stops idle child runtimes but retains their histories, planning data, and lifecycle status. A saved `"active"` status means the child remains open for future work. Its turn must have finished before suspension. Restore leaves the runtimes unloaded and requires current `NativeAgents` when descendants are present. The model can resume children through `followup_task` without replaying completed work.

Without a handler, an ordinary one-shot run can raise `InteractionRequired` with a saved manual checkpoint. This automatic save supports Python or manual tools with plain-text or native strict output. It supports fewer combinations than an explicit Session. See the [one-shot continuation example](approvals.md#continue-a-one-shot-manual-request) for the handler and calling code.

### Automatic checkpoint limits

Without `before_step`, selected manual tools without a handler cannot save automatically with any of these features:

- Programmatic tool calling or `PromptedOutput` Agent output.
- Selected MCP tools, local storage, an Environment, or `NativeAgents`.
- A `WebSearch` declaration on the opening Agent.

The SDK rejects these combinations without a checkpoint. It checks visible declarations before opening and may check saved selections or changed Session definitions afterward, before submitting work.

With `before_step`, the SDK opens the Session and checks each step's selected tools. Steps without selected manual tools can complete normally. If the model calls a selected manual tool without a handler, the run raises `InteractionRequired` with `state=None` after opening and inference. Earlier tool effects may already have occurred. Other native questions and late undeclared manual calls do not produce portable checkpoints.

For supported live interactions, use a handler. You can also open an explicit Session and use `suspend()` when the pending manual request supports suspension.

### Resume a saved manual request

Resume a supported checkpoint with `agent.run(None, state=state, on_interaction=answer)`. The `None` input resumes the existing turn. Ordinary input cannot replace a pending turn.

Without a handler, a pending restore raises `InteractionRequired` and keeps the supplied checkpoint in `error.state`. You cannot change tools or `before_step` on an already admitted turn. Restore creates fresh requests for the current declarations, so reply through those requests. Old handles do not transfer, and completed tools do not run again.

For an idle checkpoint, pass ordinary input to start a new turn. To save its result, use [`return_state=True`](#use-one-shot-runs). `turn.get_result()` waits for completion but does not save state.

For typed output and other suspension constraints, check [manual checkpoint support](#check-manual-checkpoint-support) before capturing state.

### Protect saved data and effects

Saving again retains goals, checklists, and native history. Inspect planning data through the saved state.

Manual checkpoints may contain tool arguments, accepted results, and private application data. Apply the [same storage controls as ordinary checkpoints](#store-and-trust-checkpoint-data). A checkpoint supports continuation from accepted history. It does not coordinate concurrent processes or guarantee durable execution. If an effect occurred but its result was never accepted, a retry may repeat it.
