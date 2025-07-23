---
search:
  exclude: true
---
# エージェントの実行

エージェントは `Runner` クラスを介して実行できます。方法は 3 つあります。

1. `Runner.run()` : 非同期で実行し、 `RunResult` を返します。  
2. `Runner.run_sync()` : 同期メソッドで、内部的には `.run()` を呼び出します。  
3. `Runner.run_streamed()` : 非同期で実行し、 `RunResultStreaming` を返します。LLM をストリーミングモードで呼び出し、受信したイベントを逐次ストリームします。

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

`Runner` の run メソッドを使う際には、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージと見なされます）か、OpenAI Responses API のアイテムのリストを指定できます。

ランナーは次のループを実行します。

1. 現在のエージェントと入力を用いて LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. `final_output` が返された場合、ループを終了し結果を返します。  
    2. ハンドオフが行われた場合、現在のエージェントと入力を更新してループを再実行します。  
    3. ツール呼び出しを含む場合は、それらを実行し結果を追加してループを再実行します。  
3. `max_turns` を超えた場合は `MaxTurnsExceeded` 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、望ましい型のテキスト出力であり、ツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを有効にすると、LLM の実行中にストリーミングイベントも受け取れます。ストリーム完了後、 `RunResultStreaming` には実行に関する完全な情報（新しく生成されたすべての出力を含む）が格納されます。`.stream_events()` を呼び出してストリーミングイベントを取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェント実行に対するグローバル設定を行えます。

- `model` : 各エージェントの `model` 設定に関わらず、グローバルに使用する LLM モデルを指定します。  
- `model_provider` : モデル名を解決するモデルプロバイダーで、デフォルトは OpenAI です。  
- `model_settings` : エージェント固有の設定を上書きします。例として、グローバルな `temperature` や `top_p` を設定できます。  
- `input_guardrails`, `output_guardrails` : すべての実行に適用する入力／出力ガードレールのリスト。  
- `handoff_input_filter` : 既にフィルターが設定されていないハンドオフに対し、すべてのハンドオフに適用するグローバル入力フィルター。詳細は `Handoff.input_filter` のドキュメントを参照してください。  
- `tracing_disabled` : 実行全体に対して トレーシング を無効化します。  
- `trace_include_sensitive_data` : トレースに LLM やツール呼び出しの入出力など、機密の可能性があるデータを含めるかどうかを設定します。  
- `workflow_name`, `trace_id`, `group_id` : 実行のトレーシングに使うワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。`group_id` は複数実行にわたるトレースを関連付けるための任意フィールドです。  
- `trace_metadata` : すべてのトレースに含めるメタデータ。  

## 会話／チャットスレッド

いずれの run メソッドを呼び出しても、1 回または複数のエージェント（＝複数の LLM 呼び出し）が実行されますが、チャット会話上は 1 つの論理的ターンとなります。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner 実行: 第 1 エージェントが LLM を呼び出しツールを実行し、第 2 エージェントへハンドオフ。第 2 エージェントがさらにツールを実行し、最終出力を生成。  

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。たとえばエージェントが生成したすべての新規アイテムを表示するか、最終出力のみを表示するかを選べます。いずれの場合も、ユーザーが追加入力を行ったら再び run メソッドを呼び出します。

### 手動での会話管理

[`RunResultBase.to_input_list()`] メソッドを使って次のターンの入力を取得し、会話履歴を手動で管理できます。

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

### Session を使った自動会話管理

より簡単な方法として、[Sessions](sessions.md) を利用すれば `.to_input_list()` を手動で呼び出さずに会話履歴を自動管理できます。

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

Session は自動で以下を行います。

- 各実行前に会話履歴を取得  
- 各実行後に新しいメッセージを保存  
- 異なる session ID ごとに個別の会話を維持  

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 例外

特定の状況で SDK は例外を送出します。完全な一覧は `agents.exceptions` にあります。概要は以下のとおりです。

- `AgentsException` : SDK が送出するすべての例外の基底クラス。  
- `MaxTurnsExceeded` : 実行が run メソッドに渡した `max_turns` を超えた場合に送出。  
- `ModelBehaviorError` : モデルが無効な出力（例: 不正な JSON や存在しないツールの使用）を生成した場合に送出。  
- `UserError` : SDK 利用時の実装ミスなど、ユーザー側のエラーで送出。  
- `InputGuardrailTripwireTriggered`, `OutputGuardrailTripwireTriggered` : ガードレール がトリップした際に送出。