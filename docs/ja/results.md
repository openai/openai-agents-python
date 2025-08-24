---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります:

-   [`RunResult`][agents.result.RunResult]（`run` または `run_sync` を呼び出した場合）
-   [`RunResultStreaming`][agents.result.RunResultStreaming]（`run_streamed` を呼び出した場合）

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、主要な有用情報はそこに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が含まれます。これは次のいずれかです:

-   最後の エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に出力タイプが定義されている場合は、`last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフ があるため、静的型付けはできません。ハンドオフ が発生すると、どの エージェント でも最後の エージェント になり得るため、可能な出力タイプの集合を静的には特定できません。

## 次ターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、実行結果を入力リストに変換できます。これは、あなたが提供した元の入力に、エージェント 実行中に生成された項目を連結したものです。これにより、ある エージェント 実行の出力を別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が含まれます。アプリケーションによっては、これは次回 ユーザー が入力する際に役立つことがよくあります。たとえば、フロントラインの トリアージ エージェント が言語特化の エージェント にハンドオフ する場合、最後の エージェント を保存しておき、次に ユーザー が エージェント にメッセージを送るときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しい項目が含まれます。項目は [`RunItem`][agents.items.RunItem] です。Run item は、LLM が生成した raw なアイテムをラップします。

-   [`MessageOutputItem`][agents.items.MessageOutputItem]: LLM からのメッセージを示します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem]: LLM がハンドオフ ツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem]: ハンドオフ が発生したことを示します。raw アイテムはハンドオフ ツール呼び出しへのツールのレスポンスです。項目からソース/ターゲットの エージェント にもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem]: LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]: ツールが呼び出されたことを示します。raw アイテムはツールのレスポンスです。項目からツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem]: LLM の推論項目を示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、該当する場合、ガードレールの実行結果が含まれます。ガードレール結果には、ログ記録や保存に役立つ情報が含まれることがあるため、利用できるようにしています。

### Raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM によって生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。ほとんどの場合は不要ですが、必要に応じて利用できます。