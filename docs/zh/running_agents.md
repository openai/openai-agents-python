---
search:
  exclude: true
---
# 智能体运行

你可以通过 [`Runner`][agents.run.Runner] 类运行智能体。你有 3 个选项：

1. [`Runner.run()`][agents.run.Runner.run]，异步运行并返回一个 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]，同步方法，底层只是运行 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，异步运行并返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式传输模式调用 LLM，并在接收到事件时将这些事件流式传输给你。

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

更多信息请阅读[结果指南](results.md)。

## Runner 生命周期与配置

### 智能体循环

当你使用 `Runner` 中的 run 方法时，需要传入一个起始智能体和输入。输入可以是：

-   字符串（被视为用户消息），
-   OpenAI Responses API 格式的输入项列表，或
-   在恢复被中断的运行时使用的 [`RunState`][agents.run_state.RunState]。

然后，运行器会运行一个循环：

1. 我们使用当前输入为当前智能体调用 LLM。
2. LLM 生成其输出。
    1. 如果 LLM 返回 `final_output`，循环结束，并返回结果。
    2. 如果 LLM 执行任务转移，我们会更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 生成工具调用，我们会运行这些工具调用、追加结果，并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。传入 `max_turns=None` 可禁用此轮次限制。

!!! note

    判断 LLM 输出是否被视为“最终输出”的规则是：它会产生所需类型的文本输出，并且没有工具调用。

### 流式传输

流式传输允许你在 LLM 运行时额外接收流式传输事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含此次运行的完整信息，包括生成的所有新输出。你可以调用 `.stream_events()` 获取流式传输事件。更多信息请阅读[流式传输指南](streaming.md)。

#### Responses WebSocket 传输（可选辅助工具）

如果启用 OpenAI Responses websocket 传输，你仍然可以继续使用常规的 `Runner` API。建议使用 websocket 会话辅助工具来复用连接，但这不是必需的。

这是基于 websocket 传输的 Responses API，而不是 [Realtime API](realtime/guide.md)。

关于传输选择规则，以及具体模型对象或自定义提供商的注意事项，请参阅[模型](models/index.md#responses-websocket-transport)。

##### 模式 1：无会话辅助工具（可用）

当你只需要 websocket 传输，并且不需要 SDK 为你管理共享 provider/session 时，请使用此模式。

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

此模式适合单次运行。如果你反复调用 `Runner.run()` / `Runner.run_streamed()`，每次运行都可能重新连接，除非你手动复用同一个 `RunConfig` / provider 实例。

##### 模式 2：使用 `responses_websocket_session()`（推荐用于多轮复用）

当你希望在多次运行之间共享支持 websocket 的 provider 和 `RunConfig`（包括继承相同 `run_config` 的嵌套 agent-as-tool 调用）时，请使用 [`responses_websocket_session()`][agents.responses_websocket_session]。

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

请在上下文退出前完成对流式结果的消费。如果在 websocket 请求仍在进行时退出上下文，可能会强制关闭共享连接。

如果长时间推理轮次触发 websocket keepalive 超时，请增加 `ping_timeout`，或设置 `ping_timeout=None` 以禁用心跳超时。对于可靠性比 websocket 延迟更重要的运行，请使用 HTTP/SSE 传输。

### 运行配置

`run_config` 参数允许你为智能体运行配置一些全局设置：

#### 常见运行配置类别

使用 `RunConfig` 可以在不更改每个智能体定义的情况下，覆盖单次运行的行为。

##### 模型、provider 与会话默认值

-   [`model`][agents.run.RunConfig.model]：允许设置要使用的全局 LLM 模型，而不受每个 Agent 自身 `model` 的影响。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型 provider，默认是 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定的设置。例如，你可以设置全局 `temperature` 或 `top_p`。
-   [`session_settings`][agents.run.RunConfig.session_settings]：在运行期间检索历史时，覆盖会话级默认值（例如 `SessionSettings(limit=...)`）。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：使用 Sessions 时，在每一轮之前自定义如何将新的用户输入与会话历史合并。回调可以是同步或异步的。

##### 安全防护措施、任务转移与模型输入整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：要包含在所有运行中的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器，前提是该任务转移尚未配置过滤器。输入过滤器允许你编辑发送给新智能体的输入。更多细节请参阅 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 中的文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：可选择启用的 beta 功能，会在调用下一个智能体之前，将此前的对话记录折叠为一条 assistant 消息。在我们稳定嵌套任务转移期间，此功能默认禁用；设置为 `True` 可启用，或保持 `False` 以传递原始对话记录。当你未传入 `RunConfig` 时，所有 [Runner 方法][agents.run.Runner]都会自动创建一个，因此快速入门和示例会保持默认关闭，并且任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖它。单个任务转移可以通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选的 callable；每当你选择启用 `nest_handoff_history` 时，它会接收规范化后的对话记录（历史 + 任务转移项）。它必须返回要转发给下一个智能体的确切输入项列表，从而允许你替换内置摘要，而无需编写完整的任务转移过滤器。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：一个钩子，可在模型调用前立即编辑完全准备好的模型输入（instructions 和输入项），例如用于裁剪历史或注入系统提示词。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：控制当运行器将先前输出转换为下一轮模型输入时，是否保留或省略推理项 ID。

##### 追踪与可观测性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许你为整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig] 以覆盖追踪导出设置，例如每次运行的追踪 API 密钥。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪是否会包含潜在敏感数据，例如 LLM 和工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：设置此次运行的追踪工作流名称、追踪 ID 和追踪组 ID。我们建议至少设置 `workflow_name`。组 ID 是一个可选字段，可让你在多次运行之间关联追踪。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪中的元数据。

##### 工具执行、审批与工具错误行为

-   [`tool_execution`][agents.run.RunConfig.tool_execution]：配置本地工具调用在 SDK 侧的执行行为，例如限制同时运行的函数工具数量。
-   [`tool_not_found_behavior`][agents.run.RunConfig.tool_not_found_behavior]：配置运行器如何处理模型发出的、无法解析的函数工具调用。默认会抛出 `ModelBehaviorError`；也可以选择改为返回模型可见的错误输出。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：自定义模型可见的工具错误消息，例如审批拒绝和选择启用的 tool-not-found 输出。

嵌套任务转移作为可选择启用的 beta 功能提供。通过传入 `RunConfig(nest_handoff_history=True)` 启用折叠对话记录行为，或设置 `handoff(..., nest_handoff_history=True)` 为特定任务转移启用它。如果你希望保留原始对话记录（默认行为），请不要设置该标志，或提供一个 `handoff_input_filter`（或 `handoff_history_mapper`），以完全按你的需要转发对话。如需更改生成摘要中使用的包装文本，而不编写自定义 mapper，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并调用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 恢复默认值）。

#### 运行配置细节

##### `tool_execution`

当你希望配置本地函数工具在 SDK 侧的行为时，例如限制某次运行中的本地函数工具并发数，请使用 `tool_execution`。

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

`max_function_tool_concurrency=None` 会保留默认行为：当模型在一个轮次中发出多个函数工具调用时，SDK 会启动所有已发出的本地函数工具调用。设置一个整数值可限制这些本地函数工具中同时运行的数量。

这与 provider 侧的 [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] 是分开的。`parallel_tool_calls` 控制模型是否允许在单个响应中发出多个工具调用。`tool_execution.max_function_tool_concurrency` 控制模型发出本地函数工具调用后，SDK 如何执行这些调用。

`pre_approval_tool_input_guardrails=False` 会保留默认审批流程：如果某个函数工具需要审批，运行会先暂停，并且工具输入安全防护措施只会在审批通过后、执行前立即运行。当你希望函数工具输入安全防护措施在发出待审批中断之前运行时，请将其设置为 `True`。通过此预审批检查的调用，在审批通过后仍会再次运行相同的输入安全防护措施，因此时间敏感的检查会在执行前重新验证。

##### `tool_not_found_behavior`

默认情况下，如果模型发出的函数工具调用与当前智能体可用的任何函数工具都不匹配，运行器会抛出 `ModelBehaviorError`。

当你希望运行保持可恢复时，请设置 `tool_not_found_behavior="return_error_to_model"`。在该模式下，SDK 会为无法解析的工具调用追加一个 `function_call_output`，然后再次运行模型，以便模型选择可用工具，或在不使用该工具的情况下作答。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Handle this request with the available tools.",
    run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
)
```

此选项目前仅适用于无法解析的函数工具调用。其他无效工具 payload 会继续使用其现有错误行为。

##### `tool_error_formatter`

使用 `tool_error_formatter` 可以自定义当 SDK 创建模型可见的工具错误输出时返回给模型的消息。

formatter 会接收 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]，其中包含：

-   `kind`：错误类别，例如 `"approval_rejected"` 或 `"tool_not_found"`。
-   `tool_type`：工具运行时（`"function"`、`"computer"`、`"shell"`、`"apply_patch"` 或 `"custom"`）。
-   `tool_name`：工具名称。
-   `call_id`：工具调用 ID。
-   `default_message`：SDK 默认的模型可见消息。
-   `run_context`：当前活动的运行上下文包装器。

返回一个字符串可替换该消息，或返回 `None` 以使用 SDK 默认值。

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

`reasoning_item_id_policy` 控制当运行器向前传递历史时（例如使用 `RunResult.to_input_list()` 或基于会话的运行），推理项如何转换为下一轮模型输入。

-   `None` 或 `"preserve"`（默认）：保留推理项 ID。
-   `"omit"`：从生成的下一轮输入中移除推理项 ID。

使用 `"omit"` 主要是作为一种可选择启用的缓解措施，用于应对某类 Responses API 400 错误：推理项带有 `id`，但缺少必需的后续项（例如 `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

在多轮智能体运行中，当 SDK 根据先前输出构造后续输入时（包括会话持久化、服务端管理的对话增量、流式/非流式后续轮次以及恢复路径），如果保留了推理项 ID，但 provider 要求该 ID 必须与其对应的后续项保持配对，就可能发生这种情况。

设置 `reasoning_item_id_policy="omit"` 会保留推理内容，但移除推理项 `id`，从而避免在 SDK 生成的后续输入中触发该 API 不变量。

范围说明：

-   这只会更改 SDK 在构建后续输入时生成/转发的推理项。
-   它不会重写用户提供的初始输入项。
-   在应用此策略后，`call_model_input_filter` 仍然可以有意重新引入推理 ID。

## 状态与对话管理

### 内存策略选择

将状态带入下一轮有四种常见方式：

| 策略 | 状态所在位置 | 适用场景 | 下一轮传入的内容 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 你的应用内存 | 小型聊天循环、完全手动控制、任何 provider | `result.to_input_list()` 的列表加上下一条用户消息 |
| `session` | 你的存储加 SDK | 持久聊天状态、可恢复运行、自定义存储 | 同一个 `session` 实例，或指向同一存储的另一个实例 |
| `conversation_id` | OpenAI Conversations API | 你希望跨 worker 或服务共享的具名服务端对话 | 同一个 `conversation_id`，且仅加新的用户轮次 |
| `previous_response_id` | OpenAI Responses API | 无需创建对话资源的轻量级服务端托管续接 | `result.last_response_id`，且仅加新的用户轮次 |

`result.to_input_list()` 和 `session` 由客户端管理。`conversation_id` 和 `previous_response_id` 由 OpenAI 管理，并且仅在你使用 OpenAI Responses API 时适用。在大多数应用中，每个对话请选择一种持久化策略。混合使用客户端管理的历史和 OpenAI 管理的状态可能会重复上下文，除非你是在有意协调这两层。

!!! note

    会话持久化不能与服务端管理的对话设置
    （`conversation_id`、`previous_response_id` 或 `auto_previous_response_id`）
    在同一次运行中组合使用。请为每次调用选择一种方法。

### 对话/聊天线程

调用任何运行方法都可能导致一个或多个智能体运行（因而触发一次或多次 LLM 调用），但它表示聊天对话中的一个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM、运行工具、任务转移到第二个智能体，第二个智能体运行更多工具，然后生成输出。

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

#### 使用 Sessions 的自动对话管理

为了采用更简单的方法，你可以使用 [Sessions](sessions/index.md) 自动处理对话历史，而无需手动调用 `.to_input_list()`：

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
-   为不同的会话 ID 维护独立对话

更多细节请参阅 [Sessions 文档](sessions/index.md)。


#### 服务端托管对话

你也可以让 OpenAI 对话状态功能在服务端管理对话状态，而不是使用 `to_input_list()` 或 `Sessions` 在本地处理。这样你就可以保留对话历史，而无需手动重新发送所有过去的消息。对于下面任一服务端托管方法，请在每个请求中仅传入新轮次的输入，并复用已保存的 ID。更多细节请参阅 [OpenAI 对话状态指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨轮次跟踪状态的方式：

##### 1. `conversation_id` 的使用

你首先使用 OpenAI Conversations API 创建一个对话，然后在之后的每次调用中复用其 ID：

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

##### 2. `previous_response_id` 的使用

另一个选项是**响应链式衔接**，即每个轮次都会显式链接到上一轮的响应 ID。

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

如果某次运行因审批而暂停，并且你从 [`RunState`][agents.run_state.RunState] 恢复，则
SDK 会保留已保存的 `conversation_id` / `previous_response_id` / `auto_previous_response_id`
设置，因此恢复后的轮次会在同一个服务端托管对话中继续。

`conversation_id` 和 `previous_response_id` 互斥。当你希望使用可跨系统共享的具名对话资源时，请使用 `conversation_id`。当你希望从一个轮次到下一个轮次使用最轻量的 Responses API 续接基本组件时，请使用 `previous_response_id`。

!!! note

    SDK 会自动以退避方式重试 `conversation_locked` 错误。在服务端托管
    对话运行中，它会在重试前回退内部对话跟踪器输入，以便
    同一批准备好的项可以被干净地重新发送。

    在本地基于会话的运行中（它不能与 `conversation_id`、
    `previous_response_id` 或 `auto_previous_response_id` 组合使用），SDK 也会尽力
    回滚最近持久化的输入项，以减少重试后重复的历史条目。

    即使你没有配置 `ModelSettings.retry`，也会发生此兼容性重试。关于
    模型请求上更广泛的可选择启用重试行为，请参阅 [Runner 管理的重试](models/index.md#runner-managed-retries)。

## 钩子与自定义

### 模型调用输入过滤器

使用 `call_model_input_filter` 可以在模型调用前立即编辑模型输入。该钩子会接收当前智能体、上下文以及合并后的输入项（如存在会话历史，也包含其中），并返回一个新的 `ModelInputData`。

返回值必须是 [`ModelInputData`][agents.run.ModelInputData] 对象。其 `input` 字段是必需的，并且必须是输入项列表。返回任何其他结构都会抛出 `UserError`。

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

运行器会向该钩子传入一份准备好的输入列表副本，因此你可以裁剪、替换或重新排序它，而不会原地改变调用方的原始列表。

如果你正在使用会话，`call_model_input_filter` 会在会话历史已经加载并与当前轮次合并后运行。当你希望自定义更早的合并步骤本身时，请使用 [`session_input_callback`][agents.run.RunConfig.session_input_callback]。

如果你正在使用带有 `conversation_id`、`previous_response_id` 或 `auto_previous_response_id` 的 OpenAI 服务端托管对话状态，该钩子会在下一次 Responses API 调用的已准备 payload 上运行。该 payload 可能已经只表示新轮次增量，而不是对早期历史的完整重放。只有你返回的项会被标记为已发送，用于该服务端托管续接。

通过 `run_config` 为每次运行设置该钩子，以脱敏敏感数据、裁剪过长历史或注入额外系统指导。

## 错误与恢复

### 错误处理器

所有 `Runner` 入口点都接受 `error_handlers`，这是一个以错误类型为键的字典。支持的键为 `"max_turns"` 和 `"model_refusal"`。当你希望返回受控的最终输出，而不是抛出 `MaxTurnsExceeded` 或 `ModelRefusalError` 时，请使用它们。

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

当你不希望将 fallback 输出追加到对话历史时，请设置 `include_in_history=False`。

当模型拒绝应生成应用特定的 fallback，而不是以 `ModelRefusalError` 结束运行时，请使用 `"model_refusal"`。

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

对于工具审批的暂停/恢复模式，请从专门的[人工介入指南](human_in_the_loop.md)开始。
下面的集成适用于持久编排，场景包括运行可能跨越长时间等待、重试或进程重启。

### Dapr

你可以使用 Agents SDK 的 [Dapr](https://dapr.io) Diagrid 集成来运行持久的长时间运行智能体，这些智能体可在支持人工介入的情况下从故障中自动恢复。Dapr 是一个厂商中立的 [CNCF](https://cncf.io) 工作流编排器。请在[此处](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai)开始使用 Dapr 和 OpenAI 智能体。

### Temporal

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行持久的长时间运行工作流，包括人工介入任务。观看[此视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中 Temporal 与 Agents SDK 协同完成长时间运行任务的演示，并[在此查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。 

### Restate

你可以使用 Agents SDK 的 [Restate](https://restate.dev/) 集成来运行轻量级、持久的智能体，包括人工审批、任务转移和会话管理。该集成需要 Restate 的单二进制运行时作为依赖，并支持将智能体作为进程/容器或无服务器函数运行。
阅读[概览](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)或查看[文档](https://docs.restate.dev/ai)以了解更多细节。

### DBOS

你可以使用 Agents SDK 的 [DBOS](https://dbos.dev/) 集成来运行可靠的智能体，这些智能体可在故障和重启之间保留进度。它支持长时间运行智能体、人工介入工作流和任务转移。它同时支持同步和异步方法。该集成只需要 SQLite 或 Postgres 数据库。查看集成[仓库](https://github.com/dbos-inc/dbos-openai-agents)和[文档](https://docs.dbos.dev/integrations/openai-agents)以了解更多细节。

## 异常

SDK 会在某些情况下抛出异常。完整列表位于 [`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：这是 SDK 内部抛出的所有异常的基类。它作为通用类型，所有其他特定异常都从它派生。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体的运行超过传给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时，会抛出此异常。它表示智能体无法在指定的交互轮次数内完成任务。设置 `max_turns=None` 可禁用该限制。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）生成意外或无效输出时，会发生此异常。这可能包括：
    -   JSON 格式错误：当模型为工具调用或在直接输出中提供格式错误的 JSON 结构时，尤其是在定义了特定 `output_type` 的情况下。
    -   与工具相关的意外失败：当模型未能按预期方式使用工具时
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：当函数工具调用超过其配置的超时时间，并且该工具使用 `timeout_behavior="raise_exception"` 时，会抛出此异常。
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时犯错，会抛出此异常。这通常由错误的代码实现、无效配置或误用 SDK API 导致。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入安全防护措施或输出安全防护措施的条件分别满足时，会抛出此异常。输入安全防护措施在处理前检查传入消息，而输出安全防护措施在交付前检查智能体的最终响应。