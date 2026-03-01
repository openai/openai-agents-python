---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラス経由でエージェントを実行できます。選択肢は 3 つあります。

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

詳細は [実行結果ガイド](results.md) を参照してください。

## Runner ライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使う際は、開始エージェントと入力を渡します。入力には次を指定できます。

-   文字列（ユーザーメッセージとして扱われます）
-   OpenAI Responses API 形式の入力アイテムのリスト
-   中断した実行を再開する場合の [`RunState`][agents.run_state.RunState]

その後、runner は次のループを実行します。

1. 現在の入力で、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えると、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力を「最終出力」とみなすルールは、望ましい型のテキスト出力を生成し、かつツール呼び出しが存在しないことです。

### ストリーミング

ストリーミングを使うと、LLM 実行中のストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成された新しい出力を含む実行の完全な情報が入ります。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

#### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket トランスポートを有効化すると、通常の `Runner` API をそのまま利用できます。接続再利用には websocket session helper の利用を推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

##### パターン 1: session helper なし（動作します）

websocket トランスポートだけを使いたく、SDK に共有 provider / session の管理を任せる必要がない場合はこちらを使います。

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

このパターンは単発実行に適しています。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼ぶ場合、同じ `RunConfig` / provider インスタンスを手動で再利用しない限り、各実行で再接続が発生する可能性があります。

##### パターン 2: `responses_websocket_session()` を使用（複数ターン再利用に推奨）

複数実行間で websocket 対応 provider と `RunConfig` を共有したい場合（同じ `run_config` を継承するネストされた agent-as-tool 呼び出しを含む）は、[`responses_websocket_session()`][agents.responses_websocket_session] を使います。

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

コンテキストを抜ける前に、ストリーミング結果の消費を完了してください。websocket リクエスト実行中にコンテキストを終了すると、共有接続が強制的に閉じられる場合があります。

### 実行設定

`run_config` パラメーターで、エージェント実行に関するグローバル設定を構成できます。

#### 一般的な実行設定カテゴリー

`RunConfig` を使うと、各エージェント定義を変更せずに、単一実行の挙動を上書きできます。

##### モデル、provider、session のデフォルト

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、グローバルで使用する LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名解決に使う model provider で、既定値は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有設定を上書きします。たとえばグローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際の session レベル既定値（例: `SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 利用時に、各ターン前に新規ユーザー入力を session 履歴へ統合する方法をカスタマイズします。callback は同期・非同期のどちらも使用できます。

##### ガードレール、ハンドオフ、モデル入力整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ側で未設定の場合に、すべてのハンドオフへ適用するグローバル入力フィルターです。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェント呼び出し前に、直前までの transcript を 1 つの assistant message に折りたたむ opt-in beta です。ネストされたハンドオフの安定化中のため既定では無効です。有効化は `True`、raw transcript をそのまま渡すには `False` を使います。[Runner methods][agents.run.Runner] は `RunConfig` 未指定時に自動作成されるため、quickstart と examples は既定で無効のままです。また明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] callback は引き続きこれを上書きします。個別ハンドオフでは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` を有効化した際に、正規化された transcript（履歴 + handoff items）を受け取る任意 callable です。次エージェントに転送する入力アイテムの完全なリストを返す必要があり、完全なハンドオフフィルターを書かずに組み込み要約を置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前の、完全に準備済みのモデル入力（instructions と input items）を編集するフックです。例: 履歴の短縮、システムプロンプトの注入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner が過去出力を次ターンのモデル入力へ変換する際に、reasoning item ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: [`TracingConfig`][agents.tracing.TracingConfig] を渡して、この実行の exporter、processor、トレーシング metadata を上書きできます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微データをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング workflow 名、trace ID、trace group ID を設定します。最低でも `workflow_name` の設定を推奨します。group ID は任意で、複数実行のトレースを関連付けできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含める metadata です。

##### ツール承認とツールエラー挙動

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フローでツール呼び出しが拒否された際に、モデルへ見せるメッセージをカスタマイズします。

ネストされたハンドオフは opt-in beta として利用可能です。折りたたみ transcript 挙動を有効にするには `RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで有効化するには `handoff(..., nest_handoff_history=True)` を設定します。raw transcript（既定）を維持したい場合は、フラグを未設定のままにするか、必要な会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約のラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（既定に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

#### 実行設定詳細

##### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否された際にモデルへ返すメッセージをカスタマイズできます。

formatter は [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。内容は次の通りです。

-   `kind`: エラーカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`: ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`）。
-   `tool_name`: ツール名。
-   `call_id`: ツール呼び出し ID。
-   `default_message`: SDK 既定のモデル表示メッセージ。
-   `run_context`: アクティブな実行コンテキストラッパー。

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

`reasoning_item_id_policy` は、runner が履歴を次ターンに引き継ぐ際（例: `RunResult.to_input_list()` や session ベース実行）に、reasoning items を次ターンモデル入力へどう変換するかを制御します。

-   `None` または `"preserve"`（既定）: reasoning item ID を保持します。
-   `"omit"`: 生成される次ターン入力から reasoning item ID を削除します。

`"omit"` は主に、reasoning item に `id` があるのに必須の後続 item がない場合に発生する Responses API 400 エラー群への opt-in 緩和策として使用します（例: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が過去出力から追加入力を構築する複数ターンのエージェント実行（session 永続化、サーバー管理会話差分、ストリーミング / 非ストリーミングの追加入力ターン、再開パスを含む）で、reasoning item ID が保持されつつ、provider 側がその ID と対応する後続 item のペア維持を要求する場合に起こりえます。

`reasoning_item_id_policy="omit"` を設定すると、reasoning content は保持したまま reasoning item の `id` を取り除くため、SDK 生成の追加入力でこの API 不変条件の違反を回避できます。

スコープに関する注意:

-   変更対象は、SDK が追加入力を構築する際に生成 / 転送する reasoning items のみです。
-   ユーザー提供の初期入力 items は書き換えません。
-   `call_model_input_filter` は、このポリシー適用後に意図的に reasoning IDs を再導入できます。

## 状態と会話管理

### メモリ戦略の選択

次ターンへ状態を引き継ぐ一般的な方法は 4 つあります。

| Strategy | Where state lives | Best for | What you pass on the next turn |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意の provider | `result.to_input_list()` のリスト + 次のユーザーメッセージ |
| `session` | あなたのストレージ + SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを指す別インスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有したい名前付きサーバー側会話 | 同じ `conversation_id` + 新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作らない軽量なサーバー管理継続 | `result.last_response_id` + 新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理で、OpenAI Responses API 使用時にのみ適用されます。多くのアプリでは、会話ごとに永続化戦略を 1 つ選ぶのが推奨です。クライアント管理履歴と OpenAI 管理状態を混在させると、意図的に両レイヤーを整合させない限りコンテキストが重複する可能性があります。

!!! note

    Session 永続化は、サーバー管理会話設定
    （`conversation_id`、`previous_response_id`、`auto_previous_response_id`）と
    同一実行で併用できません。呼び出しごとに 1 つの方式を選択してください。

### 会話 / チャットスレッド

どの run メソッドを呼んでも、1 つ以上のエージェント（したがって 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話上は 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフし、2 つ目のエージェントがさらにツールを実行して出力を生成

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成したすべての新規 item を見せることも、最終出力のみを見せることもできます。どちらの場合でも、ユーザーが追質問したら run メソッドを再度呼び出せます。

#### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使うと、次ターンの入力を取得して会話履歴を手動管理できます。

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

Sessions は自動で次を行います。

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   異なる session ID ごとに会話を分離して維持

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。


#### サーバー管理会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の会話状態機能でサーバー側の会話状態を管理することもできます。これにより、過去メッセージを毎回手動再送せずに履歴を保持できます。以下いずれのサーバー管理方式でも、各リクエストでは新しいターン入力のみを渡し、保存済み ID を再利用してください。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI にはターン間の状態追跡方法が 2 つあります。

##### 1. `conversation_id` を使用

まず OpenAI Conversations API で会話を作成し、その ID を以後のすべての呼び出しで再利用します。

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

もう 1 つは **response chaining** で、各ターンを前ターンの response ID に明示的に紐付けます。

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
設定を保持するため、再開ターンも同じサーバー管理会話で継続されます。

`conversation_id` と `previous_response_id` は排他的です。システム間で共有できる名前付き会話リソースが必要なら `conversation_id` を使います。ターン間の最小限な Responses API 継続プリミティブが必要なら `previous_response_id` を使います。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動再試行します。サーバー管理
    会話実行では、再試行前に内部の conversation-tracker 入力を巻き戻し、同じ準備済み item を
    きれいに再送できるようにします。

    ローカル session ベース実行（`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` とは併用不可）でも、SDK は
    再試行後の履歴重複を減らすため、直近で永続化された入力 items の最善努力ロールバックを行います。

## フックとカスタマイズ

### モデル呼び出し入力フィルター

`call_model_input_filter` を使うと、モデル呼び出し直前にモデル入力を編集できます。このフックは現在のエージェント、コンテキスト、結合済み入力 items（存在する場合は session 履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

機微データのマスキング、長い履歴の短縮、追加のシステムガイダンス注入には、`run_config` 経由で実行ごとにこのフックを設定してください。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーにした dict である `error_handlers` を受け付けます。現時点での対応キーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに、制御された最終出力を返したい場合に使います。

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

## Durable execution 統合と human-in-the-loop

ツール承認の pause / resume パターンについては、まず専用の [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。
以下の統合は、実行が長い待機、再試行、プロセス再起動にまたがる場合の durable なオーケストレーション向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使うと、human-in-the-loop タスクを含む durable で長時間実行されるワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使うと、human approval、ハンドオフ、session 管理を含む軽量で durable なエージェントを実行できます。この統合は Restate の single-binary runtime を依存関係として必要とし、process / container と serverless functions の両方でエージェント実行をサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使うと、障害や再起動をまたいで進行状況を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期 / 非同期の両メソッドに対応しています。この統合に必要なのは SQLite または Postgres データベースのみです。詳細は統合 [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次の通りです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的例外はこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェント実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 制限を超えた場合に送出されます。指定ターン数内でタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには次が含まれます。
    -   不正な JSON: モデルがツール呼び出し用、または直接出力で不正な JSON 構造を返す場合（特に特定の `output_type` が定義されている場合）
    -   予期しないツール関連の失敗: モデルが期待される方法でツールを使えない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 関数ツール呼び出しが設定タイムアウトを超え、かつツールが `timeout_behavior="raise_exception"` を使っている場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]: SDK 利用時に（SDK を使うコードを書く）あなたが誤りをした場合に送出されます。通常は誤ったコード実装、無効な設定、または SDK API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件に合致した場合にそれぞれ送出されます。入力ガードレールは処理前に受信メッセージを検査し、出力ガードレールは配信前にエージェントの最終応答を検査します。