---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。選択肢は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行され、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行され、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントをそのまま ストリーミング します。

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

詳しくは [実行結果ガイド](results.md) をご覧ください。

## エージェントループ

`Runner` の run メソッドを使うとき、開始エージェントと入力を渡します。入力は文字列（ ユーザー メッセージとみなされます）または入力アイテムのリスト（ OpenAI Responses API のアイテム）です。

ランナーは次のループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新して、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型のテキスト出力を生成し、かつ ツール呼び出し がないことです。

## ストリーミング

ストリーミング を使うと、LLM 実行中の ストリーミング イベントも受け取れます。ストリーム完了時には、[`RunResultStreaming`][agents.result.RunResultStreaming] に、生成されたすべての新規出力を含む実行の完全な情報が格納されます。ストリーミング イベントは `.stream_events()` を呼び出して受け取れます。詳しくは [ストリーミング ガイド](streaming.md) をご覧ください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既定のフィルターがない場合に、すべての ハンドオフ に適用されるグローバルな入力フィルターです。入力フィルターは、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、次のエージェントを呼び出す前に、ランナーはそれまでの発話を単一の assistant メッセージにまとめます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロック内に配置し、以後の ハンドオフ が発生するたびに新しいターンが追記されます。生の transcript をそのまま渡したい場合は、これを `False` にするか、カスタムの ハンドオフ フィルターを指定してください。すべての [`Runner` methods](agents.run.Runner) は、未指定時に自動で `RunConfig` を作成するため、クイックスタートや code examples はこのデフォルトを自動で利用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個別の ハンドオフ は、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によってこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: 省略可能な callable。`nest_handoff_history` が `True` のとき、正規化された transcript（履歴 + ハンドオフ アイテム）を受け取り、次のエージェントに転送する入力アイテムのリストを正確に返す必要があります。これにより、完全な ハンドオフ フィルターを書かずに組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM や ツール呼び出し の入出力など、機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行に渡るトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

デフォルトでは、SDK はあるエージェントが別のエージェントへ ハンドオフ する際に、それまでのターンを単一の assistant 要約メッセージ内に入れ子にします。これにより assistant メッセージの重複を減らし、全文 transcript を新しいエージェントがすばやく走査できる単一ブロックに保ちます。以前の挙動に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要どおりに転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定の ハンドオフ に対して個別にオプトアウト（またはイン）するには、`handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタム マッパーを書かずに生成された要約で使われるラッパー文言を変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（デフォルトへ戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

任意の run メソッドの呼び出しは、1 つ以上のエージェントの実行（つまり 1 回以上の LLM 呼び出し）になる可能性がありますが、チャット会話における単一の論理的ターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. ランナーの実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへ ハンドオフ、2 つ目のエージェントがさらにツールを実行し、最終的な出力を生成。

エージェント実行の最後に、 ユーザー に何を見せるかを選べます。たとえば、エージェントが生成したすべての新規アイテムを表示するか、最終出力のみを表示します。いずれの場合でも、その後に ユーザー がフォローアップの質問をするかもしれないため、その場合は再度 run メソッドを呼び出せます。

### 手動での会話管理

次のターンの入力を取得するには、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動管理できます。

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

より簡単な方法として、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出さずに会話履歴を自動で処理できます。

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

Sessions は自動で次を行います。

-   各実行の前に会話履歴を取得
-   各実行の後に新しいメッセージを保存
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) をご覧ください。


### サーバー管理の会話

ローカルで `to_input_list()` や `Sessions` を使って管理する代わりに、 サーバー 側で OpenAI の会話状態機能に状態管理を任せることもできます。これにより、過去のメッセージをすべて再送せずに会話履歴を保持できます。詳しくは [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供します。

#### 1. `conversation_id` を使う

まず OpenAI Conversations API で会話を作成し、その ID を以後のすべての呼び出しで再利用します。

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

#### 2. `previous_response_id` を使う

もう 1 つの方法は、各ターンが前のターンのレスポンス ID に明示的にリンクする **response chaining** です。

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

## ロングランニング エージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む永続的なロングランニング ワークフローを実行できます。Temporal と Agents SDK が連携してロングランニング タスクを完了するデモは、[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) を参照し、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) をご覧ください。

## 例外

SDK は特定の場合に例外を送出します。全一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で発生するすべての例外の基底クラスです。ほかの特定の例外はすべてこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` の上限を超えた場合に送出されます。指定されたインタラクション ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（ LLM ）が予期しない／不正な出力を生成した場合に発生します。これには以下が含まれます。
    -   不正な JSON: 特定の `output_type` が定義されている場合に特に、ツール呼び出しや直接の出力として不正な JSON 構造を返す。
    -   予期しないツール関連の失敗: モデルが期待される方法でツールを使用できない。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（コードを書く）あなたが、SDK の使用中にエラーを起こした場合に送出されます。これは通常、不正なコード実装、無効な設定、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力 ガードレール または出力 ガードレール の条件が満たされたときにそれぞれ送出されます。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール はエージェントの最終レスポンスを配信前にチェックします。