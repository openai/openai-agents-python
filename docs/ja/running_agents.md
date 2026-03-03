---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを介してエージェントを実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的に `.run()` を実行するだけです。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントを逐次ストリーミングします。

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

詳細は [実行結果ガイド](results.md) をご覧ください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使う際は、開始エージェントと入力を渡します。入力には次を指定できます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力アイテムのリスト
-   中断された実行を再開する際の [`RunState`][agents.run_state.RunState]

その後、runner は次のループを実行します。

1. 現在の入力で、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えた場合は、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力を「final output」と見なすルールは、期待される型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、この実行に関する完全な情報（新しく生成されたすべての出力を含む）が格納されます。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [ストリーミングガイド](streaming.md) をご覧ください。

#### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket transport を有効にすると、通常の `Runner` API をそのまま使えます。接続再利用には websocket session helper を推奨しますが、必須ではありません。

これは websocket transport 上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

トランスポート選択ルールと、具体的な model オブジェクトや custom provider に関する注意点は、[Models](models/index.md#responses-websocket-transport) をご覧ください。

##### パターン 1：session helper なし（動作します）

websocket transport を使いたいだけで、共有 provider/session の管理を SDK に任せる必要がない場合に使います。

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

このパターンは単発実行には問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼び出す場合、同じ `RunConfig` / provider インスタンスを手動で再利用しない限り、実行ごとに再接続する可能性があります。

##### パターン 2：`responses_websocket_session()` を使用（複数ターン再利用に推奨）

複数回の実行で websocket 対応 provider と `RunConfig` を共有したい場合（同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含む）は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します。

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

ストリーミング結果の消費はコンテキスト終了前に完了してください。websocket リクエストが処理中のままコンテキストを終了すると、共有接続が強制的に閉じられる場合があります。

### 実行設定

`run_config` パラメーターを使うと、エージェント実行のグローバル設定をいくつか構成できます。

#### 一般的な実行設定カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに単一実行の動作を上書きできます。

##### model・provider・session のデフォルト

-   [`model`][agents.run.RunConfig.model]：各 Agent の `model` 設定に関係なく、グローバルに使う LLM model を設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：model 名解決に使う model provider で、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有設定を上書きします。たとえばグローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際の session レベルのデフォルト（例：`SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions 使用時、各ターン前に新しいユーザー入力を session 履歴とどうマージするかをカスタマイズします。callback は sync / async のどちらでも使えます。

##### ガードレール・ハンドオフ・model 入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ側に未設定の場合に、すべてのハンドオフへ適用されるグローバル入力フィルターです。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次のエージェント呼び出し前に、直前までの transcript を 1 つの assistant message に畳み込む opt-in beta 機能です。ネストされたハンドオフの安定化中のため、デフォルトは無効です。有効化は `True`、raw transcript をそのまま渡す場合は `False` のままにしてください。いずれの [Runner methods][agents.run.Runner] も、未指定時は `RunConfig` を自動生成するため、quickstart やコード例ではデフォルトで無効のままです。明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] callback は引き続きこちらを上書きします。個別ハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` を有効にした際に、正規化済み transcript（履歴 + ハンドオフ項目）を受け取る任意 callable です。次のエージェントへ渡す入力アイテムの正確なリストを返す必要があり、完全な handoff filter を書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：model 呼び出し直前に、完全に準備済みの model 入力（instructions と input items）を編集するフックです。例：履歴の切り詰め、システムプロンプトの注入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：runner が過去出力を次ターンの model 入力へ変換する際、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体で [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]：[`TracingConfig`][agents.tracing.TracingConfig] を渡して、この実行の exporter、processor、またはトレーシングメタデータを上書きできます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：LLM やツール呼び出しの入出力など、機微データを trace に含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：この実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は任意で、複数実行にまたがる trace の関連付けに使えます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべての trace に含めるメタデータです。

##### ツール承認とツールエラー動作

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フロー中にツール呼び出しが拒否された際、model に見えるメッセージをカスタマイズします。

ネストされたハンドオフは opt-in beta として利用できます。畳み込み transcript 動作は `RunConfig(nest_handoff_history=True)` を渡すか、`handoff(..., nest_handoff_history=True)` を設定して特定ハンドオフで有効化します。raw transcript（デフォルト）を維持したい場合は、このフラグを未設定のままにするか、必要どおりに会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。custom mapper を書かずに生成要約のラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトへ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### 実行設定の詳細

##### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否された際に model へ返すメッセージをカスタマイズできます。

formatter は次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`：エラーカテゴリー。現在は `"approval_rejected"` です。
-   `tool_type`：ツール実行時タイプ（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`）。
-   `tool_name`：ツール名。
-   `call_id`：ツール呼び出し ID。
-   `default_message`：SDK のデフォルト model 表示メッセージ。
-   `run_context`：アクティブな実行コンテキストラッパー。

メッセージを置き換える文字列、または SDK デフォルトを使う場合は `None` を返してください。

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

`reasoning_item_id_policy` は、runner が履歴を次ターンへ引き継ぐとき（例：`RunResult.to_input_list()` や session ベース実行）に、reasoning item を次ターン model 入力へどう変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）：reasoning item ID を保持します。
-   `"omit"`：生成される次ターン入力から reasoning item ID を取り除きます。

`"omit"` は主に、reasoning item に `id` がある一方で必要な後続 item がない場合に発生する Responses API の 400 エラー群への opt-in 緩和策として使います（例：`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が過去出力から追加入力を構築するマルチターンエージェント実行時（session 永続化、サーバー管理会話差分、ストリーミング / 非ストリーミングの追加入力ターン、再開経路を含む）に、reasoning item ID が保持され、かつ provider 側が対応する後続 item との対での維持を要求する場合に起こりえます。

`reasoning_item_id_policy="omit"` を設定すると、reasoning 内容は保持しつつ reasoning item の `id` を除去するため、SDK 生成の追加入力でその API 制約違反を回避できます。

スコープに関する注意：

-   変更されるのは、SDK が追加入力を構築する際に生成 / 転送する reasoning item のみです。
-   ユーザーが与えた初期入力 item は書き換えません。
-   このポリシー適用後でも、`call_model_input_filter` により意図的に reasoning ID を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

次のターンへ状態を引き継ぐ一般的な方法は 4 つあります。

| Strategy | Where state lives | Best for | What you pass on the next turn |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模チャットループ、完全手動制御、任意 provider | `result.to_input_list()` のリスト + 次のユーザーメッセージ |
| `session` | ストレージ + SDK | 永続チャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別インスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい、名前付きサーバー側会話 | 同じ `conversation_id` + 新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作らない軽量なサーバー管理継続 | `result.last_response_id` + 新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API 使用時のみ適用されます。多くのアプリケーションでは、会話ごとに永続化戦略を 1 つ選んでください。クライアント管理履歴と OpenAI 管理状態を混在させると、意図的に両層を整合しない限りコンテキストが重複する可能性があります。

!!! note

    Session 永続化はサーバー管理会話設定
    （`conversation_id`、`previous_response_id`、`auto_previous_response_id`）と
    同一実行内で併用できません。
    呼び出しごとに 1 つの方式を選択してください。

### 会話 / チャットスレッド

いずれかの run メソッド呼び出しにより、1 つ以上のエージェント（したがって 1 回以上の LLM 呼び出し）が実行される場合がありますが、チャット会話上は 1 つの論理ターンを表します。例：

1. ユーザーターン：ユーザーがテキストを入力
2. Runner 実行：最初のエージェントが LLM 呼び出し、ツール実行、2 つ目のエージェントへハンドオフ、2 つ目のエージェントがさらにツールを実行し、最終的に出力を生成

エージェント実行終了時に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが新規生成した全 item を表示することも、最終出力のみを表示することもできます。その後ユーザーが追質問した場合は、run メソッドを再度呼び出せます。

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
-   各実行後に新しいメッセージを保存
-   異なる session ID ごとに別会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) をご覧ください。


#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の conversation state 機能で会話状態をサーバー側管理することもできます。これにより、過去メッセージ全体を手動再送せずに会話履歴を保持できます。以下いずれのサーバー管理方式でも、各リクエストでは新規ターン入力のみを渡し、保存済み ID を再利用してください。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI ではターン間の状態追跡方法を 2 つ提供しています。

##### 1. `conversation_id` を使用

まず OpenAI Conversations API で会話を作成し、その ID を以降の呼び出しすべてで再利用します。

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

もう 1 つは **response chaining** で、各ターンを直前ターンの response ID に明示的に連結します。

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
設定を維持するため、再開ターンも同じサーバー管理会話で継続されます。

`conversation_id` と `previous_response_id` は同時に使えません。システム間で共有可能な名前付き会話リソースが必要なら `conversation_id` を使ってください。ターン間で最小限の Responses API 継続プリミティブを使いたいなら `previous_response_id` を使ってください。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動リトライします。サーバー管理
    会話実行では、リトライ前に内部 conversation-tracker 入力を巻き戻すため、
    同じ準備済み item をクリーンに再送できます。

    ローカル session ベース実行（`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` とは併用不可）でも、
    SDK はベストエフォートで直近に永続化した入力 item をロールバックし、
    リトライ後の履歴重複エントリを減らします。

## フックとカスタマイズ

### model 呼び出し入力フィルター

`call_model_input_filter` を使うと、model 呼び出し直前に model 入力を編集できます。このフックは現在のエージェント、コンテキスト、統合済み入力 item（存在する場合は session 履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

run ごとの `run_config` でこのフックを設定し、機微データのマスキング、長い履歴の切り詰め、追加システムガイダンスの注入を行えます。

## エラーと復旧

### エラーハンドラー

`Runner` のすべてのエントリポイントは `error_handlers` を受け取ります。これはエラー種別をキーにした dict です。現在サポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに制御された最終出力を返したい場合に使用します。

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

フォールバック出力を会話履歴へ追加したくない場合は `include_in_history=False` を設定してください。

## Durable execution 統合と human-in-the-loop

ツール承認の一時停止 / 再開パターンについては、まず専用の [Human-in-the-loop ガイド](human_in_the_loop.md) をご覧ください。
以下の統合は、長い待機・リトライ・プロセス再起動をまたぐ実行向けの durable なオーケストレーションです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使うと、human-in-the-loop タスクを含む durable で長時間実行可能なワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使うと、人手承認、ハンドオフ、session 管理を含む軽量で durable なエージェントを実行できます。この統合には依存関係として Restate の single-binary runtime が必要で、process/container または serverless function としてのエージェント実行をサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [ドキュメント](https://docs.restate.dev/ai) をご覧ください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使うと、障害や再起動をまたいでも進行状況を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。sync / async 両方のメソッドに対応します。この統合に必要なのは SQLite または Postgres データベースのみです。詳細は統合 [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) をご覧ください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的な例外はこの汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` 上限を超えたときに送出されます。指定ターン数内でタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤 model（LLM）が予期しない、または無効な出力を生成した際に発生します。具体例：
    -   不正な JSON：特に特定の `output_type` が定義されている場合に、ツール呼び出しまたは直接出力で不正な JSON 構造を返したとき。
    -   想定外のツール関連失敗：model が期待どおりの方法でツールを利用しなかったとき
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定されたタイムアウトを超え、かつツールで `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK 利用コードを書くあなた（開発者）が SDK 使用時に誤りをした場合に送出されます。通常は実装ミス、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：それぞれ入力ガードレールまたは出力ガードレールの条件が満たされた場合に送出されます。入力ガードレールは処理前の受信メッセージを検査し、出力ガードレールは配信前のエージェント最終応答を検査します。