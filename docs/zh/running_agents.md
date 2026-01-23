---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类运行智能体。你有 3 个选项：

1. [`Runner.run()`][agents.run.Runner.run]：以异步方式运行，并返回 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，底层只是运行 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：以异步方式运行，并返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式传输模式调用 LLM，并在事件到达时将其流式传输给你。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="You are a helpful assistant")

    result = await Runner.run(agent, "Write a haiku about recursion in programming.")
    print(result.final_output)
    # Code within the code,
    # Functions calling themselves,
    # Infinite loop's dance
```

在[结果指南](results.md)中了解更多。

## 智能体循环

当你在 `Runner` 中使用 run 方法时，需要传入一个起始智能体和输入。输入可以是字符串（会被视为一条用户消息），也可以是输入项列表（即 OpenAI Responses API 中的 items）。

然后 Runner 会运行一个循环：

1. 使用当前输入为当前智能体调用 LLM。
2. LLM 生成输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 执行任务转移，我们会更新当前智能体与输入，并重新运行循环。
    3. 如果 LLM 产生工具调用，我们会运行这些工具调用，追加结果，并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判定 LLM 输出是否为“最终输出”的规则是：它生成了具有期望类型的文本输出，并且没有任何工具调用。

## 流式传输

流式传输允许你在 LLM 运行时额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含本次运行的完整信息，包括产生的所有新输出。你可以调用 `.stream_events()` 获取流式事件。在[流式传输指南](streaming.md)中了解更多。

## 运行配置

`run_config` 参数让你为智能体运行配置一些全局设置：

-   [`model`][agents.run.RunConfig.model]：允许设置一个全局使用的 LLM 模型，不受每个 Agent 自身 `model` 的影响。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认为 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体级别的设置。例如，你可以设置全局 `temperature` 或 `top_p`。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：在所有运行中包含的一组输入或输出安全防护措施。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器（若该任务转移本身未指定）。输入过滤器允许你编辑发送给新智能体的输入。更多详情参见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 的文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：可选启用的 beta 功能，会在调用下一个智能体前将此前的对话记录折叠为单条 assistant 消息。由于我们正在稳定嵌套任务转移，该功能默认关闭；将其设为 `True` 以启用，或保持 `False` 以透传原始对话记录。所有 [`Runner` 方法](agents.run.Runner)在你未传入 `RunConfig` 时都会自动创建一个 `RunConfig`，因此快速入门与示例会保持默认关闭；任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖它。单个任务转移可通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选可调用对象。当你选择启用 `nest_handoff_history` 时，它会在每次任务转移接收标准化后的对话记录（history + handoff items）。它必须返回要转发给下一个智能体的输入项列表，使你无需编写完整的任务转移过滤器也能替换内置摘要。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许你为整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig] 以覆盖本次运行的 exporter、processor 或追踪元数据。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在敏感数据，例如 LLM 与工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：为本次运行设置追踪工作流名称、trace ID 和 trace group ID。我们建议至少设置 `workflow_name`。group ID 是可选字段，可用于跨多个运行关联 traces。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有 traces 中的元数据。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：在使用 Sessions 时，自定义每个回合前如何将新的用户输入与会话历史合并。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：用于在模型调用前，编辑已完全准备好的模型输入（instructions 和输入项）的 hook，例如用于裁剪历史或注入系统提示词。

嵌套任务转移以可选启用的 beta 形式提供。通过传入 `RunConfig(nest_handoff_history=True)` 启用折叠对话记录行为，或对某个特定任务转移设置 `handoff(..., nest_handoff_history=True)` 以仅对其启用。若你更希望保留原始对话记录（默认行为），请保持该标志不设置，或提供一个 `handoff_input_filter`（或 `handoff_history_mapper`）来按需精确转发对话。若要在不编写自定义 mapper 的情况下更改生成摘要中使用的包装文本，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并使用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 恢复默认值）。

## 对话/聊天线程

调用任一 run 方法都可能导致一个或多个智能体运行（因此也会进行一次或多次 LLM 调用），但它代表聊天对话中的一个逻辑回合。例如：

1. 用户回合：用户输入文本
2. Runner run：第一个智能体调用 LLM、运行工具、任务转移到第二个智能体；第二个智能体运行更多工具，然后生成输出。

在智能体运行结束时，你可以选择向用户展示什么。例如，你可能向用户展示智能体生成的每一个新条目，或只展示最终输出。无论哪种方式，用户随后都可能提出追问，这时你可以再次调用 run 方法。

### 手动对话管理

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法获取下一回合的输入，从而手动管理对话历史：

```python
async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
        print(result.final_output)
        # San Francisco

        # Second turn
        new_input = result.to_input_list() + [{"role": "user", "content": "What state is it in?"}]
        result = await Runner.run(agent, new_input)
        print(result.final_output)
        # California
```

### 使用 Sessions 的自动对话管理

为了更简单，你可以使用 [Sessions](sessions/index.md) 自动处理对话历史，而无需手动调用 `.to_input_list()`：

```python
from agents import Agent, Runner, SQLiteSession

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create session instance
    session = SQLiteSession("conversation_123")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
        print(result.final_output)
        # San Francisco

        # Second turn - agent automatically remembers previous context
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(result.final_output)
        # California
```

Sessions 会自动：

-   在每次运行前检索对话历史
-   在每次运行后存储新消息
-   为不同的 session ID 维护彼此独立的对话

更多详情参见 [Sessions 文档](sessions/index.md)。

### 由服务端管理的对话

你也可以让 OpenAI conversation state 功能在服务端管理对话状态，而不是在本地通过 `to_input_list()` 或 `Sessions` 处理。这使你无需手动重发所有历史消息也能保留对话历史。更多信息请参见 [OpenAI Conversation state 指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨回合跟踪状态的方式：

#### 1. 使用 `conversation_id`

你先通过 OpenAI Conversations API 创建一个对话，然后在后续每次调用中复用其 ID：

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id

    while True:
        user_input = input("You: ")
        result = await Runner.run(agent, user_input, conversation_id=conv_id)
        print(f"Assistant: {result.final_output}")
```

#### 2. 使用 `previous_response_id`

另一种方式是**响应链式调用（response chaining）**，即每一回合都显式链接到上一回合的 response ID。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    previous_response_id = None

    while True:
        user_input = input("You: ")

        # Setting auto_previous_response_id=True enables response chaining automatically
        # for the first turn, even when there's no actual previous response ID yet.
        result = await Runner.run(
            agent,
            user_input,
            previous_response_id=previous_response_id,
            auto_previous_response_id=True,
        )
        previous_response_id = result.last_response_id
        print(f"Assistant: {result.final_output}")
```

## 调用模型输入过滤器

使用 `call_model_input_filter` 在模型调用前编辑模型输入。该 hook 会接收当前智能体、上下文以及合并后的输入项（包含存在时的会话历史），并返回新的 `ModelInputData`。

```python
from agents import Agent, Runner, RunConfig
from agents.run import CallModelData, ModelInputData

def drop_old_messages(data: CallModelData[None]) -> ModelInputData:
    # Keep only the last 5 items and preserve existing instructions.
    trimmed = data.model_data.input[-5:]
    return ModelInputData(input=trimmed, instructions=data.model_data.instructions)

agent = Agent(name="Assistant", instructions="Answer concisely.")
result = Runner.run_sync(
    agent,
    "Explain quines",
    run_config=RunConfig(call_model_input_filter=drop_old_messages),
)
```

你可以通过 `run_config` 为每次运行设置该 hook，或在 `Runner` 上设置默认值，以便对敏感数据做脱敏、裁剪过长的历史记录，或注入额外的系统指引。

## 长时间运行的智能体与人工参与（human-in-the-loop）

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行可持久化、长时间运行的工作流，包括人工参与（human-in-the-loop）任务。观看 Temporal 与 Agents SDK 协同完成长时间任务的演示[视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)，以及[文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 异常

SDK 会在某些情况下抛出异常。完整列表在 [`agents.exceptions`][] 中。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内所有异常的基类。它是一个通用类型，所有其他更具体的异常都从它派生。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体的运行超过传递给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时抛出。它表明智能体无法在指定的交互回合数内完成任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生意外或无效输出时发生。可能包括：
    -   JSON 格式错误：当模型为工具调用或直接输出提供了格式错误的 JSON 结构时，尤其是在定义了特定 `output_type` 的情况下。
    -   与工具相关的意外失败：当模型未能以预期方式使用工具时
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时出现错误而抛出。通常源于错误的代码实现、无效配置或误用 SDK 的 API。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入安全防护措施或输出安全防护措施的条件分别被满足时抛出。输入安全防护措施会在处理前检查传入消息，而输出安全防护措施会在交付前检查智能体的最终响应。