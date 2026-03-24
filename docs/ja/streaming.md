---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使うと、エージェントの実行が進行する間の更新を購読できます。これは、エンドユーザーに進捗更新や部分的な応答を表示するのに役立ちます。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより [`RunResultStreaming`][agents.result.RunResultStreaming] が得られます。`result.stream_events()` を呼び出すと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームが得られます。

非同期イテレーターが終了するまで `result.stream_events()` の消費を続けてください。ストリーミング実行は、イテレーターが終了するまで完了しません。また、セッション永続化、承認の記録管理、履歴の圧縮といった後処理は、最後の可視トークン到着後に完了する場合があります。ループを抜けた時点で、`result.is_complete` が最終的な実行状態を反映します。

## raw response イベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw イベントです。これらは OpenAI Responses API 形式であり、各イベントはタイプ（`response.created`、`response.output_text.delta` など）とデータを持ちます。これらのイベントは、生成され次第すぐにレスポンスメッセージをユーザーへストリーミングしたい場合に有用です。

コンピュータツールの raw イベントは、保存済み結果と同じく preview と GA の区別を維持します。Preview フローでは 1 つの `action` を含む `computer_call` アイテムをストリーミングし、`gpt-5.4` ではバッチ化された `actions[]` を含む `computer_call` アイテムをストリーミングできます。より高レベルの [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] サーフェスでは、このためのコンピュータ専用イベント名は追加されません。どちらの形も引き続き `tool_called` として表出し、スクリーンショット結果は `computer_call_output` アイテムをラップした `tool_output` として返されます。

たとえば、これは LLM が生成するテキストをトークン単位で出力します。

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

ストリーミングは、ツール承認のために一時停止する実行とも互換性があります。ツールに承認が必要な場合、`result.stream_events()` は終了し、保留中の承認は [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] に公開されます。`result.to_state()` で結果を [`RunState`][agents.run_state.RunState] に変換し、割り込みを承認または拒否してから、`Runner.run_streamed(...)` で再開します。

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

一時停止 / 再開の完全な手順は、[human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

## 現在のターン後のストリーミングキャンセル

ストリーミング実行を途中で停止する必要がある場合は、[`result.cancel()`][agents.result.RunResultStreaming.cancel] を呼び出します。デフォルトでは、これにより実行は即時停止します。停止前に現在のターンをきれいに完了させるには、代わりに `result.cancel(mode="after_turn")` を呼び出してください。

ストリーミング実行は、`result.stream_events()` が終了するまで完了しません。SDK は、最後の可視トークンの後でも、セッション項目の永続化、承認状態の確定、履歴の圧縮を続ける場合があります。

[`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list] から手動で継続していて、`cancel(mode="after_turn")` がツールターン後に停止した場合は、新しいユーザーターンをすぐ追加するのではなく、その正規化済み入力で `result.last_agent` を再実行して未完了ターンを継続してください。
-   ストリーミング実行がツール承認で停止した場合、それを新しいターンとして扱わないでください。ストリームの消費を最後まで完了し、`result.interruptions` を確認してから、`result.to_state()` から再開してください。
-   次のモデル呼び出し前に、取得したセッション履歴と新しいユーザー入力をどのようにマージするかをカスタマイズするには [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。そこで新規ターン項目を書き換えた場合、そのターンで永続化されるのは書き換え後のバージョンです。

## 実行項目イベントとエージェントイベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] はより高レベルのイベントです。項目が完全に生成されたときに通知します。これにより、各トークン単位ではなく、「メッセージ生成済み」「ツール実行済み」などのレベルで進捗更新を送れます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変わったとき（例: ハンドオフの結果）に更新を提供します。

### 実行項目イベント名

`RunItemStreamEvent.name` は、固定のセマンティックなイベント名セットを使用します。

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

ホスト型ツール検索を使用すると、モデルがツール検索リクエストを発行したときに `tool_search_called` が発行され、Responses API が読み込まれたサブセットを返したときに `tool_search_output_created` が発行されます。

たとえば、これは raw イベントを無視して、ユーザーへの更新をストリーミングします。

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