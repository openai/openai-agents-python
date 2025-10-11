---
search:
  exclude: true
---
# 流式处理

流式处理让你可以订阅智能体运行过程中的更新。这对于向最终用户显示进度更新和部分响应很有用。

要进行流式处理，你可以调用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]，它会返回一个 [`RunResultStreaming`][agents.result.RunResultStreaming]。调用 `result.stream_events()` 会给你一个在下面描述的 [`StreamEvent`][agents.stream_events.StreamEvent] 对象的异步流。

## 原始响应事件

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] 是从LLM直接传递的原始事件。它们采用OpenAI响应API格式，这意味着每个事件都有一个类型（如`response.created`、`response.output_text.delta`等）和数据。当你想在生成响应消息时立即将它们流式传输给用户，这些事件很有用。

例如，这将逐token输出LLM生成的文本。

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

## 运行项目事件和智能体事件

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] 是更高级别的事件。它们通知你某个项目已完全生成的时间。这使你可以在"消息已生成"、"工具已运行"等层面上推送进度更新，而不是每个token。同样，[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] 在当前智能体发生更改时（例如由于交接的结果）为你提供更新。

例如，这将忽略原始事件并向用户流式传输更新。

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
    print("=== 运行开始 ===")

    async for event in result.stream_events():
        # 我们将忽略原始响应事件增量
        if event.type == "raw_response_event":
            continue
        # 当智能体更新时，打印该信息
        elif event.type == "agent_updated_stream_event":
            print(f"智能体已更新: {event.new_agent.name}")
            continue
        # 当项目生成时，打印它们
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                print("-- 工具被调用")
            elif event.item.type == "tool_call_output_item":
                print(f"-- 工具输出: {event.item.output}")
            elif event.item.type == "message_output_item":
                print(f"-- 消息输出:\n {ItemHelpers.text_message_output(event.item)}")
            else:
                pass  # 忽略其他事件类型

    print("=== 运行完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
```