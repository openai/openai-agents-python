---
search:
  exclude: true
---
# 流式传输

流式传输让你能够订阅智能体运行过程中的更新。这对于向最终用户展示进度更新和部分响应非常有用。

若要进行流式传输，可以调用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，它会返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。调用 `result.stream_events()` 会得到一个由 [`StreamEvent`][agents.stream_events.StreamEvent] 对象组成的异步流，这些对象将在下文介绍。

请持续消费 `result.stream_events()`，直到异步迭代器结束。只有当迭代器结束时，一次流式运行才算完成；会话持久化、审批记账或历史压缩等后处理可能会在最后一个可见 token 到达后才完成。当循环退出时，`result.is_complete` 会反映最终运行状态。

## 原始响应事件

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] 是直接从 LLM 传递过来的原始事件。它们采用 OpenAI Responses API 格式，这意味着每个事件都有一个类型（例如 `response.created`、`response.output_text.delta` 等）和数据。如果你希望在响应消息生成后立即将其流式传输给用户，这些事件会很有用。

计算机工具原始事件会保留与已存储结果相同的 Preview 与 GA 区分。Preview 流会流式传输带有一个 `action` 的 `computer_call` 项，而 `gpt-5.5` 可以流式传输带有批量 `actions[]` 的 `computer_call` 项。更高层级的 [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 表面不会为此添加特殊的仅限计算机的事件名称：两种形态仍然都会以 `tool_called` 的形式呈现，截图结果则会以 `tool_output` 的形式返回，并包装一个 `computer_call_output` 项。

例如，这将逐个 token 输出 LLM 生成的文本。

```python
import asyncio
from openai.types.responses import ResponseTextDeltaEvent
from agents import Agent, Runner

async def main():
    agent = Agent(
        name="Joker",
        instructions="You are a helpful assistant.",
    )

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
```

## 流式传输与审批

流式传输与会暂停以等待工具审批的运行兼容。如果某个工具需要审批，`result.stream_events()` 会结束，待处理的审批会通过 [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 暴露。使用 `result.to_state()` 将结果转换为 [`RunState`][agents.run_state.RunState]，批准或拒绝该中断，然后使用 `Runner.run_streamed(...)` 恢复运行。

```python
result = Runner.run_streamed(agent, "Delete temporary files if they are no longer needed.")
async for _event in result.stream_events():
    pass

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = Runner.run_streamed(agent, state)
    async for _event in result.stream_events():
        pass
```

有关完整的暂停/恢复演练，请参阅 [human-in-the-loop 指南](human_in_the_loop.md)。

## 当前轮次后的流式传输取消

如果需要在中途停止一次流式运行，请调用 [`result.cancel()`][agents.result.RunResultStreaming.cancel]。默认情况下，这会立即停止运行。若要让当前轮次在停止前干净地完成，请改为调用 `result.cancel(mode="after_turn")`。

在 `result.stream_events()` 完成之前，流式运行并未完成。在最后一个可见 token 之后，SDK 可能仍在持久化会话项、最终确定审批状态或压缩历史。

如果你正在从 [`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list] 手动继续，并且 `cancel(mode="after_turn")` 在一次工具轮次后停止，请通过使用该规范化输入重新运行 `result.last_agent` 来继续那个未完成的轮次，而不是立即追加一个新的用户轮次。
-   如果流式运行因工具审批而停止，不要将其视为一个新轮次。请先完全消费流，检查 `result.interruptions`，然后改为从 `result.to_state()` 恢复。
-   使用 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] 来自定义在下一次模型调用之前，如何合并检索到的会话历史与新的用户输入。如果你在那里重写新轮次项，那么被重写的版本就是该轮次会持久化的内容。

## 运行项事件与智能体事件

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 是更高层级的事件。它们会在某个项完全生成后通知你。这使你可以按“消息已生成”“工具已运行”等级别向用户推送进度更新，而不是按每个 token 推送。类似地，[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] 会在当前智能体发生变化时（例如由于任务转移）向你提供更新。

### 运行项事件名称

`RunItemStreamEvent.name` 使用一组固定的语义事件名称：

-   `message_output_created`
-   `handoff_requested`
-   `handoff_occured`
-   `tool_called`
-   `tool_search_called`
-   `tool_search_output_created`
-   `tool_output`
-   `reasoning_item_created`
-   `mcp_approval_requested`
-   `mcp_approval_response`
-   `mcp_list_tools`

`handoff_occured` 为了向后兼容而有意拼写错误。

当你使用托管工具搜索时，模型发出工具搜索请求时会发出 `tool_search_called`，Responses API 返回已加载的子集时会发出 `tool_search_output_created`。

例如，这将忽略原始事件，并将更新流式传输给用户。

```python
import asyncio
import random
from agents import Agent, ItemHelpers, Runner, function_tool

@function_tool
def how_many_jokes() -> int:
    return random.randint(1, 10)


async def main():
    agent = Agent(
        name="Joker",
        instructions="First call the `how_many_jokes` tool, then tell that many jokes.",
        tools=[how_many_jokes],
    )

    result = Runner.run_streamed(
        agent,
        input="Hello",
    )
    print("=== Run starting ===")

    async for event in result.stream_events():
        # We'll ignore the raw responses event deltas
        if event.type == "raw_response_event":
            continue
        # When the agent updates, print that
        elif event.type == "agent_updated_stream_event":
            print(f"Agent updated: {event.new_agent.name}")
            continue
        # When items are generated, print them
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                print("-- Tool was called")
            elif event.item.type == "tool_call_output_item":
                print(f"-- Tool output: {event.item.output}")
            elif event.item.type == "message_output_item":
                print(f"-- Message output:\n {ItemHelpers.text_message_output(event.item)}")
            else:
                pass  # Ignore other event types

    print("=== Run complete ===")


if __name__ == "__main__":
    asyncio.run(main())
```