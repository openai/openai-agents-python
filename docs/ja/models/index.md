---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに使える形で 2 種類サポートしています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## モデル設定の選択

設定に応じて、このページを次の順序で参照してください。

| Goal | Start here |
| --- | --- |
| SDK 既定値で OpenAI ホストモデルを使う | [OpenAI モデル](#openai-models) |
| websocket トランスポートで OpenAI Responses API を使う | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 非 OpenAI プロバイダーを使う | [非 OpenAI モデル](#non-openai-models) |
| 1 つのワークフローでモデル/プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) と [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [非 OpenAI プロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` を初期化するときにモデルを指定しない場合、既定のモデルが使われます。既定値は現在、互換性と低レイテンシのために [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能であれば、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) をエージェントに設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような別モデルに切り替えるには、エージェントを設定する方法が 2 つあります。

### 既定モデル

まず、カスタムモデルを設定していないすべてのエージェントで一貫して特定モデルを使いたい場合は、エージェント実行前に環境変数 `OPENAI_DEFAULT_MODEL` を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

次に、`RunConfig` で実行単位の既定モデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの GPT-5 モデルを使うと、SDK は既定の `ModelSettings` を適用します。ほとんどのユースケースで最適に動作する設定になります。既定モデルの推論 effort を調整するには、独自の `ModelSettings` を渡してください。

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

低レイテンシのためには、`gpt-5.4` で `reasoning.effort="none"` を使うことを推奨します。gpt-4.1 系列（ mini / nano バリアントを含む）も、対話型エージェントアプリ構築における堅実な選択肢です。

#### ComputerTool モデル選択

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれている場合、実際の Responses リクエストで有効なモデルによって、SDK が送信するコンピュータツールの payload が決まります。明示的な `gpt-5.4` リクエストでは GA の組み込み `computer` ツールが使われ、明示的な `computer-use-preview` リクエストでは従来の `computer_use_preview` payload が維持されます。

主な例外は prompt 管理の呼び出しです。prompt template がモデルを管理し、SDK がリクエストから `model` を省略する場合、SDK は prompt が固定しているモデルを推測しないように、preview 互換のコンピュータ payload を既定で使います。このフローで GA パスを維持するには、リクエストで `model="gpt-5.4"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制してください。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効リクエストモデルに一致する組み込みセレクターに正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として動作し続けます。

preview 互換リクエストでは、`environment` と表示サイズを先にシリアライズする必要があります。そのため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使う prompt 管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制する必要があります。移行の詳細は [Tools](../tools.md#computertool-and-the-responses-computer-tool) を参照してください。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK は任意モデルと互換性のある汎用 `ModelSettings` に戻ります。

### Responses 専用のツール検索機能

次のツール機能は OpenAI Responses モデルでのみサポートされます。

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` およびその他の deferred-loading Responses ツール surface

これらの機能は Chat Completions モデルおよび非 Responses バックエンドでは拒否されます。deferred-loading ツールを使う場合は、エージェントに `ToolSearchTool()` を追加し、名前空間名のみや deferred 専用関数名を強制するのではなく、`auto` または `required` の tool choice でモデルにツールをロードさせてください。設定詳細と現時点の制約は [Tools](../tools.md#hosted-tool-search) を参照してください。

### Responses WebSocket トランスポート

既定では、OpenAI Responses API リクエストは HTTP トランスポートを使います。OpenAI バックエンドモデル使用時は websocket トランスポートを有効化できます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、既定の OpenAI プロバイダーで解決される OpenAI Responses モデル（`"gpt-5.4"` のような文字列モデル名を含む）に影響します。

トランスポート選択は、SDK がモデル名をモデルインスタンスへ解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、トランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合は、グローバル既定ではなくそのプロバイダーがトランスポート選択を制御します。

プロバイダー単位または実行単位で websocket トランスポートを設定することもできます。

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

プレフィックスベースのモデルルーティングが必要な場合（例: 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在）、代わりに [`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は 2 つの歴史的既定値を維持しています。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   未知のプレフィックスはそのまま渡されず、`UserError` を発生させます。

OpenAI 互換 endpoint で、リテラルな名前空間付きモデル ID を期待する場合は、明示的に pass-through 動作を有効化してください。websocket 有効構成では、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドがリテラルな `openai/...` 文字列を期待する場合は `openai_prefix_mode="model_id"` を使います。バックエンドが `openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket トランスポート外の `MultiProvider` でも動作します。この例は、このセクションで説明しているトランスポート設定の一部として websocket を有効にしたままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

カスタム OpenAI 互換 endpoint または proxy を使う場合、websocket トランスポートには互換性のある websocket `/responses` endpoint も必要です。これらの構成では `websocket_base_url` を明示設定する必要がある場合があります。

注意点:

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や、Responses websocket `/responses` endpoint をサポートしない非 OpenAI プロバイダーには適用されません。
-   環境で利用可能でない場合は `websockets` パッケージをインストールしてください。
-   websocket トランスポートを有効化した後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使えます。複数ターンのワークフローで、ターン間（およびネストした agent-as-tool 呼び出し間）で同じ websocket 接続を再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM integration](./litellm.md) 経由で利用できます。まず、litellm dependency group をインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックス付きで任意の [supported models](https://docs.litellm.ai/docs/providers) を使います。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に便利です。これは LLM プロバイダーが OpenAI 互換 API endpoint を持ち、`base_url` と `api_key` を設定できる場合向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行のすべてのエージェントでカスタムモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを混在できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なほとんどのモデルを簡単に使う方法として [LiteLLM integration](./litellm.md) があります。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` によるトレーシング無効化、または [別のトレーシングプロセッサー](../tracing.md) の設定を推奨します。

!!! note

    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API / model を使っています。LLM プロバイダーが対応している場合は、Responses の使用を推奨します。

## 高度なモデル選択と混在

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクにはより大型で高性能なモデルを使えます。[`Agent`][agents.Agent] を設定する際は、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の shape をサポートしていますが、2 つの shape はサポートする機能とツールのセットが異なるため、ワークフローごとに 1 つのモデル shape を使うことを推奨します。ワークフローでモデル shape の混在が必要な場合は、使用中のすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使うモデルをさらに設定したい場合は、temperature などの任意のモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

#### 一般的な高度な `ModelSettings` オプション

OpenAI Responses API を使う場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、それらに `extra_args` は不要です。

| Field | Use it for |
| --- | --- |
| `parallel_tool_calls` | 同一ターンで複数のツール呼び出しを許可または禁止します。 |
| `truncation` | コンテキスト超過時に失敗する代わりに、Responses API に最も古い会話項目を削除させるには `"auto"` を設定します。 |
| `prompt_cache_retention` | たとえば `"24h"` のように、キャッシュされた prompt prefix をより長く保持します。 |
| `response_include` | `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、よりリッチな response payload を要求します。 |
| `top_logprobs` | 出力テキストの top-token logprobs を要求します。SDK は `message.output_text.logprobs` も自動追加します。 |

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

プロバイダー固有、または SDK がまだトップレベルで直接公開していない新しいリクエストフィールドが必要な場合は `extra_args` を使ってください。

また、OpenAI の Responses API を使う場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、これらも `extra_args` で渡せます。

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

トレーシング関連エラーが出る場合、trace が OpenAI サーバーにアップロードされる一方で OpenAI API キーがないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用 OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーは trace のアップロードにのみ使われ、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。
3. 非 OpenAI のトレーシングプロセッサーを使う。[トレーシング docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK は既定で Responses API を使いますが、ほとんどの他 LLM プロバイダーはまだ対応していません。その結果、404 などの問題が発生する場合があります。解決には次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### Structured outputs サポート

モデルプロバイダーによっては [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデルプロバイダーの制約です。JSON 出力はサポートしていても、出力に使う `json_schema` を指定できません。現在修正を進めていますが、JSON schema 出力をサポートするプロバイダーの利用を推奨します。そうでない場合、形式不正な JSON によりアプリが頻繁に壊れるためです。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を把握しておく必要があります。そうしないとエラーになる場合があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらをサポートしていません。次の制約に注意してください。

-   非対応の `tools` を理解しないプロバイダーに送らない
-   テキスト専用モデルを呼ぶ前にマルチモーダル入力を除外する
-   structured JSON 出力をサポートしないプロバイダーは、ときどき無効な JSON を生成する点に注意する