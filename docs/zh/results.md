---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，你会得到以下其中之一：

-   如果你调用 `run` 或 `run_sync`，得到 [`RunResult`][agents.result.RunResult]
-   如果你调用 `run_streamed`，得到 [`RunResultStreaming`][agents.result.RunResultStreaming]

这两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，其中包含了大多数有用信息。

## 结果属性选择

大多数应用只需要少量结果属性或辅助方法：

| 属性或辅助方法 | 在你需要以下内容时使用... |
| --- | --- |
| `final_output` | 展示给用户的最终答案。 |
| `to_input_list()` | 当你手动维护对话历史时，获取完整的下一轮输入列表。 |
| `new_items` | 包含智能体、工具和任务转移元数据的丰富运行项，用于日志、UI 或审计。 |
| `last_agent` | 通常应处理下一轮的智能体。 |
| `last_response_id` | 在下一次 OpenAI Responses 轮次中，使用 `previous_response_id` 进行延续。 |
| `interruptions` | 在恢复运行前必须处理的待定工具审批。 |
| `to_state()` | 用于暂停/恢复或持久化任务工作流的可序列化快照。 |

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行的智能体的最终输出。它可能是：

-   `str`，如果最后一个智能体未定义 `output_type`
-   `last_agent.output_type` 类型的对象，如果该智能体定义了输出类型。

!!! note

    `final_output` 的类型是 `Any`。由于任务转移，我们无法对其进行静态类型标注。如果发生任务转移，任何 Agent 都可能成为最后一个智能体，因此我们在静态上无法确定可能输出类型的集合。

## 下一轮输入

你可以使用 [`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 将结果转换为输入列表：它会把你提供的原始输入与智能体运行期间生成的条目拼接在一起。这样可以方便地将一次智能体运行的输出传给下一次运行，或在循环中运行并每次追加新的用户输入。

在实践中：

-   当你的应用手动维护完整对话记录时，使用 `result.to_input_list()`。
-   当你希望 SDK 为你加载和保存历史时，使用 [`session=...`](sessions/index.md)。
-   如果你使用 OpenAI 服务端管理状态（`conversation_id` 或 `previous_response_id`），通常只需传入新的用户输入并复用已存储的 ID，而不是重新发送 `result.to_input_list()`。

## 最后一个智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 属性包含最后一个运行的智能体。根据你的应用场景，这通常对用户下一次输入很有用。例如，如果你有一个前线分流智能体，并将任务转移给特定语言智能体，你可以保存最后一个智能体，并在用户下次给智能体发消息时复用它。

在流式传输模式下，[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] 会随着运行推进而更新，因此你可以在任务转移发生时实时观察。

## 新条目

[`new_items`][agents.result.RunResultBase.new_items] 属性包含运行期间生成的新条目。这些条目是 [`RunItem`][agents.items.RunItem]。运行条目会封装由 LLM 生成的原始条目。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] 表示来自 LLM 的消息。原始条目是生成的消息。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] 表示 LLM 调用了任务转移工具。原始条目是来自 LLM 的工具调用条目。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] 表示发生了任务转移。原始条目是对任务转移工具调用的工具响应。你也可以从该条目访问源/目标智能体。
-   [`ToolCallItem`][agents.items.ToolCallItem] 表示 LLM 调用了某个工具。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 表示某个工具已被调用。原始条目是工具响应。你也可以从该条目访问工具输出。
-   [`ReasoningItem`][agents.items.ReasoningItem] 表示来自 LLM 的推理条目。原始条目是生成的推理内容。

## 运行状态

当你需要运行的可序列化快照时，调用 [`result.to_state()`][agents.result.RunResult.to_state]。这是已完成或已暂停运行与后续恢复之间的桥梁，尤其适用于审批流程或持久化工作进程系统。

## 其他信息

### 安全防护措施结果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 属性包含安全防护措施的结果（如果有）。安全防护措施结果有时会包含你希望记录或存储的有用信息，因此我们将其提供给你。

工具的安全防护措施结果可分别通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 获取。这些安全防护措施可以附加到工具上，并且这些工具调用会在智能体工作流期间执行相应安全防护措施。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 属性包含由 LLM 生成的 [`ModelResponse`][agents.items.ModelResponse]。

### 原始输入

[`input`][agents.result.RunResultBase.input] 属性包含你提供给 `run` 方法的原始输入。在大多数情况下你不需要它，但在需要时可用。

### 中断与恢复运行

如果运行因工具审批而暂停，待处理审批会显示在
[`RunResult.interruptions`][agents.result.RunResult.interruptions] 或
[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 中。将
结果通过 `to_state()` 转换为 [`RunState`][agents.run_state.RunState]，批准或拒绝
中断项，然后使用 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复运行。

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

[`RunResult`][agents.result.RunResult] 和
[`RunResultStreaming`][agents.result.RunResultStreaming] 都支持 `to_state()`。关于持久化
审批工作流，请参阅[human-in-the-loop 指南](human_in_the_loop.md)。

### 便捷辅助方法

`RunResultBase` 包含一些在生产流程中有用的辅助方法/属性：

- [`final_output_as(...)`][agents.result.RunResultBase.final_output_as] 将最终输出转换为特定类型（可选运行时类型检查）。
- [`last_response_id`][agents.result.RunResultBase.last_response_id] 返回最新模型响应 ID。当你希望在下一轮继续 OpenAI Responses API 链时，将其作为 `previous_response_id` 传回。
- [`release_agents(...)`][agents.result.RunResultBase.release_agents] 在检查结果后，丢弃对智能体的强引用，以减少内存压力。