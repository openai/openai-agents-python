---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。選択肢は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部では単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信されるたびにそれらのイベントをストリーミングします。

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

`Runner` の run メソッドを使用するときは、開始エージェントと入力を渡します。入力には次を使用できます:

-   文字列 (ユーザーメッセージとして扱われます)、
-   OpenAI Responses API 形式の入力項目のリスト、または
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]。

その後、Runner はループを実行します:

1. 現在の入力を使って、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、実行結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。このターン制限を無効にするには、`max_turns=None` を渡します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型のテキスト出力を生成し、ツール呼び出しがないことです。

### ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が含まれます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳しくは [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート (任意のヘルパー)

OpenAI Responses WebSocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。WebSocket セッションヘルパーは接続の再利用に推奨されますが、必須ではありません。

これは WebSocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

具体的なモデルオブジェクトやカスタムプロバイダーに関するトランスポート選択ルールと注意点については、[モデル](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: セッションヘルパーなし (動作可)

WebSocket トランスポートだけが必要で、SDK に共有プロバイダー / セッションを管理させる必要がない場合に使用します。

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

このパターンは単一の実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼び出す場合、同じ `RunConfig` / プロバイダーインスタンスを手動で再利用しない限り、実行ごとに再接続される可能性があります。

##### パターン 2: `responses_websocket_session()` の使用 (複数ターンの再利用に推奨)

複数の実行にまたがって共有の WebSocket 対応プロバイダーと `RunConfig` を使いたい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します (同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含みます)。

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

コンテキストを抜ける前に、ストリーミングされた実行結果の消費を完了してください。WebSocket リクエストがまだ処理中の間にコンテキストを抜けると、共有接続が強制的に閉じられる可能性があります。

長い推論ターンで WebSocket の keepalive タイムアウトに達する場合は、`ping_timeout` を増やすか、`ping_timeout=None` を設定してハートビートタイムアウトを無効にしてください。WebSocket レイテンシより信頼性が重要な実行には、HTTP/SSE トランスポートを使用してください。

### 実行設定

`run_config` パラメーターを使用すると、エージェント実行のいくつかのグローバル設定を構成できます:

#### 一般的な実行設定カテゴリー

各エージェント定義を変更せずに単一の実行の動作を上書きするには、`RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各エージェントが持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を検索するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際のセッションレベルのデフォルト (たとえば `SessionSettings(limit=...)`) を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions を使用する際、各ターンの前に新しいユーザー入力をセッション履歴とどのようにマージするかをカスタマイズします。コールバックは同期または非同期にできます。

##### ガードレール、ハンドオフ、モデル入力の整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフにまだ設定されていない場合に、すべてのハンドオフへ適用するグローバル入力フィルターです。入力フィルターを使うと、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 前のトランスクリプトを単一のアシスタントメッセージに折りたたんでから次のエージェントを呼び出す、オプトインのベータ機能です。ネストされたハンドオフを安定化している間、これはデフォルトで無効になっています。有効にするには `True` に設定し、未加工のトランスクリプトをそのまま渡すには `False` のままにします。[Runner メソッド][agents.run.Runner]は、`RunConfig` を渡さない場合に自動的に作成するため、クイックスタートとコード例ではデフォルトがオフのままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個々のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を介してこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` にオプトインしたときに、正規化されたトランスクリプト (履歴 + ハンドオフ項目) を受け取る任意の呼び出し可能オブジェクトです。次のエージェントへ転送する入力項目の正確なリストを返す必要があり、完全なハンドオフフィルターを書かずに組み込みのサマリーを置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出しの直前に、完全に準備されたモデル入力 (instructions と入力項目) を編集するためのフックです。たとえば、履歴をトリミングしたり、システムプロンプトを注入したりできます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: Runner が以前の出力を次ターンのモデル入力に変換する際、推論項目 ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体で [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]: 実行ごとのトレーシング API キーなどのトレースエクスポート設定を上書きするには、[`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入力 / 出力など、潜在的に機密性の高いデータをトレースに含めるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することをお勧めします。グループ ID は、複数の実行にまたがってトレースをリンクできる任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

##### ツール実行、承認、ツールエラー動作

-   [`tool_execution`][agents.run.RunConfig.tool_execution]: 同時に実行される関数ツール数を制限するなど、ローカルツール呼び出しに対する SDK 側の実行動作を構成します。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フロー中にツール呼び出しが拒否された場合に、モデルから見えるメッセージをカスタマイズします。

ネストされたハンドオフは、オプトインのベータ機能として利用できます。トランスクリプトを折りたたむ動作を有効にするには `RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで有効にするには `handoff(..., nest_handoff_history=True)` を設定します。未加工のトランスクリプトを保持したい場合 (デフォルト) は、フラグを未設定のままにするか、必要な形で会話を正確に転送する `handoff_input_filter` (または `handoff_history_mapper`) を指定します。カスタムマッパーを書かずに、生成されるサマリーで使われるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します (デフォルトを復元するには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] を呼び出します)。

#### 実行設定の詳細

##### `tool_execution`

`tool_execution` は、実行中のローカル関数ツールの並行実行数を SDK で制限したい場合に使用します。

```python
from agents import Agent, RunConfig, Runner, ToolExecutionConfig

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Run the required tool calls.",
    run_config=RunConfig(
        tool_execution=ToolExecutionConfig(max_function_tool_concurrency=2),
    ),
)
```

`max_function_tool_concurrency=None` はデフォルト動作を維持します。モデルが 1 ターンで複数の関数ツール呼び出しを生成すると、SDK は生成されたすべてのローカル関数ツール呼び出しを開始します。整数値を設定すると、それらのローカル関数ツールのうち同時に実行される数に上限を設けられます。

これはプロバイダー側の [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] とは別です。`parallel_tool_calls` は、モデルが単一のレスポンスで複数のツール呼び出しを生成できるかどうかを制御します。`tool_execution.max_function_tool_concurrency` は、モデルがそれらを生成した後に SDK がローカル関数ツール呼び出しをどのように実行するかを制御します。

##### `tool_error_formatter`

`tool_error_formatter` は、承認フローでツール呼び出しが拒否された場合にモデルへ返されるメッセージをカスタマイズするために使用します。

フォーマッターは、次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります:

-   `kind`: エラーカテゴリーです。現在は `"approval_rejected"` です。
-   `tool_type`: ツールランタイムです (`"function"`、`"computer"`、`"shell"`、`"apply_patch"`、または `"custom"`)。
-   `tool_name`: ツール名です。
-   `call_id`: ツール呼び出し ID です。
-   `default_message`: SDK のデフォルトのモデルから見えるメッセージです。
-   `run_context`: アクティブな実行コンテキストラッパーです。

文字列を返すとメッセージを置き換え、`None` を返すと SDK のデフォルトを使用します。

```python
from agents import Agent, RunConfig, Runner, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind == "approval_rejected":
        return (
            f"Tool call '{args.tool_name}' was rejected by a human reviewer. "
            "Ask for confirmation or propose a safer alternative."
        )
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

##### `reasoning_item_id_policy`

`reasoning_item_id_policy` は、Runner が履歴を引き継ぐ際 (たとえば `RunResult.to_input_list()` やセッションベースの実行を使用する場合) に、推論項目を次ターンのモデル入力へどのように変換するかを制御します。

-   `None` または `"preserve"` (デフォルト): 推論項目 ID を保持します。
-   `"omit"`: 生成される次ターン入力から推論項目 ID を削除します。

`"omit"` は主に、推論項目が `id` を持つ一方で必要な後続項目なしに送信される場合に発生する、一部の Responses API 400 エラーに対するオプトインの緩和策として使用します (例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`)。

これは、SDK が以前の出力からフォローアップ入力を構築する複数ターンのエージェント実行 (セッション永続化、サーバー管理の会話差分、ストリーミング / 非ストリーミングのフォローアップターン、再開パスを含む) で、推論項目 ID が保持されているものの、プロバイダーがその ID が対応する後続項目とペアのままであることを要求する場合に発生する可能性があります。

`reasoning_item_id_policy="omit"` を設定すると、推論内容は保持しつつ推論項目の `id` を削除するため、SDK が生成するフォローアップ入力でその API 不変条件に触れることを避けられます。

適用範囲に関する注意:

-   これは、SDK がフォローアップ入力を構築するときに生成 / 転送する推論項目のみを変更します。
-   ユーザーが指定した初期入力項目は書き換えません。
-   `call_model_input_filter` は、このポリシーが適用された後でも、推論 ID を意図的に再導入できます。

## 状態と会話の管理

### メモリ戦略の選択

状態を次のターンに引き継ぐ一般的な方法は 4 つあります:

| 戦略 | 状態の保存先 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` からのリストと次のユーザーメッセージ |
| `session` | アプリのストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい名前付きのサーバー側会話 | 同じ `conversation_id` と、新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作成しない、軽量なサーバー管理の継続 | `result.last_response_id` と、新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API を使用している場合にのみ適用されます。ほとんどのアプリケーションでは、会話ごとに 1 つの永続化戦略を選択してください。クライアント管理の履歴と OpenAI 管理の状態を混在させると、両方の層を意図的に整合させている場合を除き、コンテキストが重複する可能性があります。

!!! note

    セッション永続化は、サーバー管理の会話設定
    (`conversation_id`、`previous_response_id`、または `auto_previous_response_id`) と
    同じ実行で併用できません。呼び出しごとに 1 つのアプローチを選択してください。

### 会話 / チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行される (したがって 1 回以上の LLM 呼び出しが発生する) 可能性がありますが、チャット会話における 1 つの論理ターンを表します。たとえば:

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner の実行: 1 つ目のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフし、2 つ目のエージェントがさらにツールを実行してから出力を生成します。

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。たとえば、エージェントによって生成されたすべての新しい項目をユーザーに表示することも、最終出力だけを表示することもできます。いずれの場合でも、その後ユーザーがフォローアップの質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用すると、次のターンの入力を取得して会話履歴を手動で管理できます:

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

#### Sessions による自動会話管理

よりシンプルな方法として、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に処理できます:

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

Sessions は自動的に次を行います:

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なるセッション ID ごとに別々の会話を維持します

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。


#### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに処理する代わりに、OpenAI の会話状態機能にサーバー側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを手動で再送信せずに会話履歴を保持できます。以下のいずれのサーバー管理方式でも、各リクエストでは新しいターンの入力のみを渡し、保存した ID を再利用します。詳細は [OpenAI 会話状態ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間で状態を追跡する方法を 2 つ提供しています:

##### 1. `conversation_id` の使用

まず OpenAI Conversations API を使用して会話を作成し、その ID を以降のすべての呼び出しで再利用します:

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

別の選択肢は **レスポンスチェーン** です。この方法では、各ターンが前のターンのレスポンス ID に明示的にリンクします。

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
SDK は保存済みの `conversation_id` / `previous_response_id` / `auto_previous_response_id`
設定を保持するため、再開されたターンは同じサーバー管理の会話で続行されます。

`conversation_id` と `previous_response_id` は相互に排他的です。システム間で共有できる名前付きの会話リソースが必要な場合は `conversation_id` を使用します。1 つのターンから次のターンへの最も軽量な Responses API の継続用基本コンポーネントが必要な場合は `previous_response_id` を使用します。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動的にリトライします。サーバー管理の
    会話実行では、リトライ前に内部の会話トラッカー入力を巻き戻すため、
    同じ準備済み項目をクリーンに再送信できます。

    ローカルのセッションベース実行 (`conversation_id`、
    `previous_response_id`、または `auto_previous_response_id` と併用できません) では、SDK は
    リトライ後の履歴エントリの重複を減らすために、最近永続化された入力項目のベストエフォートの
    ロールバックも実行します。

    この互換性のためのリトライは、`ModelSettings.retry` を設定していない場合でも行われます。モデルリクエストに対する
    より広範なオプトインのリトライ動作については、[Runner 管理のリトライ](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

`call_model_input_filter` は、モデル呼び出しの直前にモデル入力を編集するために使用します。このフックは、現在のエージェント、コンテキスト、結合済みの入力項目 (存在する場合はセッション履歴を含む) を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力項目のリストである必要があります。その他の形状を返すと `UserError` が発生します。

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

Runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元のリストをインプレースで変更せずに、トリミング、置換、並べ替えを行えます。

セッションを使用している場合、`call_model_input_filter` はセッション履歴がすでに読み込まれ、現在のターンとマージされた後に実行されます。その前段のマージ手順自体をカスタマイズしたい場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。

`conversation_id`、`previous_response_id`、または `auto_previous_response_id` を使って OpenAI のサーバー管理の会話状態を使用している場合、フックは次の Responses API 呼び出し用に準備されたペイロードに対して実行されます。そのペイロードは、以前の履歴を完全に再生するものではなく、すでに新しいターンの差分のみを表している場合があります。返した項目だけが、そのサーバー管理の継続に対して送信済みとしてマークされます。

`run_config` を介して実行ごとにフックを設定し、機密データのマスク、長い履歴のトリミング、追加のシステムガイダンスの注入を行えます。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする辞書 `error_handlers` を受け取ります。サポートされるキーは `"max_turns"` と `"model_refusal"` です。`MaxTurnsExceeded` や `ModelRefusalError` を発生させる代わりに、制御された最終出力を返したい場合に使用します。

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

モデルの拒否で `ModelRefusalError` により実行を終了する代わりに、アプリケーション固有のフォールバックを生成したい場合は `"model_refusal"` を使用します。

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

## 永続的な実行統合と human-in-the-loop

ツール承認の一時停止 / 再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の統合は、実行が長い待機、リトライ、またはプロセス再起動をまたぐ可能性がある場合の永続的なオーケストレーション向けです。

### Dapr

Agents SDK の [Dapr](https://dapr.io) Diagrid 統合を使用すると、human-in-the-loop サポートを備え、障害から自動復旧する永続的で長時間実行されるエージェントを実行できます。Dapr はベンダー中立の [CNCF](https://cncf.io) ワークフローオーケストレーターです。Dapr と OpenAI エージェントの始め方は [こちら](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai) を参照してください。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使用すると、human-in-the-loop タスクを含む、永続的で長時間実行されるワークフローを実行できます。Temporal と Agents SDK が実際に連携して長時間実行タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認できます。また、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) を参照してください。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使用すると、人間による承認、ハンドオフ、セッション管理を含む、軽量で永続的なエージェントを実行できます。この統合では依存関係として Restate の単一バイナリランタイムが必要で、エージェントをプロセス / コンテナまたはサーバーレス関数として実行することをサポートしています。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使用すると、障害や再起動をまたいで進行状況を保持する信頼性の高いエージェントを実行できます。長時間実行されるエージェント、human-in-the-loop ワークフロー、ハンドオフをサポートしています。同期メソッドと非同期メソッドの両方をサポートしています。この統合に必要なのは SQLite または Postgres データベースだけです。詳細は統合の [リポジトリ](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を発生させます。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。他のすべての個別例外の派生元となる汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: この例外は、エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に発生します。エージェントが指定された対話ターン数内にタスクを完了できなかったことを示します。制限を無効にするには `max_turns=None` を設定します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: この例外は、基盤モデル (LLM) が予期しない出力または無効な出力を生成した場合に発生します。これには次が含まれます:
    -   不正な JSON: モデルがツール呼び出しまたは直接出力で不正な JSON 構造を提供した場合。特に特定の `output_type` が定義されている場合です。
    -   予期しないツール関連の失敗: モデルが期待される方法でツールを使用できない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: この例外は、関数ツール呼び出しが構成されたタイムアウトを超え、そのツールが `timeout_behavior="raise_exception"` を使用している場合に発生します。
-   [`UserError`][agents.exceptions.UserError]: この例外は、SDK を使ってコードを書いている方が SDK の使用中に誤りをした場合に発生します。これは通常、不正なコード実装、無効な設定、または SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: この例外は、入力ガードレールまたは出力ガードレールの条件がそれぞれ満たされた場合に発生します。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。