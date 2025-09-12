---
search:
  exclude: true
---
# 使用量

Agents SDK は各実行ごとにトークン使用量を自動で追跡します。実行コンテキストから参照でき、コストの監視、制限の適用、アナリティクスの記録に利用できます。

## 追跡対象

- **requests**: 実行した LLM API コール数
- **input_tokens**: 送信した入力トークン合計
- **output_tokens**: 受信した出力トークン合計
- **total_tokens**: 入力 + 出力
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用量の取得

`Runner.run(...)` 実行後、`result.context_wrapper.usage` から使用量にアクセスできます。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量は実行中のすべてのモデル呼び出し（ツール呼び出しやハンドオフを含む）にわたって集計されます。

## セッションでの使用量の取得

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` への各呼び出しは、その特定の実行の使用量を返します。セッションはコンテキスト用に会話履歴を保持しますが、各実行の使用量は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは実行間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される使用量メトリクスは、その実行の結果のみを表します。セッションでは、以前のメッセージが各実行の入力として再投入される場合があり、その後のターンの入力トークン数に影響します。

## フックでの使用量の活用

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、重要なライフサイクル時点で使用量を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス

詳細な API ドキュメントは次を参照してください:

- [`Usage`][agents.usage.Usage] - 使用量追跡のデータ構造
- [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストから使用量へアクセス
- [`RunHooks`][agents.run.RunHooks] - 使用量追跡ライフサイクルへのフック