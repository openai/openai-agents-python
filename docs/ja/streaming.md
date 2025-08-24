---
search:
  exclude: true
---
# ストリーミング

ストリーミング を使用すると、エージェント の実行の進行に合わせて更新を購読できます。これはエンドユーザーに進捗更新や部分的な応答を表示するのに有用です。

ストリーム配信を行うには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これは [`RunResultStreaming`][agents.result.RunResultStreaming] を返します。`result.stream_events()` を呼ぶと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの async ストリームが得られます。

## raw レスポンスイベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw なイベントです。これは OpenAI Responses API 形式であり、各イベントには種類（`response.created`、`response.output_text.delta` など）とデータがあります。生成され次第、応答メッセージを ユーザー にストリーミングしたい場合に便利です。

たとえば、次の例では LLM が生成したテキストをトークンごとに出力します。

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

## 実行アイテムイベントと エージェント のイベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルのイベントです。アイテムが完全に生成されたタイミングを知らせます。これにより、各トークン単位ではなく、「メッセージが生成された」「ツールが実行された」といったレベルで進捗更新を配信できます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在の エージェント が変更されたとき（たとえば ハンドオフ の結果として）の更新を通知します。

たとえば、次の例では raw イベントを無視し、ユーザー への更新のみをストリーミングします。

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