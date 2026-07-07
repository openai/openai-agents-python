---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run] は非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] は同期メソッドで、内部では単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] は非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。これはストリーミングモードで LLM を呼び出し、受信したイベントをそのままストリーミングします。

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

詳しくは [実行結果ガイド](results.md) を参照してください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使用するときは、開始エージェントと入力を渡します。入力には次を指定できます。

-   文字列（ユーザーメッセージとして扱われます）、
-   OpenAI Responses API 形式の入力項目のリスト、または
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]。

runner は次にループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、実行結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。このターン制限を無効にするには `max_turns=None` を渡します。

!!! note

    LLM の出力が「最終出力」とみなされる条件は、望ましい型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が含まれます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳しくは [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意のヘルパー）

OpenAI Responses websocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。接続の再利用には websocket セッションヘルパーの使用を推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルール、および具象モデルオブジェクトやカスタムプロバイダーに関する注意点については、[モデル](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: セッションヘルパーなし（動作可）

websocket トランスポートだけが必要で、共有プロバイダーやセッションを SDK に管理させる必要がない場合に使用します。

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

このパターンは単発の実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼び出す場合、同じ `RunConfig` / プロバイダーインスタンスを手動で再利用しない限り、各実行で再接続される可能性があります。

##### パターン 2: `responses_websocket_session()` の使用（複数ターンでの再利用に推奨）

複数の実行にわたって共有の websocket 対応プロバイダーと `RunConfig` を使用したい場合（同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含む）は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します。

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

コンテキストを抜ける前に、ストリーミングされた実行結果の消費を完了してください。websocket リクエストがまだ進行中の状態でコンテキストを抜けると、共有接続が強制的に閉じられる可能性があります。

長い推論ターンで websocket の keepalive タイムアウトに達する場合は、`ping_timeout` を増やすか、`ping_timeout=None` を設定してハートビートタイムアウトを無効にしてください。websocket のレイテンシより信頼性が重要な実行では、HTTP/SSE トランスポートを使用してください。

### 実行設定

`run_config` パラメーターを使用すると、エージェント実行の一部のグローバル設定を構成できます。

#### 一般的な実行設定カテゴリー

各エージェント定義を変更せずに単一の実行の挙動を上書きするには、`RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent が持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名の検索に使用するモデルプロバイダーです。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際のセッションレベルのデフォルト（例: `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: セッションを使用する場合に、各ターンの前に新しいユーザー入力をセッション履歴とどのようにマージするかをカスタマイズします。コールバックは同期または非同期にできます。

##### ガードレール、ハンドオフ、モデル入力の整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに入力フィルターがまだない場合に、すべてのハンドオフへ適用するグローバル入力フィルターです。入力フィルターを使用すると、新しいエージェントへ送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェントを呼び出す前に、以前のトランスクリプトを単一の assistant メッセージに折りたたむオプトインのベータ機能です。ネストされたハンドオフを安定化している間、これはデフォルトで無効です。有効にするには `True` に設定し、raw トランスクリプトをそのまま渡すには `False` のままにします。すべての [Runner メソッド][agents.run.Runner] は、渡されていない場合に自動的に `RunConfig` を作成するため、クイックスタートやコード例ではデフォルトがオフのままになります。また、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個別のハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を通じてこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` をオプトインしたときに、正規化されたトランスクリプト（履歴 + ハンドオフ項目）を受け取る任意の callable です。次のエージェントへ転送する入力項目の正確なリストを返す必要があり、完全なハンドオフフィルターを書かずに組み込みの要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力項目）を編集するためのフックです。たとえば、履歴をトリミングしたり、システムプロンプトを注入したりできます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が以前の出力を次ターンのモデル入力に変換するときに、reasoning 項目の ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]: 実行ごとのトレーシング API キーなど、トレースエクスポート設定を上書きするには [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに LLM やツール呼び出しの入力/出力など、機微である可能性のあるデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は、複数の実行にわたってトレースを関連付けられる任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

##### ツール実行、承認、ツールエラーの挙動

-   [`tool_execution`][agents.run.RunConfig.tool_execution]: 一度に実行される関数ツールの数を制限するなど、ローカルツール呼び出しに対する SDK 側の実行挙動を設定します。
-   [`tool_not_found_behavior`][agents.run.RunConfig.tool_not_found_behavior]: モデルによって出力された未解決の関数ツール呼び出しを runner がどのように扱うかを設定します。デフォルトでは `ModelBehaviorError` が発生します。代わりにモデルから見えるエラー出力を返すようにオプトインできます。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認拒否やオプトインのツール未検出出力など、モデルから見えるツールエラーメッセージをカスタマイズします。

ネストされたハンドオフはオプトインのベータ機能として利用できます。折りたたみトランスクリプトの挙動を有効にするには `RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで有効にするには `handoff(..., nest_handoff_history=True)` を設定します。raw トランスクリプトを維持したい場合（デフォルト）は、フラグを未設定のままにするか、必要なとおりに会話を転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定します。カスタムマッパーを書かずに、生成される要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトを復元するには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] を呼び出します）。

#### 実行設定の詳細

##### `tool_execution`

実行におけるローカル関数ツールの同時実行数の制限など、ローカル関数ツールに対する SDK 側の挙動を設定したい場合は、`tool_execution` を使用します。

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

`max_function_tool_concurrency=None` はデフォルトの挙動を維持します。モデルが 1 ターンで複数の関数ツール呼び出しを出力した場合、SDK は出力されたすべてのローカル関数ツール呼び出しを開始します。整数値を設定すると、それらのローカル関数ツールのうち同時に実行される数に上限を設けられます。

これはプロバイダー側の [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] とは別です。`parallel_tool_calls` は、モデルが 1 つのレスポンスで複数のツール呼び出しを出力できるかどうかを制御します。`tool_execution.max_function_tool_concurrency` は、モデルがそれらを出力した後に、SDK がローカル関数ツール呼び出しをどのように実行するかを制御します。

`pre_approval_tool_input_guardrails=False` はデフォルトの承認フローを維持します。関数ツールに承認が必要な場合、実行はまず一時停止し、ツール入力ガードレールは承認後、実行直前にのみ実行されます。保留中の承認割り込みが出力される前に関数ツール入力ガードレールを実行したい場合は、`True` に設定します。この事前承認チェックに合格した呼び出しでも、承認後に同じ入力ガードレールが再度実行されるため、時間依存のチェックは実行前に再検証されます。

##### `tool_not_found_behavior`

デフォルトでは、モデルが現在のエージェントで利用可能な関数ツールのいずれにも一致しない関数ツール呼び出しを出力した場合、runner は `ModelBehaviorError` を発生させます。

実行を回復可能なままにしたい場合は、`tool_not_found_behavior="return_error_to_model"` を設定します。このモードでは、SDK は未解決のツール呼び出しに対する `function_call_output` を追加し、モデルを再度実行します。これにより、モデルは利用可能なツールを選択するか、そのツールを使用せずに回答できます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Handle this request with the available tools.",
    run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
)
```

このオプションは現在、未解決の関数ツール呼び出しにのみ適用されます。その他の無効なツールペイロードでは、既存のエラー挙動が引き続き使用されます。

##### `tool_error_formatter`

SDK がモデルから見えるツールエラー出力を作成するときにモデルへ返されるメッセージをカスタマイズするには、`tool_error_formatter` を使用します。

formatter は、次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`: `"approval_rejected"` や `"tool_not_found"` などのエラーカテゴリーです。
-   `tool_type`: ツールランタイム（`"function"`, `"computer"`, `"shell"`, `"apply_patch"`, または `"custom"`）です。
-   `tool_name`: ツール名です。
-   `call_id`: ツール呼び出し ID です。
-   `default_message`: SDK のデフォルトのモデルから見えるメッセージです。
-   `run_context`: アクティブな実行コンテキストラッパーです。

メッセージを置き換えるには文字列を返し、SDK のデフォルトを使用するには `None` を返します。

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

`reasoning_item_id_policy` は、runner が履歴を次へ持ち越すとき（たとえば、`RunResult.to_input_list()` やセッションに基づく実行を使用するとき）に、reasoning 項目を次ターンのモデル入力へどのように変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）: reasoning 項目 ID を保持します。
-   `"omit"`: 生成される次ターン入力から reasoning 項目 ID を削除します。

`"omit"` は主に、reasoning 項目が `id` 付きで送信されたものの、必須の後続項目がない場合に発生する Responses API 400 エラーの一種に対するオプトインの緩和策として使用します（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が以前の出力から後続入力を構築する複数ターンのエージェント実行（セッション永続化、サーバー管理の会話差分、ストリーミング/非ストリーミングの後続ターン、再開パスを含む）において、reasoning 項目 ID が保持されている一方で、プロバイダーがその ID を対応する後続項目とペアのままにすることを要求する場合に発生する可能性があります。

`reasoning_item_id_policy="omit"` を設定すると、reasoning の内容は保持しつつ reasoning 項目の `id` を削除します。これにより、SDK が生成する後続入力でその API 不変条件に抵触することを回避できます。

スコープに関する注意:

-   これは、SDK が後続入力を構築するときに生成または転送する reasoning 項目のみを変更します。
-   ユーザーが指定した初期入力項目は書き換えません。
-   このポリシーが適用された後でも、`call_model_input_filter` は意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

次のターンへ状態を持ち越す一般的な方法は 4 つあります。

| 戦略 | 状態の所在 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` からのリストに次のユーザーメッセージを加えたもの |
| `session` | アプリのストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい名前付きのサーバー側会話 | 同じ `conversation_id` と、新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作成しない、軽量なサーバー管理の継続 | `result.last_response_id` と、新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理であり、OpenAI Responses API を使用している場合にのみ適用されます。ほとんどのアプリケーションでは、会話ごとに 1 つの永続化戦略を選択してください。クライアント管理の履歴と OpenAI 管理の状態を混在させると、両方のレイヤーを意図的に調整している場合を除き、コンテキストが重複する可能性があります。

!!! note

    セッション永続化は、サーバー管理の会話設定
    （`conversation_id`, `previous_response_id`, または `auto_previous_response_id`）と同じ実行内で
    組み合わせることはできません。呼び出しごとに 1 つの方法を選択してください。

### 会話/チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行される（したがって 1 回以上の LLM 呼び出しが行われる）可能性がありますが、チャット会話における単一の論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行してから出力を生成します。

エージェントの実行が終了した時点で、ユーザーに何を表示するかを選択できます。たとえば、エージェントによって生成されたすべての新しい項目をユーザーに表示することも、最終出力だけを表示することもできます。いずれの場合も、その後ユーザーがフォローアップ質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して次ターンの入力を取得することで、会話履歴を手動で管理できます。

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

より簡単な方法として、`.to_input_list()` を手動で呼び出さずに会話履歴を自動処理するために [セッション](sessions/index.md) を使用できます。

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

セッションは次を自動的に行います。

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なるセッション ID ごとに別々の会話を維持します

詳細については、[セッションのドキュメント](sessions/index.md) を参照してください。


#### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに処理する代わりに、OpenAI の会話状態機能にサーバー側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを手動で再送信せずに会話履歴を保持できます。以下のいずれのサーバー管理アプローチでも、各リクエストでは新しいターンの入力のみを渡し、保存した ID を再利用してください。詳細については、[OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI は、ターン間で状態を追跡する 2 つの方法を提供しています。

##### 1. `conversation_id` の使用

まず OpenAI Conversations API を使用して会話を作成し、その後のすべての呼び出しでその ID を再利用します。

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

実行が承認のために一時停止し、[`RunState`][agents.run_state.RunState] から再開する場合、SDK は保存された `conversation_id` / `previous_response_id` / `auto_previous_response_id` 設定を維持するため、再開されたターンは同じサーバー管理の会話内で継続します。

`conversation_id` と `previous_response_id` は相互に排他的です。システム間で共有できる名前付きの会話リソースが必要な場合は `conversation_id` を使用します。あるターンから次のターンへ最も軽量な Responses API 継続の基本コンポーネントが必要な場合は `previous_response_id` を使用します。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動的に再試行します。サーバー管理の
    会話実行では、再試行前に内部の会話トラッカー入力を巻き戻し、
    同じ準備済み項目をクリーンに再送信できるようにします。

    ローカルのセッションベースの実行（`conversation_id`,
    `previous_response_id`, または `auto_previous_response_id` と組み合わせることはできません）では、SDK は
    再試行後に履歴エントリが重複するのを減らすため、最近永続化された入力項目についてもベストエフォートの
    ロールバックを行います。

    この互換性のための再試行は、`ModelSettings.retry` を設定していない場合でも行われます。モデルリクエストに対する
    より広範なオプトインの再試行挙動については、[Runner 管理の再試行](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

モデル呼び出しの直前にモデル入力を編集するには、`call_model_input_filter` を使用します。このフックは現在のエージェント、コンテキスト、および結合済みの入力項目（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力項目のリストでなければなりません。それ以外の形式を返すと `UserError` が発生します。

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

runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元のリストをその場で変更せずに、トリミング、置換、並べ替えができます。

セッションを使用している場合、`call_model_input_filter` はセッション履歴がすでに読み込まれ、現在のターンとマージされた後に実行されます。その前段のマージ手順自体をカスタマイズしたい場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用してください。

`conversation_id`、`previous_response_id`、または `auto_previous_response_id` を使って OpenAI のサーバー管理の会話状態を使用している場合、このフックは次の Responses API 呼び出し向けに準備されたペイロード上で実行されます。そのペイロードは、以前の履歴全体の再生ではなく、すでに新しいターンの差分のみを表している場合があります。返した項目のみが、そのサーバー管理の継続に対して送信済みとしてマークされます。

機微データのマスク、長い履歴のトリミング、追加のシステムガイダンスの注入を行うには、`run_config` を通じて実行ごとにフックを設定します。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする dict である `error_handlers` を受け取ります。サポートされているキーは `"max_turns"` と `"model_refusal"` です。`MaxTurnsExceeded` や `ModelRefusalError` を発生させる代わりに、制御された最終出力を返したい場合に使用します。

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

モデルの拒否によって `ModelRefusalError` で実行を終了するのではなく、アプリケーション固有のフォールバックを生成したい場合は、`"model_refusal"` を使用します。

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

## 耐久実行の統合とヒューマンインザループ

ツール承認の一時停止/再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。以下の統合は、実行が長い待機、再試行、またはプロセス再起動にまたがる可能性がある場合の耐久オーケストレーション向けです。

### Dapr

Agents SDK の [Dapr](https://dapr.io) Diagrid 統合を使用すると、ヒューマンインザループをサポートしつつ、障害から自動的に復旧する、耐久性のある長時間実行エージェントを実行できます。Dapr はベンダー中立の [CNCF](https://cncf.io) ワークフローオーケストレーターです。Dapr と OpenAI エージェントの利用開始は [こちら](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai) から行えます。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使用すると、ヒューマンインザループタスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間実行タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) で参照できます。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合は、人による承認、ハンドオフ、セッション管理を含む、軽量で耐久性のあるエージェントに使用できます。この統合では Restate の単一バイナリランタイムが依存関係として必要であり、エージェントをプロセス/コンテナーまたはサーバーレス関数として実行することをサポートしています。詳細については、[概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使用すると、障害や再起動をまたいで進行状況を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、ヒューマンインザループワークフロー、ハンドオフをサポートしています。同期メソッドと非同期メソッドの両方をサポートしています。この統合に必要なのは SQLite または Postgres データベースのみです。詳細については、統合の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を発生させます。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: これは SDK 内で発生するすべての例外の基底クラスです。他のすべての具体的な例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: この例外は、エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に発生します。これは、指定された対話ターン数の範囲内でエージェントがタスクを完了できなかったことを示します。制限を無効にするには `max_turns=None` を設定します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: この例外は、基盤モデル（LLM）が予期しない出力または無効な出力を生成した場合に発生します。これには次が含まれます。
    -   不正な形式の JSON: モデルがツール呼び出しまたは直接出力で不正な形式の JSON 構造を提供した場合です。特に特定の `output_type` が定義されている場合に該当します。
    -   予期しないツール関連の失敗: モデルが期待された方法でツールを使用できなかった場合です
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: この例外は、関数ツール呼び出しが設定されたタイムアウトを超え、そのツールが `timeout_behavior="raise_exception"` を使用している場合に発生します。
-   [`UserError`][agents.exceptions.UserError]: この例外は、あなた（SDK を使用してコードを書く人）が SDK の使用中に誤りを犯した場合に発生します。通常は、不正なコード実装、無効な設定、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: この例外は、入力ガードレールまたは出力ガードレールの条件がそれぞれ満たされた場合に発生します。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。