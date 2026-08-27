---
search:
  exclude: true
---
# 使用状況

Agents SDK は、実行ごとのトークン使用状況を自動的に追跡します。実行コンテキストからアクセスし、コストの監視、制限の適用、分析データの記録に使用できます。

## 追跡対象 {#what-is-tracked}

- **requests**: 実行された LLM API 呼び出しの数
- **input_tokens**: 送信された入力トークンの合計
- **output_tokens**: 受信した出力トークンの合計
- **total_tokens**: 入力 + 出力
- **request_usage_entries**: リクエストごとの使用状況の内訳のリスト
- **details**:
  - `input_tokens_details.cached_tokens`
  - `input_tokens_details.cache_write_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用状況へのアクセス {#accessing-usage-from-a-run}

`Runner.run(...)` の実行後、`result.context_wrapper.usage` から使用状況にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用状況は、ツール呼び出しやハンドオフを生成するモデル呼び出しを含め、実行中のすべてのモデル呼び出しについて集計されます。

[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] が実行の終了前に履歴を自動的にコンパクト化した場合、その `responses.compact` リクエストによって報告された使用状況も、同じ実行の合計に加算されます。実行の外部で手動による `run_compaction()` 呼び出しを行った場合、包含する実行コンテキストがないため、以前の実行から返された使用状況オブジェクトは更新されません。[OpenAI Responses のコンパクションセッション](sessions/index.md#openai-responses-compaction-sessions)を参照してください。

### サードパーティーアダプターでの使用状況の有効化 {#enabling-usage-with-third-party-adapters}

使用状況の報告は、サードパーティーアダプターやプロバイダーバックエンドによって異なります。サードパーティーアダプター経由でモデルにアクセスし、正確な `result.context_wrapper.usage` 値が必要な場合は、以下を確認してください。

- `AnyLLMModel` では、上流プロバイダーが使用状況を返すと、自動的に伝播されます。Chat Completions バックエンドからレスポンスをストリーミングする場合、使用状況チャンクを出力するために `ModelSettings(include_usage=True)` が必要になることがあります。
- `LitellmModel` では、一部のプロバイダーバックエンドがデフォルトで使用状況を報告しないため、多くの場合 `ModelSettings(include_usage=True)` が必要です。

モデルガイドの[サードパーティーアダプター](models/index.md#third-party-adapters)セクションにあるアダプター固有の注意事項を確認し、デプロイ予定のプロバイダーバックエンドで使用状況が正しく報告されることを検証してください。

## リクエストごとの使用状況の追跡 {#per-request-usage-tracking}

SDK は、`request_usage_entries` 内の各 API リクエストの使用状況を自動的に追跡します。これは、詳細なコスト計算やコンテキストウィンドウの消費量の監視に役立ちます。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## プロバイダーの使用状況ペイロードの保持 {#preserving-provider-usage-payloads}

Agents SDK は、プロバイダーの使用状況を、モデルプロバイダー間で一貫した合計値を提供する [`Usage`][agents.usage.Usage] フィールドに正規化します。アプリケーションでプロバイダー固有の使用状況フィールドを保持する必要がある場合、または省略されたフィールドとプロバイダーが報告したゼロを区別する必要がある場合は、[`ModelSettings.preserve_raw_usage`][agents.model_settings.ModelSettings.preserve_raw_usage] を `True` に設定します。

```python
from agents import Agent, ModelSettings, Runner

agent = Agent(
    name="Assistant",
    model_settings=ModelSettings(preserve_raw_usage=True),
)
result = await Runner.run(agent, "What's the weather in Tokyo?")

for response in result.raw_responses:
    print(response.raw_usage)
```

Agents SDK は、各モデル呼び出しのプロバイダーペイロードについて、各 [`ModelResponse.raw_usage`][agents.items.ModelResponse.raw_usage] 値を分離された JSON 互換スナップショットとして保存します。Agents SDK は、実行全体で `raw_usage` を集計しません。保持が無効になっている場合、プロバイダーが使用状況ペイロードを返さない場合、または上流アダプターが元のフィールドの存在有無に関する情報をすでに破棄している場合、この値は `None` のままです。

`preserve_raw_usage` は、モデルアダプターに到達した使用状況ペイロードのみを保持します。この設定によって、プロバイダーに使用状況を要求することはありません。ストリーミング対応の Chat Completions プロバイダーで明示的な使用状況リクエストが必要な場合は、`ModelSettings(include_usage=True)` も設定してください。

`LitellmModel` は現在、ストリーミング実行と非ストリーミング実行のいずれでも `ModelResponse.raw_usage` を設定しないため、そのアダプターでは `preserve_raw_usage=True` は効果がありません。`LitellmModel` を使用する場合は、正規化された [`Usage`][agents.usage.Usage] フィールドを引き続き使用してください。プロバイダー固有のフィールドの存在有無を保持する必要がある場合は、raw 使用状況の保持をサポートするアダプターを選択してください。

## セッションでの使用状況へのアクセス {#accessing-usage-with-sessions}

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` を呼び出すたびに、その特定の実行の使用状況が返されます。セッションはコンテキストのために会話履歴を保持しますが、各実行の使用状況は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションは実行間で会話コンテキストを保持しますが、各 `Runner.run()` 呼び出しによって返される使用状況の指標は、その特定の実行のみを表します。セッションでは、以前のメッセージが各実行への入力として再度渡される場合があり、後続のターンにおける入力トークン数に影響します。

## RunState チェックポイントでの使用状況 {#usage-in-runstate-checkpoints}

[`RunResult.to_state()`][agents.result.RunResult.to_state] は、それまでに蓄積された使用状況の独立したスナップショットを取得します。そのチェックポイントから再開された実行は、取得済みの合計値から開始し、独自のモデル呼び出しによる使用状況を加算します。再開された実行では、これらの新しい合計値は元の `RunResult` にも、その実行結果から作成された別のチェックポイントにも加算されません。

```python
first = await Runner.run(agent, "First request")
checkpoint_a = first.to_state()
checkpoint_b = first.to_state()

resumed_a = await Runner.run(agent, checkpoint_a)
resumed_b = await Runner.run(agent, checkpoint_b)

assert resumed_a.context_wrapper.usage is not first.context_wrapper.usage
assert resumed_b.context_wrapper.usage is not resumed_a.context_wrapper.usage
```

この分離は、[`Usage`][agents.usage.Usage] 内の `request_usage_entries` リストにも適用されます。ただし、再開されたネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行は、独立したトップレベルの集計の例外です。再開後のモデル使用状況は、ネストされた実行の以前のモデル呼び出しと同様に、アクティブな外側の実行の使用状況へ意図的に集計されます。

## フックでの使用状況 {#using-usage-in-hooks}

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれます。これにより、ライフサイクルの重要な時点で使用状況を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス {#api-reference}

API の詳細なドキュメントについては、以下を参照してください。

-   [`Usage`][agents.usage.Usage] - 使用状況追跡のデータ構造
-   [`RequestUsage`][agents.usage.RequestUsage] - リクエストごとの使用状況の詳細
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストからの使用状況へのアクセス
-   [`RunHooks`][agents.run.RunHooks] - 使用状況追跡のライフサイクルへのフック