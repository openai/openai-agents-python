---
search:
  exclude: true
---
# 使用状況

Agents SDKは、各実行ごとにトークン使用状況を自動追跡します。実行コンテキストから参照でき、コストの監視、制限の適用、分析の記録に利用できます。

## 追跡項目

- **requests**: LLM API の呼び出し回数
- **input_tokens**: 送信した入力トークンの合計
- **output_tokens**: 受信した出力トークンの合計
- **total_tokens**: 入力 + 出力
- **詳細**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用状況の取得

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

## セッションでの使用状況の取得

`Session`（例: `SQLiteSession`）を使用する場合、同一実行内の複数ターンにわたって使用状況が蓄積されます。`Runner.run(...)` の各呼び出しは、その時点での実行の累積使用状況を返します。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # includes both turns
```

## フックでの使用状況の活用

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、重要なライフサイクルのタイミングで使用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```