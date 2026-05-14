---
search:
  exclude: true
---
# 结果

调用 `Runner.run` 方法时，你会收到以下两种结果类型之一：

-   来自 `Runner.run(...)` 或 `Runner.run_sync(...)` 的 [`RunResult`][agents.result.RunResult]
-   来自 `Runner.run_streamed(...)` 的 [`RunResultStreaming`][agents.result.RunResultStreaming]

二者都继承自 [`RunResultBase`][agents.result.RunResultBase]，后者公开了共享的结果接口，例如 `final_output`、`new_items`、`last_agent`、`raw_responses` 和 `to_state()`。

`RunResultStreaming` 增加了流式传输专用控制项，例如 [`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete] 和 [`cancel(...)`][agents.result.RunResultStreaming.cancel]。

## 合适的结果接口

大多数应用只需要少数几个结果属性或辅助方法：

| 如果你需要... | 使用 |
| --- | --- |
| 展示给用户的最终答案 | `final_output` |
| 可用于重放的下一轮输入列表，包含完整本地转录记录 | `to_input_list()` |
| 包含智能体、工具、任务转移和审批元数据的丰富运行条目 | `new_items` |
| 通常应处理下一轮用户输入的智能体 | `last_agent` |
| 使用 `previous_response_id` 进行 OpenAI Responses API 链接 | `last_response_id` |
| 待处理审批和可恢复快照 | `interruptions` 和 `to_state()` |
| 当前嵌套 `Agent.as_tool()` 调用的元数据 | `agent_tool_invocation` |
| 原始模型调用或安全防护措施诊断 | `raw_responses` 和安全防护措施结果数组 |

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后运行的智能体的最终输出。它可能是：

-   一个 `str`，如果最后一个智能体未定义 `output_type`
-   `last_agent.output_type` 类型的对象，如果最后一个智能体定义了输出类型
-   `None`，如果运行在产生最终输出之前停止，例如因审批中断而暂停

!!! note

    `final_output` 的类型为 `Any`。任务转移可能会改变哪个智能体结束运行，因此 SDK 无法静态得知所有可能的输出类型。

在流式传输模式下，`final_output` 会保持为 `None`，直到流处理完成。有关逐事件流程，请参阅[流式传输](streaming.md)。

## 输入、下一轮历史记录和新条目

这些接口回答的是不同问题：

| 属性或辅助方法 | 包含的内容 | 最适合 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 此运行片段的基础输入。如果任务转移输入过滤器重写了历史记录，这里会反映运行继续时所使用的过滤后输入。 | 审计此运行实际使用了什么作为输入 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 运行的输入条目视图。默认 `mode="preserve_all"` 会保留来自 `new_items` 的完整转换后历史记录；`mode="normalized"` 会在任务转移过滤重写模型历史记录时优先使用规范续接输入。 | 手动聊天循环、客户端管理的对话状态，以及普通条目历史记录检查 |
| [`new_items`][agents.result.RunResultBase.new_items] | 包含智能体、工具、任务转移和审批元数据的丰富 [`RunItem`][agents.items.RunItem] 包装器。 | 日志、UI、审计和调试 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 运行中每次模型调用产生的原始 [`ModelResponse`][agents.items.ModelResponse] 对象。 | 提供方级诊断或原始响应检查 |

实践中：

-   当你想要运行的普通输入条目视图时，使用 `to_input_list()`。
-   当你希望在任务转移过滤或嵌套任务转移历史记录重写后，为下一次 `Runner.run(..., input=...)` 调用获得规范本地输入时，使用 `to_input_list(mode="normalized")`。
-   当你希望 SDK 为你加载和保存历史记录时，使用 [`session=...`](sessions/index.md)。
-   如果你使用带有 `conversation_id` 或 `previous_response_id` 的 OpenAI服务管理状态，通常只传递新的用户输入，并复用已存储的 ID，而不是重新发送 `to_input_list()`。
-   当你需要用于日志、UI 或审计的完整转换后历史记录时，使用默认的 `to_input_list()` 模式或 `new_items`。

与 JavaScript SDK 不同，Python 不会公开一个单独的 `output` 属性来仅表示模型形态的增量。当你需要 SDK 元数据时，使用 `new_items`；当你需要原始模型载荷时，检查 `raw_responses`。

计算机工具重放遵循原始 Responses 载荷结构。预览模型的 `computer_call` 条目会保留单个 `action`，而 `gpt-5.5` 计算机调用可以保留批处理的 `actions[]`。[`to_input_list()`][agents.result.RunResultBase.to_input_list] 和 [`RunState`][agents.run_state.RunState] 会保留模型生成的任一结构，因此手动重放、暂停/恢复流程和已存储的转录记录都能继续适用于预览版和 GA 计算机工具调用。本地执行结果仍会以 `computer_call_output` 条目的形式出现在 `new_items` 中。

### 新条目

[`new_items`][agents.result.RunResultBase.new_items] 为你提供运行期间所发生事件的最丰富视图。常见条目类型包括：

-   用于助手消息的 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   用于推理条目的 [`ReasoningItem`][agents.items.ReasoningItem]
-   用于 Responses 工具搜索请求和已加载工具搜索结果的 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 与 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   用于工具调用及其结果的 [`ToolCallItem`][agents.items.ToolCallItem] 与 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   用于因审批而暂停的工具调用的 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   用于任务转移请求和已完成转移的 [`HandoffCallItem`][agents.items.HandoffCallItem] 与 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

每当你需要智能体关联、工具输出、任务转移边界或审批边界时，应选择 `new_items` 而不是 `to_input_list()`。

使用托管工具搜索时，检查 `ToolSearchCallItem.raw_item` 可查看模型发出的搜索请求，检查 `ToolSearchOutputItem.raw_item` 可查看该轮次加载了哪些命名空间、函数或托管 MCP 服务。

## 对话的继续或恢复

### 下一轮智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 包含最后运行的智能体。在任务转移之后，这通常是下一轮用户输入最适合复用的智能体。

在流式传输模式下，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随着运行进展而更新，因此你可以在流结束前观察任务转移。

### 中断和运行状态

如果工具需要审批，待处理审批会通过 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 或 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 暴露。这可以包括由直接工具引发、由任务转移后到达的工具引发，或由嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行引发的审批。

调用 [`to_state()`][agents.result.RunResult.to_state] 可捕获可恢复的 [`RunState`][agents.run_state.RunState]，批准或拒绝待处理条目，然后使用 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复。

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

对于流式传输运行，请先完成对 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 的消费，然后检查 `result.interruptions` 并从 `result.to_state()` 恢复。有关完整审批流程，请参阅[人在回路](human_in_the_loop.md)。

### 服务管理的续接

[`last_response_id`][agents.result.RunResultBase.last_response_id] 是运行中最新的模型响应 ID。当你想继续 OpenAI Responses API 链时，在下一轮将它作为 `previous_response_id` 传回。

如果你已经使用 `to_input_list()`、`session` 或 `conversation_id` 继续对话，通常不需要 `last_response_id`。如果需要多步运行中的每个模型响应，请改为检查 `raw_responses`。

## 智能体作为工具的元数据

当结果来自嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行时，[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] 会公开关于外层工具调用的不可变元数据：

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

对于普通顶层运行，`agent_tool_invocation` 为 `None`。

这在 `custom_output_extractor` 内尤其有用，因为在对嵌套结果进行后处理时，你可能需要外层工具名称、调用 ID 或原始参数。有关周围的 `Agent.as_tool()` 模式，请参阅[工具](tools.md)。

如果你还需要该嵌套运行的已解析结构化输入，请读取 `context_wrapper.tool_input`。这是 [`RunState`][agents.run_state.RunState] 用于以通用方式序列化嵌套工具输入的字段，而 `agent_tool_invocation` 是当前嵌套调用的实时结果访问器。

## 流式传输生命周期和诊断

[`RunResultStreaming`][agents.result.RunResultStreaming] 继承上述相同结果接口，但增加了流式传输专用控制项：

-   [`stream_events()`][agents.result.RunResultStreaming.stream_events] 用于消费语义流事件
-   [`current_agent`][agents.result.RunResultStreaming.current_agent] 用于在运行过程中跟踪活跃智能体
-   [`is_complete`][agents.result.RunResultStreaming.is_complete] 用于查看流式传输运行是否已完全结束
-   [`cancel(...)`][agents.result.RunResultStreaming.cancel] 用于立即停止运行，或在当前轮次结束后停止运行

持续消费 `stream_events()`，直到异步迭代器结束。只有该迭代器结束后，流式传输运行才算完成；并且在最后一个可见 token 到达后，`final_output`、`interruptions`、`raw_responses` 等汇总属性以及会话持久化副作用可能仍在收尾。

如果调用 `cancel()`，请继续消费 `stream_events()`，以便取消和清理能够正确完成。

Python 不会公开单独的流式 `completed` promise 或 `error` 属性。终止性流式传输失败会通过 `stream_events()` 抛出异常来呈现，而 `is_complete` 反映运行是否已到达其终止状态。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 包含运行期间收集的原始模型响应。多步运行可能会生成多个响应，例如跨任务转移或重复的模型/工具/模型循环。

[`last_response_id`][agents.result.RunResultBase.last_response_id] 只是 `raw_responses` 最后一项中的 ID。

### 安全防护措施结果

智能体级安全防护措施通过 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 暴露。

工具安全防护措施则分别通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 暴露。

这些数组会在运行过程中累积，因此它们适用于记录决策、存储额外的安全防护措施元数据，或调试运行为何被阻止。

### 上下文和用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] 会公开你的应用上下文，以及由 SDK 管理的运行时元数据，例如审批、用量和嵌套 `tool_input`。

用量在 `context_wrapper.usage` 上跟踪。对于流式传输运行，用量总计可能会滞后，直到流的最终分块处理完毕。有关完整的包装器结构和持久化注意事项，请参阅[上下文管理](context.md)。