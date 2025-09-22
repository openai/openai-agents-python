---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります。

- [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
- [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

いずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

- 最後のエージェントに `output_type` が定義されていない場合は `str`
- エージェントに出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` は型 `Any` です。これは ハンドオフ のため静的型付けはできません。ハンドオフ が発生する場合、どのエージェントが最後になるか分からないため、可能な出力タイプの集合を静的には特定できません。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、提供した元の入力に、エージェントの実行中に生成された項目を連結した入力リストに変換できます。これにより、あるエージェント実行の出力を別の実行へ渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、次回 ユーザー が何かを入力する際に有用です。たとえば、フロントラインのトリアージ エージェントが言語特化のエージェントへ ハンドオフ する場合、最後のエージェントを保存しておき、次に ユーザー がメッセージを送るときに再利用できます。

## 新しい項目

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しい項目が含まれます。項目は [`RunItem`][agents.items.RunItem] です。RunItem は、LLM が生成した raw な項目をラップします。

- [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。raw 項目は生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM が ハンドオフ ツールを呼び出したことを示します。raw 項目は LLM からのツール呼び出し項目です。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフ が発生したことを示します。raw 項目は ハンドオフ ツール呼び出しへのツール応答です。項目からソース／ターゲットのエージェントにもアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。raw 項目はツールの応答です。項目からツールの出力にもアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論項目を示します。raw 項目は生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレール の結果（存在する場合）が含まれます。ガードレール の結果には、ログや保存に役立つ情報が含まれることがあるため、利用できるようにしています。

### Raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。多くの場合これは不要ですが、必要なときのために利用できます。