---
search:
  exclude: true
---
# エージェントの実行

エージェントは `Runner` クラス経由で実行できます。方法は 3 つあります。

1. `Runner.run()` : 非同期で実行され、 `RunResult` を返します。  
2. `Runner.run_sync()` : 同期メソッドで、内部的には `.run()` を呼び出します。  
3. `Runner.run_streamed()` : 非同期で実行され、 `RunResultStreaming` を返します。LLM をストリーミング モードで呼び出し、受信したイベントをそのままストリーム配信します。

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

 Runner の run メソッドを使用すると、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージとみなされます）または OpenAI Responses API のアイテム一覧のどちらかです。

Runner は次のループを実行します。

1. 現在のエージェントと入力で LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. `final_output` を返した場合、ループを終了し結果を返します。  
    2. ハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。  
    3. ツール呼び出しを生成した場合、それらのツールを実行し結果を追加して、ループを再実行します。  
3. 渡された `max_turns` を超えた場合、 `MaxTurnsExceeded` 例外を発生させます。

!!! note

    出力が「最終出力」と見なされる条件は、必要な型のテキスト出力があり、ツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを使用すると、LLM 実行中のストリーミングイベントを受け取れます。ストリーム完了後、 `RunResultStreaming` には実行に関する完全な情報（生成されたすべての新しい出力を含む）が格納されます。 `.stream_events()` を呼び出してストリーミングイベントを取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## Run 設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます。

- `model` : 各エージェントの `model` 設定に関わらず、グローバルで使用する LLM モデルを指定します。  
- `model_provider` : モデル名を解決するプロバイダー。デフォルトは OpenAI です。  
- `model_settings` : エージェント固有の設定を上書きします。たとえばグローバルな `temperature` や `top_p` を設定できます。  
- `input_guardrails`, `output_guardrails` : すべての実行に適用する入力／出力ガードレールのリスト。  
- `handoff_input_filter` : ハンドオフに既にフィルターが設定されていない場合に適用するグローバル入力フィルター。新しいエージェントへ送る入力を編集できます。詳細は `Handoff.input_filter` を参照してください。  
- `tracing_disabled` : 実行全体での [トレーシング](tracing.md) を無効化します。  
- `trace_include_sensitive_data` : トレースに LLM やツール呼び出しの入出力など機微データを含めるかどうかを設定します。  
- `workflow_name`, `trace_id`, `group_id` : トレーシング用のワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。`group_id` は複数実行間でトレースを関連付ける任意フィールドです。  
- `trace_metadata` : すべてのトレースに含めるメタデータ。  

## 会話／チャットスレッド

いずれかの run メソッドを呼び出すと、一度の論理ターンで 1 つ以上のエージェント（＝ 複数の LLM 呼び出し）が実行される可能性があります。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner 実行: 最初のエージェントが LLM を呼び出しツールを実行、2 つ目のエージェントへハンドオフ、さらにツールを実行し最終出力を生成  

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。すべての新規アイテムを表示することも、最終出力のみを表示することも可能です。ユーザーがフォローアップ質問をした場合は、再度 run メソッドを呼び出します。

### 手動の会話管理

次のターンの入力を取得するには、 `RunResultBase.to_input_list()` メソッドで会話履歴を手動管理できます。

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

より簡単な方法として、 [Sessions](sessions.md) を使用すると `.to_input_list()` を手動で呼び出さずに会話履歴を自動管理できます。

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

Sessions は以下を自動で行います。

- 各実行前に会話履歴を取得  
- 各実行後に新規メッセージを保存  
- 異なるセッション ID ごとに会話を分離  

詳細は [Sessions ドキュメント](sessions.md) を参照してください。

## 長時間実行エージェントと人間の介在

Agents SDK は [Temporal](https://temporal.io/) 連携により、耐障害性のある長時間実行ワークフローや human-in-the-loop タスクを実現できます。Temporal と Agents SDK で長時間タスクを完了させるデモは [こちらの動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) をご覧ください。ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)。

## 例外

SDK は状況に応じて例外を発生させます。完全な一覧は `agents.exceptions` にあります。概要は以下のとおりです。

- `AgentsException` : SDK 内で発生するすべての例外の基底クラスです。  
- `MaxTurnsExceeded` : `Runner.run` / `Runner.run_sync` / `Runner.run_streamed` が `max_turns` を超えた場合に発生します。  
- `ModelBehaviorError` : 基盤モデル (LLM) が予期しないまたは無効な出力を生成したときに発生します。  
    - 不正な JSON 形式: ツール呼び出しや直接出力で JSON 構造が壊れている場合（`output_type` が指定されている場合を含む）。  
    - 予期しないツール関連の失敗: モデルがツールを想定どおりに使用できなかった場合。  
- `UserError` : SDK の使用方法を誤った場合に発生します。  
- `InputGuardrailTripwireTriggered`, `OutputGuardrailTripwireTriggered` : それぞれ入力ガードレールまたは出力ガードレールの条件が満たされた際に発生します。