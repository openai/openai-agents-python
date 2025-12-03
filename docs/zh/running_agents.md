---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类来运行智能体。你有 3 种方式：

1. [`Runner.run()`][agents.run.Runner.run]：异步运行，并返回 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，内部调用 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：异步运行，并返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式方式调用 LLM，并在接收时将事件流式传输给你。

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

当你在 `Runner` 中使用 run 方法时，你需要传入一个起始智能体和输入。输入可以是字符串（视为用户消息），也可以是输入项列表，即 OpenAI Responses API 中的项。

runner 随后会运行一个循环：

1. 使用当前输入调用当前智能体的 LLM。
2. LLM 生成输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 进行任务转移（handoff），我们会更新当前智能体和输入，然后重新运行循环。
    3. 如果 LLM 产生工具调用（tool calls），我们会运行这些工具调用，追加结果，然后重新运行循环。
3. 如果超过传入的 `max_turns`，会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判断 LLM 输出是否为“最终输出”的规则是：它生成了期望类型的文本输出，且没有工具调用。

## 流式传输

流式传输允许你在 LLM 运行时实时接收事件。一旦流结束，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含有关此次运行的完整信息，包括所有新生成的输出。你可以调用 `.stream_events()` 获取流事件。参见[流式传输指南](streaming.md)。

## 运行配置

`run_config` 参数可让你为智能体运行配置一些全局设置：

-   [`model`][agents.run.RunConfig.model]：允许设置一个全局 LLM 模型使用，而不管每个 Agent 自身的 `model`。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认是 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定的设置。例如，你可以设置全局 `temperature` 或 `top_p`。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：在所有运行中包含的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：为所有任务转移应用的全局输入过滤器（如果该 handoff 尚未设置）。输入过滤器允许你编辑发送给新智能体的输入。更多详情参见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 的文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：当为 `True`（默认）时，runner 会在调用下一个智能体前，将先前的对话记录折叠为单条 assistant 消息。辅助工具会将内容置于一个不断追加新轮次的 `<CONVERSATION HISTORY>` 块中。若你希望传递原始对话记录，请将其设为 `False`，或提供自定义 handoff 过滤器。当你未传入时，所有 [`Runner` 方法](agents.run.Runner) 会自动创建一个 `RunConfig`，因此快速上手和 code examples 会自动使用此默认值，且任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍将覆盖它。单次 handoff 可通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的可调用对象，当 `nest_handoff_history` 为 `True` 时接收规范化的对话记录（history + handoff 项）。它必须返回要转发给下一个智能体的精确输入项列表，允许你在不编写完整 handoff 过滤器的情况下替换内置摘要。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许为整个运行禁用[追踪](tracing.md)。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在敏感数据，例如 LLM 与工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：为此次运行设置追踪的工作流名称、trace ID 和 trace group ID。我们建议至少设置 `workflow_name`。group ID 是可选字段，用于将多次运行的追踪关联起来。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：在所有追踪中包含的元数据。

默认情况下，SDK 现在在智能体进行任务转移时，会将先前的多轮对话嵌套进单条 assistant 摘要消息中。这样减少重复的 assistant 消息，并将完整对话记录保存在一个新智能体可快速扫描的单一块中。若你希望恢复旧行为，传入 `RunConfig(nest_handoff_history=False)`，或提供一个 `handoff_input_filter`（或 `handoff_history_mapper`）以按需原样转发对话。你也可以为特定 handoff 选择退出（或加入），通过设置 `handoff(..., nest_handoff_history=False)` 或 `True`。若仅想修改生成摘要中使用的包装文本，而不编写自定义 mapper，可调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（使用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 恢复默认值）。

## 会话/聊天线程

调用任意 run 方法可能会导致一个或多个智能体运行（从而进行一次或多次 LLM 调用），但它代表聊天会话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM，运行工具，进行一次任务转移到第二个智能体；第二个智能体运行更多工具，然后生成输出。

在智能体运行结束时，你可以选择向用户展示什么内容。例如，你可以展示智能体生成的每一条新项，或只展示最终输出。无论哪种方式，用户都可能提出追问，此时你可以再次调用 run 方法。

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

更简单的方式是使用 [Sessions](sessions/index.md)，自动处理会话历史，而无需手动调用 `.to_input_list()`：

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

-   在每次运行前检索会话历史
-   在每次运行后存储新消息
-   为不同的 session ID 维护独立会话

更多详情参见 [Sessions 文档](sessions/index.md)。

### 由服务端管理的会话

你也可以让 OpenAI 的对话状态功能在服务端管理会话状态，而不是通过 `to_input_list()` 或 `Sessions` 在本地处理。这样可以在无需手动重发全部历史消息的情况下保留会话历史。参见 [OpenAI Conversation state 指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)了解更多。

OpenAI 提供两种跨轮次跟踪状态的方式：

#### 1. 使用 `conversation_id`

先使用 OpenAI Conversations API 创建一个会话，然后在后续每次调用中复用其 ID：

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id    

    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?", conversation_id=conv_id)
    print(result1.final_output)
    # San Francisco

    # Second turn reuses the same conversation_id
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        conversation_id=conv_id,
    )
    print(result2.final_output)
    # California
```

#### 2. 使用 `previous_response_id`

另一种方式是**响应链（response chaining）**，每一轮显式链接到上一轮的 response ID。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
    print(result1.final_output)
    # San Francisco

    # Second turn, chained to the previous response
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        previous_response_id=result1.last_response_id,
    )
    print(result2.final_output)
    # California
```


## 长时运行的智能体与人类参与（human-in-the-loop）

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行持久的、长时运行的工作流，包括含有人类参与的任务。在[这个视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中查看 Temporal 与 Agents SDK 协作完成长时任务的演示，并[查看此处的文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 异常

SDK 会在特定情况下抛出异常。完整列表见 [`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 中所有异常的基类。它作为通用类型，其他特定异常均从此派生。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时抛出。表示智能体无法在指定的交互轮次内完成任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生意外或无效输出时发生，包括：
    -   JSON 结构不合法：当模型为工具调用或直接输出提供了格式错误的 JSON，尤其是在定义了特定 `output_type` 时。
    -   与工具相关的意外失败：当模型未按预期方式使用工具
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时出错会抛出此异常。通常源于错误的代码实现、无效配置或对 SDK API 的误用。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入或输出的安全防护措施条件被触发时分别抛出。输入安全防护措施在处理前检查传入消息，而输出安全防护措施在交付前检查智能体的最终响应。