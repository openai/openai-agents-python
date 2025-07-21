---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、返される値は次のいずれかです。

-   [`RunResult`][agents.result.RunResult] を返すのは `run` もしくは `run_sync` を呼び出した場合
-   [`RunResultStreaming`][agents.result.RunResultStreaming] を返すのは `run_streamed` を呼び出した場合

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、ほとんどの有用な情報はここに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行された エージェント からの最終出力が入っています。内容は次のいずれかです。

-   最後の エージェント に `output_type` が定義されていない場合は `str`
-   エージェント に `output_type` が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` の型は `Any` です。ハンドオフが発生する可能性があるため、静的型付けはできません。ハンドオフが起こると、どの エージェント が最後になるか分からないため、可能な出力型の集合を静的に特定できないのです。

## 次のターン用入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、実行時に生成されたアイテムを元の入力と連結した入力リストに変換できます。これにより、ある エージェント の実行結果をそのまま別の実行に渡したり、ループで実行して毎回新しい ユーザー 入力を追加したりするのが簡単になります。

## 最後に実行されたエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行された エージェント が入っています。アプリケーションによっては、これは次回 ユーザー が入力を送ってきたときに便利です。たとえば、一次トリアージを行い言語別 エージェント へハンドオフするフロントライン エージェント がいる場合、最後の エージェント を保存しておけば、次回のメッセージで再利用できます。

## 新しく生成されたアイテム

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しいアイテムが入っています。アイテムは [`RunItem`][agents.items.RunItem] でラップされており、その中に LLM が生成した raw アイテムが含まれます。

-   [`MessageOutputItem`][agents.items.MessageOutputItem] は LLM からのメッセージを表します。raw アイテムは生成されたメッセージです。
-   [`HandoffCallItem`][agents.items.HandoffCallItem] は LLM がハンドオフツールを呼び出したことを示します。raw アイテムは LLM からのツール呼び出しアイテムです。
-   [`HandoffOutputItem`][agents.items.HandoffOutputItem] はハンドオフが発生したことを示します。raw アイテムはハンドオフツール呼び出しへのツール応答です。また、アイテムからソース / ターゲット エージェント にもアクセスできます。
-   [`ToolCallItem`][agents.items.ToolCallItem] は LLM がツールを呼び出したことを示します。
-   [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] はツールが呼び出されたことを示します。raw アイテムはツール応答です。また、アイテムからツール出力にもアクセスできます。
-   [`ReasoningItem`][agents.items.ReasoningItem] は LLM からの推論アイテムを示します。raw アイテムは生成された推論です。

## その他の情報

### ガードレール結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、ガードレールがあればその結果が格納されます。ガードレール結果にはログ出力や保存に役立つ情報が含まれることがあるため、利用できるようにしています。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM が生成した [`ModelResponse`][agents.items.ModelResponse] が入っています。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに渡した元の入力が入っています。通常は必要ありませんが、必要に応じて利用できます。