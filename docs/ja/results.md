---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります。

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、最も有用な情報の多くはそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は、`last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフがあるため、静的な型付けはできません。ハンドオフが発生する場合、どのエージェントが最後になるか分からないため、可能な出力タイプの集合を静的に特定できないためです。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、提供した元の入力に、エージェントの実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、これは次回 ユーザー が入力する際に役立つことがよくあります。たとえば、フロントラインのトリアージ用エージェントが言語別のエージェントにハンドオフする場合、最後のエージェントを保存しておき、次回 ユーザー がエージェントにメッセージを送る際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、LLM が生成した生のアイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。生のアイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフ ツールを呼び出したことを示します。生のアイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。生のアイテムはハンドオフ ツール呼び出しに対するツール応答です。アイテムから送信元/宛先のエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。生のアイテムはツールの応答です。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。生のアイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果（ある場合）が含まれます。ガードレール結果には、ログや保存に役立つ情報が含まれることがあるため、参照できるようにしています。

### 生の応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合は不要ですが、必要に応じて参照できます。