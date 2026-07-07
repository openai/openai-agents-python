---
search:
  exclude: true
---
# 智能体运行

您可以通过[`Runner`][agents.run.Runner]类运行智能体。您有3种选择：

1. [`Runner.run()`][agents.run.Runner.run]，异步运行并返回[`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]，同步方法，底层只是运行`.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，异步运行并返回[`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式传输模式调用LLM，并在收到事件时将其流式传输给您。

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

请在[结果指南](results.md)中阅读更多内容。

## Runner生命周期与配置

### 智能体循环

当您使用`Runner`中的run方法时，需要传入一个起始智能体和输入。输入可以是：

-   字符串（视为用户消息），
-   OpenAI Responses API格式的输入项列表，或
-   在恢复中断运行时使用的[`RunState`][agents.run_state.RunState]。

然后，运行器会执行一个循环：

1. 我们使用当前输入调用当前智能体的LLM。
2. LLM生成其输出。
    1. 如果LLM返回`final_output`，循环结束并返回结果。
    2. 如果LLM执行任务转移，我们会更新当前智能体和输入，并重新运行循环。
    3. 如果LLM生成工具调用，我们会运行这些工具调用、追加结果，并重新运行循环。
3. 如果超过传入的`max_turns`，我们会抛出[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]异常。传入`max_turns=None`可禁用此轮次限制。

!!! note

    判断LLM输出是否被视为“最终输出”的规则是：它生成具有所需类型的文本输出，并且没有工具调用。

### 流式传输

流式传输让您在LLM运行时额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming]将包含该运行的完整信息，包括生成的所有新输出。您可以调用`.stream_events()`获取流式事件。请在[流式传输指南](streaming.md)中阅读更多内容。

#### Responses WebSocket传输（可选辅助工具）

如果您启用OpenAI Responses websocket传输，可以继续使用常规`Runner` API。建议使用websocket会话辅助工具来复用连接，但这不是必需的。

这是通过websocket传输使用的Responses API，而不是[Realtime API](realtime/guide.md)。

关于传输选择规则，以及具体模型对象或自定义提供方的注意事项，请参阅[模型](models/index.md#responses-websocket-transport)。

##### 模式1：不使用会话辅助工具（可行）

当您只想使用websocket传输，且不需要SDK为您管理共享提供方/会话时，请使用此模式。

```python
import asyncio

from agents import Agent, Runner, set_default_openai_responses_transport


async def main():
    set_default_openai_responses_transport("websocket")

    agent = Agent(name="Assistant", instructions="Be concise.")
    result = Runner.run_streamed(agent, "Summarize recursion in one sentence.")

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            continue
        print(event.type)


asyncio.run(main())
```

这种模式适合单次运行。如果您反复调用`Runner.run()` / `Runner.run_streamed()`，除非手动复用同一个`RunConfig` / 提供方实例，否则每次运行都可能重新连接。

##### 模式2：使用`responses_websocket_session()`（建议用于多轮复用）

当您希望在多次运行之间共享支持websocket的提供方和`RunConfig`时，请使用[`responses_websocket_session()`][agents.responses_websocket_session]（包括继承同一`run_config`的嵌套智能体作为工具调用）。

```python
import asyncio

from agents import Agent, responses_websocket_session


async def main():
    agent = Agent(name="Assistant", instructions="Be concise.")

    async with responses_websocket_session(
        responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
    ) as ws:
        first = ws.run_streamed(agent, "Say hello in one short sentence.")
        async for _event in first.stream_events():
            pass

        second = ws.run_streamed(
            agent,
            "Now say goodbye.",
            previous_response_id=first.last_response_id,
        )
        async for _event in second.stream_events():
            pass


asyncio.run(main())
```

在上下文退出之前完成对流式传输结果的消费。如果websocket请求仍在进行中时退出上下文，可能会强制关闭共享连接。

如果长推理轮次触发websocket keepalive超时，请增大`ping_timeout`，或设置`ping_timeout=None`以禁用心跳超时。对于可靠性比websocket延迟更重要的运行，请使用HTTP/SSE传输。

### 运行配置

`run_config`参数让您可以为智能体运行配置一些全局设置：

#### 常见运行配置类别

使用`RunConfig`可以在不更改每个智能体定义的情况下覆盖单次运行的行为。

##### 模型、提供方和会话默认值

-   [`model`][agents.run.RunConfig.model]：允许设置要使用的全局LLM模型，而不受每个智能体各自`model`的影响。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认值为OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定设置。例如，您可以设置全局`temperature`或`top_p`。
-   [`session_settings`][agents.run.RunConfig.session_settings]：在运行期间检索历史记录时覆盖会话级默认值（例如`SessionSettings(limit=...)`）。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：使用Sessions时，在每一轮之前自定义如何将新的用户输入与会话历史合并。该回调可以是同步或异步的。

##### 安全防护措施、任务转移与模型输入整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：要包含在所有运行中的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：如果任务转移本身尚未设置输入过滤器，则应用于所有任务转移的全局输入过滤器。输入过滤器允许您编辑发送给新智能体的输入。更多详细信息请参阅[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter]中的文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：选择加入的beta功能，会在调用下一个智能体之前，将先前的对话记录折叠为一条assistant消息。在我们稳定嵌套任务转移期间，该功能默认禁用；设置为`True`可启用，或保持`False`以传递原始对话记录。当您未传入`RunConfig`时，所有[Runner方法][agents.run.Runner]都会自动创建一个，因此快速入门和代码示例会保持默认关闭，并且任何显式的[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter]回调仍会覆盖它。单个任务转移可以通过[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选可调用对象；每当您选择加入`nest_handoff_history`时，它都会接收规范化的对话记录（历史记录+任务转移项）。它必须返回要转发给下一个智能体的准确输入项列表，让您无需编写完整的任务转移过滤器即可替换内置摘要。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：用于在模型调用前立即编辑已完全准备好的模型输入（instructions和输入项）的钩子，例如修剪历史记录或注入系统提示词。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：控制运行器在将先前输出转换为下一轮模型输入时，是保留还是省略推理项ID。

##### 追踪与可观测性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许您为整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入[`TracingConfig`][agents.tracing.TracingConfig]以覆盖追踪导出设置，例如每次运行的追踪API密钥。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪是否包含潜在敏感数据，例如LLM和工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：设置该运行的追踪工作流名称、追踪ID和追踪组ID。我们建议至少设置`workflow_name`。组ID是一个可选字段，可让您关联多次运行的追踪。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪中的元数据。

##### 工具执行、审批与工具错误行为

-   [`tool_execution`][agents.run.RunConfig.tool_execution]：配置本地工具调用在SDK侧的执行行为，例如限制同时运行的工具调用数量。
-   [`tool_not_found_behavior`][agents.run.RunConfig.tool_not_found_behavior]：配置运行器如何处理模型发出的未解析函数工具调用。默认行为会抛出`ModelBehaviorError`；选择加入后可改为返回模型可见的错误输出。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：自定义模型可见的工具错误消息，例如审批拒绝和选择加入的“找不到工具”输出。

嵌套任务转移作为选择加入的beta功能提供。通过传入`RunConfig(nest_handoff_history=True)`启用折叠对话记录行为，或设置`handoff(..., nest_handoff_history=True)`以针对特定任务转移启用。如果您希望保留原始对话记录（默认行为），请保持该标志未设置，或提供一个`handoff_input_filter`（或`handoff_history_mapper`）按您所需的方式准确转发对话。若要更改生成摘要中使用的包装文本而无需编写自定义映射器，请调用[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并使用[`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]恢复默认值）。

#### 运行配置详情

##### `tool_execution`

当您希望配置本地工具调用在SDK侧的行为时，请使用`tool_execution`，例如限制一次运行中的本地工具调用并发数。

```python
from agents import Agent, RunConfig, Runner, ToolExecutionConfig

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Run the required tool calls.",
    run_config=RunConfig(
        tool_execution=ToolExecutionConfig(
            max_function_tool_concurrency=2,
            pre_approval_tool_input_guardrails=True,
        ),
    ),
)
```

`max_function_tool_concurrency=None`会保留默认行为：当模型在一轮中发出多个函数工具调用时，SDK会启动所有发出的本地函数工具调用。设置整数值可以限制这些本地函数工具一次运行的数量。

这与提供方侧的[`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls]是分开的。`parallel_tool_calls`控制模型是否允许在单个响应中发出多个工具调用。`tool_execution.max_function_tool_concurrency`控制模型发出本地函数工具调用后，SDK如何执行这些调用。

`pre_approval_tool_input_guardrails=False`会保留默认审批流程：如果某个函数工具需要审批，运行会先暂停，并且工具输入安全防护措施仅在审批之后、执行之前立即运行。当您希望在发出待审批中断之前运行函数工具输入安全防护措施时，请将其设置为`True`。通过此前置审批检查的调用，在审批之后仍会再次运行相同的输入安全防护措施，因此时间敏感的检查会在执行前重新验证。

##### `tool_not_found_behavior`

默认情况下，如果模型发出的函数工具调用与当前智能体可用的任何函数工具都不匹配，运行器会抛出`ModelBehaviorError`。

当您希望运行保持可恢复时，请设置`tool_not_found_behavior="return_error_to_model"`。在该模式下，SDK会为未解析的工具调用追加一个`function_call_output`并再次运行模型，这样模型就可以选择一个可用工具，或不使用该工具直接回答。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Handle this request with the available tools.",
    run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
)
```

此选项目前仅适用于未解析的函数工具调用。其他无效工具载荷会继续使用其现有错误行为。

##### `tool_error_formatter`

使用`tool_error_formatter`可以自定义SDK创建模型可见工具错误输出时返回给模型的消息。

格式化器会接收[`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]，其中包含：

-   `kind`：错误类别，例如`"approval_rejected"`或`"tool_not_found"`。
-   `tool_type`：工具运行时（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`或`"custom"`）。
-   `tool_name`：工具名称。
-   `call_id`：工具调用ID。
-   `default_message`：SDK默认的模型可见消息。
-   `run_context`：活动运行上下文包装器。

返回字符串可替换该消息，或返回`None`以使用SDK默认值。

```python
from agents import Agent, RunConfig, Runner, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind == "approval_rejected":
        return (
            f"Tool call '{args.tool_name}' was rejected by a human reviewer. "
            "Ask for confirmation or propose a safer alternative."
        )
    if args.kind == "tool_not_found":
        return f"Tool '{args.tool_name}' is not available. Choose one of the listed tools."
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

##### `reasoning_item_id_policy`

`reasoning_item_id_policy`控制运行器在向前携带历史记录时（例如使用`RunResult.to_input_list()`或由会话支持的运行），如何将推理项转换为下一轮模型输入。

-   `None`或`"preserve"`（默认）：保留推理项ID。
-   `"omit"`：从生成的下一轮输入中移除推理项ID。

使用`"omit"`主要是作为一种选择加入式缓解措施，用于处理一类Responses API 400错误：推理项带有`id`但缺少必需的后续项（例如`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

在多轮智能体运行中，当SDK根据先前输出构造后续输入（包括会话持久化、服务端管理的对话增量、流式/非流式后续轮次以及恢复路径）时，可能会发生这种情况：推理项ID被保留，但提供方要求该ID必须继续与其对应的后续项成对出现。

设置`reasoning_item_id_policy="omit"`会保留推理内容，但移除推理项`id`，从而避免在SDK生成的后续输入中触发该API不变量。

范围说明：

-   这只会改变SDK在构建后续输入时生成/转发的推理项。
-   它不会重写用户提供的初始输入项。
-   `call_model_input_filter`仍可以在应用此策略后有意重新引入推理ID。

## 状态与对话管理

### 内存策略选择

将状态带入下一轮有四种常见方式：

| 策略 | 状态所在位置 | 最适合 | 下一轮传入的内容 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 您的应用内存 | 小型聊天循环、完全手动控制、任何提供方 | 来自`result.to_input_list()`的列表加上下一条用户消息 |
| `session` | 您的存储加上SDK | 持久聊天状态、可恢复运行、自定义存储 | 相同的`session`实例，或指向同一存储的另一个实例 |
| `conversation_id` | OpenAI Conversations API | 您希望跨工作进程或服务共享的具名服务端对话 | 相同的`conversation_id`，外加仅新的用户轮次 |
| `previous_response_id` | OpenAI Responses API | 无需创建对话资源的轻量级服务端管理延续 | `result.last_response_id`，外加仅新的用户轮次 |

`result.to_input_list()`和`session`由客户端管理。`conversation_id`和`previous_response_id`由OpenAI管理，并且仅在您使用OpenAI Responses API时适用。在大多数应用中，每个对话请选择一种持久化策略。将客户端管理的历史记录与OpenAI管理的状态混用可能会导致上下文重复，除非您有意协调这两层。

!!! note

    会话持久化不能在同一次运行中与服务端管理的对话设置
    （`conversation_id`、`previous_response_id`或`auto_previous_response_id`）
    结合使用。每次调用请选择一种方法。

### 对话/聊天线程

调用任何run方法都可能导致一个或多个智能体运行（因此会有一次或多次LLM调用），但它表示聊天对话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner运行：第一个智能体调用LLM、运行工具、向第二个智能体执行任务转移，第二个智能体运行更多工具，然后生成输出。

在智能体运行结束时，您可以选择向用户展示什么。例如，您可以向用户展示智能体生成的每个新项，也可以只展示最终输出。无论哪种方式，用户随后都可能提出后续问题，在这种情况下您可以再次调用run方法。

#### 手动对话管理

您可以使用[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list]方法手动管理对话历史，以获取下一轮的输入：

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

#### 使用会话的自动对话管理

对于更简单的方法，您可以使用[会话](sessions/index.md)自动处理对话历史，而无需手动调用`.to_input_list()`：

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

会话会自动：

-   在每次运行前检索对话历史
-   在每次运行后存储新消息
-   为不同的会话ID维护独立对话

更多详细信息请参阅[会话文档](sessions/index.md)。


#### 服务端管理的对话

您也可以让OpenAI对话状态功能在服务端管理对话状态，而不是使用`to_input_list()`或`Sessions`在本地处理。这让您无需手动重新发送所有过去的消息即可保留对话历史。对于下面任一服务端管理方法，每次请求仅传入新轮次的输入，并复用保存的ID。更多详细信息请参阅[OpenAI对话状态指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI提供两种跨轮次跟踪状态的方式：

##### 1. `conversation_id`的使用

您首先使用OpenAI Conversations API创建一个对话，然后在之后的每次调用中复用其ID：

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

##### 2. `previous_response_id`的使用

另一种选项是**响应链式连接**，即每一轮都显式链接到上一轮的响应ID。

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

如果运行因审批而暂停，而您从[`RunState`][agents.run_state.RunState]恢复，SDK会保留保存的`conversation_id` / `previous_response_id` / `auto_previous_response_id`设置，因此恢复的轮次会继续处于同一个服务端管理的对话中。

`conversation_id`和`previous_response_id`互斥。当您希望使用可跨系统共享的具名对话资源时，请使用`conversation_id`。当您希望从一轮到下一轮使用最轻量的Responses API延续基础组件时，请使用`previous_response_id`。

!!! note

    SDK会自动以退避方式重试`conversation_locked`错误。在服务端管理的
    对话运行中，它会在重试前回退内部对话跟踪器输入，以便
    可以干净地重新发送相同的已准备项。

    在基于本地会话的运行中（不能与`conversation_id`、
    `previous_response_id`或`auto_previous_response_id`结合使用），SDK还会尽最大努力
    回滚最近持久化的输入项，以减少重试后的重复历史条目。

    即使您未配置`ModelSettings.retry`，也会发生此兼容性重试。对于
    模型请求上更广泛的选择加入式重试行为，请参阅[Runner管理的重试](models/index.md#runner-managed-retries)。

## 钩子与自定义

### 模型调用输入过滤器

使用`call_model_input_filter`可以在模型调用之前编辑模型输入。该钩子接收当前智能体、上下文以及合并后的输入项（如果存在会话历史，也包括在内），并返回新的`ModelInputData`。

返回值必须是[`ModelInputData`][agents.run.ModelInputData]对象。其`input`字段为必填项，且必须是输入项列表。返回任何其他形状都会抛出`UserError`。

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

运行器会将已准备输入列表的副本传递给钩子，因此您可以修剪、替换或重新排序它，而不会原地修改调用方的原始列表。

如果您正在使用会话，`call_model_input_filter`会在会话历史已加载并与当前轮次合并之后运行。当您希望自定义更早的合并步骤本身时，请使用[`session_input_callback`][agents.run.RunConfig.session_input_callback]。

如果您正在使用带有`conversation_id`、`previous_response_id`或`auto_previous_response_id`的OpenAI服务端管理对话状态，该钩子会在为下一次Responses API调用准备好的载荷上运行。该载荷可能已经只表示新轮次增量，而不是对早期历史的完整重放。只有您返回的项会被标记为已发送，用于该服务端管理的延续。

通过`run_config`为每次运行设置该钩子，以遮盖敏感数据、修剪过长的历史记录，或注入额外的系统指导。

## 错误与恢复

### 错误处理器

所有`Runner`入口点都接受`error_handlers`，这是一个以错误种类为键的字典。支持的键为`"max_turns"`和`"model_refusal"`。当您希望返回受控的最终输出，而不是抛出`MaxTurnsExceeded`或`ModelRefusalError`时，请使用它们。

```python
from agents import (
    Agent,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    Runner,
)

agent = Agent(name="Assistant", instructions="Be concise.")


def on_max_turns(_data: RunErrorHandlerInput[None]) -> RunErrorHandlerResult:
    return RunErrorHandlerResult(
        final_output="I couldn't finish within the turn limit. Please narrow the request.",
        include_in_history=False,
    )


result = Runner.run_sync(
    agent,
    "Analyze this long transcript",
    max_turns=3,
    error_handlers={"max_turns": on_max_turns},
)
print(result.final_output)
```

当您不希望将回退输出追加到对话历史时，请设置`include_in_history=False`。

当模型拒绝应生成特定于应用的回退结果，而不是以`ModelRefusalError`结束运行时，请使用`"model_refusal"`。

```python
from pydantic import BaseModel

from agents import Agent, ModelRefusalError, RunErrorHandlerInput, Runner


class Recipe(BaseModel):
    ingredients: list[str]
    refusal_reason: str | None = None


def on_model_refusal(data: RunErrorHandlerInput[None]) -> Recipe:
    assert isinstance(data.error, ModelRefusalError)
    return Recipe(ingredients=[], refusal_reason=data.error.refusal)


agent = Agent(
    name="Recipe assistant",
    instructions="Return a structured recipe.",
    output_type=Recipe,
)

result = Runner.run_sync(
    agent,
    "Make me something unsafe.",
    error_handlers={"model_refusal": on_model_refusal},
)
print(result.final_output)
```

## 持久执行集成与人在环路

对于工具审批暂停/恢复模式，请从专门的[人在环路指南](human_in_the_loop.md)开始。下面的集成用于持久编排，适合运行可能跨越长时间等待、重试或进程重启的场景。

### Dapr

您可以使用Agents SDK的[Dapr](https://dapr.io) Diagrid集成来运行持久、长时间运行的智能体，这些智能体支持人在环路并可自动从故障中恢复。Dapr是一个厂商中立的[CNCF](https://cncf.io)工作流编排器。请从[这里](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai)开始使用Dapr和OpenAI智能体。

### Temporal

您可以使用Agents SDK的[Temporal](https://temporal.io/)集成来运行持久、长时间运行的工作流，包括人在环路任务。请在[这个视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中查看Temporal与Agents SDK协同完成长时间运行任务的演示，并[在这里查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

### Restate

您可以使用Agents SDK的[Restate](https://restate.dev/)集成来构建轻量、持久的智能体，包括人工审批、任务转移和会话管理。该集成需要Restate的单二进制运行时作为依赖，并支持将智能体作为进程/容器或serverless函数运行。更多详细信息请阅读[概览](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)或查看[文档](https://docs.restate.dev/ai)。

### DBOS

您可以使用Agents SDK的[DBOS](https://dbos.dev/)集成来运行可靠的智能体，在故障和重启期间保留进度。它支持长时间运行的智能体、人在环路工作流和任务转移。它同时支持同步和异步方法。更多详细信息请查看集成[仓库](https://github.com/dbos-inc/dbos-openai-agents)和[文档](https://docs.dbos.dev/integrations/openai-agents)。

## 异常

SDK在某些情况下会抛出异常。完整列表位于[`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：这是SDK内抛出的所有异常的基类。它作为一个通用类型，所有其他特定异常都派生自它。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传递给`Runner.run`、`Runner.run_sync`或`Runner.run_streamed`方法的`max_turns`限制时，会抛出此异常。它表示智能体无法在指定的交互轮次数内完成任务。设置`max_turns=None`可禁用该限制。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）生成意外或无效输出时，会发生此异常。这可能包括：
    -   格式错误的JSON：当模型为工具调用或在其直接输出中提供格式错误的JSON结构时，尤其是在定义了特定`output_type`的情况下。
    -   意外的工具相关失败：当模型未能以预期方式使用工具时
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：当函数工具调用超过其配置的超时时间，且该工具使用`timeout_behavior="raise_exception"`时，会抛出此异常。
-   [`UserError`][agents.exceptions.UserError]：当您（使用SDK编写代码的人）在使用SDK时出错，会抛出此异常。这通常源于不正确的代码实现、无效配置或对SDK API的误用。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入安全防护措施或输出安全防护措施的条件分别被满足时，会抛出此异常。输入安全防护措施在处理前检查传入消息，而输出安全防护措施在交付前检查智能体的最终响应。