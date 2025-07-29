---
search:
  exclude: true
---
# モデル

Agents SDK には、すぐに使える 2 種類の OpenAI モデルサポートが含まれています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]  
    新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]  
    [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## Non-OpenAI モデル

ほとんどの Non-OpenAI モデルは [LiteLLM integration](./litellm.md) 経由で利用できます。まず、litellm の依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

その後、`litellm/` プレフィックスを付けて [supported models](https://docs.litellm.ai/docs/providers) のいずれかを使います。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### Non-OpenAI モデルを使用するその他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に code examples があります）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   グローバルに `AsyncOpenAI` インスタンスを LLM クライアントとして使用したい場合に便利です。LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使用します。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで指定します。これにより、「この実行中のすべての エージェント でカスタムモデルプロバイダーを使用する」と宣言できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model]  
   個々の Agent インスタンスでモデルを指定できます。異なる エージェント に対して異なるプロバイダーを組み合わせて使用できます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどのモデルを簡単に使う方法として [LiteLLM integration](./litellm.md) があります。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別の tracing processor](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API／モデルを使用しています。ご利用の LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で エージェント ごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小さく高速なモデルを、複雑なタスクにはより大きく高性能なモデルを使用するなどです。[`Agent`][agents.Agent] を設定する際には、以下のいずれかでモデルを選択できます。

1. モデル名を直接渡す。  
2. 任意のモデル名 + それをモデルインスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。  

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、各ワークフローでは 1 つのモデル形状のみを使用することを推奨します。2 つの形状はサポートする機能やツールが異なるためです。ワークフローでモデル形状を混在させる場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使用するモデルをさらに詳細に設定したい場合は、温度などのオプション設定パラメーターを持つ [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、`user` や `service_tier` など [いくつかの追加オプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで指定できない場合は、`extra_args` で渡すことができます。

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

## 他の LLM プロバイダー使用時の一般的な問題

### Tracing クライアントエラー 401

トレーシング関連のエラーが発生する場合、これはトレースが OpenAI サーバーにアップロードされるためで、OpenAI API キーがないことが原因です。解決策は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。  
3. OpenAI 以外の trace processor を使用する。詳細は [tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他の LLM プロバイダーはまだ対応していません。このため 404 などのエラーが発生する場合があります。解決策は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。  
   これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。  
   例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。  

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その結果、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のプロバイダーの制限で、JSON 出力には対応していても `json_schema` を指定できないためです。現在修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを利用することを推奨します。さもないと、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの混在

モデルプロバイダーごとの機能差異を理解しておかないと、エラーに遭遇する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホストされた file search と Web 検索をサポートしていますが、多くの他プロバイダーはこれらをサポートしていません。以下の制限に注意してください。

-   対応していない `tools` を理解しないプロバイダーに送らない  
-   テキストのみのモデルを呼ぶ前にマルチモーダル入力を除外する  
-   structured JSON outputs をサポートしないプロバイダーでは、無効な JSON が生成されることがあることを理解する