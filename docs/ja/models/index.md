---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルのサポートが最初から 2 つの形で含まれています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

設定に応じて、このページを次の順序で参照してください。

| Goal | Start here |
| --- | --- |
| SDK のデフォルトで OpenAI ホストモデルを使う | [OpenAI モデル](#openai-models) |
| websocket 転送で OpenAI Responses API を使う | [Responses WebSocket 転送](#responses-websocket-transport) |
| 非 OpenAI プロバイダーを使う | [非 OpenAI モデル](#non-openai-models) |
| 1 つのワークフローでモデル/プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) と [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [非 OpenAI プロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` の初期化時にモデルを指定しない場合は、デフォルトモデルが使われます。現在のデフォルトは、互換性と低レイテンシのため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。アクセス可能であれば、明示的な `model_settings` を維持したまま、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) をエージェントに設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの他モデルに切り替えるには、エージェントを設定する方法が 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定モデルを一貫して使いたい場合は、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

次に、`RunConfig` 経由で実行ごとのデフォルトモデルを設定できます。エージェントにモデルを設定していない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの GPT-5 モデルを使うと、SDK はデフォルトの `ModelSettings` を適用します。これはほとんどのユースケースで最適に動作する設定です。デフォルトモデルの推論 effort を調整するには、独自の `ModelSettings` を渡してください。

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

低レイテンシのためには、`gpt-5.4` で `reasoning.effort="none"` を使うことを推奨します。gpt-4.1 ファミリー（ mini / nano を含む）も、対話型エージェントアプリ構築において引き続き堅実な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、SDK が送信する computer-tool ペイロードが決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールを使用し、明示的な `computer-use-preview` リクエストでは旧来の `computer_use_preview` ペイロードを維持します。

主な例外は prompt 管理の呼び出しです。prompt template がモデルを管理し、SDK がリクエストから `model` を省略する場合、SDK は prompt がどのモデルに固定されているかを推測しないため、preview 互換の computer ペイロードをデフォルトで使います。このフローで GA 経路を維持するには、リクエストで `model="gpt-5.4"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制してください。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効リクエストモデルに一致する組み込みセレクターへ正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として動作し続けます。

preview 互換リクエストでは `environment` と表示サイズを事前にシリアライズする必要があるため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使う prompt 管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細は [Tools](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡した場合、SDK は任意モデル互換の汎用 `ModelSettings` に戻します。

### Responses 専用ツール検索機能

次のツール機能は OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` とその他の defer loading な Responses ツールサーフェス

これらの機能は Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。defer loading ツールを使う場合は、エージェントに `ToolSearchTool()` を追加し、素の namespace 名や defer 専用関数名を強制するのではなく、`auto` または `required` の tool choice でモデルにツールを読み込ませてください。設定詳細と現時点の制約は [Tools](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket 転送

デフォルトでは、OpenAI Responses API リクエストは HTTP 転送を使います。OpenAI バックモデル使用時は websocket 転送を有効化できます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルト OpenAI プロバイダーで解決される OpenAI Responses モデル（ `"gpt-5.4"` のような文字列モデル名を含む）に影響します。

転送方式の選択は、SDK がモデル名をモデルインスタンスへ解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、その転送方式はすでに固定です。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合は、グローバルデフォルトではなくそのプロバイダーが転送選択を制御します。

websocket 転送はプロバイダー単位または実行単位でも設定できます。

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

プレフィックスベースのモデルルーティングが必要な場合（例: 1 回の実行で `openai/...` と `litellm/...` モデル名を混在）、代わりに [`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は 2 つの歴史的デフォルトを維持します。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   未知のプレフィックスはパススルーされずに `UserError` を発生させます。

OpenAI 互換エンドポイントがリテラルの namespaced model ID を期待する場合、明示的にパススルー動作を有効化してください。websocket 有効構成では、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドがリテラルな `openai/...` 文字列を期待する場合は `openai_prefix_mode="model_id"` を使用します。`openrouter/openai/gpt-4.1-mini` など他の namespaced model ID を期待する場合は `unknown_prefix_mode="model_id"` を使用します。これらのオプションは websocket 転送外の `MultiProvider` でも機能します。この例では本セクションで説明している転送設定の一部として websocket を有効のままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用可能です。

カスタム OpenAI 互換エンドポイントまたはプロキシを使う場合、websocket 転送には互換性のある websocket `/responses` エンドポイントも必要です。これらの設定では `websocket_base_url` を明示設定する必要があることがあります。

注意:

-   これは websocket 転送上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や、Responses websocket `/responses` エンドポイントをサポートしない非 OpenAI プロバイダーには適用されません。
-   環境で未導入の場合は `websockets` パッケージをインストールしてください。
-   websocket 転送有効化後に [`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接利用できます。複数ターンのワークフローで同一 websocket 接続をターン間（ネストされた agent-as-tool 呼び出しを含む）で再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

多くの非 OpenAI モデルは [LiteLLM integration](./litellm.md) 経由で使用できます。まず litellm 依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて任意の [対応モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーはさらに 3 つの方法で統合できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。これは LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合向けです。設定可能なコード例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行のすべてのエージェントでカスタムモデルプロバイダーを使う」と指定できます。設定可能なコード例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では特定の Agent インスタンスにモデルを指定できます。これにより、異なるエージェントごとに異なるプロバイダーを混在利用できます。設定可能なコード例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能な多くのモデルを簡単に使う方法として [LiteLLM integration](./litellm.md) があります。

`platform.openai.com` の API key がない場合は、`set_tracing_disabled()` によるトレーシング無効化、または [別のトレーシングプロセッサー](../tracing.md) の設定を推奨します。

!!! note

    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API / モデルを使用しています。LLM プロバイダーがサポートしている場合は Responses の利用を推奨します。

## 高度なモデル選択と混在

単一ワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえばトリアージには小型で高速なモデルを使い、複雑なタスクにはより大型で高性能なモデルを使うことができます。[`Agent`][agents.Agent] の設定時には、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしますが、2 つはサポート機能とツールセットが異なるため、各ワークフローでは単一のモデル形を使うことを推奨します。ワークフローでモデル形の混在が必要な場合は、使用する機能が両方で利用可能であることを確認してください。

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

#### 一般的な高度 `ModelSettings` オプション

OpenAI Responses API を使用している場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、`extra_args` は不要です。

| Field | Use it for |
| --- | --- |
| `parallel_tool_calls` | 同一ターンでの複数ツール呼び出しを許可または禁止します。 |
| `truncation` | コンテキストあふれ時に失敗する代わりに Responses API が最古の会話項目を削除できるよう、`"auto"` を設定します。 |
| `prompt_cache_retention` | たとえば `"24h"` で、キャッシュ済み prompt prefix をより長く保持します。 |
| `response_include` | `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、よりリッチなレスポンスペイロードを要求します。 |
| `top_logprobs` | 出力テキストの top-token logprobs を要求します。SDK は `message.output_text.logprobs` も自動追加します。 |
| `retry` | モデル呼び出しに runner 管理の再試行設定を有効化します。[Runner 管理の再試行](#runner-managed-retries) を参照してください。 |

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.4",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

#### Runner 管理の再試行

再試行は実行時限定で、明示的に有効化する必要があります。`ModelSettings(retry=...)` を設定し、再試行ポリシーが再試行を選択しない限り、SDK は一般的なモデルリクエストを再試行しません。

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
| `max_retries` | `int \| None` | 初回リクエスト後に許可される再試行回数です。 |
| `backoff` | `ModelRetryBackoffSettings \| dict \| None` | ポリシーが明示遅延を返さず再試行する場合のデフォルト遅延戦略です。 |
| `policy` | `RetryPolicy \| None` | 再試行可否を判断するコールバックです。このフィールドは実行時限定でシリアライズされません。 |

再試行ポリシーは [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取り、以下を利用できます。

- `attempt` と `max_retries`（試行回数に応じた判断のため）。
- `stream`（ストリーミング/非ストリーミングで分岐するため）。
- `error`（生の検査のため）。
- `normalized` 情報（`status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` など）。
- 基盤モデルアダプターが再試行ガイダンスを提供できる場合の `provider_advice`。

ポリシーは次のいずれかを返せます。

- 単純な再試行判断として `True` / `False`。
- 遅延を上書きしたり診断理由を付与したい場合の [`RetryDecision`][agents.retry.RetryDecision]。

SDK は `retry_policies` に既製ヘルパーを提供しています。

| Helper | Behavior |
| --- | --- |
| `retry_policies.never()` | 常に無効化します。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーの再試行助言に従います。 |
| `retry_policies.network_error()` | 一時的な転送障害とタイムアウト障害に一致します。 |
| `retry_policies.http_status([...])` | 指定した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントがある場合にのみ、その遅延で再試行します。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかが有効化した場合に再試行します。 |
| `retry_policies.all(...)` | ネストされたポリシーすべてが有効化した場合にのみ再試行します。 |

ポリシーを合成する場合、`provider_suggested()` はプロバイダーが区別可能なときに veto と replay-safety 承認を保持できるため、最も安全な最初の構成要素です。

##### 安全境界

一部の失敗は自動再試行されません。

- Abort エラー。
- プロバイダー助言が replay を unsafe と示すリクエスト。
- 出力開始後で replay が unsafe になるストリーミング実行。

`previous_response_id` または `conversation_id` を使う状態付きフォローアップリクエストも、より保守的に扱われます。これらでは `network_error()` や `http_status([500])` などの非プロバイダープレディケートだけでは不十分です。再試行ポリシーには、通常 `retry_policies.provider_suggested()` によるプロバイダーの replay-safe 承認を含めるべきです。

##### Runner とエージェントのマージ動作

`retry` は runner レベルと agent レベルの `ModelSettings` 間でディープマージされます。

- エージェントは `retry.max_retries` のみ上書きし、runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部のみ上書きし、他の backoff フィールドは runner から維持できます。
- `policy` は実行時限定なので、シリアライズされた `ModelSettings` には `max_retries` と `backoff` は保持されますが、コールバック自体は含まれません。

より完全なコード例は [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [`examples/basic/retry_litellm.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は `extra_args` を使用してください。

また OpenAI の Responses API 利用時には、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。これらがトップレベルで利用できない場合も、`extra_args` で渡せます。

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

トレーシング関連エラーが出る場合、トレースは OpenAI サーバーへアップロードされる一方で OpenAI API key がないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI key を設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API key はトレースアップロード専用で、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレースプロセッサーを使う。[tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、多くの他 LLM プロバイダーはまだ未対応です。その結果、404 などの問題が発生する場合があります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) です。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生する場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデルプロバイダー側の制約です。JSON 出力はサポートしていても、出力に使う `json_schema` を指定できません。現在修正に取り組んでいますが、JSON schema 出力をサポートするプロバイダーへの依存を推奨します。そうでない場合、不正形式 JSON によりアプリが頻繁に壊れる可能性があるためです。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を認識しておく必要があります。そうしないとエラーが発生する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、多くの他プロバイダーはこれらをサポートしません。次の制約に注意してください。

-   未対応の `tools` を、それを理解しないプロバイダーに送らない
-   テキスト専用モデルを呼び出す前にマルチモーダル入力を除外する
-   structured JSON 出力をサポートしないプロバイダーは、無効な JSON をときどき生成する点に注意する