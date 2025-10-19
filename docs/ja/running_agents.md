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

詳しくは[結果ガイド](results.md)をご覧ください。

## エージェントループ

`Runner` の run メソッドでは、開始するエージェントと入力を渡します。入力は文字列（ユーザーメッセージとみなされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

Runner は次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行し、結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型でテキスト出力を生成し、ツール呼び出しが 1 つもないことです。

## ストリーミング

ストリーミングを使うと、LLM 実行中のストリーミング イベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報（生成されたすべての新しい出力を含む）が含まれます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは[ストリーミング ガイド](streaming.md)をご覧ください。

## 実行設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力の ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに対して、既に個別設定がない場合に適用されるグローバルな入力フィルターです。入力フィルターにより、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の[トレーシング](tracing.md)を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、潜在的に機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。最低でも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にわたるトレースを関連付けるのに役立ちます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話/チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェント（したがって 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントにハンドオフ、2 番目のエージェントが更にツールを実行し、その後出力を生成。

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。例えば、エージェントが生成したすべての新しいアイテムを表示するか、最終出力のみを表示します。いずれの場合も、ユーザーが追質問をするかもしれず、その場合は再度 run メソッドを呼び出します。

### 手動の会話管理

次のターン用の入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions/index.md) を使用すれば、`.to_input_list()` を手動で呼び出さずに会話履歴を自動で扱えます:

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

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   セッション ID ごとに別々の会話を維持

詳しくは [Sessions のドキュメント](sessions/index.md)をご覧ください。


### サーバー管理の会話

`to_input_list()` や `Sessions` を使ってローカルで管理する代わりに、OpenAI の conversation state 機能に サーバー 側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを手動で再送せずに会話履歴を保持できます。詳しくは [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)をご覧ください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供します:

#### 1. `conversation_id` の使用

まず OpenAI Conversations API を使用して会話を作成し、その ID を後続のすべての呼び出しで再利用します:

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

もう 1 つの方法は、各ターンを直前のターンのレスポンス ID に明示的にリンクする **レスポンス チェイニング（response chaining）** です。

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


## 長時間実行エージェントとヒューマン・イン・ザ・ループ

Agents SDK の [Temporal](https://temporal.io/) 統合を使用すると、ヒューマン・イン・ザ・ループのタスクを含む永続的で長時間実行のワークフローを実行できます。長時間タスクを完了するために Temporal と Agents SDK が連携するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で、ドキュメントは[こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)をご覧ください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他の特定の例外はすべてこの汎用タイプから派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しないまたは無効な出力を生成した際に発生します。これには次が含まれます:
    -   不正な JSON: 特定の `output_type` が定義されている場合に特に、ツール呼び出しや直接出力で不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが想定どおりの方法でツールを使用できない場合。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（SDK を使ってコードを書く）あなたが誤りを行った場合に送出されます。これは一般に、誤ったコード実装、無効な設定、または SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力ガードレールまたは出力ガードレールの条件が満たされた場合に送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。