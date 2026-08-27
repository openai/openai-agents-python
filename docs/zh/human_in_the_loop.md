---
search:
  exclude: true
---
# 人工介入

使用人工介入（HITL）流程暂停智能体执行，直到人员批准或拒绝敏感的工具调用。工具会声明其何时需要审批，运行结果会以中断项的形式呈现待处理的审批，而 `RunState` 允许你序列化已暂停的运行，并在做出决策后恢复运行。

该审批机制覆盖整个运行，并不限于当前的顶层智能体。无论工具属于当前智能体、通过任务转移到达的智能体，还是嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 执行，都适用相同的模式。在嵌套 `Agent.as_tool()` 的情况下，中断仍会呈现在外层运行中，因此你需要在外层 `RunState` 上批准或拒绝它，然后恢复原始的顶层运行。

使用 `Agent.as_tool()` 时，审批可能发生在两个不同层级：智能体工具本身可以通过 `Agent.as_tool(..., needs_approval=...)` 要求审批，而嵌套智能体中的工具也可能在嵌套运行开始后发起自己的审批请求。这两种情况都通过相同的外层运行中断流程处理。

本页重点介绍通过 `interruptions` 实现的手动审批流程。如果你的应用可以通过代码做出决策，某些工具类型还支持程序化审批回调，使运行无需暂停即可继续。

## 需审批工具的标记 {#marking-tools-that-need-approval}

将 `needs_approval` 设置为 `True` 可始终要求审批，也可以提供一个异步函数，针对每次调用分别做出决策。该可调用对象会接收运行上下文、解析后的工具参数和工具调用 ID。

当 SDK 无法安全检查参数时，可调用审批规则会采用失败关闭策略。如果参数是格式错误的 JSON、是有效的 JSON 但不是对象（例如 `null` 或列表），或包含 `NaN`、`Infinity` 或 `-Infinity` 等非标准常量，则不会调用该可调用对象，并且该调用需要手动审批。Runner 和 Realtime 工具调用的行为相同。

```python
from agents import Agent
from agents.decorators import tool


@tool(needs_approval=True)
async def cancel_order(order_id: int) -> str:
    return f"Cancelled order {order_id}"


async def requires_review(_ctx, params, _call_id) -> bool:
    return "refund" in params.get("subject", "").lower()


@tool(needs_approval=requires_review)
async def send_email(subject: str, body: str) -> str:
    return f"Sent '{subject}'"


agent = Agent(
    name="Support agent",
    instructions="Handle tickets and ask for approval when needed.",
    tools=[cancel_order, send_email],
)
```

[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 均提供 `needs_approval`。本地 MCP 服务器也支持通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse] 和 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 上的 `require_approval` 进行审批。托管的 MCP 服务器通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 支持审批，其中使用 `tool_config={"require_approval": "always"}`，并可选择提供 `on_approval_request` 回调。如果你希望自动批准或自动拒绝，而不呈现中断项，Shell 和 apply_patch 工具可接受 `on_approval` 回调。

## 审批流程 {#how-the-approval-flow-works}

1. 当模型发出工具调用时，运行器会评估其审批规则（`needs_approval`、`require_approval` 或托管 MCP 的对应规则）。
2. 如果该工具调用的审批决策已存储在 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 中，运行器将继续执行而不再提示。单次调用审批的作用域限定于特定调用 ID；传入 `always_approve=True` 或 `always_reject=True`，可在本次运行的剩余期间，为后续对同一工具标识的调用保留相同决策。
3. 如果审批规则要求审批，并且尚未存储该工具调用的决策，执行将暂停，`RunResult.interruptions`（或 `RunResultStreaming.interruptions`）会包含 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 条目，其中包含 `agent.name`、`tool_name` 和 `arguments` 等详细信息。这也包括任务转移之后或嵌套 `Agent.as_tool()` 执行内部发起的审批。
4. 使用 `result.to_state()` 将结果转换为 `RunState`，调用 `state.approve(...)` 或 `state.reject(...)`，然后使用 `Runner.run(agent, state)` 或 `Runner.run_streamed(agent, state)` 恢复运行，其中 `agent` 是该次运行的原始顶层智能体。
5. 恢复后的运行会从暂停处继续，并在需要新的审批时重新进入此流程。

使用 `always_approve=True` 或 `always_reject=True` 创建的持久决策会存储在运行状态中，因此当你之后恢复同一个已暂停的运行时，这些决策在经过 `state.to_string()` / `RunState.from_string(...)` 和 `state.to_json()` / `RunState.from_json(...)` 后仍然有效。

对于来自 [`HostedMCPTool`][agents.tool.HostedMCPTool] 的审批请求，Agents SDK 使用 `server_label` 与工具名称的组合来标识持久工具决策。在一个托管 MCP 服务器上对 `lookup_account` 做出的始终批准决策，不会批准另一个服务器上同名的工具。只有当托管 MCP 审批请求包含两个非空标识字段时，Agents SDK 才会持久保存始终批准或始终拒绝的决策。

你不必在同一轮处理中解决所有待处理审批。`interruptions` 可以同时包含常规函数工具、托管 MCP 审批以及嵌套的 `Agent.as_tool()` 审批。如果你仅批准或拒绝部分项目后重新运行，已解决的调用可以继续，而未解决的调用仍会保留在 `interruptions` 中，并再次暂停运行。

## 自定义拒绝消息 {#custom-rejection-messages}

默认情况下，被拒绝的工具调用会将 SDK 的标准拒绝文本返回到运行中。你可以在两个层级自定义该消息：

-   全运行范围的后备设置：设置 [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]，以控制整个运行中审批遭拒时默认向模型显示的消息。
-   单次调用覆盖：如果你希望某个特定的被拒绝工具调用呈现不同消息，请向 `state.reject(...)` 传入 `rejection_message=...`。

如果两者都已提供，则单次调用的 `rejection_message` 优先于全运行范围的格式化器。

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

有关同时展示这两个层级的完整代码示例，请参阅 [`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py)。

## 自动审批决策 {#automatic-approval-decisions}

手动 `interruptions` 是最通用的模式，但并非唯一方式：

-   本地 [`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 可以使用 `on_approval`，在代码中立即批准或拒绝。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 可以结合使用 `tool_config={"require_approval": "always"}` 与 `on_approval_request`，做出同类程序化决策。
-   普通 [`function_tool`][agents.tool.function_tool] 工具和 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 使用本页介绍的手动中断流程。

当这些回调返回决策时，运行会继续，而无需暂停等待人工响应。对于 Realtime 和语音会话 API，请参阅 [Realtime 指南](realtime/guide.md)中的审批流程。

## 流式传输与会话 {#streaming-and-sessions}

同一中断流程也适用于流式运行。流式运行暂停后，应持续消费 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events]，直到迭代器结束；然后检查 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]、解决其中的中断项，并在希望恢复后的输出继续进行流式传输时，使用 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] 恢复。有关此模式的流式版本，请参阅[流式传输](streaming.md)。

如果你还使用了会话，请在从 `RunState` 恢复时继续传入同一个会话实例，或者传入针对相同会话 ID 和后端存储配置的另一个会话对象。恢复后的轮次随后会追加到同一份已存储的对话历史中。有关会话生命周期的详细信息，请参阅[会话](sessions/index.md)。

## 暂停、批准与恢复示例 {#example-pause-approve-resume}

下面的代码片段与 JavaScript HITL 指南采用相同流程：它会在工具需要审批时暂停，将状态持久化到磁盘，重新加载状态，并在收集决策后恢复运行。

```python
import asyncio
import json
from pathlib import Path

from agents import Agent, Runner, RunState
from agents.decorators import tool


async def needs_oakland_approval(_ctx, params, _call_id) -> bool:
    return "Oakland" in params.get("city", "")


@tool(needs_approval=needs_oakland_approval)
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

在此示例中，`prompt_approval` 是同步的，因为它使用 `input()`，并通过 `run_in_executor(...)` 执行。如果你的审批来源已经是异步的（例如 HTTP 请求或异步数据库查询），则可以改用 `async def` 函数，并直接对其使用 `await`。

若要在可能因审批而暂停的运行中使用流式传输，请调用 `Runner.run_streamed`，消费 `result.stream_events()` 直至其完成，然后执行与上述相同的 `result.to_state()` 和恢复步骤。

## 仓库模式与代码示例 {#repository-patterns-and-examples}

- **流式审批**：`examples/agent_patterns/human_in_the_loop_stream.py` 展示了如何完整消费 `stream_events()`，然后批准待处理的工具调用，最后使用 `Runner.run_streamed(agent, state)` 恢复运行。
- **自定义拒绝文本**：`examples/agent_patterns/human_in_the_loop_custom_rejection.py` 展示了在审批被拒绝时，如何将运行级 `tool_error_formatter` 与单次调用的 `rejection_message` 覆盖设置结合使用。
- **智能体工具审批**：当委托给智能体的任务需要审核时，`Agent.as_tool(..., needs_approval=...)` 会应用相同的中断流程。嵌套中断仍会呈现在外层运行中，因此应恢复原始顶层智能体，而不是嵌套智能体。
- **本地 Shell 和 apply_patch 工具**：`ShellTool` 和 `ApplyPatchTool` 也支持 `needs_approval`。使用 `state.approve(interruption, always_approve=True)` 或 `state.reject(..., always_reject=True)`，可在本次运行的剩余期间为该工具的后续调用缓存决策。对于自动决策，请提供 `on_approval`（参阅 `examples/tools/shell.py`）；对于手动决策，请处理中断项（参阅 `examples/tools/shell_human_in_the_loop.py`）。托管的 Shell 环境不支持 `needs_approval` 或 `on_approval`；请参阅[工具指南](tools.md)。
- **本地 MCP 服务器**：在 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 上使用 `require_approval` 控制 MCP 工具调用（参阅 `examples/mcp/get_all_mcp_tools_example/main.py` 和 `examples/mcp/tool_filter_example/main.py`）。
- **托管 MCP 服务器**：在 `HostedMCPTool` 上设置 `tool_config={"require_approval": "always"}` 以强制执行 HITL，并可选择提供 `on_approval_request` 来自动批准或拒绝（参阅 `examples/hosted_mcp/human_in_the_loop.py` 和 `examples/hosted_mcp/on_approval.py`）。对于受信任的服务器，请使用 `"never"`（`examples/hosted_mcp/simple.py`）。
- **会话与记忆**：向 `Runner.run` 传入会话，使审批和对话历史能够跨多个轮次保留。SQLite 和 OpenAI Conversations 会话变体位于 `examples/memory/memory_session_hitl_example.py` 和 `examples/memory/openai_session_hitl_example.py` 中。
- **实时智能体**：实时演示提供了 WebSocket 消息，可通过 `RealtimeSession` 上的 `approve_tool_call` / `reject_tool_call` 批准或拒绝工具调用（有关服务器端处理程序，请参阅 `examples/realtime/app/server.py`；有关 API 接口，请参阅 [Realtime 指南](realtime/guide.md#tool-approvals)）。

## 长期审批 {#long-running-approvals}

`RunState` 专为持久化而设计。使用 `state.to_json()` 或 `state.to_string()` 将待处理工作存储在数据库或队列中，之后再使用 `RunState.from_json(...)` 或 `RunState.from_string(...)` 重新创建它。

实用的序列化选项：

-   `context_serializer`：自定义非映射类型上下文对象的序列化方式。
-   `context_deserializer`：使用 `RunState.from_json(...)` 或 `RunState.from_string(...)` 加载状态时，重新构建非映射类型的上下文对象。
- `strict_context=True`：除非上下文本身已是映射类型，或你提供了 `context_serializer`，否则序列化将失败；除非上下文本身已是映射类型，或你提供了 `context_deserializer`，否则反序列化将失败。
- `context_override`：加载状态时替换已序列化的上下文。当你不希望恢复原始上下文对象时，此选项很有用，但它不会从已序列化的 payload 中移除该上下文。
- `include_tracing_api_key=True`：在需要恢复后的工作继续使用相同凭据导出追踪数据时，将追踪 API 密钥包含在已序列化的追踪 payload 中。

已序列化的运行状态包含应用上下文，以及由 SDK 管理的运行时元数据，例如审批、用量、已序列化的 `tool_input`、嵌套的智能体工具恢复信息、追踪元数据和服务器管理的对话设置。如果你计划存储或传输已序列化的状态，请将 `RunContextWrapper.context` 视为持久化数据；除非你明确希望密钥随状态一起传递，否则请避免将密钥放入其中。

## 待处理任务的版本管理 {#versioning-pending-tasks}

如果审批可能长时间处于待处理状态，请将智能体定义或 SDK 的版本标记与已序列化状态一同存储。这样，你就可以将反序列化操作路由到匹配的代码路径，避免模型、提示词或工具定义发生变化时出现不兼容问题。