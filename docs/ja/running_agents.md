---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。選択肢は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行するだけです。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままあなたにストリーミングします。

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

## エージェントループ

`Runner` の `run` メソッドを使うときは、開始エージェントと入力を渡します。入力は、文字列（ユーザーメッセージとして扱われます）または入力項目のリスト（OpenAI Responses API の item）です。

その後、Runner は次のループを実行します:

1. 現在の入力で、現在のエージェントに対して LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「final output」と見なされるルールは、望ましい型のテキスト出力を生成していて、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングでは、LLM の実行中にストリーミングイベントも追加で受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行に関する完全な情報が入ります。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定をいくつか構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent が持つ `model` とは無関係に、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力のガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ側に既に指定がない場合に、すべてのハンドオフへ適用するグローバル入力フィルターです。入力フィルターにより、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェントを呼び出す前に、直前までのトランスクリプトを 1 つの assistant メッセージに折りたたむ、オプトインのベータ機能です。ネストされたハンドオフの安定化中はデフォルトで無効です。有効化するには `True`、raw のトランスクリプトをそのまま通すには `False` のままにします。いずれの [`Runner` メソッド](agents.run.Runner) も、渡されない場合は自動的に `RunConfig` を作成するため、クイックスタートと examples は既定で無効のままです。また、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個々のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によりこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` にオプトインしたときに、正規化されたトランスクリプト（履歴 + ハンドオフ項目）を受け取る任意の callable です。次のエージェントへ転送する入力項目の完全に同一のリストを返す必要があり、完全なハンドオフフィルターを書かずに、組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行における exporter、プロセッサー、またはトレーシングメタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに、LLM とツール呼び出しの入力/出力など、潜在的に機微なデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は任意フィールドで、複数の実行にまたがってトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターンの前に新しいユーザー入力をセッション履歴へどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、完全に準備されたモデル入力（instructions と input items）を編集するためのフックです。たとえば、履歴をトリミングしたり、システムプロンプトを注入したりできます。

ネストされたハンドオフはオプトインのベータとして利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフで `handoff(..., nest_handoff_history=True)` を設定して、トランスクリプト折りたたみ動作を有効化します。raw のトランスクリプト（デフォルト）を維持したい場合は、このフラグを未設定のままにするか、会話を必要どおりに正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（既定へ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出します。

## 会話 / チャットスレッド

いずれの run メソッドを呼んでも、1 つ以上のエージェントが実行され（したがって 1 回以上の LLM 呼び出しが行われ）得ますが、チャット会話における 1 つの論理ターンを表します。たとえば:

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner 実行: 最初のエージェントが LLM を呼び出してツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行してから出力を生成します。

エージェント実行の終了時に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成した新規 item をすべて表示することも、最終出力だけを表示することもできます。どちらの場合でも、ユーザーがフォローアップ質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドで次のターンの入力を取得し、会話履歴を手動で管理できます:

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

よりシンプルな方法として、[Sessions](sessions/index.md) を使えば、`.to_input_list()` を手動で呼び出さずに会話履歴を自動的に処理できます:

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

Sessions は自動的に:

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なるセッション ID ごとに別々の会話を維持します

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。

### サーバー管理の会話

`to_input_list()` や `Sessions` を使ってローカルで扱う代わりに、OpenAI conversation state 機能でサーバー側の会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送信することなく会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間で状態を追跡する方法を 2 つ提供します:

#### 1. `conversation_id` の使用

まず OpenAI Conversations API を使って会話を作成し、その ID を以降のすべての呼び出しで再利用します:

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

#### 2. `previous_response_id` の使用

もう 1 つの選択肢は **response chaining** で、各ターンが 1 つ前のターンの response ID に明示的にリンクします。

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

`call_model_input_filter` を使うと、モデル呼び出しの直前にモデル入力を編集できます。このフックは現在のエージェント、コンテキスト、結合済みの入力項目（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

機微なデータのマスキング、長い履歴のトリミング、追加のシステムガイダンスの注入などのために、`run_config` で実行ごとにフックを設定するか、`Runner` のデフォルトとして設定します。

## 長時間実行エージェント & human-in-the-loop

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使うと、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは、[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) をご覧ください。ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使うと、人による承認、ハンドオフ、セッション管理を含む、軽量で耐久性のあるエージェントを実行できます。この統合は依存関係として Restate の単一バイナリ runtime を必要とし、プロセス/コンテナまたはサーバーレス関数としてエージェントを実行することをサポートします。
詳細は [概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[docs](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使うと、障害や再起動をまたいでも進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。また、同期と非同期の両方のメソッドをサポートします。この統合が必要とするのは SQLite または Postgres データベースのみです。詳細は統合の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [docs](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。全リストは [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的な例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えたときに送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。これには次が含まれます:
    -   不正な形式の JSON: モデルがツール呼び出しや直接出力に対して不正な JSON 構造を返した場合。特に特定の `output_type` が定義されている場合です。
    -   予期しないツール関連の失敗: モデルが期待される方法でツールを使用できない場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するコードを書くあなた（SDK を使ってコードを書く人）が、SDK 利用中に誤りをしたときに送出されます。これは通常、誤ったコード実装、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件が満たされたときに、それぞれ送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。