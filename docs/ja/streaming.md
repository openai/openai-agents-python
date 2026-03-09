---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使用すると、エージェント実行の進行に合わせた更新を購読できます。これは、エンドユーザーに進捗更新や部分的な応答を表示するのに役立ちます。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより [`RunResultStreaming`][agents.result.RunResultStreaming] が返されます。`result.stream_events()` を呼び出すと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの async ストリームが得られます。

async イテレーターが終了するまで、`result.stream_events()` の消費を続けてください。ストリーミング実行は、イテレーターが終了するまで完了しません。セッション永続化、承認記録、履歴圧縮などの後処理は、最後の可視トークン到着後に完了する場合があります。ループ終了時に、`result.is_complete` は最終的な実行状態を反映します。

## raw 応答イベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw イベントです。これらは OpenAI Responses API 形式であり、各イベントは type（`response.created`、`response.output_text.delta` など）と data を持ちます。これらのイベントは、応答メッセージを生成され次第すぐにユーザーへストリーミングしたい場合に有用です。

コンピュータツールの raw イベントは、保存された結果と同じく preview と GA の区別を維持します。Preview フローは 1 つの `action` を持つ `computer_call` 項目をストリーミングしますが、`gpt-5.4` はバッチ化された `actions[]` を持つ `computer_call` 項目をストリーミングできます。より高レベルな [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] の表層では、これに対してコンピュータ専用の特別なイベント名は追加されません。どちらの形も引き続き `tool_called` として表れ、スクリーンショット結果は `computer_call_output` 項目をラップした `tool_output` として返されます。

たとえば、以下は LLM が生成したテキストをトークン単位で出力します。

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

## ストリーミングと承認

ストリーミングは、ツール承認のために一時停止する実行と互換性があります。ツールが承認を必要とする場合、`result.stream_events()` は終了し、保留中の承認は [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] に公開されます。結果を `result.to_state()` で [`RunState`][agents.run_state.RunState] に変換し、割り込みを承認または拒否してから、`Runner.run_streamed(...)` で再開してください。

```python
result = Runner.run_streamed(agent, "Delete temporary files if they are no longer needed.")
async for _event in result.stream_events():
    pass

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = Runner.run_streamed(agent, state)
    async for _event in result.stream_events():
        pass
```

一時停止 / 再開の完全な手順については、[human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

## 実行項目イベントとエージェントイベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルのイベントです。これは、項目が完全に生成されたタイミングを通知します。これにより、各トークンではなく「メッセージ生成」「ツール実行」などのレベルで進捗更新を送れます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変わったとき（例: ハンドオフの結果）に更新を提供します。

### 実行項目イベント名

`RunItemStreamEvent.name` は、固定された意味的イベント名のセットを使用します。

-   `message_output_created`
-   `handoff_requested`
-   `handoff_occured`
-   `tool_called`
-   `tool_search_called`
-   `tool_search_output_created`
-   `tool_output`
-   `reasoning_item_created`
-   `mcp_approval_requested`
-   `mcp_approval_response`
-   `mcp_list_tools`

`handoff_occured` は、後方互換性のため意図的にスペルミスのままです。

ホストされたツール検索を使用すると、モデルがツール検索リクエストを発行したときに `tool_search_called` が送出され、Responses API が読み込まれたサブセットを返したときに `tool_search_output_created` が送出されます。

たとえば、以下は raw イベントを無視し、ユーザーへの更新をストリーミングします。

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