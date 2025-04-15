# 結果

`Runner.run` メソッドを呼び出すと、以下のいずれかが返されます:

-   `run` または `run_sync` を呼び出した場合は、[`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出した場合は、[`RunResultStreaming`][agents.result.RunResultStreaming]

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されます。これは以下のいずれかです:

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに `output_type` が定義されている場合は、`last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフのため、静的に型を決定することはできません。ハンドオフが発生した場合、どのエージェントが最後になるか分からないため、出力型の集合を静的に知ることができません。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うことで、実行結果を入力リストに変換できます。これは、あなたが提供した元の入力と、エージェント実行中に生成されたアイテムを連結したものです。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されます。アプリケーションによっては、次回ユーザーが何か入力した際にこれが役立つことがよくあります。たとえば、フロントラインのトリアージエージェントが言語特化エージェントにハンドオフする場合、最後のエージェントを保存しておき、次回ユーザーがエージェントにメッセージを送る際に再利用できます。

## 新しいアイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが格納されます。これらのアイテムは [`RunItem`][agents.items.RunItem] です。RunItem は LLM によって生成された raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。raw アイテムは LLM からのツールコールアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw アイテムはハンドオフツールコールへのツールレスポンスです。また、アイテムからソース/ターゲットエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツールレスポンスです。また、アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果（存在する場合）が格納されます。ガードレール結果には、ログや保存に役立つ有用な情報が含まれることがあるため、これらを利用できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が格納されます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が格納されます。ほとんどの場合これは必要ありませんが、必要な場合のために利用可能です。