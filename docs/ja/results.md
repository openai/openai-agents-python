---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります:

-   `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ここに最も有用な情報が含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです:

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は、`last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` は型 `Any` です。ハンドオフ があるため、静的型付けはできません。ハンドオフ が発生すると、どのエージェントでも最後のエージェントになり得るため、可能な出力タイプの集合を静的に知ることができません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、提供した元の入力とエージェント実行中に生成されたアイテムを連結した入力リストに、実行結果 を変換できます。これにより、あるエージェントの実行結果 を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、これは次回 ユーザー が何かを入力する際に役立つことがよくあります。たとえば、フロントラインのトリアージ エージェントが言語特化のエージェントにハンドオフ する場合、最後のエージェントを保存しておき、次回 ユーザー がエージェントにメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフ が発生したことを示します。raw アイテムはハンドオフ ツール呼び出しへのツール応答です。アイテムからソース／ターゲットのエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw アイテムはツール応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、該当する場合にガードレールの実行結果 が含まれます。ガードレールの実行結果 には、ログ記録や保存に役立つ有用な情報が含まれることがあるため、参照できるように提供しています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合これを必要としませんが、必要なときのために利用可能です。