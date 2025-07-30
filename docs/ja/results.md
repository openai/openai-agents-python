---
search:
  exclude: true
---
# 結果

` Runner.run ` メソッドを呼び出すと、返されるのは次のいずれかです。

-   [`RunResult`][agents.result.RunResult] ─ ` run ` または ` run_sync ` を呼び出した場合
-   [`RunResultStreaming`][agents.result.RunResultStreaming] ─ ` run_streamed ` を呼び出した場合

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。内容は以下のいずれかです。

-   最後のエージェントで ` output_type ` が定義されていない場合は ` str `
-   エージェントで ` output_type ` が定義されている場合は ` last_agent.output_type ` 型のオブジェクト

!!! note

    ` final_output ` の型は ` Any ` です。ハンドオフが存在するため静的型付けができません。ハンドオフが発生すると、どのエージェントが最後になるか分からないため、取り得る出力型の集合を静的に特定できないからです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力とエージェント実行中に生成されたアイテムを連結した入力リストを作成できます。これにより、あるエージェント実行の出力を別の実行へ渡したり、ループで回して毎回新しいユーザー入力を追加したりすることが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入ります。アプリケーションによっては、次回のユーザー入力時にこれが役立つことがよくあります。たとえば、一次受付のエージェントが言語別エージェントへハンドオフする場合、最後のエージェントを保存しておけば、ユーザーが次にメッセージを送った際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入ります。アイテムは [`RunItem`][agents.items.RunItem] でラップされており、LLM が生成した raw アイテムを保持します。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は、LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフが発生したことを示します。raw アイテムはハンドオフツール呼び出しへのツール応答です。アイテムから source/target エージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw アイテムはツール応答で、アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は、LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が入ります（存在する場合）。ガードレール結果にはログや保存に役立つ情報が含まれることがあるため、これらを参照できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されています。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、` run ` メソッドへ渡した元の入力が入ります。多くの場合は不要ですが、必要に応じて参照できます。