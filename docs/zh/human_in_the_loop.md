---
search:
  exclude: true
---
# 人工介入

使用人工介入（HITL）流程暂停智能体执行，直到相关人员批准或拒绝敏感的工具调用。工具会声明何时需要审批，运行结果会以中断形式呈现待处理的审批，而`RunState`则允许你在作出决定后序列化并恢复运行。

该审批机制适用于整个运行，并不限于当前顶层智能体。无论工具属于当前智能体、通过任务转移到达的智能体，还是嵌套的[`Agent.as_tool()`][agents.agent.Agent.as_tool]执行，都适用相同的模式。在嵌套的`Agent.as_tool()`场景中，中断仍会出现在外层运行中，因此你需要在外层`RunState`上批准或拒绝它，然后恢复原始顶层运行。

使用`Agent.as_tool()`时，审批可能发生在两个不同层级：智能体工具本身可以通过`Agent.as_tool(..., needs_approval=...)`要求审批，而嵌套智能体内的工具也可以在嵌套运行开始后发起自己的审批。两者都通过同一个外层运行中断流程处理。

本页重点介绍通过`interruptions`实现的手动审批流程。如果你的应用能够通过代码作出决定，某些工具类型也支持程序化审批回调，使运行无需暂停即可继续。

## 需要审批的工具标记

将`needs_approval`设置为`True`可始终要求审批，也可以提供一个异步函数来逐次决定。该可调用对象会接收运行上下文、已解析的工具参数和工具调用 ID。

当 SDK 无法安全检查参数时，可调用审批规则会采用失败关闭策略。如果参数是格式错误的 JSON、是有效 JSON 但并非对象（例如`null`或列表），或者包含`NaN`、`Infinity`或`-Infinity`等非标准常量，则不会调用该可调用对象，而是要求手动审批。Runner 和 Realtime 工具调用的行为相同。

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

[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]和[`ApplyPatchTool`][agents.tool.ApplyPatchTool]均支持`needs_approval`。本地MCP服务也支持通过[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]和[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]上的`require_approval`进行审批。托管式MCP服务通过[`HostedMCPTool`][agents.tool.HostedMCPTool]支持审批，可设置`tool_config={"require_approval": "always"}`，并可选择提供`on_approval_request`回调。如果希望自动批准或自动拒绝，而不呈现中断，Shell 和 apply_patch 工具可接受`on_approval`回调。

## 审批流程机制

1. 当模型发出工具调用时，运行器会评估其审批规则（`needs_approval`、`require_approval`或托管式MCP的对应设置）。
2. 如果该工具调用的审批决定已经存储在[`RunContextWrapper`][agents.run_context.RunContextWrapper]中，运行器会直接继续而不发出提示。单次调用审批仅适用于特定调用 ID；传入`always_approve=True`或`always_reject=True`，可在本次运行剩余期间对该工具之后的调用持续应用同一决定。
3. 否则，执行会暂停，`RunResult.interruptions`（或`RunResultStreaming.interruptions`）中会包含[`ToolApprovalItem`][agents.items.ToolApprovalItem]条目，其中提供`agent.name`、`tool_name`和`arguments`等详细信息。这也包括任务转移后或嵌套`Agent.as_tool()`执行中发起的审批。
4. 使用`result.to_state()`将结果转换为`RunState`，调用`state.approve(...)`或`state.reject(...)`，然后通过`Runner.run(agent, state)`或`Runner.run_streamed(agent, state)`恢复运行，其中`agent`是本次运行的原始顶层智能体。
5. 恢复后的运行会从暂停处继续，并在需要新审批时重新进入此流程。

使用`always_approve=True`或`always_reject=True`创建的持久决定会存储在运行状态中，因此当你之后恢复同一暂停运行时，这些决定会通过`state.to_string()` / `RunState.from_string(...)`和`state.to_json()` / `RunState.from_json(...)`保留下来。

你无需在同一次处理中解决所有待审批项。`interruptions`中可以同时包含常规工具调用、托管式MCP审批和嵌套的`Agent.as_tool()`审批。如果你仅批准或拒绝其中部分项目后重新运行，已处理的调用可以继续，而未处理的项目仍会保留在`interruptions`中并再次暂停运行。

## 自定义拒绝消息

默认情况下，被拒绝的工具调用会将 SDK 的标准拒绝文本返回到运行中。你可以在两个层级自定义该消息：

-   运行级后备设置：设置[`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]，以控制整个运行中审批被拒绝时默认向模型显示的消息。
-   单次调用覆盖：当你希望某个被拒绝的特定工具调用呈现不同消息时，将`rejection_message=...`传给`state.reject(...)`。

如果两者均已提供，则单次调用的`rejection_message`优先于运行级格式化器。

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

有关同时展示这两个层级的完整代码示例，请参阅[`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py)。

## 自动审批决策

手动`interruptions`是最通用的模式，但并非唯一选择：

-   本地[`ShellTool`][agents.tool.ShellTool]和[`ApplyPatchTool`][agents.tool.ApplyPatchTool]可以使用`on_approval`在代码中立即批准或拒绝。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool]可以将`tool_config={"require_approval": "always"}`与`on_approval_request`结合使用，实现同类程序化决策。
-   普通[`function_tool`][agents.tool.function_tool]工具和[`Agent.as_tool()`][agents.agent.Agent.as_tool]使用本页介绍的手动中断流程。

当这些回调返回决定时，运行会继续，而无需暂停以等待人工响应。对于 Realtime 和语音会话 API，请参阅[Realtime 指南](realtime/guide.md)中的审批流程。

## 流式传输与会话

相同的中断流程也适用于流式传输运行。流式运行暂停后，继续消费[`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events]，直到迭代器结束；然后检查[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]，处理其中的中断。如果希望恢复后的输出继续进行流式传输，请使用[`Runner.run_streamed(...)`][agents.run.Runner.run_streamed]恢复。有关此模式的流式传输版本，请参阅[流式传输](streaming.md)。

如果你还在使用会话，从`RunState`恢复时应继续传入同一个会话实例，或者传入指向同一底层存储的另一个会话对象。恢复后的轮次会追加到同一份已存储对话历史中。有关会话生命周期的详细信息，请参阅[会话](sessions/index.md)。

## 示例：暂停、批准与恢复

以下代码片段与 JavaScript HITL 指南中的流程一致：当工具需要审批时暂停运行，将状态持久化到磁盘，重新加载状态，并在获得决定后恢复运行。

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

在此代码示例中，`prompt_approval`是同步函数，因为它使用`input()`，并通过`run_in_executor(...)`执行。如果你的审批来源已经是异步的（例如 HTTP 请求或异步数据库查询），则可以使用`async def`函数并直接对其使用`await`。

若要在等待审批时以流式传输方式输出，请调用`Runner.run_streamed`，消费`result.stream_events()`直至完成，然后按照上文所示执行相同的`result.to_state()`和恢复步骤。

## 仓库模式与代码示例

- **流式传输审批**：`examples/agent_patterns/human_in_the_loop_stream.py`展示了如何读取完`stream_events()`，然后批准待处理的工具调用，再通过`Runner.run_streamed(agent, state)`恢复运行。
- **自定义拒绝文本**：`examples/agent_patterns/human_in_the_loop_custom_rejection.py`展示了审批被拒绝时，如何将运行级`tool_error_formatter`与单次调用的`rejection_message`覆盖结合使用。
- **智能体作为工具的审批**：当委托的智能体任务需要审核时，`Agent.as_tool(..., needs_approval=...)`会应用相同的中断流程。嵌套中断仍会出现在外层运行中，因此应恢复原始顶层智能体，而不是嵌套智能体。
- **本地 shell 和 apply_patch 工具**：`ShellTool`和`ApplyPatchTool`也支持`needs_approval`。使用`state.approve(interruption, always_approve=True)`或`state.reject(..., always_reject=True)`，可为之后的调用缓存该决定。对于自动决策，请提供`on_approval`（参阅`examples/tools/shell.py`）；对于手动决策，请处理中断（参阅`examples/tools/shell_human_in_the_loop.py`）。托管 shell 环境不支持`needs_approval`或`on_approval`；请参阅[工具指南](tools.md)。
- **本地MCP服务**：使用`MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp`上的`require_approval`为MCP工具调用设置审批门槛（参阅`examples/mcp/get_all_mcp_tools_example/main.py`和`examples/mcp/tool_filter_example/main.py`）。
- **托管式MCP服务**：将`HostedMCPTool`上的`require_approval`设置为`"always"`，可强制启用 HITL；也可以提供`on_approval_request`以自动批准或拒绝（参阅`examples/hosted_mcp/human_in_the_loop.py`和`examples/hosted_mcp/on_approval.py`）。对于可信服务，请使用`"never"`（`examples/hosted_mcp/simple.py`）。
- **会话与记忆**：将会话传给`Runner.run`，使审批和对话历史能够跨多个轮次保留。SQLite 和 OpenAI Conversations 会话变体位于`examples/memory/memory_session_hitl_example.py`和`examples/memory/openai_session_hitl_example.py`中。
- **Realtime智能体**：Realtime 演示提供了 WebSocket 消息，可通过`RealtimeSession`上的`approve_tool_call` / `reject_tool_call`批准或拒绝工具调用（有关服务端处理程序，请参阅`examples/realtime/app/server.py`；有关 API 接口，请参阅[Realtime 指南](realtime/guide.md#tool-approvals)）。

## 长时审批

`RunState`采用持久化设计。使用`state.to_json()`或`state.to_string()`将待处理工作存储在数据库或队列中，之后再通过`RunState.from_json(...)`或`RunState.from_string(...)`重新创建。

实用的序列化选项：

-   `context_serializer`：自定义非映射类型上下文对象的序列化方式。
-   `context_deserializer`：使用`RunState.from_json(...)`或`RunState.from_string(...)`加载状态时，重新构建非映射类型上下文对象。
- `strict_context=True`：除非上下文本身已是映射类型，或者你提供了相应的序列化器/反序列化器，否则序列化或反序列化会失败。
- `context_override`：加载状态时替换已序列化的上下文。当你不想恢复原始上下文对象时，此选项非常有用，但它不会从已序列化的有效载荷中移除该上下文。
- `include_tracing_api_key=True`：当你需要恢复后的工作继续使用相同凭据导出追踪数据时，将追踪 API 密钥包含在已序列化的追踪有效载荷中。

已序列化的运行状态包含应用上下文，以及由 SDK 管理的运行时元数据，例如审批、使用量、已序列化的`tool_input`、嵌套的智能体工具恢复信息、追踪元数据和服务端管理的对话设置。如果你计划存储或传输已序列化状态，应将`RunContextWrapper.context`视为持久化数据，并避免在其中放置机密信息，除非你明确希望这些信息随状态一同传递。

## 待处理任务版本管理

如果审批可能会搁置一段时间，请将智能体定义或 SDK 的版本标记与已序列化状态一起存储。之后，你可以将反序列化路由到匹配的代码路径，以避免模型、提示词或工具定义发生变化时出现不兼容问题。