---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを使用してエージェントを実行できます。3 つの選択肢があります。

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

詳しくは [実行結果ガイド](results.md) を参照してください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使用する場合、開始エージェントと入力を渡します。入力には次のものを指定できます。

-   文字列（ユーザーメッセージとして扱われます）、
-   OpenAI Responses API 形式の入力項目のリスト、または
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]。

その後、runner はループを実行します。

1. 現在のエージェントに対して、現在の入力を使って LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、実行結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力が「最終出力」とみなされる条件は、目的の型のテキスト出力を生成し、ツール呼び出しが存在しないことです。

### ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行に関する完全な情報が含まれます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳しくは [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意のヘルパー）

OpenAI Responses websocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。接続の再利用には websocket セッションヘルパーを推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルール、および具体的なモデルオブジェクトやカスタムプロバイダーに関する注意点については、[モデル](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: セッションヘルパーなし（動作します）

websocket トランスポートだけが必要で、共有プロバイダーやセッションの管理を SDK に任せる必要がない場合に使用します。

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

##### パターン 2: `responses_websocket_session()` の使用（複数ターンでの再利用に推奨）

複数の実行にわたって（同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含め）、websocket 対応の共有プロバイダーと `RunConfig` を使いたい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します。

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

コンテキストを終了する前に、ストリーミングされた実行結果の消費を完了してください。websocket リクエストがまだ進行中の状態でコンテキストを終了すると、共有接続が強制的に閉じられる可能性があります。

長い reasoning ターンで websocket keepalive タイムアウトが発生する場合は、`ping_timeout` を増やすか、`ping_timeout=None` を設定してハートビートタイムアウトを無効にしてください。websocket のレイテンシよりも信頼性が重要な実行では、HTTP/SSE トランスポートを使用してください。

### 実行設定

`run_config` パラメーターを使用すると、エージェント実行の一部のグローバル設定を構成できます。

#### 一般的な実行設定カテゴリー

各エージェント定義を変更せずに単一の実行の動作を上書きするには、`RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent が持つ `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名の検索に使用するモデルプロバイダーです。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際のセッションレベルのデフォルト（たとえば `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions を使用する際に、各ターンの前に新しいユーザー入力をセッション履歴とどのようにマージするかをカスタマイズします。コールバックは同期または非同期にできます。

##### ガードレール、ハンドオフ、モデル入力の整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフにまだ入力フィルターがない場合に、すべてのハンドオフに適用するグローバル入力フィルターです。入力フィルターにより、新しいエージェントに送信される入力を編集できます。詳しくは [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェントを呼び出す前に、以前のトランスクリプトを単一のアシスタントメッセージに折りたたむオプトインのベータ機能です。ネストされたハンドオフを安定化している間はデフォルトで無効です。有効にするには `True` に設定し、raw トランスクリプトをそのまま渡すには `False` のままにします。すべての [Runner メソッド][agents.run.Runner] は、指定がない場合に自動的に `RunConfig` を作成するため、クイックスタートとコード例ではデフォルトがオフのままとなり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個別のハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を通じてこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` をオプトインしたときに、正規化されたトランスクリプト（履歴 + ハンドオフ項目）を受け取る任意の callable です。次のエージェントに転送する入力項目の正確なリストを返す必要があり、完全なハンドオフフィルターを書かずに組み込みの要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力項目）を編集するフックです。たとえば、履歴をトリミングしたり、システムプロンプトを注入したりできます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が以前の出力を次ターンのモデル入力に変換する際に、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]: 実行ごとのトレーシング API キーなど、trace export settings を上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入力/出力など、潜在的に機密性の高いデータを traces に含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、trace ID、trace group ID を設定します。少なくとも `workflow_name` を設定することを推奨します。group ID は、複数の実行にまたがって traces を関連付けられる任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべての traces に含めるメタデータです。

##### ツール承認とツールエラーの動作

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フロー中にツール呼び出しが拒否された場合に、モデルから見えるメッセージをカスタマイズします。

ネストされたハンドオフは、オプトインのベータ機能として利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで有効にするために `handoff(..., nest_handoff_history=True)` を設定して、トランスクリプトを折りたたむ動作を有効にします。raw トランスクリプトを保持したい場合（デフォルト）は、フラグを未設定のままにするか、会話を必要なとおりに正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに、生成される要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出します。

#### 実行設定の詳細

##### `tool_error_formatter`

承認フローでツール呼び出しが拒否されたときにモデルへ返されるメッセージをカスタマイズするには、`tool_error_formatter` を使用します。

フォーマッターは、次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`: エラーカテゴリーです。現時点では `"approval_rejected"` です。
-   `tool_type`: ツールランタイム（`"function"`, `"computer"`, `"shell"`, `"apply_patch"`, または `"custom"`）です。
-   `tool_name`: ツール名です。
-   `call_id`: ツール呼び出し ID です。
-   `default_message`: SDK のデフォルトのモデル可視メッセージです。
-   `run_context`: アクティブな実行コンテキストラッパーです。

メッセージを置き換える文字列、または SDK デフォルトを使用する場合は `None` を返します。

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

`reasoning_item_id_policy` は、runner が履歴を引き継ぐ際（たとえば `RunResult.to_input_list()` や session-backed の実行を使用する場合）に、reasoning items を次ターンのモデル入力へどのように変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）: reasoning item ID を保持します。
-   `"omit"`: 生成された次ターン入力から reasoning item ID を取り除きます。

`"omit"` は主に、reasoning item が `id` 付きで送信されたものの、必要な後続項目がない場合（たとえば `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）に発生する Responses API 400 エラーの一種に対する、オプトインの緩和策として使用します。

これは、SDK が以前の出力から後続入力を構築するマルチターンのエージェント実行で発生することがあります（セッション永続化、サーバー管理の会話差分、ストリーミング/非ストリーミングの後続ターン、再開パスを含みます）。reasoning item ID は保持されている一方で、プロバイダーがその ID を対応する後続項目と組み合わせたままにすることを要求する場合です。

`reasoning_item_id_policy="omit"` を設定すると、reasoning content は保持しつつ reasoning item の `id` を取り除くため、SDK が生成した後続入力でその API 不変条件をトリガーすることを避けられます。

範囲に関する注意:

-   これは、SDK が後続入力を構築するときに生成/転送する reasoning items のみを変更します。
-   ユーザーが指定した初期入力項目は書き換えません。
-   `call_model_input_filter` は、このポリシーが適用された後でも、意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

状態を次のターンへ持ち越す一般的な方法は 4 つあります。

| 戦略 | 状態の保存場所 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` からのリストに次のユーザーメッセージを加えたもの |
| `session` | 自分のストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい、名前付きのサーバー側会話 | 同じ `conversation_id` と新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | conversation resource を作成しない軽量なサーバー管理の継続 | `result.last_response_id` と新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理であり、OpenAI Responses API を使用している場合にのみ適用されます。多くのアプリケーションでは、会話ごとに 1 つの永続化戦略を選択してください。クライアント管理の履歴と OpenAI 管理の状態を混在させると、両方のレイヤーを意図的に調整していない限り、コンテキストが重複する可能性があります。

!!! note

    セッション永続化は、同じ実行内でサーバー管理の会話設定
    （`conversation_id`, `previous_response_id`, または `auto_previous_response_id`）と
    組み合わせることはできません。呼び出しごとに 1 つの方法を選択してください。

### 会話/チャットスレッド

いずれかの実行メソッドを呼び出すと、1 つ以上のエージェントが実行される場合があります（したがって 1 回以上の LLM 呼び出しが発生します）が、チャット会話における 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフし、2 つ目のエージェントがさらにツールを実行してから出力を生成します。

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。たとえば、エージェントが生成したすべての新しい項目をユーザーに表示することも、最終出力だけを表示することもできます。どちらの場合でも、ユーザーが続けて質問することがあり、その場合は run メソッドを再度呼び出せます。

#### 手動の会話管理

次のターンの入力を取得するには、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、会話履歴を手動で管理できます。

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

Sessions は自動的に次を行います。

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なる session ID ごとに別々の会話を維持します

詳しくは [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理の会話

`to_input_list()` や `Sessions` を使ってローカルで処理する代わりに、OpenAI の会話状態機能にサーバー側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを手動で再送することなく会話履歴を保持できます。以下のいずれのサーバー管理アプローチでも、各リクエストでは新しいターンの入力のみを渡し、保存された ID を再利用します。詳しくは [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI は、ターン間で状態を追跡する 2 つの方法を提供しています。

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

もう 1 つの選択肢は **レスポンスチェーニング** で、各ターンが前のターンのレスポンス ID に明示的にリンクされます。

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
設定を保持するため、再開後のターンも同じサーバー管理の会話で継続します。

`conversation_id` と `previous_response_id` は相互に排他的です。システム間で共有できる名前付きの conversation resource が必要な場合は `conversation_id` を使用します。1 ターンから次のターンへの最も軽量な Responses API 継続プリミティブが必要な場合は `previous_response_id` を使用します。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動的に再試行します。サーバー管理の
    会話実行では、再試行前に内部の conversation-tracker 入力を巻き戻すため、
    同じ準備済み項目をきれいに再送できます。

    ローカルの session-based 実行（`conversation_id`,
    `previous_response_id`, または `auto_previous_response_id` と組み合わせることはできません）でも、SDK は再試行後の履歴エントリ重複を減らすために、最近永続化された入力項目のベストエフォートな
    ロールバックを行います。

    この互換性のための再試行は、`ModelSettings.retry` を設定していなくても実行されます。モデルリクエストに対するより広範なオプトインの再試行動作については、[Runner 管理の再試行](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

モデル呼び出しの直前にモデル入力を編集するには、`call_model_input_filter` を使用します。このフックは、現在のエージェント、コンテキスト、および結合された入力項目（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力項目のリストである必要があります。それ以外の形を返すと `UserError` が送出されます。

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

runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元のリストをその場で変更せずに、トリミング、置換、または並べ替えができます。

セッションを使用している場合、`call_model_input_filter` は、セッション履歴がすでに読み込まれ、現在のターンとマージされた後に実行されます。その前段のマージ手順自体をカスタマイズしたい場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用してください。

OpenAI のサーバー管理会話状態を `conversation_id`, `previous_response_id`, または `auto_previous_response_id` とともに使用している場合、このフックは次の Responses API 呼び出し用に準備されたペイロード上で実行されます。そのペイロードは、以前の履歴の完全な再生ではなく、新しいターンの差分だけをすでに表している場合があります。返した項目だけが、そのサーバー管理の継続に対して送信済みとしてマークされます。

機密データの墨消し、長い履歴のトリミング、または追加のシステムガイダンスの注入を行うには、`run_config` 経由で実行ごとにフックを設定します。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする dict である `error_handlers` を受け付けます。サポートされるキーは `"max_turns"` と `"model_refusal"` です。`MaxTurnsExceeded` または `ModelRefusalError` を送出する代わりに、制御された最終出力を返したい場合に使用します。

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

モデル拒否が `ModelRefusalError` で実行を終了する代わりに、アプリケーション固有のフォールバックを生成すべき場合は、`"model_refusal"` を使用します。

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

## Durable execution 連携と human-in-the-loop

ツール承認の一時停止/再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の連携は、実行が長い待機、再試行、またはプロセス再起動をまたぐ可能性がある場合の durable orchestration 向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使用すると、human-in-the-loop タスクを含む、durable で長時間実行されるワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使用すると、人間による承認、ハンドオフ、セッション管理を含む軽量で durable なエージェントを利用できます。この連携は Restate の単一バイナリランタイムを依存関係として必要とし、エージェントをプロセス/コンテナまたはサーバーレス関数として実行することをサポートします。
詳しくは [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使用すると、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行されるエージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期メソッドと非同期メソッドの両方をサポートします。この連携に必要なのは SQLite または Postgres データベースのみです。詳しくは連携の [リポジトリ](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: これは SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的な例外の派生元となる汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: この例外は、エージェントの実行が `Runner.run`, `Runner.run_sync`, または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に送出されます。これは、指定された対話ターン数内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: この例外は、基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには次のものが含まれます。
    -   不正な形式の JSON: モデルがツール呼び出しや直接出力で不正な形式の JSON 構造を提供した場合、特に特定の `output_type` が定義されている場合です。
    -   予期しないツール関連の失敗: モデルが期待される方法でツールを使用できなかった場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: この例外は、関数ツール呼び出しが設定されたタイムアウトを超え、そのツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]: この例外は、あなた（SDK を使用してコードを書く人）が SDK の使用中に誤りを犯した場合に送出されます。通常は、不正なコード実装、無効な設定、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: この例外は、それぞれ入力ガードレールまたは出力ガードレールの条件が満たされた場合に送出されます。入力ガードレールは処理前に受信メッセージを確認し、出力ガードレールは配信前にエージェントの最終応答を確認します。