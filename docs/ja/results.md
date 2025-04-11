# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが得られます：

-   `run` または `run_sync` を呼び出すと [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出すと [`RunResultStreaming`][agents.result.RunResultStreaming]

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここにあります。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれています。これは次のいずれかです：

-   `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は `last_agent.output_type` のオブジェクト

!!! note

    `final_output` は `Any` 型です。ハンドオフのため、静的に型を指定することはできません。ハンドオフが発生すると、どのエージェントが最後になるか分からないため、可能な出力タイプのセットを静的に知ることはできません。

## 次のターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用して、結果を入力リストに変換できます。これにより、提供した元の入力をエージェント実行中に生成されたアイテムに連結します。これにより、1つのエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれています。アプリケーションによっては、次回ユーザーが何かを入力する際に役立つことがよくあります。たとえば、フロントラインのトリアージエージェントが言語特定のエージェントにハンドオフする場合、最後のエージェントを保存し、次回ユーザーがエージェントにメッセージを送信する際に再利用できます。

## 新しいアイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれています。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは LLM によって生成された raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw アイテムはハンドオフツール呼び出しへのツール応答です。アイテムからソース/ターゲットエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツール応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が含まれています。ガードレール結果には、ログや保存に役立つ情報が含まれることがあるため、これらを利用可能にしています。

### Raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれています。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれています。ほとんどの場合、これを必要としませんが、必要な場合に備えて利用可能です。