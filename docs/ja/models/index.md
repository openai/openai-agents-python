---
search:
  exclude: true
---
# モデル

Agents SDK には、すぐに使える OpenAI モデルが 2 種類用意されています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## OpenAI 以外のモデル

ほとんどの OpenAI 以外のモデルは [LiteLLM 連携](./litellm.md) を通じて利用できます。まず、litellm の依存関係グループをインストールしてください。

```bash
pip install "openai-agents[litellm]"
```

その後、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 以外のモデルの利用方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に code examples があります）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに利用したい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケースに適しています。詳細は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。  
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで設定します。これにより、「この実行中のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。詳細は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) をご覧ください。  
3. [`Agent.model`][agents.agent.Agent.model] を使うと、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使用できます。詳細は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) をご覧ください。ほとんどのモデルを簡単に利用する方法としては [LiteLLM 連携](./litellm.md) が便利です。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note
    これらの例では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もし LLM プロバイダーが Responses API をサポートしている場合は、Responses を使用することを推奨します。

## モデルの組み合わせ

一つのワークフロー内でエージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型かつ高速なモデルを、複雑なタスクには大型で高性能なモデルを使用する、といった具合です。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選択できます。

1. モデル名を直接渡す  
2. 任意のモデル名と、それを Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す  

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしますが、ワークフローごとに単一のモデル形状を使用することを推奨します。両形状はサポートする機能や tools が異なるためです。もし両形状を混在させる必要がある場合は、使用するすべての機能が両方で利用可能かを確認してください。

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

エージェントで使用するモデルをさらに設定したい場合は、`temperature` などのオプションを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合は、`user` や `service_tier` など [他にもいくつかのオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルにない場合は、`extra_args` を利用して渡せます。

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

## 他社 LLM プロバイダー使用時の一般的な問題

### Tracing クライアントエラー 401

Tracing 関連のエラーが発生する場合、トレースが OpenAI サーバーへアップロードされる際に OpenAI API キーがないことが原因です。解決策は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   ※ この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。  
3. OpenAI 以外のトレースプロセッサーを使用する。詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。  

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの LLM プロバイダーはまだ対応していません。そのため 404 エラーなどが発生することがあります。対処方法は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   （環境変数 `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に有効）  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。  

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。次のようなエラーが発生する場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力には対応していても `json_schema` を指定できないためです。現在修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを利用することを推奨します。そうしないと、JSON が不正な形式で返されアプリが壊れることがあります。

## プロバイダー間でのモデル混在

モデルプロバイダーごとの機能差に注意しないと、エラーが発生する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホストされた file search・web search をサポートしていますが、多くの他社プロバイダーはこれらをサポートしていません。以下の制限に注意してください。

-   サポートしていない `tools` を理解しないプロバイダーへ送らない  
-   テキストのみ対応のモデルを呼び出す前にマルチモーダル入力を除外する  
-   structured JSON outputs に対応していないプロバイダーでは、不正な JSON が返ることがある点に注意する  