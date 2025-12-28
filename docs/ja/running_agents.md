---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラス経由で エージェント を実行できます。方法は 3 つあります。

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的に `.run()` を実行します。
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

`Runner` の run メソッドを使うとき、開始 エージェント と入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（ OpenAI Responses API のアイテム）を渡せます。

ランナーは次のループを実行します。

1. 現在の エージェント と現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し結果を返します。
    2. LLM が ハンドオフ を行った場合、現在の エージェント と入力を更新し、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力 (final output)」と見なされる条件は、望ましい型のテキスト出力を生成し、ツール呼び出しが存在しないことです。

## ストリーミング

ストリーミング を使うと、LLM の実行中に ストリーミング イベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に実行の完全な情報（生成されたすべての新規出力を含む）が格納されます。ストリーミング イベントは `.stream_events()` で取得できます。詳細は [ストリーミング ガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターで、エージェント実行のグローバル設定を構成できます。

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` に関係なく、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するモデルプロバイダーで、既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既にフィルターがない場合に適用されるグローバル入力フィルターです。入力フィルターを使うと、新しい エージェント に送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、次の エージェント を呼び出す前に、それまでのやり取りを 1 つのアシスタント メッセージに折りたたみます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロック内に配置し、後続の ハンドオフ が発生するたびに新しいターンを追記します。生の (raw) 文字起こしをそのまま渡したい場合は `False` にするか、カスタムの ハンドオフ フィルターを指定してください。いずれの [`Runner` のメソッド](agents.run.Runner) でも、未指定時は自動的に `RunConfig` を作成するため、クイックスタートや code examples はこの既定を自動で利用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックがある場合は引き続きそれが優先されます。個々の ハンドオフ は、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: 省略可能な呼び出し可能オブジェクトで、`nest_handoff_history` が `True` のときに正規化された文字起こし（履歴 + ハンドオフ アイテム）を受け取ります。次の エージェント に転送する入力アイテムのリストをそのまま返す必要があり、完全な ハンドオフ フィルターを書かずに組み込み要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、機微なデータをトレースに含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがってトレースを関連付けできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

デフォルトでは、ある エージェント が別の エージェント に ハンドオフ する際、SDK はそれまでのターンを単一のアシスタント要約メッセージ内にネストします。これにより、繰り返しのアシスタント メッセージが減り、新しい エージェント が迅速にスキャンできる単一ブロック内に完全な文字起こしが維持されます。従来の動作に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要どおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定の ハンドオフ でオプトアウト（またはオプトイン）することもでき、その場合は `handoff(..., nest_handoff_history=False)` または `True` を設定します。カスタム マッパーを書かずに生成される要約に使うラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（既定に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上の エージェント（したがって 1 回以上の LLM 呼び出し）が動作する可能性がありますが、チャット会話の 1 回の論理ターンを表します。例:

1. ユーザー のターン: ユーザー がテキストを入力
2. ランナーの実行: 最初の エージェント が LLM を呼び出し、ツールを実行し、2 番目の エージェント に ハンドオフ、2 番目の エージェント がさらにツールを実行し、最終的に出力を生成

エージェントの実行終了時に、ユーザー に何を表示するかを選べます。たとえば、エージェント によって生成されたすべての新規アイテムを表示する、または最終出力のみを表示するといった形です。いずれにせよ、ユーザー がフォローアップの質問をするかもしれません。その場合は、再度 run メソッドを呼び出せます。

### 手動の会話管理

次のターンの入力を得るために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って会話履歴を手動で管理できます。

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

より簡単に行うには、[Sessions](sessions/index.md) を使って、`.to_input_list()` を手動で呼び出さずに会話履歴を自動処理できます。

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
-   異なるセッション ID ごとに個別の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、 OpenAI の conversation state 機能にサーバー側で会話状態を管理させることもできます。これにより、過去のメッセージをすべて手動で再送しなくても会話履歴を保存できます。詳細は [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供します。

#### 1. `conversation_id` を使用

最初に OpenAI Conversations API で会話を作成し、その ID を以後のすべての呼び出しで再利用します。

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

別の選択肢は **レスポンスのチェーン**（response chaining）で、各ターンが前のターンのレスポンス ID に明示的にリンクします。

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

## 長時間実行エージェントと人間参加 (human-in-the-loop)

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、人間参加 (human-in-the-loop) を含む永続的な長時間実行ワークフローを動かせます。長時間実行タスクを完了するために Temporal と Agents SDK が連携して動くデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)です。

## 例外

SDK は特定の状況で例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定例外はこの型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` メソッドに渡した `max_turns` 制限を超えたときに送出されます。指定されたインタラクション ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（ LLM ）が予期しない、または無効な出力を生成した場合に発生します。以下を含みます。
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力で不正な JSON 構造を返したとき。
    -   予期しないツール関連の失敗: モデルが期待どおりの方法でツールを使用できなかったとき
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（この SDK を使ってコードを書く人）が誤った使い方をした場合に送出されます。多くは不適切なコード実装、無効な設定、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ入力 ガードレール または出力 ガードレール の条件が満たされたときに送出されます。入力 ガードレール は処理前に受信メッセージを検査し、出力 ガードレール は配信前にエージェントの最終応答を検査します。