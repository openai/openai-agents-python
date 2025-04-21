---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、以下のいずれかが返されます。

-   `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに格納されています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されます。内容は以下のいずれかです。

-   `output_type` が定義されていない場合は `str`
-   `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフが発生する可能性があるため、静的に型付けできません。ハンドオフが発生すると、どのエージェントでも最後になり得るため、可能性のある出力型を静的に特定できないのです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、エージェント実行中に生成されたアイテムを元の入力に連結した入力リストへ変換できます。これにより、あるエージェント実行の出力を別の実行へ渡したり、ループで実行して毎回新しいユーザー入力を追加したりすることが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されています。アプリケーションによっては、次回ユーザーが入力する際にこれが役立つことがよくあります。例えば、フロントラインのトリアージ エージェントが言語専用のエージェントにハンドオフする場合、最後のエージェントを保存しておき、ユーザーが次にメッセージを送ったときに再利用できます。

## 新しいアイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。これらのアイテムは [`RunItem`][agents.items.RunItem] です。RunItem は、 LLM が生成した  raw  アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] —  LLM からのメッセージを示します。  raw  アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] —  LLM がハンドオフ ツールを呼び出したことを示します。  raw  アイテムは  LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] — ハンドオフが発生したことを示します。  raw  アイテムはハンドオフ ツール呼び出しに対するツール応答です。また、アイテムから送信元 / 送信先エージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] —  LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] — ツールが呼び出されたことを示します。  raw  アイテムはツール応答です。また、アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] —  LLM からの推論アイテムを示します。  raw  アイテムは生成された推論内容です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果が存在する場合に格納されます。ガードレール結果には、ログや保存を行いたい有用な情報が含まれることがあるため、これらを参照できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、 LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が格納されます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が格納されます。ほとんどの場合は必要ありませんが、必要に応じて参照できるように用意されています。