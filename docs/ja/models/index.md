---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルの標準サポートが 2 種類用意されています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

セットアップに合う最もシンプルな方法から始めてください。

| 実現したいこと | 推奨される方法 | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | デフォルトの OpenAI プロバイダーを Responses モデルパスで使用する | [OpenAI モデル](#openai-models) |
| websocket トランスポート経由で OpenAI Responses API を使用する | Responses モデルパスを維持し、websocket トランスポートを有効にする | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 1 つの非 OpenAI プロバイダーを使用する | 組み込みのプロバイダー統合ポイントから始める | [非 OpenAI モデル](#non-openai-models) |
| エージェント間でモデルまたはプロバイダーを混在させる | 実行ごと、またはエージェントごとにプロバイダーを選択し、機能差を確認する | [1 つのワークフローでのモデルの混在](#mixing-models-in-one-workflow) と [プロバイダー間でのモデルの混在](#mixing-models-across-providers) |
| 高度な OpenAI Responses リクエスト設定を調整する | OpenAI Responses パスで `ModelSettings` を使用する | [高度な OpenAI Responses 設定](#advanced-openai-responses-settings) |
| 非 OpenAI または混在プロバイダーのルーティングにサードパーティアダプターを使用する | サポートされているベータ版アダプターを比較し、リリース予定のプロバイダーパスを検証する | [サードパーティアダプター](#third-party-adapters) |

## OpenAI モデル

ほとんどの OpenAI のみのアプリでは、デフォルトの OpenAI プロバイダーで文字列のモデル名を使用し、Responses モデルパスを維持する方法が推奨されます。

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使用されます。低レイテンシのエージェントワークフロー向けに、現在のデフォルトは [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) で、`reasoning.effort="none"` と `verbosity="low"` が設定されています。アクセス権がある場合は、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) をエージェントに設定することを推奨します。

[`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) などの他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

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

この方法で [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) などの GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースで最も適切に動作する設定が適用されます。デフォルトモデルの reasoning effort を調整するには、独自の `ModelSettings` を渡します。

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

低レイテンシにするには、GPT-5 モデルで `reasoning.effort="none"` を使用することを推奨します。

#### ComputerTool のモデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、SDK が送信する computer-tool ペイロードが決まります。明示的な `gpt-5.5` リクエストでは GA 組み込み `computer` ツールが使用され、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` ペイロードが維持されます。

主な例外は、プロンプト管理の呼び出しです。プロンプトテンプレートがモデルを保持し、SDK がリクエストから `model` を省略する場合、SDK は、プロンプトが固定しているモデルを推測しないように、プレビュー互換の computer ペイロードをデフォルトにします。そのフローで GA パスを維持するには、リクエストで `model="gpt-5.5"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

登録済みの [`ComputerTool`][agents.tool.ComputerTool] がある場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名と同様に動作し続けます。

プレビュー互換のリクエストでは、`environment` と表示寸法を事前にシリアライズする必要があります。そのため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使用するプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool)を参照してください。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで非 GPT-5 モデル名を渡すと、SDK は任意のモデルと互換性のある汎用の `ModelSettings` に戻します。

### Responses のみのツール検索機能

次のツール機能は、OpenAI Responses モデルでのみサポートされています。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` とその他の遅延読み込み Responses ツールサーフェス

これらの機能は、Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。遅延読み込みツールを使用する場合は、エージェントに `ToolSearchTool()` を追加し、裸の名前空間名や遅延専用の関数名を強制するのではなく、`auto` または `required` のツール選択を通じてモデルにツールを読み込ませてください。セットアップの詳細と現在の制約については、[ツール](../tools.md#hosted-tool-search)を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI ベースのモデルを使用する場合は、websocket トランスポートを有効にできます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデル（`"gpt-5.5"` などの文字列モデル名を含む）に影響します。

トランスポートの選択は、SDK がモデル名をモデルインスタンスに解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket を使用し、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合、グローバルデフォルトではなく、そのプロバイダーがトランスポート選択を制御します。

#### プロバイダーまたは実行レベルの設定

プロバイダーごと、または実行ごとに websocket トランスポートを設定することもできます。

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

OpenAI ベースのプロバイダーは、任意のエージェント登録設定も受け付けます。これは、OpenAI の設定で harness ID などのプロバイダーレベルの登録メタデータが想定されている場合の高度なオプションです。

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

プレフィックスベースのモデルルーティングが必要な場合（たとえば 1 つの実行で `openai/...` と `any-llm/...` のモデル名を混在させる場合）は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` は、歴史的なデフォルトを 2 つ保持しています。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスは、パススルーされるのではなく `UserError` を発生させます。

OpenAI プロバイダーを、リテラルな名前空間付きモデル ID を想定する OpenAI 互換エンドポイントに向ける場合は、パススルー動作を明示的に有効にします。websocket が有効なセットアップでは、`MultiProvider` でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドがリテラルな `openai/...` 文字列を想定する場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` など、他の名前空間付きモデル ID を想定する場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは、websocket トランスポート外の `MultiProvider` でも機能します。この例では、このセクションで説明しているトランスポート設定の一部であるため、websocket を有効なままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` 経由でルーティングしながら同じプロバイダーレベルの登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡すと、基盤となる OpenAI プロバイダーに転送されます。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使用する場合、websocket トランスポートには互換性のある websocket `/responses` エンドポイントも必要です。そのようなセットアップでは、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注記

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Responses websocket `/responses` エンドポイントをサポートしていない限り、Chat Completions や非 OpenAI プロバイダーには適用されません。
-   環境でまだ利用できない場合は、`websockets` パッケージをインストールしてください。
-   websocket トランスポートを有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで、ターン間（および入れ子の agent-as-tool 呼び出し）で同じ websocket 接続を再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md)ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。
-   長い推論ターンやレイテンシの急増があるネットワークでは、`responses_websocket_options` で websocket keepalive の動作をカスタマイズしてください。遅延した pong フレームを許容するには `ping_timeout` を増やすか、ping を有効にしたままハートビートタイムアウトを無効にするには `ping_timeout=None` を設定します。websocket レイテンシより信頼性が重要な場合は、HTTP/SSE トランスポートを優先してください。

## 非 OpenAI モデル

非 OpenAI プロバイダーが必要な場合は、SDK の組み込みプロバイダー統合ポイントから始めてください。多くのセットアップでは、サードパーティアダプターを追加しなくてもこれで十分です。各パターンの例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### 非 OpenAI プロバイダーの統合方法

| アプローチ | 使用する場合 | 範囲 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにしたい場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを 1 回の実行に適用したい場合 | 実行ごと |
| [`Agent.model`][agents.agent.Agent.model] | 異なるエージェントに異なるプロバイダーや具体的なモデルオブジェクトが必要な場合 | エージェントごと |
| サードパーティアダプター | 組み込みパスでは提供されない、アダプター管理のプロバイダー対応範囲やルーティングが必要な場合 | [サードパーティアダプター](#third-party-adapters)を参照 |

これらの組み込みパスで、他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。これは、LLM プロバイダーに OpenAI 互換 API エンドポイントがあり、`base_url` と `api_key` を設定できる場合に使用します。設定可能な例については、[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより、「この実行内のすべてのエージェントにカスタムモデルプロバイダーを使用する」と指定できます。設定可能な例については、[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] により、特定の Agent インスタンスでモデルを指定できます。これにより、異なるエージェントに対して異なるプロバイダーを自由に組み合わせられます。設定可能な例については、[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` からの API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md)を設定することを推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらの例では、Chat Completions API/モデルを使用しています。多くの LLM プロバイダーは、まだ Responses API をサポートしていないためです。LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## 1 つのワークフローでのモデルの混在

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小さく高速なモデルを使用し、複雑なタスクにはより大きく高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定する際、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、2 つの形状でサポートされる機能とツールのセットが異なるため、各ワークフローでは単一のモデル形状を使用することを推奨します。ワークフローでモデル形状を組み合わせる必要がある場合は、使用するすべての機能が両方で利用できることを確認してください。

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

エージェントで使用するモデルをさらに設定したい場合は、temperature などの任意のモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses パスを使用していて、より詳細な制御が必要な場合は、`ModelSettings` から始めてください。

### 一般的な高度な `ModelSettings` オプション

OpenAI Responses API を使用している場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、それらに `extra_args` は不要です。

- `parallel_tool_calls`: 同じターン内の複数のツール呼び出しを許可または禁止します。
- `truncation`: コンテキストがあふれる場合に失敗するのではなく、Responses API が最も古い会話アイテムを削除できるようにするには、`"auto"` を設定します。
- `store`: 生成されたレスポンスを後で取得できるようにサーバー側に保存するかどうかを制御します。これは、レスポンス ID に依存する後続ワークフローや、`store=False` のときにローカル入力へのフォールバックが必要になる可能性があるセッション圧縮フローで重要です。
- `context_management`: `compact_threshold` を使用した Responses 圧縮など、サーバー側のコンテキスト処理を設定します。
- `prompt_cache_retention`: たとえば `"24h"` を使って、キャッシュされたプロンプトプレフィックスをより長く保持します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、よりリッチなレスポンスペイロードをリクエストします。
- `top_logprobs`: 出力テキストの top-token logprobs をリクエストします。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対する runner 管理の再試行設定を有効にします。[Runner 管理の再試行](#runner-managed-retries)を参照してください。

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

`store=False` を設定すると、Responses API はそのレスポンスを後でサーバー側から取得できるようには保持しません。これはステートレス、またはゼロデータ保持スタイルのフローに便利ですが、通常ならレスポンス ID を再利用する機能が、代わりにローカル管理の状態に依存する必要があることも意味します。たとえば、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されていない場合、デフォルトの `"auto"` 圧縮パスを入力ベースの圧縮に切り替えます。[Sessions ガイド](../sessions/index.md#openai-responses-compaction-sessions)を参照してください。

サーバー側圧縮は、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] とは異なります。`context_management=[{"type": "compaction", "compact_threshold": ...}]` は各 Responses API リクエストと一緒に送信され、レンダリングされたコンテキストがしきい値を超えると、API はレスポンスの一部として圧縮アイテムを出力できます。`OpenAIResponsesCompactionSession` はターン間でスタンドアロンの `responses.compact` エンドポイントを呼び出し、ローカルセッション履歴を書き換えます。

### `extra_args` の受け渡し

プロバイダー固有、または SDK がまだトップレベルで直接公開していない新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。それらがトップレベルで利用できない場合は、`extra_args` を使用して渡すこともできます。同じリクエストフィールドを、直接の `ModelSettings` フィールドでも同時に設定しないでください。

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

再試行はランタイム専用で、明示的な有効化が必要です。`ModelSettings(retry=...)` を設定し、再試行ポリシーが再試行を選択しない限り、SDK は一般的なモデルリクエストを再試行しません。

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

`ModelRetrySettings` には 3 つのフィールドがあります。

<div class="field-table" markdown="1">

| フィールド | 型 | 注記 |
| --- | --- | --- |
| `max_retries` | `int | None` | 初回リクエスト後に許可される再試行回数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずに再試行する場合のデフォルトの遅延戦略。`backoff.max_delay` は、この計算された backoff 遅延のみを上限設定します。ポリシーから返された明示的な遅延や retry-after ヒントには上限を設定しません。 |
| `policy` | `RetryPolicy | None` | 再試行するかどうかを決定するコールバック。このフィールドはランタイム専用で、シリアライズされません。 |

</div>

再試行ポリシーは、以下を含む [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。

- `attempt` と `max_retries`。試行回数を考慮した判断を行えます。
- `stream`。ストリーミングと非ストリーミングの動作を分岐できます。
- `error`。生の内容を確認できます。
- `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの正規化された情報。
- 基盤のモデルアダプターが再試行ガイダンスを提供できる場合の `provider_advice`。

ポリシーは以下のいずれかを返せます。

- 単純な再試行判断のための `True` / `False`。
- 遅延を上書きしたり、診断理由を付加したりしたい場合の [`RetryDecision`][agents.retry.RetryDecision]。

SDK は、`retry_policies` に既製のヘルパーをエクスポートしています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常にオプトアウトします。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーの再試行助言に従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害とタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使用して再試行します。このヘルパーは retry-after 値を明示的なポリシー遅延として扱うため、`backoff.max_delay` はそれを上限設定しません。 |
| `retry_policies.any(...)` | 入れ子のポリシーのいずれかが有効化した場合に再試行します。 |
| `retry_policies.all(...)` | 入れ子のすべてのポリシーが有効化した場合にのみ再試行します。 |

ポリシーを合成する場合、`provider_suggested()` は最も安全な最初の構成要素です。プロバイダーがそれらを区別できる場合に、プロバイダーの拒否とリプレイ安全性の承認を保持するためです。

##### 安全境界

一部の障害は自動的には再試行されません。

- 中止エラー。
- プロバイダーからの助言がリプレイを安全でないと示すリクエスト。
- リプレイが安全でなくなる形で出力がすでに開始された後のストリーミング実行。

`previous_response_id` または `conversation_id` を使用するステートフルな後続リクエストも、より保守的に扱われます。これらのリクエストでは、`network_error()` や `http_status([500])` など、プロバイダー以外の述語だけでは十分ではありません。再試行ポリシーには、通常 `retry_policies.provider_suggested()` を通じて、プロバイダーからのリプレイ安全性の承認を含める必要があります。

##### Runner とエージェントのマージ動作

`retry` は、runner レベルとエージェントレベルの `ModelSettings` の間でディープマージされます。

- エージェントは `retry.max_retries` だけを上書きし、runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部だけを上書きし、runner の兄弟 backoff フィールドを維持できます。
- `policy` はランタイム専用であるため、シリアライズされた `ModelSettings` は `max_retries` と `backoff` を保持しますが、コールバック自体は省略します。

より完全な例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [アダプターベースの再試行例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)を参照してください。

## 非 OpenAI プロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシングに関連するエラーが発生する場合、トレースが OpenAI サーバーにアップロードされる一方で、OpenAI API キーがないことが原因です。これを解決するには、3 つの選択肢があります。

1. トレーシングを完全に無効にする: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものである必要があります。
3. 非 OpenAI トレースプロセッサーを使用する。[トレーシングドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだこれをサポートしていません。その結果、404 などの問題が発生する場合があります。解決するには、2 つの選択肢があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)に例があります。

### structured outputs のサポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生する場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていますが、出力に使用する `json_schema` の指定は許可していません。この問題の修正に取り組んでいますが、JSON schema 出力をサポートしているプロバイダーに依存することを推奨します。そうしないと、不正な形式の JSON が原因でアプリが頻繁に壊れるためです。

## プロバイダー間でのモデルの混在

モデルプロバイダー間の機能差を理解しておく必要があります。そうしないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

-   サポートされていない `tools` を、それらを理解しないプロバイダーに送信しないでください
-   テキスト専用のモデルを呼び出す前に、マルチモーダル入力を除外してください
-   structured JSON 出力をサポートしていないプロバイダーは、無効な JSON を生成することがある点に注意してください。

## サードパーティアダプター

SDK の組み込みプロバイダー統合ポイントで十分でない場合にのみ、サードパーティアダプターを使用してください。この SDK で OpenAI モデルのみを使用している場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] パスを優先してください。サードパーティアダプターは、OpenAI モデルと非 OpenAI プロバイダーを組み合わせる必要がある場合、または組み込みパスでは提供されないアダプター管理のプロバイダー対応範囲やルーティングが必要な場合のためのものです。アダプターは SDK と上流のモデルプロバイダーの間に別の互換性レイヤーを追加するため、機能サポートとリクエストセマンティクスはプロバイダーによって異なる場合があります。SDK には現在、Any-LLM と LiteLLM がベストエフォートのベータ版アダプター統合として含まれています。

### Any-LLM

Any-LLM のサポートは、Any-LLM 管理のプロバイダー対応範囲やルーティングが必要な場合向けに、ベストエフォートのベータ版として含まれています。

上流プロバイダーパスによって、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換性レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使用するか、`AnyLLMModel` を直接インスタンス化するか、実行スコープで `AnyLLMProvider` を使用できます。モデルサーフェスを明示的に固定する必要がある場合は、`AnyLLMModel` を構築するときに `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM はサードパーティアダプターレイヤーであり続けるため、プロバイダー依存関係と機能ギャップは SDK ではなく Any-LLM によって上流で定義されます。使用状況メトリクスは、上流プロバイダーが返す場合に自動的に伝播されますが、ストリーミング Chat Completions バックエンドでは、usage chunks を出力する前に `ModelSettings(include_usage=True)` が必要になる場合があります。structured outputs、ツール呼び出し、使用状況レポート、または Responses 固有の動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。

### LiteLLM

LiteLLM のサポートは、LiteLLM 固有のプロバイダー対応範囲やルーティングが必要な場合向けに、ベストエフォートのベータ版として含まれています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` モデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

一部の LiteLLM ベースのプロバイダーは、デフォルトでは SDK の使用状況メトリクスを設定しません。使用状況レポートが必要な場合は、`ModelSettings(include_usage=True)` を渡し、structured outputs、ツール呼び出し、使用状況レポート、またはアダプター固有のルーティング動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。