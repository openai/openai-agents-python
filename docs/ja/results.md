---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかを取得します。

-   `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、有用な情報の大半はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントで `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は、型 `last_agent.output_type` のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフがあるため、これを静的には型付けできません。ハンドオフが発生すると、どの Agent でも最後のエージェントになり得るため、可能な出力タイプの集合を静的に把握できないからです。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、結果を入力リストに変換できます。この入力リストは、あなたが提供した元の入力と、エージェント実行中に生成された項目を連結したものです。これにより、1 回のエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりすることが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、これは次回ユーザーが入力したときに役立つことがよくあります。たとえば、言語別エージェントにハンドオフするフロントラインのトリアージエージェントがある場合、最後のエージェントを保存して、次回ユーザーがエージェントにメッセージを送るときに再利用できます。

## 新しい項目

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しい項目が含まれます。項目は [`RunItem`][agents.items.RunItem] です。実行項目は、LLM が生成した raw 項目をラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw 項目は生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフツールを呼び出したことを示します。raw 項目は LLM からのツール呼び出し項目です。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフが発生したことを示します。raw 項目はハンドオフツール呼び出しに対するツール応答です。項目から送信元 / 送信先エージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw 項目はツール応答です。項目からツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論項目を示します。raw 項目は生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合、ガードレールの結果が含まれます。ガードレール結果には、ログに記録したり保存したりしたい有用な情報が含まれることがあるため、これらを利用できるようにしています。

ツールガードレールの結果は、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として別途利用できます。これらのガードレールはツールにアタッチでき、エージェントワークフロー中のツール呼び出しで実行されます。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合これは不要ですが、必要な場合に備えて利用できます。

### 中断と実行の再開

実行がツール承認のために一時停止した場合、保留中の承認は
[`RunResult.interruptions`][agents.result.RunResult.interruptions] または
[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。結果を
`to_state()` で [`RunState`][agents.run_state.RunState] に変換し、中断を承認または却下して、
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
[`RunResultStreaming`][agents.result.RunResultStreaming] はどちらも `to_state()` をサポートしています。永続的な
承認ワークフローについては、[human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

### 便利なヘルパー

`RunResultBase` には、本番フローで役立ついくつかのヘルパーメソッド / プロパティが含まれています。

- [`final_output_as(...)`][agents.result.RunResultBase.final_output_as] は、最終出力を特定の型にキャストします（任意でランタイム型チェック付き）。
- [`last_response_id`][agents.result.RunResultBase.last_response_id] は最新のモデルレスポンス ID を返し、レスポンスの連鎖に役立ちます。
- [`release_agents(...)`][agents.result.RunResultBase.release_agents] は、結果を確認した後にメモリ負荷を下げたい場合、エージェントへの強参照を解放します。