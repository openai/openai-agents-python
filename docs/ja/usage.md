---
search:
  exclude: true
---
# 使用量

Agents SDK は、各実行のトークン使用量を自動的に追跡します。実行コンテキストからアクセスでき、コストの監視、上限の適用、分析情報の記録に利用できます。

## 追跡対象

- **requests**: 実行された LLM API 呼び出しの数
- **input_tokens**: 送信された入力トークンの合計
- **output_tokens**: 受信された出力トークンの合計
- **total_tokens**: 入力 + 出力
- **request_usage_entries**: リクエストごとの使用量内訳のリスト
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用量へのアクセス

`Runner.run(...)` の後に、`result.context_wrapper.usage` 経由で使用量にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量は、実行中のすべてのモデル呼び出し（ツール呼び出しやハンドオフを含む）にわたって集計されます。

### サードパーティアダプターでの使用量の有効化

使用量レポートは、サードパーティアダプターやプロバイダーのバックエンドによって異なります。アダプター経由のモデルに依存しており、正確な `result.context_wrapper.usage` 値が必要な場合は、次の点に注意してください。

- `AnyLLMModel` では、上流プロバイダーが使用量を返す場合、使用量は自動的に伝播されます。ストリーミングされた Chat Completions バックエンドでは、使用量チャンクが出力される前に `ModelSettings(include_usage=True)` が必要になる場合があります。
- `LitellmModel` では、一部のプロバイダーのバックエンドがデフォルトで使用量をレポートしないため、`ModelSettings(include_usage=True)` が必要になることがよくあります。

Models ガイドの [サードパーティアダプター](models/index.md#third-party-adapters) セクションにあるアダプター固有の注記を確認し、デプロイ予定の正確なプロバイダーバックエンドを検証してください。

## リクエストごとの使用量追跡

SDK は、各 API リクエストの使用量を `request_usage_entries` で自動的に追跡します。これは詳細なコスト計算やコンテキストウィンドウ消費量の監視に役立ちます。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## セッションでの使用量へのアクセス

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` の各呼び出しは、その特定の実行の使用量を返します。セッションはコンテキスト用に会話履歴を保持しますが、各実行の使用量は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは実行間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される使用量メトリクスは、その特定の実行のみを表すことに注意してください。セッションでは、以前のメッセージが各実行の入力として再投入される場合があり、その結果、以降のターンで入力トークン数に影響します。

## フックでの使用量の利用

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、主要なライフサイクルのタイミングで使用量をログに記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス

詳細な API ドキュメントについては、以下を参照してください。

-   [`Usage`][agents.usage.Usage] - 使用量追跡データ構造
-   [`RequestUsage`][agents.usage.RequestUsage] - リクエストごとの使用量詳細
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストから使用量にアクセス
-   [`RunHooks`][agents.run.RunHooks] - 使用量追跡ライフサイクルへのフックイン