---
search:
  exclude: true
---
# モデル

Agents SDK は、すぐに利用できる OpenAI モデルのサポートを 2 種類提供しています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## モデル設定の選択

まず、環境に適した最もシンプルな方法を選択してください。

| 目的 | 推奨される方法 | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | デフォルトの OpenAI プロバイダーと Responses モデルのパスを使用する | [OpenAI モデル](#openai-models) |
| WebSocket トランスポート経由で OpenAI Responses API を使用する | Responses モデルのパスを維持し、WebSocket トランスポートを有効にする | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| OpenAI 以外のプロバイダーを 1 つ使用する | 組み込みのプロバイダー統合ポイントから始める | [OpenAI 以外のモデル](#non-openai-models) |
| エージェント間でモデルやプロバイダーを混在させる | 実行ごと、またはエージェントごとにプロバイダーを選択し、機能の違いを確認する | [1 つのワークフローでのモデルの混在](#mixing-models-in-one-workflow)および[プロバイダー間でのモデルの混在](#mixing-models-across-providers) |
| OpenAI Responses の高度なリクエスト設定を調整する | OpenAI Responses のパスで `ModelSettings` を使用する | [OpenAI Responses の高度な設定](#advanced-openai-responses-settings) |
| OpenAI 以外、または複数プロバイダーのルーティングにサードパーティ製アダプターを使用する | サポートされているベータ版アダプターを比較し、リリース予定のプロバイダーパスを検証する | [サードパーティ製アダプター](#third-party-adapters) |

## OpenAI モデル

OpenAI のみを使用するほとんどのアプリでは、デフォルトの OpenAI プロバイダーでモデル名の文字列を使用し、Responses モデルのパスを維持する方法を推奨します。

`Agent` の初期化時にモデルを指定しない場合は、デフォルトモデルが使用されます。現在のデフォルトは、低レイテンシーのエージェントワークフロー向けに `reasoning.effort="none"` と `verbosity="low"` を設定した [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) です。アクセス権がある場合は、明示的な `model_settings` を維持しながら品質を高めるため、エージェントに `gpt-5.6-sol` を設定することを推奨します。

`gpt-5.6-sol` などの別のモデルに切り替える場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用する場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

次に、`RunConfig` を使用して実行のデフォルトモデルを設定できます。エージェントにモデルを設定しなかった場合は、この実行のモデルが使用されます。

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

この方法で `gpt-5.6-sol` などの任意の GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースに最適な設定が使用されます。デフォルトモデルの推論エフォートを調整するには、独自の `ModelSettings` を渡します。

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

レイテンシーを短縮するには、GPT-5 モデルで `reasoning.effort="none"` を使用することを推奨します。

#### ComputerTool のモデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれている場合、実際の Responses リクエストで有効なモデルによって、SDK が送信するコンピューターツールのペイロードが決まります。明示的な `gpt-5.5` リクエストでは GA の組み込み `computer` ツールが使用され、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` ペイロードが維持されます。

主な例外は、プロンプトで管理される呼び出しです。プロンプトテンプレートがモデルを管理し、SDK がリクエストから `model` を省略する場合、SDK はプロンプトが固定しているモデルを推測しないように、プレビュー互換のコンピューターペイロードをデフォルトで使用します。このフローで GA のパスを維持するには、リクエストで `model="gpt-5.5"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` を使用して GA セレクターを強制します。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに対応する組み込みセレクターへ正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は引き続き通常の関数名として動作します。

プレビュー互換リクエストでは、`environment` と表示サイズを事前にシリアライズする必要があります。そのため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使用するプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエストを送信する前に GA セレクターを強制する必要があります。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool)を参照してください。

#### GPT-5 以外のモデル

カスタムの `model_settings` を指定せずに GPT-5 以外のモデル名を渡すと、SDK は任意のモデルと互換性がある汎用的な `ModelSettings` に戻します。

### Responses 専用のツール検索機能

次のツール機能は、OpenAI Responses モデルでのみサポートされています。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` およびその他の遅延読み込み対応の Responses ツールインターフェース

これらの機能は、Chat Completions モデルおよび Responses 以外のバックエンドでは拒否されます。遅延読み込みツールを使用する場合は、エージェントに `ToolSearchTool()` を追加し、単独の名前空間名や遅延読み込み専用の関数名を強制するのではなく、`auto` または `required` のツール選択を通じてモデルにツールを読み込ませます。設定の詳細と現在の制約については、[ツール](../tools.md#hosted-tool-search)を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI が提供するモデルを使用する場合は、WebSocket トランスポートを有効にできます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデルに影響します。これには、`"gpt-5.6-sol"` などのモデル名の文字列も含まれます。

トランスポートの選択は、SDK がモデル名をモデルインスタンスへ解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡した場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は WebSocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions を維持します。`RunConfig(model_provider=...)` を渡した場合は、グローバルデフォルトではなく、そのプロバイダーがトランスポートの選択を制御します。

#### プロバイダーまたは実行レベルの設定

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

OpenAI が提供するプロバイダーは、オプションのエージェント登録設定も受け付けます。これは、OpenAI の設定でハーネス ID など、プロバイダーレベルの登録メタデータが必要な場合の高度なオプションです。

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

プレフィックスに基づくモデルルーティングが必要な場合、たとえば 1 回の実行で `openai/...` と `any-llm/...` のモデル名を混在させる場合は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` では、以前からのデフォルト動作が 2 つ維持されています。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスは、そのまま渡されるのではなく `UserError` を発生させます。

リテラルの名前空間付きモデル ID を必要とする OpenAI 互換エンドポイントを OpenAI プロバイダーに指定する場合は、パススルー動作を明示的に有効にします。WebSocket を有効にした設定では、`MultiProvider` にも `openai_use_responses_websocket=True` を設定したままにします。

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

バックエンドがリテラルの `openai/...` 文字列を必要とする場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` など、その他の名前空間付きモデル ID を必要とする場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは、WebSocket トランスポートを使用しない `MultiProvider` でも機能します。この例では、このセクションで説明しているトランスポート設定の一部であるため、WebSocket を有効にしたままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` を介してルーティングしながら、同じプロバイダーレベルの登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡します。これは基盤となる OpenAI プロバイダーに転送されます。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使用する場合、WebSocket トランスポートには互換性のある WebSocket `/responses` エンドポイントも必要です。このような設定では、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注意事項

-   これは WebSocket トランスポート経由の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や OpenAI 以外のプロバイダーには、それらが Responses WebSocket `/responses` エンドポイントをサポートしていない限り適用されません。
-   環境にまだ存在しない場合は、`websockets` パッケージをインストールしてください。
-   WebSocket トランスポートを有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで、ターン間およびネストされたエージェントのツール呼び出し間で同じ WebSocket 接続を再利用する場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md)ガイドおよび [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。
-   推論に時間がかかるターンやレイテンシーが急増するネットワークでは、`responses_websocket_options` を使用して WebSocket のキープアライブ動作をカスタマイズします。遅延した pong フレームを許容するには `ping_timeout` を増やし、ping を有効にしたままハートビートのタイムアウトを無効にするには `ping_timeout=None` を設定します。WebSocket のレイテンシーよりも信頼性が重要な場合は、HTTP/SSE トランスポートを優先してください。
-   デフォルトでは、SDK は受信メッセージのサイズ制限を無効にします（`max_size=None`）。プロキシの背後で長期間稼働するエージェントプロセスや、メモリに制約があるコンテナーでは、`responses_websocket_options={"max_size": 8 * 1024 * 1024}` を設定し、メッセージごとのメモリ使用量を制限します。

## OpenAI 以外のモデル

OpenAI 以外のプロバイダーが必要な場合は、SDK の組み込みプロバイダー統合ポイントから始めてください。多くの環境では、サードパーティ製アダプターを追加しなくても、これで十分です。各パターンのコード例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### OpenAI 以外のプロバイダーの統合方法

| 方法 | 使用する状況 | 適用範囲 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにする場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを単一の実行に適用する場合 | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | エージェントごとに異なるプロバイダーまたは具体的なモデルオブジェクトが必要な場合 | エージェント単位 |
| サードパーティ製アダプター | 組み込みの方法では提供されない、アダプター管理のプロバイダーカバレッジまたはルーティングが必要な場合 | [サードパーティ製アダプター](#third-party-adapters)を参照 |

次の組み込みの方法で、その他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用する場合に便利です。これは、LLM プロバイダーに OpenAI 互換 API エンドポイントがあり、`base_url` と `api_key` を設定できる場合に使用します。設定可能なコード例については、[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで使用します。これにより、「この実行のすべてのエージェントにカスタムモデルプロバイダーを使用する」と指定できます。設定可能なコード例については、[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
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

    これらのコード例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API / モデルを使用しています。使用する LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## 1 つのワークフローでのモデルの混在

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小さく高速なモデルを使用し、複雑なタスクには大きく高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定する際は、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡します。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡します。
3. [`Model`][agents.models.interface.Model] の実装を直接指定します。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形式をサポートしていますが、この 2 つの形式ではサポートされる機能とツールのセットが異なるため、ワークフローごとに 1 つのモデル形式を使用することを推奨します。ワークフローでモデル形式を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1.  OpenAI モデルの名前を直接設定します。
2.  [`Model`][agents.models.interface.Model] の実装を指定します。

エージェントで使用するモデルをさらに設定する場合は、temperature などのオプションのモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。

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

OpenAI Responses のパスを使用しており、より詳細な制御が必要な場合は、まず `ModelSettings` を使用します。

### 一般的な高度な `ModelSettings` オプション

OpenAI Responses API を使用する場合、複数のリクエストフィールドに対応する `ModelSettings` のフィールドがすでに用意されているため、それらに `extra_args` を使用する必要はありません。

- `parallel_tool_calls`: 同じターンで複数のツール呼び出しを許可または禁止します。
- `truncation`: `"auto"` を設定すると、コンテキストが上限を超える場合に失敗する代わりに、Responses API が最も古い会話項目を削除します。
- `store`: 生成されたレスポンスを後で取得できるようにサーバー側へ保存するかどうかを制御します。これは、レスポンス ID に依存する後続ワークフローや、`store=False` の場合にローカル入力へフォールバックする必要があるセッション圧縮フローに影響します。
- `context_management`: `compact_threshold` を使用する Responses の圧縮など、サーバー側のコンテキスト処理を設定します。
- `prompt_cache_retention`: キャッシュされたプロンプトプレフィックスを、たとえば `"24h"` のように、より長く保持します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より詳細なレスポンスペイロードをリクエストします。
- `top_logprobs`: 出力テキストの上位トークンの logprobs をリクエストします。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対して Runner が管理する再試行設定を有効にします。[Runner が管理する再試行](#runner-managed-retries)を参照してください。

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

`store=False` を設定すると、Responses API はそのレスポンスを後でサーバー側から取得できる状態で保持しません。これは、ステートレスなフローやデータを保持しない形式のフローに便利ですが、通常ならレスポンス ID を再利用する機能が、代わりにローカルで管理される状態に依存する必要があることも意味します。たとえば、最後のレスポンスが保存されていない場合、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] はデフォルトの `"auto"` 圧縮パスを入力ベースの圧縮へ切り替えます。[セッションガイド](../sessions/index.md#openai-responses-compaction-sessions)を参照してください。

サーバー側の圧縮は、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] とは異なります。`context_management=[{"type": "compaction", "compact_threshold": ...}]` は各 Responses API リクエストとともに送信され、レンダリングされたコンテキストがしきい値を超えると、API はレスポンスの一部として圧縮項目を出力できます。`OpenAIResponsesCompactionSession` はターン間でスタンドアロンの `responses.compact` エンドポイントを呼び出し、ローカルのセッション履歴を書き換えます。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

また、OpenAI の Responses API を使用する場合、[その他にもいくつかのオプションパラメーターがあります](https://platform.openai.com/docs/api-reference/responses/create)（`user`、`service_tier` など）。それらがトップレベルで利用できない場合は、`extra_args` を使用して渡すこともできます。同じリクエストフィールドを `ModelSettings` の直接のフィールドでも設定しないでください。

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

## Runner が管理する再試行

再試行は実行時にのみ適用され、明示的に有効にする必要があります。`ModelSettings(retry=...)` を設定し、再試行ポリシーが再試行を選択しない限り、SDK は一般的なモデルリクエストを再試行しません。

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
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずに再試行する場合の、デフォルトの遅延方式です。`backoff.max_delay` は、この計算されたバックオフ遅延だけを上限設定します。ポリシーまたは retry-after ヒントが返す明示的な遅延には上限を設定しません。 |
| `policy` | `RetryPolicy | None` | 再試行するかどうかを決定するコールバックです。このフィールドは実行時専用で、シリアライズされません。 |

</div>

再試行ポリシーは、次の情報を持つ [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。

- `attempt` と `max_retries`: 試行回数を考慮した判断を行うために使用します。
- `stream`: ストリーミング動作と非ストリーミング動作を分岐するために使用します。
- `error`: raw な内容を調査するために使用します。
- `normalized`: `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの正規化された情報です。
- `provider_advice`: 基盤となるモデルアダプターが再試行の指針を提供できる場合に使用されます。

ポリシーは、次のいずれかを返せます。

- 単純な再試行判断を示す `True` / `False`
- 遅延を上書きする場合、または診断理由を付加する場合の [`RetryDecision`][agents.retry.RetryDecision]

SDK は、`retry_policies` で利用できる既成のヘルパーをエクスポートしています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常に再試行を行いません。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーの再試行指針に従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害とタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使用して再試行します。このヘルパーは retry-after の値を明示的なポリシー遅延として扱うため、`backoff.max_delay` はその上限を設定しません。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかが再試行を選択した場合に再試行します。 |
| `retry_policies.all(...)` | ネストされたすべてのポリシーが再試行を選択した場合にのみ再試行します。 |

ポリシーを組み合わせる場合、プロバイダーが再生の安全性を区別できるときに、プロバイダーによる拒否と再生安全性の承認を維持できるため、`provider_suggested()` が最も安全な最初の基本要素です。

##### 安全性の境界

一部の障害は、自動的には再試行されません。

- 中断エラー
- プロバイダーの指針で再生が安全でないと示されたリクエスト
- 出力がすでに開始され、再生が安全でなくなる状態のストリーミング実行

`previous_response_id` または `conversation_id` を使用するステートフルな後続リクエストも、より慎重に扱われます。このようなリクエストでは、`network_error()` や `http_status([500])` など、プロバイダーに基づかない述語だけでは不十分です。再試行ポリシーには、通常 `retry_policies.provider_suggested()` を使用して、プロバイダーによる再生安全性の承認を含める必要があります。

##### Runner とエージェントのマージ動作

Runner レベルとエージェントレベルの `ModelSettings` の間で、`retry` はディープマージされます。

- エージェントは `retry.max_retries` だけを上書きしながら、Runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部だけを上書きし、Runner のその他のバックオフフィールドを維持できます。
- `policy` は実行時専用であるため、シリアライズされた `ModelSettings` は `max_retries` と `backoff` を維持しますが、コールバック自体は省略します。

より詳しいコード例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) および[アダプターを使用した再試行のコード例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)を参照してください。

## OpenAI 以外のプロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシングに関連するエラーが発生する場合、トレースが OpenAI のサーバーへアップロードされる一方で、OpenAI API キーが設定されていないことが原因です。これを解決する方法は 3 つあります。

1. トレーシングを完全に無効にします: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用の OpenAI キーを設定します: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) で発行されたものである必要があります。
3. OpenAI 以外のトレースプロセッサーを使用します。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、その他の多くの LLM プロバイダーはまだこれをサポートしていません。その結果、404 エラーや同様の問題が発生する場合があります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に使用できます。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### Chat Completions の互換性オプション

Chat Completions を介してルーティングする場合、SDK は、`previous_response_id`、`conversation_id`、プロンプト、テキストのみではないツール出力など、Chat Completions では送信できない Responses 専用フィールドを暗黙的に削除することで互換性を維持します。開発中にこのような不一致を即座に失敗させる場合は、OpenAI プロバイダーで厳格な機能検証を有効にします。

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

一部の OpenAI 互換 Chat Completions プロバイダーは、SDK が増分処理するには信頼性が不十分なチャンクで、ツール呼び出しの差分をストリーミングします。その場合は、ストリーミングされるツール呼び出しのバッファリングを有効にし、プロバイダーのストリームが終了した後にのみ SDK がツール呼び出しを出力するようにします。

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

[`MultiProvider`][agents.MultiProvider] では、`openai_buffer_streamed_tool_calls=True` を使用します。

### structured outputs のサポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生する場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていますが、出力に使用する `json_schema` を指定できません。現在この問題の修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーの利用を推奨します。そうでない場合、不正な形式の JSON によってアプリが頻繁に停止する可能性があります。

## プロバイダー間でのモデルの混在

モデルプロバイダー間の機能差を把握しておく必要があります。把握していないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホストされたファイル検索と Web 検索をサポートしていますが、その他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

-   サポートしていないプロバイダーには、未対応の `tools` を送信しないでください
-   テキストのみを扱うモデルを呼び出す前に、マルチモーダル入力を除外してください
-   構造化された JSON 出力をサポートしていないプロバイダーは、無効な JSON を生成する場合があることに注意してください。

## サードパーティ製アダプター

SDK の組み込みプロバイダー統合ポイントでは不十分な場合にのみ、サードパーティ製アダプターを使用してください。この SDK で OpenAI モデルのみを使用する場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] のパスを優先してください。サードパーティ製アダプターは、OpenAI モデルと OpenAI 以外のプロバイダーを組み合わせる必要がある場合や、組み込みの方法では提供されない、アダプター管理のプロバイダーカバレッジまたはルーティングが必要な場合に使用します。アダプターは SDK と上流のモデルプロバイダーの間に別の互換性レイヤーを追加するため、機能サポートとリクエストのセマンティクスはプロバイダーによって異なる場合があります。SDK には現在、ベストエフォート方式のベータ版アダプター統合として Any-LLM と LiteLLM が含まれています。

### Any-LLM

Any-LLM が管理するプロバイダーカバレッジまたはルーティングが必要な場合に向けて、Any-LLM のサポートがベストエフォート方式のベータ版として含まれています。

上流のプロバイダーパスに応じて、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換性レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使用するか、`AnyLLMModel` を直接インスタンス化するか、実行スコープで `AnyLLMProvider` を使用できます。モデルインターフェースを明示的に固定する必要がある場合は、`AnyLLMModel` の構築時に `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM は引き続きサードパーティ製アダプターレイヤーであるため、プロバイダーの依存関係と機能差は SDK ではなく、上流の Any-LLM によって定義されます。上流プロバイダーが使用量メトリクスを返す場合、それらは自動的に伝播されます。ただし、ストリーミング Chat Completions バックエンドでは、使用量チャンクを出力する前に `ModelSettings(include_usage=True)` が必要になる場合があります。structured outputs、ツール呼び出し、使用量レポート、または Responses 固有の動作に依存する場合は、デプロイ予定のプロバイダーバックエンドを正確に検証してください。

### LiteLLM

LiteLLM 固有のプロバイダーカバレッジまたはルーティングが必要な場合に向けて、LiteLLM のサポートがベストエフォート方式のベータ版として含まれています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` モデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

LiteLLM を使用する一部のプロバイダーは、デフォルトでは SDK の使用量メトリクスを設定しません。使用量レポートが必要な場合は、`ModelSettings(include_usage=True)` を渡してください。また、structured outputs、ツール呼び出し、使用量レポート、またはアダプター固有のルーティング動作に依存する場合は、デプロイ予定のプロバイダーバックエンドを正確に検証してください。