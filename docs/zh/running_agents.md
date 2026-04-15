---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类运行智能体。你有 3 种选项：

1. [`Runner.run()`][agents.run.Runner.run]，异步运行并返回 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]，同步方法，底层只是运行 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，异步运行并返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式模式调用 LLM，并在接收到事件时将其流式传输给你。

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

## Runner 生命周期与配置

### 智能体循环

当你在 `Runner` 中使用 run 方法时，需要传入一个起始智能体和输入。输入可以是：

-   字符串（视为一条用户消息），
-   OpenAI Responses API 格式的输入项列表，或
-   在恢复被中断的运行时传入 [`RunState`][agents.run_state.RunState]。

然后 runner 会运行一个循环：

1. 我们使用当前输入为当前智能体调用 LLM。
2. LLM 生成其输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 执行了任务转移，我们会更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 生成了工具调用，我们会执行这些工具调用、追加结果，并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判断 LLM 输出是否被视为“最终输出”的规则是：它产生了所需类型的文本输出，且没有工具调用。

### 流式传输

流式传输允许你在 LLM 运行时额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含本次运行的完整信息，包括所有新生成的输出。你可以调用 `.stream_events()` 获取流式事件。请在[流式传输指南](streaming.md)中阅读更多内容。

#### Responses WebSocket 传输（可选辅助）

如果启用 OpenAI Responses websocket 传输，你仍可继续使用常规 `Runner` API。建议使用 websocket 会话辅助器以复用连接，但这不是必需的。

这是基于 websocket 传输的 Responses API，不是 [Realtime API](realtime/guide.md)。

有关传输选择规则，以及围绕具体模型对象或自定义 provider 的注意事项，请参阅[模型](models/index.md#responses-websocket-transport)。

##### 模式 1：不使用会话辅助器（可用）

当你只想使用 websocket 传输且不需要 SDK 为你管理共享 provider/session 时，使用此方式。

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

此模式适用于单次运行。如果你重复调用 `Runner.run()` / `Runner.run_streamed()`，每次运行都可能重新连接，除非你手动复用同一个 `RunConfig` / provider 实例。

##### 模式 2：使用 `responses_websocket_session()`（推荐用于多轮复用）

当你希望在多次运行间共享具备 websocket 能力的 provider 和 `RunConfig`（包括继承同一 `run_config` 的嵌套 agent-as-tool 调用）时，请使用 [`responses_websocket_session()`][agents.responses_websocket_session]。

```python
import asyncio

from agents import Agent, responses_websocket_session


async def main():
    agent = Agent(name="Assistant", instructions="Be concise.")

    async with responses_websocket_session() as ws:
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

请在上下文退出前完成对流式结果的消费。在 websocket 请求仍在进行时退出上下文，可能会强制关闭共享连接。

### 运行配置

`run_config` 参数允许你为智能体运行配置一些全局设置：

#### 常见运行配置目录

使用 `RunConfig` 可在单次运行中覆盖行为，而无需更改每个智能体定义。

##### 模型、provider 与会话默认值

-   [`model`][agents.run.RunConfig.model]：允许设置全局 LLM 模型，不受各 Agent 自身 `model` 配置影响。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型 provider，默认为 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定设置。例如，你可以设置全局 `temperature` 或 `top_p`。
-   [`session_settings`][agents.run.RunConfig.session_settings]：在运行期间检索历史记录时覆盖会话级默认值（例如 `SessionSettings(limit=...)`）。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：使用 Sessions 时，自定义每轮前如何将新用户输入与会话历史合并。该回调可为同步或异步。

##### 安全防护措施、任务转移与模型输入整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：在所有运行中包含的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器（若任务转移本身尚未设置）。该过滤器允许你编辑发送给新智能体的输入。详见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：可选启用的 beta 功能，在调用下一个智能体前将先前转录折叠为单条 assistant 消息。为稳定嵌套任务转移，此功能默认关闭；设为 `True` 启用，或保留 `False` 以透传原始转录。当你未传入 `RunConfig` 时，所有 [Runner 方法][agents.run.Runner] 会自动创建一个 `RunConfig`，因此 quickstart 和示例保持默认关闭，且任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖该设置。单个任务转移可通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选可调用对象，当你启用 `nest_handoff_history` 时，每次都会接收标准化转录（历史 + 任务转移项）。它必须返回要转发给下一个智能体的精确输入项列表，使你无需编写完整任务转移过滤器即可替换内置摘要。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：在模型调用前立即编辑完整准备好的模型输入（instructions 和输入项）的钩子，例如裁剪历史或注入系统提示词。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：控制当 runner 将先前输出转换为下一轮模型输入时，是否保留或省略 reasoning 项 ID。

##### 追踪与可观测性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许你对整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig] 以覆盖追踪导出设置，例如每次运行的追踪 API key。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在敏感数据，例如 LLM 与工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：设置运行的追踪工作流名称、trace ID 和 trace group ID。我们建议至少设置 `workflow_name`。group ID 为可选字段，可用于关联多次运行的追踪。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：包含在所有追踪中的元数据。

##### 工具审批与工具错误行为

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：在审批流程中工具调用被拒绝时，自定义向模型可见的消息。

嵌套任务转移以可选启用 beta 的形式提供。可通过传入 `RunConfig(nest_handoff_history=True)` 启用折叠转录行为，或通过设置 `handoff(..., nest_handoff_history=True)` 为特定任务转移启用。若你希望保留原始转录（默认行为），请保持该标志未设置，或提供能按你需求精确转发对话的 `handoff_input_filter`（或 `handoff_history_mapper`）。若要在不编写自定义 mapper 的情况下修改生成摘要所用包装文本，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并可用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 恢复默认值）。

#### 运行配置细节

##### `tool_error_formatter`

使用 `tool_error_formatter` 自定义审批流程中工具调用被拒绝时返回给模型的消息。

格式化器会收到包含以下字段的 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]：

-   `kind`：错误类别。当前为 `"approval_rejected"`。
-   `tool_type`：工具运行时类型（`"function"`、`"computer"`、`"shell"`、`"apply_patch"` 或 `"custom"`）。
-   `tool_name`：工具名称。
-   `call_id`：工具调用 ID。
-   `default_message`：SDK 默认的模型可见消息。
-   `run_context`：当前运行上下文包装器。

返回字符串可替换该消息，或返回 `None` 以使用 SDK 默认值。

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

`reasoning_item_id_policy` 控制当 runner 向后携带历史时（例如使用 `RunResult.to_input_list()` 或基于 session 的运行），reasoning 项如何转换为下一轮模型输入。

-   `None` 或 `"preserve"`（默认）：保留 reasoning 项 ID。
-   `"omit"`：从生成的下一轮输入中移除 reasoning 项 ID。

`"omit"` 主要作为可选缓解手段，用于应对一类 Responses API 400 错误：某个 reasoning 项携带了 `id`，但缺少必需的后续项（例如，`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

这可能发生在多轮智能体运行中：SDK 从先前输出构建后续输入（包括 session 持久化、服务端管理的会话增量、流式/非流式后续轮次及恢复路径）时，保留了 reasoning 项 ID，但 provider 要求该 ID 必须与其对应后续项成对出现。

设置 `reasoning_item_id_policy="omit"` 会保留 reasoning 内容，但移除 reasoning 项 `id`，从而避免在 SDK 生成的后续输入中触发该 API 不变量约束。

作用域说明：

-   这只会改变 SDK 在构建后续输入时生成/转发的 reasoning 项。
-   它不会改写用户提供的初始输入项。
-   在应用该策略后，`call_model_input_filter` 仍可有意重新引入 reasoning ID。

## 状态与会话管理

### 内存策略选择

将状态带入下一轮通常有四种方式：

| 策略 | 状态存放位置 | 最适合 | 下一轮传入内容 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 你的应用内存 | 小型聊天循环、完全手动控制、任意 provider | `result.to_input_list()` 返回的列表 + 下一条用户消息 |
| `session` | 你的存储 + SDK | 持久化聊天状态、可恢复运行、自定义存储 | 同一个 `session` 实例，或指向同一存储的另一个实例 |
| `conversation_id` | OpenAI Conversations API | 希望在多个 worker 或服务间共享的命名服务端会话 | 同一个 `conversation_id` + 仅新的用户轮次 |
| `previous_response_id` | OpenAI Responses API | 无需创建会话资源的轻量服务端托管延续 | `result.last_response_id` + 仅新的用户轮次 |

`result.to_input_list()` 和 `session` 是客户端管理。`conversation_id` 和 `previous_response_id` 是 OpenAI 管理，且仅适用于你使用 OpenAI Responses API 的情况。在大多数应用中，每个会话选择一种持久化策略即可。除非你有意协调这两层，否则混用客户端管理历史与 OpenAI 托管状态可能会导致上下文重复。

!!! note

    Session 持久化不能与服务端托管会话设置
    （`conversation_id`、`previous_response_id` 或 `auto_previous_response_id`）
    在同一次运行中组合使用。每次调用请选择一种方式。

### 会话/聊天线程

调用任一 run 方法都可能导致一个或多个智能体运行（因此会有一次或多次 LLM 调用），但它表示聊天会话中的单个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM、运行工具、任务转移到第二个智能体；第二个智能体运行更多工具，然后产出输出。

在智能体运行结束后，你可以选择向用户展示什么。例如，你可以展示智能体生成的每个新项，或仅展示最终输出。无论哪种方式，用户都可能继续追问，此时你可以再次调用 run 方法。

#### 手动会话管理

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法手动管理会话历史，以获取下一轮输入：

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

#### 使用 sessions 自动会话管理

若想更简单，可使用 [Sessions](sessions/index.md) 自动处理会话历史，而无需手动调用 `.to_input_list()`：

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
-   为不同 session ID 维护独立会话

更多细节请参阅 [Sessions 文档](sessions/index.md)。


#### 服务端托管会话

你也可以让 OpenAI 会话状态功能在服务端管理会话状态，而不是在本地通过 `to_input_list()` 或 `Sessions` 处理。这可让你在无需手动重发全部历史消息的情况下保留会话历史。使用以下任一服务端托管方式时，每次请求只传入新轮次输入并复用已保存 ID。更多细节见 [OpenAI 会话状态指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种跨轮次跟踪状态的方法：

##### 1. 使用 `conversation_id`

你先通过 OpenAI Conversations API 创建会话，然后在后续每次调用中复用其 ID：

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

另一种选项是**响应链式衔接**，每轮都显式关联到上一轮的响应 ID。

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
设置，以便恢复后的轮次继续在同一个服务端托管会话中进行。

`conversation_id` 和 `previous_response_id` 互斥。若你需要可跨系统共享的命名会话资源，请使用 `conversation_id`。若你想要从一轮到下一轮最轻量的 Responses API 延续基本组件，请使用 `previous_response_id`。

!!! note

    SDK 会自动对 `conversation_locked` 错误进行带退避的重试。在服务端托管
    会话运行中，重试前会回退内部会话跟踪器输入，以便可干净地重发
    同一批已准备项。

    在本地基于 session 的运行中（不能与 `conversation_id`、
    `previous_response_id` 或 `auto_previous_response_id` 组合），SDK 也会尽力
    回滚最近持久化的输入项，以减少重试后的重复历史条目。

    即使你未配置 `ModelSettings.retry`，该兼容性重试也会发生。若需
    模型请求的更广泛可选重试行为，请参阅 [Runner 管理重试](models/index.md#runner-managed-retries)。

## 钩子与自定义

### 模型调用输入过滤器

使用 `call_model_input_filter` 可在模型调用前立即编辑模型输入。该钩子接收当前智能体、上下文以及合并后的输入项（若存在 session 历史也包含在内），并返回新的 `ModelInputData`。

返回值必须是 [`ModelInputData`][agents.run.ModelInputData] 对象。其 `input` 字段为必填，且必须是输入项列表。返回其他形状会抛出 `UserError`。

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

runner 会将准备好的输入列表副本传给该钩子，因此你可以裁剪、替换或重排，而不必原地修改调用方的原始列表。

若你使用 session，`call_model_input_filter` 会在 session 历史已加载并与当前轮次合并后运行。若你希望自定义更早的合并步骤，请使用 [`session_input_callback`][agents.run.RunConfig.session_input_callback]。

若你使用 `conversation_id`、`previous_response_id` 或 `auto_previous_response_id` 的 OpenAI 服务端托管会话状态，该钩子会作用于下一次 Responses API 调用的已准备负载。该负载可能已仅表示新轮次增量，而非完整重放早期历史。只有你返回的项会被标记为该服务端托管延续已发送。

可通过 `run_config` 按次设置该钩子，用于脱敏敏感数据、裁剪长历史或注入额外系统引导。

## 错误与恢复

### 错误处理器

所有 `Runner` 入口都接受 `error_handlers`（按错误类型为键的字典）。当前支持的键是 `"max_turns"`。当你希望返回可控的最终输出而非抛出 `MaxTurnsExceeded` 时可使用它。

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

当你不希望将回退输出追加到会话历史时，设置 `include_in_history=False`。

## 持久执行集成与 human-in-the-loop

对于工具审批的暂停/恢复模式，请先阅读专门的 [Human-in-the-loop 指南](human_in_the_loop.md)。
以下集成用于可持久化编排，适用于运行可能跨越长时间等待、重试或进程重启的场景。

### Temporal

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行持久化的长时工作流，包括 human-in-the-loop 任务。你可以在[此视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中查看 Temporal 与 Agents SDK 协作完成长时任务的演示，也可[在此查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。 

### Restate

你可以使用 Agents SDK 的 [Restate](https://restate.dev/) 集成来构建轻量且持久的智能体，包括人工审批、任务转移和会话管理。该集成依赖 Restate 的单二进制运行时，并支持将智能体作为进程/容器或无服务函数运行。
请阅读[概览](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)或查看[文档](https://docs.restate.dev/ai)了解更多细节。

### DBOS

你可以使用 Agents SDK 的 [DBOS](https://dbos.dev/) 集成来运行可靠智能体，在故障和重启后保留进度。它支持长时智能体、human-in-the-loop 工作流和任务转移。它同时支持同步与异步方法。该集成仅需 SQLite 或 Postgres 数据库。请查看集成 [repo](https://github.com/dbos-inc/dbos-openai-agents) 和[文档](https://docs.dbos.dev/integrations/openai-agents)了解更多细节。

## 异常

SDK 在某些情况下会抛出异常。完整列表见 [`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：这是 SDK 内所有异常的基类。它作为通用类型，其他所有具体异常都从它派生。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传入 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时抛出。表示智能体无法在指定交互轮次数内完成任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生意外或无效输出时发生。包括：
    -   JSON 格式错误：模型为工具调用或直接输出提供了格式错误的 JSON 结构，尤其是在定义了特定 `output_type` 时。
    -   与工具相关的意外失败：模型未按预期方式使用工具
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：当工具调用超过其配置超时时间，且工具使用 `timeout_behavior="raise_exception"` 时抛出。
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时出错时抛出。通常由错误代码实现、无效配置或误用 SDK API 导致。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当分别满足输入安全防护措施或输出安全防护措施的触发条件时抛出。输入安全防护措施在处理前检查传入消息，输出安全防护措施在交付前检查智能体最终响应。