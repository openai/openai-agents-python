---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデル向けの即時利用可能なサポートが 2 種類あります。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

設定に応じて、このページを次の順序で利用してください。

| 目標 | 開始箇所 |
| --- | --- |
| SDK のデフォルトで OpenAI ホストモデルを使う | [OpenAI モデル](#openai-models) |
| WebSocket 転送で OpenAI Responses API を使う | [Responses WebSocket 転送](#responses-websocket-transport) |
| OpenAI 以外のプロバイダーを使う | [OpenAI 以外のモデル](#non-openai-models) |
| 1 つのワークフローでモデル / プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) と [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [OpenAI 以外のプロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` 初期化時にモデルを指定しない場合、デフォルトモデルが使われます。現在のデフォルトは、互換性と低レイテンシのため [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。利用可能であれば、明示的な `model_settings` を維持したまま、より高品質な [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) をエージェントに設定することを推奨します。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような他モデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで一貫して特定モデルを使いたい場合は、エージェント実行前に環境変数 `OPENAI_DEFAULT_MODEL` を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

次に、`RunConfig` を介して実行単位のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) などの GPT-5.x モデルを使うと、SDK はデフォルトの `ModelSettings` を適用します。ほとんどのユースケースで最適に動作する設定が使われます。デフォルトモデルの reasoning effort を調整するには、独自の `ModelSettings` を渡してください。

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

より低レイテンシにするには、`gpt-5.2` で `reasoning.effort="none"` を使うことを推奨します。gpt-4.1 ファミリー（ mini / nano を含む）も、インタラクティブなエージェントアプリ構築において引き続き有力な選択肢です。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 のモデル名を渡すと、SDK は任意のモデルと互換性のある汎用 `ModelSettings` に戻します。

### Responses WebSocket 転送

デフォルトでは、OpenAI Responses API リクエストは HTTP 転送を使います。OpenAI バックエンドのモデルを使う場合は、WebSocket 転送を有効化できます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI provider によって解決される OpenAI Responses モデル（ `"gpt-5.2"` のような文字列モデル名を含む）に影響します。

転送方式の選択は、SDK がモデル名をモデルインスタンスへ解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡した場合、その転送方式はすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は WebSocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡すと、グローバルデフォルトではなく、その provider が転送方式の選択を制御します。

WebSocket 転送は、provider 単位または実行単位でも設定できます。

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

プレフィックスベースのモデルルーティング（たとえば 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在）を使う必要がある場合は、[`MultiProvider`][agents.MultiProvider] を使い、代わりに `openai_use_responses_websocket=True` をそこで設定してください。

カスタムの OpenAI 互換 endpoint や proxy を使う場合、WebSocket 転送には互換性のある WebSocket `/responses` endpoint も必要です。これらの構成では `websocket_base_url` を明示的に設定する必要がある場合があります。

注意:

-   これは WebSocket 転送上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や、Responses WebSocket `/responses` endpoint をサポートしない OpenAI 以外のプロバイダーには適用されません。
-   環境で未導入の場合は、`websockets` パッケージをインストールしてください。
-   WebSocket 転送を有効化した後に、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで、同じ WebSocket 接続をターン間（およびネストされた agent-as-tool 呼び出し間）で再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## OpenAI 以外のモデル

ほとんどの OpenAI 以外のモデルは、[ LiteLLM 統合](./litellm.md) を通じて利用できます。まず、litellm dependency group をインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックス付きで [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 以外のモデルを使う他の方法

他の LLM provider は、さらに 3 つの方法で統合できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。これは、LLM provider が OpenAI 互換 API endpoint を持ち、`base_url` と `api_key` を設定できる場合向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行のすべてのエージェントにカスタムモデル provider を使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なる provider を組み合わせられます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なモデルの多くを簡単に使う方法としては、[ LiteLLM 統合](./litellm.md) があります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` によってトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では、ほとんどの LLM provider がまだ Responses API をサポートしていないため、Chat Completions API / model を使っています。LLM provider が対応している場合は、Responses の利用を推奨します。

## 高度なモデル選択と混在

単一のワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクにはより大型で高性能なモデルを使う、といった構成です。[`Agent`][agents.Agent] を設定する際は、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、2 つの形状はサポートする機能とツールのセットが異なるため、各ワークフローでは単一のモデル形状を使うことを推奨します。ワークフローでモデル形状の混在が必要な場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使うモデルをさらに設定したい場合は、温度などの任意のモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使う場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。これらがトップレベルで利用できない場合でも、`extra_args` を使って渡せます。

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

### トレーシングクライアントエラー 401

トレーシング関連のエラーが出る場合、トレースは OpenAI サーバーにアップロードされる一方で、OpenAI API キーがないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. OpenAI 以外のトレースプロセッサーを使う。[トレーシングドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、ほとんどの他の LLM provider はまだ対応していません。その結果、404 や類似の問題が発生する場合があります。解決するには次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部のモデル provider は [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデル provider の制約です。JSON 出力はサポートしていても、出力で使う `json_schema` を指定できません。この問題の修正を進めていますが、JSON schema 出力をサポートする provider に依存することを推奨します。そうでない場合、JSON の形式不正によりアプリが頻繁に壊れるためです。

## プロバイダー間でのモデル混在

モデル provider 間の機能差を認識しておく必要があります。そうしないとエラーに遭遇する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしますが、多くの他プロバイダーはこれらをサポートしません。次の制約に注意してください。

-   非対応の provider には、理解できない `tools` を送信しない
-   テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外する
-   structured JSON 出力をサポートしない provider は、無効な JSON をときどき生成する点に注意する