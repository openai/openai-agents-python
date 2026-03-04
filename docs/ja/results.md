---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次の 2 種類の結果型のいずれかを受け取ります。

-   `Runner.run(...)` または `Runner.run_sync(...)` からの [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)` からの [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、`final_output`、`new_items`、`last_agent`、`raw_responses`、`to_state()` などの共通の結果サーフェスを公開します。

`RunResultStreaming` は、[`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete]、[`cancel(...)`][agents.result.RunResultStreaming.cancel] などのストリーミング固有の制御を追加します。

## 適切な結果サーフェスの選択

ほとんどのアプリケーションでは、必要なのは一部の結果プロパティまたはヘルパーのみです。

| 必要なもの | 使用対象 |
| --- | --- |
| ユーザーに表示する最終回答 | `final_output` |
| 完全なローカル会話履歴を含む、再実行可能な次ターン入力リスト | `to_input_list()` |
| エージェント、ツール、ハンドオフ、承認メタデータを含むリッチな実行アイテム | `new_items` |
| 通常、次のユーザーターンを処理すべきエージェント | `last_agent` |
| `previous_response_id` を使った OpenAI Responses API チェーン | `last_response_id` |
| 保留中の承認と再開可能なスナップショット | `interruptions` と `to_state()` |
| 現在のネストした `Agent.as_tool()` 呼び出しに関するメタデータ | `agent_tool_invocation` |
| raw モデル呼び出しまたはガードレール診断 | `raw_responses` とガードレール結果配列 |

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   最後のエージェントに出力型が定義されている場合は `last_agent.output_type` 型のオブジェクト
-   最終出力が生成される前に実行が停止した場合（例: 承認割り込みで一時停止した場合）は `None`

!!! note

    `final_output` は `Any` として型付けされています。ハンドオフによって実行を完了するエージェントが変わる可能性があるため、 SDK は可能な出力型の全体集合を静的に把握できません。

ストリーミングモードでは、ストリームの処理が完了するまで `final_output` は `None` のままです。イベントごとのフローは [Streaming](streaming.md) を参照してください。

## 入力、次ターン履歴、new items

これらのサーフェスは、それぞれ異なる問いに答えます。

| プロパティまたはヘルパー | 含まれる内容 | 最適な用途 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | この実行セグメントのベース入力。ハンドオフ入力フィルターが履歴を書き換えた場合、実行が継続したフィルター済み入力を反映します。 | この実行が実際に入力として使用した内容の監査 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | `input` と、この実行の変換済み `new_items` から構築される、再実行可能な次ターン入力リスト。 | 手動チャットループとクライアント管理の会話状態 |
| [`new_items`][agents.result.RunResultBase.new_items] | エージェント、ツール、ハンドオフ、承認メタデータを持つリッチな [`RunItem`][agents.items.RunItem] ラッパー。 | ログ、 UI 、監査、デバッグ |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 実行内の各モデル呼び出しからの raw [`ModelResponse`][agents.items.ModelResponse] オブジェクト。 | プロバイダーレベルの診断または raw レスポンス検査 |

実際には次のとおりです。

-   アプリケーションが会話履歴全体を手動で保持する場合は `to_input_list()` を使用します。
-   SDK に履歴の読み書きを任せたい場合は [`session=...`](sessions/index.md) を使用します。
-   `conversation_id` または `previous_response_id` を使った OpenAI サーバー管理状態を使う場合は、通常 `to_input_list()` を再送信する代わりに新しいユーザー入力のみを渡し、保存済み ID を再利用します。

JavaScript SDK と異なり、 Python はモデル形状の差分のみを示す独立した `output` プロパティを公開しません。 SDK メタデータが必要な場合は `new_items` を使用し、モデルの raw ペイロードが必要な場合は `raw_responses` を確認してください。

### New items

[`new_items`][agents.result.RunResultBase.new_items] は、実行中に何が起きたかを最も豊富に把握できるビューです。一般的なアイテム型は次のとおりです。

-   アシスタントメッセージ用の [`MessageOutputItem`][agents.items.MessageOutputItem]
-   推論アイテム用の [`ReasoningItem`][agents.items.ReasoningItem]
-   ツール呼び出しとその結果用の [`ToolCallItem`][agents.items.ToolCallItem] と [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   承認待ちで一時停止したツール呼び出し用の [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   ハンドオフ要求と完了した転送用の [`HandoffCallItem`][agents.items.HandoffCallItem] と [`HandoffOutputItem`][agents.items.HandoffOutputItem]

エージェント関連付け、ツール出力、ハンドオフ境界、承認境界が必要な場合は、`to_input_list()` ではなく `new_items` を選択してください。

## 会話の継続または再開

### 次ターンのエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] には最後に実行されたエージェントが含まれます。これは、ハンドオフ後の次のユーザーターンで再利用するエージェントとして最適な場合が多いです。

ストリーミングモードでは、[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] が実行進行中に更新されるため、ストリーム完了前にハンドオフを観測できます。

### 割り込みと実行状態

ツールに承認が必要な場合、保留中の承認は [`RunResult.interruptions`][agents.result.RunResult.interruptions] または [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] に公開されます。これには、直接ツールによる承認、ハンドオフ後に到達したツールによる承認、またはネストした [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行による承認が含まれる場合があります。

[`to_state()`][agents.result.RunResult.to_state] を呼び出して再開可能な [`RunState`][agents.run_state.RunState] を取得し、保留中アイテムを承認または拒否したうえで、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

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

ストリーミング実行では、まず [`stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を完了し、その後 `result.interruptions` を確認して `result.to_state()` から再開してください。完全な承認フローは [Human-in-the-loop](human_in_the_loop.md) を参照してください。

### サーバー管理の継続

[`last_response_id`][agents.result.RunResultBase.last_response_id] は実行における最新のモデルレスポンス ID です。 OpenAI Responses API チェーンを継続したい場合は、次ターンでこれを `previous_response_id` として返します。

すでに `to_input_list()`、`session`、または `conversation_id` で会話を継続している場合、通常は `last_response_id` は不要です。複数ステップ実行のすべてのモデルレスポンスが必要な場合は、代わりに `raw_responses` を確認してください。

## Agent-as-tool メタデータ

結果がネストした [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行から来ている場合、[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] は外側のツール呼び出しに関する不変メタデータを公開します。

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

通常のトップレベル実行では、`agent_tool_invocation` は `None` です。

これは `custom_output_extractor` 内で特に有用で、ネストした結果の後処理中に外側ツール名、呼び出し ID、または raw 引数が必要になる場合があります。周辺の `Agent.as_tool()` パターンは [Tools](tools.md) を参照してください。

そのネスト実行のパース済み structured inputs も必要な場合は、`context_wrapper.tool_input` を参照してください。これは [`RunState`][agents.run_state.RunState] がネストしたツール入力用に汎用的にシリアライズするフィールドであり、`agent_tool_invocation` は現在のネスト呼び出しに対するライブ結果アクセサーです。

## ストリーミングライフサイクルと診断

[`RunResultStreaming`][agents.result.RunResultStreaming] は上記と同じ結果サーフェスを継承しますが、ストリーミング固有の制御を追加します。

-   セマンティックなストリームイベントを消費する [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   実行途中のアクティブなエージェントを追跡する [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   ストリーミング実行が完全に終了したかを確認する [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   実行を即時または現在のターン後に停止する [`cancel(...)`][agents.result.RunResultStreaming.cancel]

非同期イテレーターが終了するまで `stream_events()` の消費を続けてください。ストリーミング実行はそのイテレーターが終わるまで完了ではなく、`final_output`、`interruptions`、`raw_responses`、セッション永続化の副作用などの要約プロパティは、最後の可視トークン到着後もしばらく確定処理中の場合があります。

`cancel()` を呼び出した場合でも、キャンセルとクリーンアップを正しく完了させるために `stream_events()` の消費を継続してください。

Python は独立したストリーミング `completed` Promise や `error` プロパティを公開しません。終端時のストリーミング失敗は `stream_events()` からの例外送出として表面化し、`is_complete` は実行が終端状態に到達したかどうかを反映します。

### Raw responses

[`raw_responses`][agents.result.RunResultBase.raw_responses] には、実行中に収集された raw モデルレスポンスが含まれます。複数ステップ実行では、たとえばハンドオフやモデル/ツール/モデルの反復サイクルをまたいで、複数のレスポンスが生成されることがあります。

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、`raw_responses` の最後のエントリの ID にすぎません。

### ガードレール結果

エージェントレベルのガードレールは [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] として公開されます。

ツールガードレールは別途、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として公開されます。

これらの配列は実行全体で蓄積されるため、判断のログ記録、追加のガードレールメタデータ保存、実行がブロックされた理由のデバッグに有用です。

### コンテキストと使用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] は、アプリコンテキストと、承認、使用量、ネストした `tool_input` などの SDK 管理ランタイムメタデータをまとめて公開します。

使用量は `context_wrapper.usage` で追跡されます。ストリーミング実行では、ストリームの最終チャンクが処理されるまで使用量合計が遅れて反映されることがあります。ラッパーの完全な形状と永続化時の注意点は [Context management](context.md) を参照してください。