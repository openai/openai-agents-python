---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。選択肢は 3 つあります。

1. 非同期で実行し、[`RunResult`][agents.result.RunResult] を返す [`Runner.run()`][agents.run.Runner.run]  
2. 同期メソッドで、内部的に `.run()` を実行する [`Runner.run_sync()`][agents.run.Runner.run_sync]  
3. 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返す [`Runner.run_streamed()`][agents.run.Runner.run_streamed]  
   LLM をストリーミング モードで呼び出し、受信したイベントを逐次配信します。

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

詳細は [結果ガイド](results.md) をご覧ください。

## エージェントループ

` Runner ` の run メソッドを使用する際は、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージと見なされます）または OpenAI Responses API の項目リストのいずれかです。

Runner は次のループを実行します。

1. 現在のエージェントに対して LLM を呼び出し、現在の入力を渡します。  
2. LLM が出力を生成します。  
    1. `final_output` が返された場合、ループを終了し結果を返します。  
    2. ハンドオフが発生した場合、現在のエージェントと入力を更新し、ループを再実行します。  
    3. ツール呼び出しが生成された場合、それらを実行し結果を追加してループを再実行します。  
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、望ましい型のテキスト出力を生成し、かつツール呼び出しが存在しない場合です。

## ストリーミング

ストリーミングを利用すると、LLM 実行中のイベントを逐次受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報（生成されたすべての新規出力を含む）が格納されます。`.stream_events()` を呼び出してストリーミング イベントを取得できます。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

## Run 設定

` run_config ` パラメーターでエージェント実行のグローバル設定を行えます。

- [`model`][agents.run.RunConfig.model]: 各エージェントの `model` 設定に関わらず、使用する LLM モデルをグローバルに指定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するプロバイダー。既定では OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例として `temperature` や `top_p` をグローバルに設定可能です。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力／出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 既に handoff にフィルターが指定されていない場合に適用されるグローバル入力フィルター。新しいエージェントへ送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にします。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微情報をトレースに含めるかどうかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング workflow 名、trace ID、trace group ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は複数実行にまたがるトレースを関連付けるための任意フィールドです。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話／チャットスレッド

いずれかの run メソッドを呼び出すと、1 回または複数回のエージェント実行（つまり複数回の LLM 呼び出し）が発生しますが、チャット会話における 1 つの論理ターンを表します。例:

1. ユーザー ターン: ユーザーがテキストを入力  
2. Runner 実行: 第 1 エージェントが LLM を呼び出しツールを実行、次に第 2 エージェントへハンドオフし追加のツールを実行、最終出力を生成  

エージェント実行後、ユーザーにどの項目を表示するか選択できます。たとえば、エージェントが生成したすべての新規項目を表示することも、最終出力のみを表示することもできます。いずれの場合でも、ユーザーがフォローアップ質問をしたら再度 run メソッドを呼び出せます。

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

より簡単な方法として、[Sessions](sessions.md) を使用すると `.to_input_list()` を手動で呼ばずに会話履歴を自動管理できます。

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

Sessions は次を自動で行います。

- 各実行前に会話履歴を取得  
- 各実行後に新規メッセージを保存  
- 異なるセッション ID ごとに個別の会話を維持  

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

- [`AgentsException`][agents.exceptions.AgentsException] : SDK で送出されるすべての例外の基底クラス  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] : 実行が run メソッドに渡した `max_turns` を超えた場合に送出  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError] : モデルが無効な出力（例: 不正な JSON や存在しないツールの使用）を生成した場合に送出  
- [`UserError`][agents.exceptions.UserError] : SDK を使用するコードの記述者が誤った使い方をした場合に送出  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] : [ガードレール](guardrails.md) がトリップした際に送出