---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次の 2 つの実行結果型のいずれかを受け取ります。

-   `Runner.run(...)` または `Runner.run_sync(...)` からの [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)` からの [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、`final_output`、`new_items`、`last_agent`、`raw_responses`、`to_state()` などの共通の実行結果サーフェスを公開します。

`RunResultStreaming` は、[`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete]、[`cancel(...)`][agents.result.RunResultStreaming.cancel] など、ストリーミング固有の制御機能を追加します。

## 適切な実行結果サーフェスの選択

ほとんどのアプリケーションでは、いくつかの実行結果プロパティまたはヘルパーだけで十分です。

| 必要なもの... | 使用するもの |
| --- | --- |
| ユーザーに表示する最終回答 | `final_output` |
| 完全なローカルトランスクリプトを含む、リプレイ可能な次ターン入力リスト | `to_input_list()` |
| エージェント、ツール、ハンドオフ、承認メタデータを含む詳細な実行項目 | `new_items` |
| 通常、次のユーザーターンを処理すべきエージェント | `last_agent` |
| `previous_response_id` による OpenAI Responses API チェーン | `last_response_id` |
| 保留中の承認と再開可能なスナップショット | `interruptions` and `to_state()` |
| 現在のネストされた `Agent.as_tool()` 呼び出しに関するメタデータ | `agent_tool_invocation` |
| raw モデル呼び出しまたはガードレール診断 | `raw_responses` and the guardrail result arrays |

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていなかった場合は `str`
-   最後のエージェントに出力型が定義されていた場合は `last_agent.output_type` 型のオブジェクト
-   承認中断で一時停止した場合など、最終出力が生成される前に実行が停止した場合は `None`

!!! note

    `final_output` は `Any` として型付けされています。ハンドオフによって、どのエージェントが実行を終了するかが変わる可能性があるため、SDK は考えられる出力型の全体集合を静的に把握できません。

ストリーミングモードでは、ストリームの処理が完了するまで `final_output` は `None` のままです。イベントごとのフローについては、[ストリーミング](streaming.md)を参照してください。

## 入力、次ターン履歴、新規項目

これらのサーフェスは、それぞれ異なる問いに対応します。

| プロパティまたはヘルパー | 含まれる内容 | 最適な用途 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | この実行セグメントの基本入力です。ハンドオフ入力フィルターが履歴を書き換えた場合、実行が継続されたフィルター済み入力がここに反映されます。 | この実行が実際に入力として使用した内容の監査 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 実行を入力項目として見たビューです。デフォルトの `mode="preserve_all"` は、`new_items` から変換された完全な履歴を保持します。`mode="normalized"` は、ハンドオフフィルタリングによってモデル履歴が書き換えられた場合に、正規の継続入力を優先します。 | 手動のチャットループ、クライアント管理の会話状態、プレーンな項目履歴の確認 |
| [`new_items`][agents.result.RunResultBase.new_items] | エージェント、ツール、ハンドオフ、承認メタデータを含む詳細な [`RunItem`][agents.items.RunItem] ラッパーです。 | ログ、UI、監査、デバッグ |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 実行内の各モデル呼び出しからの raw [`ModelResponse`][agents.items.ModelResponse] オブジェクトです。 | プロバイダーレベルの診断または raw レスポンスの確認 |

実際には、次のように使い分けます。

-   実行のプレーンな入力項目ビューが必要な場合は、`to_input_list()` を使用します。
-   ハンドオフフィルタリングまたはネストされたハンドオフ履歴の書き換え後に、次の `Runner.run(..., input=...)` 呼び出しに渡す正規のローカル入力が必要な場合は、`to_input_list(mode="normalized")` を使用します。
-   SDK に履歴の読み込みと保存を任せたい場合は、[`session=...`](sessions/index.md) を使用します。
-   `conversation_id` または `previous_response_id` を使って OpenAI のサーバー管理状態を使用している場合、通常は `to_input_list()` を再送信する代わりに、新しいユーザー入力のみを渡して保存済み ID を再利用します。
-   ログ、UI、監査向けに完全な変換済み履歴が必要な場合は、デフォルトの `to_input_list()` モードまたは `new_items` を使用します。

JavaScript SDK とは異なり、Python ではモデル形式の差分のみを表す個別の `output` プロパティは公開されません。SDK メタデータが必要な場合は `new_items` を使用し、raw モデルペイロードが必要な場合は `raw_responses` を確認してください。

コンピュータツールのリプレイは、raw Responses ペイロードの形状に従います。プレビューモデルの `computer_call` 項目は単一の `action` を保持しますが、`gpt-5.5` のコンピュータ呼び出しではバッチ化された `actions[]` を保持できます。[`to_input_list()`][agents.result.RunResultBase.to_input_list] と [`RunState`][agents.run_state.RunState] は、モデルが生成した形状をそのまま保持するため、手動リプレイ、一時停止/再開フロー、保存済みトランスクリプトは、プレビュー版と GA 版の両方のコンピュータツール呼び出しで引き続き機能します。ローカル実行結果は引き続き `new_items` 内の `computer_call_output` 項目として表示されます。

### 新規項目

[`new_items`][agents.result.RunResultBase.new_items] は、実行中に何が起きたかを最も詳細に確認できるビューです。一般的な項目型は次のとおりです。

-   アシスタントメッセージ用の [`MessageOutputItem`][agents.items.MessageOutputItem]
-   推論項目用の [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses のツール検索リクエストと、ロードされたツール検索の実行結果用の [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] および [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   ツール呼び出しとその実行結果用の [`ToolCallItem`][agents.items.ToolCallItem] および [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   承認待ちで一時停止したツール呼び出し用の [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   ハンドオフリクエストと完了済みの引き継ぎ用の [`HandoffCallItem`][agents.items.HandoffCallItem] および [`HandoffOutputItem`][agents.items.HandoffOutputItem]

エージェントの関連付け、ツール出力、ハンドオフの境界、承認の境界が必要な場合は、常に `to_input_list()` よりも `new_items` を選択してください。

ホスト型ツール検索を使用する場合、モデルが生成した検索リクエストを確認するには `ToolSearchCallItem.raw_item` を確認し、そのターンでどの名前空間、関数、またはホスト型 MCP サーバーがロードされたかを確認するには `ToolSearchOutputItem.raw_item` を確認してください。

## 会話の継続または再開

### 次ターンのエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] には、最後に実行されたエージェントが含まれます。これは多くの場合、ハンドオフ後の次のユーザーターンで再利用するのに最適なエージェントです。

ストリーミングモードでは、[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] が実行の進行に合わせて更新されるため、ストリームが終了する前にハンドオフを観察できます。

### 中断と実行状態

ツールに承認が必要な場合、保留中の承認は [`RunResult.interruptions`][agents.result.RunResult.interruptions] または [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。これには、直接呼び出されたツール、ハンドオフ後に到達したツール、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行によって発生した承認が含まれることがあります。

[`to_state()`][agents.result.RunResult.to_state] を呼び出して、再開可能な [`RunState`][agents.run_state.RunState] を取得し、保留中の項目を承認または拒否してから、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="Use tools when needed.")
result = await Runner.run(agent, "Delete temp files that are no longer needed.")

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state)
```

ストリーミング実行では、まず [`stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を完了してから `result.interruptions` を確認し、`result.to_state()` から再開してください。承認フロー全体については、[ヒューマンインザループ](human_in_the_loop.md)を参照してください。

### サーバー管理の継続

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、実行から得られた最新のモデルレスポンス ID です。OpenAI Responses API チェーンを継続したい場合は、次のターンで `previous_response_id` として渡してください。

すでに `to_input_list()`、`session`、または `conversation_id` で会話を継続している場合、通常は `last_response_id` は不要です。複数ステップの実行におけるすべてのモデルレスポンスが必要な場合は、代わりに `raw_responses` を確認してください。

## ツールとしてのエージェントのメタデータ

実行結果がネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行に由来する場合、[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] は外側のツール呼び出しに関する変更不可のメタデータを公開します。

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

通常のトップレベル実行では、`agent_tool_invocation` は `None` です。

これは、ネストされた実行結果を後処理する際に、外側のツール名、呼び出し ID、または生の引数が必要になることがある `custom_output_extractor` 内で特に便利です。関連する `Agent.as_tool()` パターンについては、[ツール](tools.md)を参照してください。

そのネストされた実行のパース済み構造化入力も必要な場合は、`context_wrapper.tool_input` を読み取ってください。これは、ネストされたツール入力に対して [`RunState`][agents.run_state.RunState] が汎用的にシリアライズするフィールドです。一方、`agent_tool_invocation` は、現在のネストされた呼び出しに対するライブの実行結果アクセサーです。

## ストリーミングのライフサイクルと診断

[`RunResultStreaming`][agents.result.RunResultStreaming] は、上記と同じ実行結果サーフェスを継承しますが、ストリーミング固有の制御機能を追加します。

-   セマンティックなストリームイベントを消費するための [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   実行途中でアクティブなエージェントを追跡するための [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   ストリーミング実行が完全に終了したかどうかを確認するための [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   実行を即時または現在のターン後に停止するための [`cancel(...)`][agents.result.RunResultStreaming.cancel]

非同期イテレーターが終了するまで `stream_events()` を消費し続けてください。そのイテレーターが終了するまで、ストリーミング実行は完了していません。また、`final_output`、`interruptions`、`raw_responses` などの要約プロパティや、セッション永続化の副作用は、目に見える最後のトークンが到着した後もまだ確定中の場合があります。

`cancel()` を呼び出した場合は、キャンセルとクリーンアップが正しく完了できるように、`stream_events()` を消費し続けてください。

Python では、ストリーミング用の個別の `completed` プロミスや `error` プロパティは公開されません。終端的なストリーミング失敗は `stream_events()` から例外が送出されることで表面化し、`is_complete` は実行が終端状態に到達したかどうかを反映します。

### raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] には、実行中に収集された raw モデルレスポンスが含まれます。複数ステップの実行では、ハンドオフをまたいだり、モデル/ツール/モデルのサイクルが繰り返されたりする場合など、複数のレスポンスが生成されることがあります。

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、`raw_responses` の最後のエントリの ID にすぎません。

### ガードレールの実行結果

エージェントレベルのガードレールは、[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] として公開されます。

ツールガードレールは、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] および [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として個別に公開されます。

これらの配列は実行全体で蓄積されるため、判定のログ記録、追加のガードレールメタデータの保存、または実行がブロックされた理由のデバッグに役立ちます。

### コンテキストと使用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] は、アプリのコンテキストと、承認、使用量、ネストされた `tool_input` など SDK が管理するランタイムメタデータを公開します。

使用量は `context_wrapper.usage` で追跡されます。ストリーミング実行では、ストリームの最終チャンクが処理されるまで、使用量の合計値が遅れて反映される場合があります。ラッパーの完全な形状と永続化に関する注意事項については、[コンテキスト管理](context.md)を参照してください。