---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次の 2 種類の結果タイプのいずれかを受け取ります。

-   `Runner.run(...)` または `Runner.run_sync(...)` からの [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)` からの [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、`final_output`、`new_items`、`last_agent`、`raw_responses`、`to_state()` などの共通の結果サーフェスを公開します。

`RunResultStreaming` には、[`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete]、[`cancel(...)`][agents.result.RunResultStreaming.cancel] などのストリーミング固有の制御が追加されています。

## 適切な結果サーフェスの選択

ほとんどのアプリケーションで必要なのは、いくつかの結果プロパティまたはヘルパーだけです。

| 必要なもの | 使用先 |
| --- | --- |
| ユーザーに表示する最終回答 | `final_output` |
| ローカルの完全なトランスクリプトを含む、再生可能な次ターン入力リスト | `to_input_list()` |
| エージェント、ツール、ハンドオフ、承認メタデータを含むリッチな実行アイテム | `new_items` |
| 通常、次のユーザーターンを処理すべきエージェント | `last_agent` |
| `previous_response_id` を用いた OpenAI Responses API チェーン | `last_response_id` |
| 保留中の承認と再開可能なスナップショット | `interruptions` と `to_state()` |
| 現在のネストされた `Agent.as_tool()` 呼び出しに関するメタデータ | `agent_tool_invocation` |
| 生のモデル呼び出しまたはガードレール診断 | `raw_responses` とガードレール結果配列 |

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていない場合は `str`
-   最後のエージェントに出力型が定義されている場合は `last_agent.output_type` 型のオブジェクト
-   承認による割り込みで一時停止した場合など、最終出力が生成される前に実行が停止した場合は `None`

!!! note

    `final_output` は `Any` 型です。ハンドオフにより実行を完了するエージェントが変わる可能性があるため、SDK は取り得る出力型の完全な集合を静的に把握できません。

ストリーミングモードでは、ストリームの処理が完了するまで `final_output` は `None` のままです。イベントごとの流れは [Streaming](streaming.md) を参照してください。

## 入力、次ターン履歴、new items

これらのサーフェスは、それぞれ異なる問いに答えます。

| プロパティまたはヘルパー | 含まれる内容 | 最適な用途 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | この実行セグメントのベース入力。ハンドオフ入力フィルターが履歴を書き換えた場合、実行が継続したフィルター後の入力が反映されます。 | この実行が実際に入力として何を使ったかの監査 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 実行の入力アイテムビュー。既定の `mode="preserve_all"` は `new_items` から変換された完全な履歴を保持し、`mode="normalized"` はハンドオフフィルタリングでモデル履歴が書き換えられた際に正規の継続入力を優先します。 | 手動チャットループ、クライアント管理の会話状態、プレーンアイテム履歴の確認 |
| [`new_items`][agents.result.RunResultBase.new_items] | エージェント、ツール、ハンドオフ、承認メタデータを持つリッチな [`RunItem`][agents.items.RunItem] ラッパー。 | ログ、UI、監査、デバッグ |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 実行内の各モデル呼び出しから得られる生の [`ModelResponse`][agents.items.ModelResponse] オブジェクト。 | プロバイダーレベルの診断や生レスポンスの確認 |

実運用では次のとおりです。

-   実行のプレーンな入力アイテムビューが必要な場合は `to_input_list()` を使います。
-   ハンドオフフィルタリングやネストされたハンドオフ履歴書き換え後、次の `Runner.run(..., input=...)` 呼び出し向けの正規ローカル入力が必要な場合は `to_input_list(mode="normalized")` を使います。
-   SDK に履歴の読み書きを任せたい場合は [`session=...`](sessions/index.md) を使います。
-   `conversation_id` や `previous_response_id` による OpenAI のサーバー管理状態を使っている場合、通常は `to_input_list()` を再送せず、新しいユーザー入力のみを渡して保存済み ID を再利用します。
-   ログ、UI、監査のために完全な変換済み履歴が必要な場合は、既定の `to_input_list()` モードまたは `new_items` を使います。

JavaScript SDK と異なり、Python はモデル形状の差分のみを表す独立した `output` プロパティを公開しません。SDK メタデータが必要なら `new_items` を使い、生のモデルペイロードが必要なら `raw_responses` を確認してください。

コンピュータツールのリプレイは、生の Responses ペイロード形状に従います。プレビュー版モデルの `computer_call` アイテムは単一の `action` を保持し、`gpt-5.4` のコンピュータ呼び出しはバッチ化された `actions[]` を保持できます。[`to_input_list()`][agents.result.RunResultBase.to_input_list] と [`RunState`][agents.run_state.RunState] は、モデルが生成した形状をそのまま保持するため、手動リプレイ、一時停止/再開フロー、保存済みトランスクリプトはプレビュー版と GA の両方のコンピュータツール呼び出しで継続して機能します。ローカルの実行結果は引き続き `new_items` 内で `computer_call_output` アイテムとして現れます。

### New items

[`new_items`][agents.result.RunResultBase.new_items] は、実行中に何が起きたかを最もリッチに把握できるビューです。一般的なアイテムタイプは次のとおりです。

-   アシスタントメッセージ用の [`MessageOutputItem`][agents.items.MessageOutputItem]
-   推論アイテム用の [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses ツール検索リクエストおよび読み込まれたツール検索結果用の [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] と [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   ツール呼び出しとその結果用の [`ToolCallItem`][agents.items.ToolCallItem] と [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   承認待ちで一時停止したツール呼び出し用の [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   ハンドオフ要求と完了した転送用の [`HandoffCallItem`][agents.items.HandoffCallItem] と [`HandoffOutputItem`][agents.items.HandoffOutputItem]

エージェントとの関連付け、ツール出力、ハンドオフ境界、承認境界が必要な場合は、`to_input_list()` より `new_items` を選んでください。

ホストされたツール検索を使う場合、モデルが出力した検索リクエストは `ToolSearchCallItem.raw_item` を、当該ターンでどの名前空間・関数・ホストされた MCP サーバーが読み込まれたかは `ToolSearchOutputItem.raw_item` を確認してください。

## 会話の継続または再開

### 次ターンのエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] には、最後に実行されたエージェントが含まれます。これはハンドオフ後の次のユーザーターンで再利用するエージェントとして最適なことがよくあります。

ストリーミングモードでは、[`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] は実行進行に応じて更新されるため、ストリーム完了前にハンドオフを観察できます。

### 割り込みと実行状態

ツールに承認が必要な場合、保留中の承認は [`RunResult.interruptions`][agents.result.RunResult.interruptions] または [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。これには、直接ツールで発生した承認、ハンドオフ後に到達したツールで発生した承認、ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行で発生した承認が含まれる場合があります。

[`to_state()`][agents.result.RunResult.to_state] を呼び出して再開可能な [`RunState`][agents.run_state.RunState] を取得し、保留中アイテムを承認または拒否してから、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

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

ストリーミング実行では、まず [`stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を完了し、その後 `result.interruptions` を確認して `result.to_state()` から再開してください。承認フロー全体は [Human-in-the-loop](human_in_the_loop.md) を参照してください。

### サーバー管理の継続

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、この実行における最新のモデルレスポンス ID です。OpenAI Responses API チェーンを継続したい場合は、次ターンでこれを `previous_response_id` として渡します。

すでに `to_input_list()`、`session`、または `conversation_id` で会話を継続している場合、通常は `last_response_id` は不要です。マルチステップ実行のすべてのモデルレスポンスが必要な場合は、代わりに `raw_responses` を確認してください。

## Agent-as-tool メタデータ

結果がネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行から来ている場合、[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] は外側ツール呼び出しの不変メタデータを公開します。

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

通常のトップレベル実行では、`agent_tool_invocation` は `None` です。

これは特に `custom_output_extractor` 内で有用で、ネスト結果を後処理する際に外側のツール名、呼び出し ID、または生の引数が必要になることがあります。周辺の `Agent.as_tool()` パターンは [Tools](tools.md) を参照してください。

そのネスト実行のパース済み structured outputs 入力も必要な場合は、`context_wrapper.tool_input` を読んでください。これは [`RunState`][agents.run_state.RunState] がネストツール入力向けに汎用的にシリアライズするフィールドであり、`agent_tool_invocation` は現在のネスト呼び出し向けのライブ結果アクセサです。

## ストリーミングライフサイクルと診断

[`RunResultStreaming`][agents.result.RunResultStreaming] は上記と同じ結果サーフェスを継承しますが、ストリーミング固有の制御を追加します。

-   セマンティックなストリームイベントを消費する [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   実行途中のアクティブエージェントを追跡する [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   ストリーミング実行が完全に終了したかを確認する [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   実行を即時または現在ターン後に停止する [`cancel(...)`][agents.result.RunResultStreaming.cancel]

非同期イテレーターが終了するまで `stream_events()` を消費し続けてください。ストリーミング実行はそのイテレーターが終わるまで完了しません。また、`final_output`、`interruptions`、`raw_responses`、セッション永続化の副作用などの要約プロパティは、最後に見えるトークン到着後も確定中である可能性があります。

`cancel()` を呼び出した場合も、キャンセルとクリーンアップを正しく完了させるために `stream_events()` の消費を続けてください。

Python は、ストリーミング専用の `completed` promise や `error` プロパティを別途公開しません。終端のストリーミング失敗は `stream_events()` からの例外送出として表面化し、`is_complete` は実行が終端状態に達したかどうかを反映します。

### Raw responses

[`raw_responses`][agents.result.RunResultBase.raw_responses] には、実行中に収集された生のモデルレスポンスが含まれます。マルチステップ実行では、たとえばハンドオフやモデル/ツール/モデルの反復サイクルをまたいで、複数のレスポンスが生成されることがあります。

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、`raw_responses` の最後のエントリの ID にすぎません。

### ガードレール結果

エージェントレベルのガードレールは [`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] として公開されます。

ツールのガードレールは、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として別途公開されます。

これらの配列は実行全体で蓄積されるため、判定のログ化、追加ガードレールメタデータの保存、実行がブロックされた理由のデバッグに有用です。

### コンテキストと使用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] は、承認、使用量、ネストされた `tool_input` などの SDK 管理ランタイムメタデータとともに、アプリコンテキストを公開します。

使用量は `context_wrapper.usage` で追跡されます。ストリーミング実行では、ストリーム最終チャンクの処理が終わるまで使用量合計が遅延する場合があります。ラッパーの完全な形状と永続化時の注意点は [Context management](context.md) を参照してください。