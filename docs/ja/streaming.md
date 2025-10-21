---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使うと、エージェントの実行が進むにつれて更新を購読できます。これは、エンドユーザーに進行状況の更新や部分的な応答を表示するのに役立ちます。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより、[`RunResultStreaming`][agents.result.RunResultStreaming] が得られます。`result.stream_events()` を呼び出すと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームを受け取れます。

## raw レスポンスイベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw なイベントです。OpenAI Responses API フォーマットであり、各イベントには `response.created` や `response.output_text.delta` などのタイプとデータがあります。これらのイベントは、生成され次第、応答メッセージをユーザーにストリーミングしたい場合に有用です。

例えば、次のコードは LLM が生成するテキストをトークンごとに出力します。

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

## Run アイテムイベントと エージェントイベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルなイベントです。アイテムが完全に生成されたタイミングを通知します。これにより、各トークンではなく、「メッセージが生成された」「ツールが実行された」などのレベルで進捗をプッシュできます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変更されたとき（例: ハンドオフの結果として）に更新を提供します。

例えば、次のコードは raw イベントを無視し、ユーザーに更新をストリーミングします。

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

## ツール出力ストリーミングイベント

[`ToolOutputStreamEvent`][agents.stream_events.ToolOutputStreamEvent] を使用すると、ツールの実行中に増分出力を受け取ることができます。これは、長時間実行されるツールでユーザーにリアルタイムで進捗を表示したい場合に有用です。

ストリーミングツールを作成するには、文字列チャンクを yield する非同期ジェネレータ関数を定義します：

```python
import asyncio
from collections.abc import AsyncIterator
from agents import Agent, Runner, ToolOutputStreamEvent, function_tool

@function_tool
async def search_documents(query: str) -> AsyncIterator[str]:
    """ドキュメントを検索し、見つかった結果をストリーミングします。"""
    documents = [
        f"ドキュメント 1 には {query} に関する情報が含まれています...\n",
        f"ドキュメント 2 には {query} に関する追加の詳細があります...\n",
        f"ドキュメント 3 は {query} の分析を提供します...\n",
    ]
    
    for doc in documents:
        # 処理時間をシミュレート
        await asyncio.sleep(0.5)
        # 増分結果を yield
        yield doc


async def main():
    agent = Agent(
        name="Research Assistant",
        instructions="あなたはユーザーの情報検索を支援します。",
        tools=[search_documents],
    )

    result = Runner.run_streamed(
        agent,
        input="AI に関する情報を検索してください",
    )

    async for event in result.stream_events():
        # ツールストリーミングイベントを処理
        if event.type == "tool_output_stream_event":
            print(f"[{event.tool_name}] {event.delta}", end="", flush=True)
        # 最終ツール出力を処理
        elif event.type == "run_item_stream_event" and event.name == "tool_output":
            print(f"\n✓ ツール完了\n")


if __name__ == "__main__":
    asyncio.run(main())
```

ストリーミングツールに関する重要なポイント：

- ストリーミングツールは `AsyncIterator[str]`（文字列を yield する非同期ジェネレータ）を返す必要があります
- yield された各チャンクは `ToolOutputStreamEvent` として発行されます
- すべてのチャンクは自動的に蓄積され、最終的なツール出力として LLM に送信されます
- 非ストリーミングツールはストリーミングツールと一緒に正常に動作します
- 非ストリーミングモード（`Runner.run()`）では、ストリーミングツールは返す前にすべてのチャンクを自動的に収集します