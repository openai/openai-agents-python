---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに使える形で 2 つの方式でサポートしています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## モデル設定の選択

ご利用の構成に合う最もシンプルな経路から始めてください。

| 目的 | 推奨経路 | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみを使う | デフォルトの OpenAI provider と Responses モデル経路を使う | [OpenAI モデル](#openai-models) |
| websocket 転送で OpenAI Responses API を使う | Responses モデル経路を維持し、websocket 転送を有効化する | [Responses WebSocket 転送](#responses-websocket-transport) |
| 1 つの non-OpenAI provider を使う | 組み込みの provider 統合ポイントから始める | [non-OpenAI モデル](#non-openai-models) |
| エージェント間でモデルや provider を混在させる | 実行単位またはエージェント単位で provider を選び、機能差を確認する | [1 つのワークフロー内でのモデル混在](#mixing-models-in-one-workflow) および [provider 間でのモデル混在](#mixing-models-across-providers) |
| OpenAI Responses の高度なリクエスト設定を調整する | OpenAI Responses 経路で `ModelSettings` を使う | [高度な OpenAI Responses 設定](#advanced-openai-responses-settings) |
| non-OpenAI Chat Completions provider に LiteLLM を使う | LiteLLM を beta のフォールバックとして扱う | [LiteLLM](#litellm) |

## OpenAI モデル

ほとんどの OpenAI 専用アプリでは、デフォルトの OpenAI provider と文字列のモデル名を使い、Responses モデル経路を維持する方法を推奨します。

`Agent` 初期化時にモデルを指定しない場合は、デフォルトモデルが使われます。現在のデフォルトは互換性と低遅延のため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能であれば、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) をエージェントに設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような他モデルに切り替えるには、エージェントを設定する方法が 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定しないすべてのエージェントで特定モデルを一貫して使いたい場合は、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

次に、`RunConfig` で実行ごとのデフォルトモデルを設定できます。エージェントにモデルを設定しなければ、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような GPT-5 モデルを使う場合、SDK はデフォルトの `ModelSettings` を適用します。多くのユースケースで最適に動く設定が使われます。デフォルトモデルの推論 effort を調整するには、独自の `ModelSettings` を渡します。

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

低遅延のためには、`gpt-5.4` で `reasoning.effort="none"` を使うことを推奨します。gpt-4.1 ファミリー（ mini / nano を含む）も、対話型エージェントアプリ構築において有力な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、SDK が送信する computer-tool ペイロードが決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールを使い、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` ペイロードを維持します。

主な例外は prompt 管理型呼び出しです。prompt テンプレートがモデルを所有し、SDK がリクエストから `model` を省略する場合、SDK は prompt がどのモデルに固定されているかを推測しないため、preview 互換の computer ペイロードをデフォルトで使います。このフローで GA 経路を維持するには、リクエストで `model="gpt-5.4"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制してください。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として振る舞い続けます。

preview 互換リクエストでは `environment` と表示寸法を先にシリアライズする必要があるため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使う prompt 管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細は [Tools](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### non-GPT-5 モデル

カスタム `model_settings` なしで non–GPT-5 モデル名を渡すと、SDK は任意モデル互換の汎用 `ModelSettings` に戻ります。

### Responses 専用ツール検索機能

次のツール機能は OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` と、その他の遅延読み込み Responses ツール面

これらの機能は Chat Completions モデルおよび non-Responses バックエンドでは拒否されます。遅延読み込みツールを使う場合は、エージェントに `ToolSearchTool()` を追加し、素の namespace 名や遅延専用関数名を強制する代わりに、`auto` または `required` の tool choice でモデルにツールを読み込ませてください。設定詳細と現時点の制約は [Tools](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket 転送

デフォルトでは、OpenAI Responses API リクエストは HTTP 転送を使います。OpenAI バックエンドのモデル使用時には websocket 転送を有効化できます。

#### 基本設定

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルト OpenAI provider で解決される OpenAI Responses モデル（ `"gpt-5.4"` のような文字列モデル名を含む）に影響します。

転送方式の選択は、SDK がモデル名をモデルインスタンスへ解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡した場合、その転送方式はすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合は、グローバルデフォルトではなくその provider が転送選択を制御します。

#### provider / 実行レベル設定

websocket 転送は provider 単位または実行単位でも設定できます。

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

接頭辞ベースのモデルルーティングが必要な場合（例: 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在させる）、[`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は 2 つの従来デフォルトを維持しています。

-   `openai/...` は OpenAI provider のエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   未知の接頭辞はそのまま渡されず、`UserError` を発生させます。

OpenAI 互換エンドポイントで、名前空間付きモデル ID の文字列をそのまま期待する場合は、明示的に pass-through 動作を有効化してください。websocket 有効構成では、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドが `openai/...` の文字列リテラルを期待する場合は `openai_prefix_mode="model_id"` を使います。`openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket 転送外の `MultiProvider` でも動作します。この例で websocket を有効化しているのは、このセクションで説明している転送設定の一部だからです。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用可能です。

カスタムの OpenAI 互換エンドポイントや proxy を使う場合、websocket 転送には互換 websocket `/responses` エンドポイントも必要です。このような構成では `websocket_base_url` の明示設定が必要になることがあります。

#### 注記

-   これは websocket 転送上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や、Responses websocket `/responses` エンドポイントをサポートしない non-OpenAI provider には適用されません。
-   環境で未導入の場合は `websockets` パッケージをインストールしてください。
-   websocket 転送を有効化後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使えます。複数ターンのワークフローで同じ websocket 接続をターン間（ネストした agent-as-tool 呼び出しを含む）で再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## non-OpenAI モデル

non-OpenAI provider が必要な場合は、まず SDK の組み込み provider 統合ポイントから始めてください。多くの構成では LiteLLM 追加なしで十分です。各パターンの例は [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### non-OpenAI provider 統合方法

| アプローチ | 使用する場面 | スコープ |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにしたい | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタム provider を単一実行に適用したい | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | エージェントごとに異なる provider または具体的モデルオブジェクトが必要 | エージェント単位 |
| LiteLLM (beta) | LiteLLM 固有の provider カバレッジやルーティングが必要 | [LiteLLM](#litellm) を参照 |

これらの組み込み経路で他の LLM provider を統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。LLM provider が OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使います。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行の全エージェントでカスタム model provider を使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では特定 Agent インスタンスでモデルを指定できます。これによりエージェントごとに異なる provider を組み合わせられます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API key がない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では、Chat Completions API / model を使っています。多くの LLM provider がまだ Responses API をサポートしていないためです。LLM provider が対応している場合は Responses の利用を推奨します。

## 1 つのワークフロー内でのモデル混在

単一ワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクには大きく高性能なモデルを使う、といった構成です。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、2 つは対応機能・ツール集合が異なるため、ワークフローごとに単一のモデル形状を使うことを推奨します。モデル形状を混在させる必要がある場合は、利用する機能が両方で使えることを確認してください。

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

エージェントで使うモデルをさらに設定したい場合は、temperature などの任意モデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses 経路でより細かな制御が必要な場合は、`ModelSettings` から始めてください。

### 一般的な高度 `ModelSettings` オプション

OpenAI Responses API 利用時は、いくつかのリクエストフィールドに直接対応する `ModelSettings` フィールドがすでにあるため、それらに `extra_args` は不要です。

- `parallel_tool_calls`: 同一ターンでの複数 tool call を許可 / 禁止します。
- `truncation`: `"auto"` を設定すると、コンテキスト超過時に失敗せず、Responses API が最も古い会話項目を削除します。
- `store`: 生成レスポンスを後続取得のためサーバー側に保存するかを制御します。レスポンス ID に依存するフォローアップワークフローや、`store=False` 時にローカル入力へフォールバックが必要なセッション圧縮フローで重要です。
- `prompt_cache_retention`: たとえば `"24h"` のように、キャッシュされた prompt 接頭辞をより長く保持します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より豊富なレスポンスペイロードを要求します。
- `top_logprobs`: 出力テキストの上位 token logprobs を要求します。SDK は `message.output_text.logprobs` も自動追加します。
- `retry`: モデル呼び出しに対する runner 管理 retry 設定を有効化します。[Runner 管理リトライ](#runner-managed-retries) を参照してください。

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

`store=False` を設定すると、Responses API はそのレスポンスを後続のサーバー側取得に利用できる状態で保持しません。これは stateless または zero-data-retention 風フローで有用ですが、通常レスポンス ID を再利用する機能は、代わりにローカル管理状態へ依存する必要があります。たとえば [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されていない場合、デフォルト `"auto"` 圧縮経路を入力ベース圧縮へ切り替えます。[Sessions ガイド](../sessions/index.md#openai-responses-compaction-sessions) を参照してください。

### `extra_args` の受け渡し

SDK がまだトップレベルで直接公開していない provider 固有または新しいリクエストフィールドが必要な場合は `extra_args` を使います。

また OpenAI の Responses API を使う場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルにない場合は、`extra_args` で渡せます。

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

リトライは実行時限定で、明示的な opt-in です。`ModelSettings(retry=...)` を設定し、かつ retry policy が再試行を選択しない限り、SDK は一般的なモデルリクエストをリトライしません。

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
| `max_retries` | `int | None` | 初回リクエスト後に許可される再試行回数です。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | policy が明示的 delay を返さずに再試行するときのデフォルト遅延戦略です。 |
| `policy` | `RetryPolicy | None` | 再試行するかを決めるコールバックです。このフィールドは実行時限定でシリアライズされません。 |

</div>

retry policy は [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。内容は以下です。

- `attempt` と `max_retries`（試行回数に応じた判断に使用）。
- `stream`（streamed / non-streamed で分岐可能）。
- `error`（raw 検査用）。
- `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの `normalized` 情報。
- 下位モデルアダプターが retry ガイダンスを提供できる場合の `provider_advice`。

policy は次のいずれかを返せます。

- 単純な再試行判定としての `True` / `False`。
- delay 上書きや診断理由付与を行いたい場合の [`RetryDecision`][agents.retry.RetryDecision]。

SDK は `retry_policies` に既製ヘルパーを提供しています。

| ヘルパー | 振る舞い |
| --- | --- |
| `retry_policies.never()` | 常に opt-out します。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、provider の retry 推奨に従います。 |
| `retry_policies.network_error()` | 一時的な転送 / timeout 障害に一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP status code に一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントがある場合のみ、その delay で再試行します。 |
| `retry_policies.any(...)` | ネスト policy のいずれかが opt-in したとき再試行します。 |
| `retry_policies.all(...)` | ネスト policy のすべてが opt-in したときのみ再試行します。 |

policy を組み合わせる場合、`provider_suggested()` は最も安全な最初の構成要素です。provider が判別可能な場合、provider veto と replay-safety 承認を保持できるためです。

##### 安全境界

次の障害は自動再試行されません。

- Abort エラー。
- provider アドバイスが replay unsafe と判定したリクエスト。
- 出力がすでに始まっており replay が unsafe になる streamed 実行。

`previous_response_id` または `conversation_id` を使う状態付きフォローアップリクエストも、より保守的に扱われます。これらのリクエストでは `network_error()` や `http_status([500])` のような非 provider 判定だけでは不十分です。retry policy には通常 `retry_policies.provider_suggested()` を通じた provider の replay-safe 承認を含める必要があります。

##### Runner とエージェントのマージ挙動

`retry` は runner レベルとエージェントレベルの `ModelSettings` 間で deep-merge されます。

- エージェントは `retry.max_retries` のみを上書きしつつ、runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部のみを上書きし、他の backoff フィールドは runner から維持できます。
- `policy` は実行時限定のため、シリアライズされた `ModelSettings` は `max_retries` と `backoff` を保持し、コールバック自体は省略します。

より完全な例は [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [`examples/basic/retry_litellm.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

## non-OpenAI provider のトラブルシューティング

### トレーシングクライアントエラー 401

トレーシング関連エラーが出る場合、トレースが OpenAI サーバーへアップロードされる一方で OpenAI API key がないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用 OpenAI key を設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API key はトレースアップロード専用で、[platform.openai.com](https://platform.openai.com/) 由来である必要があります。
3. non-OpenAI トレースプロセッサーを使う。[tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、多くの他 LLM provider はまだ対応していません。その結果 404 などの問題が発生することがあります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使います。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部の model provider は [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが出る場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部 model provider 側の制約です。JSON 出力はサポートしていても、出力に使う `json_schema` の指定を許可しません。この問題の修正に取り組んでいますが、JSON schema 出力をサポートする provider の利用を推奨します。そうでない場合、アプリは不正な JSON によって頻繁に壊れる可能性があります。

## provider 間でのモデル混在

model provider 間の機能差を把握していないとエラーになる可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、多くの他 provider はこれらをサポートしません。次の制約に注意してください。

-   未対応 provider に、未対応の `tools` を送らない
-   テキスト専用モデル呼び出し前に、マルチモーダル入力を除外する
-   structured JSON 出力非対応 provider は、ときどき不正な JSON を生成する点に注意する

## LiteLLM

LiteLLM サポートは、non-OpenAI provider を Agents SDK ワークフローへ取り込む必要があるケース向けに、best-effort の beta として提供されています。

この SDK で OpenAI モデルを使う場合は、LiteLLM ではなく組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 経路を推奨します。

OpenAI モデルと non-OpenAI provider を組み合わせる必要があり、とくに Chat Completions 互換 API 経由で使う場合、LiteLLM は beta オプションとして利用できますが、すべての構成で最適とは限りません。

non-OpenAI provider で LiteLLM が必要な場合は `openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` モデル名を使うか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

LiteLLM のレスポンスで SDK の usage metrics を埋めたい場合は、`ModelSettings(include_usage=True)` を渡してください。