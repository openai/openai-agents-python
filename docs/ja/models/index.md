---
search:
  exclude: true
---
# モデル

Agents SDK は、OpenAI モデルを次の 2 種類でサポートしています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]  
  新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]  
  [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM 連携](./litellm.md) を通じて利用できます。まず、litellm の依存グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、任意の [対応モデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを利用するその他の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` インスタンスをグローバルに LLM クライアントとして使いたい場合に便利です。LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。  
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで使用します。「この実行で動くすべてのエージェントに対してカスタムモデルプロバイダーを使う」と宣言できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。  
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の `Agent` インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使えます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどのモデルを簡単に使う方法として [LiteLLM 連携](./litellm.md) があります。  

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note
    これらの例では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もし使用している LLM プロバイダーが Responses API をサポートしている場合は、Responses の利用をお勧めします。

## モデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使用することがあります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクには大きく高性能なモデルを使う、といったケースです。[`Agent`][agents.Agent] を設定する際、次のいずれかでモデルを指定できます。

1. モデル名を直接渡す。  
2. 任意のモデル名と、それを `Model` インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、ワークフローでは 1 種類のモデル形状のみを使うことを推奨します。2 つの形状は利用できる機能やツールが異なるためです。ワークフローで形状を混在させる場合は、使用するすべての機能が両方で利用可能か確認してください。

```python
from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model="o3-mini", # (1)!
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIChatCompletionsModel( # (2)!
        model="gpt-4o",
        openai_client=AsyncOpenAI()
    ),
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model="gpt-3.5-turbo",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1. OpenAI モデル名を直接設定しています。  
2. [`Model`][agents.models.interface.Model] 実装を提供しています。  

エージェントで使用するモデルをさらに設定したい場合は、温度などのオプションパラメーターを持つ [`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

さらに、OpenAI の Responses API を使用する際は、`user` や `service_tier` など [いくつかの追加パラメーター](https://platform.openai.com/docs/api-reference/responses/create) を指定できます。トップレベルで指定できない場合は、`extra_args` を使って渡してください。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(
        temperature=0.1,
        extra_args={"service_tier": "flex", "user": "user_12345"},
    ),
)
```

## 他の LLM プロバイダーを使用する際の一般的な問題

### Tracing クライアントのエラー 401

トレーシング関連のエラーが発生する場合、トレースが OpenAI サーバーにアップロードされる際に OpenAI API キーがないことが原因です。解決策は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. 非 OpenAI のトレースプロセッサーを使う。詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。  

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの LLM プロバイダーはまだ対応していません。その結果、404 エラーなどが発生することがあります。解決策は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。  
   これは環境変数 `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使う。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) を参照してください。  

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) に対応していません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のプロバイダーの制限によるものです。JSON 出力には対応していても、出力に使用する `json_schema` を指定できません。現在修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーを利用することを推奨します。そうでない場合、アプリが不正な JSON により頻繁に壊れる可能性があります。

## プロバイダー間でのモデルの混在

モデルプロバイダー間の機能差に注意しないとエラーが発生します。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホストされたファイル検索および Web 検索をサポートしていますが、多くの他プロバイダーはこれらの機能をサポートしていません。以下の制限に注意してください。

- 対応していないプロバイダーには `tools` を送らない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力を除外する  
- structured JSON 出力をサポートしないプロバイダーでは、無効な JSON が生成されることがある点を認識する