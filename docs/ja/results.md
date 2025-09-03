---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります。

- [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼んだ場合）
- [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼んだ場合）

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。これは次のいずれかです。

- 最後のエージェントに `output_type` が定義されていない場合は `str`
- エージェントに出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。これはハンドオフのために静的な型付けができません。ハンドオフが発生する場合、どのエージェントが最後になるか分からないため、可能な出力タイプの集合を静的には特定できません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、あなたが提供した元の入力と、エージェントの実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を付け足したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入ります。アプリケーションによっては、次回 ユーザー が入力する際に役立つことがよくあります。たとえば、一次トリアージのエージェントが言語別のエージェントにハンドオフする構成の場合、最後のエージェントを保存しておき、次回 ユーザー がエージェントにメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入ります。アイテムは [`RunItem`][agents.items.RunItem] です。ランアイテムは、LLM が生成した raw アイテムをラップします。

- [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフが発生したことを示します。raw アイテムはハンドオフ ツール呼び出しに対するツール応答です。アイテムからソース/ターゲットのエージェントにもアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。raw アイテムはツール応答です。アイテムからツール出力にもアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合にガードレールの実行結果が入ります。ガードレール結果には、ログや保存に有用な情報が含まれることがあるため、参照できるように提供しています。

### Raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が入ります。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が入ります。多くの場合は不要ですが、必要なときのために利用できるようになっています。