---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラス経由で実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]。非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]。同期メソッドで、内部的には `.run()` を実行するだけです。
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

詳細は [結果ガイド](results.md) を参照してください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使うときは、開始エージェントと入力を渡します。入力には次を指定できます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力アイテムのリスト
-   中断された実行を再開する場合の [`RunState`][agents.run_state.RunState]

その後、runner は次のループを実行します。

1. 現在の入力を使って、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力を「最終出力」とみなすルールは、期待する型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使うと、LLM 実行中のストリーミングイベントも追加で受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行の完全な情報が入ります。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket transport を有効化しても、通常の `Runner` API をそのまま利用できます。接続再利用には websocket session helper が推奨されますが、必須ではありません。

これは websocket transport 上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルールや、具体的なモデルオブジェクト / カスタムプロバイダーに関する注意点は [Models](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1: session helper なし（動作します）

websocket transport だけを使いたく、共有 provider / session の管理を SDK に任せる必要がない場合に使います。

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

##### パターン 2: `responses_websocket_session()` を使用（複数ターン再利用に推奨）

複数実行で websocket 対応 provider と `RunConfig` を共有したい場合（同じ `run_config` を継承するネストした agent-as-tool 呼び出しを含む）は [`responses_websocket_session()`][agents.responses_websocket_session] を使用します。

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

コンテキスト終了前に、ストリーミング結果の消費を完了してください。websocket リクエストの送受信中にコンテキストを終了すると、共有接続が強制的に閉じられる可能性があります。

### 実行設定

`run_config` パラメーターを使うと、エージェント実行のグローバル設定を行えます。

#### 共通の実行設定カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに単一実行の動作を上書きできます。

##### モデル、プロバイダー、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名解決に使うモデルプロバイダーです。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有設定を上書きします。たとえばグローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴取得する際のセッションレベルのデフォルト（例: `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターン前に新規ユーザー入力をセッション履歴へどうマージするかをカスタマイズします。コールバックは同期 / 非同期どちらでも可能です。

##### ガードレール、ハンドオフ、モデル入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力 / 出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ側に未設定の場合、すべてのハンドオフに適用するグローバル入力フィルターです。新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: オプトイン beta 機能で、次のエージェント呼び出し前に過去 transcript を単一の assistant メッセージにまとめます。ネストしたハンドオフ安定化中のためデフォルト無効です。有効化は `True`、raw transcript をそのまま渡す場合は `False` のままにします。[Runner methods][agents.run.Runner] は `RunConfig` 未指定時に自動生成されるため、クイックスタートやコード例ではデフォルト無効のままです。また、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個別ハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] で上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` を有効化した際に、正規化済み transcript（履歴 + handoff items）を受け取る任意 callable です。次エージェントへ渡す入力アイテムの**正確なリスト**を返す必要があり、完全な handoff filter を書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するフックです。例: 履歴の切り詰め、システムプロンプトの挿入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が過去出力を次ターンのモデル入力へ変換する際、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: [`TracingConfig`][agents.tracing.TracingConfig] を渡して、この実行の exporter、processor、またはトレーシングメタデータを上書きします。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微データをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング用 workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は任意で、複数実行のトレースを関連付けできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

##### ツール承認とツールエラー時の動作

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フロー中にツール呼び出しが拒否されたとき、モデルに見えるメッセージをカスタマイズします。

ネストしたハンドオフはオプトイン beta として利用できます。transcript 圧縮動作は `RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで `handoff(..., nest_handoff_history=True)` を設定して有効化します。raw transcript（デフォルト）を維持したい場合はフラグを未設定のままにするか、必要な形で会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成サマリーのラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルト復元は [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### 実行設定詳細

##### `tool_error_formatter`

`tool_error_formatter` は、承認フローでツール呼び出しが拒否された際にモデルへ返すメッセージをカスタマイズするために使います。

formatter には以下を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] が渡されます。

-   `kind`: エラーカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`: ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`）。
-   `tool_name`: ツール名。
-   `call_id`: ツール呼び出し ID。
-   `default_message`: SDK のデフォルトのモデル可視メッセージ。
-   `run_context`: アクティブな実行コンテキストラッパー。

メッセージを置き換える文字列を返すか、SDK デフォルトを使うには `None` を返します。

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

`reasoning_item_id_policy` は、runner が履歴を次ターンへ引き継ぐ際（例: `RunResult.to_input_list()` や session ベース実行）に、reasoning item を次ターンのモデル入力へどう変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）: reasoning item ID を保持します。
-   `"omit"`: 生成される次ターン入力から reasoning item ID を除去します。

`"omit"` は主に、reasoning item が `id` を持つのに必須の後続 item がない場合に発生する Responses API の 400 エラー群へのオプトイン緩和策として使います（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは複数ターンのエージェント実行で、SDK が過去出力から追加入力を構築する際（session 永続化、サーバー管理会話 delta、ストリーミング / 非ストリーミング追加入力ターン、再開経路を含む）に、reasoning item ID が保持され、かつ provider 側がその ID を対応する後続 item と対にすることを要求する場合に起こり得ます。

`reasoning_item_id_policy="omit"` を設定すると、reasoning 内容は保持したまま reasoning item の `id` だけを除去するため、SDK 生成の追加入力でこの API 不変条件に触れるのを回避できます。

適用範囲の注意:

-   変更対象は、SDK が追加入力を構築する際に生成 / 転送する reasoning items のみです。
-   ユーザー提供の初期入力 items は書き換えません。
-   `call_model_input_filter` は、このポリシー適用後でも意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

状態を次ターンに引き継ぐ一般的な方法は 4 つあります。

| Strategy | Where state lives | Best for | What you pass on the next turn |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模チャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` のリスト + 次のユーザーメッセージ |
| `session` | ストレージ + SDK | 永続チャット状態、再開可能実行、カスタムストア | 同じ `session` インスタンス、または同一ストアを指す別インスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカー / サービス間で共有したい、名前付きサーバー側会話 | 同じ `conversation_id` + 新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作らない軽量なサーバー管理継続 | `result.last_response_id` + 新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API 利用時のみ適用されます。多くのアプリでは、1 会話につき永続化戦略は 1 つ選んでください。クライアント管理履歴と OpenAI 管理状態を混在させると、意図して両層を突き合わせない限りコンテキスト重複が起こり得ます。

!!! note

    セッション永続化はサーバー管理会話設定
    （`conversation_id`、`previous_response_id`、`auto_previous_response_id`）と
    同一実行で併用できません。
    呼び出しごとに 1 つの方法を選んでください。

### 会話 / チャットスレッド

どの run メソッド呼び出しでも、1 つ以上のエージェントが実行され（したがって 1 回以上の LLM 呼び出しが発生し）ますが、チャット会話では 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキスト入力
2. Runner 実行: 最初のエージェントが LLM 呼び出し、ツール実行、2 番目のエージェントへハンドオフ、2 番目のエージェントがさらにツール実行し、最終的に出力を生成

エージェント実行の最後に、ユーザーに何を表示するかを選べます。たとえば、エージェントが生成したすべての新規 item を表示することも、最終出力のみ表示することもできます。いずれの場合も、ユーザーが続けて質問したら run メソッドを再度呼び出せます。

#### 手動の会話管理

次ターンの入力を取得するには、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] を使って会話履歴を手動管理できます。

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

よりシンプルな方法として、[Sessions](sessions/index.md) を使えば `.to_input_list()` を手動で呼ばずに会話履歴を自動処理できます。

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
-   異なる session ID ごとに別会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の会話状態機能でサーバー側に会話状態を管理させることもできます。これにより、過去メッセージを毎回手動で再送せずに会話履歴を保持できます。以下いずれのサーバー管理方式でも、各リクエストでは新規ターンの入力のみを渡し、保存済み ID を再利用します。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI にはターン間で状態を追跡する方法が 2 つあります。

##### 1. `conversation_id` を使う

まず OpenAI Conversations API で会話を作成し、その後のすべての呼び出しでその ID を再利用します。

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

##### 2. `previous_response_id` を使う

もう 1 つの方法は **response chaining** で、各ターンを前ターンの response ID に明示的に連結します。

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
設定を保持するため、再開ターンは同じサーバー管理会話で継続されます。

`conversation_id` と `previous_response_id` は排他的です。システム間共有可能な名前付き会話リソースが必要な場合は `conversation_id` を使います。ターン間の最も軽量な Responses API 継続プリミティブが必要な場合は `previous_response_id` を使います。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動再試行します。サーバー管理
    会話実行では、再試行前に内部会話トラッカー入力を巻き戻し、同じ準備済み item を
    クリーンに再送できるようにします。

    ローカルの session ベース実行（`conversation_id`、`previous_response_id`、
    `auto_previous_response_id` と併用不可）でも、SDK はベストエフォートで最近永続化した
    入力 item のロールバックを行い、再試行後の履歴重複を減らします。

    この互換性再試行は `ModelSettings.retry` 未設定でも実行されます。モデルリクエストの
    より広いオプトイン再試行動作は [Runner 管理リトライ](models/index.md#runner-managed-retries)
    を参照してください。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

`call_model_input_filter` を使うと、モデル呼び出し直前にモデル入力を編集できます。このフックは現在のエージェント、コンテキスト、結合済み入力 item（存在する場合は session 履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。`input` フィールドは必須で、入力 item のリストでなければなりません。これ以外の形を返すと `UserError` が送出されます。

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

runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元リストをその場で変更せずに、切り詰め、置換、並べ替えができます。

session を使用している場合、`call_model_input_filter` は session 履歴の読み込みと現在ターンへのマージが完了した後に実行されます。その前段のマージ処理自体をカスタマイズしたい場合は [`session_input_callback`][agents.run.RunConfig.session_input_callback] を使ってください。

`conversation_id`、`previous_response_id`、`auto_previous_response_id` を使う OpenAI サーバー管理会話状態では、このフックは次の Responses API 呼び出し用に準備された payload に対して実行されます。その payload は、過去履歴の完全再送ではなく新規ターン delta のみを表している場合があります。サーバー管理継続で送信済みとしてマークされるのは、あなたが返した items のみです。

機微データのマスキング、長い履歴の切り詰め、追加システムガイダンス挿入などのために、`run_config` で実行単位にこのフックを設定してください。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーにした dict である `error_handlers` を受け取れます。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出せず、制御された最終出力を返したい場合に使います。

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

ツール承認の一時停止 / 再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の連携は、実行が長時間待機、再試行、プロセス再起動にまたがる場合の durable なオーケストレーション向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む durable で長時間実行ワークフローを実行できます。長時間タスク完了に向けて Temporal と Agents SDK が連携して動作するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を参照し、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、human approval、ハンドオフ、セッション管理を含む軽量で durable なエージェントを構築できます。この連携には依存関係として Restate の single-binary runtime が必要で、process / container または serverless functions としてエージェント実行をサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進行状況を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期 / 非同期メソッドの両方に対応しています。この連携に必要なのは SQLite または Postgres データベースのみです。詳細は連携 [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的な例外はこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` 上限を超えたときに送出されます。指定ターン数内でタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。次を含みます。
    -   不正な JSON: モデルがツール呼び出しや直接出力で不正な JSON 構造を返した場合（特に特定の `output_type` が定義されている場合）。
    -   想定外のツール関連失敗: モデルが想定どおりにツールを使えない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 関数ツール呼び出しが設定されたタイムアウトを超え、かつツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]: あなた（SDK を使ってコードを書く人）が SDK 利用時に誤りをしたときに送出されます。通常は、コード実装の誤り、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件が満たされたときに、それぞれ送出されます。入力ガードレールは受信メッセージを処理前に検査し、出力ガードレールはエージェントの最終応答を配信前に検査します。