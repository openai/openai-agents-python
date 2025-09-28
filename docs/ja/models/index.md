---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルのサポートが 2 つの形で標準搭載されています:

-  **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-  [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、デフォルトのモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェントワークフローにおける予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) など別のモデルへ切り替える場合は、次のセクションの手順に従ってください。

### 既定の OpenAI モデル

カスタムモデルを設定していないすべての エージェント で特定のモデルを一貫して使用したい場合は、エージェント を実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、[`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) といった GPT-5 の reasoning モデルをこの方法で使用すると、SDK は既定で妥当な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` をともに `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

より低レイテンシや特定要件のために、異なるモデルや設定を選べます。デフォルトモデルの reasoning effort を調整するには、独自の `ModelSettings` を渡します:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    model_settings=ModelSettings(reasoning=Reasoning(effort="minimal"), verbosity="low")
    # If OPENAI_DEFAULT_MODEL=gpt-5 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    # model="gpt-5",
)
```

特に低レイテンシが必要な場合、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) モデルで `reasoning.effort="minimal"` を使うと、デフォルト設定よりも高速に応答が返ることが多いです。ただし、Responses API の一部の組み込みツール（ファイル検索や画像生成など）は `"minimal"` の reasoning effort をサポートしていません。そのため、この Agents SDK の既定は `"low"` になっています。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK は任意のモデルと互換性のある汎用の `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を通じて、ほとんどの非 OpenAI モデルを利用できます。まず、litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポート対象モデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使うその他の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、グローバルに `AsyncOpenAI` のインスタンスを LLM クライアントとして使いたい場合に有用です。これは LLM プロバイダーが OpenAI 互換の API エンドポイントを提供し、`base_url` と `api_key` を設定できるケース向けです。設定可能なサンプルは [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで指定します。これにより、「この実行ではすべての エージェント にカスタムモデルプロバイダーを使う」と指定できます。設定可能なサンプルは [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できます。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使用できます。設定可能なサンプルは [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なモデルの多くを簡単に使う方法として、[LiteLLM 連携](./litellm.md) があります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` で トレーシング を無効化するか、[別の トレーシング プロセッサー](../tracing.md) を設定することをお勧めします。

!!! note

    これらの examples では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もし利用中の LLM プロバイダーがサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェント ごとに異なるモデルを使いたい場合があります。例えば、選別には小型で高速なモデルを使い、複雑なタスクには大きく高性能なモデルを使うといった具合です。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選べます:

1. モデル名を渡す。
2. 任意のモデル名 + それを Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、ワークフローごとに単一のモデル形状を使用することを推奨します。両者はサポートする機能やツールが異なるためです。ワークフロー上でモデル形状を組み合わせる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI のモデル名を直接設定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

エージェント で使用するモデルをさらに構成したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。これは temperature などの任意のモデル構成 パラメーター を提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する際には、[他にも任意 パラメーター がいくつかあります](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）。それらがトップレベルで利用できない場合は、`extra_args` を使って渡せます。

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

## 他の LLM プロバイダー使用時の一般的な問題

### トレーシング クライアントエラー 401

トレーシング に関連するエラーが発生する場合、トレースは OpenAI の サーバー にアップロードされるため、OpenAI API キーがないことが原因です。解決するには次の 3 つの選択肢があります:

1. トレーシング を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング 用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI の trace プロセッサーを使用する。[トレーシング ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生することがあります。解決するには、次の 2 つの方法があります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダー側の制限で、JSON 出力はサポートしているものの、出力に使用する `json_schema` を指定できません。現在この点の改善に取り組んでいますが、JSON schema 出力をサポートするプロバイダーに依存することを推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れてしまいます。

## プロバイダー間でのモデルの混在

モデルプロバイダー間の機能差を理解していないと、エラーに遭遇する可能性があります。例えば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 および Web 検索 をサポートしますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください:

-  サポートされていない `tools` を理解しないプロバイダーに送信しない
-  テキストのみのモデルを呼び出す前に、マルチモーダル入力を除去する
-  structured JSON 出力をサポートしないプロバイダーは、無効な JSON を出力することがある点に注意する