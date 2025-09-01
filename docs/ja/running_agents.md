---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスでエージェントを実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信次第イベントをストリーミングします。

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

詳しくは [実行結果ガイド](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドを使うとき、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージとして扱われます）か、OpenAI Responses API のアイテムのリストのいずれかです。

Runner は次のループを実行します:

1. 現在のエージェントと現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し、実行結果を返します。
    2. LLM がハンドオフした場合、現在のエージェントと入力を更新して、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」とみなされる条件は、所望の型のテキスト出力を生成し、ツール呼び出しが 1 つもないことです。

## ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成された新しい出力を含む実行全体の情報が含まれます。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関わらず、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名のルックアップに使うプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力ガードレールのリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既定のものがない場合に適用するグローバルな入力フィルター。入力フィルターを使うと、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに、LLM やツール呼び出しの入出力などの機微なデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングのワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けるのに使えます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。

## 会話/チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上のエージェント（したがって 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行してから出力を生成。

エージェントの実行が終わったら、ユーザーに何を見せるかを選べます。たとえば、エージェントが生成したすべての新しいアイテムを表示する、または最終出力のみを表示する、といった選択です。いずれの場合でも、ユーザーが追質問をするかもしれません。その場合は再度 run メソッドを呼び出せます。

### 手動の会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます:

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

より簡単な方法として、[セッション](sessions.md) を使えば `.to_input_list()` を手動で呼び出さずに会話履歴を自動処理できます:

```python
from agents import Agent, Runner, SQLiteSession

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create session instance
    session = SQLiteSession("conversation_123")

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

セッションは自動的に次を行います:

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   異なるセッション ID ごとに個別の会話を維持

詳細は [セッションのドキュメント](sessions.md) を参照してください。

## 長時間実行のエージェントとヒューマン・イン・ザ・ループ

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、ヒューマン・イン・ザ・ループを含む永続的で長時間実行のワークフローを実行できます。長時間タスクを完了させる Temporal と Agents SDK の連携デモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

この SDK は特定の状況で例外を送出します。全リストは [`agents.exceptions`][] にあります。概要:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定の例外はすべてここから派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡された `max_turns` の上限を超えたときに送出されます。エージェントが指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例:
    -   不正な JSON: モデルがツール呼び出し用、または特定の `output_type` が定義されている場合の直接出力として、不正な JSON 構造を返したとき。
    -   予期しないツール関連の失敗: モデルが期待どおりの方法でツールを使用できなかったとき
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（この SDK を使ってコードを書く）あなたが、SDK の使用中に誤りを犯したときに送出されます。誤ったコード実装、無効な設定、SDK の API の誤用などが典型的な原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力ガードレールまたは出力ガードレールの条件が満たされたときに送出されます。入力ガードレールは処理前に受信メッセージを確認し、出力ガードレールは配信前にエージェントの最終応答を確認します。