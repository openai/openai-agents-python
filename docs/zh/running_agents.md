---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类来运行智能体。你有 3 种选择：

1. [`Runner.run()`][agents.run.Runner.run]：异步运行并返回一个 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，本质上在内部调用 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：异步运行并返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式模式调用 LLM，并将接收到的事件实时流式传输给你。

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

在[结果指南](results.md)中阅读更多内容。

## 智能体循环

当你使用 `Runner` 中的 run 方法时，你需要传入一个起始智能体和输入。输入可以是字符串（被视为用户消息），也可以是输入项列表，这些输入项符合 OpenAI Responses API 的格式。

runner 随后会运行一个循环：

1. 我们用当前输入为当前智能体调用 LLM。
2. LLM 生成输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 进行任务转移，我们会更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 生成工具调用，我们会运行这些工具调用，追加结果，并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判断 LLM 输出是否被视为“最终输出”的规则是：输出为所需类型的文本，且没有工具调用。

## 流式传输

流式传输允许在 LLM 运行时额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含关于此次运行的完整信息，包括所有新生成的输出。你可以调用 `.stream_events()` 获取流式事件。更多内容参见[流式传输指南](streaming.md)。

## 运行配置

`run_config` 参数允许你为一次智能体运行配置一些全局设置：

-   [`model`][agents.run.RunConfig.model]：允许设置一个全局 LLM 模型来使用，而不考虑每个 Agent 的 `model`。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认为 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定的设置。例如，你可以设置全局的 `temperature` 或 `top_p`。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：在所有运行中包含的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：对所有任务转移应用的全局输入过滤器（如果该任务转移尚未设置）。输入过滤器允许你编辑发送给新智能体的输入。更多细节参见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：当为 `True`（默认）时，runner 会在调用下一个智能体之前，将之前的对话记录折叠为一条助理消息。该助手会将内容放入一个 `<CONVERSATION HISTORY>` 块，并在后续发生任务转移时不断追加新的轮次。如果你更希望传递原始对话记录，可将其设为 `False`，或提供自定义的 handoff 过滤器。所有 [`Runner` 方法](agents.run.Runner) 在未显式传入时会自动创建一个 `RunConfig`，因此快速上手和 code examples 会自动采用该默认值，且任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖它。单个任务转移也可通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的可调用对象，当 `nest_handoff_history` 为 `True` 时接收规范化后的对话记录（history + handoff items）。它必须返回要转发给下一个智能体的精确输入项列表，使你无需编写完整的 handoff 过滤器即可替换内置摘要。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许为整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig]，以覆盖本次运行的导出器、进程或追踪元数据。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在的敏感数据，例如 LLM 和工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：为本次运行设置追踪工作流名称、追踪 ID 和追踪分组 ID。我们建议至少设置 `workflow_name`。分组 ID 是可选字段，允许你将多个运行的追踪关联在一起。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪中的元数据。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：在使用 Sessions 时，自定义在每轮之前如何将新的用户输入与会话历史合并。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：在模型调用前的最后一步编辑已准备好的模型输入（instructions 和 input items）的钩子，例如用于裁剪历史或注入 system prompt。

默认情况下，当一个智能体将任务转移给另一个智能体时，SDK 现在会将之前的轮次嵌套进单个助理总结消息内。这减少了重复的助理消息，并将完整对话记录保存在单个块中，便于新智能体快速扫描。如果你希望恢复旧的行为，可传入 `RunConfig(nest_handoff_history=False)` 或提供 `handoff_input_filter`（或 `handoff_history_mapper`）以按需转发对话。你也可以在特定 handoff 上选择退出（或加入），通过设置 `handoff(..., nest_handoff_history=False)` 或 `True`。如需在不编写自定义映射器的情况下更改生成摘要中使用的包装文本，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（以及 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 来恢复默认值）。

## 对话/聊天线程

调用任一运行方法都可能导致一个或多个智能体运行（从而一个或多个 LLM 调用），但它代表聊天会话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM、运行工具、将任务转移给第二个智能体，第二个智能体运行更多工具，然后生成输出。

在智能体运行结束时，你可以选择向用户展示什么。例如，你可以向用户展示智能体生成的每个新项目，或者仅展示最终输出。无论哪种方式，用户都可能继续追问，此时你可以再次调用运行方法。

### 手动对话管理

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 手动管理对话历史，以获取下一轮的输入：

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

更简单的方法是使用 [Sessions](sessions/index.md) 自动处理对话历史，而无需手动调用 `.to_input_list()`：

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
-   为不同的会话 ID 维护独立的对话

更多细节参见 [Sessions 文档](sessions/index.md)。

### 服务端管理的对话

你也可以让 OpenAI 的对话状态功能在服务端管理对话状态，而不是在本地通过 `to_input_list()` 或 `Sessions` 来处理。这样可以在无需手动重发所有历史消息的情况下保留对话历史。更多细节参见 [OpenAI Conversation state 指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨轮次追踪状态的方式：

#### 1. 使用 `conversation_id`

你首先使用 OpenAI Conversations API 创建一个对话，然后在后续的每次调用中复用其 ID：

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

另一种选择是**响应链（response chaining）**，每一轮都显式链接到上一轮的响应 ID。

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

## 模型调用输入过滤器

使用 `call_model_input_filter` 在模型调用前编辑模型输入。该钩子接收当前智能体、上下文以及合并后的输入项（在存在会话历史时包含会话历史），并返回新的 `ModelInputData`。

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

通过 `run_config` 为每次运行设置该钩子，或在你的 `Runner` 上设置为默认，以便去除敏感数据、裁剪过长历史，或注入额外的系统指导。

## 长时运行智能体与人类参与环节

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行持久的、长时运行的工作流，包括人类参与（human-in-the-loop）任务。查看一段演示 Temporal 与 Agents SDK 协同完成长时任务的[视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)，并[在此查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 异常

SDK 在某些情况下会抛出异常。完整列表见 [`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：这是 SDK 内抛出的所有异常的基类。它作为通用类型，其他所有具体异常均由此派生。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体的运行超过传递给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 的 `max_turns` 限制时抛出该异常。它表示智能体无法在指定的交互轮次内完成任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生意外或无效输出时发生此异常。这可能包括：
    -   JSON 格式错误：当模型为工具调用或直接输出提供了格式错误的 JSON 结构，尤其是在定义了特定的 `output_type` 时。
    -   意外的与工具相关的失败：当模型未以预期方式使用工具时
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时出现错误，会抛出该异常。通常由错误的代码实现、无效的配置或对 SDK API 的误用导致。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入安全防护措施或输出安全防护措施的条件分别被触发时，抛出该异常。输入安全防护措施在处理前检查传入消息，而输出安全防护措施在交付前检查智能体的最终响应。