---
search:
  exclude: true
---
# モデル

Agents SDK には、すぐに使える 2 種類の OpenAI モデルのサポートが含まれています:

-   ** 推奨 **: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を通じて、ほとんどの非 OpenAI モデルを利用できます。まず、 litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、 `litellm/` プレフィックスを付けて、[サポート対象モデル](https://docs.litellm.ai/docs/providers) を利用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う別の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます (examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)):

1. [`set_default_openai_client`][agents.set_default_openai_client] は、グローバルに `AsyncOpenAI` のインスタンスを LLM クライアントとして使いたい場合に便利です。これは、 LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、 `base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで設定します。これにより、「この実行のすべての エージェント に対してカスタムのモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できます。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使えます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なモデルの多くを簡単に使う方法として、[LiteLLM 連携](./litellm.md) があります。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、 Chat Completions API/モデルを使用しています。お使いの LLM プロバイダーがサポートしている場合は、 Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェント ごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小型で高速なモデルを使い、複雑なタスクには大型で高性能なモデルを使うなどです。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選択できます:

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、各ワークフローでは 1 つのモデル形状を使うことを推奨します。両者はサポートする機能やツールのセットが異なるためです。ワークフローでモデル形状を混在させる必要がある場合は、利用するすべての機能が両方で利用可能であることを確認してください。

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

エージェント に使用するモデルをさらに設定したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。これは、 temperature などの任意のモデル設定パラメーターを提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、 OpenAI の Responses API を使う場合、[いくつかの他の任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで指定できない場合は、`extra_args` を使って渡せます。

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

### トレーシング クライアント エラー 401

トレーシング関連のエラーが発生する場合、トレースは OpenAI サーバーにアップロードされるためで、 OpenAI の API キーがないことが原因です。解決するには次の 3 つの選択肢があります:

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。詳しくは [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他の LLM プロバイダーはまだサポートしていません。その結果、 404 などの問題が発生することがあります。解決策は 2 つあります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。このため、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制限で、 JSON 出力はサポートするものの、出力に使用する `json_schema` を指定できません。現在この問題の修正に取り組んでいますが、 JSON schema 出力をサポートするプロバイダーに依存することを推奨します。そうでないと、不正な JSON によりアプリが頻繁に壊れてしまいます。

## プロバイダー間でのモデルの混在利用

モデルプロバイダー間の機能差に注意しないと、エラーに遭遇する可能性があります。たとえば、 OpenAI は structured outputs、マルチモーダル入力、 OpenAI がホストするファイル検索 と Web 検索 をサポートしますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください:

-   理解しないプロバイダーに未サポートの `tools` を送らない
-   テキスト専用のモデルを呼び出す前に、マルチモーダル入力をフィルタリングする
-   structured JSON 出力をサポートしないプロバイダーは、無効な JSON を生成することがある点に注意する