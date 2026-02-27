---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントをそのままストリーミングします。

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

詳細は [結果ガイド](results.md) をご覧ください。

## Runner ライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使うときは、開始エージェントと入力を渡します。入力は次のいずれかです。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力項目のリスト
-   中断した実行を再開する場合の [`RunState`][agents.run_state.RunState]

その後、runner は次のループを実行します。

1. 現在の入力で、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えた場合は、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力を「最終出力」と見なす条件は、目的の型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使うと、LLM の実行中にイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新規出力を含む実行情報全体が含まれます。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [ストリーミングガイド](streaming.md) をご覧ください。

#### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket transport を有効にすると、通常の `Runner` API を引き続き使用できます。接続再利用には websocket session helper の利用を推奨しますが、必須ではありません。

これは websocket transport 上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

##### パターン 1: セッションヘルパーなし（動作します）

websocket transport のみを使いたく、SDK に共有 provider / session の管理を任せる必要がない場合に使います。

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

このパターンは単発実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼ぶ場合、同じ `RunConfig` / provider インスタンスを手動で再利用しないと、実行ごとに再接続されることがあります。

##### パターン 2: `responses_websocket_session()` を使用（マルチターン再利用に推奨）

複数回の実行で websocket 対応の共有 provider と `RunConfig` を使いたい場合（同じ `run_config` を継承するネストした agent-as-tool 呼び出しを含む）は、[`responses_websocket_session()`][agents.responses_websocket_session] を使います。

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

コンテキスト終了前にストリーミング結果の消費を完了してください。websocket リクエスト処理中にコンテキストを終了すると、共有接続が強制的に閉じられる場合があります。

### 実行設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます。

#### 共通の実行設定カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに単一実行の挙動を上書きできます。

##### モデル・provider・session の既定値

-   [`model`][agents.run.RunConfig.model]：各 Agent の `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名解決用の model provider です。既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有設定を上書きします。たとえばグローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際の session レベル既定値（例：`SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions 使用時に、各ターン前の session 履歴と新しいユーザー入力のマージ方法をカスタマイズします。コールバックは同期・非同期のどちらでも可能です。

##### ガードレール・ハンドオフ・モデル入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ側に未設定の場合、すべてのハンドオフに適用されるグローバル入力フィルターです。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次のエージェント呼び出し前に過去 transcript を 1 つの assistant メッセージへ折りたたむ opt-in beta です。ネストされたハンドオフ安定化のため既定では無効です。`True` で有効化、`False` で raw transcript をそのまま渡します。[Runner メソッド][agents.run.Runner] は `RunConfig` 未指定時に自動生成されるため、quickstart や例は既定で無効のままです。明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個別ハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] で上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` を有効にした際、正規化 transcript（履歴 + ハンドオフ項目）を受け取る任意 callable です。次エージェントへ渡す入力項目リストをそのまま返す必要があり、完全なハンドオフフィルターを書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出し直前に、準備済みモデル入力（instructions と入力項目）を編集するフックです。たとえば履歴の切り詰めやシステムプロンプトの挿入に使えます。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：runner が過去出力を次ターン入力へ変換する際に、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]：[`TracingConfig`][agents.tracing.TracingConfig] を渡し、この実行の exporter・processor・トレーシングメタデータを上書きできます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：LLM やツール呼び出しの入出力など、機微データをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は複数実行のトレースを関連付ける任意項目です。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。

##### ツール承認とツールエラー挙動

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フローでツール呼び出しが拒否された際、モデルに見えるメッセージをカスタマイズします。

ネストされたハンドオフは opt-in beta で利用可能です。`RunConfig(nest_handoff_history=True)` を渡すか、`handoff(..., nest_handoff_history=True)` を設定すると特定ハンドオフで折りたたみ transcript 挙動を有効化できます。raw transcript（既定）を維持したい場合は、このフラグを設定しないか、必要な形で会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約のラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（既定値へ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出します。

#### 実行設定の詳細

##### `tool_error_formatter`

`tool_error_formatter` は、承認フローでツール呼び出しが拒否されたときにモデルへ返すメッセージをカスタマイズするために使います。

formatter は [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。内容は次の通りです。

-   `kind`：エラーカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`：ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`）。
-   `tool_name`：ツール名。
-   `call_id`：ツール呼び出し ID。
-   `default_message`：SDK 既定のモデル可視メッセージ。
-   `run_context`：アクティブな実行コンテキストラッパー。

文字列を返すとメッセージを置き換え、`None` を返すと SDK 既定を使用します。

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

`reasoning_item_id_policy` は、runner が履歴を引き継ぐ際（例：`RunResult.to_input_list()` や session ベース実行）に、reasoning item を次ターンのモデル入力へどう変換するかを制御します。

-   `None` または `"preserve"`（既定）：reasoning item ID を保持します。
-   `"omit"`：生成される次ターン入力から reasoning item ID を削除します。

`"omit"` は主に、reasoning item に `id` があるのに必須の後続 item がない場合に発生する Responses API 400 エラー（例：`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）への opt-in 緩和策として使います。

これは、SDK が過去出力からフォローアップ入力を構築するマルチターン実行で起こることがあります（session 永続化、サーバー管理会話 delta、ストリーミング / 非ストリーミングのフォローアップターン、再開経路を含む）。reasoning item ID が保持され、provider 側が対応する後続 item との対を要求する場合です。

`reasoning_item_id_policy="omit"` を設定すると reasoning 内容は保持しつつ reasoning item `id` を削除するため、SDK 生成のフォローアップ入力でこの API 不変条件に抵触しません。

スコープに関する注意:

-   これは SDK がフォローアップ入力を構築する際に SDK が生成 / 転送する reasoning item のみを変更します。
-   ユーザー提供の初期入力項目は書き換えません。
-   ポリシー適用後でも、`call_model_input_filter` により意図的に reasoning ID を再導入できます。

## 状態と会話管理

### 会話 / チャットスレッド

どの run メソッドを呼んでも、1 つ以上のエージェント実行（つまり 1 回以上の LLM 呼び出し）になる場合がありますが、会話上は 1 つの論理ターンを表します。例:

1. ユーザーターン：ユーザーがテキストを入力
2. Runner 実行：最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフし、2 つ目のエージェントがさらにツールを実行して出力を生成

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成したすべての新規項目を表示することも、最終出力のみを表示することもできます。いずれの場合も、ユーザーが追質問したら run メソッドを再度呼べます。

#### 会話状態戦略の選択

実行ごとに次のいずれかの方法を使ってください。

| Approach | Best for | What you manage |
| --- | --- | --- |
| 手動（`result.to_input_list()`） | 履歴整形を完全に制御したい場合 | 過去の入力項目を自分で構築して再送します |
| Sessions（`session=...`） | アプリ管理のマルチターンチャット状態 | SDK が選択した backend で履歴を読み書きします |
| サーバー管理（`conversation_id` / `previous_response_id`） | OpenAI にターン状態管理を任せる場合 | ID のみ保存し、会話状態はサーバーが保存します |

!!! note

    Session 永続化は、サーバー管理会話設定
    （`conversation_id`、`previous_response_id`、`auto_previous_response_id`）と
    同一実行で併用できません。呼び出しごとに 1 つの方法を選択してください。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] を使うと、次ターン入力を取得して履歴を手動管理できます。

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

Sessions は次を自動で行います。

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   異なる session ID ごとに別会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) をご覧ください。


#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI conversation state 機能で会話状態をサーバー側管理することもできます。これにより、過去メッセージを毎回手動再送せずに会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI はターン間状態追跡の方法を 2 つ提供しています。

##### 1. `conversation_id` の使用

まず OpenAI Conversations API で会話を作成し、その ID を以後の呼び出しで再利用します。

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

もう 1 つの方法は **response chaining** で、各ターンを前ターンの response ID に明示的にリンクします。

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

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動再試行します。サーバー管理
    会話実行では、再試行前に内部の会話トラッカー入力を巻き戻し、同じ準備済み項目を
    問題なく再送できるようにします。

    ローカルの session ベース実行（`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` とは併用不可）でも、SDK は
    再試行後の重複履歴項目を減らすため、直近で永続化した入力項目をベストエフォートで
    ロールバックします。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

`call_model_input_filter` は、モデル呼び出し直前にモデル入力を編集するために使います。このフックは現在のエージェント、コンテキスト、および結合済み入力項目（存在する場合は session 履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

機微データのマスキング、長い履歴の切り詰め、追加のシステムガイダンス挿入などのために、`run_config` で実行ごとに設定できます。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは `error_handlers`（エラー種別をキーにした dict）を受け取れます。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに制御された最終出力を返したい場合に使います。

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

## Durable execution 連携と human-in-the-loop

ツール承認の一時停止 / 再開パターンは、まず専用の [Human-in-the-loop ガイド](human_in_the_loop.md) をご覧ください。
以下の連携は、実行が長時間待機、再試行、プロセス再起動にまたがる場合の durable orchestration 向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む durable な長時間ワークフローを実行できます。長時間タスク完了に向けて Temporal と Agents SDK が連携するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) をご覧ください。ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、human approval、ハンドオフ、session 管理を含む軽量で durable なエージェントを実行できます。この連携には Restate の single-binary runtime 依存関係が必要で、process / container または serverless functions としてエージェントを実行できます。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [ドキュメント](https://docs.restate.dev/ai) をご覧ください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフに対応しています。同期 / 非同期メソッドの両方をサポートします。この連携に必要なのは SQLite または Postgres データベースのみです。詳細は連携 [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) をご覧ください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次の通りです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的例外の汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 上限を超えたときに送出されます。指定ターン数内でタスク完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。これには次が含まれます。
    -   不正な JSON：ツール呼び出しや直接出力で、不正な JSON 構造がモデルから返された場合（特に特定の `output_type` が定義されている場合）。
    -   予期しないツール関連の失敗：モデルが期待された方法でツールを使用できなかった場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定されたタイムアウトを超え、かつツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK を使うあなた（SDK を使ってコードを書く人）が SDK 利用時に誤りを行った場合に送出されます。通常、誤ったコード実装、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：それぞれ入力ガードレールまたは出力ガードレールの条件に一致した場合に送出されます。入力ガードレールは受信メッセージを処理前にチェックし、出力ガードレールは配信前のエージェント最終応答をチェックします。