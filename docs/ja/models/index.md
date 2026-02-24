---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに使える形で 2 つの方式でサポートしています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## モデル設定の選択

セットアップに応じて、次の順序でこのページを使用してください。

| 目的 | ここから開始 |
| --- | --- |
| SDK デフォルトで OpenAI がホストするモデルを使う | [OpenAI モデル](#openai-models) |
| websocket トランスポートで OpenAI Responses API を使う | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| OpenAI 以外のプロバイダーを使う | [OpenAI 以外のモデル](#non-openai-models) |
| 1 つのワークフロー内でモデル/プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) と [プロバイダー間のモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [OpenAI 以外のプロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` を初期化するときにモデルを指定しない場合、デフォルトモデルが使用されます。互換性と低レイテンシーのため、現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。アクセス可能であれば、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) をエージェントに設定することを推奨します。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

次に、`RunConfig` を使って実行ごとのデフォルトモデルを設定できます。エージェントにモデルを設定しない場合は、この実行のモデルが使用されます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.2"),
)
```

#### GPT-5.x モデル

この方法で [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような任意の GPT-5.x モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。これは多くのユースケースで最もよく機能するものを設定します。デフォルトモデルの推論努力を調整するには、独自の `ModelSettings` を渡してください。

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.2 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5.x model name explicitly:
    model="gpt-5.2",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

低レイテンシーのため、`gpt-5.2` では `reasoning.effort="none"` の使用を推奨します。gpt-4.1 ファミリー（mini および nano バリアントを含む）も、対話的なエージェントアプリを構築するうえで引き続き堅実な選択肢です。

#### GPT-5 以外のモデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡すと、SDK は任意のモデルと互換性のある汎用 `ModelSettings` に戻ります。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI バックエンドのモデルを使用する際に websocket トランスポートを有効化できます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデル（`"gpt-5.2"` のような文字列のモデル名を含む）に影響します。

プロバイダーごと、または実行ごとに websocket トランスポートを設定することもできます。

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

プレフィックスベースのモデルルーティング（たとえば 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在させる）を必要とする場合は、[`MultiProvider`][agents.MultiProvider] を使用し、代わりにそこで `openai_use_responses_websocket=True` を設定してください。

注:

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。
-   環境に `websockets` パッケージがまだない場合はインストールしてください。
-   websocket トランスポートを有効化した後は、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。同一の websocket 接続をターン（およびネストされた agent-as-tool 呼び出し）をまたいで再利用したいマルチターンのワークフローでは、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーの使用を推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## OpenAI 以外のモデル

[LiteLLM 統合](./litellm.md) を介して、ほとんどの OpenAI 以外のモデルを使用できます。まず、litellm の依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックス付きで [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 以外のモデルを使うその他の方法

他の LLM プロバイダーを統合する方法は、さらに 3 つあります（例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使います。設定可能な例として [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定可能な例として [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] を使うと、特定の `Agent` インスタンスに対してモデルを指定できます。これにより、異なるエージェントで異なるプロバイダーを混在できます。設定可能な例として [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なモデルの大半を簡単に使う方法としては、[LiteLLM 統合](./litellm.md) の利用があります。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md) をセットアップすることを推奨します。

!!! note

    これらの例では Chat Completions API/モデルを使用しています。これは、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないためです。LLM プロバイダーがサポートしている場合は、Responses の使用を推奨します。

## 高度なモデル選択と混在

単一のワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージにはより小さく高速なモデルを使い、複雑なタスクにはより大きく高性能なモデルを使えます。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名と、その名前を Model インスタンスにマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、2 つの形状はサポートする機能やツールのセットが異なるため、各ワークフローでは単一のモデル形状を使用することを推奨します。ワークフローでモデル形状を混在させる必要がある場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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
    model="gpt-5",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1. OpenAI モデル名を直接設定します。
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

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、`extra_args` を使ってそれらも渡せます。

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

## OpenAI 以外のプロバイダーのトラブルシューティング

### トレーシングのクライアントエラー 401

トレーシングに関連するエラーが出る場合、トレースが OpenAI サーバーにアップロードされる一方で、OpenAI API キーがないことが原因です。解決するには 3 つの選択肢があります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものである必要があります。
3. OpenAI 以外のトレースプロセッサーを使用する。[tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他の LLM プロバイダーはまだこれをサポートしていません。その結果、404 などの問題が見られる場合があります。解決するには 2 つの選択肢があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その結果、次のようなエラーになる場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの不備です。JSON 出力はサポートしていても、出力に使用する `json_schema` の指定を許可していません。修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することを推奨します。そうでない場合、不正な形式の JSON によりアプリが頻繁に壊れるためです。

## プロバイダー間のモデル混在

モデルプロバイダー間の機能差を把握しておかないと、エラーになる可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型の file search と web search をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

-   理解できないプロバイダーに、未対応の `tools` を送らない
-   テキスト専用モデルを呼び出す前に、マルチモーダル入力をフィルタリングする
-   structured JSON 出力をサポートしないプロバイダーは、無効な JSON をときどき生成することに注意する