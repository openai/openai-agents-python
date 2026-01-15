---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。オプションは 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミング モードで呼び出し、受信したイベントをリアルタイムでストリーミングします。

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

`Runner` の run メソッドを使うとき、開始エージェントと入力を渡します。入力は文字列（ ユーザー メッセージとして扱われます）か、OpenAI Responses API のアイテムである入力アイテムのリストのいずれかです。

runner は次のループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、望ましい型のテキスト出力を生成し、かつ ツール呼び出し が存在しないことです。

## ストリーミング

ストリーミングにより、LLM の実行中にストリーミングイベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が含まれます。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳細は [streaming guide](streaming.md) を参照してください。

## 実行設定 (Run config)

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力の ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に独自のフィルターがない場合に適用する、すべてのハンドオフに対するグローバルな入力フィルターです。入力フィルターにより、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、runner は次のエージェントを呼び出す前に、これまでの発話履歴を 1 つの assistant メッセージにまとめます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロック内に配置し、その後のハンドオフが発生するたびに新しいターンを追加します。以前の raw な書き起こしをそのまま渡したい場合は、これを `False` に設定するか、カスタムのハンドオフフィルターを指定してください。すべての [`Runner` methods](agents.run.Runner) は、未指定時に自動で `RunConfig` を作成するため、クイックスタートや code examples はこのデフォルトを自動で利用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個々のハンドオフは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` が `True` のときに正規化された書き起こし（履歴 + ハンドオフ項目）を受け取る任意の呼び出し可能オブジェクトです。次のエージェントに転送する入力アイテムの正確なリストを返す必要があり、完全なハンドオフフィルターを書くことなく組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [tracing](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行のエクスポーター、プロセッサー、またはトレーシングメタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM や ツール呼び出し の入出力など、潜在的に機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: この実行のトレーシングのワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターンの前に新しい ユーザー 入力をセッション履歴とどのようにマージするかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するフックです。例えば履歴のトリミングや system prompt の注入に使用します。

デフォルトでは、SDK はあるエージェントが別のエージェントにハンドオフするたびに、前のターンを 1 つの assistant 要約メッセージ内に入れ子にします。これにより、assistant メッセージの重複を減らし、完全な書き起こしを新しいエージェントが高速にスキャンできる 1 つのブロック内に保持します。従来の挙動に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定のハンドオフでのみオプトアウト（またはオプトイン）するには、`handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタムマッパーを書かずに生成される要約のラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話／チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行される（つまり、1 回以上の LLM 呼び出し）可能性がありますが、チャット会話における 1 つの論理的なターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフ、2 番目のエージェントがさらにツールを実行し、最終的に出力を生成。

エージェント実行の最後に、ユーザー に何を表示するかを選べます。例えば、エージェントによって生成されたすべての新しいアイテムを表示するか、最終出力のみを表示します。どちらの場合でも、ユーザー が追質問をするかもしれないので、その場合は再度 run メソッドを呼び出します。

### 手動の会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使うと、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動処理できます。

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

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   セッション ID ごとに独立した会話を維持

詳細は [Sessions documentation](sessions/index.md) を参照してください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の会話状態機能により サーバー 側で会話状態を管理することもできます。これにより、過去のメッセージを手動で再送せずに会話履歴を保持できます。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間で状態を追跡する 2 つの方法を提供します。

#### 1. `conversation_id` を使用

まず OpenAI Conversations API を使って会話を作成し、その ID を以後の呼び出しで使い回します。

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

もう 1 つの方法は **response chaining** で、各ターンが前のターンのレスポンス ID に明示的にリンクします。

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

モデル呼び出し直前のモデル入力を編集するには `call_model_input_filter` を使用します。フックは現在のエージェント、コンテキスト、（存在する場合はセッション履歴を含む）結合済み入力アイテムを受け取り、新しい `ModelInputData` を返します。

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

`run_config` で実行ごとに、または `Runner` のデフォルトとしてフックを設定して、機微情報の編集、長い履歴のトリミング、追加の system prompt の注入などを行えます。

## 長時間実行のエージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使用すると、human-in-the-loop タスクを含む永続的で長時間実行のワークフローを実行できます。Temporal と Agents SDK が協調して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他すべての特定例外はこの汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡された `max_turns` 制限を超えたときに送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（LLM）が想定外または無効な出力を生成した場合に発生します。例えば次のようなケースです。
    -   不正な JSON: 特定の `output_type` が定義されている場合に特に、ツール呼び出しや直接の出力で不正な JSON 構造を返した。
    -   想定外のツール関連の失敗: モデルが想定どおりにツールを使用できなかった。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（コードを書く）あなたが誤りを犯した場合に送出されます。これは通常、不正なコード実装、無効な構成、SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力 ガードレール または出力 ガードレール の条件が満たされた場合にそれぞれ送出されます。入力 ガードレール は処理前の受信メッセージを検査し、出力 ガードレール はエージェントの最終応答を配信前に検査します。