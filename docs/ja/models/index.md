---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルに対する標準サポートが 2 つの形で用意されています:

-   **推奨**: 新しい Responses API を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   Chat Completions API を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合は、デフォルトのモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェント型ワークフローにおける予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) などの他のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### デフォルトの OpenAI モデル

カスタムモデルを設定していないすべてのエージェントで一貫して特定のモデルを使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 の推論モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用する場合、SDK はデフォルトで妥当な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` の両方を `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

より低レイテンシや特定の要件のために、別のモデルと設定を選択できます。デフォルトモデルの推論負荷を調整するには、独自の `ModelSettings` を渡します:

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

特に低レイテンシを重視する場合は、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) モデルで `reasoning.effort="minimal"` を使用すると、デフォルト設定よりも高速に応答が返ることがよくあります。ただし、Responses API の一部の組み込みツール（ファイル検索 や 画像生成 など）は `"minimal"` の推論負荷をサポートしていないため、この Agents SDK のデフォルトは `"low"` になっています。

#### 非 GPT-5 モデル

カスタムの `model_settings` を指定せずに GPT-5 以外のモデル名を渡した場合、SDK はあらゆるモデルと互換性のある汎用的な `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を通じて、ほとんどの他社製モデルを使用できます。まず、litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダを統合する方法はさらに 3 通りあります（code examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に便利です。これは、LLM プロバイダが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に該当します。設定可能なサンプルは [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行のすべてのエージェントでカスタムモデルプロバイダを使用する」と指定できます。設定可能なサンプルは [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できます。これにより、異なるエージェントごとに異なるプロバイダを組み合わせて使用できます。利用可能なモデルの多くを簡単に使う方法として、[LiteLLM 連携](./litellm.md) の利用をおすすめします。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの code examples では Chat Completions API/モデルを使用しています。多くの LLM プロバイダはまだ Responses API をサポートしていないためです。LLM プロバイダがサポートしている場合は、Responses の使用をおすすめします。

## モデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。例えば、トリアージには小型で高速なモデルを使用し、複雑なタスクには大型で高性能なモデルを使用する、といった使い分けです。[`Agent`][agents.Agent] を設定する際、次のいずれかの方法で特定のモデルを選択できます:

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、各ワークフローでは 1 つのモデル形状に統一することをおすすめします。両者はサポートする機能やツールのセットが異なるためです。ワークフローでモデル形状を組み合わせる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1.  OpenAI のモデル名を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を提供します。

エージェントで使用するモデルをさらに設定したい場合は、温度などのオプションのモデル構成パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合は、[他にもいくつかのオプションのパラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、`extra_args` を使用してそれらも渡すことができます。

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

## 他社 LLM プロバイダ使用時の一般的な問題

### トレーシングクライアントのエラー 401

トレーシングに関連するエラーが発生する場合、トレースは OpenAI のサーバーにアップロードされる一方で、OpenAI API キーを持っていないことが原因です。解決策は次の 3 つです:

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI のキーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. OpenAI 以外のトレース プロセッサーを使用する。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの他社 LLM プロバイダはまだサポートしていません。その結果、404 などの問題が発生する場合があります。解決するには次の 2 つの方法があります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。このため、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダの制限で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないというものです。現在この問題の解決に取り組んでいますが、JSON schema 出力をサポートするプロバイダに依存することをおすすめします。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダをまたいだモデルの組み合わせ

モデルプロバイダ間の機能差異を理解しておく必要があります。そうでないとエラーが発生する場合があります。例えば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 および Web 検索 をサポートしていますが、他の多くのプロバイダはこれらの機能をサポートしていません。次の制限に注意してください:

-   サポートされていない `tools` を理解しないプロバイダに送信しないでください
-   テキスト専用のモデルを呼び出す前に、マルチモーダル入力をフィルタリングしてください
-   構造化された JSON 出力をサポートしていないプロバイダは、無効な JSON を出力する場合があることに注意してください。