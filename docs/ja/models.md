# モデル

Agents SDK は、OpenAI モデルを以下の 2 種類の形式で標準サポートしています。

- **推奨** : 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
- [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## モデルの混在と組み合わせ

単一のワークフロー内で、各エージェントごとに異なるモデルを使用したい場合があります。例えば、トリアージには小型で高速なモデルを使用し、複雑なタスクにはより大きく高性能なモデルを使用することができます。[`Agent`][agents.Agent] を設定する際、以下のいずれかの方法で特定のモデルを選択できます。

1. OpenAI モデルの名前を直接指定する。
2. 任意のモデル名と、その名前をモデルインスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を指定する。
3. [`Model`][agents.models.interface.Model] 実装を直接指定する。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形式をサポートしていますが、各ワークフローでは単一のモデル形式を使用することを推奨します。これは、2 つの形式が異なる機能やツールをサポートしているためです。ワークフローで複数のモデル形式を混在させる必要がある場合は、使用するすべての機能が両方の形式で利用可能であることを確認してください。

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

1. OpenAI モデルの名前を直接指定しています。
2. [`Model`][agents.models.interface.Model] 実装を指定しています。

## 他の LLM プロバイダーの使用

他の LLM プロバイダーを使用するには、以下の 3 つの方法があります（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、グローバルに `AsyncOpenAI` インスタンスを LLM クライアントとして使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使用します。設定可能なコード例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで指定します。これにより、「この実行におけるすべてのエージェントにカスタムモデルプロバイダーを使用する」と指定できます。設定可能なコード例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は特定のエージェントインスタンスにモデルを指定できます。これにより、異なるエージェントに異なるプロバイダーを組み合わせて使用できます。設定可能なコード例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` を使用してトレーシングを無効化するか、[別のトレーシングプロセッサ](tracing.md) を設定することを推奨します。

!!! note

    これらのコード例では Chat Completions API / モデルを使用しています。これは、多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。使用する LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## 他の LLM プロバイダー使用時の一般的な問題

### トレーシングクライアントのエラー 401

トレーシングに関連するエラーが発生する場合、これはトレースが OpenAI サーバーにアップロードされるためであり、OpenAI API キーを持っていないことが原因です。解決方法は以下の 3 つです。

1. トレーシングを完全に無効化する : [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用に OpenAI キーを設定する : [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) から取得する必要があります。
3. OpenAI 以外のトレースプロセッサを使用する : 詳細は [トレーシングドキュメント](tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの他の LLM プロバイダーはまだこれをサポートしていません。そのため、404 エラーなどが発生する場合があります。解決方法は以下の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に有効です。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) を参照してください。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。そのため、以下のようなエラーが発生する場合があります。

```
BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}
```

これは一部のモデルプロバイダーの制限であり、JSON 出力はサポートしていますが、出力に使用する `json_schema` を指定できないためです。この問題への対応を進めていますが、現時点では JSON スキーマ出力をサポートするプロバイダーを使用することを推奨します。そうでない場合、アプリケーションが不適切な JSON により頻繁に破損する可能性があります。