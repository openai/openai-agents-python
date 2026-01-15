---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります。

-   [`RunResult`][agents.result.RunResult]: `run` または `run_sync` を呼び出した場合
-   [`RunResultStreaming`][agents.result.RunResultStreaming]: `run_streamed` を呼び出した場合

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、そこで多くの有用な情報が提供されます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。これは ハンドオフ のため、静的型付けができません。ハンドオフ が発生する場合、どのエージェントでも最後のエージェントになり得るため、可能な出力タイプの集合を静的には特定できません。

## 次ターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、結果を入力リストに変換し、あなたが提供した元の入力と、エージェントの実行中に生成されたアイテムを連結できます。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、これは次回 ユーザー が何かを入力する際に役立つことがよくあります。たとえば、フロントラインのトリアージ エージェントが言語特化のエージェントに ハンドオフ する構成の場合、最後のエージェントを保存しておき、次に ユーザー がそのエージェントにメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。Run item は、LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM が ハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフ が発生したことを示します。raw アイテムは ハンドオフ ツール呼び出しへのツール応答です。アイテムからソース/ターゲット エージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。raw アイテムはツールの応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] の各プロパティには、ガードレール の結果 (存在する場合) が含まれます。ガードレール の結果には、記録や保存に役立つ情報が含まれることがあるため、参照できるようにしています。

ツールの ガードレール 結果は、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として別々に利用できます。これらの ガードレール はツールに付与でき、ツール呼び出しはエージェントのワークフロー中に ガードレール を実行します。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。多くの場合これは不要ですが、必要に応じて参照できます。