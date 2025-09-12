---
search:
  exclude: true
---
# エージェントの実行

エージェント は [ `Runner` ][agents.run.Runner] クラスで実行できます。方法は 3 つあります:

1. [ `Runner.run()` ][agents.run.Runner.run]: 非同期で実行し、[ `RunResult` ][agents.result.RunResult] を返します。
2. [ `Runner.run_sync()` ][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [ `Runner.run_streamed()` ][agents.run.Runner.run_streamed]: 非同期で実行し、[ `RunResultStreaming` ][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントをそのまま ストリーミング します。

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

詳しくは [実行結果ガイド](results.md) をご覧ください。

## エージェント ループ

`Runner` の run メソッドを使うとき、開始するエージェント と入力を渡します。入力は文字列（ ユーザー メッセージと見なされます）か、OpenAI Responses API のアイテムのリストのいずれかです。

runner は次のループを実行します:

1. 現在のエージェント と現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェント と入力を更新して、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[ `MaxTurnsExceeded` ][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、かつツール呼び出しがないことです。

## ストリーミング

ストリーミング を使うと、LLM の実行中に ストリーミング イベントも受け取れます。ストリームが完了すると、[ `RunResultStreaming` ][agents.result.RunResultStreaming] に実行に関する完全な情報（新たに生成されたすべての出力を含む）が含まれます。ストリーミング イベントは `.stream_events()` を呼び出して受け取れます。詳しくは [ストリーミング ガイド](streaming.md) をご覧ください。

## 実行設定

`run_config` パラメーターでは、エージェント 実行のグローバル設定を構成できます:

- [ `model` ][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを設定できます。
- [ `model_provider` ][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
- [ `model_settings` ][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
- [ `input_guardrails` ][agents.run.RunConfig.input_guardrails], [ `output_guardrails` ][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力 ガードレール のリストです。
- [ `handoff_input_filter` ][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既に設定がない場合に適用されるグローバルな入力フィルターです。入力フィルターを使うと、新しいエージェント に送信される入力を編集できます。詳細は [ `Handoff.input_filter` ][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
- [ `tracing_disabled` ][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
- [ `trace_include_sensitive_data` ][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微情報がトレースに含まれるかどうかを設定します。
- [ `workflow_name` ][agents.run.RunConfig.workflow_name], [ `trace_id` ][agents.run.RunConfig.trace_id], [ `group_id` ][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID はオプションで、複数の実行にまたがるトレースを関連付けられます。
- [ `trace_metadata` ][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

## 会話/チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上のエージェント が実行され（つまり 1 回以上の LLM 呼び出しが発生し）ますが、チャット会話の 1 つの論理的なターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. Runner の実行: 最初のエージェント が LLM を呼び出し、ツールを実行し、2 つ目のエージェント に ハンドオフ、2 つ目のエージェント がさらにツールを実行し、その後に出力を生成。

エージェント の実行終了時に、ユーザー に何を見せるかを選べます。例えば、エージェント が生成したすべての新しいアイテムを見せるか、最終出力だけを見せるかです。いずれにせよ、その後に ユーザー がフォローアップの質問をするかもしれません。その場合は、再び run メソッドを呼び出せます。

### 手動の会話管理

次のターンの入力を取得するために、[ `RunResultBase.to_input_list()` ][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions.md) を使うと、手動で `.to_input_list()` を呼び出さずに会話履歴を自動処理できます:

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

Sessions は自動的に以下を行います:

- 各実行の前に会話履歴を取得
- 各実行の後に新しいメッセージを保存
- 異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions ドキュメント](sessions.md) をご覧ください。

## 長時間実行エージェントと Human-in-the-Loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了させるデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定の状況で例外を送出します。全一覧は [ `agents.exceptions` ][] にあります。概要は次のとおりです:

- [ `AgentsException` ][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他のすべての特定例外はここから派生します。
- [ `MaxTurnsExceeded` ][agents.exceptions.MaxTurnsExceeded]: エージェント の実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` 制限を超えた場合に送出されます。指定されたインタラクション回数内にタスクを完了できなかったことを示します。
- [ `ModelBehaviorError` ][agents.exceptions.ModelBehaviorError]: 基盤のモデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例えば次が含まれます:
    - 不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力で不正な JSON 構造を返す。
    - 想定外のツール関連の失敗: モデルが期待どおりの方法でツールを使用できない場合
- [ `UserError` ][agents.exceptions.UserError]: SDK を利用するあなた（コードの記述者）が、SDK の使用中にエラーを起こした場合に送出されます。これは通常、誤ったコード実装、無効な設定、または SDK の API の誤用が原因です。
- [ `InputGuardrailTripwireTriggered` ][agents.exceptions.InputGuardrailTripwireTriggered], [ `OutputGuardrailTripwireTriggered` ][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力 ガードレール または出力 ガードレール の条件が満たされたときに、それぞれ送出されます。入力 ガードレール は処理前に受信メッセージを確認し、出力 ガードレール は配信前にエージェント の最終応答を確認します。