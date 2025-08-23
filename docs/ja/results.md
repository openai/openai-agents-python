---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかを受け取ります:

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が含まれます。これは次のいずれかです:

-   最後の エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に出力型が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフ のため、これを静的に型付けすることはできません。ハンドオフ が発生すると、どの エージェント でも最後になる可能性があるため、可能な出力型の集合を静的には把握できません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、実行結果を、元の入力に実行中に生成されたアイテムを連結した input list に変換できます。これにより、1 回の エージェント 実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が含まれます。アプリケーションによっては、これは次回 ユーザー が何かを入力する際に有用です。例えば、一次対応のトリアージ エージェント が言語特化の エージェント にハンドオフ する場合、最後の エージェント を保存しておき、次に ユーザー が エージェント にメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新規アイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。Run item は、LLM によって生成された raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフ が発生したことを示します。raw アイテムはハンドオフ ツール呼び出しへのツール応答です。アイテムからソース/ターゲットの エージェント にもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。raw アイテムはツールの応答です。アイテムからツールの出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合はガードレールの実行結果が含まれます。ガードレールの実行結果には、ログ保存や保管に有用な情報が含まれることがあるため、これらを利用可能にしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が含まれます。ほとんどの場合これは不要ですが、必要な場合に備えて利用可能です。