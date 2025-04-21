# モデル

Agents SDK には、OpenAI モデルの 2 種類のサポートが標準で用意されています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## モデルの組み合わせ

1 つのワークフロー内で、各エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクにはより大きく高性能なモデルを使うことができます。[`Agent`][agents.Agent] を設定する際、以下のいずれかの方法で特定のモデルを選択できます。

1. OpenAI モデル名を直接渡す。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接指定する。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、各ワークフローで 1 つのモデル形状のみを使うことを推奨します。なぜなら、2 つの形状はサポートする機能やツールが異なるためです。ワークフローでモデル形状を組み合わせて使う場合は、利用するすべての機能が両方で利用可能かご確認ください。

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

1.  OpenAI モデル名を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を指定します。

エージェントで使用するモデルをさらに細かく設定したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。これにより、temperature などのオプションのモデル設定パラメーターを指定できます。

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

他の LLM プロバイダーは、3 つの方法で利用できます（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に code examples があります）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに利用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使います。[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) に設定例があります。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで利用します。これにより、「この実行のすべてのエージェントでカスタムモデルプロバイダーを使う」と指定できます。[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) に設定例があります。
3. [`Agent.model`][agents.agent.Agent.model] で、特定のエージェントインスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使うことができます。[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) に設定例があります。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](tracing.md) を設定することを推奨します。

!!! note

    これらの code examples では Chat Completions API/モデルを使っています。なぜなら、ほとんどの LLM プロバイダーはまだ Responses API をサポートしていないためです。もし LLM プロバイダーが Responses API をサポートしている場合は、Responses の利用を推奨します。

## 他の LLM プロバイダー利用時のよくある問題

### Tracing クライアントの 401 エラー

トレーシングに関連するエラーが発生した場合、これはトレースが OpenAI サーバーにアップロードされるため、OpenAI API キーがないことが原因です。解決方法は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使われ、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. OpenAI 以外のトレースプロセッサーを使う。[トレーシングのドキュメント](tracing.md#custom-tracing-processors) をご覧ください。

### Responses API サポート

SDK はデフォルトで Responses API を使いますが、ほとんどの他の LLM プロバイダーはまだ対応していません。そのため、404 エラーなどが発生する場合があります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に有効です。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使います。[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に code examples があります。

### structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```
BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}
```

これは一部のモデルプロバイダーの制限で、JSON 出力には対応していても、出力に使う `json_schema` を指定できない場合があります。現在この問題の修正に取り組んでいますが、JSON schema 出力をサポートしているプロバイダーの利用を推奨します。そうでない場合、不正な JSON によりアプリが頻繁に動作しなくなる可能性があります。