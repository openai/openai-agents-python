---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
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

詳細は [結果ガイド](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドを使うとき、開始するエージェントと入力を渡します。入力は文字列（ ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

Runner は次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM が ツール呼び出し を行った場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、ツール呼び出しがないことです。

## ストリーミング

ストリーミングを使うと、LLM の実行に合わせてストリーミングイベントも受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新規出力を含む実行の完全な情報が入ります。ストリーミングイベントは `.stream_events()` を呼び出してください。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます:

- [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関わらず、使用するグローバルな LLM モデルを設定できます。
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名の解決に使うモデルプロバイダーで、既定は OpenAI です。
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力の ガードレール のリストです。
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に適用するグローバルな入力フィルターです（ハンドオフに既存のフィルターがない場合）。入力フィルターにより、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを構成します。
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けるのに使えます。
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話/チャットスレッド

いずれの run メソッドを呼び出した場合も、1 つ以上のエージェント（つまり 1 回以上の LLM 呼び出し）が実行されることがありますが、チャット会話の単一の論理ターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへ ハンドオフ、2 つ目のエージェントがさらにツールを実行し、最終的な出力を生成。

エージェントの実行が終わったら、ユーザー に何を見せるかを選べます。例えば、エージェントが生成したすべての新規アイテムを見せるか、最終出力のみを見せるかです。いずれにしても、その後に ユーザー が追質問をするかもしれません。その場合は run メソッドを再度呼び出してください。

### 手動での会話管理

次のターンの入力を取得するには、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions.md) を使うと、`.to_input_list()` を手動で呼ばなくても会話履歴を自動で処理できます:

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

Sessions は自動で以下を行います:

- 各実行の前に会話履歴を取得
- 各実行の後に新規メッセージを保存
- セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行のエージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop のタスクを含む、耐久性のある長時間実行のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)です。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要:

- [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定の例外はこの汎用型から派生します。
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` の上限を超えた場合に送出されます。指定した対話ターン数内にエージェントがタスクを完了できなかったことを示します。
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または不正な出力を生成した場合に発生します。例:
    - 不正な JSON: ツール呼び出し用、または直接の出力で不正な JSON 構造を返した場合（特に特定の `output_type` が定義されているとき）。
    - 予期しないツール関連の失敗: モデルが想定どおりにツールを使用できなかった場合
- [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（この SDK を使ってコードを書く人）が誤りを犯した場合に送出されます。これは通常、不正なコード実装、無効な設定、または SDK の API の誤用が原因です。
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力 ガードレール または出力 ガードレール の条件が満たされた場合に、それぞれ送出されます。入力 ガードレール は処理前に受信メッセージを検査し、出力 ガードレール は配信前にエージェントの最終応答を検査します。