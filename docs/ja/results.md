---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返されます。

- `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
- `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、多くの有用な情報はここに格納されています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。内容は次のいずれかです。

- 最後のエージェントで `output_type` が定義されていない場合は `str`
- `last_agent.output_type` が定義されている場合はその型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフがある可能性があるため、静的に型付けすることはできません。ハンドオフが発生すると、どのエージェントが最後になるか分からないため、可能な出力型の集合を静的に特定できないからです。

## 次ターン用の入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、最初に渡した入力にエージェント実行中に生成された項目を連結した入力リストを取得できます。これにより、一度のエージェント実行の出力を次の実行に渡したり、ループ処理でユーザーの新しい入力を都度追加したりするのが簡単になります。

## 最後に実行したエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されます。アプリケーションによっては、次にユーザーが入力した際に役立つことが多いです。たとえば、一次対応のトリアージエージェントが言語固有のエージェントへハンドオフする場合、`last_agent` を保存しておき、次回ユーザーがメッセージを送ったときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新規アイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] でラップされています。RunItem は LLM が生成した raw アイテムを保持します。

- [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。raw アイテムはツール呼び出しアイテムです。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw アイテムはハンドオフツールへの応答です。ソース／ターゲットエージェントにもアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツール応答です。ツールの出力にもアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論内容です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が入ります（存在する場合）。ガードレール結果にはログや保存に役立つ情報が含まれることがあるため、参照できるようにしています。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が保存されています。大半のケースでは不要ですが、必要に応じて参照できます。