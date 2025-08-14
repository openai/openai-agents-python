---
search:
  exclude: true
---
# モデル

Agents SDK には、2 種類の OpenAI モデルへのすぐに使えるサポートが含まれます:

-   ** 推奨 **: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を通じて、ほとんどの非 OpenAI モデルを使用できます。まず、 litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [対応モデル](https://docs.litellm.ai/docs/providers) のいずれかを使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、グローバルに `AsyncOpenAI` のインスタンスを LLM クライアントとして使いたい場合に便利です。これは、 LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使用します。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行のすべての エージェント にカスタムのモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンス上でモデルを指定できます。これにより、異なる エージェント に対して異なるプロバイダーを組み合わせて使用できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。最も手軽に多くのモデルを使う方法は [LiteLLM 連携](./litellm.md) です。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` で トレーシング を無効化するか、[別の トレーシング プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの例では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーはまだ Responses API をサポートしていないためです。プロバイダーが対応している場合は、 Responses の使用をおすすめします。

## モデルの組み合わせ

単一のワークフロー内で、エージェント ごとに異なるモデルを使いたい場合があります。例えば、振り分けには小さく高速なモデルを、複雑なタスクにはより大きく高性能なモデルを使う、といった使い分けです。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選べます:

1. モデル名を渡す。
2. 任意のモデル名と、それを Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    この SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしますが、ワークフローごとに単一のモデル形状を使うことを推奨します。両者は対応する機能やツールのセットが異なるためです。ワークフローでモデル形状を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1.  OpenAI モデルの名前を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を提供します。

エージェント で使用するモデルをさらに設定したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。これは、 temperature などの任意のモデル設定 パラメーター を提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、 OpenAI の Responses API を使用する場合、[他にもいくつかの任意 パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで指定できない場合は、`extra_args` を使って渡せます。

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

### トレーシング クライアントの 401 エラー

トレーシング に関連するエラーが発生する場合は、トレースが OpenAI の サーバー にアップロードされる一方で、 OpenAI の API キーをお持ちでないことが原因です。解決方法は次の 3 つです:

1. トレーシング を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング 用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[トレーシング ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだ対応していません。その結果、 404s などの問題が発生することがあります。解決するには次の 2 つの方法があります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。この場合、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの弱点で、 JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないというものです。現在この点の改善に取り組んでいますが、 JSON schema 出力をサポートするプロバイダーに依存することをおすすめします。さもないと、 JSON の形式不備により、アプリが頻繁に動作しなくなる可能性があります。

## プロバイダー間でのモデルの組み合わせ

モデルプロバイダー間の機能差に注意しないと、エラーに直面する可能性があります。例えば、 OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 と Web 検索 をサポートしますが、他の多くのプロバイダーはこれらをサポートしていません。次の制約に注意してください:

-   サポートしていないプロバイダーに、理解しない `tools` を送らないでください
-   テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外してください
-   structured JSON 出力をサポートしないプロバイダーは、無効な JSON を出力することがあります