# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。3つのオプションがあります：

1. 非同期で実行し、[`RunResult`][agents.result.RunResult] を返す [`Runner.run()`][agents.run.Runner.run]。
2. 同期メソッドで、内部で `.run()` を実行する [`Runner.run_sync()`][agents.run.Runner.run_sync]。
3. 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返す [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。ストリーミングモードで LLM を呼び出し、受信したイベントをストリームします。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="You are a helpful assistant")

    result = await Runner.run(agent, "Write a haiku about recursion in programming.")
    print(result.final_output)
    # Code within the code,
    # Functions calling themselves,
    # Infinite loop's dance.
```

詳細は[結果ガイド](results.md)をご覧ください。

## エージェントループ

`Runner` の実行メソッドを使用する際、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージと見なされる）または OpenAI Responses API のアイテムリストのいずれかです。

ランナーは次のループを実行します：

1. 現在のエージェントと入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返すと、ループは終了し、結果を返します。
    2. LLM がハンドオフを行う場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成する場合、それらを実行し、結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。

!!! note

    LLM の出力が「最終出力」と見なされるかどうかのルールは、望ましいタイプのテキスト出力を生成し、ツール呼び出しがないことです。

## ストリーミング

ストリーミングを使用すると、LLM が実行される際にストリーミングイベントを受け取ることができます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] は実行に関する完全な情報を含み、生成されたすべての新しい出力を含みます。ストリーミングイベントには `.stream_events()` を呼び出せます。[ストリーミングガイド](streaming.md)で詳細を確認してください。

## 実行設定

`run_config` パラメーターを使用して、エージェント実行のグローバル設定を構成できます：

-   [`model`][agents.run.RunConfig.model]: 各エージェントの `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を検索するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力ガードレールのリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: すべてのハンドオフに適用するグローバル入力フィルター。ハンドオフに既にフィルターがない場合に適用されます。入力フィルターを使用して、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の[トレーシング](tracing.md)を無効にできます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに LLM やツール呼び出しの入出力など、潜在的に機密性のあるデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することをお勧めします。グループ ID は、複数の実行にわたってトレースをリンクするためのオプションフィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。

## 会話/チャットスレッド

いずれかの実行メソッドを呼び出すと、1つ以上のエージェントが実行される可能性があります（したがって、1つ以上の LLM 呼び出しが行われます）が、チャット会話の単一の論理ターンを表します。例えば：

1. ユーザーターン：ユーザーがテキストを入力
2. ランナー実行：最初のエージェントが LLM を呼び出し、ツールを実行し、2番目のエージェントにハンドオフし、2番目のエージェントがさらにツールを実行し、出力を生成します。

エージェント実行の終了時に、ユーザーに何を表示するかを選択できます。例えば、エージェントによって生成されたすべての新しいアイテムをユーザーに表示するか、最終出力のみを表示するかです。いずれにせよ、ユーザーがフォローアップの質問をする可能性があり、その場合は再度実行メソッドを呼び出すことができます。

次のターンの入力を取得するには、基本の [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用できます。

```python
async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

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

## 例外

SDK は特定のケースで例外を発生させます。完全なリストは [`agents.exceptions`][] にあります。概要は以下の通りです：

-   [`AgentsException`][agents.exceptions.AgentsException] は、SDK で発生するすべての例外の基本クラスです。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] は、実行が渡された `max_turns` を超えた場合に発生します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError] は、モデルが無効な出力を生成した場合に発生します。例：不正な形式の JSON や存在しないツールの使用。
-   [`UserError`][agents.exceptions.UserError] は、SDK を使用する際にエラーを犯した場合に発生します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] は、[ガードレール](guardrails.md)が作動した場合に発生します。