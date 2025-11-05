---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントをそのまま ストリーミング します。

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

`Runner` の run メソッドを使うときは、開始するエージェントと入力を渡します。入力は文字列（ ユーザー メッセージとみなされます）か、OpenAI Responses API のアイテムのリストのいずれかです。

runner は次のループを実行します。

1. 現在のエージェントに対し、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合は、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、所望の型のテキスト出力を生成し、ツール呼び出しがない場合です。

## ストリーミング

ストリーミング により、LLM の実行中に ストリーミング イベントを追加で受け取れます。ストリーム完了時、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新規出力を含む実行の完全な情報が含まれます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) をご覧ください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバルな設定を行えます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関わらず、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力／出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ にすでに設定がない場合に適用される、すべての ハンドオフ に対するグローバル入力フィルターです。入力フィルターにより、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM や ツール呼び出し の入出力など、機微なデータをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング用ワークフロー名、Trace ID、トレースグループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがってトレースを関連付けるのに使えます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話／チャットスレッド

任意の run メソッドを呼ぶと、1 つ以上のエージェント（つまり 1 回以上の LLM 呼び出し）が動作する場合がありますが、チャット会話の 1 つの論理的なターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへ ハンドオフ、2 番目のエージェントがさらにツールを実行し、その後に出力を生成。

エージェントの実行終了時に、ユーザー に何を見せるかを選べます。たとえば、エージェントが生成したすべての新しいアイテムを見せる、または最終出力のみを見せる、などです。いずれの場合でも、その後 ユーザー が追質問をするかもしれないので、そのときは再度 run メソッドを呼び出せます。

### 手動での会話管理

次のターンへの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使えば、`.to_input_list()` を手動で呼ばずに会話履歴を自動で処理できます。

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

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) をご覧ください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカル管理する代わりに、OpenAI の conversation state 機能に サーバー 側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを手動で再送せずに会話履歴を保持できます。詳しくは [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供しています。

#### 1. `conversation_id` の使用

最初に OpenAI Conversations API で会話を作成し、その ID を以降のすべての呼び出しで再利用します。

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id    

    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?", conversation_id=conv_id)
    print(result1.final_output)
    # San Francisco

    # Second turn reuses the same conversation_id
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        conversation_id=conv_id,
    )
    print(result2.final_output)
    # California
```

#### 2. `previous_response_id` の使用

もう一つの方法は、各ターンが前のターンのレスポンス ID に明示的にリンクする **レスポンスのチェイニング** です。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
    print(result1.final_output)
    # San Francisco

    # Second turn, chained to the previous response
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        previous_response_id=result1.last_response_id,
    )
    print(result2.final_output)
    # California
```


## 長時間実行のエージェントと Human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使えば、Human-in-the-loop を含む永続的で長時間実行のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他の特定の例外はすべてここから派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡された `max_turns` 制限を超えた場合に送出されます。指定された対話ターン数内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または無効な出力を生成したときに発生します。含まれる例:
    -   不正な形式の JSON: 特定の `output_type` が定義されている場合などに、ツール呼び出しや直接の出力で不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが想定どおりにツールを使用できない場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（SDK を使ってコードを書く）あなたが、SDK の使用方法を誤ったときに送出されます。典型的には、不正なコード実装、無効な設定、SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力 ガードレール または出力 ガードレール の条件が満たされたときに送出されます。入力 ガードレール は処理前に受信メッセージを、出力 ガードレール は配信前にエージェントの最終応答をチェックします。