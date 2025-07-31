---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run] — 非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] — 同期メソッドで、内部的には `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] — 非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミング モードで呼び出し、受信したイベントを逐次ストリームします。

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

`Runner` の run メソッドを使用するときは、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージと見なされます）または入力アイテムのリストで、これは OpenAI Responses API のアイテムです。

その後、Runner は以下のループを実行します:

1. 現在のエージェントと入力を使って LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。  
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。  
    3. LLM がツール呼び出しを生成した場合、それらを実行し結果を追加してループを再実行します。  
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note
    「最終出力」と見なされる条件は、所定の型でテキストを出力し、かつツール呼び出しが存在しない場合です。

## ストリーミング

ストリーミングを使用すると、LLM 実行中にストリーミング イベントを受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報（生成されたすべての新しい出力を含む）が格納されます。ストリーミング イベントは `.stream_events()` で取得できます。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を行えます:

- [`model`][agents.run.RunConfig.model]: 各エージェントの `model` 設定に関わらず、グローバルに使用する LLM モデルを指定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を検索するモデル プロバイダー。既定は OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例: グローバル `temperature` や `top_p` を設定。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力／出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既にフィルターがない場合に適用されるグローバル入力フィルター。新しいエージェントへ送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化します。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微情報をトレースに含めるかどうかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: トレーシングのワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は複数実行にまたがるトレースを関連付ける際に使用できます。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話／チャットスレッド

いずれの run メソッドを呼び出しても、1 回で 1 つ以上のエージェント（＝複数の LLM 呼び出し）が実行されますが、チャット会話上は単一の論理ターンとして扱われます。例:

1. ユーザー ターン: ユーザーがテキストを入力  
2. Runner 実行: 最初のエージェントが LLM を呼び出しツールを実行、次に別のエージェントへハンドオフし、さらにツールを実行して最終出力を生成  

エージェント実行の最後に、ユーザーへ何を表示するか選択できます。エージェントが生成したすべての新しいアイテムを表示しても、最終出力だけを表示しても構いません。いずれの場合も、ユーザーがフォローアップ質問をしたら再度 run メソッドを呼び出します。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用して次のターンの入力を取得し、会話履歴を手動で管理できます:

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

### Sessions を使用した自動会話管理

より簡単な方法として、[Sessions](sessions.md) を使用すれば `.to_input_list()` を呼び出さずに会話履歴を自動管理できます:

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
- 各実行後に新規メッセージを保存  
- 異なる session ID ごとに個別の会話を保持  

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行エージェント & ヒューマンインザループ

Agents SDK の [Temporal](https://temporal.io/) 連携を使用すると、ヒューマンインザループ タスクを含む耐久性のある長時間実行ワークフローを作成できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [こちらの動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK では特定の状況で例外が送出されます。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです:

- [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` で指定した `max_turns` を超えた場合に送出されます。  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル (LLM) が想定外または無効な出力を生成した場合に発生します。  
    - 不正な JSON: ツール呼び出しや `output_type` が指定されている場合の直接出力で、構造が壊れている JSON を返した場合。  
    - 予期しないツール関連の失敗: モデルがツールを期待どおりに使用できなかった場合。  
- [`UserError`][agents.exceptions.UserError]: SDK の誤用や無効な設定など、利用者の実装ミスによって発生します。  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力ガードレールまたは出力ガードレールの条件に合致した場合に送出されます。入力ガードレールは処理前のメッセージを、出力ガードレールはエージェントの最終応答をチェックします。