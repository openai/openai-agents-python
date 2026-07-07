---
search:
  exclude: true
---
# モデル

Agents SDK には、すぐに使える OpenAI モデルのサポートが 2 種類用意されています:

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

セットアップに合う最もシンプルな方法から始めてください:

| 目的 | 推奨パス | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | デフォルトの OpenAI プロバイダーを Responses モデルパスで使用する | [OpenAI モデル](#openai-models) |
| websocket トランスポート経由で OpenAI Responses API を使用する | Responses モデルパスを維持し、websocket トランスポートを有効にする | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 1 つの非 OpenAI プロバイダーを使用する | 組み込みのプロバイダー統合ポイントから始める | [非 OpenAI モデル](#non-openai-models) |
| エージェント間でモデルまたはプロバイダーを混在させる | 実行ごとまたはエージェントごとにプロバイダーを選択し、機能差異を確認する | [1 つのワークフロー内でのモデルの混在](#mixing-models-in-one-workflow) と [プロバイダー間でのモデルの混在](#mixing-models-across-providers) |
| 高度な OpenAI Responses リクエスト設定を調整する | OpenAI Responses パスで `ModelSettings` を使用する | [高度な OpenAI Responses 設定](#advanced-openai-responses-settings) |
| 非 OpenAI または混在プロバイダーのルーティングにサードパーティアダプターを使用する | サポートされているベータ版アダプターを比較し、出荷予定のプロバイダーパスを検証する | [サードパーティアダプター](#third-party-adapters) |

## OpenAI モデル

ほとんどの OpenAI のみのアプリでは、デフォルトの OpenAI プロバイダーで文字列のモデル名を使用し、Responses モデルパスを維持する方法をお勧めします。

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使用されます。デフォルトは現在、低レイテンシのエージェントワークフロー向けに `reasoning.effort="none"` と `verbosity="low"` を指定した [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) です。アクセス権がある場合は、明示的な `model_settings` を維持しながら、より高い品質のためにエージェントを [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) に設定することをお勧めします。

[`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.5
python3 my_awesome_agent.py
```

次に、`RunConfig` を通じて実行のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使用されます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.5"),
)
```

#### GPT-5 モデル

この方法で [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) など任意の GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースで最も適切に機能する設定が適用されます。デフォルトモデルの推論 effort を調整するには、独自の `ModelSettings` を渡します:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.5 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.5",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

低レイテンシにするには、GPT-5 モデルで `reasoning.effort="none"` を使用することをお勧めします。

#### ComputerTool モデルの選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエスト上の有効なモデルによって、SDK が送信する computer-tool ペイロードが決まります。明示的な `gpt-5.5` リクエストでは GA 組み込みの `computer` ツールが使用され、明示的な `computer-use-preview` リクエストでは古い `computer_use_preview` ペイロードが維持されます。

主な例外は、プロンプト管理の呼び出しです。プロンプトテンプレートがモデルを所有し、SDK がリクエストから `model` を省略する場合、SDK はプロンプトがどのモデルを固定しているかを推測しないよう、プレビュー互換のコンピューターペイロードをデフォルトにします。そのフローで GA パスを維持するには、リクエストで `model="gpt-5.5"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

登録済みの [`ComputerTool`][agents.tool.ComputerTool] がある場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として引き続き動作します。

プレビュー互換リクエストでは、`environment` と表示サイズを事前にシリアライズする必要があるため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリを使用するプロンプト管理フローでは、具象の `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool)を参照してください。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで非 GPT-5 モデル名を渡すと、SDK は任意のモデルと互換性のある汎用の `ModelSettings` に戻ります。

### Responses 専用のツール検索機能

次のツール機能は、OpenAI Responses モデルでのみサポートされています:

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` と、その他の遅延読み込みの Responses ツールサーフェス

これらの機能は、Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。遅延読み込みツールを使用する場合は、`ToolSearchTool()` をエージェントに追加し、素の名前空間名や遅延専用の関数名を強制する代わりに、`auto` または `required` のツール選択を通じてモデルにツールを読み込ませます。設定の詳細と現在の制約については、[ツール](../tools.md#hosted-tool-search)を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI を基盤とするモデルを使用する場合、websocket トランスポートをオプトインできます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデル（`"gpt-5.5"` などの文字列モデル名を含む）に影響します。

トランスポートの選択は、SDK がモデル名をモデルインスタンスへ解決するときに行われます。具象の [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket を使用し、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合、グローバルデフォルトではなく、そのプロバイダーがトランスポート選択を制御します。

#### プロバイダーまたは実行レベルの設定

websocket トランスポートは、プロバイダーごと、または実行ごとにも設定できます:

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

OpenAI を基盤とするプロバイダーは、任意のエージェント登録設定も受け付けます。これは、OpenAI のセットアップで harness ID などのプロバイダーレベルの登録メタデータが想定される場合向けの高度なオプションです。

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

プレフィックスベースのモデルルーティングが必要な場合（たとえば、1 つの実行内で `openai/...` と `any-llm/...` のモデル名を混在させる場合）は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` は、歴史的なデフォルトを 2 つ維持しています:

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスは、パススルーされる代わりに `UserError` を発生させます。

リテラルな名前空間付きモデル ID を想定する OpenAI 互換エンドポイントに OpenAI プロバイダーを向ける場合は、パススルー動作を明示的にオプトインしてください。websocket が有効なセットアップでは、`MultiProvider` でも `openai_use_responses_websocket=True` を維持します:

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

バックエンドがリテラルな `openai/...` 文字列を想定する場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を想定する場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは、websocket トランスポート以外の `MultiProvider` でも機能します。この例で websocket を有効にしているのは、このセクションで説明しているトランスポート設定の一部であるためです。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` 経由でルーティングしながら同じプロバイダーレベルの登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡すと、基盤となる OpenAI プロバイダーに転送されます。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使用する場合、websocket トランスポートには互換性のある websocket `/responses` エンドポイントも必要です。そのようなセットアップでは、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注記

-   これは [Realtime API](../realtime/guide.md) ではなく、websocket トランスポート経由の Responses API です。Responses websocket `/responses` エンドポイントをサポートしていない限り、Chat Completions や非 OpenAI プロバイダーには適用されません。
-   環境でまだ利用できない場合は、`websockets` パッケージをインストールしてください。
-   websocket トランスポートを有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで、ターン間（およびネストされた agent-as-tool 呼び出し）で同じ websocket 接続を再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーをお勧めします。[エージェントの実行](../running_agents.md)ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。
-   長い推論ターンやレイテンシスパイクのあるネットワークでは、`responses_websocket_options` で websocket の keepalive 動作をカスタマイズします。遅延した pong フレームを許容するには `ping_timeout` を増やすか、ping を有効にしたままハートビートタイムアウトを無効にするには `ping_timeout=None` を設定します。信頼性が websocket のレイテンシより重要な場合は、HTTP/SSE トランスポートを優先してください。
-   デフォルトでは、SDK は受信メッセージサイズの上限を無効にします（`max_size=None`）。プロキシ背後の長寿命エージェントプロセスや、メモリ制約のあるコンテナーでは、メッセージごとのメモリ使用量に上限を設けるために `responses_websocket_options={"max_size": 8 * 1024 * 1024}` を設定します。

## 非 OpenAI モデル

非 OpenAI プロバイダーが必要な場合は、SDK に組み込まれたプロバイダー統合ポイントから始めてください。多くのセットアップでは、サードパーティアダプターを追加しなくてもこれで十分です。各パターンのコード例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### 非 OpenAI プロバイダーの統合方法

| アプローチ | 使用する場合 | 範囲 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにする必要がある場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを単一の実行に適用する必要がある場合 | 実行ごと |
| [`Agent.model`][agents.agent.Agent.model] | 異なるエージェントが異なるプロバイダーまたは具象モデルオブジェクトを必要とする場合 | エージェントごと |
| サードパーティアダプター | 組み込みパスでは提供されない、アダプター管理のプロバイダー対応範囲またはルーティングが必要な場合 | [サードパーティアダプター](#third-party-adapters) を参照 |

これらの組み込みパスを使って、他の LLM プロバイダーを統合できます:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合向けです。設定可能なコード例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルのものです。これにより、「この実行内のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定可能なコード例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスでモデルを指定できます。これにより、異なるエージェントに対して異なるプロバイダーを組み合わせて使用できます。設定可能なコード例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md)を設定することをお勧めします。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらのコード例では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーは、まだ Responses API をサポートしていないためです。LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用をお勧めします。

## 1 つのワークフロー内でのモデルの混在

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小型で高速なモデルを使用し、複雑なタスクには大型で高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定する際、次のいずれかの方法で特定のモデルを選択できます:

1. モデル名を渡す。
2. 任意のモデル名と、その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形式をサポートしていますが、2 つの形式はサポートする機能やツールのセットが異なるため、各ワークフローでは 1 つのモデル形式を使用することをお勧めします。ワークフローでモデル形式を混在させる必要がある場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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
    model="gpt-5.5",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  OpenAI モデルの名前を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を提供します。

エージェントで使用するモデルをさらに設定したい場合は、temperature などの任意のモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## 高度な OpenAI Responses 設定

OpenAI Responses パスを使用していて、より細かく制御する必要がある場合は、`ModelSettings` から始めてください。

### 一般的な高度な `ModelSettings` オプション

OpenAI Responses API を使用している場合、いくつかのリクエストフィールドはすでに直接の `ModelSettings` フィールドを持つため、それらに `extra_args` は必要ありません。

- `parallel_tool_calls`: 同じターンで複数のツール呼び出しを許可または禁止します。
- `truncation`: コンテキストがあふれる場合に失敗する代わりに、Responses API が最も古い会話項目を削除できるようにするには、`"auto"` を設定します。
- `store`: 生成されたレスポンスを後で取得できるようサーバー側に保存するかどうかを制御します。これは、レスポンス ID に依存するフォローアップワークフローや、`store=False` の場合にローカル入力へフォールバックする必要があるセッション圧縮フローで重要です。
- `context_management`: `compact_threshold` を使った Responses 圧縮など、サーバー側のコンテキスト処理を設定します。
- `prompt_cache_retention`: たとえば `"24h"` を使って、キャッシュされたプロンプトプレフィックスをより長く保持します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、よりリッチなレスポンスペイロードをリクエストします。
- `top_logprobs`: 出力テキストの上位トークン logprobs をリクエストします。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対する Runner 管理のリトライ設定をオプトインします。[Runner 管理リトライ](#runner-managed-retries)を参照してください。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.5",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

`store=False` を設定すると、Responses API はそのレスポンスを後でサーバー側で取得できる状態に保持しません。これは、ステートレスまたはゼロデータ保持スタイルのフローに有用ですが、レスポンス ID を再利用する機能が、代わりにローカル管理の状態に依存する必要があることも意味します。たとえば、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されていない場合、デフォルトの `"auto"` 圧縮パスを入力ベースの圧縮に切り替えます。[セッションガイド](../sessions/index.md#openai-responses-compaction-sessions)を参照してください。

サーバー側圧縮は [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] とは異なります。`context_management=[{"type": "compaction", "compact_threshold": ...}]` は各 Responses API リクエストとともに送信され、レンダリングされたコンテキストがしきい値を超えると、API はレスポンスの一部として圧縮項目を出力できます。`OpenAIResponsesCompactionSession` はターン間でスタンドアロンの `responses.compact` エンドポイントを呼び出し、ローカルのセッション履歴を書き換えます。

### `extra_args` の渡し方

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーターがあります](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）。トップレベルで利用できない場合は、それらも `extra_args` で渡せます。同じリクエストフィールドを直接の `ModelSettings` フィールドでも設定しないでください。

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

## Runner 管理リトライ

リトライは実行時専用で、オプトインです。`ModelSettings(retry=...)` を設定し、リトライポリシーがリトライを選択しない限り、SDK は一般的なモデルリクエストをリトライしません。

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.5",
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

`ModelRetrySettings` には 3 つのフィールドがあります:

<div class="field-table" markdown="1">

| フィールド | 型 | 注記 |
| --- | --- | --- |
| `max_retries` | `int | None` | 初回リクエスト後に許可されるリトライ試行回数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずにリトライする場合のデフォルト遅延戦略。`backoff.max_delay` は、この計算されたバックオフ遅延だけに上限を設けます。ポリシーが返す明示的な遅延や retry-after ヒントには上限を設けません。 |
| `policy` | `RetryPolicy | None` | リトライするかどうかを決定するコールバック。このフィールドは実行時専用で、シリアライズされません。 |

</div>

リトライポリシーは、次を持つ [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります:

- `attempt` と `max_retries`: 試行回数を考慮した判断に使用できます。
- `stream`: ストリーミングと非ストリーミングの挙動を分岐できます。
- `error`: raw な確認に使用します。
- `normalized`: `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの正規化済み情報です。
- `provider_advice`: 下位のモデルアダプターがリトライのガイダンスを提供できる場合に使用します。

ポリシーは次のいずれかを返せます:

- シンプルなリトライ判断として `True` / `False`。
- 遅延を上書きしたり診断理由を付加したりしたい場合の [`RetryDecision`][agents.retry.RetryDecision]。

SDK は、`retry_policies` でそのまま使えるヘルパーをエクスポートしています:

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常にオプトアウトします。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーのリトライ助言に従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害とタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使ってリトライします。このヘルパーは retry-after 値を明示的なポリシー遅延として扱うため、`backoff.max_delay` は上限を設けません。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかがオプトインした場合にリトライします。 |
| `retry_policies.all(...)` | ネストされたすべてのポリシーがオプトインした場合にのみリトライします。 |

ポリシーを合成する場合、`provider_suggested()` は最初の構成要素として最も安全です。プロバイダーが区別できる場合に、プロバイダーの拒否と再実行安全性の承認を保持するためです。

##### 安全性の境界

一部の失敗は自動では決してリトライされません:

- 中止エラー。
- プロバイダーの助言によって再実行が安全でないと示されたリクエスト。
- 出力がすでに開始されており、再実行が安全でなくなる形のストリーミング実行。

`previous_response_id` または `conversation_id` を使用するステートフルなフォローアップリクエストも、より保守的に扱われます。これらのリクエストでは、`network_error()` や `http_status([500])` などのプロバイダー由来ではない条件だけでは不十分です。リトライポリシーには、通常 `retry_policies.provider_suggested()` を通じた、プロバイダーからの再実行安全性の承認を含める必要があります。

##### Runner とエージェントのマージ動作

`retry` は、Runner レベルとエージェントレベルの `ModelSettings` の間でディープマージされます:

- エージェントは `retry.max_retries` だけを上書きし、Runner の `policy` を引き続き継承できます。
- エージェントは `retry.backoff` の一部だけを上書きし、同階層のバックオフフィールドを Runner から維持できます。
- `policy` は実行時専用であるため、シリアライズされた `ModelSettings` では `max_retries` と `backoff` は保持されますが、コールバック自体は省略されます。

より詳しいコード例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [アダプターを利用したリトライ例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)を参照してください。

## 非 OpenAI プロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシングに関連するエラーが発生する場合、これはトレースが OpenAI サーバーにアップロードされる一方で、OpenAI API キーがないことが原因です。これを解決するには、3 つの選択肢があります:

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものである必要があります。
3. 非 OpenAI のトレースプロセッサーを使用する。[トレーシングドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果として 404 エラーや類似の問題が発生する場合があります。解決するには、2 つの選択肢があります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### Chat Completions 互換性オプション

Chat Completions 経由でルーティングする場合、SDK は Chat Completions が送信できない Responses 専用フィールド（`previous_response_id`、`conversation_id`、プロンプト、テキストのみではないツール出力など）を黙って削除することで互換性を維持します。開発中にこれらの不一致を早期に失敗させたい場合は、OpenAI プロバイダーで厳格な機能検証を有効にしてください:

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

一部の OpenAI 互換 Chat Completions プロバイダーは、ツール呼び出しの差分をチャンクでストリーミングしますが、SDK のインクリメンタル処理に十分な信頼性がない場合があります。その場合は、ストリーミングされたツール呼び出しのバッファリングを有効にし、プロバイダーのストリームが終了した後にのみ SDK がツール呼び出しを出力するようにします:

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

[`MultiProvider`][agents.MultiProvider] では、`openai_buffer_streamed_tool_calls=True` を使用します。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) に対応していません。これにより、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないためです。現在この修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーに依存することをお勧めします。そうしないと、不正な JSON によってアプリが頻繁に失敗する可能性があります。

## プロバイダー間でのモデルの混在

モデルプロバイダー間の機能差異を把握しておく必要があります。そうしないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください:

-   サポートされていない `tools` を、それらを理解しないプロバイダーに送信しないでください
-   テキストのみのモデルを呼び出す前に、マルチモーダル入力を除外してください
-   構造化 JSON 出力をサポートしていないプロバイダーは、ときどき無効な JSON を生成することに注意してください。

## サードパーティアダプター

サードパーティアダプターは、SDK に組み込まれたプロバイダー統合ポイントだけでは不十分な場合にのみ使用してください。この SDK で OpenAI モデルのみを使用している場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] パスを優先してください。サードパーティアダプターは、OpenAI モデルを非 OpenAI プロバイダーと組み合わせる必要がある場合、または組み込みパスでは提供されないアダプター管理のプロバイダー対応範囲やルーティングが必要な場合向けです。アダプターは SDK と上流のモデルプロバイダーの間に別の互換性レイヤーを追加するため、機能サポートとリクエストセマンティクスはプロバイダーによって異なる場合があります。SDK には現在、ベストエフォートのベータ版アダプター統合として Any-LLM と LiteLLM が含まれています。

### Any-LLM

Any-LLM のサポートは、Any-LLM が管理するプロバイダー対応範囲またはルーティングが必要な場合向けに、ベストエフォートのベータ版として含まれています。

上流のプロバイダーパスに応じて、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換性レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めます。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使用したり、`AnyLLMModel` を直接インスタンス化したり、実行スコープで `AnyLLMProvider` を使用したりできます。モデルサーフェスを明示的に固定する必要がある場合は、`AnyLLMModel` を構築する際に `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM はサードパーティアダプターレイヤーであるため、プロバイダーの依存関係と機能ギャップは SDK ではなく Any-LLM によって上流で定義されます。使用量メトリクスは、上流プロバイダーが返す場合に自動的に伝播されますが、ストリーミングされた Chat Completions バックエンドでは、使用量チャンクを出力する前に `ModelSettings(include_usage=True)` が必要になる場合があります。structured outputs、ツール呼び出し、使用量レポート、または Responses 固有の動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。

### LiteLLM

LiteLLM のサポートは、LiteLLM 固有のプロバイダー対応範囲またはルーティングが必要な場合向けに、ベストエフォートのベータ版として含まれています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めます。`litellm/...` モデル名を使用したり、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化したりできます。

LiteLLM を利用する一部のプロバイダーは、デフォルトでは SDK の使用量メトリクスを設定しません。使用量レポートが必要な場合は、`ModelSettings(include_usage=True)` を渡し、structured outputs、ツール呼び出し、使用量レポート、またはアダプター固有のルーティング動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。