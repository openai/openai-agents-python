---
search:
  exclude: true
---
# 结果

当你调用`Runner.run`方法时，你会得到：

-   如果调用`run`或`run_sync`，则为 [`RunResult`][agents.result.RunResult]
-   如果调用`run_streamed`，则为 [`RunResultStreaming`][agents.result.RunResultStreaming]

这两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，其中包含大部分有用信息。

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后运行的智能体的最终输出。这是以下之一：

-   如果最后一个智能体没有定义`output_type`，则为 `str`
-   如果智能体定义了输出类型，则为 `last_agent.output_type` 类型的对象

!!! note

    `final_output` 的类型是 `Any`。由于交接的存在，我们无法对其进行静态类型化。如果发生交接，意味着任何智能体都可能成为最后一个智能体，因此我们无法静态地知道可能的输出类型集合。

## 下一轮输入

你可以使用 [`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 将结果转换为输入列表，将你提供的原始输入与智能体运行期间生成的项目连接起来。这使得将一个智能体运行的输出传递到另一个运行中，或者在循环中运行并每次添加新的用户输入变得很方便。

## 最后一个智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 属性包含最后运行的智能体。根据你的应用，这在用户下次输入某些内容时通常很有用。例如，如果你有一个前线分类智能体交接给特定语言的智能体，你可以存储最后一个智能体，并在用户下次向智能体发送消息时重用它。

## 新项目

[`new_items`][agents.result.RunResultBase.new_items] 属性包含运行期间生成的新项目。这些项目是 [`RunItem`][agents.items.RunItem]。运行项目包装了LLM生成的原始项目。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] 表示来自LLM的消息。原始项目是生成的消息。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] 表示LLM调用了交接工具。原始项目是LLM的工具调用项目。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] 表示发生了交接。原始项目是对交接工具调用的工具响应。你也可以从项目中访问源/目标智能体。
-   [`ToolCallItem`][agents.items.ToolCallItem] 表示LLM调用了工具。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 表示调用了工具。原始项目是工具响应。你也可以从项目中访问工具输出。
-   [`ReasoningItem`][agents.items.ReasoningItem] 表示来自LLM的推理项目。原始项目是生成的推理。

## 其他信息

### 护栏结果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 属性包含护栏的结果（如果有）。护栏结果有时包含有用的信息，你可能想要记录或存储，因此我们让你可以访问这些信息。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 属性包含LLM生成的 [`ModelResponse`][agents.items.ModelResponse]。

### 原始输入

[`input`][agents.result.RunResultBase.input] 属性包含你提供给`run`方法的原始输入。在大多数情况下你不需要这个，但如果你需要，它可供你使用。