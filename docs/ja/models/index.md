---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルの即時利用可能なサポートが 2 つの形で用意されています:

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデル設定の選択

設定に応じて、このページを次の順序で利用してください:

| 目的 | 開始ポイント |
| --- | --- |
| SDK のデフォルトで OpenAI ホストモデルを使用する | [OpenAI モデル](#openai-models) |
| websocket トランスポートで OpenAI Responses API を使用する | [Responses WebSocket トランスポート](#responses-websocket-transport) |
| 非 OpenAI プロバイダーを使用する | [非 OpenAI モデル](#non-openai-models) |
| 1 つのワークフローでモデル/プロバイダーを混在させる | [高度なモデル選択と混在](#advanced-model-selection-and-mixing) および [プロバイダー間でのモデル混在](#mixing-models-across-providers) |
| プロバイダー互換性の問題をデバッグする | [非 OpenAI プロバイダーのトラブルシューティング](#troubleshooting-non-openai-providers) |

## OpenAI モデル

`Agent` の初期化時にモデルを指定しない場合、デフォルトモデルが使われます。現在のデフォルトは、互換性と低レイテンシのため [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。アクセス可能であれば、明示的な `model_settings` を維持したまま、より高品質な [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) をエージェントに設定することを推奨します。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで一貫して特定モデルを使いたい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

次に、`RunConfig` を使って実行単位のデフォルトモデルを設定できます。エージェントにモデルを設定しない場合、この実行のモデルが使われます。

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

この方法で [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) などの GPT-5.x モデルを使う場合、SDK はデフォルトの `ModelSettings` を適用します。多くのユースケースで最適に機能する設定が適用されます。デフォルトモデルの推論 effort を調整するには、独自の `ModelSettings` を渡します:

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

低レイテンシのため、`gpt-5.2` では `reasoning.effort="none"` の使用を推奨します。gpt-4.1 ファミリー (mini と nano バリアントを含む) も、対話型エージェントアプリ構築の堅実な選択肢です。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで非 GPT-5 モデル名を渡した場合、SDK は任意モデルと互換性のある汎用 `ModelSettings` に戻します。

### Responses WebSocket トランスポート

デフォルトでは、OpenAI Responses API リクエストは HTTP トランスポートを使います。OpenAI バックドモデルを使う場合は、websocket トランスポートを有効にできます。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーで解決される OpenAI Responses モデル (`"gpt-5.2"` のような文字列モデル名を含む) に影響します。

トランスポート選択は、SDK がモデル名をモデルインスタンスに解決する際に行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、そのトランスポートはすでに固定されています: [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は websocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions のままです。`RunConfig(model_provider=...)` を渡す場合は、グローバルデフォルトではなくそのプロバイダーがトランスポート選択を制御します。

websocket トランスポートは、プロバイダー単位または実行単位でも設定できます:

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

プレフィックスベースのモデルルーティング (例: 1 回の実行で `openai/...` と `litellm/...` のモデル名を混在) が必要な場合は、[`MultiProvider`][agents.MultiProvider] を使い、そこで `openai_use_responses_websocket=True` を設定してください。

`MultiProvider` は 2 つの従来デフォルトを維持しています:

-   `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
-   不明なプレフィックスはそのまま渡されず、`UserError` を発生させます。

OpenAI プロバイダーを、リテラルな名前空間付きモデル ID を期待する OpenAI 互換エンドポイントに向ける場合は、パススルー動作を明示的に有効化してください。websocket 有効構成では、`MultiProvider` でも `openai_use_responses_websocket=True` を維持してください:

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

バックエンドがリテラルな `openai/...` 文字列を期待する場合は `openai_prefix_mode="model_id"` を使ってください。バックエンドが `openrouter/openai/gpt-4.1-mini` のような他の名前空間付きモデル ID を期待する場合は `unknown_prefix_mode="model_id"` を使ってください。これらのオプションは websocket トランスポート外の `MultiProvider` でも機能します。この例は、このセクションで説明しているトランスポート設定の一部として websocket を有効化したままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも使用可能です。

カスタム OpenAI 互換エンドポイントまたはプロキシを使う場合、websocket トランスポートには互換性のある websocket `/responses` エンドポイントも必要です。そのような構成では、`websocket_base_url` を明示的に設定する必要がある場合があります。

注記:

-   これは websocket トランスポート上の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions や非 OpenAI プロバイダーには、Responses websocket `/responses` エンドポイントをサポートしない限り適用されません。
-   環境で未導入の場合は、`websockets` パッケージをインストールしてください。
-   websocket トランスポートを有効化した後は、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで同じ websocket 接続をターン間 (およびネストした agent-as-tool 呼び出し) で再利用したい場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーの利用を推奨します。[エージェント実行](../running_agents.md) ガイドおよび [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは、[LiteLLM 統合](./litellm.md) 経由で使用できます。まず、litellm 依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて任意の [サポート対象モデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります (コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)):

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバル利用したい場合に有用です。これは、LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合向けです。設定可能なコード例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行内のすべてのエージェントでカスタムモデルプロバイダーを使う」と指定できます。設定可能なコード例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスにモデルを指定できます。これにより、異なるエージェントで異なるプロバイダーを組み合わせて使えます。設定可能なコード例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なほとんどのモデルを簡単に使う方法は、[LiteLLM 統合](./litellm.md) 経由です。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` によるトレーシング無効化、または [別のトレーシングプロセッサー](../tracing.md) の設定を推奨します。

!!! note

    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API / モデルを使っています。LLM プロバイダーが対応している場合は、Responses の使用を推奨します。

## 高度なモデル選択と混在

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクには大型で高性能なモデルを使うことができます。[`Agent`][agents.Agent] を設定するときは、次のいずれかで特定モデルを選択できます:

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしていますが、2 つの形はサポートする機能とツールのセットが異なるため、ワークフローごとに単一のモデル形を使うことを推奨します。モデル形の混在が必要な場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使用するモデルをさらに設定したい場合は、temperature などの任意モデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

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

OpenAI Responses API を使う場合、いくつかのリクエストフィールドにはすでに直接の `ModelSettings` フィールドがあるため、それらに `extra_args` は不要です。

| フィールド | 用途 |
| --- | --- |
| `parallel_tool_calls` | 同一ターン内で複数ツール呼び出しを許可または禁止します。 |
| `truncation` | コンテキストがあふれる際に失敗する代わりに Responses API が最古の会話項目を削除するよう、`"auto"` を設定します。 |
| `prompt_cache_retention` | たとえば `"24h"` で、キャッシュされたプロンプト接頭辞をより長く保持します。 |
| `response_include` | `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より豊富なレスポンスペイロードを要求します。 |
| `top_logprobs` | 出力テキストの上位トークン logprobs を要求します。SDK は `message.output_text.logprobs` も自動追加します。 |

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

プロバイダー固有、または SDK がまだトップレベルで直接公開していない新しいリクエストフィールドが必要な場合は、`extra_args` を使用してください。

また、OpenAI の Responses API を使う場合は、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create) (`user`、`service_tier` など) があります。トップレベルで利用できない場合は、`extra_args` で渡すこともできます。

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

トレーシング関連エラーが出る場合、トレースは OpenAI サーバーにアップロードされるため、OpenAI API キーがないことが原因です。解決には 3 つの選択肢があります:

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースアップロード専用で、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレースプロセッサーを使う。[トレーシングドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、ほとんどの他 LLM プロバイダーはまだサポートしていません。その結果として 404 などの問題が発生する場合があります。解決には 2 つの選択肢があります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) です。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていても、出力に使う `json_schema` を指定できません。この問題の修正に取り組んでいますが、JSON schema 出力をサポートするプロバイダーの利用を推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れるためです。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差を認識していないと、エラーに遭遇する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索と Web 検索をサポートしますが、多くの他プロバイダーはこれらをサポートしません。次の制約に注意してください:

-   未対応のプロバイダーには、未対応の `tools` を送らない
-   テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外する
-   structured JSON 出力をサポートしないプロバイダーは、ときどき無効な JSON を生成することを理解する