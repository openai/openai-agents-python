---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返されます。

-   [`RunResult`][agents.result.RunResult] — `run` または `run_sync` を呼び出した場合  
-   [`RunResultStreaming`][agents.result.RunResultStreaming] — `run_streamed` を呼び出した場合  

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。内容は次のいずれかです。

-   `str` 型 — 最後のエージェントに `output_type` が設定されていない場合  
-   `last_agent.output_type` 型のオブジェクト — エージェントに `output_type` が設定されている場合  

!!! note

    `final_output` の型は `Any` です。ハンドオフが起こり得るため、静的に型を固定できません。ハンドオフが発生すると、どのエージェントが最後になるか分からないため、可能な出力型の集合を静的に特定できないからです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、元の入力にエージェント実行中に生成されたアイテムを連結した入力リストを取得できます。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりすることが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されます。アプリケーションによっては、次回ユーザーが入力した際にこれを再利用すると便利です。たとえば、一次受付のエージェントが言語別エージェントへハンドオフする場合、最後のエージェントを保存しておき、ユーザーが次にメッセージを送った際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入ります。アイテムは [`RunItem`][agents.items.RunItem] でラップされており、 raw アイテムは LLM が生成した生データです。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] — LLM からのメッセージを示します。 raw アイテムは生成されたメッセージです。  
-   [`HandoffCallItem`][agents.items.HandoffCallItem] — LLM がハンドオフツールを呼び出したことを示します。 raw アイテムは LLM のツール呼び出しです。  
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] — ハンドオフが発生したことを示します。 raw アイテムはハンドオフツール呼び出しへのツール応答です。このアイテムからソース／ターゲットのエージェントにもアクセスできます。  
-   [`ToolCallItem`][agents.items.ToolCallItem] — LLM がツールを呼び出したことを示します。  
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] — ツールが呼び出されたことを示します。 raw アイテムはツール応答で、ツール出力にもアクセスできます。  
-   [`ReasoningItem`][agents.items.ReasoningItem] — LLM からの推論内容を示します。 raw アイテムは生成された推論テキストです。  

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの実行結果が入ります（存在する場合）。ガードレール結果にはログや保存に役立つ情報が含まれることがあるため、ここで取得できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、 LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が入ります。多くの場合は不要ですが、必要に応じて参照できます。