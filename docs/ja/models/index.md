---
search:
  exclude: true
---
# モデル

 Agents SDK には、OpenAI モデルに対するサポートが 2 つの形態で最初から組み込まれています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を用いて OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を用いて OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM 連携](./litellm.md) を通じて利用できます。まずは litellm 依存グループをインストールしてください:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、[サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使用するその他の方法

他の LLM プロバイダーは、さらに 3 通りの方法で統合できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）:

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを持つ場合に、`base_url` と `api_key` を設定して利用できます。[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) に設定例があります。  
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで使用します。「この実行内のすべてのエージェントでカスタムのモデルプロバイダーを使う」と宣言できます。[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) に設定例があります。  
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の `Agent` インスタンスでモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使用できます。[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) に設定例があります。多くのモデルを簡単に使う方法としては、[LiteLLM 連携](./litellm.md) が便利です。  

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note
    これらの例では Chat Completions API / モデルを使用しています。ほとんどの LLM プロバイダーがまだ Responses API に対応していないためです。もし利用中の LLM プロバイダーが Responses API をサポートしている場合は、Responses を使用することを推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、軽量で高速なモデルでトリアージを行い、より大きく高性能なモデルで複雑なタスクを処理するといった使い分けです。[`Agent`][agents.Agent] を設定する際には、以下の方法でモデルを選択できます。

1. モデル名を直接渡す  
2. モデル名と、それを `Model` インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す  

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、ワークフローごとに 1 つのモデル形状を使用することを推奨します。両モデル形状は利用できる機能やツールが異なるためです。ワークフローで両方を混在させる場合は、使用するすべての機能が両モデルで利用可能かを確認してください。

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

エージェントに使用するモデルをさらに細かく設定したい場合は、`temperature` などのオプション設定を持つ [`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する際には、`user` や `service_tier` などの[追加パラメーター](https://platform.openai.com/docs/api-reference/responses/create) を指定できます。トップレベルで利用できない場合は、`extra_args` に渡してください。

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

## 他の LLM プロバイダー利用時の一般的な問題

### Tracing client error 401

トレーシング関連のエラーが発生する場合、これはトレースが OpenAI サーバーへアップロードされるため、OpenAI の API キーがないことが原因です。以下のいずれかで解決できます。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロード専用で、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. OpenAI 以外のトレースプロセッサーを使用する。詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他社 LLM プロバイダーはまだ対応していません。このため 404 エラーなどが発生する場合があります。以下のいずれかを試してください。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にコード例があります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。この場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力には対応しているものの、出力に使用する `json_schema` を指定できません。現在修正に取り組んでいますが、JSON Schema 出力をサポートするプロバイダーを利用することを推奨します。そうしないと、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダーを跨いだモデルの組み合わせ

モデルプロバイダー間の機能差を理解しておかないと、エラーを招く可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホストされた file search / web search をサポートしていますが、多くの他社プロバイダーはこれらをサポートしていません。以下の制限に注意してください。

- 対応していないプロバイダーに `tools` を送らない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力を除去する  
- structured JSON outputs をサポートしないプロバイダーでは、無効な JSON が返る場合があることを念頭に置く  