---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使うと、エージェント実行の進行に合わせて更新を購読できます。これは、エンドユーザーに進捗更新や部分的な応答を表示するのに役立ちます。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより、[`RunResultStreaming`][agents.result.RunResultStreaming] が返されます。`result.stream_events()` を呼ぶと、後述の [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームが得られます。

## Raw レスポンスイベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw なイベントです。形式は OpenAI Responses API で、各イベントは `response.created`、`response.output_text.delta` などのタイプとデータを持ちます。これらのイベントは、生成され次第ユーザーに応答メッセージをストリーミングしたい場合に有用です。

例えば、以下は LLM が生成したテキストをトークンごとに出力します。

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

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルのイベントです。アイテムが完全に生成されたタイミングを知らせます。これにより、各トークンではなく「メッセージが生成された」「ツールが実行された」といったレベルで進捗更新をプッシュできます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変化したとき（例: ハンドオフの結果として）に更新を提供します。

例えば、以下は raw イベントを無視し、ユーザーに更新のみをストリーミングします。

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