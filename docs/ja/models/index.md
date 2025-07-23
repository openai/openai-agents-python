---
search:
  exclude: true
---
# モデル

Agents SDK には OpenAI モデルの 2 種類が標準でサポートされています:

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい Responses API を用いて OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、Chat Completions API を用いて OpenAI API を呼び出します。

## Non-OpenAI モデル

ほとんどの Non-OpenAI モデルは [LiteLLM インテグレーション](./litellm.md) 経由で利用できます。まず、litellm の依存グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### Non-OpenAI モデルを利用するその他の方法

他の LLM プロバイダーは、次の 3 つの方法でも統合できます (コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)):

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに利用したい場合に便利です。LLM プロバイダーが OpenAI 互換エンドポイントを持つ場合、`base_url` と `api_key` を設定できます。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   これは `Runner.run` レベルで、「この実行ではすべてのエージェントにカスタムモデルプロバイダーを使う」と指定できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) をご覧ください。
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) をご覧ください。多くのモデルを簡単に使う方法として [LiteLLM インテグレーション](./litellm.md) があります。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API/モデルを使用しています。もしご利用の LLM プロバイダーが Responses API をサポートしている場合は、Responses を使用することを推奨します。

## モデルの組み合わせ

1 つのワークフロー内でエージェントごとに異なるモデルを使いたい場合があります。たとえば、簡単な振り分けには小さく高速なモデルを、複雑なタスクには大きく高性能なモデルを使うといったパターンです。[`Agent`][agents.Agent] を設定する際には、次のいずれかでモデルを指定できます:

1. モデル名を直接渡す  
2. 任意のモデル名 + その名前を Model インスタンスにマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、ワークフローごとに 1 種類のモデル形状を使うことを推奨します。両者は対応する機能や tools が異なるためです。もし混在が必要な場合は、利用する全機能が両方でサポートされているか確認してください。

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

エージェントで使用するモデルをさらに設定したい場合は、`temperature` などのオプションを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使う場合は [追加のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) (例: `user`, `service_tier` など) も利用できます。トップレベルにない場合は `extra_args` から渡してください。

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

### トレーシング クライアントのエラー 401

トレースは OpenAI サーバーへアップロードされるため、OpenAI API キーがない場合にエラーが発生します。以下のいずれかで解決できます:

1. トレーシングを完全に無効化: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用の OpenAI キーを設定: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   このキーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。  
3. OpenAI 以外のトレースプロセッサーを使用: [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの LLM プロバイダーはまだ対応していません。そのため 404 などのエラーが発生する場合があります。以下のいずれかで解決してください:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   これは環境変数 `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に有効です。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーになることがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは、JSON 出力には対応しているものの、出力に使用する `json_schema` を指定できないプロバイダーの制限です。修正に取り組んでいますが、JSON スキーマ出力に対応しているプロバイダーを利用することを推奨します。そうでない場合、生成される JSON が不正でアプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの組み合わせ

モデルプロバイダー間の機能差に注意しないとエラーが発生することがあります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホストされたファイル検索と Web 検索をサポートしますが、多くの他プロバイダーは対応していません。以下の制限に注意してください:

- 対応していないプロバイダーにはサポート外の `tools` を送らない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力を除外する  
- structured JSON 出力をサポートしないプロバイダーは無効な JSON を返す場合がある点を認識する