---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类来运行智能体。你有3个选项：

1. [`Runner.run()`][agents.run.Runner.run]，异步运行并返回 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]，这是一个同步方法，底层运行 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，异步运行并返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式模式调用LLM，并在接收到事件时将这些事件流式传输给你。

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

阅读更多内容请参考[结果指南](results.md)。

## 智能体循环

当你在`Runner`中使用运行方法时，你传入一个起始智能体和输入。输入可以是字符串（被视为用户消息），或者输入项列表，这些是OpenAI响应API中的项。

然后运行器运行一个循环：

1. 我们使用当前输入为当前智能体调用LLM。
2. LLM产生其输出。
    1. 如果LLM返回`final_output`，循环结束，我们返回结果。
    2. 如果LLM进行交接，我们更新当前智能体和输入，并重新运行循环。
    3. 如果LLM产生工具调用，我们运行这些工具调用，追加结果，并重新运行循环。
3. 如果我们超过了传入的`max_turns`，我们会引发一个 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! 注意

    LLM输出是否被视为"最终输出"的规则是它产生所需类型的文本输出，并且没有工具调用。

## 流式传输

流式传输允许你在LLM运行时额外接收流式传输事件。流式传输完成后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含有关运行的完整信息，包括产生的所有新输出。你可以调用 `.stream_events()` 来获取流式传输事件。阅读更多内容请参考[流式传输指南](streaming.md)。

## 运行配置

`run_config` 参数允许你为智能体运行配置一些全局设置：

-   [`model`][agents.run.RunConfig.model]: 允许设置要使用的全局LLM模型，不管每个智能体有什么`model`。
-   [`model_provider`][agents.run.RunConfig.model_provider]: 用于查找模型名称的模型提供程序，默认为OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]: 覆盖特定于智能体的设置。例如，你可以设置全局的`temperature`或`top_p`。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 要包含在所有运行上的输入或输出护栏列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 如果交接还没有输入过滤器，则应用于所有交接的全局输入过滤器。输入过滤器允许你编辑发送到新智能体的输入。有关详细信息，请参见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 中的文档。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 允许你为整个运行禁用[追踪](tracing.md)。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: 配置追踪是否将包含潜在敏感数据，例如LLM和工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 为运行设置追踪工作流名称、追踪ID和追踪组ID。我们建议至少设置`workflow_name`。组ID是一个可选字段，允许你跨多个运行链接追踪。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 要包含在所有追踪上的元数据。

## 对话/聊天线程

调用任何运行方法都可能导致一个或多个智能体运行（因此一个或多个LLM调用），但它代表聊天对话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. 运行器运行：第一个智能体调用LLM，运行工具，交接给第二个智能体，第二个智能体运行更多工具，然后产生输出。

在智能体运行结束时，你可以选择向用户显示什么。例如，你可能会向用户显示智能体生成的每个新项目，或者只显示最终输出。无论哪种方式，用户都可能会问后续问题，在这种情况下，你可以再次调用运行方法。

### 手动对话管理

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法手动管理对话历史记录，以获取下一轮次的输入：

```python
async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    thread_id = "thread_123"  # 示例线程ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # 第一轮次
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
        print(result.final_output)
        # San Francisco

        # 第二轮次
        new_input = result.to_input_list() + [{"role": "user", "content": "What state is it in?"}]
        result = await Runner.run(agent, new_input)
        print(result.final_output)
        # California
```

### 使用Sessions自动对话管理

对于更简单的方法，你可以使用[Sessions](sessions.md)来自动处理对话历史记录，而无需手动调用`.to_input_list()`：

```python
from agents import Agent, Runner, SQLiteSession

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # 创建会话实例
    session = SQLiteSession("conversation_123")

    thread_id = "thread_123"  # 示例线程ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # 第一轮次
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
        print(result.final_output)
        # San Francisco

        # 第二轮次 - 智能体自动记住之前的上下文
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(result.final_output)
        # California
```

Sessions自动：

-   在每次运行前检索对话历史记录
-   在每次运行后存储新消息
-   为不同的会话ID维护单独的对话

有关详细信息，请参见[Sessions文档](sessions.md)。


### 服务器管理的对话

你还可以让OpenAI对话状态功能在服务器端管理对话状态，而不是使用`to_input_list()`或`Sessions`在本地处理它。这允许你保留对话历史记录，而无需手动重新发送所有过去的消息。有关详细信息，请参见[OpenAI对话状态指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI提供了两种跨轮次跟踪状态的方法：

#### 1. 使用`conversation_id`

你首先使用OpenAI对话API创建一个对话，然后为每个后续调用重用其ID：

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    # 创建一个服务器管理的对话
    conversation = await client.conversations.create()
    conv_id = conversation.id

    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # 第一轮次
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?", conversation_id=conv_id)
    print(result1.final_output)
    # San Francisco

    # 第二轮次重用相同的conversation_id
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        conversation_id=conv_id,
    )
    print(result2.final_output)
    # California
```

#### 2. 使用`previous_response_id`

另一个选项是**响应链接**，其中每个轮次显式链接到前一轮次的响应ID。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # 第一轮次
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
    print(result1.final_output)
    # San Francisco

    # 第二轮次，链接到之前的响应
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        previous_response_id=result1.last_response_id,
    )
    print(result2.final_output)
    # California
```


## 长时间运行的智能体和人机交互

你可以使用Agents SDK [Temporal](https://temporal.io/) 集成来运行持久的、长时间运行的工作流，包括人机交互任务。查看[此视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中Temporal和Agents SDK协同工作完成长时间运行任务的演示，以及[查看此处的文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 异常

SDK在某些情况下会引发异常。完整列表在 [`agents.exceptions`][] 中。概述如下：

-   [`AgentsException`][agents.exceptions.AgentsException]: 这是SDK内引发的所有异常的基类。它充当所有其他特定异常的通用类型。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 当智能体的运行超过传递给`Runner.run`、`Runner.run_sync`或`Runner.run_streamed`方法的`max_turns`限制时，会引发此异常。它表明智能体无法在指定的交互轮次内完成其任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 当底层模型（LLM）产生意外或无效的输出时，会发生此异常。这可能包括：
    -   格式错误的JSON：当模型为工具调用或其直接输出提供格式错误的JSON结构时，特别是如果定义了特定的`output_type`。
    -   意外的工具相关失败：当模型未能以预期方式使用工具时
-   [`UserError`][agents.exceptions.UserError]: 当你（使用SDK编写代码的人）在使用SDK时出错，会引发此异常。这通常是由于代码实现不正确、配置无效或误用SDK的API导致的。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 当满足输入护栏或输出护栏的条件时，分别会引发此异常。输入护栏在处理前检查传入消息，而输出护栏在传递前检查智能体的最终响应。