---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが得られます。

- [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
- [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が含まれます。これは次のいずれかです。

- 最後の エージェント に `output_type` が定義されていない場合は `str`
- エージェント に出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフ の可能性があるため、静的な型付けはできません。ハンドオフ が発生すると、どの エージェント でも最後になり得るため、可能な出力タイプの集合を静的には把握できません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、実行結果を、最初に提供した元の入力に、エージェント 実行中に生成された項目を連結した入力リストへと変換できます。これにより、ある エージェント 実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が含まれます。アプリケーションによっては、これは次回 ユーザー が何かを入力する際に有用なことが多いです。たとえば、最前線のトリアージ エージェント から言語特化の エージェント にハンドオフ する場合、最後の エージェント を保存しておき、次回 ユーザー がメッセージを送る際に再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、 LLM が生成した raw アイテムをラップします。

- [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフ が発生したことを示します。raw アイテムはハンドオフ ツール呼び出しに対するツールの応答です。アイテムからソース/ターゲットの エージェント にアクセスすることもできます。
- [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツールの応答です。アイテムからツールの出力にアクセスすることもできます。
- [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレールの結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合はガードレールの結果が含まれます。ガードレールの結果には、記録や保存に有用な情報が含まれることがあるため、これらを利用できるようにしています。

ツールのガードレール結果は、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として個別に利用できます。これらのガードレールはツールにアタッチでき、ツール呼び出しは エージェント のワークフロー中にガードレールを実行します。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、 LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合これは不要ですが、必要な場合に備えて利用可能です。