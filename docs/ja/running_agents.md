---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスで実行できます。オプションは 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]: 非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。LLM を ストリーミング モードで呼び出し、受信したイベントをそのまま ストリーミング します。

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

`Runner` の run メソッドを使うときは、開始するエージェントと入力を渡します。入力は文字列（ユーザー メッセージと見なされます）か、OpenAI Responses API のアイテムのリストのいずれかです。

ランナーは次のループを実行します:

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループは終了し、結果を返します。
    2. LLM が ハンドオフ を行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM が ツール呼び出し を生成した場合、それらを実行し、結果を追加してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、目的の型のテキスト出力を生成し、かつ ツール呼び出し がない場合です。

## ストリーミング

ストリーミング を使うと、LLM の実行中に追加で ストリーミング イベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] に、その実行で生成されたすべての新規出力を含む、実行に関する完全な情報が入ります。ストリーミング イベントは `.stream_events()` を呼び出して取得できます。詳しくは [ストリーミング ガイド](streaming.md) をご覧ください。

## 実行設定 (Run config)

`run_config` パラメーターで、エージェント実行のグローバル設定をいくつか構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent の `model` 設定に関係なく、使用するグローバルな LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名の解決に使うモデルプロバイダーで、デフォルトは OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。例えば、グローバルな `temperature` や `top_p` を設定できます。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力または出力の ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ に既定でフィルターがない場合に適用する、すべての ハンドオフ に対するグローバルな入力フィルターです。入力フィルターでは、新しいエージェントに送る入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`（デフォルト）の場合、次のエージェントを呼び出す前に、ランナーはそれまでの記録を 1 つの assistant メッセージに折りたたみます。ヘルパーは内容を `<CONVERSATION HISTORY>` ブロック内に配置し、以降の ハンドオフ ごとに新しいターンを追記していきます。生の記録をそのまま渡したい場合は、これを `False` にするか、カスタムの ハンドオフ フィルターを指定してください。いずれの [`Runner` メソッド](agents.run.Runner) も、未指定の場合は自動で `RunConfig` を作成します。そのため、クイックスタートや code examples ではこのデフォルトが自動的に適用され、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックがある場合は引き続きそれが優先されます。個々の ハンドオフ は [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] でこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: 省略可能な呼び出し可能オブジェクトで、`nest_handoff_history` が `True` のときに正規化された記録（履歴 + ハンドオフ アイテム）を受け取ります。次のエージェントへ転送する入力アイテムのリストを正確に返す必要があり、完全な ハンドオフ フィルターを書かずに組み込み要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM や ツール呼び出し の入出力など、機微なデータをトレースに含めるかを設定します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシング ワークフロー名、トレース ID、トレース グループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。グループ ID は任意で、複数の実行にまたがるトレースをリンクできます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。

デフォルトでは、SDK はあるエージェントが別のエージェントに ハンドオフ する際、以前のターンを 1 つの assistant 要約メッセージの中に入れ子にします。これにより、assistant メッセージの重複が減り、完全な記録を新しいエージェントがすばやく走査できる 1 つのブロックに保持できます。従来の動作に戻したい場合は、`RunConfig(nest_handoff_history=False)` を渡すか、会話を必要なとおりにそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定してください。特定の ハンドオフ については、`handoff(..., nest_handoff_history=False)` または `True` を設定して個別にオプトアウト（またはオプトイン）できます。カスタム マッパーを書かずに生成される要約のラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）。

## 会話/チャットスレッド

任意の run メソッドの呼び出しは、1 回以上のエージェント実行（つまり 1 回以上の LLM 呼び出し）を引き起こす可能性がありますが、チャット会話における 1 回の論理的なターンを表します。例:

1. ユーザーのターン: ユーザーがテキストを入力
2. ランナーの実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントに ハンドオフ、2 番目のエージェントがさらにツールを実行し、その後に出力を生成。

エージェントの実行の最後に、ユーザーに何を見せるかを選べます。たとえば、エージェントが生成したすべての新規アイテム、または最終出力のみを表示できます。いずれの場合でも、ユーザーが追質問をする可能性があり、その場合は再度 run メソッドを呼び出せばよいです。

### 手動の会話管理

次のターンの入力を取得するために、[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、会話履歴を手動で管理できます:

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

より簡単な方法として、[Sessions](sessions/index.md) を使えば、`.to_input_list()` を手動で呼び出すことなく、会話履歴を自動処理できます:

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

Sessions は自動で以下を行います:

-   各実行前に会話履歴を取得
-   各実行後に新しいメッセージを保存
-   セッション ID ごとに別々の会話を維持

詳細は [Sessions のドキュメント](sessions/index.md) をご覧ください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI の conversation state 機能により サーバー 側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送しなくても、会話履歴を保持できます。詳しくは [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI はターン間の状態を追跡する 2 つの方法を提供しています:

#### 1. `conversation_id` の使用

まず OpenAI Conversations API を使って会話を作成し、その ID を以降のすべての呼び出しで再利用します:

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

#### 2. `previous_response_id` の使用

もう 1 つの方法は **response chaining** で、各ターンを前のターンの response ID に明示的にリンクします。

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


## 長時間実行エージェントと human-in-the-loop

Agents SDK の [Temporal](https://temporal.io/) 連携を使うと、human-in-the-loop タスクを含む、耐久性のある長時間実行のワークフローを構築できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは [この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) をご覧ください。ドキュメントは [こちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) にあります。

## 例外

SDK は特定の場合に例外を送出します。完全な一覧は [`agents.exceptions`][] にあります。概要は以下のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。その他の特定の例外はすべてこの一般的な型から派生します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡した `max_turns` 制限を超えたときに送出されます。指定した対話ターン数内にタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤のモデル（LLM）が予期しない、または無効な出力を生成した場合に発生します。例:
    -   不正な JSON: 特定の `output_type` が定義されている場合に特に、ツール呼び出しや直接出力で不正な JSON 構造を返した場合。
    -   予期しないツール関連の失敗: モデルが期待どおりにツールを使用できなかった場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するあなた（この SDK を用いてコードを書く人）が SDK の使用中に誤りを犯したときに送出されます。これは通常、誤ったコード実装、無効な構成、または SDK の API の誤用が原因です。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: それぞれ、入力 ガードレール または出力 ガードレール の条件が満たされたときに送出されます。入力 ガードレール は処理前に受信メッセージを検査し、出力 ガードレール はエージェントの最終応答を配信前に検査します。