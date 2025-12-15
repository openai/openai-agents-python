---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラス経由で実行できます。次の 3 つの方法があります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。 LLM を ストリーミング モードで呼び出し、受信したイベントを順次ストリームします。

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

`Runner` の run メソッドを使用する際、開始するエージェントと入力を渡します。入力は文字列（ユーザーメッセージとして扱われます）または入力アイテムのリスト（ OpenAI Responses API のアイテム）を指定できます。

ランナーは次のループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新して、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行し、結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM 出力が「最終出力」と見なされる条件は、目的の型のテキスト出力を生成し、かつツール呼び出しがないことです。

## ストリーミング

ストリーミングにより、 LLM の実行中にストリーミングイベントを追加で受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] は、その実行で生成された新しい出力を含む、実行に関する完全な情報を保持します。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関わらず、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を検索するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力の ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに既に入力フィルターがない場合に適用されるグローバル入力フィルターです。新しいエージェントに送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、次のエージェントを呼び出す前に、ランナーは直前のトランスクリプトを 1 つの assistant メッセージに折りたたみます。ヘルパーはコンテンツを `<CONVERSATION HISTORY>` ブロック内に配置し、以降のハンドオフが発生するたびに新しいターンを追加します。生のトランスクリプトをそのまま渡したい場合は、これを `False` に設定するか、カスタムのハンドオフフィルターを提供してください。いずれの [`Runner` メソッド](agents.run.Runner) も、未指定の場合に自動で `RunConfig` を作成するため、クイックスタートや code examples はこのデフォルトを自動的に利用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個々のハンドオフは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を使ってこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` が `True` のときに正規化されたトランスクリプト（履歴 + ハンドオフアイテム）を受け取る任意のコール可能です。次のエージェントに転送する入力アイテムのリストを正確に返す必要があり、完全なハンドオフフィルターを書かずに組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入力/出力など、潜在的に機微なデータをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は、複数の実行にまたがるトレースをリンクできる任意のフィールドです。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

デフォルトでは、 SDK は、あるエージェントが別のエージェントへハンドオフする際に、それ以前のターンを 1 つの assistant 要約メッセージ内にネストするようになりました。これにより、 assistant メッセージの重複を減らし、新しいエージェントがすばやくスキャンできる 1 つのブロック内に完全なトランスクリプトを保持します。従来の動作に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定のハンドオフに対してオプトアウト（またはオプトイン）するには、`handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタムマッパーを書かずに生成される要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

任意の run メソッドの呼び出しは、1 つ以上のエージェントの実行（つまり 1 回以上の LLM 呼び出し）になる可能性がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. ランナーの実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行してから出力を生成

エージェントの実行が終わったら、ユーザーに何を表示するかを選べます。たとえば、エージェントが生成したすべての新規アイテムを表示する、または最終出力のみを表示するといった方法です。いずれにせよ、ユーザーが追質問をする可能性があり、その場合は再度 run メソッドを呼び出せます。

### 手動の会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、会話履歴を手動で管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出さなくても会話履歴を自動で扱えます。

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

Sessions は自動で以下を行います。

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに管理する代わりに、 OpenAI の conversation state 機能にサーバー側で会話状態を管理させることもできます。これにより、過去のメッセージをすべて手動で再送信せずに会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI は、ターン間の状態を追跡する 2 つの方法を提供します。

#### 1. `conversation_id` を使用

まず OpenAI Conversations API を使って会話を作成し、その ID を以降のすべての呼び出しで再利用します。

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

もう 1 つの選択肢は、各ターンが前のターンのレスポンス ID に明示的にリンクする **response chaining** です。

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

## 長時間実行エージェントと人間の介在

Agents SDK の [Temporal](https://temporal.io/) 連携を使用すると、 human-in-the-loop のタスクを含む、耐久性のある長時間実行のワークフローを実行できます。 Temporal と Agents SDK が連携して長時間実行タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)を参照し、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)をご覧ください。

## 例外

SDK は特定のケースで例外を送出します。全リストは [`agents.exceptions`][] にあります。概要:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定例外はすべてこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡された `max_turns` 制限を超えた場合に送出されます。指定されたインタラクションターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（ LLM ）が予期しない、または無効な出力を生成した場合に発生します。これには次が含まれます。
    -   JSON の不正形式: 特定の `output_type` が定義されている場合に、ツール呼び出し用や直接出力で不正な JSON 構造を返すケース。
    -   予期しないツール関連の失敗: モデルが期待どおりの方法でツールを使用できない場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（この SDK を用いてコードを書く人）がエラーを犯した場合に送出されます。通常は、コードの誤った実装、無効な構成、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力 ガードレール または出力 ガードレール の条件が満たされたときに送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール はエージェントの最終応答を配信前にチェックします。