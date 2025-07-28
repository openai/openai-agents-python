---
search:
  exclude: true
---
# 結果

`Runner.run` メソッドを呼び出すと、戻り値は次のいずれかになります。

-   `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
-   `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

これらはどちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、有用な情報のほとんどはここに含まれます。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント の最終出力が入ります。内容は次のいずれかです。

-   最後の エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。 ハンドオフ があるため静的型付けはできません。 ハンドオフ が発生すると、どの エージェント でも最後の エージェント になり得るため、可能な出力型の集合を静的に知ることができないからです。

## 次のターンへの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、結果を入力リストへ変換できます。このリストには、元の入力に加えて エージェント の実行中に生成されたアイテムが連結されます。これにより、ある エージェント の実行結果を次の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりする際に便利です。

## 最後の エージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が格納されています。アプリケーションによっては、次回 ユーザー が入力する際にこれが役立つことがよくあります。たとえば、一時受け付けの エージェント が言語特化型の エージェント へ ハンドオフ するような場合、`last_agent` を保存しておけば、次に ユーザー がメッセージを送ったときに再利用できます。

## 新規アイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが含まれます。アイテムは [`RunItem`][agents.items.RunItem] でラップされています。RunItem は LLM が生成した raw アイテムを包むものです。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを示します。 raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM が ハンドオフ ツールを呼び出したことを示します。 raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] は ハンドオフ が発生したことを示します。 raw アイテムは ハンドオフ ツール呼び出しへのツールレスポンスです。ソース / ターゲット エージェント もアイテムから取得できます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。 raw アイテムはツールレスポンスです。ツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。 raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレール の結果があれば格納されています。ガードレール の結果にはログや保存に役立つ情報が含まれることがあるため、これらを参照できるようにしています。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドへ渡した元の入力が入っています。多くの場合これは不要ですが、必要な際に参照できるように公開されています。