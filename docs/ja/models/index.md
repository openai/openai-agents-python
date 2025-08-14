---
search:
  exclude: true
---
# モデル

Agents SDK には、 OpenAI モデルをすぐに利用できる 2 つの方式が用意されています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。  
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## OpenAI 以外のモデル

ほとんどの OpenAI 以外のモデルは、 [LiteLLM 連携](./litellm.md) を介して利用できます。まずは litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

その後、 `litellm/` プレフィックスを付けて、 [対応モデル](https://docs.litellm.ai/docs/providers) を利用できます。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 以外のモデルを使うその他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) を参照）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。 LLM プロバイダーが OpenAI 互換エンドポイントを持っており、 `base_url` と `api_key` を設定できるケースで使用します。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。  
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで指定します。「この実行内のすべての エージェント でカスタムモデルプロバイダーを使う」と宣言できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。  
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の Agent インスタンスに対してモデルを指定します。これにより、 エージェント ごとに異なるプロバイダーを組み合わせることができます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどの既存モデルを簡単に使う方法としては、 LiteLLM 連携が便利です。

`platform.openai.com` の API キーをお持ちでない場合は、 `set_tracing_disabled()` でトレーシングを無効にするか、 [別のトレーシング プロセッサー](../tracing.md) を設定することを推奨します。

!!! note
    これらの例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、 Chat Completions API／モデルを使用しています。もしお使いの LLM プロバイダーが Responses API をサポートしている場合は、 Responses を利用することを推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、 エージェント ごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小規模で高速なモデルを、複雑なタスクには大規模で高性能なモデルを使う、といったケースです。 [`Agent`][agents.Agent] を設定する際、次のいずれかでモデルを選択できます。

1. モデル名を直接渡す。  
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、ワークフローごとに 1 つのモデル形状を使うことを推奨します。両モデル形状は対応する機能・ツールが異なるため、混在させる場合は使用する機能が両方で利用可能か確認してください。

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

エージェントで使用するモデルをさらに詳細に設定したい場合は、 `temperature` などのオプションパラメーターを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、 OpenAI の Responses API を使用する場合、 `user` や `service_tier` など [いくつかの追加オプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで指定できない場合は、 `extra_args` を使って渡してください。

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

## 他の LLM プロバイダー利用時によくある問題

### Tracing client error 401

トレーシングに関するエラーが発生する場合、トレースが OpenAI サーバーにアップロードされるため、 OpenAI API キーがないことが原因です。解決策は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、 [platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. 非 OpenAI のトレース プロセッサーを使用する。詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの LLM プロバイダーはまだ対応していません。そのため 404 エラーなどが発生する場合があります。解決策は 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。  
   これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダーは、 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、 JSON 出力はサポートしているものの、出力に使用する `json_schema` を指定できないためです。現在修正に取り組んでいますが、 JSON スキーマ出力をサポートしているプロバイダーを利用することを推奨します。そうでない場合、 JSON が不正な形式で返され、アプリが頻繁に壊れる可能性があります。

## プロバイダーをまたぐモデルの混在

モデル プロバイダー間の機能差に注意しないと、エラーが発生する場合があります。たとえば、 OpenAI は structured outputs、マルチモーダル入力、ホストされたファイル検索・ Web 検索をサポートしていますが、多くの他社プロバイダーはこれらをサポートしていません。次の点に注意してください。

-   対応していないプロバイダーに `tools` を送らない  
-   テキスト専用モデルを呼び出す前にマルチモーダル入力を除外する  
-   structured JSON 出力をサポートしないプロバイダーでは、無効な JSON が返ることがあります