---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，你会得到：

- [`RunResult`][agents.result.RunResult]，如果你调用 `run` 或 `run_sync`
- [`RunResultStreaming`][agents.result.RunResultStreaming]，如果你调用 `run_streamed`

二者都继承自 [`RunResultBase`][agents.result.RunResultBase]，大多数有用信息都在这里。

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行的智能体的最终输出。这可能是：

- `str`，如果最后一个智能体没有定义 `output_type`
- 类型为 `last_agent.output_type` 的对象，如果智能体定义了输出类型。

!!! note

    `final_output` 的类型是 `Any`。由于任务转移，我们无法进行静态类型标注。如果发生任务转移，意味着任意智能体都有可能成为最后一个运行的智能体，因此我们在静态上无法知道可能的输出类型集合。

## 下一轮的输入

你可以使用 [`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 将结果转换为一个输入列表，它会把你提供的原始输入与智能体运行期间生成的项连接起来。这样可以方便地将一次智能体运行的输出传入另一轮运行，或在循环中运行并在每次追加新的用户输入。

## 最后一个智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 属性包含最后一个运行的智能体。根据你的应用场景，这对下一次用户输入时通常很有用。比如，如果你有一个前线分诊智能体会将任务转移给特定语言的智能体，你可以存储最后一个智能体，并在用户下次与智能体对话时复用它。

## 新项

[`new_items`][agents.result.RunResultBase.new_items] 属性包含运行期间生成的新项。每个项都是 [`RunItem`][agents.items.RunItem]。运行项封装了由 LLM 生成的原始项。

- [`MessageOutputItem`][agents.items.MessageOutputItem] 表示来自 LLM 的消息。原始项是生成的消息。
- [`HandoffCallItem`][agents.items.HandoffCallItem] 表示 LLM 调用了任务转移工具。原始项是来自 LLM 的工具调用项。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] 表示发生了任务转移。原始项是对任务转移工具调用的工具响应。你也可以从该项访问源/目标智能体。
- [`ToolCallItem`][agents.items.ToolCallItem] 表示 LLM 调用了某个工具。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 表示某个工具被调用。原始项是工具响应。你也可以从该项访问工具输出。
- [`ReasoningItem`][agents.items.ReasoningItem] 表示来自 LLM 的推理项。原始项是生成的推理内容。

## 其他信息

### 安全防护措施结果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 属性包含安全防护措施（如有）的结果。安全防护措施结果有时包含你可能希望记录或存储的有用信息，因此我们提供了访问方式。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 属性包含由 LLM 生成的 [`ModelResponse`][agents.items.ModelResponse]。

### 原始输入

[`input`][agents.result.RunResultBase.input] 属性包含你提供给 `run` 方法的原始输入。多数情况下你不需要它，但在需要时可用。