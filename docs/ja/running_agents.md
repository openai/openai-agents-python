---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。オプションは 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM をストリーミングモードで呼び出し、受信したイベントをそのままストリーミングします。

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

## エージェントのループ

`Runner` の run メソッドを使うとき、開始するエージェントと入力を渡します。入力は文字列（ユーザー メッセージとして扱われます）または入力アイテムのリスト（OpenAI Responses API のアイテム）にできます。

Runner は次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新して、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらを実行し、結果を追加して、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型のテキスト出力を生成し、かつツール呼び出しがない場合です。

## ストリーミング

ストリーミングを使うと、LLM の実行中にストリーミングイベントも受け取れます。ストリームが終わると、[`RunResultStreaming`][agents.result.RunResultStreaming] に、その実行で新たに生成されたすべての出力を含む、完全な情報が入っています。ストリーミングイベントは `.stream_events()` を呼び出してください。詳しくは [ストリーミングガイド](streaming.md) を参照してください。

## 実行設定

`run_config` パラメーターでは、エージェント実行のグローバル設定を構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力のガードレールのリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフに固有のフィルターがない場合に、すべてのハンドオフに適用するグローバル入力フィルターです。入力フィルターを使うと、新しいエージェントに送信する入力を編集できます。詳しくは [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、Runner は次のエージェントを呼び出す前に、直前のやり取りを 1 つの assistant メッセージに折りたたみます。ヘルパーは、コンテンツを `<CONVERSATION HISTORY>` ブロック内に配置し、その後のハンドオフが発生するたびに新しいターンを追加していきます。raw なトランスクリプトをそのまま渡したい場合は、これを `False` に設定するか、カスタムのハンドオフフィルターを指定してください。いずれの [`Runner` メソッド](agents.run.Runner) も、未指定時は自動で `RunConfig` を作成するため、クイックスタートや code examples ではこのデフォルトが自動的に適用され、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続き優先されます。個々のハンドオフは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: オプションの呼び出し可能オブジェクトで、`nest_handoff_history` が `True` のときに正規化されたトランスクリプト（履歴 + ハンドオフ アイテム）を受け取ります。次のエージェントに転送する入力アイテムの完全なリストを返す必要があり、フルのハンドオフフィルターを書かずに組み込みの要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM やツール呼び出しの入出力など、トレースに機微なデータを含めるかどうかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがってトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

デフォルトでは、SDK は、エージェントが別のエージェントにハンドオフするたびに、直前のターンを 1 つの assistant の要約メッセージ内に入れ子にします。これにより、assistant メッセージの重複が減り、新しいエージェントがすばやくスキャンできる単一のブロックに完全なトランスクリプトを保持します。レガシーの挙動に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定のハンドオフについては、`handoff(..., nest_handoff_history=False)` または `True` を設定してオプトアウト（またはオプトイン）できます。カスタムマッパーを書かずに生成される要約のラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

どの run メソッドを呼んでも、1 つ以上のエージェント（したがって 1 回以上の LLM 呼び出し）が実行される可能性がありますが、チャット会話における 1 つの論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフ、2 番目のエージェントがさらにツールを実行し、その後に出力を生成。

エージェントの実行が終わったら、ユーザーに何を表示するかを選べます。たとえば、エージェントによって生成されたすべての新しいアイテムを表示するか、最終出力のみを表示するかです。どちらの場合でも、ユーザーが追質問をする可能性があり、その場合は再度 run メソッドを呼び出します。

### 手動の会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使って、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions/index.md) を使うと、`.to_input_list()` を手動で呼ばずに会話履歴を自動管理できます:

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
-   各実行後に新しいメッセージを保存
-   異なるセッション ID ごとに個別の会話を維持

詳しくは [Sessions ドキュメント](sessions/index.md) を参照してください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の会話ステート機能により、サーバー側で会話ステートを管理することもできます。これにより、過去のメッセージをすべて手動で再送信することなく、会話履歴を保持できます。詳しくは [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI は、ターン間でステートを追跡する 2 つの方法を提供しています:

#### 1. `conversation_id` の使用

まず OpenAI Conversations API で会話を作成し、その ID を以降のすべての呼び出しで再利用します:

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

#### 2. `previous_response_id` の使用

もう 1 つの選択肢は **レスポンスのチェイニング (response chaining)** で、各ターンが直前のターンのレスポンス ID に明示的にリンクします。

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

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む永続的で長時間実行のワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)で確認でき、[こちらのドキュメント](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)も参照してください。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。ほかの特定の例外はすべてこれを継承します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が `Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` に渡した `max_turns` 制限を超えた場合に送出されます。指定した対話ターン数内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。これには次が含まれます:
    -   不正な JSON: 特定の `output_type` が定義されている場合に、ツール呼び出しや直接出力で不正な JSON 構造を返す
    -   予期しないツール関連の失敗: モデルが想定どおりにツールを使用できなかった場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（SDK を用いてコードを書く人）が、SDK の使用方法を誤った場合に送出されます。これは通常、不正なコード実装、無効な構成、または SDK の API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力ガードレールまたは出力ガードレールの条件が満たされたときに送出されます。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールはエージェントの最終応答を配信前にチェックします。