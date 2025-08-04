---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返されます。

-   [`RunResult`][agents.result.RunResult]   `run` または `run_sync` を呼び出した場合  
-   [`RunResultStreaming`][agents.result.RunResultStreaming]   `run_streamed` を呼び出した場合

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されます。内容は次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`  
-   エージェントに `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。handoffs が発生する可能性があるため、静的に型を決定できません。handoffs が起こると、どのエージェントが最後になるか分からないため、可能な出力型の集合を静的に特定できないのです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力とエージェント実行中に生成されたアイテムを連結した入力リストを取得できます。これにより、あるエージェント実行の出力を次の実行にそのまま渡したり、ループで実行して毎回新しい user 入力を追加したりすることが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されます。アプリケーションによっては、次に user が入力した際にこれを再利用すると便利です。たとえば、フロントラインのトリアージ エージェントが言語別エージェントへ handoff する場合、最後のエージェントを保存しておき、次回の user メッセージで再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが格納されます。アイテムは [`RunItem`][agents.items.RunItem] でラップされており、raw アイテムは LLM が生成したものです。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]   LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。  
-   [`HandoffCallItem`][agents.items.HandoffCallItem]   LLM が handoff ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。  
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]   handoff が発生したことを示します。raw アイテムは handoff ツール呼び出しへのツール応答です。このアイテムから source/target エージェントにもアクセスできます。  
-   [`ToolCallItem`][agents.items.ToolCallItem]   LLM がツールを呼び出したことを示します。  
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]   ツールが呼び出されたことを示します。raw アイテムはツール応答です。このアイテムからツール出力にもアクセスできます。  
-   [`ReasoningItem`][agents.items.ReasoningItem]   LLM からの reasoning アイテムを示します。raw アイテムは生成された reasoning です。  

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が格納されます（存在する場合）。ガードレール結果には記録や保存が必要な有用情報が含まれることがあるため、これらを提供しています。

### Raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が格納されています。通常は使用しませんが、必要な場合に備えて利用可能です。