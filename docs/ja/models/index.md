---
search:
  exclude: true
---
# モデル

 Agents SDK は、 OpenAI モデルに対してすぐに使える 2 つのオプションを提供しています:

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。これは新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。これは [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## OpenAI 以外のモデル

 ほとんどの他社 LLM を [LiteLLM integration](./litellm.md) 経由で利用できます。まず、 litellm 依存関係グループをインストールしてください:

```bash
pip install "openai-agents[litellm]"
```

 その後、 `litellm/` プレフィックスを付けて [supported models](https://docs.litellm.ai/docs/providers) を指定します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 以外のモデルを使用する他の方法

 他社 LLM プロバイダーを統合する方法はさらに 3 つあります（コード例は [here](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) を参照）:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、 `AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。 LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、 `base_url` と `api_key` を設定できるケース向けです。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。  
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで利用できます。これにより、「この実行内のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。  
3. [`Agent.model`][agents.agent.Agent.model] により特定の エージェント インスタンスに対してモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどの利用可能なモデルを簡単に使う方法として、 [LiteLLM integration](./litellm.md) が便利です。

 `platform.openai.com` からの API キーをお持ちでない場合は、 `set_tracing_disabled()` でトレーシングを無効化するか、 [different tracing processor](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では Chat Completions API/モデルを使用しています。これは、多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もしご利用の LLM プロバイダーが Responses API をサポートしている場合は、 Responses の使用を推奨します。

## モデルの組み合わせ

 1 つのワークフロー内でエージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小型で高速なモデルを使用し、複雑なタスクには大型で高性能なモデルを使うといった形です。 [`Agent`][agents.Agent] を設定するとき、次のいずれかの方法で特定のモデルを指定できます:

1. モデル名を直接渡す。  
2. 任意のモデル名 + その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note

    SDK では [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、各ワークフローで 1 つのモデル形状を使用することを推奨します。両モデル形状はサポートする機能やツールが異なるためです。ワークフローで両形状を混在させる場合は、使用するすべての機能が両方で利用可能か確認してください。

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

 エージェントで使用するモデルをさらに設定したい場合は、温度などのオプション設定パラメーターを含む [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

 また、 OpenAI の Responses API を使用する場合、 `user`、 `service_tier` など [その他のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで指定できない場合は、 `extra_args` に渡すこともできます。

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

## 他社 LLM 利用時によくある問題

### Tracing client error 401

 トレーシングに関連するエラーが発生する場合、これはトレースが OpenAI サーバーへアップロードされる際に OpenAI API キーがないためです。次のいずれかで解決できます:

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、 [platform.openai.com](https://platform.openai.com/) のものが必要です。  
3. OpenAI 以外のトレースプロセッサーを使用する。詳細は [tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

 SDK はデフォルトで Responses API を使用しますが、多くの他社 LLM プロバイダーはまだサポートしていません。その結果、 404 エラーなどが発生することがあります。解決策は次の 2 つです:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は [here](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

 一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

 これは一部プロバイダーの制限で、 JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないというものです。現在修正に取り組んでいますが、 JSON schema 出力をサポートするプロバイダーを利用することを推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデル混在

 モデルプロバイダー間の機能差異を理解しておかないと、エラーが発生する場合があります。たとえば、 OpenAI は structured outputs、マルチモーダル入力、ホストされた file search や web search をサポートしていますが、多くの他社プロバイダーはこれらをサポートしていません。以下の制限に注意してください:

- サポートされていない `tools` を理解しないプロバイダーに送らない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力をフィルタリングする  
- structured JSON outputs をサポートしないプロバイダーでは、無効な JSON が返される可能性があることを理解する