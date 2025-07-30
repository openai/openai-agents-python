---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]  
   非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]  
   同期メソッドで、内部的には `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]  
   非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントを順次ストリームします。

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

詳細は [results guide](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドでは、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージと見なされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

Runner は次のループを実行します。

1. 現在のエージェントと入力で LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. `final_output` を返した場合、ループを終了し結果を返します。  
    2. ハンドオフが行われた場合、現在のエージェントと入力を更新してループを再実行します。  
    3. ツール呼び出しがある場合、それらを実行し結果を追加してループを再実行します。  
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「final output」と見なされるルールは、希望する型のテキスト出力を生成し、ツール呼び出しが存在しない場合です。

## ストリーミング

ストリーミングを使用すると、LLM 実行中にストリーミングイベントを受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報（生成されたすべての新しい出力を含む）が格納されます。`.stream_events()` を呼び出してストリーミングイベントを取得できます。詳細は [streaming guide](streaming.md) を参照してください。

## Run config

`run_config` パラメーターでは、エージェント実行のグローバル設定を行えます。

- [`model`][agents.run.RunConfig.model]: 各エージェントの `model` 設定に関わらず、使用するグローバル LLM モデルを指定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダー。デフォルトは OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例として、グローバルな `temperature` や `top_p` を設定できます。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力・出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既にフィルターがない場合に適用されるグローバル入力フィルター。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [tracing](tracing.md) を無効化します。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微情報をトレースに含めるかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: トレース用のワークフロー名、トレース ID、グループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は複数実行間でトレースを関連付ける際に使用できます。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話 / チャットスレッド

いずれの run メソッドを呼び出しても、1 回以上のエージェント実行（つまり 1 回以上の LLM 呼び出し）が発生する可能性がありますが、チャット会話における 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner 実行:  
   - 最初のエージェントが LLM を呼び出し、ツールを実行し、第 2 のエージェントへハンドオフ  
   - 第 2 のエージェントがさらにツールを実行し、出力を生成  

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。エージェントが生成したすべての新規アイテムを見せることも、最終出力のみを見せることも可能です。どちらの場合でも、ユーザーが追質問をした場合は再度 run メソッドを呼び出します。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、次ターンの入力を取得し、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions.md) を利用すると `.to_input_list()` を呼び出すことなく会話履歴を自動管理できます。

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

Sessions は自動的に以下を行います。

- 各実行前に会話履歴を取得  
- 各実行後に新しいメッセージを保存  
- 異なる session ID ごとに個別の会話を維持  

詳細は [Sessions ドキュメント](sessions.md) を参照してください。

## 例外

特定の状況で SDK は例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです。

- [`AgentsException`][agents.exceptions.AgentsException]  
  SDK で送出されるすべての例外の基底クラス。  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]  
  実行が `max_turns` を超えた場合に送出されます。  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]  
  モデルが無効な出力（例: 不正な JSON、存在しないツールの使用）を生成した場合に送出されます。  
- [`UserError`][agents.exceptions.UserError]  
  SDK を使用する開発者が誤った使い方をした場合に送出されます。  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]  
  [ガードレール](guardrails.md) がトリップした際に送出されます。