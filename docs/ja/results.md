---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります:

-   `run` または `run_sync` を呼んだ場合は [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼んだ場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、主な有用情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。次のいずれかです:

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。これは ハンドオフ のため、静的型付けができません。ハンドオフ が発生すると、どのエージェントでも最後のエージェントになり得るため、可能な出力型の集合を静的には特定できません。

## 次ターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、提供した元の入力に、エージェントの実行中に生成されたアイテムを連結した入力リストに実行結果を変換できます。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を末尾に追加したりするのが容易になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入ります。アプリケーションによっては、これは次回 ユーザー が何かを入力する際に役立つことがよくあります。たとえば、一次トリアージのエージェントから特定言語に特化したエージェントへ ハンドオフ する構成の場合、最後のエージェントを保存しておき、次に ユーザー がそのエージェントにメッセージを送る際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入ります。アイテムは [`RunItem`][agents.items.RunItem] です。Run item は、LLM が生成した生のアイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを表します。生のアイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM が ハンドオフ ツールを呼び出したことを示します。生のアイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフ が行われたことを示します。生のアイテムは ハンドオフ ツール呼び出しへのツール応答です。アイテムから送信元/送信先のエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを起動したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。生のアイテムはツールの応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論アイテムを示します。生のアイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの実行結果（ある場合）が入ります。ガードレールの実行結果には、ログ保存しておきたい有用な情報が含まれることがあるため、利用できるようにしています。

### 生のレスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が入ります。多くの場合これは不要ですが、必要な場合に備えて利用できます。