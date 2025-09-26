---
search:
  exclude: true
---
# 利用状況

Agents SDK は各ランのトークン利用状況を自動で追跡します。ランのコンテキストから参照でき、コストの監視、上限の適用、分析記録に活用できます。

## 追跡対象

- **requests**: 行われた LLM API 呼び出し回数
- **input_tokens**: 送信された入力トークンの合計
- **output_tokens**: 受信した出力トークンの合計
- **total_tokens**: 入力 + 出力
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## ランからの利用状況へのアクセス

`Runner.run(...)` の後、`result.context_wrapper.usage` から利用状況にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

利用状況は、ラン中のすべてのモデル呼び出し（ツール呼び出しやハンドオフを含む）にわたって集計されます。

### LiteLLM モデルでの usage 有効化

LiteLLM プロバイダーは既定では利用状況メトリクスを報告しません。[`LitellmModel`](models/litellm.md) を使用する場合、エージェントに `ModelSettings(include_usage=True)` を渡して、LiteLLM のレスポンスが `result.context_wrapper.usage` を埋めるようにします。

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

## セッションでの利用状況へのアクセス

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` への各呼び出しは、その特定のランの利用状況を返します。セッションはコンテキストのための会話履歴を保持しますが、各ランの利用状況は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションはラン間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される利用状況メトリクスは、その実行のみを表します。セッションでは前のメッセージが各ランに入力として再投入される場合があり、その結果、後続ターンの入力トークン数に影響します。

## フックでの利用状況の利用

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、ライフサイクルの重要なポイントで利用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス

詳細な API ドキュメントは次をご覧ください:

-   [`Usage`][agents.usage.Usage] - 利用状況の追跡データ構造
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - ランのコンテキストから利用状況へアクセス
-   [`RunHooks`][agents.run.RunHooks] - 利用状況トラッキングのライフサイクルにフックする