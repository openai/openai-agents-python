---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、2 種類の実行結果タイプのいずれかを受け取ります。

- [`RunResult`][agents.result.RunResult]（`Runner.run(...)` または `Runner.run_sync(...)` から）
- [`RunResultStreaming`][agents.result.RunResultStreaming]（`Runner.run_streamed(...)` から）

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、`final_output`、`new_items`、`last_agent`、`raw_responses`、`to_state()` などの共通の実行結果サーフェスを公開します。

`RunResultStreaming` は、[`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete]、[`cancel(...)`][agents.result.RunResultStreaming.cancel] など、ストリーミング固有の制御を追加します。

## 適切な実行結果サーフェスの選択

ほとんどのアプリケーションでは、いくつかの実行結果プロパティまたはヘルパーだけが必要です。

| 必要なもの | 使用するもの |
| --- | --- |
| ユーザーに表示する最終回答 | `final_output` |
| 完全なローカルトランスクリプトを含む、再生可能な次ターン入力リスト | `to_input_list()` |
| エージェント、ツール、ハンドオフ、承認メタデータを含む豊富な実行項目 | `new_items` |
| 通常、次のユーザーターンを処理すべきエージェント | `last_agent` |
| `previous_response_id` による OpenAI Responses API のチェーン | `last_response_id` |
| 保留中の承認と再開可能なスナップショット | `interruptions` と `to_state()` |
| 現在のネストされた `Agent.as_tool()` 呼び出しに関するメタデータ | `agent_tool_invocation` |
| raw モデル呼び出しまたはガードレール診断 | `raw_responses` とガードレール実行結果配列 |

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

- 最後のエージェントに `output_type` が定義されていなかった場合は `str`
- 最後のエージェントに出力タイプが定義されていた場合は `last_agent.output_type` 型のオブジェクト
- たとえば承認中断で一時停止したために、最終出力が生成される前に実行が停止した場合は `None`

!!! note

    `final_output` は `Any` として型付けされています。ハンドオフによってどのエージェントが実行を終了するかが変わる可能性があるため、SDK は取り得る出力タイプの完全な集合を静的に知ることはできません。

ストリーミングモードでは、ストリームの処理が完了するまで `final_output` は `None` のままです。イベントごとのフローについては [ストリーミング](streaming.md) を参照してください。

## 入力、次ターン履歴、新規項目

これらのサーフェスは異なる問いに答えます。

| プロパティまたはヘルパー | 含まれるもの | 最適な用途 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | この実行セグメントのベース入力です。ハンドオフ入力フィルターが履歴を書き換えた場合、実行が継続したフィルター済み入力が反映されます。 | この実行が実際に入力として使用した内容の監査 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 実行の入力項目ビューです。デフォルトの `mode="preserve_all"` は、`new_items` から変換された完全な履歴を保持します。`mode="normalized"` は、ハンドオフフィルタリングによってモデル履歴が書き換えられる場合、正規の継続入力を優先します。 | 手動のチャットループ、クライアント管理の会話状態、プレーン項目履歴の確認 |
| [`new_items`][agents.result.RunResultBase.new_items] | エージェント、ツール、ハンドオフ、承認メタデータを含む豊富な [`RunItem`][agents.items.RunItem] ラッパーです。 | ログ、UI、監査、デバッグ |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 実行内の各モデル呼び出しからの raw [`ModelResponse`][agents.items.ModelResponse] オブジェクトです。 | プロバイダーレベルの診断または raw レスポンスの確認 |

実際には、次のように使います。

- プレーンな入力項目ビューが必要な場合は `to_input_list()` を使用します。
- ハンドオフフィルタリングまたはネストされたハンドオフ履歴の書き換え後、次の `Runner.run(..., input=...)` 呼び出しのための正規のローカル入力が必要な場合は、`to_input_list(mode="normalized")` を使用します。
- SDK に履歴の読み込みと保存を任せたい場合は、[`session=...`](sessions/index.md) を使用します。
- `conversation_id` または `previous_response_id` で OpenAI サーバー管理状態を使用している場合は、通常、`to_input_list()` を再送するのではなく、新しいユーザー入力のみを渡して保存済み ID を再利用します。
- ログ、UI、監査のために変換済みの完全な履歴が必要な場合は、デフォルトの `to_input_list()` モードまたは `new_items` を使用します。

JavaScript SDK とは異なり、Python ではモデル形状の差分のみを表す個別の `output` プロパティは公開されません。SDK メタデータが必要な場合は `new_items` を使用し、raw モデルペイロードが必要な場合は `raw_responses` を確認してください。

コンピュータツールの再生は、raw Responses ペイロードの形状に従います。プレビューモデルの `computer_call` 項目は単一の `action` を保持しますが、`gpt-5.5` のコンピュータ呼び出しはバッチ化された `actions[]` を保持できます。[`to_input_list()`][agents.result.RunResultBase.to_input_list] と [`RunState`][agents.run_state.RunState] はモデルが生成した形状をそのまま保持するため、手動再生、一時停止/再開フロー、保存済みトランスクリプトは、プレビュー版と GA のコンピュータツール呼び出しの両方で引き続き機能します。ローカルの実行結果は、引き続き `new_items` 内の `computer_call_output` 項目として表示されます。

### 新規項目

[`new_items`][agents.result.RunResultBase.new_items] は、実行中に起きたことを最も豊富に確認できるビューを提供します。一般的な項目タイプは次のとおりです。

- アシスタントメッセージの [`MessageOutputItem`][agents.items.MessageOutputItem]
- 推論項目の [`ReasoningItem`][agents.items.ReasoningItem]
- Responses ツール検索リクエストと読み込まれたツール検索結果の [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] および [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
- ツール呼び出しとその実行結果の [`ToolCallItem`][agents.items.ToolCallItem] および [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
- 承認のために一時停止したツール呼び出しの [`ToolApprovalItem`][agents.items.ToolApprovalItem]
- ハンドオフリクエストと完了した転送の [`HandoffCallItem`][agents.items.HandoffCallItem] および [`HandoffOutputItem`][agents.items.HandoffOutputItem]

エージェントの関連付け、ツール出力、ハンドオフ境界、または承認境界が必要な場合は、常に `to_input_list()` よりも `new_items` を選択してください。

ホスト型ツール検索を使用する場合は、`ToolSearchCallItem.raw_item` を確認してモデルが発行した検索リクエストを確認し、`ToolSearchOutputItem.raw_item` を確認してそのターンで読み込まれた名前空間、関数、またはホスト型 MCP サーバーを確認してください。

## 会話の継続または再開

### 次ターンのエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] には、最後に実行されたエージェントが含まれます。これは多くの場合、ハンドオフ後の次のユーザーターンで再利用するのに最適なエージェントです。

ストリーミングモードでは、実行の進行に応じて [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] が更新されるため、ストリームが完了する前にハンドオフを観察できます。

### 中断と実行状態

ツールに承認が必要な場合、保留中の承認は [`RunResult.interruptions`][agents.result.RunResult.interruptions] または [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。これには、直接ツールによって発生した承認、ハンドオフ後に到達したツールによって発生した承認、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行によって発生した承認が含まれる場合があります。

[`to_state()`][agents.result.RunResult.to_state] を呼び出して、再開可能な [`RunState`][agents.run_state.RunState] を取得し、保留中の項目を承認または却下してから、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

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

ストリーミング実行では、まず [`stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を完了し、その後 `result.interruptions` を確認して `result.to_state()` から再開します。完全な承認フローについては、[Human-in-the-loop](human_in_the_loop.md) を参照してください。

### サーバー管理の継続

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、実行から得られた最新のモデルレスポンス ID です。OpenAI Responses API チェーンを継続したい場合は、次のターンで `previous_response_id` として渡します。

すでに `to_input_list()`、`session`、または `conversation_id` で会話を継続している場合、通常 `last_response_id` は必要ありません。複数ステップの実行からすべてのモデルレスポンスが必要な場合は、代わりに `raw_responses` を確認してください。

## Agent-as-tool メタデータ

ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行から実行結果が返される場合、[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] は外側のツール呼び出しに関する不変のメタデータを公開します。

- `tool_name`
- `tool_call_id`
- `tool_arguments`

通常のトップレベル実行では、`agent_tool_invocation` は `None` です。

これは、ネストされた実行結果を後処理する際に外側のツール名、呼び出し ID、または raw 引数が必要になることがある `custom_output_extractor` 内で特に有用です。周辺の `Agent.as_tool()` パターンについては、[ツール](tools.md) を参照してください。

そのネストされた実行の解析済み structured input も必要な場合は、`context_wrapper.tool_input` を読み取ります。これは [`RunState`][agents.run_state.RunState] がネストされたツール入力として汎用的にシリアライズするフィールドであり、`agent_tool_invocation` は現在のネストされた呼び出しに対するライブ実行結果アクセサーです。

## ストリーミングのライフサイクルと診断

[`RunResultStreaming`][agents.result.RunResultStreaming] は上記と同じ実行結果サーフェスを継承しますが、ストリーミング固有の制御を追加します。

- セマンティックなストリームイベントを消費する [`stream_events()`][agents.result.RunResultStreaming.stream_events]
- 実行中のアクティブなエージェントを追跡する [`current_agent`][agents.result.RunResultStreaming.current_agent]
- ストリーミング実行が完全に終了したかどうかを確認する [`is_complete`][agents.result.RunResultStreaming.is_complete]
- 現在のターンの直後または即座に実行を停止する [`cancel(...)`][agents.result.RunResultStreaming.cancel]

非同期イテレーターが終了するまで `stream_events()` を消費し続けてください。ストリーミング実行は、そのイテレーターが終了するまで完了していません。また、`final_output`、`interruptions`、`raw_responses`、セッション永続化の副作用などのサマリープロパティは、最後に見えるトークンが到着した後もまだ確定中の場合があります。

`cancel()` を呼び出した場合は、キャンセルとクリーンアップが正しく完了できるように、`stream_events()` の消費を続けてください。

Python では、ストリーミングされた個別の `completed` promise や `error` プロパティは公開されません。終端的なストリーミング失敗は `stream_events()` から例外を送出することで表面化し、`is_complete` は実行が終端状態に到達したかどうかを反映します。

### Raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] には、実行中に収集された raw モデルレスポンスが含まれます。複数ステップの実行では、たとえばハンドオフや、モデル/ツール/モデルのサイクルの繰り返しをまたいで、複数のレスポンスが生成される場合があります。

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、`raw_responses` の最後のエントリーからの ID にすぎません。

### ガードレール実行結果

エージェントレベルのガードレールは、[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] として公開されます。

ツールのガードレールは、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] および [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として別途公開されます。

これらの配列は実行全体を通じて蓄積されるため、判断のログ記録、追加のガードレールメタデータの保存、または実行がブロックされた理由のデバッグに役立ちます。

### コンテキストと使用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] は、承認、使用量、ネストされた `tool_input` など、SDK 管理のランタイムメタデータとともにアプリのコンテキストを公開します。

使用量は `context_wrapper.usage` で追跡されます。ストリーミング実行では、ストリームの最後のチャンクが処理されるまで使用量の合計が遅れることがあります。完全なラッパー形状と永続化に関する注意事項については、[コンテキスト管理](context.md) を参照してください。