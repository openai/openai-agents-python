---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类来运行智能体。你有 3 个选项：

1. [`Runner.run()`][agents.run.Runner.run]，异步运行并返回一个 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]，这是一个同步方法，底层只是运行 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，异步运行并返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式模式调用 LLM，并在接收到事件时将这些事件流式返回给你。

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

## Runner 生命周期与配置

### 智能体循环

当你在 `Runner` 中使用 run 方法时，你需要传入一个起始智能体和输入。输入可以是：

-   一个字符串（视为用户消息），
-   OpenAI Responses API 格式的输入项列表，或
-   在恢复中断运行时传入一个 [`RunState`][agents.run_state.RunState]。

随后 runner 会执行一个循环：

1. 我们使用当前输入为当前智能体调用 LLM。
2. LLM 生成其输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 执行任务转移，我们更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 生成工具调用，我们执行这些工具调用，追加结果，并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判断 LLM 输出是否被视为“最终输出”的规则是：它生成了目标类型的文本输出，且没有工具调用。

### 流式传输

流式传输允许你在 LLM 运行时额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含此次运行的完整信息，包括所有新生成的输出。你可以调用 `.stream_events()` 获取流式事件。在[流式传输指南](streaming.md)中阅读更多内容。

#### Responses WebSocket 传输（可选辅助）

如果你启用了 OpenAI Responses websocket 传输，仍可继续使用常规的 `Runner` API。建议使用 websocket session helper 以复用连接，但这不是必需的。

这是基于 websocket 传输的 Responses API，而不是 [Realtime API](realtime/guide.md)。

关于传输选择规则，以及具体模型对象或自定义 provider 的注意事项，请参见[模型](models/index.md#responses-websocket-transport)。

##### 模式 1：不使用 session helper（可用）

当你只想使用 websocket 传输，且不需要 SDK 为你管理共享 provider/session 时使用此模式。

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

此模式适用于单次运行。如果你反复调用 `Runner.run()` / `Runner.run_streamed()`，除非你手动复用同一个 `RunConfig` / provider 实例，否则每次运行都可能重新连接。

##### 模式 2：使用 `responses_websocket_session()`（推荐用于多轮复用）

当你希望在多次运行中共享具备 websocket 能力的 provider 和 `RunConfig`（包括继承同一 `run_config` 的嵌套 agent-as-tool 调用）时，请使用 [`responses_websocket_session()`][agents.responses_websocket_session]。

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

使用 `RunConfig` 可在不修改每个智能体定义的前提下覆盖单次运行行为。

##### 模型、provider 与 session 默认值

-   [`model`][agents.run.RunConfig.model]：允许设置一个全局 LLM 模型，不受各 Agent 自身 `model` 设置影响。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型 provider，默认为 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体特定设置。例如，你可以设置全局 `temperature` 或 `top_p`。
-   [`session_settings`][agents.run.RunConfig.session_settings]：在运行期间检索历史时，覆盖 session 级默认值（例如 `SessionSettings(limit=...)`）。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：在使用 Sessions 时，自定义每轮前新用户输入与 session 历史的合并方式。该回调可以是同步或异步。

##### 安全防护措施、任务转移与模型输入塑形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：包含在所有运行中的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器（如果该任务转移尚未设置过滤器）。输入过滤器允许你编辑发送给新智能体的输入。更多细节见 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：可选启用的测试版功能，在调用下一个智能体前将先前对话记录折叠为一条 assistant 消息。为稳定嵌套任务转移，该功能默认禁用；设为 `True` 启用，或保持 `False` 以传递原始记录。所有 [Runner 方法][agents.run.Runner] 在你未传入 `RunConfig` 时都会自动创建一个，因此快速开始和示例默认保持关闭，且任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖它。单个任务转移可通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选可调用对象；当你启用 `nest_handoff_history` 时，它会接收规范化后的对话记录（历史 + 任务转移项）。它必须返回要转发给下一个智能体的精确输入项列表，让你无需编写完整任务转移过滤器即可替换内置摘要。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：在模型调用前立即编辑完整准备好的模型输入（instructions 与输入项）的钩子，例如裁剪历史或注入系统提示词。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：控制 runner 在将先前输出转换为下一轮模型输入时，是否保留或省略 reasoning item ID。

##### 追踪与可观测性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许你为整个运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig] 以覆盖本次运行的导出器、进程或追踪元数据。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪中是否包含潜在敏感数据，例如 LLM 和工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：为本次运行设置追踪工作流名称、trace ID 和 trace group ID。建议至少设置 `workflow_name`。group ID 是可选字段，可用于关联多次运行的 traces。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：包含在所有 traces 中的元数据。

##### 工具审批与工具错误行为

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：在审批流中工具调用被拒绝时，自定义对模型可见的消息。

嵌套任务转移作为可选启用测试版提供。可通过传入 `RunConfig(nest_handoff_history=True)` 启用折叠对话记录行为，或设置 `handoff(..., nest_handoff_history=True)` 为特定任务转移启用。若你希望保留原始对话记录（默认行为），请保持该标志未设置，或提供一个按需原样转发会话的 `handoff_input_filter`（或 `handoff_history_mapper`）。若你想在不编写自定义 mapper 的情况下修改生成摘要所用的包装文本，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并使用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 恢复默认值）。

#### 运行配置细节

##### `tool_error_formatter`

使用 `tool_error_formatter` 自定义在审批流中工具调用被拒绝时返回给模型的消息。

格式化器会接收 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]，其中包含：

-   `kind`：错误类别。当前为 `"approval_rejected"`。
-   `tool_type`：工具运行时（`"function"`、`"computer"`、`"shell"` 或 `"apply_patch"`）。
-   `tool_name`：工具名称。
-   `call_id`：工具调用 ID。
-   `default_message`：SDK 默认的模型可见消息。
-   `run_context`：当前运行上下文包装器。

返回字符串可替换该消息，返回 `None` 则使用 SDK 默认值。

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

`reasoning_item_id_policy` 控制当 runner 延续历史时（例如使用 `RunResult.to_input_list()` 或基于 session 的运行），reasoning items 如何被转换为下一轮模型输入。

-   `None` 或 `"preserve"`（默认）：保留 reasoning item ID。
-   `"omit"`：从生成的下一轮输入中移除 reasoning item ID。

`"omit"` 主要作为可选缓解手段，用于应对一类 Responses API 400 错误：发送 reasoning item 时带有 `id`，但缺少其必需的后续项（例如 `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

这可能发生在多轮智能体运行中：SDK 从先前输出构造后续输入时（包括 session 持久化、服务端管理的会话增量、流式/非流式后续轮次，以及恢复路径），保留了 reasoning item ID，但 provider 要求该 ID 必须与对应后续项保持配对。

设置 `reasoning_item_id_policy="omit"` 会保留 reasoning 内容，但移除 reasoning item 的 `id`，从而避免在 SDK 生成的后续输入中触发该 API 不变量约束。

作用域说明：

-   这只会影响 SDK 在构建后续输入时生成/转发的 reasoning items。
-   不会改写用户提供的初始输入项。
-   `call_model_input_filter` 仍可在该策略应用后有意重新引入 reasoning IDs。

## 状态与会话管理

### 内存策略选择

将状态带入下一轮有四种常见方式：

| 策略 | 状态存储位置 | 最佳适用场景 | 下一轮传入内容 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 你的应用内存 | 小型聊天循环、完全手动控制、任意 provider | 来自 `result.to_input_list()` 的列表加上下一条用户消息 |
| `session` | 你的存储加 SDK | 持久聊天状态、可恢复运行、自定义存储 | 同一个 `session` 实例，或指向同一存储的另一个实例 |
| `conversation_id` | OpenAI Conversations API | 你希望跨 worker 或服务共享的命名服务端会话 | 同一个 `conversation_id`，并且只传入新的用户轮次 |
| `previous_response_id` | OpenAI Responses API | 无需创建 conversation 资源的轻量服务端管理续接 | `result.last_response_id`，并且只传入新的用户轮次 |

`result.to_input_list()` 和 `session` 由客户端管理。`conversation_id` 和 `previous_response_id` 由 OpenAI 管理，且仅在你使用 OpenAI Responses API 时适用。在多数应用中，每段会话选择一种持久化策略即可。除非你有意协调两层状态，否则混用客户端管理历史与 OpenAI 管理状态会导致上下文重复。

!!! note

    Session 持久化不能与服务端管理会话设置
    （`conversation_id`、`previous_response_id` 或 `auto_previous_response_id`）在
    同一次运行中组合使用。每次调用请选择一种方式。

### 会话/聊天线程

调用任何 run 方法都可能导致一个或多个智能体运行（因此也会有一次或多次 LLM 调用），但它在聊天会话中代表一个逻辑轮次。例如：

1. 用户轮次：用户输入文本
2. Runner 运行：第一个智能体调用 LLM，运行工具，任务转移到第二个智能体，第二个智能体运行更多工具，然后产出输出。

在智能体运行结束时，你可以选择向用户展示什么。例如，你可以展示智能体生成的每个新项，或仅展示最终输出。无论哪种方式，用户都可能继续追问，此时你可以再次调用 run 方法。

#### 手动会话管理

你可以通过 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法手动管理会话历史，以获取下一轮输入：

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

#### 使用 Sessions 的自动会话管理

更简单的方法是使用 [Sessions](sessions/index.md) 自动处理会话历史，无需手动调用 `.to_input_list()`：

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

更多细节请参见[Sessions 文档](sessions/index.md)。


#### 服务端管理会话

你也可以让 OpenAI 会话状态功能在服务端管理会话状态，而不是通过 `to_input_list()` 或 `Sessions` 在本地处理。这使你无需手动重发全部历史消息即可保留会话历史。对于下述任一服务端管理方式，每次请求仅传入新轮次输入并复用已保存 ID。更多细节请参见 [OpenAI 会话状态指南](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供了两种跨轮次跟踪状态的方式：

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

另一个选项是**响应链式连接**，即每一轮都显式关联到上一轮的 response ID。

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

如果运行因审批而暂停，并且你从 [`RunState`][agents.run_state.RunState] 恢复，
SDK 会保留保存的 `conversation_id` / `previous_response_id` / `auto_previous_response_id`
设置，使恢复后的轮次在同一个服务端管理会话中继续进行。

`conversation_id` 和 `previous_response_id` 互斥。当你希望使用可跨系统共享的命名会话资源时使用 `conversation_id`。当你希望使用从一轮到下一轮最轻量的 Responses API 续接原语时使用 `previous_response_id`。

!!! note

    SDK 会自动重试 `conversation_locked` 错误并使用退避策略。在服务端管理
    会话的运行中，它会在重试前回退内部的会话跟踪器输入，以便可干净地
    重新发送相同的已准备项。

    在本地基于 session 的运行中（不能与 `conversation_id`、
    `previous_response_id` 或 `auto_previous_response_id` 结合使用），SDK 也会尽力
    回滚最近持久化的输入项，以减少重试后重复历史条目。

    即使你没有配置 `ModelSettings.retry`，此兼容性重试也会发生。有关模型请求
    更广泛的可选重试行为，请参见[Runner 管理重试](models/index.md#runner-managed-retries)。

## 钩子与自定义

### 调用模型输入过滤器

使用 `call_model_input_filter` 在模型调用前编辑模型输入。该钩子接收当前智能体、上下文以及合并后的输入项（若存在 session 历史则包含其内容），并返回新的 `ModelInputData`。

返回值必须是 [`ModelInputData`][agents.run.ModelInputData] 对象。其中 `input` 字段是必填项，且必须为输入项列表。返回任何其他结构都会抛出 `UserError`。

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

runner 会将准备好的输入列表副本传递给该钩子，因此你可以裁剪、替换或重排输入，而无需原地修改调用方原始列表。

如果你使用 session，`call_model_input_filter` 会在 session 历史已加载并与当前轮次合并后运行。若你希望自定义更早阶段的合并步骤，请使用 [`session_input_callback`][agents.run.RunConfig.session_input_callback]。

如果你使用 `conversation_id`、`previous_response_id` 或 `auto_previous_response_id` 的 OpenAI 服务端管理会话状态，该钩子会作用于下一次 Responses API 调用的已准备 payload。该 payload 可能已经只是新轮次增量，而不是完整重放早期历史。你返回的项才会被标记为该服务端管理续接中的已发送内容。

通过 `run_config` 按次设置此钩子，以便脱敏敏感数据、裁剪过长历史或注入额外系统指导。

## 错误与恢复

### 错误处理器

所有 `Runner` 入口点都接受 `error_handlers`，这是一个按错误类型键控的字典。当前支持的键是 `"max_turns"`。当你希望返回可控的最终输出而不是抛出 `MaxTurnsExceeded` 时可使用它。

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

当你不希望回退输出被追加到会话历史时，设置 `include_in_history=False`。

## 持久化执行集成与 human-in-the-loop

对于工具审批暂停/恢复模式，请先阅读专门的[Human-in-the-loop 指南](human_in_the_loop.md)。
以下集成用于运行可能跨越长时间等待、重试或进程重启的持久化编排场景。

### Temporal

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行持久化的长时间工作流，包括 human-in-the-loop 任务。你可以在[此视频](https://www.youtube.com/watch?v=fFBZqzT4DD8)中查看 Temporal 与 Agents SDK 协作完成长时任务的演示，也可以[在此查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

### Restate

你可以使用 Agents SDK 的 [Restate](https://restate.dev/) 集成来构建轻量、持久化智能体，支持人工审批、任务转移与会话管理。该集成依赖 Restate 的单二进制运行时，并支持将智能体作为进程/容器或无服务器函数运行。
更多细节请阅读[概览](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)或查看[文档](https://docs.restate.dev/ai)。

### DBOS

你可以使用 Agents SDK 的 [DBOS](https://dbos.dev/) 集成来运行可靠智能体，在故障与重启间保留进度。它支持长时间运行的智能体、human-in-the-loop 工作流与任务转移。它同时支持同步与异步方法。该集成仅需 SQLite 或 Postgres 数据库。更多细节请查看集成 [repo](https://github.com/dbos-inc/dbos-openai-agents) 和[文档](https://docs.dbos.dev/integrations/openai-agents)。

## 异常

SDK 在某些情况下会抛出异常。完整列表见 [`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：这是 SDK 内抛出的所有异常的基类。它作为通用类型，其他具体异常均派生自它。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时抛出。它表示智能体无法在指定交互轮次数内完成任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）生成意外或无效输出时发生。包括：
    -   JSON 格式错误：当模型为工具调用或直接输出提供了格式错误的 JSON 结构时，尤其是定义了特定 `output_type` 的情况下。
    -   与工具相关的意外失败：当模型未按预期方式使用工具时
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：当工具调用超过配置超时时间且工具使用 `timeout_behavior="raise_exception"` 时抛出。
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时出错而抛出。通常由代码实现不正确、配置无效或误用 SDK API 导致。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入安全防护措施或输出安全防护措施的触发条件分别满足时抛出。输入安全防护措施在处理前检查传入消息，输出安全防护措施在交付前检查智能体最终响应。