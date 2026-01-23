---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを介してエージェントを実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部では単に `.run()` を実行します。
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

## エージェント ループ

`Runner` の run メソッドを使うときは、開始 エージェント と入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）にできます。

その後、runner はループを実行します。

1. 現在の エージェント に対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し execution results を返します。
    2. LLM が ハンドオフ を行った場合、現在の エージェント と入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追記してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型でテキスト出力を生成し、かつツール呼び出しがないことです。

## ストリーミング

ストリーミング を使うと、LLM の実行中に追加で ストリーミング イベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には生成されたすべての新しい出力を含む実行の完全な情報が入ります。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターを使うと、エージェント実行のグローバル設定をいくつか構成できます。

-   [`model`][agents.run.RunConfig.model]：各 Agent が持つ `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名を参照するための モデル プロバイダー で、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフ がすでに持っていない場合に、すべての ハンドオフ に適用するグローバル 入力フィルター です。入力フィルターにより、新しい エージェント に送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次の エージェント を呼び出す前に、直前までのトランスクリプトを 1 つの assistant メッセージに折りたたむ opt-in ベータです。ネストされた ハンドオフ を安定化している間はデフォルトで無効です。有効化するには `True` を設定し、raw なトランスクリプトをそのまま渡すには `False` のままにします。いずれの [`Runner` methods](agents.run.Runner) も、渡さない場合は自動的に `RunConfig` を作成するため、クイックスタートや例ではデフォルトの無効状態のままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個々の ハンドオフ は [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によりこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` を opt-in したときに、正規化されたトランスクリプト（履歴 + ハンドオフ アイテム）を受け取る任意の callable です。次の エージェント に転送する入力アイテムの完全に同一のリストを返す必要があり、完全な ハンドオフ フィルターを書かずに組み込み要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体で [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]：この実行の exporter、プロセッサー、または トレーシング メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに、LLM およびツール呼び出しの入力/出力など潜在的に機微なデータを含めるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：この実行の トレーシング ワークフロー名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は、複数の実行にまたがってトレースを関連付けられる任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions を使用する際に、各ターンの前で新しい user 入力をセッション履歴とどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するためのフックです。たとえば、履歴をトリムしたり、システムプロンプトを注入したりできます。

ネストされた ハンドオフ は opt-in ベータとして利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定の ハンドオフ に対して有効化するには `handoff(..., nest_handoff_history=True)` を設定して、折りたたみトランスクリプトの挙動を有効にします。raw なトランスクリプト（デフォルト）を維持したい場合は、このフラグを未設定のままにするか、会話を必要どおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を提供してください。カスタム mapper を書かずに生成要約で使われるラッパー文言を変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（既定に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

## 会話/チャット スレッド

いずれかの run メソッドを呼ぶと 1 つ以上の エージェント が実行される（したがって 1 回以上の LLM 呼び出しが行われる）可能性がありますが、これはチャット会話における単一の論理ターンを表します。たとえば次のとおりです。

1. ユーザー ターン：ユーザーがテキストを入力します
2. Runner 実行：最初の エージェント が LLM を呼び出し、ツールを実行し、2 番目の エージェント へ ハンドオフ し、2 番目の エージェント がさらにツールを実行してから出力を生成します。

エージェントの実行が終了したら、ユーザーに何を表示するかを選べます。たとえば、エージェントが生成した新しいアイテムをすべてユーザーに表示することも、最終出力だけを表示することもできます。いずれの場合も、ユーザーがフォローアップの質問をする可能性があり、その場合は run メソッドを再度呼び出せます。

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

### Sessions による自動の会話管理

より簡単なアプローチとして、[Sessions](sessions/index.md) を使えば `.to_input_list()` を手動で呼び出さずに会話履歴を自動的に扱えます。

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

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なる session ID ごとに別々の会話を維持します

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。

### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の conversation state 機能でサーバー側に会話状態を管理させることもできます。これにより、過去のメッセージをすべて手動で再送しなくても会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターンをまたいで状態を追跡する 2 つの方法を提供しています。

#### 1. `conversation_id` の使用

まず OpenAI Conversations API を使って会話を作成し、その ID を以後の呼び出しで毎回再利用します。

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

## モデル呼び出し入力フィルター

`call_model_input_filter` を使うと、モデル呼び出しの直前にモデル入力を編集できます。このフックは現在の エージェント、コンテキスト、結合済みの入力アイテム（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

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

このフックは `run_config` 経由で実行ごとに設定するか、`Runner` のデフォルトとして設定できます。機微なデータのマスキング、長い履歴のトリム、追加のシステム ガイダンスの注入などに使えます。

## 長時間実行エージェント & human-in-the-loop

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使うと、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使うと、human approval、ハンドオフ、セッション管理を含む、軽量で耐久性のある エージェント を利用できます。この統合には依存関係として Restate の単一バイナリ ランタイムが必要で、プロセス/コンテナとして、またはサーバーレス関数として エージェント を実行することをサポートします。
詳細は [overview](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[docs](https://docs.restate.dev/ai) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で送出されるすべての例外の基底クラスです。他のすべての具体的な例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えたときに送出されます。指定された対話ターン数以内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤モデル（LLM）が想定外または無効な出力を生成したときに発生します。これには次が含まれます。
    -   不正な形式の JSON：モデルがツール呼び出し、または直接出力に対して不正な JSON 構造を返した場合（特に特定の `output_type` が定義されている場合）。
    -   予期しないツール関連の失敗：モデルが期待される形でツールを使用できなかった場合
-   [`UserError`][agents.exceptions.UserError]：SDK を使ってコードを書くあなたが、SDK の利用中に誤りをしたときに送出されます。通常は、コード実装の誤り、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：入力 ガードレール または出力 ガードレール の条件が満たされたときに、それぞれ送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール は配信前にエージェントの最終応答をチェックします。