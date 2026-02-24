---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部では `.run()` を実行するだけです。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントをそのままあなたに ストリーミング します。

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

詳細は [execution results ガイド](results.md) を参照してください。

## Runner のライフサイクルと設定

### エージェント ループ

`Runner` の run メソッドを使用する際は、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）です。

その後、runner はループを実行します。

1. 現在の入力で、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して execution results を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行して結果を追記し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「final output」と見なされるルールは、望ましい型のテキスト出力を生成し、かつツール呼び出しがないことです。

### ストリーミング

ストリーミング を使用すると、LLM の実行中に ストリーミング イベントも追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行の完全な情報が格納されます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

#### Responses WebSocket transport（任意のヘルパー）

OpenAI Responses websocket transport を有効にすると、通常の `Runner` API を引き続き利用できます。WebSocket セッション ヘルパーは接続再利用のために推奨されますが、必須ではありません。

これは websocket transport 上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

##### パターン 1：セッション ヘルパーなし（動作します）

WebSocket transport だけが必要で、SDK に共有 provider/session の管理を任せる必要がない場合に使用します。

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

このパターンは単発の実行であれば問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼ぶ場合、同じ `RunConfig` / provider インスタンスを手動で再利用しない限り、各実行で再接続される可能性があります。

##### パターン 2：`responses_websocket_session()` を使用（複数ターン再利用に推奨）

複数の実行（同じ `run_config` を継承する、ネストした Agents-as-tools 呼び出しを含む）にわたって、WebSocket 対応の共有 provider と `RunConfig` を使いたい場合は [`responses_websocket_session()`][agents.responses_websocket_session] を使用します。

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

コンテキストを抜ける前に、ストリーミング された execution results の消費を完了してください。WebSocket リクエストがまだ進行中のままコンテキストを終了すると、共有接続が強制的にクローズされる場合があります。

### 実行設定

`run_config` パラメーターにより、エージェント実行のグローバル設定をいくつか構成できます。

#### 一般的な実行設定のカテゴリー

`RunConfig` を使用すると、各エージェント定義を変更せずに、単一の実行に対して挙動を上書きできます。

##### モデル、provider、セッションのデフォルト

-   [`model`][agents.run.RunConfig.model]：各 Agent の `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名の解決に使用するモデル provider で、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえばグローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際のセッション レベルのデフォルト（例：`SessionSettings(limit=...)`）を上書きします。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions を使用する場合に、各ターン前に新しいユーザー入力をセッション履歴とマージする方法をカスタマイズします。

##### ガードレール、ハンドオフ、モデル入力の整形

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ側でまだ指定されていない場合に、すべての ハンドオフ に適用するグローバル入力フィルターです。入力フィルターを使うと、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次のエージェントを呼び出す前に、直前までの transcript を 1 つの assistant メッセージに折りたたむ、オプトインの beta です。ネストした ハンドオフ の安定化のため、デフォルトでは無効です。有効にするには `True`、raw transcript をそのまま通すには `False` のままにしてください。[Runner メソッド][agents.run.Runner] は、渡さない場合に自動で `RunConfig` を作成するため、クイックスタートや examples ではデフォルトのまま（無効）になり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個別の ハンドオフ では [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によりこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` をオプトインした際に、正規化された transcript（履歴 + ハンドオフ アイテム）を受け取る任意の callable です。次のエージェントへ転送する入力アイテムのリストを**そのまま**返す必要があり、完全なハンドオフ フィルターを書かずに、組み込みのサマリーを置き換えられます。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するためのフックです。例：履歴のトリミングや システムプロンプト の注入。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：runner が過去の出力を次ターンのモデル入力に変換する際、reasoning アイテム ID を保持するか省略するかを制御します。

##### トレーシング と可観測性

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行の exporter、プロセッサー、または トレーシング メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに、LLM やツール呼び出しの入出力などの機微情報が含まれ得るデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行の トレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` は設定することを推奨します。グループ ID は、複数の実行にまたがってトレースを関連付けられる任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。

##### ツール承認とツール エラー時の挙動

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フロー中にツール呼び出しが拒否された際、モデルに見えるメッセージをカスタマイズします。

ネストした ハンドオフ はオプトインの beta として利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定の ハンドオフ に対して `handoff(..., nest_handoff_history=True)` を設定して、transcript 折りたたみ挙動を有効にしてください。raw transcript（デフォルト）を維持したい場合はフラグを設定しないか、必要どおりに会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成サマリーで使われるラッパー テキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（既定値に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出します。

#### 実行設定の詳細

##### `tool_error_formatter`

`tool_error_formatter` を使用すると、承認フローでツール呼び出しが拒否されたときにモデルへ返されるメッセージをカスタマイズできます。

formatter は [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。内容は次のとおりです。

-   `kind`：エラーのカテゴリーです。現時点では `"approval_rejected"` です。
-   `tool_type`：ツール runtime（`"function"`、`"computer"`、`"shell"`、または `"apply_patch"`）です。
-   `tool_name`：ツール名です。
-   `call_id`：ツール呼び出し ID です。
-   `default_message`：SDK によるデフォルトのモデル可視メッセージです。
-   `run_context`：アクティブな run context wrapper です。

メッセージを置き換える文字列を返すか、SDK のデフォルトを使うには `None` を返します。

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

`reasoning_item_id_policy` は、runner が履歴を引き継ぐ際（例：`RunResult.to_input_list()` の使用時や Sessions を背後に持つ実行時）に、reasoning アイテムが次ターンのモデル入力へどのように変換されるかを制御します。

-   `None` または `"preserve"`（デフォルト）：reasoning アイテム ID を保持します。
-   `"omit"`：生成される次ターン入力から reasoning アイテム ID を取り除きます。

`"omit"` は主に、Responses API の 400 エラーの一種（reasoning アイテムが `id` を伴って送られたものの、必須の後続アイテムがない場合に発生するもの）へのオプトイン緩和策として使用してください（例：`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が以前の出力からフォローアップ入力を構築する（セッション永続化、サーバー管理の会話差分、ストリーミング／非ストリーミングのフォローアップ ターン、再開パスを含む）複数ターンのエージェント実行において、reasoning アイテム ID が保持される一方で、provider がその ID を対応する後続アイテムとペアのままにすることを要求する場合に起こり得ます。

`reasoning_item_id_policy="omit"` を設定すると reasoning の内容は維持しつつ reasoning アイテムの `id` を取り除くため、SDK が生成するフォローアップ入力でその API 不変条件を踏まないようにできます。

スコープに関する注意点：

-   これは、SDK がフォローアップ入力を構築する際に生成／転送する reasoning アイテムのみを変更します。
-   ユーザーが最初に与えた入力アイテムは書き換えません。
-   `call_model_input_filter` で、このポリシー適用後に意図的に reasoning ID を再導入することは可能です。

## 状態と会話の管理

### 会話／チャット スレッド

いずれかの run メソッドを呼ぶと、1 つ以上のエージェントが実行され（したがって 1 回以上 LLM が呼ばれ）る可能性がありますが、これはチャット会話における 1 つの論理ターンを表します。例：

1. ユーザー ターン：ユーザーがテキストを入力
2. Runner の実行：最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへ ハンドオフ し、2 番目のエージェントがさらにツールを実行して出力を生成

エージェント実行の終了時に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成した新規アイテムをすべて表示することも、final output だけを表示することもできます。いずれの場合でも、ユーザーが追加の質問をしてくる可能性があり、そのときは run メソッドを再度呼び出せます。

#### 会話状態戦略の選択

実行ごとに、次のいずれかのアプローチを使用してください。

| アプローチ | 最適な用途 | 管理するもの |
| --- | --- | --- |
| 手動（`result.to_input_list()`） | 履歴整形を完全に制御 | 過去の入力アイテムを構築して再送 |
| Sessions（`session=...`） | アプリで管理する複数ターンのチャット状態 | SDK が選択した backend に履歴を読み書き |
| サーバー管理（`conversation_id` / `previous_response_id`） | OpenAI にターン状態管理を任せる | ID のみ保存し、サーバーが会話状態を保存 |

!!! note

    セッション永続化は、サーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）と
    同一の実行で併用できません。呼び出しごとに 1 つのアプローチを選択してください。

#### 手動での会話管理

次のターンの入力を得るために [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、会話履歴を手動で管理できます。

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

よりシンプルなアプローチとして、[Sessions](sessions/index.md) を使用すれば、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に扱えます。

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
-   異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。

#### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の conversation state 機能によりサーバー側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送することなく会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI は、ターンをまたいで状態を追跡する 2 つの方法を提供しています。

##### 1. `conversation_id` を使用

まず OpenAI Conversations API を使用して会話を作成し、その ID を後続の呼び出しすべてで再利用します。

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

もう 1 つの選択肢は **response chaining** で、各ターンが前のターンの response ID に明示的にリンクします。

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

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動的にリトライします。サーバー管理の
    会話実行では、リトライ前に内部の conversation-tracker 入力を巻き戻し、同じ準備済みアイテムを
    きれいに再送できるようにします。

    ローカルの session ベース実行（`conversation_id`、`previous_response_id`、または `auto_previous_response_id` と
    併用できません）でも、SDK はリトライ後の重複した履歴エントリを減らすために、直近で永続化された
    入力アイテムのベストエフォートなロールバックを実行します。

## フックとカスタマイズ

### Call model input filter

`call_model_input_filter` を使用すると、モデル呼び出し直前にモデル入力を編集できます。このフックは現在のエージェント、コンテキスト、結合済みの入力アイテム（存在する場合はセッション履歴も含む）を受け取り、新しい `ModelInputData` を返します。

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

機微情報のマスキング、長い履歴のトリミング、追加のシステム ガイダンスの注入を行うために、`run_config` 経由で実行ごとにフックを設定するか、`Runner` のデフォルトとして設定してください。

## エラーと復旧

### エラー ハンドラー

すべての `Runner` エントリ ポイントは、エラー種別をキーにした dict である `error_handlers` を受け付けます。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに、制御された final output を返したい場合に使用してください。

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

フォールバック出力を会話履歴に追記したくない場合は `include_in_history=False` を設定してください。

## Durable execution 統合と human-in-the-loop

ツール承認の一時停止／再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) から始めてください。
以下の統合は、実行が長時間の待機、リトライ、またはプロセス再起動にまたがる可能性がある場合の durable orchestration 向けです。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使用すると、human-in-the-loop タスクを含む durable で長時間稼働するワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使用すると、人間の承認、ハンドオフ、セッション管理を含む軽量な durable エージェントを構築できます。この統合では依存関係として Restate の単一バイナリ runtime が必要で、プロセス／コンテナとしてのエージェント実行や serverless 関数もサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) または [ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使用すると、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間稼働するエージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期・非同期の両メソッドをサポートします。この統合には SQLite または Postgres データベースのみが必要です。詳細は統合の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。全一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェント実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。例：
    -   不正な JSON：モデルがツール呼び出し用、または直接出力として不正な JSON 構造を返す場合（特に特定の `output_type` が定義されている場合）。
    -   予期しないツール関連の失敗：モデルが想定どおりにツールを使用できない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定されたタイムアウトを超え、かつツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK を使ってコードを書くあなた（SDK 利用者）が、SDK 使用時にエラーを起こした場合に送出されます。典型的には、誤ったコード実装、無効な設定、または SDK API の誤用によって発生します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：それぞれ入力ガードレールまたは出力ガードレールの条件が満たされたときに送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。