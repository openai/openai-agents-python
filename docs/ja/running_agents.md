---
search:
  exclude: true
---
# エージェントの実行

`Runner` クラスを使って エージェント を実行できます。方法は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]  
   非同期で実行され、`RunResult` を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]  
   同期メソッドで、内部的には `.run()` を呼び出します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]  
   非同期で実行され、`RunResultStreaming` を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのまま ストリーミング します。

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

`Runner` の `run` メソッドを使用するときは、開始 エージェント と入力を渡します。入力は文字列（ユーザーメッセージと見なされます）か、OpenAI Responses API のアイテムのリストのいずれかです。

ランナーは次のループを実行します:

1. 現在の エージェント と入力で LLM を呼び出します。
2. LLM が出力を生成します。  
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。  
    2. LLM が ハンドオフ を行った場合、現在の エージェント と入力を更新し、ループを再実行します。  
    3. LLM が ツール呼び出し を生成した場合、それらを実行し結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力が「ファイナル出力」と見なされるルールは、求められる型のテキストを生成し、ツール呼び出しが存在しないことです。

## ストリーミング

ストリーミング を使うと、LLM 実行中にストリーミングイベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行の完全な情報（生成されたすべての新しい出力を含む）が格納されます。`.stream_events()` を呼び出してイベントを取得できます。詳しくは [ストリーミングガイド](streaming.md) をご覧ください。

## Run config

`run_config` パラメーターでは、エージェント実行のグローバル設定を行えます:

-   [`model`][agents.run.RunConfig.model]: 各 エージェント の `model` 設定に関係なく、グローバルで使用する LLM モデルを指定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決する モデルプロバイダー で、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルで `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力／出力 ガードレール のリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に入力フィルターが指定されていない場合に適用されるグローバル入力フィルター。新しい エージェント に送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効にします。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。`group_id` は複数の実行にわたるトレースをリンクするための任意フィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。

## 会話／チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上の エージェント が実行され（つまり 1 回以上の LLM 呼び出しが発生し）、チャット会話上の 1 つの論理ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner 実行: 最初の エージェント が LLM を呼び出し、ツールを実行し、別の エージェント にハンドオフ。2 番目の エージェント がさらにツールを実行し、最終出力を生成。

エージェント実行の終了時に、ユーザーへ何を表示するかを選択できます。たとえば、エージェント が生成したすべての新しいアイテムを表示するか、最終出力のみを表示するかです。どちらの場合も、ユーザーがフォローアップ質問を行えば、再度 run メソッドを呼び出せます。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、次のターンの入力を取得し、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions.md) を使用すると `.to_input_list()` を手動で呼び出さずに会話履歴を自動管理できます:

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

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   異なる session ID ごとに個別の会話を維持

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行エージェント & 人間介在

Agents SDK は [Temporal](https://temporal.io/) と統合して、耐久性のある長時間実行ワークフロー（人間介在タスクを含む）を実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [こちらの動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラス。その他の特定例外はすべてこれを継承します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` のいずれかで、`max_turns` を超えた場合に送出されます。指定ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。例:  
    -   JSON 形式が不正: ツール呼び出しや直接出力で JSON が壊れている場合（特に `output_type` が指定されているとき）。  
    -   予期しないツール関連エラー: モデルがツールを想定どおりに使用しなかった場合。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する際に、あなた（SDK を利用する開発者）が誤った実装や不正な設定を行った場合に送出されます。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力ガードレール、出力ガードレールの条件を満たしたときに送出されます。入力ガードレールは処理前のメッセージを、出力ガードレールは最終応答をチェックします。