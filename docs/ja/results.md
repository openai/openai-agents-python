---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次の 2 種類の実行結果のいずれかを受け取ります。

-   `Runner.run(...)` または `Runner.run_sync(...)` から返される [`RunResult`][agents.result.RunResult]
-   `Runner.run_streamed(...)` から返される [`RunResultStreaming`][agents.result.RunResultStreaming]

どちらも [`RunResultBase`][agents.result.RunResultBase] を継承しており、`final_output`、`new_items`、`last_agent`、`raw_responses`、`to_state()` などの共通の実行結果インターフェースを公開します。

`RunResultStreaming` には、[`stream_events()`][agents.result.RunResultStreaming.stream_events]、[`current_agent`][agents.result.RunResultStreaming.current_agent]、[`is_complete`][agents.result.RunResultStreaming.is_complete]、[`cancel(...)`][agents.result.RunResultStreaming.cancel] など、ストリーミング固有の制御機能が追加されています。

## 適切な実行結果インターフェースの選択 {#choose-the-right-result-surface}

ほとんどのアプリケーションで必要になる実行結果のプロパティやヘルパーは、ごく一部です。

| 必要なもの | 使用するもの |
| --- | --- |
| ユーザーに表示する最終回答 | `final_output` |
| ローカルの完全なトランスクリプトを含む、再実行可能な次ターン用入力リスト | `to_input_list()` |
| エージェント、ツール、ハンドオフ、承認のメタデータを含む詳細な実行項目 | `new_items` |
| 通常、次のユーザーターンを処理すべきエージェント | `last_agent` |
| `previous_response_id` を使用した OpenAI Responses API のチェーン | `last_response_id` |
| 保留中の承認と再開可能なスナップショット | `interruptions` と `to_state()` |
| 現在のネストされた `Agent.as_tool()` 呼び出しに関するメタデータ | `agent_tool_invocation` |
| raw モデル呼び出しまたはガードレールの診断情報 | `raw_responses` とガードレール結果の配列 |

## 最終出力 {#final-output}

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていなかった場合は、`str`
-   最後のエージェントに出力型が定義されていた場合は、`last_agent.output_type` 型のオブジェクト
-   承認による中断で一時停止した場合など、最終出力が生成される前に実行が停止した場合は、`None`

!!! note

    `final_output` は `Any` として型付けされています。ハンドオフによって実行を完了するエージェントが変わる可能性があるため、SDK は可能性のある出力型の完全な集合を静的に把握できません。

ストリーミングモードでは、ストリームの処理が完了するまで `final_output` は `None` のままです。イベントごとのフローについては、[ストリーミング](streaming.md)を参照してください。

## 入力、次ターンの履歴、新規項目 {#input-next-turn-history-and-new-items}

以下のインターフェースは、それぞれ異なる目的に対応します。

| プロパティまたはヘルパー | 含まれる内容 | 最適な用途 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | この実行セグメントの基本入力。ハンドオフ入力フィルターが履歴を書き換えた場合、実行が継続時に使用したフィルター済み入力が反映されます。 | この実行が実際に入力として使用した内容の監査 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 実行を入力項目として表現したビュー。デフォルトの `mode="preserve_all"` は `new_items` から変換された履歴を保持しますが、SDK のデフォルトのネストされたハンドオフ履歴へすでに移動済みの Session 項目と完全に同一の出現箇所を、再度追加することはありません。`mode="normalized"` は、ハンドオフフィルタリングによってモデル履歴が書き換えられた場合に、正規の継続入力を優先します。 | 手動のチャットループ、クライアント管理の会話状態、通常の項目としての履歴の確認 |
| [`new_items`][agents.result.RunResultBase.new_items] | エージェント、ツール、ハンドオフ、承認のメタデータを含む詳細な [`RunItem`][agents.items.RunItem] ラッパー。 | ログ、UI、監査、デバッグ |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 実行中の各モデル呼び出しから得られた raw [`ModelResponse`][agents.items.ModelResponse] オブジェクト。 | プロバイダーレベルの診断または raw レスポンスの確認 |

実際には、次のように使い分けます。

-   実行を通常の入力項目として確認する場合は、`to_input_list()` を使用します。
-   ハンドオフフィルタリングまたはネストされたハンドオフ履歴の書き換え後に、次の `Runner.run(..., input=...)` 呼び出し用の正規のローカル入力が必要な場合は、`to_input_list(mode="normalized")` を使用します。
-   SDK に履歴の読み込みと保存を任せる場合は、[`session=...`](sessions/index.md) を使用します。
-   `conversation_id` または `previous_response_id` で OpenAI のサーバー管理状態を使用している場合、通常は `to_input_list()` を再送信するのではなく、新しいユーザー入力のみを渡して、保存済みの ID を再利用します。
-   ログ、UI、監査のために変換済みの完全な履歴が必要な場合は、デフォルトの `to_input_list()` モードまたは `new_items` を使用します。

SDK のデフォルトのネストされたハンドオフ履歴がメッセージ項目をそのまま保持する場合、Sessions、`RunState`、`to_input_list()` は、内容による重複排除ではなく、所有されている正確な出現箇所を追跡します。個別に発生した同一のメッセージは別々に保持されます。すでに所有されている出現箇所だけが、再度追加されないように処理されます。

モデル出力を再実行可能な入力へ変換する際、`to_input_list()`、[`ModelResponse.to_input_items()`][agents.items.ModelResponse.to_input_items]、各 [`RunItemBase.to_input_item()`][agents.items.RunItemBase.to_input_item] 呼び出しは、プロバイダーの出力専用 `created_by` メタデータを削除します。これには、ネストされた `shell_call_output` チャンク上の `created_by` も含まれます。この変換では、影響を受けるマッピングが再構築され、元の raw 項目は変更されません。

JavaScript SDK とは異なり、Python では、実行中に新たに生成されたモデル形式の項目のみを含む独立した `output` プロパティは公開されません。SDK メタデータが必要な場合は `new_items` を使用し、raw モデルペイロードが必要な場合は `raw_responses` を確認してください。

コンピューターツールの項目を会話入力として再送信する場合は、raw Responses ペイロード形式を使用します。プレビューモデルの `computer_call` 項目は単一の `action` を保持しますが、`gpt-5.5` コンピューター呼び出しでは、バッチ化された `actions[]` を保持できます。[`to_input_list()`][agents.result.RunResultBase.to_input_list] と [`RunState`][agents.run_state.RunState] は、モデルが生成した形式をそのまま保持します。そのため、これらの項目を手動で会話入力として再送信する処理、一時停止と再開のフロー、保存済みトランスクリプトは、プレビュー版と GA 版の両方のコンピューターツール呼び出しで引き続き動作します。ローカルの実行結果は、引き続き `new_items` 内の `computer_call_output` 項目として表示されます。

### 新規項目 {#new-items}

[`new_items`][agents.result.RunResultBase.new_items] を使用すると、実行中に起きたことを最も詳細に確認できます。一般的な項目の型は次のとおりです。

-   再開されたモデル呼び出しの直前に `RunState.pending_input` から受け入れられた入力を表す [`InputItem`][agents.items.InputItem]
-   アシスタントメッセージを表す [`MessageOutputItem`][agents.items.MessageOutputItem]
-   推論項目を表す [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses のツール検索リクエストと読み込まれたツール検索結果を表す [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] と [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   ツール呼び出しとその実行結果を表す [`ToolCallItem`][agents.items.ToolCallItem] と [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   承認のために一時停止したツール呼び出しを表す [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   ホストされた MCP の承認とツールカタログを表す [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem]、[`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem]、[`MCPListToolsItem`][agents.items.MCPListToolsItem]
-   ハンドオフリクエストと完了した移管を表す [`HandoffCallItem`][agents.items.HandoffCallItem] と [`HandoffOutputItem`][agents.items.HandoffOutputItem]

エージェントとの関連付け、ツール出力、ハンドオフの境界、承認の境界が必要な場合は、`to_input_list()` ではなく `new_items` を選択してください。

ホストされたツール検索を使用する場合、モデルが生成した検索リクエストを確認するには `ToolSearchCallItem.raw_item` を、該当ターンで読み込まれた名前空間、関数、ホストされた MCPサーバーを確認するには `ToolSearchOutputItem.raw_item` を調べます。

プログラムによるツール呼び出しでは、生成された `program` は `ToolCallItem` になり、そのプログラムが所有する通常の子ツール呼び出しも `ToolCallItem` エントリになります。また、対応する `program_output` は `ToolCallOutputItem` になります。プログラムが所有するホストされた MCP の `mcp_approval_request` 項目と `mcp_list_tools` 項目は例外であり、それぞれ `MCPApprovalRequestItem` エントリと `MCPListToolsItem` エントリになります。

raw 項目には、型付きの Responses オブジェクトまたはマッピングを使用できます。特に、プログラムが所有する shell 呼び出しと apply-patch 呼び出しではマッピングが使用されます。マッピングに対して安全な確認パターンを使用してください。

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
    caller.get("caller_id") if isinstance(caller, Mapping) else getattr(caller, "caller_id", None)
)
```

プログラムが所有する子呼び出しでは、`caller` の `type` フィールドは `program` となり、`caller_id` は親プログラム呼び出しを識別します。

## 会話の継続と再開 {#continue-or-resume-the-conversation}

### 次ターンのエージェント {#next-turn-agent}

[`last_agent`][agents.result.RunResultBase.last_agent] には、最後に実行されたエージェントが格納されます。多くの場合、ハンドオフ後の次のユーザーターンで再利用するエージェントとして最適です。

ストリーミングモードでは、実行の進行に伴って [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] が更新されるため、ストリームが完了する前にハンドオフを確認できます。

### 中断と実行状態 {#interruptions-and-run-state}

ツールに承認が必要な場合、保留中の承認は [`RunResult.interruptions`][agents.result.RunResult.interruptions] または [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。これには、直接使用されたツール、ハンドオフ後に到達したツール、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行によって要求された承認が含まれる場合があります。

再開可能な [`RunState`][agents.run_state.RunState] を取得するには [`to_state()`][agents.result.RunResult.to_state] を呼び出し、保留中の項目を承認または拒否してから、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

[`ToolCallOutputItem`][agents.items.ToolCallOutputItem] の出力が Pydantic モデルまたは dataclass の場合、`RunState` はその出力を構造化データとしてシリアライズします。`RunState` は辞書、リスト、タプルも走査し、それらのコンテナー内で見つかった Pydantic モデルまたは dataclass を変換します。JSON のラウンドトリップ後、タプルはリストとして復元されます。JSON と互換性のないその他の値は文字列表現にフォールバックする場合があるため、カスタム型を正確に保持する必要がある場合は、明示的に JSON 互換のデータを返してください。

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

#### 再開した Session の書き込み失敗からの復旧 {#recover-a-failed-resumed-session-write}

再開された実行では、承認済みのツール処理が完了しても、同じモデルレスポンス内のハンドオフを含め、さらに別のモデル呼び出しが発生する場合があります。その後、完了したツール呼び出しと出力をクライアント管理の [`Session`][agents.memory.session.Session] に書き込む際に失敗することがあります。同じ [`RunState`][agents.run_state.RunState] を保持するか、それをシリアライズして復元し、元の Session バックエンドと `session_id` を使用して `Runner.run(...)` または `Runner.run_streamed(...)` を再試行してください。次のモデル呼び出しの前に、SDK は保留中のバッチと Session 履歴を照合します。Session が完全なバッチをコミットしたものの、確認応答に失敗した場合、SDK は履歴末尾が完全に一致することを認識し、バッチを再度追加しません。書き込みがコミットされていない場合、SDK は追加処理を再試行します。SDK は、完了済みのツール、ツールガードレール、フック、ハンドオフを再実行しません。再開された実行は、完了したハンドオフによって選択されたエージェントで継続します。

Session 履歴が完全に一致しない場合、復旧は安全側で失敗します。元の Session バックエンドと `session_id` を使用し、再開された実行がその履歴へ排他的にアクセスできるようにしてください。別の書き込み元が履歴末尾を変更した場合、保留中のバッチの一部しか存在しない場合、または履歴にその他の曖昧さがある場合、SDK は次のモデル呼び出しの前に [`UserError`][agents.exceptions.UserError] を発生させます。再開する前に元の Session 履歴を修復してください。完了済みの処理を再実行しないでください。保留中のバッチ、選択されたエージェント、蓄積されたツールガードレールの結果は、復旧完了前に別の承認による中断が発生した場合も含め、`RunState` の JSON および文字列のラウンドトリップ後も保持されます。[`stream_events()`][agents.result.RunResultStreaming.stream_events] が Session 書き込みエラーを発生させた後、[`RunResultStreaming.to_state()`][agents.result.RunResultStreaming.to_state] も同じ復旧データを保持する切り離された状態を返します。

この復旧は、実行が最終出力を受け入れ、出力ガードレールと終了時フックを完了した後、その最終ターンの永続化に失敗した場合には適用されません。その状態を再実行すると、終了時のライフサイクル効果が繰り返される可能性があるため、SDK は `RunState` を復旧不能としてマークします。その状態を使用した後続のすべての `Runner.run(...)` または `Runner.run_streamed(...)` の試行では、Session の照合、サンドボックスの準備、モデル呼び出し、ツール、ガードレール、フックが実行される前に [`UserError`][agents.exceptions.UserError] が発生します。このマーカーは `RunState` のシリアライズ後も保持されます。その状態を再試行するのではなく、新しい実行を開始してください。この境界は、終了時の関数ツール出力と、履歴への保存が受け入れられた `max_turns` ハンドラー出力にも適用されます。

#### 再開前の入力追加 {#add-input-before-resuming}

実行が一時停止した場合、または完了したターンの後に停止したものの、未完了の実行が次のモデル呼び出しに到達する前に新しいユーザー入力が届いた場合は、[`RunState.add_input()`][agents.run_state.RunState.add_input] を使用します。文字列はユーザーメッセージになり、複数回の呼び出しでは挿入順序が保持されます。ステージングされた入力は、シリアライズされた `RunState` の一部であるため、`to_json()` / `from_json()` および `to_string()` / `from_string()` のラウンドトリップ後も保持されます。

```python
state = result.to_state()
state.add_input("Also keep the generated report in the project folder.")

for interruption in state.get_interruptions():
    state.approve(interruption)

result = await Runner.run(agent, state)
```

再開時、Runner は現在のエージェントの入力ガードレールと [`RunConfig`][agents.run.RunConfig] の入力ガードレールの両方を、ステージングされた入力にのみ適用します。クライアント管理の [`Session`][agents.memory.session.Session] が設定されている場合、Runner は受け入れられたステージング済み入力を永続的な [`InputItem`][agents.items.InputItem] に変換し、モデルリクエストを発行する前に Session への書き込みを待機します。クライアント管理の Session もサーバー管理の会話も使用しない場合、Runner はモデルリクエストを発行する前に、受け入れられたステージング済み入力を `InputItem` に変換します。サーバー管理の会話では、サーバーリクエストが入力を受け入れるまで、入力は保留中のままです。シリアライズ、再開、再実行しても安全な再試行を通して、SDK は永続的な `InputItem` の出現を 1 つだけ保持します。この SDK による出現回数の保証は、プロバイダーへの配信保証ではありません。リクエストがプロバイダーに到達した可能性がある後で再試行ポリシーが `RetryDecision(approve_unsafe_replay=True)` を返した場合、Runner はステージング済み入力を再送信でき、プロバイダー側の処理が繰り返される可能性があります。正常に受け入れられた入力は、`new_items` に `InputItem` として表示されます。切り離されたコピーを取得するには [`RunState.pending_input`][agents.run_state.RunState.pending_input] を読み取り、再開前にすべてのステージング済み入力を破棄するには [`RunState.clear_pending_input()`][agents.run_state.RunState.clear_pending_input] を呼び出します。

`RunState.add_input()` は、終了状態、モデルターンが残っていない状態、受け入れ済みのモデルレスポンスがローカル処理を待っている状態、保留中のツール実行結果によって次のモデル呼び出し前に実行が終了する可能性がある中断状態を拒否します。このような場合は、現在の実行を完了してから、新しいユーザーターンを開始してください。

ストリーミング実行では、まず [`stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を完了し、その後 `result.interruptions` を確認して `result.to_state()` から再開してください。承認フロー全体については、[Human-in-the-loop](human_in_the_loop.md)を参照してください。

### サーバー管理の継続 {#server-managed-continuation}

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、実行から得られた最新のモデルレスポンス ID です。OpenAI Responses API のチェーンを継続する場合は、次のターンで `previous_response_id` として渡してください。

`to_input_list()`、`session`、`conversation_id` のいずれかを使用して会話をすでに継続している場合、通常は `last_response_id` は必要ありません。複数ステップの実行に含まれるすべてのモデルレスポンスが必要な場合は、代わりに `raw_responses` を確認してください。

## エージェントをツールとして使用する場合のメタデータ {#agent-as-tool-metadata}

ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行から実行結果が得られた場合、[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] は、それを囲む `Agent.as_tool()` 呼び出しに関する不変のメタデータを公開します。

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

通常のトップレベル実行では、`agent_tool_invocation` は `None` です。

これは `custom_output_extractor` 内で特に有用です。ネストされた実行結果を後処理する際に、それを囲む `Agent.as_tool()` 呼び出しのツール名、呼び出し ID、raw 引数が必要になる場合があります。関連する `Agent.as_tool()` のパターンについては、[ツール](tools.md)を参照してください。

そのネストされた実行で解析済みの構造化入力も必要な場合は、`context_wrapper.tool_input` を読み取ります。これは、[`RunState`][agents.run_state.RunState] がネストされたツール入力用に汎用的にシリアライズするフィールドです。一方、`agent_tool_invocation` は現在のネストされた呼び出しのメタデータを実行結果上で直接公開します。

## ストリーミングのライフサイクルと診断 {#streaming-lifecycle-and-diagnostics}

[`RunResultStreaming`][agents.result.RunResultStreaming] は、上記と同じ実行結果インターフェースを継承しますが、ストリーミング固有の制御機能も追加します。

-   セマンティックなストリームイベントを消費するための [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   実行中にアクティブなエージェントを追跡するための [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   ストリーミング実行が完全に終了したかどうかを確認するための [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   実行を直ちに、または現在のターンの後で停止するための [`cancel(...)`][agents.result.RunResultStreaming.cancel]

非同期イテレーターが終了するまで、`stream_events()` の消費を続けてください。そのイテレーターが終了するまでストリーミング実行は完了しません。また、最後に表示されるトークンが到着した後も、`final_output`、`interruptions`、`raw_responses` などの概要プロパティや、Session の永続化による副作用の処理が継続している場合があります。

`cancel()` を呼び出した場合は、キャンセルとクリーンアップが正しく完了できるように、`stream_events()` の消費を続けてください。

Python では、ストリーミング用の独立した `completed` Promise や `error` プロパティは公開されません。実行を終了させるストリーミングエラーは `stream_events()` によって発生し、`is_complete` は実行が終了状態に到達したかどうかを示します。

### Raw レスポンス {#raw-responses}

[`raw_responses`][agents.result.RunResultBase.raw_responses] には、実行中に収集された raw モデルレスポンスが格納されます。複数ステップの実行では、ハンドオフやモデル、ツール、モデルというサイクルの繰り返しなどにより、複数のレスポンスが生成される場合があります。

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、`raw_responses` の最後のエントリに含まれる ID です。

各 [`ModelResponse`][agents.items.ModelResponse] は、その個別のモデル呼び出しに適用される 2 つの診断情報も公開します。

-   [`request_id`][agents.items.ModelResponse.request_id] は、モデルアダプターとトランスポートがリクエスト ID を伝播する場合のトランスポートリクエスト ID です。組み込みの `OpenAIResponsesModel` と `OpenAIChatCompletionsModel` は、HTTP および SSE のトランスポート経路で、利用可能なサーバー生成の `x-request-id` を伝播します。設定されたエンドポイントが OpenAI API の場合、本番環境では `None` ではない値をログに記録すると、障害を OpenAI サポートと関連付けることができます。OpenAI 互換のプロバイダーまたはプロキシの場合は、代わりにそのサービスのサポート窓口を使用してください。`OpenAIResponsesWSModel` では現在、`request_id` は `None` のままです。サードパーティ製アダプターでは、リクエスト ID の伝播は保証されません。AnyLLM Chat Completions アダプターと `LitellmModel` では現在、`request_id` は `None` のままです。Agents SDK の AnyLLM Responses アダプターでも、トランスポートリクエスト ID を保持せずにプロバイダーレスポンスを正規化する場合、`request_id` が `None` のままになることがあります。
-   [`raw_usage`][agents.items.ModelResponse.raw_usage] は、Agents SDK がペイロードを正規化する前のプロバイダーの使用量ペイロードについて、オプトインで取得できる JSON 互換のスナップショットです。`ModelSettings(preserve_raw_usage=True)` で `raw_usage` を有効にしてください。[プロバイダーの使用量ペイロードの保持](usage.md#preserving-provider-usage-payloads)を参照してください。

`ModelResponse.request_id` と `ModelResponse.raw_usage` はそれぞれ `None` になる可能性があるため、会話状態ではなく、任意の診断情報として扱ってください。

### ガードレールの結果 {#guardrail-results}

エージェントレベルのガードレールは、[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] として公開されます。

ツールガードレールは、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として個別に公開されます。

これらの配列は実行全体にわたって蓄積されるため、判断のログ記録、追加のガードレールメタデータの保存、実行がブロックされた理由のデバッグに役立ちます。

エージェントレベルの出力ガードレールが、終了時の関数ツールによって直接生成された最終出力をブロックする場合、1 つの編集ルールが適用されます。ブロックされた現在のレスポンスでは、`output_guardrail_results` が拒否されたエージェント出力を置き換え、ペイロードを含む出力メタデータを消去します。また、`tool_output_guardrail_results` がペイロードを含むツールメタデータを置き換えます。以前に受け入れられた実行結果は変更されません。サニタイズ済みの出力ガードレール結果は、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] の `guardrail_result` として公開されます。サニタイズ済みの出力ガードレール結果とツール出力ガードレール結果は、ストリーミングされた実行結果の状態と `RunState` からも公開されます。[出力ガードレール](guardrails.md#output-guardrails)を参照してください。

### コンテキストと使用量 {#context-and-usage}

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] は、アプリケーションのコンテキストに加え、承認、使用量、ネストされた `tool_input` など、SDK が管理するランタイムメタデータを公開します。

使用量は `context_wrapper.usage` で追跡されます。ストリーミング実行では、ストリームの最後のチャンクが処理されるまで、使用量の合計値に遅れが生じる場合があります。ラッパーの完全な構造と永続化に関する注意事項については、[コンテキスト管理](context.md)を参照してください。