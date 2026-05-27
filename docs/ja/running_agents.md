---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを通じてエージェントを実行できます。3 つの選択肢があります。

1. [`Runner.run()`][agents.run.Runner.run] は非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] は同期メソッドで、内部的には単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] は非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリーミングします。

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

`Runner` の run メソッドを使用するときは、開始エージェントと入力を渡します。入力には以下を指定できます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力項目のリスト
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]

その後、runner はループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、実行結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。このターン制限を無効にするには、`max_turns=None` を渡します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型のテキスト出力が生成され、ツール呼び出しがないことです。

### ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行に関する完全な情報が含まれます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意のヘルパー）

OpenAI Responses WebSocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。WebSocket セッションヘルパーは接続の再利用に推奨されますが、必須ではありません。

これは WebSocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

具体的なモデルオブジェクトやカスタムプロバイダーに関するトランスポート選択ルールと注意点については、[モデル](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: セッションヘルパーなし（動作します）

WebSocket トランスポートだけを使用したく、SDK に共有プロバイダー / セッションを管理させる必要がない場合に使用します。

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

##### パターン 2: `responses_websocket_session()` の使用（複数ターンの再利用に推奨）

複数の実行にわたって共有の WebSocket 対応プロバイダーと `RunConfig` を使用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します（同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含みます）。

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

コンテキストを抜ける前に、ストリーミングされた実行結果の消費を完了してください。WebSocket リクエストがまだ実行中の状態でコンテキストを抜けると、共有接続が強制的に閉じられる可能性があります。

長い推論ターンで WebSocket の keepalive タイムアウトに達する場合は、`ping_timeout` を増やすか、`ping_timeout=None` を設定してハートビートタイムアウトを無効にしてください。WebSocket のレイテンシより信頼性が重要な実行では、HTTP/SSE トランスポートを使用してください。

### 実行設定

`run_config` パラメーターを使用すると、エージェント実行に関するいくつかのグローバル設定を構成できます。

#### 一般的な実行設定カテゴリー

各エージェント定義を変更せずに単一の実行の動作を上書きするには、`RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent が持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名の検索に使用するモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際のセッションレベルのデフォルト（たとえば `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions を使用する際、各ターンの前に新しいユーザー入力をセッション履歴とどのようにマージするかをカスタマイズします。コールバックは同期でも非同期でもかまいません。

##### ガードレール、ハンドオフ、モデル入力の整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフにまだ入力フィルターがない場合に、すべてのハンドオフに適用するグローバル入力フィルターです。入力フィルターを使用すると、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェントを呼び出す前に、以前のトランスクリプトを単一のアシスタントメッセージに折りたたむオプトインのベータ機能です。ネストされたハンドオフを安定化させている間、この機能はデフォルトで無効です。有効にするには `True` に設定し、未加工のトランスクリプトをそのまま渡すには `False` のままにします。すべての [Runner メソッド][agents.run.Runner] は、渡されない場合に自動的に `RunConfig` を作成するため、クイックスタートとコード例ではデフォルトでオフのままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個別のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を通じてこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` にオプトインした場合に、正規化されたトランスクリプト（履歴 + ハンドオフ項目）を受け取る任意の callable です。次のエージェントに転送する入力項目の正確なリストを返す必要があり、完全なハンドオフフィルターを書かずに組み込みの要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力項目）を編集するフックです。たとえば、履歴をトリミングしたり、システムプロンプトを挿入したりできます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が以前の出力を次ターンのモデル入力に変換するときに、推論項目 ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体で [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]: 実行ごとのトレーシング API キーなど、トレースエクスポート設定を上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに、LLM やツール呼び出しの入力 / 出力など、潜在的に機密性の高いデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することをおすすめします。グループ ID は、複数の実行間でトレースを関連付けるための任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

##### ツール実行、承認、ツールエラーの動作

-   [`tool_execution`][agents.run.RunConfig.tool_execution]: 一度に実行する関数ツール数を制限するなど、ローカルツール呼び出しに対する SDK 側の実行動作を設定します。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フロー中にツール呼び出しが拒否された場合に、モデルに表示されるメッセージをカスタマイズします。

ネストされたハンドオフは、オプトインのベータ機能として利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで `handoff(..., nest_handoff_history=True)` を設定すると、折りたたみトランスクリプト動作を有効にできます。未加工のトランスクリプトを保持したい場合（デフォルト）は、フラグを未設定のままにするか、必要に応じて会話を正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定します。カスタムマッパーを書かずに、生成される要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### 実行設定の詳細

##### `tool_execution`

実行に対してローカル関数ツールの同時実行数を SDK に制限させたい場合は、`tool_execution` を使用します。

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

`max_function_tool_concurrency=None` はデフォルトの動作を維持します。モデルが 1 ターンで複数の関数ツール呼び出しを生成した場合、SDK は生成されたすべてのローカル関数ツール呼び出しを開始します。整数値を設定すると、それらのローカル関数ツールのうち同時に実行される数に上限を設けられます。

これはプロバイダー側の [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] とは別のものです。`parallel_tool_calls` は、モデルが 1 つのレスポンスで複数のツール呼び出しを生成できるかどうかを制御します。`tool_execution.max_function_tool_concurrency` は、モデルがそれらを生成した後に、SDK がローカル関数ツール呼び出しをどのように実行するかを制御します。

##### `tool_error_formatter`

承認フローでツール呼び出しが拒否されたときにモデルへ返されるメッセージをカスタマイズするには、`tool_error_formatter` を使用します。

フォーマッターは、以下を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`: エラーカテゴリーです。現在は `"approval_rejected"` です。
-   `tool_type`: ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`、または `"custom"`）です。
-   `tool_name`: ツール名です。
-   `call_id`: ツール呼び出し ID です。
-   `default_message`: SDK のデフォルトのモデル表示メッセージです。
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
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

##### `reasoning_item_id_policy`

`reasoning_item_id_policy` は、runner が履歴を引き継ぐとき（たとえば `RunResult.to_input_list()` やセッションに基づく実行を使用する場合）に、推論項目を次ターンのモデル入力へどのように変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）: 推論項目 ID を保持します。
-   `"omit"`: 生成される次ターンの入力から推論項目 ID を取り除きます。

`"omit"` は主に、推論項目が `id` 付きで送信されているものの、必須の後続項目がない場合に発生する Responses API 400 エラーの一群に対する、オプトインの緩和策として使用します（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が以前の出力からフォローアップ入力を構築するマルチターンのエージェント実行で発生する可能性があります（セッション永続化、サーバー管理の会話差分、ストリーミング / 非ストリーミングのフォローアップターン、再開パスを含みます）。推論項目 ID が保持されている一方で、プロバイダーがその ID を対応する後続項目とペアのままにすることを要求する場合です。

`reasoning_item_id_policy="omit"` を設定すると、推論コンテンツは保持したまま推論項目の `id` を取り除くため、SDK が生成するフォローアップ入力でその API 不変条件がトリガーされることを回避できます。

スコープに関する注記:

-   これは、SDK がフォローアップ入力を構築するときに SDK によって生成 / 転送される推論項目のみを変更します。
-   ユーザーが指定した初期入力項目は書き換えません。
-   `call_model_input_filter` は、このポリシー適用後でも意図的に推論 ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

状態を次のターンへ引き継ぐ一般的な方法は 4 つあります。

| 戦略 | 状態の保存場所 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` からのリストに次のユーザーメッセージを加えたもの |
| `session` | ストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい名前付きのサーバー側会話 | 同じ `conversation_id` と新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作成しない、軽量なサーバー管理の継続 | `result.last_response_id` と新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API を使用している場合にのみ適用されます。ほとんどのアプリケーションでは、会話ごとに 1 つの永続化戦略を選択します。意図的に両方のレイヤーを調整している場合を除き、クライアント管理の履歴と OpenAI 管理の状態を混在させると、コンテキストが重複する可能性があります。

!!! note

    セッション永続化は、同じ実行内でサーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）と
    組み合わせることはできません。呼び出しごとに 1 つのアプローチを選択してください。

### 会話 / チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行される（したがって 1 回以上の LLM 呼び出しが行われる）場合がありますが、チャット会話における単一の論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行してから出力を生成します。

エージェント実行の終了時に、ユーザーに何を表示するかを選択できます。たとえば、エージェントによって生成されたすべての新しい項目をユーザーに表示することも、最終出力だけを表示することもできます。いずれの場合でも、その後ユーザーが追加の質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して次のターンの入力を取得し、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に処理できます。

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

Sessions は自動的に以下を行います。

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なるセッション ID ごとに別々の会話を維持します

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに処理する代わりに、OpenAI の会話状態機能にサーバー側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを手動で再送信することなく、会話履歴を保持できます。以下のどちらのサーバー管理アプローチでも、各リクエストでは新しいターンの入力だけを渡し、保存した ID を再利用します。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

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

もう 1 つの選択肢は **レスポンスチェーン** で、各ターンを前のターンのレスポンス ID に明示的にリンクします。

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

実行が承認のために一時停止し、[`RunState`][agents.run_state.RunState] から再開した場合、
SDK は保存された `conversation_id` / `previous_response_id` / `auto_previous_response_id`
設定を保持するため、再開されたターンは同じサーバー管理の会話で続行されます。

`conversation_id` と `previous_response_id` は同時に使用できません。システム間で共有できる名前付きの会話リソースが必要な場合は `conversation_id` を使用します。あるターンから次のターンへ最も軽量な Responses API の継続基本コンポーネントが必要な場合は `previous_response_id` を使用します。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動的に再試行します。サーバー管理の
    会話実行では、再試行前に内部の会話トラッカー入力を巻き戻し、同じ準備済み項目を
    クリーンに再送信できるようにします。

    ローカルセッションベースの実行（`conversation_id`、
    `previous_response_id`、または `auto_previous_response_id` と組み合わせることはできません）では、SDK は再試行後の重複した履歴エントリを減らすために、
    直近に永続化された入力項目のベストエフォートなロールバックも実行します。

    この互換性再試行は、`ModelSettings.retry` を設定していない場合でも発生します。モデルリクエストに対するより広範なオプトインの再試行動作については、[Runner 管理の再試行](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

モデル呼び出しの直前にモデル入力を編集するには、`call_model_input_filter` を使用します。このフックは現在のエージェント、コンテキスト、結合された入力項目（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力項目のリストでなければなりません。それ以外の形状を返すと `UserError` が発生します。

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

runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元のリストをその場で変更することなく、トリミング、置換、並べ替えができます。

セッションを使用している場合、`call_model_input_filter` はセッション履歴がすでに読み込まれ、現在のターンとマージされた後に実行されます。その前段のマージ手順自体をカスタマイズしたい場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用してください。

`conversation_id`、`previous_response_id`、または `auto_previous_response_id` を使用して OpenAI のサーバー管理の会話状態を使用している場合、このフックは次の Responses API 呼び出し用に準備されたペイロード上で実行されます。そのペイロードは、以前の履歴全体の再生ではなく、すでに新しいターンの差分のみを表している場合があります。返した項目だけが、そのサーバー管理の継続に対して送信済みとしてマークされます。

機密データの編集、長い履歴のトリミング、追加のシステムガイダンスの挿入を行うには、実行ごとに `run_config` でこのフックを設定します。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする dict である `error_handlers` を受け取ります。サポートされるキーは `"max_turns"` と `"model_refusal"` です。`MaxTurnsExceeded` または `ModelRefusalError` を発生させる代わりに、制御された最終出力を返したい場合に使用します。

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

モデル拒否が `ModelRefusalError` で実行を終了するのではなく、アプリケーション固有のフォールバックを生成すべき場合は、`"model_refusal"` を使用します。

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

## 耐久実行インテグレーションと human-in-the-loop

ツール承認の一時停止 / 再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下のインテグレーションは、実行が長い待機、再試行、またはプロセス再起動をまたぐ可能性がある場合の耐久性のあるオーケストレーション向けです。

### Dapr

Agents SDK の [Dapr](https://dapr.io) Diagrid インテグレーションを使用すると、human-in-the-loop サポート付きで障害から自動的に復旧する、耐久性のある長時間実行エージェントを実行できます。Dapr はベンダーニュートラルな [CNCF](https://cncf.io) ワークフローオーケストレーターです。Dapr と OpenAI エージェントの始め方は [こちら](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai) です。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) インテグレーションを使用すると、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間実行タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) インテグレーションを使用すると、人間による承認、ハンドオフ、セッション管理を含む、軽量で耐久性のあるエージェントを利用できます。このインテグレーションは依存関係として Restate の単一バイナリランタイムを必要とし、エージェントをプロセス / コンテナまたはサーバーレス関数として実行することをサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) インテグレーションを使用すると、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期メソッドと非同期メソッドの両方をサポートします。このインテグレーションに必要なのは SQLite または Postgres データベースのみです。詳細はインテグレーションの [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を発生させます。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。他のすべての具体的な例外の派生元となる汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: この例外は、エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に発生します。エージェントが指定された相互作用ターン数内にタスクを完了できなかったことを示します。制限を無効にするには `max_turns=None` を設定します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: この例外は、基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには以下が含まれる場合があります。
    -   不正な形式の JSON: モデルがツール呼び出しまたは直接出力で不正な形式の JSON 構造を提供した場合、特に特定の `output_type` が定義されている場合です。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できなかった場合です
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: この例外は、関数ツール呼び出しが設定されたタイムアウトを超え、そのツールが `timeout_behavior="raise_exception"` を使用している場合に発生します。
-   [`UserError`][agents.exceptions.UserError]: この例外は、あなた（SDK を使用してコードを書いている人）が SDK の使用中にエラーを起こした場合に発生します。通常、誤ったコード実装、無効な設定、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: この例外は、それぞれ入力ガードレールまたは出力ガードレールの条件が満たされた場合に発生します。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終レスポンスをチェックします。