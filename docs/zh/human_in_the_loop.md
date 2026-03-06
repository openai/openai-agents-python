---
search:
  exclude: true
---
# 人工参与

使用人工参与（HITL）流程，在人员批准或拒绝敏感工具调用之前暂停智能体执行。工具会声明何时需要批准，运行结果会将待处理批准显示为中断，`RunState` 则允许你在决策完成后序列化并恢复运行。

该批准界面是针对整个运行的，而不仅限于当前顶层智能体。无论工具属于当前智能体、属于通过任务转移到达的智能体，还是属于嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 执行，模式都相同。在嵌套 `Agent.as_tool()` 的情况下，中断仍会显示在外层运行上，因此你需要在外层 `RunState` 上批准或拒绝它，并恢复原始顶层运行。

对于 `Agent.as_tool()`，批准可能发生在两个不同层级：智能体工具本身可通过 `Agent.as_tool(..., needs_approval=...)` 要求批准；嵌套智能体内部的工具也可在嵌套运行开始后触发其自身批准请求。这两者都通过同一个外层运行中断流程处理。

本页重点介绍通过 `interruptions` 的手动批准流程。如果你的应用可以在代码中做出决策，某些工具类型也支持编程式批准回调，从而让运行无需暂停即可继续。

## 标记需要批准的工具

将 `needs_approval` 设为 `True` 可始终要求批准，或提供一个异步函数按每次调用决定。该可调用对象接收运行上下文、解析后的工具参数以及工具调用 ID。

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

`needs_approval` 可用于 [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool]。本地 MCP 服务也支持通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse] 和 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 上的 `require_approval` 进行批准控制。托管 MCP 服务通过带有 `tool_config={"require_approval": "always"}` 的 [`HostedMCPTool`][agents.tool.HostedMCPTool] 支持批准，并可选提供 `on_approval_request` 回调。Shell 和 apply_patch 工具接受 `on_approval` 回调，如果你希望在不显示中断的情况下自动批准或自动拒绝。

## 批准流程机制

1. 当模型发出工具调用时，runner 会评估其批准规则（`needs_approval`、`require_approval` 或托管 MCP 对应配置）。
2. 如果该工具调用的批准决策已存储在 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 中，runner 将无需提示直接继续。按调用的批准仅作用于特定调用 ID；传入 `always_approve=True` 或 `always_reject=True` 可将同一决策持久化，用于本次运行剩余期间对该工具的后续调用。
3. 否则，执行会暂停，且 `RunResult.interruptions`（或 `RunResultStreaming.interruptions`）将包含 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 条目，含有如 `agent.name`、`tool_name` 和 `arguments` 等详细信息。这也包括任务转移后或嵌套 `Agent.as_tool()` 执行内部触发的批准请求。
4. 将结果转换为 `RunState`（`result.to_state()`），调用 `state.approve(...)` 或 `state.reject(...)`，然后通过 `Runner.run(agent, state)` 或 `Runner.run_streamed(agent, state)` 恢复，其中 `agent` 是该运行原始顶层智能体。
5. 恢复后的运行会从中断处继续；若出现新的批准需求，将再次进入此流程。

通过 `always_approve=True` 或 `always_reject=True` 创建的粘性决策会存储在运行状态中，因此在你稍后恢复同一暂停运行时，它们会在 `state.to_string()` / `RunState.from_string(...)` 和 `state.to_json()` / `RunState.from_json(...)` 之间保留。

你无需在同一次处理中解决所有待处理批准。`interruptions` 可同时包含常规工具调用、托管 MCP 批准以及嵌套 `Agent.as_tool()` 批准。如果你在仅批准或拒绝部分条目后重新运行，已处理的调用可继续执行，而未处理项会继续留在 `interruptions` 中并再次暂停运行。

## 自动批准决策

手动 `interruptions` 是最通用模式，但不是唯一方式：

-   本地 [`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 可使用 `on_approval` 在代码中立即批准或拒绝。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 可使用 `tool_config={"require_approval": "always"}` 配合 `on_approval_request` 实现同类编程式决策。
-   普通 [`function_tool`][agents.tool.function_tool] 工具和 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 使用本页所述手动中断流程。

当这些回调返回决策后，运行会继续，无需暂停等待人工响应。对于 Realtime 和语音会话 API，请参阅 [Realtime 指南](realtime/guide.md) 中的批准流程。

## 流式传输与会话

同一中断流程也适用于流式运行。流式运行暂停后，继续消费 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events]，直到迭代器结束；检查 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]；完成处理后，如需让恢复后的输出继续流式传输，请使用 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] 恢复。该模式的流式版本见 [流式传输](streaming.md)。

如果你还在使用会话，从 `RunState` 恢复时请持续传入同一个会话实例，或传入另一个指向同一底层存储的会话对象。恢复后的轮次会追加到同一已存储对话历史中。会话生命周期细节见 [会话](sessions/index.md)。

## 示例：暂停、批准、恢复

下面的代码片段与 JavaScript HITL 指南一致：当工具需要批准时暂停，将状态持久化到磁盘，重新加载，并在收集决策后恢复。

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

在此示例中，`prompt_approval` 是同步函数，因为它使用 `input()` 并通过 `run_in_executor(...)` 执行。如果你的批准来源本身已是异步的（例如 HTTP 请求或异步数据库查询），可改用 `async def` 函数并直接 `await`。

若要在等待批准时流式输出，请调用 `Runner.run_streamed`，消费 `result.stream_events()` 直到完成，然后按上文相同方式执行 `result.to_state()` 和恢复步骤。

## 仓库模式与代码示例

- **流式传输批准**：`examples/agent_patterns/human_in_the_loop_stream.py` 展示了如何清空 `stream_events()`，然后在使用 `Runner.run_streamed(agent, state)` 恢复前批准待处理工具调用。
- **智能体作为工具的批准**：`Agent.as_tool(..., needs_approval=...)` 在被委托的智能体任务需要审查时应用同样的中断流程。嵌套中断仍显示在外层运行上，因此应恢复原始顶层智能体，而不是嵌套智能体。
- **本地 shell 与 apply_patch 工具**：`ShellTool` 和 `ApplyPatchTool` 也支持 `needs_approval`。使用 `state.approve(interruption, always_approve=True)` 或 `state.reject(..., always_reject=True)` 可为后续调用缓存决策。自动决策可提供 `on_approval`（见 `examples/tools/shell.py`）；手动决策可处理中断（见 `examples/tools/shell_human_in_the_loop.py`）。托管 shell 环境不支持 `needs_approval` 或 `on_approval`；见[工具指南](tools.md)。
- **本地 MCP 服务**：在 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 上使用 `require_approval` 以控制 MCP 工具调用（见 `examples/mcp/get_all_mcp_tools_example/main.py` 和 `examples/mcp/tool_filter_example/main.py`）。
- **托管 MCP 服务**：在 `HostedMCPTool` 上将 `require_approval` 设为 `"always"` 可强制 HITL，并可选提供 `on_approval_request` 以自动批准或拒绝（见 `examples/hosted_mcp/human_in_the_loop.py` 和 `examples/hosted_mcp/on_approval.py`）。对可信服务可使用 `"never"`（`examples/hosted_mcp/simple.py`）。
- **会话与记忆**：向 `Runner.run` 传入会话，使批准与对话历史跨多个轮次保留。SQLite 和 OpenAI Conversations 会话变体位于 `examples/memory/memory_session_hitl_example.py` 和 `examples/memory/openai_session_hitl_example.py`。
- **Realtime 智能体**：realtime 演示通过 WebSocket 消息在 `RealtimeSession` 上调用 `approve_tool_call` / `reject_tool_call` 来批准或拒绝工具调用（服务端处理器见 `examples/realtime/app/server.py`，API 说明见 [Realtime 指南](realtime/guide.md#tool-approvals)）。

## 长时批准

`RunState` 设计为可持久化。使用 `state.to_json()` 或 `state.to_string()` 将待处理工作存储到数据库或队列，并在之后通过 `RunState.from_json(...)` 或 `RunState.from_string(...)` 重新创建。

有用的序列化选项：

-   `context_serializer`：自定义如何序列化非映射的上下文对象。
-   `context_deserializer`：在使用 `RunState.from_json(...)` 或 `RunState.from_string(...)` 加载状态时重建非映射上下文对象。
-   `strict_context=True`：除非上下文本身已是映射类型，或你提供了相应的 serializer/deserializer，否则序列化或反序列化会失败。
-   `context_override`：加载状态时替换已序列化上下文。当你不想恢复原始上下文对象时很有用，但它不会从已序列化负载中移除该上下文。
-   `include_tracing_api_key=True`：当你需要恢复后的工作继续使用相同凭据导出追踪数据时，在序列化的追踪负载中包含 tracing API key。

序列化后的运行状态包含你的应用上下文，以及由 SDK 管理的运行时元数据，例如批准信息、用量、序列化后的 `tool_input`、嵌套 agent-as-tool 恢复、追踪元数据和服务端管理的会话设置。如果你计划存储或传输序列化状态，请将 `RunContextWrapper.context` 视为持久化数据，并避免在其中放置秘密信息，除非你有意让其随状态一同传递。

## 待处理任务版本管理

如果批准请求可能会搁置一段时间，请将你的智能体定义或 SDK 版本标记与序列化状态一并存储。这样在模型、提示词或工具定义发生变化时，你就可以将反序列化路由到匹配的代码路径，以避免不兼容问题。