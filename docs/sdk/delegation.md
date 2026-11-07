# Delegate work to native children

Use `NativeAgents` when the model needs to delegate independent tasks within one conversation. Native children share the parent's executor workspace and selected tools, so give each child a task that can safely run alongside the others.

Before running the examples:

1. [Configure an Environment](execution.md) with `exec_command`, `write_stdin`, and `apply_patch`.
2. Choose an explicit model that supports those tools. See [model and role limits](#configure-roles-and-delegation-limits).
3. [Configure the provider](configuration.md#provider-connection).

The examples run inside an async application. The first example uses the `environment` you configured.

## Native children

Add `NativeAgents()` to `Agent.tools` or the opening `agent.session(tools=[...])` call. The child uses the parent's provider and credentials, plus the configured Environment's connection and workspace policy.

The first example waits for the root result and then closes the Session. If your application also needs results from children still running at that point, [observe those children](#observe-child-results) inside the open Session before leaving `async with`. Session exit stops the native tree.

```python
from agents import Agent, NativeAgents

agent = Agent(
    model="gpt-5.5",
    instructions="Delegate independent workspace tasks to native children.",
    tools=[NativeAgents()],
)
async with agent.session(environment=environment) as session:
    turn = await session.submit("Review the workspace using independent child tasks.")
    result = await turn.get_result()
```

### Configure roles and delegation limits

Built-in `default`, `explorer`, and `worker` roles inherit `Agent.instructions`. For separate developer instructions, import `NativeRole` from `agents` and pass `additional_roles=[NativeRole(name=..., description=..., instructions=...)]`. Additional roles cannot replace built-in roles. Full-history forks inherit the parent's role. To select another role, use `fork_turns="none"`.

Children inherit the parent's model and reasoning effort unless a role supplies compatible settings. Model selection has these limits:

- Native children require an explicit model that supports all three native executor tools. The SDK validates each declared model and reasoning effort before connecting to the executor.
- A role can set `model` and `model_settings={"reasoning_effort": ...}`. Other role model settings are unsupported.
- If the Agent enables programmatic tool calling, each selected role model must support it. Some models can run as children with programmatic tool calling disabled but cannot delegate further.
- Unknown or incompatible model metadata fails without fallback.

Set delegation limits on `NativeAgents`:

- `max_depth=1` (default) permits direct children only. Larger positive values allow further delegation but do not add model capabilities.
- `max_children=None` uses Codex's native limit. A positive value limits concurrent children across the tree, not the total created.

### Select tools for each child step

To narrow a child's tools before each model step, set `NativeAgents(before_step=select)`. The synchronous or asynchronous `select(step)` callback receives a typed `ChildStepContext`. `step.context` is the Session application context, `step.child` is the `ChildInvocation` for that exact child instance and turn, and `step.step_index` counts preparations. The callback can apply tenant policy without an external context lookup. Return `None` to use that child operation's default selection. Return a sequence to narrow the admitted tools, or `[]` to disable all model tools for that preparation. Return `StopTurn(...)` to stop that exact child turn; other children and the root can continue. The selection cannot add tools or replace their definitions.

`step.step_index` starts at zero for each child turn and counts preparations, which can end before a model request. Root turns use the separate [`before_step` callback on submission](sessions.md#select-tools-before-each-model-step).

### Shared tools and permissions

The root receives `spawn_agent`, `wait_agent`, `send_message`, `followup_task`, `interrupt_agent`, and `list_agents` in the native `collaboration` namespace. There is no programmatic child-creation method.

Children share selected executor, Python, manual, HTTP MCP, and local stdio MCP tools, application context, and policies. Before dispatch, the runtime rejects unknown roles, model-selected override arguments, and unselected calls. Executor permissions apply at every depth.

By default, `NativeAgents` sets native approval policy to `never` for root and child turns. Codex can return permission denials but cannot ask the application to approve escalation. `Environment.approval` still controls selected executor calls but cannot grant extra native permissions. Without `NativeAgents`, existing native approval behavior remains when `Environment.network` is omitted. An explicit [network policy](execution.md#network-and-local-socket-access) disables escalation.

Set `NativeAgents(interactions=True)` to receive supported child executor approvals, MCP forms and MCP URL requests through `session.interactions()` or `on_interaction`. Requests retain the originating child's identity. HTTP forms must come from a request associated with that child. Background forms without that association remain unsupported. This option does not enable the child's `request_user_input` or `request_permissions` prompts, or root escalation.

With this option, each child turn starts its own stdio MCP connections and closes them when that turn ends. Server process state and subscriptions do not survive between child turns. Unowned server-startup prompts fail child creation.

The root can use structured `output_type`; children return text. Answer manual calls through `on_interaction` or `session.interactions()`. Pending child manual requests cannot be checkpointed.

### Observe child results

A completed root can leave child turns running. Observe and control each child separately; a child Turn cannot control the root.

Read descendants with `async for child in session.subagents()`. Each immutable `Subagent` snapshot contains identity, `status`, and the current or last observed `turn`. The turn is `None` until observed.

```python
async for child in session.subagents():
    if child.turn is not None:
        result = await child.turn.get_result()
        print(child.id, result.final_output)
```

An `active` status means the child remains open for work; it can be idle or unloaded. Check `is_live` to see whether the snapshot captured a loaded active instance. Closing or reloading a child can make its snapshot stale. `start()` checks that the captured instance remains current.

`ChildTurn.get_result()` returns the child's `Completed[str]`, retaining its text and latest reported thread usage after later turns. Missing usage is `None`; root results exclude child usage. Native failures and interruptions raise `NativeError`. Python tool failures can raise the function's exception after cleanup.

A child's callback `submission` is a `ChildInvocation` in `ToolContext`, `MCPResultContext`, and `ExecutorContext`. This identity cannot wait for or cancel work. Wait through `Subagent.turn` in application code. `session.events()` covers root and child work.

### Identify child lifecycle records

Observe `SubagentLifecycle` through `session.events()`. Its `kind` is `"created"` for a newly spawned child, or `"active"`, `"closed"`, or `"snapshot"`. These Session-wide records do not identify a submission. A snapshot can describe restored history and a child that is not running. Before controlling a child, read a fresh `session.subagents()` snapshot and inspect `is_live`.

### Start, steer, and cancel child work

Use the captured child and turn handles:

- `await child.start(message)` starts work on an idle, live child and returns its `ChildTurn`.
- `await child.turn.steer(message)` steers the observed active turn.
- `await child.try_cancel(child.turn)` cancels the captured child turn. `await session.cancel_all()` cancels the tree. See [interruption](sessions.md#interrupt-work) for return values and cleanup limits.

These methods cannot create children or resume closed or reloaded instances. Reading descendants starts no work. The methods have no pagination arguments. Cancelling a result reader leaves native work running.

During a stop of all work, new child input is rejected but pending manual replies remain allowed. Afterward, validated explicit input to a live child can lift the stop; queued automatic wake-ups cannot.

Late steering may start a separate follow-up turn after the observed turn finishes. The captured Turn waits only for its original result. Follow successors through `session.events()` or a fresh descendant snapshot. Rereading an old snapshot starts no work.

Interruption preserves recorded history but may discard input that has not yet been recorded. Check [interruption behavior](sessions.md#interrupt-work) before continuing after a stop.

### Close, update, and restore

Root completion does not imply child completion. Closing the Session shuts down the native tree.

Model settings, instructions, and `NativeAgents` remain fixed for the Session. Use [idle tool replacement](tools.md#replace-tools-between-turns) only after all children close.

Once work and callbacks finish, idle checkpoints can retain child conversations, task history, and older goals and checklists. To restore them, supply current `NativeAgents` and matching Python and manual tool definitions. Restore uses the current Agent's MCP declarations and creates fresh runtime resources.

Restored child runtimes remain unloaded. To continue an active saved child, submit fresh input to the root asking the model to continue that named child with `followup_task`. The model can reload the child using its saved history. After the child starts, read a fresh `session.subagents()` snapshot or observe `session.events()` to obtain its new Turn. `Subagent.start()` cannot reload an unloaded child.

Live child Turns do not transfer, and interrupted child work does not resume automatically. Saved goals and checklists grant no tools. See [save and restore](state.md#save-and-restore-a-portable-checkpoint).

## Native tool decisions

Set `NativeAgents.approval` to approve collaboration calls before native dispatch. The callback receives `ToolContext` and `NativeToolCall` and can be synchronous or asynchronous. It covers root and descendant collaboration calls. It does not control Python functions, MCP tools or Environment executor tools.

This declaration denies new child creation while allowing other selected collaboration calls:

```python
from agents import NativeAgents, NativeToolCall, ToolContext


def approve_call(context: ToolContext[None], call: NativeToolCall) -> bool:
    return call.name != "spawn_agent"


children = NativeAgents(approval=approve_call)
```

Return `True` to allow the pending call or `False` to deny it. An exception, a non-boolean result or cancellation denies the call. Approval callbacks cannot replace the call's arguments or result. The call record includes thread, turn and call IDs; its `input` and `result` can contain sensitive data and are omitted from its representation.

During cleanup, the Session waits for approval callbacks that have started. Callbacks must not wait on, interrupt, close, or change their own Session. Use [tool notifications](tools.md#tool-notifications) when a passive notification is sufficient.
