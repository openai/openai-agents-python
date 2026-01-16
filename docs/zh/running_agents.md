---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类来运行智能体。你有 3 种选择：

1. [`Runner.run()`][agents.run.Runner.run]：异步运行并返回 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，实际上在内部调用 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：异步运行并返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式模式调用 LLM，并在接收事件时将其流式传输给你。

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

当你在 `Runner` 中使用 run 方法时，你会传入一个起始智能体和输入。输入可以是字符串（被视为用户消息），也可以是输入项列表，这些输入项符合 OpenAI Responses API 的格式。

运行器随后执行一个循环：

1. 我们使用当前输入调用当前智能体的 LLM。
2. LLM 生成输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 进行任务转移，我们更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 产生工具调用，我们运行这些工具调用、追加结果并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判断 LLM 输出是否为“最终输出”的规则是：它生成了所需类型的文本输出，且没有工具调用。

## 流式传输

流式传输允许你在 LLM 运行时接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含关于此次运行的完整信息，包括所有新产生的输出。你可以调用 `.stream_events()` 获取流式事件。更多内容请阅读[流式传输指南](streaming.md)。

## 运行配置

`run_config` 参数可让你为智能体运行配置一些全局设置：

-   [`model`][agents.run.RunConfig.model]：允许设置一个全局 LLM 模型使用，而不受每个 Agent 的 `model` 限制。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认是 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定设置。例如，你可以设置全局的 `temperature` 或 `top_p`。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：要在所有运行中包含的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器（如果该任务转移尚未设置）。输入过滤器允许你编辑发送给新智能体的输入。详见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 的文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：当为 `True`（默认）时，运行器会在调用下一个智能体之前，将先前的对话记录折叠为单个 assistant 消息。辅助工具会将内容放入一个 `<CONVERSATION HISTORY>` 块中，随着后续任务转移不断追加新的回合。如果你希望传递原始对话记录，可将其设为 `False`，或提供自定义的任务转移过滤器。当你未显式传入时，所有 [`Runner` 方法](agents.run.Runner) 会自动创建一个 `RunConfig`，因此快速上手和 code examples 会自动采用该默认值，任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖该行为。单个任务转移也可以通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的可调用对象，在 `nest_handoff_history` 为 `True` 时接收规范化的对话记录（历史 + 任务转移项）。它必须返回要转发给下一个智能体的精确输入项列表，使你无需编写完整的任务转移过滤器即可替换内置摘要。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许为整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig]，以覆盖本次运行的导出器、进程或追踪元数据。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪是否包含潜在敏感数据，如 LLM 和工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：为此次运行设置追踪的工作流名称、追踪 ID 和追踪分组 ID。我们建议至少设置 `workflow_name`。分组 ID 是可选字段，用于将多个运行的追踪关联起来。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪中的元数据。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：使用 Sessions 时，自定义在每个回合前如何将新的用户输入与会话历史合并。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：在模型调用前立即编辑已完整准备好的模型输入（instructions 和输入项）的钩子，例如用于裁剪历史或注入 system prompt。

默认情况下，SDK 现在会在智能体向另一个智能体进行任务转移时，将先前的回合嵌套在单个 assistant 摘要消息中。这减少了重复的 assistant 消息，并将完整对话记录置于一个新智能体可快速扫描的单独块中。如果你想恢复旧版行为，请传入 `RunConfig(nest_handoff_history=False)`，或提供一个按你需求精确转发对话的 `handoff_input_filter`（或 `handoff_history_mapper`）。你也可以为特定的任务转移选择退出（或加入），方式是设置 `handoff(..., nest_handoff_history=False)` 或 `True`。若希望在不编写自定义映射器的情况下更改生成摘要中使用的包装文本，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（以及用于恢复默认值的 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会话/聊天线程

调用任意运行方法都可能导致运行一个或多个智能体（因此也会有一次或多次 LLM 调用），但它表示聊天会话中的单个逻辑回合。例如：

1. 用户回合：用户输入文本
2. Runner 运行：第一个智能体调用 LLM，运行工具，进行一次任务转移到第二个智能体，第二个智能体运行更多工具，然后产生输出。

在智能体运行结束时，你可以选择向用户展示什么。例如，你可以向用户展示智能体生成的每一条新内容，或仅展示最终输出。无论哪种方式，用户都可能接着提出后续问题，此时你可以再次调用运行方法。

### 手动管理会话

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法手动管理会话历史，从而获取下一回合的输入：

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

如果希望更简单的方式，你可以使用 [Sessions](sessions/index.md) 自动处理会话历史，而无需手动调用 `.to_input_list()`：

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
-   为不同的会话 ID 维护独立的会话

更多详情请参阅 [Sessions 文档](sessions/index.md)。

### 服务托管的会话

你也可以让 OpenAI 会话状态功能在服务上管理会话状态，而不是用 `to_input_list()` 或 `Sessions` 在本地处理。这样可以在无需手动重新发送所有历史消息的情况下保留会话历史。详情参见 [OpenAI Conversation state 指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨回合跟踪状态的方式：

#### 1. 使用 `conversation_id`

你首先使用 OpenAI Conversations API 创建一个会话，然后在后续每次调用中复用该 ID：

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

另一种选择是**响应串联（response chaining）**，其中每个回合都显式链接到上一回合的响应 ID。

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

使用 `call_model_input_filter` 在模型调用前编辑模型输入。该钩子接收当前智能体、上下文以及合并后的输入项（在存在会话历史时包括会话历史），并返回新的 `ModelInputData`。

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

通过 `run_config` 为每次运行设置该钩子，或将其作为 `Runner` 的默认设置，以实现敏感数据脱敏、裁剪过长历史或注入额外的系统指引。

## 长时运行智能体与人类参与

你可以使用 Agents SDK 与 [Temporal](https://temporal.io/) 的集成来运行可靠的长时工作流，包括有人类参与的任务。查看一个 Temporal 与 Agents SDK 协同完成长时任务的演示[视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)，以及[相关文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 异常

SDK 在某些情况下会抛出异常。完整列表见 [`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内抛出的所有异常的基类。它作为通用类型，其他特定异常均从此派生。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时抛出。表示智能体无法在指定的交互回合数内完成任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生意外或无效输出时发生。这可能包括：
    -   JSON 格式错误：当模型为工具调用或直接输出提供了格式错误的 JSON，特别是在定义了特定 `output_type` 时。
    -   意外的工具相关失败：当模型未以预期方式使用工具时
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时发生错误会抛出该异常。通常由错误的代码实现、无效配置或对 SDK API 的误用导致。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入安全防护措施或输出安全防护措施的条件被满足时分别抛出。输入安全防护措施在处理前检查传入消息，而输出安全防护措施在交付前检查智能体的最终响应。