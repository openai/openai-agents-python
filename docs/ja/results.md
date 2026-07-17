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

## 適切な実行結果インターフェースの選択

ほとんどのアプリケーションでは、少数の実行結果プロパティまたはヘルパーのみが必要です。

| 必要なもの | 使用するもの |
| --- | --- |
| ユーザーに表示する最終回答 | `final_output` |
| ローカルの完全なトランスクリプトを含む、再実行可能な次ターンの入力リスト | `to_input_list()` |
| エージェント、ツール、ハンドオフ、承認のメタデータを含む詳細な実行項目 | `new_items` |
| 通常、次のユーザーターンを処理するエージェント | `last_agent` |
| `previous_response_id` を使用した OpenAI Responses API のチェーン | `last_response_id` |
| 保留中の承認と再開可能なスナップショット | `interruptions` と `to_state()` |
| 現在のネストされた `Agent.as_tool()` 呼び出しに関するメタデータ | `agent_tool_invocation` |
| raw モデル呼び出しまたはガードレールの診断 | `raw_responses` とガードレールの実行結果配列 |

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が格納されます。これは次のいずれかです。

-   最後のエージェントに `output_type` が定義されていなかった場合は `str`
-   最後のエージェントに出力型が定義されていた場合は、`last_agent.output_type` 型のオブジェクト
-   承認待ちの中断で一時停止した場合など、最終出力が生成される前に実行が停止した場合は `None`

!!! note

    `final_output` の型は `Any` です。ハンドオフによって実行を完了するエージェントが変わる可能性があるため、SDK は考えられる出力型の完全な集合を静的に把握できません。

ストリーミングモードでは、ストリームの処理が完了するまで `final_output` は `None` のままです。イベントごとのフローについては、[ストリーミング](streaming.md)を参照してください。

## 入力、次ターンの履歴、新規項目

これらのインターフェースは、それぞれ異なる目的に対応します。

| プロパティまたはヘルパー | 格納される内容 | 最適な用途 |
| --- | --- | --- |
| [`input`][agents.result.RunResultBase.input] | この実行セグメントの基本入力。ハンドオフ入力フィルターが履歴を書き換えた場合は、実行の続行に使用されたフィルター済みの入力が反映されます。 | この実行で実際に使用された入力の監査 |
| [`to_input_list()`][agents.result.RunResultBase.to_input_list] | 実行を入力項目として表したもの。デフォルトの `mode="preserve_all"` では、`new_items` から変換された履歴が維持されます。ただし、SDK デフォルトのネストされたハンドオフ履歴へすでに移された同一のセッション項目は、再度追加されません。`mode="normalized"` では、ハンドオフのフィルタリングによってモデル履歴が書き換えられた場合、正規の継続入力が優先されます。 | 手動のチャットループ、クライアント管理の会話状態、プレーンな項目履歴の確認 |
| [`new_items`][agents.result.RunResultBase.new_items] | エージェント、ツール、ハンドオフ、承認のメタデータを含む詳細な [`RunItem`][agents.items.RunItem] ラッパー。 | ログ、UI、監査、デバッグ |
| [`raw_responses`][agents.result.RunResultBase.raw_responses] | 実行中の各モデル呼び出しから得られた raw [`ModelResponse`][agents.items.ModelResponse] オブジェクト。 | プロバイダーレベルの診断または raw レスポンスの確認 |

実際には、次のように使い分けます。

-   実行をプレーンな入力項目として確認する場合は、`to_input_list()` を使用します。
-   ハンドオフのフィルタリングやネストされたハンドオフ履歴の書き換え後、次の `Runner.run(..., input=...)` 呼び出しに使用する正規のローカル入力が必要な場合は、`to_input_list(mode="normalized")` を使用します。
-   SDK に履歴の読み込みと保存を任せる場合は、[`session=...`](sessions/index.md) を使用します。
-   `conversation_id` または `previous_response_id` を使って OpenAI のサーバー管理状態を使用している場合、通常は `to_input_list()` を再送せず、新しいユーザー入力のみを渡して保存済みの ID を再利用します。
-   ログ、UI、監査のために変換済みの完全な履歴が必要な場合は、デフォルトモードの `to_input_list()` または `new_items` を使用します。

SDK デフォルトのネストされたハンドオフ履歴でメッセージ項目がそのまま保持される場合、Sessions、`RunState`、`to_input_list()` は、内容で重複排除するのではなく、所有対象となる個々の出現を追跡します。個別に発生した同一のメッセージは別々のものとして維持され、すでに所有されている出現のみが再度追加されないように処理されます。

JavaScript SDK とは異なり、Python ではモデル形式の差分のみを表す独立した `output` プロパティは公開されていません。SDK のメタデータが必要な場合は `new_items` を使用し、raw モデルペイロードが必要な場合は `raw_responses` を確認してください。

コンピュータツールの再実行では、raw Responses ペイロードの形式が使用されます。プレビューモデルの `computer_call` 項目は単一の `action` を保持しますが、`gpt-5.5` のコンピュータ呼び出しはバッチ化された `actions[]` を保持できます。[`to_input_list()`][agents.result.RunResultBase.to_input_list] と [`RunState`][agents.run_state.RunState] は、モデルが生成した形式をそのまま維持するため、手動の再実行、一時停止と再開のフロー、保存済みトランスクリプトは、プレビュー版と GA 版の両方のコンピュータツール呼び出しで引き続き機能します。ローカルでの実行結果は、引き続き `new_items` 内の `computer_call_output` 項目として表示されます。

### 新規項目

[`new_items`][agents.result.RunResultBase.new_items] を使用すると、実行中に起きたことを最も詳細に確認できます。一般的な項目型は次のとおりです。

-   アシスタントメッセージを表す [`MessageOutputItem`][agents.items.MessageOutputItem]
-   推論項目を表す [`ReasoningItem`][agents.items.ReasoningItem]
-   Responses のツール検索リクエストと読み込まれたツール検索結果を表す [`ToolSearchCallItem`][agents.items.ToolSearchCallItem] と [`ToolSearchOutputItem`][agents.items.ToolSearchOutputItem]
-   ツール呼び出しとその実行結果を表す [`ToolCallItem`][agents.items.ToolCallItem] と [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]
-   承認のために一時停止したツール呼び出しを表す [`ToolApprovalItem`][agents.items.ToolApprovalItem]
-   ホスト型 MCP の承認とツールカタログを表す [`MCPApprovalRequestItem`][agents.items.MCPApprovalRequestItem]、[`MCPApprovalResponseItem`][agents.items.MCPApprovalResponseItem]、[`MCPListToolsItem`][agents.items.MCPListToolsItem]
-   ハンドオフリクエストと完了した転送を表す [`HandoffCallItem`][agents.items.HandoffCallItem] と [`HandoffOutputItem`][agents.items.HandoffOutputItem]

エージェントとの関連付け、ツール出力、ハンドオフ境界、承認境界が必要な場合は、`to_input_list()` ではなく `new_items` を選択してください。

ホスト型ツール検索を使用する場合、モデルが生成した検索リクエストを確認するには `ToolSearchCallItem.raw_item` を、該当ターンで読み込まれた名前空間、関数、ホスト型 MCP サーバーを確認するには `ToolSearchOutputItem.raw_item` を調べます。

プログラムによるツール呼び出し (Programmatic Tool Calling) では、生成された `program` は `ToolCallItem` であり、そのプログラムが所有する通常の子ツール呼び出しも `ToolCallItem` エントリです。また、対応する `program_output` は `ToolCallOutputItem` です。プログラムが所有するホスト型 MCP の `mcp_approval_request` 項目と `mcp_list_tools` 項目は例外で、それぞれ `MCPApprovalRequestItem` エントリと `MCPListToolsItem` エントリになります。

raw 項目には、型付きの Responses オブジェクトまたはマッピングを使用できます。特に、プログラムが所有する shell 呼び出しと apply-patch 呼び出しではマッピングが使用されます。マッピングでも安全な次の検査パターンを使用してください。

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

プログラムが所有する子呼び出しでは、`caller` の型は `program` で、`caller_id` は親プログラムの呼び出しを識別します。

## 会話の続行または再開

### 次ターンのエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] には、最後に実行されたエージェントが格納されます。多くの場合、ハンドオフ後の次のユーザーターンで再利用するエージェントとして最適です。

ストリーミングモードでは、実行の進行に合わせて [`RunResultStreaming.current_agent`][agents.result.RunResultStreaming.current_agent] が更新されるため、ストリームが完了する前にハンドオフを確認できます。

### 中断と実行状態

ツールに承認が必要な場合、保留中の承認は [`RunResult.interruptions`][agents.result.RunResult.interruptions] または [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] で公開されます。これには、直接使用されたツール、ハンドオフ後に到達したツール、ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行によって発生した承認が含まれる場合があります。

[`to_state()`][agents.result.RunResult.to_state] を呼び出して再開可能な [`RunState`][agents.run_state.RunState] を取得し、保留中の項目を承認または拒否してから、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

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

ストリーミング実行では、まず [`stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を完了し、その後で `result.interruptions` を確認して `result.to_state()` から再開します。承認フローの全体については、[Human-in-the-loop](human_in_the_loop.md)を参照してください。

### サーバー管理による継続

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、実行で得られた最新のモデルレスポンス ID です。OpenAI Responses API のチェーンを継続する場合は、次のターンで `previous_response_id` として渡します。

すでに `to_input_list()`、`session`、`conversation_id` を使用して会話を継続している場合、通常は `last_response_id` は必要ありません。複数ステップの実行に含まれるすべてのモデルレスポンスが必要な場合は、代わりに `raw_responses` を確認してください。

## ツールとしてのエージェントのメタデータ

ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行から実行結果が返された場合、[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] は外側のツール呼び出しに関する不変のメタデータを公開します。

-   `tool_name`
-   `tool_call_id`
-   `tool_arguments`

通常のトップレベル実行では、`agent_tool_invocation` は `None` です。

これは特に `custom_output_extractor` 内で役立ちます。ネストされた実行結果を後処理する際に、外側のツール名、呼び出し ID、raw 引数が必要になる場合があるためです。関連する `Agent.as_tool()` のパターンについては、[ツール](tools.md)を参照してください。

そのネストされた実行の解析済み構造化入力も必要な場合は、`context_wrapper.tool_input` を参照してください。これは [`RunState`][agents.run_state.RunState] がネストされたツール入力として汎用的にシリアライズするフィールドです。一方、`agent_tool_invocation` は現在のネストされた呼び出しに対する実行結果のライブアクセサーです。

## ストリーミングのライフサイクルと診断

[`RunResultStreaming`][agents.result.RunResultStreaming] は前述の実行結果インターフェースを継承し、さらにストリーミング固有の次の制御機能を追加します。

-   意味レベルのストリームイベントを消費するための [`stream_events()`][agents.result.RunResultStreaming.stream_events]
-   実行中にアクティブなエージェントを追跡するための [`current_agent`][agents.result.RunResultStreaming.current_agent]
-   ストリーミング実行が完全に終了したかどうかを確認するための [`is_complete`][agents.result.RunResultStreaming.is_complete]
-   実行を即座に、または現在のターンの完了後に停止するための [`cancel(...)`][agents.result.RunResultStreaming.cancel]

非同期イテレーターが終了するまで `stream_events()` を消費し続けてください。そのイテレーターが終了するまでストリーミング実行は完了していません。また、最後の可視トークンが到着した後も、`final_output`、`interruptions`、`raw_responses` などの要約プロパティや、セッション永続化の副作用が確定処理中である可能性があります。

`cancel()` を呼び出した場合も、キャンセルとクリーンアップが正しく完了するよう、`stream_events()` を引き続き消費してください。

Python では、ストリーミング用の独立した `completed` Promise や `error` プロパティは公開されていません。ストリーミングの終端エラーは `stream_events()` から例外が送出されることで通知され、`is_complete` は実行が終端状態に達したかどうかを示します。

### Raw レスポンス

[`raw_responses`][agents.result.RunResultBase.raw_responses] には、実行中に収集された raw モデルレスポンスが格納されます。複数ステップの実行では、ハンドオフやモデル、ツール、モデルの反復サイクルなどにより、複数のレスポンスが生成されることがあります。

[`last_response_id`][agents.result.RunResultBase.last_response_id] は、`raw_responses` の最後のエントリに含まれる ID にすぎません。

### ガードレールの実行結果

エージェントレベルのガードレールは、[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] と [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] として公開されます。

ツールのガードレールは、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] と [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として個別に公開されます。

これらの配列には実行全体の情報が蓄積されるため、判断内容のログ記録、追加のガードレールメタデータの保存、実行がブロックされた理由のデバッグに役立ちます。

### コンテキストと使用量

[`context_wrapper`][agents.result.RunResultBase.context_wrapper] は、アプリケーションのコンテキストと、承認、使用量、ネストされた `tool_input` など、SDK が管理するランタイムメタデータをまとめて公開します。

使用量は `context_wrapper.usage` で追跡されます。ストリーミング実行では、ストリームの最後のチャンクが処理されるまで使用量の合計値の反映が遅れる場合があります。ラッパーの完全な形式と永続化に関する注意事項については、[コンテキスト管理](context.md)を参照してください。