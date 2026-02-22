---
search:
  exclude: true
---
# エージェント実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的に `.run()` を実行するだけです。
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

詳細は [results guide](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドを使うときは、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージとして扱われます）か、入力アイテムのリスト（OpenAI Responses API のアイテム）にできます。

その後、runner は次のループを実行します。

1. 現在の入力で、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して execution results を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、そのツール呼び出しを実行し、結果を追記してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、希望する型のテキスト出力が生成され、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミングイベントも追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が含まれます。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [streaming guide](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定をいくつか構成できます。

-   [`model`][agents.run.RunConfig.model]：各 Agent が持つ `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名の解決に使うモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえば、グローバル `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際のセッションレベルのデフォルト（例：`SessionSettings(limit=...)`）を上書きします。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力のガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフにすでに設定がない場合に、すべてのハンドオフへ適用するグローバル入力フィルターです。入力フィルターにより、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次のエージェントを呼び出す前に、以前のトランスクリプトを 1 つの assistant メッセージへ折りたたむオプトインのベータ機能です。ネストされたハンドオフを安定化する間はデフォルトで無効です。有効にするには `True`、raw のトランスクリプトをそのまま通すには `False` のままにします。いずれの [Runner methods][agents.run.Runner] も、渡さない場合は自動的に `RunConfig` を作成するため、クイックスタートや examples ではデフォルトでオフのままです。また、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のコールバックは引き続きこれを上書きします。個別のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によりこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` をオプトインしたときに、正規化されたトランスクリプト（履歴 + ハンドオフ項目）を受け取る任意の callable です。次のエージェントへ転送する入力アイテムの正確なリストを返す必要があり、完全なハンドオフフィルターを書かずに、組み込み要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [tracing](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行の exporter、プロセッサー、または tracing メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに LLM とツール呼び出しの入出力など、機微なデータが含まれる可能性があるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行の tracing ワークフロー名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は、複数の実行にまたがってトレースを関連付けるための任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions を使用する際、各ターンの前に新しいユーザー入力をセッション履歴へどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するためのフックです。例：履歴のトリミング、システムプロンプトの注入。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フロー中にツール呼び出しが拒否されたとき、モデルに見えるメッセージをカスタマイズします。
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：runner が過去の出力を次ターンのモデル入力へ変換するときに、reasoning アイテム ID を保持するか省略するかを制御します。

ネストされたハンドオフはオプトインのベータとして利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフに対して `handoff(..., nest_handoff_history=True)` を設定すると、トランスクリプト折りたたみの挙動が有効になります。raw のトランスクリプト（デフォルト）を保持したい場合は、フラグを未設定のままにするか、必要なとおりに会話を正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約で使われるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトへ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

### 実行設定詳細

#### `tool_error_formatter`

`tool_error_formatter` を使うと、承認フローでツール呼び出しが拒否された際にモデルへ返すメッセージをカスタマイズできます。

formatter は次を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

-   `kind`：エラーカテゴリーです。現時点では `"approval_rejected"` です。
-   `tool_type`：ツールランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`）。
-   `tool_name`：ツール名です。
-   `call_id`：ツール呼び出し ID です。
-   `default_message`：SDK のデフォルトのモデル可視メッセージです。
-   `run_context`：アクティブな実行コンテキストラッパーです。

メッセージを置き換える文字列を返すか、SDK デフォルトを使う場合は `None` を返します。

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

`reasoning_item_id_policy` は、runner が履歴を引き継ぐ際（例：`RunResult.to_input_list()` を使う場合や、セッションに裏打ちされた実行）に、reasoning アイテムを次ターンのモデル入力へどう変換するかを制御します。

-   `None` または `"preserve"`（デフォルト）：reasoning アイテム ID を保持します。
-   `"omit"`：生成される次ターン入力から reasoning アイテム ID を削除します。

`"omit"` は主に、reasoning アイテムが `id` 付きで送信されたものの、必要な後続アイテムが欠けている場合に発生する Responses API の 400 エラー群への、オプトインの緩和策として使用してください（例：`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が以前の出力からフォローアップ入力を構築する（セッション永続化、サーバー管理の会話差分、ストリーミング/非ストリーミングのフォローアップターン、再開パスを含む）マルチターンのエージェント実行で起こり得ます。reasoning アイテム ID が保持される一方で、プロバイダーがその ID を対応する後続アイテムと常にペアで維持することを要求する場合です。

`reasoning_item_id_policy="omit"` を設定すると、reasoning の内容は保持しつつ、reasoning アイテムの `id` を削除するため、SDK が生成するフォローアップ入力におけるその API 不変条件のトリガーを回避できます。

スコープに関する注意:

-   これは、SDK がフォローアップ入力を構築する際に生成/転送する reasoning アイテムのみを変更します。
-   ユーザーが与える初期入力アイテムは書き換えません。
-   `call_model_input_filter` は、このポリシー適用後に意図的に reasoning ID を再導入できます。

## 会話 / チャットスレッド

いずれの run メソッドを呼んでも、1 つ以上のエージェントが実行され（したがって 1 回以上 LLM を呼び出します）が、チャット会話における 1 つの論理ターンを表します。例:

1. ユーザーターン：ユーザーがテキストを入力
2. Runner の実行：最初のエージェントが LLM を呼び出してツールを実行し、2 つ目のエージェントへハンドオフし、2 つ目のエージェントがさらにツールを実行してから出力を生成

エージェント実行の終わりに、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成したすべての新しいアイテムを表示することも、最終出力だけを表示することもできます。いずれの場合でも、ユーザーがフォローアップ質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドで次のターンの入力を取得し、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使えば、`.to_input_list()` を手動で呼ばずに会話履歴を自動的に扱えます。

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
-   各実行の後に新しいメッセージを保存
-   異なるセッション ID ごとに別々の会話を維持

!!! note

    セッション永続化は、サーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）と
    同じ実行内で併用できません。呼び出しごとに 1 つの方式を選んでください。

詳細は [Sessions documentation](sessions/index.md) を参照してください。

### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに処理する代わりに、OpenAI の conversation state 機能によりサーバー側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送せずに会話履歴を保持できます。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI は、ターンをまたいで状態を追跡する方法を 2 つ提供します。

#### 1. `conversation_id` を使用

最初に OpenAI Conversations API を使って会話を作成し、その後の呼び出しで毎回その ID を再利用します。

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

別の選択肢は **response chaining** で、各ターンが直前のターンの response ID に明示的にリンクします。

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

    SDK は `conversation_locked` エラーをバックオフ付きで自動的に再試行します。サーバー管理の
    会話実行では、再試行前に内部の会話トラッカー入力を巻き戻し、同じ準備済みアイテムを
    きれいに再送できるようにします。

    ローカルのセッションベースの実行（`conversation_id`、
    `previous_response_id`、`auto_previous_response_id` と併用不可）では、SDK は再試行後の
    重複履歴エントリを減らすため、直近に永続化した入力アイテムのベストエフォートな
    ロールバックも実行します。

## Call model input filter

`call_model_input_filter` を使うと、モデル呼び出しの直前にモデル入力を編集できます。このフックは、現在のエージェント、コンテキスト、結合された入力アイテム（存在する場合はセッション履歴も含む）を受け取り、新しい `ModelInputData` を返します。

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

機微データのマスキング、長い履歴のトリミング、追加のシステムガイダンスの注入などのために、`run_config` 経由で実行ごとにフックを設定するか、`Runner` のデフォルトとして設定してください。

## エラーハンドラー

すべての `Runner` エントリポイントは、エラー種別をキーとする dict である `error_handlers` を受け取れます。現在、サポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに、制御された最終出力を返したいときに使用します。

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

ツール承認の一時停止/再開パターンについては、専用の [Human-in-the-loop guide](human_in_the_loop.md) を参照してください。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む、永続的で長時間実行のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、人による承認、ハンドオフ、セッション管理を含む、軽量で永続的なエージェントを実現できます。この連携は依存関係として Restate のシングルバイナリランタイムを必要とし、エージェントをプロセス/コンテナとして実行する方法と、サーバーレス関数として実行する方法をサポートします。
詳細は [overview](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[docs](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。また、同期・非同期の両メソッドをサポートします。この連携が必要とするのは SQLite または Postgres データベースのみです。詳細は連携の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [docs](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要:

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的な例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェントの実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡された `max_turns` 制限を超えたときに送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が想定外または無効な出力を生成したときに発生します。これには次が含まれます。
    -   不正な形式の JSON：特に `output_type` が定義されている場合に、ツール呼び出しや直接出力として不正な JSON 構造が返されたとき。
    -   想定外のツール関連失敗：モデルが期待される形でツールを使用できなかったとき。
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが構成されたタイムアウトを超え、ツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK を使うあなた（SDK を用いるコードを書く人）が SDK の使用中に誤りをした場合に送出されます。通常、誤ったコード実装、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：それぞれ入力ガードレールまたは出力ガードレールの条件が満たされたときに送出されます。入力ガードレールは処理前に受信メッセージを検査し、出力ガードレールは配信前にエージェントの最終応答を検査します。