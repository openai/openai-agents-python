---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。オプションは 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントを順次ストリーミングします。

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

`Runner` の run メソッドを使用する際、開始するエージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）か、OpenAI Responses API の入力アイテムのリストのいずれかです。

その後、runner は次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、所望の型のテキスト出力を生成し、かつツール呼び出しがないことです。

## ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミングイベントを受け取れます。ストリーム完了後、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報（新たに生成されたすべての出力を含む）が格納されます。ストリーミングイベントは `.stream_events()` を呼び出して受け取れます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定 (Run config)

`run_config` パラメーターにより、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関わらず、使用するグローバルな LLM モデルを設定します。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名の解決に使うモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力の ガードレール のリスト。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに適用するグローバルな入力フィルター（ハンドオフに既定のフィルターがない場合）。入力フィルターは、新しいエージェントに送信する入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、runner は次のエージェントを呼び出す前に、直前のやり取りを単一の assistant メッセージに折りたたみます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロックに配置し、以降のハンドオフで新しいターンを追記します。過去の raw トランスクリプトをそのまま渡したい場合は、`False` に設定するか、カスタムのハンドオフフィルターを指定してください。すべての [`Runner` methods](agents.run.Runner) は `RunConfig` を未指定時に自動生成するため、クイックスタートや code examples はこのデフォルトを自動的に利用し、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個々のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: 省略可能な呼び出し可能オブジェクトで、`nest_handoff_history` が `True` のときに正規化済みトランスクリプト（履歴 + ハンドオフ アイテム）を受け取り、次のエージェントに転送する入力アイテムのリストを正確に返す必要があります。これにより、完全なハンドオフフィルターを書くことなく、組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体に対して [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、トレースに潜在的に機微なデータを含めるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID はオプションで、複数の実行にわたってトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータ。

デフォルトでは、SDK は、エージェントが別のエージェントへハンドオフする際、以前のターンを単一の assistant 要約メッセージ内に入れ子にします。これにより assistant メッセージの重複を減らし、新しいエージェントがすばやくスキャンできるように、完全なトランスクリプトを 1 つのブロック内に保持します。従来の動作に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりに転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定のハンドオフについては、`handoff(..., nest_handoff_history=False)` または `True` を設定して個別にオプトアウト（またはオプトイン）できます。カスタム マッパーを書かずに生成される要約に使うラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（およびデフォルトへ戻す [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

## 会話/チャットスレッド

いずれかの run メソッドの呼び出しは、1 つ以上のエージェントの実行（すなわち 1 回以上の LLM 呼び出し）を伴う場合がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 つ目のエージェントへハンドオフ、2 つ目のエージェントがさらにツールを実行し、出力を生成

エージェントの実行が終わったら、ユーザーに何を表示するかを選べます。たとえば、エージェントが生成したすべての新しいアイテムをユーザーに見せる、または最終出力のみを表示する、などです。いずれにせよ、ユーザーが追質問をするかもしれないので、その場合は再度 run メソッドを呼び出せます。

### 手動での会話管理

次のターン用の入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions/index.md) を使用すれば、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に処理できます:

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

Sessions は自動で次を行います:

-   各実行前に会話履歴を取得
-   各実行後に新規メッセージを保存
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) を参照してください。


### サーバー管理の会話

OpenAI の conversation state 機能に会話状態の管理を任せ、`to_input_list()` や `Sessions` でローカル管理を行わないこともできます。これにより、過去のメッセージをすべて手動で再送せずに会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間で状態を追跡する 2 つの方法を提供しています:

#### 1. `conversation_id` を使用

最初に OpenAI Conversations API で会話を作成し、その ID を以後の各呼び出しで再利用します:

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id    

    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?", conversation_id=conv_id)
    print(result1.final_output)
    # San Francisco

    # Second turn reuses the same conversation_id
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        conversation_id=conv_id,
    )
    print(result2.final_output)
    # California
```

#### 2. `previous_response_id` を使用

もう 1 つの方法は **response chaining** で、各ターンが前のターンのレスポンス ID に明示的にリンクします。

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
    print(result1.final_output)
    # San Francisco

    # Second turn, chained to the previous response
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        previous_response_id=result1.last_response_id,
    )
    print(result2.final_output)
    # California
```


## 長時間稼働のエージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop のタスクを含む永続的で長時間稼働のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了させるデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で確認でき、[こちらのドキュメント](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)も参照してください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定の例外はここから派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に送出されます。これは、指定したやり取り回数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤となるモデル（LLM）が予期しない、または無効な出力を生成したときに発生します。これには次が含まれます:
    -   不正な JSON: 特定の `output_type` が定義されている場合に特に、ツール呼び出しや直接出力で不正な JSON 構造を返す場合。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できない場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用する際に（SDK を使ってコードを書く）あなたが誤りを犯した場合に送出されます。これは、誤ったコード実装、無効な構成、または SDK の API の誤用が原因で発生するのが一般的です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力ガードレールまたは出力ガードレールの条件が満たされたときに、それぞれ送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールはエージェントの最終応答を配信前にチェックします。