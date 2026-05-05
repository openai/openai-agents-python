---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，会收到以下两种结果类型之一：

-   来自 `Runner.run(...)` 或 `Runner.run_sync(...)` 的 [`RunResult`][agents.result.RunResult]
-   来自 `Runner.run_streamed(...)` 的 [`RunResultStreaming`][agents.result.RunResultStreaming]

二者都继承自 [`RunResultBase`][agents.result.RunResultBase]，后者暴露共享的结果表面，例如 `final_output`、`new_items`、`last_agent`、`raw_responses` 和 `to_state()`。

`RunResultStreaming` 添加了特定于流式传输的控制项，例如 [`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete] 和 [`cancel(...)`][agents.result.RunResultStreaming.cancel]。

## 正确结果表面的选择

大多数应用只需要少数结果属性或辅助方法：

| 如果你需要... | 使用 |
| --- | --- |
| 展示给用户的最终答案 | `final_output` |
| 带有完整本地转录、可用于重放的下一轮输入列表 | `to_input_list()` |
| 包含智能体、工具、任务转移和审批元数据的丰富运行项 | `new_items` |
| 通常应处理下一轮用户输入的智能体 | `last_agent` |
| 使用 `previous_response_id` 的 OpenAI Responses API 链接 | `last_response_id` |
| 待审批项和可恢复快照 | `interruptions` 和 `to_state()` |
| 关于当前嵌套 `Agent.as_tool()` 调用的元数据 | `agent_tool_invocation` |
| 原始模型调用或安全防护措施诊断信息 | `raw_responses` 和安全防护措施结果数组 |

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后运行的智能体的最终输出。它可能是：

-   `str`，如果最后的智能体没有定义 `output_type`
-   `last_agent.output_type` 类型的对象，如果最后的智能体定义了输出类型
-   `None`，如果运行在生成最终输出之前停止，例如因为在审批中断处暂停

!!! note

    `final_output` 的类型为 `Any`。任务转移可能会改变哪个智能体结束运行，因此 SDK 无法静态获知所有可能的输出类型。

在流式传输模式下，`final_output` 会保持为 `None`，直到流处理完成。有关逐事件流程，请参阅[流式传输](streaming.md)。

## 输入、下一轮历史和新项

这些表面回答不同的问题：

| 属性或辅助方法 | 包含内容 | 最适合 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 此运行片段的基础输入。如果任务转移输入过滤器重写了历史，则这里反映运行继续使用的已过滤输入。 | 审计此运行实际使用的输入 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 运行的输入项视图。默认的 `mode="preserve_all"` 会保留从 `new_items` 转换而来的完整历史；`mode="normalized"` 会在任务转移过滤重写模型历史时优先使用规范的延续输入。 | 手动聊天循环、客户端管理的对话状态，以及普通项历史检查 |
| [`new_items`][agents.result.RunResultBase.new_items] | 带有智能体、工具、任务转移和审批元数据的丰富 [`RunItem`][agents.items.RunItem] 包装器。 | 日志、UI、审计和调试 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 运行中每次模型调用产生的原始 [`ModelResponse`][agents.items.ModelResponse] 对象。 | 提供方级诊断或原始响应检查 |

实际使用中：

-   当你想要运行的普通输入项视图时，使用 `to_input_list()`。
-   当你想要在任务转移过滤或嵌套任务转移历史重写之后，用于下一次 `Runner.run(..., input=...)` 调用的规范本地输入时，使用 `to_input_list(mode="normalized")`。
-   当你希望 SDK 为你加载和保存历史时，使用 [`session=...`](sessions/index.md)。
-   如果你使用 OpenAI 服务端管理状态以及 `conversation_id` 或 `previous_response_id`，通常只传递新的用户输入并复用已存储的 ID，而不是重新发送 `to_input_list()`。
-   当你需要用于日志、UI 或审计的完整转换历史时，使用默认的 `to_input_list()` 模式或 `new_items`。

与 JavaScript SDK 不同，Python 不会为仅按模型形状表示的增量暴露单独的 `output` 属性。当你需要 SDK 元数据时使用 `new_items`，当你需要原始模型载荷时检查 `raw_responses`。

计算机工具重放遵循原始 Responses 载荷形状。预览模型的 `computer_call` 项会保留单个 `action`，而 `gpt-5.5` 计算机调用可以保留批量的 `actions[]`。[`to_input_list()`][agents.result.RunResultBase.to_input_list] 和 [`RunState`][agents.run_state.RunState] 会保留模型生成的任一形状，因此手动重放、暂停/恢复流程和已存储的转录可在预览版和 GA 计算机工具调用中继续工作。本地执行结果仍会在 `new_items` 中显示为 `computer_call_output` 项。

### 新项

[`new_items`][agents.result.RunResultBase.new_items] 为你提供运行期间所发生事情的最丰富视图。常见项类型包括：

-   用于助手消息的 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   用于推理项的 [`ReasoningItem`][agents.items.ReasoningItem]
-   用于 Responses 工具检索请求和已加载工具检索结果的 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 和 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   用于工具调用及其结果的 [`ToolCallItem`][agents.items.ToolCallItem] 和 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   用于因审批而暂停的工具调用的 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   用于任务转移请求和已完成转移的 [`HandoffCallItem`][agents.items.HandoffCallItem] 和 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

每当你需要智能体关联、工具输出、任务转移边界或审批边界时，请选择 `new_items`，而不是 `to_input_list()`。

当你使用托管工具检索时，检查 `ToolSearchCallItem.raw_item` 可查看模型发出的检索请求，检查 `ToolSearchOutputItem.raw_item` 可查看本轮加载了哪些命名空间、函数或托管 MCP 服务。

## 对话的继续或恢复

### 下一轮智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 包含最后运行的智能体。在任务转移之后，这通常是下一轮用户输入中最适合复用的智能体。

在流式传输模式下，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随着运行推进而更新，因此你可以在流结束之前观察任务转移。

### 中断和运行状态

如果工具需要审批，待审批项会通过 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 或 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 暴露出来。这可能包括由直接工具、任务转移后到达的工具，或嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行提出的审批。

调用 [`to_state()`][agents.result.RunResult.to_state] 以捕获可恢复的 [`RunState`][agents.run_state.RunState]，批准或拒绝待处理项，然后使用 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复。

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

对于流式传输运行，请先消费完 [`stream_events()`][agents.result.RunResultStreaming.stream_events]，然后检查 `result.interruptions` 并从 `result.to_state()` 恢复。完整审批流程请参阅[人在环路](human_in_the_loop.md)。

### 服务端管理的延续

[`last_response_id`][agents.result.RunResultBase.last_response_id] 是运行中的最新模型响应 ID。当你想继续 OpenAI Responses API 链时，在下一轮将它作为 `previous_response_id` 传回。

如果你已经通过 `to_input_list()`、`session` 或 `conversation_id` 继续对话，通常不需要 `last_response_id`。如果你需要多步骤运行中的每个模型响应，请改为检查 `raw_responses`。

## Agent-as-tool 元数据

当结果来自嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行时，[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] 会暴露关于外层工具调用的不可变元数据：

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

对于普通的顶层运行，`agent_tool_invocation` 为 `None`。

这在 `custom_output_extractor` 内尤其有用，你可能需要在对嵌套结果进行后处理时使用外层工具名称、调用 ID 或原始参数。有关周边的 `Agent.as_tool()` 模式，请参阅[工具](tools.md)。

如果你还需要该嵌套运行的已解析结构化输入，请读取 `context_wrapper.tool_input`。这是 [`RunState`][agents.run_state.RunState] 用于通用序列化嵌套工具输入的字段，而 `agent_tool_invocation` 是当前嵌套调用的实时结果访问器。

## 流式传输生命周期和诊断

[`RunResultStreaming`][agents.result.RunResultStreaming] 继承上文相同的结果表面，但添加了特定于流式传输的控制项：

-   [`stream_events()`][agents.result.RunResultStreaming.stream_events] 用于消费语义流事件
-   [`current_agent`][agents.result.RunResultStreaming.current_agent] 用于在运行中途跟踪活动智能体
-   [`is_complete`][agents.result.RunResultStreaming.is_complete] 用于查看流式运行是否已完全结束
-   [`cancel(...)`][agents.result.RunResultStreaming.cancel] 用于立即停止运行，或在当前轮次后停止运行

持续消费 `stream_events()`，直到异步迭代器结束。流式传输运行只有在该迭代器结束后才算完成，并且在最后一个可见 token 到达后，`final_output`、`interruptions`、`raw_responses` 等摘要属性以及会话持久化副作用可能仍在收尾。

如果你调用 `cancel()`，请继续消费 `stream_events()`，以便取消和清理能够正确完成。

Python 不会暴露单独的流式 `completed` promise 或 `error` 属性。终止性流式传输失败会通过 `stream_events()` 抛出异常来呈现，而 `is_complete` 反映运行是否已达到其终止状态。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 包含运行期间收集的原始模型响应。多步骤运行可能会产生多个响应，例如跨任务转移或重复的模型/工具/模型循环。

[`last_response_id`][agents.result.RunResultBase.last_response_id] 只是 `raw_responses` 中最后一个条目的 ID。

### 安全防护措施结果

智能体级安全防护措施通过 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 暴露。

工具安全防护措施则分别通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 暴露。

这些数组会在整个运行过程中累积，因此它们对记录决策、存储额外的安全防护措施元数据，或调试运行为何被阻止很有用。

### 上下文和用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] 会将你的应用上下文与 SDK 管理的运行时元数据一起暴露，例如审批、用量和嵌套 `tool_input`。

用量会在 `context_wrapper.usage` 上跟踪。对于流式传输运行，在流的最终分块处理完成之前，用量总计可能会滞后。有关完整包装器形状和持久化注意事项，请参阅[上下文管理](context.md)。