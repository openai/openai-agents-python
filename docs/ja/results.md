---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次の 2 種類の実行結果のいずれかを受け取ります。

-   `Runner.run(...)` または `Runner.run_sync(...)` からの [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)` からの [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、`final_output`、`new_items`、`last_agent`、`raw_responses`、`to_state()` など、共通の実行結果サーフェスを公開します。

`RunResultStreaming` には、[`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete]、[`cancel(...)`][agents.result.RunResultStreaming.cancel] など、ストリーミング固有の制御が追加されています。

## 適切な実行結果サーフェスの選択 {#choose-the-right-result-surface}

ほとんどのアプリケーションで必要になる実行結果のプロパティやヘルパーは、ごく一部です。

| 必要なもの | 使用するもの |
| --- | --- |
| ユーザーに表示する最終回答 | `final_output` |
| ローカルの完全な会話記録を含む、再実行可能な次ターンの入力リスト | `to_input_list()` |
| エージェント、ツール、ハンドオフ、承認のメタデータを含む詳細な実行項目 | `new_items` |
| 通常、次のユーザーターンを処理すべきエージェント | `last_agent` |
| `previous_response_id` を使用した OpenAI Responses API のチェーン | `last_response_id` |
| 保留中の承認と再開可能なスナップショット | `interruptions` と `to_state()` |
| 現在のネストされた `Agent.as_tool()` 呼び出しに関するメタデータ | `agent_tool_invocation` |
| raw モデル呼び出しまたはガードレールの診断情報 | `raw_responses` とガードレールの実行結果配列 |

## 最終出力 {#final-output}

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていなかった場合は、`str`
-   最後のエージェントに出力型が定義されていた場合は、`last_agent.output_type` 型のオブジェクト
-   承認による中断で一時停止した場合など、最終出力が生成される前に実行が停止した場合は、`None`

!!! note

    `final_output` の型は `Any` です。ハンドオフによって実行を完了するエージェントが変わる可能性があるため、SDK は考えられるすべての出力型を静的に把握できません。

ストリーミングモードでは、ストリームの処理が完了するまで `final_output` は `None` のままです。イベントごとのフローについては、[ストリーミング](streaming.md)を参照してください。

## 入力、次ターンの履歴、新規項目 {#input-next-turn-history-and-new-items}

これらのサーフェスは、それぞれ異なる問いに対応します。

| プロパティまたはヘルパー | 含まれるもの | 最適な用途 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | この実行セグメントの基本入力。ハンドオフ入力フィルターによって履歴が書き換えられた場合は、実行の続行に使用されたフィルター適用後の入力が反映されます。 | この実行で実際に入力として使用された内容の監査 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 実行を入力項目として表したビュー。デフォルトの `mode="preserve_all"` では、`new_items` から変換された履歴が維持されます。ただし、SDK のデフォルトのネストされたハンドオフ履歴へすでに移されたセッション項目の同一の出現は、再度追加されません。`mode="normalized"` では、ハンドオフフィルタリングによってモデル履歴が書き換えられた場合、正規の続行用入力が優先されます。 | 手動のチャットループ、クライアント管理の会話状態、プレーンな項目による履歴の確認 |
| [`new_items`][agents.result.RunResultBase.new_items] | エージェント、ツール、ハンドオフ、承認のメタデータを含む詳細な [`RunItem`][agents.items.RunItem] ラッパー。 | ログ、UI、監査、デバッグ |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 実行内の各モデル呼び出しから得られた raw [`ModelResponse`][agents.items.ModelResponse] オブジェクト。 | プロバイダーレベルの診断または raw レスポンスの確認 |

実際には、次のように使い分けます。

-   実行をプレーンな入力項目として確認する場合は、`to_input_list()` を使用します。
-   ハンドオフフィルタリングまたはネストされたハンドオフ履歴の書き換え後、次の `Runner.run(..., input=...)` 呼び出しに使用する正規のローカル入力が必要な場合は、`to_input_list(mode="normalized")` を使用します。
-   SDK に履歴の読み込みと保存を任せる場合は、[`session=...`](sessions/index.md) を使用します。
-   `conversation_id` または `previous_response_id` を使用して OpenAI のサーバー管理状態を利用している場合、通常は `to_input_list()` を再送信せず、新しいユーザー入力のみを渡して、保存されている ID を再利用します。
-   ログ、UI、監査のために変換済みの完全な履歴が必要な場合は、デフォルトの `to_input_list()` モードまたは `new_items` を使用します。

SDK のデフォルトのネストされたハンドオフ履歴がメッセージ項目をそのまま保持する場合、Sessions、`RunState`、`to_input_list()` は内容に基づいて重複を排除するのではなく、所有している同一の出現を追跡します。同じ内容のメッセージが別々に発生した場合は別々に保持され、すでに所有されている出現のみが再度追加されないようになります。

JavaScript SDK とは異なり、Python には実行中に新たに生成されたモデル形式の項目だけを含む独立した `output` プロパティはありません。SDK のメタデータが必要な場合は `new_items` を使用し、raw モデルペイロードが必要な場合は `raw_responses` を確認してください。

コンピュータツール項目を会話入力として再送信する場合は、raw Responses ペイロード形式が使用されます。プレビューモデルの `computer_call` 項目では単一の `action` が保持されますが、`gpt-5.5` のコンピュータ呼び出しでは、バッチ化された `actions[]` を保持できます。[`to_input_list()`][agents.result.RunResultBase.to_input_list] と [`RunState`][agents.run_state.RunState] はモデルが生成した形式をそのまま保持するため、これらの項目を会話入力として手動で再送信する処理、一時停止と再開のフロー、保存された会話記録は、プレビュー版と GA 版の両方のコンピュータツール呼び出しで引き続き機能します。ローカルの実行結果は、引き続き `new_items` 内に `computer_call_output` 項目として表示されます。

### 新規項目 {#new-items}

[`new_items`][agents.result.RunResultBase.new_items] では、実行中に発生した内容を最も詳細に確認できます。一般的な項目の型は次のとおりです。

-   再開後のモデル呼び出しの直前に `RunState.pending_input` から受け入れられた入力を表す [`InputItem`][agents.items.InputItem]
-   アシスタントメッセージを表す [`MessageOutputItem`][agents.items.MessageOutputItem]
-   推論項目を表す [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses のツール検索リクエストと読み込まれたツール検索結果を表す [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] と [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   ツール呼び出しとその実行結果を表す [`ToolCallItem`][agents.items.ToolCallItem] と [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   承認待ちで一時停止したツール呼び出しを表す [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   ホスト型 MCP の承認とツールカタログを表す [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem]、[`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem]、[`MCPListToolsItem`][agents.items.MCPListToolsItem]
-   ハンドオフリクエストと完了した転送を表す [`HandoffCallItem`][agents.items.HandoffCallItem] と [`HandoffOutputItem`][agents.items.HandoffOutputItem]

エージェントとの関連付け、ツール出力、ハンドオフの境界、承認の境界が必要な場合は、`to_input_list()` ではなく `new_items` を選択してください。

ホスト型ツール検索を使用する場合は、モデルが発行した検索リクエストを確認するには `ToolSearchCallItem.raw_item` を、該当ターンで読み込まれた名前空間、関数、ホスト型 MCP サーバーを確認するには `ToolSearchOutputItem.raw_item` を調べます。

Programmatic Tool Calling では、生成された `program` は `ToolCallItem` になり、そのプログラムが所有する通常の子ツール呼び出しも `ToolCallItem` のエントリになり、対応する `program_output` は `ToolCallOutputItem` になります。プログラムが所有するホスト型 MCP の `mcp_approval_request` 項目と `mcp_list_tools` 項目は例外であり、それぞれ `MCPApprovalRequestItem` エントリと `MCPListToolsItem` エントリになります。

raw 項目は、型付きの Responses オブジェクトまたはマッピングの場合があります。特に、プログラムが所有するシェル呼び出しとパッチ適用呼び出しではマッピングが使用されます。マッピングに対して安全な確認パターンを使用してください。

```python
from collections.abc import Mapping


def raw_field(item, name):
    raw_item = item.raw_item
    if isinstance(raw_item, Mapping):
        return raw_item.get(name)
    return getattr(raw_item, name, None)


raw_type = raw_field(item, "type")
caller = raw_field(item, "caller")
caller_id = (
    caller.get("caller_id")
    if isinstance(caller, Mapping)
    else getattr(caller, "caller_id", None)
)
```

プログラムが所有する子呼び出しでは、`caller` の `type` フィールドは `program` であり、`caller_id` によって親プログラム呼び出しが識別されます。

## 会話の続行または再開 {#continue-or-resume-the-conversation}

### 次ターンのエージェント {#next-turn-agent}

[`last_agent`][agents.result.RunResultBase.last_agent] には、最後に実行されたエージェントが含まれます。多くの場合、ハンドオフ後の次のユーザーターンで再利用するには、このエージェントが最適です。

ストリーミングモードでは、実行の進行に伴って [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] が更新されるため、ストリームが完了する前にハンドオフを確認できます。

### 中断と実行状態 {#interruptions-and-run-state}

ツールに承認が必要な場合、保留中の承認は [`RunResult.interruptions`][agents.result.RunResult.interruptions] または [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。これには、直接のツール、ハンドオフ後に到達したツール、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行によって発生した承認が含まれる場合があります。

再開可能な [`RunState`][agents.run_state.RunState] を取得するには、[`to_state()`][agents.result.RunResult.to_state] を呼び出します。次に、保留中の項目を承認または拒否し、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

[`ToolCallOutputItem`][agents.items.ToolCallOutputItem] の出力が Pydantic モデルまたはデータクラスの場合、`RunState` はその出力を構造化データとしてシリアライズします。`RunState` は辞書、リスト、タプルも走査し、それらのコンテナ内で検出した Pydantic モデルまたはデータクラスを変換します。タプルは JSON のラウンドトリップ後にリストとして復元されます。JSON と互換性のないその他の値は、文字列表現にフォールバックする場合があります。そのため、カスタム型を正確にシリアライズ後も維持する必要がある場合は、JSON と明示的に互換性のあるデータを返してください。

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

#### 再開前の入力追加 {#add-input-before-resuming}

実行が一時停止した後、または完了済みのターンの後で停止した後に新しいユーザー入力が到着し、未完了の実行が次のモデル呼び出しに到達する前である場合は、[`RunState.add_input()`][agents.run_state.RunState.add_input] を使用します。文字列はユーザーメッセージになり、複数回呼び出した場合は挿入順が維持されます。ステージングされた入力はシリアライズ済みの `RunState` に含まれるため、`to_json()` / `from_json()` および `to_string()` / `from_string()` のラウンドトリップ後も維持されます。

```python
state = result.to_state()
state.add_input("Also keep the generated report in the project folder.")

for interruption in state.get_interruptions():
    state.approve(interruption)

result = await Runner.run(agent, state)
```

再開時、ランナーは現在のエージェントの入力ガードレールと [`RunConfig`][agents.run.RunConfig] の入力ガードレールの両方を、ステージングされた入力にのみ適用します。クライアント管理の [`Session`][agents.memory.session.Session] が設定されている場合、ランナーは受け入れられたステージング入力を永続的な [`InputItem`][agents.items.InputItem] に変換し、モデルリクエストを発行する前にセッションへの書き込みを待機します。クライアント管理セッションまたはサーバー管理の会話がない場合、ランナーはモデルリクエストを発行する前に、受け入れられたステージング入力を `InputItem` に変換します。サーバー管理の会話では、サーバーリクエストが受け入れるまで入力は保留状態のままです。シリアライズ、再開、再実行に対して安全なリトライを通じて、SDK は永続的な `InputItem` の出現を 1 つ保持します。この SDK による出現回数の保証は、プロバイダーへの配信を保証するものではありません。リクエストがプロバイダーに到達した可能性がある状態でリトライポリシーが `RetryDecision(approve_unsafe_replay=True)` を返した場合、ランナーはステージングされた入力を再送信する可能性があり、プロバイダー側の処理が繰り返されることがあります。正常に受け入れられた入力は、`new_items` に `InputItem` として表示されます。切り離されたコピーを取得するには [`RunState.pending_input`][agents.run_state.RunState.pending_input] を読み取り、再開前にステージングされた入力をすべて破棄するには [`RunState.clear_pending_input()`][agents.run_state.RunState.clear_pending_input] を呼び出します。

`RunState.add_input()` は、終端状態、モデルの残りターンがない状態、受け入れ済みのモデルレスポンスがローカル処理を待っている状態、または保留中のツールの実行結果によって次のモデル呼び出し前に実行が終了する可能性がある中断状態を拒否します。このような場合は、現在の実行を完了してから、新しいユーザーターンを開始してください。

ストリーミング実行では、まず [`stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を完了してから、`result.interruptions` を確認し、`result.to_state()` から再開します。承認フロー全体については、[Human-in-the-loop](human_in_the_loop.md)を参照してください。

### サーバー管理による続行 {#server-managed-continuation}

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、実行から得られた最新のモデルレスポンス ID です。OpenAI Responses API のチェーンを続行する場合は、次のターンでこれを `previous_response_id` として渡します。

すでに `to_input_list()`、`session`、`conversation_id` を使用して会話を続行している場合、通常は `last_response_id` は必要ありません。複数ステップの実行からすべてのモデルレスポンスが必要な場合は、代わりに `raw_responses` を確認してください。

## エージェントをツールとして使用する場合のメタデータ {#agent-as-tool-metadata}

実行結果がネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行から得られた場合、[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] は、それを包含する `Agent.as_tool()` 呼び出しに関するイミュータブルなメタデータを公開します。

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

通常のトップレベルの実行では、`agent_tool_invocation` は `None` です。

これは特に `custom_output_extractor` 内で役立ちます。ネストされた実行を後処理する際に、それを包含する `Agent.as_tool()` 呼び出しのツール名、呼び出し ID、raw 引数が必要になる場合があるためです。関連する `Agent.as_tool()` のパターンについては、[ツール](tools.md)を参照してください。

そのネストされた実行について、パース済みの構造化入力も必要な場合は、`context_wrapper.tool_input` を読み取ります。これは、[`RunState`][agents.run_state.RunState] がネストされたツール入力用に汎用的にシリアライズするフィールドです。一方、`agent_tool_invocation` は現在のネストされた呼び出しのメタデータを実行結果上で直接公開します。

## ストリーミングのライフサイクルと診断 {#streaming-lifecycle-and-diagnostics}

[`RunResultStreaming`][agents.result.RunResultStreaming] は上記と同じ実行結果サーフェスを継承し、さらにストリーミング固有の制御を追加します。

-   セマンティックなストリームイベントを消費するための [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   実行中にアクティブなエージェントを追跡するための [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   ストリーミングされた実行が完全に終了したかどうかを確認するための [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   実行を即時または現在のターンの後で停止するための [`cancel(...)`][agents.result.RunResultStreaming.cancel]

非同期イテレーターが完了するまで、`stream_events()` の消費を続けてください。そのイテレーターが終了するまで、ストリーミング実行は完了していません。また、最後の可視トークンが到着した後も、`final_output`、`interruptions`、`raw_responses` などの概要プロパティや、セッション永続化の副作用に関する処理が続いている場合があります。

`cancel()` を呼び出した場合は、キャンセルとクリーンアップが正しく完了するように、`stream_events()` の消費を続けてください。

Python には、ストリーミング用の独立した `completed` Promise または `error` プロパティはありません。実行を終了させるストリーミングエラーは `stream_events()` によって送出され、`is_complete` は実行が終端状態に到達したかどうかを示します。

### raw レスポンス {#raw-responses}

[`raw_responses`][agents.result.RunResultBase.raw_responses] には、実行中に収集された raw モデルレスポンスが含まれます。複数ステップの実行では、ハンドオフや繰り返されるモデル／ツール／モデルのサイクルなどにより、複数のレスポンスが生成される場合があります。

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、`raw_responses` の最後のエントリに含まれる ID にすぎません。

各 [`ModelResponse`][agents.items.ModelResponse] は、個々のモデル呼び出しに適用される次の 2 つの診断情報も公開します。

-   [`request_id`][agents.items.ModelResponse.request_id] は、モデルアダプターとトランスポートが ID を伝播する場合のトランスポートリクエスト ID です。組み込みの `OpenAIResponsesModel` と `OpenAIChatCompletionsModel` は、HTTP および SSE のトランスポートパスで、利用可能なサーバー生成の `x-request-id` を伝播します。設定されたエンドポイントが OpenAI API の場合は、本番環境で `None` ではない値をログに記録し、障害を OpenAI サポートに関連付けられるようにしてください。OpenAI 互換のプロバイダーまたはプロキシの場合は、代わりにそのサービスのサポート窓口を利用してください。`OpenAIResponsesWSModel` は現在、`request_id` を `None` のままにします。サードパーティー製アダプターでは、リクエスト ID の伝播は保証されません。AnyLLM Chat Completions アダプターと `LitellmModel` は現在、`request_id` を `None` のままにします。Agents SDK の AnyLLM Responses アダプターでも、トランスポートリクエスト ID を保持せずにプロバイダーレスポンスを正規化した場合、`request_id` が `None` のままになることがあります。
-   [`raw_usage`][agents.items.ModelResponse.raw_usage] は、Agents SDK がペイロードを正規化する前の、プロバイダーの使用量ペイロードを JSON 互換形式で保存したオプトインのスナップショットです。`ModelSettings(preserve_raw_usage=True)` を使用して `raw_usage` を有効にしてください。[プロバイダーの使用量ペイロードの保持](usage.md#preserving-provider-usage-payloads)を参照してください。

`ModelResponse.request_id` と `ModelResponse.raw_usage` はそれぞれ `None` になる可能性があるため、これらの値は会話状態ではなく、オプションの診断情報として扱ってください。

### ガードレールの実行結果 {#guardrail-results}

エージェントレベルのガードレールは、[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] として公開されます。

ツールのガードレールは、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として個別に公開されます。

これらの配列には実行全体の実行結果が蓄積されるため、判断内容のログ記録、追加のガードレールメタデータの保存、実行がブロックされた理由のデバッグに役立ちます。

エージェントレベルの出力ガードレールが、終端となる関数ツールによって直接生成された最終出力をブロックした場合、1 つの秘匿化ルールが適用されます。ブロックされた現在のレスポンスでは、`output_guardrail_results` が拒否されたエージェント出力を置き換え、ペイロードを含む出力メタデータをクリアします。また、`tool_output_guardrail_results` がペイロードを含むツールメタデータを置き換えます。それ以前に受け入れられた実行結果は変更されません。サニタイズされた出力ガードレールの実行結果は、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] の `guardrail_result` として公開されます。サニタイズされた出力ガードレールとツール出力ガードレールの実行結果は、ストリーミングされた実行結果の状態と `RunState` からも公開されます。[出力ガードレール](guardrails.md#output-guardrails)を参照してください。

### コンテキストと使用量 {#context-and-usage}

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] は、アプリのコンテキストに加えて、承認、使用量、ネストされた `tool_input` など、SDK が管理するランタイムメタデータを公開します。

使用量は `context_wrapper.usage` で追跡されます。ストリーミング実行では、ストリームの最後のチャンクが処理されるまで、使用量の合計への反映が遅れる場合があります。ラッパーの完全な形式と永続化に関する注意事項については、[コンテキスト管理](context.md)を参照してください。