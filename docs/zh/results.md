---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，你会得到：

-   [运行结果（RunResult）][agents.result.RunResult]，如果你调用的是 `run` 或 `run_sync`
-   [流式运行结果（RunResultStreaming）][agents.result.RunResultStreaming]，如果你调用的是 `run_streamed`

这两者都继承自[运行结果基类（RunResultBase）][agents.result.RunResultBase]，大多数有用信息都在这里。

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行的智能体的最终输出。可能是：

-   `str`，如果最后一个智能体未定义 `output_type`
-   类型为 `last_agent.output_type` 的对象，如果该智能体定义了输出类型。

!!! note

    `final_output` 的类型为 `Any`。由于存在任务转移，我们无法进行静态类型标注。如果发生任务转移，意味着任意智能体都可能成为最后一个，因此我们无法静态地知道可能的输出类型集合。

## 下一轮输入

你可以使用 [`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 将结果转换为一个输入列表，该列表把你提供的原始输入与智能体运行期间生成的条目连接起来。这样便于将一次智能体运行的输出传递给另一次运行，或者在循环中每次追加新的用户输入。

## 最后一个智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 属性包含最后一个运行的智能体。根据你的应用，这对下一次用户输入时通常很有用。例如，如果你有一个一线分诊智能体会将任务转移到某个特定语言的智能体，你可以存储并在下次用户向智能体发送消息时复用这个最后的智能体。

## 新条目

[`new_items`][agents.result.RunResultBase.new_items] 属性包含本次运行期间生成的新条目。条目是 [运行条目（RunItem）][agents.items.RunItem]。运行条目会封装由 LLM 生成的原始条目。

-   [消息输出条目（MessageOutputItem）][agents.items.MessageOutputItem] 表示来自 LLM 的一条消息。原始条目是生成的消息。
-   [任务转移调用条目（HandoffCallItem）][agents.items.HandoffCallItem] 表示 LLM 调用了任务转移工具。原始条目是来自 LLM 的工具调用条目。
-   [任务转移输出条目（HandoffOutputItem）][agents.items.HandoffOutputItem] 表示发生了任务转移。原始条目是对任务转移工具调用的工具响应。你也可以从该条目访问源/目标智能体。
-   [工具调用条目（ToolCallItem）][agents.items.ToolCallItem] 表示 LLM 调用了某个工具。
-   [工具调用输出条目（ToolCallOutputItem）][agents.items.ToolCallOutputItem] 表示某个工具被调用了。原始条目是工具响应。你也可以从该条目访问工具输出。
-   [推理条目（ReasoningItem）][agents.items.ReasoningItem] 表示来自 LLM 的推理条目。原始条目是生成的推理内容。

## 其他信息

### 安全防护措施结果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 属性包含安全防护措施的结果（如果有）。安全防护措施结果有时包含你可能希望记录或存储的有用信息，因此我们向你提供这些结果。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 属性包含由 LLM 生成的[模型响应（ModelResponse）][agents.items.ModelResponse]。

### 原始输入

[`input`][agents.result.RunResultBase.input] 属性包含你传递给 `run` 方法的原始输入。大多数情况下你不需要它，但在需要时可以使用。