---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラス経由で実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]。非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]。同期メソッドで、内部では `.run()` を実行するだけです。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままストリーミングします。

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

詳細は [results ガイド](results.md) を参照してください。

## Runner ライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使うときは、開始エージェントと入力を渡します。入力には以下を指定できます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力アイテムのリスト
-   中断した実行を再開する際の [`RunState`][agents.run_state.RunState]

その後、Runner は次のループを実行します。

1. 現在の入力を使って、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力を「最終出力」と見なすルールは、期待する型のテキスト出力が生成され、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使うと、LLM 実行中のストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行情報全体が格納されます。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket トランスポートを有効化しても、通常の `Runner` API をそのまま使えます。接続再利用には websocket session helper の利用を推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルールや、具体的なモデルオブジェクト／カスタムプロバイダーに関する注意点は、[Models](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: session helper なし（動作します）

websocket トランスポートだけを使いたく、SDK に共有 provider / session 管理を任せる必要がない場合に使います。

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

このパターンは単発実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼ぶ場合、同じ `RunConfig` / provider インスタンスを手動で再利用しない限り、実行ごとに再接続が発生する可能性があります。

##### パターン 2: `responses_websocket_session()` を使用（複数ターン再利用に推奨）

複数回の実行で websocket 対応 provider と `RunConfig` を共有したい場合（同じ `run_config` を継承するネストした agent-as-tool 呼び出しを含む）は、[`responses_websocket_session()`][agents.responses_websocket_session] を使います。

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

コンテキストを抜ける前に、ストリーミング結果の消費を完了してください。websocket リクエストが進行中のままコンテキストを終了すると、共有接続が強制クローズされる場合があります。

### RunConfig

`run_config` パラメーターを使うと、エージェント実行のグローバル設定をいくつか構成できます。

#### 共通 RunConfig カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに単一の実行に対して動作を上書きできます。

##### モデル、プロバイダー、セッションの既定値

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、グローバルに使用する LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーです。既定値は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際のセッションレベル既定値（例: `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターン前に新しいユーザー入力をセッション履歴へどうマージするかをカスタマイズします。コールバックは同期／非同期どちらでも可能です。

##### ガードレール、ハンドオフ、モデル入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力／出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ側に未設定の場合、すべてのハンドオフに適用するグローバル入力フィルターです。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次エージェント呼び出し前に、直前までの transcript を単一の assistant メッセージへ折りたたむ opt-in beta 機能です。ネストしたハンドオフの安定化中のため既定で無効です。有効化は `True`、raw transcript をそのまま通すには `False` を使います。[Runner メソッド][agents.run.Runner] は `RunConfig` 未指定時に自動作成されるため、quickstart や examples では既定の無効状態が維持され、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個々のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] で上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` を有効化した際に、正規化された transcript（履歴 + ハンドオフアイテム）を受け取る任意 callable です。次エージェントへ渡す入力アイテムの**正確なリスト**を返す必要があり、完全なハンドオフフィルターを書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、完全に準備済みのモデル入力（instructions と入力アイテム）を編集するフックです。例: 履歴のトリミングやシステムプロンプトの注入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: Runner が過去出力を次ターンのモデル入力へ変換する際に、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: [`TracingConfig`][agents.tracing.TracingConfig] を渡し、実行単位のトレーシング API key などの trace export 設定を上書きします。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: trace に LLM やツール呼び出しの入力／出力などの機微データを含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は任意で、複数実行間の trace を関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべての trace に含めるメタデータです。

##### ツール承認とツールエラー動作

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フロー中にツール呼び出しが拒否された場合、モデルに見えるメッセージをカスタマイズします。

ネストしたハンドオフは opt-in beta として利用できます。折りたたみ transcript 動作を有効にするには `RunConfig(nest_handoff_history=True)` を渡すか、特定ハンドオフで `handoff(..., nest_handoff_history=True)` を設定してください。raw transcript（既定）を維持したい場合は、フラグを未設定のままにするか、必要な形で会話を正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約で使うラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（既定へ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### RunConfig 詳細

##### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否された際にモデルへ返すメッセージをカスタマイズできます。

formatter には以下を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] が渡されます。

-   `kind`: エラーカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`: ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`、`"custom"`）。
-   `tool_name`: ツール名。
-   `call_id`: ツール呼び出し ID。
-   `default_message`: SDK 既定のモデル可視メッセージ。
-   `run_context`: 現在の run context wrapper。

メッセージを置き換える文字列を返すか、SDK 既定を使う場合は `None` を返します。

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

`reasoning_item_id_policy` は、Runner が履歴を引き継ぐ際（例: `RunResult.to_input_list()` やセッションバック実行）に reasoning items を次ターンのモデル入力へどう変換するかを制御します。

-   `None` または `"preserve"`（既定）: reasoning item ID を保持します。
-   `"omit"`: 生成される次ターン入力から reasoning item ID を除去します。

`"omit"` は主に、reasoning item に `id` があるが必須の後続 item がない場合に発生する Responses API 400 エラー群への opt-in 緩和策として使います（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が過去出力から後続入力を構築する複数ターンエージェント実行（セッション永続化、サーバー管理会話 delta、ストリーミング／非ストリーミング後続ターン、再開経路を含む）で、reasoning item ID が保持される一方、プロバイダー側でその ID を対応する後続 item とペアで維持することを要求する場合に発生し得ます。

`reasoning_item_id_policy="omit"` を設定すると、reasoning 内容は保持しつつ reasoning item の `id` を除去するため、SDK 生成の後続入力でその API 不変条件の違反を回避できます。

スコープに関する注意:

-   変更対象は、SDK が後続入力を構築する際に生成／転送する reasoning items のみです。
-   ユーザー提供の初期入力 items は書き換えません。
-   `call_model_input_filter` により、このポリシー適用後に意図的に reasoning ID を再導入することは可能です。

## 状態と会話管理

### メモリ戦略の選択

状態を次ターンへ渡す一般的な方法は 4 つあります。

| Strategy | Where state lives | Best for | What you pass on the next turn |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模チャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` のリスト + 次のユーザーメッセージ |
| `session` | ユーザーのストレージ + SDK | 永続チャット状態、再開可能実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別インスタンス |
| `conversation_id` | OpenAI Conversations API | 複数ワーカー／サービス間で共有したい名前付きサーバー側会話 | 同じ `conversation_id` + 新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作らない軽量サーバー管理継続 | `result.last_response_id` + 新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API 使用時のみ適用されます。多くのアプリでは、会話ごとに永続化戦略を 1 つ選んでください。クライアント管理履歴と OpenAI 管理状態を混在させると、意図的に両レイヤーを調整していない限りコンテキストが重複する場合があります。

!!! note

    セッション永続化はサーバー管理会話設定
    （`conversation_id`、`previous_response_id`、`auto_previous_response_id`）と
    同一実行で併用できません。
    呼び出しごとにどちらか 1 つの方式を選んでください。

### Conversations/chat threads

どの run メソッドを呼び出しても、結果として 1 つ以上のエージェント実行（つまり 1 回以上の LLM 呼び出し）が発生する可能性がありますが、チャット会話上は 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキスト入力
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフし、2 つ目のエージェントがさらにツールを実行して出力を生成

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成した新規アイテムをすべて表示することも、最終出力のみ表示することもできます。いずれの場合も、その後ユーザーがフォローアップ質問をしたら、run メソッドを再度呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使うと、次ターン用入力を取得して会話履歴を手動管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使うと `.to_input_list()` を手動で呼ばずに会話履歴を自動処理できます。

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

Sessions は自動で次を行います。

-   各実行前に会話履歴を取得
-   各実行後に新規メッセージを保存
-   異なるセッション ID ごとに別会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の会話状態機能でサーバー側管理することもできます。これにより、過去メッセージを毎回手動で再送せずに会話履歴を保持できます。以下いずれのサーバー管理方式でも、各リクエストでは新規ターン入力のみを渡し、保存済み ID を再利用してください。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI ではターン間状態追跡に 2 つの方法があります。

##### 1. `conversation_id` を使用

最初に OpenAI Conversations API で会話を作成し、以降の呼び出しごとにその ID を再利用します。

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

もう 1 つは **response chaining** で、各ターンが前ターンの response ID に明示的にリンクします。

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

実行が承認待ちで一時停止し、[`RunState`][agents.run_state.RunState] から再開した場合、
SDK は保存済みの `conversation_id` / `previous_response_id` / `auto_previous_response_id`
設定を維持するため、再開ターンも同じサーバー管理会話で継続されます。

`conversation_id` と `previous_response_id` は排他的です。システム間で共有可能な名前付き会話リソースが必要なら `conversation_id` を使ってください。ターン間継続の最も軽量な Responses API プリミティブが必要なら `previous_response_id` を使ってください。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動再試行します。サーバー管理
    会話実行では、再試行前に内部の conversation-tracker 入力を巻き戻し、同じ
    準備済みアイテムをクリーンに再送できるようにします。

    ローカルのセッションベース実行（`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` と併用不可）でも、
    SDK は再試行後の履歴重複を減らすため、直近で永続化した入力アイテムの
    ベストエフォートなロールバックを行います。

    この互換性再試行は、`ModelSettings.retry` を設定していなくても実行されます。より
    広範な opt-in モデルリクエスト再試行については、[Runner 管理再試行](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### call model input filter

`call_model_input_filter` を使うと、モデル呼び出し直前にモデル入力を編集できます。このフックは現在のエージェント、コンテキスト、結合済み入力アイテム（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。`input` フィールドは必須で、入力アイテムのリストでなければなりません。これ以外の形を返すと `UserError` が発生します。

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

Runner は準備済み入力リストのコピーをこのフックに渡すため、呼び出し元の元リストを直接変更せずに、トリミング、置換、並べ替えができます。

session 使用時、`call_model_input_filter` はセッション履歴の読み込みと現在ターンへのマージが完了した後に実行されます。この前段のマージ処理自体をカスタマイズしたい場合は [`session_input_callback`][agents.run.RunConfig.session_input_callback] を使ってください。

`conversation_id`、`previous_response_id`、`auto_previous_response_id` による OpenAI サーバー管理会話状態を使う場合、このフックは次の Responses API 呼び出し用に準備されたペイロードに対して実行されます。そのペイロードは、過去履歴の完全再送ではなく新規ターン差分のみを表すことがあります。サーバー管理継続で送信済みとしてマークされるのは、あなたが返したアイテムのみです。

このフックは `run_config` 経由で実行ごとに設定でき、機微データのマスキング、長い履歴のトリミング、追加のシステムガイダンス注入に使えます。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーにした dict `error_handlers` を受け取れます。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出せず、制御された最終出力を返したい場合に使用します。

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

## 耐久実行連携と human-in-the-loop

ツール承認の pause / resume パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の連携は、実行が長時間待機、再試行、プロセス再起動をまたぐ場合の耐久オーケストレーション向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む耐久的な長時間ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を参照し、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、human approval、ハンドオフ、セッション管理を含む軽量で耐久性のあるエージェントを利用できます。この連携は依存関係として Restate の single-binary runtime を必要とし、プロセス／コンテナまたはサーバーレス関数としてエージェント実行をサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期／非同期メソッドの両方に対応しています。この連携に必要なのは SQLite または Postgres データベースのみです。詳細は連携 [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。他のすべての具体的な例外はこの汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 制限を超えたときに送出されます。指定された対話ターン数内でタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例:
    -   不正な JSON: モデルがツール呼び出し用、または直接出力で不正な JSON 構造を返した場合（特に特定の `output_type` が定義されている場合）。
    -   予期しないツール関連の失敗: モデルが期待される方法でツールを使用しない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 関数ツール呼び出しが設定したタイムアウトを超え、かつツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]: SDK 使用時に（SDK を使ったコードを書く人が）誤りをした場合に送出されます。通常は不正なコード実装、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力ガードレールまたは出力ガードレールの条件が満たされたときに送出されます。入力ガードレールは処理前の受信メッセージを検査し、出力ガードレールは配信前のエージェント最終応答を検査します。