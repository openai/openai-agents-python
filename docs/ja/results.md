---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、戻り値は次のいずれかになります。

-   [`RunResult`][agents.result.RunResult] — `run` または `run_sync` を呼び出した場合
-   [`RunResultStreaming`][agents.result.RunResultStreaming] — `run_streamed` を呼び出した場合

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、多くの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。内容は次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` は `Any` 型です。ハンドオフが発生する可能性があるため、静的に型指定することはできません。ハンドオフが行われた場合、どのエージェントが最後になるか分からないため、可能な出力型の集合を静的に把握できないからです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力とエージェント実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、あるエージェント実行の出力を次の実行へ渡したり、ループで実行して毎回新しいユーザー入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入ります。アプリケーションによっては、次回ユーザーが入力する際に役立つことがよくあります。たとえば、一次対応用のエージェントが言語別のエージェントへハンドオフする場合、最後のエージェントを保存しておき、次回のユーザーメッセージで再利用することができます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入ります。アイテムは [`RunItem`][agents.items.RunItem] でラップされています。RunItem は LLM が生成した raw アイテムを包むものです。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM が handoff ツールを呼び出したことを示します。raw アイテムは LLM からのツールコールです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw アイテムは handoff ツールコールへのツール応答です。アイテムからソース／ターゲットエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツール応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が入ります（存在する場合）。ガードレール結果にはログや保存に役立つ情報が含まれることがあるため、これらを公開しています。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が入ります。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が入ります。通常は必要ありませんが、必要な場合に備えて利用できるようにしています。