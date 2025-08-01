---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。方法は次の 3 つです。

1. [`Runner.run()`][agents.run.Runner.run]  
   非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]  
   同期メソッドで、内部的には `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]  
   非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミング モードで LLM を呼び出し、受信イベントを逐次ストリームします。

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

詳細は [results ガイド](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドを使う際、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージと見なされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）です。

Runner は次のループを実行します。

1. 現在のエージェントと入力で LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. `final_output` が返された場合、ループを終了し結果を返します。  
    2. ハンドオフが行われた場合、現在のエージェントと入力を更新してループを再実行します。  
    3. ツール呼び出しが生成された場合、それらを実行し結果を追加してループを再実行します。  
3. 渡した `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    出力が「final output」と見なされる条件は、望ましい型でのテキスト出力があり、ツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを利用すると、LLM 実行中にストリーミング イベントを受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報（生成されたすべての新しい出力を含む）が格納されます。`.stream_events()` を呼び出してイベントを取得できます。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

## Run config

`run_config` パラメーターは、エージェント実行のグローバル設定を構成します。

- [`model`][agents.run.RunConfig.model]: 各エージェントの `model` 設定に関係なく、グローバルで使用する LLM モデルを指定します。  
- [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデル プロバイダー。デフォルトは OpenAI です。  
- [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例として、グローバルな `temperature` や `top_p` を設定できます。  
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力／出力ガードレールのリスト。  
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 既に設定されていない場合にすべてのハンドオフへ適用されるグローバル入力フィルター。新しいエージェントへ渡される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。  
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体で [トレーシング](tracing.md) を無効化します。  
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに LLM やツール呼び出しの入出力など、機微情報を含めるかを設定します。  
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: トレーシング用のワークフロー名、トレース ID、グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は複数実行に跨るトレースを関連付ける任意フィールドです。  
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話／チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上のエージェント（ひいては 1 つ以上の LLM 呼び出し）が実行されますが、チャット会話における 1 つの論理的ターンを表します。例:

1. ユーザー ターン: ユーザーがテキストを入力  
2. Runner 実行:  
   - 第 1 エージェントが LLM を呼び出し、ツールを実行  
   - 第 2 エージェントへハンドオフ  
   - 第 2 エージェントがさらにツールを実行し、出力を生成  

エージェント実行の最後にユーザーへ何を表示するかは自由です。すべての新しいアイテムを表示しても、最終出力だけを表示しても構いません。ユーザーがフォローアップ質問をしたら、再度 run メソッドを呼び出します。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドで、次のターン用の入力を取得し会話履歴を手動管理できます。

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

より簡単な方法として、[Sessions](sessions.md) を使用して `.to_input_list()` を呼び出すことなく会話履歴を自動処理できます。

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

Sessions は自動で以下を行います。

- 各実行前に会話履歴を取得  
- 各実行後に新しいメッセージを保存  
- セッション ID ごとに個別の会話を維持  

詳細は [Sessions ドキュメント](sessions.md) を参照してください。

## 長時間実行エージェントと Human-in-the-Loop

Agents SDK は [Temporal](https://temporal.io/) との連携により、Human-in-the-Loop を含む耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で視聴でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)にあります。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

- [`AgentsException`][agents.exceptions.AgentsException]  
  SDK 内で送出されるすべての例外の基底クラスです。  
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]  
  `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` のいずれかで `max_turns` 制限を超えた場合に送出されます。指定されたターン数内でタスクを完了できなかったことを示します。  
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]  
  基盤モデル (LLM) が予期しない、または無効な出力を生成した場合に発生します。例:  
    - 不正な JSON: ツール呼び出しや `output_type` が指定されている場合の直接出力で JSON が壊れている。  
    - ツールに関する予期しない失敗。  
- [`UserError`][agents.exceptions.UserError]  
  SDK を使用する際の実装ミス、無効な設定、API の誤用など、ユーザー (コード作成者) 側のエラー時に送出されます。  
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]  
  それぞれ入力ガードレール、出力ガードレールの条件を満たした場合に送出されます。入力ガードレールは処理前のメッセージを、出力ガードレールはエージェントの最終応答をチェックします。