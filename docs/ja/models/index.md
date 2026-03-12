---
search:
  exclude: true
---
# モデル

Agents SDK には、すぐに使える OpenAI モデルのサポートが 2 種類あります。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

設定に応じて、次の順序でこのページを参照してください。

| 目標 | 開始位置 |
| --- | --- |
| SDK のデフォルトで OpenAI ホストモデルを使う | [OpenAI モデル](#openai-models) |
| websocket トランスポートで OpenAI Responses API を使う | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 非 OpenAI プロバイダーを使う | [非 OpenAI モデル](#non-openai-models) |
| 1 つのワークフローでモデル / プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) と [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [非 OpenAI プロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` 初期化時にモデルを指定しない場合、デフォルトモデルが使われます。現在のデフォルトは、互換性と低遅延のため [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能であれば、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) にエージェントを設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) など別モデルに切り替えるには、エージェントを設定する方法が 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定モデルを一貫して使いたい場合は、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

次に、`RunConfig` 経由で実行時のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの GPT-5 モデルを使う場合、SDK はデフォルトの `ModelSettings` を適用します。これは多くのユースケースで最適に動作する設定です。デフォルトモデルの推論 effort を調整するには、独自の `ModelSettings` を渡します。

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

低遅延が必要な場合、`gpt-5.4` では `reasoning.effort="none"` の使用を推奨します。gpt-4.1 ファミリー（ mini / nano を含む）も、対話型エージェントアプリ構築で引き続き有力な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルによって、SDK が送信する computer-tool ペイロードが決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールを使い、明示的な `computer-use-preview` リクエストでは旧 `computer_use_preview` ペイロードを維持します。

主な例外は prompt 管理の呼び出しです。prompt テンプレートがモデルを管理し、SDK がリクエストから `model` を省略する場合、SDK は prompt が固定するモデルを推測しないよう、preview 互換の computer ペイロードをデフォルトで使います。このフローで GA パスを維持するには、リクエストで `model="gpt-5.4"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制してください。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として動作し続けます。

preview 互換リクエストでは `environment` と表示サイズを先にシリアライズする必要があるため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使う prompt 管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制してください。移行の詳細は [Tools](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡すと、SDK は任意モデル互換の汎用 `ModelSettings` に戻ります。

### Responses 専用のツール検索機能

次のツール機能は OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` とその他の deferred-loading Responses ツール機能

これらは Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。deferred-loading ツールを使う場合は、エージェントに `ToolSearchTool()` を追加し、モデルが `auto` または `required` の tool choice でツールを読み込むようにしてください。名前空間名のみや deferred 専用関数名の強制は避けてください。設定詳細と現在の制約は [Tools](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使います。OpenAI バックエンドモデル使用時は websocket トランスポートに切り替え可能です。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルト OpenAI プロバイダーで解決される OpenAI Responses モデル（`"gpt-5.4"` のような文字列モデル名を含む）に影響します。

トランスポート選択は、SDK がモデル名をモデルインスタンスに解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合は、グローバルデフォルトではなくそのプロバイダーがトランスポート選択を制御します。

websocket トランスポートは、プロバイダー単位または実行単位でも設定できます。

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

プレフィックスベースのモデルルーティング（例: 1 回の実行で `openai/...` と `litellm/...` を混在） が必要な場合は、[`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は 2 つの過去互換デフォルトを維持します。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスはパススルーされず、`UserError` を発生させます。

OpenAI 互換エンドポイントで、名前空間付きモデル ID の文字列そのものを期待する場合は、明示的にパススルー動作を有効化してください。websocket 有効構成では、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持します。

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

バックエンドが `openai/...` 文字列をそのまま期待する場合は `openai_prefix_mode="model_id"` を使います。`openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket トランスポート外の `MultiProvider` でも有効です。この例では本セクションのトランスポート設定の一部として websocket を有効にしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

カスタム OpenAI 互換エンドポイントやプロキシを使う場合、websocket トランスポートには互換性のある websocket `/responses` エンドポイントも必要です。これらの構成では `websocket_base_url` を明示的に設定する必要がある場合があります。

注意:

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や、Responses websocket `/responses` エンドポイントをサポートしない非 OpenAI プロバイダーには適用されません。
-   環境で未導入の場合は `websockets` パッケージをインストールしてください。
-   websocket トランスポート有効化後は [`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使えます。複数ターンのワークフローで同一 websocket 接続をターン間（およびネストした Agents-as-tools 呼び出し）で再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM integration](./litellm.md) 経由で利用できます。まず、litellm 依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスで任意の [対応モデル](https://docs.litellm.ai/docs/providers) を使います。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーはさらに 3 つの方法で統合できます（例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。これは LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行の全エージェントでカスタムモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能な多くのモデルを簡単に使う方法として [LiteLLM integration](./litellm.md) もあります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API / モデルを使っています。LLM プロバイダーが対応している場合は Responses の使用を推奨します。

## 高度なモデル選択と混在

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクには大型で高性能なモデルを使えます。[`Agent`][agents.Agent] を設定する際は、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしますが、2 つは対応機能とツールセットが異なるため、各ワークフローでは単一のモデル形状を使うことを推奨します。モデル形状を混在させる必要がある場合は、使っている機能が両方で利用可能であることを確認してください。

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

エージェントで使用するモデルをさらに設定したい場合は、temperature などの任意設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses API を使う場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、それらに `extra_args` は不要です。

| フィールド | 用途 |
| --- | --- |
| `parallel_tool_calls` | 同一ターンで複数ツール呼び出しを許可 / 禁止します。 |
| `truncation` | コンテキスト超過時に失敗させず、Responses API が古い会話項目を削除できるよう `"auto"` を設定します。 |
| `prompt_cache_retention` | たとえば `"24h"` で、キャッシュ済みプロンプト接頭辞をより長く保持します。 |
| `response_include` | `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より豊富なレスポンスペイロードを要求します。 |
| `top_logprobs` | 出力テキストの top-token logprobs を要求します。SDK は `message.output_text.logprobs` も自動追加します。 |
| `retry` | モデル呼び出しで Runner 管理の再試行設定を有効化します。[Runner 管理再試行](#runner-managed-retries) を参照してください。 |

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

#### Runner 管理再試行

再試行は実行時限定で、明示的に有効化する必要があります。`ModelSettings(retry=...)` を設定し、かつ再試行ポリシーが再試行を選択しない限り、SDK は一般的なモデルリクエストを再試行しません。

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

| フィールド | 型 | 注記 |
| --- | --- | --- |
| `max_retries` | `int \| None` | 初回リクエスト後に許可される再試行回数。 |
| `backoff` | `ModelRetryBackoffSettings \| dict \| None` | 明示的遅延が返されない場合にポリシー再試行で使うデフォルト遅延戦略。 |
| `policy` | `RetryPolicy \| None` | 再試行するかを決めるコールバック。このフィールドは実行時限定でシリアライズされません。 |

再試行ポリシーは [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取り、次が含まれます。

- `attempt` と `max_retries`（試行回数に応じた判断用）
- `stream`（ストリーミング / 非ストリーミング分岐用）
- 生の確認用 `error`
- `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの正規化情報 `normalized`
- 基盤モデルアダプターが再試行指針を提供できる場合の `provider_advice`

ポリシーは次のいずれかを返せます。

- 単純な再試行判定の `True` / `False`
- 遅延上書きや診断理由付与を行いたい場合の [`RetryDecision`][agents.retry.RetryDecision]

SDK は `retry_policies` に既成ヘルパーを提供します。

| ヘルパー | 挙動 |
| --- | --- |
| `retry_policies.never()` | 常に無効。 |
| `retry_policies.provider_suggested()` | 利用可能な場合はプロバイダーの再試行指針に従う。 |
| `retry_policies.network_error()` | 一時的な転送 / タイムアウト障害に一致。 |
| `retry_policies.http_status([...])` | 指定 HTTP ステータスコードに一致。 |
| `retry_policies.retry_after()` | retry-after ヒントがある場合のみ、その遅延で再試行。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかが有効なら再試行。 |
| `retry_policies.all(...)` | ネストされたポリシーすべてが有効な場合のみ再試行。 |

ポリシーを合成する場合、`provider_suggested()` は最初の構成要素として最も安全です。これは、プロバイダーが区別可能な場合に veto と replay-safety 承認を保持するためです。

##### 安全性境界

一部の失敗は自動再試行されません。

- Abort エラー。
- プロバイダー指針で replay が unsafe とされたリクエスト。
- 出力開始後で replay が unsafe になるストリーミング実行。

`previous_response_id` または `conversation_id` を使う状態保持の後続リクエストも、より保守的に扱われます。これらでは `network_error()` や `http_status([500])` のような非プロバイダー述語だけでは不十分です。再試行ポリシーには、通常 `retry_policies.provider_suggested()` を通じたプロバイダーの replay-safe 承認を含める必要があります。

##### Runner とエージェントのマージ挙動

`retry` は Runner レベルとエージェントレベルの `ModelSettings` 間で深くマージされます。

- エージェントは `retry.max_retries` のみ上書きし、Runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部のみ上書きし、兄弟 backoff フィールドを Runner から維持できます。
- `policy` は実行時限定のため、シリアライズされた `ModelSettings` には `max_retries` と `backoff` は保持されますが、コールバック自体は含まれません。

より完全な例は [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) と [`examples/basic/retry_litellm.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py) を参照してください。

SDK がまだ最上位で直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は `extra_args` を使ってください。

また OpenAI の Responses API 使用時、[他にもいくつか任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。最上位で利用できない場合は、それらも `extra_args` で渡せます。

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

トレーシング関連エラーが出る場合、トレースが OpenAI サーバーへアップロードされる一方で OpenAI API キーがないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用 OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースアップロードにのみ使われ、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレースプロセッサーを使う。[トレーシングドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、ほとんどの他 LLM プロバイダーはまだ対応していません。その結果、404 などの問題が発生することがあります。解決には 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に有効です。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生する場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデルプロバイダーの制約です。JSON 出力はサポートしていても、出力で使う `json_schema` を指定できません。この問題の修正を進めていますが、JSON schema 出力をサポートするプロバイダーへの依存を推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を把握しておく必要があります。把握していないとエラーが発生する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、多くの他プロバイダーはこれらをサポートしません。次の制約に注意してください。

-   非対応の `tools` を理解しないプロバイダーに送らない
-   テキスト専用モデル呼び出し前にマルチモーダル入力を除外する
-   structured JSON 出力非対応プロバイダーは無効な JSON を時折生成する可能性があることを認識する