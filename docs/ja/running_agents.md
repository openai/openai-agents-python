---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラス経由で実行できます。オプションは 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run] — 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync] — 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed] — 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで  LLM  を呼び出し、受信したイベントをそのままストリーミングします。

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

詳しくは [結果ガイド](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドを使うと、開始するエージェントと入力を渡します。入力は文字列（ユーザーメッセージとして扱われます）か、OpenAI  Responses  API  のアイテムである入力アイテムのリストのいずれかです。

Runner は次のようにループを実行します:

1. 現在の入力を用いて、現在のエージェントに対して  LLM  を呼び出します。
2.  LLM  が出力を生成します。
    1.  LLM  が `final_output` を返した場合、ループを終了し、実行結果を返します。
    2.  LLM  がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3.  LLM  がツール呼び出しを生成した場合、それらを実行し、その実行結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    ある  LLM  出力が「最終出力」と見なされる条件は、所望の型のテキスト出力を生成し、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを使うと、  LLM  の実行中にストリーミングイベントも受け取れます。ストリーム完了時には、[`RunResultStreaming`][agents.result.RunResultStreaming] に、生成されたすべての新規出力を含む実行の完全な情報が含まれます。ストリーミングイベントは `.stream_events()` を呼び出すと取得できます。詳しくは [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェントの実行に関するグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各エージェントの `model` に関わらず、使用するグローバルな  LLM  モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは  OpenAI  です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に適用する入力または出力のガードレールのリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既定の入力フィルターがない場合に、すべてのハンドオフへ適用するグローバルな入力フィルターです。入力フィルターにより、新しいエージェントへ送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体に対して [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに、  LLM  やツール呼び出しの入出力など、潜在的に機密なデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は、複数の実行にまたがってトレースを関連付けるための任意のフィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。

## 会話 / チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行されることがあります（つまり、1 回以上の  LLM  呼び出しが発生します）。ただし、これはチャット会話における 1 回の論理的なターンを表します。たとえば:

1. ユーザーのターン: ユーザーがテキストを入力します。
2. Runner の実行: 最初のエージェントが  LLM  を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行し、最終的に出力を生成します。

エージェントの実行の最後に、ユーザーへ何を表示するかを選べます。たとえば、エージェントが生成した新規アイテムをすべて表示することも、最終出力のみを表示することもできます。いずれにせよ、その後ユーザーが追質問をするかもしれません。その場合は、再度 run メソッドを呼び出します。

### 手動での会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って次のターンの入力を取得し、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions.md) を使えば、`.to_input_list()` を手動で呼び出さずに会話履歴を自動で扱えます:

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

Sessions は自動で次を行います:

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   異なるセッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions.md) を参照してください。

## 長時間実行のエージェントと人間の介在 (human-in-the-loop)

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む、堅牢で長時間実行のワークフローを実行できます。長時間実行タスクを完了するために Temporal と Agents SDK が連携して動作するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定の状況で例外を送出します。全一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他の特定の例外の親となる汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` の各メソッドに渡した `max_turns` 制限を、エージェントの実行が超えたときに送出されます。指定されたやり取り回数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成したときに発生します。例:
    -   不正な JSON: モデルが、ツール呼び出し用、または直接出力に対して不正な  JSON  構造を返した場合（とくに特定の `output_type` が定義されているとき）。
    -   予期しないツール関連の失敗: モデルが想定どおりにツールを使用できなかった場合。
-   [`UserError`][agents.exceptions.UserError]: SDK を利用するあなた（SDK を用いてコードを書く人）が誤った使い方をしたときに送出されます。誤った実装、無効な設定、SDK の API の誤用が主な原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力ガードレールまたは出力ガードレールの条件に合致したときに送出されます。入力ガードレールは処理前に受信メッセージを検査し、出力ガードレールは配信前にエージェントの最終応答を検査します。