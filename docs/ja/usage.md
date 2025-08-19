---
search:
  exclude: true
---
# 使用状況

Agents SDK は、すべての run のトークン使用状況を自動で追跡します。run のコンテキストから参照でき、コストの監視、上限の適用、分析の記録に利用できます。

## 追跡項目

- **requests**: 行われた LLM API 呼び出し回数
- **input_tokens**: 送信した合計入力トークン数
- **output_tokens**: 受信した合計出力トークン数
- **total_tokens**: input + output
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

使用状況は、実行中のすべてのモデル呼び出し（ツール呼び出しや ハンドオフ を含む）にわたり集計されます。

## セッションでの使用状況へのアクセス

`Session`（例: `SQLiteSession`）を使用する場合、同じ run 内の複数ターンにわたり使用状況が蓄積されます。`Runner.run(...)` の各呼び出しは、その時点での run の累積使用状況を返します。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # includes both turns
```

## フックでの使用状況の活用

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、重要なライフサイクル時点で使用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```