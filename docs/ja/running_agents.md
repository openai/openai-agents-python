# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを通じて実行できます。以下の 3 つの方法があります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的には `.run()` を呼び出します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントを順次ストリームします。

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

詳細は [results ガイド](results.md) を参照してください。

## エージェントの実行ループ

`Runner` の run メソッドを使用する際、開始エージェントと入力を渡します。入力は文字列（ユーザーメッセージとして扱われる）または OpenAI Responses API の項目リストのいずれかです。

Runner は以下のループを実行します。

1. 現在のエージェントと入力を用いて LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、再度ループを実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追加して再度ループを実行します。
3. 指定された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。

!!! note

    LLM の出力が「最終出力（final output）」とみなされる条件は、望ましいタイプのテキスト出力を生成し、かつツール呼び出しがない場合です。

## ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントを受け取ることができます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行に関する完全な情報（生成されたすべての新しい出力を含む）が格納されます。ストリーミングイベントを取得するには `.stream_events()` を呼び出します。詳細は [streaming ガイド](streaming.md) を参照してください。

## 実行設定（Run config）

`run_config` パラメータを使用すると、エージェント実行時のグローバル設定を構成できます。

- [`model`][agents.run.RunConfig.model]：各エージェントの `model` 設定に関係なく、グローバルな LLM モデルを指定します。
- [`model_provider`][agents.run.RunConfig.model_provider]：モデル名を検索するためのモデルプロバイダーを指定します（デフォルトは OpenAI）。
- [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
- [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に適用する入力または出力のガードレールのリストです。
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフに既存の入力フィルターがない場合に適用されるグローバルな入力フィルターです。入力フィルターを使用すると、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の [トレーシング](tracing.md) を無効にします。
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：トレースに LLM やツール呼び出しの入出力など、機密性の高いデータを含めるかどうかを設定します。
- [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：トレーシングのワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID はオプションで、複数の実行間でトレースを関連付けることができます。
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。

## 会話／チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行され（したがって 1 つ以上の LLM 呼び出しが発生）、チャット会話における単一の論理的ターンを表します。例えば：

1. ユーザーのターン：ユーザーがテキストを入力
2. Runner の実行：最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントにハンドオフし、2 番目のエージェントがさらにツールを実行して出力を生成

エージェントの実行終了時に、ユーザーに何を表示するかを選択できます。例えば、エージェントが生成したすべての新しい項目を表示するか、最終出力のみを表示するかを選択できます。いずれの場合も、ユーザーが追加の質問をした場合、再度 run メソッドを呼び出します。

次のターンの入力を取得するには、基本クラスの [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用できます。

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

SDK は特定の状況で例外を発生させます。完全なリストは [`agents.exceptions`][] にあります。概要は以下の通りです。

- [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で発生するすべての例外の基底クラスです。
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：実行が指定された `max_turns` を超えた場合に発生します。
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：モデルが不正な出力（不正な JSON や存在しないツールの使用など）を生成した場合に発生します。
- [`UserError`][agents.exceptions.UserError]：SDK を使用するコードに誤りがある場合に発生します。
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：[ガードレール](guardrails.md) がトリガーされた場合に発生します。