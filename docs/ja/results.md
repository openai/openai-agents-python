---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります:

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が入ります。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   エージェントに出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` は型が `Any` です。ハンドオフの可能性があるため、静的な型付けはできません。ハンドオフが発生すると、どのエージェントが最後になるか分からないため、可能な出力タイプの集合を静的に特定できません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、実行結果を、あなたが最初に提供した元の入力と、エージェントの実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追記したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが入ります。アプリケーションによっては、次回 ユーザー が入力する際にこれが役立つことがよくあります。たとえば、フロントラインのトリアージ エージェントが言語別エージェントへハンドオフする場合、最後のエージェントを保存しておき、次回 ユーザー がエージェントにメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入ります。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、LLM が生成した生のアイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。生のアイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフ ツールを呼び出したことを示します。生のアイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。生のアイテムはハンドオフ ツール呼び出しへのツール応答です。アイテムからソース/ターゲットのエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。生のアイテムはツール応答です。アイテムからツールの出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。生のアイテムは生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの実行結果（存在する場合）が入ります。ガードレールの結果には、記録や保存したい有用な情報が含まれることがあるため、利用できるようにしています。

### Raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が入ります。ほとんどの場合は不要ですが、必要であれば利用できます。