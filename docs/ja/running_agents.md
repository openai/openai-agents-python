---
search:
  exclude: true
---
# エージェントの実行

[`Runner`][agents.run.Runner] クラスを使用してエージェントを実行できます。次の 3 つの方法があります。

1. [`Runner.run()`][agents.run.Runner.run]：非同期で実行し、[`RunResult`][agents.result.RunResult] を返します。
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]：同期メソッドで、内部的には `.run()` を実行します。
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]：非同期で実行し、[`RunResultStreaming`][agents.result.RunResultStreaming] を返します。ストリーミングモードで LLM を呼び出し、受信したイベントをそのままストリーミングします。

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

詳細については、[実行結果ガイド](results.md)を参照してください。

## Runner のライフサイクルと設定

### エージェントループ

`Runner` の run メソッドを使用する場合、開始エージェントと入力を渡します。入力には次のものを使用できます。

- 文字列（ユーザーメッセージとして扱われます）
- OpenAI Responses API 形式の入力項目のリスト
- 中断された実行を再開する場合は [`RunState`][agents.run_state.RunState]

その後、runner はループを実行します。

1. 現在のエージェントに対して、現在の入力で LLM を呼び出します。
2. LLM が出力を生成します。
    1. LLM が `final_output` を返した場合、ループを終了して実行結果を返します。
    2. LLM がハンドオフを行った場合、現在のエージェントと入力を更新し、ループを再実行します。
    3. LLM がツール呼び出しを生成した場合、そのツール呼び出しを実行して結果を追加し、ループを再実行します。
3. 渡された `max_turns` を超えた場合、[`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 例外を発生させます。このターン制限を無効にするには、`max_turns=None` を渡します。

!!! note

    LLM の出力が「最終出力」と見なされる条件は、目的の型のテキスト出力が生成され、ツール呼び出しが存在しないことです。

### ストリーミング

ストリーミングを使用すると、LLM の実行中にストリーミングイベントも受信できます。ストリームが完了すると、[`RunResultStreaming`][agents.result.RunResultStreaming] には、生成されたすべての新しい出力を含む、実行に関する完全な情報が格納されます。ストリーミングイベントには `.stream_events()` を呼び出せます。詳細については、[ストリーミングガイド](streaming.md)を参照してください。

#### Responses WebSocket トランスポート（オプションのヘルパー）

OpenAI Responses の websocket トランスポートを有効にしても、通常の `Runner` API を引き続き使用できます。接続を再利用するには websocket セッションヘルパーの使用を推奨しますが、必須ではありません。

これは websocket トランスポート上の Responses API であり、[Realtime API](realtime/guide.md)ではありません。

トランスポートの選択ルール、および具体的なモデルオブジェクトやカスタムプロバイダーに関する注意事項については、[モデル](models/index.md#responses-websocket-transport)を参照してください。

##### パターン 1：セッションヘルパーなし（動作可能）

websocket トランスポートのみが必要で、共有プロバイダーやセッションを SDK に管理させる必要がない場合に使用します。

```python
import asyncio

from agents import Agent, Runner, set_default_openai_responses_transport


async def main():
    set_default_openai_responses_transport("websocket")

    agent = Agent(name="Assistant", instructions="Be concise.")
    result = Runner.run_streamed(agent, "Summarize recursion in one sentence.")

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            continue
        print(event.type)


asyncio.run(main())
```

このパターンは、単一の実行に適しています。`Runner.run()` / `Runner.run_streamed()` を繰り返し呼び出す場合、同じ `RunConfig` / プロバイダーインスタンスを手動で再利用しない限り、実行ごとに再接続される可能性があります。

##### パターン 2：`responses_websocket_session()` の使用（複数ターンでの再利用に推奨）

複数の実行で websocket 対応の共有プロバイダーと `RunConfig` を使用する場合は、[`responses_websocket_session()`][agents.responses_websocket_session] を使用します。これには、同じ `run_config` を継承する、ネストされた Agents-as-tools 呼び出しも含まれます。

```python
import asyncio

from agents import Agent, responses_websocket_session


async def main():
    agent = Agent(name="Assistant", instructions="Be concise.")

    async with responses_websocket_session(
        responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
    ) as ws:
        first = ws.run_streamed(agent, "Say hello in one short sentence.")
        async for _event in first.stream_events():
            pass

        second = ws.run_streamed(
            agent,
            "Now say goodbye.",
            previous_response_id=first.last_response_id,
        )
        async for _event in second.stream_events():
            pass


asyncio.run(main())
```

コンテキストを終了する前に、ストリーミングされた実行結果の処理を完了してください。websocket リクエストが進行中の状態でコンテキストを終了すると、共有接続が強制的に閉じられる可能性があります。

長時間の推論ターンで websocket の keepalive タイムアウトが発生する場合は、`ping_timeout` を大きくするか、`ping_timeout=None` を設定してハートビートタイムアウトを無効にしてください。websocket のレイテンシよりも信頼性が重要な実行では、HTTP/SSE トランスポートを使用してください。

### 実行設定

`run_config` パラメーターを使用すると、エージェントの実行に関するグローバル設定を構成できます。

#### 一般的な実行設定カテゴリー

各エージェントの定義を変更せず、単一の実行に対する動作を上書きするには、`RunConfig` を使用します。

##### モデル、プロバイダー、セッションのデフォルト設定

- [`model`][agents.run.RunConfig.model]：各 Agent に設定されている `model` に関係なく、使用するグローバルな LLM モデルを設定できます。
- [`model_provider`][agents.run.RunConfig.model_provider]：モデル名を検索するためのモデルプロバイダーです。デフォルトは OpenAI です。
- [`model_settings`][agents.run.RunConfig.model_settings]：エージェント固有の設定を上書きします。たとえば、グローバルな `temperature` または `top_p` を設定できます。
- [`session_settings`][agents.run.RunConfig.session_settings]：実行中に履歴を取得する際のセッションレベルのデフォルト設定（たとえば、`SessionSettings(limit=...)`）を上書きします。
- [`session_input_callback`][agents.run.RunConfig.session_input_callback]：Sessions の使用時に、各ターンの前に新しいユーザー入力をセッション履歴とマージする方法をカスタマイズします。コールバックは同期または非同期にできます。

##### ガードレール、ハンドオフ、モデル入力の整形

- [`input_guardrails`][agents.run.RunConfig.input_guardrails]、[`output_guardrails`][agents.run.RunConfig.output_guardrails]：すべての実行に含める入力ガードレールまたは出力ガードレールのリストです。
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]：ハンドオフにフィルターがまだ設定されていない場合、すべてのハンドオフに適用するグローバル入力フィルターです。入力フィルターを使用すると、新しいエージェントに送信される入力を編集できます。詳細については、[`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] のドキュメントを参照してください。
- [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]：次のエージェントを呼び出す前に、それまでのトランスクリプトを単一の assistant メッセージにまとめる、オプトインのベータ機能です。ネストされたハンドオフの安定化を進めているため、デフォルトでは無効です。有効にするには `True` を設定し、raw トランスクリプトをそのまま渡すには `False` のままにします。[Runner の各メソッド][agents.run.Runner]は、`RunConfig` が渡されなかった場合に自動的に作成するため、クイックスタートとコード例ではデフォルトで無効のままです。また、明示的な [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] コールバックは引き続きこの設定を上書きします。個々のハンドオフでは、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を使用してこの設定を上書きできます。
- [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]：`nest_handoff_history` を有効にした際に、正規化されたトランスクリプト（履歴とハンドオフ項目）を受け取るオプションの callable です。次のエージェントに転送する入力項目の正確なリストを返す必要があり、完全なハンドオフフィルターを記述することなく、組み込みの要約を置き換えられます。
- [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]：モデル呼び出しの直前に、完全に準備されたモデル入力（instructions と入力項目）を編集するためのフックです。たとえば、履歴の短縮やシステムプロンプトの挿入に使用できます。
- [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]：runner が以前の出力を次のターンのモデル入力に変換するときに、推論項目 ID を保持するか省略するかを制御します。

##### トレーシングと可観測性

- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]：実行全体の[トレーシング](tracing.md)を無効にできます。
- [`tracing`][agents.run.RunConfig.tracing]：実行ごとのトレーシング API キーなど、トレースのエクスポート設定を上書きするには、[`TracingConfig`][agents.tracing.TracingConfig] を渡します。
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]：LLM やツール呼び出しの入出力など、機密である可能性があるデータをトレースに含めるかどうかを設定します。
- [`workflow_name`][agents.run.RunConfig.workflow_name]、[`trace_id`][agents.run.RunConfig.trace_id]、[`group_id`][agents.run.RunConfig.group_id]：実行のトレーシングワークフロー名、トレース ID、トレースグループ ID を設定します。少なくとも `workflow_name` を設定することを推奨します。グループ ID は、複数の実行にわたってトレースを関連付けるためのオプションフィールドです。
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]：すべてのトレースに含めるメタデータです。

##### ツールの実行、承認、エラー動作

- [`tool_execution`][agents.run.RunConfig.tool_execution]：同時に実行する関数ツールの数を制限するなど、ローカルツール呼び出しに対する SDK 側の実行動作を設定します。
- [`tool_not_found_behavior`][agents.run.RunConfig.tool_not_found_behavior]：モデルが出力した、解決できない関数ツール呼び出しを runner が処理する方法を設定します。デフォルトでは `ModelBehaviorError` が発生します。代わりに、モデルから参照可能なエラー出力を返すようオプトインできます。
- [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]：承認の拒否や、オプトインしたツール未検出時の出力など、モデルから参照可能なツールエラーメッセージをカスタマイズします。

ネストされたハンドオフは、オプトインのベータ機能として利用できます。トランスクリプトをまとめる動作を有効にするには、`RunConfig(nest_handoff_history=True)` を渡すか、特定のハンドオフに対して `handoff(..., nest_handoff_history=True)` を設定します。raw トランスクリプトを保持する場合（デフォルト）は、フラグを設定しないか、必要に応じて会話をそのまま転送する `handoff_input_filter`（または `handoff_history_mapper`）を指定します。カスタム mapper を記述せずに、生成される要約で使用されるラッパーテキストを変更するには、[`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します。デフォルトに戻すには [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] を呼び出します。

#### 実行設定の詳細

##### `tool_execution`

実行時のローカル関数ツールの同時実行数を制限するなど、ローカル関数ツールに対する SDK 側の動作を設定する場合は、`tool_execution` を使用します。

```python
from agents import Agent, RunConfig, Runner, ToolExecutionConfig

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Run the required tool calls.",
    run_config=RunConfig(
        tool_execution=ToolExecutionConfig(
            max_function_tool_concurrency=2,
            pre_approval_tool_input_guardrails=True,
        ),
    ),
)
```

`max_function_tool_concurrency=None` はデフォルトの動作を維持します。モデルが 1 ターンで複数の関数ツール呼び出しを出力すると、SDK は出力されたすべてのローカル関数ツール呼び出しを開始します。同時に実行するローカル関数ツールの数を制限するには、整数値を設定します。

これは、プロバイダー側の [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls] とは別のものです。`parallel_tool_calls` は、モデルが 1 回のレスポンスで複数のツール呼び出しを出力できるかどうかを制御します。`tool_execution.max_function_tool_concurrency` は、モデルがツール呼び出しを出力した後に、SDK がローカル関数ツール呼び出しを実行する方法を制御します。

`pre_approval_tool_input_guardrails=False` はデフォルトの承認フローを維持します。関数ツールに承認が必要な場合、実行は最初に一時停止し、ツール入力ガードレールは承認後の実行直前にのみ実行されます。保留中の承認による中断が発生する前に関数ツールの入力ガードレールを実行する場合は、`True` に設定します。この承認前チェックに合格した呼び出しでも、承認後に同じ入力ガードレールが再度実行されるため、時間依存のチェックは実行前に再検証されます。

##### `tool_not_found_behavior`

デフォルトでは、モデルが現在のエージェントで使用可能な関数ツールのいずれにも一致しない関数ツール呼び出しを出力すると、runner は `ModelBehaviorError` を発生させます。

実行を復旧可能な状態に保つ場合は、`tool_not_found_behavior="return_error_to_model"` を設定します。このモードでは、SDK は解決できないツール呼び出しに対する `function_call_output` を追加し、モデルを再実行します。これにより、モデルは使用可能なツールを選択するか、そのツールを使用せずに回答できます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Handle this request with the available tools.",
    run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
)
```

現在、このオプションは解決できない関数ツール呼び出しにのみ適用されます。その他の無効なツールペイロードには、既存のエラー動作が引き続き適用されます。

##### `tool_error_formatter`

SDK がモデルから参照可能なツールエラー出力を作成する際にモデルへ返すメッセージをカスタマイズするには、`tool_error_formatter` を使用します。

formatter は、次の内容を含む [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs] を受け取ります。

- `kind`：`"approval_rejected"` や `"tool_not_found"` などのエラーカテゴリーです。
- `tool_type`：ツールのランタイム（`"function"`、`"computer"`、`"shell"`、`"apply_patch"`、または `"custom"`）です。
- `tool_name`：ツール名です。
- `call_id`：ツール呼び出し ID です。
- `default_message`：モデルから参照可能な SDK のデフォルトメッセージです。
- `run_context`：アクティブな実行コンテキストのラッパーです。

メッセージを置き換えるには文字列を返し、SDK のデフォルトを使用するには `None` を返します。

```python
from agents import Agent, RunConfig, Runner, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind == "approval_rejected":
        return (
            f"Tool call '{args.tool_name}' was rejected by a human reviewer. "
            "Ask for confirmation or propose a safer alternative."
        )
    if args.kind == "tool_not_found":
        return f"Tool '{args.tool_name}' is not available. Choose one of the listed tools."
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

##### `reasoning_item_id_policy`

`reasoning_item_id_policy` は、runner が履歴を次のターンへ引き継ぐ際に、推論項目を次のターンのモデル入力へ変換する方法を制御します（たとえば、`RunResult.to_input_list()` またはセッションに基づく実行を使用する場合）。

- `None` または `"preserve"`（デフォルト）：推論項目 ID を保持します。
- `"omit"`：生成される次のターンの入力から推論項目 ID を削除します。

`"omit"` は主に、推論項目が `id` を伴って送信される一方で、必須の後続項目がない場合に発生する Responses API の 400 エラー群に対する、オプトインの緩和策として使用します（例：`Item 'rs_...' of type 'reasoning' was provided without its required following item.`）。

これは、SDK が以前の出力から後続入力を構築し、推論項目 ID が保持される一方で、プロバイダーがその ID と対応する後続項目とのペアを維持するよう要求する場合に、複数ターンのエージェント実行で発生する可能性があります。これには、セッションの永続化、サーバー管理の会話差分、ストリーミング／非ストリーミングの後続ターン、再開パスが含まれます。

`reasoning_item_id_policy="omit"` を設定すると、推論内容を保持しながら推論項目の `id` を削除できます。これにより、SDK が生成する後続入力で、その API の不変条件に抵触することを回避できます。

適用範囲に関する注意事項：

- これは、SDK が後続入力を構築するときに生成または転送する推論項目のみを変更します。
- ユーザーが指定した初期入力項目は書き換えません。
- このポリシーの適用後でも、`call_model_input_filter` によって意図的に推論 ID を再導入できます。

## 状態と会話の管理

### メモリ戦略の選択

状態を次のターンへ引き継ぐ一般的な方法は 4 つあります。

| 戦略 | 状態の保存場所 | 最適な用途 | 次のターンで渡すもの |
| --- | --- | --- | --- |
| `result.to_input_list()` | アプリのメモリ | 小規模なチャットループ、完全な手動制御、任意のプロバイダー | `result.to_input_list()` のリストと次のユーザーメッセージ |
| `session` | ストレージと SDK | 永続的なチャット状態、再開可能な実行、カスタムストア | 同じ `session` インスタンス、または同じストアを参照する別のインスタンス |
| `conversation_id` | OpenAI Conversations API | ワーカーやサービス間で共有する、名前付きのサーバー側会話 | 同じ `conversation_id` と新しいユーザーターンのみ |
| `previous_response_id` | OpenAI Responses API | 会話リソースを作成せずに行う軽量なサーバー管理の継続 | `result.last_response_id` と新しいユーザーターンのみ |

`result.to_input_list()` と `session` はクライアント管理です。`conversation_id` と `previous_response_id` は OpenAI 管理であり、OpenAI Responses API を使用している場合にのみ適用されます。ほとんどのアプリケーションでは、会話ごとに 1 つの永続化戦略を選択してください。クライアント管理の履歴と OpenAI 管理の状態を混在させると、両方のレイヤーを意図的に調整している場合を除き、コンテキストが重複する可能性があります。

!!! note

    セッションの永続化と、サーバー管理の会話設定
    （`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）を
    同じ実行で組み合わせることはできません。呼び出しごとにいずれか 1 つの方法を選択してください。

### 会話／チャットスレッド

いずれかの run メソッドを呼び出すと、1 つ以上のエージェントが実行される可能性があり、それに伴って LLM が 1 回以上呼び出されますが、これはチャット会話における論理的な 1 ターンを表します。次に例を示します。

1. ユーザーターン：ユーザーがテキストを入力します。
2. Runner の実行：最初のエージェントが LLM を呼び出し、ツールを実行して 2 番目のエージェントへハンドオフします。2 番目のエージェントがさらにツールを実行し、出力を生成します。

エージェントの実行終了時に、ユーザーに何を表示するかを選択できます。たとえば、エージェントが生成したすべての新しい項目を表示することも、最終出力のみを表示することもできます。いずれの場合も、ユーザーが続けて質問した場合は、run メソッドを再度呼び出せます。

#### 手動による会話管理

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] メソッドを使用して次のターンの入力を取得し、会話履歴を手動で管理できます。

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

#### セッションによる自動会話管理

より簡単な方法として、[Sessions](sessions/index.md) を使用すると、`.to_input_list()` を手動で呼び出すことなく、会話履歴を自動的に処理できます。

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

Sessions は次の処理を自動的に行います。

- 各実行の前に会話履歴を取得します。
- 各実行の後に新しいメッセージを保存します。
- セッション ID ごとに個別の会話を維持します。

詳細については、[Sessions のドキュメント](sessions/index.md)を参照してください。


#### サーバー管理の会話

`to_input_list()` または `Sessions` を使用してローカルで処理する代わりに、OpenAI の会話状態機能にサーバー側で会話状態を管理させることもできます。これにより、過去のすべてのメッセージを毎回手動で再送信することなく、会話履歴を維持できます。以下のいずれかのサーバー管理方式では、各リクエストで新しいターンの入力のみを渡し、保存した ID を再利用します。詳細については、[OpenAI の会話状態ガイド](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)を参照してください。

OpenAI は、ターンをまたいで状態を追跡する 2 つの方法を提供します。

##### 1. `conversation_id` の使用

まず OpenAI Conversations API を使用して会話を作成し、その後の各呼び出しで ID を再利用します。

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

##### 2. `previous_response_id` の使用

もう 1 つの方法は **レスポンスチェーン** です。各ターンを前のターンのレスポンス ID に明示的に関連付けます。

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

実行が承認待ちで一時停止し、[`RunState`][agents.run_state.RunState] から再開した場合、SDK は保存された `conversation_id` / `previous_response_id` / `auto_previous_response_id` の設定を保持するため、再開したターンは同じサーバー管理の会話内で継続されます。

`conversation_id` と `previous_response_id` は同時に使用できません。システム間で共有できる名前付き会話リソースが必要な場合は、`conversation_id` を使用します。ターン間で最も軽量な Responses API の継続用基本コンポーネントが必要な場合は、`previous_response_id` を使用します。

!!! note

    SDK は `conversation_locked` エラーをバックオフ付きで自動的に再試行します。サーバー管理の
    会話を使用する実行では、同じ準備済み項目を問題なく再送信できるよう、再試行前に内部の
    conversation tracker の入力を巻き戻します。

    ローカルのセッションに基づく実行（`conversation_id`、`previous_response_id`、
    `auto_previous_response_id` のいずれとも組み合わせられません）でも、SDK は再試行後に
    履歴項目が重複することを抑えるため、直近に永続化された入力項目のロールバックを可能な範囲で行います。

    この互換性維持のための再試行は、`ModelSettings.retry` を設定していない場合でも行われます。
    モデルリクエストに対する、より広範なオプトインの再試行動作については、[Runner 管理の再試行](models/index.md#runner-managed-retries)を参照してください。

## フックとカスタマイズ

### モデル呼び出しの入力フィルター

モデル呼び出しの直前にモデル入力を編集するには、`call_model_input_filter` を使用します。このフックは、現在のエージェント、コンテキスト、および統合された入力項目（存在する場合はセッション履歴を含む）を受け取り、新しい `ModelInputData` を返します。

戻り値は [`ModelInputData`][agents.run.ModelInputData] オブジェクトである必要があります。その `input` フィールドは必須で、入力項目のリストでなければなりません。それ以外の形式を返すと `UserError` が発生します。

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

runner は準備済み入力リストのコピーをフックに渡すため、呼び出し元の元のリストを直接変更することなく、短縮、置換、並べ替えを行えます。

セッションを使用している場合、`call_model_input_filter` はセッション履歴がすでに読み込まれ、現在のターンとマージされた後に実行されます。この前段階のマージ処理自体をカスタマイズする場合は、[`session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。

`conversation_id`、`previous_response_id`、または `auto_previous_response_id` を使用して OpenAI のサーバー管理の会話状態を利用している場合、フックは次の Responses API 呼び出し用に準備されたペイロードに対して実行されます。そのペイロードは、以前の履歴全体の再現ではなく、新しいターンの差分のみをすでに表している場合があります。返された項目だけが、そのサーバー管理の継続処理に送信済みとしてマークされます。

機密データの編集、長い履歴の短縮、追加のシステムガイダンスの挿入を行うには、`run_config` を介して実行ごとにフックを設定します。

## エラーと復旧

### エラーハンドラー

すべての `Runner` エントリーポイントは、エラー種別をキーとする dict の `error_handlers` を受け取ります。サポートされているキーは `"max_turns"`、`"model_refusal"`、`"invalid_final_output"` です。対応するエラーで実行を終了する代わりに、制御された最終出力を返す場合に使用します。

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

モデルメッセージがエージェントの structured な `output_type` に対する検証に失敗した場合、またはモデルが structured な最終メッセージを返さない場合は、`"invalid_final_output"` を使用します。ハンドラーはアプリケーション固有のフォールバックを返すことができ、SDK は同じ `output_type` に対してそれを検証します。モデル呼び出しの再試行や、ツールの副作用の再実行は行いません。`None` を返すと、復旧を行いません。フォールバックがない場合、空でないレスポンスの検証失敗では引き続き `ModelBehaviorError` が発生し、空の structured レスポンスでは既存の次ターンの動作が維持されます。

```python
from pydantic import BaseModel

from agents import Agent, ModelBehaviorError, RunErrorHandlerInput, Runner


class Recipe(BaseModel):
    ingredients: list[str]
    recovered_from_invalid_output: bool = False


def on_invalid_final_output(data: RunErrorHandlerInput[None]) -> Recipe:
    assert isinstance(data.error, ModelBehaviorError)
    return Recipe(ingredients=[], recovered_from_invalid_output=True)


agent = Agent(
    name="Recipe assistant",
    instructions="Return a structured recipe.",
    output_type=Recipe,
)

result = Runner.run_sync(
    agent,
    "Plan tonight's dinner.",
    error_handlers={"invalid_final_output": on_invalid_final_output},
)
print(result.final_output)
```

フォールバック出力を会話履歴に追加しない場合は、`include_in_history=False` を設定します。

モデルによる拒否が発生した際に、`ModelRefusalError` で実行を終了する代わりにアプリケーション固有のフォールバックを生成する場合は、`"model_refusal"` を使用します。

```python
from pydantic import BaseModel

from agents import Agent, ModelRefusalError, RunErrorHandlerInput, Runner


class Recipe(BaseModel):
    ingredients: list[str]
    refusal_reason: str | None = None


def on_model_refusal(data: RunErrorHandlerInput[None]) -> Recipe:
    assert isinstance(data.error, ModelRefusalError)
    return Recipe(ingredients=[], refusal_reason=data.error.refusal)


agent = Agent(
    name="Recipe assistant",
    instructions="Return a structured recipe.",
    output_type=Recipe,
)

result = Runner.run_sync(
    agent,
    "Make me something unsafe.",
    error_handlers={"model_refusal": on_model_refusal},
)
print(result.final_output)
```

## 永続的な実行の統合とヒューマンインザループ

ツール承認の一時停止／再開パターンについては、専用の[ヒューマンインザループガイド](human_in_the_loop.md)から参照してください。以下の統合は、実行が長時間の待機、再試行、プロセスの再起動にまたがる可能性がある場合の永続的なオーケストレーションを目的としています。

### Dapr

Agents SDK の [Dapr](https://dapr.io) Diagrid 統合を使用すると、ヒューマンインザループをサポートし、障害から自動的に復旧する、永続的かつ長時間実行されるエージェントを実行できます。Dapr はベンダー中立の [CNCF](https://cncf.io) ワークフローオーケストレーターです。Dapr と OpenAI エージェントの使用を開始するには、[こちら](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai)を参照してください。

### Temporal

Agents SDK の [Temporal](https://temporal.io/) 統合を使用すると、ヒューマンインザループのタスクを含む、永続的で長時間実行されるワークフローを実行できます。Temporal と Agents SDK が連携して長時間実行タスクを完了するデモについては、[この動画](https://www.youtube.com/watch?v=fFBZqzT4DD8)を参照してください。また、[ドキュメントはこちら](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)から確認できます。

### Restate

Agents SDK の [Restate](https://restate.dev/) 統合を使用すると、人による承認、ハンドオフ、セッション管理を含む、軽量で永続的なエージェントを利用できます。この統合では、Restate の単一バイナリランタイムが依存関係として必要です。また、エージェントをプロセス／コンテナまたはサーバーレス関数として実行できます。詳細については、[概要](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)または[ドキュメント](https://docs.restate.dev/ai)を参照してください。

### DBOS

Agents SDK の [DBOS](https://dbos.dev/) 統合を使用すると、障害や再起動が発生しても進行状況を保持する、信頼性の高いエージェントを実行できます。長時間実行されるエージェント、ヒューマンインザループのワークフロー、ハンドオフをサポートします。同期メソッドと非同期メソッドの両方に対応しています。この統合に必要なのは、SQLite または Postgres データベースのみです。詳細については、統合の[リポジトリ](https://github.com/dbos-inc/dbos-openai-agents)と[ドキュメント](https://docs.dbos.dev/integrations/openai-agents)を参照してください。

## 例外

SDK は特定の状況で例外を発生させます。完全な一覧は [`agents.exceptions`][] にあります。概要は次のとおりです。

- [`AgentsException`][agents.exceptions.AgentsException]：SDK 内で発生するすべての例外の基底クラスです。他のすべての具体的な例外は、この汎用型から派生します。
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]：エージェントの実行が、`Runner.run`、`Runner.run_sync`、または `Runner.run_streamed` メソッドに渡された `max_turns` 制限を超えた場合に発生する例外です。指定された対話ターン数以内にエージェントがタスクを完了できなかったことを示します。制限を無効にするには、`max_turns=None` を設定します。
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]：基盤となるモデル（LLM）が予期しない出力または無効な出力を生成した場合に発生する例外です。これには次のものが含まれます。
    - 不正な JSON：特に特定の `output_type` が定義されている場合に、モデルがツール呼び出しまたは直接出力で不正な JSON 構造を返した場合です。
    - 予期しないツール関連の失敗：モデルが想定された方法でツールを使用できなかった場合です。
- [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]：関数ツール呼び出しが設定されたタイムアウトを超え、そのツールが `timeout_behavior="raise_exception"` を使用している場合に発生する例外です。
- [`UserError`][agents.exceptions.UserError]：SDK を使用するコードを作成しているユーザーが、SDK の使用時に誤りを犯した場合に発生する例外です。通常は、不適切なコード実装、無効な設定、SDK API の誤用によって発生します。
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]：それぞれ、入力ガードレールまたは出力ガードレールの条件が満たされた場合に発生する例外です。入力ガードレールは処理前に受信メッセージをチェックし、出力ガードレールは配信前にエージェントの最終レスポンスをチェックします。