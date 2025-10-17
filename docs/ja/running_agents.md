---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。オプションは 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期に実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期に実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリーミングします。

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

詳細は [結果ガイド](results.md) をご覧ください。

## エージェントループ

`Runner` の run メソッドを使うときは、開始するエージェントと入力を渡します。入力は文字列（ユーザーからのメッセージとして扱われます）か、OpenAI Responses API のアイテムのリストのいずれかです。

ランナーは次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、ツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングにより、LLM の実行中にストリーミングイベントを追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行に関する完全な情報（生成されたすべての新しい出力を含む）が含まれます。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳細は [ストリーミングガイド](streaming.md) をご覧ください。

## 実行設定

`run_config` パラメーターを使うと、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関わらず、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力の ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: すべての ハンドオフ に適用するグローバルな入力フィルター（ハンドオフに既定のものがない場合）。入力フィルターにより、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに LLM やツール呼び出しの入出力などの機微なデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング用 workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID はオプションで、複数の実行にわたるトレースの関連付けに使えます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話/チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上のエージェント（つまり 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話の単一の論理的ターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントに ハンドオフ、2 番目のエージェントがさらにツールを実行し、その後に出力を生成。

エージェントの実行終了時に、ユーザーに何を表示するかを選択できます。たとえば、エージェントが生成したすべての新しいアイテムを表示するか、最終出力のみを表示するかです。いずれの場合でも、ユーザーがフォローアップの質問をする可能性があり、その場合は再び run メソッドを呼び出します。

### 手動の会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions/index.md) を使って `.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に処理できます:

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

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) をご覧ください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の conversation state 機能にサーバー側で会話状態を管理させることもできます。これにより、過去のメッセージをすべて手動で再送信せずに会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI はターン間で状態を追跡する方法を 2 つ提供しています:

#### 1. `conversation_id` の使用

最初に OpenAI Conversations API で会話を作成し、その ID を以後のすべての呼び出しで再利用します:

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

もう 1 つの方法は **response chaining** で、各ターンを前のターンの response ID に明示的にリンクします。

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

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、ヒューマン・イン・ザ・ループのタスクを含む、永続的で長時間実行のワークフローを動かせます。Temporal と Agents SDK が連携して長時間実行タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で、ドキュメントは[こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)をご覧ください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。ほかの特定の例外はすべてこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` 制限を超えた場合に送出されます。指定した対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには次が含まれます:
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接の出力で不正な JSON 構造を返す場合。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できなかった場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（コードを書く）あなたが、SDK の使用中にエラーを起こした場合に送出されます。これは通常、不正なコード実装、無効な設定、SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力 ガードレール または出力 ガードレール の条件を満たした場合に送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール は配信前にエージェントの最終応答をチェックします。