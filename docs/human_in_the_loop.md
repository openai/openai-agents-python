# Human-in-the-loop

Use the human-in-the-loop (HITL) flow to pause agent execution until a person approves or rejects sensitive tool calls. Tools declare when they need approval, run results surface pending approvals as interruptions, and `RunState` lets you serialize and resume runs after decisions are made.

That approval surface is run-wide, not limited to the current top-level agent. The same pattern applies when the tool belongs to the current agent, to an agent reached through a handoff, or to a nested [`Agent.as_tool()`][agents.agent.Agent.as_tool] execution. In the nested `Agent.as_tool()` case, the interruption still surfaces on the outer run, so you approve or reject it on the outer `RunState` and resume the original top-level run.

With `Agent.as_tool()`, approvals can happen at two different layers: the agent tool itself can require approval via `Agent.as_tool(..., needs_approval=...)`, and tools inside the nested agent can later raise their own approvals after the nested run starts. Both are handled through the same outer-run interruption flow.

This page focuses on the manual approval flow via `interruptions`. If your app can decide in code, some tool types also support programmatic approval callbacks so the run can continue without pausing.

## Marking tools that need approval

Set `needs_approval` to `True` to always require approval or provide an async function that decides per call. The callable receives the run context, parsed tool parameters, and the tool call ID.

```python
from agents import Agent, Runner, function_tool


@function_tool(needs_approval=True)
async def cancel_order(order_id: int) -> str:
    return f"Cancelled order {order_id}"


async def requires_review(_ctx, params, _call_id) -> bool:
    return "refund" in params.get("subject", "").lower()


@function_tool(needs_approval=requires_review)
async def send_email(subject: str, body: str) -> str:
    return f"Sent '{subject}'"


agent = Agent(
    name="Support agent",
    instructions="Handle tickets and ask for approval when needed.",
    tools=[cancel_order, send_email],
)
```

`needs_approval` is available on [`function_tool`][agents.tool.function_tool], [`Agent.as_tool`][agents.agent.Agent.as_tool], [`ShellTool`][agents.tool.ShellTool], and [`ApplyPatchTool`][agents.tool.ApplyPatchTool]. Local MCP servers also support approvals through `require_approval` on [`MCPServerStdio`][agents.mcp.server.MCPServerStdio], [`MCPServerSse`][agents.mcp.server.MCPServerSse], and [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]. Hosted MCP servers support approvals via [`HostedMCPTool`][agents.tool.HostedMCPTool] with `tool_config={"require_approval": "always"}` and an optional `on_approval_request` callback. Shell and apply_patch tools accept an `on_approval` callback if you want to auto-approve or auto-reject without surfacing an interruption.

## How the approval flow works

1. When the model emits a tool call, the runner evaluates its approval rule (`needs_approval`, `require_approval`, or the hosted MCP equivalent).
2. If an approval decision for that tool call is already stored in the [`RunContextWrapper`][agents.run_context.RunContextWrapper], the runner proceeds without prompting. Per-call approvals are scoped to the specific call ID; pass `always_approve=True` or `always_reject=True` to persist the same decision for future calls to that tool during the rest of the run.
3. Otherwise, execution pauses and `RunResult.interruptions` (or `RunResultStreaming.interruptions`) contains [`ToolApprovalItem`][agents.items.ToolApprovalItem] entries with details such as `agent.name`, `tool_name`, and `arguments`. This includes approvals raised after a handoff or inside nested `Agent.as_tool()` executions.
4. Convert the result to a `RunState` with `result.to_state()`, call `state.approve(...)` or `state.reject(...)`, and then resume with `Runner.run(agent, state)` or `Runner.run_streamed(agent, state)`, where `agent` is the original top-level agent for the run.
5. The resumed run continues where it left off and will re-enter this flow if new approvals are needed.

Sticky decisions created with `always_approve=True` or `always_reject=True` are stored in the run state, so they survive `state.to_string()` / `RunState.from_string(...)` and `state.to_json()` / `RunState.from_json(...)` when you resume the same paused run later.

You do not need to resolve every pending approval in the same pass. `interruptions` can contain a mix of regular function tools, hosted MCP approvals, and nested `Agent.as_tool()` approvals. If you rerun after approving or rejecting only some items, those resolved calls can continue while unresolved ones remain in `interruptions` and pause the run again.

## Automatic approval decisions

Manual `interruptions` are the most general pattern, but they are not the only one:

-   Local [`ShellTool`][agents.tool.ShellTool] and [`ApplyPatchTool`][agents.tool.ApplyPatchTool] can use `on_approval` to approve or reject immediately in code.
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] can use `tool_config={"require_approval": "always"}` together with `on_approval_request` for the same kind of programmatic decision.
-   Plain [`function_tool`][agents.tool.function_tool] tools and [`Agent.as_tool()`][agents.agent.Agent.as_tool] use the manual interruption flow on this page.

When these callbacks return a decision, the run continues without pausing for a human response. For Realtime and voice session APIs, see the approval flow in the [Realtime guide](realtime/guide.md).

## Streaming and sessions

The same interruption flow works in streaming runs. After a streamed run pauses, keep consuming [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] until the iterator finishes, inspect [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions], resolve them, and resume with [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] if you want the resumed output to keep streaming. See [Streaming](streaming.md) for the streamed version of this pattern.

If you are also using a session, keep passing the same session instance when you resume from `RunState`, or pass another session object that points at the same backing store. The resumed turn is then appended to the same stored conversation history. See [Sessions](sessions/index.md) for the session lifecycle details.

## Example: pause, approve, resume

The snippet below mirrors the JavaScript HITL guide: it pauses when a tool needs approval, persists state to disk, reloads it, and resumes after collecting a decision.

```python
import asyncio
import json
from pathlib import Path

from agents import Agent, Runner, RunState, function_tool


async def needs_oakland_approval(_ctx, params, _call_id) -> bool:
    return "Oakland" in params.get("city", "")


@function_tool(needs_approval=needs_oakland_approval)
async def get_temperature(city: str) -> str:
    return f"The temperature in {city} is 20° Celsius"


agent = Agent(
    name="Weather assistant",
    instructions="Answer weather questions with the provided tools.",
    tools=[get_temperature],
)

STATE_PATH = Path(".cache/hitl_state.json")


def prompt_approval(tool_name: str, arguments: str | None) -> bool:
    answer = input(f"Approve {tool_name} with {arguments}? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


async def main() -> None:
    result = await Runner.run(agent, "What is the temperature in Oakland?")

    while result.interruptions:
        # Persist the paused state.
        state = result.to_state()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(state.to_string())

        # Load the state later (could be a different process).
        stored = json.loads(STATE_PATH.read_text())
        state = await RunState.from_json(agent, stored)

        for interruption in result.interruptions:
            approved = await asyncio.get_running_loop().run_in_executor(
                None, prompt_approval, interruption.name or "unknown_tool", interruption.arguments
            )
            if approved:
                state.approve(interruption, always_approve=False)
            else:
                state.reject(interruption)

        result = await Runner.run(agent, state)

    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

In this example, `prompt_approval` is synchronous because it uses `input()` and is executed with `run_in_executor(...)`. If your approval source is already asynchronous (for example, an HTTP request or async database query), you can use an `async def` function and `await` it directly instead.

To stream output while waiting for approvals, call `Runner.run_streamed`, consume `result.stream_events()` until it completes, and then follow the same `result.to_state()` and resume steps shown above.

## Repository patterns and examples

- **Streaming approvals**: `examples/agent_patterns/human_in_the_loop_stream.py` shows how to drain `stream_events()` and then approve pending tool calls before resuming with `Runner.run_streamed(agent, state)`.
- **Agent as tool approvals**: `Agent.as_tool(..., needs_approval=...)` applies the same interruption flow when delegated agent tasks need review. Nested interruptions still surface on the outer run, so resume the original top-level agent rather than the nested one.
- **Local shell and apply_patch tools**: `ShellTool` and `ApplyPatchTool` also support `needs_approval`. Use `state.approve(interruption, always_approve=True)` or `state.reject(..., always_reject=True)` to cache the decision for future calls. For automatic decisions, provide `on_approval` (see `examples/tools/shell.py`); for manual decisions, handle interruptions (see `examples/tools/shell_human_in_the_loop.py`). Hosted shell environments do not support `needs_approval` or `on_approval`; see the [tools guide](tools.md).
- **Local MCP servers**: Use `require_approval` on `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` to gate MCP tool calls (see `examples/mcp/get_all_mcp_tools_example/main.py` and `examples/mcp/tool_filter_example/main.py`).
- **Hosted MCP servers**: Set `require_approval` to `"always"` on `HostedMCPTool` to force HITL, optionally providing `on_approval_request` to auto-approve or reject (see `examples/hosted_mcp/human_in_the_loop.py` and `examples/hosted_mcp/on_approval.py`). Use `"never"` for trusted servers (`examples/hosted_mcp/simple.py`).
- **Sessions and memory**: Pass a session to `Runner.run` so approvals and conversation history survive multiple turns. SQLite and OpenAI Conversations session variants are in `examples/memory/memory_session_hitl_example.py` and `examples/memory/openai_session_hitl_example.py`.
- **Realtime agents**: The realtime demo exposes WebSocket messages that approve or reject tool calls via `approve_tool_call` / `reject_tool_call` on the `RealtimeSession` (see `examples/realtime/app/server.py` for the server-side handlers and [Realtime guide](realtime/guide.md#tool-approvals) for the API surface).

## Long-running approvals

`RunState` is designed to be durable. Use `state.to_json()` or `state.to_string()` to store pending work in a database or queue and recreate it later with `RunState.from_json(...)` or `RunState.from_string(...)`.

Useful serialization options:

-   `context_serializer`: Customize how non-mapping context objects are serialized.
-   `context_deserializer`: Rebuild non-mapping context objects when loading state with `RunState.from_json(...)` or `RunState.from_string(...)`.
-   `strict_context=True`: Fail serialization or deserialization unless the context is already a
    mapping or you provide the appropriate serializer/deserializer.
-   `context_override`: Replace the serialized context when loading state. This is useful when you
    do not want to restore the original context object, but it does not remove that context from an
    already serialized payload.
-   `include_tracing_api_key=True`: Include the tracing API key in the serialized trace payload
    when you need resumed work to keep exporting traces with the same credentials.

Serialized run state includes your app context plus SDK-managed runtime metadata such as approvals,
usage, serialized `tool_input`, nested agent-as-tool resumptions, trace metadata, and server-managed
conversation settings. If you plan to store or transmit serialized state, treat
`RunContextWrapper.context` as persisted data and avoid placing secrets there unless you
intentionally want them to travel with the state.

## Versioning pending tasks

If approvals may sit for a while, store a version marker for your agent definitions or SDK alongside the serialized state. You can then route deserialization to the matching code path to avoid incompatibilities when models, prompts, or tool definitions change.
