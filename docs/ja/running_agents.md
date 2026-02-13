---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを介して エージェント を実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的には `.run()` を実行するだけです。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのまま ストリーミング で受け取れます。

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

## エージェントループ

`Runner` の run メソッドを使うときは、開始 エージェント と入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリストで、後者は OpenAI Responses API のアイテムです。

Runner は次のループを実行します。

1. 現在の エージェント と現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して 実行結果 を返します。
    2. LLM が ハンドオフ を行った場合、現在の エージェント と入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追記して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力が「最終出力」と見なされる条件は、望ましい型のテキスト出力が生成され、かつツール呼び出しがないことです。

## ストリーミング

ストリーミング を使うと、LLM の実行中に ストリーミング イベントも追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行に関する完全な情報（生成されたすべての新しい出力を含む）が格納されます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターにより、エージェント実行に関するグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]：各 Agent が持つ `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名の解決に使うモデルプロバイダーで、既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際、セッション レベルの既定（例：`SessionSettings(limit=...)`）を上書きします。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ 側に既に設定がない場合に、すべての ハンドオフ に適用するグローバル入力フィルターです。入力フィルターを使うと、新しい エージェント に送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次の エージェント を呼び出す前に、直前までの transcript を 1 つの assistant メッセージに折りたたむ、オプトインのベータ機能です。ネストされた ハンドオフ の安定化中のため既定では無効です。有効にするには `True` を設定し、raw transcript をそのまま通す場合は `False` のままにしてください。[Runner メソッド][agents.run.Runner] は `RunConfig` を渡さない場合に自動的に `RunConfig` を作成するため、クイックスタートや例では既定でオフのままになっており、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個々の ハンドオフ は [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を通じてこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` をオプトインした際に、正規化された transcript（履歴 + ハンドオフ アイテム）を受け取る任意の callable です。次の エージェント に転送する入力アイテムのリストを厳密に返す必要があり、完全な ハンドオフ フィルターを書かなくても、組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行における exporter、プロセッサー、または トレーシング メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに、LLM およびツール呼び出しの入力/出力など、機微情報の可能性があるデータを含めるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行に対して、トレーシング のワークフロー名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は複数の実行にまたがってトレースを関連付けるための任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions 使用時に、各ターンの前に新しい ユーザー 入力をセッション履歴へどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するためのフックです。たとえば、履歴をトリムしたり システムプロンプト を注入したりできます。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認フロー中にツール呼び出しが拒否されたとき、モデルに見えるメッセージをカスタマイズします。

ネストされた ハンドオフ はオプトインのベータとして利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定の ハンドオフ に対して有効化するには `handoff(..., nest_handoff_history=True)` を設定して、折りたたみ transcript の挙動を有効にしてください。raw transcript（既定）を維持したい場合は、フラグを未設定のままにするか、会話を必要な形でそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約で使われるラッパー文言を変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（既定に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上の エージェント が実行され（つまり 1 回以上 LLM を呼び出し）得ますが、チャット会話としては 1 回の論理ターンを表します。たとえば次のとおりです。

1. ユーザー ターン：ユーザー がテキストを入力
2. Runner 実行：最初の エージェント が LLM を呼び出し、ツールを実行し、2 番目の エージェント に ハンドオフ し、2 番目の エージェント がさらにツールを実行して、出力を生成

エージェント実行の終了時に、ユーザー に何を表示するかを選べます。たとえば、エージェント が生成した新しいアイテムをすべて ユーザー に見せることも、最終出力だけを見せることもできます。いずれの場合も、その後 ユーザー がフォローアップの質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使うと、次のターンの入力を取得して会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使うと、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に扱えます。

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

-   実行のたびに、事前に会話履歴を取得
-   実行のたびに、新しいメッセージを保存
-   異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。

### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI conversation state 機能によりサーバー側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送することなく会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間で状態を追跡するために 2 つの方法を提供しています。

#### 1. `conversation_id` を使う

最初に OpenAI Conversations API で会話を作成し、その ID を以後の呼び出しすべてで再利用します。

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

#### 2. `previous_response_id` を使う

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

## Call model 入力フィルター

モデル呼び出しの直前にモデル入力を編集するには `call_model_input_filter` を使います。このフックは現在の エージェント、コンテキスト、結合された入力アイテム（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

機微データのマスキング、長い履歴のトリム、追加のシステム ガイダンスの注入のために、`run_config` 経由で実行ごとにフックを設定するか、`Runner` の既定として設定してください。

## エラーハンドラー

すべての `Runner` エントリポイントは `error_handlers` を受け取ります。これはエラー種別をキーとする dict です。現時点でサポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに制御された最終出力を返したい場合に使用してください。

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

ツール承認の一時停止/再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使うと、human approval、ハンドオフ、セッション管理を含む、軽量で耐久性のある エージェント を実行できます。この連携には依存関係として Restate の single-binary runtime が必要で、プロセス/コンテナまたは serverless functions として エージェント を実行できます。
詳細は [overview](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[docs](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使うと、障害や再起動をまたいで進捗を保持できる信頼性の高い エージェント を実行できます。長時間実行 エージェント、human-in-the-loop ワークフロー、ハンドオフ をサポートします。同期メソッドと非同期メソッドの両方をサポートします。この連携に必要なのは SQLite または Postgres データベースだけです。詳細は連携の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [docs](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。ほかのすべての具体的な例外は、この汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェント実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡した `max_turns` 上限を超えたときに送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。これには次が含まれます。
    -   不正な形式の JSON：特に特定の `output_type` が定義されている場合に、モデルがツール呼び出し用または直接出力として不正な形式の JSON 構造を返すとき。
    -   予期しないツール関連の失敗：モデルが期待される方法でツールを使えないとき
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定されたタイムアウトを超え、かつツールが `timeout_behavior="raise_exception"` を使用している場合に送出されます。
-   [`UserError`][agents.exceptions.UserError]：SDK を使うコードを書くあなたが、SDK の使用中に誤りを犯したときに送出されます。通常、誤ったコード実装、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：入力 ガードレール または出力 ガードレール の条件がそれぞれ満たされたときに送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール は配信前に エージェント の最終応答をチェックします。