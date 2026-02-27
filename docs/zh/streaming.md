---
search:
  exclude: true
---
# 流式传输

流式传输让你可以在智能体运行过程中订阅其更新。这对于向最终用户展示进度更新和部分响应非常有用。

要进行流式传输，你可以调用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，它会返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。调用 `result.stream_events()` 会为你提供一个由 [`StreamEvent`][agents.stream_events.StreamEvent] 对象组成的异步流，下面会进行说明。

## 原始响应事件

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] 是直接从 LLM 传递过来的原始事件。它们采用 OpenAI Responses API 格式，这意味着每个事件都有一个类型（如 `response.created`、`response.output_text.delta` 等）和数据。如果你希望在响应消息生成后立即流式传给用户，这些事件会很有用。

例如，下面会按 token 逐个输出 LLM 生成的文本。

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

## 运行项事件与智能体事件

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 是更高层级的事件。它们会在某个项完全生成后通知你。这使你可以按“消息已生成”“工具已运行”等层级推送进度更新，而不是按每个 token 推送。类似地，[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] 会在当前智能体发生变化时向你提供更新（例如由于任务转移）。

### 运行项事件名称

`RunItemStreamEvent.name` 使用一组固定的语义事件名称：

-   `message_output_created`
-   `handoff_requested`
-   `handoff_occured`
-   `tool_called`
-   `tool_output`
-   `reasoning_item_created`
-   `mcp_approval_requested`
-   `mcp_approval_response`
-   `mcp_list_tools`

`handoff_occured` 为了向后兼容而有意保留了拼写错误。

例如，下面会忽略原始事件，并向用户流式传输更新。

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