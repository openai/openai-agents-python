---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类运行智能体。你有 3 个选项：

1. [`Runner.run()`][agents.run.Runner.run]，异步运行并返回 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]，这是一个同步方法，底层只是运行 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，异步运行并返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式传输模式调用 LLM，并在事件收到时将这些事件流式传输给你。

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

## Runner 生命周期和配置

### 智能体循环

当你使用 `Runner` 中的 run 方法时，需要传入一个起始智能体和输入。输入可以是：

- 一个字符串（视为用户消息），
- OpenAI Responses API 格式的输入项列表，或
- 在恢复中断的运行时使用的 [`RunState`][agents.run_state.RunState]。

然后 runner 会运行一个循环：

1. 我们使用当前输入为当前智能体调用 LLM。
2. LLM 生成其输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 执行任务转移，我们会更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 生成工具调用，我们会运行这些工具调用、追加结果，并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会引发 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。传入 `max_turns=None` 可禁用此轮次限制。

!!! note

    判断 LLM 输出是否被视为“最终输出”的规则是：它生成了所需类型的文本输出，并且没有工具调用。

### 流式传输

流式传输允许你在 LLM 运行时额外接收流式事件。流完成后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含有关该运行的完整信息，包括生成的所有新输出。你可以调用 `.stream_events()` 获取流式事件。在[流式传输指南](streaming.md)中阅读更多内容。

#### Responses WebSocket 传输（可选辅助工具）

如果你启用 OpenAI Responses websocket 传输，可以继续使用普通的 `Runner` API。建议使用 websocket 会话辅助工具来复用连接，但这不是必需的。

这是基于 websocket 传输的 Responses API，而不是 [Realtime API](realtime/guide.md)。

有关传输选择规则以及具体模型对象或自定义提供方的注意事项，请参阅[模型](models/index.md#responses-websocket-transport)。

##### 模式 1：无会话辅助工具（可用）

当你只需要 websocket 传输，并且不需要 SDK 为你管理共享提供方/会话时，请使用此模式。

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

此模式适合单次运行。如果你反复调用 `Runner.run()` / `Runner.run_streamed()`，除非手动复用同一个 `RunConfig` / 提供方实例，否则每次运行都可能重新连接。

##### 模式 2：使用 `responses_websocket_session()`（推荐用于多轮复用）

当你希望在多次运行中共享支持 websocket 的提供方和 `RunConfig`（包括继承相同 `run_config` 的嵌套 agent-as-tool 调用）时，请使用 [`responses_websocket_session()`][agents.responses_websocket_session]。

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

在上下文退出前完成对流式结果的消费。如果在 websocket 请求仍在进行时退出上下文，可能会强制关闭共享连接。

如果长推理轮次触发 websocket keepalive 超时，请增加 `ping_timeout` 或设置 `ping_timeout=None` 以禁用心跳超时。对于可靠性比 websocket 延迟更重要的运行，请使用 HTTP/SSE 传输。

### 运行配置

`run_config` 参数允许你为智能体运行配置一些全局设置：

#### 常见运行配置目录

使用 `RunConfig` 可在不更改每个智能体定义的情况下覆盖单次运行的行为。

##### 模型、提供方和会话默认值

- [`model`][agents.run.RunConfig.model]：允许设置要使用的全局 LLM 模型，而不考虑每个 Agent 拥有的 `model`。
- [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型提供方，默认值为 OpenAI。
- [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定的设置。例如，你可以设置全局 `temperature` 或 `top_p`。
- [`session_settings`][agents.run.RunConfig.session_settings]：在运行期间检索历史记录时覆盖会话级默认值（例如 `SessionSettings(limit=...)`）。
- [`session_input_callback`][agents.run.RunConfig.session_input_callback]：使用 Sessions 时，自定义每轮之前新用户输入与会话历史合并的方式。回调可以是同步或异步的。

##### 安全防护措施、任务转移和模型输入塑形

- [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：要包含在所有运行中的输入或输出安全防护措施列表。
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器，前提是该任务转移尚未有一个过滤器。输入过滤器允许你编辑发送给新智能体的输入。有关更多详细信息，请参阅 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 中的文档。
- [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：选择启用的 beta 功能，会在调用下一个智能体之前，将先前的转录折叠为一条 assistant 消息。在我们稳定嵌套任务转移期间，此功能默认禁用；设置为 `True` 以启用，或保留 `False` 以传递原始转录。当你未传入 `RunConfig` 时，所有 [Runner 方法][agents.run.Runner]都会自动创建一个 `RunConfig`，因此快速入门和示例会保持默认关闭，并且任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调都会继续覆盖它。单个任务转移可以通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
- [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的可调用对象，当你选择启用 `nest_handoff_history` 时，它会接收规范化转录（历史记录 + 任务转移项）。它必须返回要转发给下一个智能体的确切输入项列表，允许你替换内置摘要，而无需编写完整的任务转移过滤器。
- [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：用于在模型调用前立即编辑完全准备好的模型输入（instructions 和输入项）的钩子，例如修剪历史记录或注入系统提示词。
- [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：控制 runner 将先前输出转换为下一轮模型输入时，是保留还是省略 reasoning item ID。

##### 追踪和可观测性

- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许你对整个运行禁用[追踪](tracing.md)。
- [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig] 以覆盖 trace 导出设置，例如每次运行的追踪 API 密钥。
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置 trace 是否包含可能敏感的数据，例如 LLM 和工具调用的输入/输出。
- [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：设置该运行的追踪工作流名称、trace ID 和 trace group ID。我们建议至少设置 `workflow_name`。group ID 是一个可选字段，可让你将多次运行的 trace 关联起来。
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有 trace 中的元数据。

##### 工具执行、审批和工具错误行为

- [`tool_execution`][agents.run.RunConfig.tool_execution]：为本地工具调用配置 SDK 端执行行为，例如限制同时运行的 function tools 数量。
- [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：自定义在审批流程中拒绝工具调用时模型可见的消息。

嵌套任务转移作为选择启用的 beta 功能提供。通过传入 `RunConfig(nest_handoff_history=True)` 启用折叠转录行为，或设置 `handoff(..., nest_handoff_history=True)` 为特定任务转移启用它。如果你希望保留原始转录（默认），请保持该标志未设置，或提供一个 `handoff_input_filter`（或 `handoff_history_mapper`）来按你的需求精确转发对话。若要更改生成摘要中使用的包装文本而不编写自定义 mapper，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（以及 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 以恢复默认值）。

#### 运行配置详情

##### `tool_execution`

当你希望 SDK 限制某次运行中的本地 function-tool 并发时，请使用 `tool_execution`。

```python
from agents import Agent, RunConfig, Runner, ToolExecutionConfig

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Run the required tool calls.",
    run_config=RunConfig(
        tool_execution=ToolExecutionConfig(max_function_tool_concurrency=2),
    ),
)
```

`max_function_tool_concurrency=None` 会保留默认行为：当模型在一轮中发出多个 function tool 调用时，SDK 会启动所有发出的本地 function tool 调用。设置一个整数值可限制这些本地 function tools 同时运行的数量。

这与提供方侧的 [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] 是分开的。`parallel_tool_calls` 控制模型是否允许在单个响应中发出多个工具调用。`tool_execution.max_function_tool_concurrency` 控制 SDK 在模型发出本地 function tool 调用后如何执行它们。

##### `tool_error_formatter`

使用 `tool_error_formatter` 可自定义在审批流程中拒绝工具调用时返回给模型的消息。

formatter 接收 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]，其中包含：

- `kind`：错误目录。当前为 `"approval_rejected"`。
- `tool_type`：工具运行时（`"function"`、`"computer"`、`"shell"`、`"apply_patch"` 或 `"custom"`）。
- `tool_name`：工具名称。
- `call_id`：工具调用 ID。
- `default_message`：SDK 默认的模型可见消息。
- `run_context`：活动运行上下文包装器。

返回字符串以替换消息，或返回 `None` 使用 SDK 默认值。

```python
from agents import Agent, RunConfig, Runner, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind == "approval_rejected":
        return (
            f"Tool call '{args.tool_name}' was rejected by a human reviewer. "
            "Ask for confirmation or propose a safer alternative."
        )
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

##### `reasoning_item_id_policy`

`reasoning_item_id_policy` 控制当 runner 携带历史记录向前推进时（例如使用 `RunResult.to_input_list()` 或基于会话的运行时），reasoning items 如何转换为下一轮模型输入。

- `None` 或 `"preserve"`（默认）：保留 reasoning item ID。
- `"omit"`：从生成的下一轮输入中移除 reasoning item ID。

主要将 `"omit"` 用作选择启用的缓解措施，用于处理一类 Responses API 400 错误：reasoning item 带有 `id` 发送，但没有所需的后续项（例如，`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

在多轮智能体运行中，如果 SDK 从先前输出构造后续输入（包括会话持久化、服务管理的对话增量、流式/非流式后续轮次以及恢复路径），并且保留了 reasoning item ID，但提供方要求该 ID 与其对应的后续项保持配对，就可能发生这种情况。

设置 `reasoning_item_id_policy="omit"` 会保留 reasoning 内容，但移除 reasoning item 的 `id`，从而避免在 SDK 生成的后续输入中触发该 API 不变量。

范围说明：

- 这只会更改 SDK 在构建后续输入时生成/转发的 reasoning items。
- 它不会重写用户提供的初始输入项。
- 在应用此策略后，`call_model_input_filter` 仍可有意重新引入 reasoning ID。

## 状态和对话管理

### 记忆策略选择

有四种常见方式可以将状态带入下一轮：

| 策略 | 状态所在位置 | 最适合 | 下一轮传入的内容 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 你的应用内存 | 小型聊天循环、完全手动控制、任何提供方 | 来自 `result.to_input_list()` 的列表加上下一个用户消息 |
| `session` | 你的存储加上 SDK | 持久聊天状态、可恢复运行、自定义存储 | 相同的 `session` 实例，或指向同一存储的另一个实例 |
| `conversation_id` | OpenAI Conversations API | 你希望跨工作器或服务共享的具名服务端对话 | 相同的 `conversation_id`，并且只加上新的用户轮次 |
| `previous_response_id` | OpenAI Responses API | 无需创建对话资源的轻量级服务管理延续 | `result.last_response_id`，并且只加上新的用户轮次 |

`result.to_input_list()` 和 `session` 由客户端管理。`conversation_id` 和 `previous_response_id` 由 OpenAI 管理，并且仅在你使用 OpenAI Responses API 时适用。在大多数应用中，每个对话选择一种持久化策略。混合客户端管理的历史与 OpenAI 管理的状态可能会导致上下文重复，除非你有意协调这两层。

!!! note

    会话持久化不能与服务管理的对话设置
    （`conversation_id`、`previous_response_id` 或 `auto_previous_response_id`）在同一次运行中组合使用。
    每次调用请选择一种方法。

### 对话/聊天线程

调用任何运行方法都可能导致一个或多个智能体运行（因此也会有一次或多次 LLM 调用），但它表示聊天对话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM、运行工具、向第二个智能体执行任务转移；第二个智能体运行更多工具，然后生成输出。

在智能体运行结束时，你可以选择向用户展示什么。例如，你可以向用户展示智能体生成的每个新项，或者只展示最终输出。无论哪种方式，用户随后都可能提出后续问题，在这种情况下你可以再次调用 run 方法。

#### 手动对话管理

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法手动管理对话历史，以获取下一轮的输入：

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

#### 使用 Sessions 自动管理对话

为了更简单的方法，你可以使用 [Sessions](sessions/index.md) 自动处理对话历史，而无需手动调用 `.to_input_list()`：

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

- 在每次运行前检索对话历史
- 在每次运行后存储新消息
- 为不同的 session ID 维护独立对话

更多详细信息请参阅 [Sessions 文档](sessions/index.md)。


#### 服务管理的对话

你还可以让 OpenAI 对话状态功能在服务端管理对话状态，而不是使用 `to_input_list()` 或 `Sessions` 在本地处理。这样你就可以保留对话历史，而无需手动重新发送所有过去的消息。对于下面任一服务管理方法，每次请求只传入新轮次的输入，并复用保存的 ID。更多详细信息请参阅 [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨轮次跟踪状态的方式：

##### 1. 使用 `conversation_id`

你首先使用 OpenAI Conversations API 创建一个对话，然后在之后每次调用中复用其 ID：

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

##### 2. 使用 `previous_response_id`

另一种选项是**响应链式连接**，其中每个轮次都显式链接到上一轮的响应 ID。

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

如果某次运行因审批而暂停，并且你从 [`RunState`][agents.run_state.RunState] 恢复，
SDK 会保留已保存的 `conversation_id` / `previous_response_id` / `auto_previous_response_id`
设置，使恢复的轮次继续处于同一个服务管理的对话中。

`conversation_id` 和 `previous_response_id` 互斥。当你希望使用可跨系统共享的具名对话资源时，请使用 `conversation_id`。当你希望获得从一轮到下一轮最轻量的 Responses API 延续基本组件时，请使用 `previous_response_id`。

!!! note

    SDK 会自动以退避方式重试 `conversation_locked` 错误。在服务管理的
    对话运行中，它会在重试前回退内部对话跟踪器输入，以便
    相同的已准备项可以被干净地重新发送。

    在基于本地 session 的运行中（不能与 `conversation_id`、
    `previous_response_id` 或 `auto_previous_response_id` 组合使用），SDK 还会尽最大努力
    回滚最近持久化的输入项，以减少重试后重复的历史条目。

    即使你未配置 `ModelSettings.retry`，也会发生此兼容性重试。有关
    模型请求上更广泛的选择启用重试行为，请参阅 [Runner-managed retries](models/index.md#runner-managed-retries)。

## 钩子和自定义

### 调用模型输入过滤器

使用 `call_model_input_filter` 在模型调用前立即编辑模型输入。该钩子接收当前智能体、上下文和合并后的输入项（存在会话历史时包括会话历史），并返回一个新的 `ModelInputData`。

返回值必须是 [`ModelInputData`][agents.run.ModelInputData] 对象。其 `input` 字段是必需的，并且必须是输入项列表。返回任何其他形状都会引发 `UserError`。

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

runner 会将准备好的输入列表副本传递给该钩子，因此你可以修剪、替换或重新排序它，而不会就地改变调用方的原始列表。

如果你正在使用 session，`call_model_input_filter` 会在会话历史已经加载并与当前轮次合并之后运行。当你希望自定义更早的合并步骤本身时，请使用 [`session_input_callback`][agents.run.RunConfig.session_input_callback]。

如果你正在使用带有 `conversation_id`、`previous_response_id` 或 `auto_previous_response_id` 的 OpenAI 服务管理对话状态，该钩子会在下一次 Responses API 调用的已准备 payload 上运行。该 payload 可能已经只表示新轮次增量，而不是对早期历史的完整重放。只有你返回的项会被标记为已发送到该服务管理的延续。

通过 `run_config` 为每次运行设置该钩子，以编辑敏感数据、修剪较长历史记录或注入额外系统指导。

## 错误和恢复

### 错误处理器

所有 `Runner` 入口点都接受 `error_handlers`，这是一个按错误类型作为键的 dict。支持的键为 `"max_turns"` 和 `"model_refusal"`。当你希望返回受控的最终输出，而不是引发 `MaxTurnsExceeded` 或 `ModelRefusalError` 时，请使用它们。

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

当你不希望将回退输出追加到对话历史时，请设置 `include_in_history=False`。

当模型拒绝应生成应用特定的回退，而不是以 `ModelRefusalError` 结束运行时，请使用 `"model_refusal"`。

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

## 持久执行集成和人工参与

对于工具审批暂停/恢复模式，请从专门的[人工参与指南](human_in_the_loop.md)开始。
以下集成适用于运行可能跨越长时间等待、重试或进程重启的持久编排。

### Temporal

你可以使用 Agents SDK [Temporal](https://temporal.io/) 集成来运行持久、长时间运行的工作流，包括人工参与任务。观看 Temporal 和 Agents SDK 实际协同完成长时间运行任务的演示[视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)，并[在此查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

### Restate

你可以使用 Agents SDK [Restate](https://restate.dev/) 集成来实现轻量、持久的智能体，包括人工审批、任务转移和会话管理。该集成需要 Restate 的单二进制运行时作为依赖，并支持将智能体作为进程/容器或 serverless functions 运行。
阅读[概览](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)或查看[文档](https://docs.restate.dev/ai)以获取更多详细信息。

### DBOS

你可以使用 Agents SDK [DBOS](https://dbos.dev/) 集成来运行可靠的智能体，在故障和重启时保留进度。它支持长时间运行的智能体、人工参与工作流和任务转移。它同时支持同步和异步方法。该集成只需要一个 SQLite 或 Postgres 数据库。查看集成[仓库](https://github.com/dbos-inc/dbos-openai-agents)和[文档](https://docs.dbos.dev/integrations/openai-agents)以获取更多详细信息。

## 异常

SDK 在某些情况下会引发异常。完整列表位于 [`agents.exceptions`][]。概览如下：

- [`AgentsException`][agents.exceptions.AgentsException]：这是 SDK 内部引发的所有异常的基类。它作为通用类型，所有其他特定异常都派生自它。
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体的运行超过传递给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时，会引发此异常。它表示智能体无法在指定数量的交互轮次内完成任务。设置 `max_turns=None` 可禁用该限制。
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）生成意外或无效输出时，会发生此异常。这可能包括：
    - 格式错误的 JSON：当模型为工具调用或在其直接输出中提供格式错误的 JSON 结构时，尤其是在定义了特定 `output_type` 的情况下。
    - 意外的工具相关失败：当模型未能以预期方式使用工具时
- [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：当工具调用超过其配置的超时时间，并且该工具使用 `timeout_behavior="raise_exception"` 时，会引发此异常。
- [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时出错，会引发此异常。这通常源于不正确的代码实现、无效配置或误用 SDK API。
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当分别满足输入安全防护措施或输出安全防护措施的条件时，会引发此异常。输入安全防护措施会在处理前检查传入消息，而输出安全防护措施会在交付前检查智能体的最终响应。