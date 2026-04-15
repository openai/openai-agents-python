---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデル向けの即時利用可能なサポートが 2 つの形式で含まれています:

- **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
- [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

ご利用環境に合う最もシンプルな経路から開始してください:

| If you are trying to... | Recommended path | Read more |
| --- | --- | --- |
| OpenAI モデルのみを使用する | 既定の OpenAI provider と Responses model path を使用する | [OpenAI モデル](#openai-models) |
| websocket transport で OpenAI Responses API を使用する | Responses model path を維持し、websocket transport を有効化する | [Responses WebSocket transport](#responses-websocket-transport) |
| 1 つの non-OpenAI provider を使用する | 組み込み provider 統合ポイントから開始する | [Non-OpenAI モデル](#non-openai-models) |
| エージェント間でモデルまたは provider を混在させる | 実行ごとまたはエージェントごとに provider を選択し、機能差を確認する | [1 つのワークフローでのモデル混在](#mixing-models-in-one-workflow) と [provider 間でのモデル混在](#mixing-models-across-providers) |
| 高度な OpenAI Responses リクエスト設定を調整する | OpenAI Responses path で `ModelSettings` を使用する | [高度な OpenAI Responses 設定](#advanced-openai-responses-settings) |
| non-OpenAI または mixed-provider ルーティング用にサードパーティ adapter を使用する | サポートされる beta adapter を比較し、提供予定の provider path を検証する | [サードパーティ adapter](#third-party-adapters) |

## OpenAI モデル

ほとんどの OpenAI 専用アプリでは、推奨経路は既定の OpenAI provider で文字列のモデル名を使い、Responses model path を維持することです。

`Agent` の初期化時にモデルを指定しない場合、既定モデルが使用されます。現在の既定は互換性と低遅延のため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能であれば、明示的な `model_settings` を維持したまま、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) をエージェントに設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような他モデルへ切り替える場合、エージェントを設定する方法は 2 つあります。

### 既定モデル

まず、カスタムモデルを設定していないすべてのエージェントで特定モデルを一貫して使いたい場合は、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

次に、`RunConfig` を通じて実行単位の既定モデルを設定できます。エージェントにモデルを設定しない場合は、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) など任意の GPT-5 モデルを使うと、SDK は既定の `ModelSettings` を適用します。ほとんどのユースケースで最適に動作する設定です。既定モデルの推論 effort を調整するには、独自の `ModelSettings` を渡します:

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

より低遅延にするには、`gpt-5.4` で `reasoning.effort="none"` の使用が推奨されます。gpt-4.1 ファミリー（ mini / nano バリアントを含む）も、対話型エージェントアプリ構築における堅実な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルにより、SDK が送信するコンピュータツール payload が決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールを使用し、明示的な `computer-use-preview` リクエストでは旧 `computer_use_preview` payload を維持します。

主な例外は prompt 管理呼び出しです。prompt template がモデルを管理し、SDK がリクエストから `model` を省略する場合、SDK は prompt 固定モデルを推測しないよう preview 互換のコンピュータ payload を既定で使います。このフローで GA path を維持するには、リクエストで `model="gpt-5.4"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は有効リクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として動作し続けます。

preview 互換リクエストでは `environment` と表示寸法を事前に serialize する必要があるため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使う prompt 管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細は [Tools](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡した場合、SDK は任意モデル互換の汎用 `ModelSettings` に戻ります。

### Responses 専用ツール検索機能

以下のツール機能は OpenAI Responses モデルでのみサポートされます:

- [`ToolSearchTool`][agents.tool.ToolSearchTool]
- [`tool_namespace()`][agents.tool.tool_namespace]
- `@function_tool(defer_loading=True)` およびその他の deferred-loading Responses ツール surface

これらの機能は Chat Completions モデルと non-Responses backend では拒否されます。deferred-loading ツールを使う場合は、エージェントに `ToolSearchTool()` を追加し、素の namespace 名や deferred 専用関数名を強制せず、`auto` または `required` の tool choice でモデルにツールをロードさせてください。設定詳細と現在の制約は [Tools](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket transport

既定では、OpenAI Responses API リクエストは HTTP transport を使います。OpenAI バックエンドモデル使用時に websocket transport を有効化できます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは既定の OpenAI provider により解決される OpenAI Responses モデル（`"gpt-5.4"` などの文字列モデル名を含む）に影響します。

transport の選択は、SDK がモデル名をモデルインスタンスへ解決する時点で行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、その transport はすでに固定です: [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡した場合、global default ではなくその provider が transport 選択を制御します。

#### provider / 実行レベル設定

websocket transport は provider 単位または実行単位でも設定できます:

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

OpenAI バックエンド provider は任意のエージェント登録設定も受け付けます。これは OpenAI 設定が harness ID などの provider レベル登録メタデータを期待するケース向けの高度なオプションです。

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

prefix ベースのモデルルーティング（例: 1 回の実行で `openai/...` と `any-llm/...` モデル名を混在）を必要とする場合は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は 2 つの履歴的既定値を維持します:

- `openai/...` は OpenAI provider の alias として扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
- 不明な prefix は pass-through されず `UserError` を発生させます。

OpenAI provider を、文字通り namespaced モデル ID を期待する OpenAI 互換 endpoint に向ける場合は、明示的に pass-through 動作を有効化してください。websocket 有効構成では、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持します:

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

backend が文字列 `openai/...` をそのまま期待する場合は `openai_prefix_mode="model_id"` を使います。`openrouter/openai/gpt-4.1-mini` のような他の namespaced モデル ID を backend が期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket transport 外の `MultiProvider` でも動作します。この例で websocket を有効にしているのは、この節で説明している transport 設定の一部だからです。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用可能です。

`MultiProvider` 経由ルーティング時にも同じ provider レベル登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡すと、基盤の OpenAI provider へ転送されます。

カスタム OpenAI 互換 endpoint または proxy を使う場合、websocket transport には互換 websocket `/responses` endpoint も必要です。これらの構成では `websocket_base_url` を明示設定する必要がある場合があります。

#### 注記

- これは websocket transport 上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や、Responses websocket `/responses` endpoint をサポートしない non-OpenAI provider には適用されません。
- 環境に未導入であれば `websockets` パッケージをインストールしてください。
- websocket transport 有効化後は [`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで同一 websocket 接続をターン間（およびネストした agent-as-tool 呼び出し間）で再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## Non-OpenAI モデル

non-OpenAI provider が必要な場合、まず SDK 組み込みの provider 統合ポイントから始めてください。多くの構成ではサードパーティ adapter を追加せずに十分です。各パターンの例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### non-OpenAI provider 統合方法

| Approach | Use it when | Scope |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換 endpoint を大半または全エージェントの既定にしたい | グローバル既定 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタム provider を単一実行に適用したい | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | エージェントごとに異なる provider または具体モデルオブジェクトが必要 | エージェント単位 |
| サードパーティ adapter | 組み込み経路で提供されない adapter 管理の provider カバレッジまたはルーティングが必要 | [サードパーティ adapters](#third-party-adapters) を参照 |

これらの組み込み経路で他の LLM provider を統合できます:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` インスタンスを LLM クライアントとしてグローバル利用したい場合に有用です。これは LLM provider が OpenAI 互換 API endpoint を持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行の全エージェントでカスタムモデル provider を使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は特定 Agent インスタンスでモデルを指定できます。これによりエージェントごとに異なる provider を混在できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API key がない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらの例では、多くの LLM provider がまだ Responses API をサポートしていないため、Chat Completions API / model を使用しています。LLM provider が対応している場合は Responses の使用を推奨します。

## 1 つのワークフローでのモデル混在

単一ワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージにはより小型で高速なモデル、複雑タスクにはより大型で高性能なモデルを使えます。[`Agent`][agents.Agent] 設定時は、次のいずれかで特定モデルを選択できます:

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両 shape をサポートしますが、2 つの shape は対応機能とツール集合が異なるため、各ワークフローでは単一 shape の使用を推奨します。shape を混在させる必要がある場合は、使用する全機能が両方で利用可能であることを確認してください。

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

1. OpenAI モデル名を直接設定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

エージェントで使用するモデルをさらに設定したい場合は、temperature などの任意モデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses path でより細かい制御が必要な場合は、まず `ModelSettings` から始めてください。

### 一般的な高度 `ModelSettings` オプション

OpenAI Responses API 使用時、いくつかのリクエストフィールドには対応する `ModelSettings` フィールドがすでにあるため、それらには `extra_args` は不要です。

- `parallel_tool_calls`: 同一ターンでの複数ツール呼び出しを許可または禁止します。
- `truncation`: context あふれ時に失敗させる代わりに、Responses API が最も古い会話項目を削除するよう `"auto"` を設定します。
- `store`: 生成応答を後で取得できるようサーバー側に保存するかを制御します。これは response ID に依存するフォローアップワークフローや、`store=False` 時にローカル入力へフォールバックが必要になり得るセッション圧縮フローで重要です。
- `prompt_cache_retention`: たとえば `"24h"` でキャッシュ済み prompt prefix をより長く保持します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、よりリッチな応答 payload を要求します。
- `top_logprobs`: 出力テキストの top-token logprobs を要求します。SDK は `message.output_text.logprobs` も自動追加します。
- `retry`: モデル呼び出しに runner 管理リトライ設定を opt in します。[Runner 管理リトライ](#runner-managed-retries) を参照してください。

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

`store=False` を設定すると、Responses API はその応答を後でサーバー側取得できる状態で保持しません。これは stateless またはゼロデータ保持スタイルのフローに有用ですが、通常 response ID を再利用する機能が、代わりにローカル管理状態へ依存することも意味します。たとえば [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後の応答が保存されていない場合、既定 `"auto"` 圧縮経路を input ベース圧縮へ切り替えます。[Sessions ガイド](../sessions/index.md#openai-responses-compaction-sessions) を参照してください。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない provider 固有または新しいリクエストフィールドが必要な場合は `extra_args` を使います。

また OpenAI の Responses API 使用時は、[他にもいくつか任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、`extra_args` で渡せます。

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

リトライは実行時専用で opt in です。`ModelSettings(retry=...)` を設定し、かつリトライポリシーがリトライを選択しない限り、SDK は一般的なモデルリクエストをリトライしません。

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

`ModelRetrySettings` には 3 つのフィールドがあります:

<div class="field-table" markdown="1">

| Field | Type | Notes |
| --- | --- | --- |
| `max_retries` | `int | None` | 初回リクエスト後に許可されるリトライ試行回数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示遅延を返さずリトライする場合の既定遅延戦略。 |
| `policy` | `RetryPolicy | None` | リトライするか決定するコールバック。このフィールドは実行時専用で serialize されません。 |

</div>

リトライポリシーは [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。内容:

- 試行回数依存の判断に使える `attempt` と `max_retries`。
- ストリーミング / 非ストリーミング動作を分岐できる `stream`。
- raw 検査用の `error`。
- `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` など正規化情報の `normalized`。
- 基盤モデル adapter がリトライ指針を提供できる場合の `provider_advice`。

ポリシーは次のいずれかを返せます:

- 単純なリトライ判定の `True` / `False`。
- 遅延上書きや診断理由付与が必要な場合の [`RetryDecision`][agents.retry.RetryDecision]。

SDK は `retry_policies` に既製ヘルパーを公開しています:

| Helper | Behavior |
| --- | --- |
| `retry_policies.never()` | 常に opt out します。 |
| `retry_policies.provider_suggested()` | 利用可能な場合 provider のリトライ助言に従います。 |
| `retry_policies.network_error()` | 一時的な transport / timeout 失敗に一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP status code に一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントがある場合のみ、その遅延でリトライします。 |
| `retry_policies.any(...)` | ネストした任意ポリシーが opt in したときにリトライします。 |
| `retry_policies.all(...)` | ネストしたすべてのポリシーが opt in したときのみリトライします。 |

ポリシーを合成する場合、`provider_suggested()` は provider veto と replay-safe 承認を維持できるため、最も安全な最初の構成要素です。

##### 安全境界

一部失敗は自動リトライされません:

- Abort エラー。
- provider 助言が replay を unsafe と判定したリクエスト。
- 出力開始後で replay が unsafe になるストリーミング実行。

`previous_response_id` または `conversation_id` を使う stateful なフォローアップリクエストも、より保守的に扱われます。これらのリクエストでは、`network_error()` や `http_status([500])` のような non-provider 条件だけでは不十分です。リトライポリシーには通常 `retry_policies.provider_suggested()` を通じた provider の replay-safe 承認を含めるべきです。

##### Runner とエージェントのマージ動作

`retry` は runner レベルとエージェントレベルの `ModelSettings` 間で deep-merge されます:

- エージェントは `retry.max_retries` のみ上書きし、runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部のみ上書きし、兄弟 backoff フィールドを runner から維持できます。
- `policy` は実行時専用のため、serialize された `ModelSettings` は `max_retries` と `backoff` を保持し、コールバック自体は省略します。

より完全な例は [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [adapter-backed retry 例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

## non-OpenAI provider のトラブルシューティング

### トレーシングクライアントエラー 401

トレーシング関連エラーが出る場合、trace は OpenAI サーバーへアップロードされるため、OpenAI API key がないことが原因です。解決方法は 3 つあります:

1. トレーシングを完全に無効化: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用 OpenAI key を設定: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API key は trace アップロード専用で、[platform.openai.com](https://platform.openai.com/) 由来である必要があります。
3. non-OpenAI の trace プロセッサーを使用。詳細は [tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK は既定で Responses API を使いますが、他の多くの LLM provider はまだサポートしていません。その結果 404 などの問題が発生することがあります。解決方法は 2 つあります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部モデル provider は [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデル provider の制限です。JSON 出力はサポートしていても、出力に使う `json_schema` 指定を許可しません。この問題の修正を進めていますが、JSON schema 出力をサポートする provider への依存を推奨します。そうでない場合、不正 JSON によりアプリが頻繁に壊れる可能性があります。

## provider 間でのモデル混在

モデル provider 間の機能差を理解していないと、エラーに遭遇する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、多くの他 provider はこれらをサポートしません。次の制約に注意してください:

- サポートしない provider に未対応の `tools` を送らない
- テキスト専用モデル呼び出し前にマルチモーダル入力を除外する
- structured JSON 出力非対応 provider は無効 JSON を時折生成する点を認識する

## サードパーティ adapters

SDK の組み込み provider 統合ポイントで不十分な場合にのみ、サードパーティ adapter を使用してください。この SDK で OpenAI モデルのみを使う場合、Any-LLM や LiteLLM ではなく、組み込み [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 経路を優先してください。サードパーティ adapter は、OpenAI モデルと non-OpenAI provider の組み合わせ、または組み込み経路で提供されない adapter 管理の provider カバレッジ / ルーティングが必要なケース向けです。adapter は SDK と上流モデル provider の間に別の互換レイヤーを追加するため、機能サポートとリクエスト意味論は provider により変動します。SDK は現在、Any-LLM と LiteLLM を best-effort の beta adapter 統合として含みます。

### Any-LLM

Any-LLM サポートは、Any-LLM 管理の provider カバレッジまたはルーティングが必要なケース向けに、best-effort な beta として含まれます。

上流 provider 経路により、Any-LLM は Responses API、Chat Completions 互換 API、または provider 固有の互換レイヤーを使う場合があります。

Any-LLM が必要な場合は `openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から開始してください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` モデル名を使う、`AnyLLMModel` を直接インスタンス化する、または実行スコープで `AnyLLMProvider` を使うことができます。モデル surface を明示固定したい場合は、`AnyLLMModel` 構築時に `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM はサードパーティ adapter レイヤーであり、provider 依存関係と機能ギャップは SDK ではなく Any-LLM 側で定義されます。使用量メトリクスは上流 provider が返す場合に自動伝搬されますが、ストリーミング Chat Completions backend では usage chunk 出力前に `ModelSettings(include_usage=True)` が必要な場合があります。structured outputs、ツール呼び出し、使用量レポート、Responses 固有動作に依存する場合は、デプロイ予定の正確な provider backend を検証してください。

### LiteLLM

LiteLLM サポートは、LiteLLM 固有の provider カバレッジまたはルーティングが必要なケース向けに、best-effort な beta として含まれます。

LiteLLM が必要な場合は `openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から開始してください。`litellm/...` モデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

一部 LiteLLM バックエンド provider は、既定では SDK 使用量メトリクスを設定しません。使用量レポートが必要な場合は `ModelSettings(include_usage=True)` を渡し、structured outputs、ツール呼び出し、使用量レポート、adapter 固有ルーティング動作に依存する場合は、デプロイ予定の正確な provider backend を検証してください。