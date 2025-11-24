---
search:
  exclude: true
---
# 使用状況

Agents SDK は、各実行ごとにトークン使用状況を自動で追跡します。実行コンテキストから参照でき、コストの監視、制限の適用、分析の記録に利用できます。

## 追跡対象

- **requests**: 実行された LLM API 呼び出し回数
- **input_tokens**: 送信された入力トークン合計
- **output_tokens**: 受信した出力トークン合計
- **total_tokens**: 入力 + 出力
- **request_usage_entries**: リクエストごとの使用状況内訳の一覧
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用状況の参照

`Runner.run(...)` の後、`result.context_wrapper.usage` から使用状況にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用状況は、実行中のすべてのモデル呼び出し（ツール呼び出しや ハンドオフ を含む）にわたって集計されます。

### LiteLLM モデルでの使用状況の有効化

LiteLLM プロバイダーはデフォルトでは使用状況メトリクスを報告しません。[`LitellmModel`](models/litellm.md) を使用する場合は、エージェントに `ModelSettings(include_usage=True)` を渡して、LiteLLM のレスポンスが `result.context_wrapper.usage` に反映されるようにします。

```python
from agents import Agent, ModelSettings, Runner
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)

result = await Runner.run(agent, "What's the weather in Tokyo?")
print(result.context_wrapper.usage.total_tokens)
```

## リクエスト単位の使用状況トラッキング

SDK は、各 API リクエストの使用状況を `request_usage_entries` に自動で記録します。詳細なコスト計算やコンテキストウィンドウ消費の監視に役立ちます。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## セッションでの使用状況の参照

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` の各呼び出しは、その実行に特有の使用状況を返します。セッションはコンテキスト用に会話履歴を保持しますが、各実行の使用状況は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは実行間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される使用状況メトリクスはその実行に限られます。セッションでは、前のメッセージが各実行の入力として再投入される場合があり、その結果、後続ターンの入力トークン数に影響します。

## フックでの使用状況の利用

`RunHooks` を使用する場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、ライフサイクルの重要なタイミングで使用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## フックから会話履歴を編集する

`RunContextWrapper` には `message_history` も含まれており、フックから会話を直接読み書きできます。

- `get_messages()` は元の入力・モデル出力・保留中の挿入を含む完全な履歴を `ResponseInputItem` のリストとして返します。
- `add_message(agent=..., message=...)` は任意のメッセージ（文字列、辞書、または `ResponseInputItem` のリスト）をキューに追加します。追加されたメッセージは即座に LLM への入力に連結され、実行結果やストリームイベントでは `InjectedInputItem` として公開されます。
- `override_next_turn(messages)` は次の LLM 呼び出しに送信される履歴全体を置き換えます。ガードレールや外部レビュー後に履歴を書き換えたい場合に使用できます。

```python
class BroadcastHooks(RunHooks):
  def __init__(self, reviewer_name: str):
    self.reviewer_name = reviewer_name

  async def on_llm_start(
    self,
    context: RunContextWrapper,
    agent: Agent,
    _instructions: str | None,
    _input_items: list[TResponseInputItem],
  ) -> None:
    context.message_history.add_message(
      agent=agent,
      message={
        "role": "user",
        "content": f"{self.reviewer_name}: 先に付録のデータを引用してください。",
      },
    )
```

> **注意:** `conversation_id` または `previous_response_id` を指定して実行している場合、履歴はサーバー側のスレッドで管理されるため、そのランでは `message_history.override_next_turn()` を使用できません。

## API リファレンス

詳細な API ドキュメントは次を参照してください:

-   [`Usage`][agents.usage.Usage] - 使用状況トラッキングのデータ構造
-   [`RequestUsage`][agents.usage.RequestUsage] - リクエストごとの使用状況の詳細
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストから使用状況にアクセス
-   [`MessageHistory`][agents.run_context.MessageHistory] - フックから会話履歴を閲覧・編集
-   [`RunHooks`][agents.run.RunHooks] - 使用状況トラッキングのライフサイクルにフック