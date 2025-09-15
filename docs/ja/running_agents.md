---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラス経由で実行できます。方法は 3 つあります。

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

詳細は[結果ガイド](results.md)をご覧ください。

## エージェントループ

`Runner` の run メソッドを使用する際は、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）です。

Runner は次のループを実行します。

1. 現在のエージェントと現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新して、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行し、結果を追記して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、所望の型のテキスト出力を生成し、かつツール呼び出しがないことです。

## ストリーミング

ストリーミング を使うと、LLM 実行中のイベントを逐次受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] に、その実行で生成されたすべての新規出力を含む、完全な情報が格納されます。ストリーミング イベントは `.stream_events()` を呼び出して受け取れます。詳細は[ストリーミング ガイド](streaming.md)をご覧ください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関わらず、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーで、既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例: グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既存のフィルターがない場合に適用するグローバル入力フィルターです。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の[トレーシング](tracing.md)を無効化します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM およびツール呼び出しの入出力など、機微データをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング用 Workflow 名、Trace ID、Trace Group ID を設定します。少なくとも `workflow_name` の設定を推奨します。Group ID は任意で、複数実行にまたがるトレースを関連付けできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話/チャットスレッド

任意の run メソッドの呼び出しは、1 回以上のエージェント実行（つまり 1 回以上の LLM 呼び出し）につながる可能性がありますが、チャット会話の 1 つの論理ターンを表します。例:

1. ユーザー ターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントにハンドオフ、2 番目のエージェントがさらにツールを実行し、最後に出力を生成。

エージェント実行の最後に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成した新規アイテムをすべて表示するか、最終出力のみを表示します。いずれの場合も、ユーザーが追質問するかもしれないため、その際は再度 run メソッドを呼び出せます。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、次のターン用の入力を取得し、会話履歴を手動で管理できます。

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

-   各実行前に会話履歴を取得
-   各実行後に新規メッセージを保存
-   異なる セッション ID ごとに個別の会話を維持

詳細は [Sessions のドキュメント](sessions.md)をご覧ください。

## 長時間実行エージェントと Human-in-the-Loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使用して、Human-in-the-Loop を含む永続的で長時間実行のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了させるデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で、ドキュメントは[こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)をご覧ください。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他の特定例外はこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 制限を超えた場合に送出されます。指定されたターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が想定外または不正な出力を生成した場合に発生します。例:
    -   不正な JSON: 特に特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力として不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できない場合。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（SDK 利用者）が誤りを犯した場合に送出されます。誤ったコード実装、無効な設定、SDK の API の誤用などが典型例です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力 ガードレール または出力 ガードレール の条件が満たされた場合に送出されます。入力 ガードレール は処理前に受信メッセージを検査し、出力 ガードレール はエージェントの最終応答を配信前に検査します。