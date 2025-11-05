---
search:
  exclude: true
---
# 使用量

Agents SDK は各実行ごとにトークン使用量を自動追跡します。実行コンテキストから参照でき、コストの監視、制限の適用、分析の記録に利用できます。

## 追跡対象

- **requests**: 実行された LLM API 呼び出し数
- **input_tokens**: 送信された入力トークン合計
- **output_tokens**: 受信した出力トークン合計
- **total_tokens**: input + output
- **request_usage_entries**: リクエストごとの使用量内訳の一覧
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用量アクセス

`Runner.run(...)` の後、`result.context_wrapper.usage` から使用量にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量は実行中のすべてのモデル呼び出し（ツール呼び出しや ハンドオフ を含む）で集計されます。

### LiteLLM モデルでの使用量の有効化

LiteLLM プロバイダーはデフォルトで使用量メトリクスを報告しません。[`LitellmModel`](models/litellm.md) を使用する場合、エージェントに `ModelSettings(include_usage=True)` を渡すと、LiteLLM のレスポンスが `result.context_wrapper.usage` に反映されます。

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

## リクエスト単位の使用量トラッキング

SDK は `request_usage_entries` に各 API リクエストの使用量を自動追跡します。詳細なコスト計算やコンテキストウィンドウ消費の監視に有用です。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## セッションでの使用量アクセス

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` の各呼び出しはその実行に固有の使用量を返します。セッションは文脈として会話履歴を保持しますが、各実行の使用量は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは実行間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される使用量メトリクスはその実行のみを表します。セッションでは、過去のメッセージが各実行の入力として再投入されることがあり、その結果、後続ターンの入力トークン数に影響します。

## フックでの使用量の利用

`RunHooks` を使用する場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、重要なライフサイクルのタイミングで使用量を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス

詳細な API ドキュメントは以下をご覧ください。

- [`Usage`][agents.usage.Usage] - 使用量トラッキングのデータ構造
- [`RequestUsage`][agents.usage.RequestUsage] - リクエストごとの使用量詳細
- [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストからの使用量アクセス
- [`RunHooks`][agents.run.RunHooks] - 使用量トラッキングのライフサイクルにフックする