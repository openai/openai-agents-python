---
search:
  exclude: true
---
# エージェントの実行

エージェントは [`Runner`][agents.run.Runner] クラスを介して実行できます。選択肢は 3 つあります:

1. [`Runner.run()`][agents.run.Runner.run]。非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]。同期メソッドで、内部では単に `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままあなたにストリームします。

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

詳細は [execution results ガイド](results.md) を参照してください。

## エージェントループ

`Runner` の run メソッドを使用する場合、開始エージェントと入力を渡します。入力は文字列 (ユーザーメッセージとして扱われます) か、入力アイテムのリスト (OpenAI Responses API のアイテム) のいずれかです。

その後 runner はループを実行します:

1. 現在の入力を用いて、現在のエージェントのために LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して execution results を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新してループを再実行します。
    3. LLM がツール呼び出しを生成した場合、それらのツール呼び出しを実行し、結果を追記してループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を送出します。

!!! note

    LLM の出力が「最終出力」と見なされるルールは、望ましい型でテキスト出力を生成し、かつツール呼び出しが存在しないことです。

## ストリーミング

ストリーミングを使用すると、LLM の実行中に追加でストリーミングイベントを受け取れます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には実行に関する完全な情報 (生成されたすべての新しい出力を含む) が格納されます。ストリーミングイベントは `.stream_events()` を呼び出して取得できます。詳細は [ストリーミングガイド](streaming.md) を参照してください。

## Run 設定

`run_config` パラメーターを使用すると、エージェント実行のグローバル設定をいくつか構成できます:

-   [`model`][agents.run.RunConfig.model]: 各 Agent が持つ `model` に関係なく、使用するグローバル LLM モデルを設定できます。
-   [`model_provider`][agents.run.RunConfig.model_provider]: モデル名を解決するためのモデルプロバイダーで、既定は OpenAI です。
-   [`model_settings`][agents.run.RunConfig.model_settings]: エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` や `top_p` を設定できます。
-   [`session_settings`][agents.run.RunConfig.session_settings]: 実行中に履歴を取得する際のセッションレベル既定 (例: `SessionSettings(limit=...)`) を上書きします。
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: すべての実行に含める入力/出力 ガードレール のリストです。
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: ハンドオフ側にまだ設定がない場合に、すべてのハンドオフへ適用するグローバル入力フィルターです。入力フィルターにより、新しいエージェントに送信される入力を編集できます。詳細は [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 次のエージェントを呼び出す前に、直前までのトランスクリプトを 1 つの assistant メッセージに畳み込むオプトインのベータ機能です。ネストされたハンドオフを安定化する間、既定では無効です。有効化するには `True` を設定し、raw のトランスクリプトをそのまま渡すには `False` のままにします。[Runner の各メソッド][agents.run.Runner] は、渡されない場合に自動で `RunConfig` を作成するため、クイックスタートと例は既定でオフのままになり、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きそれを上書きします。個々のハンドオフは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] によりこの設定を上書きできます。
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` をオプトインしたときに、正規化されたトランスクリプト (履歴 + ハンドオフアイテム) を受け取る任意の callable です。次のエージェントに転送する入力アイテムのリストをそのまま返す必要があり、完全なハンドオフフィルターを書かずに組み込み要約を置き換えられます。
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 実行全体の [トレーシング](tracing.md) を無効化できます。
-   [`tracing`][agents.run.RunConfig.tracing]: この実行におけるエクスポーター、プロセッサー、またはトレーシングメタデータを上書きするために [`TracingConfig`][agents.tracing.TracingConfig] を渡します。
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: トレースに、LLM およびツール呼び出しの入出力など、機密の可能性があるデータを含めるかどうかを構成します。
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 実行のトレーシングにおけるワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` の設定を推奨します。group ID は任意のフィールドで、複数の実行にまたがってトレースを関連付けられます。
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: すべてのトレースに含めるメタデータです。
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 使用時に、各ターンの前に新しいユーザー入力がセッション履歴とどのようにマージされるかをカスタマイズします。
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: モデル呼び出しの直前に、完全に準備されたモデル入力 (instructions と入力アイテム) を編集するためのフックです。たとえば、履歴をトリムしたり、システムプロンプトを注入したりできます。
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 承認フロー中にツール呼び出しが拒否された場合の、モデルから見えるメッセージをカスタマイズします。

ネストされたハンドオフはオプトインのベータとして利用できます。`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフに対して `handoff(..., nest_handoff_history=True)` を設定して、トランスクリプトを畳み込む挙動を有効にしてください。raw のトランスクリプト (既定) を維持したい場合は、このフラグを設定しないか、会話を必要なとおりにそのまま転送する `handoff_input_filter` (または `handoff_history_mapper`) を指定してください。カスタム mapper を書かずに生成要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します (既定に戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers])。

## 会話 / チャットスレッド

いずれの run メソッドを呼び出しても、1 つ以上のエージェントが実行され (したがって 1 回以上の LLM 呼び出しが行われ) る可能性がありますが、チャット会話における 1 つの論理的なターンを表します。たとえば:

1. ユーザーターン: ユーザーがテキストを入力します
2. Runner の実行: 最初のエージェントが LLM を呼び出し、ツールを実行し、2 番目のエージェントへハンドオフし、2 番目のエージェントがさらにツールを実行してから出力を生成します。

エージェントの実行が終わったら、ユーザーに何を表示するかを選べます。たとえば、エージェントが生成した新しいアイテムをすべて表示することも、最終出力だけを表示することもできます。いずれにしても、その後ユーザーがフォローアップ質問をするかもしれず、その場合は再度 run メソッドを呼び出せます。

### 手動の会話管理

次のターンの入力を取得するために [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して、会話履歴を手動で管理できます:

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

### Sessions による自動の会話管理

より簡単な方法として、[Sessions](sessions/index.md) を使用すれば、`.to_input_list()` を手動で呼び出すことなく会話履歴を自動的に扱えます:

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

Sessions は自動的に次を行います:

-   各実行の前に会話履歴を取得します
-   各実行の後に新しいメッセージを保存します
-   異なるセッション ID ごとに別々の会話を維持します

詳細は [Sessions ドキュメント](sessions/index.md) を参照してください。


### サーバー管理の会話

`to_input_list()` や `Sessions` でローカルに扱う代わりに、OpenAI conversation state 機能によりサーバー側で会話状態を管理することもできます。これにより、過去のメッセージをすべて手動で再送しなくても会話履歴を保持できます。詳細は [OpenAI Conversation state ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses) を参照してください。

OpenAI は、ターンをまたいで状態を追跡する 2 つの方法を提供します:

#### 1. `conversation_id` を使用する

まず OpenAI Conversations API を使用して会話を作成し、その後のすべての呼び出しでその ID を再利用します:

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

#### 2. `previous_response_id` を使用する

別の選択肢は **response chaining** で、各ターンを直前のターンの response ID に明示的にリンクします。

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

## Call model input filter

`call_model_input_filter` を使用して、モデル呼び出し直前にモデル入力を編集します。このフックは現在のエージェント、コンテキスト、結合済み入力アイテム (セッション履歴がある場合はそれを含む) を受け取り、新しい `ModelInputData` を返します。

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

機密データのマスキング、長い履歴のトリミング、追加のシステムガイダンスの注入のために、`run_config` で実行ごとにフックを設定するか、`Runner` の既定として設定してください。

## エラーハンドラー

すべての `Runner` エントリポイントは、エラー種別をキーとする dict である `error_handlers` を受け取ります。現在サポートされるキーは `"max_turns"` です。`MaxTurnsExceeded` を送出する代わりに、制御された最終出力を返したい場合に使用してください。

```python
from agents import (
    Agent,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    Runner,
)

agent = Agent(name="Assistant", instructions="Be concise.")


def on_max_turns(_data: RunErrorHandlerInput[None]) -> RunErrorHandlerResult:
    return RunErrorHandlerResult(
        final_output="I couldn't finish within the turn limit. Please narrow the request.",
        include_in_history=False,
    )


result = Runner.run_sync(
    agent,
    "Analyze this long transcript",
    max_turns=3,
    error_handlers={"max_turns": on_max_turns},
)
print(result.final_output)
```

フォールバック出力を会話履歴に追記したくない場合は、`include_in_history=False` を設定してください。

## 長時間実行エージェントと human-in-the-loop

ツール承認の一時停止/再開パターンについては、専用の [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 連携を使用すると、human-in-the-loop タスクを含む、耐久性のある長時間実行ワークフローを実行できます。Temporal と Agents SDK が連携して長時間タスクを完了するデモは、[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8) で確認でき、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) です。 

### Restate

Agents SDK の [Restate](https://restate.dev/) 連携を使用すると、人による承認、ハンドオフ、セッション管理を含む、軽量で耐久性のあるエージェントを利用できます。この連携には依存関係として Restate のシングルバイナリ runtime が必要で、プロセス/コンテナまたはサーバーレス関数としてエージェントを実行することをサポートします。
詳細は [overview](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) を読むか、[docs](https://docs.restate.dev/ai) を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 連携を使用すると、障害や再起動をまたいでも進捗を保持する信頼性の高いエージェントを実行できます。長時間実行エージェント、human-in-the-loop ワークフロー、ハンドオフをサポートします。同期および非同期メソッドの両方をサポートします。この連携に必要なのは SQLite または Postgres データベースのみです。詳細は、連携の [repo](https://github.com/dbos-inc/dbos-openai-agents) と [docs](https://docs.dbos.dev/integrations/openai-agents) を参照してください。

## 例外

SDK は特定のケースで例外を送出します。全一覧は [`agents.exceptions`][] にあります。概要は次のとおりです:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 内で送出されるすべての例外の基底クラスです。ほかのすべての特定例外が派生する汎用型として機能します。
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: エージェントの実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に送出される例外です。指定された対話ターン数内にエージェントがタスクを完了できなかったことを示します。
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 基盤モデル (LLM) が予期しない、または無効な出力を生成した場合に発生する例外です。たとえば次を含みます:
    -   不正な形式の JSON: モデルが、ツール呼び出しや直接出力に対して不正な形式の JSON 構造を返す場合。特に特定の `output_type` が定義されている場合。
    -   想定外のツール関連の失敗: モデルが想定どおりにツールを使用しない場合
-   [`UserError`][agents.exceptions.UserError]: SDK を使用するコードを書くあなた (SDK を使うコードの作成者) が、SDK の使用中に誤りをした場合に送出される例外です。通常、誤ったコード実装、無効な設定、または SDK API の誤用に起因します。
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 入力 ガードレール または出力 ガードレール の条件がそれぞれ満たされた場合に送出される例外です。入力 ガードレール は処理前に受信メッセージをチェックし、出力 ガードレール は配信前にエージェントの最終応答をチェックします。