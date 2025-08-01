---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返されます:

-   [`RunResult`][agents.result.RunResult] — `run` あるいは `run_sync` を呼び出した場合  
-   [`RunResultStreaming`][agents.result.RunResultStreaming] — `run_streamed` を呼び出した場合  

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、有用な情報のほとんどはそこに格納されています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されます。内容は次のいずれかです:

-   最後のエージェントに `output_type` が定義されていない場合は `str`  
-   `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト  

!!! note

    `final_output` の型は `Any` です。ハンドオフが発生すると、どの Agent でも最後のエージェントになり得るため、静的に型を決定できません。その結果、取り得る出力型の集合を静的に判定することができないのです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力とエージェント実行中に生成されたアイテムを結合した入力リストを作成できます。これにより、あるエージェント実行の出力を別の実行へ渡したり、ループで実行して毎回新しいユーザー入力を追加したりすることが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されます。アプリケーションによっては、次回ユーザーが入力する際にこれを利用すると便利です。たとえば、フロントラインのトリアージエージェントが言語別エージェントへハンドオフする場合、`last_agent` を保存しておけば、次回ユーザーがメッセージを送ったときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] であり、raw アイテムをラップしています。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] — LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。  
-   [`HandoffCallItem`][agents.items.HandoffCallItem] — LLM がハンドオフツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。  
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] — ハンドオフが発生したことを示します。raw アイテムはハンドオフツール呼び出しへのツール応答です。ソース／ターゲットのエージェントにもアクセスできます。  
-   [`ToolCallItem`][agents.items.ToolCallItem] — LLM がツールを呼び出したことを示します。  
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] — ツールが呼び出されたことを示します。raw アイテムはツールの応答であり、ツール出力にもアクセスできます。  
-   [`ReasoningItem`][agents.items.ReasoningItem] — LLM からの推論アイテムを示します。raw アイテムは生成された推論内容です。  

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] には、ガードレールの結果が含まれます (存在する場合)。ログや保存に役立つ情報が含まれることがあるため、これらを参照できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されています。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が格納されています。通常は不要ですが、必要に応じて参照できます。