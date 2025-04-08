# 実行結果

`Runner.run` メソッドを呼び出すと、以下のいずれかが返されます。

- `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
- `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されています。これは以下のいずれかです。

- 最後のエージェントに `output_type` が定義されていない場合は `str`
- エージェントに `output_type` が定義されている場合は、その型（`last_agent.output_type`）のオブジェクト

!!! note

    `final_output` は型としては `Any` です。これはハンドオフが発生する可能性があるため、静的に型付けできないためです。ハンドオフが発生すると、どのエージェントでも最後のエージェントになり得るため、可能な出力型の集合を静的に特定できません。

## 次のターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、実行結果を入力リストに変換できます。これは、元々提供した入力とエージェントの実行中に生成された項目を連結したものです。これにより、あるエージェントの実行結果を別の実行に渡したり、ループ内で実行して毎回新しいユーザー入力を追加したりすることが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されています。アプリケーションによっては、次回ユーザーが何かを入力する際に役立つことがあります。例えば、最前線のトリアージエージェントが言語特化型エージェントにハンドオフする場合、最後のエージェントを保存しておき、次回ユーザーがエージェントにメッセージを送る際に再利用できます。

## 新規項目

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しい項目が格納されています。これらの項目は [`RunItem`][agents.items.RunItem] です。RunItem は LLM によって生成された raw 項目をラップしています。

- [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw 項目は生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。raw 項目は LLM からのツール呼び出し項目です。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw 項目はハンドオフツール呼び出しに対するツールの応答です。また、この項目からソース／ターゲットエージェントにアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw 項目はツールの応答です。また、この項目からツールの出力にアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem] は LLM による推論項目を示します。raw 項目は生成された推論内容です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの実行結果が格納されています（存在する場合）。ガードレールの実行結果にはログや保存したい有用な情報が含まれることがあるため、これらを利用可能にしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が格納されています。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が格納されています。ほとんどの場合、これは必要ありませんが、必要に応じて利用可能です。