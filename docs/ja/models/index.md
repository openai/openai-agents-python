---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに利用できる 2 種類のサポートが用意されています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] － 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] － [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM 連携](./litellm.md) を通じて利用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

その後、`litellm/` プレフィックスを付けて、[サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを利用するその他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります（コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` インスタンスを LLM クライアントとして全体で利用したい場合に便利です。OpenAI 互換の API エンドポイントを持つ LLM プロバイダーを使うケースで、`base_url` と `api_key` を設定できます。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。  
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで指定します。「この実行中のすべてのエージェントでカスタムモデルプロバイダーを使用する」と宣言できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。  
3. [`Agent.model`][agents.agent.Agent.model]  
   特定のエージェントインスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせることができます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどのモデルを簡単に使うには [LiteLLM 連携](./litellm.md) が便利です。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では Chat Completions API／モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もし利用する LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、振り分けには小さく高速なモデルを使い、複雑なタスクにはより大きく高性能なモデルを使うといったケースです。[`Agent`][agents.Agent] を設定する際、以下のいずれかでモデルを選択できます。

1. モデル名を直接渡す  
2. 任意のモデル名と、それを Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す  

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、ワークフローごとに 1 つのモデル形状を使用することを推奨します。2 つの形状は対応機能や tools が異なるためです。もし混在させる必要がある場合は、使用する機能が両形状で利用可能か確認してください。

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

エージェントが使用するモデルをさらに詳細に設定したい場合は、`temperature` などのオプションを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、`user` や `service_tier` など[追加のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create)があります。トップレベルに用意されていない場合は、`extra_args` を使って渡すことも可能です。

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

### Tracing クライアントのエラー 401

トレーシング関連のエラーが発生する場合、トレースは OpenAI サーバーへアップロードされるため、OpenAI API キーがないことが原因です。対処法は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. OpenAI 以外のトレースプロセッサーを利用する。詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの LLM プロバイダーはまだ対応していません。そのため 404 エラーなどが発生することがあります。対処法は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に有効です。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制限によるものです。JSON 出力自体はサポートしていても、出力に使用する `json_schema` を指定できません。当社では修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを利用することを推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダーを跨いだモデルの混在

モデルプロバイダー間の機能差を理解しておかないと、エラーに遭遇することがあります。たとえば、OpenAI は structured outputs、マルチモーダル入力、OpenAI がホストするファイル検索や Web 検索をサポートしていますが、多くの他プロバイダーは未対応です。以下の制限に注意してください。

- 対応していないプロバイダーに `tools` を送らない  
- テキスト専用モデルを呼び出す前にマルチモーダル入力をフィルターする  
- structured JSON 出力をサポートしないプロバイダーでは、無効な JSON が返ることがある点に注意する  