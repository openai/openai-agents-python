---
search:
  exclude: true
---
# 运行智能体

你可以通过[`Runner`][agents.run.Runner]类运行智能体。你有 3 个选项：

1. [`Runner.run()`][agents.run.Runner.run]：异步运行并返回[`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，内部实际调用`.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：异步运行并返回[`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式模式调用 LLM，并在接收到事件时将其流式传输给你。

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

更多内容见[结果指南](results.md)。

## 智能体循环

当你在`Runner`中使用 run 方法时，需要传入一个起始智能体和输入。输入可以是字符串（视为用户消息），也可以是输入项列表，即 OpenAI Responses API 中的条目。

runner 随后运行一个循环：

1. 我们为当前智能体，使用当前输入调用 LLM。
2. LLM 生成输出。
    1. 如果 LLM 返回`final_output`，循环结束并返回结果。
    2. 如果 LLM 执行了任务转移，我们会更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 生成了工具调用，我们会执行这些工具调用，追加结果，并重新运行循环。
3. 如果超过传入的`max_turns`，将抛出[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]异常。

!!! note

    何时将 LLM 输出视为“最终输出”的规则是：它产生了期望类型的文本输出，并且没有工具调用。

## 流式传输

流式传输允许你在 LLM 运行时额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming]将包含关于此次运行的完整信息，包括所有新生成的输出。你可以调用`.stream_events()`获取流式事件。详见[流式传输指南](streaming.md)。

## 运行配置

`run_config`参数可用于配置智能体运行的一些全局设置：

- [`model`][agents.run.RunConfig.model]：允许设置一个全局 LLM 模型，不受各 Agent 的`model`设置影响。
- [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认是 OpenAI。
- [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定设置。例如，你可以设置全局`temperature`或`top_p`。
- [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：在所有运行中包含的输入或输出安全防护措施列表。
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器（如果该任务转移尚未设置）。输入过滤器允许你编辑发送至新智能体的输入。详见[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter]的文档。
- [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：当为`True`（默认）时，runner 会在调用下一个智能体前，将先前的对话记录折叠为单条 assistant 消息。辅助工具会将内容放入一个`<CONVERSATION HISTORY>`块中，并在随后的任务转移中持续追加新轮次。如果你更倾向于透传原始记录，可将其设为`False`，或提供自定义的 handoff 过滤器。当你未显式传入时，所有[`Runner`方法](agents.run.Runner)都会自动创建一个`RunConfig`，因此快速入门与 code examples 会自动采用该默认值，任何显式的[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter]回调仍会覆盖它。单个任务转移也可通过[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]覆盖此设置。
- [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的可调用对象，在`nest_handoff_history`为`True`时接收规范化的对话记录（历史 + handoff 条目）。它必须返回要转发到下一个智能体的输入项的精确列表，使你无需编写完整的 handoff 过滤器即可替换内置摘要。
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许为整个运行禁用[追踪](tracing.md)。
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在敏感数据，如 LLM 和工具调用的输入/输出。
- [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：为此次运行设置追踪的工作流名称、追踪 ID 和追踪分组 ID。建议至少设置`workflow_name`。分组 ID 是可选字段，可用于关联多次运行的追踪。
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪中的元数据。

默认情况下，SDK 现在会在智能体转移至另一个智能体时，将先前轮次嵌入到一条 assistant 摘要消息中。这减少了重复的 assistant 消息，并将完整的对话记录保存在一个单独的块中，便于新智能体快速扫描。如果你希望恢复旧行为，请传入`RunConfig(nest_handoff_history=False)`，或提供能够按需转发对话的`handoff_input_filter`（或`handoff_history_mapper`）。你也可以为特定的任务转移选择退出（或启用），通过设置`handoff(..., nest_handoff_history=False)`或`True`。若希望在不编写自定义映射器的情况下更改生成摘要中使用的包装文本，请调用[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（以及[`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]以恢复默认值）。

## 会话/聊天线程

调用任意运行方法可能会导致一个或多个智能体运行（因此一次或多次 LLM 调用），但这代表聊天会话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM，运行工具，执行一次任务转移到第二个智能体，第二个智能体运行更多工具，然后产出输出。

在智能体运行结束后，你可以选择向用户展示什么。例如，你可以向用户展示由智能体生成的每个新条目，或仅展示最终输出。无论哪种方式，用户都可能继续追问，此时你可以再次调用 run 方法。

### 手动会话管理

你可以使用[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list]手动管理会话历史，以获取下一轮的输入：

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

为简化流程，你可以使用[Sessions](sessions/index.md)自动处理会话历史，而无需手动调用`.to_input_list()`：

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
- 为不同的会话 ID 维持独立会话

更多细节见[Sessions 文档](sessions/index.md)。

### 服务端托管的会话

你也可以让 OpenAI 的会话状态功能在服务端管理会话状态，而不是通过`to_input_list()`或`Sessions`在本地处理。这样可以在无需手动重发所有历史消息的情况下保留会话历史。详见[OpenAI Conversation state 指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨轮次跟踪状态的方式：

#### 1. 使用`conversation_id`

你首先使用 OpenAI Conversations API 创建一个会话，然后在每次后续调用中复用其 ID：

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

#### 2. 使用`previous_response_id`

另一种选择是**响应链式关联**，即每一轮显式链接到上一轮的 response ID。

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

你可以使用 Agents SDK 的[Temporal](https://temporal.io/)集成来运行持久、长时间运行的工作流，包括人类参与（human-in-the-loop）任务。可在[此视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中观看 Temporal 与 Agents SDK 协同完成长时任务的演示，并[在此查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 异常

SDK 在某些情况下会抛出异常。完整列表见[`agents.exceptions`][]。概览如下：

- [`AgentsException`][agents.exceptions.AgentsException]：SDK 内抛出的所有异常的基类。它是所有其他特定异常的通用父类型。
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传入至`Runner.run`、`Runner.run_sync`或`Runner.run_streamed`方法的`max_turns`限制时抛出。表示智能体无法在指定的交互轮次内完成任务。
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生出乎意料或无效的输出时发生。包括但不限于：
    - 格式错误的 JSON：当模型为工具调用或其直接输出提供了格式错误的 JSON，尤其是在定义了特定`output_type`时。
    - 意外的工具相关失败：当模型未按预期方式使用工具
- [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时出错而抛出。通常由错误的代码实现、无效配置或对 SDK API 的误用导致。
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当分别满足输入安全防护措施或输出安全防护措施的触发条件时抛出。输入安全防护措施在处理前检查传入消息，输出安全防护措施在交付前检查智能体的最终响应。