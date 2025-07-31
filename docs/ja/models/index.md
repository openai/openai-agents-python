---
search:
  exclude: true
---
# モデル

Agents SDK は、OpenAI モデルを 2 種類で即座にサポートしています:

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM 連携](./litellm.md) を通じて利用できます。まず、litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [対応モデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルのその他の使用方法

他の LLM プロバイダーを統合する方法は、さらに 3 つあります（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に code examples）:

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケースに適しています。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで使用します。この方法では、「この run 内のすべてのエージェントでカスタムモデルプロバイダーを使う」と指定できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の `Agent` インスタンスに対してモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせることが可能です。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。多くのモデルを簡単に使う方法として [LiteLLM 連携](./litellm.md) があります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの例では Chat Completions API / モデルを使用しています。ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないためです。LLM プロバイダーが Responses API をサポートしている場合は、Responses の利用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたいことがあります。たとえば、トリアージには小さくて高速なモデルを使い、複雑なタスクには大きく高性能なモデルを使う、という具合です。[`Agent`][agents.Agent] を設定する際、以下のいずれかでモデルを選択できます:

1. モデル名を直接渡す  
2. 任意のモデル名と、それを [`ModelProvider`][agents.models.interface.ModelProvider] が Model インスタンスにマッピングできるように渡す  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す  

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、ワークフローごとに 1 つのモデル形状を使用することを推奨します。両モデル形状は利用できる機能や tools が異なるためです。ワークフローで複数形状を混在させる場合は、使用するすべての機能が両方で利用可能か確認してください。

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

1. OpenAI モデル名を直接設定します。  
2. [`Model`][agents.models.interface.Model] 実装を提供します。  

エージェントで使用するモデルをさらに設定したい場合は、`temperature` などのオプション設定を持つ [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、`user` や `service_tier` など [追加のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで指定できない場合は、`extra_args` で渡せます。

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

## 他の LLM プロバイダーを使用する際のよくある問題

### トレーシング クライアント エラー 401

トレーシング関連のエラーが出る場合、トレースは OpenAI サーバーへアップロードされるため、OpenAI API キーがないことが原因です。以下の 3 つの方法で解決できます:

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. 非 OpenAI のトレースプロセッサーを使用する。詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの LLM プロバイダーはまだ対応していません。その結果、404 などのエラーが発生することがあります。解決策は 2 つあります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   code examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限です。JSON 出力には対応していても、出力に使用する `json_schema` を指定できません。現在この問題を解決中ですが、JSON スキーマ出力をサポートするプロバイダーを使用することを推奨します。そうでない場合、無効な JSON が生成され、アプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差に注意しないと、エラーの原因になります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホストされた file search や web search をサポートしていますが、多くの他プロバイダーはこれらをサポートしていません。次の制限に注意してください:

- 対応していないプロバイダーには `tools` を送信しない  
- テキスト専用モデルを呼び出す前にマルチモーダル入力を除去する  
- structured JSON 出力をサポートしていないプロバイダーでは、無効な JSON が生成されることがあります