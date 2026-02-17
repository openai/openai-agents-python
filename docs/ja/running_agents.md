---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを介して エージェント を実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部では単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままあなたに ストリーミング します。

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

詳細は [実行結果 ガイド](results.md) を参照してください。

## エージェント ループ

`Runner` で run メソッドを使うときは、開始 エージェント と入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）か、OpenAI Responses API の入力アイテムにあたる入力アイテムのリストのいずれかです。

次に runner はループを実行します。

1. 現在の エージェント と現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返す場合、ループは終了し、実行結果 を返します。
    2. LLM が ハンドオフ を行う場合、現在の エージェント と入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成する場合、それらのツール呼び出しを実行し、結果を追記して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミング を使うと、LLM の実行中に ストリーミング イベントも追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成された新しい出力を含む実行の完全な情報が格納されます。ストリーミング イベントは `.stream_events()` で取得できます。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターにより、エージェント 実行のグローバル設定をいくつか構成できます。

-   [`model`][agents.run.RunConfig.model]：各 Agent が持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名を参照するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント 固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際のセッション レベルのデフォルト（例：`SessionSettings(limit=...)`）を上書きします。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ に既に指定がない場合に、すべての ハンドオフ に適用するグローバルな入力フィルターです。入力フィルターにより、新しい エージェント に送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次の エージェント を呼び出す前に、直前までのトランスクリプトを 1 つの assistant メッセージに折りたたむオプトインのベータ機能です。ネストされた ハンドオフ を安定化している間はデフォルトで無効です。有効化するには `True`、raw のトランスクリプトをそのまま渡すには `False` のままにしてください。いずれの [Runner メソッド][agents.run.Runner] も、渡さない場合は自動的に `RunConfig` を作成するため、クイックスタートと examples ではデフォルトで無効のままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個々の ハンドオフ は [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` をオプトインしたときに、正規化されたトランスクリプト（履歴 + ハンドオフ アイテム）を受け取る任意の callable です。次の エージェント に転送する入力アイテムの完全に同一のリストを返す必要があり、完全な ハンドオフ フィルターを書かずに内蔵の要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行の exporter、プロセッサー、または トレーシング メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに LLM やツール呼び出しの入出力などの機微データが含まれ得るかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行の トレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意フィールドで、複数の実行にまたがるトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions 使用時に、各ターン前に新しい ユーザー 入力をセッション履歴へどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するフックです。例：履歴をトリムする、または システムプロンプト を注入する。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フロー中にツール呼び出しが拒否された場合に、モデルに見えるメッセージをカスタマイズします。

ネストされた ハンドオフ はオプトインのベータとして利用できます。折りたたみトランスクリプトの挙動を有効にするには `RunConfig(nest_handoff_history=True)` を渡すか、特定の ハンドオフ に対して `handoff(..., nest_handoff_history=True)` を設定してください。raw のトランスクリプト（デフォルト）を維持したい場合は、フラグを未設定のままにするか、必要どおりに会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約で使われるラッパー文言を変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出します。

## 会話 / チャット スレッド

いずれの run メソッドを呼び出しても、1 つ以上の エージェント が実行され（したがって 1 回以上の LLM 呼び出しが発生し得ます）、それでもチャット会話における 1 回の論理ターンを表します。たとえば次のとおりです。

1. ユーザー ターン：ユーザー がテキストを入力
2. Runner 実行：最初の エージェント が LLM を呼び出し、ツールを実行し、2 つ目の エージェント へ ハンドオフ し、2 つ目の エージェント がさらにツールを実行してから出力を生成

エージェント 実行の最後に、ユーザー に何を見せるかを選べます。たとえば、エージェント が生成したすべての新規アイテムを ユーザー に見せることも、最終出力だけを見せることもできます。いずれの場合でも、その後 ユーザー がフォローアップ質問をするかもしれません。その場合は再度 run メソッドを呼び出せます。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って次のターンの入力を取得し、会話履歴を手動で管理できます。

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

### Sessions を使った自動の会話管理

より簡単な方法として、[Sessions](sessions/index.md) を使えば `.to_input_list()` を手動で呼び出さずに会話履歴を自動処理できます。

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
-   異なるセッション ID ごとに別々の会話を維持

!!! note

    セッションの永続化は、同一の実行内で サーバー 管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）
    と併用できません。呼び出しごとにいずれか一方の方式を選んでください。

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。

### サーバー 管理の会話

`to_input_list()` や `Sessions` でローカル処理する代わりに、OpenAI の conversation state 機能により サーバー 側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送しなくても会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターンをまたいで状態を追跡する 2 つの方法を提供します。

#### 1. `conversation_id` を使用

OpenAI Conversations API を使って最初に会話を作成し、その ID を以後の呼び出しで再利用します。

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

別の選択肢は **response chaining** で、各ターンが直前ターンの response ID に明示的にリンクします。

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

## Call model input filter

モデル呼び出し直前にモデル入力を編集するには `call_model_input_filter` を使います。このフックは現在の エージェント、context、結合済みの入力アイテム（存在する場合はセッション履歴も含む）を受け取り、新しい `ModelInputData` を返します。

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

機微データのマスキング、長い履歴のトリム、追加のシステム ガイダンスの注入を行うために、`run_config` で実行ごとにフックを設定するか、`Runner` のデフォルトとして設定します。

## エラー ハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする dict の `error_handlers` を受け取ります。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに、制御された最終出力を返したいときに使用します。

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

## 長時間実行 エージェント と human-in-the-loop

ツール承認の一時停止 / 再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使うと、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使うと、軽量で耐久性のある エージェント を実行できます。human approval、ハンドオフ、セッション管理も含まれます。この統合は依存関係として Restate の single-binary runtime を必要とし、プロセス / コンテナとして、または サーバーレス 関数として エージェント を実行することをサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[ドキュメント](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使うと、障害や再起動をまたいで進捗を保持する信頼性の高い エージェント を実行できます。長時間実行 エージェント、human-in-the-loop ワークフロー、ハンドオフ をサポートします。sync / async の両メソッドをサポートします。この統合が必要とするのは SQLite または Postgres データベースだけです。詳細は統合の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [ドキュメント](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。全一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。ほかのすべての具体的な例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡した `max_turns` 制限を エージェント の実行が超えたときに送出されます。指定された対話ターン数内に エージェント がタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例：
    -   不正な形式の JSON：モデルがツール呼び出し、または直接出力で不正な形式の JSON 構造を返した場合（特に特定の `output_type` が定義されている場合）
    -   予期しないツール関連の失敗：モデルが想定どおりにツールを使用しない場合
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定されたタイムアウトを超え、かつツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK を使用するあなた（SDK を使うコードを書く人）が SDK の使用中にエラーを起こした場合に送出されます。通常、誤ったコード実装、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：入力 ガードレール または出力 ガードレール の条件がそれぞれ満たされたときに送出されます。入力 ガードレール は処理前に受信メッセージを検査し、出力 ガードレール は配信前に エージェント の最終応答を検査します。