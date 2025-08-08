---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返されます:

-   [`RunResult`][agents.result.RunResult] — `run` または `run_sync` を呼び出した場合  
-   [`RunResultStreaming`][agents.result.RunResultStreaming] — `run_streamed` を呼び出した場合  

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が格納されます。内容は次のいずれかです:

-   エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note
    `final_output` の型は `Any` です。ハンドオフ が発生する可能性があるため、静的に型付けすることはできません。ハンドオフ が起こると、どの エージェント が最後になるか事前には分からないため、取り得る出力型の集合を静的に確定できないからです。

## 次ターン用入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力に エージェント 実行中に生成されたアイテムを連結した入力リストを取得できます。これにより、ある エージェント の出力を別の実行へ渡したり、ループで実行しつつ新しい ユーザー 入力を毎回追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が格納されます。アプリケーションによっては、これは次回 ユーザー が入力する際に役立つことが多いです。たとえば、一次受付用の エージェント から言語別 エージェント へハンドオフする場合、最後の エージェント を保存しておき、次回 ユーザー がメッセージを送ったときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。各アイテムは [`RunItem`][agents.items.RunItem] で、LLM が生成した raw アイテムをラップしています。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] — LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。  
-   [`HandoffCallItem`][agents.items.HandoffCallItem] — LLM が handoff ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しです。  
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] — ハンドオフ が発生したことを示します。raw アイテムは handoff ツール呼び出しへのツール応答です。アイテムからソース／ターゲット エージェント にもアクセスできます。  
-   [`ToolCallItem`][agents.items.ToolCallItem] — LLM がツールを呼び出したことを示します。  
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] — ツールが呼び出されたことを示します。raw アイテムはツール応答です。アイテムからツール出力にもアクセスできます。  
-   [`ReasoningItem`][agents.items.ReasoningItem] — LLM からの推論アイテムを示します。raw アイテムは生成された推論です。  

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレール の結果が格納されます (存在する場合)。ガードレール結果にはログや保存に有用な情報が含まれることがあるため、参照できるようにしています。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドへ渡した元の入力が格納されています。多くの場合は不要ですが、必要に応じて参照できます。