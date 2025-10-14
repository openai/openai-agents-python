---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントを逐次 ストリーミング します。

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

## エージェントのループ

`Runner` の run メソッドを使うとき、開始するエージェントと入力を渡します。入力は文字列（ ユーザー メッセージとみなされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）です。

その後、Runner は以下のループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM が ツール呼び出し を行った場合、それらを実行して結果を追記し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、求められた型のテキスト出力を生成し、かつツール呼び出しがないことです。

## ストリーミング

ストリーミング を使うと、LLM の実行中に ストリーミング イベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行全体の情報（新しく生成されたすべての出力を含む）が含まれます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を行えます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関わらず、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名のルックアップに使うモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: すべての ハンドオフ に適用するグローバル入力フィルターです（ハンドオフ 側で既に設定されていない場合）。入力フィルターにより、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話/チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上のエージェントが実行される（つまり 1 回以上の LLM 呼び出しが行われる）可能性がありますが、これはチャット会話の単一の論理ターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへ ハンドオフ、2 つ目のエージェントがさらにツールを実行し、その後出力を生成

エージェントの実行の最後に、 ユーザー に何を表示するかを選べます。例えば、エージェントが生成した新しいアイテムをすべて表示するか、最終出力のみを表示します。いずれの場合も、 ユーザー が後続の質問をするかもしれず、その場合は再度 run メソッドを呼び出せます。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドで、次のターンの入力を取得し、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions.md) を使うと、`.to_input_list()` を手動で呼び出すことなく、会話履歴を自動管理できます。

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
-   セッション ID ごとに個別の会話を維持

詳しくは [Sessions のドキュメント](sessions.md) を参照してください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに管理する代わりに、OpenAI の会話状態機能に サーバー 側での状態管理を任せることもできます。これにより、過去のメッセージをすべて手動で再送信せずに会話履歴を保持できます。詳しくは [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供します。

#### 1. `conversation_id` を使う

まず OpenAI Conversations API で会話を作成し、その ID を以降のすべての呼び出しで再利用します。

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

#### 2. `previous_response_id` を使う

もう 1 つの選択肢は **response chaining** で、各ターンが前のターンのレスポンス ID に明示的にリンクします。

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


## 長時間実行のエージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop（人間参加型）タスクを含む、耐久性のある長時間実行のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で、ドキュメントは[こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)をご覧ください。

## 例外

SDK は特定の状況で例外を送出します。全リストは [`agents.exceptions`][] にあります。概要は以下のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定の例外はここから派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。例:
    -   不正な JSON: 特定の `output_type` が定義されている場合などに、モデルがツール呼び出しや直接出力で不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できない場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（この SDK を使ってコードを書く人）が誤った使い方をした場合に送出されます。誤ったコード実装、無効な設定、SDK の API の誤用などが原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力 ガードレール または出力 ガードレール の条件が満たされた場合に送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール は配信前にエージェントの最終応答をチェックします。