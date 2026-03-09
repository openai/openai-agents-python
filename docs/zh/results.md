---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，会收到两种结果类型之一：

-   来自 `Runner.run(...)` 或 `Runner.run_sync(...)` 的 [`RunResult`][agents.result.RunResult]
-   来自 `Runner.run_streamed(...)` 的 [`RunResultStreaming`][agents.result.RunResultStreaming]

两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，它公开了共享的结果接口，例如 `final_output`、`new_items`、`last_agent`、`raw_responses` 和 `to_state()`。

`RunResultStreaming` 增加了流式传输专用控制项，例如 [`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete] 和 [`cancel(...)`][agents.result.RunResultStreaming.cancel]。

## 结果接口选择

大多数应用只需要少量结果属性或辅助方法：

| 如果你需要... | 使用 |
| --- | --- |
| 展示给用户的最终答案 | `final_output` |
| 可重放的下一轮输入列表（包含完整本地转录） | `to_input_list()` |
| 包含智能体、工具、任务转移和审批元数据的丰富运行条目 | `new_items` |
| 通常应处理下一轮用户输入的智能体 | `last_agent` |
| 使用 `previous_response_id` 的 OpenAI Responses API 链式调用 | `last_response_id` |
| 待处理审批与可恢复快照 | `interruptions` 和 `to_state()` |
| 当前嵌套 `Agent.as_tool()` 调用的元数据 | `agent_tool_invocation` |
| 原始模型调用或安全防护措施诊断信息 | `raw_responses` 和 guardrail 结果数组 |

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行智能体的最终输出。它可以是：

-   `str`，如果最后一个智能体未定义 `output_type`
-   `last_agent.output_type` 类型的对象，如果最后一个智能体定义了输出类型
-   `None`，如果运行在产生最终输出前停止，例如因审批中断而暂停

!!! note

    `final_output` 的类型标注为 `Any`。任务转移可能会改变由哪个智能体完成运行，因此 SDK 无法在静态层面知道所有可能输出类型的完整集合。

在流式模式下，`final_output` 会在流处理完成前一直保持为 `None`。关于逐事件流程，参见[流式传输](streaming.md)。

## 输入、下一轮历史与新条目

这些接口回答的是不同问题：

| 属性或辅助方法 | 包含内容 | 最适用场景 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 此运行片段的基础输入。如果任务转移输入过滤器重写了历史，这里反映的是运行继续时使用的过滤后输入。 | 审计此运行实际使用了什么输入 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 基于 `input` 加上此运行中转换后的 `new_items` 构建的、可重放的下一轮输入列表。 | 手动聊天循环和客户端管理的会话状态 |
| [`new_items`][agents.result.RunResultBase.new_items] | 带有智能体、工具、任务转移和审批元数据的丰富 [`RunItem`][agents.items.RunItem] 包装。 | 日志、UI、审计和调试 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 运行中每次模型调用产生的原始 [`ModelResponse`][agents.items.ModelResponse] 对象。 | 提供方级诊断或原始响应检查 |

在实践中：

-   当你的应用手动维护完整对话转录时，使用 `to_input_list()`。
-   当你希望 SDK 为你加载和保存历史时，使用 [`session=...`](sessions/index.md)。
-   如果你使用 OpenAI 服务端托管状态（`conversation_id` 或 `previous_response_id`），通常只需传入新的用户输入并复用已存储 ID，而不是重新发送 `to_input_list()`。

与 JavaScript SDK 不同，Python 不提供单独的 `output` 属性来仅表示模型形状增量。需要 SDK 元数据时使用 `new_items`，需要原始模型负载时检查 `raw_responses`。

计算机工具重放遵循原始 Responses 负载结构。预览模型的 `computer_call` 条目保留单个 `action`，而 `gpt-5.4` 的计算机调用可保留批量 `actions[]`。[`to_input_list()`][agents.result.RunResultBase.to_input_list] 和 [`RunState`][agents.run_state.RunState] 会保持模型产出的结构，因此手动重放、暂停/恢复流程和存储转录在预览版与 GA 计算机工具调用之间都可继续工作。本地执行结果仍会以 `computer_call_output` 条目形式出现在 `new_items` 中。

### 新条目

[`new_items`][agents.result.RunResultBase.new_items] 为你提供运行期间发生情况的最丰富视图。常见条目类型有：

-   用于助手消息的 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   用于推理条目的 [`ReasoningItem`][agents.items.ReasoningItem]
-   用于 Responses 工具搜索请求及已加载工具检索结果的 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 和 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   用于工具调用及其结果的 [`ToolCallItem`][agents.items.ToolCallItem] 和 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   用于因审批而暂停的工具调用的 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   用于任务转移请求与已完成转移的 [`HandoffCallItem`][agents.items.HandoffCallItem] 和 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

当你需要智能体关联、工具输出、任务转移边界或审批边界时，应优先使用 `new_items` 而不是 `to_input_list()`。

使用托管工具检索时，可检查 `ToolSearchCallItem.raw_item` 以查看模型发出的检索请求，并检查 `ToolSearchOutputItem.raw_item` 以查看该轮加载了哪些命名空间、函数或托管 MCP 服务。

## 对话继续或恢复

### 下一轮智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 包含最后一个运行的智能体。在任务转移后，这通常是下一轮用户输入最适合复用的智能体。

在流式模式中，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随着运行推进而更新，因此你可以在流结束前观察任务转移。

### 中断与运行状态

如果工具需要审批，待处理审批会暴露在 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 或 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 中。这可能包含由直接工具、任务转移后到达的工具，或嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行触发的审批。

调用 [`to_state()`][agents.result.RunResult.to_state] 以捕获可恢复的 [`RunState`][agents.run_state.RunState]，审批或拒绝待处理条目，然后通过 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复运行。

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="Use tools when needed.")
result = await Runner.run(agent, "Delete temp files that are no longer needed.")

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state)
```

对于流式运行，先完成对 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 的消费，再检查 `result.interruptions` 并从 `result.to_state()` 恢复。完整审批流程请参见[人类参与](human_in_the_loop.md)。

### 服务端托管续接

[`last_response_id`][agents.result.RunResultBase.last_response_id] 是该运行中最新模型响应 ID。若要在下一轮继续 OpenAI Responses API 链，可将其作为 `previous_response_id` 传回。

如果你已经通过 `to_input_list()`、`session` 或 `conversation_id` 继续对话，通常不需要 `last_response_id`。若你需要多步骤运行中的每个模型响应，请改为检查 `raw_responses`。

## Agent-as-tool 元数据

当结果来自嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行时，[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] 会暴露外层工具调用的不可变元数据：

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

对于普通顶层运行，`agent_tool_invocation` 为 `None`。

这在 `custom_output_extractor` 中尤其有用，此时你可能需要在对嵌套结果做后处理时使用外层工具名、调用 ID 或原始参数。有关周边 `Agent.as_tool()` 模式，参见[工具](tools.md)。

如果你还需要该嵌套运行的已解析结构化输入，请读取 `context_wrapper.tool_input`。这是 [`RunState`][agents.run_state.RunState] 为嵌套工具输入做通用序列化时使用的字段，而 `agent_tool_invocation` 是当前嵌套调用的实时结果访问器。

## 流式生命周期与诊断

[`RunResultStreaming`][agents.result.RunResultStreaming] 继承了上述相同结果接口，但增加了流式传输专用控制项：

-   使用 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 消费语义化流事件
-   使用 [`current_agent`][agents.result.RunResultStreaming.current_agent] 跟踪运行中当前活跃智能体
-   使用 [`is_complete`][agents.result.RunResultStreaming.is_complete] 查看流式运行是否已完全结束
-   使用 [`cancel(...)`][agents.result.RunResultStreaming.cancel] 立即停止运行或在当前轮次后停止

持续消费 `stream_events()`，直到异步迭代器结束。只有当该迭代器结束后，流式运行才算完成；而 `final_output`、`interruptions`、`raw_responses` 以及会话持久化副作用等汇总属性，在最后一个可见 token 到达后仍可能继续收敛。

如果你调用了 `cancel()`，请继续消费 `stream_events()`，以便正确完成取消和清理。

Python 不提供单独的流式 `completed` promise 或 `error` 属性。终止性的流式失败会通过 `stream_events()` 抛出异常体现，而 `is_complete` 反映运行是否已到达终态。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 包含运行期间收集的原始模型响应。多步骤运行可能产生多个响应，例如跨任务转移或重复的 模型/工具/模型 循环。

[`last_response_id`][agents.result.RunResultBase.last_response_id] 只是 `raw_responses` 最后一项中的 ID。

### 安全防护措施结果

智能体级安全防护措施结果通过 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 暴露。

工具级安全防护措施结果则单独通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 暴露。

这些数组会在整个运行过程中持续累积，因此非常适合用于记录决策、存储额外的安全防护措施元数据，或调试运行被拦截的原因。

### 上下文与用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] 会暴露你的应用上下文以及由 SDK 管理的运行时元数据，例如审批、用量和嵌套 `tool_input`。

用量记录在 `context_wrapper.usage` 上。对于流式运行，用量总计可能会滞后，直到流的最终分块被处理完成。完整包装结构和持久化注意事项请参见[上下文管理](context.md)。