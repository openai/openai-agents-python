---
search:
  exclude: true
---
# 結果

` Runner.run ` メソッドを呼び出すと、戻り値は次のいずれかになります。

-   [`RunResult`][agents.result.RunResult] （ `run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming] （ `run_streamed` を呼び出した場合）

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入っています。内容は次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note
    `final_output` の型は `Any` です。ハンドオフが発生する可能性があるため静的に型付けできません。ハンドオフが発生した場合、どのエージェントが最後になるか分からないため、可能な出力型の集合を静的に知ることはできません。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、元の入力とエージェント実行中に生成されたアイテムを連結して入力リストを作成できます。これにより、一度のエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入っています。アプリケーションによっては、ユーザーが次に入力する際にこれを再利用すると便利です。たとえば、最初にフロントラインのトリアージエージェントが言語別エージェントへハンドオフする場合、最後のエージェントを保存しておけば、ユーザーが次にメッセージを送ったときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入っています。アイテムは [`RunItem`][agents.items.RunItem] でラップされており、LLM が生成した生のアイテムを保持します。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。生のアイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。生のアイテムはツールコールアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。生のアイテムはハンドオフツールコールへのツール応答です。アイテムからソース／ターゲットエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが実行されたことを示します。生のアイテムはツール応答です。ツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。生のアイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果（存在する場合）が入っています。ガードレール結果にはログや保存に有用な情報が含まれることがあるため、こちらで参照できます。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が入っています。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、 `run` メソッドに渡した元の入力が入っています。通常は必要ありませんが、必要に応じて参照できます。