---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを通じてエージェントを実行できます。次の 3 つの選択肢があります。

1. [`Runner.run()`][agents.run.Runner.run] は非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] は同期メソッドで、内部では単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] は非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリーミングします。

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

詳細は [実行結果ガイド](results.md) を参照してください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使用するときは、開始エージェントと入力を渡します。入力には次のものを指定できます。

-   文字列（ユーザーメッセージとして扱われます）、
-   OpenAI Responses API 形式の入力項目のリスト、または
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]。

その後、runner はループを実行します。

1. 現在のエージェントに対して、現在の入力を使って LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、実行結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。このターン制限を無効にするには、`max_turns=None` を渡してください。

!!! note

    LLM の出力が「最終出力」とみなされる条件は、期待される型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が含まれます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意のヘルパー）

OpenAI Responses websocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。接続の再利用には websocket セッションヘルパーが推奨されますが、必須ではありません。

これは websocket トランスポート経由の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルール、および具体的なモデルオブジェクトやカスタムプロバイダーに関する注意点については、[モデル](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: セッションヘルパーなし（動作可）

SDK に共有プロバイダー/セッションを管理させる必要がなく、websocket トランスポートだけが必要な場合に使用します。

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

このパターンは単一の実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼び出す場合、同じ `RunConfig` / プロバイダーインスタンスを手動で再利用しない限り、各実行で再接続される可能性があります。

##### パターン 2: `responses_websocket_session()` の使用（複数ターンの再利用に推奨）

複数の実行にまたがって共有の websocket 対応プロバイダーと `RunConfig` を使用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します（同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含みます）。

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

ストリーミングされた実行結果の消費は、コンテキストを抜ける前に完了してください。websocket リクエストがまだ処理中の状態でコンテキストを抜けると、共有接続が強制終了される可能性があります。

長い reasoning ターンが websocket keepalive タイムアウトに達する場合は、`ping_timeout` を増やすか、`ping_timeout=None` を設定して heartbeat タイムアウトを無効にしてください。websocket レイテンシーよりも信頼性が重要な実行には、HTTP/SSE トランスポートを使用してください。

### 実行設定

`run_config` パラメーターを使用すると、エージェント実行に関するいくつかのグローバル設定を構成できます。

#### 一般的な実行設定カテゴリー

各エージェント定義を変更せずに、1 回の実行について動作を上書きするには `RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent に設定された `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を検索するためのモデルプロバイダーです。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得するときのセッションレベルのデフォルト（例: `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions を使用している場合に、各ターンの前に新しいユーザー入力をセッション履歴とどのようにマージするかをカスタマイズします。このコールバックは同期または非同期にできます。

##### ガードレール、ハンドオフ、モデル入力の整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフがまだ独自の入力フィルターを持っていない場合に、すべてのハンドオフへ適用するグローバル入力フィルターです。入力フィルターを使用すると、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェントを呼び出す前に、以前のトランスクリプトを単一の assistant メッセージに折りたたむ、オプトインの beta 機能です。ネストされたハンドオフを安定化している間はデフォルトで無効になっています。有効にするには `True` に設定し、生のトランスクリプトをそのまま渡すには `False` のままにします。すべての [Runner メソッド][agents.run.Runner] は、`RunConfig` が渡されない場合に自動的に作成するため、クイックスタートとコード例ではデフォルトがオフのままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個々のハンドオフは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を通じてこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` をオプトインした場合に、正規化されたトランスクリプト（履歴 + ハンドオフ項目）を受け取る任意の呼び出し可能オブジェクトです。次のエージェントに転送する入力項目の正確なリストを返す必要があり、完全なハンドオフフィルターを書かずに組み込みの要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力項目）を編集するためのフックです。たとえば、履歴をトリミングしたり、システムプロンプトを注入したりできます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が以前の出力を次ターンのモデル入力に変換するときに、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]: 実行ごとのトレーシング API キーなど、トレースのエクスポート設定を上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入力/出力など、潜在的に機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は、複数の実行にまたがってトレースをリンクできる任意のフィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

##### ツール実行、承認、ツールエラーの動作

-   [`tool_execution`][agents.run.RunConfig.tool_execution]: ローカルツール呼び出しに関する SDK 側の実行動作を設定します。たとえば、一度に実行される関数ツールの数を制限できます。
-   [`tool_not_found_behavior`][agents.run.RunConfig.tool_not_found_behavior]: モデルが出力した未解決の関数ツール呼び出しを runner がどのように処理するかを設定します。デフォルトでは `ModelBehaviorError` が発生します。代わりに、モデルに見えるエラー出力を返すようにオプトインできます。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認拒否やオプトインの tool-not-found 出力など、モデルに見えるツールエラーメッセージをカスタマイズします。

ネストされたハンドオフは、オプトインの beta として利用できます。トランスクリプト折りたたみ動作を有効にするには、`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで有効にするために `handoff(..., nest_handoff_history=True)` を設定します。生のトランスクリプト（デフォルト）を保持したい場合は、フラグを未設定のままにするか、会話を必要な形で正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定します。カスタムマッパーを書かずに、生成される要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### 実行設定の詳細

##### `tool_execution`

実行におけるローカル関数ツールの並行実行数を制限するなど、ローカル関数ツールに関する SDK 側の動作を設定したい場合は、`tool_execution` を使用します。

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

`max_function_tool_concurrency=None` はデフォルト動作を維持します。モデルが 1 ターンで複数の関数ツール呼び出しを出力すると、SDK は出力されたすべてのローカル関数ツール呼び出しを開始します。同時に実行されるローカル関数ツールの数に上限を設定するには、整数値を設定します。

これはプロバイダー側の [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] とは別のものです。`parallel_tool_calls` は、モデルが 1 つのレスポンス内で複数のツール呼び出しを出力することを許可されるかどうかを制御します。`tool_execution.max_function_tool_concurrency` は、モデルがローカル関数ツール呼び出しを出力した後に、SDK がそれらをどのように実行するかを制御します。

`pre_approval_tool_input_guardrails=False` はデフォルトの承認フローを維持します。関数ツールに承認が必要な場合、実行はまず一時停止し、ツール入力ガードレールは承認後、実行直前にのみ実行されます。保留中の承認中断が発行される前に関数ツール入力ガードレールを実行したい場合は、`True` に設定します。この事前承認チェックを通過した呼び出しでも、承認後に同じ入力ガードレールが再度実行されるため、時間に敏感なチェックは実行直前に再検証されます。

##### `tool_not_found_behavior`

デフォルトでは、モデルが現在のエージェントで利用可能な関数ツールのいずれにも一致しない関数ツール呼び出しを出力した場合、runner は `ModelBehaviorError` を発生させます。

実行を回復可能なままにしたい場合は、`tool_not_found_behavior="return_error_to_model"` を設定します。このモードでは、SDK は未解決のツール呼び出しに対する `function_call_output` を追加し、モデルを再度実行します。これにより、モデルは利用可能なツールを選択するか、そのツールを使わずに回答できます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Handle this request with the available tools.",
    run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
)
```

このオプションは現在、未解決の関数ツール呼び出しにのみ適用されます。その他の無効なツールペイロードは、引き続き既存のエラー動作を使用します。

##### `tool_error_formatter`

SDK がモデルに見えるツールエラー出力を作成するときに、モデルへ返されるメッセージをカスタマイズするには、`tool_error_formatter` を使用します。

フォーマッターは、以下を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`: `"approval_rejected"` や `"tool_not_found"` などのエラーカテゴリー。
-   `tool_type`: ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`、または `"custom"`）。
-   `tool_name`: ツール名。
-   `call_id`: ツール呼び出し ID。
-   `default_message`: SDK のデフォルトのモデルに見えるメッセージ。
-   `run_context`: アクティブな実行コンテキストラッパー。

メッセージを置き換えるには文字列を返し、SDK デフォルトを使用するには `None` を返します。

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

##### `reasoning_item_id_policy`

`reasoning_item_id_policy` は、runner が履歴を引き継ぐとき（たとえば、`RunResult.to_input_list()` やセッションに基づく実行を使用する場合）に、reasoning item を次ターンのモデル入力へどのように変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）: reasoning item ID を保持します。
-   `"omit"`: 生成された次ターンの入力から reasoning item ID を削除します。

主に、reasoning item が `id` 付きで送信される一方で、必須の後続項目がない場合に発生する Responses API 400 エラーの一種に対する、オプトインの緩和策として `"omit"` を使用します（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が以前の出力からフォローアップ入力を構築する複数ターンのエージェント実行で発生することがあります（セッション永続化、サーバー管理の会話差分、ストリーミング/非ストリーミングのフォローアップターン、再開パスを含みます）。reasoning item ID が保持されている一方で、プロバイダーがその ID を対応する後続項目とペアのままにすることを要求する場合です。

`reasoning_item_id_policy="omit"` を設定すると、reasoning 内容は維持しつつ reasoning item の `id` を削除するため、SDK が生成するフォローアップ入力でその API 不変条件に触れることを回避できます。

スコープに関する注記:

-   これは、SDK がフォローアップ入力を構築するときに生成/転送する reasoning item のみを変更します。
-   ユーザーが指定した初期入力項目は書き換えません。
-   `call_model_input_filter` は、このポリシー適用後でも意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

状態を次のターンに引き継ぐ一般的な方法は 4 つあります。

| 戦略 | 状態の保存場所 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` からのリストに次のユーザーメッセージを加えたもの |
| `session` | ストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい名前付きのサーバー側会話 | 同じ `conversation_id` に、新しいユーザーターンのみを加えたもの |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作成せずに行う、軽量なサーバー管理の継続 | `result.last_response_id` に、新しいユーザーターンのみを加えたもの |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理であり、OpenAI Responses API を使用している場合にのみ適用されます。ほとんどのアプリケーションでは、会話ごとに 1 つの永続化戦略を選択してください。クライアント管理の履歴と OpenAI 管理の状態を混在させると、両方のレイヤーを意図的に照合している場合を除き、コンテキストが重複する可能性があります。

!!! note

    セッション永続化は、同じ実行内でサーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）と
    組み合わせることはできません。呼び出しごとに 1 つのアプローチを選択してください。

### 会話/チャットスレッド

いずれかの実行メソッドを呼び出すと、1 つ以上のエージェントが実行される（したがって 1 回以上の LLM 呼び出しが発生する）ことがありますが、チャット会話における単一の論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフします。2 番目のエージェントがさらにツールを実行し、その後出力を生成します。

エージェント実行の終了時に、ユーザーに何を表示するかを選択できます。たとえば、エージェントが生成したすべての新しい項目をユーザーに表示することも、最終出力だけを表示することもできます。いずれの場合も、その後ユーザーがフォローアップの質問をすることがあり、その場合は run メソッドを再度呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して次のターンの入力を取得することで、会話履歴を手動で管理できます。

```python
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

#### セッションによる自動会話管理

よりシンプルな方法として、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に処理できます。

```python
from agents import Agent, Runner, SQLiteSession

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

Sessions は自動的に次を行います。

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なるセッション ID ごとに別々の会話を維持します

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理の会話

`to_input_list()` や `Sessions` を使ってローカルで処理する代わりに、OpenAI の会話状態機能にサーバー側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを手動で再送信することなく、会話履歴を保持できます。以下のどちらのサーバー管理方式でも、各リクエストでは新しいターンの入力のみを渡し、保存された ID を再利用します。詳細は [OpenAI 会話状態ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間で状態を追跡する 2 つの方法を提供します。

##### 1. `conversation_id` の使用

まず OpenAI Conversations API を使用して会話を作成し、その後の各呼び出しでその ID を再利用します。

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

##### 2. `previous_response_id` の使用

もう 1 つの選択肢は **レスポンスチェーン** で、各ターンが前のターンのレスポンス ID に明示的にリンクします。

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

実行が承認のために一時停止し、[`RunState`][agents.run_state.RunState] から再開する場合、
SDK は保存された `conversation_id` / `previous_response_id` / `auto_previous_response_id`
設定を維持するため、再開されたターンは同じサーバー管理の会話内で継続します。

`conversation_id` と `previous_response_id` は相互排他的です。システム間で共有できる名前付きの会話リソースが必要な場合は `conversation_id` を使用します。あるターンから次のターンへ進むための最も軽量な Responses API 継続用の基本コンポーネントが必要な場合は、`previous_response_id` を使用します。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動的にリトライします。サーバー管理の
    会話実行では、リトライ前に内部の会話トラッカー入力を巻き戻すため、
    同じ準備済み項目をきれいに再送信できます。

    ローカルのセッションベースの実行（`conversation_id`、
    `previous_response_id`、または `auto_previous_response_id` と組み合わせることはできません）でも、SDK はリトライ後の重複した履歴エントリを減らすために、最近永続化された入力項目のベストエフォートな
    ロールバックを行います。

    この互換性のためのリトライは、`ModelSettings.retry` を設定していない場合でも発生します。モデルリクエストに関するより広範なオプトインのリトライ動作については、[Runner 管理のリトライ](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### モデル入力フィルターの呼び出し

モデル呼び出しの直前にモデル入力を編集するには、`call_model_input_filter` を使用します。このフックは、現在のエージェント、コンテキスト、結合済みの入力項目（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力項目のリストでなければなりません。他の形式を返すと `UserError` が発生します。

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

runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元リストをその場で変更せずに、トリミング、置換、並べ替えができます。

セッションを使用している場合、`call_model_input_filter` は、セッション履歴がすでに読み込まれ、現在のターンとマージされた後に実行されます。その前段のマージ処理自体をカスタマイズしたい場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。

`conversation_id`、`previous_response_id`、または `auto_previous_response_id` を使って OpenAI のサーバー管理会話状態を使用している場合、このフックは次の Responses API 呼び出し用に準備されたペイロードに対して実行されます。そのペイロードは、以前の履歴の完全な再送ではなく、すでに新しいターンの差分のみを表している場合があります。返した項目だけが、そのサーバー管理の継続に対して送信済みとしてマークされます。

`run_config` 経由で実行ごとにこのフックを設定し、機微データを秘匿したり、長い履歴をトリミングしたり、追加のシステムガイダンスを注入したりできます。

## エラーとリカバリー

### エラーハンドラー

すべての `Runner` エントリポイントは、エラー種別をキーとする dict である `error_handlers` を受け付けます。サポートされるキーは `"max_turns"` と `"model_refusal"` です。`MaxTurnsExceeded` または `ModelRefusalError` を発生させる代わりに、制御された最終出力を返したい場合に使用します。

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

フォールバック出力を会話履歴に追加したくない場合は、`include_in_history=False` を設定します。

モデルの拒否時に `ModelRefusalError` で実行を終了するのではなく、アプリケーション固有のフォールバックを生成したい場合は、`"model_refusal"` を使用します。

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

## 永続的実行の統合と人間参加型

ツール承認の一時停止/再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の統合は、実行が長い待機、リトライ、またはプロセス再起動をまたぐ可能性がある場合の永続的なオーケストレーション向けです。

### Dapr

Agents SDK の [Dapr](https://dapr.io) Diagrid 統合を使用すると、人間参加型のサポートにより障害から自動的に復旧する、永続的で長時間実行されるエージェントを実行できます。Dapr はベンダーニュートラルな [CNCF](https://cncf.io) ワークフローオーケストレーターです。Dapr と OpenAI エージェントの開始方法は [こちら](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai) です。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使用すると、人間参加型タスクを含む、永続的で長時間実行されるワークフローを実行できます。長時間実行タスクを完了するために Temporal と Agents SDK が実際に連携するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認できます。また、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) を参照してください。

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使用すると、人間による承認、ハンドオフ、セッション管理を含む、軽量で永続的なエージェントを利用できます。この統合では Restate の単一バイナリランタイムが依存関係として必要であり、エージェントをプロセス/コンテナまたはサーバーレス関数として実行することをサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使用すると、失敗や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行されるエージェント、人間参加型ワークフロー、ハンドオフをサポートします。同期メソッドと非同期メソッドの両方をサポートします。この統合に必要なのは SQLite または Postgres データベースのみです。詳細は統合 [リポジトリ](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を発生させます。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。その他すべての具体的な例外の派生元となる汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: この例外は、エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えたときに発生します。これは、指定された対話ターン数の範囲内でエージェントがタスクを完了できなかったことを示します。制限を無効にするには、`max_turns=None` を設定します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: この例外は、基盤となるモデル（LLM）が予期しない、または無効な出力を生成したときに発生します。これには次のものが含まれます。
    -   不正な形式の JSON: モデルが、特に特定の `output_type` が定義されている場合に、ツール呼び出しまたは直接出力で不正な形式の JSON 構造を提供する場合。
    -   予期しないツール関連の失敗: モデルが期待される方法でツールを使用できない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: この例外は、関数ツール呼び出しが設定されたタイムアウトを超え、そのツールが `timeout_behavior="raise_exception"` を使用している場合に発生します。
-   [`UserError`][agents.exceptions.UserError]: この例外は、SDK を使用してコードを書いているあなたが、SDK の使用中にエラーを起こした場合に発生します。通常は、不適切なコード実装、無効な設定、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: この例外は、入力ガードレールまたは出力ガードレールの条件がそれぞれ満たされた場合に発生します。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。