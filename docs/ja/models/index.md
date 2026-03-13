---
search:
  exclude: true
---
# モデル

Agents SDK には、 OpenAI モデルをすぐに使える形で 2 種類サポートしています。

-   **推奨**: 新しい [ Responses API ](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [ Chat Completions API ](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## モデル設定の選択

設定に応じて、このページを次の順序で参照してください。

| Goal | Start here |
| --- | --- |
| SDK デフォルトで OpenAI ホストモデルを使う | [ OpenAI モデル ](#openai-models) |
| websocket トランスポート経由で OpenAI Responses API を使う | [ Responses WebSocket トランスポート ](#responses-websocket-transport) |
| 非 OpenAI プロバイダーを使う | [ 非 OpenAI モデル ](#non-openai-models) |
| 1 つのワークフローでモデル / プロバイダーを混在させる | [ 高度なモデル選択と混在 ](#advanced-model-selection-and-mixing) および [ プロバイダー間でのモデル混在 ](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [ 非 OpenAI プロバイダーのトラブルシューティング ](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使われます。現在のデフォルトは、互換性と低レイテンシのために [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。アクセス可能であれば、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) をエージェントに設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで一貫して特定モデルを使いたい場合は、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

次に、`RunConfig` 経由で実行ごとのデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの GPT-5 モデルを使う場合、 SDK はデフォルトの `ModelSettings` を適用します。これは多くのユースケースで最適に動作する設定です。デフォルトモデルの推論 effort を調整するには、独自の `ModelSettings` を渡してください。

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

より低レイテンシにするには、`gpt-5.4` で `reasoning.effort="none"` を使うことを推奨します。 gpt-4.1 ファミリー（ mini / nano バリアントを含む）も、インタラクティブなエージェントアプリ構築の有力な選択肢です。

#### ComputerTool のモデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、 SDK が送信するコンピュータツール payload が決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールを使い、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` payload を維持します。

主な例外は prompt 管理呼び出しです。 prompt template がモデルを保持し、 SDK がリクエストから `model` を省略する場合、 SDK は prompt がどのモデルに固定されているかを推測しないよう、 preview 互換のコンピュータ payload をデフォルトで使います。このフローで GA パスを維持するには、リクエストで `model="gpt-5.4"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名と同様に振る舞い続けます。

preview 互換リクエストでは `environment` と表示寸法を事前にシリアライズする必要があるため、[`ComputerProvider`][agents.tool.ComputerProvider] factory を使う prompt 管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制してください。移行の詳細は [ Tools ](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡すと、 SDK はどのモデルとも互換な汎用 `ModelSettings` に戻ります。

### Responses 専用のツール検索機能

次のツール機能は OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` およびその他の遅延読み込み Responses ツール面

これらの機能は Chat Completions モデルと、非 Responses バックエンドでは拒否されます。遅延読み込みツールを使う場合は、`ToolSearchTool()` をエージェントに追加し、素の namespace 名や遅延専用関数名を強制する代わりに、`auto` または `required` の tool choice でモデルにツールを読み込ませてください。設定詳細と現在の制約は [ Tools ](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、 OpenAI Responses API リクエストは HTTP トランスポートを使います。 OpenAI バックドモデルを使う場合、 websocket トランスポートを有効化できます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルト OpenAI プロバイダーで解決される OpenAI Responses モデル（`"gpt-5.4"` のような文字列モデル名を含む）に影響します。

トランスポートの選択は、 SDK がモデル名をモデルインスタンスに解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。 [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合は、グローバルデフォルトではなくそのプロバイダーがトランスポート選択を制御します。

プロバイダーごと / 実行ごとに websocket トランスポートを設定することもできます。

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

プレフィックスベースのモデルルーティングが必要な場合（例: 1 回の実行で `openai/...` と `litellm/...` を混在）、代わりに [`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は、過去からの 2 つのデフォルトを維持します。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   未知のプレフィックスは透過渡しされず、`UserError` を送出します。

OpenAI 互換エンドポイントが、名前空間付きモデル ID のリテラルを期待する場合は、明示的に pass-through 動作を有効化してください。 websocket 有効構成では、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドがリテラルな `openai/...` 文字列を期待する場合は `openai_prefix_mode="model_id"` を使います。バックエンドが `openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket トランスポート外の `MultiProvider` でも動作します。この例では、この節で説明するトランスポート設定の一部として websocket を有効のままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

カスタム OpenAI 互換エンドポイントやプロキシを使う場合、 websocket トランスポートには互換の websocket `/responses` エンドポイントも必要です。その構成では `websocket_base_url` を明示設定する必要がある場合があります。

注意事項:

-   これは websocket トランスポート上の Responses API であり、[ Realtime API ](../realtime/guide.md) ではありません。 Chat Completions や非 OpenAI プロバイダーには、 Responses websocket `/responses` エンドポイントをサポートしない限り適用されません。
-   環境で `websockets` パッケージが未導入の場合はインストールしてください。
-   websocket トランスポート有効化後は [`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接利用できます。複数ターンのワークフローで、ターン間（および入れ子の agent-as-tool 呼び出し間）で同じ websocket 接続を再利用したい場合は [`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[ Running agents ](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

多くの非 OpenAI モデルは [ LiteLLM integration ](./litellm.md) で利用できます。まず litellm 依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックス付きで任意の [ 対応モデル ](https://docs.litellm.ai/docs/providers) を使います。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります（コード例は [ こちら ](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。これは LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に適しています。設定可能なコード例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行のすべてのエージェントでカスタムモデルプロバイダーを使う」と指定できます。設定可能なコード例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを混在できます。設定可能なコード例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能な多くのモデルを簡単に使う方法として、[ LiteLLM integration ](./litellm.md) があります。

`platform.openai.com` の API key を持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[ 別のトレーシングプロセッサー ](../tracing.md) を設定することを推奨します。

!!! note

    これらのコード例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、 Chat Completions API / model を使っています。 LLM プロバイダーが対応している場合は Responses の利用を推奨します。

## 高度なモデル選択と混在

単一ワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクには大型で高性能なモデルを使えます。[`Agent`][agents.Agent] を設定する際は、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスへマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、2 つはサポートする機能とツールの集合が異なるため、ワークフローごとに単一のモデル形状を使うことを推奨します。モデル形状を混在させる必要がある場合は、使用中の全機能が両方で利用可能であることを確認してください。

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

エージェントで使うモデルをさらに設定したい場合は [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。これにより temperature などの任意のモデル設定パラメーターを指定できます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

#### 一般的な高度 `ModelSettings` オプション

OpenAI Responses API を使う場合、いくつかのリクエストフィールドにはすでに `ModelSettings` の直接フィールドがあるため、それらに `extra_args` は不要です。

| Field | Use it for |
| --- | --- |
| `parallel_tool_calls` | 同一ターンでの複数ツール呼び出しを許可 / 禁止します。 |
| `truncation` | コンテキストが溢れる際に失敗する代わりに、 Responses API が最も古い会話項目を破棄できるよう `"auto"` を設定します。 |
| `store` | 生成レスポンスを後で取得できるようサーバー側に保存するかを制御します。これは response ID に依存するフォローアップワークフローや、`store=False` のときにローカル入力へフォールバックが必要なセッション compact 化フローで重要です。 |
| `prompt_cache_retention` | たとえば `"24h"` で、キャッシュされた prompt 接頭辞をより長く保持します。 |
| `response_include` | `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、よりリッチなレスポンス payload を要求します。 |
| `top_logprobs` | 出力テキストの top-token logprobs を要求します。 SDK は `message.output_text.logprobs` も自動追加します。 |
| `retry` | モデル呼び出しに対して runner 管理リトライ設定を有効化します。[ Runner 管理リトライ ](#runner-managed-retries) を参照してください。 |

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

`store=False` を設定すると、 Responses API はそのレスポンスを後でサーバー側取得できる状態に保持しません。これは stateless または zero-data-retention 形式のフローで有用ですが、通常 response ID を再利用する機能は代わりにローカル管理状態へ依存する必要があります。たとえば [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] は、最後のレスポンスが保存されていない場合、デフォルト `"auto"` compact 化パスを input ベース compact 化へ切り替えます。[ Sessions ガイド ](../sessions/index.md#openai-responses-compaction-sessions) を参照してください。

#### Runner 管理リトライ

リトライは実行時専用で opt-in です。`ModelSettings(retry=...)` を設定し、かつリトライポリシーがリトライを選択しない限り、 SDK は一般的なモデルリクエストをリトライしません。

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

| Field | Type | Notes |
| --- | --- | --- |
| `max_retries` | `int \| None` | 初回リクエスト後に許可されるリトライ試行回数。 |
| `backoff` | `ModelRetryBackoffSettings \| dict \| None` | ポリシーが明示的遅延を返さずにリトライする場合のデフォルト遅延戦略。 |
| `policy` | `RetryPolicy \| None` | リトライ可否を決定するコールバック。このフィールドは実行時専用でシリアライズされません。 |

リトライポリシーは [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。内容は次の通りです。

- `attempt` と `max_retries`（試行回数に応じた判断のため）
- `stream`（ストリーミング / 非ストリーミングで分岐するため）
- `error`（raw 検査用）
- `normalized` 情報（`status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort`）
- 基盤モデル adapter がリトライ指針を提供できる場合の `provider_advice`

ポリシーは次のいずれかを返せます。

- 単純なリトライ判断としての `True` / `False`
- 遅延上書きや診断理由付与をしたい場合の [`RetryDecision`][agents.retry.RetryDecision]

SDK は `retry_policies` に既製ヘルパーを提供しています。

| Helper | Behavior |
| --- | --- |
| `retry_policies.never()` | 常に無効化します。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーのリトライ指針に従います。 |
| `retry_policies.network_error()` | 一時的なトランスポート障害とタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 指定した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントがある場合のみ、その遅延でリトライします。 |
| `retry_policies.any(...)` | ネストしたいずれかのポリシーが有効化した場合にリトライします。 |
| `retry_policies.all(...)` | ネストしたすべてのポリシーが有効化した場合のみリトライします。 |

ポリシーを合成する際、`provider_suggested()` は最も安全な最初の構成要素です。これは、プロバイダーが区別可能な場合に veto と replay-safety 承認を保持するためです。

##### 安全性境界

次の障害は自動リトライされません。

- Abort エラー。
- プロバイダー指針が replay を unsafe と示すリクエスト。
- 出力開始後で replay が unsafe になるストリーミング実行。

`previous_response_id` や `conversation_id` を使う stateful なフォローアップリクエストも、より保守的に扱われます。これらでは `network_error()` や `http_status([500])` のような非プロバイダー述語だけでは不十分です。通常、`retry_policies.provider_suggested()` を通じた provider 側 replay-safe 承認をリトライポリシーに含める必要があります。

##### Runner と agent のマージ動作

`retry` は runner レベルと agent レベルの `ModelSettings` 間で deep-merge されます。

- agent は `retry.max_retries` のみ上書きし、runner の `policy` を継承できます。
- agent は `retry.backoff` の一部のみ上書きし、backoff の兄弟フィールドを runner から維持できます。
- `policy` は実行時専用のため、シリアライズされた `ModelSettings` には `max_retries` と `backoff` は残り、コールバック自体は含まれません。

より完全なコード例は [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [`examples/basic/retry_litellm.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

SDK がまだトップレベルで直接公開していないプロバイダー固有または新しいリクエストフィールドが必要な場合は `extra_args` を使ってください。

また OpenAI の Responses API を使う場合、[ ほかにもいくつか任意パラメーター ](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、同様に `extra_args` で渡せます。

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

## 非 OpenAI プロバイダーのトラブルシューティング

### トレーシングクライアントエラー 401

トレーシング関連エラーが出る場合、トレースは OpenAI サーバーにアップロードされる一方で、 OpenAI API key がないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用 OpenAI key を設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API key はトレースアップロードのみに使われ、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI トレースプロセッサーを使う。[ トレーシングドキュメント ](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、他の多くの LLM プロバイダーはまだ未対応です。その結果、404 などの問題が発生する場合があります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [ こちら ](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部のモデルプロバイダーは [ structured outputs ](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、ときどき次のようなエラーになります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデルプロバイダー側の制限です。 JSON 出力はサポートしていても、出力に使う `json_schema` を指定できません。この問題の修正に取り組んでいますが、 JSON schema 出力をサポートするプロバイダーへの依存を推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れるためです。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を理解しておく必要があります。そうしないとエラーになる可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、他の多くのプロバイダーはこれらに未対応です。次の制限に注意してください。

-   未対応プロバイダーに未対応の `tools` を送らない
-   text-only モデルを呼び出す前にマルチモーダル入力を除外する
-   structured JSON 出力をサポートしないプロバイダーは、ときどき無効な JSON を生成することを認識しておく