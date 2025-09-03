---
search:
  exclude: true
---
# 使用状況

Agents SDK は各実行のトークン使用状況を自動的に追跡します。実行コンテキストからアクセスでき、コストの監視、上限の適用、分析の記録に使えます。

## 追跡対象

- **requests**: 実行された LLM API 呼び出しの数
- **input_tokens**: 送信した入力トークンの合計
- **output_tokens**: 受信した出力トークンの合計
- **total_tokens**: 入力 + 出力
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用状況へのアクセス

`Runner.run(...)` の後、`result.context_wrapper.usage` から使用状況にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用状況は、実行中のすべてのモデル呼び出し（ツール呼び出しやハンドオフを含む）にわたって集計されます。

## セッションでの使用状況へのアクセス

`Session`（例: `SQLiteSession`）を使う場合、`Runner.run(...)` への各呼び出しは、その実行専用の使用状況を返します。セッションは文脈用に会話履歴を保持しますが、各実行の使用状況は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは実行間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しで返される使用状況メトリクスは、その実行のみを表します。セッションでは、前のメッセージが各実行の入力として再投入される場合があり、その結果、後続ターンの入力トークン数に影響します。

## フックでの使用状況

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、ライフサイクルの重要なタイミングで使用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス

詳細な API ドキュメントは次をご覧ください:

-   [`Usage`][agents.usage.Usage] - 使用状況追跡のデータ構造
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストから使用状況にアクセス
-   [`RunHooks`][agents.run.RunHooks] - 使用状況追跡ライフサイクルへのフック