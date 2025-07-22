---
search:
  exclude: true
---
# モデル

Agents SDK には、すぐに使える 2 種類の OpenAI モデル対応が含まれています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] — 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] — [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非  OpenAI  モデルは [LiteLLM インテグレーション](./litellm.md) 経由で利用できます。まず、litellm 依存グループをインストールしてください。

```bash
pip install "openai-agents[litellm]"
```

その後、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を指定します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを利用するその他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に code examples あり）。

1. [`set_default_openai_client`][agents.set_default_openai_client] — `AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。LLM プロバイダーが OpenAI 互換 API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。  
2. [`ModelProvider`][agents.models.interface.ModelProvider] — `Runner.run` レベルで指定できます。「この run 内のすべての エージェント ではカスタムモデルプロバイダーを使う」といった指定が可能です。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) をご覧ください。  
3. [`Agent.model`][agents.agent.Agent.model] — 特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを混在させることができます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) をご覧ください。ほとんどのモデルを簡単に利用するには [LiteLLM インテグレーション](./litellm.md) が便利です。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの例では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もしお使いの LLM プロバイダーが Responses API をサポートしている場合は、Responses を使用することを推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージにはより小さく高速なモデルを使い、複雑なタスクにはより大型で高性能なモデルを使うといったケースです。[`Agent`][agents.Agent] を設定するとき、以下のいずれかの方法で特定のモデルを選択できます。

1. モデル名を直接渡す。  
2. どのモデル名でも渡し、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしますが、ワークフローごとに 1 つのモデル形に統一することを推奨します。2 つの形では利用できる機能や tools が異なるためです。モデル形を混在させる場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使用するモデルをさらに細かく設定したい場合は、`temperature` などのオプションパラメーターを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合は [その他のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create)（`user`、`service_tier` など）もあります。トップレベルで指定できない場合は、`extra_args` を利用して渡すことができます。

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

### トレーシング クライアント 401 エラー

トレーシング関連のエラーが発生する場合、トレースが OpenAI サーバーにアップロードされる仕組みであり、OpenAI API キーがないことが原因です。解決策は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key] — この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. 非 OpenAI のトレース プロセッサーを使用する。詳細は [tracing docs](../tracing.md#custom-tracing-processors) を参照してください。  

### Responses API サポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他社 LLM プロバイダーはまだ対応していません。その結果、404 エラーなどが発生することがあります。対処方法は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数 `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。code examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。  

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力には対応しているものの、出力に使用する `json_schema` を指定できません。現在修正に取り組んでいますが、JSON schema 出力をサポートするプロバイダーを利用することを推奨します。そうでない場合、壊れた JSON が返され、アプリが頻繁に失敗する可能性があります。

## プロバイダー間でのモデルの組み合わせ

モデルプロバイダーごとの機能差を理解しておかないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の file search と web search をサポートしていますが、多くの他社プロバイダーはこれらをサポートしていません。以下の制限に注意してください。

- 未対応の `tools` を理解しないプロバイダーへ送らない  
- テキストのみのモデルに呼び出す前にマルチモーダル入力をフィルタリングする  
- structured JSON outputs をサポートしないプロバイダーでは、無効な JSON が返る場合があることを理解する