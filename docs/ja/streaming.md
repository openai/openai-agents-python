---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使用すると、エージェントの実行が進行するにつれて、その更新情報を購読できます。これは、エンドユーザーに進捗状況や途中経過のレスポンスを表示する際に役立ちます。

ストリーミングを行うには、 `Runner.run_streamed()` を呼び出します。これにより `RunResultStreaming` が返されます。続いて `result.stream_events()` を呼び出すと、 `StreamEvent` オブジェクトの async ストリームが取得できます。これらは後述します。

## Raw response イベント

`RawResponsesStreamEvent` は、 LLM から直接渡される raw イベントです。これらは OpenAI Responses API フォーマットであり、各イベントにはタイプ（ `response.created` 、 `response.output_text.delta` など）とデータが含まれます。生成された直後にレスポンスメッセージをユーザーにストリーミングしたい場合に便利です。

たとえば、以下のコードは LLM が生成したテキストをトークンごとに出力します。

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

`RunItemStreamEvent` は、より高レベルのイベントです。アイテムが完全に生成されたタイミングを通知します。これにより、トークンごとの更新ではなく「メッセージが生成された」「ツールが実行された」などの粒度で進捗をユーザーに伝えられます。同様に、 `AgentUpdatedStreamEvent` は、ハンドオフの結果などで現在のエージェントが変化した際に更新を通知します。

例として、以下のコードは raw イベントを無視し、ユーザーへ更新のみをストリーミングします。

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