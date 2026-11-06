---
search:
  exclude: true
---
# 人在回路

使用人在回路（HITL）流程暂停智能体执行，直到有人批准或拒绝敏感的工具调用。工具会声明何时需要批准，运行结果会以中断形式呈现待处理的批准请求，而 `RunState` 可让你序列化已暂停的运行，并在作出决定后恢复运行。

该批准机制适用于整个运行，并不限于当前的顶层智能体。无论工具属于当前智能体、通过任务转移到达的智能体，还是嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 执行，都使用相同的模式。在嵌套 `Agent.as_tool()` 的情况下，中断仍会呈现在外层运行上，因此你需要在外层 `RunState` 上批准或拒绝，并恢复原始的顶层运行。

使用 `Agent.as_tool()` 时，批准可能发生在两个不同层级：智能体工具本身可以通过 `Agent.as_tool(..., needs_approval=...)` 要求批准，而嵌套智能体内部的工具可以在嵌套运行开始后再提出自己的批准请求。两者都通过相同的外层运行中断流程处理。

本页重点介绍通过 `interruptions` 进行的手动批准流程。如果你的应用可以在代码中作出决定，某些工具类型也支持编程式批准回调，使运行无需暂停即可继续。

## 需要批准的工具标记 {#marking-tools-that-need-approval}

将 `needs_approval` 设置为 `True`，即可始终要求批准；也可以提供一个异步函数，针对每次调用作出决定。该可调用对象会接收运行上下文、解析后的工具参数和工具调用 ID。

当 SDK 无法安全检查参数时，可调用的批准规则将以默认拒绝的方式失败。如果参数缺失、为空、仅包含空白字符、是格式错误的 JSON、是有效 JSON 但并非对象（例如 `null` 或列表），或包含 `NaN`、`Infinity` 或 `-Infinity` 等非标准常量，则不会调用该可调用对象，且该调用需要手动批准。Runner 和 Realtime 工具调用的行为相同。

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

[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 均提供 `needs_approval`。本地 MCP 服务器也支持通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse] 和 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 上的 `require_approval` 进行批准。托管 MCP 服务器通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 支持批准，其中使用 `tool_config={"require_approval": "always"}` 和可选的 `on_approval_request` 回调。如果你希望自动批准或自动拒绝，而不呈现中断，Shell 和 apply_patch 工具可接受 `on_approval` 回调。

对于本地 `ShellTool` 和 `ApplyPatchTool`，批准功能需要主动启用：`needs_approval` 默认为 `False`。仅设置 `on_approval` 回调并不会启用批准功能。还应设置 `needs_approval=True` 或可调用的批准策略；SDK 仅会对需要批准且尚无现有批准决定的调用触发该回调。有关执行职责和配置示例，请参阅[本地 Shell 和文件编辑的批准机制](tools.md#approval-for-local-shell-and-file-edits)。

对于 `ComputerTool`，使用 `on_safety_check` 接受或拒绝 Responses API 返回的安全检查。该回调仅针对具有待处理检查的计算机调用运行，不使用本页所述的暂停与恢复批准流程。有关详情和示例，请参阅[计算机安全检查](tools.md#pending-computer-safety-checks)。

## 批准流程 {#how-the-approval-flow-works}

1. 当模型发出工具调用时，Runner 会评估其批准规则（`needs_approval`、`require_approval` 或托管 MCP 的对应规则）。
2. 如果该工具调用的批准决定已存储在 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 中，Runner 将直接继续执行，不再提示。单次调用批准仅适用于特定调用 ID；传入 `always_approve=True` 或 `always_reject=True`，可将相同决定持久应用于未来对同一工具标识的调用。
3. 如果批准规则要求批准，且未存储该工具调用的决定，执行将暂停，而 `RunResult.interruptions`（或 `RunResultStreaming.interruptions`）将包含 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 条目，其中提供 `agent.name`、`tool_name` 和 `arguments` 等详细信息。这也包括任务转移之后或嵌套 `Agent.as_tool()` 执行内部提出的批准请求。
4. 使用 `result.to_state()` 将结果转换为 `RunState`，调用 `state.approve(...)` 或 `state.reject(...)`，然后通过 `Runner.run(agent, state)` 或 `Runner.run_streamed(agent, state)` 恢复运行，其中 `agent` 是该次运行原始的顶层智能体。
5. 恢复后的运行将从暂停位置继续；如果需要新的批准，则会重新进入此流程。

通过 `always_approve=True` 或 `always_reject=True` 创建的持久决定，在复用承载批准状态的 `RunContextWrapper` 时会保持有效，包括跨不同的 `Runner.run(...)` 或 `Runner.run_streamed(...)` 调用。将普通应用上下文对象传入新的运行会创建全新的包装器；仅复用该对象并不会保留批准状态。`RunState` 也会存储持久决定，因此稍后恢复已暂停的运行时，这些决定可以在 `state.to_string()` / `RunState.from_string(...)` 和 `state.to_json()` / `RunState.from_json(...)` 之间保留。

对于函数工具，持久决定的作用域限定为请求批准的智能体。即使另一个智能体的函数工具名称相同，也不会因此继承该决定。

!!! warning "持久的 Shell 和文件编辑决定不限定于智能体"

    对于本地 [`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool]，持久决定依据工具类型和名称进行匹配，而不依据所属智能体、执行器或编辑器。当智能体共享批准状态时，即使执行器或编辑器拥有不同权限，在任务转移后，某个 `always_approve=True` 决定也可能应用于另一个智能体中类型和名称相同的工具。两者的默认名称分别为 `shell` 和 `apply_patch`。当该持久批准生效时，SDK 不会再次提示，也不会重新评估目标工具的 `needs_approval` 策略或 `on_approval` 回调。`always_reject=True` 决定也会以相同方式共享。

    当执行器或编辑器拥有不同权限时，请使用单次调用决定：调用 `state.approve(interruption)` 或 `state.reject(interruption)`，并将 `always_approve` 和 `always_reject` 保持为默认值 `False`。若要对每个新的 Shell 或文件编辑调用都要求人工决定，还应设置 `needs_approval=True` 并省略 `on_approval`。单次调用决定不会清除现有的持久决定。

    使用不同的执行权限开始新运行时，请使用新的 `RunContextWrapper`，而不要复用持有批准状态的包装器。恢复现有的 `RunState` 会保留其批准状态，即使你传入新的上下文对象或包装器也是如此；覆盖上下文不会重置批准状态。

对于来自 [`HostedMCPTool`][agents.tool.HostedMCPTool] 的批准请求，Agents SDK 使用 `server_label` 与工具名称的组合作为持久工具决定的标识。在一个托管 MCP 服务器上对 `lookup_account` 作出的始终批准决定，不会批准另一个服务器上同名的工具。仅当托管 MCP 批准请求同时包含两个非空标识字段时，Agents SDK 才会持久保存始终批准或始终拒绝决定。

你无需在同一次处理中解决所有待处理批准。`interruptions` 可以同时包含常规函数工具、托管 MCP 批准和嵌套 `Agent.as_tool()` 批准。如果仅批准或拒绝部分条目后重新运行，已解决的调用可以继续，而未解决的调用会保留在 `interruptions` 中，并再次暂停运行。

## 自定义拒绝消息 {#custom-rejection-messages}

默认情况下，被拒绝的工具调用会将 SDK 的标准拒绝文本返回到运行中。你可以在两个层级自定义该消息：

-   运行级回退：设置 [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]，控制整个运行中批准请求遭拒时模型可见的默认消息。
-   单次调用覆盖：如果希望某个特定的已拒绝工具调用呈现不同消息，请向 `state.reject(...)` 传入 `rejection_message=...`。

如果同时提供两者，单次调用的 `rejection_message` 优先于运行级格式化器。

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

有关同时展示这两个层级的完整示例，请参阅 [`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py)。

## 自动批准决定 {#automatic-approval-decisions}

手动 `interruptions` 是最通用的模式，但不是唯一方式：

-   本地 [`ShellTool`][agents.tool.ShellTool] 和 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 可以使用 `on_approval`，在代码中立即批准或拒绝。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 可以将 `tool_config={"require_approval": "always"}` 与 `on_approval_request` 结合使用，作出同类编程式决定。
-   普通 [`function_tool`][agents.tool.function_tool] 工具和 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 使用本页所述的手动中断流程。

当这些回调返回决定时，运行会继续，而不会暂停等待人工响应。对于 Realtime 和语音会话 API，请参阅 [Realtime 指南](realtime/guide.md)中的批准流程。

## 流式传输与会话 {#streaming-and-sessions}

相同的中断流程也适用于流式运行。流式运行暂停后，继续使用 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events]，直到迭代器结束；检查 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 并解决中断；如果希望恢复后的输出继续流式传输，请使用 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] 恢复。有关此模式的流式版本，请参阅[流式传输](streaming.md)。

如果你同时使用会话，从 `RunState` 恢复时，请继续传入同一个会话实例，或传入为相同会话 ID 和后端存储配置的另一个会话对象。恢复后的轮次随后会追加到同一份已存储对话历史记录中。有关会话生命周期的详细信息，请参阅[会话](sessions/index.md)。

## 示例：暂停、批准与恢复 {#example-pause-approve-resume}

以下代码片段对应 JavaScript HITL 指南中的流程：当工具需要批准时暂停运行，将状态持久保存到磁盘，重新加载状态，并在收集到决定后恢复运行。

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

在此示例中，`prompt_approval` 是同步的，因为它使用 `input()`，并通过 `run_in_executor(...)` 执行。如果你的批准来源本身已经是异步的（例如 HTTP 请求或异步数据库查询），则可以使用 `async def` 函数，并直接对其执行 `await`。

若要在可能因批准而暂停的运行中使用流式传输，请调用 `Runner.run_streamed`，持续使用 `result.stream_events()` 直到完成，然后执行上文所示的相同 `result.to_state()` 和恢复步骤。

## 仓库模式与代码示例 {#repository-patterns-and-examples}

- **流式批准**：`examples/agent_patterns/human_in_the_loop_stream.py` 展示如何排空 `stream_events()`，然后批准待处理的工具调用，再通过 `Runner.run_streamed(agent, state)` 恢复运行。
- **自定义拒绝文本**：`examples/agent_patterns/human_in_the_loop_custom_rejection.py` 展示批准请求被拒绝时，如何将运行级 `tool_error_formatter` 与单次调用的 `rejection_message` 覆盖结合使用。
- **智能体作为工具的批准**：当委派给智能体的任务需要审查时，`Agent.as_tool(..., needs_approval=...)` 会应用相同的中断流程。嵌套中断仍会呈现在外层运行上，因此应恢复原始的顶层智能体，而不是嵌套智能体。
- **本地 Shell 和 apply_patch 工具**：`ShellTool` 和 `ApplyPatchTool` 也支持 `needs_approval`。使用 `state.approve(interruption)` 或 `state.reject(interruption)` 作出单次调用决定。`always_approve=True` 和 `always_reject=True` 选项会将决定持久保存在 `RunContextWrapper` 和 `RunState` 中；使用这些选项前，请查看[批准作用域警告](#how-the-approval-flow-works)。若要在回调中解决批准请求，请提供 `on_approval`；`examples/tools/shell.py` 展示了一个默认提示 Operator 作出决定的交互式回调。若要自动拒绝尚无批准决定的 Shell 调用，请在 `ShellTool` 上设置 `needs_approval=True` 和 `on_approval=lambda _context, _item: {"approve": False, "reason": "Disabled by policy"}`。若要让应用改为审查已暂停的运行，请处理中断（参阅 `examples/tools/shell_human_in_the_loop.py`）。托管 Shell 环境不支持 `needs_approval` 或 `on_approval`；请参阅[工具指南](tools.md)。
- **本地 MCP 服务器**：在 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 上使用 `require_approval`，对 MCP 工具调用设置门控（参阅 `examples/mcp/get_all_mcp_tools_example/main.py` 和 `examples/mcp/tool_filter_example/main.py`）。
- **托管 MCP 服务器**：在 `HostedMCPTool` 上设置 `tool_config={"require_approval": "always"}` 以强制使用 HITL，也可提供 `on_approval_request` 以自动批准或拒绝（参阅 `examples/hosted_mcp/human_in_the_loop.py` 和 `examples/hosted_mcp/on_approval.py`）。对于受信任的服务器，请使用 `"never"`（`examples/hosted_mcp/simple.py`）。
- **会话与内存**：向 `Runner.run` 传入会话，以跨轮次保留对话历史记录；恢复已暂停的运行时，使用 `RunState` 保留批准决定。SQLite 和 OpenAI Conversations 会话变体位于 `examples/memory/memory_session_hitl_example.py` 和 `examples/memory/openai_session_hitl_example.py` 中。
- **Realtime 智能体**：Realtime 演示公开了 WebSocket 消息，可通过 `RealtimeSession` 上的 `approve_tool_call` / `reject_tool_call` 批准或拒绝工具调用（有关服务器端处理程序，请参阅 `examples/realtime/app/server.py`；有关 API 接口，请参阅 [Realtime 指南](realtime/guide.md#tool-approvals)）。

## 长时间运行的批准流程 {#long-running-approvals}

`RunState` 采用可持久化设计。使用 `state.to_json()` 或 `state.to_string()` 将待处理工作存储在数据库或队列中，之后再通过 `RunState.from_json(...)` 或 `RunState.from_string(...)` 重新创建。

### 服务器端批准状态 {#keep-approval-state-on-the-server}

序列化的 `RunState` 包含执行状态，其中包括批准决定、待处理工具调用和工具参数。SDK 会恢复此状态；`RunState.from_json()` 和 `RunState.from_string()` 不会验证快照或提交快照者的身份。仅反序列化来自受信任存储的快照，或应用已完整验证其完整性和所有权的快照。模式检查或工具调用指纹无法验证快照的真实性。

对于浏览器或移动端批准界面，应将完整快照保存在由应用控制的服务器存储中。仅向审查者发送其有权查看的工具详情，以及待处理决定的不透明标识符。应将工具名称和参数视为不受信任的显示内容，并在渲染 HTML 时进行转义。

收到决定后，服务器必须：

1. 使用应用的会话或身份验证中间件验证审查者身份。不要从批准请求正文中获取审查者身份。
2. 授权该审查者对已存储的运行和选定的待处理调用执行操作。拥有运行 ID 或决定 ID 并不代表已获得授权。
3. 根据服务器上存储的待处理请求，验证提交的决定标识符和布尔决定。加载服务器拥有的快照，并通过 `state.get_interruptions()` 获取待处理条目；不要接受来自客户端的替代工具调用、参数、批准记录或序列化状态。
4. 对这些由服务器拥有的条目应用 `state.approve(...)` 或 `state.reject(...)`，然后恢复运行。应协调每个待处理请求的处理与存储，避免并发提交或重放提交导致同一快照被恢复两次。在共享存储中，应在启动恢复执行之前，使用检查所有者的原子状态转换。

[服务器端批准示例](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/human_in_the_loop_server.py)通过 CLI 客户端模拟和仅限单个进程中单个事件循环的存储，展示了此模式。它要求对一批待处理调用中的每个调用都作出决定。该示例会在反序列化和恢复执行之前消费请求，因此失败和取消同样会消费请求。生产应用必须提供身份验证、请求保护、存储保留机制和恢复机制，并在重试之前核对工具副作用；此示例并非可部署的 HTTP 服务。

将 `context` 替换为 `context_override`、设置 `strict_context=True`，或仅移除序列化的批准记录，都无法使不受信任的快照变得安全。其他字段仍会控制恢复后的执行。如果应用通过客户端传输完整快照，则必须在反序列化之前验证其完整性，将其绑定到已授权的用户和运行，并防止重放。此类验证不会加密快照，也不会向客户端隐藏其内容。

### 序列化选项 {#serialization-options}

实用的序列化选项：

-   `context_serializer`：自定义非映射上下文对象的序列化方式。
-   `context_deserializer`：通过 `RunState.from_json(...)` 或 `RunState.from_string(...)` 加载状态时，重新构建非映射上下文对象。
- `strict_context=True`：除非上下文本身已是映射或你提供了 `context_serializer`，否则序列化失败；除非上下文本身已是映射或你提供了 `context_deserializer`，否则反序列化失败。
- `context_override`：加载状态时替换序列化的上下文。如果你不希望恢复原始上下文对象，此选项很有用，但它不会从已经序列化的载荷中移除该上下文。
- `include_tracing_api_key=True`：在序列化的追踪载荷中包含追踪 API 密钥，以便恢复后的工作可以继续使用相同凭据导出追踪数据。

序列化的运行状态包括应用上下文，以及由 SDK 管理的运行时元数据，例如批准状态、用量、序列化的 `tool_input`、嵌套的智能体作为工具恢复状态、追踪元数据和服务器管理的对话设置。如果你计划存储或传输序列化状态，请将 `RunContextWrapper.context` 视为持久化数据；除非你有意让机密随状态一起传递，否则不要将机密放入其中。

## 待处理任务的版本控制 {#versioning-pending-tasks}

如果批准请求可能搁置较长时间，请将智能体定义或 SDK 的版本标记与序列化状态一同存储。随后可以将反序列化路由到匹配的代码路径，避免模型、提示词或工具定义发生变化时出现不兼容问题。