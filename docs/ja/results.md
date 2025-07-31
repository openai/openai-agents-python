---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、返されるのは次のいずれかです。

-   [`RunResult`][agents.result.RunResult] — `run` または `run_sync` を呼び出した場合  
-   [`RunResultStreaming`][agents.result.RunResultStreaming] — `run_streamed` を呼び出した場合  

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。内容は次のいずれかです。

-   エージェントに `output_type` が定義されていない場合は `str`
-   `last_agent.output_type` が定義されている場合はその型のオブジェクト

!!! note

    `final_output` は `Any` 型です。ハンドオフが発生する可能性があるため、静的に型付けすることはできません。ハンドオフが起きると、どのエージェントが最後になるか静的には分からないため、出力型の集合も不定になります。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力にエージェント実行中に生成された項目を連結した入力リストへ変換できます。これにより、一度のエージェント実行の出力を別の実行へ渡したり、ループで実行しながら毎回新しいユーザー入力を追加するのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入ります。アプリケーションによっては、ユーザーが次に入力する際にこれが役立つことがよくあります。たとえば、最初に振り分けを行うエージェントから言語別エージェントへハンドオフする場合、`last_agent` を保存しておき、ユーザーが次にメッセージを送ったときに再利用できます。

## 新しい項目

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新規項目が格納されます。項目は [`RunItem`][agents.items.RunItem] でラップされ、LLM が生成した raw アイテムを含みます。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] — LLM からのメッセージ。raw アイテムは生成されたメッセージです。  
-   [`HandoffCallItem`][agents.items.HandoffCallItem] — LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムはツール呼び出しアイテムです。  
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] — ハンドオフが発生したことを示します。raw アイテムはハンドオフ ツール呼び出しへの応答です。ソース／ターゲット エージェントにもアクセスできます。  
-   [`ToolCallItem`][agents.items.ToolCallItem] — LLM がツールを呼び出したことを示します。  
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] — ツールが呼び出されたことを示します。raw アイテムはツール応答で、ツール出力にもアクセスできます。  
-   [`ReasoningItem`][agents.items.ReasoningItem] — LLM の推論項目です。raw アイテムは生成された推論内容です。  

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が入ります（存在する場合）。これらにはログや保存に役立つ情報が含まれることがあるため、利用できるようにしています。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が入ります。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が入ります。通常は使用しませんが、必要に応じて参照できます。