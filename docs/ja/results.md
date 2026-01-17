---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかを取得します:

-   [`RunResult`][agents.result.RunResult] は `run` または `run_sync` を呼び出した場合
-   [`RunResultStreaming`][agents.result.RunResultStreaming] は `run_streamed` を呼び出した場合

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです:

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力型が定義されている場合は、型 `last_agent.output_type` のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフがあるため、静的型付けはできません。ハンドオフが発生すると、どのエージェントでも最後のエージェントになり得るため、可能な出力型の集合を静的に知ることができないためです。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、結果を入力リストに変換し、あなたが提供した元の入力に、エージェントの実行中に生成された項目を連結できます。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、これは次回ユーザーが入力する際に有用です。たとえば、フロントラインのトリアージ用エージェントから言語別エージェントへハンドオフする場合、最後のエージェントを保存しておけば、次回ユーザーがメッセージを送る際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。Run item は、LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフのツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフが発生したことを示します。raw アイテムはハンドオフのツール呼び出しに対するツールの応答です。アイテムから送信元/送信先のエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw アイテムはツールの応答です。アイテムからツールの出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は、LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合にガードレールの実行結果が含まれます。ガードレールの実行結果には、ログや保存したい有用な情報が含まれることがあるため、これらを利用できるようにしています。

ツールのガードレール結果は、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として別々に利用できます。これらのガードレールはツールにアタッチでき、ツール呼び出しはエージェントのワークフロー中にガードレールを実行します。

### Raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合これは不要ですが、必要に応じて利用できます。