---
search:
  exclude: true
---
# 人在回路

使用人在回路（HITL）流程暂停智能体执行，直到有人批准或拒绝敏感工具调用。工具会声明何时需要审批，运行结果会以中断形式呈现待处理审批，而 `RunState` 让你能够在做出决策后序列化并恢复运行。

该审批机制作用于整个运行，而不仅限于当前的顶层智能体。当工具属于当前智能体、属于通过任务转移到达的智能体，或属于嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 执行时，都适用同一模式。在嵌套 `Agent.as_tool()` 的情况下，中断仍会呈现在外层运行上，因此你需要在外层 `RunState` 上批准或拒绝它，并恢复原始的顶层运行。

使用 `Agent.as_tool()` 时，审批可能发生在两个不同层级：智能体工具本身可以通过 `Agent.as_tool(..., needs_approval=...)` 要求审批，而嵌套智能体内的工具也可以在嵌套运行开始后再发起自己的审批请求。两者都通过相同的外层运行中断流程处理。

本页重点介绍通过 `interruptions` 进行的手动审批流程。如果你的应用可以在代码中做出决策，某些工具类型还支持程序化审批回调，使运行无需暂停即可继续。

## 需要审批的工具标记

将 `needs_approval` 设置为 `True` 可始终要求审批，或提供一个按调用进行决策的异步函数。该可调用对象会接收运行上下文、已解析的工具参数以及工具调用 ID。

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

`needs_approval` 可用于 [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool]。本地 MCP 服务也支持通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse] 和 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 上的 `require_approval` 进行审批。托管 MCP 服务支持通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 以及 `tool_config={"require_approval": "always"}` 和可选的 `on_approval_request` 回调进行审批。如果你想在不呈现中断的情况下自动批准或自动拒绝，Shell 和 apply_patch 工具接受 `on_approval` 回调。

## 审批流程机制

1. 当模型发出工具调用时，运行器会评估其审批规则（`needs_approval`、`require_approval` 或托管 MCP 的等效机制）。
2. 如果该工具调用的审批决策已存储在 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 中，运行器会继续执行而不提示。按调用的审批作用域限定在特定调用 ID；传入 `always_approve=True` 或 `always_reject=True` 可在运行的剩余过程中，为该工具后续调用持久保留同一决策。
3. 否则，执行会暂停，并且 `RunResult.interruptions`（或 `RunResultStreaming.interruptions`）会包含 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 条目，其中包含 `agent.name`、`tool_name` 和 `arguments` 等详细信息。这包括在任务转移之后或嵌套 `Agent.as_tool()` 执行内部发起的审批。
4. 使用 `result.to_state()` 将结果转换为 `RunState`，调用 `state.approve(...)` 或 `state.reject(...)`，然后使用 `Runner.run(agent, state)` 或 `Runner.run_streamed(agent, state)` 恢复，其中 `agent` 是该运行的原始顶层智能体。
5. 恢复后的运行会从暂停处继续，并且如果需要新的审批，将再次进入此流程。

使用 `always_approve=True` 或 `always_reject=True` 创建的持续性决策会存储在运行状态中，因此当你稍后恢复同一个已暂停运行时，它们会在 `state.to_string()` / `RunState.from_string(...)` 和 `state.to_json()` / `RunState.from_json(...)` 之间保留。

你不需要在同一次处理中解决所有待处理审批。`interruptions` 可以包含常规工具调用、托管 MCP 审批和嵌套 `Agent.as_tool()` 审批的混合。如果你在只批准或拒绝部分项目后重新运行，已解决的调用可以继续，而未解决的调用会保留在 `interruptions` 中并再次暂停运行。

## 自定义拒绝消息

默认情况下，被拒绝的工具调用会将 SDK 的标准拒绝文本返回到运行中。你可以在两个层级自定义该消息：

-   运行范围后备：设置 [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]，以控制整个运行中审批拒绝时默认的模型可见消息。
-   按调用覆盖：当你希望某个特定的被拒绝工具调用呈现不同消息时，将 `rejection_message=...` 传给 `state.reject(...)`。

如果两者都提供，则按调用的 `rejection_message` 优先于运行范围格式化器。

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

请参阅 [`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py)，其中的完整示例展示了如何同时使用这两个层级。

## 自动审批决策

手动 `interruptions` 是最通用的模式，但并不是唯一模式：

-   本地 [`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 可以使用 `on_approval` 在代码中立即批准或拒绝。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 可以将 `tool_config={"require_approval": "always"}` 与 `on_approval_request` 一起使用，以进行同类程序化决策。
-   普通 [`function_tool`][agents.tool.function_tool] 工具和 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 使用本页的手动中断流程。

当这些回调返回决策时，运行会继续，而无需暂停等待人工响应。对于 Realtime 和语音会话 API，请参阅 [Realtime 指南](realtime/guide.md)中的审批流程。

## 流式传输和会话

同一中断流程也适用于流式传输运行。流式传输运行暂停后，继续消费 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events]，直到迭代器结束，然后检查 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]、解决它们，并在你希望恢复后的输出继续流式传输时使用 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] 恢复。有关此模式的流式传输版本，请参阅[流式传输](streaming.md)。

如果你还在使用会话，则在从 `RunState` 恢复时继续传入同一个会话实例，或传入另一个指向相同后端存储的会话对象。恢复后的轮次随后会追加到同一个已存储的对话历史中。有关会话生命周期详情，请参阅[会话](sessions/index.md)。

## 暂停、批准与恢复示例

下面的代码片段与 JavaScript HITL 指南一致：它会在工具需要审批时暂停，将状态持久化到磁盘，重新加载状态，并在收集到决策后恢复。

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

在此示例中，`prompt_approval` 是同步的，因为它使用 `input()`，并通过 `run_in_executor(...)` 执行。如果你的审批来源已经是异步的（例如 HTTP 请求或异步数据库查询），则可以改用 `async def` 函数并直接 `await` 它。

若要在等待审批时流式传输输出，请调用 `Runner.run_streamed`，消费 `result.stream_events()` 直到完成，然后按照上面展示的相同 `result.to_state()` 和恢复步骤操作。

## 仓库模式和代码示例

- **流式传输审批**: `examples/agent_patterns/human_in_the_loop_stream.py` 展示了如何消费完 `stream_events()`，然后在使用 `Runner.run_streamed(agent, state)` 恢复之前批准待处理的工具调用。
- **自定义拒绝文本**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` 展示了在审批被拒绝时，如何将运行级 `tool_error_formatter` 与按调用的 `rejection_message` 覆盖结合使用。
- **智能体作为工具的审批**: `Agent.as_tool(..., needs_approval=...)` 会在委派的智能体任务需要审查时应用相同的中断流程。嵌套中断仍会呈现在外层运行上，因此应恢复原始顶层智能体，而不是嵌套智能体。
- **本地 shell 和 apply_patch 工具**: `ShellTool` 和 `ApplyPatchTool` 也支持 `needs_approval`。使用 `state.approve(interruption, always_approve=True)` 或 `state.reject(..., always_reject=True)` 可缓存未来调用的决策。对于自动决策，请提供 `on_approval`（参见 `examples/tools/shell.py`）；对于手动决策，请处理中断（参见 `examples/tools/shell_human_in_the_loop.py`）。托管 shell 环境不支持 `needs_approval` 或 `on_approval`；请参阅[工具指南](tools.md)。
- **本地 MCP 服务**: 在 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 上使用 `require_approval` 来拦截 MCP 工具调用（参见 `examples/mcp/get_all_mcp_tools_example/main.py` 和 `examples/mcp/tool_filter_example/main.py`）。
- **托管 MCP 服务**: 在 `HostedMCPTool` 上将 `require_approval` 设置为 `"always"` 以强制 HITL，并可选择提供 `on_approval_request` 来自动批准或拒绝（参见 `examples/hosted_mcp/human_in_the_loop.py` 和 `examples/hosted_mcp/on_approval.py`）。对于可信服务，请使用 `"never"`（`examples/hosted_mcp/simple.py`）。
- **会话和记忆**: 将会话传给 `Runner.run`，使审批和对话历史能够跨多个轮次保留。SQLite 和 OpenAI Conversations 会话变体位于 `examples/memory/memory_session_hitl_example.py` 和 `examples/memory/openai_session_hitl_example.py`。
- **Realtime 智能体**: Realtime 演示公开了 WebSocket 消息，可通过 `RealtimeSession` 上的 `approve_tool_call` / `reject_tool_call` 批准或拒绝工具调用（有关服务端处理程序，请参见 `examples/realtime/app/server.py`；有关 API 表面，请参阅 [Realtime 指南](realtime/guide.md#tool-approvals)）。

## 长时间运行的审批

`RunState` 设计为可持久化。使用 `state.to_json()` 或 `state.to_string()` 将待处理工作存储在数据库或队列中，并稍后使用 `RunState.from_json(...)` 或 `RunState.from_string(...)` 重新创建。

有用的序列化选项：

-   `context_serializer`: 自定义非映射上下文对象的序列化方式。
-   `context_deserializer`: 使用 `RunState.from_json(...)` 或 `RunState.from_string(...)` 加载状态时，重建非映射上下文对象。
-   `strict_context=True`: 除非上下文已经是一个
    映射，或你提供了相应的序列化器/反序列化器，否则序列化或反序列化将失败。
-   `context_override`: 加载状态时替换已序列化的上下文。当你
    不想恢复原始上下文对象时，这很有用，但它不会从
    已序列化的载荷中移除该上下文。
-   `include_tracing_api_key=True`: 当你需要恢复后的工作继续使用相同凭证导出追踪时，
    在序列化的追踪载荷中包含追踪 API 密钥。

序列化后的运行状态包括你的应用上下文，以及 SDK 管理的运行时元数据，例如审批、
用量、序列化的 `tool_input`、嵌套的智能体作为工具恢复、追踪元数据以及由服务管理的
对话设置。如果你计划存储或传输序列化状态，请将
`RunContextWrapper.context` 视为持久化数据，并避免在其中放置机密信息，除非你
明确希望它们随状态一起传递。

## 待处理任务版本管理

如果审批可能搁置一段时间，请将你的智能体定义或 SDK 的版本标记与序列化状态一起存储。然后，你可以将反序列化路由到匹配的代码路径，以避免在模型、提示词或工具定义发生变化时出现不兼容问题。