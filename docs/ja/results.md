---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります。

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、有用な情報の多くはそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は、`last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は Any です。ハンドオフの可能性があるため、静的な型付けはできません。ハンドオフが発生すると、どのエージェントが最後になるか分からないため、取りうる出力タイプの集合を静的に特定できません。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、あなたが提供した元の入力に、エージェントの実行中に生成された項目を連結した入力リストに変換できます。これにより、あるエージェント実行の出力を別の実行へ渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、次回 ユーザー が何か入力する際にこれが有用なことが多いです。例えば、フロントラインのトリアージ用エージェントが言語別のエージェントにハンドオフする構成の場合、最後のエージェントを保存しておき、次回 ユーザー がメッセージを送ったときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しい項目が含まれます。各項目は [`RunItem`][agents.items.RunItem] です。RunItem は LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM のツール呼び出し項目です。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフが発生したことを示します。raw アイテムはハンドオフ ツール呼び出しへのツール応答です。項目から送信元/宛先のエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。raw アイテムはツールの応答です。項目からツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論項目を示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレールの結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合、ガードレールの結果が含まれます。ガードレールの結果には、記録や保存をしたい有用な情報が含まれることがあるため、参照できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が含まれます。ほとんどの場合は不要ですが、必要な場合のために利用可能になっています。