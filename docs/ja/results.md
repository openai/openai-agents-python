---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります:

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、そこに最も有用な情報が含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が含まれます。これは次のいずれかです:

-   最後の エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に出力タイプが定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフ があるため、これは静的に型付けできません。ハンドオフ が発生する場合、どの エージェント も最後の エージェント になり得るため、可能な出力タイプの集合を静的に知ることはできません。

## 次のターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、実行結果を、元の入力と エージェント 実行中に生成されたアイテムを連結した入力リストに変換できます。これにより、ある エージェント 実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが簡単になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が含まれます。アプリケーションによっては、次回 ユーザー が入力する際に便利です。例えば、フロントラインのトリアージ エージェント が言語特化の エージェント にハンドオフ する構成であれば、最後の エージェント を保存しておき、次回 ユーザー が エージェント にメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新規アイテムが含まれます。各アイテムは [`RunItem`][agents.items.RunItem] です。Run item は、LLM が生成した生のアイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。生のアイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM がハンドオフ ツールを呼び出したことを示します。生のアイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフ が発生したことを示します。生のアイテムはハンドオフ ツール呼び出しに対するツールのレスポンスです。アイテムからソース/ターゲットの エージェント にもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。生のアイテムはツールのレスポンスです。アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM からの推論アイテムを示します。生のアイテムは生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合に ガードレール の実行結果が含まれます。ガードレール の実行結果には、ログ記録や保存に有用な情報が含まれることがあるため、アクセス可能にしています。

### 生のレスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が含まれます。ほとんどの場合これは不要ですが、必要な場合に備えて参照できます。