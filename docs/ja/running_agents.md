---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを使ってエージェントを実行できます。次の 3 つの方法があります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリーミングします。

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

`Runner` の run メソッドを使うとき、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージとみなされます）か、OpenAI Responses API の入力アイテムのリストのいずれかです。

ランナーは次のループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM がハンドオフした場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、要求された型のテキスト出力を生成し、ツール呼び出しがない場合です。

## ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミングイベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に、その実行で生成されたすべての新しい出力を含む完全な情報が格納されます。ストリーミングイベントは `.stream_events()` を呼び出して受け取れます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行構成

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます。

- [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーで、デフォルトは OpenAI です。
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力のガードレールのリストです。
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既定のフィルターがない場合に適用するグローバル入力フィルターです。入力フィルターにより、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに、LLM やツール呼び出しの入出力など、機微なデータを含めるかどうかを設定します。
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング用 workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は任意で、複数の実行にまたがるトレースを関連付けるのに使えます。
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話/チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上のエージェント（ひいては 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話の 1 つの論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. ランナーの実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントにハンドオフ。2 番目のエージェントがさらにツールを実行し、出力を生成。

エージェントの実行終了時に、ユーザーに何を表示するかを選べます。たとえば、エージェントによって生成されたすべての新しいアイテムを表示する、または最終出力のみを表示するなどです。いずれの場合も、ユーザーが追質問をするかもしれません。その場合は再度 run メソッドを呼び出してください。

### 手動での会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions.md) を使えば、`.to_input_list()` を手動で呼び出さずに会話履歴を自動で扱えます。

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

- 各実行の前に会話履歴を取得
- 各実行の後に新しいメッセージを保存
- 異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行エージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使って、human-in-the-loop タスクを含む、耐久的で長時間実行のワークフローを動かせます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) を参照してください。

## 例外

この SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

- [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定の例外はこの一般的な型から派生します。
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡した `max_turns` の上限を超えた場合に送出されます。指定したインタラクション回数内にタスクを完了できなかったことを示します。
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例としては次が含まれます。
    - 不正な JSON: とくに特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力で不正な JSON 構造を返すケース。
    - 予期しないツール関連の失敗: モデルが期待どおりにツールを使用できなかった場合
- [`UserError`][agents.exceptions.UserError]: SDK を使用する際に（SDK を用いたコードを書く）あなたがエラーを起こしたときに送出されます。これは通常、不正なコード実装、無効な構成、または SDK の API の誤用が原因です。
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力ガードレールまたは出力ガードレールの条件が満たされたときに送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終応答をチェックします。