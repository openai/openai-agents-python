---
search:
  exclude: true
---
# モデル

Agents SDK には、すぐに使える OpenAI モデルのサポートが 2 種類用意されています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を利用すれば、多くの非 OpenAI モデルを利用できます。まずは litellm の依存関係グループをインストールしてください。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、[サポートされているモデル](https://docs.litellm.ai/docs/providers) を指定します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 他の非 OpenAI モデルを使用する方法

他の LLM プロバイダーを統合する方法は、さらに 3 つあります（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にコード例があります）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` のインスタンスをグローバルに LLM クライアントとして使用したい場合に便利です。OpenAI 互換のエンドポイントを持つプロバイダーで `base_url` と `api_key` を設定できるケースに該当します。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで設定します。これにより、「この run 内のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の `Agent` インスタンス単位でモデルを指定できます。エージェントごとに異なるプロバイダーを組み合わせて使用できます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどのモデルを簡単に使う方法としては、[LiteLLM 連携](./litellm.md) が便利です。

`platform.openai.com` の API キーを持たない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では Chat Completions API／モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もしお使いの LLM プロバイダーが Responses API をサポートしている場合は、Responses の利用を推奨します。

## モデルを組み合わせて使用する

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、仕分けには小さく高速なモデルを、複雑なタスクには大きく高性能なモデルを使うイメージです。[`Agent`][agents.Agent] を設定する際、以下のいずれかでモデルを指定できます。

1. モデル名を直接渡す。  
2. 任意のモデル名 + その名前をモデルインスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、ワークフローごとに 1 つのモデル形状に統一することを推奨します。両モデルは利用できる機能やツールが異なるためです。モデル形状を混在させる必要がある場合は、使用したい機能が両形状でサポートされていることを確認してください。

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

1. OpenAI モデル名を直接指定しています。  
2. [`Model`][agents.models.interface.Model] 実装を提供しています。  

エージェントで使用するモデルをさらに細かく設定したい場合は、`temperature` などを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡してください。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使う場合は、`user` や `service_tier` などの[その他のオプション](https://platform.openai.com/docs/api-reference/responses/create)も利用できます。トップレベルで指定できない場合は、`extra_args` で渡してください。

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

### トレーシングクライアントでの 401 エラー

トレーシングに関連するエラーが出る場合、トレースが OpenAI サーバーにアップロードされる設計のため、OpenAI API キーがないことが原因です。解決策は次の 3 通りです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. OpenAI 以外のトレーシングプロセッサーを使用する。詳しくは [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだ対応していません。そのため 404 などのエラーが発生する場合があります。対処方法は以下の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。  
   これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力には対応していても `json_schema` を指定できないために起こります。現在修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーを選ぶことを推奨します。そうしないと、不正な JSON が返りアプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの混在

モデルプロバイダーごとの機能差に注意しないと、エラーに遭遇する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型 file search や web search をサポートしていますが、多くの他プロバイダーはこれらをサポートしていません。以下の制限事項にご注意ください。

- サポートしていない `tools` を理解しないプロバイダーに送らない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力を除外する  
- structured JSON 出力をサポートしていないプロバイダーでは、無効な JSON が返ることがあります