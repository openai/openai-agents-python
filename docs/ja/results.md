---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかを取得します。

-   `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここにあります。

## どの result プロパティを使うべきか

ほとんどのアプリケーションで必要になるのは、いくつかの result プロパティまたはヘルパーのみです。

| Property or helper | これが必要な場合に使用 |
| --- | --- |
| `final_output` | ユーザーに表示する最終回答。 |
| `to_input_list()` | 会話履歴を自前で保持している場合に、次ターン用の完全な入力リスト。 |
| `new_items` | ログ、 UI 、監査向けに、エージェント、ツール、ハンドオフのメタデータを含むリッチな実行アイテム。 |
| `last_agent` | 通常、次ターンを処理すべきエージェント。 |
| `last_response_id` | 次の OpenAI Responses ターンで `previous_response_id` として継続する場合。 |
| `interruptions` | 再開前に解決が必要な、保留中のツール承認。 |
| `to_state()` | 一時停止 / 再開や耐久性のあるジョブワークフロー向けの、シリアライズ可能なスナップショット。 |

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力型が定義されている場合は、`last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフがあるため、これを静的に型付けできません。ハンドオフが発生すると、どの Agent も最後のエージェントになり得るため、可能な出力型の集合を静的に把握できないためです。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、結果を入力リストに変換できます。この入力リストは、あなたが提供した元の入力と、エージェント実行中に生成されたアイテムを連結したものです。これにより、1 回のエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりすることが簡単になります。

実運用では次のとおりです。

-   アプリケーションが会話全体の記録を手動で保持する場合は `result.to_input_list()` を使います。
-   SDK に履歴の読み書きを任せたい場合は [`session=...`](sessions/index.md) を使います。
-   `conversation_id` または `previous_response_id` を使った OpenAI サーバー管理状態を使用している場合は、通常 `result.to_input_list()` を再送する代わりに、新しいユーザー入力のみを渡して保存済み ID を再利用します。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、これは次回ユーザーが入力した際に役立つことがよくあります。たとえば、一次対応のトリアージエージェントが言語別エージェントにハンドオフする場合、最後のエージェントを保存し、次回ユーザーがエージェントにメッセージしたときに再利用できます。

ストリーミングモードでは、[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] は実行の進行に応じて更新されるため、ハンドオフが発生する様子をそのまま観察できます。

## 新しいアイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。これらのアイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、 LLM が生成した raw アイテムをラップしたものです。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw アイテムはハンドオフツール呼び出しへのツール応答です。アイテムからソース / ターゲットエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツール応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## 実行状態

実行のシリアライズ可能なスナップショットが必要な場合は [`result.to_state()`][agents.result.RunResult.to_state] を呼び出します。これは、完了または一時停止した実行と、後での再開をつなぐ橋渡しであり、特に承認フローや耐久性のあるワーカーシステムで有用です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合、ガードレールの結果が含まれます。ガードレール結果には、ログ記録や保存に有用な情報が含まれることがあるため、これらを利用できるようにしています。

ツールガードレール結果は、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] および [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として別途利用できます。これらのガードレールはツールにアタッチでき、それらのツール呼び出しはエージェントワークフロー中にガードレールを実行します。

### Raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、 LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドにあなたが提供した元の入力が含まれます。ほとんどの場合は不要ですが、必要な場合に利用できます。

### 中断と実行再開

実行がツール承認のために一時停止した場合、保留中の承認は
[`RunResult.interruptions`][agents.result.RunResult.interruptions] または
[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。結果を
`to_state()` で [`RunState`][agents.run_state.RunState] に変換し、中断を承認または拒否してから、
`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="Use tools when needed.")
result = await Runner.run(agent, "Delete temp files that are no longer needed.")

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state)
```

[`RunResult`][agents.result.RunResult] と
[`RunResultStreaming`][agents.result.RunResultStreaming] はどちらも `to_state()` をサポートしています。耐久性のある
承認ワークフローについては、[human-in-the-loop ガイド](human_in_the_loop.md)を参照してください。

### 便利なヘルパー

`RunResultBase` には、本番フローで有用ないくつかのヘルパーメソッド / プロパティが含まれます。

- [`final_output_as(...)`][agents.result.RunResultBase.final_output_as] は最終出力を特定の型にキャストします（必要に応じてランタイム型チェック付き）。
- [`last_response_id`][agents.result.RunResultBase.last_response_id] は最新のモデル応答 ID を返します。次ターンで OpenAI Responses API チェーンを継続したい場合は、これを `previous_response_id` として渡します。
- [`release_agents(...)`][agents.result.RunResultBase.release_agents] は、結果を確認した後にメモリ圧力を減らしたい場合、エージェントへの強参照を破棄します。