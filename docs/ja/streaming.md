---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使用すると、エージェント実行の進行中に更新を購読できます。これは、エンドユーザーに進捗状況や部分的な応答を表示する際に便利です。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより、[`RunResultStreaming`][agents.result.RunResultStreaming] が返されます。`result.stream_events()` を呼び出すと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームが返されます。

非同期イテレーターが完了するまで、`result.stream_events()` の取得を続けてください。ストリーミング実行はイテレーターが終了するまで完了せず、セッションの永続化、承認情報の記録、履歴の圧縮などの後処理は、最後に表示されるトークンが到着した後に完了する場合があります。ループが終了すると、`result.is_complete` に最終的な実行状態が反映されます。

## raw レスポンスイベント {#raw-response-events}

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] オブジェクトは、LLM から直接渡された raw イベントをラップします。各オブジェクトの `data` フィールドには、`response.created` や `response.output_text.delta` などの型を持つ OpenAI Responses API イベントが含まれます。これらのイベントは、応答メッセージが生成され次第、ユーザーにストリーミングしたい場合に便利です。

コンピュータツールの raw イベントでは、保存された実行結果と同様に、プレビュー版と GA 版の区別が維持されます。プレビュー版のフローでは、1 つの `action` を持つ `computer_call` 項目がストリーミングされます。一方、`gpt-5.5` では、バッチ化された `actions[]` を持つ `computer_call` 項目をストリーミングできます。上位レベルの [`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] インターフェースでは、このためにコンピュータ専用の特別なイベント名は追加されません。どちらの形式も引き続き `tool_called` として公開され、スクリーンショットの実行結果は `computer_call_output` 項目をラップする `tool_output` として返されます。

たとえば、以下では LLM が生成したテキストをトークン単位で出力します。

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

## ストリーミングと承認 {#streaming-and-approvals}

ストリーミングは、ツールの承認待ちで一時停止する実行にも対応しています。ツールに承認が必要な場合、`result.stream_events()` は終了し、保留中の承認が [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] に公開されます。`result.to_state()` を使用して実行結果を [`RunState`][agents.run_state.RunState] に変換し、中断を承認または拒否してから、`Runner.run_streamed(...)` で再開します。

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

一時停止と再開の手順全体については、[human-in-the-loop ガイド](human_in_the_loop.md)を参照してください。

## 現在のターン終了後のストリーミング停止 {#cancel-streaming-after-the-current-turn}

ストリーミング実行を途中で停止する必要がある場合は、[`result.cancel()`][agents.result.RunResultStreaming.cancel] を呼び出します。デフォルトでは、実行は直ちに停止します。停止する前に現在のターンを正常に完了させるには、代わりに `result.cancel(mode="after_turn")` を呼び出します。

ストリーミング実行は、`result.stream_events()` が完了するまで完了しません。最後に表示されるトークンの後も、SDK がセッション項目を永続化したり、承認状態を確定したり、履歴を圧縮したりしている場合があります。

[`result.to_input_list(mode="normalized")`][agents.result.RunResultBase.to_input_list] から手動で続行していて、`cancel(mode="after_turn")` がツールターンの後に停止した場合は、新しいユーザーターンをすぐに追加するのではなく、その正規化済み入力を使用して `result.last_agent` を再実行し、未完了の既存ユーザーターンを続行します。

-   未完了の実行を再開する前に新しいユーザー入力が到着した場合は、最後まで取得した実行結果を `result.to_state()` で変換し、[`state.add_input(...)`][agents.run_state.RunState.add_input] を呼び出して、その状態から再開します。Runner は、次のモデル呼び出しの直前に準備済みの入力を取り込みます。[再開前の入力追加](results.md#add-input-before-resuming)を参照してください。
-   ストリーミング実行がツールの承認待ちで停止した場合、それを新しいターンとして扱わないでください。ストリームを最後まで取得し、`result.interruptions` を確認して、代わりに `result.to_state()` から再開します。
-   [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] を使用すると、取得したセッション履歴と新しいユーザー入力を次のモデル呼び出しの前にどのようにマージするかをカスタマイズできます。そこで新しいターンの項目を書き換えた場合、そのターンでは書き換え後の内容が永続化されます。

## 実行項目イベントとエージェントイベント {#run-item-events-and-agent-events}

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より上位レベルのイベントです。項目の生成が完全に完了したときに通知されます。これにより、トークンごとではなく、「メッセージが生成された」「ツールが実行された」などの単位で進捗状況を通知できます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変更されたとき（ハンドオフの結果など）に更新を提供します。

### 実行項目イベント名 {#run-item-event-names}

`RunItemStreamEvent.name` では、次の固定されたセマンティックイベント名のセットを使用します。

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

`handoff_occured` は、後方互換性のため意図的に誤った綴りになっています。

ハンドオフ呼び出しは `handoff_requested` としてのみ生成され、`tool_called` として重複して生成されることはありません。同じターン内の通常の関数ツール呼び出しでは、引き続き `tool_called` が生成されます。

ホスト型ツール検索を使用すると、モデルがツール検索リクエストを発行したときに `tool_search_called` が生成され、Responses API がロード済みのサブセットを返したときに `tool_search_output_created` が生成されます。

Programmatic Tool Calling を使用する場合、生成された `program` と、プログラム側が所有する通常の子ツール呼び出しに対して `tool_called` が生成されます。子ツールの出力と、生成された `program` に対応する `program_output` に対しては、`tool_output` が生成されます。プログラム側が所有するホスト型 MCP の `mcp_approval_request` 項目と `mcp_list_tools` 項目は例外です。これらはそれぞれ、[`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem] と [`MCPListToolsItem`][agents.items.MCPListToolsItem] をラップする `mcp_approval_requested` と `mcp_list_tools` として生成されます。その他の項目を区別するには、raw 項目の `type` を確認してください。プログラム側が所有する子呼び出しには、型が `program` で、呼び出し元 ID により親プログラムを識別できる `caller` も含まれます。

たとえば、以下では raw イベントを無視し、更新をユーザーにストリーミングします。

```python
import asyncio
import random
from agents import Agent, ItemHelpers, Runner
from agents.decorators import tool


@tool
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