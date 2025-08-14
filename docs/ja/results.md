---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、以下のいずれかが返されます:

- [`RunResult`][agents.result.RunResult] は `run` または `run_sync` を呼び出した場合
- [`RunResultStreaming`][agents.result.RunResultStreaming] は `run_streamed` を呼び出した場合

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、有用な情報のほとんどはそこに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されます。内容は次のいずれかです:

- 最後のエージェントに `output_type` が設定されていない場合は `str`
- エージェントに `output_type` が設定されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフが発生する可能性があるため、静的に型を決定できません。ハンドオフが起こると、どのエージェントが最後になるか分からないため、可能な出力型の集合を静的に特定できないのです。

## 次ターン入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、元の入力とエージェント実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、一度のエージェント実行の出力を次の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されます。アプリケーションによっては、ユーザーが次に入力を送る際にこれが役立つことがよくあります。たとえば、フロントラインのトリアージエージェントが言語別エージェントへハンドオフする場合、最後のエージェントを保存しておき、ユーザーが次にメッセージを送ったときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。各アイテムは [`RunItem`][agents.items.RunItem]s です。RunItem は LLM が生成した raw アイテムをラップします。

- [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw アイテムはハンドオフツール呼び出しに対するツール応答です。source/target エージェントにもアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツールの応答で、ツール出力にもアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレール（存在する場合）の結果が含まれます。ガードレールの結果にはログや保存に便利な情報が含まれることがあるため、これらを利用できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が格納されています。通常は不要ですが、必要に応じて参照できます。