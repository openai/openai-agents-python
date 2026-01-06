---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。選択肢は 3 つあります:

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

詳しくは [execution results ガイド](results.md) を参照してください。

## エージェント ループ

`Runner` の run メソッドを使うとき、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージと見なされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）です。

Runner は次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行して結果を追記し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、目的のタイプのテキスト出力を生成し、かつツール呼び出しが 1 つもないことです。

## ストリーミング

ストリーミング を使うと、LLM の実行中に ストリーミング イベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に、その実行で生成されたすべての新規出力を含む完全な情報が入ります。ストリーミング イベントは `.stream_events()` を呼び出してください。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを指定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデル プロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに個別のフィルターがない場合に適用する、すべてのハンドオフに対するグローバルな入力フィルターです。新しいエージェントに渡す入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、次のエージェントを呼び出す前に、Runner は直前までの会話を 1 件の assistant メッセージにまとめます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロックに入れ、後続のハンドオフが発生するたびに新しいターンを追記していきます。生のトランスクリプトをそのまま渡したい場合は `False` にするか、カスタムのハンドオフ フィルターを指定してください。いずれの [`Runner` メソッド](agents.run.Runner) も、`RunConfig` を渡さなかった場合に自動的に作成するため、クイックスタートや code examples はこのデフォルトを自動的に使用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックがある場合は引き続きそれが優先されます。個々のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` が `True` のときに正規化済みトランスクリプト（履歴 + ハンドオフ項目）を受け取り、次のエージェントへ転送する入力アイテムのリストを正確に返す任意の呼び出し可能オブジェクトです。完全なハンドオフ フィルターを書くことなく、組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、潜在的に機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

デフォルトでは、あるエージェントが別のエージェントにハンドオフする際、SDK は直前のターンを 1 つの assistant 要約メッセージに入れ子にします。これにより重複する assistant メッセージが減り、新しいエージェントがすばやく走査できる単一ブロック内に完全なトランスクリプトを保持できます。従来の挙動に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定のハンドオフ単位でオプトアウト（またはオプトイン）するには、`handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタム マッパーを書かずに生成される要約のラッパー テキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェント（つまり 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザー ターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントにハンドオフ。2 番目のエージェントがさらにツールを実行し、その後に出力を生成。

エージェントの実行が終わったら、ユーザーに何を表示するかを選べます。たとえば、エージェントが生成したすべての新しいアイテムを表示するか、最終出力のみを表示します。いずれの場合も、ユーザーが追質問をするかもしれないので、そのときは再度 run メソッドを呼び出してください。

### 手動の会話管理

次のターン用の入力を取得するには、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions/index.md) を使えば、`.to_input_list()` を手動で呼び出さなくても会話履歴を自動的に処理できます:

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

Sessions は自動で次を行います:

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。


### サーバー管理の会話

OpenAI の会話状態機能に会話状態の管理をサーバー 側で任せることもでき、`to_input_list()` や `Sessions` でローカル管理する代わりになります。これにより、過去のメッセージをすべて手動で再送しなくても会話履歴を保持できます。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供しています:

#### 1. `conversation_id` の使用

まず OpenAI Conversations API を使って会話を作成し、その ID を以降の呼び出しですべて再利用します:

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

もう 1 つの方法は、各ターンが前のターンのレスポンス ID に明示的にリンクする **レスポンス チェーン（response chaining）** です。

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

## 長時間実行エージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む耐障害性のある長時間実行ワークフローを動かせます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で、ドキュメントは[こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)をご覧ください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。他の特定の例外はすべてこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡された `max_turns` 制限を超えた場合に送出されます。指定された対話ターン数内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。例:
    -   不正な JSON: モデルがツール呼び出しや直接の出力で不正な JSON 構造を返した場合。特に特定の `output_type` が定義されているときに問題になります。
    -   予期しないツール関連の失敗: モデルが期待どおりの方法でツールを使用できなかった場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（コードを書く）あなたが、SDK の使用中にエラーを起こした場合に送出されます。通常は、コードの誤り、無効な設定、SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力 ガードレール または出力 ガードレール の条件が満たされた場合に送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール はエージェントの最終応答を配信前にチェックします。