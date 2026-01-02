---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类来运行智能体。共有 3 种方式：

1. [`Runner.run()`][agents.run.Runner.run]：异步运行并返回一个 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，内部调用 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：异步运行并返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式传输模式调用 LLM，并在接收时将这些事件流式传输给你。

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

当你在 `Runner` 中使用 run 方法时，需要传入一个起始智能体和输入。输入可以是一个字符串（视为用户消息），也可以是一个输入项列表，这些输入项即 OpenAI Responses API 中的条目。

runner 会运行如下循环：

1. 我们使用当前输入为当前智能体调用 LLM。
2. LLM 生成输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 进行任务转移，我们会更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 产生工具调用，我们会运行这些工具调用，追加结果并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判断 LLM 输出是否为“最终输出”的规则是：它生成了具有期望类型的文本输出，且没有工具调用。

## 流式传输

流式传输允许你在 LLM 运行时同时接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含关于本次运行的完整信息，包括所有新生成的输出。你可以调用 `.stream_events()` 获取流式事件。更多内容参见[流式传输指南](streaming.md)。

## 运行配置

`run_config` 参数可用于配置本次智能体运行的一些全局设置：

- [`model`][agents.run.RunConfig.model]：允许设置一个全局的 LLM 模型使用，而不受各个 Agent 的 `model` 配置影响。
- [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认是 OpenAI。
- [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体级别的设置。例如，你可以设置全局的 `temperature` 或 `top_p`。
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：要在所有运行中包含的输入或输出安全防护措施列表。
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：一个全局输入过滤器，若某次任务转移未定义，则应用于所有任务转移。输入过滤器允许你编辑发送给新智能体的输入。详见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter]。
- [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：当为 `True`（默认）时，runner 会在调用下一个智能体前，将先前的对话记录折叠为单个助手消息。帮助器将内容放入一个 `<CONVERSATION HISTORY>` 块中，并在后续任务转移发生时不断追加新轮次。如果你希望传递原始对话记录，请将其设为 `False`，或提供自定义的 handoff 过滤器。所有 [`Runner` 方法](agents.run.Runner) 在你未显式传入时会自动创建一个 `RunConfig`，因此快速上手与 code examples 会自动采用该默认值，且任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖它。单次任务转移可通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖该设置。
- [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的可调用对象。当 `nest_handoff_history` 为 `True` 时，它会接收规范化后的对话记录（history + handoff items）。它必须返回要转发给下一个智能体的精确输入项列表，使你无需编写完整的 handoff 过滤器即可替换内置摘要。
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许为整个运行禁用[追踪](tracing.md)。
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在的敏感数据，例如 LLM 与工具调用的输入/输出。
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：为此次运行设置追踪的工作流名称、追踪 ID 和追踪分组 ID。我们建议至少设置 `workflow_name`。分组 ID 是可选字段，允许你关联多次运行之间的追踪。
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪上的元数据。

默认情况下，当一个智能体将任务转移给另一个智能体时，SDK 会将先前的轮次嵌套到单个助手摘要消息中。这减少了重复的助手消息，并将完整的对话记录保存在一个单独的块中，便于新智能体快速扫描。如果你希望恢复旧行为，可以传入 `RunConfig(nest_handoff_history=False)`，或者提供一个 `handoff_input_filter`（或 `handoff_history_mapper`）以按需原样转发对话。你也可以为特定的任务转移选择退出（或启用），通过设置 `handoff(..., nest_handoff_history=False)` 或 `True`。若想在不编写自定义映射器的情况下更改生成摘要所用的包装文本，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（以及用于恢复默认值的 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会话/聊天线程

调用任一运行方法，可能会导致一个或多个智能体运行（因此可能会有一次或多次 LLM 调用），但其代表一次聊天会话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM、运行工具、将任务转移给第二个智能体，第二个智能体运行更多工具，然后生成输出。

在智能体运行结束时，你可以选择向用户展示的内容。例如，你可以展示智能体生成的每一个新条目，或仅展示最终输出。无论哪种方式，用户都可能继续追问，此时你可以再次调用 run 方法。

### 手动会话管理

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法手动管理会话历史，以获取下一轮的输入：

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

### 使用 Sessions 的自动会话管理

若想更简化，你可以使用 [Sessions](sessions/index.md) 自动处理会话历史，而无需手动调用 `.to_input_list()`：

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

- 在每次运行前检索会话历史
- 在每次运行后存储新消息
- 为不同的 session ID 维护独立会话

更多详情参见 [Sessions 文档](sessions/index.md)。

### 由服务管理的会话

你也可以让 OpenAI 的对话状态功能在服务端管理会话状态，而不是通过 `to_input_list()` 或 `Sessions` 在本地处理。这样可以在无需手动重发全部历史消息的情况下保留会话历史。更多内容参见 [OpenAI Conversation state 指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨轮次跟踪状态的方法：

#### 1. 使用 `conversation_id`

你首先通过 OpenAI Conversations API 创建一个会话，然后在后续每次调用中复用其 ID：

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

另一种方式是**响应链（response chaining）**，每一轮显式链接到上一轮的响应 ID。

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

## 长时间运行的智能体与人类参与

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行持久、长时间运行的工作流，包括 human-in-the-loop 任务。可在[此视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中观看 Temporal 与 Agents SDK 协同完成长任务的演示，并[在此查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 异常

SDK 在某些情况下会抛出异常。完整列表见 [`agents.exceptions`][]。概览如下：

- [`AgentsException`][agents.exceptions.AgentsException]：SDK 内抛出的所有异常的基类。它是所有其他特定异常的通用父类型。
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体的运行超过传给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时抛出。表示智能体未能在指定的交互轮次数内完成任务。
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生意外或无效输出时发生。这可能包括：
    - 格式错误的 JSON：当模型为工具调用或其直接输出提供格式错误的 JSON 结构，尤其是在指定了特定 `output_type` 时。
    - 与工具相关的意外失败：当模型未能以预期方式使用工具
- [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时发生错误时抛出。通常由不正确的代码实现、无效的配置或对 SDK API 的误用导致。
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入或输出安全防护措施的触发条件分别被满足时抛出。输入安全防护措施在处理之前检查传入消息，而输出安全防护措施在交付之前检查智能体的最终响应。