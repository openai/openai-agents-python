---
search:
  exclude: true
---
# モデル

Agents SDK には、標準で 2 種類の OpenAI モデルサポートが含まれています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] — 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を利用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] — [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を利用して OpenAI API を呼び出します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、振り分けには小さく高速なモデルを、複雑なタスクには大きく高性能なモデルを使う、といった使い分けです。[`Agent`][agents.Agent] を設定する際は、以下のいずれかで特定のモデルを指定できます。

1. OpenAI モデル名を直接渡す  
2. 任意のモデル名と、それを `Model` インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す  

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形に対応していますが、ワークフローごとに 1 つのモデル形を使用することを推奨します。2 つの形ではサポートする機能・ツールが異なるためです。どうしても混在させる場合は、利用するすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI モデル名を直接指定  
2. [`Model`][agents.models.interface.Model] 実装を提供  

エージェントで使用するモデルをさらに細かく設定したい場合は、`temperature` などのオプションを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

## 他の LLM プロバイダーの利用

他の LLM プロバイダーは 3 通りの方法で利用できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   OpenAI 互換の API エンドポイントを持つ場合に、`AsyncOpenAI` インスタンスをグローバルに LLM クライアントとして設定できます。`base_url` と `api_key` を設定するケースです。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。  

2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで「この実行中のすべてのエージェントにカスタムモデルプロバイダーを使う」と宣言できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。  

3. [`Agent.model`][agents.agent.Agent.model]  
   特定の Agent インスタンスにモデルを指定できます。エージェントごとに異なるプロバイダーを組み合わせられます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。多くのモデルを簡単に使う方法として [LiteLLM 連携](./litellm.md) があります。  

`platform.openai.com` の API キーを持たない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note
    これらの例では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もしプロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## 他の LLM プロバイダーでよくある問題

### Tracing クライアントの 401 エラー

トレースは OpenAI サーバーへアップロードされるため、OpenAI API キーがない場合にエラーになります。解決策は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   このキーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。  
3. OpenAI 以外のトレースプロセッサーを使う。詳しくは [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。  

### Responses API サポート

SDK は既定で Responses API を使用しますが、多くの LLM プロバイダーはまだ対応していません。そのため 404 などのエラーが発生する場合があります。対処方法は 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   環境変数 `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。  

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```
BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}
```

これは一部プロバイダーの制限で、JSON 出力はサポートしていても `json_schema` を指定できません。現在修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーを利用することを推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。