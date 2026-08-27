---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを使用して、エージェントを実行できます。次の 3 つの方法があります。

1. [`Runner.run()`][agents.run.Runner.run] は、非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] は同期メソッドで、内部では単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] は、非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、イベントを受信すると同時にストリーミングします。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="You are a helpful assistant")

    result = await Runner.run(agent, "Write a haiku about recursion in programming.")
    print(result.final_output)
    # Code within the code,
    # Functions calling themselves,
    # Infinite loop's dance
```

詳しくは、[実行結果ガイド](results.md)をご覧ください。

## Runner のライフサイクルと設定 {#runner-lifecycle-and-configuration}

### エージェントループ {#the-agent-loop}

上記 3 つの `Runner` メソッドのいずれかを呼び出すときは、開始エージェントと入力を渡します。入力には次のものを指定できます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力項目のリスト
-   一時停止された実行、または `cancel(mode="after_turn")` で停止された実行を再開する場合の [`RunState`][agents.run_state.RunState]。状態には、[次回の再開後のモデル呼び出し用に準備された入力](results.md#add-input-before-resuming)も含められます。

その後、Runner は次のループを実行します。

1. 現在のエージェントについて、現在の入力を使用して LLM を呼び出します。
2. LLM が出力を生成します。
    1. Runner が LLM の出力を最終出力と判定した場合、ループを終了して実行結果を返します。
    2. LLM がハンドオフを要求した場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行して実行結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。このターン制限を無効にするには、`max_turns=None` を渡します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、目的の型のテキスト出力が生成され、ツール呼び出しがないことです。

### ストリーミング {#streaming}

ストリーミングを使用すると、LLM の実行中にストリーミングイベントも受信できます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行の完全な情報が格納されます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳しくは、[ストリーミングガイド](streaming.md)をご覧ください。

#### Responses WebSocket トランスポート（オプションのヘルパー） {#responses-websocket-transport-optional-helper}

OpenAI Responses の WebSocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。接続を再利用する場合は WebSocket セッションヘルパーを推奨しますが、必須ではありません。

これは WebSocket トランスポート経由の Responses API であり、[Realtime API](realtime/guide.md)ではありません。

トランスポートの選択規則や、具象モデルオブジェクトまたはカスタムプロバイダーに関する注意事項については、[モデル](models/index.md#responses-websocket-transport)をご覧ください。

##### パターン 1：セッションヘルパーなし（動作可能） {#pattern-1-no-session-helper-works}

WebSocket トランスポートだけが必要で、共有プロバイダーやセッションを SDK で管理する必要がない場合に使用します。

```python
import asyncio

from agents import Agent, Runner, set_default_openai_responses_transport


async def main():
    set_default_openai_responses_transport("websocket")

    agent = Agent(name="Assistant", instructions="Be concise.")
    result = Runner.run_streamed(agent, "Summarize recursion in one sentence.")

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            continue
        print(event.type)


asyncio.run(main())
```

このパターンは、単一の実行には適しています。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼び出すと、同じ `RunConfig` / プロバイダーインスタンスを手動で再利用しない限り、実行ごとに再接続される可能性があります。

##### パターン 2：`responses_websocket_session()` の使用（複数ターンでの再利用に推奨） {#pattern-2-use-responses_websocket_session-recommended-for-multi-turn-reuse}

複数の実行で、WebSocket 対応の共有プロバイダーと `RunConfig` を使用する場合は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します。同じ `run_config` を継承する、エージェントをツールとして使用するネストされた呼び出しも対象です。

```python
import asyncio

from agents import Agent, responses_websocket_session


async def main():
    agent = Agent(name="Assistant", instructions="Be concise.")

    async with responses_websocket_session(
        responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
    ) as ws:
        first = ws.run_streamed(agent, "Say hello in one short sentence.")
        async for _event in first.stream_events():
            pass

        second = ws.run_streamed(
            agent,
            "Now say goodbye.",
            previous_response_id=first.last_response_id,
        )
        async for _event in second.stream_events():
            pass


asyncio.run(main())
```

コンテキストを終了する前に、ストリーミングされた実行結果を最後まで消費してください。WebSocket リクエストの処理中にコンテキストを終了すると、共有接続が強制的に閉じられる場合があります。

各 WebSocket 接続では、一度に 1 つのレスポンスが処理され、接続時間は 60 分に制限されます。ヘルパーは接続を再利用しますが、これらの制約を取り除くものではありません。再接続後、`store=False` および ZDR フローでは、キャッシュされていない `previous_response_id` を復元できません。完全な入力コンテキストで新しいチェーンを開始するか、ローカルで管理しているセッション状態から再構築してください。復元動作の詳細については、[Responses WebSocket トランスポートに関する注意事項](models/index.md#responses-websocket-transport)をご覧ください。

長時間の推論ターンで WebSocket のキープアライブタイムアウトが発生する場合は、`ping_timeout` を増やすか、`ping_timeout=None` を設定してハートビートタイムアウトを無効にしてください。WebSocket のレイテンシより信頼性が重要な実行では、HTTP/SSE トランスポートを使用してください。

### 実行設定 {#run-config}

`run_config` パラメーターを使用すると、エージェントの実行に関する一部のグローバル設定を構成できます。

#### 一般的な実行設定のカテゴリー {#common-run-config-categories}

各エージェントの定義を変更せず、単一の実行だけ動作を上書きするには、`RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト {#model-provider-and-session-defaults}

-   [`model`][agents.run.RunConfig.model]：各 Agent が持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名を検索するためのモデルプロバイダーです。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` または `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際のセッションレベルのデフォルト（たとえば `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions の使用時に、各 `Runner` の実行前に新しいユーザー入力をセッション履歴と結合する方法をカスタマイズします。コールバックは同期または非同期にできます。

##### ガードレール、ハンドオフ、モデル入力の整形 {#guardrails-handoffs-and-model-input-shaping}

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフに入力フィルターがまだない場合、すべてのハンドオフに適用するグローバル入力フィルターです。入力フィルターを使用すると、新しいエージェントに送信される入力を編集できます。詳しくは、[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次のエージェントを呼び出す前に、ロスレスなメッセージ項目を元の位置に保持しながら、要約可能な履歴を順序付きのアシスタント要約セグメントへ圧縮する、オプトインのベータ機能です。ネストされたハンドオフの安定化を進めているため、デフォルトでは無効です。有効にするには `True` を設定し、未加工のトランスクリプトをそのまま渡すには `False` のままにします。SDK のデフォルトのネスト履歴がメッセージをすでに保持している場合、Sessions、`RunState`、`RunResult.to_input_list()` は同一のメッセージ出現を二重に追加しない一方、別々に存在する同一メッセージは保持します。すべての [Runner メソッド][agents.run.Runner]は、渡されなかった場合に `RunConfig` を自動的に作成するため、クイックスタートとコード例ではデフォルトが無効のままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこの設定を上書きします。個々のハンドオフでは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を使用してこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` をオプトインした場合に、正規化されたトランスクリプト（履歴とハンドオフ項目）を受け取るオプションの callable です。完全なハンドオフフィルターを記述せずに、組み込みの順序付き要約セグメントを置き換え、次のエージェントへ転送する入力項目の正確なリストを返す必要があります。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力項目）を編集するためのフックです。たとえば、履歴のトリミングやシステムプロンプトの挿入に使用できます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：Runner が以前の出力を次のターンのモデル入力に変換するとき、推論項目 ID を保持するか省略するかを制御します。

##### トレーシングと可観測性 {#tracing-and-observability}

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の[トレーシング](tracing.md)を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]：実行ごとのトレーシング API キーなど、トレースのエクスポート設定を上書きするには、[`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：LLM およびツール呼び出しの入出力など、機密である可能性のあるデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は、複数の実行にまたがるトレースを関連付けるためのオプションフィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。

##### ツールの実行、承認、エラー動作 {#tool-execution-approval-and-tool-error-behavior}

-   [`tool_execution`][agents.run.RunConfig.tool_execution]：同時に実行するローカル関数ツール呼び出し数の制限など、ローカルツール呼び出しに対する SDK 側の実行動作を設定します。
-   [`tool_not_found_behavior`][agents.run.RunConfig.tool_not_found_behavior]：モデルが生成した関数ツール呼び出しのツール名が、現在のエージェントで利用可能ないずれの関数ツールとも一致しない場合の Runner の処理方法を設定します。デフォルトでは `ModelBehaviorError` が発生します。代わりに、モデルから認識できるエラー出力を返すようオプトインできます。
-   [`tool_name_collision_policy`][agents.run.RunConfig.tool_name_collision_policy]：名前空間のない関数ツール名とハンドオフ名が競合した場合の Runner の処理方法を設定します。デフォルトの `"warn"` では、対処可能な警告をログに記録し、現在のディスパッチ先として選ばれたものだけを公開します。`"error"` では、モデルが呼び出される前に `UserError` が発生します。名前空間付きツールと遅延読み込みツールに対する厳密な検証は変更されません。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認の拒否や、オプトインしたツール未検出時の出力など、モデルから認識できるツールエラーメッセージをカスタマイズします。

ネストされたハンドオフは、オプトインのベータ機能として利用できます。順序付きトランスクリプト圧縮を有効にするには `RunConfig(nest_handoff_history=True)` を渡し、特定のハンドオフで有効にするには `handoff(..., nest_handoff_history=True)` を設定します。組み込みのマッパーは、トランスクリプト全体を 1 つのメッセージにまとめるのではなく、生成されたアシスタント要約セグメントをロスレスなメッセージ項目の前後に配置します。未加工のトランスクリプトを保持する場合（デフォルト）は、フラグを設定しないか、必要な形式で会話を転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定します。カスタムマッパーを記述せずに、生成される要約セグメントで使用するラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] を呼び出します）。

#### 実行設定の詳細 {#run-config-details}

##### `tool_execution` {#tool_execution}

実行時のローカル関数ツールの同時実行数を制限するなど、ローカル関数ツールに対する SDK 側の動作を設定する場合は、`tool_execution` を使用します。

```python
from agents import Agent, RunConfig, Runner, ToolExecutionConfig

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Run the required tool calls.",
    run_config=RunConfig(
        tool_execution=ToolExecutionConfig(
            max_function_tool_concurrency=2,
            pre_approval_tool_input_guardrails=True,
        ),
    ),
)
```

`max_function_tool_concurrency=None` はデフォルトの動作を維持します。モデルが 1 ターンで複数の関数ツール呼び出しを生成すると、SDK は生成されたすべてのローカル関数ツール呼び出しを開始します。同時に実行するローカル関数ツール呼び出し数を制限するには、整数値を設定します。

これは、プロバイダー側の [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] とは別のものです。`parallel_tool_calls` は、モデルが 1 つのレスポンスで複数のツール呼び出しを生成できるかどうかを制御します。`tool_execution.max_function_tool_concurrency` は、モデルがツール呼び出しを生成した後に、SDK がローカル関数ツール呼び出しをどのように実行するかを制御します。

`pre_approval_tool_input_guardrails=False` はデフォルトの承認フローを維持します。関数ツールに承認が必要な場合、まず実行が一時停止し、ツール入力ガードレールは承認後、実行直前にのみ実行されます。保留中の承認による中断が生成される前に関数ツールの入力ガードレールを実行する場合は、`True` に設定します。この承認前チェックを通過した呼び出しでも、承認後に同じ入力ガードレールが再度実行されるため、時間に依存するチェックは実行前に再検証されます。

##### `tool_not_found_behavior` {#tool_not_found_behavior}

デフォルトでは、モデルが生成した関数ツール呼び出しが、現在のエージェントで利用可能ないずれの関数ツールとも一致しない場合、Runner は `ModelBehaviorError` を発生させます。

実行を復元可能な状態に保つ場合は、`tool_not_found_behavior="return_error_to_model"` を設定します。このモードでは、SDK は解決できなかったツール呼び出しに `function_call_output` を追加してモデルを再実行するため、モデルは利用可能なツールを選択するか、そのツールを使用せずに回答できます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Handle this request with the available tools.",
    run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
)
```

現在、このオプションはツール名の検索に失敗した関数ツール呼び出しにのみ適用されます。その他の無効なツールペイロードでは、既存のエラー動作が引き続き使用されます。

##### `tool_error_formatter` {#tool_error_formatter}

SDK がモデルから認識できるツールエラー出力を作成する際、モデルに返すメッセージをカスタマイズするには、`tool_error_formatter` を使用します。

フォーマッターは、次の内容を持つ [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`：`"approval_rejected"` や `"tool_not_found"` などのエラーカテゴリー。
-   `tool_type`：ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`、または `"custom"`）。
-   `tool_name`：ツール名。
-   `call_id`：ツール呼び出し ID。
-   `default_message`：SDK のデフォルトの、モデルから認識できるメッセージ。
-   `run_context`：アクティブな実行コンテキストラッパー。

メッセージを置き換える文字列を返すか、SDK のデフォルトを使用する場合は `None` を返します。

```python
from agents import Agent, RunConfig, Runner, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind == "approval_rejected":
        return (
            f"Tool call '{args.tool_name}' was rejected by a human reviewer. "
            "Ask for confirmation or propose a safer alternative."
        )
    if args.kind == "tool_not_found":
        return f"Tool '{args.tool_name}' is not available. Choose one of the listed tools."
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

##### `reasoning_item_id_policy` {#reasoning_item_id_policy}

`reasoning_item_id_policy` は、Runner が履歴を引き継ぐとき（たとえば、`RunResult.to_input_list()` またはセッションに基づく実行を使用するとき）、推論項目を次のターンのモデル入力へ変換する方法を制御します。

-   `None` または `"preserve"`（デフォルト）：推論項目 ID を保持します。
-   `"omit"`：生成される次のターンの入力から推論項目 ID を取り除きます。

推論項目が `id` とともに送信される一方で、必要な後続項目（たとえば `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）がないために発生する、一種の Responses API 400 エラーへのオプトインの緩和策として、主に `"omit"` を使用します。

これは、SDK が以前の出力から後続入力を構築する複数ターンのエージェント実行で発生する可能性があります。これには、セッションの永続化、サーバー管理の会話差分、ストリーミングおよび非ストリーミングの後続ターン、再開パスが含まれます。このとき推論項目 ID が保持されていても、プロバイダーがその ID と対応する後続項目のペアを維持するよう要求する場合があります。

`reasoning_item_id_policy="omit"` を設定すると、推論内容は維持されますが、推論項目の `id` は取り除かれます。これにより、SDK が生成する後続入力でこの API の不変条件に違反することを回避できます。

適用範囲に関する注意事項：

-   これは、SDK が後続入力を構築するときに生成または転送する推論項目のみを変更します。
-   ユーザーが指定した初期入力項目は書き換えません。
-   `call_model_input_filter` は、このポリシーが適用された後でも意図的に推論 ID を再導入できます。

## 状態と会話の管理 {#state-and-conversation-management}

### メモリ戦略の選択 {#choose-a-memory-strategy}

次のターンへ状態を引き継ぐ一般的な方法は 4 つあります。

| 戦略 | 状態の保存場所 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` のリストと次のユーザーメッセージ |
| `session` | ストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを参照する別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有する、名前付きのサーバー側会話 | 同じ `conversation_id` と新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作成しない、軽量なサーバー管理の継続 | `result.last_response_id` と新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理であり、OpenAI Responses API を使用している場合にのみ適用されます。ほとんどのアプリケーションでは、会話ごとに 1 つの永続化戦略を選択してください。両方のレイヤーを意図的に調整している場合を除き、クライアント管理の履歴と OpenAI 管理の状態を混在させると、コンテキストが重複する可能性があります。

!!! note

    同じ実行で、セッションの永続化とサーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）を
    組み合わせることはできません。呼び出しごとに 1 つの方法を選択してください。

### 会話とチャットスレッド {#conversationschat-threads}

いずれかの実行メソッドを呼び出すと、1 つ以上のエージェントが実行される可能性があり、その結果として 1 回以上の LLM 呼び出しが行われます。ただし、チャット会話上は 1 つの論理ターンを表します。たとえば、次のようになります。

1. ユーザーターン：ユーザーがテキストを入力します
2. Runner の実行：最初のエージェントが LLM を呼び出してツールを実行し、2 番目のエージェントへハンドオフします。2 番目のエージェントはさらにツールを実行し、出力を生成します。

エージェントの実行終了時に、ユーザーへ表示する内容を選択できます。たとえば、エージェントが生成したすべての新しい項目を表示することも、最終出力だけを表示することもできます。いずれの場合も、その後ユーザーが追加の質問をする可能性があり、その場合は実行メソッドを再度呼び出せます。

#### 会話の手動管理 {#manual-conversation-management}

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して次のターンの入力を取得し、会話履歴を手動で管理できます。

```python
from agents import Agent, Runner, trace

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
        print(result.final_output)
        # San Francisco

        # Second turn
        new_input = result.to_input_list() + [{"role": "user", "content": "What state is it in?"}]
        result = await Runner.run(agent, new_input)
        print(result.final_output)
        # California
```

#### Sessions による会話の自動管理 {#automatic-conversation-management-with-sessions}

より簡単な方法として、[Sessions](sessions/index.md)を使用すると、`.to_input_list()` を手動で呼び出さずに会話履歴を自動的に処理できます。

```python
from agents import Agent, Runner, SQLiteSession, trace

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create session instance
    session = SQLiteSession("conversation_123")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
        print(result.final_output)
        # San Francisco

        # Second turn - agent automatically remembers previous context
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(result.final_output)
        # California
```

Sessions は、次の処理を自動的に行います。

-   各実行前に会話履歴を取得します
-   各実行後に新しいメッセージを保存します
-   セッション ID ごとに個別の会話を維持します

詳しくは、[Sessions のドキュメント](sessions/index.md)をご覧ください。


#### サーバー管理の会話 {#server-managed-conversations}

`to_input_list()` または `Sessions` を使用してローカルで処理する代わりに、OpenAI の会話状態機能でサーバー側の会話状態を管理することもできます。これにより、過去のすべてのメッセージを手動で再送信せずに会話履歴を保持できます。以下のどちらのサーバー管理方式でも、各リクエストでは新しいターンの入力だけを渡し、保存した ID を再利用します。詳しくは、[OpenAI の会話状態ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)をご覧ください。

OpenAI では、ターン間の状態を追跡する方法を 2 つ提供しています。

##### 1. `conversation_id` の使用 {#1-using-conversation_id}

まず OpenAI Conversations API を使用して会話を作成し、それ以降のすべての呼び出しでその ID を再利用します。

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id

    while True:
        user_input = input("You: ")
        result = await Runner.run(agent, user_input, conversation_id=conv_id)
        print(f"Assistant: {result.final_output}")
```

##### 2. `previous_response_id` の使用 {#2-using-previous_response_id}

もう 1 つの方法は **レスポンスチェイニング** です。各ターンを、直前のターンのレスポンス ID に明示的に関連付けます。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    previous_response_id = None

    while True:
        user_input = input("You: ")

        # Setting auto_previous_response_id=True enables response chaining automatically
        # for the first turn, even when there's no actual previous response ID yet.
        result = await Runner.run(
            agent,
            user_input,
            previous_response_id=previous_response_id,
            auto_previous_response_id=True,
        )
        previous_response_id = result.last_response_id
        print(f"Assistant: {result.final_output}")
```

実行が承認待ちで一時停止し、[`RunState`][agents.run_state.RunState] から再開する場合、SDK は保存されている `conversation_id` / `previous_response_id` / `auto_previous_response_id` の設定を維持するため、再開されたターンは同じサーバー管理の会話で継続されます。

`conversation_id` と `previous_response_id` は同時に使用できません。システム間で共有できる名前付き会話リソースが必要な場合は、`conversation_id` を使用します。ターン間で最も軽量な Responses API の継続用基本コンポーネントが必要な場合は、`previous_response_id` を使用します。

!!! note

    SDK は、`conversation_locked` エラーをバックオフ付きで自動的に再試行します。サーバー管理の
    会話を使用する実行では、再試行前に内部の会話トラッカー入力を巻き戻すため、準備済みの
    同じ項目を問題なく再送信できます。

    ローカルのセッションベースの実行（`conversation_id`、
    `previous_response_id`、または `auto_previous_response_id` とは組み合わせられません）では、
    SDK は最近永続化した入力項目のベストエフォートなロールバックも行い、再試行後の履歴項目の
    重複を抑えます。

    この互換性のための再試行は、`ModelSettings.retry` を設定していない場合でも実行されます。
    モデルリクエストに対する、より広範なオプトインの再試行動作については、[Runner 管理の再試行](models/index.md#runner-managed-retries)をご覧ください。

## フックとカスタマイズ {#hooks-and-customization}

### モデル呼び出し入力フィルター {#call-model-input-filter}

モデル呼び出しの直前にモデル入力を編集するには、`call_model_input_filter` を使用します。このフックは、現在のエージェント、コンテキスト、結合済みの入力項目（存在する場合はセッション履歴を含みます）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトでなければなりません。その `input` フィールドは必須で、入力項目のリストでなければなりません。それ以外の形式を返すと、`UserError` が発生します。

```python
from agents import Agent, Runner, RunConfig
from agents.run import CallModelData, ModelInputData

def drop_old_messages(data: CallModelData[None]) -> ModelInputData:
    # Keep only the last 5 items and preserve existing instructions.
    trimmed = data.model_data.input[-5:]
    return ModelInputData(input=trimmed, instructions=data.model_data.instructions)

agent = Agent(name="Assistant", instructions="Answer concisely.")
result = Runner.run_sync(
    agent,
    "Explain quines",
    run_config=RunConfig(call_model_input_filter=drop_old_messages),
)
```

Runner は準備済み入力リストのコピーをフックへ渡すため、呼び出し元の元のリストをその場で変更することなく、トリミング、置換、並べ替えができます。

セッションを使用している場合、`call_model_input_filter` はセッション履歴が読み込まれ、現在のターンと結合された後に実行されます。それより前の結合処理自体をカスタマイズする場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。

`conversation_id`、`previous_response_id`、または `auto_previous_response_id` を使用して OpenAI のサーバー管理の会話状態を利用している場合、フックは次回の Responses API 呼び出し用に準備されたペイロードに対して実行されます。そのペイロードは、以前の履歴全体の再送ではなく、新しいターンの差分のみをすでに表している場合があります。返した項目だけが、そのサーバー管理の継続処理で送信済みとして記録されます。

機密データの編集、長い履歴のトリミング、追加のシステムガイダンスの挿入を行うには、`run_config` を使用して実行ごとにフックを設定します。

## エラーと復旧 {#errors-and-recovery}

### エラーハンドラー {#error-handlers}

すべての `Runner` エントリーポイントは、エラー種別をキーとする辞書 `error_handlers` を受け取ります。サポートされるキーは、`"max_turns"`、`"model_refusal"`、`"invalid_final_output"` です。対応するエラーで実行を終了する代わりに、制御された最終出力を返す場合に使用します。

```python
from agents import (
    Agent,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    Runner,
)

agent = Agent(name="Assistant", instructions="Be concise.")


def on_max_turns(_data: RunErrorHandlerInput[None]) -> RunErrorHandlerResult:
    return RunErrorHandlerResult(
        final_output="I couldn't finish within the turn limit. Please narrow the request.",
        include_in_history=False,
    )


result = Runner.run_sync(
    agent,
    "Analyze this long transcript",
    max_turns=3,
    error_handlers={"max_turns": on_max_turns},
)
print(result.final_output)
```

モデルメッセージがエージェントの structured `output_type` に対して検証を通過しない場合、またはモデルが structured な最終メッセージを返さない場合は、`"invalid_final_output"` を使用します。ハンドラーはアプリケーション固有のフォールバックを返すことができ、SDK は同じ `output_type` に対して検証します。モデル呼び出しの再試行や、ツールの副作用の再実行は行いません。`None` を返すと、復旧を行いません。フォールバックがない場合、空でない値の検証失敗では引き続き `ModelBehaviorError` が発生し、空の structured レスポンスでは既存の次ターン動作が維持されます。

```python
from pydantic import BaseModel

from agents import Agent, ModelBehaviorError, RunErrorHandlerInput, Runner


class Recipe(BaseModel):
    ingredients: list[str]
    recovered_from_invalid_output: bool = False


def on_invalid_final_output(data: RunErrorHandlerInput[None]) -> Recipe:
    assert isinstance(data.error, ModelBehaviorError)
    return Recipe(ingredients=[], recovered_from_invalid_output=True)


agent = Agent(
    name="Recipe assistant",
    instructions="Return a structured recipe.",
    output_type=Recipe,
)

result = Runner.run_sync(
    agent,
    "Plan tonight's dinner.",
    error_handlers={"invalid_final_output": on_invalid_final_output},
)
print(result.final_output)
```

`RunErrorHandlerResult.include_in_history` のデフォルトは `True` です。最大ターン数ハンドラーでは、合成されたフォールバック出力が会話履歴に追加され、設定済みのセッションに永続化されます。実行結果の履歴やセッションストレージに追加せず、フォールバックを呼び出し元へ返す場合は、`include_in_history=False` を設定します。

モデルによる拒否で `ModelRefusalError` により実行を終了する代わりに、アプリケーション固有のフォールバックを生成する場合は、`"model_refusal"` を使用します。

```python
from pydantic import BaseModel

from agents import Agent, ModelRefusalError, RunErrorHandlerInput, Runner


class Recipe(BaseModel):
    ingredients: list[str]
    refusal_reason: str | None = None


def on_model_refusal(data: RunErrorHandlerInput[None]) -> Recipe:
    assert isinstance(data.error, ModelRefusalError)
    return Recipe(ingredients=[], refusal_reason=data.error.refusal)


agent = Agent(
    name="Recipe assistant",
    instructions="Return a structured recipe.",
    output_type=Recipe,
)

result = Runner.run_sync(
    agent,
    "Make me something unsafe.",
    error_handlers={"model_refusal": on_model_refusal},
)
print(result.final_output)
```

## 永続実行との統合とヒューマンインザループ {#durable-execution-integrations-and-human-in-the-loop}

ツール承認の一時停止と再開のパターンについては、専用の[ヒューマンインザループガイド](human_in_the_loop.md)から始めてください。以下の統合は、実行が長時間の待機、再試行、プロセスの再起動にまたがる可能性がある場合の永続的なオーケストレーションを目的としています。

### Dapr {#dapr}

Agents SDK の [Dapr](https://dapr.io) Diagrid 統合を使用すると、障害から自動的に復旧し、ヒューマンインザループのワークフローをサポートする、永続的で長時間実行されるエージェントを実行できます。Dapr はベンダー中立の [CNCF](https://cncf.io) ワークフローオーケストレーターです。Dapr と OpenAI エージェントの使用を開始するには、[こちら](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai)をご覧ください。

### Temporal {#temporal}

Agents SDK の [Temporal](https://temporal.io/) 統合を使用すると、ヒューマンインザループのタスクを含む、永続的で長時間実行されるワークフローを実行できます。Temporal と Agents SDK が連携して長時間実行タスクを完了するデモは、[こちらの動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で確認できます。また、[こちらのドキュメント](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)もご覧ください。 

### Restate {#restate}

Agents SDK の [Restate](https://restate.dev/) 統合を使用すると、人による承認、ハンドオフ、セッション管理を含む、軽量で永続的なエージェントを実現できます。この統合では Restate の単一バイナリランタイムが依存関係として必要であり、エージェントをプロセス、コンテナ、またはサーバーレス関数として実行できます。詳しくは、[概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)または[ドキュメント](https://docs.restate.dev/ai)をご覧ください。

### DBOS {#dbos}

Agents SDK の [DBOS](https://dbos.dev/) 統合を使用すると、障害や再起動が発生しても進行状況を保持する、信頼性の高いエージェントを実行できます。長時間実行されるエージェント、ヒューマンインザループのワークフロー、ハンドオフをサポートします。同期メソッドと非同期メソッドの両方に対応しています。この統合に必要なのは SQLite または Postgres データベースだけです。詳しくは、統合の[リポジトリ](https://github.com/dbos-inc/dbos-openai-agents)および[ドキュメント](https://docs.dbos.dev/integrations/openai-agents)をご覧ください。

## 例外 {#exceptions}

SDK は特定の場合に例外を発生させます。完全なリストは [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK が発生させるすべての例外の基底クラスです。その他すべての固有の例外は、この汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェントの実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` の制限を超えた場合に発生します。これは、指定されたエージェントループのターン数（LLM 呼び出し回数）内にエージェントがタスクを完了できなかったことを示します。制限を無効にするには、`max_turns=None` を設定します。
-   [`ModelTimeoutError`][agents.exceptions.ModelTimeoutError]：モデル呼び出しの試行が [`ModelSettings.timeout`][agents.model_settings.ModelSettings.timeout] を超えた場合に発生します。適用範囲と再試行動作については、[モデル呼び出しのタイムアウト](models/index.md#model-call-timeouts)をご覧ください。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤となるモデル（LLM）が予期しない出力または無効な出力を生成した場合に発生します。これには次のものが含まれます。
    -   不正な JSON：モデルがツール呼び出しまたは直接出力で不正な JSON 構造を返した場合。特に、特定の `output_type` が定義されている場合が該当します。
    -   予期しないツール関連の失敗：モデルが想定された方法でツールを使用できなかった場合
    -   失敗または未完了の非ストリーミング Responses 呼び出し：返されたレスポンスの最終ステータスが `failed` または `incomplete` の場合、`OpenAIResponsesModel` および `AnyLLMModel` の Responses パスはこの例外を発生させます。例外には最終ステータスが示され、レスポンスから取得可能なエラーまたは未完了の詳細が含まれます。
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定済みのタイムアウトを超え、ツールが `timeout_behavior="raise_exception"` を使用している場合に発生します。
-   [`UserError`][agents.exceptions.UserError]：SDK を使用してコードを記述する人が、SDK の使用時に誤りを犯した場合に発生します。通常、コードの実装ミス、無効な設定、SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：入力ガードレールの条件が満たされると `InputGuardrailTripwireTriggered` が発生し、出力ガードレールの条件が満たされると `OutputGuardrailTripwireTriggered` が発生します。入力ガードレールは処理前に受信メッセージを確認し、出力ガードレールは配信前にエージェントの最終レスポンスを確認します。