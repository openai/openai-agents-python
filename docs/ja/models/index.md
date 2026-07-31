---
search:
  exclude: true
---
# モデル

Agents SDK は、すぐに利用できる OpenAI モデルを 2 種類サポートしています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## モデル設定の選択

まず、設定に適した最もシンプルな方法を選択してください。

| 実現したいこと | 推奨される方法 | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | Responses モデルの経路でデフォルトの OpenAI プロバイダーを使用する | [OpenAI モデル](#openai-models) |
| websocket トランスポート経由で OpenAI Responses API を使用する | Responses モデルの経路を維持し、websocket トランスポートを有効にする | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| OpenAI がホストするサブエージェントを使用する | 試験的なホスト型マルチエージェントモデルを使用する | [ホスト型マルチエージェント](#hosted-multi-agent-experimental) |
| OpenAI 以外のプロバイダーを 1 つ使用する | 組み込みのプロバイダー統合ポイントから始める | [OpenAI 以外のモデル](#non-openai-models) |
| エージェント間でモデルまたはプロバイダーを組み合わせる | 実行単位またはエージェント単位でプロバイダーを選択し、機能の違いを確認する | [1 つのワークフローでのモデルの組み合わせ](#mixing-models-in-one-workflow)および[プロバイダーをまたぐモデルの組み合わせ](#mixing-models-across-providers) |
| OpenAI Responses の高度なリクエスト設定を調整する | OpenAI Responses の経路で `ModelSettings` を使用する | [OpenAI Responses の高度な設定](#advanced-openai-responses-settings) |
| OpenAI 以外のプロバイダーまたは複数プロバイダーのルーティングにサードパーティ製アダプターを使用する | サポート対象のベータ版アダプターを比較し、リリース予定のプロバイダー経路を検証する | [サードパーティ製アダプター](#third-party-adapters) |

## OpenAI モデル

OpenAI のみを使用するほとんどのアプリでは、デフォルトの OpenAI プロバイダーで文字列のモデル名を使用し、Responses モデルの経路を維持することを推奨します。

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使用されます。現在のデフォルトは、低レイテンシーのエージェントワークフロー向けに `reasoning.effort="none"` と `verbosity="low"` を設定した [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) です。利用可能な場合は、明示的な `model_settings` を維持しながら、品質向上のためにエージェントを `gpt-5.6-sol` に設定することを推奨します。

`gpt-5.6-sol` などの別のモデルに切り替える場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルが設定されていないすべてのエージェントで特定のモデルを一貫して使用するには、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

次に、`RunConfig` を使用して実行のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、その実行のモデルが使用されます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.6-sol"),
)
```

#### GPT-5 モデル

この方法で `gpt-5.6-sol` などの GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースに最適な設定が適用されます。デフォルトモデルの推論エフォートを調整するには、独自の `ModelSettings` を渡します。

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.6-sol is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.6-sol",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

レイテンシーを低減するには、GPT-5 モデルで `reasoning.effort="none"` を使用することを推奨します。

GPT-5.6 は、既存の `reasoning` 設定を通じて、推論モード、永続化された推論コンテキスト、および `"max"` エフォートレベルもサポートします。これらの制御は Responses API の経路で利用できます。

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

agent = Agent(
    name="Deep research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        reasoning=Reasoning(
            mode="pro",
            effort="max",
            context="all_turns",
        ),
    ),
)
```

`reasoning.mode` と `reasoning.context` は Responses 専用の設定です。Chat Completions は `reasoning.effort` のみを使用し、サポートされるエフォートレベルはモデルと API サーフェスによって異なります。GPT-5.6 の `"max"` エフォートには Responses API を使用してください。Chat Completions アダプターは警告を出してモードとコンテキストを無視します。その警告をエラーにするには、OpenAI プロバイダーで `strict_feature_validation=True` を設定します。

`context="all_turns"` を使用する場合は、`previous_response_id`、サーバー側の会話、または以前の推論項目の再送により会話を維持します。ステートレスな `store=False` 呼び出しでは、レスポンスに `reasoning.encrypted_content` を含め、次のリクエストでそれらの推論項目を再送してください。

#### ComputerTool のモデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、SDK が送信するコンピューターツールのペイロードが決まります。明示的な `gpt-5.5` リクエストでは GA の組み込み `computer` ツールが使用されますが、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` ペイロードが維持されます。

主な例外は、プロンプトによって管理される呼び出しです。プロンプトテンプレートがモデルを保持し、SDK がリクエストから `model` を省略する場合、プロンプトに固定されたモデルを SDK が推測しないように、プレビュー互換のコンピューターペイロードがデフォルトで使用されます。このフローで GA の経路を維持するには、リクエストで `model="gpt-5.5"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、および `"computer_use_preview"` は、有効なリクエストモデルに対応する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は引き続き通常の関数名として動作します。

プレビュー互換リクエストでは、`environment` と表示サイズを事前にシリアライズする必要があります。そのため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使用するプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool)を参照してください。

#### GPT-5 以外のモデル

カスタムの `model_settings` を指定せずに GPT-5 以外のモデル名を渡すと、SDK は任意のモデルと互換性のある汎用的な `ModelSettings` に戻ります。

### Responses 専用のツール機能

次のツール機能は、OpenAI Responses モデルでのみサポートされています。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` およびその他の遅延読み込み対応 Responses ツールサーフェス
-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]、`allowed_callers`、および `tool_choice="programmatic_tool_calling"`

これらの機能は、Chat Completions モデルおよび Responses 以外のバックエンドでは拒否されます。遅延読み込みツールを使用する場合は、エージェントに `ToolSearchTool()` を追加し、修飾されていない名前空間名や遅延読み込み専用の関数名を強制するのではなく、`auto` または `required` のツール選択を通じてモデルにツールを読み込ませます。設定の詳細と現在の制約については、[ホスト型ツール検索](../tools.md#hosted-tool-search)および[プログラムによるツール呼び出し](../tools.md#programmatic-tool-calling)を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI ベースのモデルを使用する場合は、websocket トランスポートを有効にできます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデル（`"gpt-5.6-sol"` などの文字列モデル名を含む）に影響します。

トランスポートの選択は、SDK がモデル名をモデルインスタンスに解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合、グローバルなデフォルトではなく、そのプロバイダーがトランスポートの選択を制御します。

#### プロバイダー単位または実行単位の設定

プロバイダー単位または実行単位で websocket トランスポートを設定することもできます。

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
    # Optional low-level websocket keepalive settings.
    responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

OpenAI ベースのプロバイダーでは、オプションのエージェント登録設定も使用できます。これは、OpenAI の設定でハーネス ID などのプロバイダー単位の登録メタデータが必要な場合に使用する高度なオプションです。

```python
from agents import (
    Agent,
    OpenAIAgentRegistrationConfig,
    OpenAIProvider,
    RunConfig,
    Runner,
)

provider = OpenAIProvider(
    use_responses_websocket=True,
    agent_registration=OpenAIAgentRegistrationConfig(harness_id="your-harness-id"),
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

#### `MultiProvider` による高度なルーティング

プレフィックスベースのモデルルーティングが必要な場合（たとえば、1 回の実行で `openai/...` と `any-llm/...` のモデル名を組み合わせる場合）、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` には、歴史的なデフォルトが 2 つあります。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスは、そのまま渡されるのではなく `UserError` を発生させます。

OpenAI プロバイダーを、リテラルの名前空間付きモデル ID を必要とする OpenAI 互換エンドポイントに接続する場合は、パススルー動作を明示的に有効にします。websocket を有効にした設定では、`MultiProvider` にも `openai_use_responses_websocket=True` を設定してください。

```python
from agents import Agent, MultiProvider, RunConfig, Runner

provider = MultiProvider(
    openai_base_url="https://openrouter.ai/api/v1",
    openai_api_key="...",
    openai_use_responses_websocket=True,
    openai_prefix_mode="model_id",
    unknown_prefix_mode="model_id",
)

agent = Agent(
    name="Assistant",
    instructions="Be concise.",
    model="openai/gpt-4.1",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

バックエンドがリテラルの `openai/...` 文字列を必要とする場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` など、その他の名前空間付きモデル ID を必要とする場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは、websocket トランスポート外の `MultiProvider` でも機能します。この例では、このセクションで説明しているトランスポート設定の一部であるため、websocket を有効なままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` を通じてルーティングする際に、同じプロバイダー単位の登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡すと、基盤となる OpenAI プロバイダーに転送されます。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使用する場合、websocket トランスポートにも互換性のある websocket `/responses` エンドポイントが必要です。このような設定では、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注意事項

-   これは websocket トランスポート経由の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や OpenAI 以外のプロバイダーには、Responses websocket `/responses` エンドポイントをサポートしていない限り適用されません。
-   環境にまだ存在しない場合は、`websockets` パッケージをインストールしてください。
-   websocket トランスポートを有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで、ターン間（およびネストされた Agents-as-tools 呼び出し）に同じ websocket 接続を再利用する場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md)ガイドおよび [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。
-   長時間の推論ターンやレイテンシーの急増が発生するネットワークでは、`responses_websocket_options` を使用して websocket のキープアライブ動作をカスタマイズします。遅延した pong フレームを許容するには `ping_timeout` を増やすか、ping を有効にしたままハートビートのタイムアウトを無効にするには `ping_timeout=None` を設定します。websocket のレイテンシーより信頼性が重要な場合は、HTTP/SSE トランスポートを選択してください。
-   デフォルトでは、SDK は受信メッセージのサイズ制限を無効にします（`max_size=None`）。プロキシの背後にある長時間稼働エージェントプロセスや、メモリが制限されたコンテナでは、`responses_websocket_options={"max_size": 8 * 1024 * 1024}` を設定して、メッセージ単位のメモリ使用量に上限を設けます。
-   [Responses API WebSocket サービス](https://developers.openai.com/api/docs/guides/websocket-mode)は、各接続で一度に 1 つのレスポンスを処理し、各接続を 60 分に制限します。この制限に達したら新しい接続を開いてください。並列実行が必要な場合は、複数の接続を使用します。
-   サービスは、接続ローカルのメモリに最新のレスポンスのみを保持します。失敗した `4xx` または `5xx` のターンでは、参照された `previous_response_id` が削除されます。再接続後も、保存済みのレスポンスが利用可能であれば続行できますが、`store=False` および ZDR フローには永続化されたフォールバックがありません。`previous_response_id=None` を使用して新しいチェーンを開始し、完全な入力コンテキストを送信するか、ローカルで管理されるセッション状態からそのコンテキストを再構築してください。

### ホスト型マルチエージェント（試験的）

OpenAI Responses API のホスト型マルチエージェントベータでは、GPT-5.6 のルートモデルがサーバーでホストされるサブエージェントを作成して調整できます。Agents SDK は通常の `Runner` を引き続き使用できます。ホスト型オーケストレーションはサービス上で行われ、開発者が定義した関数ツールはアプリケーション内で実行されます。

この統合は試験的であり、ローカル関数の出力を `response.inject` によってアクティブなホスト型エージェントへ返せるように、Responses WebSocket トランスポートを使用します。`client.beta.responses.connect` を公開するベータビルドを含む `openai[realtime]>=2.45.0` が必要です。インターフェースとベータ版の項目スキーマは、一般提供前に変更される可能性があります。

#### モデルの設定

試験的モジュールからモデルをインポートし、SDK の `Agent` に割り当てます。

```python
from agents import Agent
from agents.extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel

agent = Agent(
    name="Research coordinator",
    instructions="Delegate independent research tasks, then synthesize the findings.",
    model=OpenAIHostedMultiAgentModel(model="gpt-5.6-sol", config={"max_concurrent_subagents": 3}),
)
```

`OpenAIHostedMultiAgentModel` を構築すると、`multi_agent.enabled` が有効になり、`OpenAI-Beta: responses_multi_agent=v1` WebSocket ヘッダーが送信されます。`openai_client` が指定されていない場合、モデルはデフォルトの OpenAI クライアントを使用します。`max_concurrent_subagents` を省略した場合は、サービスのデフォルトが使用されます。

#### ローカル関数ツール

すべてのホスト型エージェントは、リクエストに設定されたモデルとツールを共有します。どのホスト型エージェントが関数を呼び出すかは、Responses API が決定します。通常の SDK Runner は関数をローカルで実行し、同じ呼び出し ID を持つ `function_call_output` をアクティブな WebSocket レスポンスに注入します。これにより、サービスは元のホスト型呼び出し元を再開できます。関数の実行には、Runner の通常のガードレール、フック、および失敗変換が引き続き適用されます。SDK ツールの承認による中断はサポートされません。`needs_approval` 設定が `False` ではない関数ツールは、リクエストの送信前に拒否されます。

ツールで呼び出し元を考慮したログ記録または認可が必要な場合は、`get_hosted_agent_metadata()` を使用します。

```python
from typing import Any

from agents.decorators import tool
from agents.extensions.experimental.hosted_multi_agent import get_hosted_agent_metadata
from agents.tool_context import ToolContext

@tool
def lookup_document(ctx: ToolContext[Any], section: str) -> str:
    metadata = get_hosted_agent_metadata(ctx)
    caller = metadata.agent_name if metadata else "unknown"
    print(f"tool caller: {caller}; call ID: {ctx.tool_call_id}")
    return f"Contents for {section}"
```

ホスト型エージェントの名前は観測用メタデータであり、ローカルのルーティング機構ではありません。SDK が提供する呼び出し ID を使用して出力をルーティングしてください。副作用を伴うツールでは、その呼び出し ID を冪等性キーとして使用し、ツールの実行前または実行中に、必要な認可をアプリケーションコードで適用してください。このモデルでは `needs_approval` を使用しないでください。ツールの引数と出力は Responses API の境界を越えます。

#### 出力とストリーミングの動作

`final_answer` フェーズを持ち、`/root` に帰属するメッセージのみが通常の最終メッセージになります。試験的アダプターは、サブエージェントのメッセージとホスト型オーケストレーションのレコードを高レベルの `RunResult` から除外します。SDK がそれらのレコードをローカル関数として実行することはありません。

raw ストリーミングでは、ホスト型の出力項目や `response.inject.created` の確認応答を含む、ベータ版 Responses イベントが引き続き公開されます。アダプターは、関数呼び出しの準備が整った時点で、アクティブな 1 つのプロバイダーレスポンスを SDK から見える論理的なモデルターンに分割し、Runner が出力を生成した後に同じプロバイダーレスポンスを再開します。帰属情報を確認するには、raw のホスト型項目または `ToolContext` とともに `get_hosted_agent_metadata()` を使用します。

#### SDK オーケストレーションとの関係

ホスト型マルチエージェントは、SDK のハンドオフおよび Agents-as-tools とは別のものです。

-   ホスト型マルチエージェントは、OpenAI サービス上でサブエージェントを作成します。アプリケーションがそれらのサブエージェントを作成またはスケジュールすることはありません。
-   SDK のハンドオフは、アクティブなローカル SDK `Agent` を変更します。この試験的モデルを使用する場合は、すべてのホスト型エージェントが同じハンドオフツールを受け取り、所有権の競合が発生するため、ハンドオフは拒否されます。
-   Agents-as-tools は引き続き利用できますが、使用するとクライアント側とサーバー側のオーケストレーションがネストされます。追加のレイテンシー、コスト、およびツールの公開範囲を慎重に評価してください。

#### 現在の制限事項

試験的モデルは、`reasoning.summary`、`max_tool_calls`、および呼び出し元が指定する `multi_agent` または `betas` のオーバーライドを拒否します。Responses の `/compact` エンドポイントはベータ版ではサポートされません。ただし、サービスがホスト型エージェントごとのコンテキストを個別に自動圧縮するため、明示的な `context_management.compact_threshold` は使用できます。

1 つの `OpenAIHostedMultiAgentModel` インスタンスが同時に保持できるアクティブなホスト型レスポンスは、最大 1 つです。ローカル関数の出力を待機している間に実行を中止した場合は、`await model.close()` を呼び出して WebSocket を解放してください。進行中のホスト型レスポンスを別のプロセスまたはイベントループで復元することは、現在サポートされていません。

基盤となる Responses API ベータ版の動作については、[OpenAI マルチエージェントガイド](https://developers.openai.com/api/docs/guides/tools-multi-agent)を参照してください。非ストリーミングおよびストリーミングでの SDK の使用方法については、[`examples/agent_patterns/hosted_multi_agent_beta.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/hosted_multi_agent_beta.py) を参照してください。

## OpenAI 以外のモデル

OpenAI 以外のプロバイダーが必要な場合は、SDK の組み込みプロバイダー統合ポイントから始めてください。多くの設定では、サードパーティ製アダプターを追加しなくても、これで十分です。各パターンのコード例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### OpenAI 以外のプロバイダーの統合方法

| 方法 | 使用する状況 | 適用範囲 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにする場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを単一の実行に適用する場合 | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | エージェントごとに異なるプロバイダーまたは具体的なモデルオブジェクトが必要な場合 | エージェント単位 |
| サードパーティ製アダプター | 組み込みの経路では提供されない、アダプター管理のプロバイダーカバレッジまたはルーティングが必要な場合 | [サードパーティ製アダプター](#third-party-adapters)を参照 |

次の組み込みの経路を使用して、他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用する場合に便利です。これは、LLM プロバイダーに OpenAI 互換の API エンドポイントがあり、`base_url` と `api_key` を設定できる場合に使用します。設定可能なコード例については、[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで使用します。これにより、「この実行内のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定可能なコード例については、[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] を使用すると、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定可能なコード例については、[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` を使用してトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md)を設定することを推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらのコード例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API／モデルを使用しています。LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## 1 つのワークフローでのモデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小型で高速なモデルを使用し、複雑なタスクには大型で高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定する際は、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡します。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡します。
3. [`Model`][agents.models.interface.Model] の実装を直接指定します。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形式をサポートしていますが、この 2 つの形式ではサポートされる機能とツールが異なるため、ワークフローごとに 1 つのモデル形式を使用することを推奨します。ワークフローで複数のモデル形式を組み合わせる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

```python
import asyncio

from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model="gpt-5-mini", # (1)!
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIChatCompletionsModel( # (2)!
        model="gpt-5-nano",
        openai_client=AsyncOpenAI()
    ),
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model="gpt-5.6-sol",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

1.  OpenAI モデルの名前を直接設定します。
2.  [`Model`][agents.models.interface.Model] の実装を指定します。

エージェントで使用するモデルをさらに設定するには、temperature などのオプションのモデル設定パラメーターを提供する [`ModelSettings`][agents.model_settings.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## OpenAI Responses の高度な設定

OpenAI Responses の経路を使用していて、より詳細な制御が必要な場合は、まず `ModelSettings` を使用してください。

### 一般的な高度な `ModelSettings` オプション

OpenAI Responses API を使用する場合、いくつかのリクエストフィールドには対応する `ModelSettings` フィールドがすでに用意されているため、それらに `extra_args` を使用する必要はありません。

- `parallel_tool_calls`: 同じターン内で複数のツール呼び出しを許可または禁止します。
- `truncation`: コンテキストが上限を超える場合に失敗させるのではなく、Responses API に最も古い会話項目を削除させるには、`"auto"` を設定します。
- `store`: 生成されたレスポンスを後から取得できるよう、サーバー側に保存するかどうかを制御します。これは、レスポンス ID に依存する後続ワークフローや、`store=False` の場合にローカル入力へフォールバックする必要があるセッション圧縮フローに影響します。
- `context_management`: `compact_threshold` を使用した Responses の圧縮など、サーバー側のコンテキスト処理を設定します。
- `prompt_cache_retention`: 以前のモデルファミリー向けに、たとえば
  `"24h"` を使用して保持期間の延長を設定します。
- `prompt_cache_options`: 暗黙的または明示的なプロンプトキャッシュを選択し、GPT-5.6 では `"30m"` のキャッシュ TTL を設定します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より詳細なレスポンスペイロードをリクエストします。
- `top_logprobs`: 出力テキストの上位トークンの logprobs をリクエストします。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対する Runner 管理の再試行設定を有効にします。[Runner 管理の再試行](#runner-managed-retries)を参照してください。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_options={"mode": "explicit", "ttl": "30m"},
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

明示的なプロンプトキャッシュでは、再利用可能なプレフィックスの末尾にあるコンテンツ部分へブレークポイントを追加します。同じ `ModelSettings.prompt_cache_options` フィールドが Responses および Chat Completions のリクエストに渡され、Chat Completions コンバーターはテキスト、画像、音声、ファイルの各コンテンツ部分にあるブレークポイントを維持します。

```python
from agents import Runner

result = await Runner.run(
    research_agent,
    [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Reusable background material...",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {
                    "type": "input_text",
                    "text": "Analyze the latest question.",
                },
            ],
        }
    ],
)
```

`prompt_cache_retention` は、従来の保持制御を使用する以前のモデルファミリーでも引き続き利用できます。
`ModelSettings` の直接フィールドと、`extra_args` 内の同じキーを併用しないでください。

`store=False` を設定すると、Responses API はそのレスポンスを後からサーバー側で取得できるようには保持しません。これはステートレスまたはゼロデータ保持形式のフローに役立ちますが、通常であればレスポンス ID を再利用する機能が、代わりにローカルで管理される状態に依存する必要があることも意味します。たとえば、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されなかった場合、デフォルトの `"auto"` 圧縮経路を入力ベースの圧縮へ切り替えます。[セッションガイド](../sessions/index.md#openai-responses-compaction-sessions)を参照してください。

サーバー側の圧縮は、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] とは異なります。`context_management=[{"type": "compaction", "compact_threshold": ...}]` は各 Responses API リクエストとともに送信され、レンダリングされたコンテキストがしきい値を超えると、API はレスポンスの一部として圧縮項目を生成できます。`OpenAIResponsesCompactionSession` はターン間で独立した `responses.compact` エンドポイントを呼び出し、ローカルのセッション履歴を書き換えます。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

また、OpenAI の Responses API を使用する場合、[その他のオプションパラメーターもいくつかあります](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）。トップレベルで利用できない場合は、`extra_args` を使用してこれらを渡すこともできます。同じリクエストフィールドを `ModelSettings` の直接フィールドでも設定しないでください。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(
        temperature=0.1,
        extra_args={"service_tier": "flex", "user": "user_12345"},
    ),
)
```

## Runner 管理の再試行

再試行は実行時のみ有効で、明示的な有効化が必要です。`ModelSettings(retry=...)` を設定し、再試行ポリシーが再試行を選択しない限り、SDK は一般的なモデルリクエストを再試行しません。

Responses websocket トランスポートでは、`retry_policies.provider_suggested()` はレスポンス前の過負荷フレームと、コードのない `server_error` フレームを再試行の提案として認識します。これだけでは再試行は有効になりません。引き続き `ModelRetrySettings` が必要で、通常の再送安全性チェックも適用されます。レスポンスイベントが 1 つでも到着した後は、SDK はリクエストを再送しません。

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        retry=ModelRetrySettings(
            max_retries=4,
            backoff={
                "initial_delay": 0.5,
                "max_delay": 5.0,
                "multiplier": 2.0,
                "jitter": True,
            },
            policy=retry_policies.any(
                retry_policies.provider_suggested(),
                retry_policies.retry_after(),
                retry_policies.network_error(),
                retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
            ),
        )
    ),
)
```

`ModelRetrySettings` には 3 つのフィールドがあります。

<div class="field-table" markdown="1">

| フィールド | 型 | 注意事項 |
| --- | --- | --- |
| `max_retries` | `int | None` | 最初のリクエスト後に許可される再試行回数です。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずに再試行する場合の、デフォルトの遅延戦略です。`backoff.max_delay` は、この方法で計算されるバックオフ遅延のみを制限します。ポリシーが返す明示的な遅延や retry-after ヒントは制限しません。 |
| `policy` | `RetryPolicy | None` | 再試行するかどうかを決定するコールバックです。このフィールドは実行時専用であり、シリアライズされません。 |

</div>

再試行ポリシーは、次の情報を持つ [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。

- `attempt` と `max_retries`: 試行回数を考慮した判断に使用できます。
- `stream`: ストリーミングと非ストリーミングの動作を分岐できます。
- `error`: raw の内容を確認できます。
- `normalized`: `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの正規化された情報です。
- `provider_advice`: 基盤となるモデルアダプターが再試行のガイダンスを提供できる場合に設定されます。

ポリシーは次のいずれかを返せます。

- 単純に再試行を判断する `True`／`False`
- 遅延を上書きしたり診断用の理由を付加したりする場合の [`RetryDecision`][agents.retry.RetryDecision]

SDK は、`retry_policies` で既製のヘルパーを公開しています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常に再試行しません。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーの再試行アドバイスに従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害とタイムアウトに一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使用して再試行します。このヘルパーは retry-after 値を明示的なポリシー遅延として扱うため、`backoff.max_delay` はその値を制限しません。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかが再試行を選択した場合に再試行します。 |
| `retry_policies.all(...)` | ネストされたすべてのポリシーが再試行を選択した場合にのみ再試行します。 |

ポリシーを組み合わせる場合、プロバイダーが拒否判断と再送安全性の承認を区別できるときにそれらを維持するため、最初の構成要素としては `provider_suggested()` が最も安全です。

##### 安全性の境界

一部の障害は自動的に再試行されません。

- 中止エラー
- プロバイダーのアドバイスで再送が安全でないと判断されたリクエスト
- 出力がすでに開始され、再送が安全でなくなるストリーミング実行

`previous_response_id` または `conversation_id` を使用するステートフルな後続リクエストも、より慎重に扱われます。これらのリクエストでは、`network_error()` や `http_status([500])` など、プロバイダーに依存しない述語だけでは不十分です。再試行ポリシーには、通常は `retry_policies.provider_suggested()` を使用して、プロバイダーによる再送安全性の承認を含める必要があります。

##### Runner とエージェントのマージ動作

`retry` は、Runner レベルとエージェントレベルの `ModelSettings` の間でディープマージされます。

- エージェントは `retry.max_retries` のみを上書きしながら、Runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部のみを上書きしながら、Runner の他のバックオフフィールドを維持できます。
- `policy` は実行時専用であるため、シリアライズされた `ModelSettings` は `max_retries` と `backoff` を保持しますが、コールバック自体は省略します。

より完全なコード例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) および[アダプターベースの再試行コード例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)を参照してください。

## OpenAI 以外のプロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシングに関連するエラーが発生する場合、トレースが OpenAI サーバーにアップロードされる一方で、OpenAI API キーがないことが原因です。これを解決するには、次の 3 つの方法があります。

1. トレーシングを完全に無効にします: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用の OpenAI キーを設定します: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) で発行されたものである必要があります。
3. OpenAI 以外のトレースプロセッサーを使用します。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだ対応していません。その結果、404 エラーまたは同様の問題が発生する場合があります。これを解決するには、次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### Chat Completions の互換性オプション

Chat Completions 経由でルーティングする場合、SDK は、`previous_response_id`、`conversation_id`、プロンプト、テキストのみではないツール出力など、Chat Completions では送信できない Responses 専用フィールドを暗黙的に削除することで互換性を維持します。開発中にこのような不一致を即座にエラーにするには、OpenAI プロバイダーで厳格な機能検証を有効にします。

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses=False,
    strict_feature_validation=True,
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

[`MultiProvider`][agents.MultiProvider] を使用する場合は、代わりに `openai_strict_feature_validation=True` を渡します。

一部の OpenAI 互換 Chat Completions プロバイダーは、SDK が段階的に処理するには信頼性が不十分なチャンクで、ツール呼び出しの差分をストリーミングします。その場合は、ストリーミングされたツール呼び出しのバッファリングを有効にし、プロバイダーのストリームが完了した後にのみ SDK がツール呼び出しを生成するようにします。

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

[`MultiProvider`][agents.MultiProvider] では、`openai_buffer_streamed_tool_calls=True` を使用します。

### structured outputs のサポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていますが、出力に使用する `json_schema` を指定できません。この問題の修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーを使用することを推奨します。そうしない場合、不正な形式の JSON によってアプリが頻繁に動作しなくなる可能性があります。

## プロバイダーをまたぐモデルの組み合わせ

モデルプロバイダー間の機能差を把握しておかないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

-   対応していないプロバイダーへ、サポートされていない `tools` を送信しないでください
-   テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外してください
-   構造化 JSON 出力をサポートしていないプロバイダーは、無効な JSON を生成する場合があることに注意してください。

## サードパーティ製アダプター

サードパーティ製アダプターは、SDK の組み込みプロバイダー統合ポイントだけでは不十分な場合にのみ使用してください。この SDK で OpenAI モデルのみを使用する場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] の経路を推奨します。サードパーティ製アダプターは、OpenAI モデルと OpenAI 以外のプロバイダーを組み合わせる必要がある場合や、組み込みの経路では提供されない、アダプター管理のプロバイダーカバレッジまたはルーティングが必要な場合に使用します。アダプターは SDK と上流のモデルプロバイダーの間に互換性レイヤーを追加するため、サポートされる機能とリクエストのセマンティクスはプロバイダーによって異なる場合があります。現在、SDK にはベストエフォートのベータ版アダプター統合として Any-LLM と LiteLLM が含まれています。

### Any-LLM

Any-LLM のサポートは、Any-LLM が管理するプロバイダーカバレッジまたはルーティングが必要な場合に向けて、ベストエフォートのベータ版として提供されています。

上流のプロバイダー経路に応じて、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換性レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使用するか、`AnyLLMModel` を直接インスタンス化するか、実行スコープで `AnyLLMProvider` を使用できます。モデルサーフェスを明示的に固定する必要がある場合は、`AnyLLMModel` の構築時に `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM はサードパーティ製アダプターレイヤーであるため、プロバイダーの依存関係と機能差は SDK ではなく、上流の Any-LLM によって定義されます。上流のプロバイダーが使用量指標を返す場合、それらは自動的に伝播されます。ただし、ストリーミングされる Chat Completions バックエンドでは、使用量チャンクを生成する前に `ModelSettings(include_usage=True)` が必要になる場合があります。structured outputs、ツール呼び出し、使用量レポート、または Responses 固有の動作に依存する場合は、デプロイ予定のプロバイダーバックエンドを正確に検証してください。

### LiteLLM

LiteLLM のサポートは、LiteLLM 固有のプロバイダーカバレッジまたはルーティングが必要な場合に向けて、ベストエフォートのベータ版として提供されています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` モデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

LiteLLM ベースの一部のプロバイダーは、デフォルトでは SDK の使用量指標を設定しません。使用量レポートが必要な場合は、`ModelSettings(include_usage=True)` を渡してください。また、structured outputs、ツール呼び出し、使用量レポート、またはアダプター固有のルーティング動作に依存する場合は、デプロイ予定のプロバイダーバックエンドを正確に検証してください。

LiteLLM がレスポンスオブジェクトに対する Pydantic シリアライザー警告を生成する場合は、LiteLLM アダプターをインポートする前に、SDK の互換性パッチを有効にできます。

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

このパッチはデフォルトでは無効で、値が `1` または `true` の場合にのみ有効になります。プライベートな LiteLLM ロギングヘルパーをラップすることで、特定の種類の LiteLLM レスポンスシリアライズ警告を抑制します。そのため、一般的なシリアライズ設定ではなく、対象を限定した回避策として扱ってください。プライベートな LiteLLM API に依存しているため、LiteLLM をアップグレードする際には再度検証し、上流で警告が発生しなくなったら環境変数を削除してください。