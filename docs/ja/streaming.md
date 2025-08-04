---
search:
  exclude: true
---
# ストリーミング

Streaming を使用すると、エージェント の実行が進行するにつれて送られてくる更新を購読できます。これはエンドユーザー に進捗更新や部分的な応答を表示する際に役立ちます。

ストリーミングを行うには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより [`RunResultStreaming`][agents.result.RunResultStreaming] が返されます。`result.stream_events()` を呼び出すと、下記で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームを受け取れます。

## raw response イベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、 LLM から直接渡される raw なイベントです。OpenAI Responses API フォーマットであり、各イベントには `response.created` や `response.output_text.delta` などの type と data が含まれます。生成されたメッセージを即座にユーザー にストリーム配信したい場合に便利です。

例えば、以下のコードは LLM が生成したテキストをトークン単位で出力します。

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

## Run item イベントとエージェント イベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルのイベントであり、アイテムが完全に生成されたときに通知します。これにより、トークン単位ではなく「メッセージが生成された」「ツールが実行された」といったレベルで進捗をプッシュできます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、ハンドオフ の結果などで現在のエージェント が変更されたときに更新を提供します。

以下の例では raw イベントを無視し、ユーザー に更新のみをストリーム配信します。

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