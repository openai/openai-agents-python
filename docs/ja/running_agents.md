---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミング モードで呼び出し、受信したイベントを逐次ストリーミングします。

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

## エージェント ループ

`Runner` の run メソッドを使うとき、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）か、OpenAI Responses API の入力アイテムのリストのいずれかです。

runner は次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、ツール呼び出しが無いことです。

## ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミング イベントを受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新規出力を含む実行の完全な情報が含まれます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターは、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデル プロバイダー。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力のガードレールのリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既に設定がない場合に適用されるグローバルな入力フィルター。入力フィルターにより、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。

## 会話/チャットスレッド

いずれかの run メソッドを呼び出すと 1 つ以上のエージェント（したがって 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話の 1 つの論理ターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフ、2 番目のエージェントがさらにツールを実行し、最終的に出力を生成。

エージェント実行の最後に、ユーザーに何を見せるかを選べます。例えば、エージェントが生成したすべての新規アイテムを見せることも、最終出力だけを見せることもできます。いずれにせよ、ユーザーが追質問をするかもしれないので、その場合は再度 run メソッドを呼び出します。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドで次のターンの入力を取得し、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions.md) を使えば、`.to_input_list()` を手動で呼び出さずに会話履歴を自動処理できます:

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
-   異なるセッション ID ごとに個別の会話を維持

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行のエージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む永続的で長時間実行のワークフローを動かせます。Temporal と Agents SDK が協調して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を参照し、[こちらのドキュメント](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) もご覧ください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他の特定の例外はここから派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` 上限を超えたときに送出されます。指定されたやり取り回数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル (LLM) が予期しない、または無効な出力を生成した場合に発生します。例:
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力で不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できない。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（SDK を使ってコードを書く人）が、誤った使い方をした場合に送出されます。誤ったコード実装、無効な設定、SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力ガードレールまたは出力ガードレールの条件に合致した際に送出されます。入力ガードレールは処理前の受信メッセージを、出力ガードレールはエージェントの最終応答を配信前にチェックします。