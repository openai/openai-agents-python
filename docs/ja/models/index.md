---
search:
  exclude: true
---
# モデル

Agents SDK には、 OpenAI モデル向けの即時利用可能なサポートが 2 つの形で用意されています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## モデル設定の選択

まずは、構成に合う最もシンプルな方法から始めてください。

| 次のことをしたい場合 | 推奨パス | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | 既定の OpenAI プロバイダーと Responses モデルパスを使用する | [OpenAI モデル](#openai-models) |
| websocket トランスポートで OpenAI Responses API を使用する | Responses モデルパスを維持し、 websocket トランスポートを有効化する | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 1 つの非 OpenAI プロバイダーを使用する | 組み込みのプロバイダー統合ポイントから始める | [非 OpenAI モデル](#non-openai-models) |
| エージェント間でモデルまたはプロバイダーを混在させる | 実行ごとまたはエージェントごとにプロバイダーを選択し、機能差を確認する | [1 つのワークフローでのモデル混在](#mixing-models-in-one-workflow) と [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| OpenAI Responses の高度なリクエスト設定を調整する | OpenAI Responses パスで `ModelSettings` を使用する | [高度な OpenAI Responses 設定](#advanced-openai-responses-settings) |
| 非 OpenAI または混在プロバイダーのルーティングにサードパーティーアダプターを使う | サポートされているベータアダプターを比較し、出荷予定のプロバイダーパスを検証する | [サードパーティーアダプター](#third-party-adapters) |

## OpenAI モデル

ほとんどの OpenAI 専用アプリでは、既定の OpenAI プロバイダーで文字列のモデル名を使い、 Responses モデルパスを使い続けるのが推奨です。

`Agent` 初期化時にモデルを指定しない場合は、既定モデルが使われます。現在の既定は互換性と低レイテンシのため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能な場合は、明示的な `model_settings` を維持したまま、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) にエージェントを設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような他モデルへ切り替えるには、エージェントの設定方法が 2 つあります。

### 既定モデル

1 つ目として、カスタムモデルを設定しないすべてのエージェントで特定モデルを一貫して使いたい場合は、エージェント実行前に環境変数 `OPENAI_DEFAULT_MODEL` を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

2 つ目として、 `RunConfig` で実行単位の既定モデルを設定できます。エージェントにモデルを設定しなければ、この実行のモデルが使われます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.4"),
)
```

#### GPT-5 モデル

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの GPT-5 モデルを使う場合、 SDK は既定の `ModelSettings` を適用します。これは多くのユースケースで最適に動く設定です。既定モデルの推論 effort を調整するには、独自の `ModelSettings` を渡してください。

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.4 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.4",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

低レイテンシには、 `gpt-5.4` で `reasoning.effort="none"` を使うことを推奨します。 gpt-4.1 ファミリー（ mini と nano を含む）も、対話型エージェントアプリ構築において引き続き有力な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、 SDK が送信するコンピュータツール payload が決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールを使い、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` payload を維持します。

主な例外はプロンプト管理呼び出しです。プロンプトテンプレートがモデルを保持し、 SDK がリクエストから `model` を省略する場合、 SDK はプロンプトが固定するモデルを推測しないため、 preview 互換のコンピュータ payload を既定で使います。このフローで GA パスを維持するには、リクエストで `model="gpt-5.4"` を明示するか、 `ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制してください。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、 `tool_choice="computer"` 、 `"computer_use"` 、 `"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。 `ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として動作し続けます。

preview 互換リクエストでは、 `environment` と表示寸法を先にシリアライズする必要があるため、 [`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使うプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細は [Tools](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡すと、 SDK は任意モデル互換の汎用 `ModelSettings` に戻ります。

### Responses 専用ツール検索機能

次のツール機能は OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` と、その他の遅延読み込み Responses ツールサーフェス

これらの機能は Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。遅延読み込みツールを使う場合は、エージェントに `ToolSearchTool()` を追加し、 namespace 名や遅延専用関数名を強制する代わりに、 `auto` または `required` の tool choice でモデルにツールを読み込ませてください。設定詳細と現在の制約は [Tools](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket トランスポート

既定では、 OpenAI Responses API リクエストは HTTP トランスポートを使用します。 OpenAI バックエンドモデル使用時は websocket トランスポートを有効化できます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、既定の OpenAI プロバイダーで解決される OpenAI Responses モデル（ `"gpt-5.4"` のような文字列モデル名を含む）に影響します。

トランスポート選択は、 SDK がモデル名をモデルインスタンスへ解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートは既に固定されています。 [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。 `RunConfig(model_provider=...)` を渡した場合、グローバル既定ではなくそのプロバイダーがトランスポート選択を制御します。

#### プロバイダーまたは実行レベル設定

websocket トランスポートはプロバイダー単位または実行単位でも設定できます。

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

#### `MultiProvider` を使った高度なルーティング

プレフィックスベースのモデルルーティングが必要な場合（例: 1 回の実行で `openai/...` と `any-llm/...` を混在）、 [`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` は 2 つの従来既定を維持します。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、 `openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスはそのまま渡されず、 `UserError` を発生させます。

OpenAI 互換エンドポイントが名前空間付きモデル ID の文字列そのものを期待する場合は、明示的にパススルー動作を有効化してください。 websocket 有効構成では、 `MultiProvider` 側でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドが文字列 `openai/...` をそのまま期待する場合は `openai_prefix_mode="model_id"` を使います。バックエンドが `openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket 以外の `MultiProvider` でも利用可能です。この例では本セクションのトランスポート設定の一部として websocket を有効のままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

カスタム OpenAI 互換エンドポイントまたはプロキシを使う場合、 websocket トランスポートには互換 websocket `/responses` エンドポイントも必要です。その構成では `websocket_base_url` を明示設定する必要がある場合があります。

#### 注意事項

-   これは websocket トランスポート上の Responses API であり、 [Realtime API](../realtime/guide.md) ではありません。 Chat Completions や、 Responses websocket `/responses` エンドポイントをサポートしない非 OpenAI プロバイダーには適用されません。
-   環境で未導入の場合は `websockets` パッケージをインストールしてください。
-   websocket トランスポート有効化後は [`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。同じ websocket 接続をターン間（ネストした agent-as-tool 呼び出しを含む）で再利用したいマルチターンワークフローでは、 [`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドおよび [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

非 OpenAI プロバイダーが必要な場合は、 SDK の組み込みプロバイダー統合ポイントから始めてください。多くの構成では、サードパーティーアダプターを追加せずに十分対応できます。各パターンの例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### 非 OpenAI プロバイダー統合方法

| アプローチ | 使用する場面 | スコープ |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントをほとんどまたはすべてのエージェントの既定にしたい | グローバル既定 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを単一実行に適用したい | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | 異なるエージェントに異なるプロバイダーまたは具体的モデルオブジェクトが必要 | エージェント単位 |
| サードパーティーアダプター | 組み込みパスで提供されない、アダプター管理のプロバイダー対応またはルーティングが必要 | [サードパーティーアダプター](#third-party-adapters) を参照 |

これらの組み込みパスで他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、 `AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使用したい場合に有用です。これは、 LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、 `base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は特定の Agent インスタンスでモデルを指定できます。これにより、異なるエージェントに対して異なるプロバイダーを組み合わせられます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーがない場合は、 `set_tracing_disabled()` でトレーシングを無効化するか、 [別のトレーシングプロセッサー](../tracing.md) の設定を推奨します。

!!! note

    これらの例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、 Chat Completions API / モデルを使用しています。お使いの LLM プロバイダーが対応している場合は、 Responses の使用を推奨します。

## 1 つのワークフローでのモデル混在

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクにはより大規模で高機能なモデルを使えます。[`Agent`][agents.Agent] を設定する際、特定モデルは次のいずれかで選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマップ可能な [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしますが、 2 つは対応機能とツールが異なるため、ワークフローごとに 1 つのモデル形に統一することを推奨します。ワークフローでモデル形の混在が必要な場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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
    model="gpt-5.4",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  OpenAI モデル名を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を提供します。

エージェントで使うモデルをさらに設定したい場合は、 temperature などの任意パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses パスでより細かな制御が必要な場合は、まず `ModelSettings` を使用してください。

### 一般的な高度 `ModelSettings` オプション

OpenAI Responses API 使用時は、いくつかのリクエストフィールドに対応する `ModelSettings` フィールドが既にあるため、それらには `extra_args` は不要です。

- `parallel_tool_calls`: 同一ターンでの複数ツール呼び出しを許可または禁止します。
- `truncation`: コンテキストあふれ時に失敗する代わりに、 Responses API に最古の会話項目を削除させるには `"auto"` を設定します。
- `store`: 生成された応答を後で取得できるようサーバー側に保存するかを制御します。これは、 response ID に依存するフォローアップワークフローや、 `store=False` 時にローカル入力へフォールバックが必要なセッション圧縮フローで重要です。
- `prompt_cache_retention`: たとえば `"24h"` のように、キャッシュされたプロンプト接頭辞をより長く保持します。
- `response_include`: `web_search_call.action.sources` 、 `file_search_call.results` 、 `reasoning.encrypted_content` など、より豊富な応答 payload を要求します。
- `top_logprobs`: 出力テキストの上位トークン logprobs を要求します。 SDK は `message.output_text.logprobs` も自動追加します。
- `retry`: モデル呼び出しにランナー管理リトライ設定を有効化します。[ランナー管理リトライ](#runner-managed-retries) を参照してください。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.4",
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

`store=False` を設定すると、 Responses API はその応答を後でサーバー側取得可能な状態に保持しません。これはステートレスまたはゼロデータ保持スタイルのフローで有用ですが、通常 response ID を再利用する機能は、代わりにローカル管理状態に依存する必要があります。たとえば、 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後の応答が保存されていない場合、既定の `"auto"` 圧縮パスを入力ベース圧縮に切り替えます。[Sessions ガイド](../sessions/index.md#openai-responses-compaction-sessions) を参照してください。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は `extra_args` を使います。

また OpenAI の Responses API 使用時には、[その他の任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user` 、 `service_tier` など）があります。トップレベルにない場合、これらも `extra_args` で渡せます。

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

## ランナー管理リトライ

リトライは実行時専用で、明示的に有効化する必要があります。 SDK は `ModelSettings(retry=...)` を設定し、かつリトライポリシーがリトライを選択した場合を除き、一般的なモデルリクエストをリトライしません。

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.4",
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
| `max_retries` | `int | None` | 初回リクエスト後に許可されるリトライ試行回数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示遅延を返さずにリトライする場合の既定遅延戦略。 |
| `policy` | `RetryPolicy | None` | リトライ可否を判断するコールバック。このフィールドは実行時専用でシリアライズされません。 |

</div>

リトライポリシーは、 [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。内容は次のとおりです。

- `attempt` と `max_retries` （試行回数を考慮した判断用）
- `stream` （ストリーミング / 非ストリーミング分岐用）
- `error` （raw 検査用）
- `normalized` 情報（ `status_code` 、 `retry_after` 、 `error_code` 、 `is_network_error` 、 `is_timeout` 、 `is_abort` など）
- 基盤モデルアダプターがリトライ指針を提供できる場合の `provider_advice`

ポリシーは次のいずれかを返せます。

- 単純なリトライ判断として `True` / `False`
- 遅延上書きや診断理由付与をしたい場合の [`RetryDecision`][agents.retry.RetryDecision]

SDK は `retry_policies` に既製ヘルパーを提供しています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常に無効化します。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーのリトライ助言に従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート / タイムアウト失敗に一致します。 |
| `retry_policies.http_status([...])` | 指定した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントがある場合のみ、その遅延でリトライします。 |
| `retry_policies.any(...)` | ネストしたポリシーのいずれかが有効化したらリトライします。 |
| `retry_policies.all(...)` | ネストしたポリシーのすべてが有効化した場合のみリトライします。 |

ポリシーを組み合わせる場合、 `provider_suggested()` は最も安全な最初の構成要素です。プロバイダーが判別可能な場合、プロバイダー側の拒否や再実行安全性承認を維持できるためです。

##### 安全境界

一部の失敗は自動リトライされません。

- 中断エラー。
- プロバイダー助言で再実行が unsafe とされたリクエスト。
- 出力開始後で再実行が unsafe になるストリーミング実行。

`previous_response_id` または `conversation_id` を使う状態保持フォローアップリクエストも、より保守的に扱われます。これらでは `network_error()` や `http_status([500])` のような非プロバイダー述語だけでは不十分です。リトライポリシーには通常 `retry_policies.provider_suggested()` を通じた、プロバイダー由来の replay-safe 承認を含める必要があります。

##### ランナーとエージェントのマージ動作

`retry` はランナーレベルとエージェントレベルの `ModelSettings` 間でディープマージされます。

- エージェントは `retry.max_retries` のみ上書きし、ランナーの `policy` を継承できます。
- エージェントは `retry.backoff` の一部のみ上書きし、兄弟 backoff フィールドをランナーから維持できます。
- `policy` は実行時専用のため、シリアライズされた `ModelSettings` では `max_retries` と `backoff` は保持されますが、コールバック自体は除外されます。

より完全な例は [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [adapter-backed retry example](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

## 非 OpenAI プロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシング関連エラーが出る場合、トレースは OpenAI サーバーへアップロードされる一方で OpenAI API キーがないことが原因です。解決策は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用 OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースアップロード専用で、 [platform.openai.com](https://platform.openai.com/) 発行である必要があります。
3. 非 OpenAI のトレースプロセッサーを使う。[tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK は既定で Responses API を使いますが、他の多くの LLM プロバイダーはまだ対応していません。その結果として 404 などの問題が発生する場合があります。解決策は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。この場合、次のようなエラーになることがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデルプロバイダーの制約です。 JSON 出力はサポートしていても、出力に使用する `json_schema` の指定を許可していません。現在修正に取り組んでいますが、 JSON schema 出力をサポートするプロバイダーの利用を推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を把握しておく必要があります。そうしないとエラーになる可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、多くの他プロバイダーはこれらをサポートしません。次の制約に注意してください。

-   非対応プロバイダーへ、対応していない `tools` を送らない
-   テキスト専用モデル呼び出し前にマルチモーダル入力を除外する
-   structured JSON 出力非対応プロバイダーは無効 JSON を時々生成する可能性があることを理解する

## サードパーティーアダプター

SDK の組み込みプロバイダー統合ポイントで不足する場合のみ、サードパーティーアダプターを選択してください。この SDK で OpenAI モデルのみを使用する場合、 Any-LLM や LiteLLM ではなく組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] パスを優先してください。サードパーティーアダプターは、 OpenAI モデルと非 OpenAI プロバイダーを組み合わせる必要がある場合、または組み込みパスで提供されないアダプター管理のプロバイダー対応 / ルーティングが必要な場合向けです。アダプターは SDK と上流モデルプロバイダーの間に別の互換レイヤーを追加するため、機能サポートやリクエスト意味論はプロバイダーごとに異なります。 SDK には現在、 Any-LLM と LiteLLM がベストエフォートのベータ統合として含まれています。

### Any-LLM

Any-LLM サポートは、 Any-LLM 管理のプロバイダー対応またはルーティングが必要な場合向けに、ベストエフォートのベータとして含まれています。

上流プロバイダーパスに応じて、 Any-LLM は Responses API、 Chat Completions 互換 API、またはプロバイダー固有互換レイヤーを使う場合があります。

Any-LLM が必要な場合は `openai-agents[any-llm]` をインストールし、 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使うか、 `AnyLLMModel` を直接インスタンス化するか、実行スコープで `AnyLLMProvider` を使えます。モデルサーフェスを明示固定したい場合は、 `AnyLLMModel` 構築時に `api="responses"` または `api="chat_completions"` を渡してください。

Any-LLM はサードパーティーアダプターレイヤーであるため、プロバイダー依存関係と機能差分は SDK ではなく Any-LLM 側で定義されます。使用量メトリクスは上流プロバイダーが返す場合に自動伝播されますが、ストリーミング Chat Completions バックエンドでは usage チャンク出力前に `ModelSettings(include_usage=True)` が必要な場合があります。structured outputs、ツール呼び出し、 usage レポート、 Responses 固有動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。

### LiteLLM

LiteLLM サポートは、 LiteLLM 固有のプロバイダー対応またはルーティングが必要な場合向けに、ベストエフォートのベータとして含まれています。

LiteLLM が必要な場合は `openai-agents[litellm]` をインストールし、 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。 `litellm/...` モデル名を使うか、 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

一部の LiteLLM バックエンドプロバイダーは既定で SDK usage メトリクスを埋めません。 usage レポートが必要な場合は `ModelSettings(include_usage=True)` を渡し、 structured outputs、ツール呼び出し、 usage レポート、またはアダプター固有ルーティング動作に依存する場合は、デプロイ予定の正確なプロバイダーバックエンドを検証してください。