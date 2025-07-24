---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、以下のいずれかが返されます。

- `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
- `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。これは次のいずれかです。

- 最後のエージェントに `output_type` が定義されていない場合は `str`
- エージェントに `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフが発生する可能性があるため、静的に型を固定できません。ハンドオフが起こると、どのエージェントが最後になるか分からないため、出力型の集合を静的に特定できないからです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、元の入力にエージェント実行中に生成された項目を連結した入力リストへ変換できます。これにより、あるエージェント実行の出力を別の実行へ渡したり、ループ内で実行して毎回新しいユーザー入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入ります。アプリケーションによっては、これはユーザーが次に入力する際に役立つことが多いです。たとえば、一次対応のトリアージ エージェントが言語特化型エージェントへハンドオフする場合、最後のエージェントを保存しておき、ユーザーが次にメッセージを送ったときに再利用できます。

## 新しい項目

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しい項目が含まれます。各項目は [`RunItem`][agents.items.RunItem] です。RunItem は LLM が生成した raw 項目をラップします。

- [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw 項目は生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM が handoff ツールを呼び出したことを示します。raw 項目は LLM からのツール呼び出し項目です。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] は handoff が発生したことを示します。raw 項目は handoff ツール呼び出しに対するツールの応答です。この項目からソース／ターゲット エージェントにもアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw 項目はツールの応答です。この項目からツール出力にもアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論項目を示します。raw 項目は生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が入ります (存在する場合)。ガードレール結果には保存やログ出力したい有用な情報が含まれることがあるため、これらを利用できます。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が入ります。ほとんどの場合は不要ですが、必要に応じて参照できます。