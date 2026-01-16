---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります。

- [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
- [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ここに最も有用な情報が含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

- 最後のエージェントに `output_type` が定義されていない場合は `str`
- エージェントに出力タイプが定義されている場合は、型 `last_agent.output_type` のオブジェクト

!!! note

    `final_output` は型 `Any` です。handoffs の可能性があるため、静的型付けはできません。handoffs が発生すると、どのエージェントが最後になるか分からないため、可能な出力タイプの集合を静的には特定できません。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、提供した元の入力と、エージェントの実行中に生成されたアイテムを連結した入力リストへ結果を変換できます。これにより、あるエージェント実行の出力を別の実行へ渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりしやすくなります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、次に ユーザー が何か入力する際に役立ちます。たとえば、フロントラインのトリアージ エージェントが言語別のエージェントに handoff する場合、最後のエージェントを保存しておき、次回 ユーザー がエージェントにメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、LLM が生成した生のアイテムをラップします。

- [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。生のアイテムは生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM が handoff ツールを呼び出したことを示します。生のアイテムは LLM からのツール呼び出しアイテムです。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem]: handoff が発生したことを示します。生のアイテムは handoff ツール呼び出しへのツール応答です。アイテムからソース/ターゲットのエージェントにもアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。生のアイテムはツール応答です。アイテムからツール出力にもアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論アイテムを示します。生のアイテムは生成された推論です。

## その他の情報

### ガードレールの結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果（存在する場合）が含まれます。ガードレールの結果には、記録や保存したい有用な情報が含まれることがあるため、参照できるようにしています。

ツールのガードレール結果は、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として個別に利用できます。これらのガードレールはツールに付与でき、エージェントのワークフロー中にそのツール呼び出しがガードレールを実行します。

### 生の応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合は不要ですが、必要なときのために参照できるようになっています。