# モデル

エージェント SDK は、次の 2 種類の OpenAI モデルをサポートしています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## モデルの組み合わせ

1 つのワークフロー内で、各エージェントに異なるモデルを使用したい場合があります。たとえば、トリアージには小さくて高速なモデルを使用し、複雑なタスクにはより大きくて高機能なモデルを使用することができます。[`Agent`][agents.Agent] を設定する際に、次の方法で特定のモデルを選択できます。

1. OpenAI モデルの名前を渡す。
2. モデル名とその名前をモデルインスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. 直接 [`Model`][agents.models.interface.Model] 実装を提供する。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、各ワークフローに対して単一のモデル形状を使用することをお勧めします。2 つの形状は異なる機能とツールをサポートしているためです。ワークフローでモデル形状を組み合わせる必要がある場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI モデルの名前を直接設定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

## 他の LLM プロバイダーの使用

他の LLM プロバイダーを 3 つの方法で使用できます（例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持っている場合で、`base_url` と `api_key` を設定できます。[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) に設定可能な例があります。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで使用します。これにより、「この実行のすべてのエージェントにカスタムモデルプロバイダーを使用する」と指定できます。[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) に設定可能な例があります。
3. [`Agent.model`][agents.agent.Agent.model] では、特定のエージェントインスタンスにモデルを指定できます。これにより、異なるエージェントに対して異なるプロバイダーを組み合わせて使用することができます。[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) に設定可能な例があります。

`platform.openai.com` からの API キーがない場合は、`set_tracing_disabled()` を使用してトレーシングを無効にするか、[異なるトレーシングプロセッサー](tracing.md)を設定することをお勧めします。

!!! note

    これらの例では、Chat Completions API/モデルを使用しています。なぜなら、ほとんどの LLM プロバイダーはまだ Responses API をサポートしていないからです。LLM プロバイダーがそれをサポートしている場合は、Responses を使用することをお勧めします。

## 他の LLM プロバイダーを使用する際の一般的な問題

### トレーシングクライアントエラー 401

トレーシングに関連するエラーが発生した場合、これはトレースが OpenAI サーバーにアップロードされ、OpenAI API キーを持っていないためです。これを解決するには、次の 3 つのオプションがあります。

1. トレーシングを完全に無効にする: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) からのものでなければなりません。
3. 非 OpenAI トレースプロセッサーを使用する。[トレーシングドキュメント](tracing.md#custom-tracing-processors)を参照してください。

### Responses API サポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他の LLM プロバイダーはまだサポートしていません。その結果、404 エラーや類似の問題が発生することがあります。これを解決するには、次の 2 つのオプションがあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数を介して `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### 適切な形式のデータのサポート

一部のモデルプロバイダーは[適切な形式のデータ](https://platform.openai.com/docs/guides/structured-outputs)をサポートしていません。これにより、次のようなエラーが発生することがあります。

```
BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}
```

これは一部のモデルプロバイダーの欠点で、JSON 出力をサポートしていますが、出力に使用する `json_schema` を指定することはできません。この問題の修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーに依存することをお勧めします。さもないと、アプリが不正な JSON のために頻繁に壊れることになります。