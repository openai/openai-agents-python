---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，你会得到：

- [`RunResult`][agents.result.RunResult]，如果你调用的是 `run` 或 `run_sync`
- [`RunResultStreaming`][agents.result.RunResultStreaming]，如果你调用的是 `run_streamed`

二者都继承自 [`RunResultBase`][agents.result.RunResultBase]，大多数有用信息都在这里。

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行的智能体的最终输出。它可能是：

- 一个 `str`，如果最后一个智能体未定义 `output_type`
- 一个类型为 `last_agent.output_type` 的对象，如果该智能体定义了输出类型。

!!! note

    `final_output` 的类型是 `Any`。由于存在任务转移，我们无法进行静态类型标注。如果发生任务转移，意味着任意智能体都可能成为最后一个智能体，因此我们在静态上无法知道可能的输出类型集合。

## 下一轮输入

你可以使用 [`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 将结果转换为一个输入列表，它会把你提供的原始输入与智能体运行期间生成的条目进行拼接。这样便于将一次智能体运行的输出传递到另一次运行中，或在循环中运行并每次追加新的用户输入。

## 最后的智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 属性包含最后一个运行的智能体。根据你的应用场景，这对下一次用户输入通常很有用。例如，如果你有一个前线分诊智能体会进行任务转移到特定语言的智能体，你可以存储最后的智能体，并在用户下次向智能体发送消息时复用它。

## 新增条目

[`new_items`][agents.result.RunResultBase.new_items] 属性包含此次运行期间生成的新条目。每个条目都是一个 [`RunItem`][agents.items.RunItem]。运行条目会封装由 LLM 生成的原始条目。

- [`MessageOutputItem`][agents.items.MessageOutputItem] 表示来自 LLM 的消息。原始条目是生成的消息。
- [`HandoffCallItem`][agents.items.HandoffCallItem] 表示 LLM 调用了任务转移工具。原始条目是来自 LLM 的工具调用条目。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] 表示发生了一次任务转移。原始条目是对任务转移工具调用的工具响应。你还可以从条目中访问源/目标智能体。
- [`ToolCallItem`][agents.items.ToolCallItem] 表示 LLM 调用了一个工具。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 表示一个工具被调用。原始条目是工具响应。你也可以从条目中访问工具输出。
- [`ReasoningItem`][agents.items.ReasoningItem] 表示来自 LLM 的推理条目。原始条目是生成的推理。

## 其他信息

### 安全防护措施结果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 属性包含安全防护措施的结果（如果有）。这些结果有时包含你可能想要记录或存储的有用信息，因此我们将其提供给你。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 属性包含由 LLM 生成的 [`ModelResponse`][agents.items.ModelResponse]。

### 原始输入

[`input`][agents.result.RunResultBase.input] 属性包含你提供给 `run` 方法的原始输入。大多数情况下你可能不需要它，但在需要时它是可用的。