# Human-in-the-loop

Use the human-in-the-loop (HITL) flow to pause agent execution until a person approves or rejects sensitive tool calls. Tools declare when they need approval, run results surface pending approvals as interruptions, and `RunState` lets you serialize and resume runs after decisions are made.

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

1. When the model emits a tool call, the runner evaluates `needs_approval`.
2. If an approval decision for that tool call is already stored in the [`RunContextWrapper`][agents.run_context.RunContextWrapper] (for example, from `always_approve=True`), the runner proceeds without prompting. Per-call approvals are scoped to the specific call ID; use `always_approve=True` to allow future calls automatically.
3. Otherwise, execution pauses and `RunResult.interruptions` (or `RunResultStreaming.interruptions`) contains `ToolApprovalItem` entries with details such as `agent.name`, `name`, and `arguments`.
4. Convert the result to a `RunState` with `result.to_state()`, call `state.approve(...)` or `state.reject(...)` (optionally passing `always_approve` or `always_reject`), and then resume with `Runner.run(agent, state)` or `Runner.run_streamed(agent, state)`.
5. The resumed run continues where it left off and will re-enter this flow if new approvals are needed.

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

## Other patterns in this repository

- **Streaming approvals**: `examples/agent_patterns/human_in_the_loop_stream.py` shows how to drain `stream_events()` and then approve pending tool calls before resuming with `Runner.run_streamed(agent, state)`.
- **Agent as tool approvals**: `Agent.as_tool(..., needs_approval=...)` applies the same interruption flow when delegated agent tasks need review.
- **Shell and apply_patch tools**: `ShellTool` and `ApplyPatchTool` also support `needs_approval`. Use `state.approve(interruption, always_approve=True)` or `state.reject(..., always_reject=True)` to cache the decision for future calls. For automatic decisions, provide `on_approval` (see `examples/tools/shell.py`); for manual decisions, handle interruptions (see `examples/tools/shell_human_in_the_loop.py`).
- **Local MCP servers**: Use `require_approval` on `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` to gate MCP tool calls (see `examples/mcp/get_all_mcp_tools_example/main.py` and `examples/mcp/tool_filter_example/main.py`).
- **Hosted MCP servers**: Set `require_approval` to `"always"` on `HostedMCPTool` to force HITL, optionally providing `on_approval_request` to auto-approve or reject (see `examples/hosted_mcp/human_in_the_loop.py` and `examples/hosted_mcp/on_approval.py`). Use `"never"` for trusted servers (`examples/hosted_mcp/simple.py`).
- **Sessions and memory**: Pass a session to `Runner.run` so approvals and conversation history survive multiple turns. SQLite and OpenAI Conversations session variants are in `examples/memory/memory_session_hitl_example.py` and `examples/memory/openai_session_hitl_example.py`.
- **Realtime agents**: The realtime demo exposes WebSocket messages that approve or reject tool calls via `approve_tool_call` / `reject_tool_call` on the `RealtimeSession` (see `examples/realtime/app/server.py` for the server-side handlers).

## Long-running approvals

`RunState` is designed to be durable. Use `state.to_json()` or `state.to_string()` to store pending work in a database or queue and recreate it later with `RunState.from_json(...)` or `RunState.from_string(...)`. Pass `context_override` if you do not want to persist sensitive context data in the serialized payload.

## Versioning pending tasks

If approvals may sit for a while, store a version marker for your agent definitions or SDK alongside the serialized state. You can then route deserialization to the matching code path to avoid incompatibilities when models, prompts, or tool definitions change.
