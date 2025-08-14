---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使用すると、エージェントの実行が進行するにつれて発生する更新を購読できます。これにより、エンドユーザーに進行状況の更新や部分的なレスポンスを表示するのに役立ちます。

ストリームするには、`Runner.run_streamed()` を呼び出します。これにより `RunResultStreaming` が返されます。返された `result.stream_events()` を呼び出すと、非同期で `StreamEvent` オブジェクトのストリームを取得できます。各オブジェクトの詳細は後述します。

## raw レスポンスイベント

`RawResponsesStreamEvent` は LLM から直接渡される raw なイベントです。これらは OpenAI Responses API 形式で提供されるため、各イベントは type（例: `response.created`, `response.output_text.delta` など）と data を持ちます。これらのイベントは、レスポンスメッセージが生成され次第ユーザーへストリーム配信したい場合に便利です。

例えば、以下の例では LLM が生成したテキストをトークン単位で出力します。

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

## Run アイテムイベントとエージェントイベント

`RunItemStreamEvent` はより高レベルのイベントです。アイテムが完全に生成されたときに通知します。これにより、各トークンではなく「メッセージが生成された」「ツールが実行された」などのレベルで進行状況をプッシュできます。同様に、`AgentUpdatedStreamEvent` は現在のエージェントが変更されたとき（例: ハンドオフの結果として）に更新を提供します。

例えば、以下の例では raw イベントを無視し、ユーザーへ更新のみをストリーム配信します。

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