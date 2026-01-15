---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントを逐次ストリーミングします。

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

詳細は [results guide](results.md) をご覧ください。

## エージェントループ

`Runner` の run メソッドを使うとき、開始するエージェントと入力を渡します。入力は文字列（ ユーザー メッセージとみなされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

runner は次のループを実行します:

1. 現在のエージェントに対し、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型のテキスト出力を生成し、かつツール呼び出しが一切ないことです。

## ストリーミング

ストリーミング を使うと、LLM 実行中のストリーミングイベントを追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行に関する完全な情報（生成されたすべての新規出力を含む）が含まれます。ストリーミングイベントは `.stream_events()` を呼び出してください。詳しくは [streaming guide](streaming.md) をご覧ください。

## Run 設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関わらず、使用するグローバルな LLM モデルを指定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力 / 出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に固有のフィルターがない場合に適用する、すべてのハンドオフに対するグローバル入力フィルターです。新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、runner は次のエージェントを呼び出す前に、過去のやり取りを 1 つの assistant メッセージに折りたたみます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロック内に配置し、その後のハンドオフごとに新しいターンを追記します。生の transcript をそのまま渡したい場合は、これを `False` にするか、必要に応じた内容を転送するカスタム ハンドオフ フィルターを指定してください。すべての [`Runner` methods](agents.run.Runner) は、未指定の場合に自動で `RunConfig` を作成するため、クイックスタートや code examples はこのデフォルトを自動的に使用します。また、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個々のハンドオフは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によってこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: オプションの callable。`nest_handoff_history` が `True` のとき、正規化された transcript（履歴 + handoff items）を受け取り、次のエージェントへ転送する入力アイテムのリストを正確に返します。フルのハンドオフフィルターを書くことなく、組み込みサマリーを置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [tracing](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行に対してエクスポーター、プロセッサー、またはトレーシングメタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けるのに役立ちます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時、各ターンの前に新しい ユーザー 入力をセッション履歴とどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、準備済みのモデル入力（instructions と入力アイテム）を編集するフックです。例えば履歴のトリミングや システムプロンプト の挿入に使えます。

デフォルトでは、SDK はあるエージェントが別のエージェントへハンドオフする際、過去のターンを 1 つの assistant サマリーメッセージの中にネストします。これにより assistant メッセージの重複を減らし、完全な transcript を新しいエージェントが素早くスキャンできる 1 つのブロックにまとめます。従来の挙動に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりに転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定のハンドオフについてオプトアウト（またはオプトイン）するには、`handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタムの mapper を書かずに生成サマリーで使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（デフォルトへ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

## 会話 / チャットスレッド

いずれかの run メソッドを呼ぶと、1 つ以上のエージェント（ひいては 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話では 1 つの論理的なターンを表します。例:

1. ユーザー ターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び、ツールを実行し、2 つ目のエージェントにハンドオフ、2 つ目のエージェントがさらにツールを実行し、最後に出力を生成。

エージェントの実行の最後に、ユーザーに何を表示するかを選べます。例えば、エージェントが生成したすべての新規アイテムを見せるか、最終出力だけを見せるかです。いずれの場合も、ユーザーが追質問をする可能性があり、その場合は再度 run メソッドを呼び出せます。

### 手動での会話管理

次のターンの入力を得るために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます:

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

### Sessions による自動会話管理

より簡単な方法として、[Sessions](sessions/index.md) を使うと、`.to_input_list()` を手動で呼び出さずに会話履歴を自動処理できます:

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

Sessions は自動的に次を行います:

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions documentation](sessions/index.md) をご覧ください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の conversation state 機能にサーバー側で会話状態を管理させることもできます。これにより、過去のメッセージをすべて手動で再送信せずに会話履歴を保持できます。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供します:

#### 1. `conversation_id` の使用

最初に OpenAI Conversations API を使って会話を作成し、その ID を以降のすべての呼び出しで再利用します:

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

もう一つの選択肢は、各ターンが前のターンのレスポンス ID に明示的にリンクする **response chaining** です。

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

モデル呼び出し直前にモデル入力を編集するには `call_model_input_filter` を使用します。フックは現在のエージェント、コンテキスト、（セッション履歴がある場合はそれも含む）結合済みの入力アイテムを受け取り、新しい `ModelInputData` を返します。

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

フックは `run_config` で実行ごとに設定するか、`Runner` にデフォルトとして設定して、機微情報のマスキング、長い履歴のトリミング、追加のシステムガイダンスの挿入などに使えます。

## 長時間実行エージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、耐久性のある長時間実行のワークフロー（human-in-the-loop タスクを含む）を実行できます。Temporal と Agents SDK が長時間タスクを完了する様子のデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基本クラスです。その他の特定の例外はこの汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` 制限をエージェントの実行が超えたときに送出されます。指定した対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例えば次が含まれます:
    -   不正な JSON: 特定の `output_type` が定義されている場合などに、ツール呼び出しや直接の出力で不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できない。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（SDK を用いてコードを書く人）が誤った使い方をしたときに送出されます。これは通常、不正なコード実装、無効な構成、SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力 ガードレール または出力 ガードレール の条件が満たされたときに、それぞれ送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールはエージェントの最終応答を配信前にチェックします。