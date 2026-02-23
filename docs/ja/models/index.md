---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデル向けの即時利用可能なサポートが 2 種類含まれています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合は、デフォルトモデルが使用されます。デフォルトは現在、互換性と低レイテンシのために [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。アクセス権がある場合は、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) をエージェントに設定することを推奨します。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

次に、`RunConfig` を介して実行単位のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合は、この実行のモデルが使用されます。

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

この方法で [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような GPT-5.x モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。これは、ほとんどのユースケースで最もよく機能する設定を用います。デフォルトモデルの reasoning の effort を調整するには、独自の `ModelSettings` を渡します。

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

低レイテンシのためには、`gpt-5.2` で `reasoning.effort="none"` を使用することを推奨します。gpt-4.1 ファミリー（mini と nano のバリアントを含む）も、インタラクティブなエージェントアプリを構築するうえで引き続き堅実な選択肢です。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで非 GPT-5 のモデル名を渡すと、SDK はどのモデルとも互換性のある汎用 `ModelSettings` に戻ります。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使用します。OpenAI バックエンドのモデルを使用する場合、websocket トランスポートをオプトインできます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI provider により解決される OpenAI Responses モデル（`"gpt-5.2"` のような文字列モデル名を含む）に影響します。

provider 単位または実行単位でも websocket トランスポートを設定できます。

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

（たとえば 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在させるなど）接頭辞ベースのモデルルーティングが必要な場合は、[`MultiProvider`][agents.MultiProvider] を使用し、代わりにそこで `openai_use_responses_websocket=True` を設定してください。

注意:

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。
-   `websockets` パッケージが環境にまだない場合はインストールしてください。
-   websocket トランスポートを有効化した後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。ターン間（およびネストされた agent-as-tool 呼び出し）で同一の websocket 接続を再利用したい複数ターンのワークフローでは、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md) ガイドと、[`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

[LiteLLM integration](./litellm.md) を介して、ほとんどの非 OpenAI モデルを使用できます。まず、litellm の dependency group をインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックス付きで、任意の [supported models](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使用するその他の方法

他の LLM provider を統合する方法は、さらに 3 つあります（例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に有用です。これは、LLM provider が OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に向けたものです。[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) に設定可能な例があります。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行内のすべてのエージェントでカスタムの model provider を使う」と指定できます。[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) に設定可能な例があります。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の `Agent` インスタンス上でモデルを指定できます。これにより、異なるエージェントで異なる provider を組み合わせて使えます。[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) に設定可能な例があります。利用可能なモデルの多くを簡単に使う方法として、[LiteLLM integration](./litellm.md) もあります。

`platform.openai.com` からの API key を持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では Chat Completions API / モデルを使用しています。多くの LLM provider がまだ Responses API をサポートしていないためです。LLM provider が対応している場合は、Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクにはより大きく高性能なモデルを使う、といった構成です。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を `Model` インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、形状によってサポートする機能とツールのセットが異なるため、各ワークフローでは単一のモデル形状を使用することを推奨します。ワークフローでモデル形状を混在させる必要がある場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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

1.  OpenAI モデル名を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を提供します。

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

また、OpenAI の Responses API を使用する場合、[いくつかの追加の任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。それらがトップレベルで利用できない場合でも、`extra_args` を使って渡せます。

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

## 他の LLM provider 利用時の一般的な問題

### Tracing client error 401

トレーシングに関するエラーが出る場合、トレースが OpenAI サーバーへアップロードされる一方で OpenAI API key を持っていないことが原因です。解決策は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用に OpenAI key を設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API key はトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものである必要があります。
3. 非 OpenAI のトレースプロセッサーを使用する。[トレーシング docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使用しますが、他の LLM provider の多くはまだ対応していません。その結果として 404 などの問題が発生することがあります。解決策は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部の model provider は [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部の model provider の制約です。JSON 出力はサポートしていても、出力に使用する `json_schema` の指定が許可されません。これに対する修正に取り組んでいますが、JSON schema 出力をサポートする provider に依存することを推奨します。そうでない場合、JSON の不正形式によってアプリが頻繁に壊れる可能性があります。

## provider をまたいだモデルの混在

model provider 間の機能差を把握していないと、エラーに遭遇する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型の file search と web search をサポートしますが、他の provider の多くはこれらの機能をサポートしていません。次の制約に注意してください。

-   未対応の provider に、理解できない `tools` を送らない
-   テキスト専用のモデルを呼び出す前に、マルチモーダル入力をフィルタリングする
-   structured JSON 出力をサポートしない provider は、無効な JSON を時折生成することがある点に注意する