---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。選択肢は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run] — 非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] — 同期メソッドで、内部的には `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] — 非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントを逐次ストリーミングします。

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

`Runner` の run メソッドを使用する際には、開始エージェントと入力を渡します。入力は文字列 (ユーザー メッセージと見なされます) か、OpenAI Responses API のアイテム リスト (input items) のいずれかです。

ランナーは次のループを実行します:

1. 現在のエージェントと入力を用いて LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. LLM が `final_output` を返した場合、ループを終了し、結果を返します。  
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。  
    3. LLM がツール呼び出しを生成した場合、それらのツールを実行し、結果を追加し、ループを再実行します。  
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型を持つテキスト出力を生成し、かつツール呼び出しが存在しない場合です。

## ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントを追加で受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行に関する完全な情報 (生成されたすべての新しい出力を含む) が格納されます。ストリーミングイベントは `.stream_events()` で取得できます。詳しくは [ストリーミング ガイド](streaming.md) をご覧ください。

## Run 設定

`run_config` パラメーターを使用すると、エージェント実行のグローバル設定を構成できます:

- [`model`][agents.run.RunConfig.model]: 各エージェントの `model` 設定に関係なく、使用するグローバルな LLM モデルを設定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダー。デフォルトは OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力または出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに入力フィルターが設定されていない場合に適用するグローバル入力フィルター。新しいエージェントへ送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化します。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は複数の実行にまたがるトレースを関連付けるための任意項目です。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話 / チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェント (ひいては 1 つ以上の LLM 呼び出し) が実行されますが、チャット会話における 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner 実行: 1 つ目のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフします。2 つ目のエージェントがさらにツールを実行し、その後出力を生成します。  

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。たとえば、エージェントが生成したすべての新しいアイテムを表示するか、最終出力のみを表示するかです。いずれの場合も、ユーザーが続けて質問したら、再度 run メソッドを呼び出せばよいです。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って、次のターンの入力を取得し、会話履歴を手動で管理できます:

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

### Sessions を用いた自動会話管理

より簡単な方法として、[Sessions](sessions.md) を利用して `.to_input_list()` を手動で呼び出さずに会話履歴を自動管理できます:

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

Sessions は次の処理を自動で行います:

- 各実行前に会話履歴を取得  
- 各実行後に新しいメッセージを保存  
- 異なるセッション ID ごとに個別の会話を維持  

詳細は [Sessions ドキュメント](sessions.md) を参照してください。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです:

- [`AgentsException`][agents.exceptions.AgentsException]: SDK で送出されるすべての例外の基底クラス。  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 実行が run メソッドに渡した `max_turns` を超えた場合に送出されます。  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: モデルが不正な出力 (例: 不正な JSON や存在しないツールの使用) を生成した場合に送出されます。  
- [`UserError`][agents.exceptions.UserError]: SDK の使用方法を誤った場合に (SDK を利用する開発者向けに) 送出されます。  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: [ガードレール](guardrails.md) が発動した場合に送出されます。