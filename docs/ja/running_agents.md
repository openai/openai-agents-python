---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスでエージェントを実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。 LLM をストリーミングモードで呼び出し、受信したイベントを逐次ストリーミングします。

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

詳細は [実行結果ガイド](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドを使うとき、開始するエージェントと入力を渡します。入力は文字列（ユーザーからのメッセージとみなされます）か、 OpenAI Responses API の入力アイテムのリストのいずれかです。

Runner は次のループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツールコールを生成した場合、それらを実行し、結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型のテキスト出力を生成し、ツールコールが存在しないことです。

## ストリーミング

ストリーミングを使うと、 LLM の実行中にストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む実行の完全情報が格納されます。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力／出力のガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: すでに設定されていない場合に、すべてのハンドオフに適用するグローバルな入力フィルターです。入力フィルターを使うと、新しいエージェントに送る入力を編集できます。詳しくは [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の[トレーシング](tracing.md)を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツールコールの入出力など、トレースに機微なデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングにおけるワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話／チャットスレッド

任意の run メソッドの呼び出しは、1 つ以上のエージェント（つまり 1 回以上の LLM 呼び出し）が走る可能性がありますが、チャット会話における 1 回の論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントにハンドオフ。2 つ目のエージェントがさらにツールを実行し、出力を生成。

エージェントの実行が終わったら、ユーザーに何を見せるかを選べます。たとえば、エージェントが生成したすべての新規アイテムを見せるか、最終出力だけを見せるかです。いずれにせよ、ユーザーが追質問をするかもしれないため、その場合は再度 run メソッドを呼び出します。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って、次のターンの入力を取得し、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions.md) を使えば、`.to_input_list()` を手動で呼び出さずに会話履歴を自動処理できます。

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
-   異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行のエージェントとヒューマン・イン・ザ・ループ

Agents SDK の [Temporal](https://temporal.io/) 連携を使って、ヒューマン・イン・ザ・ループを含む永続的で長時間実行のワークフローを動かせます。 Temporal と Agents SDK が連携して長時間タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で、ドキュメントは[こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)でご覧いただけます。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。ほかの特定の例外はすべてここから派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 制限を超えたときに送出されます。指定した対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（ LLM ）が予期せぬ、または無効な出力を生成したときに発生します。次を含む場合があります。
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツールコールや直接出力として不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが想定どおりにツールを使用できない。
-   [`UserError`][agents.exceptions.UserError]: SDK を使うあなた（この SDK を用いてコードを書く人）が誤った使い方をしたときに送出されます。通常は、誤ったコード実装、無効な設定、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件が満たされたときに、それぞれ送出されます。入力ガードレールは処理前の受信メッセージを、出力ガードレールは配信前のエージェント最終応答を検査します。