---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに利用できる 2 つの方式が用意されています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

多くの非 OpenAI モデルは [LiteLLM 連携](./litellm.md)を通じて利用できます。まず、litellm の依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて任意の [対応モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使用するその他の方法

他の LLM プロバイダーは、以下の 3 つの方法でも統合できます（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にコード例があります）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   グローバルに `AsyncOpenAI` インスタンスを LLM クライアントとして使用したい場合に便利です。API エンドポイントが OpenAI 互換で、`base_url` と `api_key` を設定できるプロバイダー向けです。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで使用します。この方法では、「この実行の全エージェントに対してカスタムモデルプロバイダーを使用する」と指定できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使用できます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどのモデルを簡単に利用するには [LiteLLM 連携](./litellm.md) を使うのが便利です。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note
    これらの例では Chat Completions API／モデルを使用しています。これは、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないためです。もしお使いの LLM プロバイダーが Responses API をサポートしている場合は、Responses の利用を推奨します。

## モデルの組み合わせ

ひとつのワークフロー内で、エージェントごとに異なるモデルを使いたいことがあります。たとえば、トリアージには小さく高速なモデルを使用し、複雑なタスクには大規模で高性能なモデルを使用する、といったケースです。[`Agent`][agents.Agent] を設定する際、以下のいずれかでモデルを選択できます。

1. モデル名を直接渡す。  
2. 任意のモデル名と、それを [`ModelProvider`][agents.models.interface.ModelProvider] が Model インスタンスへマッピングできるようにする。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、ワークフローごとにモデル形状を 1 つに統一することを推奨します。両モデル形状は対応機能やツールが異なるためです。もし混在させる場合は、利用する機能が両方で利用可能か必ず確認してください。

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
2. [`Model`][agents.models.interface.Model] の実装を提供しています。  

エージェントで使用するモデルをさらに設定したい場合は、`temperature` などのオプションパラメーターを含む [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合は、`user` や `service_tier` など [他にもオプションのパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで指定できない場合は、`extra_args` を利用して渡してください。

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

### Tracing クライアントエラー 401

トレースは OpenAI サーバーへアップロードされるため、OpenAI API キーがない場合にエラーになることがあります。以下のいずれかで解決できます。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. 非 OpenAI トレースプロセッサーを使用する。詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの LLM プロバイダーはまだ対応していません。そのため 404 エラーなどが発生することがあります。次のいずれかで解決できます。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数 `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に有効です。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。  

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力には対応していても `json_schema` を指定できないために起こります。現在修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを利用することを推奨します。サポートがない場合、JSON が不正形式になりアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデル混在

モデルプロバイダーごとの機能差に注意しないと、エラーになる場合があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型ファイル検索や Web 検索をサポートしていますが、多くのプロバイダーはこれらをサポートしていません。以下の点に注意してください。

- 対応していない `tools` を理解しないプロバイダーには送らない  
- テキスト専用モデルを呼び出す前にマルチモーダル入力を除去する  
- structured JSON outputs をサポートしないプロバイダーでは、無効な JSON が返されることがありますので注意する  