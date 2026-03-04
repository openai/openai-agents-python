---
search:
  exclude: true
---
# Human-in-the-loop

使用 human-in-the-loop（HITL）流程，在人员批准或拒绝敏感工具调用之前暂停智能体执行。工具可声明何时需要审批，运行结果会将待审批项作为中断暴露出来，而 `RunState` 允许你在做出决策后序列化并恢复运行。

## 需要审批的工具标记

将 `needs_approval` 设为 `True` 可始终要求审批，或提供一个按每次调用决定的异步函数。该可调用对象会接收运行上下文、解析后的工具参数以及工具调用 ID。

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

`needs_approval` 可用于 [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool]。本地 MCP 服务也支持通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse] 和 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 上的 `require_approval` 实现审批。托管 MCP 服务通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 支持审批，配置为 `tool_config={"require_approval": "always"}`，并可选提供 `on_approval_request` 回调。Shell 和 apply_patch 工具接受 `on_approval` 回调，若你希望在不暴露中断的情况下自动批准或自动拒绝。

## 审批流程机制

1. 当模型发出工具调用时，runner 会评估 `needs_approval`。
2. 如果该工具调用的审批决策已存储在 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 中（例如来自 `always_approve=True`），runner 会在不提示的情况下继续。按调用的审批仅作用于特定调用 ID；使用 `always_approve=True` 可自动允许后续调用。
3. 否则，执行会暂停，且 `RunResult.interruptions`（或 `RunResultStreaming.interruptions`）会包含 `ToolApprovalItem` 条目，其中含有 `agent.name`、`name`、`arguments` 等详细信息。
4. 使用 `result.to_state()` 将结果转换为 `RunState`，调用 `state.approve(...)` 或 `state.reject(...)`（可选传入 `always_approve` 或 `always_reject`），然后用 `Runner.run(agent, state)` 或 `Runner.run_streamed(agent, state)` 恢复。
5. 恢复后的运行会从中断处继续；如果需要新的审批，将再次进入该流程。

## 示例：暂停、批准、恢复

下面的代码片段与 JavaScript HITL 指南一致：当工具需要审批时会暂停，将状态持久化到磁盘，重新加载后在收集决策后恢复。

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

在此示例中，`prompt_approval` 是同步的，因为它使用 `input()` 且通过 `run_in_executor(...)` 执行。如果你的审批来源本身已是异步的（例如 HTTP 请求或异步数据库查询），则可使用 `async def` 函数并直接 `await`。

若要在等待审批时进行流式输出，请调用 `Runner.run_streamed`，消费 `result.stream_events()` 直到完成，然后按上文所示执行同样的 `result.to_state()` 与恢复步骤。

## 仓库模式与示例

- **流式审批**：`examples/agent_patterns/human_in_the_loop_stream.py` 展示了如何消费 `stream_events()`，然后在使用 `Runner.run_streamed(agent, state)` 恢复前批准待处理的工具调用。
- **Agents as tools 审批**：当委派的智能体任务需要审查时，`Agent.as_tool(..., needs_approval=...)` 应用相同的中断流程。
- **Shell 和 apply_patch 工具**：`ShellTool` 与 `ApplyPatchTool` 也支持 `needs_approval`。使用 `state.approve(interruption, always_approve=True)` 或 `state.reject(..., always_reject=True)` 可为后续调用缓存决策。自动决策可提供 `on_approval`（见 `examples/tools/shell.py`）；手动决策则处理中断（见 `examples/tools/shell_human_in_the_loop.py`）。
- **本地 MCP 服务**：在 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 上使用 `require_approval` 来管控 MCP 工具调用（见 `examples/mcp/get_all_mcp_tools_example/main.py` 和 `examples/mcp/tool_filter_example/main.py`）。
- **托管 MCP 服务**：将 `HostedMCPTool` 上的 `require_approval` 设为 `"always"` 以强制 HITL，并可选提供 `on_approval_request` 以自动批准或拒绝（见 `examples/hosted_mcp/human_in_the_loop.py` 和 `examples/hosted_mcp/on_approval.py`）。对可信服务使用 `"never"`（`examples/hosted_mcp/simple.py`）。
- **会话与内存**：向 `Runner.run` 传入会话，使审批与对话历史在多轮中持续保留。SQLite 和 OpenAI Conversations 会话变体见 `examples/memory/memory_session_hitl_example.py` 与 `examples/memory/openai_session_hitl_example.py`。
- **Realtime 智能体**：realtime 演示公开了 WebSocket 消息，可通过 `RealtimeSession` 上的 `approve_tool_call` / `reject_tool_call` 批准或拒绝工具调用（服务端处理器见 `examples/realtime/app/server.py`，API 接口见 [Realtime 指南](realtime/guide.md#tool-approvals)）。

## 长时间运行审批

`RunState` 设计为可持久化。使用 `state.to_json()` 或 `state.to_string()` 可将待处理工作存入数据库或队列，并在之后通过 `RunState.from_json(...)` 或 `RunState.from_string(...)` 重建。

实用的序列化选项：

-   `context_serializer`：自定义非映射上下文对象的序列化方式。
-   `strict_context=True`：除非上下文本身已是映射类型，或你提供了合适的序列化器/反序列化器，否则序列化或反序列化将失败。
-   `context_override`：加载状态时替换已序列化的上下文。当你不想恢复原始上下文对象时很有用，但它不会从已序列化负载中移除该上下文。
-   `include_tracing_api_key=True`：当你需要恢复后的任务继续使用相同凭据导出追踪时，在序列化的追踪负载中包含 tracing API key。

`RunState` 还会保留追踪元数据和由服务端管理的会话设置，因此恢复后的运行可以继续同一条追踪以及相同的 `conversation_id` / `previous_response_id` 链路。

## 待处理任务版本控制

如果审批可能会搁置一段时间，请将智能体定义或 SDK 的版本标记与序列化状态一并存储。这样在模型、提示词或工具定义变化时，你可以将反序列化路由到匹配的代码路径，以避免不兼容问题。