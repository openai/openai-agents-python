---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミング モードで呼び出し、受信したイベントを順次ストリーミングします。

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

`Runner` の run メソッドを使うときは、開始するエージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）です。

runner は次のループを実行します。

1. 現在のエージェントと現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる規則は、望ましい型のテキスト出力を生成し、ツール呼び出しがない場合です。

## ストリーミング

ストリーミングを使用すると、LLM の実行に伴うストリーミング イベントを追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が含まれます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力／出力のガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに入力フィルターが未設定の場合に適用する、すべてのハンドオフに対するグローバルな入力フィルターです。入力フィルターを使うと、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングにおけるワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は任意で、複数の実行にまたがるトレースを関連付けるのに使えます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話／チャットスレッド

任意の run メソッドの呼び出しは、1 つ以上のエージェント（したがって 1 回以上の LLM 呼び出し）の実行につながる場合がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザー ターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントにハンドオフし、2 番目のエージェントがさらにツールを実行してから出力を生成します。

エージェント実行の最後に、ユーザーに何を表示するかを選べます。たとえば、エージェントが生成したすべての新しいアイテムをユーザーに表示する、または最終出力のみを表示する、などです。いずれにせよ、ユーザーがフォローアップの質問をするかもしれないので、その場合は再度 run メソッドを呼び出せます。

### 手動の会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions.md) を使うと、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動で扱えます。

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
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行エージェントとヒューマン・イン・ザ・ループ

Agents SDK の [Temporal](https://temporal.io/) 連携を使用すると、ヒューマン・イン・ザ・ループのタスクを含む、永続的で長時間実行のワークフローを実行できます。長時間実行タスクを完了するために Temporal と Agents SDK が連携して動作するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で確認でき、[こちらのドキュメント](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)も参照してください。

## 例外

この SDK は、特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定の例外はすべて、この型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` の上限をエージェントの実行が超えたときに送出されます。これは、指定された対話ターン数内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。次を含みます。
    -   不正な JSON: 特定の `output_type` が定義されている場合に特に、ツール呼び出しや直接の出力でモデルが不正な JSON 構造を返したとき。
    -   予期しないツール関連の失敗: モデルが期待どおりの方法でツールを使用できなかったとき。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（この SDK を使ってコードを書く人）がエラーを起こしたときに送出されます。これは通常、誤ったコード実装、無効な設定、または SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力ガードレールまたは出力ガードレールの条件が満たされたときに送出されます。入力ガードレールは処理前に受信メッセージを検査し、出力ガードレールは配信前にエージェントの最終応答を検査します。