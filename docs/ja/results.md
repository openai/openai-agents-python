---
search:
  exclude: true
---
# 実行結果

`Runner.run` 系メソッドを呼び出すと、結果は次のいずれかです。

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、有用な情報の多くはそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。内容は次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   そのエージェントに出力タイプが定義されている場合は、型 `last_agent.output_type` のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフが発生し得るため、静的に型付けできません。ハンドオフが起こると、どのエージェントでも最後のエージェントになり得るため、取り得る出力タイプの集合を静的には特定できません。

## 次のターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、実行結果を、最初に与えた元の入力とエージェントの実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、あるエージェントの実行結果を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが格納されます。アプリケーションによっては、次回ユーザーが入力する際に役立つことがよくあります。たとえば、フロントラインのトリアージエージェントが言語別のエージェントにハンドオフする構成であれば、最後のエージェントを保存しておき、次回ユーザーがエージェントにメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] です。実行アイテムは、 LLM によって生成された生のアイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は、 LLM からのメッセージを示します。生のアイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は、 LLM がハンドオフツールを呼び出したことを示します。生のアイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフが発生したことを示します。生のアイテムは、そのハンドオフツール呼び出しに対するツールのレスポンスです。アイテムからソース/ターゲットのエージェントにもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は、 LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。生のアイテムはツールのレスポンスです。アイテムからツールの出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は、 LLM からの推論アイテムを示します。生のアイテムは生成された推論です。

## その他の情報

### ガードレールの結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールの結果（存在する場合）が含まれます。ガードレール結果には、ログに記録したり保存したくなる有用な情報が含まれることがあるため、これらを参照できるようにしています。

### 生のレスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、 LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が含まれます。多くの場合これは不要ですが、必要なときのために参照できます。