---
search:
  exclude: true
---
# エージェントの実行

エージェントは `Runner` クラスを通じて実行できます。選択肢は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run] — 非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。  
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] — 同期メソッドで、内部的には `.run()` を呼び出します。  
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] — 非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリームで提供します。

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

`Runner` の run メソッドでは、開始エージェントと入力を渡します。入力は文字列 (ユーザー メッセージと見なされます) か、OpenAI Responses API のアイテムのリストのいずれかです。

runner は次のループを実行します:

1. 現在のエージェントに対して LLM を呼び出し、現在の入力を渡します。  
2. LLM が出力を生成します。  
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。  
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。  
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追加してループを再実行します。  
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力が「最終出力」と見なされる条件は、望ましい型でテキスト出力を生成し、かつツール呼び出しが 1 つも含まれていないことです。

## ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行の全情報 (生成されたすべての新しい出力を含む) が格納されます。ストリーミングイベントを受け取るには `.stream_events()` を呼び出してください。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

## run_config の設定

`run_config` パラメーターを使用すると、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、実行全体で使用する LLM モデルを設定します。  
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダー。デフォルトは OpenAI です。  
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。  
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力 / 出力ガードレールのリスト。  
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに特定の input_filter が未設定の場合に適用されるグローバル入力フィルター。新しいエージェントに渡す入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] を参照してください。  
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化します。  
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。  
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: トレーシング用のワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は複数の実行にまたがるトレースを関連付ける任意項目です。  
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。  

## 会話 / チャットスレッド

どの run メソッドを呼び出しても、1 つ以上のエージェント (したがって 1 回以上の LLM 呼び出し) が実行される可能性がありますが、チャット会話における論理上は 1 ターンを表します。例:

1. ユーザーターン: ユーザーがテキストを入力  
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフ。2 番目のエージェントがさらにツールを実行し、最終出力を生成。  

エージェントの実行が終了したら、ユーザーに表示する内容を選択できます。たとえば、エージェントが生成したすべての新しいアイテムを表示することも、最終出力だけを表示することも可能です。いずれの場合も、ユーザーがフォローアップ質問をしたら再度 run メソッドを呼び出してください。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、次ターンの入力を取得し、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions.md) を使用すると、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動で扱えます:

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

Sessions は次を自動で行います:

-   各実行前に会話履歴を取得  
-   各実行後に新しいメッセージを保存  
-   異なる session ID ごとに個別の会話を維持  

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行エージェントと Human-in-the-Loop

Agents SDK の [Temporal](https://temporal.io/) インテグレーションを使用すると、Human-in-the-Loop タスクを含む耐久性のある長時間実行ワークフローを構築できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) をご覧ください。また、[こちらのドキュメント](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) も参照してください。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。すべての特定例外はこのクラスを継承します。  
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドで `max_turns` 制限を超えた場合に送出されます。ターン数内でエージェントがタスクを完了できなかったことを示します。  
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル (LLM) が予期しない、または無効な出力を生成した場合に発生します。例:  
    -   不正な JSON 形式: ツール呼び出し、または特定の `output_type` が定義されている場合の直接出力で、モデルが不正な JSON 構造を返したとき  
    -   予期しないツール関連の失敗: モデルがツールを期待どおりに使用しなかったとき  
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた (コードを書く人) がエラーを起こした場合に送出されます。誤ったコード実装、無効な設定、SDK の API の誤用などが原因です。  
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力ガードレールまたは出力ガードレールの条件が満たされた場合に送出されます。入力ガードレールは処理前の受信メッセージを、出力ガードレールはエージェントの最終応答をチェックします。