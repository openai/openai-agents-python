---
search:
  exclude: true
---
# 结果

调用 `Runner.run` 方法时，你会收到以下两种结果类型之一：

- [`RunResult`][agents.result.RunResult]，来自 `Runner.run(...)` 或 `Runner.run_sync(...)`
- [`RunResultStreaming`][agents.result.RunResultStreaming]，来自 `Runner.run_streamed(...)`

两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，后者提供共享的结果接口，例如 `final_output`、`new_items`、`last_agent`、`raw_responses` 和 `to_state()`。

`RunResultStreaming` 还添加了流式传输专用控制功能，例如 [`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete] 和 [`cancel(...)`][agents.result.RunResultStreaming.cancel]。

## 结果接口的选择

大多数应用只需要少数几个结果属性或辅助方法：

| 如果需要…… | 使用 |
| --- | --- |
| 向用户显示最终答案 | `final_output` |
| 包含完整本地对话记录、可用于重放的下一轮输入列表 | `to_input_list()` |
| 包含智能体、工具、任务转移和审批元数据的丰富运行项 | `new_items` |
| 通常应处理下一轮用户输入的智能体 | `last_agent` |
| 使用 `previous_response_id` 串联 OpenAI Responses API | `last_response_id` |
| 待处理的审批和可恢复快照 | `interruptions` 和 `to_state()` |
| 当前嵌套 `Agent.as_tool()` 调用的元数据 | `agent_tool_invocation` |
| 原始模型调用或安全防护措施诊断信息 | `raw_responses` 和安全防护措施结果数组 |

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后运行的智能体所生成的最终输出。它可能是：

- `str`，如果最后一个智能体未定义 `output_type`
- `last_agent.output_type` 类型的对象，如果最后一个智能体定义了输出类型
- `None`，如果运行在生成最终输出之前停止，例如因审批中断而暂停

!!! note

    `final_output` 的类型标注为 `Any`。任务转移可能会改变最终完成运行的智能体，因此 SDK 无法静态确定所有可能的输出类型。

在流式传输模式下，`final_output` 会一直保持为 `None`，直到流处理完成。有关逐事件处理流程，请参阅[流式传输](streaming.md)。

## 输入、下一轮历史记录和新项目

这些接口分别回答不同的问题：

| 属性或辅助方法 | 包含的内容 | 最适合 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 此运行片段的基础输入。如果任务转移输入过滤器重写了历史记录，此属性会反映运行继续执行时所使用的过滤后输入。 | 审计此运行实际使用的输入 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 运行的输入项视图。默认的 `mode="preserve_all"` 会保留从 `new_items` 转换而来的历史记录，但不会再次追加已被移入 SDK 默认嵌套任务转移历史记录的同一会话项；当任务转移过滤重写模型历史记录时，`mode="normalized"` 会优先使用规范化的延续输入。 | 手动聊天循环、由客户端管理的对话状态和普通项目历史记录检查 |
| [`new_items`][agents.result.RunResultBase.new_items] | 包含智能体、工具、任务转移和审批元数据的丰富 [`RunItem`][agents.items.RunItem] 包装对象。 | 日志、UI、审计和调试 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 运行中每次模型调用产生的原始 [`ModelResponse`][agents.items.ModelResponse] 对象。 | 提供方级别的诊断或原始响应检查 |

实际使用时：

- 当你需要运行的普通输入项视图时，使用 `to_input_list()`。
- 当你需要在任务转移过滤或嵌套任务转移历史记录重写后，将规范化本地输入用于下一次 `Runner.run(..., input=...)` 调用时，使用 `to_input_list(mode="normalized")`。
- 当你希望 SDK 自动加载和保存历史记录时，使用 [`session=...`](sessions/index.md)。
- 如果你正在使用通过 `conversation_id` 或 `previous_response_id` 实现的 OpenAI 服务端托管状态，通常只需传入新的用户输入并复用已存储的 ID，而不必重新发送 `to_input_list()`。
- 当你需要用于日志、UI 或审计的完整转换后历史记录时，使用默认的 `to_input_list()` 模式或 `new_items`。

当 SDK 默认的嵌套任务转移历史记录逐字保留某个消息项时，Sessions、`RunState` 和 `to_input_list()` 会追踪该项由其拥有的确切实例，而不是按内容进行去重。分别出现的相同消息仍会保持独立；只有已被拥有的实例不会被再次追加。

与 JavaScript SDK 不同，Python 不会为仅包含模型形态增量的内容提供单独的 `output` 属性。当你需要 SDK 元数据时，请使用 `new_items`；当你需要原始模型载荷时，请检查 `raw_responses`。

计算机工具重放遵循原始 Responses 载荷结构。预览模型的 `computer_call` 项会保留单个 `action`，而 `gpt-5.5` 的计算机调用可以保留批量的 `actions[]`。[`to_input_list()`][agents.result.RunResultBase.to_input_list] 和 [`RunState`][agents.run_state.RunState] 会保留模型生成的结构，因此手动重放、暂停/恢复流程和存储的对话记录都能同时适用于预览版和正式版（GA）的计算机工具调用。本地执行结果仍会作为 `computer_call_output` 项出现在 `new_items` 中。

### 新项目

[`new_items`][agents.result.RunResultBase.new_items] 提供运行期间所发生事件的最丰富视图。常见的项目类型包括：

- 用于助手消息的 [`MessageOutputItem`][agents.items.MessageOutputItem]
- 用于推理项目的 [`ReasoningItem`][agents.items.ReasoningItem]
- 用于 Responses 工具搜索请求和已加载工具搜索结果的 [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 和 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
- 用于工具调用及其结果的 [`ToolCallItem`][agents.items.ToolCallItem] 和 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
- 用于因等待审批而暂停的工具调用的 [`ToolApprovalItem`][agents.items.ToolApprovalItem]
- 用于托管 MCP 审批和工具目录的 [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem]、[`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem] 和 [`MCPListToolsItem`][agents.items.MCPListToolsItem]
- 用于任务转移请求和已完成转移的 [`HandoffCallItem`][agents.items.HandoffCallItem] 和 [`HandoffOutputItem`][agents.items.HandoffOutputItem]

只要你需要智能体关联信息、工具输出、任务转移边界或审批边界，就应选择 `new_items` 而不是 `to_input_list()`。

使用托管工具搜索时，检查 `ToolSearchCallItem.raw_item` 可查看模型发出的搜索请求，检查 `ToolSearchOutputItem.raw_item` 可查看该轮加载了哪些命名空间、函数或托管 MCP 服务。

使用程序化工具调用时，生成的 `program` 是一个 `ToolCallItem`，归该程序所有的普通子工具调用也会作为 `ToolCallItem` 项，而匹配的 `program_output` 则是一个 `ToolCallOutputItem`。程序拥有的托管 MCP `mcp_approval_request` 和 `mcp_list_tools` 项属于例外：它们会分别成为 `MCPApprovalRequestItem` 和 `MCPListToolsItem` 项。

原始项目可以是带类型的 Responses 对象，也可以是映射。特别是，程序拥有的 shell 和 apply-patch 调用会使用映射。请使用对映射安全的检查模式：

```python
from collections.abc import Mapping


def raw_field(item, name):
    raw_item = item.raw_item
    if isinstance(raw_item, Mapping):
        return raw_item.get(name)
    return getattr(raw_item, name, None)


raw_type = raw_field(item, "type")
caller = raw_field(item, "caller")
caller_id = (
    caller.get("caller_id")
    if isinstance(caller, Mapping)
    else getattr(caller, "caller_id", None)
)
```

对于程序拥有的子调用，`caller` 的类型为 `program`，而 `caller_id` 用于标识父程序调用。

## 对话的继续与恢复

### 下一轮智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 包含最后运行的智能体。在任务转移后，它通常是下一轮用户输入中最适合复用的智能体。

在流式传输模式下，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随着运行推进而更新，因此你可以在流结束之前观察任务转移。

### 中断与运行状态

如果某个工具需要审批，待处理的审批会通过 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 或 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 提供。其中可能包括直接工具发起的审批、任务转移后访问的工具发起的审批，或嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行发起的审批。

调用 [`to_state()`][agents.result.RunResult.to_state] 可获取可恢复的 [`RunState`][agents.run_state.RunState]，审批或拒绝待处理项目，然后使用 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复运行。

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

对于流式运行，请先完成对 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 的消费，然后检查 `result.interruptions`，并从 `result.to_state()` 恢复。有关完整的审批流程，请参阅[人在回路](human_in_the_loop.md)。

### 服务端管理的延续

[`last_response_id`][agents.result.RunResultBase.last_response_id] 是此次运行中最新模型响应的 ID。如果希望继续串联 OpenAI Responses API，请在下一轮将其作为 `previous_response_id` 传回。

如果你已经通过 `to_input_list()`、`session` 或 `conversation_id` 继续对话，通常不需要 `last_response_id`。如果需要多步骤运行中的每个模型响应，请改为检查 `raw_responses`。

## 智能体工具元数据

当结果来自嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行时，[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] 会提供关于外层工具调用的不可变元数据：

- `tool_name`
- `tool_call_id`
- `tool_arguments`

对于普通的顶层运行，`agent_tool_invocation` 为 `None`。

这在 `custom_output_extractor` 中尤其有用，因为在对嵌套结果进行后处理时，你可能需要外层工具名称、调用 ID 或原始参数。有关相关的 `Agent.as_tool()` 模式，请参阅[工具](tools.md)。

如果还需要该嵌套运行的已解析结构化输入，请读取 `context_wrapper.tool_input`。这是 [`RunState`][agents.run_state.RunState] 为嵌套工具输入进行通用序列化的字段，而 `agent_tool_invocation` 是当前嵌套调用的实时结果访问接口。

## 流式传输生命周期与诊断

[`RunResultStreaming`][agents.result.RunResultStreaming] 继承上述相同的结果接口，同时添加了流式传输专用控制功能：

- [`stream_events()`][agents.result.RunResultStreaming.stream_events]，用于消费语义流事件
- [`current_agent`][agents.result.RunResultStreaming.current_agent]，用于在运行过程中追踪活动智能体
- [`is_complete`][agents.result.RunResultStreaming.is_complete]，用于查看流式运行是否已完全结束
- [`cancel(...)`][agents.result.RunResultStreaming.cancel]，用于立即停止运行或在当前轮结束后停止运行

持续消费 `stream_events()`，直到异步迭代器结束。只有该迭代器结束后，流式运行才算完成；在最后一个可见 token 到达后，`final_output`、`interruptions`、`raw_responses` 等汇总属性以及会话持久化副作用可能仍在处理中。

如果调用 `cancel()`，请继续消费 `stream_events()`，以确保取消和清理操作能够正确完成。

Python 不提供单独的流式 `completed` promise 或 `error` 属性。流式传输的终止性故障会通过 `stream_events()` 抛出，而 `is_complete` 则反映运行是否已到达终止状态。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 包含运行期间收集的原始模型响应。多步骤运行可能会生成多个响应，例如跨任务转移或重复的模型/工具/模型循环。

[`last_response_id`][agents.result.RunResultBase.last_response_id] 只是 `raw_responses` 中最后一个条目的 ID。

### 安全防护措施结果

智能体级安全防护措施通过 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 提供。

工具安全防护措施则通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 单独提供。

这些数组会在整个运行期间持续累积，因此可用于记录决策、存储额外的安全防护措施元数据，或调试运行被阻止的原因。

### 上下文与用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] 提供应用上下文以及由 SDK 管理的运行时元数据，例如审批、用量和嵌套的 `tool_input`。

用量记录在 `context_wrapper.usage` 中。对于流式运行，在处理完流的最后几个数据块之前，用量总计可能会有所滞后。有关完整的包装对象结构和持久化注意事项，请参阅[上下文管理](context.md)。