---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]。非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]。同期メソッドで、内部では `.run()` を実行するだけです。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリーミングします。

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

詳しくは [実行結果ガイド](results.md) をご覧ください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使う際は、開始エージェントと入力を渡します。入力には次を指定できます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力アイテムのリスト
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]

その後、runner は次のループを実行します。

1. 現在の入力を使って、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、そのツール呼び出しを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力を「最終出力」とみなす条件は、期待する型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使うと、LLM 実行中のストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、その実行に関する完全な情報（生成されたすべての新しい出力を含む）が格納されます。ストリーミングイベントは `.stream_events()` で取得できます。詳しくは [ストリーミングガイド](streaming.md) をご覧ください。

#### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket transport を有効化した場合でも、通常の `Runner` API をそのまま使用できます。接続再利用には websocket session helper を推奨しますが、必須ではありません。

これは websocket transport 経由の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルールや、具体的なモデルオブジェクト / カスタムプロバイダーに関する注意点は [Models](models/index.md#responses-websocket-transport) をご覧ください。

##### パターン 1: session helper なし（動作可）

websocket transport だけ使いたく、共有 provider / session の管理を SDK に任せる必要がない場合に使用します。

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

このパターンは単発実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼ぶ場合、同じ `RunConfig` / provider インスタンスを手動で再利用しない限り、実行ごとに再接続される可能性があります。

##### パターン 2: `responses_websocket_session()` を使用（複数ターン再利用に推奨）

複数の実行で websocket 対応 provider と `RunConfig` を共有したい場合は [`responses_websocket_session()`][agents.responses_websocket_session] を使います（同じ `run_config` を継承するネストした agent-as-tool 呼び出しを含む）。

```python
import asyncio

from agents import Agent, responses_websocket_session


async def main():
    agent = Agent(name="Assistant", instructions="Be concise.")

    async with responses_websocket_session() as ws:
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

コンテキストを終了する前に、ストリーミング実行結果の消費を完了してください。websocket リクエストが進行中のままコンテキストを終了すると、共有接続が強制クローズされる可能性があります。

### 実行設定

`run_config` パラメーターを使うと、エージェント実行のグローバル設定をいくつか構成できます。

#### 共通の実行設定カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに 1 回の実行だけ挙動を上書きできます。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、グローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーです。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際、セッションレベルのデフォルト（例: `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時、各ターン前に新しいユーザー入力とセッション履歴をどうマージするかをカスタマイズします。コールバックは同期 / 非同期のどちらでも可能です。

##### ガードレール、ハンドオフ、モデル入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力 / 出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ側に未設定の場合に全ハンドオフへ適用するグローバル入力フィルターです。新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェント呼び出し前に、それまでの transcript を 1 つの assistant メッセージへ折りたたむ opt-in beta 機能です。ネストしたハンドオフの安定化中のためデフォルトは無効です。有効化は `True`、raw transcript をそのまま渡す場合は `False` のままにします。[Runner methods][agents.run.Runner] は `RunConfig` 未指定時に自動作成されるため、クイックスタートやコード例ではデフォルトで無効のままです。明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個別ハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` を有効化したときに、正規化済み transcript（履歴 + ハンドオフ項目）を受け取る任意の callable です。次のエージェントへ渡す入力アイテムの**正確な**リストを返す必要があり、完全なハンドオフフィルターを書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するフックです。例: 履歴のトリミングやシステムプロンプトの注入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が過去出力を次ターンのモデル入力へ変換するとき、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行の exporter、processor、またはトレーシング metadata を上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微な可能性のあるデータをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は複数実行のトレースを関連付けるための任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含める metadata です。

##### ツール承認とツールエラー時の挙動

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フローでツール呼び出しが拒否された際、モデルに見えるメッセージをカスタマイズします。

ネストしたハンドオフは opt-in beta として利用できます。折りたたみ transcript 挙動を有効化するには `RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフに対して `handoff(..., nest_handoff_history=True)` を設定します。raw transcript（デフォルト）を維持したい場合は、このフラグを設定しないか、必要どおり会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約のラッパーテキストを変更したい場合は [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトへ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### 実行設定の詳細

##### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否されたときにモデルへ返すメッセージをカスタマイズできます。

formatter は次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`: エラーカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`: ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`）。
-   `tool_name`: ツール名。
-   `call_id`: ツール呼び出し ID。
-   `default_message`: SDK のデフォルトのモデル表示メッセージ。
-   `run_context`: アクティブな実行コンテキストラッパー。

メッセージを置き換える文字列、または SDK デフォルトを使う場合は `None` を返します。

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

`reasoning_item_id_policy` は、runner が履歴を次へ引き継ぐ際（例: `RunResult.to_input_list()` や session バック実行）に、reasoning item を次ターンのモデル入力へどう変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）: reasoning item ID を保持します。
-   `"omit"`: 生成される次ターン入力から reasoning item ID を取り除きます。

`"omit"` は主に、reasoning item が `id` 付きで送信される一方、必須の後続 item がない場合に起きる Responses API の 400 エラー群への opt-in 緩和策です（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

この問題は、SDK が過去出力からフォローアップ入力を構築する複数ターンのエージェント実行（session 永続化、サーバー管理会話 delta、ストリーミング / 非ストリーミングのフォローアップターン、resume パスを含む）で、reasoning item ID が保持される一方、そのプロバイダーが対応する後続 item とのペア維持を要求する場合に発生し得ます。

`reasoning_item_id_policy="omit"` を設定すると、reasoning の内容は維持しつつ reasoning item の `id` を除去するため、SDK 生成のフォローアップ入力でその API 不変条件に触れなくなります。

スコープに関する注意:

-   変更対象は、SDK がフォローアップ入力構築時に生成 / 転送する reasoning item のみです。
-   ユーザーが指定した初期入力 item は書き換えません。
-   `call_model_input_filter` は、このポリシー適用後でも意図的に reasoning ID を再導入できます。

## 状態と会話の管理

### メモリ戦略の選択

次のターンへ状態を引き継ぐ一般的な方法は 4 つあります。

| 戦略 | 状態の保存場所 | 最適な用途 | 次ターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` のリスト + 次のユーザーメッセージ |
| `session` | ユーザーのストレージ + SDK | 永続チャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別インスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカー / サービス間で共有したい、名前付きのサーバー側会話 | 同じ `conversation_id` + 新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作らない軽量なサーバー管理継続 | `result.last_response_id` + 新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API 使用時にのみ適用されます。ほとんどのアプリケーションでは、1 つの会話につき 1 つの永続化戦略を選んでください。クライアント管理履歴と OpenAI 管理状態を混在させると、意図的に両レイヤーを突き合わせない限り文脈が重複する可能性があります。

!!! note

    Session 永続化は、サーバー管理会話設定
    （`conversation_id`、`previous_response_id`、`auto_previous_response_id`）と
    同一実行では併用できません。
    呼び出しごとにどちらか 1 つを選択してください。

### 会話 / チャットスレッド

いずれの run メソッド呼び出しでも、1 つ以上のエージェント実行（つまり 1 回以上の LLM 呼び出し）が行われる可能性がありますが、チャット会話としては 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行して出力を生成

エージェント実行の終了後、何をユーザーに見せるか選べます。たとえば、エージェントが生成した新規 item をすべて見せることも、最終出力だけ見せることもできます。いずれの場合も、ユーザーが追質問したら再度 run メソッドを呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドで次ターン入力を取得し、会話履歴を手動管理できます。

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

よりシンプルな方法として、[Sessions](sessions/index.md) を使うと `.to_input_list()` を手動で呼ばずに会話履歴を自動処理できます。

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

Sessions は次を自動で行います。

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   異なる session ID ごとに別会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) をご覧ください。


#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の会話状態機能でサーバー側に会話状態を管理させることもできます。これにより、過去メッセージを毎回すべて再送しなくても会話履歴を保持できます。以下のいずれのサーバー管理方式でも、各リクエストでは新しいターンの入力のみを渡し、保存済み ID を再利用します。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI はターン間状態追跡の方法を 2 つ提供します。

##### 1. `conversation_id` を使用

まず OpenAI Conversations API で会話を作成し、その ID を以降の呼び出しで再利用します。

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

##### 2. `previous_response_id` を使用

もう 1 つの方法は **response chaining** で、各ターンを前ターンの response ID へ明示的に関連付けます。

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

実行が承認待ちで一時停止し、[`RunState`][agents.run_state.RunState] から再開する場合でも、
SDK は保存済みの `conversation_id` / `previous_response_id` / `auto_previous_response_id`
設定を維持するため、再開ターンは同じサーバー管理会話で継続されます。

`conversation_id` と `previous_response_id` は相互排他です。システム間共有可能な名前付き会話リソースが必要なら `conversation_id` を使ってください。ターン間の最軽量な Responses API 継続プリミティブを使いたい場合は `previous_response_id` を使ってください。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動リトライします。サーバー管理
    会話実行では、リトライ前に内部の会話トラッカー入力を巻き戻すため、
    同じ準備済み item を重複なく再送できます。

    ローカルな session ベース実行（`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` と併用不可）でも、SDK はベストエフォートで
    直近に永続化された入力 item をロールバックし、リトライ後の履歴重複を減らします。

## フックとカスタマイズ

### call model input filter

`call_model_input_filter` を使うと、モデル呼び出し直前にモデル入力を編集できます。このフックは現在のエージェント、コンテキスト、結合済み入力 item（存在する場合は session 履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力 item のリストでなければなりません。これ以外の形を返すと `UserError` が発生します。

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

runner は準備済み入力リストのコピーをフックへ渡すため、呼び出し元の元リストをインプレース変更せずに、トリミング / 置換 / 並べ替えができます。

session を使用している場合、`call_model_input_filter` は session 履歴の読み込みと現在ターンとのマージが完了した後に実行されます。より前段のマージ処理自体をカスタマイズしたい場合は [`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用してください。

`conversation_id`、`previous_response_id`、`auto_previous_response_id` による OpenAI サーバー管理会話状態を使っている場合、このフックは次の Responses API 呼び出し向けに準備されたペイロードに対して実行されます。そのペイロードは、過去履歴の完全再送ではなく、新ターンの差分のみを表している場合があります。返した item のみが、そのサーバー管理継続で送信済みとしてマークされます。

機微データのマスキング、長い履歴のトリミング、追加のシステムガイダンス挿入などのために、`run_config` 経由で実行ごとにこのフックを設定してください。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする辞書 `error_handlers` を受け取れます。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出せず、制御された最終出力を返したい場合に使用します。

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

フォールバック出力を会話履歴へ追加したくない場合は、`include_in_history=False` を設定してください。

## Durable execution 連携と human-in-the-loop

ツール承認の一時停止 / 再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の連携は、実行が長時間待機、リトライ、プロセス再起動をまたぐ場合の durable なオーケストレーション向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む durable で長時間実行されるワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、人手承認、ハンドオフ、セッション管理を含む軽量で durable なエージェントを実行できます。この連携には依存関係として Restate の single-binary runtime が必要で、プロセス / コンテナまたは serverless functions としてのエージェント実行をサポートします。
詳細は [overview](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [docs](https://docs.restate.dev/ai) をご覧ください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期 / 非同期メソッドの両方に対応しています。この連携に必要なのは SQLite または Postgres データベースのみです。詳細は連携の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [docs](https://docs.dbos.dev/integrations/openai-agents) をご覧ください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。ほかの具体的な例外はすべてこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えたときに送出されます。指定ターン数内でエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例:
    -   不正な JSON: 特に特定の `output_type` が定義されている場合に、ツール呼び出しまたは直接出力でモデルが不正な JSON 構造を返したとき。
    -   予期しないツール関連の失敗: モデルが期待どおりの方法でツールを使えないとき
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 関数ツール呼び出しが設定済みタイムアウトを超過し、ツールが `timeout_behavior="raise_exception"` を使っているときに送出されます。
-   [`UserError`][agents.exceptions.UserError]: SDK 使用時にユーザー（SDK を使ってコードを書く人）が誤りをしたときに送出されます。通常はコード実装ミス、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件が満たされたときにそれぞれ送出されます。入力ガードレールは処理前の受信メッセージをチェックし、出力ガードレールは配信前のエージェント最終応答をチェックします。