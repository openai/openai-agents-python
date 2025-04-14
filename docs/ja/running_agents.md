# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを使って実行できます。3 つのオプションがあります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをリアルタイムでストリームします。

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

詳細は [results guide](results.md) をご覧ください。

## エージェントループ

`Runner` の run メソッドを使用する際、開始するエージェントと入力を渡します。入力は文字列（ユーザーメッセージと見なされます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

runner は次のようなループを実行します。

1. 現在のエージェントと入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。

!!! note

    LLM の出力が「final output」と見なされるルールは、希望する型のテキスト出力が生成され、ツール呼び出しがない場合です。

## ストリーミング

ストリーミングを利用すると、LLM の実行中にストリーミングイベントを受け取ることができます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行に関するすべての情報（新たに生成されたすべての出力を含む）が格納されます。ストリーミングイベントは `.stream_events()` で取得できます。詳細は [streaming guide](streaming.md) をご覧ください。

## Run config

`run_config` パラメーターでは、エージェント実行のグローバル設定をいくつか構成できます。

-   [`model`][agents.run.RunConfig.model]：各エージェントの `model` 設定に関わらず、グローバルで使用する LLM モデルを指定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]：モデル名を検索するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力または出力ガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：すべてのハンドオフに適用するグローバルな入力フィルターです（ハンドオフに既にフィルターがない場合）。入力フィルターを使うと、新しいエージェントに送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効にできます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに LLM やツール呼び出しの入出力など、機密性のあるデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]：実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は複数の実行にまたがるトレースをリンクするためのオプション項目です。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。

## 会話・チャットスレッド

いずれかの run メソッドを呼び出すと、1 つまたは複数のエージェント（および 1 つまたは複数の LLM 呼び出し）が実行されますが、チャット会話における 1 回の論理的なターンを表します。例：

1. ユーザーのターン：ユーザーがテキストを入力
2. Runner の実行：最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントにハンドオフ、2 番目のエージェントがさらにツールを実行し、出力を生成

エージェントの実行が終わったら、ユーザーに何を表示するか選択できます。たとえば、エージェントが生成したすべての新しいアイテムをユーザーに見せることも、最終出力だけを見せることもできます。いずれの場合も、ユーザーが追加の質問をした場合は、再度 run メソッドを呼び出せます。

次のターンの入力を取得するには、ベースの [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用できます。

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

SDK は特定のケースで例外を発生させます。全リストは [`agents.exceptions`][] にあります。概要は以下の通りです。

-   [`AgentsException`][agents.exceptions.AgentsException]：SDK で発生するすべての例外の基底クラスです。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：run メソッドに渡した `max_turns` を超えた場合に発生します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：モデルが不正な出力（例：不正な JSON や存在しないツールの使用）を生成した場合に発生します。
-   [`UserError`][agents.exceptions.UserError]：SDK を使用する際に、あなた（SDK を使ってコードを書く人）がエラーを起こした場合に発生します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：[ガードレール](guardrails.md) が作動した場合に発生します。