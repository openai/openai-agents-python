---
search:
  exclude: true
---
# ストリーミング

ストリーミングにより、エージェントの実行が進むにつれて更新を購読できます。これは、エンドユーザーに進捗状況の更新や部分的なレスポンスを表示する場合に役立ちます。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより [`RunResultStreaming`][agents.result.RunResultStreaming] が返されます。`result.stream_events()` を呼び出すと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームが得られます。

非同期イテレーターが終了するまで、`result.stream_events()` を消費し続けてください。ストリーミング実行は、イテレーターが終了するまで完了しません。また、セッションの永続化、承認の記録管理、履歴の圧縮などの後処理は、最後の可視トークンが到着した後に完了する場合があります。ループが終了すると、`result.is_complete` は最終的な実行状態を反映します。

## raw レスポンスイベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw イベントです。これらは OpenAI Responses API 形式であり、各イベントには型（`response.created`、`response.output_text.delta` など）とデータがあります。これらのイベントは、レスポンスメッセージが生成され次第、ユーザーにストリーミングしたい場合に役立ちます。

コンピュータツールの raw イベントは、保存された実行結果と同じ preview と GA の区別を維持します。Preview フローでは、1 つの `action` を持つ `computer_call` アイテムをストリーミングします。一方、`gpt-5.5` では、バッチ化された `actions[]` を持つ `computer_call` アイテムをストリーミングできます。高レベルの [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] サーフェスは、このために特別なコンピュータ専用イベント名を追加しません。どちらの形式も引き続き `tool_called` として表面化し、スクリーンショットの実行結果は `computer_call_output` アイテムをラップする `tool_output` として返されます。

たとえば、これは LLM によって生成されたテキストをトークンごとに出力します。

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

ストリーミングは、ツール承認のために一時停止する実行と互換性があります。ツールに承認が必要な場合、`result.stream_events()` は終了し、保留中の承認は [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。`result.to_state()` を使って実行結果を [`RunState`][agents.run_state.RunState] に変換し、中断を承認または拒否してから、`Runner.run_streamed(...)` で再開します。

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

一時停止/再開の完全なウォークスルーについては、[human-in-the-loop ガイド](human_in_the_loop.md)を参照してください。

## 現在のターン後のストリーミングのキャンセル

途中でストリーミング実行を停止する必要がある場合は、[`result.cancel()`][agents.result.RunResultStreaming.cancel] を呼び出します。デフォルトでは、これにより実行はすぐに停止します。停止する前に現在のターンを正常に完了させるには、代わりに `result.cancel(mode="after_turn")` を呼び出します。

ストリーミング実行は、`result.stream_events()` が終了するまで完了しません。最後の可視トークンの後も、SDK がセッションアイテムを永続化したり、承認状態を確定したり、履歴を圧縮したりしている場合があります。

[`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list] から手動で継続しており、`cancel(mode="after_turn")` がツールターンの後で停止した場合は、すぐに新しいユーザーターンを追加するのではなく、その正規化された入力で `result.last_agent` を再実行して、未完了のターンを継続してください。
-   ストリーミング実行がツール承認のために停止した場合、それを新しいターンとして扱わないでください。ストリームの読み出しを最後まで完了し、`result.interruptions` を確認して、代わりに `result.to_state()` から再開してください。
-   次のモデル呼び出しの前に、取得したセッション履歴と新しいユーザー入力をどのようにマージするかをカスタマイズするには、[`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。そこで新しいターンのアイテムを書き換えた場合、その書き換え後のバージョンがそのターンとして永続化されます。

## 実行アイテムイベントとエージェントイベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルのイベントです。アイテムが完全に生成されたタイミングを通知します。これにより、各トークン単位ではなく、「メッセージが生成された」「ツールが実行された」などのレベルで進捗更新を送信できます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変更されたとき（例: ハンドオフの結果として）に更新を提供します。

### 実行アイテムイベント名

`RunItemStreamEvent.name` は、固定された一連のセマンティックなイベント名を使用します。

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

`handoff_occured` は、後方互換性のため意図的にスペルミスのままになっています。

ホストされたツール検索を使用する場合、モデルがツール検索リクエストを発行すると `tool_search_called` が送出され、Responses API が読み込まれたサブセットを返すと `tool_search_output_created` が送出されます。

たとえば、これは raw イベントを無視し、更新をユーザーにストリーミングします。

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