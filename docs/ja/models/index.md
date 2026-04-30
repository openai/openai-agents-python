---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルに対する標準サポートが 2 つの形で含まれています。

- **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
- [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

ご利用の構成に合う最もシンプルな方法から始めてください。

| やりたいこと | 推奨される方法 | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | デフォルトの OpenAI プロバイダーを Responses モデル経路で使用する | [OpenAI モデル](#openai-models) |
| WebSocket トランスポート経由で OpenAI Responses API を使用する | Responses モデル経路を維持し、WebSocket トランスポートを有効にする | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 1 つの非 OpenAI プロバイダーを使用する | 組み込みのプロバイダー統合ポイントから始める | [非 OpenAI モデル](#non-openai-models) |
| エージェント間でモデルやプロバイダーを混在させる | 実行ごと、またはエージェントごとにプロバイダーを選択し、機能差を確認する | [1 つのワークフロー内でのモデルの混在](#mixing-models-in-one-workflow) と [プロバイダー間でのモデルの混在](#mixing-models-across-providers) |
| 高度な OpenAI Responses リクエスト設定を調整する | OpenAI Responses 経路で `ModelSettings` を使用する | [高度な OpenAI Responses 設定](#advanced-openai-responses-settings) |
| 非 OpenAI または混在プロバイダーのルーティングにサードパーティ製アダプターを使用する | サポートされているベータ版アダプターを比較し、出荷予定のプロバイダー経路を検証する | [サードパーティ製アダプター](#third-party-adapters) |

## OpenAI モデル

ほとんどの OpenAI のみのアプリでは、デフォルトの OpenAI プロバイダーで文字列のモデル名を使用し、Responses モデル経路を使い続ける方法を推奨します。

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使用されます。現在のデフォルトは、互換性と低レイテンシーのため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能な場合は、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) にエージェントを設定することを推奨します。

[`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) などの他のモデルに切り替えたい場合、エージェントの設定方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.5
python3 my_awesome_agent.py
```

次に、`RunConfig` を通じて 1 回の実行のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使用されます。

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

この方法で [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) などの GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースで最もよく機能する設定が適用されます。デフォルトモデルの推論エフォートを調整するには、独自の `ModelSettings` を渡します。

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

低レイテンシーには、`gpt-5.5` で `reasoning.effort="none"` を使用することを推奨します。gpt-4.1 ファミリー（mini や nano バリアントを含む）も、インタラクティブなエージェントアプリを構築するうえで堅実な選択肢です。

#### ComputerTool のモデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、SDK が送信する computer-tool ペイロードが決まります。明示的な `gpt-5.5` リクエストでは GA 組み込みの `computer` ツールが使用され、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` ペイロードが維持されます。

主な例外は、プロンプト管理の呼び出しです。プロンプトテンプレートがモデルを所有し、SDK がリクエストから `model` を省略する場合、SDK はプロンプトがどのモデルに固定されているかを推測しないよう、プレビュー互換の computer ペイロードをデフォルトにします。このフローで GA 経路を維持するには、リクエストで `model="gpt-5.5"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制してください。

登録済みの [`ComputerTool`][agents.tool.ComputerTool] がある場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名のように引き続き動作します。

プレビュー互換リクエストでは `environment` と表示サイズを事前にシリアライズする必要があるため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリを使用するプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエストを送信する前に GA セレクターを強制する必要があります。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool)を参照してください。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡した場合、SDK は任意のモデルと互換性のある汎用 `ModelSettings` に戻ります。

### Responses 専用のツール検索機能

次のツール機能は、OpenAI Responses モデルでのみサポートされています。

- [`ToolSearchTool`][agents.tool.ToolSearchTool]
- [`tool_namespace()`][agents.tool.tool_namespace]
- `@function_tool(defer_loading=True)` およびその他の遅延読み込み Responses ツールサーフェス

これらの機能は、Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。遅延読み込みツールを使用する場合は、エージェントに `ToolSearchTool()` を追加し、裸の名前空間名や遅延専用の関数名を強制するのではなく、モデルが `auto` または `required` のツール選択を通じてツールを読み込めるようにしてください。設定の詳細と現在の制約については、[ツール](../tools.md#hosted-tool-search)を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI ベースのモデルを使用する場合、WebSocket トランスポートをオプトインできます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデル（`"gpt-5.5"` などの文字列モデル名を含む）に影響します。

トランスポートの選択は、SDK がモデル名をモデルインスタンスに解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は WebSocket を使用し、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合、グローバルデフォルトではなく、そのプロバイダーがトランスポート選択を制御します。

#### プロバイダーまたは実行レベルの設定

プロバイダーごと、または実行ごとに WebSocket トランスポートを設定することもできます。

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

OpenAI ベースのプロバイダーは、任意のエージェント登録設定も受け付けます。これは、OpenAI 設定が harness ID などのプロバイダーレベルの登録メタデータを想定している場合の高度なオプションです。

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

プレフィックスベースのモデルルーティングが必要な場合（たとえば 1 回の実行で `openai/...` と `any-llm/...` のモデル名を混在させる場合）は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` は、過去のデフォルトを 2 つ維持しています。

- `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
- 不明なプレフィックスは、そのまま渡されるのではなく `UserError` を発生させます。

OpenAI プロバイダーを、リテラルの名前空間付きモデル ID を期待する OpenAI 互換エンドポイントに向ける場合は、パススルー動作を明示的にオプトインしてください。WebSocket が有効な設定では、`MultiProvider` でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドがリテラルの `openai/...` 文字列を期待する場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` などの他の名前空間付きモデル ID を期待する場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは WebSocket トランスポート外の `MultiProvider` でも機能します。この例では、このセクションで説明しているトランスポート設定の一部であるため WebSocket を有効にしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` 経由でルーティングしながら同じプロバイダーレベルの登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡すと、基盤となる OpenAI プロバイダーに転送されます。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使用する場合、WebSocket トランスポートには互換性のある WebSocket `/responses` エンドポイントも必要です。そのような構成では、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注記

- これは WebSocket トランスポート経由の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や非 OpenAI プロバイダーには、それらが Responses WebSocket `/responses` エンドポイントをサポートしていない限り適用されません。
- 環境でまだ利用できない場合は、`websockets` パッケージをインストールしてください。
- WebSocket トランスポートを有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。ターン間（およびネストされた agent-as-tool 呼び出し）で同じ WebSocket 接続を再利用したいマルチターンワークフローでは、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md)ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

非 OpenAI プロバイダーが必要な場合は、SDK の組み込みプロバイダー統合ポイントから始めてください。多くの構成では、サードパーティ製アダプターを追加しなくてもこれで十分です。各パターンの例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### 非 OpenAI プロバイダーの統合方法

| アプローチ | 使用する場面 | スコープ |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにしたい場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを 1 回の実行に適用したい場合 | 実行ごと |
| [`Agent.model`][agents.agent.Agent.model] | 異なるエージェントに異なるプロバイダーまたは具体的なモデルオブジェクトが必要な場合 | エージェントごと |
| サードパーティ製アダプター | 組み込み経路では提供されない、アダプター管理のプロバイダーカバレッジやルーティングが必要な場合 | [サードパーティ製アダプター](#third-party-adapters)を参照 |

これらの組み込み経路で他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより、「この実行内のすべてのエージェントにカスタムモデルプロバイダーを使用する」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] により、特定の Agent インスタンスでモデルを指定できます。これにより、異なるエージェントに対して異なるプロバイダーを組み合わせて使用できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md)を設定することを推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらの例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API/モデルを使用しています。ご利用の LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## 1 つのワークフロー内でのモデルの混在

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小さく高速なモデルを使用し、複雑なタスクにはより大きく高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定する際、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、各ワークフローでは単一のモデル形状を使用することを推奨します。これは、2 つの形状がサポートする機能とツールのセットが異なるためです。ワークフローでモデル形状を組み合わせる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

OpenAI Responses 経路を使用していて、より詳細に制御したい場合は、`ModelSettings` から始めてください。

### 一般的な高度な `ModelSettings` オプション

OpenAI Responses API を使用している場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、それらに `extra_args` は不要です。

- `parallel_tool_calls`: 同じターン内で複数のツール呼び出しを許可または禁止します。
- `truncation`: コンテキストがあふれる場合に失敗する代わりに、Responses API が最も古い会話項目を削除できるようにするには `"auto"` を設定します。
- `store`: 生成されたレスポンスを後で取得できるようサーバー側に保存するかどうかを制御します。これは、レスポンス ID に依存する後続ワークフローや、`store=False` の場合にローカル入力へフォールバックする必要があるセッション圧縮フローで重要です。
- `prompt_cache_retention`: たとえば `"24h"` で、キャッシュされたプロンプト接頭辞をより長く保持します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より豊富なレスポンスペイロードをリクエストします。
- `top_logprobs`: 出力テキストの上位トークン logprobs をリクエストします。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対する runner 管理のリトライ設定をオプトインします。[Runner 管理のリトライ](#runner-managed-retries)を参照してください。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.5",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

`store=False` を設定すると、Responses API はそのレスポンスを後でサーバー側から取得できるようには保持しません。これはステートレスまたはゼロデータ保持スタイルのフローに役立ちますが、一方で、通常ならレスポンス ID を再利用する機能が、代わりにローカルで管理される状態に依存する必要があることも意味します。たとえば、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されていなかった場合、デフォルトの `"auto"` 圧縮経路を入力ベースの圧縮に切り替えます。[セッションガイド](../sessions/index.md#openai-responses-compaction-sessions)を参照してください。

### `extra_args` の渡し方

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、それらも `extra_args` で渡せます。

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

## Runner 管理のリトライ

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

`ModelRetrySettings` には 3 つのフィールドがあります。

<div class="field-table" markdown="1">

| フィールド | 型 | 注記 |
| --- | --- | --- |
| `max_retries` | `int | None` | 初回リクエスト後に許可されるリトライ試行回数です。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずにリトライする場合のデフォルト遅延戦略です。 |
| `policy` | `RetryPolicy | None` | リトライするかどうかを決定するコールバックです。このフィールドは実行時専用で、シリアライズされません。 |

</div>

リトライポリシーは、次を含む [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。

- 試行を考慮した判断を行えるようにする `attempt` と `max_retries`。
- ストリーミングと非ストリーミングの挙動を分岐できるようにする `stream`。
- raw な検査用の `error`。
- `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの正規化された事実を表す `normalized`。
- 基盤となるモデルアダプターがリトライガイダンスを提供できる場合の `provider_advice`。

ポリシーは次のいずれかを返せます。

- 単純なリトライ判断としての `True` / `False`。
- 遅延を上書きしたい場合や診断理由を添付したい場合の [`RetryDecision`][agents.retry.RetryDecision]。

SDK は、`retry_policies` でそのまま使えるヘルパーをエクスポートしています。

| ヘルパー | 挙動 |
| --- | --- |
| `retry_policies.never()` | 常にオプトアウトします。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーのリトライ助言に従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害やタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使用してリトライします。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかがオプトインした場合にリトライします。 |
| `retry_policies.all(...)` | ネストされたすべてのポリシーがオプトインした場合にのみリトライします。 |

ポリシーを合成する場合、`provider_suggested()` は最も安全な最初の構成要素です。プロバイダーがそれらを区別できる場合、プロバイダーの拒否とリプレイ安全性の承認を保持するためです。

##### 安全境界

一部の失敗は自動的には決してリトライされません。

- 中断エラー。
- プロバイダー助言がリプレイを安全でないと示すリクエスト。
- 出力がすでに開始され、リプレイが安全でなくなるようなストリーミング実行。

`previous_response_id` または `conversation_id` を使用するステートフルな後続リクエストも、より保守的に扱われます。これらのリクエストでは、`network_error()` や `http_status([500])` のような非プロバイダー述語だけでは十分ではありません。リトライポリシーには、通常 `retry_policies.provider_suggested()` を通じて、プロバイダーからのリプレイ安全性の承認を含める必要があります。

##### Runner とエージェントのマージ動作

`retry` は、runner レベルとエージェントレベルの `ModelSettings` の間でディープマージされます。

- エージェントは `retry.max_retries` だけを上書きし、runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部だけを上書きし、runner から兄弟の backoff フィールドを維持できます。
- `policy` は実行時専用のため、シリアライズされた `ModelSettings` は `max_retries` と `backoff` を保持しますが、コールバック自体は省略します。

より詳しい例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [アダプターを使用したリトライ例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)を参照してください。

## 非 OpenAI プロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシングに関連するエラーが発生する場合、これはトレースが OpenAI サーバーにアップロードされるためであり、OpenAI API キーを持っていないことが原因です。これを解決するには 3 つの選択肢があります。

1. トレーシングを完全に無効にする: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものである必要があります。
3. 非 OpenAI のトレースプロセッサーを使用する。[トレーシングドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだこれをサポートしていません。その結果、404 や類似の問題が発生する場合があります。解決するには 2 つの選択肢があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs サポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていますが、出力に使用する `json_schema` を指定できません。これについては修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することを推奨します。そうしないと、不正な形式の JSON によってアプリが頻繁に壊れるためです。

## プロバイダー間でのモデルの混在

モデルプロバイダー間の機能差を認識しておく必要があります。そうしないとエラーに遭遇する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

- サポートされていない `tools` を、それを理解しないプロバイダーに送信しないでください
- テキスト専用のモデルを呼び出す前に、マルチモーダル入力を除外してください
- structured JSON 出力をサポートしていないプロバイダーは、ときどき無効な JSON を生成することに注意してください。

## サードパーティ製アダプター

サードパーティ製アダプターは、SDK の組み込みプロバイダー統合ポイントだけでは不十分な場合にのみ使用してください。この SDK で OpenAI モデルのみを使用している場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 経路を優先してください。サードパーティ製アダプターは、OpenAI モデルと非 OpenAI プロバイダーを組み合わせる必要がある場合、または組み込み経路では提供されないアダプター管理のプロバイダーカバレッジやルーティングが必要な場合のためのものです。アダプターは SDK と上流のモデルプロバイダーの間に追加の互換性レイヤーを加えるため、機能サポートとリクエストの意味論はプロバイダーによって異なる場合があります。SDK には現在、ベストエフォートのベータ版アダプター統合として Any-LLM と LiteLLM が含まれています。

### Any-LLM

Any-LLM サポートは、Any-LLM が管理するプロバイダーカバレッジやルーティングが必要な場合のために、ベストエフォートのベータ版として含まれています。

上流プロバイダーの経路に応じて、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使用したり、`AnyLLMModel` を直接インスタンス化したり、実行スコープで `AnyLLMProvider` を使用したりできます。モデルサーフェスを明示的に固定する必要がある場合は、`AnyLLMModel` の構築時に `api="responses"` または `api="chat_completions"` を渡してください。

Any-LLM は引き続きサードパーティ製アダプターレイヤーであるため、プロバイダーの依存関係と機能ギャップは SDK ではなく、上流の Any-LLM によって定義されます。利用メトリクスは、上流プロバイダーが返す場合に自動的に伝播されますが、ストリーミングされる Chat Completions バックエンドでは、使用量チャンクを出力する前に `ModelSettings(include_usage=True)` が必要な場合があります。structured outputs、ツール呼び出し、使用量レポート、または Responses 固有の動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。

### LiteLLM

LiteLLM サポートは、LiteLLM 固有のプロバイダーカバレッジやルーティングが必要な場合のために、ベストエフォートのベータ版として含まれています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` モデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

一部の LiteLLM ベースのプロバイダーは、デフォルトでは SDK の使用量メトリクスを設定しません。使用量レポートが必要な場合は、`ModelSettings(include_usage=True)` を渡し、structured outputs、ツール呼び出し、使用量レポート、またはアダプター固有のルーティング動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。