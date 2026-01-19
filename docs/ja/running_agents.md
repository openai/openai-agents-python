---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを使って エージェント を実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントを順次 ストリーミング します。

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

詳しくは [結果ガイド](results.md) をご覧ください。

## エージェントループ

`Runner` の run メソッドを使うとき、開始 エージェント と入力を渡します。入力は文字列（ ユーザー メッセージと見なされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）です。

runner は次のループを実行します。

1. 現在の エージェント と現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在の エージェント と入力を更新し、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追記し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、ツール呼び出しが存在しない場合です。

## ストリーミング

ストリーミング を使うと、LLM 実行中の ストリーミング イベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報（生成されたすべての新規出力を含む）が含まれます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) をご覧ください。

## Run 設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力 ガードレール のリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既定のものがない場合にすべての ハンドオフ に適用するグローバル入力フィルタ。入力フィルタでは、新しい エージェント に送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（既定）の場合、runner は次の エージェント を呼び出す前に、それまでの発話録を 1 件の assistant メッセージへ折りたたみます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロックに配置し、以降の ハンドオフ ごとに新しいターンを追加します。raw の発話録をそのまま渡したい場合は、これを `False` にするか、カスタムの ハンドオフ フィルタを指定してください。すべての [`Runner` メソッド](agents.run.Runner) は、未指定時に自動で `RunConfig` を作成するため、クイックスタートや code examples はこの既定値を自動で利用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックがある場合は引き続きそれが優先されます。個々の ハンドオフ は [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` が `True` のときに正規化された発話録（履歴 + ハンドオフ アイテム）を受け取る任意の callable。次の エージェント に転送する入力アイテムのリストを正確に返す必要があり、完全な ハンドオフ フィルタを書かずに組み込み要約を差し替えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体に対する [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行のエクスポーター、プロセッサー、またはトレーシング メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、センシティブなデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID はオプションで、複数の実行にまたがるトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターン前の新規 ユーザー 入力をセッション履歴とマージする方法をカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するフック。たとえば履歴のトリミングや system prompt の注入などに使えます。

既定では、SDK は エージェント が別の エージェント へ ハンドオフ するたびに、それまでのターンを 1 つの assistant の要約メッセージにネストします。これにより重複する assistant メッセージが減り、新しい エージェント が迅速にスキャンできる単一ブロックに完全な発話録が収まります。旧来の挙動に戻したい場合は `RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定の ハンドオフ に対して個別にオプトアウト（またはオプトイン）するには、`handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタム マッパーを書かずに生成される要約で用いられるラッパーテキストを変更したい場合は、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（既定に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

## 会話/チャットスレッド

任意の run メソッドの呼び出しは、1 つ以上の エージェント の実行（つまり 1 回以上の LLM 呼び出し）につながる可能性がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. Runner の実行: 最初の エージェント が LLM を呼び出し、ツールを実行し、2 番目の エージェント へ ハンドオフ。2 番目の エージェント がさらにツールを実行し、出力を生成。

エージェント の実行が終わったら、ユーザー に何を表示するかを選べます。たとえば、エージェント によって生成されたすべての新しいアイテムを見せるか、最終出力のみを見せるかです。いずれにせよ、ユーザー がフォローアップの質問をするかもしれないため、その場合は再度 run メソッドを呼び出せばよいです。

### 手動での会話管理

次のターン用の入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使えば、手動で `.to_input_list()` を呼び出さなくても会話履歴を自動的に処理できます。

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
-   セッション ID ごとに別個の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) をご覧ください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の conversation state 機能により サーバー 側で会話状態を管理することもできます。これにより、過去のすべてのメッセージを手動で再送信することなく会話履歴を保持できます。詳しくは [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI はターン間で状態を追跡する 2 つの方法を提供しています。

#### 1. `conversation_id` の使用

まず OpenAI Conversations API を使って会話を作成し、その ID を後続のすべての呼び出しで再利用します。

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

別の方法は、各ターンが前のターンのレスポンス ID に明示的にリンクする、 **レスポンス チェイニング** です。

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

モデル呼び出し直前にモデル入力を編集するには `call_model_input_filter` を使用します。フックは現在の エージェント、コンテキスト、（セッション履歴がある場合はそれも含む）結合済みの入力アイテムを受け取り、新しい `ModelInputData` を返します。

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

実行ごとに `run_config` で設定するか、`Runner` のデフォルトとして設定して、センシティブデータの編集、長い履歴のトリミング、追加のシステムガイダンスの注入などを行います。

## 長時間実行エージェントと人間の介在

Agents SDK の [Temporal](https://temporal.io/) 連携を使って、耐久性のある長時間実行ワークフロー（human-in-the-loop タスクを含む）を実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定例外はすべてこの汎用型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェント の実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡した `max_turns` 制限を超えたときに送出されます。指定された対話ターン数内に エージェント がタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには次が含まれます。
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力で不正な JSON 構造を返す場合。
    -   予期しないツール関連の失敗: モデルが想定どおりにツールを使用できない場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（SDK を使ってコードを書く人）が誤りを犯した場合に送出されます。これは通常、コードの誤実装、無効な設定、SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力 ガードレール または出力 ガードレール の条件が満たされたときに、それぞれ送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール は エージェント の最終応答を配信前にチェックします。