---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかを取得します。

-   [`RunResult`][agents.result.RunResult] は、`run` または `run_sync` を呼び出した場合
-   [`RunResultStreaming`][agents.result.RunResultStreaming] は、`run_streamed` を呼び出した場合

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、最も有用な情報の多くはここに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントで `output_type` が定義されていない場合は `str`
-   エージェントで出力タイプが定義されている場合は、`last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` は型が `Any` です。ハンドオフ があるため、これは静的に型付けできません。ハンドオフ が発生する場合、どのエージェントでも最後になる可能性があるため、可能な出力タイプの集合を静的に把握できません。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、結果を入力リストに変換できます。これは、提供した元の入力に、エージェントの実行中に生成されたアイテムを連結したものです。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、これは次回 ユーザー が何かを入力する際に有用です。例えば、フロントラインのトリアージ エージェントが言語別のエージェントへハンドオフ する場合、最後のエージェントを保存しておき、次に ユーザー がメッセージを送る際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。ランアイテムは、LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は、LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフ が発生したことを示します。raw アイテムはハンドオフ ツール呼び出しへのツール応答です。アイテムからソース/ターゲットのエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw アイテムはツールの応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は、LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレール の結果が存在する場合に含まれます。ガードレール の結果には、ログ記録や保存に有用な情報が含まれることがあるため、利用できるようにしています。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合これは不要ですが、必要な場合に備えて利用可能です。