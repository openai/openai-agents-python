---
search:
  exclude: true
---
# ストリーミング

ストリーミングを利用すると、エージェント実行の進行に合わせて更新を購読できます。これはエンドユーザーに進捗状況や途中結果を表示する際に役立ちます。

ストリーミングを行うには、`Runner.run_streamed()` を呼び出します。これにより `RunResultStreaming` が返されます。続いて `result.stream_events()` を呼ぶと、`StreamEvent` オブジェクトの非同期ストリームを取得できます。詳しくは後述します。

## raw レスポンスイベント

`RawResponsesStreamEvent` は LLM から直接渡される raw イベントです。これらは OpenAI Responses API 形式で、各イベントには `response.created`, `response.output_text.delta` などの type と data が含まれます。生成されたメッセージを即座にユーザーへストリーミングしたい場合に便利です。

例えば、次のコードは LLM が生成したテキストをトークンごとに出力します。

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

## Run item イベントとエージェントイベント

`RunItemStreamEvent` はより高レベルのイベントで、アイテムが完全に生成されたタイミングを知らせます。これにより、各トークンではなく「メッセージが生成された」「ツールが実行された」といった粒度で進捗をプッシュできます。同様に、`AgentUpdatedStreamEvent` はハンドオフなどによって現在のエージェントが変わったときに更新を通知します。

例えば、次のコードは raw イベントを無視し、ユーザーへ更新のみをストリーミングします。

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