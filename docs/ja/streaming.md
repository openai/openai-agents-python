---
search:
  exclude: true
---
# ストリーミング

ストリーミングを使うと、エージェント実行の進行中に更新を購読できます。これは、エンドユーザーに進捗更新や部分的な応答を表示するのに便利です。

ストリーミングするには、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を呼び出します。これにより [`RunResultStreaming`][agents.result.RunResultStreaming] が得られます。`result.stream_events()` を呼び出すと、以下で説明する [`StreamEvent`][agents.stream_events.StreamEvent] オブジェクトの非同期ストリームを取得できます。

非同期イテレーターが終了するまで、`result.stream_events()` の消費を続けてください。ストリーミング実行は、イテレーターが終了するまで完了しません。また、セッション永続化、承認管理、履歴圧縮などの後処理は、最後に表示されるトークンが到着した後に完了する場合があります。ループ終了時に、`result.is_complete` が最終的な実行状態を反映します。

## raw 応答イベント

[`RawResponsesStreamEvent`][agents.stream_events.RawResponsesStreamEvent] は、LLM から直接渡される raw イベントです。これらは OpenAI Responses API 形式であり、各イベントは type（`response.created`、`response.output_text.delta` など）と data を持ちます。これらのイベントは、生成され次第すぐに応答メッセージをユーザーへストリーミングしたい場合に有用です。

たとえば、これは LLM が生成したテキストをトークン単位で出力します。

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

ストリーミングは、ツール承認のために一時停止する実行と互換性があります。ツールに承認が必要な場合、`result.stream_events()` は終了し、保留中の承認は [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。`result.to_state()` で結果を [`RunState`][agents.run_state.RunState] に変換し、割り込みを承認または拒否してから、`Runner.run_streamed(...)` で再開してください。

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

一時停止/再開の完全な手順は、[human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

## 実行アイテムイベントとエージェントイベント

[`RunItemStreamEvent`][agents.stream_events.RunItemStreamEvent] は、より高レベルのイベントです。アイテムが完全に生成されたタイミングを通知します。これにより、各トークン単位ではなく、「メッセージ生成」「ツール実行」などのレベルで進捗更新をプッシュできます。同様に、[`AgentUpdatedStreamEvent`][agents.stream_events.AgentUpdatedStreamEvent] は、現在のエージェントが変更されたとき（例: ハンドオフの結果）に更新を提供します。

### 実行アイテムイベント名

`RunItemStreamEvent.name` は、固定された意味論的イベント名のセットを使用します。

-   `message_output_created`
-   `handoff_requested`
-   `handoff_occured`
-   `tool_called`
-   `tool_output`
-   `reasoning_item_created`
-   `mcp_approval_requested`
-   `mcp_approval_response`
-   `mcp_list_tools`

`handoff_occured` は、後方互換性のために意図的にスペルミスのままになっています。

たとえば、これは raw イベントを無視して、更新をユーザーにストリーミングします。

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