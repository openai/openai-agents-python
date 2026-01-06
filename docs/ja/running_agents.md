---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します।
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

詳しくは [結果ガイド](results.md) をご覧ください。

## エージェントループ

`Runner` の run メソッドでは、開始エージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）を指定できます。

runner は次のループを実行します。

1. 現在のエージェントと現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、目的の型のテキスト出力を生成し、ツール呼び出しがないことです。

## ストリーミング

ストリーミング を使用すると、LLM の実行中に ストリーミング イベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に、その実行で生成されたすべての新規出力を含む完全な情報が格納されます。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) をご覧ください。

## 実行設定 (Run config)

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダー。デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力 ガードレール のリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既にフィルターがない場合に適用するグローバルな入力フィルター。入力フィルターは、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントをご覧ください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、runner は次のエージェントを呼び出す前に、これまでのやり取りを 1 つの assistant メッセージに折りたたみます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロック内に配置し、以降の ハンドオフ ごとに新しいターンを追記します。生の transcript をそのまま渡したい場合は `False` にするか、独自の ハンドオフ フィルターを指定してください。すべての [`Runner` methods](agents.run.Runner) は、未指定時に自動的に `RunConfig` を作成するため、クイックスタートや code examples はこのデフォルトを自動的に使用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個々の ハンドオフ は [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` が `True` のときに正規化された transcript（履歴 + ハンドオフ アイテム）を受け取り、次のエージェントに渡す入力アイテムのリストをそのまま返す任意の呼び出し可能オブジェクト。完全な ハンドオフ フィルターを書かずに、組み込みの要約を差し替えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行に跨るトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。

デフォルトでは、SDK はあるエージェントが別のエージェントに ハンドオフ する際、以前のターンを 1 つの assistant 要約メッセージ内にネストします。これにより assistant メッセージの重複が減り、全文 transcript が 1 つのブロック内に収まり、新しいエージェントが高速にスキャンできます。レガシー動作に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりに転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定の ハンドオフ でのみオプトアウト（またはイン）するには、`handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタム マッパーを書かずに生成される要約に使われるラッパー文言を変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話 / チャットスレッド

任意の run メソッドの呼び出しは、1 つ以上のエージェントの実行（つまり 1 回以上の LLM 呼び出し）になる場合がありますが、チャット会話における 1 つの論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントに ハンドオフ、2 番目のエージェントがさらにツールを実行し、その後に出力を生成。

エージェントの実行終了時に、ユーザーへ何を見せるかを選べます。たとえば、エージェントが生成したすべての新しいアイテムを見せる、または最終出力のみを見せる、などです。どちらの場合でも、ユーザーが追質問をするかもしれません。その場合は再度 run メソッドを呼び出します。

### 手動による会話管理

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

より簡単な方法として、[Sessions](sessions/index.md) を使うと、`.to_input_list()` を手動で呼び出さずに会話履歴を自動で処理できます。

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

詳細は [Sessions のドキュメント](sessions/index.md) をご覧ください。


### サーバー管理の会話

ローカルで `to_input_list()` や `Sessions` を使って管理する代わりに、OpenAI の conversation state 機能により サーバー 側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送信せずに会話履歴を保持できます。詳しくは [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) をご覧ください。

OpenAI はターン間で状態を追跡するための 2 つの方法を提供しています。

#### 1. `conversation_id` を使用

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

## 長時間実行エージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使って、human-in-the-loop タスクを含む永続的で長時間実行のワークフローを動かせます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) をご覧ください。ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。

## 例外

SDK は特定のケースで例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。他の特定例外はすべてこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡された `max_turns` 制限を超えたときに送出されます。指定された対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。例:
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力の JSON 構造が不正なとき。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できないとき。
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する（コードを書く）あなたがエラーを起こした場合に送出されます。これは通常、不適切なコード実装、無効な設定、SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力 ガードレール または出力 ガードレール の条件が満たされた場合に送出されます。入力 ガードレール は処理前に受信メッセージを確認し、出力 ガードレール はエージェントの最終応答を配信前に確認します。