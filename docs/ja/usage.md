---
search:
  exclude: true
---
# 使用状況

Agents SDK は各 run のトークン使用状況を自動で追跡します。run のコンテキストから参照でき、コストの監視、上限の適用、分析の記録に利用できます。

## 追跡対象

- **requests**: 実行された LLM API 呼び出しの回数
- **input_tokens**: 送信された入力トークンの合計
- **output_tokens**: 受信した出力トークンの合計
- **total_tokens**: input + output
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用状況の取得

`Runner.run(...)` の後、`result.context_wrapper.usage` から使用状況にアクセスできます。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用状況は run 中のすべてのモデル呼び出し（ツール呼び出しやハンドオフを含む）を集計します。

### LiteLLM モデルでの使用状況の有効化

LiteLLM プロバイダーはデフォルトでは使用状況メトリクスを報告しません。[`LitellmModel`](models/litellm.md) を使用する場合、エージェントに `ModelSettings(include_usage=True)` を渡すと、LiteLLM のレスポンスが `result.context_wrapper.usage` に反映されます。

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

## セッションでの使用状況の取得

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` の各呼び出しはその特定の run の使用状況を返します。セッションはコンテキスト用に会話履歴を保持しますが、各 run の使用状況は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは run 間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される使用状況メトリクスは、その実行のみを表します。セッションでは、前のメッセージが各 run の入力として再投入されることがあり、その結果、後続ターンの入力トークン数に影響します。

## フックでの使用状況の利用

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、重要なライフサイクルのタイミングで使用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス

詳細な API ドキュメントは以下を参照してください。

- [`Usage`][agents.usage.Usage] - 使用状況の追跡データ構造
- [`RunContextWrapper`][agents.run.RunContextWrapper] - run コンテキストから使用状況へアクセス
- [`RunHooks`][agents.run.RunHooks] - 使用状況トラッキングのライフサイクルにフックする