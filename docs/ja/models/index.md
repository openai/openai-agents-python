---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルのサポートが次の 2 つの形で同梱されています。

-   **推奨**: 新しい Responses API を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   Chat Completions API を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## 非 OpenAI モデル

[LiteLLM 統合](../litellm.md) を通じて、ほとんどのその他の非 OpenAI モデルを利用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、[対応モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う別の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に該当します。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行のすべての エージェント にカスタムのモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できます。これにより、異なる エージェント で異なるプロバイダーを組み合わせて使用できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なほとんどのモデルを簡単に使う方法として、[LiteLLM 統合](../litellm.md) があります。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシング プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの例では、Responses API をまだサポートしていない LLM プロバイダーが多いため、Chat Completions API/モデルを使用しています。ご利用の LLM プロバイダーが Responses をサポートしている場合は、Responses の使用をおすすめします。

## モデルの組み合わせ

単一のワークフロー内で、各 エージェント に異なるモデルを使用したい場合があります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクにはより大きく高機能なモデルを使う、といった形です。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名と、それを Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形に対応していますが、2 つの形はサポートする機能やツールが異なるため、各ワークフローでは単一のモデルの形を使うことをおすすめします。ワークフローでモデルの形を混在させる必要がある場合は、利用するすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI のモデル名を直接設定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

エージェントで使用するモデルをさらに詳細に設定したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。これは temperature などの任意のモデル設定パラメーターを提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、[いくつかの他の任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで指定できない場合は、`extra_args` を使って渡すことができます。

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

### トレーシング クライアントのエラー 401

トレーシングに関連するエラーが発生する場合、トレースは OpenAI の サーバー にアップロードされる一方で、OpenAI の API キーをお持ちでないことが原因です。解決策は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用に OpenAI のキーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生することがあります。解決するには次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### Structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの弱点で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できません。現在この問題の修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することをおすすめします。そうでない場合、JSON の不正形式によりアプリが頻繁に壊れてしまう可能性があります。

## プロバイダーをまたぐモデルの組み合わせ

モデルプロバイダー間の機能差に注意しないと、エラーに遭遇する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 と Web 検索 をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制約に注意してください。

-   サポートされない `tools` を理解しないプロバイダーには送らないでください
-   テキストのみのモデルを呼び出す前に、マルチモーダル入力を除外してください
-   structured JSON 出力をサポートしないプロバイダーでは、無効な JSON が出力される場合があることに注意してください。