---
search:
  exclude: true
---
# モデル

 Agents SDK には、 OpenAI モデルをすぐに利用できる 2 種類のサポートが用意されています:

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## Non-OpenAI モデル

 ほとんどの Non-OpenAI モデルは [LiteLLM 連携](./litellm.md) を通じて利用できます。まず、 litellm 依存グループをインストールしてください:

```bash
pip install "openai-agents[litellm]"
```

 その後、 `litellm/` プレフィックスを付けて、 [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### Non-OpenAI モデルを使用するその他の方法

 他の LLM プロバイダーを統合する方法はさらに 3 つあります ( [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) のコード例を参照):

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   OpenAI 互換の API エンドポイントを持つ LLM プロバイダーで、 `base_url` と `api_key` を設定できる場合に、 `AsyncOpenAI` インスタンスをアプリ全体で使用する際に便利です。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。  
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで使用します。「この run 内のすべてのエージェントでカスタムモデルプロバイダーを使う」と宣言できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。  
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使えます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) をご覧ください。ほとんどのモデルを簡単に利用する方法として [LiteLLM 連携](./litellm.md) があります。

 `platform.openai.com` の API キーをお持ちでない場合は、 `set_tracing_disabled()` で tracing を無効化するか、 [別の tracing プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの例では、ほとんどの LLM プロバイダーが Responses API をまだサポートしていないため、 Chat Completions API/モデルを使用しています。ご利用の LLM プロバイダーが Responses API をサポートしている場合は、 Responses の利用を推奨します。

## モデルの組み合わせ

 1 つのワークフロー内でエージェントごとに異なるモデルを使用したい場合があります。たとえば、振り分け (triage) には小型で高速なモデルを、複雑なタスクには大型で高性能なモデルを使用するといった具合です。 [`Agent`][agents.Agent] を設定する際、以下のいずれかでモデルを選択できます:

1. モデル名を直接指定する。  
2. 任意のモデル名と、それをモデルインスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note

    SDK では [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、ワークフローごとに 1 種類のモデル形状に統一することを推奨します。両モデル形状は利用できる機能や tools が異なるためです。どうしても混在させる場合は、使用する機能が両形状で利用可能かを必ず確認してください。

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

1.  OpenAI モデル名を直接設定します。  
2.  [`Model`][agents.models.interface.Model] 実装を提供します。  

 エージェントで使用するモデルをさらに設定したい場合は、 [`ModelSettings`][agents.models.interface.ModelSettings] を渡して `temperature` などのオプションパラメーターを指定できます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

 OpenAI の Responses API を使用する場合には、 `user` や `service_tier` など [追加のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで指定できない場合は、 `extra_args` で渡してください。

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

### Tracing クライアントの 401 エラー

 tracing 関連のエラーが発生する場合、トレースが OpenAI サーバーにアップロードされるのに OpenAI API キーを持っていないことが原因です。解決策は次の 3 つです:

1. tracing を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. tracing 用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、 [platform.openai.com](https://platform.openai.com/) から取得したものである必要があります。  
3. 非 OpenAI の trace プロセッサーを使用する。詳しくは [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

 SDK はデフォルトで Responses API を使用しますが、ほとんどの LLM プロバイダーはまだ対応していません。その結果、 404 などのエラーが発生することがあります。解決策は次の 2 つです:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。  
   これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

 一部のモデルプロバイダーは、 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。この場合、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

 これは一部プロバイダーの制限で、 JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないためです。現在この問題の解決に取り組んでいますが、 JSON schema 出力をサポートするプロバイダーを利用することを推奨します。そうでない場合、 JSON が不正な形式で返され、アプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの混在

 モデルプロバイダーごとの機能差を理解していないと、エラーに遭遇することがあります。たとえば、 OpenAI は structured outputs、マルチモーダル入力、ホスト型 file search や web search をサポートしていますが、多くの他社プロバイダーはこれらをサポートしていません。以下の制限に注意してください:

- サポートしていない `tools` を理解できないプロバイダーに送信しない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力を除外する  
- structured JSON outputs をサポートしないプロバイダーでは、無効な JSON が返されることがある点を理解する  