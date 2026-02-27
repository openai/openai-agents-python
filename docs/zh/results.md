---
search:
  exclude: true
---
# 结果

当你调用 `Runner.run` 方法时，你会得到以下之一：

-   如果你调用 `run` 或 `run_sync`，会得到 [`RunResult`][agents.result.RunResult]
-   如果你调用 `run_streamed`，会得到 [`RunResultStreaming`][agents.result.RunResultStreaming]

这两者都继承自 [`RunResultBase`][agents.result.RunResultBase]，其中包含了大部分有用信息。

## 最终输出

[`final_output`][agents.result.RunResultBase.final_output] 属性包含最后一个运行的智能体的最终输出。它可能是：

-   如果最后一个智能体未定义 `output_type`，则为 `str`
-   如果该智能体定义了输出类型，则为 `last_agent.output_type` 类型的对象。

!!! note

    `final_output` 的类型是 `Any`。由于存在任务转移，我们无法对其进行静态类型标注。如果发生任务转移，这意味着任意 Agent 都可能是最后一个智能体，因此我们无法在静态阶段确定可能的输出类型集合。

## 下一轮的输入

你可以使用 [`result.to_input_list()`][agents.result.RunResultBase.to_input_list] 将结果转换为输入列表，该列表会把你提供的原始输入与智能体运行期间生成的条目拼接在一起。这使得将一次智能体运行的输出传入下一次运行，或在循环中运行并每次追加新的用户输入都更加方便。

## 最后一个智能体

[`last_agent`][agents.result.RunResultBase.last_agent] 属性包含最后一个运行的智能体。根据你的应用场景，这通常对用户下一次输入时很有帮助。例如，如果你有一个前线分流智能体会任务转移给特定语言的智能体，你可以保存最后一个智能体，并在用户下次向智能体发送消息时复用它。

## 新条目

[`new_items`][agents.result.RunResultBase.new_items] 属性包含本次运行期间生成的新条目。这些条目是 [`RunItem`][agents.items.RunItem]。运行条目封装了由 LLM 生成的原始条目。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] 表示来自 LLM 的一条消息。原始条目是生成的消息。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] 表示 LLM 调用了任务转移工具。原始条目是来自 LLM 的工具调用条目。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] 表示发生了任务转移。原始条目是对任务转移工具调用的工具响应。你还可以从该条目访问源/目标智能体。
-   [`ToolCallItem`][agents.items.ToolCallItem] 表示 LLM 调用了某个工具。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] 表示某个工具已被调用。原始条目是工具响应。你还可以从该条目访问工具输出。
-   [`ReasoningItem`][agents.items.ReasoningItem] 表示来自 LLM 的推理条目。原始条目是生成的推理内容。

## 其他信息

### 安全防护措施结果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] 和 [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] 属性包含安全防护措施的结果（如果有）。安全防护措施结果有时会包含你希望记录或存储的有用信息，因此我们将其提供给你。

工具安全防护措施结果会单独提供为 [`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] 和 [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results]。这些安全防护措施可以附加到工具上，这些工具调用会在智能体工作流期间执行相应的安全防护措施。

### 原始响应

[`raw_responses`][agents.result.RunResultBase.raw_responses] 属性包含由 LLM 生成的 [`ModelResponse`][agents.items.ModelResponse]。

### 原始输入

[`input`][agents.result.RunResultBase.input] 属性包含你提供给 `run` 方法的原始输入。大多数情况下你不需要它，但如果需要也可使用。

### 中断与恢复运行

如果一次运行因工具审批而暂停，待处理的审批会暴露在
[`RunResult.interruptions`][agents.result.RunResult.interruptions] 或
[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 中。将
结果通过 `to_state()` 转换为 [`RunState`][agents.run_state.RunState]，批准或拒绝这些
中断，然后通过 `Runner.run(...)` 或 `Runner.run_streamed(...)` 恢复运行。

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
[`RunResultStreaming`][agents.result.RunResultStreaming] 都支持 `to_state()`。关于持久化的
审批工作流，请参阅[人工参与指南](human_in_the_loop.md)。

### 便捷辅助功能

`RunResultBase` 包含一些在生产流程中很有用的辅助方法/属性：

- [`final_output_as(...)`][agents.result.RunResultBase.final_output_as] 将最终输出转换为特定类型（可选运行时类型检查）。
- [`last_response_id`][agents.result.RunResultBase.last_response_id] 返回最新的模型响应 ID，可用于响应链式衔接。
- [`release_agents(...)`][agents.result.RunResultBase.release_agents] 在你检查完结果后，释放对智能体的强引用，以降低内存压力。