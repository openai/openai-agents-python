---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。次の 3 つの選択肢があります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントを逐次 ストリーミング します。

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

`Runner` の run メソッドを使うとき、開始するエージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

Runner は次のループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新して、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追記し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、所望の型のテキスト出力を生成し、ツール呼び出しが存在しないことです。

## ストリーミング

ストリーミング を使用すると、LLM の実行中に ストリーミング イベントも受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に、その実行で生成された新しい出力を含む完全な情報が含まれます。ストリーミング イベントは `.stream_events()` を呼び出してください。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関わらず、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力の ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既にフィルターがない場合に適用されるグローバルな入力フィルターです。入力フィルターにより、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、Runner は次のエージェントを呼び出す前に、それまでのトランスクリプトを 1 つの assistant メッセージに折りたたみます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロックに配置し、以後のハンドオフ発生時に新しいターンを追加していきます。生のトランスクリプトをそのまま渡したい場合は、これを `False` にするか、カスタムの handoff フィルターを指定してください。すべての [`Runner` メソッド](agents.run.Runner) は、未指定時に自動で `RunConfig` を作成するため、クイックスタートや code examples はこのデフォルトを自動的に利用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個々のハンドオフは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によってこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` が `True` のときに正規化済みのトランスクリプト（履歴 + handoff アイテム）を受け取る任意の呼び出し可能オブジェクトです。次のエージェントへ転送する入力アイテムのリストを正確に返す必要があり、完全な handoff フィルターを書かずに組み込み要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体で [トレーシング](tracing.md) を無効化します。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行のエクスポーター、プロセッサー、または トレーシング メタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行の トレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は任意で、複数の実行に跨るトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時、各ターン前に新しいユーザー入力をセッション履歴へマージする方法をカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出し直前に、完全に準備されたモデル入力（instructions と入力アイテム）を編集するフックです。例: 履歴のトリミングや system prompt の挿入。

デフォルトでは、SDK はあるエージェントが別のエージェントへ ハンドオフ するたびに、それまでのターンを 1 つの assistant の要約メッセージ内にネストします。これにより、assistant メッセージの重複が減り、完全なトランスクリプトが 1 つのブロックに収まり、新しいエージェントがすばやく参照できます。従来の動作に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりに転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定のハンドオフごとに、`handoff(..., nest_handoff_history=False)` または `True` を設定してオプトアウト（またはオプトイン）できます。カスタム マッパーを書かずに生成される要約で使われるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（デフォルトへ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

いずれの run メソッドでも、1 回の呼び出しで 1 つ以上のエージェント（すなわち 1 回以上の LLM 呼び出し）が実行されますが、これはチャット会話の 1 つの論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへ ハンドオフ、2 つ目のエージェントがさらにツールを実行し、最終的な出力を生成

エージェントの実行が終わったら、ユーザーに何を見せるかを選べます。たとえば、エージェントが生成したすべての新しいアイテムを見せる、または最終出力だけを見せる、といったことが可能です。いずれの場合でも、ユーザーが追質問をするかもしれません。その場合は再度 run メソッドを呼び出してください。

### 手動での会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使えば、`.to_input_list()` を手動で呼び出さずに会話履歴を自動管理できます。

```python
from agents import Agent, Runner, SQLiteSession

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create session instance
    session = SQLiteSession("conversation_123")

    thread_id = "thread_123"  # Example thread ID
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

Sessions は自動的に次を行います。

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   セッション ID ごとに独立した会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに管理する代わりに、OpenAI の conversation state 機能により、サーバー側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送せずに会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供します。

#### 1. `conversation_id` を使用

最初に OpenAI Conversations API で会話を作成し、その ID を以降のすべての呼び出しで再利用します。

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id

    while True:
        user_input = input("You: ")
        result = await Runner.run(agent, user_input, conversation_id=conv_id)
        print(f"Assistant: {result.final_output}")
```

#### 2. `previous_response_id` を使用

もう 1 つの方法は、各ターンが前のターンの response ID に明示的にリンクする **response chaining** です。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    previous_response_id = None

    while True:
        user_input = input("You: ")

        # Setting auto_previous_response_id=True enables response chaining automatically
        # for the first turn, even when there's no actual previous response ID yet.
        result = await Runner.run(
            agent,
            user_input,
            previous_response_id=previous_response_id,
            auto_previous_response_id=True,
        )
        previous_response_id = result.last_response_id
        print(f"Assistant: {result.final_output}")
```

## call_model_input_filter

モデル呼び出し直前にモデル入力を編集するには `call_model_input_filter` を使用します。フックは現在のエージェント、コンテキスト、結合済みの入力アイテム（セッション履歴があればそれも含む）を受け取り、新しい `ModelInputData` を返します。

```python
from agents import Agent, Runner, RunConfig
from agents.run import CallModelData, ModelInputData

def drop_old_messages(data: CallModelData[None]) -> ModelInputData:
    # Keep only the last 5 items and preserve existing instructions.
    trimmed = data.model_data.input[-5:]
    return ModelInputData(input=trimmed, instructions=data.model_data.instructions)

agent = Agent(name="Assistant", instructions="Answer concisely.")
result = Runner.run_sync(
    agent,
    "Explain quines",
    run_config=RunConfig(call_model_input_filter=drop_old_messages),
)
```

機微情報のマスキング、長い履歴のトリミング、追加のシステムガイダンスの挿入などのために、`run_config` で実行ごとに設定するか、`Runner` のデフォルトとして設定してください。

## 長時間実行のエージェント & human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop を含む永続的で長時間実行のワークフローを動かせます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を参照し、ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) を参照してください。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他の特定の例外はすべてこれを継承します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` に渡した `max_turns` の上限を超えた場合に送出されます。指定された対話ターン数内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには以下が含まれます。
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接の出力で不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが想定どおりにツールを使用できない場合。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（SDK を使ってコードを書く人）が誤りを犯した場合に送出されます。これは通常、コードの実装ミス、無効な設定、SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力ガードレールまたは出力ガードレールの条件に合致した場合に送出されます。入力ガードレールは処理前に受信メッセージを検査し、出力ガードレールはエージェントの最終応答を配送前に検査します。