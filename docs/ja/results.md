---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります:

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、最も有用な情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです:

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフがあるため、静的に型付けできません。ハンドオフが発生すると、任意のエージェントが最後のエージェントになり得るため、可能な出力タイプの集合を静的には把握できません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、もともと渡した入力に、エージェントの実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、あるエージェントの出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、次に ユーザー が入力する際に役立つことがよくあります。例えば、フロントラインの振り分けエージェントが言語別のエージェントにハンドオフする場合、最後のエージェントを保存しておき、次回 ユーザー がエージェントにメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、LLM が生成した raw アイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフが発生したことを示します。raw アイテムはハンドオフ ツール呼び出しへのツール応答です。アイテムからソース/ターゲットのエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw アイテムはツールの応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの実行結果（存在する場合）が含まれます。ガードレールの実行結果には、記録や保存に有用な情報が含まれることがあるため、参照できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が含まれます。多くの場合は不要ですが、必要な場合に備えて利用できるようにしています。