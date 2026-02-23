---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的には `.run()` を実行するだけです。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままあなたにストリーミングします。

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

## エージェントループ

`Runner` の run メソッドを使う場合、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージとして扱われます）か、入力アイテムのリスト（OpenAI Responses API のアイテム）にできます。

その後 runner はループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して execution results を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追記してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」とみなされるルールは、望ましい型のテキスト出力が生成され、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミングイベントも追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行の完全な情報が格納されます。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳細は [streaming ガイド](streaming.md) を参照してください。

### Responses WebSocket トランスポート（任意ヘルパー）

OpenAI Responses websocket トランスポートを有効にすると、通常の `Runner` API を引き続き使用できます。接続の再利用には websocket セッションヘルパーの利用を推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md) ではありません。

#### パターン 1：セッションヘルパーなし（動作します）

websocket トランスポートだけが必要で、SDK に共有 provider / セッションの管理を任せる必要がない場合に使用します。

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

このパターンは単発の実行であれば問題ありません。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼び出す場合、同じ `RunConfig` / provider インスタンスを手動で再利用しない限り、各実行で再接続される可能性があります。

#### パターン 2：`responses_websocket_session()` を使用（マルチターンでの再利用に推奨）

複数回の実行（同じ `run_config` を継承する、ネストした Agents-as-tools 呼び出しを含む）にわたり、websocket 対応の共有 provider と `RunConfig` を使いたい場合は [`responses_websocket_session()`][agents.responses_websocket_session] を使用します。

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

コンテキストを抜ける前に、ストリーミングされた結果の消費を完了してください。websocket リクエストがまだ処理中の状態でコンテキストを終了すると、共有接続が強制的にクローズされる場合があります。

## 実行設定

`run_config` パラメーターにより、エージェント実行に関するいくつかのグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]：各 Agent が持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名の解決に使うモデル provider で、既定では OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際のセッションレベルの既定値（例：`SessionSettings(limit=...)`）を上書きします。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフにまだ入力フィルターがない場合、すべてのハンドオフに適用するグローバル入力フィルターです。入力フィルターにより、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次のエージェントを呼び出す前に、直前までの transcript を単一の assistant メッセージへ折りたたむオプトイン beta です。ネストしたハンドオフを安定化している間は既定で無効です。有効化するには `True` を設定し、raw transcript をそのまま通すには `False` のままにします。[Runner メソッド][agents.run.Runner] は `RunConfig` を渡さない場合に自動で作成するため、クイックスタートと examples は既定でオフのままであり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこの設定を上書きします。個別のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によりこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` をオプトインしたときに、正規化された transcript（履歴 + ハンドオフアイテム）を受け取る任意の callable です。次のエージェントへ転送する入力アイテムのリストを厳密に返す必要があり、完全なハンドオフフィルターを書かずに、組み込みサマリーを置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [tracing](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行に対して exporter、プロセッサー、または tracing メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに LLM およびツール呼び出しの入力/出力など、機微なデータが含まれ得るかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行の tracing ワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意フィールドで、複数の実行にまたがってトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions を使用する場合に、各ターンの前に新しいユーザー入力をセッション履歴とどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するためのフックです。例：履歴のトリミングや system prompt の注入。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フロー中にツール呼び出しが拒否されたときの、モデルに見えるメッセージをカスタマイズします。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：runner が過去の出力を次ターンのモデル入力へ変換する際、reasoning アイテム ID を保持するか省略するかを制御します。

ネストしたハンドオフはオプトイン beta として利用できます。折りたたみ transcript の挙動は `RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフに対して `handoff(..., nest_handoff_history=True)` を設定して有効化します。raw transcript（既定）を維持したい場合はフラグを未設定のままにするか、必要に応じて会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成サマリーに使われるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（既定へ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

### 実行設定の詳細

#### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否された際にモデルへ返されるメッセージをカスタマイズできます。

formatter は次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`：エラーのカテゴリー。現時点では `"approval_rejected"` です。
-   `tool_type`：ツール実行環境（`"function"`、`"computer"`、`"shell"`、または `"apply_patch"`）。
-   `tool_name`：ツール名。
-   `call_id`：ツール呼び出し ID。
-   `default_message`：SDK の既定の、モデルに見えるメッセージ。
-   `run_context`：アクティブな実行コンテキストのラッパー。

メッセージを置き換える文字列を返すか、SDK の既定を使う場合は `None` を返します。

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

#### `reasoning_item_id_policy`

`reasoning_item_id_policy` は、runner が履歴を繰り越す際（例：`RunResult.to_input_list()` を使う場合や、セッションに裏打ちされた実行）に、reasoning アイテムが次ターンのモデル入力へどのように変換されるかを制御します。

-   `None` または `"preserve"`（既定）：reasoning アイテム ID を保持します。
-   `"omit"`：生成される次ターン入力から reasoning アイテム ID を取り除きます。

`"omit"` は主に、Responses API の 400 エラーの一種（reasoning アイテムが `id` を持つ一方で、必須の後続アイテムが欠けて送られる場合。例：`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）に対する、オプトインの緩和策として使用します。

これは、SDK が過去の出力からフォローアップ入力を構築する（セッション永続化、サーバー管理の会話差分、ストリーミング/非ストリーミングのフォローアップターン、再開パスを含む）マルチターンのエージェント実行において、reasoning アイテム ID が保持されたものの、provider がその ID を対応する後続アイテムとペアのまま維持することを要求する場合に起こり得ます。

`reasoning_item_id_policy="omit"` を設定すると reasoning の内容は保持しつつ reasoning アイテムの `id` を取り除くため、SDK が生成するフォローアップ入力においてその API の不変条件に抵触しないようにできます。

スコープに関する注意点：

-   これは、SDK がフォローアップ入力を構築する際に生成/転送する reasoning アイテムだけを変更します。
-   ユーザーが与えた初期入力アイテムは書き換えません。
-   `call_model_input_filter` では、このポリシー適用後でも意図的に reasoning ID を再導入できます。

## 会話 / チャットスレッド

いずれの run メソッドも、1 つ以上のエージェント実行（したがって 1 回以上の LLM 呼び出し）につながり得ますが、チャット会話における 1 つの論理ターンを表します。例：

1. ユーザーターン：ユーザーがテキストを入力
2. Runner 実行：最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行し、その後に出力を生成する。

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成した新しいアイテムをすべてユーザーに表示することも、最終出力だけを表示することもできます。いずれの場合でも、その後にユーザーがフォローアップ質問をしたら、再度 run メソッドを呼び出せます。

### 手動の会話管理

次ターンの入力を取得するために [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます。

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

### Sessions による自動の会話管理

より簡単な方法として、[Sessions](sessions/index.md) を使うと、`.to_input_list()` を手動で呼び出さずに会話履歴を自動で扱えます。

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

-   各実行の前に会話履歴を取得
-   各実行の後に新規メッセージを保存
-   異なるセッション ID ごとに別々の会話を維持

!!! note

    セッション永続化は、サーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）と
    同じ実行内では併用できません。呼び出しごとにどちらか一方のアプローチを選択してください。

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。

### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の会話状態機能によりサーバー側で会話状態を管理することもできます。これにより、過去メッセージをすべて手動で再送しなくても会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターンをまたいで状態を追跡する方法を 2 つ提供しています。

#### 1. `conversation_id` を使用

まず OpenAI Conversations API を使って会話を作成し、その ID を以後の呼び出しで再利用します。

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

#### 2. `previous_response_id` を使用

別の選択肢は **response chaining** で、各ターンが前のターンのレスポンス ID に明示的に紐づきます。

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
    会話実行では、リトライ前に内部の会話トラッカー入力を巻き戻し、同じ準備済みアイテムを
    きれいに再送できるようにします。

    ローカルのセッションベース実行（`conversation_id`、`previous_response_id`、または
    `auto_previous_response_id` と併用できません）では、SDK はリトライ後の重複した履歴エントリを
    減らすため、直近に永続化した入力アイテムのベストエフォートなロールバックも行います。

## Call model input filter

`call_model_input_filter` を使うと、モデル呼び出し直前にモデル入力を編集できます。このフックは、現在のエージェント、コンテキスト、結合済み入力アイテム（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

機微情報のマスキング、長い履歴のトリミング、追加のシステム指針の注入のために、`run_config` により実行ごとにフックを設定するか、`Runner` の既定として設定します。

## エラーハンドラー

すべての `Runner` エントリポイントは、エラー種別をキーにした dict の `error_handlers` を受け取ります。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに制御された最終出力を返したい場合に使用します。

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

フォールバック出力を会話履歴へ追記したくない場合は `include_in_history=False` を設定してください。

## 長時間実行エージェント & human-in-the-loop

ツール承認の一時停止 / 再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)では、Temporal と Agents SDK が連携して長時間タスクを完了するデモを確認できます。また、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、人による承認、ハンドオフ、セッション管理を含む、軽量で耐久性のあるエージェントを利用できます。この連携は依存関係として Restate の単一バイナリ runtime を必要とし、プロセス/コンテナとして、またはサーバーレス関数としてエージェントを実行することをサポートします。詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期および非同期メソッドの両方をサポートします。この連携は SQLite または Postgres データベースのみを必要とします。詳細は連携の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [docs](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。全リストは [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。ほかのすべての特定例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡された `max_turns` 制限を超えた場合に送出されます。指定された対話ターン数の範囲内でタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには次が含まれます。
    -   不正な形式の JSON：モデルがツール呼び出し用、または直接出力として不正な形式の JSON 構造を返す場合（特に特定の `output_type` が定義されている場合）。
    -   予期しないツール関連の失敗：モデルが期待される方法でツールを使用できない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定されたタイムアウトを超え、ツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK を使用するコードを書くあなたが、SDK の使用中に誤りを犯した場合に送出されます。通常、誤ったコード実装、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：入力ガードレールまたは出力ガードレールの条件がそれぞれ満たされた場合に送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。