# ストリーミング

ストリーミングを使用すると、エージェントの実行中にその進行状況の更新を購読できます。これは、エンドユーザーに進捗状況や部分的な応答を表示する際に役立ちます。

ストリーミングを行うには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより [`RunResultStreaming`][agents.result.RunResultStreaming] が返されます。`result.stream_events()` を呼び出すと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームが得られます。

## raw response events

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw イベントです。これらは OpenAI Responses API の形式であり、各イベントはタイプ（`response.created` や `response.output_text.delta` など）とデータを持ちます。これらのイベントは、生成された応答メッセージを即座にユーザーにストリーミングしたい場合に役立ちます。

例えば、以下のコードは LLM が生成したテキストをトークンごとに出力します。

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

## Run item events と agent events

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルのイベントです。これらはアイテムが完全に生成された際に通知されます。これにより、トークン単位ではなく、「メッセージが生成された」「ツールが実行された」などのレベルで進捗状況をユーザーに通知できます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変更された際（例えばハンドオフの結果として）に更新を通知します。

例えば、以下のコードは raw イベントを無視し、更新情報のみをユーザーにストリーミングします。

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
        # raw response イベントのデルタは無視します
        if event.type == "raw_response_event":
            continue
        # エージェントが更新された場合、それを表示します
        elif event.type == "agent_updated_stream_event":
            print(f"Agent updated: {event.new_agent.name}")
            continue
        # アイテムが生成された場合、それを表示します
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                print("-- Tool was called")
            elif event.item.type == "tool_call_output_item":
                print(f"-- Tool output: {event.item.output}")
            elif event.item.type == "message_output_item":
                print(f"-- Message output:\n {ItemHelpers.text_message_output(event.item)}")
            else:
                pass  # 他のイベントタイプは無視します

    print("=== Run complete ===")


if __name__ == "__main__":
    asyncio.run(main())
```