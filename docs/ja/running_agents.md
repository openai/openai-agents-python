---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを介してエージェントを実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]。非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]。同期メソッドで、内部的には `.run()` を実行するだけです。
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

詳細は [結果ガイド](results.md) を参照してください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使うときは、開始エージェントと入力を渡します。入力には次を指定できます。

-   文字列 (ユーザーメッセージとして扱われます)
-   OpenAI Responses API 形式の入力アイテムのリスト
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]

その後、Runner は次のループを実行します。

1. 現在の入力を使って、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追記してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力を「最終出力」とみなす条件は、期待された型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使うと、LLM 実行中のストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行の完全な情報が入ります。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート (任意ヘルパー)

OpenAI Responses websocket トランスポートを有効化しても、通常の `Runner` API をそのまま使えます。接続再利用には websocket session helper の利用を推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルールと、具体的な model オブジェクトや custom provider に関する注意点は、[Models](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: session helper なし (動作可)

websocket トランスポートだけ使いたい場合、また SDK に共有 provider / session を管理させる必要がない場合に使います。

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

このパターンは単発実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼ぶ場合、同じ `RunConfig` / provider インスタンスを手動で再利用しない限り、各実行で再接続が発生する可能性があります。

##### パターン 2: `responses_websocket_session()` を使用 (マルチターン再利用に推奨)

複数実行間で websocket 対応 provider と `RunConfig` を共有したい場合 (同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含む) は [`responses_websocket_session()`][agents.responses_websocket_session] を使います。

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

ストリーミング結果の消費は context を抜ける前に完了してください。websocket リクエストが進行中のまま context を終了すると、共有接続が強制的に閉じられる可能性があります。

### RunConfig

`run_config` パラメーターを使うと、エージェント実行のグローバル設定をいくつか構成できます。

#### 共通の run_config カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに、単一実行の挙動を上書きできます。

##### model / provider / session の既定値

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、グローバルで使う LLM model を設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: model 名の解決に使う model provider で、既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有設定を上書きします。例えばグローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際の session レベル既定値 (例: `SessionSettings(limit=...)`) を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターン前に新規ユーザー入力を session 履歴へどうマージするかをカスタマイズします。callback は同期 / 非同期のどちらでも可能です。

##### ガードレール / ハンドオフ / model 入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力 / 出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ側に既存設定がない場合、すべてのハンドオフに適用されるグローバル入力フィルターです。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェント呼び出し前に、直前までの transcript を 1 つの assistant message に折りたたむ opt-in beta です。ネストされたハンドオフの安定化中のため既定では無効です。有効化は `True`、raw transcript をそのまま渡す場合は `False` にします。[Runner methods][agents.run.Runner] は `RunConfig` 未指定時に自動作成するため、quickstart や examples では既定で無効のままです。また明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] callback は引き続き優先されます。個別ハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] で上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` を有効化した際に、正規化 transcript (履歴 + ハンドオフ項目) を受け取る任意 callable です。次エージェントへ渡す入力アイテムの正確なリストを返す必要があり、完全な handoff filter を書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: model 呼び出し直前に、完全に準備された model 入力 (`instructions` と入力アイテム) を編集する hook です。例: 履歴のトリミングやシステムプロンプト注入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: Runner が過去出力を次ターンの model 入力へ変換する際に、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行の exporter / processor / tracing metadata を上書きする [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微データをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: この実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は任意で、複数実行にまたがるトレース関連付けに使えます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含める metadata です。

##### ツール承認とツールエラー挙動

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フローでツール呼び出しが拒否された際に、model に見えるメッセージをカスタマイズします。

ネストされたハンドオフは opt-in beta として利用できます。折りたたみ transcript の挙動は `RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで `handoff(..., nest_handoff_history=True)` を設定すると有効になります。raw transcript (既定) を維持したい場合は、フラグを未設定のままにするか、必要どおりに会話をそのまま転送する `handoff_input_filter` (または `handoff_history_mapper`) を指定してください。custom mapper を書かずに生成要約のラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼びます (既定値復元は [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers])。

#### RunConfig 詳細

##### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否されたときに model へ返すメッセージをカスタマイズできます。

formatter は以下を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`: エラーカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`: ツール runtime (`"function"`、`"computer"`、`"shell"`、`"apply_patch"`)。
-   `tool_name`: ツール名。
-   `call_id`: ツール呼び出し ID。
-   `default_message`: SDK 既定の model 向けメッセージ。
-   `run_context`: アクティブな run context wrapper。

文字列を返すとメッセージを置換し、`None` を返すと SDK 既定値を使います。

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

`reasoning_item_id_policy` は、Runner が履歴を引き継ぐ際 (例: `RunResult.to_input_list()` や session-backed 実行) に、reasoning item を次ターン model 入力へどう変換するかを制御します。

-   `None` または `"preserve"` (既定): reasoning item ID を保持します。
-   `"omit"`: 生成される次ターン入力から reasoning item ID を削除します。

`"omit"` は主に、reasoning item が `id` 付きで送信されたが必須の後続 item がない場合に発生する Responses API 400 エラー群への opt-in 緩和策として使います (例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`)。

これは、SDK が過去出力から follow-up 入力を構築するマルチターンエージェント実行時に発生し得ます (session 永続化、サーバー管理 conversation delta、streamed / non-streamed follow-up ターン、resume 経路を含む)。reasoning item ID が保持され、provider 側で対応する後続 item とのペア維持が要求される場合です。

`reasoning_item_id_policy="omit"` を設定すると reasoning 内容は維持しつつ reasoning item `id` を削除するため、SDK 生成 follow-up 入力でこの API 不変条件に抵触するのを回避できます。

適用範囲の注意:

-   影響するのは、SDK が follow-up 入力構築時に生成 / 転送する reasoning item のみです。
-   ユーザー提供の初期入力アイテムは書き換えません。
-   `call_model_input_filter` は、この policy 適用後に意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

状態を次ターンへ引き継ぐ一般的な方法は 4 つあります。

| Strategy | Where state lives | Best for | What you pass on the next turn |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリメモリ内 | 小規模チャットループ、完全手動制御、任意 provider | `result.to_input_list()` のリスト + 次のユーザーメッセージ |
| `session` | 自身のストレージ + SDK | 永続チャット状態、再開可能実行、カスタムストア | 同じ `session` インスタンス、または同じ store を指す別インスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカー / サービス間で共有したい名前付きサーバー側会話 | 同じ `conversation_id` + 新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | conversation リソースを作らない軽量なサーバー管理継続 | `result.last_response_id` + 新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API 使用時のみ適用されます。多くのアプリでは、1 つの会話につき 1 つの永続化戦略を選ぶのが適切です。クライアント管理履歴と OpenAI 管理状態を混在させると、意図的に両レイヤーを調停しない限り、コンテキスト重複が起こる可能性があります。

!!! note

    Session 永続化は、サーバー管理会話設定
    (`conversation_id`、`previous_response_id`、`auto_previous_response_id`) と
    同じ実行内で併用できません。呼び出しごとに 1 つの方式を選んでください。

### 会話 / チャットスレッド

いずれの run メソッドも、結果として 1 つ以上のエージェント実行 (つまり 1 回以上の LLM 呼び出し) を含む可能性がありますが、チャット会話上は 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキスト入力
2. Runner 実行: 最初のエージェントが LLM 呼び出し、ツール実行、2 番目エージェントへハンドオフ、2 番目エージェントがさらにツール実行し、その後出力を生成

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。例えば、エージェントが生成したすべての新規アイテムを表示することも、最終出力だけ表示することもできます。どちらの場合でも、その後ユーザーが追質問したら、run メソッドを再度呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って次ターン入力を取得し、会話履歴を手動管理できます。

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

Sessions は自動的に次を行います。

-   各実行前に会話履歴を取得
-   各実行後に新規メッセージを保存
-   session ID ごとに別々の会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。

#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル処理する代わりに、OpenAI conversation state 機能でサーバー側会話状態を管理することもできます。これにより、過去メッセージを毎回手動で再送せずに会話履歴を保持できます。以下のいずれのサーバー管理方式でも、各リクエストでは新規ターン入力のみを渡し、保存済み ID を再利用します。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間状態追跡の方法を 2 つ提供します。

##### 1. `conversation_id` の使用

最初に OpenAI Conversations API で会話を作成し、その ID を以降のすべての呼び出しで再利用します。

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

もう 1 つは **response chaining** で、各ターンを前ターンの response ID に明示的に連結します。

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

実行が承認待ちで一時停止し、[`RunState`][agents.run_state.RunState] から再開する場合、
SDK は保存済みの `conversation_id` / `previous_response_id` / `auto_previous_response_id`
設定を保持するため、再開ターンも同じサーバー管理会話で継続されます。

`conversation_id` と `previous_response_id` は排他的です。システム間で共有可能な名前付き会話リソースが必要なら `conversation_id` を使います。ターン間で最も軽量な Responses API 継続プリミティブが必要なら `previous_response_id` を使います。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動リトライします。サーバー管理
    会話実行では、リトライ前に内部 conversation-tracker 入力を巻き戻し、同じ
    準備済みアイテムを重複なく再送できるようにします。

    ローカルな session ベース実行 (`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` とは併用不可) でも、SDK は
    リトライ後の履歴重複を減らすため、直近で永続化した入力アイテムのベストエフォート
    ロールバックを行います。

    この互換性リトライは `ModelSettings.retry` 未設定でも実行されます。model リクエストに対する
    より広い opt-in リトライ挙動は、[Runner 管理リトライ](models/index.md#runner-managed-retries) を参照してください。

## フックとカスタマイズ

### call model input filter

`call_model_input_filter` を使うと、model 呼び出し直前の model 入力を編集できます。この hook は現在のエージェント、context、および (存在する場合は session 履歴を含む) 結合済み入力アイテムを受け取り、新しい `ModelInputData` を返します。

返り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。`input` フィールドは必須で、入力アイテムのリストでなければなりません。それ以外の形を返すと `UserError` が発生します。

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

Runner は準備済み入力リストのコピーを hook に渡すため、呼び出し元の元リストをインプレース変更せずに、トリミング / 置換 / 並べ替えができます。

session を使っている場合、`call_model_input_filter` は session 履歴の読み込みと現在ターンへのマージが完了した後に実行されます。より前段のマージ処理自体をカスタマイズしたい場合は [`session_input_callback`][agents.run.RunConfig.session_input_callback] を使ってください。

`conversation_id`、`previous_response_id`、`auto_previous_response_id` を使った OpenAI サーバー管理会話状態を使う場合、この hook は次の Responses API 呼び出し向けに準備された payload に対して実行されます。その payload は、過去履歴の完全再送ではなく新規ターン差分のみを表すことがあります。サーバー管理継続で送信済みとして扱われるのは、あなたが返したアイテムだけです。

機微データのマスキング、長い履歴のトリミング、追加システムガイダンスの注入には、`run_config` で実行単位にこの hook を設定してください。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーに持つ dict `error_handlers` を受け付けます。現在サポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出せず、制御された最終出力を返したい場合に使います。

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

フォールバック出力を会話履歴に追加したくない場合は `include_in_history=False` を設定します。

## Durable execution 連携と human-in-the-loop

ツール承認の一時停止 / 再開パターンは、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の連携は、長時間待機、リトライ、プロセス再起動をまたぐ可能性がある Durable なオーケストレーション向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む Durable で長時間実行のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を参照し、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) を参照してください。

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、human approval、ハンドオフ、session 管理を含む軽量で Durable なエージェントを実行できます。この連携には依存関係として Restate の single-binary runtime が必要で、エージェントを process / container または serverless function として実行できます。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進行状況を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期 / 非同期メソッドの両方をサポートします。この連携に必要なのは SQLite または Postgres データベースのみです。詳細は連携 [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。ほかのすべての具体例外がこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 上限を超えたときに送出されます。指定された対話ターン数内でタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤 model (LLM) が予期しない、または無効な出力を生成したときに発生します。例:
    -   不正な JSON: ツール呼び出し用、または直接出力内の JSON 構造が不正な場合。特に特定の `output_type` が定義されている場合。
    -   想定外のツール関連失敗: model が想定どおりにツールを使えない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 関数ツール呼び出しが設定タイムアウトを超過し、ツールが `timeout_behavior="raise_exception"` を使っている場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]: SDK 使用中に、あなた (SDK を使ってコードを書く人) が誤りをしたときに送出されます。通常はコード実装不備、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件が満たされたときに、それぞれ送出されます。入力ガードレールは処理前の受信メッセージを検査し、出力ガードレールは配信前のエージェント最終応答を検査します。