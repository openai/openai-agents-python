---
search:
  exclude: true
---
# 人机协同（Human-in-the-loop）

使用人机协同（HITL）流程，在人员批准或拒绝敏感的工具调用之前暂停智能体执行。工具会声明何时需要审批，运行结果会将待审批项以中断形式呈现，而 `RunState` 允许你在做出决策后序列化并恢复运行。

## 标记需要审批的工具

将 `needs_approval` 设置为 `True` 以始终要求审批，或提供一个按调用决定的异步函数。该可调用对象会接收运行上下文、解析后的工具参数以及工具调用 ID。

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

`needs_approval` 可用于 [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool]。本地 MCP 服务也支持通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse] 和 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 上的 `require_approval` 来进行审批。托管 MCP 服务通过带 `tool_config={"require_approval": "always"}` 的 [`HostedMCPTool`][agents.tool.HostedMCPTool] 支持审批，并可选提供 `on_approval_request` 回调。Shell 和 apply_patch 工具如果你希望在不暴露中断的情况下自动批准或自动拒绝，则接受 `on_approval` 回调。

## 审批流程的工作方式

1. 当模型发出工具调用时，runner 会评估 `needs_approval`。
2. 如果该工具调用的审批决定已存储在 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 中（例如来自 `always_approve=True`），runner 会在不提示的情况下继续执行。按调用的审批仅作用于特定的调用 ID；使用 `always_approve=True` 可自动允许后续调用。
3. 否则，执行会暂停，`RunResult.interruptions`（或 `RunResultStreaming.interruptions`）将包含 `ToolApprovalItem` 条目，其中包括 `agent.name`、`name` 和 `arguments` 等详细信息。
4. 使用 `result.to_state()` 将结果转换为 `RunState`，调用 `state.approve(...)` 或 `state.reject(...)`（可选传入 `always_approve` 或 `always_reject`），然后通过 `Runner.run(agent, state)` 或 `Runner.run_streamed(agent, state)` 恢复运行。
5. 恢复后的运行会从中断处继续，如果需要新的审批，将再次进入该流程。

## 示例：暂停、批准、恢复

下面的片段与 JavaScript HITL 指南一致：当工具需要审批时暂停，将状态持久化到磁盘，重新加载，并在收集到决策后恢复。

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

在此示例中，`prompt_approval` 是同步的，因为它使用 `input()` 并通过 `run_in_executor(...)` 执行。如果你的审批来源本身就是异步的（例如 HTTP 请求或异步数据库查询），你可以使用 `async def` 函数并直接 `await` 它。

要在等待审批时进行流式输出，请调用 `Runner.run_streamed`，消费 `result.stream_events()` 直到完成，然后遵循与上面相同的 `result.to_state()` 和恢复步骤。

## 此仓库中的其他模式

- **流式审批**：`examples/agent_patterns/human_in_the_loop_stream.py` 展示了如何耗尽 `stream_events()`，然后在使用 `Runner.run_streamed(agent, state)` 恢复之前批准待处理的工具调用。
- **作为工具的智能体审批**：`Agent.as_tool(..., needs_approval=...)` 在委派的智能体任务需要审查时，应用相同的中断流程。
- **Shell 与 apply_patch 工具**：`ShellTool` 和 `ApplyPatchTool` 也支持 `needs_approval`。使用 `state.approve(interruption, always_approve=True)` 或 `state.reject(..., always_reject=True)` 来缓存对未来调用的决定。对于自动决策，提供 `on_approval`（参见 `examples/tools/shell.py`）；对于人工决策，处理 interruptions（参见 `examples/tools/shell_human_in_the_loop.py`）。
- **本地 MCP 服务**：在 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 上使用 `require_approval` 以控制 MCP 工具调用（参见 `examples/mcp/get_all_mcp_tools_example/main.py` 和 `examples/mcp/tool_filter_example/main.py`）。
- **托管 MCP 服务**：在 `HostedMCPTool` 上将 `require_approval` 设为 `"always"` 以强制 HITL，并可选提供 `on_approval_request` 来自动批准或拒绝（参见 `examples/hosted_mcp/human_in_the_loop.py` 和 `examples/hosted_mcp/on_approval.py`）。对可信服务使用 `"never"`（`examples/hosted_mcp/simple.py`）。
- **会话与记忆**：向 `Runner.run` 传入 session，使审批与对话历史能够跨多轮保留。SQLite 与 OpenAI Conversations 的 session 变体位于 `examples/memory/memory_session_hitl_example.py` 和 `examples/memory/openai_session_hitl_example.py`。
- **Realtime 智能体**：realtime demo 暴露了 WebSocket 消息，可通过 `RealtimeSession` 上的 `approve_tool_call` / `reject_tool_call` 批准或拒绝工具调用（服务端处理器参见 `examples/realtime/app/server.py`）。

## 长时间运行的审批

`RunState` 被设计为可持久化。使用 `state.to_json()` 或 `state.to_string()` 将待处理工作存储到数据库或队列中，并在之后通过 `RunState.from_json(...)` 或 `RunState.from_string(...)` 重新创建。如果你不希望在序列化负载中持久化敏感的上下文数据，请传入 `context_override`。

## 待处理任务的版本管理

如果审批可能会搁置一段时间，请在序列化状态旁存储智能体定义或 SDK 的版本标记。这样，你就可以将反序列化路由到匹配的代码路径，以避免在模型、提示词或工具定义发生变化时出现不兼容问题。