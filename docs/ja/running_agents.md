---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラス経由で実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままストリーミングします。

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

`Runner` の run メソッドを使う場合、開始エージェントと入力を渡します。入力には次のものを使えます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力アイテムのリスト
-   中断された実行を再開する際の [`RunState`][agents.run_state.RunState]

その後、Runner は次のループを実行します。

1. 現在の入力を使って、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力を「最終出力」とみなす条件は、期待される型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使うと、LLM 実行中のストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新規出力を含む実行情報一式が入ります。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket トランスポートを有効にしても、通常の `Runner` API をそのまま使えます。接続再利用には websocket セッションヘルパーを推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルールや、具体的な model オブジェクト / カスタムプロバイダー周りの注意点は [Models](models/index.md#responses-websocket-transport) を参照してください。

##### パターン 1：セッションヘルパーなし（動作します）

websocket トランスポートだけを使いたく、SDK に共有プロバイダー / セッション管理を任せる必要がない場合に使います。

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

このパターンは単発実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼ぶ場合、同じ `RunConfig` / プロバイダーインスタンスを手動で再利用しない限り、実行ごとに再接続される可能性があります。

##### パターン 2：`responses_websocket_session()` を使用（複数ターン再利用に推奨）

複数実行で websocket 対応プロバイダーと `RunConfig` を共有したい場合（同じ `run_config` を継承するネストした agent-as-tool 呼び出しを含む）は [`responses_websocket_session()`][agents.responses_websocket_session] を使います。

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

コンテキストを抜ける前に、ストリーミング結果の消費を完了してください。websocket リクエスト処理中にコンテキストを終了すると、共有接続が強制クローズされる可能性があります。

### 実行設定

`run_config` パラメーターでは、エージェント実行に対するグローバル設定をいくつか構成できます。

#### 共通実行設定カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに単一実行の挙動を上書きできます。

##### model / provider / session の既定値

-   [`model`][agents.run.RunConfig.model]：各 Agent の `model` に関係なく、グローバルに使う LLM model を設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：model 名を解決する model provider です。既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有設定を上書きします。例：グローバルな `temperature` や `top_p`。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際の session レベル既定値（例：`SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions 使用時に、各ターン前に新しいユーザー入力と session 履歴をどう統合するかをカスタマイズします。コールバックは sync / async のどちらでも可能です。

##### ガードレール / ハンドオフ / model 入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：全実行に含める入力 / 出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ側に既存フィルターがない場合に、全ハンドオフへ適用するグローバル入力フィルターです。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次エージェント呼び出し前に、直前までの transcript を 1 つの assistant メッセージに折りたたむ opt-in beta 機能です。ネストハンドオフ安定化中のため既定では無効です。`True` で有効化、`False` で raw transcript をそのまま渡します。[Runner メソッド][agents.run.Runner] は `RunConfig` 未指定時に自動生成するため、quickstart とコード例では既定で無効のままです。また、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個別ハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] で上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` を有効化した際に、正規化された transcript（履歴 + ハンドオフアイテム）を受け取る任意 callable です。次エージェントへ渡す入力アイテムの**完全な**リストを返す必要があり、完全なハンドオフフィルターを書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：model 呼び出し直前に、完全に準備された model 入力（instructions と入力アイテム）を編集するフックです。例：履歴のトリム、システムプロンプト注入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：Runner が前回出力を次ターン model 入力へ変換する際、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行の exporter / processor / トレーシングメタデータを上書きする [`TracingConfig`][agents.tracing.TracingConfig] を渡せます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：LLM やツール呼び出しの入力 / 出力など、機微データをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は、複数実行にまたがるトレースを関連付ける任意項目です。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。

##### ツール承認とツールエラー挙動

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フローでツール呼び出しが拒否された際に、model に見えるメッセージをカスタマイズします。

ネストハンドオフは opt-in beta として提供されています。折りたたみ transcript 挙動を有効化するには `RunConfig(nest_handoff_history=True)` を渡すか、特定ハンドオフで `handoff(..., nest_handoff_history=True)` を設定してください。raw transcript（既定）を維持したい場合は、このフラグを未設定のままにするか、必要な形で会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約のラッパーテキストを変えるには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（既定値へ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### 実行設定詳細

##### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否されたときに model へ返すメッセージをカスタマイズできます。

formatter は次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`：エラーカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`：ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`）。
-   `tool_name`：ツール名。
-   `call_id`：ツール呼び出し ID。
-   `default_message`：SDK 既定の model 可視メッセージ。
-   `run_context`：アクティブな run context wrapper。

メッセージを置き換える文字列を返すか、SDK 既定を使うには `None` を返してください。

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

`reasoning_item_id_policy` は、Runner が履歴を次ターンへ引き継ぐ際（例：`RunResult.to_input_list()` や session ベース実行）に、reasoning item を次ターン model 入力へどう変換するかを制御します。

-   `None` または `"preserve"`（既定）：reasoning item ID を保持します。
-   `"omit"`：生成される次ターン入力から reasoning item ID を削除します。

`"omit"` は主に、reasoning item に `id` があるのに必須の後続 item がない場合に起きる Responses API 400 エラー群への opt-in 緩和策です（例：`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が過去出力からフォローアップ入力を構成する複数ターンエージェント実行（session 永続化、サーバー管理 conversation delta、ストリーミング / 非ストリーミングのフォローアップターン、再開パスを含む）で、reasoning item ID は保持されたが、provider 側がその ID と対応する後続 item のペア維持を要求する場合に起こり得ます。

`reasoning_item_id_policy="omit"` を設定すると reasoning 内容は保持しつつ reasoning item の `id` を削除するため、SDK 生成フォローアップ入力でこの API 不変条件を引き起こさずに済みます。

適用範囲の注意：

-   変更対象は、SDK がフォローアップ入力を組み立てる際に生成 / 転送する reasoning item のみです。
-   ユーザー提供の初期入力 item は書き換えません。
-   `call_model_input_filter` は、このポリシー適用後に意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

状態を次ターンへ引き継ぐ一般的な方法は 4 つあります。

| Strategy | Where state lives | Best for | What you pass on the next turn |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリ側メモリ | 小規模なチャットループ、完全手動制御、任意プロバイダー | `result.to_input_list()` のリスト + 次のユーザーメッセージ |
| `session` | ストレージ + SDK | 永続チャット状態、再開可能実行、カスタムストア | 同一 `session` インスタンス、または同じストアを指す別インスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカー / サービス間で共有したい名前付きサーバー側会話 | 同じ `conversation_id` + 新規ユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | conversation リソースを作らない軽量サーバー管理継続 | `result.last_response_id` + 新規ユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API を使う場合にのみ適用されます。ほとんどのアプリでは、1 会話につき永続化戦略は 1 つ選ぶのが基本です。クライアント管理履歴と OpenAI 管理状態を混在させると、意図的に両層を調停しない限りコンテキストが重複する可能性があります。

!!! note

    Session 永続化は、サーバー管理会話設定
    （`conversation_id`、`previous_response_id`、`auto_previous_response_id`）と
    同一実行で併用できません。呼び出しごとに 1 つの方式を選んでください。

### 会話 / チャットスレッド

いずれの run メソッド呼び出しでも 1 つ以上のエージェント実行（つまり 1 回以上の LLM 呼び出し）が起こり得ますが、チャット会話における単一の論理ターンを表します。例：

1. ユーザーターン：ユーザーがテキスト入力
2. Runner 実行：最初のエージェントが LLM 呼び出し、ツール実行、2 つ目のエージェントへハンドオフ、2 つ目のエージェントが追加ツール実行後に出力生成

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成した全新規 item を表示することも、最終出力だけ表示することもできます。いずれの場合も、ユーザーが追質問したら run メソッドを再度呼び出せます。

#### 手動会話管理

次ターン入力を取得するには、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] で会話履歴を手動管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使えば `.to_input_list()` を手動で呼ばずに会話履歴を自動処理できます。

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
-   session ID ごとに別会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の conversation state 機能にサーバー側会話状態管理を任せることもできます。これにより、過去メッセージを毎回手動で再送せずに会話履歴を保持できます。以下いずれのサーバー管理方式でも、各リクエストでは新規ターン入力のみを渡し、保存済み ID を再利用します。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間状態追跡に 2 つの方法を提供します。

##### 1. `conversation_id` の使用

まず OpenAI Conversations API で conversation を作成し、その ID を以後の呼び出しで再利用します。

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

実行が承認待ちで一時停止し、[`RunState`][agents.run_state.RunState] から再開する場合、
SDK は保存された `conversation_id` / `previous_response_id` / `auto_previous_response_id`
設定を保持するため、再開ターンも同じサーバー管理会話で継続します。

`conversation_id` と `previous_response_id` は相互排他です。システム間共有可能な名前付き conversation リソースが必要なら `conversation_id` を使ってください。ターン間継続における最軽量の Responses API プリミティブを望むなら `previous_response_id` を使ってください。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動再試行します。サーバー管理
    会話実行では、再試行前に内部 conversation-tracker 入力を巻き戻し、
    同じ準備済み item をクリーンに再送できるようにします。

    ローカルな session ベース実行（`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` と併用不可）でも、
    SDK は再試行後の履歴重複を減らすため、直近で永続化した入力 item の
    ベストエフォートなロールバックを行います。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

`call_model_input_filter` を使うと、model 呼び出し直前に model 入力を編集できます。このフックは現在のエージェント、コンテキスト、結合済み入力 item（存在する場合は session 履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

機微データのマスキング、長い履歴のトリム、追加システムガイダンス注入などのために、`run_config` で実行単位にフックを設定してください。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーにした dict `error_handlers` を受け付けます。現時点で対応キーは `"max_turns"` です。`MaxTurnsExceeded` を送出せず、制御された最終出力を返したい場合に使います。

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

フォールバック出力を会話履歴に追加したくない場合は `include_in_history=False` を設定してください。

## Durable execution 連携と human-in-the-loop

ツール承認の pause / resume パターンは、まず専用の [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。
以下の連携は、長時間待機、再試行、プロセス再起動にまたがる実行の durable オーケストレーション向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む durable な長時間ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を参照し、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) を参照してください。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、human approval、ハンドオフ、session 管理を含む軽量で durable なエージェントを実装できます。この連携には依存関係として Restate の single-binary runtime が必要で、プロセス / コンテナー実行と serverless function の両方をサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [docs](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進行状況を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。sync / async メソッドの両方に対応します。この連携に必要なのは SQLite または Postgres データベースのみです。詳細は連携 [repo](https://github.com/dbos-inc/dbos-openai-agents) と [docs](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定ケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的例外はこの型を継承します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 上限を超えた場合に送出されます。指定された対話ターン数内でエージェントがタスク完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤 model（LLM）が予期しない、または無効な出力を生成した場合に発生します。例：
    -   不正な JSON：特に特定の `output_type` が定義されている場合に、ツール呼び出し用または直接出力として不正な JSON 構造を model が返す場合。
    -   想定外のツール関連失敗：model が想定どおりにツールを利用できない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定タイムアウトを超過し、かつツールが `timeout_behavior="raise_exception"` を使っている場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK 利用コードを書く側（あなた）が SDK 使用時に誤りをした場合に送出されます。通常、不正なコード実装、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：それぞれ入力ガードレールまたは出力ガードレールの条件を満たした場合に送出されます。入力ガードレールは処理前の受信メッセージを検査し、出力ガードレールは配信前のエージェント最終応答を検査します。