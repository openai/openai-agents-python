---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに使える 2 つの方式のサポートがあります。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## モデル設定の選択

ご利用の構成に合う最もシンプルな方法から始めてください。

| 次のことをしたい場合 | 推奨パス | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使用する | デフォルトの OpenAI provider と Responses モデルパスを使用する | [OpenAI モデル](#openai-models) |
| websocket トランスポート経由で OpenAI Responses API を使用する | Responses モデルパスを維持し、 websocket トランスポートを有効化する | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 1 つの非 OpenAI provider を使用する | 組み込みの provider 統合ポイントから始める | [非 OpenAI モデル](#non-openai-models) |
| エージェント間でモデルや provider を混在させる | 実行単位またはエージェント単位で provider を選択し、機能差を確認する | [1 つのワークフローでのモデル混在](#mixing-models-in-one-workflow) と [provider 間でのモデル混在](#mixing-models-across-providers) |
| OpenAI Responses の高度なリクエスト設定を調整する | OpenAI Responses パスで `ModelSettings` を使用する | [OpenAI Responses の高度な設定](#advanced-openai-responses-settings) |
| 非 OpenAI または mixed-provider ルーティング向けにサードパーティアダプターを使う | 対応する beta アダプターを比較し、リリース予定の provider パスを検証する | [サードパーティアダプター](#third-party-adapters) |

## OpenAI モデル

多くの OpenAI 専用アプリでは、推奨パスはデフォルトの OpenAI provider と文字列のモデル名を使い、 Responses モデルパスを維持することです。

`Agent` 初期化時にモデルを指定しない場合は、デフォルトモデルが使われます。現在のデフォルトは、互換性と低レイテンシのため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能であれば、明示的な `model_settings` を維持したまま、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) の設定を推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような他モデルに切り替えるには、エージェントの設定方法が 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定しないすべてのエージェントで一貫して特定モデルを使いたい場合は、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

次に、 `RunConfig` で実行単位のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの GPT-5 モデルを使うと、 SDK はデフォルトの `ModelSettings` を適用します。多くの用途で最適に動作する設定が使われます。デフォルトモデルの reasoning effort を調整するには、独自の `ModelSettings` を渡してください。

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

低レイテンシが必要な場合は、 `gpt-5.4` で `reasoning.effort="none"` を使うことを推奨します。 gpt-4.1 系列（ mini と nano を含む）も、対話型エージェントアプリ構築の有力な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、 SDK が送信するコンピュータツール payload が決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールを使い、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` payload を維持します。

主な例外は prompt 管理の呼び出しです。 prompt template がモデルを所有し、 SDK がリクエストから `model` を省略する場合、 SDK は prompt が固定するモデルを推測しないよう、 preview 互換のコンピュータ payload を既定で使います。このフローで GA パスを維持するには、リクエストで `model="gpt-5.4"` を明示するか、 `ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制してください。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、 `tool_choice="computer"` 、 `"computer_use"` 、 `"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターへ正規化されます。 `ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として動作し続けます。

preview 互換リクエストでは `environment` と表示サイズを事前にシリアライズする必要があるため、 [`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使う prompt 管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制してください。移行の詳細は [Tools](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### 非 GPT-5 モデル

非 GPT-5 のモデル名をカスタム `model_settings` なしで渡すと、 SDK は任意モデル互換の汎用 `ModelSettings` に戻ります。

### Responses 専用ツール検索機能

次のツール機能は OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` と、その他の遅延ロード Responses ツール surface

これらの機能は Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。遅延ロードツールを使う場合は、エージェントに `ToolSearchTool()` を追加し、 namespace 名や遅延専用関数名を直接強制せず、 `auto` または `required` の tool choice でモデルにツールを読み込ませてください。設定と現在の制約は [Tools](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、 OpenAI Responses API リクエストは HTTP トランスポートを使います。 OpenAI バックのモデルを使う場合は websocket トランスポートを有効化できます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルト OpenAI provider で解決される OpenAI Responses モデル（ `"gpt-5.4"` のような文字列モデル名を含む）に影響します。

トランスポート選択は、 SDK がモデル名をモデルインスタンスへ解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡した場合、そのトランスポートはすでに固定されています。 [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。 `RunConfig(model_provider=...)` を渡した場合は、その provider がグローバルデフォルトではなくトランスポート選択を制御します。

#### provider 単位または実行単位の設定

websocket トランスポートは provider 単位または実行単位でも設定できます。

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

#### `MultiProvider` による高度なルーティング

接頭辞ベースのモデルルーティング（例: 1 回の実行で `openai/...` と `any-llm/...` を混在）を行う必要がある場合は、 [`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` には 2 つの歴史的デフォルトがあります。

-   `openai/...` は OpenAI provider の別名として扱われるため、 `openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   未知の接頭辞はそのまま渡されず、 `UserError` が発生します。

OpenAI provider を、リテラルな名前空間付きモデル ID を期待する OpenAI 互換 endpoint に向ける場合は、明示的に pass-through 動作を有効化してください。 websocket 有効構成でも、 `MultiProvider` 上で `openai_use_responses_websocket=True` を維持してください。

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

バックエンドがリテラルの `openai/...` 文字列を期待する場合は `openai_prefix_mode="model_id"` を使います。バックエンドが `openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket トランスポート外の `MultiProvider` でも機能します。この例では本セクションのトランスポート設定の一部として websocket を有効のままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

カスタムの OpenAI 互換 endpoint や proxy を使う場合、 websocket トランスポートには互換 websocket `/responses` endpoint も必要です。これらの構成では `websocket_base_url` を明示設定する必要がある場合があります。

#### 注意事項

-   これは websocket トランスポート上の Responses API であり、 [Realtime API](../realtime/guide.md) ではありません。 Chat Completions や非 OpenAI provider には、 Responses websocket `/responses` endpoint をサポートしない限り適用されません。
-   環境に未導入の場合は `websockets` パッケージをインストールしてください。
-   websocket トランスポート有効化後は [`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使えます。複数ターンのワークフローで、ターン間（およびネストされた agent-as-tool 呼び出し間）で同じ websocket 接続を再利用したい場合は、 [`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

非 OpenAI provider が必要な場合は、 SDK の組み込み provider 統合ポイントから始めてください。多くの構成では、サードパーティアダプターを追加しなくても十分です。各パターンの例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### 非 OpenAI provider の統合方法

| アプローチ | 使用する場面 | スコープ |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換 endpoint をほとんどまたはすべてのエージェントのデフォルトにしたい | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタム provider を 1 回の実行に適用したい | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | 異なるエージェントで異なる provider または具体的なモデルオブジェクトが必要 | エージェント単位 |
| サードパーティアダプター | 組み込みパスでは提供されない、アダプター管理の provider カバレッジやルーティングが必要 | [サードパーティアダプター](#third-party-adapters) を参照 |

これらの組み込みパスで他の LLM provider を統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、 `AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。これは、 LLM provider が OpenAI 互換 API endpoint を持ち、 `base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行の全エージェントでカスタムモデル provider を使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は特定の Agent インスタンスでモデルを指定できます。これにより、異なるエージェントで異なる provider を組み合わせられます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーを持っていない場合は、 `set_tracing_disabled()` でトレーシングを無効化するか、 [別のトレーシングプロセッサー](../tracing.md) の設定を推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

provider = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=provider)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらの例では、多くの LLM provider がまだ Responses API をサポートしていないため、 Chat Completions API / model を使っています。 LLM provider がサポートしている場合は、 Responses の使用を推奨します。

## 1 つのワークフローでのモデル混在

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクには大きく高性能なモデルを使う、といった構成です。[`Agent`][agents.Agent] を設定する際は、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしますが、両者は対応機能やツールが異なるため、ワークフローごとに 1 つのモデル形状を使うことを推奨します。モデル形状を混在させる必要がある場合は、利用するすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使うモデルをさらに設定したい場合は、温度などの任意設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses パスでより細かい制御が必要な場合は、まず `ModelSettings` を使ってください。

### よく使う高度な `ModelSettings` オプション

OpenAI Responses API を使う場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、それらに `extra_args` は不要です。

- `parallel_tool_calls`: 同一ターンで複数のツール呼び出しを許可または禁止します。
- `truncation`: `"auto"` を設定すると、コンテキスト超過時に失敗せず、 Responses API が最も古い会話項目を削除します。
- `store`: 生成レスポンスを後で取得できるようサーバー側に保存するかを制御します。これは response ID に依存するフォローアップワークフローや、 `store=False` 時にローカル入力へフォールバックする可能性があるセッション圧縮フローで重要です。
- `prompt_cache_retention`: たとえば `"24h"` のように、キャッシュ済み prompt prefix をより長く保持します。
- `response_include`: `web_search_call.action.sources` 、 `file_search_call.results` 、 `reasoning.encrypted_content` など、よりリッチな response payload を要求します。
- `top_logprobs`: 出力テキストの top-token logprobs を要求します。 SDK は `message.output_text.logprobs` も自動追加します。
- `retry`: モデル呼び出しに対する runner 管理のリトライ設定を有効化します。 [Runner 管理リトライ](#runner-managed-retries) を参照してください。

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

`store=False` を設定すると、 Responses API はそのレスポンスを後でサーバー側取得可能な状態で保持しません。これはステートレスまたはゼロデータ保持型フローに有用ですが、通常 response ID を再利用する機能は、代わりにローカル管理状態に依存する必要があります。たとえば、 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されていない場合、デフォルトの `"auto"` 圧縮パスを入力ベース圧縮へ切り替えます。[Sessions ガイド](../sessions/index.md#openai-responses-compaction-sessions) を参照してください。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない provider 固有または新しいリクエストフィールドが必要な場合は、 `extra_args` を使ってください。

また OpenAI の Responses API では、[ほかにも任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user` 、 `service_tier` など）があります。トップレベルで利用できない場合は、これらも `extra_args` で渡せます。

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

リトライは実行時専用で、明示的な有効化が必要です。 SDK は、 `ModelSettings(retry=...)` を設定し、かつリトライポリシーがリトライを選択しない限り、一般的なモデルリクエストをリトライしません。

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

| フィールド | 型 | 備考 |
| --- | --- | --- |
| `max_retries` | `int | None` | 初回リクエスト後に許可されるリトライ回数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずにリトライする際のデフォルト遅延戦略。 |
| `policy` | `RetryPolicy | None` | リトライ可否を判断するコールバック。このフィールドは実行時専用でシリアライズされません。 |

</div>

リトライポリシーは [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。内容は次の通りです。

- `attempt` と `max_retries`（試行回数に応じた判断用）
- `stream`（ストリーミング / 非ストリーミングの分岐用）
- `error`（ raw 検査用）
- `status_code` 、 `retry_after` 、 `error_code` 、 `is_network_error` 、 `is_timeout` 、 `is_abort` などの `normalized` 情報
- 基盤モデルアダプターがリトライ指針を返せる場合の `provider_advice`

ポリシーの戻り値は次のいずれかです。

- 単純なリトライ判定の `True` / `False`
- 遅延上書きや診断理由の付与を行いたい場合の [`RetryDecision`][agents.retry.RetryDecision]

SDK は `retry_policies` に既製ヘルパーを用意しています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常に無効化。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、 provider のリトライ助言に従う。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害とタイムアウトに一致。 |
| `retry_policies.http_status([...])` | 指定した HTTP ステータスコードに一致。 |
| `retry_policies.retry_after()` | retry-after ヒントがある場合のみリトライし、その遅延を使用。 |
| `retry_policies.any(...)` | ネストポリシーのいずれかが有効化した場合にリトライ。 |
| `retry_policies.all(...)` | ネストポリシーのすべてが有効化した場合のみリトライ。 |

ポリシーを組み合わせる場合、 `provider_suggested()` は最初の構成要素として最も安全です。 provider が識別可能な場合に、 provider の拒否やリプレイ安全性承認を維持できるためです。

##### 安全境界

一部の失敗は自動リトライされません。

- Abort エラー。
- provider 助言でリプレイが unsafe とされるリクエスト。
- 出力開始後で、リプレイが unsafe になるストリーミング実行。

`previous_response_id` や `conversation_id` を使う状態保持のフォローアップリクエストも、より保守的に扱われます。これらでは `network_error()` や `http_status([500])` のような非 provider 述語だけでは不十分です。通常は `retry_policies.provider_suggested()` による provider の replay-safe 承認を含める必要があります。

##### Runner とエージェントのマージ動作

`retry` は runner レベルとエージェントレベルの `ModelSettings` 間で deep-merge されます。

- エージェントは `retry.max_retries` だけを上書きし、 runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部だけを上書きし、残りの backoff フィールドを runner から維持できます。
- `policy` は実行時専用のため、シリアライズされた `ModelSettings` には `max_retries` と `backoff` は残りますが、コールバック自体は含まれません。

より完全な例は [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [adapter-backed retry 例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

## 非 OpenAI provider のトラブルシューティング

### トレーシングクライアントエラー 401

トレーシング関連エラーが出る場合、トレースが OpenAI サーバーへアップロードされる一方、 OpenAI API キーがないことが原因です。解決策は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースアップロード専用で、 [platform.openai.com](https://platform.openai.com/) のキーである必要があります。
3. 非 OpenAI のトレースプロセッサーを使う。 [トレーシングドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、多くの他 LLM provider はまだ未対応です。その結果、 404 などの問題が発生することがあります。解決策は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部モデル provider は [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデル provider の制約です。 JSON 出力はサポートしていても、出力に使う `json_schema` を指定できません。この修正に取り組んでいますが、 JSON schema 出力をサポートする provider を使うことを推奨します。そうでない場合、アプリは不正形式 JSON により頻繁に壊れる可能性があります。

## provider 間でのモデル混在

モデル provider 間の機能差を把握しておく必要があります。把握していないとエラーになる可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、多くの他 provider はこれらをサポートしません。次の制限に注意してください。

-   未対応の `tools` を、理解しない provider に送らない
-   テキスト専用モデルを呼ぶ前に、マルチモーダル入力を除外する
-   structured JSON 出力未対応の provider は、ときどき無効な JSON を生成する可能性があることを理解する

## サードパーティアダプター

SDK の組み込み provider 統合ポイントで不足する場合にのみ、サードパーティアダプターを使ってください。この SDK で OpenAI モデルのみを使う場合、 Any-LLM や LiteLLM ではなく、組み込み [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] パスを優先してください。サードパーティアダプターは、 OpenAI モデルと非 OpenAI provider を組み合わせる必要がある場合、または組み込みパスで提供されないアダプター管理の provider カバレッジ / ルーティングが必要な場合向けです。アダプターは SDK と上流モデル provider の間に互換レイヤーを 1 つ追加するため、機能サポートやリクエストセマンティクスは provider により異なることがあります。 SDK には現在、 Any-LLM と LiteLLM の best-effort な beta 統合が含まれます。

### Any-LLM

Any-LLM サポートは、 Any-LLM 管理の provider カバレッジやルーティングが必要な場合向けに、 best-effort な beta として提供されています。

上流 provider パスに応じて、 Any-LLM は Responses API、 Chat Completions 互換 API、または provider 固有互換レイヤーを使う場合があります。

Any-LLM が必要な場合は `openai-agents[any-llm]` をインストールし、 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。 [`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使う、 `AnyLLMModel` を直接生成する、または実行スコープで `AnyLLMProvider` を使うことができます。モデル surface を明示的に固定したい場合は、 `AnyLLMModel` 構築時に `api="responses"` または `api="chat_completions"` を渡してください。

Any-LLM はサードパーティアダプターレイヤーのままなので、 provider 依存関係や機能ギャップは SDK ではなく Any-LLM 側で定義されます。利用量メトリクスは上流 provider が返す場合に自動伝播されますが、ストリーミング Chat Completions バックエンドでは利用量チャンク出力前に `ModelSettings(include_usage=True)` が必要なことがあります。 structured outputs、 tool 呼び出し、利用量レポート、 Responses 固有動作に依存する場合は、デプロイ予定の provider バックエンドを正確に検証してください。

### LiteLLM

LiteLLM サポートは、 LiteLLM 固有の provider カバレッジやルーティングが必要な場合向けに、 best-effort な beta として提供されています。

LiteLLM が必要な場合は `openai-agents[litellm]` をインストールし、 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。 `litellm/...` モデル名を使うか、 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接生成できます。

一部の LiteLLM バック provider は、デフォルトでは SDK の利用量メトリクスを埋めません。利用量レポートが必要な場合は `ModelSettings(include_usage=True)` を渡し、 structured outputs、 tool 呼び出し、利用量レポート、またはアダプター固有ルーティング動作に依存する場合は、デプロイ予定の provider バックエンドを正確に検証してください。