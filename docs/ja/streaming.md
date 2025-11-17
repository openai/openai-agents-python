---
search:
  exclude: true
---
# ストリーミング

ストリーミングにより、エージェント の実行が進むにつれて更新を購読できます。これは エンドユーザー に進捗更新や部分的な応答を表示するのに有用です。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出して [`RunResultStreaming`][agents.result.RunResultStreaming] を受け取ります。`result.stream_events()` を呼ぶと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームを得られます。

## raw レスポンスイベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は LLM から直接渡される raw イベントです。OpenAI Responses API の形式であり、各イベントはタイプ（`response.created`、`response.output_text.delta` など）とデータを持ちます。これらのイベントは、生成され次第 レスポンスメッセージを ユーザー に ストリーミング したい場合に有用です。

たとえば、これは LLM が生成するテキストをトークンごとに出力します。

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

## 実行アイテムイベントとエージェントイベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] はより高レベルのイベントです。アイテムが完全に生成されたタイミングを通知します。これにより、各トークンではなく「メッセージが生成された」「ツールが実行された」といった粒度で進捗更新をプッシュできます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェント が変更された際（たとえば ハンドオフ の結果として）の更新を通知します。

たとえば、これは raw イベントを無視して、 ユーザー に更新を ストリーミング します。

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