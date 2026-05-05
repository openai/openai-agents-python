---
search:
  exclude: true
---
# 人在回路中

使用人在回路（HITL）流程，在人员批准或拒绝敏感工具调用之前暂停智能体执行。工具会声明何时需要审批，运行结果会将待审批项作为中断暴露出来，而 `RunState` 允许你在决策完成后序列化并恢复运行。

该审批界面是运行级别的，不仅限于当前顶层智能体。无论工具属于当前智能体、属于通过任务转移到达的智能体，还是属于嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 执行，都采用同一种模式。在嵌套 `Agent.as_tool()` 的情况下，中断仍会出现在外层运行上，因此你应在外层 `RunState` 上进行批准或拒绝，并恢复原始顶层运行。

使用 `Agent.as_tool()` 时，审批可能发生在两个不同层级：智能体工具本身可通过 `Agent.as_tool(..., needs_approval=...)` 要求审批；嵌套智能体内部的工具在嵌套运行开始后也可能触发各自审批。这两类都通过同一个外层运行中断流程处理。

本页重点介绍通过 `interruptions` 的手动审批流程。如果你的应用可以在代码中做决策，某些工具类型也支持编程式审批回调，使运行无需暂停即可继续。

## 需要审批的工具标记

将 `needs_approval` 设为 `True` 可始终要求审批，或提供一个异步函数按调用逐次决定。该可调用对象会接收运行上下文、解析后的工具参数以及工具调用 ID。

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

`needs_approval` 可用于 [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool]。本地 MCP 服务也支持通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse] 和 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 上的 `require_approval` 进行审批。托管 MCP 服务可通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 配置 `tool_config={"require_approval": "always"}` 支持审批，并可选提供 `on_approval_request` 回调。Shell 和 apply_patch 工具接受 `on_approval` 回调，用于在不暴露中断的情况下自动批准或自动拒绝。

## 审批流程机制

1. 当模型发出工具调用时，运行器会评估其审批规则（`needs_approval`、`require_approval` 或托管 MCP 的等效配置）。
2. 如果该工具调用的审批决定已存储在 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 中，运行器将不再提示而直接继续。按调用的审批仅作用于特定调用 ID；传入 `always_approve=True` 或 `always_reject=True` 可将同一决定持久化到本次运行后续对该工具的调用。
3. 否则，执行会暂停，且 `RunResult.interruptions`（或 `RunResultStreaming.interruptions`）会包含 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 条目，其中含有 `agent.name`、`tool_name`、`arguments` 等细节。这也包括在任务转移之后或嵌套 `Agent.as_tool()` 执行内部触发的审批。
4. 通过 `result.to_state()` 将结果转为 `RunState`，调用 `state.approve(...)` 或 `state.reject(...)`，然后用 `Runner.run(agent, state)` 或 `Runner.run_streamed(agent, state)` 恢复，其中 `agent` 是该运行的原始顶层智能体。
5. 恢复后的运行会从中断处继续；若需要新的审批，将再次进入该流程。

通过 `always_approve=True` 或 `always_reject=True` 创建的粘性决策会保存在运行状态中，因此在你稍后恢复同一已暂停运行时，它们会在 `state.to_string()` / `RunState.from_string(...)` 与 `state.to_json()` / `RunState.from_json(...)` 之间保留。

你不需要在同一轮中解决所有待审批项。`interruptions` 可以同时包含常规函数工具、托管 MCP 审批以及嵌套 `Agent.as_tool()` 审批。如果你仅批准或拒绝其中部分项目后重新运行，已解决调用可以继续，而未解决项仍会保留在 `interruptions` 中并再次暂停运行。

## 自定义拒绝消息

默认情况下，被拒绝的工具调用会将 SDK 的标准拒绝文本返回到运行中。你可以在两层进行自定义：

-   全运行回退：设置 [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]，控制整个运行中审批拒绝时对模型可见的默认消息。
-   按调用覆盖：调用 `state.reject(...)` 时传入 `rejection_message=...`，让某个特定被拒绝工具调用显示不同消息。

若两者同时提供，则按调用 `rejection_message` 优先于全运行格式化器。

```python
from agents import RunConfig, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind != "approval_rejected":
        return None
    return "Publish action was canceled because approval was rejected."


run_config = RunConfig(tool_error_formatter=format_rejection)

# Later, while resolving a specific interruption:
state.reject(
    interruption,
    rejection_message="Publish action was canceled because the reviewer denied approval.",
)
```

参见 [`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py) 获取同时展示这两层的完整示例。

## 自动审批决策

手动 `interruptions` 是最通用模式，但并非唯一方式：

-   本地 [`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 可用 `on_approval` 在代码中立即批准或拒绝。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 可使用 `tool_config={"require_approval": "always"}` 配合 `on_approval_request` 实现同类编程式决策。
-   普通 [`function_tool`][agents.tool.function_tool] 工具与 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 使用本页介绍的手动中断流程。

当这些回调返回决策时，运行会继续，无需暂停等待人工响应。对于 Realtime 和语音会话 API，请参阅 [Realtime 指南](realtime/guide.md) 中的审批流程。

## 流式传输与会话

同样的中断流程也适用于流式传输运行。流式运行暂停后，继续消费 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] 直到迭代器结束，检查 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]，解决后如需继续流式输出，可用 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] 恢复。此模式的流式版本请参见[流式传输](streaming.md)。

如果你也在使用会话，从 `RunState` 恢复时请继续传入同一个会话实例，或传入另一个指向同一后端存储的会话对象。恢复后的轮次会追加到同一已存储会话历史中。会话生命周期细节见[会话](sessions/index.md)。

## 示例：暂停、批准、恢复

下面的片段与 JavaScript HITL 指南一致：当工具需要审批时暂停，将状态持久化到磁盘，重新加载后在收集决策后恢复。

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

在此示例中，`prompt_approval` 是同步的，因为它使用 `input()` 并通过 `run_in_executor(...)` 执行。如果你的审批来源本身已是异步（例如 HTTP 请求或异步数据库查询），可改用 `async def` 函数并直接 `await`。

若要在等待审批时流式输出，请调用 `Runner.run_streamed`，消费 `result.stream_events()` 直到完成，然后按上文相同方式执行 `result.to_state()` 和恢复步骤。

## 仓库模式与代码示例

- **流式审批**：`examples/agent_patterns/human_in_the_loop_stream.py` 展示如何清空 `stream_events()`，随后批准待处理工具调用，并通过 `Runner.run_streamed(agent, state)` 恢复。
- **自定义拒绝文本**：`examples/agent_patterns/human_in_the_loop_custom_rejection.py` 展示当审批被拒绝时，如何结合运行级 `tool_error_formatter` 与按调用 `rejection_message` 覆盖。
- **智能体作为工具的审批**：`Agent.as_tool(..., needs_approval=...)` 在委派智能体任务需要审查时应用同样的中断流程。嵌套中断仍会暴露在外层运行上，因此应恢复原始顶层智能体，而不是嵌套智能体。
- **本地 shell 与 apply_patch 工具**：`ShellTool` 和 `ApplyPatchTool` 也支持 `needs_approval`。使用 `state.approve(interruption, always_approve=True)` 或 `state.reject(..., always_reject=True)` 可缓存后续调用的决策。自动决策可提供 `on_approval`（见 `examples/tools/shell.py`）；手动决策则处理中断（见 `examples/tools/shell_human_in_the_loop.py`）。托管 shell 环境不支持 `needs_approval` 或 `on_approval`；参见[工具指南](tools.md)。
- **本地 MCP 服务**：在 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 上使用 `require_approval` 以管控 MCP 工具调用（见 `examples/mcp/get_all_mcp_tools_example/main.py` 和 `examples/mcp/tool_filter_example/main.py`）。
- **托管 MCP 服务**：在 `HostedMCPTool` 上将 `require_approval` 设为 `"always"` 以强制 HITL，可选提供 `on_approval_request` 自动批准或拒绝（见 `examples/hosted_mcp/human_in_the_loop.py` 和 `examples/hosted_mcp/on_approval.py`）。对可信服务可使用 `"never"`（`examples/hosted_mcp/simple.py`）。
- **会话与记忆**：向 `Runner.run` 传入会话，使审批与会话历史可跨多轮保留。SQLite 和 OpenAI Conversations 会话变体见 `examples/memory/memory_session_hitl_example.py` 与 `examples/memory/openai_session_hitl_example.py`。
- **Realtime 智能体**：Realtime 演示通过 WebSocket 消息，使用 `RealtimeSession` 上的 `approve_tool_call` / `reject_tool_call` 批准或拒绝工具调用（服务端处理见 `examples/realtime/app/server.py`，API 说明见 [Realtime 指南](realtime/guide.md#tool-approvals)）。

## 长时审批

`RunState` 设计为可持久化。使用 `state.to_json()` 或 `state.to_string()` 将待处理工作存入数据库或队列，并可稍后用 `RunState.from_json(...)` 或 `RunState.from_string(...)` 重建。

有用的序列化选项：

-   `context_serializer`：自定义非映射上下文对象的序列化方式。
-   `context_deserializer`：在使用 `RunState.from_json(...)` 或 `RunState.from_string(...)` 加载状态时重建非映射上下文对象。
-   `strict_context=True`：除非上下文本身已是映射，或你提供了合适的序列化器/反序列化器，否则序列化或反序列化失败。
-   `context_override`：加载状态时替换序列化上下文。这在你不想恢复原始上下文对象时很有用，但不会从已序列化载荷中移除该上下文。
-   `include_tracing_api_key=True`：当你需要恢复后的工作继续使用相同凭证导出追踪时，在序列化追踪载荷中包含 tracing API key。

序列化后的运行状态包含你的应用上下文以及 SDK 管理的运行时元数据，例如审批、用量、序列化的 `tool_input`、嵌套 agent-as-tool 恢复、追踪元数据以及服务端管理的会话设置。如果你计划存储或传输序列化状态，请将 `RunContextWrapper.context` 视为持久化数据，避免在其中放置机密信息，除非你有意让其随状态传递。

## 待处理任务版本管理

如果审批可能会搁置一段时间，请将智能体定义或 SDK 的版本标记与序列化状态一起存储。这样在模型、提示词或工具定义变更时，你就可以将反序列化路由到匹配的代码路径，避免不兼容问题。