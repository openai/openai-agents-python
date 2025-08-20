---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります:

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が含まれます。これは次のいずれかです:

-   最後の エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に出力型が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフ の可能性があるため、静的型付けはできません。ハンドオフ が発生すると、どの エージェント でも最後の エージェント になり得るため、可能な出力型の集合を静的には特定できません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、あなたが提供した元の入力に、エージェント 実行中に生成されたアイテムを連結した入力リストに実行結果を変換できます。これにより、ある エージェント 実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が含まれます。アプリケーションによっては、これは次回 ユーザー が何か入力する際に役に立つことがよくあります。例えば、一次対応のトリアージ エージェント から言語特化の エージェント にハンドオフ する場合、最後の エージェント を保存しておき、次回 ユーザー がその エージェント にメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。RunItem は、LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフ が発生したことを示します。raw アイテムはハンドオフ ツール呼び出しに対するツールのレスポンスです。アイテムからソース/ターゲットの エージェント にもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを起動したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw アイテムはツールのレスポンスです。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は、LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合はガードレールの実行結果が含まれます。ガードレールの実行結果には、ログや保存を行いたい有用な情報が含まれることがあるため、利用できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。多くの場合は不要ですが、必要に応じて参照できます。