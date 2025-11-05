---
search:
  exclude: true
---
# 使用状況

Agents SDK は各実行ごとにトークン使用状況を自動的に追跡します。実行コンテキストから参照でき、コストの監視、制限の適用、分析の記録に利用できます。

## 追跡対象

- **requests**: LLM API 呼び出し数
- **input_tokens**: 送信された入力トークン合計
- **output_tokens**: 受信した出力トークン合計
- **total_tokens**: 入力 + 出力
- **request_usage_entries**: リクエストごとの使用状況の内訳リスト
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用状況へのアクセス

`Runner.run(...)` の後、 `result.context_wrapper.usage` から使用状況にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用状況は、実行中のすべてのモデル呼び出し（ツール呼び出しやハンドオフを含む）にわたって集計されます。

### LiteLLM モデルでの使用状況の有効化

LiteLLM プロバイダーはデフォルトで使用メトリクスを報告しません。[`LitellmModel`](models/litellm.md) を使用する場合は、エージェントに `ModelSettings(include_usage=True)` を渡して、LiteLLM のレスポンスが `result.context_wrapper.usage` に反映されるようにします。

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

SDK は各 API リクエストの使用状況を `request_usage_entries` に自動追跡します。詳細なコスト計算やコンテキストウィンドウ消費の監視に便利です。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## セッションでの使用状況へのアクセス

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` の各呼び出しはその実行に固有の使用状況を返します。セッションはコンテキスト用に会話履歴を保持しますが、各実行の使用状況は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは実行間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される使用メトリクスはその実行結果にのみ対応します。セッションでは、前のメッセージが各実行に入力として再投入される場合があり、その結果、後続のターンで入力トークン数に影響します。

## フックでの使用状況の利用

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、重要なライフサイクル時点で使用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス

詳細な API ドキュメントは以下をご参照ください。

-   [`Usage`][agents.usage.Usage] - 使用状況トラッキングのデータ構造
-   [`RequestUsage`][agents.usage.RequestUsage] - リクエストごとの使用状況の詳細
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストから使用状況へアクセス
-   [`RunHooks`][agents.run.RunHooks] - 使用状況トラッキングのライフサイクルにフック