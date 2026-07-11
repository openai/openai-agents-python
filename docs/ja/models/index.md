---
search:
  exclude: true
---
# モデル

Agents SDKには、すぐに利用できる OpenAIモデルのサポートが 2 種類用意されています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

設定に適した最もシンプルな方法から始めてください。

| 実現したいこと | 推奨される方法 | 詳細 |
| --- | --- | --- |
| OpenAIモデルのみを使用する | Responses モデルパスでデフォルトの OpenAIプロバイダーを使用する | [OpenAIモデル](#openai-models) |
| WebSocket トランスポート経由で OpenAI Responses API を使用する | Responses モデルパスを維持し、WebSocket トランスポートを有効にする | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| OpenAIがホストするサブエージェントを使用する | 試験的なホスト型マルチエージェントモデルを使用する | [ホスト型マルチエージェント](#hosted-multi-agent-experimental) |
| OpenAI以外のプロバイダーを 1 つ使用する | 組み込みのプロバイダー統合ポイントから始める | [OpenAI以外のモデル](#non-openai-models) |
| エージェント間でモデルまたはプロバイダーを組み合わせる | 実行ごと、またはエージェントごとにプロバイダーを選択し、機能の違いを確認する | [単一ワークフローでのモデルの組み合わせ](#mixing-models-in-one-workflow)および[プロバイダー間でのモデルの組み合わせ](#mixing-models-across-providers) |
| OpenAI Responses の高度なリクエスト設定を調整する | OpenAI Responses パスで `ModelSettings` を使用する | [OpenAI Responses の高度な設定](#advanced-openai-responses-settings) |
| OpenAI以外のプロバイダー、または複数プロバイダーのルーティングにサードパーティ製アダプターを使用する | サポートされているベータ版アダプターを比較し、リリース予定のプロバイダーパスを検証する | [サードパーティ製アダプター](#third-party-adapters) |

## OpenAIモデル

OpenAIのみを使用するほとんどのアプリでは、デフォルトの OpenAIプロバイダーで文字列のモデル名を使用し、Responses モデルパスを維持する方法を推奨します。

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使用されます。現在のデフォルトは、低レイテンシーのエージェントワークフロー向けに `reasoning.effort="none"` と `verbosity="low"` を設定した [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) です。利用できる場合は、明示的な `model_settings` を維持しながら、より高い品質を得るためにエージェントを `gpt-5.6-sol` に設定することを推奨します。

`gpt-5.6-sol` などの別のモデルに切り替える場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用するには、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

次に、`RunConfig` を使用して実行のデフォルトモデルを設定できます。エージェントにモデルを設定していない場合、この実行のモデルが使用されます。

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

この方法で `gpt-5.6-sol` などの GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースに最適な設定が使用されます。デフォルトモデルの推論エフォートを調整するには、独自の `ModelSettings` を渡します。

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

GPT-5.6 は、既存の `reasoning` 設定を通じて、推論モード、永続化された推論コンテキスト、および `"max"` エフォートレベルもサポートします。これらの制御は Responses API パスで利用できます。

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

`reasoning.mode` と `reasoning.context` は Responses 専用の設定です。Chat Completions では `reasoning.effort` のみが使用され、サポートされるエフォートレベルはモデルと API サーフェスによって異なります。GPT-5.6 の `"max"` エフォートには Responses API を使用してください。Chat Completions アダプターは警告を出してモードとコンテキストを無視します。その警告をエラーにするには、OpenAIプロバイダーで `strict_feature_validation=True` を設定します。

`context="all_turns"` を使用する場合は、`previous_response_id`、サーバー側の会話、または以前の推論項目の再現によって会話を保持してください。ステートレスな `store=False` 呼び出しでは、レスポンスに `reasoning.encrypted_content` を含め、次のリクエストでそれらの推論項目を再現してください。

#### ComputerTool のモデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれている場合、実際の Responses リクエストにおける実効モデルによって、SDK が送信するコンピューターツールのペイロードが決まります。明示的な `gpt-5.5` リクエストでは、GA の組み込み `computer` ツールが使用されます。一方、明示的な `computer-use-preview` リクエストでは、従来の `computer_use_preview` ペイロードが維持されます。

プロンプト管理の呼び出しが主な例外です。プロンプトテンプレートがモデルを所有し、SDK がリクエストから `model` を省略する場合、プロンプトが固定しているモデルを SDK が推測しないように、プレビュー互換のコンピューターペイロードがデフォルトで使用されます。このフローで GA パスを維持するには、リクエストで `model="gpt-5.5"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、実効リクエストモデルに一致する組み込みセレクターへ正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は引き続き通常の関数名として動作します。

プレビュー互換のリクエストでは、`environment` と表示サイズを事前にシリアライズする必要があります。そのため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使用するプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool)を参照してください。

#### GPT-5 以外のモデル

カスタム `model_settings` を指定せずに GPT-5 以外のモデル名を渡すと、SDK はすべてのモデルと互換性のある汎用的な `ModelSettings` に戻ります。

### Responses 専用のツール検索機能

次のツール機能は、OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` およびその他の遅延読み込み対応 Responses ツールサーフェス

これらの機能は、Chat Completions モデルおよび Responses 以外のバックエンドでは拒否されます。遅延読み込みツールを使用する場合は、エージェントに `ToolSearchTool()` を追加し、修飾されていない名前空間名や遅延読み込み専用の関数名を強制する代わりに、`auto` または `required` のツール選択を通じてモデルにツールを読み込ませます。設定の詳細と現在の制約については、[ツール](../tools.md#hosted-tool-search)を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAIを基盤とするモデルを使用する場合は、WebSocket トランスポートを有効にできます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAIプロバイダーによって解決される OpenAI Responses モデルに影響します。これには `"gpt-5.6-sol"` などの文字列モデル名も含まれます。

トランスポートの選択は、SDK がモデル名をモデルインスタンスへ解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は WebSocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合、グローバルなデフォルトではなく、そのプロバイダーがトランスポートの選択を制御します。

#### プロバイダー単位または実行単位の設定

プロバイダーごと、または実行ごとに WebSocket トランスポートを設定することもできます。

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

OpenAIを基盤とするプロバイダーでは、任意のエージェント登録設定も受け付けます。これは、ハーネス ID など、プロバイダー単位の登録メタデータを OpenAI設定が必要とする場合の高度なオプションです。

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

#### `MultiProvider` を使用した高度なルーティング

プレフィックスに基づくモデルルーティングが必要な場合、たとえば 1 回の実行で `openai/...` と `any-llm/...` のモデル名を組み合わせる場合は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` は、従来からの 2 つのデフォルト動作を維持します。

-   `openai/...` は OpenAIプロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスは、そのまま渡されるのではなく `UserError` を発生させます。

OpenAI互換エンドポイントが、名前空間付きのモデル ID を文字どおり受け取ることを想定している場合は、パススルー動作を明示的に有効にしてください。WebSocket を有効にした設定では、`MultiProvider` にも `openai_use_responses_websocket=True` を設定したままにします。

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

バックエンドが文字どおりの `openai/...` 文字列を想定している場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` など、その他の名前空間付きモデル ID を想定している場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは、WebSocket トランスポート以外の `MultiProvider` でも機能します。この例で WebSocket が有効になっているのは、このセクションで説明しているトランスポート設定の一部であるためです。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` を介してルーティングする際に同じプロバイダー単位の登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡します。これは基盤となる OpenAIプロバイダーへ転送されます。

カスタムの OpenAI互換エンドポイントまたはプロキシを使用する場合、WebSocket トランスポートには互換性のある WebSocket `/responses` エンドポイントも必要です。そのような設定では、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注意事項

-   これは WebSocket トランスポート経由の Responses API であり、[Realtime API](../realtime/guide.md)ではありません。Chat Completions、および Responses WebSocket `/responses` エンドポイントをサポートしていない OpenAI以外のプロバイダーには適用されません。
-   環境にまだ存在しない場合は、`websockets` パッケージをインストールしてください。
-   WebSocket トランスポートを有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで、ターン間およびネストされた agent-as-tool 呼び出し間で同じ WebSocket 接続を再利用する場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md)ガイドおよび [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。
-   長時間の推論ターンやレイテンシーが急増するネットワークでは、`responses_websocket_options` を使用して WebSocket のキープアライブ動作をカスタマイズします。遅延した pong フレームを許容するには `ping_timeout` を増やすか、ping を有効にしたままハートビートのタイムアウトを無効にするには `ping_timeout=None` を設定します。WebSocket のレイテンシーより信頼性が重要な場合は、HTTP/SSE トランスポートを優先してください。
-   デフォルトでは、SDK は受信メッセージのサイズ制限を無効にします（`max_size=None`）。プロキシの背後で長期間実行されるエージェントプロセスや、メモリ制約のあるコンテナでは、`responses_websocket_options={"max_size": 8 * 1024 * 1024}` を設定して、メッセージごとのメモリ使用量に上限を設けます。

### ホスト型マルチエージェント（試験的）

OpenAI Responses API のホスト型マルチエージェントベータでは、GPT-5.6 のルートモデルがサーバー上でホストされるサブエージェントを作成し、調整できます。Agents SDKは通常の `Runner` を引き続き使用できます。ホスト型オーケストレーションはサービス上で行われ、開発者が定義した関数ツールはアプリケーション内で実行されます。

この統合は試験的であり、ローカル関数の出力を `response.inject` を使用してアクティブなホスト型エージェントへ返せるように、Responses WebSocket トランスポートを使用します。`client.beta.responses.connect` を公開するベータビルドを含む `openai[realtime]>=2.45.0` が必要です。インターフェースとベータ項目のスキーマは、一般提供までに変更される可能性があります。

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

`OpenAIHostedMultiAgentModel` を構築すると、`multi_agent.enabled` が有効になり、`OpenAI-Beta: responses_multi_agent=v1` WebSocket ヘッダーが送信されます。`openai_client` が指定されていない場合、モデルはデフォルトの OpenAIクライアントを使用します。`max_concurrent_subagents` を省略すると、サービスのデフォルト値が使用されます。

#### ローカル関数ツール

すべてのホスト型エージェントは、リクエストに設定されたモデルとツールを共有します。どのホスト型エージェントが関数を呼び出すかは、Responses API が決定します。通常の SDK Runner は関数をローカルで実行し、同じ呼び出し ID を持つ `function_call_output` をアクティブな WebSocket レスポンスへ注入します。これにより、サービスは元のホスト型呼び出し元を再開できます。関数の実行には、引き続き Runner の通常のガードレール、フック、失敗変換が適用されます。SDK のツール承認による中断はサポートされません。`needs_approval` 設定が `False` ではない関数ツールは、リクエスト送信前に拒否されます。

ツールで呼び出し元を認識したロギングまたは認可が必要な場合は、`get_hosted_agent_metadata()` を使用します。

```python
from typing import Any

from agents import function_tool
from agents.extensions.experimental.hosted_multi_agent import get_hosted_agent_metadata
from agents.tool_context import ToolContext

@function_tool
def lookup_document(ctx: ToolContext[Any], section: str) -> str:
    metadata = get_hosted_agent_metadata(ctx)
    caller = metadata.agent_name if metadata else "unknown"
    print(f"tool caller: {caller}; call ID: {ctx.tool_call_id}")
    return f"Contents for {section}"
```

ホスト型エージェントの名前は観測用メタデータであり、ローカルルーティングの仕組みではありません。SDK が提供する呼び出し ID を使用して出力をルーティングしてください。副作用を伴うツールでは、その呼び出し ID を冪等性キーとして使用し、ツールの実行前または実行中に、必要な認可をアプリケーションコードで適用してください。このモデルでは `needs_approval` を使用しないでください。ツールの引数と出力は Responses API の境界を越えます。

#### 出力とストリーミングの動作

フェーズが `final_answer` であり、`/root` に帰属するメッセージのみが通常の最終メッセージになります。試験的アダプターは、サブエージェントのメッセージとホスト型オーケストレーションのレコードを高レベルの `RunResult` から除外します。SDK がそれらのレコードをローカル関数として実行することはありません。

raw ストリーミングでは、ホスト型出力項目や `response.inject.created` 確認応答を含むベータ版 Responses イベントが引き続き公開されます。アダプターは、関数呼び出しの準備が整うと、1 つのアクティブなプロバイダーレスポンスを SDK から見える論理的なモデルターンに分割します。その後、Runner が出力を生成すると、同じプロバイダーレスポンスを再開します。帰属情報を確認するには、raw のホスト型項目または `ToolContext` とともに `get_hosted_agent_metadata()` を使用します。

#### SDK オーケストレーションとの関係

ホスト型マルチエージェントは、SDK のハンドオフおよび agents-as-tools とは別のものです。

-   ホスト型マルチエージェントは、OpenAIサービス上でサブエージェントを作成します。アプリケーションがそれらのサブエージェントを作成またはスケジュールすることはありません。
-   SDK のハンドオフは、アクティブなローカル SDK `Agent` を変更します。この試験的モデルを使用している場合、すべてのホスト型エージェントが同じハンドオフツールを受け取り、所有権の競合が発生するため、ハンドオフは拒否されます。
-   Agents-as-tools は引き続き利用できますが、使用するとクライアント側とサーバー側のオーケストレーションがネストされます。追加のレイテンシー、コスト、ツールの公開範囲を慎重に評価してください。

#### 現在の制限事項

試験的モデルは、`reasoning.summary`、`max_tool_calls`、および呼び出し元が指定する `multi_agent` または `betas` のオーバーライドを拒否します。Responses の `/compact` エンドポイントはベータ版でサポートされていません。ただし、サービスが各ホスト型エージェントのコンテキストを個別に自動圧縮するため、明示的な `context_management.compact_threshold` は使用できます。

1 つの `OpenAIHostedMultiAgentModel` インスタンスが同時に保持できるアクティブなホスト型レスポンスは、最大 1 つです。ローカル関数の出力を待機している間に実行が放棄された場合は、`await model.close()` を呼び出して WebSocket を解放してください。実行中のホスト型レスポンスを別のプロセスまたはイベントループで復元することは、現在サポートされていません。

基盤となる Responses API ベータの動作については、[OpenAI マルチエージェントガイド](https://developers.openai.com/api/docs/guides/tools-multi-agent)を参照してください。非ストリーミングおよびストリーミングでの SDK の使用方法については、[`examples/agent_patterns/hosted_multi_agent_beta.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/hosted_multi_agent_beta.py) を参照してください。

## OpenAI以外のモデル

OpenAI以外のプロバイダーが必要な場合は、SDK の組み込みプロバイダー統合ポイントから始めてください。多くの設定では、サードパーティ製アダプターを追加しなくても、これで十分です。各パターンのコード例は、[examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### OpenAI以外のプロバイダーの統合方法

| アプローチ | 使用する場合 | 適用範囲 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにする場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを単一の実行に適用する場合 | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | エージェントごとに異なるプロバイダーまたは具体的なモデルオブジェクトが必要な場合 | エージェント単位 |
| サードパーティ製アダプター | 組み込みの方法では提供されない、アダプター管理のプロバイダーカバレッジまたはルーティングが必要な場合 | [サードパーティ製アダプター](#third-party-adapters)を参照 |

次の組み込み方法を使用して、他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用する場合に役立ちます。これは、LLM プロバイダーが OpenAI互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使用します。設定可能なコード例については、[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで使用します。これにより、「この実行内のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定可能なコード例については、[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] を使用すると、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使用できます。設定可能なコード例については、[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` を使用してトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md)を設定することを推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらのコード例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API/モデルを使用しています。LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## 単一ワークフローでのモデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小型で高速なモデルを使用し、複雑なタスクには大型で高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定する場合、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡します。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡します。
3. [`Model`][agents.models.interface.Model] の実装を直接指定します。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形式をサポートしていますが、2 つの形式でサポートされる機能とツールのセットが異なるため、ワークフローごとに 1 つのモデル形式を使用することを推奨します。ワークフローで複数のモデル形式を組み合わせる必要がある場合は、使用するすべての機能が両方で利用できることを確認してください。

```python
from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel
import asyncio

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
```

1.  OpenAIモデルの名前を直接設定します。
2.  [`Model`][agents.models.interface.Model] の実装を指定します。

エージェントで使用するモデルをさらに設定する場合は、temperature などの任意のモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses パスを使用していて、より詳細な制御が必要な場合は、`ModelSettings` から始めてください。

### 一般的な高度な `ModelSettings` オプション

OpenAI Responses API を使用する場合、いくつかのリクエストフィールドにはすでに対応する `ModelSettings` フィールドが用意されているため、それらに `extra_args` を使用する必要はありません。

- `parallel_tool_calls`: 同じターンで複数のツール呼び出しを許可または禁止します。
- `truncation`: コンテキストが上限を超える場合に失敗する代わりに、Responses API が最も古い会話項目を削除できるようにするには、`"auto"` を設定します。
- `store`: 生成されたレスポンスを、後で取得できるようにサーバー側へ保存するかどうかを制御します。これは、レスポンス ID に依存する後続ワークフローや、`store=False` の場合にローカル入力へのフォールバックが必要になる可能性があるセッション圧縮フローに関係します。
- `context_management`: `compact_threshold` を使用した Responses 圧縮など、サーバー側のコンテキスト処理を設定します。
- `prompt_cache_retention`: 以前のモデルファミリー向けに、たとえば
  `"24h"` を使用して保持期間の延長を設定します。
- `prompt_cache_options`: 暗黙的または明示的なプロンプトキャッシュを選択し、GPT-5.6 では `"30m"` のキャッシュ TTL を設定します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より詳細なレスポンスペイロードをリクエストします。
- `top_logprobs`: 出力テキストについて、上位トークンの logprobs をリクエストします。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対して Runner 管理の再試行設定を有効にします。[Runner 管理の再試行](#runner-managed-retries)を参照してください。

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

明示的なプロンプトキャッシュでは、再利用可能なプレフィックスの末尾にあるコンテンツ部分へブレークポイントを追加します。同じ `ModelSettings.prompt_cache_options` フィールドが Responses と Chat Completions のリクエストに渡され、Chat Completions コンバーターは、テキスト、画像、音声、ファイルのコンテンツ部分にあるブレークポイントを保持します。

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

`prompt_cache_retention` は、従来の保持制御を使用する以前のモデルファミリーでも引き続き
利用できます。直接指定する `ModelSettings` フィールドと同じキーを
`extra_args` に含めないでください。

`store=False` を設定すると、Responses API はそのレスポンスを後でサーバー側から取得できる状態で保持しません。これはステートレスまたはゼロデータ保持形式のフローに役立ちますが、通常であればレスポンス ID を再利用する機能が、代わりにローカルで管理された状態へ依存する必要があることも意味します。たとえば、最後のレスポンスが保存されていない場合、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、デフォルトの `"auto"` 圧縮パスを入力ベースの圧縮へ切り替えます。[セッションガイド](../sessions/index.md#openai-responses-compaction-sessions)を参照してください。

サーバー側の圧縮は、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] とは異なります。`context_management=[{"type": "compaction", "compact_threshold": ...}]` は Responses API リクエストごとに送信され、レンダリングされたコンテキストがしきい値を超えると、API はレスポンスの一部として圧縮項目を出力できます。`OpenAIResponsesCompactionSession` は、ターン間で独立した `responses.compact` エンドポイントを呼び出し、ローカルのセッション履歴を書き換えます。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

また、OpenAIの Responses API を使用する場合、[他にもいくつかの任意パラメーターがあります](https://platform.openai.com/docs/api-reference/responses/create)（たとえば `user`、`service_tier` など）。トップレベルで利用できない場合は、`extra_args` を使用して渡すこともできます。同じリクエストフィールドを、直接指定する `ModelSettings` フィールドでも設定しないでください。

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

再試行は実行時のみ有効で、オプトイン方式です。`ModelSettings(retry=...)` を設定し、再試行ポリシーが再試行を選択しない限り、SDK は一般的なモデルリクエストを再試行しません。

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

| フィールド | 型 | 注記 |
| --- | --- | --- |
| `max_retries` | `int | None` | 最初のリクエスト後に許可される再試行回数です。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずに再試行する場合の、デフォルトの遅延戦略です。`backoff.max_delay` は、計算されたこのバックオフ遅延のみに上限を設定します。ポリシーが返す明示的な遅延や retry-after ヒントには上限を設定しません。 |
| `policy` | `RetryPolicy | None` | 再試行するかどうかを決定するコールバックです。このフィールドは実行時のみ有効で、シリアライズされません。 |

</div>

再試行ポリシーは、次の情報を持つ [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。

- `attempt` と `max_retries`: 試行回数を考慮した判断に使用できます。
- `stream`: ストリーミング動作と非ストリーミング動作を分岐できます。
- `error`: raw の内容を調べるために使用します。
- `normalized`: `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの正規化された情報です。
- `provider_advice`: 基盤となるモデルアダプターが再試行に関するガイダンスを提供できる場合に設定されます。

ポリシーは、次のいずれかを返せます。

- 単純な再試行判断を行う場合は、`True` / `False`。
- 遅延を上書きするか、診断用の理由を付加する場合は、[`RetryDecision`][agents.retry.RetryDecision]。

SDK は、`retry_policies` でそのまま利用できるヘルパーを公開しています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常に再試行を行いません。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーの再試行アドバイスに従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害およびタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 選択された HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使用して再試行します。このヘルパーは retry-after 値を明示的なポリシー遅延として扱うため、`backoff.max_delay` による上限は適用されません。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかが再試行を選択した場合に再試行します。 |
| `retry_policies.all(...)` | ネストされたすべてのポリシーが再試行を選択した場合にのみ再試行します。 |

ポリシーを組み合わせる場合、プロバイダーが再実行の安全性を区別できるときに、プロバイダーによる拒否と再実行の安全性に関する承認を維持できるため、`provider_suggested()` が最も安全な最初の基本要素です。

##### 安全性の境界

一部の失敗は自動的に再試行されません。

- 中止エラー。
- プロバイダーのアドバイスにより、再実行が安全ではないと示されたリクエスト。
- 出力がすでに開始され、再実行が安全でなくなる状態になったストリーミング実行。

`previous_response_id` または `conversation_id` を使用するステートフルな後続リクエストも、より保守的に扱われます。これらのリクエストでは、`network_error()` や `http_status([500])` など、プロバイダー由来ではない条件だけでは不十分です。再試行ポリシーには、通常 `retry_policies.provider_suggested()` を通じて、プロバイダーから再実行が安全であることの承認を含める必要があります。

##### Runner とエージェントのマージ動作

`retry` は、Runner レベルとエージェントレベルの `ModelSettings` の間でディープマージされます。

- エージェントは `retry.max_retries` のみを上書きし、Runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部のみを上書きし、Runner の他のバックオフフィールドを維持できます。
- `policy` は実行時のみ有効であるため、シリアライズされた `ModelSettings` には `max_retries` と `backoff` が保持されますが、コールバック自体は含まれません。

より詳しいコード例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) および[アダプターを使用した再試行のコード例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)を参照してください。

## OpenAI以外のプロバイダーのトラブルシューティング

### トレーシングクライアントの 401 エラー

トレーシングに関連するエラーが発生する場合、トレースが OpenAIサーバーへアップロードされる一方で、OpenAI API キーがないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効にします: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAIキーを設定します: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) から取得したものである必要があります。
3. OpenAI以外のトレースプロセッサーを使用します。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 または同様の問題が発生する場合があります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数を通じて `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### Chat Completions の互換性オプション

Chat Completions を介してルーティングする場合、SDK は、`previous_response_id`、`conversation_id`、プロンプト、テキスト以外を含むツール出力など、Chat Completions では送信できない Responses 専用フィールドを暗黙的に削除することで互換性を維持します。開発中にこれらの不一致を早期にエラーにしたい場合は、OpenAIプロバイダーで厳密な機能検証を有効にします。

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

一部の OpenAI互換 Chat Completions プロバイダーは、SDK が増分処理するには十分な信頼性がないチャンク形式で、ツール呼び出しの差分をストリーミングします。その場合は、ストリーミングされたツール呼び出しのバッファリングを有効にし、プロバイダーのストリームが終了した後にのみ SDK がツール呼び出しを出力するようにします。

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

[`MultiProvider`][agents.MultiProvider] では、`openai_buffer_streamed_tool_calls=True` を使用します。

### structured outputs のサポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs)をサポートしていません。その結果、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力には対応していますが、出力に使用する `json_schema` を指定できません。この問題の修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを利用することを推奨します。そうしないと、不正な形式の JSON によってアプリが頻繁に動作しなくなる可能性があります。

## プロバイダー間でのモデルの組み合わせ

モデルプロバイダー間の機能差を把握しておく必要があります。そうしないと、エラーが発生する可能性があります。たとえば、OpenAIは structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

-   サポートされていない `tools` を、それらを解釈できないプロバイダーへ送信しないでください
-   テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外してください
-   構造化された JSON 出力をサポートしていないプロバイダーでは、無効な JSON が生成される場合があることに注意してください。

## サードパーティ製アダプター

SDK の組み込みプロバイダー統合ポイントでは不十分な場合にのみ、サードパーティ製アダプターを使用してください。この SDK で OpenAIモデルのみを使用する場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] パスを優先してください。サードパーティ製アダプターは、OpenAIモデルと OpenAI以外のプロバイダーを組み合わせる必要がある場合、または組み込みの方法では提供されない、アダプター管理のプロバイダーカバレッジやルーティングが必要な場合に使用します。アダプターは SDK と上流のモデルプロバイダーの間に互換性レイヤーを追加するため、機能のサポートとリクエストのセマンティクスはプロバイダーによって異なる場合があります。SDK には現在、ベストエフォート方式のベータ版アダプター統合として Any-LLM と LiteLLM が含まれています。

### Any-LLM

Any-LLM のサポートは、Any-LLM が管理するプロバイダーカバレッジまたはルーティングが必要な場合に向けて、ベストエフォート方式のベータ版として含まれています。

上流のプロバイダーパスに応じて、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換性レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使用するか、`AnyLLMModel` を直接インスタンス化するか、実行スコープで `AnyLLMProvider` を使用できます。モデルサーフェスを明示的に固定する必要がある場合は、`AnyLLMModel` の構築時に `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM は引き続きサードパーティ製アダプターレイヤーであるため、プロバイダーの依存関係と機能差は SDK ではなく、上流の Any-LLM によって定義されます。上流のプロバイダーが使用量指標を返す場合、それらは自動的に伝播されます。ただし、ストリーミング対応の Chat Completions バックエンドでは、使用量チャンクを出力する前に `ModelSettings(include_usage=True)` が必要になる場合があります。structured outputs、ツール呼び出し、使用量レポート、または Responses 固有の動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。

### LiteLLM

LiteLLM のサポートは、LiteLLM 固有のプロバイダーカバレッジまたはルーティングが必要な場合に向けて、ベストエフォート方式のベータ版として含まれています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` モデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

LiteLLM を基盤とする一部のプロバイダーは、デフォルトでは SDK の使用量指標を設定しません。使用量レポートが必要な場合は、`ModelSettings(include_usage=True)` を渡してください。また、structured outputs、ツール呼び出し、使用量レポート、またはアダプター固有のルーティング動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。