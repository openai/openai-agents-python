---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを通じてエージェントを実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run] は async で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] は sync メソッドで、内部的には単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] は async で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままストリームします。

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

`Runner` の run メソッドを使用する場合、開始エージェントと入力を渡します。入力には次のものを指定できます。

-   文字列（ユーザーメッセージとして扱われます）、
-   OpenAI Responses API 形式の入力項目のリスト、または
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]。

その後、runner はループを実行します。

1. 現在のエージェントに対して、現在の入力を用いて LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、実行結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。このターン制限を無効にするには、`max_turns=None` を渡してください。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、望ましい型のテキスト出力を生成し、ツール呼び出しがないことです。

### ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントを追加で受け取ることができます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行に関する完全な情報が含まれます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳しくは [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意のヘルパー）

OpenAI Responses websocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。接続の再利用には websocket セッションヘルパーが推奨されますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルール、および具体的なモデルオブジェクトやカスタムプロバイダーに関する注意点については、[モデル](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: セッションヘルパーなし（動作可）

websocket トランスポートだけを使いたく、SDK に共有プロバイダーやセッションを管理させる必要がない場合に使用します。

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

複数の実行にまたがって共有の websocket 対応プロバイダーと `RunConfig` を使用したい場合（同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含む）は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用してください。

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

コンテキストを終了する前に、ストリーミングされた実行結果の消費を完了してください。websocket リクエストがまだ実行中の間にコンテキストを終了すると、共有接続が強制的に閉じられる可能性があります。

長い reasoning ターンで websocket keepalive のタイムアウトが発生する場合は、`ping_timeout` を増やすか、`ping_timeout=None` を設定して heartbeat タイムアウトを無効にしてください。websocket のレイテンシより信頼性が重要な実行では、HTTP/SSE トランスポートを使用してください。

### 実行設定

`run_config` パラメーターを使用すると、エージェント実行の一部のグローバル設定を構成できます。

#### 一般的な実行設定カテゴリー

各エージェント定義を変更せずに、単一の実行の挙動を上書きするには `RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent が持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を検索するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際のセッションレベルのデフォルト（例: `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターンの前に新しいユーザー入力をセッション履歴とどのようにマージするかをカスタマイズします。コールバックは sync または async にできます。

##### ガードレール、ハンドオフ、モデル入力の整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに入力フィルターがまだない場合に、すべてのハンドオフに適用するグローバル入力フィルターです。入力フィルターを使うと、新しいエージェントに送信される入力を編集できます。詳細については [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェントを呼び出す前に、以前のトランスクリプトを 1 つの assistant メッセージに折りたたむ opt-in beta です。ネストされたハンドオフを安定化している間はデフォルトで無効です。有効にするには `True` に設定し、raw トランスクリプトをそのまま渡すには `False` のままにしてください。すべての [Runner メソッド][agents.run.Runner] は、渡されない場合に自動的に `RunConfig` を作成するため、クイックスタートとコード例ではデフォルトがオフのままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個々のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を通じてこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` を opt in したときに、正規化されたトランスクリプト（履歴 + ハンドオフ項目）を受け取る任意の callable です。次のエージェントへ転送する入力項目の正確なリストを返す必要があり、完全なハンドオフフィルターを書かずに組み込みの要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力項目）を編集するためのフックです。たとえば、履歴をトリムしたり、システムプロンプトを注入したりできます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が以前の出力を次ターンのモデル入力に変換する際に、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体で [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]: 実行ごとのトレーシング API キーなどの trace エクスポート設定を上書きするために、[`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: trace に、LLM やツール呼び出しの入力/出力など、潜在的に機密性のあるデータを含めるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、trace ID、trace group ID を設定します。少なくとも `workflow_name` を設定することをおすすめします。group ID は、複数の実行にまたがって trace をリンクできる任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべての trace に含めるメタデータです。

##### ツール実行、承認、ツールエラーの挙動

-   [`tool_execution`][agents.run.RunConfig.tool_execution]: 一度に実行される関数ツールの数を制限するなど、ローカルツール呼び出しに対する SDK 側の実行挙動を構成します。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フロー中にツール呼び出しが拒否された場合に、モデルに表示されるメッセージをカスタマイズします。

ネストされたハンドオフは opt-in beta として利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフに対して `handoff(..., nest_handoff_history=True)` を設定して、折りたたまれたトランスクリプトの挙動を有効にします。raw トランスクリプト（デフォルト）を保持したい場合は、フラグを未設定のままにするか、会話を必要どおり正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに、生成された要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

#### 実行設定の詳細

##### `tool_execution`

ある実行に対して、SDK にローカル関数ツールの同時実行数を制限させたい場合は `tool_execution` を使用します。

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

`max_function_tool_concurrency=None` はデフォルトの挙動を保持します。モデルが 1 ターンで複数の関数ツール呼び出しを発行した場合、SDK は発行されたすべてのローカル関数ツール呼び出しを開始します。同時に実行されるローカル関数ツールの数に上限を設けるには、整数値を設定します。

これはプロバイダー側の [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] とは別です。`parallel_tool_calls` は、モデルが 1 つの応答で複数のツール呼び出しを発行できるかどうかを制御します。`tool_execution.max_function_tool_concurrency` は、モデルがそれらを発行した後に SDK がローカル関数ツール呼び出しをどのように実行するかを制御します。

##### `tool_error_formatter`

承認フローでツール呼び出しが拒否されたときにモデルへ返されるメッセージをカスタマイズするには、`tool_error_formatter` を使用します。

formatter は、次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`: エラーカテゴリーです。現在これは `"approval_rejected"` です。
-   `tool_type`: ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`、または `"custom"`）です。
-   `tool_name`: ツール名です。
-   `call_id`: ツール呼び出し ID です。
-   `default_message`: SDK のデフォルトの、モデルに表示されるメッセージです。
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

`reasoning_item_id_policy` は、runner が履歴を引き継ぐとき（たとえば `RunResult.to_input_list()` やセッションに基づく実行を使用する場合）に、reasoning item を次ターンのモデル入力へ変換する方法を制御します。

-   `None` または `"preserve"`（デフォルト）: reasoning item ID を保持します。
-   `"omit"`: 生成される次ターン入力から reasoning item ID を取り除きます。

`"omit"` は主に、reasoning item が `id` 付きで送信される一方で、必要な後続項目がない場合に発生する Responses API 400 エラーの一種（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）に対する opt-in の緩和策として使用します。

これは、SDK が以前の出力からフォローアップ入力を構築するマルチターンのエージェント実行（セッション永続化、サーバー管理の会話差分、ストリーミング/非ストリーミングのフォローアップターン、再開パスを含む）で、reasoning item ID が保持される一方、プロバイダーがその ID を対応する後続項目とペアのままにすることを要求する場合に発生する可能性があります。

`reasoning_item_id_policy="omit"` を設定すると、reasoning content は保持しつつ reasoning item の `id` を取り除くため、SDK が生成するフォローアップ入力でその API invariant がトリガーされるのを避けられます。

スコープに関する注記:

-   これは、SDK がフォローアップ入力を構築する際に SDK によって生成/転送される reasoning item のみを変更します。
-   ユーザーが指定した初期入力項目は書き換えません。
-   `call_model_input_filter` は、このポリシー適用後でも意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

次のターンに状態を引き継ぐ一般的な方法は 4 つあります。

| 戦略 | 状態の保存場所 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` からのリストに次のユーザーメッセージを加えたもの |
| `session` | 自分のストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい、名前付きのサーバー側会話 | 同じ `conversation_id` と新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作成しない軽量なサーバー管理の継続 | `result.last_response_id` と新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理であり、OpenAI Responses API を使用している場合にのみ適用されます。ほとんどのアプリケーションでは、会話ごとに 1 つの永続化戦略を選んでください。クライアント管理の履歴と OpenAI 管理の状態を混在させると、両方のレイヤーを意図的に調整している場合を除き、コンテキストが重複する可能性があります。

!!! note

    セッション永続化は、サーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）と同じ実行内で組み合わせることはできません。
    呼び出しごとに 1 つのアプローチを選択してください。

### 会話 / チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行される（したがって 1 回以上の LLM 呼び出しが行われる）可能性がありますが、これはチャット会話における 1 つの論理的なターンを表します。たとえば次のようになります。

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行してから、出力を生成します。

エージェント実行の最後に、ユーザーに何を表示するかを選択できます。たとえば、エージェントによって生成されたすべての新しい項目をユーザーに表示することも、最終出力だけを表示することもできます。いずれの場合でも、その後ユーザーがフォローアップの質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

#### 手動での会話管理

次のターンの入力を取得するには、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます。

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

より簡単なアプローチとして、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出さずに会話履歴を自動で処理できます。

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
-   異なる session ID ごとに別々の会話を維持します

詳細については [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに処理する代わりに、OpenAI の会話状態機能にサーバー側で会話状態を管理させることもできます。これにより、過去のメッセージをすべて手動で再送せずに会話履歴を保持できます。以下のどちらのサーバー管理アプローチでも、各リクエストでは新しいターンの入力のみを渡し、保存された ID を再利用してください。詳細については [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間で状態を追跡する 2 つの方法を提供しています。

##### 1. `conversation_id` の使用

まず OpenAI Conversations API を使用して会話を作成し、その ID を以降のすべての呼び出しで再利用します。

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

もう 1 つの選択肢は **レスポンスチェイニング** で、各ターンを前のターンのレスポンス ID に明示的にリンクします。

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
設定を保持するため、再開されたターンは同じサーバー管理の会話内で継続します。

`conversation_id` と `previous_response_id` は相互に排他的です。システム間で共有できる名前付きの会話リソースが必要な場合は `conversation_id` を使用します。1 ターンから次のターンへ最も軽量な Responses API の継続用 basic component が必要な場合は `previous_response_id` を使用します。

!!! note

    SDK は `conversation_locked` エラーを backoff 付きで自動的に再試行します。サーバー管理の
    会話実行では、再試行前に内部の conversation-tracker 入力を巻き戻し、
    同じ準備済み項目をクリーンに再送できるようにします。

    ローカルのセッションベース実行（`conversation_id`、
    `previous_response_id`、または `auto_previous_response_id` と組み合わせることはできません）では、SDK は再試行後の重複した履歴項目を減らすために、最近永続化された入力項目の best-effort
    rollback も実行します。

    この互換性再試行は、`ModelSettings.retry` を設定していない場合でも発生します。モデルリクエストに対するより広範な opt-in retry の挙動については、[Runner 管理の再試行](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### モデル入力フィルターの呼び出し

モデル呼び出しの直前にモデル入力を編集するには、`call_model_input_filter` を使用します。このフックは現在のエージェント、コンテキスト、および結合された入力項目（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力項目のリストである必要があります。それ以外の形状を返すと `UserError` が発生します。

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

runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元のリストをその場で変更せずに、トリム、置換、または並べ替えることができます。

セッションを使用している場合、`call_model_input_filter` はセッション履歴がすでに読み込まれ、現在のターンとマージされた後に実行されます。その前のマージ手順自体をカスタマイズしたい場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用してください。

`conversation_id`、`previous_response_id`、または `auto_previous_response_id` を使用する OpenAI サーバー管理の会話状態を使用している場合、このフックは次の Responses API 呼び出し用に準備されたペイロードに対して実行されます。そのペイロードは、以前の履歴の完全な再生ではなく、新しいターンの差分のみをすでに表している場合があります。返した項目のみが、そのサーバー管理の継続に対して送信済みとしてマークされます。

機密データを編集したり、長い履歴をトリムしたり、追加のシステムガイダンスを注入したりするには、`run_config` を通じて実行ごとにフックを設定してください。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする dict である `error_handlers` を受け入れます。サポートされるキーは `"max_turns"` と `"model_refusal"` です。`MaxTurnsExceeded` または `ModelRefusalError` を発生させる代わりに、制御された最終出力を返したい場合に使用します。

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

フォールバック出力を会話履歴に追加したくない場合は、`include_in_history=False` を設定してください。

モデルの拒否により `ModelRefusalError` で実行を終了する代わりに、アプリケーション固有のフォールバックを生成したい場合は `"model_refusal"` を使用します。

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

## 永続的実行インテグレーションと human-in-the-loop

ツール承認の一時停止/再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下のインテグレーションは、実行が長い待機、再試行、またはプロセス再起動にまたがる可能性がある場合の永続的なオーケストレーション向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) インテグレーションを使用すると、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。長時間実行タスクを完了するために Temporal と Agents SDK が連携して動作するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) で参照できます。 

### Restate

Agents SDK の [Restate](https://restate.dev/) インテグレーションは、人による承認、ハンドオフ、セッション管理を含む軽量で耐久性のあるエージェントに使用できます。このインテグレーションは Restate の single-binary ランタイムを依存関係として必要とし、エージェントをプロセス/コンテナまたはサーバーレス関数として実行することをサポートします。
詳細については [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) インテグレーションを使用すると、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。sync メソッドと async メソッドの両方をサポートしています。このインテグレーションに必要なのは SQLite または Postgres データベースのみです。詳細については、インテグレーション [リポジトリ](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を発生させます。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。他のすべての具体的な例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: この例外は、エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に発生します。指定された対話ターン数内にエージェントがタスクを完了できなかったことを示します。制限を無効にするには `max_turns=None` を設定してください。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: この例外は、基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには次のものが含まれる場合があります。
    -   不正な JSON: モデルがツール呼び出し用、または直接出力内で不正な JSON 構造を提供した場合。特に特定の `output_type` が定義されている場合です。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できなかった場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: この例外は、関数ツール呼び出しが構成されたタイムアウトを超え、そのツールが `timeout_behavior="raise_exception"` を使用している場合に発生します。
-   [`UserError`][agents.exceptions.UserError]: この例外は、SDK を使用してコードを書いているあなたが、SDK の使用中にエラーを起こした場合に発生します。通常、誤ったコード実装、無効な設定、または SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: この例外は、入力ガードレールまたは出力ガードレールの条件がそれぞれ満たされた場合に発生します。入力ガードレールは処理前に受信メッセージを確認し、出力ガードレールは配信前にエージェントの最終応答を確認します。