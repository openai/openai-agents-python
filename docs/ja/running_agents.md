---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]  
   非同期 ( async ) で実行され、[`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]  
   同期メソッドで、内部的には `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]  
   非同期 ( async ) で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミング モードで LLM を呼び出し、受信したイベントをそのままストリームします。

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

`Runner` の run メソッドを使用する際、開始エージェントと入力を渡します。入力は文字列 (ユーザー メッセージと見なされます) か、OpenAI Responses API のアイテムのリストのいずれかです。

 Runner  は次のループを実行します:

1. 現在のエージェントに対して現在の入力で LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。  
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。  
    3. LLM がツール呼び出しを生成した場合、それらを実行して結果を追加し、ループを再実行します。  
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、望ましい型のテキストを生成し、かつツール呼び出しがないことです。

## ストリーミング

ストリーミングを使用すると、 LLM 実行中にイベントを逐次受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報 (生成されたすべての新しい出力を含む) が格納されます。`.stream_events()` を呼び出してストリーミング イベントを取得できます。詳細は [ストリーミング ガイド](streaming.md) をご覧ください。

## 実行設定

`run_config` パラメーターでは、エージェント実行の一部グローバル設定を行えます:

- [`model`][agents.run.RunConfig.model]: 各エージェントの `model` 設定に関わらず、グローバルに使用する LLM モデルを指定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデル プロバイダーを設定します。既定は OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有設定を上書きします。例として、グローバルな `temperature` や `top_p` を設定できます。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力 / 出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既存のフィルターがない場合に適用されるグローバル入力フィルター。新しいエージェントへ送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体で [トレーシング](tracing.md) を無効にします。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: トレースのワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は複数実行間でトレースを紐付ける際に使用できます。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに付与するメタデータ。  

## 会話 / チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェント (つまり 1 つ以上の LLM 呼び出し) が実行されますが、チャット会話における 1 つの論理ターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力  
2. Runner の実行: 第 1 エージェントが LLM を呼び出し、ツールを実行し、第 2 エージェントへハンドオフ。第 2 エージェントがさらにツールを実行し、出力を生成。  

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。例として、エージェントが生成したすべての新しいアイテムを表示する、または最終出力だけを表示する、といった方法があります。いずれにせよ、ユーザーがフォローアップ質問をすれば、再度 run メソッドを呼び出せます。

### 手動の会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して次のターンの入力を取得し、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions.md) を使用して `.to_input_list()` を手動で呼び出すことなく会話履歴を自動管理できます。

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

Sessions は自動的に以下を行います:

- 実行前に会話履歴を取得  
- 実行後に新しいメッセージを保存  
- 異なる session ID ごとに個別の会話を維持  

詳細は [Sessions ドキュメント](sessions.md) を参照してください。

## 長時間実行エージェントと Human-in-the-loop

Agents SDK は [Temporal](https://temporal.io/) との統合により、 Human-in-the-loop タスクを含む耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [こちらの動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要を示します:

- [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。ほかの特定例外はすべてこれを継承します。  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` の `max_turns` 制限を超えた際に送出されます。指定されたターン数内にタスクを完了できなかったことを示します。  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル ( LLM ) が予期しない、または無効な出力を生成した際に発生します。例:  
    - 不正な JSON: ツール呼び出しや `output_type` が定義された場合の直出力で、不正な JSON 構造を返した。  
    - 予期しないツール関連の失敗: モデルが想定どおりにツールを使用できなかった。  
- [`UserError`][agents.exceptions.UserError]: SDK を使用する際の実装ミス、無効な設定、API の誤用など、ユーザーが原因のエラーで送出されます。  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件を満たした場合に送出されます。入力ガードレールは処理前のメッセージを、出力ガードレールはエージェントの最終応答をチェックします。