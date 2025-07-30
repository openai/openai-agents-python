---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに利用できる 2 つのバリエーションが用意されています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] — 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] — [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの OpenAI 以外のモデルは、[LiteLLM integration](../litellm.md) 経由で利用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

その後、`litellm/` プレフィックスを付けて、[サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 以外のモデルを利用するその他の方法

他の LLM プロバイダーを統合する方法はあと 3 つあります（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに利用したい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを提供しているときに、`base_url` と `api_key` を設定できます。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   これは `Runner.run` レベルで使用します。「この実行ではすべてのエージェントにカスタムのモデルプロバイダーを使う」と指定できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model]  
   個々の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて利用できます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどのモデルを簡単に使う方法として [LiteLLM integration](../litellm.md) があります。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` を使ってトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では Chat Completions API／モデルを使用しています。理由は、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないためです。もしご利用の LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクには大型で高性能なモデルを使うといったケースです。[`Agent`][agents.Agent] を設定するとき、以下のいずれかの方法でモデルを指定できます。

1. モデル名を直接指定する。  
2. 任意のモデル名と、それを `Model` インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] の実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、ワークフローごとに 1 つのモデル形状を使用することを推奨します。2 つの形状では利用できる機能やツールが異なるためです。もしワークフローで混在させる場合は、利用する機能が双方でサポートされていることを確認してください。

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

1. OpenAI のモデル名を直接設定しています。  
2. [`Model`][agents.models.interface.Model] の実装を渡しています。  

エージェントで使用するモデルをさらに細かく設定したい場合は、`temperature` などのオプションを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する際には、`user` や `service_tier` など[いくつかの追加パラメーター](https://platform.openai.com/docs/api-reference/responses/create)があります。トップレベルにない場合でも、`extra_args` を使って渡すことが可能です。

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

## 他社 LLM プロバイダー利用時によくある問題

### Tracing client error 401

トレーシング関連のエラーが出る場合、トレースが OpenAI のサーバーへアップロードされるのに OpenAI API キーがないことが原因です。次のいずれかで解決できます。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. OpenAI 以外のトレースプロセッサーを使用する。詳しくは [tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの LLM プロバイダーはまだ対応していません。その結果として 404 などのエラーが発生することがあります。対応策は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### Structured outputs のサポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力には対応しているものの、出力に使用する `json_schema` を指定できません。現在修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを使用することを推奨します。そうでない場合、JSON が不正形式になるたびにアプリが壊れる可能性があります。

## プロバイダーをまたいだモデルの組み合わせ

モデルプロバイダー間の機能差を理解していないと、エラーが発生することがあります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型 file search や web search をサポートしていますが、多くの他社プロバイダーはこれらの機能をサポートしていません。以下の制限に注意してください。

- 対応していない `tools` を理解しないプロバイダーに送らない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力を除外する  
- structured JSON outputs をサポートしないプロバイダーでは、不正な JSON が返ることがある点に注意する