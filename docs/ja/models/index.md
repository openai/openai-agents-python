---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデル向けのサポートが 2 つの形で標準搭載されています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

設定に応じて、このページを次の順序で利用してください。

| Goal | Start here |
| --- | --- |
| SDK のデフォルトで OpenAI ホストモデルを使う | [OpenAI モデル](#openai-models) |
| websocket トランスポート経由で OpenAI Responses API を使う | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 非 OpenAI プロバイダーを使う | [非 OpenAI モデル](#non-openai-models) |
| 1 つのワークフローでモデル / プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) と [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [非 OpenAI プロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` の初期化時にモデルを指定しない場合は、デフォルトモデルが使われます。現在のデフォルトは、互換性と低レイテンシのために [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) です。利用可能であれば、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) にエージェントを設定することを推奨します。

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

1 つ目は、カスタムモデルを設定していないすべてのエージェントで特定モデルを一貫して使いたい場合、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

2 つ目は、`RunConfig` 経由で実行時のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) などの GPT-5 モデルを使う場合、SDK はデフォルトの `ModelSettings` を適用します。これはほとんどのユースケースで最適に動作する設定です。デフォルトモデルの推論 effort を調整するには、独自の `ModelSettings` を渡してください。

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

低レイテンシのためには、`gpt-5.4` で `reasoning.effort="none"` を使うことを推奨します。gpt-4.1 ファミリー（ mini / nano バリアントを含む）も、対話型エージェントアプリの構築において引き続き有力な選択肢です。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡すと、SDK は任意モデルと互換性のある汎用 `ModelSettings` に戻します。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使います。OpenAI バックエンドモデルを使う場合、websocket トランスポートを有効化できます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデル（`"gpt-5.4"` のような文字列モデル名を含む）に影響します。

トランスポート選択は、SDK がモデル名をモデルインスタンスに解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡した場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合は、グローバルデフォルトではなく、そのプロバイダーがトランスポート選択を制御します。

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

プレフィックスベースのモデルルーティング（例: 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在） が必要な場合は、[`MultiProvider`][agents.MultiProvider] を使い、代わりにそこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は 2 つの従来デフォルトを維持します。

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスは、そのまま通過せず `UserError` を送出します。

OpenAI プロバイダーを、リテラルな名前空間付きモデル ID を期待する OpenAI 互換エンドポイントに向ける場合は、明示的に pass-through 動作を有効にしてください。websocket 有効構成では、`MultiProvider` 側でも `openai_use_responses_websocket=True` を維持してください。

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

バックエンドがリテラルの `openai/...` 文字列を期待する場合は `openai_prefix_mode="model_id"` を使います。バックエンドが `openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使います。これらのオプションは websocket トランスポート外の `MultiProvider` でも使えます。この例では、このセクションで説明しているトランスポート設定の一部であるため websocket を有効化したままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用可能です。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使う場合、websocket トランスポートには互換性のある websocket `/responses` エンドポイントも必要です。これらの構成では `websocket_base_url` を明示的に設定する必要がある場合があります。

注意事項:

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や非 OpenAI プロバイダーには、Responses websocket `/responses` エンドポイントをサポートしていない限り適用されません。
-   環境でまだ利用可能でない場合は `websockets` パッケージをインストールしてください。
-   websocket トランスポート有効化後は、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使えます。同じ websocket 接続をターン間（およびネストした agent-as-tool 呼び出し）で再利用したいマルチターンワークフローでは、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[Running agents](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

他の多くの非 OpenAI モデルは [LiteLLM integration](./litellm.md) で利用できます。まず、litellm 依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックス付きで任意の [supported models](https://docs.litellm.ai/docs/providers) を使います。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーはさらに 3 つの方法で統合できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。これは、LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより、「この実行内のすべてのエージェントでカスタムモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスに対してモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使えます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能な多くのモデルを簡単に使う方法として [LiteLLM integration](./litellm.md) があります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` によるトレーシング無効化、または [別のトレーシングプロセッサー](../tracing.md) の設定を推奨します。

!!! note

    これらの例では Chat Completions API / モデルを使っています。これは多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。LLM プロバイダーがサポートしている場合は、Responses の利用を推奨します。

## 高度なモデル選択と混在

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージにはより小さく高速なモデルを使い、複雑なタスクにはより大規模で高性能なモデルを使うことができます。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしていますが、2 つはサポートする機能とツールのセットが異なるため、各ワークフローでは 1 つのモデル形に統一することを推奨します。ワークフロー上でモデル形の混在が必要な場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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

OpenAI Responses API を使う場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、それらに `extra_args` は不要です。

| Field | Use it for |
| --- | --- |
| `parallel_tool_calls` | 同一ターンでの複数ツール呼び出しを許可 / 禁止します。 |
| `truncation` | コンテキスト超過時に失敗させる代わりに、Responses API に最も古い会話項目を削除させるには `"auto"` を設定します。 |
| `prompt_cache_retention` | たとえば `"24h"` のように、キャッシュされたプロンプト接頭辞をより長く保持します。 |
| `response_include` | `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より豊富なレスポンス payload を要求します。 |
| `top_logprobs` | 出力テキストの上位トークン logprobs を要求します。SDK は `message.output_text.logprobs` も自動追加します。 |

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

また OpenAI の Responses API では、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、`extra_args` で渡すこともできます。

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

トレーシング関連エラーが出る場合、トレースは OpenAI サーバーにアップロードされますが OpenAI API キーがないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースアップロード専用で、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI トレースプロセッサーを使う。[tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、ほとんどの他 LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生することがあります。解決には 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に有効です。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが出ることがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデルプロバイダーの制約です。JSON 出力はサポートしていても、出力に使う `json_schema` を指定できません。現在この問題の修正に取り組んでいますが、JSON schema 出力をサポートするプロバイダーの利用を推奨します。そうでない場合、不正な JSON によってアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を把握しておく必要があります。そうしないとエラーが発生する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしますが、多くの他プロバイダーはこれらをサポートしません。以下の制約に注意してください。

-   サポートされない `tools` を、理解できないプロバイダーに送らない
-   テキスト専用モデルを呼び出す前にマルチモーダル入力を除外する
-   structured JSON outputs をサポートしないプロバイダーは、無効な JSON をときどき生成する点に注意する