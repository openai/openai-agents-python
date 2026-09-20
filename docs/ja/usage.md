---
search:
  exclude: true
---
# 使用量

Agents SDK は、実行ごとのトークン使用量を自動的に追跡します。実行コンテキストからアクセスし、コストの監視、制限の適用、分析データの記録に利用できます。

## 追跡対象 {#what-is-tracked}

- **requests**: 実行された LLM API 呼び出しの回数
- **input_tokens**: 送信された入力トークンの合計
- **output_tokens**: 受信した出力トークンの合計
- **total_tokens**: 入力と出力の合計
- **request_usage_entries**: リクエストごとの使用量内訳のリスト
- **details**:
  - `input_tokens_details.cached_tokens`
  - `input_tokens_details.cache_write_tokens`
  - `output_tokens_details.reasoning_tokens`

## 実行からの使用量へのアクセス {#accessing-usage-from-a-run}

`Runner.run(...)` の実行後、`result.context_wrapper.usage` を介して使用量にアクセスします。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量は、ツール呼び出しやハンドオフを生成するモデル呼び出しを含め、実行中のすべてのモデル呼び出しにわたって集計されます。

[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] が実行の終了前に履歴を自動的に圧縮した場合、その `responses.compact` リクエストによって報告された使用量も、同じ実行の合計に加算されます。実行外で手動の `run_compaction()` 呼び出しを行った場合、その呼び出しを包含する実行コンテキストがないため、以前の実行から返された使用量オブジェクトは更新されません。[OpenAI Responses の圧縮セッション](sessions/index.md#openai-responses-compaction-sessions)を参照してください。

### サードパーティーアダプターでの使用量の有効化 {#enabling-usage-with-third-party-adapters}

使用量の報告方法は、サードパーティーアダプターやプロバイダーのバックエンドによって異なります。サードパーティーアダプターを介してモデルにアクセスし、正確な `result.context_wrapper.usage` 値が必要な場合は、次の点に注意してください。

- `AnyLLMModel` では、上流プロバイダーが使用量を返すと、自動的に伝播されます。Chat Completions バックエンドからレスポンスをストリーミングする場合、使用量チャンクを生成するために `ModelSettings(include_usage=True)` が必要になることがあります。
- `LitellmModel` では、一部のプロバイダーのバックエンドがデフォルトで使用量を報告しないため、多くの場合 `ModelSettings(include_usage=True)` が必要です。

Models ガイドの[サードパーティーアダプター](models/index.md#third-party-adapters)セクションにあるアダプター固有の注意事項を確認し、デプロイ予定のプロバイダーのバックエンドで使用量が正しく報告されることを検証してください。

## リクエスト単位の使用量追跡 {#per-request-usage-tracking}

SDK は、各 API リクエストの使用量を `request_usage_entries` で自動的に追跡します。これは、詳細なコスト計算やコンテキストウィンドウの消費量の監視に役立ちます。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

SDK が 1 つの [`Usage`][agents.usage.Usage] オブジェクトを別のオブジェクトに集計する際、リクエスト単位のエントリと、ネストされた入力および出力トークンの詳細がコピーされます。その後に元の使用量オブジェクトを変更しても、集計先の `request_usage_entries` は変更されません。また、集計先を変更しても元のエントリは変更されません。

## プロバイダーの使用量ペイロードの保持 {#preserving-provider-usage-payloads}

Agents SDK は、プロバイダーの使用量を [`Usage`][agents.usage.Usage] のフィールドに正規化し、モデルプロバイダー間で一貫した合計値を提供します。アプリケーションでプロバイダー固有の使用量フィールドを保持する必要がある場合や、省略されたフィールドとプロバイダーが報告したゼロを区別する必要がある場合は、[`ModelSettings.preserve_raw_usage`][agents.model_settings.ModelSettings.preserve_raw_usage] を `True` に設定します。

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

Agents SDK は、各 [`ModelResponse.raw_usage`][agents.items.ModelResponse.raw_usage] 値を、そのモデル呼び出しに対するプロバイダーのペイロードから分離された JSON 互換のスナップショットとして保存します。Agents SDK は、実行全体にわたって `raw_usage` を集計しません。保持が無効な場合、プロバイダーが使用量ペイロードを返さない場合、または上流アダプターが元のフィールドの有無に関する情報をすでに破棄している場合、値は `None` のままです。

`preserve_raw_usage` が保持するのは、モデルアダプターに到達した使用量ペイロードのみです。この設定によって、プロバイダーに使用量が要求されるわけではありません。ストリーミングの Chat Completions プロバイダーで使用量の明示的な要求が必要な場合は、`ModelSettings(include_usage=True)` も設定してください。

`LitellmModel` は現在、ストリーミング実行と非ストリーミング実行のどちらでも `ModelResponse.raw_usage` を設定しないため、`preserve_raw_usage=True` はそのアダプターでは効果がありません。`LitellmModel` を使用する場合は、正規化された [`Usage`][agents.usage.Usage] フィールドを引き続き使用してください。プロバイダー固有のフィールドの有無を確認する必要がある場合は、raw 使用量の保持をサポートするアダプターを選択してください。

## セッションでの使用量へのアクセス {#accessing-usage-with-sessions}

`Session`（例: `SQLiteSession`）を使用する場合、`Runner.run(...)` を呼び出すたびに、その特定の実行の使用量が返されます。セッションではコンテキスト用の会話履歴が維持されますが、各実行の使用量は独立しています。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

セッションでは実行間で会話コンテキストが保持されますが、各 `Runner.run()` 呼び出しから返される使用量メトリクスは、その特定の実行のみを表します。セッションでは、以前のメッセージが各実行への入力として再度渡される場合があり、これが後続のターンにおける入力トークン数に影響します。

## RunState チェックポイントでの使用量 {#usage-in-runstate-checkpoints}

[`RunResult.to_state()`][agents.result.RunResult.to_state] は、その時点までに蓄積された使用量の独立したスナップショットを取得します。そのチェックポイントから再開された実行は、取得済みの合計値から開始し、独自のモデル呼び出しによる使用量を加算します。再開された実行によって新たに生じた合計値は、元の `RunResult` や、その実行結果から作成された別のチェックポイントには加算されません。

```python
first = await Runner.run(agent, "First request")
checkpoint_a = first.to_state()
checkpoint_b = first.to_state()

resumed_a = await Runner.run(agent, checkpoint_a)
resumed_b = await Runner.run(agent, checkpoint_b)

assert resumed_a.context_wrapper.usage is not first.context_wrapper.usage
assert resumed_b.context_wrapper.usage is not resumed_a.context_wrapper.usage
```

この分離は、[`Usage`][agents.usage.Usage] 内の `request_usage_entries` リストにも適用されます。ただし、再開されたネスト済みの [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行は、独立したトップレベルの集計に対する例外です。その再開後のモデル使用量は、ネストされた実行の以前のモデル呼び出しと同様に、意図的にアクティブな外側の実行の使用量へ集計されます。

## フックでの使用量の利用 {#using-usage-in-hooks}

`RunHooks` を使用している場合、各フックに渡される `context` オブジェクトには `usage` が含まれています。これにより、ライフサイクルの主要な時点で使用量を記録できます。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API リファレンス {#api-reference}

詳細な API ドキュメントについては、以下を参照してください。

-   [`Usage`][agents.usage.Usage] - 使用量追跡のデータ構造
-   [`RequestUsage`][agents.usage.RequestUsage] - リクエスト単位の使用量の詳細
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 実行コンテキストからの使用量へのアクセス
-   [`RunHooks`][agents.run.RunHooks] - 使用量追跡ライフサイクルへのフック