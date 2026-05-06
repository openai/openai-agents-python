---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルに対するすぐに使えるサポートが 2 種類用意されています。

- **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
- [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

まずは、セットアップに合う最もシンプルな方法から始めます。

| 実現したいこと | 推奨される方法 | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | デフォルトの OpenAI プロバイダーを Responses モデルの経路で使用する | [OpenAI モデル](#openai-models) |
| OpenAI Responses API を websocket トランスポートで使用する | Responses モデルの経路を維持し、websocket トランスポートを有効にする | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| OpenAI 以外のプロバイダーを 1 つ使用する | 組み込みのプロバイダー統合ポイントから始める | [OpenAI 以外のモデル](#non-openai-models) |
| エージェント間でモデルまたはプロバイダーを混在させる | 実行ごと、またはエージェントごとにプロバイダーを選択し、機能差を確認する | [1 つのワークフローでのモデルの混在](#mixing-models-in-one-workflow) と [プロバイダーをまたいだモデルの混在](#mixing-models-across-providers) |
| 高度な OpenAI Responses リクエスト設定を調整する | OpenAI Responses 経路で `ModelSettings` を使用する | [高度な OpenAI Responses 設定](#advanced-openai-responses-settings) |
| OpenAI 以外または混在プロバイダーのルーティングにサードパーティ製アダプターを使用する | サポートされているベータ版アダプターを比較し、出荷予定のプロバイダー経路を検証する | [サードパーティ製アダプター](#third-party-adapters) |

## OpenAI モデル

OpenAI のみを使うほとんどのアプリでは、デフォルトの OpenAI プロバイダーで文字列のモデル名を使用し、Responses モデルの経路を維持する方法を推奨します。

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使用されます。現在のデフォルトは、互換性と低レイテンシのため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。アクセス権がある場合は、明示的な `model_settings` を維持しながら、より高品質な [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) をエージェントに設定することを推奨します。

[`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.5
python3 my_awesome_agent.py
```

次に、`RunConfig` を介して実行用のデフォルトモデルを設定できます。エージェントにモデルを設定していない場合、この実行のモデルが使用されます。

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

この方法で [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) などの GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースで最適に動作する設定が使用されます。デフォルトモデルの推論努力を調整するには、独自の `ModelSettings` を渡します。

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

低レイテンシには、`gpt-5.5` で `reasoning.effort="none"` を使用することを推奨します。gpt-4.1 ファミリー（mini と nano バリアントを含む）も、インタラクティブなエージェントアプリを構築するうえで引き続き堅実な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれている場合、実際の Responses リクエストで有効なモデルにより、SDK が送信する computer-tool ペイロードが決まります。明示的な `gpt-5.5` リクエストでは GA 組み込みの `computer` ツールが使用され、明示的な `computer-use-preview` リクエストでは古い `computer_use_preview` ペイロードが維持されます。

主な例外は、プロンプト管理の呼び出しです。プロンプトテンプレートがモデルを所有し、SDK がリクエストから `model` を省略する場合、SDK はプロンプトがどのモデルを固定しているか推測しないように、プレビュー互換の computer ペイロードをデフォルトにします。このフローで GA 経路を維持するには、リクエストで `model="gpt-5.5"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

登録済みの [`ComputerTool`][agents.tool.ComputerTool] がある場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名と同じように動作し続けます。

プレビュー互換リクエストでは、`environment` と表示寸法を事前にシリアライズする必要があります。そのため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリを使用するプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### GPT-5 以外のモデル

カスタム `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK は任意のモデルと互換性のある汎用 `ModelSettings` に戻ります。

### Responses 専用のツール検索機能

次のツール機能は、OpenAI Responses モデルでのみサポートされています。

- [`ToolSearchTool`][agents.tool.ToolSearchTool]
- [`tool_namespace()`][agents.tool.tool_namespace]
- `@function_tool(defer_loading=True)` およびその他の遅延読み込み Responses ツールサーフェス

これらの機能は、Chat Completions モデルおよび Responses 以外のバックエンドでは拒否されます。遅延読み込みツールを使用する場合は、エージェントに `ToolSearchTool()` を追加し、裸の名前空間名や遅延専用の関数名を強制するのではなく、`auto` または `required` の tool choice によってモデルにツールを読み込ませてください。設定の詳細と現在の制約については、[ツール](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI ベースのモデルを使用している場合は、websocket トランスポートを選択できます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデル（`"gpt-5.5"` などの文字列モデル名を含む）に影響します。

トランスポートの選択は、SDK がモデル名をモデルインスタンスに解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket を使用し、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合、グローバルデフォルトではなく、そのプロバイダーがトランスポート選択を制御します。

#### プロバイダーまたは実行レベルの設定

websocket トランスポートは、プロバイダー単位または実行単位でも設定できます。

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

OpenAI ベースのプロバイダーは、任意のエージェント登録設定も受け付けます。これは、OpenAI のセットアップでハーネス ID などのプロバイダーレベルの登録メタデータが必要な場合の高度なオプションです。

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

プレフィックスベースのモデルルーティング（たとえば 1 回の実行で `openai/...` と `any-llm/...` のモデル名を混在させる）が必要な場合は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` は 2 つの従来のデフォルトを維持します。

- `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
- 不明なプレフィックスは、パススルーされるのではなく `UserError` を発生させます。

OpenAI プロバイダーを、リテラルな名前空間付きモデル ID を期待する OpenAI 互換エンドポイントに向ける場合は、パススルー動作を明示的に選択してください。websocket が有効なセットアップでは、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持します。

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

バックエンドがリテラルな `openai/...` 文字列を期待する場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` など、他の名前空間付きモデル ID を期待する場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは、websocket トランスポート以外の `MultiProvider` でも機能します。この例では、このセクションで説明しているトランスポート設定の一部であるため、websocket を有効にしたままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` 経由でルーティングする際に同じプロバイダーレベルの登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡すと、基盤となる OpenAI プロバイダーに転送されます。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使用している場合、websocket トランスポートには互換性のある websocket `/responses` エンドポイントも必要です。そのようなセットアップでは、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注記

- これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Responses websocket `/responses` エンドポイントをサポートしていない限り、Chat Completions や OpenAI 以外のプロバイダーには適用されません。
- 環境でまだ利用できない場合は、`websockets` パッケージをインストールしてください。
- websocket トランスポートを有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。ターンをまたいで同じ websocket 接続を再利用したいマルチターンワークフロー（およびネストされた agent-as-tool 呼び出し）では、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。
- 長い推論ターンやレイテンシスパイクのあるネットワークでは、`responses_websocket_options` で websocket の keepalive 動作をカスタマイズしてください。遅延した pong フレームを許容するには `ping_timeout` を増やし、ping を有効にしたままハートビートタイムアウトを無効にするには `ping_timeout=None` を設定します。信頼性が websocket のレイテンシより重要な場合は、HTTP/SSE トランスポートを優先してください。

## OpenAI 以外のモデル

OpenAI 以外のプロバイダーが必要な場合は、SDK の組み込みプロバイダー統合ポイントから始めます。多くのセットアップでは、サードパーティ製アダプターを追加しなくてもこれで十分です。各パターンの例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### OpenAI 以外のプロバイダー統合方法

| アプローチ | 使用する場面 | スコープ |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにしたい場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを単一の実行に適用したい場合 | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | エージェントごとに異なるプロバイダーや具体的なモデルオブジェクトが必要な場合 | エージェント単位 |
| サードパーティ製アダプター | 組み込みの経路では提供されない、アダプター管理のプロバイダー対応範囲またはルーティングが必要な場合 | [サードパーティ製アダプター](#third-party-adapters) を参照 |

これらの組み込み経路を使って、他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで使用します。これにより、「この実行内のすべてのエージェントにカスタムモデルプロバイダーを使用する」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらの例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API/モデルを使用しています。LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## 1 つのワークフローでのモデルの混在

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小さく高速なモデルを使用し、複雑なタスクにはより大きく高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定するとき、特定のモデルは次のいずれかで選択できます。

1. モデル名を渡す。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、2 つの形状はサポートする機能とツールのセットが異なるため、各ワークフローでは単一のモデル形状を使用することを推奨します。ワークフローでモデル形状を混在させる必要がある場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI モデルの名前を直接設定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

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

OpenAI Responses 経路を使用していて、より詳細な制御が必要な場合は、`ModelSettings` から始めます。

### 一般的な高度 `ModelSettings` オプション

OpenAI Responses API を使用している場合、いくつかのリクエストフィールドはすでに直接の `ModelSettings` フィールドとして用意されているため、それらに `extra_args` は不要です。

- `parallel_tool_calls`: 同じターン内で複数のツール呼び出しを許可または禁止します。
- `truncation`: コンテキストがあふれるときに失敗する代わりに、Responses API が最も古い会話項目を削除できるようにするには `"auto"` を設定します。
- `store`: 生成されたレスポンスを後で取得できるようにサーバー側に保存するかどうかを制御します。これは、レスポンス ID に依存するフォローアップワークフローや、`store=False` の場合にローカル入力へフォールバックする必要があるセッション圧縮フローで重要です。
- `context_management`: `compact_threshold` を使った Responses 圧縮など、サーバー側のコンテキスト処理を設定します。
- `prompt_cache_retention`: たとえば `"24h"` により、キャッシュされたプロンプトプレフィックスをより長く保持します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、よりリッチなレスポンスペイロードをリクエストします。
- `top_logprobs`: 出力テキストの top-token logprobs をリクエストします。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対して、runner 管理の retry 設定を有効にします。[Runner 管理の retry](#runner-managed-retries) を参照してください。

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

`store=False` を設定すると、Responses API はそのレスポンスを後でサーバー側から取得できるようには保持しません。これはステートレスまたはゼロデータ保持スタイルのフローに便利ですが、通常ならレスポンス ID を再利用する機能が、代わりにローカル管理の状態に依存する必要があることも意味します。たとえば、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されていなかった場合、デフォルトの `"auto"` 圧縮経路を入力ベースの圧縮に切り替えます。[Sessions ガイド](../sessions/index.md#openai-responses-compaction-sessions) を参照してください。

サーバー側の圧縮は [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] とは異なります。`context_management=[{"type": "compaction", "compact_threshold": ...}]` は各 Responses API リクエストとともに送信され、レンダリングされたコンテキストがしきい値を超えたとき、API はレスポンスの一部として圧縮項目を出力できます。`OpenAIResponsesCompactionSession` はターン間でスタンドアロンの `responses.compact` エンドポイントを呼び出し、ローカルのセッション履歴を書き換えます。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。それらがトップレベルで利用できない場合は、`extra_args` を使用して渡すこともできます。同じリクエストフィールドを直接の `ModelSettings` フィールドでも設定しないでください。

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

## Runner 管理の retry

retry は実行時のみで、明示的に有効化します。`ModelSettings(retry=...)` を設定し、retry ポリシーが retry を選択しない限り、SDK は一般的なモデルリクエストを retry しません。

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
| `max_retries` | `int | None` | 初回リクエスト後に許可される retry 試行回数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずに retry する場合のデフォルトの遅延戦略。 |
| `policy` | `RetryPolicy | None` | retry するかどうかを決定するコールバック。このフィールドは実行時のみで、シリアライズされません。 |

</div>

retry ポリシーは、次を含む [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。

- `attempt` と `max_retries`。試行回数を考慮した判断に使えます。
- `stream`。ストリーミングと非ストリーミングの動作を分岐できます。
- `error`。raw な検査用です。
- `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの `normalized` された事実。
- 基盤となるモデルアダプターが retry ガイダンスを提供できる場合の `provider_advice`。

ポリシーは次のいずれかを返せます。

- 単純な retry 判断用の `True` / `False`。
- 遅延を上書きしたり、診断理由を添付したりしたい場合の [`RetryDecision`][agents.retry.RetryDecision]。

SDK は、`retry_policies` で既製のヘルパーをエクスポートしています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常に無効にします。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーの retry アドバイスに従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害とタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使って retry します。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかが有効化した場合に retry します。 |
| `retry_policies.all(...)` | ネストされたすべてのポリシーが有効化した場合にのみ retry します。 |

ポリシーを合成する場合、`provider_suggested()` は最も安全な最初の構成要素です。プロバイダーが区別できる場合に、プロバイダーの拒否と再実行安全性の承認を保持するためです。

##### 安全性の境界

一部の失敗は自動的には retry されません。

- Abort エラー。
- プロバイダーのアドバイスが再実行を安全でないと示すリクエスト。
- 出力がすでに開始され、再実行が安全でなくなるような状態のストリーミング実行。

`previous_response_id` または `conversation_id` を使用するステートフルなフォローアップリクエストも、より保守的に扱われます。これらのリクエストでは、`network_error()` や `http_status([500])` などのプロバイダー以外の述語だけでは十分ではありません。retry ポリシーには、通常 `retry_policies.provider_suggested()` を介して、プロバイダーからの再実行安全の承認を含める必要があります。

##### Runner とエージェントのマージ動作

`retry` は、runner レベルとエージェントレベルの `ModelSettings` の間でディープマージされます。

- エージェントは `retry.max_retries` だけを上書きし、runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部だけを上書きし、runner から兄弟の backoff フィールドを保持できます。
- `policy` は実行時のみのため、シリアライズされた `ModelSettings` は `max_retries` と `backoff` を保持しますが、コールバック自体は省略します。

より完全な例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [アダプター対応 retry の例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

## OpenAI 以外のプロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシングに関連するエラーが発生する場合、trace が OpenAI サーバーにアップロードされる一方で、OpenAI API キーがないことが原因です。これを解決するには 3 つの選択肢があります。

1. トレーシングを完全に無効にする: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーは trace のアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものである必要があります。
3. OpenAI 以外の trace プロセッサーを使用する。[トレーシングドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだこれをサポートしていません。その結果、404 などの問題が発生する場合があります。解決するには、2 つの選択肢があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に例があります。

### structured outputs サポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その結果、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていますが、出力に使用する `json_schema` を指定できません。現在この修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することを推奨します。そうしないと、不正な形式の JSON によってアプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差を把握しておく必要があります。そうしないとエラーに遭遇する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホストされたファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

- サポートされていない `tools` を、それを理解しないプロバイダーに送らないでください
- テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外してください
- structured JSON 出力をサポートしていないプロバイダーは、ときどき無効な JSON を生成することに注意してください。

## サードパーティ製アダプター

SDK の組み込みプロバイダー統合ポイントでは不十分な場合にのみ、サードパーティ製アダプターを利用してください。この SDK で OpenAI モデルのみを使用する場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 経路を優先してください。サードパーティ製アダプターは、OpenAI モデルと OpenAI 以外のプロバイダーを組み合わせる必要がある場合、または組み込み経路では提供されないアダプター管理のプロバイダー対応範囲やルーティングが必要な場合のためのものです。アダプターは SDK と上流のモデルプロバイダーの間にもう 1 つの互換性レイヤーを追加するため、機能サポートとリクエストの意味はプロバイダーによって異なる場合があります。SDK には現在、ベストエフォートのベータ版アダプター統合として Any-LLM と LiteLLM が含まれています。

### Any-LLM

Any-LLM のサポートは、Any-LLM が管理するプロバイダー対応範囲またはルーティングが必要な場合のために、ベストエフォートのベータ版として含まれています。

上流のプロバイダー経路に応じて、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使用したり、`AnyLLMModel` を直接インスタンス化したり、実行スコープで `AnyLLMProvider` を使用したりできます。モデルサーフェスを明示的に固定する必要がある場合は、`AnyLLMModel` の構築時に `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM は引き続きサードパーティ製アダプターレイヤーであるため、プロバイダーの依存関係と機能ギャップは SDK ではなく Any-LLM によって上流で定義されます。使用量メトリクスは、上流プロバイダーがそれを返す場合に自動的に伝播されますが、ストリーミング Chat Completions バックエンドでは、usage チャンクを出力する前に `ModelSettings(include_usage=True)` が必要になる場合があります。structured outputs、ツール呼び出し、使用量レポート、Responses 固有の動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。

### LiteLLM

LiteLLM のサポートは、LiteLLM 固有のプロバイダー対応範囲またはルーティングが必要な場合のために、ベストエフォートのベータ版として含まれています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` モデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

一部の LiteLLM ベースのプロバイダーは、デフォルトでは SDK の使用量メトリクスを入力しません。使用量レポートが必要な場合は、`ModelSettings(include_usage=True)` を渡してください。また、structured outputs、ツール呼び出し、使用量レポート、アダプター固有のルーティング動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。