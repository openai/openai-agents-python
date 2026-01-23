---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを介して エージェント を実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部では単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミング モードで LLM を呼び出し、受信したイベントをそのままストリームします。

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

詳しくは [実行結果ガイド](results.md) を参照してください。

## エージェント ループ

`Runner` の run メソッドを使用する際は、開始 エージェント と入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）でも、OpenAI Responses API の item にあたる入力 item のリストでも構いません。

その後、runner は次のループを実行します。

1. 現在の エージェント に対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して実行結果を返します。
    2. LLM が ハンドオフ を行った場合、現在の エージェント と入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追記して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力が生成され、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミング イベントも追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が格納されます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェント 実行に関するいくつかのグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]：各 Agent が持つ `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名を参照するためのモデル プロバイダーで、既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント 固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ に既に設定がない場合に、すべての ハンドオフ に適用するグローバルな入力フィルターです。入力フィルターでは、新しい エージェント に送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次の エージェント を呼び出す前に、直前までの文字起こしを 1 つの assistant メッセージに折りたたむオプトイン ベータです。ネストした ハンドオフ を安定化している間は既定で無効です。有効にするには `True` に設定し、raw の文字起こしをそのまま通すには `False` のままにしてください。[`Runner` の各メソッド](agents.run.Runner) は `RunConfig` を渡さない場合に自動で作成するため、クイックスタートや例では既定の無効状態を維持し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこれを上書きします。個々の ハンドオフ は [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によりこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` をオプトインしている場合に、正規化された文字起こし（履歴 + handoff item）を受け取る任意の callable です。次の エージェント に転送する入力 item のリストを厳密に返す必要があり、完全な ハンドオフ フィルターを書かずに組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行における exporter、プロセッサー、またはトレーシング メタデータを上書きするために、[`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに、LLM およびツール呼び出しの入力/出力など、機密となり得るデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行のトレーシング ワークフロー名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は、複数の実行にまたがってトレースを関連付けられる任意のフィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions 使用時に、各ターンの前に新しい ユーザー 入力をセッション履歴とどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力 item）を編集するためのフックです。例：履歴のトリミングや システムプロンプト の注入。

ネストした ハンドオフ はオプトイン ベータとして提供されています。`RunConfig(nest_handoff_history=True)` を渡すか、特定の ハンドオフ に対して `handoff(..., nest_handoff_history=True)` を設定すると、文字起こし折りたたみ動作を有効化できます。raw の文字起こし（既定）を維持したい場合は、フラグを未設定のままにするか、会話を必要な形で正確に転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。カスタム mapper を書かずに生成要約で使われるラッパー テキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（既定に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

## 会話/チャット スレッド

いずれの run メソッドを呼び出しても、1 つ以上の エージェント が実行され（したがって 1 回以上の LLM 呼び出しが発生し）得ますが、これはチャット会話における 1 つの論理ターンを表します。例：

1. ユーザー ターン：ユーザー がテキストを入力
2. Runner 実行：最初の エージェント が LLM を呼び出し、ツールを実行し、2 番目の エージェント に ハンドオフ し、2 番目の エージェント がさらにツールを実行して、出力を生成

エージェント 実行の終了時に、ユーザー に何を見せるかを選べます。たとえば、エージェント が生成したすべての新しい item を見せることも、最終出力だけを見せることもできます。いずれにせよ、ユーザー がフォローアップ質問をする場合があり、その場合は run メソッドを再度呼び出せます。

### 手動での会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動処理できます。

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

Sessions は自動的に以下を行います。

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。

### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の conversation state 機能でサーバー側の会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送せずに会話履歴を保持できます。詳しくは [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターンをまたいで状態を追跡する方法を 2 つ提供しています。

#### 1. `conversation_id` を使用する

まず OpenAI Conversations API で会話を作成し、その ID を以降の呼び出しで毎回再利用します。

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

#### 2. `previous_response_id` を使用する

もう 1 つの選択肢は **response chaining** で、各ターンが直前のターンの response ID に明示的にリンクします。

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

`call_model_input_filter` を使用して、モデル呼び出しの直前にモデル入力を編集します。このフックは、現在の エージェント、コンテキスト、および結合された入力 item（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

フックは `run_config` を介して実行ごとに設定するか、`Runner` の既定として設定し、機密データのマスキング、長い履歴のトリミング、追加のシステム ガイダンスの注入などに利用できます。

## 長時間実行 エージェント と human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使用すると、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。ほかのすべての具体的な例外は、この汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェント の実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に送出されます。指定されたインタラクション ターン数の範囲内でタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには以下が含まれます。
    -   不正な形式の JSON：モデルがツール呼び出し、または直接出力に対して不正な形式の JSON 構造を返す場合（特に特定の `output_type` が定義されている場合）。
    -   想定外のツール関連の失敗：モデルが期待どおりにツールを使用できない場合
-   [`UserError`][agents.exceptions.UserError]：SDK を使ってコードを書くあなた（SDK を使用するコード作成者）が、SDK の利用中に誤りを犯した場合に送出されます。通常、誤ったコード実装、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：それぞれ、入力 ガードレール または出力 ガードレール の条件が満たされた場合に送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール は配信前に エージェント の最終応答をチェックします。