---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに使える形で 2 つの方式でサポートしています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

設定に応じて、次の順序でこのページをご利用ください。

| 目的 | 開始場所 |
| --- | --- |
| SDK のデフォルトで OpenAI ホストモデルを使う | [OpenAI モデル](#openai-models) |
| websocket トランスポートで OpenAI Responses API を使う | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| OpenAI 以外のプロバイダーを使う | [非 OpenAI モデル](#non-openai-models) |
| 1 つのワークフローでモデル / プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) と [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [非 OpenAI プロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使われます。現在のデフォルトは、互換性と低レイテンシのため [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。アクセス可能であれば、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) にエージェントを設定することを推奨します。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような他モデルに切り替えるには、エージェントを設定する方法が 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで一貫して特定モデルを使いたい場合は、エージェント実行前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

次に、`RunConfig` 経由で実行ごとのデフォルトモデルを設定できます。エージェントにモデルを設定しない場合は、この実行のモデルが使われます。

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

この方法で [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) などの GPT-5.x モデルを使う場合、SDK はデフォルトの `ModelSettings` を適用します。これはほとんどのユースケースで最適に動作する設定です。デフォルトモデルの推論負荷を調整するには、独自の `ModelSettings` を渡してください。

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

低レイテンシのためには、`gpt-5.2` で `reasoning.effort="none"` を使うことを推奨します。gpt-4.1 ファミリー（ mini および nano バリアントを含む）も、対話型エージェントアプリ構築における有力な選択肢です。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡すと、SDK は任意モデルと互換性のある汎用 `ModelSettings` に戻します。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使います。OpenAI バックエンドのモデルを使う場合、websocket トランスポートを有効化できます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーで解決される OpenAI Responses モデル（`"gpt-5.2"` のような文字列モデル名を含む）に影響します。

トランスポート選択は、SDK がモデル名をモデルインスタンスに解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡した場合、そのトランスポートはすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡した場合は、グローバルデフォルトではなくそのプロバイダーがトランスポート選択を制御します。

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

プレフィックスベースのモデルルーティング（例: 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在）を使う必要がある場合は、代わりに [`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定してください。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使う場合、websocket トランスポートには互換性のある websocket `/responses` エンドポイントも必要です。そのような構成では、`websocket_base_url` を明示的に設定する必要がある場合があります。

注意:

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や非 OpenAI プロバイダーには、Responses websocket `/responses` エンドポイントをサポートしていない限り適用されません。
-   環境で未導入の場合は、`websockets` パッケージをインストールしてください。
-   websocket トランスポート有効化後は、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使えます。複数ターンのワークフローで同じ websocket 接続をターン間（およびネストした Agents-as-tools 呼び出し間）で再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーの利用を推奨します。[エージェント実行](../running_agents.md) ガイドと [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは、[LiteLLM 統合](./litellm.md) 経由で利用できます。まず、litellm 依存関係グループをインストールしてください。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックス付きで任意の[対応モデル](https://docs.litellm.ai/docs/providers)を使います。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使うその他の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に有用です。これは、LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能なコード例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより、「この実行のすべてのエージェントにカスタムモデルプロバイダーを使う」と指定できます。設定可能なコード例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスに対してモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定可能なコード例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能な多くのモデルを簡単に使う方法として、[LiteLLM 統合](./litellm.md) があります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらのコード例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API / モデルを使っています。LLM プロバイダーが対応している場合は、Responses の利用を推奨します。

## 高度なモデル選択と混在

単一ワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクにはより大型で高性能なモデルを使えます。[`Agent`][agents.Agent] を設定する際は、次のいずれかで特定モデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形式をサポートしますが、2 つはサポートする機能とツールのセットが異なるため、ワークフローごとに単一のモデル形式を使うことを推奨します。ワークフローでモデル形式を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

OpenAI Responses API を使っている場合、いくつかのリクエストフィールドにはすでに直接対応する `ModelSettings` フィールドがあるため、`extra_args` は不要です。

| フィールド | 用途 |
| --- | --- |
| `parallel_tool_calls` | 同一ターン内で複数のツール呼び出しを許可または禁止します。 |
| `truncation` | コンテキスト超過時に失敗する代わりに、Responses API が最も古い会話項目を破棄できるよう `"auto"` を設定します。 |
| `prompt_cache_retention` | たとえば `"24h"` のように、キャッシュされたプロンプトプレフィックスをより長く保持します。 |
| `response_include` | `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より豊富なレスポンスペイロードを要求します。 |
| `top_logprobs` | 出力テキストの上位トークン logprobs を要求します。SDK は `message.output_text.logprobs` も自動で追加します。 |

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.2",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合に `extra_args` を使います。

また、OpenAI の Responses API を使う場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。これらがトップレベルで利用できない場合も、`extra_args` で渡せます。

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

トレーシング関連のエラーが出る場合、これはトレースが OpenAI サーバーにアップロードされる一方で OpenAI API キーを持っていないためです。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使われ、[platform.openai.com](https://platform.openai.com/) 発行のものが必要です。
3. 非 OpenAI のトレースプロセッサーを使う。[トレーシングドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、ほとんどの他 LLM プロバイダーはまだこれをサポートしていません。その結果、404 や類似の問題が発生することがあります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) です。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生する場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部モデルプロバイダーの制約です。JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できません。現在修正に取り組んでいますが、JSON schema 出力をサポートするプロバイダーへの依存を推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を把握する必要があります。把握していないとエラーになる可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホストされたファイル検索と Web 検索をサポートしますが、他の多くのプロバイダーはこれらをサポートしません。次の制約に注意してください。

-   対応していないプロバイダーに未対応の `tools` を送らない
-   テキスト専用モデルを呼び出す前にマルチモーダル入力を除外する
-   structured JSON 出力をサポートしないプロバイダーは、ときどき無効な JSON を生成することを認識する