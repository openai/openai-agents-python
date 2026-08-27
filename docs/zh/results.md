---
search:
  exclude: true
---
# 结果

调用 `Runner.run` 方法时，你会收到以下两种结果类型之一：

-   来自 `Runner.run(...)` 或 `Runner.run_sync(...)` 的 [`RunResult`][agents.result.RunResult]
-   来自 `Runner.run_streamed(...)` 的 [`RunResultStreaming`][agents.result.RunResultStreaming]

两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，后者公开了共享的结果接口，例如 `final_output`、`new_items`、`last_agent`、`raw_responses` 和 `to_state()`。

`RunResultStreaming` 增加了流式传输专用的控制项，例如 [`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete] 和 [`cancel(...)`][agents.result.RunResultStreaming.cancel]。

## 合适的结果接口 {#choose-the-right-result-surface}

大多数应用只需要少数几个结果属性或辅助方法：

| 如果你需要…… | 使用 |
| --- | --- |
| 向用户显示的最终答案 | `final_output` |
| 包含完整本地对话记录、可供重放的下一轮输入列表 | `to_input_list()` |
| 包含智能体、工具、任务转移和审批元数据的丰富运行项 | `new_items` |
| 通常应处理下一轮用户输入的智能体 | `last_agent` |
| 使用 `previous_response_id` 的 OpenAI Responses API 链式调用 | `last_response_id` |
| 待处理的审批和可恢复的快照 | `interruptions` 和 `to_state()` |
| 当前嵌套 `Agent.as_tool()` 调用的元数据 | `agent_tool_invocation` |
| 原始模型调用或安全防护措施诊断信息 | `raw_responses` 和安全防护措施结果数组 |

## 最终输出 {#final-output}

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后运行的智能体所生成的最终输出。它可能是：

-   如果最后一个智能体未定义 `output_type`，则为 `str`
-   如果最后一个智能体定义了输出类型，则为 `last_agent.output_type` 类型的对象
-   如果运行在生成最终输出之前停止，则为 `None`，例如因审批中断而暂停

!!! note

    `final_output` 的类型标注为 `Any`。任务转移可能会改变完成运行的智能体，因此 SDK 无法静态确定所有可能的输出类型。

在流式传输模式下，`final_output` 会一直保持为 `None`，直到流处理完成。有关逐事件流程，请参阅[流式传输](streaming.md)。

## 输入、下一轮历史记录和新项目 {#input-next-turn-history-and-new-items}

这些接口分别回答不同的问题：

| 属性或辅助方法 | 包含的内容 | 最适合 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | 此运行片段的基础输入。如果任务转移输入过滤器重写了历史记录，这里会反映运行继续使用的已过滤输入。 | 审核此运行实际使用的输入 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 运行的输入项视图。默认的 `mode="preserve_all"` 会保留来自 `new_items` 的转换后历史记录，但不会再次追加已移入 SDK 默认嵌套任务转移历史记录中的同一会话项；当任务转移过滤重写模型历史记录时，`mode="normalized"` 会优先采用规范的延续输入。 | 手动聊天循环、由客户端管理的对话状态，以及普通项目形式的历史记录检查 |
| [`new_items`][agents.result.RunResultBase.new_items] | 包含智能体、工具、任务转移和审批元数据的丰富 [`RunItem`][agents.items.RunItem] 包装器。 | 日志、UI、审核和调试 |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 运行中每次模型调用产生的原始 [`ModelResponse`][agents.items.ModelResponse] 对象。 | 提供商级别的诊断或原始响应检查 |

实际使用时：

-   如果需要运行的普通输入项视图，请使用 `to_input_list()`。
-   如果在任务转移过滤或嵌套任务转移历史记录重写后，需要用于下一次 `Runner.run(..., input=...)` 调用的规范本地输入，请使用 `to_input_list(mode="normalized")`。
-   如果希望 SDK 为你加载和保存历史记录，请使用 [`session=...`](sessions/index.md)。
-   如果正在使用通过 `conversation_id` 或 `previous_response_id` 实现的 OpenAI服务器托管状态，通常只需传递新的用户输入并复用已存储的 ID，而不是重新发送 `to_input_list()`。
-   如果日志、UI 或审核需要完整的转换后历史记录，请使用默认的 `to_input_list()` 模式或 `new_items`。

当 SDK 默认的嵌套任务转移历史记录逐字保留某个消息项时，Sessions、`RunState` 和 `to_input_list()` 会追踪准确的自有项实例，而不是按内容去重。分别出现的相同消息仍会保持分离；只会避免再次追加已经归属其中的项实例。

与 JavaScript SDK 不同，Python 不会公开单独的 `output` 属性来仅包含运行期间新生成的模型格式项目。需要 SDK 元数据时，请使用 `new_items`；需要原始模型载荷时，请检查 `raw_responses`。

将计算机工具项目作为对话输入重新提交时，会使用原始 Responses 载荷结构。预览模型的 `computer_call` 项目会保留单个 `action`，而 `gpt-5.5` 计算机调用可以保留批量的 `actions[]`。[`to_input_list()`][agents.result.RunResultBase.to_input_list] 和 [`RunState`][agents.run_state.RunState] 会保留模型生成的结构，因此，在将这些项目手动重新提交为对话输入时，暂停/恢复流程和已存储的对话记录都能继续兼容预览版和 GA 版计算机工具调用。本地执行结果仍会在 `new_items` 中显示为 `computer_call_output` 项目。

### 新项目 {#new-items}

[`new_items`][agents.result.RunResultBase.new_items] 提供运行过程中所发生事件的最丰富视图。常见项目类型包括：

-   [`InputItem`][agents.items.InputItem]，表示在恢复后的模型调用之前立即从 `RunState.pending_input` 接纳的输入
-   [`MessageOutputItem`][agents.items.MessageOutputItem]，表示助手消息
-   [`ReasoningItem`][agents.items.ReasoningItem]，表示推理项目
-   [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] 和 [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]，表示 Responses 工具搜索请求和已加载的工具搜索结果
-   [`ToolCallItem`][agents.items.ToolCallItem] 和 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]，表示工具调用及其结果
-   [`ToolApprovalItem`][agents.items.ToolApprovalItem]，表示因等待审批而暂停的工具调用
-   [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem]、[`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem] 和 [`MCPListToolsItem`][agents.items.MCPListToolsItem]，表示托管 MCP 的审批和工具目录
-   [`HandoffCallItem`][agents.items.HandoffCallItem] 和 [`HandoffOutputItem`][agents.items.HandoffOutputItem]，表示任务转移请求和已完成的转移

只要需要智能体关联信息、工具输出、任务转移边界或审批边界，就应选择 `new_items`，而不是 `to_input_list()`。

使用托管工具搜索时，请检查 `ToolSearchCallItem.raw_item` 以查看模型发出的搜索请求，并检查 `ToolSearchOutputItem.raw_item` 以查看该轮加载了哪些命名空间、函数或托管 MCP 服务器。

使用程序化工具调用时，生成的 `program` 是一个 `ToolCallItem`，该程序拥有的普通子工具调用也是 `ToolCallItem` 条目，而对应的 `program_output` 是一个 `ToolCallOutputItem`。程序拥有的托管 MCP `mcp_approval_request` 和 `mcp_list_tools` 项目属于例外：它们会成为 `MCPApprovalRequestItem` 和 `MCPListToolsItem` 条目。

原始项目可以是有类型的 Responses 对象或映射。特别是，程序拥有的 shell 和 apply-patch 调用使用映射。请使用映射安全的检查模式：

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

对于程序拥有的子调用，`caller` 的 `type` 字段为 `program`，而 `caller_id` 用于标识父程序调用。

## 对话的继续或恢复 {#continue-or-resume-the-conversation}

### 下一轮智能体 {#next-turn-agent}

[`last_agent`][agents.result.RunResultBase.last_agent] 包含最后运行的智能体。任务转移后，它通常是下一轮用户输入最适合复用的智能体。

在流式传输模式下，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随着运行进展而更新，因此你可以在流结束前观察任务转移。

### 中断和运行状态 {#interruptions-and-run-state}

如果某个工具需要审批，待处理的审批会公开在 [`RunResult.interruptions`][agents.result.RunResult.interruptions] 或 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 中。其中可能包括直接工具、任务转移后调用的工具，或嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行所触发的审批。

调用 [`to_state()`][agents.result.RunResult.to_state] 以捕获可恢复的 [`RunState`][agents.run_state.RunState]，批准或拒绝待处理项目，然后使用 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复运行。

当 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 的输出是 Pydantic 模型或数据类时，`RunState` 会将该输出序列化为结构化数据。`RunState` 还会遍历字典、列表和元组，并转换在这些容器中遇到的 Pydantic 模型或数据类；经过 JSON 往返转换后，元组会还原为列表。其他与 JSON 不兼容的值可能会回退为其字符串表示形式，因此，如果某个自定义类型必须在序列化后保持精确，请返回明确与 JSON 兼容的数据。

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

#### 恢复前添加输入 {#add-input-before-resuming}

如果运行在暂停后，或在完成一轮后停止，但尚未执行未完成运行中的下一次模型调用时有新的用户输入到达，请使用 [`RunState.add_input()`][agents.run_state.RunState.add_input]。字符串会成为一条用户消息，多次调用会保留插入顺序。暂存输入是已序列化 `RunState` 的一部分，因此在 `to_json()` / `from_json()` 和 `to_string()` / `from_string()` 往返转换后仍会保留。

```python
state = result.to_state()
state.add_input("Also keep the generated report in the project folder.")

for interruption in state.get_interruptions():
    state.approve(interruption)

result = await Runner.run(agent, state)
```

恢复时，运行器仅对暂存输入应用当前智能体的输入安全防护措施，以及 [`RunConfig`][agents.run.RunConfig] 中的输入安全防护措施。配置由客户端管理的 [`Session`][agents.memory.session.Session] 后，运行器会将已接受的暂存输入转换为持久化的 [`InputItem`][agents.items.InputItem]，等待会话写入完成，然后才发出模型请求。如果没有由客户端管理的会话或服务器托管的对话，运行器会在发出模型请求前，将已接受的暂存输入转换为 `InputItem`。对于服务器托管的对话，输入会保持待处理状态，直到服务器请求接受它。在序列化、恢复和可安全重放的重试过程中，SDK 会保留一个持久化的 `InputItem` 实例。此 SDK 实例保证并不代表提供商交付保证：如果请求可能已到达提供商后，重试策略返回 `RetryDecision(approve_unsafe_replay=True)`，运行器可能会重新发送暂存输入，提供商侧的工作也可能重复执行。成功接纳的输入会在 `new_items` 中显示为 `InputItem`。读取 [`RunState.pending_input`][agents.run_state.RunState.pending_input] 可获取一个分离副本，或调用 [`RunState.clear_pending_input()`][agents.run_state.RunState.clear_pending_input] 在恢复前丢弃所有暂存输入。

`RunState.add_input()` 会拒绝以下状态：终止状态、没有剩余模型轮次的状态、已接受的模型响应正在等待本地处理的状态，以及待处理工具结果可能在下一次模型调用前结束运行的中断状态。在这些情况下，应完成当前运行，然后开始新的用户轮次。

对于流式传输运行，请先完成对 [`stream_events()`][agents.result.RunResultStreaming.stream_events] 的消费，然后检查 `result.interruptions`，并从 `result.to_state()` 恢复。有关完整审批流程，请参阅[人在回路](human_in_the_loop.md)。

### 服务器托管的延续 {#server-managed-continuation}

[`last_response_id`][agents.result.RunResultBase.last_response_id] 是运行中最新的模型响应 ID。如果希望在下一轮继续 OpenAI Responses API 链，请将其作为 `previous_response_id` 传回。

如果已通过 `to_input_list()`、`session` 或 `conversation_id` 继续对话，通常不需要 `last_response_id`。如果需要多步骤运行中的每个模型响应，请改为检查 `raw_responses`。

## 智能体作为工具的元数据 {#agent-as-tool-metadata}

当结果来自嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行时，[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] 会公开有关外层 `Agent.as_tool()` 调用的不可变元数据：

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

对于普通的顶层运行，`agent_tool_invocation` 为 `None`。

这在 `custom_output_extractor` 中尤其有用，因为在对嵌套结果进行后处理时，你可能需要外层 `Agent.as_tool()` 调用的工具名称、调用 ID 或原始参数。有关相关的 `Agent.as_tool()` 模式，请参阅[工具](tools.md)。

如果还需要该嵌套运行的已解析结构化输入，请读取 `context_wrapper.tool_input`。这是 [`RunState`][agents.run_state.RunState] 为嵌套工具输入进行通用序列化的字段，而 `agent_tool_invocation` 会直接在结果中公开当前嵌套调用的元数据。

## 流式传输生命周期和诊断 {#streaming-lifecycle-and-diagnostics}

[`RunResultStreaming`][agents.result.RunResultStreaming] 继承了上述相同的结果接口，但增加了流式传输专用的控制项：

-   [`stream_events()`][agents.result.RunResultStreaming.stream_events]，用于消费语义流事件
-   [`current_agent`][agents.result.RunResultStreaming.current_agent]，用于在运行过程中追踪活动智能体
-   [`is_complete`][agents.result.RunResultStreaming.is_complete]，用于查看流式传输运行是否已完全结束
-   [`cancel(...)`][agents.result.RunResultStreaming.cancel]，用于立即停止运行或在当前轮次结束后停止运行

持续消费 `stream_events()`，直到异步迭代器结束。只有该迭代器结束后，流式传输运行才算完成；在最后一个可见 token 到达后，`final_output`、`interruptions`、`raw_responses` 等汇总属性以及会话持久化副作用可能仍在收尾。

如果调用 `cancel()`，请继续消费 `stream_events()`，以便正确完成取消和清理。

Python 不会公开单独的流式 `completed` promise 或 `error` 属性。导致运行终止的流式传输失败会由 `stream_events()` 抛出，而 `is_complete` 会反映运行是否已达到终止状态。

### 原始响应 {#raw-responses}

[`raw_responses`][agents.result.RunResultBase.raw_responses] 包含运行期间收集的原始模型响应。多步骤运行可能会生成多个响应，例如在任务转移期间或重复的模型/工具/模型循环中。

[`last_response_id`][agents.result.RunResultBase.last_response_id] 只是 `raw_responses` 中最后一个条目的 ID。

每个 [`ModelResponse`][agents.items.ModelResponse] 还会公开两项适用于单次模型调用的诊断信息：

-   [`request_id`][agents.items.ModelResponse.request_id] 是模型适配器和传输层传播请求 ID 时的传输请求 ID。内置的 `OpenAIResponsesModel` 和 `OpenAIChatCompletionsModel` 会在其 HTTP 和 SSE 传输路径中传播可用的、由服务器生成的 `x-request-id`。当配置的端点为 OpenAI API 时，请在生产环境中记录非 `None` 值，以便将故障与 OpenAI支持关联起来；对于与 OpenAI兼容的提供商或代理，请改用相应服务的支持渠道。`OpenAIResponsesWSModel` 当前会将 `request_id` 保持为 `None`。第三方适配器不保证会传播请求 ID。AnyLLM Chat Completions 适配器和 `LitellmModel` 当前会将 `request_id` 保持为 `None`。当 Agents SDK AnyLLM Responses 适配器在规范化提供商响应时未保留传输请求 ID，它也可能会将 `request_id` 保持为 `None`。
-   [`raw_usage`][agents.items.ModelResponse.raw_usage] 是可选启用的、与 JSON 兼容的提供商用量载荷快照，捕获时机是在 Agents SDK 规范化该载荷之前。使用 `ModelSettings(preserve_raw_usage=True)` 启用 `raw_usage`；请参阅[保留提供商用量载荷](usage.md#preserving-provider-usage-payloads)。

`ModelResponse.request_id` 和 `ModelResponse.raw_usage` 都可能是 `None`，因此应将这些值视为可选诊断信息，而不是对话状态。

### 安全防护措施结果 {#guardrail-results}

智能体级安全防护措施分别通过 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 公开。

工具安全防护措施则分别通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 公开。

这些数组会在整个运行期间持续累积，因此可用于记录决策、存储额外的安全防护措施元数据，或调试运行被阻止的原因。

当智能体级输出安全防护措施阻止由终止函数工具直接生成的最终输出时，会应用一条脱敏规则。对于当前被阻止的响应，`output_guardrail_results` 会替换被拒绝的智能体输出，并清除包含载荷的输出元数据，而 `tool_output_guardrail_results` 会替换包含载荷的工具元数据。此前已接受的结果保持不变。经过净化的输出安全防护措施结果会在 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 上公开为 `guardrail_result`。经过净化的输出安全防护措施和工具输出安全防护措施结果也会通过流式传输结果状态和 `RunState` 公开；请参阅[输出安全防护措施](guardrails.md#output-guardrails)。

### 上下文和用量 {#context-and-usage}

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] 会公开你的应用上下文，以及由 SDK 管理的运行时元数据，例如审批、用量和嵌套的 `tool_input`。

用量会在 `context_wrapper.usage` 上追踪。对于流式传输运行，用量总计可能会滞后，直到处理完流的最后几个数据块。有关完整的包装器结构和持久化注意事项，请参阅[上下文管理](context.md)。