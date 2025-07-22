---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run] — 非同期で実行され、 [`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] — 同期メソッドで、内部で `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] — 非同期で実行され、 [`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリームします。  

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

`Runner` で `run` メソッドを使用すると、開始エージェントと入力を渡します。入力は文字列 (ユーザー メッセージと見なされます) か、OpenAI Responses API のアイテム リストのいずれかです。

`Runner` は次のループを実行します:

1. 現在のエージェントに対して、現在の入力を用いて LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。  
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。  
    3. LLM がツール呼び出しを生成した場合、それらを実行し結果を追加してから、ループを再実行します。  
3. 渡された `max_turns` を超えた場合、 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。  

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、ツール呼び出しが 1 つもないことです。

## ストリーミング

ストリーミングを利用すると、LLM 実行中のイベントを逐次受け取れます。ストリーム終了後、 [`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報 (生成されたすべての新しい出力を含む) が格納されます。ストリーミングイベントは `.stream_events()` で取得できます。詳しくは [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターを使って、エージェント実行のグローバル設定を行えます:

- [`model`][agents.run.RunConfig.model]: 各エージェントの `model` 設定に関わらず、グローバルで使用する LLM モデルを指定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名の検索に用いるモデルプロバイダー。デフォルトは OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例: グローバルで `temperature` や `top_p` を設定。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力または出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに入力フィルターが設定されていない場合に適用されるグローバル入力フィルター。新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体での [トレーシング](tracing.md) を無効にします。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微情報をトレースに含めるかどうかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング用にワークフロー名、トレース ID、グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は複数の実行にわたるトレースをリンクする際に使用できます。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話 / チャットスレッド

いずれかの `run` メソッドを呼び出すと、1 つ以上のエージェント (つまり 1 回以上の LLM 呼び出し) が実行されますが、チャット会話の論理的には 1 ターンとして扱われます。例:

1. ユーザーのターン: ユーザーがテキストを入力  
2. `Runner` の実行: 最初のエージェントが LLM を呼び出しツールを実行、次に 2 番目のエージェントへハンドオフしさらにツールを実行、最後に出力を生成  

エージェント実行の終了時に、ユーザーへ何を表示するかを選択できます。エージェントが生成したすべての新規アイテムを表示することも、最終出力だけを表示することも可能です。いずれにせよ、ユーザーがフォローアップの質問をした場合は再度 `run` メソッドを呼び出せます。

### 手動での会話管理

次のターンの入力を取得するために、 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して会話履歴を手動で管理できます:

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

より簡単な方法として、 [Sessions](sessions.md) を使えば `.to_input_list()` を手動で呼び出さずに会話履歴を自動で扱えます:

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

- 各実行前に会話履歴を取得  
- 各実行後に新しいメッセージを保存  
- 異なるセッション ID ごとに会話を分離して管理  

詳細は [Sessions ドキュメント](sessions.md) を参照してください。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

- [`AgentsException`][agents.exceptions.AgentsException] — SDK で送出されるすべての例外の基底クラス。  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] — 実行が `max_turns` を超えたときに送出。  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError] — モデルが不正な出力 (例: JSON の不備や存在しないツールの使用) を返したときに送出。  
- [`UserError`][agents.exceptions.UserError] — SDK を使用する開発者側の誤りがあったときに送出。  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] — [ガードレール](guardrails.md) がトリガーされたときに送出。