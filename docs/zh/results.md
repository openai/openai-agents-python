---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，你会得到以下之一：

-   如果你调用 `run` 或 `run_sync`，则得到 [`RunResult`][agents.result.RunResult]
-   如果你调用 `run_streamed`，则得到 [`RunResultStreaming`][agents.result.RunResultStreaming]

这两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，大多数有用信息都在其中。

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行的智能体的最终输出。这可能是：

-   如果最后一个智能体未定义 `output_type`，则为 `str`
-   如果该智能体定义了输出类型，则为 `last_agent.output_type` 类型的对象

!!! note

    `final_output` 的类型是 `Any`。由于存在 handoffs，我们无法对其进行静态类型标注。如果发生 handoffs，这意味着任何 Agent 都可能是最后一个智能体，因此我们无法在静态层面知道可能的输出类型集合。

## 下一轮的输入

你可以使用 [`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 将结果转换为一个输入列表，该列表会把你提供的原始输入与智能体运行期间生成的条目拼接起来。这使得将一次智能体运行的输出传入另一次运行更方便；也可以在循环中运行，并在每次追加新的用户输入。

## 最后一个智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 属性包含最后一个运行的智能体。根据你的应用场景，这通常对下一次用户输入很有用。例如，如果你有一个前线分流智能体会任务转移到某个特定语言的智能体，你可以保存最后一个智能体，并在用户下一次给智能体发消息时复用它。

## 新条目

[`new_items`][agents.result.RunResultBase.new_items] 属性包含本次运行期间生成的新条目。这些条目是 [`RunItem`][agents.items.RunItem]。一个 run item 会封装由 LLM 生成的原始条目。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] 表示来自 LLM 的一条消息。原始条目为生成的消息。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] 表示 LLM 调用了 handoff 工具。原始条目为 LLM 的工具调用条目。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] 表示发生了任务转移。原始条目为对 handoff 工具调用的工具响应。你也可以从该条目中访问源/目标智能体。
-   [`ToolCallItem`][agents.items.ToolCallItem] 表示 LLM 调用了一个工具。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 表示某个工具被调用。原始条目为工具响应。你也可以从该条目中访问工具输出。
-   [`ReasoningItem`][agents.items.ReasoningItem] 表示来自 LLM 的一个推理条目。原始条目为生成的推理内容。

## 其他信息

### 安全防护措施结果

如果存在安全防护措施，其结果包含在 [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 属性中。安全防护措施结果有时会包含你想要记录或存储的有用信息，因此我们将其提供给你。

工具的安全防护措施结果会单独通过 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] 提供。这些安全防护措施可以附加到工具上，这些工具调用会在智能体工作流中执行相应的安全防护措施。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 属性包含由 LLM 生成的 [`ModelResponse`][agents.items.ModelResponse]。

### 原始输入

[`input`][agents.result.RunResultBase.input] 属性包含你传给 `run` 方法的原始输入。大多数情况下你不需要它，但在需要时它可用。

### 中断与恢复运行

如果某次运行因工具审批而暂停，待审批项会在 [`interruptions`][agents.result.RunResultBase.interruptions] 中暴露出来。用 `to_state()` 将结果转换为 [`RunState`][agents.run_state.RunState]，批准或拒绝中断项，然后通过 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复运行。

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

[`RunResult`][agents.result.RunResult] 和 [`RunResultStreaming`][agents.result.RunResultStreaming] 都支持 `to_state()`。

### 便捷辅助功能

`RunResultBase` 包含一些在生产流程中很有用的辅助方法/属性：

- [`final_output_as(...)`][agents.result.RunResultBase.final_output_as] 将最终输出转换为指定类型（可选进行运行时类型检查）。
- [`last_response_id`][agents.result.RunResultBase.last_response_id] 返回最新的模型响应 ID，用于响应串联。
- [`release_agents(...)`][agents.result.RunResultBase.release_agents] 在你检查完结果后，如果希望降低内存压力，可用于丢弃对智能体的强引用。