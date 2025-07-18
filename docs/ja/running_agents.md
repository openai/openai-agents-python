---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run] — 非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] — 同期メソッドで、内部的には `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] — 非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントを逐次ストリームします。  

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="You are a helpful assistant")

    result = await Runner.run(agent, "Write a haiku about recursion in programming.")
    print(result.final_output)
    # Code within the code,
    # Functions calling themselves,
    # Infinite loop's dance.
```

詳細は [結果ガイド](results.md) をご覧ください。

## エージェントループ

`Runner` の run メソッドを使用するときは、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージと見なされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

Runner は次のループを実行します:

1. 現在のエージェントと入力を使って LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. `final_output` を返した場合はループを終了し、結果を返します。  
    2. ハンドオフがある場合は現在のエージェントと入力を更新し、ループを再実行します。  
    3. ツール呼び出しがある場合はツールを実行し、その結果を追加してループを再実行します。  
3. 渡された `max_turns` を超えた場合は [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。  

!!! note

    LLM の出力が「最終出力」と見なされる条件は、求められた型のテキスト出力があり、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを利用すると、LLM 実行中にストリーミングイベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行の完全な情報（新しい出力を含む）が格納されます。`.stream_events()` を呼び出してストリーミングイベントを取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェントの実行に関するグローバル設定を構成できます:

- [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、グローバルで使用する LLM モデルを指定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダー。デフォルトは OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえばグローバル `temperature` や `top_p` を設定できます。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力／出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既定のフィルターがない場合に適用されるグローバル入力フィルター。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体で [トレーシング](tracing.md) を無効にします。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに LLM やツール呼び出しの入出力など機微なデータを含めるかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: トレーシング用のワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。`group_id` は複数の実行間でトレースを関連付けるための任意フィールドです。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話／チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェント（＝1 つ以上の LLM 呼び出し）が実行されますが、論理的にはチャット会話の 1 ターンとして扱われます。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner 実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフ。2 つ目のエージェントがさらにツールを実行し、最終的な出力を生成。  

エージェントの実行終了後、ユーザーに何を表示するかは自由です。エージェントが生成したすべての新しいアイテムを見せてもよいですし、最終出力だけを見せてもかまいません。いずれにせよ、ユーザーが続けて質問した場合は再度 run メソッドを呼び出します。

### 手動での会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して会話履歴を手動で管理できます:

```python
async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

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

より簡単な方法として、[Sessions](sessions.md) を使用すれば `.to_input_list()` を手動で呼び出さなくても会話履歴を自動で扱えます:

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

Sessions は自動で以下を行います:

- 各実行前に会話履歴を取得  
- 各実行後に新しいメッセージを保存  
- 異なる session ID ごとに個別の会話を維持  

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

- [`AgentsException`][agents.exceptions.AgentsException] — SDK が送出するすべての例外の基底クラス。  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] — 実行が `max_turns` を超えたときに送出。  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError] — 不正な出力（例: 不正な JSON、存在しないツールの使用など）をモデルが生成したときに送出。  
- [`UserError`][agents.exceptions.UserError] — SDK を使用するコードで開発者が誤った使い方をしたときに送出。  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] — [ガードレール](guardrails.md) が作動したときに送出。