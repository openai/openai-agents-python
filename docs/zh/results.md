---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，会收到以下两种结果类型之一：

-   来自 `Runner.run(...)` 或 `Runner.run_sync(...)` 的 [`RunResult`][agents.result.RunResult]
-   来自 `Runner.run_streamed(...)` 的 [`RunResultStreaming`][agents.result.RunResultStreaming]

二者都继承自 [`RunResultBase`][agents.result.RunResultBase]，后者公开了共享的结果接口，如 `final_output`、`new_items`、`last_agent`、`raw_responses` 和 `to_state()`。

`RunResultStreaming` 增加了流式传输专用控制项，例如 [`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete] 和 [`cancel(...)`][agents.result.RunResultStreaming.cancel]。

## 合适结果接口选择

大多数应用只需要少量结果属性或辅助方法：

| 如果你需要... | 使用 |
| --- | --- |
| 展示给用户的最终答案 | `final_output` |
| 可重放的下一轮输入列表（包含完整本地对话记录） | `to_input_list()` |
| 带有智能体、工具、任务转移与审批元数据的丰富运行条目 | `new_items` |
| 通常应处理下一轮用户输入的智能体 | `last_agent` |
| 使用 `previous_response_id` 进行 OpenAI Responses API 链式调用 | `last_response_id` |
| 待处理审批和可恢复快照 | `interruptions` 和 `to_state()` |
| 当前嵌套 `Agent.as_tool()` 调用的元数据 | `agent_tool_invocation` |
| 原始模型调用或安全防护措施诊断信息 | `raw_responses` 和各类安全防护措施结果数组 |

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行智能体的最终输出。它可能是：

-   `str`，如果最后一个智能体未定义 `output_type`
-   `last_agent.output_type` 类型的对象，如果最后一个智能体定义了输出类型
-   `None`，如果运行在生成最终输出前已停止，例如因审批中断而暂停

!!! note

    `final_output` 的类型为 `Any`。任务转移可能改变最终完成运行的智能体，因此 SDK 无法在静态层面获知所有可能的输出类型集合。

在流式传输模式下，`final_output` 会在流处理完成前一直保持 `None`。关于逐事件流程，请参见 [流式传输](streaming.md)。

## 输入、下一轮历史与新条目

这些接口回答的是不同问题：

| 属性或辅助方法 | 包含内容 | 最适用场景 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 此运行片段的基础输入。如果任务转移输入过滤器重写了历史，这里反映的是运行继续使用的已过滤输入。 | 审计该次运行实际使用了什么输入 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 由 `input` 加上本次运行中 `new_items` 转换结果构成的、可重放的下一轮输入列表。 | 手动聊天循环与客户端管理的对话状态 |
| [`new_items`][agents.result.RunResultBase.new_items] | 带有智能体、工具、任务转移和审批元数据的丰富 [`RunItem`][agents.items.RunItem] 包装对象。 | 日志、UI、审计与调试 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 运行中每次模型调用返回的原始 [`ModelResponse`][agents.items.ModelResponse] 对象。 | 提供商级诊断或原始响应检查 |

在实践中：

-   当你的应用手动维护完整对话记录时，使用 `to_input_list()`。
-   当你希望 SDK 为你加载和保存历史时，使用 [`session=...`](sessions/index.md)。
-   如果你在用带 `conversation_id` 或 `previous_response_id` 的 OpenAI 服务端托管状态，通常只需传入新的用户输入并复用已存储 ID，而不是重新发送 `to_input_list()`。

不同于 JavaScript SDK，Python 不会额外公开一个仅含模型形状增量的 `output` 属性。需要 SDK 元数据时用 `new_items`，需要原始模型负载时检查 `raw_responses`。

### 新条目

[`new_items`][agents.result.RunResultBase.new_items] 能让你最完整地看到运行期间发生了什么。常见条目类型包括：

-   用于助手消息的 [`MessageOutputItem`][agents.items.MessageOutputItem]
-   用于推理条目的 [`ReasoningItem`][agents.items.ReasoningItem]
-   用于工具调用及其结果的 [`ToolCallItem`][agents.items.ToolCallItem] 和 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   用于因审批而暂停的工具调用的 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   用于任务转移请求和完成转移的 [`HandoffCallItem`][agents.items.HandoffCallItem] 和 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

当你需要智能体关联、工具输出、任务转移边界或审批边界时，应优先选择 `new_items` 而非 `to_input_list()`。

## 对话延续或恢复

### 下一轮智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 包含最后运行的智能体。在发生任务转移后，它通常是下一轮用户输入最合适复用的智能体。

在流式传输模式下，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随运行进展更新，因此你可以在流结束前观察到任务转移。

### 中断与运行状态

如果工具需要审批，待处理审批会暴露在 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 或 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 中。这可能包括：直接工具触发的审批、任务转移后到达工具触发的审批，或嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行触发的审批。

调用 [`to_state()`][agents.result.RunResult.to_state] 可捕获一个可恢复的 [`RunState`][agents.run_state.RunState]，对待处理条目进行批准或拒绝，然后用 `Runner.run(...)` 或 `Runner.run_streamed(...)` 继续运行。

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

对于流式运行，先消费完 [`stream_events()`][agents.result.RunResultStreaming.stream_events]，再检查 `result.interruptions` 并从 `result.to_state()` 恢复。完整审批流程请参见 [Human-in-the-loop](human_in_the_loop.md)。

### 服务端托管的延续

[`last_response_id`][agents.result.RunResultBase.last_response_id] 是此次运行最新的模型响应 ID。若你想继续一个 OpenAI Responses API 链，可在下一轮把它作为 `previous_response_id` 传回。

如果你已经通过 `to_input_list()`、`session` 或 `conversation_id` 来延续对话，通常不需要 `last_response_id`。如果你需要多步骤运行中的每个模型响应，请改为检查 `raw_responses`。

## Agent-as-tool 元数据

当结果来自嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行时，[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] 会公开外层工具调用的不可变元数据：

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

对于普通顶层运行，`agent_tool_invocation` 为 `None`。

这在 `custom_output_extractor` 中尤其有用：当你对嵌套结果做后处理时，可能需要外层工具名、调用 ID 或原始参数。有关周边 `Agent.as_tool()` 模式，请参见 [工具](tools.md)。

如果你还需要该嵌套运行的已解析结构化输入，请读取 `context_wrapper.tool_input`。这是 [`RunState`][agents.run_state.RunState] 以通用方式序列化嵌套工具输入所用的字段，而 `agent_tool_invocation` 是当前嵌套调用的实时结果访问器。

## 流式传输生命周期与诊断

[`RunResultStreaming`][agents.result.RunResultStreaming] 继承了上述相同结果接口，但增加了流式传输专用控制项：

-   使用 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 消费语义化流事件
-   使用 [`current_agent`][agents.result.RunResultStreaming.current_agent] 跟踪运行中当前活跃智能体
-   使用 [`is_complete`][agents.result.RunResultStreaming.is_complete] 查看流式运行是否已完全结束
-   使用 [`cancel(...)`][agents.result.RunResultStreaming.cancel] 立即停止运行或在当前轮次后停止

持续消费 `stream_events()`，直到异步迭代器结束。流式运行在该迭代器结束前都不算完成；并且诸如 `final_output`、`interruptions`、`raw_responses` 以及 session 持久化副作用等汇总属性，可能在最后一个可见 token 到达后仍在收敛。

如果你调用了 `cancel()`，仍应继续消费 `stream_events()`，以便取消与清理过程正确完成。

Python 不提供独立的流式 `completed` promise 或 `error` 属性。终止性流错误会通过 `stream_events()` 抛出异常体现，而 `is_complete` 反映运行是否已到达终止状态。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 包含运行期间收集到的原始模型响应。多步骤运行可能产生不止一个响应，例如跨任务转移或重复的模型/工具/模型循环。

[`last_response_id`][agents.result.RunResultBase.last_response_id] 只是 `raw_responses` 最后一项的 ID。

### 安全防护措施结果

智能体级安全防护措施结果通过 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 暴露。

工具级安全防护措施结果则通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 分别暴露。

这些数组会在整个运行中持续累积，因此很适合用于记录决策、存储额外安全防护措施元数据，或调试运行被阻止的原因。

### 上下文与用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] 会公开你的应用上下文，以及由 SDK 管理的运行时元数据（如审批、用量和嵌套 `tool_input`）。

用量记录在 `context_wrapper.usage` 上。对于流式运行，用量总计可能会滞后，直到流的最终分块处理完毕。完整包装器结构和持久化注意事项请参见 [上下文管理](context.md)。