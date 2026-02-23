---
search:
  exclude: true
---
# 运行智能体

你可以通过 [`Runner`][agents.run.Runner] 类运行智能体。你有 3 个选项：

1. [`Runner.run()`][agents.run.Runner.run]：异步运行并返回 [`RunResult`][agents.result.RunResult]。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同步方法，底层只是运行 `.run()`。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：异步运行并返回 [`RunResultStreaming`][agents.result.RunResultStreaming]。它以流式模式调用 LLM，并在接收到事件时将这些事件流式传输给你。

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

更多信息请参见[结果指南](results.md)。

## 智能体循环

当你在 `Runner` 中使用 run 方法时，你会传入一个起始智能体和输入。输入可以是字符串（会被视为一条用户消息），也可以是输入项列表（即 OpenAI Responses API 中的 items）。

随后 runner 会运行一个循环：

1. 我们针对当前智能体、使用当前输入调用 LLM。
2. LLM 生成其输出。
    1. 如果 LLM 返回 `final_output`，循环结束并返回结果。
    2. 如果 LLM 发生任务转移，我们更新当前智能体和输入，并重新运行循环。
    3. 如果 LLM 生成工具调用，我们执行这些工具调用，追加结果，并重新运行循环。
3. 如果超过传入的 `max_turns`，我们会抛出 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 异常。

!!! note

    判断 LLM 输出是否被视为“最终输出”的规则是：它产生了具有目标类型的文本输出，并且没有工具调用。

## 流式传输

流式传输允许你在 LLM 运行期间额外接收流式事件。流结束后，[`RunResultStreaming`][agents.result.RunResultStreaming] 将包含本次运行的完整信息，包括所有新生成的输出。你可以调用 `.stream_events()` 获取流式事件。更多信息请参见[流式传输指南](streaming.md)。

### Responses WebSocket 传输（可选辅助功能）

如果你启用 OpenAI Responses 的 websocket 传输，你仍然可以继续使用常规的 `Runner` API。websocket session helper 推荐用于连接复用，但不是必需的。

这是通过 websocket 传输的 Responses API，而不是 [Realtime API](realtime/guide.md)。

#### 模式 1：不使用 session helper（可用）

当你只需要 websocket 传输、且不需要 SDK 为你管理共享 provider/session 时使用。

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

这种模式适用于单次运行。如果你反复调用 `Runner.run()` / `Runner.run_streamed()`，除非你手动复用相同的 `RunConfig` / provider 实例，否则每次运行可能都会重新连接。

#### 模式 2：使用 `responses_websocket_session()`（推荐用于多轮复用）

当你希望在多次运行之间共享一个支持 websocket 的 provider 和 `RunConfig` 时（包括继承同一 `run_config` 的嵌套 Agents-as-tools 调用），使用 [`responses_websocket_session()`][agents.responses_websocket_session]。

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

在上下文退出之前完成对流式结果的消费。如果在 websocket 请求仍在进行时退出上下文，可能会强制关闭共享连接。

## Run config

`run_config` 参数允许你为智能体运行配置一些全局设置：

-   [`model`][agents.run.RunConfig.model]：允许设置一个全局 LLM 模型，而不管每个 Agent 自身设置的 `model` 是什么。
-   [`model_provider`][agents.run.RunConfig.model_provider]：用于查找模型名称的模型 provider，默认为 OpenAI。
-   [`model_settings`][agents.run.RunConfig.model_settings]：覆盖智能体级别的设置。例如，你可以设置全局 `temperature` 或 `top_p`。
-   [`session_settings`][agents.run.RunConfig.session_settings]：在运行期间检索历史记录时，覆盖 session 级别的默认值（例如 `SessionSettings(limit=...)`）。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：要在所有运行中包含的输入或输出安全防护措施列表。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：应用于所有任务转移的全局输入过滤器（前提是该任务转移本身尚未设置过滤器）。输入过滤器允许你编辑发送给新智能体的输入。更多细节请参阅 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 的文档。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：可选加入的 beta 功能，会在调用下一个智能体之前将此前的对话记录折叠成一条 assistant 消息。由于我们仍在稳定嵌套任务转移，该功能默认禁用；设为 `True` 以启用，或保持 `False` 以透传原始对话记录。当你不传 `RunConfig` 时，所有 [Runner methods][agents.run.Runner] 都会自动创建一个 `RunConfig`，因此 quickstart 和示例会保持默认关闭；任何显式的 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 回调仍会覆盖它。单个任务转移可以通过 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 覆盖此设置。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：可选 callable，当你选择加入 `nest_handoff_history` 时，每次都会接收已规范化的对话记录（history + handoff items）。它必须返回要转发给下一个智能体的、完全一致的输入项列表，使你无需编写完整的任务转移过滤器也能替换内置摘要。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：允许你为整次运行禁用[追踪](tracing.md)。
-   [`tracing`][agents.run.RunConfig.tracing]：传入 [`TracingConfig`][agents.tracing.TracingConfig] 以覆盖本次运行的 exporter、processor 或追踪元数据。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：配置追踪是否包含潜在敏感数据，例如 LLM 与工具调用的输入/输出。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：为本次运行设置追踪工作流名称、trace ID 和 trace group ID。我们建议至少设置 `workflow_name`。group ID 是可选字段，可用于跨多次运行关联追踪。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：要包含在所有追踪中的元数据。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：在使用 Sessions 时，自定义在每个 turn 之前如何将新的用户输入与 session 历史合并。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：用于在模型调用之前、对已完整准备好的模型输入（instructions 和输入项）进行编辑的 hook，例如裁剪历史或注入系统提示词。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：在审批流程中工具调用被拒绝时，自定义对模型可见的消息。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：控制 runner 将先前输出转换为下一轮模型输入时，是否保留或省略 reasoning item ID。

嵌套任务转移以可选加入的 beta 形式提供。通过传入 `RunConfig(nest_handoff_history=True)` 启用折叠对话记录行为，或设置 `handoff(..., nest_handoff_history=True)` 为特定任务转移启用它。如果你更倾向于保留原始对话记录（默认行为），保持该标志未设置，或提供一个 `handoff_input_filter`（或 `handoff_history_mapper`）来按你的需要精确转发对话。若要在不编写自定义 mapper 的情况下更改生成摘要中使用的包装文本，请调用 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（并使用 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] 恢复默认值）。

### Run config 细节

#### `tool_error_formatter`

使用 `tool_error_formatter` 来自定义在审批流程中工具调用被拒绝时返回给模型的消息。

formatter 会接收 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]，其中包括：

-   `kind`：错误目录。当前为 `"approval_rejected"`。
-   `tool_type`：工具运行时（`"function"`、`"computer"`、`"shell"` 或 `"apply_patch"`）。
-   `tool_name`：工具名称。
-   `call_id`：工具调用 ID。
-   `default_message`：SDK 默认的对模型可见消息。
-   `run_context`：当前运行上下文包装器。

返回一个字符串以替换该消息，或返回 `None` 以使用 SDK 默认值。

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

#### `reasoning_item_id_policy`

当 runner 将历史向前传递时（例如使用 `RunResult.to_input_list()` 或基于 session 的运行），`reasoning_item_id_policy` 控制如何将 reasoning items 转换为下一轮模型输入。

-   `None` 或 `"preserve"`（默认）：保留 reasoning item ID。
-   `"omit"`：从生成的下一轮输入中移除 reasoning item ID。

使用 `"omit"` 主要作为可选加入的缓解措施，用于规避一类 Responses API 400 错误：当发送了带 `id` 的 reasoning item，但缺少其所需的后续 item（例如 `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

这可能发生在多轮智能体运行中：当 SDK 根据先前输出构造后续输入时（包括 session 持久化、server-managed conversation deltas、流式/非流式的后续 turn，以及恢复路径），如果保留了 reasoning item ID，但 provider 要求该 ID 必须与其对应的后续 item 保持配对，就会触发该问题。

设置 `reasoning_item_id_policy="omit"` 会保留 reasoning 内容，但移除 reasoning item 的 `id`，从而避免在 SDK 生成的后续输入中触发该 API 不变量。

作用域说明：

-   这只会改变 SDK 在构建后续输入时生成/转发的 reasoning items。
-   不会改写用户提供的初始输入项。
-   `call_model_input_filter` 仍然可以在该策略应用后，有意重新引入 reasoning ID。

## 对话/聊天线程

调用任何 run 方法都可能导致一个或多个智能体运行（因此可能有一次或多次 LLM 调用），但它表示聊天对话中的一个逻辑 turn。例如：

1. 用户 turn：用户输入文本
2. Runner 运行：第一个智能体调用 LLM、运行工具、任务转移到第二个智能体；第二个智能体运行更多工具，然后生成输出。

在智能体运行结束时，你可以选择展示给用户什么内容。例如，你可以展示智能体生成的每一个新 item，或只展示最终输出。无论哪种方式，用户之后都可能提出追问，这时你可以再次调用 run 方法。

### 手动管理对话

你可以使用 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 方法获取下一轮所需的输入，从而手动管理对话历史：

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

### 使用 Sessions 自动管理对话

为获得更简单的方法，你可以使用 [Sessions](sessions/index.md) 自动处理对话历史，而无需手动调用 `.to_input_list()`：

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
-   为不同的 session ID 维护彼此独立的对话

!!! note

    session 持久化不能与 server-managed conversation 设置
    （`conversation_id`、`previous_response_id` 或 `auto_previous_response_id`）在同一次运行中组合使用。每次调用请选择一种方式。

更多细节请参见 [Sessions 文档](sessions/index.md)。

### Server-managed conversations

你也可以让 OpenAI 的 conversation state 功能在服务端管理对话状态，而不是在本地使用 `to_input_list()` 或 `Sessions` 进行处理。这使你无需手动重新发送所有历史消息也能保留对话历史。更多细节请参见 [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)。

OpenAI 提供两种方式来跨 turn 跟踪状态：

#### 1. 使用 `conversation_id`

你首先通过 OpenAI Conversations API 创建一个对话，然后在后续每次调用中复用其 ID：

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

另一种选择是**响应链式调用（response chaining）**：每个 turn 显式链接到上一轮的 response ID。

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

!!! note

    SDK 会自动对 `conversation_locked` 错误进行带退避的重试。在 server-managed
    conversation 运行中，它会在重试前回滚内部 conversation-tracker 输入，以便能够干净地重新发送
    同一批已准备好的 items。

    在本地基于 session 的运行中（不能与 `conversation_id`、
    `previous_response_id` 或 `auto_previous_response_id` 组合使用），SDK 也会尽力回滚最近持久化的输入项，
    以减少重试后重复的历史记录条目。

## Call model input filter

使用 `call_model_input_filter` 在模型调用之前编辑模型输入。该 hook 会接收当前智能体、上下文以及合并后的输入项（包含存在时的 session 历史），并返回新的 `ModelInputData`。

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

你可以通过 `run_config` 为单次运行设置该 hook，或在 `Runner` 上设置默认值，以便对敏感数据进行脱敏、裁剪过长历史，或注入额外的系统指导。

## 错误处理器

所有 `Runner` 入口点都接受 `error_handlers`：一个以错误 kind 为键的 dict。当前支持的键是 `"max_turns"`。当你希望返回一个可控的最终输出而不是抛出 `MaxTurnsExceeded` 时使用。

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

当你不希望将兜底输出追加到对话历史中时，将 `include_in_history=False`。

## 长时间运行的智能体与 human-in-the-loop

关于工具审批的暂停/恢复模式，请参见专门的 [Human-in-the-loop 指南](human_in_the_loop.md)。

### Temporal

你可以使用 Agents SDK 的 [Temporal](https://temporal.io/) 集成来运行持久化、长时间运行的工作流，包括 human-in-the-loop 任务。可观看 Temporal 与 Agents SDK 协同完成长时间任务的演示视频：[in this video](https://www.youtube.com/watch?v=fFBZqzT4DD8)，并在[此处查看文档](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

### Restate

你可以使用 Agents SDK 的 [Restate](https://restate.dev/) 集成来实现轻量且持久化的智能体，包括人工审批、任务转移以及 session 管理。该集成需要 Restate 的单二进制运行时作为依赖，并支持将智能体作为进程/容器或 serverless functions 运行。
阅读[概览](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)或查看[文档](https://docs.restate.dev/ai)了解更多细节。

### DBOS

你可以使用 Agents SDK 的 [DBOS](https://dbos.dev/) 集成来运行可靠的智能体，以在故障与重启之间保留进度。它支持长时间运行的智能体、human-in-the-loop 工作流以及任务转移。它同时支持同步与异步方法。该集成只需要 SQLite 或 Postgres 数据库。请查看集成 [repo](https://github.com/dbos-inc/dbos-openai-agents) 和[文档](https://docs.dbos.dev/integrations/openai-agents)了解更多细节。

## 异常

SDK 会在某些情况下抛出异常。完整列表见 [`agents.exceptions`][]。概览如下：

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内部抛出的所有异常的基类。它作为通用类型，其他所有特定异常都由它派生。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：当智能体运行超过传递给 `Runner.run`、`Runner.run_sync` 或 `Runner.run_streamed` 方法的 `max_turns` 限制时抛出。它表示智能体无法在指定的交互轮次数内完成任务。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：当底层模型（LLM）产生意外或无效输出时发生。可能包括：
    -   JSON 格式错误：当模型为工具调用或其直接输出提供了格式错误的 JSON 结构，尤其是在定义了特定 `output_type` 时。
    -   与工具相关的意外失败：当模型未按预期方式使用工具时
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：当某次工具调用超过其配置的超时时间，且该工具使用 `timeout_behavior="raise_exception"` 时抛出。
-   [`UserError`][agents.exceptions.UserError]：当你（使用 SDK 编写代码的人）在使用 SDK 时发生错误而抛出。通常源于代码实现不正确、配置无效或误用 SDK API。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：当输入安全防护措施或输出安全防护措施的条件分别被满足时抛出。输入安全防护措施会在处理前检查传入消息；输出安全防护措施会在交付前检查智能体的最终响应。