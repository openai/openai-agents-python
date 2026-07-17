---
search:
  exclude: true
---
# 智能体运行

你可以通过[`Runner`][agents.run.Runner]类运行智能体。有以下 3 种方式：

1. [`Runner.run()`][agents.run.Runner.run]：异步运行并返回[`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，底层仅调用`.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：异步运行并返回[`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式传输模式调用 LLM，并在收到事件时将其流式传输给你。

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

更多信息请参阅[结果指南](results.md)。

## Runner 生命周期与配置

### 智能体循环

使用`Runner`中的运行方法时，需要传入起始智能体和输入。输入可以是：

-   字符串（视为用户消息），
-   OpenAI Responses API格式的输入项列表，或
-   恢复中断的运行时使用的[`RunState`][agents.run_state.RunState]。

随后，Runner 会运行一个循环：

1. 使用当前输入调用当前智能体的 LLM。
2. LLM 生成输出。
    1. 如果 LLM 返回`final_output`，循环结束并返回结果。
    2. 如果 LLM 执行任务转移，则更新当前智能体和输入，然后重新运行循环。
    3. 如果 LLM 生成工具调用，则运行这些工具调用、追加结果，然后重新运行循环。
3. 如果超过传入的`max_turns`，则抛出[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]异常。传入`max_turns=None`可禁用此轮次限制。

!!! note

    判断 LLM 输出是否为“最终输出”的规则是：它生成了所需类型的文本输出，并且不存在工具调用。

### 流式传输

流式传输允许你在 LLM 运行时额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming]将包含此次运行的完整信息，包括生成的所有新输出。你可以调用`.stream_events()`获取流式事件。更多信息请参阅[流式传输指南](streaming.md)。

#### Responses WebSocket 传输（可选辅助工具）

如果启用 OpenAI Responses WebSocket 传输，你仍可继续使用常规`Runner` API。建议使用 WebSocket 会话辅助工具来复用连接，但这并非必需。

这是通过 WebSocket 传输使用 Responses API，而不是[Realtime API](realtime/guide.md)。

有关传输选择规则，以及具体模型对象或自定义提供商的注意事项，请参阅[模型](models/index.md#responses-websocket-transport)。

##### 模式 1：不使用会话辅助工具（可用）

如果你只需要 WebSocket 传输，并且不需要 SDK 为你管理共享提供商/会话，请使用此模式。

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

此模式适用于单次运行。如果反复调用`Runner.run()` / `Runner.run_streamed()`，每次运行都可能重新连接，除非你手动复用同一个`RunConfig` / 提供商实例。

##### 模式 2：使用`responses_websocket_session()`（推荐用于多轮复用）

如果希望在多次运行之间共享支持 WebSocket 的提供商和`RunConfig`，请使用[`responses_websocket_session()`][agents.responses_websocket_session]，这也包括继承同一`run_config`的嵌套“智能体作为工具”调用。

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

请在上下文退出前完成流式结果的消费。如果在 WebSocket 请求仍在进行时退出上下文，可能会强制关闭共享连接。

如果较长的推理轮次触发 WebSocket 保活超时，请增大`ping_timeout`，或将`ping_timeout=None`设置为禁用心跳超时。对于可靠性比 WebSocket 延迟更重要的运行，请使用 HTTP/SSE 传输。

### 运行配置

`run_config`参数可用于配置智能体运行的一些全局设置：

#### 常见运行配置目录

使用`RunConfig`可覆盖单次运行的行为，而无需更改各个智能体的定义。

##### 模型、提供商和会话默认设置

-   [`model`][agents.run.RunConfig.model]：允许设置要使用的全局 LLM 模型，而不考虑每个智能体的`model`设置。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供商，默认为OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定的设置。例如，可以设置全局`temperature`或`top_p`。
-   [`session_settings`][agents.run.RunConfig.session_settings]：在运行期间检索历史记录时，覆盖会话级默认设置（例如`SessionSettings(limit=...)`）。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：使用 Sessions 时，自定义每轮开始前将新用户输入与会话历史记录合并的方式。该回调可以是同步或异步的。

##### 安全防护措施、任务转移和模型输入调整

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：要包含在所有运行中的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器，前提是该任务转移尚未设置过滤器。输入过滤器允许你编辑发送给新智能体的输入。更多详细信息请参阅[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter]的文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：一项可选择启用的 Beta 功能。在调用下一个智能体之前，它会将可摘要的历史记录压缩为有序的助手摘要片段，同时将无损消息项保留在其原始位置。在我们完善嵌套任务转移期间，此功能默认禁用；设置为`True`可启用，保持`False`则会直接传递原始记录。Sessions、`RunState`和`RunResult.to_input_list()`会避免重复追加完全相同的消息实例（当 SDK 默认的嵌套历史记录已包含该消息时），同时保留彼此独立但内容相同的消息。如果未传入`RunConfig`，所有[Runner 方法][agents.run.Runner]都会自动创建一个，因此快速入门和代码示例会保持默认关闭状态，而任何显式的[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter]回调仍会覆盖此设置。单个任务转移可通过[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的可调用对象。当你选择启用`nest_handoff_history`时，它会接收规范化的记录（历史记录 + 任务转移项）。它必须返回要转发给下一个智能体的确切输入项列表，以替换内置的有序摘要片段，而无需编写完整的任务转移过滤器。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：在调用模型前立即编辑已完整准备的模型输入（instructions 和输入项）的钩子，例如裁剪历史记录或注入系统提示词。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：控制 Runner 将先前输出转换为下一轮模型输入时，是保留还是省略推理项 ID。

##### 追踪与可观测性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许为整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入[`TracingConfig`][agents.tracing.TracingConfig]可覆盖追踪导出设置，例如每次运行所使用的追踪 API 密钥。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在敏感数据，例如 LLM 和工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：设置此次运行的追踪工作流名称、追踪 ID 和追踪组 ID。建议至少设置`workflow_name`。组 ID 是可选字段，可用于关联多次运行的追踪。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪中的元数据。

##### 工具执行、审批和工具错误行为

-   [`tool_execution`][agents.run.RunConfig.tool_execution]：配置本地工具调用在 SDK 端的执行行为，例如限制同时运行的工具调用数量。
-   [`tool_not_found_behavior`][agents.run.RunConfig.tool_not_found_behavior]：配置 Runner 如何处理模型生成但无法解析的工具调用。默认行为是抛出`ModelBehaviorError`；也可选择改为返回模型可见的错误输出。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：自定义模型可见的工具错误消息，例如审批被拒绝，以及选择启用后返回的工具未找到输出。

嵌套任务转移是一项可选择启用的 Beta 功能。传入`RunConfig(nest_handoff_history=True)`可启用有序记录压缩，也可以设置`handoff(..., nest_handoff_history=True)`，仅为特定任务转移启用此功能。内置映射器会在无损消息项前后放置生成的助手摘要片段，而不是将整个记录压缩为一条消息。如果希望保留原始记录（默认行为），请不要设置该标志，或者提供一个`handoff_input_filter`（或`handoff_history_mapper`），按需准确转发对话。如果想更改生成摘要片段时使用的包装文本，而不编写自定义映射器，请调用[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（调用[`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]可恢复默认值）。

#### 运行配置详情

##### `tool_execution`

如果希望配置本地工具调用在 SDK 端的行为，例如限制单次运行中本地工具调用的并发量，请使用`tool_execution`。

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

`max_function_tool_concurrency=None`会保留默认行为：当模型在一轮中生成多个工具调用时，SDK 会启动所有已生成的本地工具调用。将其设置为整数值，可限制同时运行的本地工具调用数量。

这与提供商端的[`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls]相互独立。`parallel_tool_calls`控制是否允许模型在单个响应中生成多个工具调用。`tool_execution.max_function_tool_concurrency`控制模型生成这些调用后，SDK 如何执行本地工具调用。

`pre_approval_tool_input_guardrails=False`会保留默认审批流程：如果工具调用需要审批，运行会先暂停，工具输入安全防护措施仅在审批完成后、执行前立即运行。如果希望工具调用输入安全防护措施在发出待审批中断之前运行，请将其设置为`True`。通过此次审批前检查的调用，在审批后仍会再次运行相同的输入安全防护措施，从而在执行前重新验证时效性检查。

##### `tool_not_found_behavior`

默认情况下，如果模型生成的工具调用与当前智能体可用的任何工具调用都不匹配，Runner 会抛出`ModelBehaviorError`。

如果希望运行仍可恢复，请设置`tool_not_found_behavior="return_error_to_model"`。在此模式下，SDK 会为未解析的工具调用追加`function_call_output`，并再次运行模型，使模型可以选择可用工具，或在不使用该工具的情况下回答。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Handle this request with the available tools.",
    run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
)
```

此选项目前仅适用于未解析的工具调用。其他无效工具载荷仍沿用其现有的错误处理行为。

##### `tool_error_formatter`

当 SDK 创建模型可见的工具错误输出时，可使用`tool_error_formatter`自定义返回给模型的消息。

格式化程序会接收包含以下字段的[`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]：

-   `kind`：错误目录，例如`"approval_rejected"`或`"tool_not_found"`。
-   `tool_type`：工具运行时（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`或`"custom"`）。
-   `tool_name`：工具名称。
-   `call_id`：工具调用 ID。
-   `default_message`：SDK 默认的模型可见消息。
-   `run_context`：当前运行上下文包装器。

返回字符串可替换该消息，返回`None`则使用 SDK 默认值。

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

`reasoning_item_id_policy`控制 Runner 向前传递历史记录时，如何将推理项转换为下一轮模型输入（例如使用`RunResult.to_input_list()`或由会话支持的运行时）。

-   `None`或`"preserve"`（默认）：保留推理项 ID。
-   `"omit"`：从生成的下一轮输入中移除推理项 ID。

`"omit"`主要用于选择启用针对一类 Responses API 400 错误的缓解措施：推理项带有`id`发送，但没有必需的后续项（例如`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

在多轮智能体运行中，如果 SDK 根据先前的输出构造后续输入（包括会话持久化、由服务端管理的对话增量、流式/非流式后续轮次和恢复路径），并且保留了推理项 ID，但提供商要求该 ID 必须与其对应的后续项配对，就可能发生这种情况。

设置`reasoning_item_id_policy="omit"`会保留推理内容，但移除推理项的`id`，从而避免在 SDK 生成的后续输入中触发该 API 不变量。

适用范围说明：

-   这只会更改 SDK 构建后续输入时生成/转发的推理项。
-   它不会重写用户提供的初始输入项。
-   应用此策略后，`call_model_input_filter`仍可有意重新引入推理 ID。

## 状态与对话管理

### 记忆策略选择

有四种常见方式可以将状态传递到下一轮：

| 策略 | 状态存储位置 | 最适用场景 | 下一轮传入的内容 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 应用内存 | 小型聊天循环、完全手动控制、任意提供商 | `result.to_input_list()`返回的列表，加上下一条用户消息 |
| `session` | 你的存储加 SDK | 持久化聊天状态、可恢复运行、自定义存储 | 同一个`session`实例，或指向同一存储的另一个实例 |
| `conversation_id` | OpenAI Conversations API | 希望在不同工作进程或服务间共享的命名服务端对话 | 同一个`conversation_id`，加上仅包含新用户轮次的内容 |
| `previous_response_id` | OpenAI Responses API | 无需创建对话资源的轻量级服务端托管延续 | `result.last_response_id`，加上仅包含新用户轮次的内容 |

`result.to_input_list()`和`session`由客户端管理。`conversation_id`和`previous_response_id`由OpenAI管理，并且仅适用于使用 OpenAI Responses API的情况。对于大多数应用程序，每个对话应选择一种持久化策略。混合使用客户端管理的历史记录和OpenAI管理的状态可能导致上下文重复，除非你有意协调这两个层级。

!!! note

    同一次运行中，无法同时使用会话持久化与服务端管理的对话设置
    （`conversation_id`、`previous_response_id`或`auto_previous_response_id`）。
    每次调用请选择一种方式。

### 对话/聊天线程

调用任一运行方法都可能导致一个或多个智能体运行（因此会进行一次或多次 LLM 调用），但在聊天对话中，这表示一个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM、运行工具、将任务转移给第二个智能体；第二个智能体运行更多工具，然后生成输出。

智能体运行结束时，你可以选择向用户展示哪些内容。例如，可以向用户展示智能体生成的每个新项目，也可以只展示最终输出。无论采用哪种方式，用户都可能继续提出后续问题，此时可以再次调用运行方法。

#### 手动对话管理

你可以使用[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list]方法手动管理对话历史记录，以获取下一轮的输入：

```python
from agents import Agent, Runner, trace

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

#### 使用会话自动管理对话

如果希望采用更简单的方法，可以使用[Sessions](sessions/index.md)自动处理对话历史记录，而无需手动调用`.to_input_list()`：

```python
from agents import Agent, Runner, SQLiteSession, trace

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

-   在每次运行前检索对话历史记录
-   在每次运行后存储新消息
-   为不同的会话 ID 维护独立对话

更多详细信息请参阅[Sessions 文档](sessions/index.md)。


#### 服务端管理的对话

你也可以让OpenAI对话状态功能在服务端管理对话状态，而不是使用`to_input_list()`或`Sessions`在本地处理。这样无需手动重新发送所有历史消息，即可保留对话历史记录。使用下述任一服务端管理方式时，每次请求仅传入新轮次的输入，并复用已保存的 ID。更多详细信息请参阅[OpenAI对话状态指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI提供两种跨轮次追踪状态的方式：

##### 1. 使用`conversation_id`

首先使用 OpenAI Conversations API创建对话，然后在后续每次调用中复用其 ID：

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

##### 2. 使用`previous_response_id`

另一种选择是**响应链接**，其中每一轮都显式链接到上一轮的响应 ID。

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

如果运行因审批而暂停，并且你从[`RunState`][agents.run_state.RunState]恢复，SDK 会保留已保存的`conversation_id` / `previous_response_id` / `auto_previous_response_id`设置，使恢复后的轮次继续使用同一个服务端管理的对话。

`conversation_id`和`previous_response_id`互斥。如果需要可跨系统共享的命名对话资源，请使用`conversation_id`。如果需要从一轮到下一轮最轻量的 Responses API延续基本组件，请使用`previous_response_id`。

!!! note

    SDK 会自动以退避方式重试`conversation_locked`错误。在服务端管理的
    对话运行中，它会在重试前回退内部对话跟踪器输入，以便干净地重新发送
    相同的已准备项目。

    在基于本地会话的运行中（无法与`conversation_id`、
    `previous_response_id`或`auto_previous_response_id`结合使用），SDK 还会尽最大努力
    回滚最近持久化的输入项，以减少重试后重复的历史记录条目。

    即使未配置`ModelSettings.retry`，也会进行此兼容性重试。有关
    更广泛、可选择启用的模型请求重试行为，请参阅[Runner 管理的重试](models/index.md#runner-managed-retries)。

## 钩子与自定义

### 模型调用输入过滤器

使用`call_model_input_filter`可在调用模型前编辑模型输入。该钩子会接收当前智能体、上下文和合并后的输入项（包括存在的会话历史记录），并返回新的`ModelInputData`。

返回值必须是[`ModelInputData`][agents.run.ModelInputData]对象。其`input`字段为必填项，并且必须是输入项列表。返回任何其他结构都会抛出`UserError`。

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

Runner 会将已准备输入列表的副本传递给钩子，因此你可以裁剪、替换或重新排序该列表，而不会原地修改调用方的原始列表。

如果使用会话，`call_model_input_filter`会在会话历史记录已加载并与当前轮次合并后运行。如果希望自定义此前的合并步骤本身，请使用[`session_input_callback`][agents.run.RunConfig.session_input_callback]。

如果使用带有`conversation_id`、`previous_response_id`或`auto_previous_response_id`的OpenAI服务端管理对话状态，该钩子会针对下一次 Responses API调用所准备的载荷运行。该载荷可能已经仅表示新轮次的增量，而不是完整重放先前的历史记录。只有你返回的项目才会被标记为已针对该服务端管理的延续发送。

通过`run_config`为每次运行设置该钩子，以编辑敏感数据、裁剪过长的历史记录或注入额外的系统指导。

## 错误与恢复

### 错误处理程序

所有`Runner`入口点都接受`error_handlers`，这是一个以错误类型为键的字典。支持的键包括`"max_turns"`、`"model_refusal"`和`"invalid_final_output"`。如果希望返回受控的最终输出，而不是以相应错误结束运行，请使用这些处理程序。

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

当模型消息无法通过智能体结构化`output_type`的验证，或模型未返回结构化最终消息时，请使用`"invalid_final_output"`。处理程序可以返回应用程序特定的回退值，SDK 会使用相同的`output_type`对其进行验证。它不会重试模型调用，也不会重新执行任何工具副作用。返回`None`表示放弃恢复。如果没有回退值，非空验证失败仍会抛出`ModelBehaviorError`，而空的结构化响应则保留现有的下一轮行为。

```python
from pydantic import BaseModel

from agents import Agent, ModelBehaviorError, RunErrorHandlerInput, Runner


class Recipe(BaseModel):
    ingredients: list[str]
    recovered_from_invalid_output: bool = False


def on_invalid_final_output(data: RunErrorHandlerInput[None]) -> Recipe:
    assert isinstance(data.error, ModelBehaviorError)
    return Recipe(ingredients=[], recovered_from_invalid_output=True)


agent = Agent(
    name="Recipe assistant",
    instructions="Return a structured recipe.",
    output_type=Recipe,
)

result = Runner.run_sync(
    agent,
    "Plan tonight's dinner.",
    error_handlers={"invalid_final_output": on_invalid_final_output},
)
print(result.final_output)
```

如果不希望将回退输出追加到对话历史记录，请设置`include_in_history=False`。

如果希望模型拒绝时生成应用程序特定的回退值，而不是以`ModelRefusalError`结束运行，请使用`"model_refusal"`。

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

## 持久执行集成与人工介入

有关工具审批暂停/恢复模式，请先参阅专门的[人工介入指南](human_in_the_loop.md)。以下集成适用于持久编排，运行过程可能包含长时间等待、重试或进程重启。

### Dapr

你可以使用Agents SDK的[Dapr](https://dapr.io) Diagrid 集成来运行持久、长时间运行的智能体；这些智能体支持人工介入，并能自动从故障中恢复。Dapr 是一个与供应商无关的[CNCF](https://cncf.io)工作流编排器。点击[此处](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai)开始使用 Dapr 和OpenAI智能体。

### Temporal

你可以使用Agents SDK的[Temporal](https://temporal.io/)集成来运行持久、长时间运行的工作流，包括人工介入任务。可在[此视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中观看 Temporal 与Agents SDK协同完成长时间运行任务的演示，并可在[此处查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

### Restate

你可以使用Agents SDK的[Restate](https://restate.dev/)集成来实现轻量级、持久的智能体，包括人工审批、任务转移和会话管理。该集成依赖 Restate 的单一二进制运行时，并支持将智能体作为进程/容器或 Serverless 函数运行。更多详细信息请阅读[概述](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)或查看[文档](https://docs.restate.dev/ai)。

### DBOS

你可以使用Agents SDK的[DBOS](https://dbos.dev/)集成运行可靠的智能体，在发生故障和重启时保留进度。它支持长时间运行的智能体、人工介入工作流和任务转移，也支持同步和异步方法。该集成仅需要 SQLite 或 Postgres 数据库。更多详细信息请查看集成[仓库](https://github.com/dbos-inc/dbos-openai-agents)和[文档](https://docs.dbos.dev/integrations/openai-agents)。

## 异常

SDK 会在特定情况下抛出异常。完整列表位于[`agents.exceptions`][]中。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：这是 SDK 内抛出的所有异常的基类。它是一种通用类型，所有其他特定异常均派生自该类。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传给`Runner.run`、`Runner.run_sync`或`Runner.run_streamed`方法的`max_turns`限制时，会抛出此异常。它表示智能体无法在指定的交互轮数内完成任务。设置`max_turns=None`可禁用该限制。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）生成意外或无效的输出时，会发生此异常。这可能包括：
    -   JSON 格式错误：模型为工具调用或直接输出提供格式错误的 JSON 结构，尤其是在定义了特定`output_type`的情况下。
    -   意外的工具相关故障：模型未按预期方式使用工具
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：当工具调用超过其配置的超时时间，并且该工具使用`timeout_behavior="raise_exception"`时，会抛出此异常。
-   [`UserError`][agents.exceptions.UserError]：当你（编写使用 SDK 的代码的人）在使用 SDK 时出错，会抛出此异常。这通常由错误的代码实现、无效配置或误用 SDK API 导致。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：分别在满足输入安全防护措施或输出安全防护措施的条件时抛出此异常。输入安全防护措施会在处理前检查传入消息，而输出安全防护措施会在交付前检查智能体的最终响应。
