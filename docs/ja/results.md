---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、戻り値は次のいずれかです:

-   `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が入ります。これは次のいずれかです:

-   最後の エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフ がある可能性があるため、静的に型付けすることはできません。ハンドオフ が発生すると、どの エージェント が最後になるか分からないため、取り得る出力型の集合を静的に決定できないからです。

## 次ターン用の入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力と エージェント 実行中に生成されたアイテムを連結した入力リストへ変換できます。これにより、ある エージェント 実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりすることが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が格納されます。アプリケーションによっては、これは次回の ユーザー 入力時に非常に便利です。たとえば、一次受付の振り分け エージェント が言語別の エージェント にハンドオフ する場合、最後の エージェント を保存しておき、次回 ユーザー からメッセージが来た際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。各アイテムは [`RunItem`][agents.items.RunItem] であり、LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。  
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM が handoff ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。  
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフ が発生したことを示します。raw アイテムは handoff ツール呼び出しへのツール応答です。このアイテムからソース / ターゲット エージェント も取得できます。  
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。  
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。raw アイテムはツールの応答です。また、このアイテムからツール出力にアクセスできます。  
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの reasoning アイテムを示します。raw アイテムは生成された reasoning です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレール の結果が格納されます (存在する場合)。ガードレール結果には記録や保存を行いたい有用な情報が含まれることがあるため、これらを参照できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が入ります。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が格納されています。多くの場合は不要ですが、必要に応じて参照できます。