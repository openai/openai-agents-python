---
search:
  exclude: true
---
# エージェントの実行

エージェントは `Runner` クラスを通じて実行できます。方法は 3 通りあります:

1. `Runner.run()` — 非同期で実行し、 `RunResult` を返します。  
2. `Runner.run_sync()` — 同期メソッドで、内部的には `.run()` を呼び出します。  
3. `Runner.run_streamed()` — 非同期で実行し、 `RunResultStreaming` を返します。 LLM をストリーミングモードで呼び出し、そのイベントを受信次第ストリームします。

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

`Runner` の run メソッドでは、開始エージェントと入力を渡します。入力は文字列 (ユーザー メッセージと見なされます) か、OpenAI Responses API のアイテムを並べたリストのいずれかです。

runner は次のループを実行します:

1. 現在のエージェントに対し、現在の入力を用いて LLM を呼び出します。  
2. LLM が出力を生成します。  
    1. `final_output` が返された場合、ループを終了し結果を返します。  
    2. ハンドオフが行われた場合、現在のエージェントと入力を更新し、ループを再実行します。  
    3. ツール呼び出しを生成した場合、それらを実行し結果を追加したうえでループを再実行します。  
3. 渡された `max_turns` を超えた場合、 `MaxTurnsExceeded` 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、所定の型でテキスト出力を生成し、かつツール呼び出しが存在しない場合です。

## ストリーミング

ストリーミングを使用すると、LLM 実行中のイベントを逐次受け取れます。ストリーム終了後、 `RunResultStreaming` には実行に関する完全な情報 (新たに生成されたすべての出力を含む) が格納されます。 ` .stream_events()` を呼び出してストリーミングイベントを取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## Run config

`run_config` パラメーターでは、エージェント実行に関するグローバル設定を行えます:

- `model` — 各エージェントの `model` 設定に関わらず、使用する LLM モデルをグローバルに指定します。  
- `model_provider` — モデル名を解決するモデルプロバイダーを指定します。既定は OpenAI です。  
- `model_settings` — エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。  
- `input_guardrails`, `output_guardrails` — すべての実行に適用する入力 / 出力ガードレールのリスト。  
- `handoff_input_filter` — ハンドオフ側で未設定の場合に適用されるグローバル入力フィルター。新しいエージェントに送る入力を編集できます。詳細は `Handoff.input_filter` のドキュメントをご覧ください。  
- `tracing_disabled` — 実行全体のトレーシングを無効化します。  
- `trace_include_sensitive_data` — LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。  
- `workflow_name`, `trace_id`, `group_id` — 実行のトレーシング用ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。 `group_id` は複数の実行にまたがるトレースを関連付ける任意フィールドです。  
- `trace_metadata` — すべてのトレースに含めるメタデータ。

## 会話 / チャットスレッド

任意の run メソッドの呼び出しは、1 つ以上のエージェント実行 (つまり 1 つ以上の LLM 呼び出し) を伴いますが、チャット会話における単一の論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner 実行: 第 1 エージェントが LLM を呼び出し、ツールを実行し、第 2 エージェントへハンドオフ。第 2 エージェントがさらにツールを実行し、出力を生成。  

エージェント実行の最後に、ユーザーへ何を表示するかを選択できます。たとえばエージェントが生成したすべての新規アイテムを見せるか、最終出力のみを見せるかです。いずれの場合も、ユーザーがフォローアップ質問を行ったら再度 run メソッドを呼び出します。

### 手動での会話管理

`RunResultBase.to_input_list()` メソッドで次ターン用の入力を取得し、会話履歴を手動管理できます:

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

より簡単な方法として、 [Sessions](sessions.md) を使用すれば `.to_input_list()` を手動で呼ばずに会話履歴を自動管理できます:

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
- 異なる session ID ごとに会話を分離して管理  

詳細は [Sessions ドキュメント](sessions.md) を参照してください。

## 長時間実行エージェント & Human-in-the-loop

Agents SDK は [Temporal](https://temporal.io/) との統合により、長時間実行ワークフローや human-in-the-loop タスクを耐障害性を持って実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

特定の状況で SDK は例外を送出します。完全な一覧は `agents.exceptions` にあります。概要は以下のとおりです:

- `AgentsException` — SDK 内で送出されるすべての例外の基底クラスで、他の具体的な例外の共通型として機能します。  
- `MaxTurnsExceeded` — `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` に渡した `max_turns` を超えたときに送出されます。指定ターン数内にタスクを完了できなかったことを示します。  
- `ModelBehaviorError` — 基盤となるモデル ( LLM ) が予期しない、または無効な出力を生成した場合に発生します。例:  
    - 不正な JSON: ツール呼び出しや直接出力で JSON 構造が壊れている場合 (特に `output_type` を指定している場合)。  
    - 予期しないツール関連の失敗: モデルがツールを想定どおりに使用できなかった場合。  
- `UserError` — SDK を使用する際の実装ミス、無効な設定、API の誤用など、ユーザー側のエラーで送出されます。  
- `InputGuardrailTripwireTriggered`, `OutputGuardrailTripwireTriggered` — 入力ガードレールまたは出力ガードレールの条件を満たした場合に送出されます。入力ガードレールは処理前のメッセージを、出力ガードレールはエージェントの最終応答を検査します。