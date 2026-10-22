---
search:
  exclude: true
---
# 结果

调用 `Runner.run` 方法时，你会收到以下两种结果类型之一：

-   来自 `Runner.run(...)` 或 `Runner.run_sync(...)` 的 [`RunResult`][agents.result.RunResult]
-   来自 `Runner.run_streamed(...)` 的 [`RunResultStreaming`][agents.result.RunResultStreaming]

两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，后者提供共享的结果接口，例如 `final_output`、`new_items`、`last_agent`、`raw_responses` 和 `to_state()`。

`RunResultStreaming` 增加了流式传输专用控制项，例如 [`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete] 和 [`cancel(...)`][agents.result.RunResultStreaming.cancel]。

## 合适的结果接口 {#choose-the-right-result-surface}

大多数应用只需要少量结果属性或辅助方法：

| 如果你需要…… | 使用 |
| --- | --- |
| 向用户显示的最终答案 | `final_output` |
| 包含完整本地记录、可直接用于重放的下一轮输入列表 | `to_input_list()` |
| 包含智能体、工具、任务转移和审批元数据的丰富运行项 | `new_items` |
| 通常应处理下一轮用户输入的智能体 | `last_agent` |
| 使用 `previous_response_id` 进行 OpenAI Responses API 链式调用 | `last_response_id` |
| 待处理审批和可恢复快照 | `interruptions` 和 `to_state()` |
| 当前嵌套 `Agent.as_tool()` 调用的元数据 | `agent_tool_invocation` |
| 原始模型调用或安全防护措施诊断信息 | `raw_responses` 和安全防护措施结果数组 |

## 最终输出 {#final-output}

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行智能体的最终输出。它可能是：

-   如果最后一个智能体未定义 `output_type`，则为 `str`
-   如果最后一个智能体定义了输出类型，则为 `last_agent.output_type` 类型的对象
-   如果运行在生成最终输出之前停止，则为 `None`，例如因等待审批而暂停

!!! note

    `final_output` 的类型为 `Any`。任务转移可能改变完成运行的智能体，因此 SDK 无法静态确定所有可能的输出类型。

在流式传输模式下，`final_output` 会一直保持为 `None`，直到流处理完毕。有关逐事件流程，请参阅[流式传输](streaming.md)。

## 输入、下一轮历史记录和新项目 {#input-next-turn-history-and-new-items}

这些接口分别用于回答不同的问题：

| 属性或辅助方法 | 所含内容 | 最适用于 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 此运行片段的基础输入。如果任务转移输入过滤器重写了历史记录，此属性会反映运行后续使用的已过滤输入。 | 审核此运行实际使用的输入 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 运行的输入项视图。默认的 `mode="preserve_all"` 会保留来自 `new_items` 的已转换历史记录，但不会再次追加已经移入 SDK 默认嵌套任务转移历史记录中的某个完全相同的会话项实例；当任务转移过滤重写模型历史记录时，`mode="normalized"` 会优先使用规范的后续输入。 | 手动聊天循环、客户端管理的对话状态和普通项目历史记录检查 |
| [`new_items`][agents.result.RunResultBase.new_items] | 包含智能体、工具、任务转移和审批元数据的丰富 [`RunItem`][agents.items.RunItem] 包装器。 | 日志、UI、审核和调试 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 运行中每次模型调用产生的原始 [`ModelResponse`][agents.items.ModelResponse] 对象。 | 提供商级诊断或原始响应检查 |

在实践中：

-   如果需要运行的普通输入项视图，请使用 `to_input_list()`。
-   在任务转移过滤或嵌套任务转移历史记录重写后，如果需要用于下一次 `Runner.run(..., input=...)` 调用的规范本地输入，请使用 `to_input_list(mode="normalized")`。
-   如果希望 SDK 为你加载和保存历史记录，请使用 [`session=...`](sessions/index.md)。
-   如果通过 `conversation_id` 或 `previous_response_id` 使用 OpenAI 服务端管理的状态，通常只需传递新的用户输入并复用已存储的 ID，而无需重新发送 `to_input_list()`。
-   如果需要完整的已转换历史记录用于日志、UI 或审核，请使用默认的 `to_input_list()` 模式或 `new_items`。

当 SDK 默认的嵌套任务转移历史记录原样保留某个消息项时，会话、`RunState` 和 `to_input_list()` 会追踪确切的所属实例，而不是按内容去重。分别出现的相同消息仍会保持独立；只有已经归属的实例不会被再次追加。

将模型输出转换为可重放输入时，`to_input_list()`、[`ModelResponse.to_input_items()`][agents.items.ModelResponse.to_input_items] 和每次 [`RunItemBase.to_input_item()`][agents.items.RunItemBase.to_input_item] 调用都会移除仅供提供商输出使用的 `created_by` 元数据。这包括嵌套 `shell_call_output` 分块中的 `created_by`。转换过程会重建受影响的映射，而不会改变原始项目。

与 JavaScript SDK 不同，Python 不提供单独的 `output` 属性来仅包含运行期间新生成的模型格式项目。需要 SDK 元数据时，请使用 `new_items`；需要原始模型载荷时，请检查 `raw_responses`。

将计算机工具项目作为对话输入重新提交时，会使用原始 Responses 载荷结构。预览模型的 `computer_call` 项目会保留单个 `action`，而 `gpt-5.5` 计算机调用可保留批量的 `actions[]`。[`to_input_list()`][agents.result.RunResultBase.to_input_list] 和 [`RunState`][agents.run_state.RunState] 会保留模型生成的结构，因此，无论是手动将这些项目重新提交为对话输入、执行暂停/恢复流程，还是使用已存储的记录，都能同时兼容预览版和正式发布版的计算机工具调用。本地执行结果仍会以 `computer_call_output` 项目的形式出现在 `new_items` 中。

### 新项目 {#new-items}

[`new_items`][agents.result.RunResultBase.new_items] 提供运行期间所发生情况的最丰富视图。常见的项目类型包括：

-   [`InputItem`][agents.items.InputItem]，表示恢复的模型调用即将执行前从 `RunState.pending_input` 接纳的输入
-   [`MessageOutputItem`][agents.items.MessageOutputItem]，表示助手消息
-   [`ReasoningItem`][agents.items.ReasoningItem]，表示推理项目
-   [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 和 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]，表示 Responses 工具搜索请求和已加载的工具搜索结果
-   [`ToolCallItem`][agents.items.ToolCallItem] 和 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]，表示工具调用及其结果
-   [`ToolApprovalItem`][agents.items.ToolApprovalItem]，表示因等待审批而暂停的工具调用
-   [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem]、[`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem] 和 [`MCPListToolsItem`][agents.items.MCPListToolsItem]，表示托管式 MCP 审批和工具目录
-   [`HandoffCallItem`][agents.items.HandoffCallItem] 和 [`HandoffOutputItem`][agents.items.HandoffOutputItem]，表示任务转移请求和已完成的转移

只要需要智能体关联信息、工具输出、任务转移边界或审批边界，就应选择 `new_items`，而不是 `to_input_list()`。

使用托管式工具搜索时，请检查 `ToolSearchCallItem.raw_item` 以查看模型发出的搜索请求，并检查 `ToolSearchOutputItem.raw_item` 以查看该轮加载了哪些命名空间、函数或托管式 MCP 服务器。

使用编程式工具调用时，生成的 `program` 是 `ToolCallItem`，该程序拥有的普通子工具调用也是 `ToolCallItem` 条目，而与之匹配的 `program_output` 是 `ToolCallOutputItem`。程序拥有的托管式 MCP `mcp_approval_request` 和 `mcp_list_tools` 项目是例外：它们会成为 `MCPApprovalRequestItem` 和 `MCPListToolsItem` 条目。

原始项目可以是有类型的 Responses 对象或映射。特别是，程序拥有的 shell 和 apply-patch 调用使用映射。请使用对映射安全的检查模式：

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
    caller.get("caller_id") if isinstance(caller, Mapping) else getattr(caller, "caller_id", None)
)
```

对于程序拥有的子调用，`caller` 的 `type` 字段为 `program`，而 `caller_id` 用于标识父程序调用。

## 对话的继续或恢复 {#continue-or-resume-the-conversation}

### 下一轮智能体 {#next-turn-agent}

[`last_agent`][agents.result.RunResultBase.last_agent] 包含最后运行的智能体。在任务转移后，它通常是下一轮用户输入最适合复用的智能体。

在流式传输模式下，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随着运行进展而更新，因此你可以在流结束之前观察任务转移。

### 中断和运行状态 {#interruptions-and-run-state}

如果某个工具需要审批，待处理审批会通过 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 或 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 提供。其中可能包括直接工具、任务转移后调用的工具，或嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行所触发的审批。

调用 [`to_state()`][agents.result.RunResult.to_state] 以捕获可恢复的 [`RunState`][agents.run_state.RunState]，批准或拒绝待处理项目，然后通过 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复运行。

当 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 的输出是 Pydantic 模型或数据类时，`RunState` 会将该输出序列化为结构化数据。`RunState` 还会遍历字典、列表和元组，并转换在这些容器中遇到的 Pydantic 模型或数据类；经过 JSON 往返转换后，元组会恢复为列表。其他与 JSON 不兼容的值可能会回退为其字符串表示形式，因此，如果需要在序列化后精确保留自定义类型，请返回明确兼容 JSON 的数据。

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

#### 恢复失败的已恢复会话写入 {#recover-a-failed-resumed-session-write}

恢复的运行可以完成已批准的工具工作，但之后仍可能触发另一次模型调用，包括在同一模型响应中进行任务转移，然后在将已完成的工具调用及输出写入由客户端管理的 [`Session`][agents.memory.session.Session] 时失败。请保留同一个 [`RunState`][agents.run_state.RunState]，或将其序列化后恢复，并使用原始会话后端和 `session_id` 重试 `Runner.run(...)` 或 `Runner.run_streamed(...)`。在任何后续模型调用之前，SDK 会将待处理批次与会话历史记录进行核对。如果会话已提交完整批次，但确认响应失败，SDK 会识别完全匹配的历史记录末尾，不会再次追加该批次。如果写入未提交，SDK 会重试追加操作。SDK 不会再次执行已完成的工具、工具安全防护措施、钩子或任务转移。恢复的运行会使用已完成任务转移所选定的智能体继续执行。

当会话历史记录并非完全匹配时，恢复会以安全关闭方式失败。请使用原始会话后端和 `session_id`，并确保恢复的运行独占访问该历史记录。如果另一写入方更改了历史记录末尾、待处理批次仅存在一部分，或历史记录存在其他歧义，SDK 会在另一次模型调用之前引发 [`UserError`][agents.exceptions.UserError]。请先修复原始会话历史记录再恢复；不要重新运行已完成的工作。待处理批次、所选智能体和累积的工具安全防护措施结果可以在 `RunState` 的 JSON 和字符串往返转换后保留，包括在恢复完成前再次发生审批中断的情况。[`stream_events()`][agents.result.RunResultStreaming.stream_events] 引发会话写入错误后，[`RunResultStreaming.to_state()`][agents.result.RunResultStreaming.to_state] 也会返回一个分离状态，其中保留相同的恢复数据。

当运行已接受最终输出、完成输出安全防护措施和终止钩子，随后在持久化该最终轮次时失败，此恢复机制不适用。重放该状态可能会重复触发终止生命周期效果，因此 SDK 会将 `RunState` 标记为不可恢复。此后每次使用该状态尝试 `Runner.run(...)` 或 `Runner.run_streamed(...)`，都会在会话核对、沙盒准备、模型调用、工具、安全防护措施或钩子执行之前引发 [`UserError`][agents.exceptions.UserError]。此标记会在 `RunState` 序列化后保留。请启动新的运行，而不是重试该状态。此边界也适用于终止函数工具输出，以及已被接受并写入历史记录的 `max_turns` 处理程序输出。

#### 恢复前的输入添加 {#add-input-before-resuming}

当运行暂停，或在完成某一轮后停止，但尚未到达未完成运行的下一次模型调用时，如果有新的用户输入到达，请使用 [`RunState.add_input()`][agents.run_state.RunState.add_input]。字符串会转换为用户消息，多次调用会保留插入顺序。暂存输入是序列化 `RunState` 的一部分，因此可以在 `to_json()` / `from_json()` 和 `to_string()` / `from_string()` 往返转换后保留。

```python
state = result.to_state()
state.add_input("Also keep the generated report in the project folder.")

for interruption in state.get_interruptions():
    state.approve(interruption)

result = await Runner.run(agent, state)
```

恢复时，运行器只会将当前智能体的输入安全防护措施和 [`RunConfig`][agents.run.RunConfig] 中的输入安全防护措施应用于暂存输入。配置由客户端管理的 [`Session`][agents.memory.session.Session] 后，运行器会将已接纳的暂存输入转换为持久的 [`InputItem`][agents.items.InputItem]，并等待会话写入完成，然后才发出模型请求。如果没有由客户端管理的会话或服务端管理的对话，运行器会在发出模型请求之前将已接纳的暂存输入转换为 `InputItem`。对于服务端管理的对话，输入会保持待处理状态，直到服务器请求接受该输入。在序列化、恢复和可安全重放的重试过程中，SDK 会保留一个持久的 `InputItem` 实例。此 SDK 实例保证并不等同于提供商交付保证：如果某个请求可能已到达提供商后，重试策略返回 `RetryDecision(approve_unsafe_replay=True)`，运行器可能会重新发送暂存输入，并可能重复执行提供商侧的工作。成功接纳的输入会在 `new_items` 中显示为 `InputItem`。读取 [`RunState.pending_input`][agents.run_state.RunState.pending_input] 可获得分离副本，也可以调用 [`RunState.clear_pending_input()`][agents.run_state.RunState.clear_pending_input]，在恢复前丢弃所有暂存输入。

`RunState.add_input()` 会拒绝以下状态：终止状态、没有剩余模型轮次的状态、已接受的模型响应正等待本地处理的状态，以及待处理工具结果可能在另一次模型调用前结束运行的中断状态。在这些情况下，请完成当前运行并开始新的用户轮次。

对于流式运行，请先完成对 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 的消费，然后检查 `result.interruptions` 并从 `result.to_state()` 恢复。有关完整审批流程，请参阅[人工介入](human_in_the_loop.md)。

### 服务端管理的后续调用 {#server-managed-continuation}

[`last_response_id`][agents.result.RunResultBase.last_response_id] 是运行中的最新模型响应 ID。如果要继续 OpenAI Responses API 调用链，请在下一轮将其作为 `previous_response_id` 传回。

如果已经通过 `to_input_list()`、`session` 或 `conversation_id` 继续对话，通常不需要 `last_response_id`。如果需要多步骤运行中的每个模型响应，请改为检查 `raw_responses`。

## 智能体作为工具的元数据 {#agent-as-tool-metadata}

当结果来自嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行时，[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] 会提供外层 `Agent.as_tool()` 调用的不可变元数据：

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

对于普通的顶层运行，`agent_tool_invocation` 为 `None`。

这在 `custom_output_extractor` 中尤其有用，因为在对嵌套结果进行后处理时，你可能需要外层 `Agent.as_tool()` 调用的工具名称、调用 ID 或原始参数。有关相关的 `Agent.as_tool()` 模式，请参阅[工具](tools.md)。

如果还需要该嵌套运行已解析的结构化输入，请读取 `context_wrapper.tool_input`。这是 [`RunState`][agents.run_state.RunState] 为嵌套工具输入进行通用序列化的字段，而 `agent_tool_invocation` 会直接在结果上提供当前嵌套调用的元数据。

## 流式传输生命周期和诊断 {#streaming-lifecycle-and-diagnostics}

[`RunResultStreaming`][agents.result.RunResultStreaming] 继承上述相同的结果接口，同时增加流式传输专用控制项：

-   [`stream_events()`][agents.result.RunResultStreaming.stream_events]，用于消费语义流事件
-   [`current_agent`][agents.result.RunResultStreaming.current_agent]，用于在运行过程中追踪活跃智能体
-   [`is_complete`][agents.result.RunResultStreaming.is_complete]，用于查看流式运行是否已完全结束
-   [`cancel(...)`][agents.result.RunResultStreaming.cancel]，用于立即停止运行或在当前轮次结束后停止运行

持续消费 `stream_events()`，直到异步迭代器结束。该迭代器结束前，流式运行并未完成；而且在最后一个可见 token 到达后，`final_output`、`interruptions`、`raw_responses` 等汇总属性以及会话持久化副作用可能仍在最终确定中。

如果调用 `cancel()`，请继续消费 `stream_events()`，以便正确完成取消和清理。

Python 不提供单独的流式 `completed` promise 或 `error` 属性。导致运行终止的流式传输故障会由 `stream_events()` 引发，而 `is_complete` 会反映运行是否已到达终止状态。

### 原始响应 {#raw-responses}

[`raw_responses`][agents.result.RunResultBase.raw_responses] 包含运行期间收集的原始模型响应。多步骤运行可能产生多个响应，例如跨任务转移或重复的模型/工具/模型循环。

[`last_response_id`][agents.result.RunResultBase.last_response_id] 只是 `raw_responses` 中最后一个条目的 ID。

每个 [`ModelResponse`][agents.items.ModelResponse] 还提供两项适用于该次模型调用的诊断信息：

-   [`request_id`][agents.items.ModelResponse.request_id] 是模型适配器和传输层传播请求 ID 时的传输请求 ID。内置的 `OpenAIResponsesModel` 和 `OpenAIChatCompletionsModel` 会在其 HTTP 和 SSE 传输路径上传播可用的、由服务器生成的 `x-request-id`。当配置的端点是 OpenAI API 时，请在生产环境中记录非 `None` 值，以便将故障与 OpenAI 支持相关联；对于兼容 OpenAI 的提供商或代理，请改用相应服务的支持渠道。`OpenAIResponsesWSModel` 目前会将 `request_id` 保持为 `None`。第三方适配器不保证传播请求 ID。AnyLLM Chat Completions 适配器和 `LitellmModel` 目前会将 `request_id` 保持为 `None`。当 Agents SDK AnyLLM Responses 适配器规范化提供商响应但未保留传输请求 ID 时，也可能会将 `request_id` 保持为 `None`。
-   [`raw_usage`][agents.items.ModelResponse.raw_usage] 是在 Agents SDK 规范化载荷之前，对提供商用量载荷创建的可选 JSON 兼容快照。使用 `ModelSettings(preserve_raw_usage=True)` 启用 `raw_usage`；请参阅[保留提供商用量载荷](usage.md#preserving-provider-usage-payloads)。

`ModelResponse.request_id` 和 `ModelResponse.raw_usage` 都可能为 `None`，因此应将这些值作为可选诊断信息处理，而不是对话状态。

### 安全防护措施结果 {#guardrail-results}

智能体级安全防护措施通过 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 提供。

工具安全防护措施则通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 单独提供。

这些数组会在运行过程中持续累积，因此适用于记录决策、存储额外的安全防护措施元数据，或调试运行被阻止的原因。

当智能体级输出安全防护措施阻止由终止函数工具直接生成的最终输出时，会应用一项编校规则。对于被阻止的当前响应，`output_guardrail_results` 会替换被拒绝的智能体输出，并清除包含载荷的输出元数据；`tool_output_guardrail_results` 则会替换包含载荷的工具元数据。之前已接受的结果保持不变。经过清理的输出安全防护措施结果会作为 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 上的 `guardrail_result` 提供。经过清理的输出安全防护措施和工具输出安全防护措施结果也会通过流式结果状态和 `RunState` 提供；请参阅[输出安全防护措施](guardrails.md#output-guardrails)。

### 上下文和用量 {#context-and-usage}

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] 会提供应用上下文，以及由 SDK 管理的运行时元数据，例如审批、用量和嵌套 `tool_input`。

用量通过 `context_wrapper.usage` 追踪。对于流式运行，在处理完流的最终分块之前，用量总计可能会有所延迟。有关完整的包装器结构和持久化注意事项，请参阅[上下文管理](context.md)。