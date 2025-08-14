---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデル向けの標準サポートが 2 つ用意されています:

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM 連携](./litellm.md) を通じて利用できます。まず、litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使うその他の方法

ほかの LLM プロバイダーを統合する方法は、さらに 3 つあります（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで指定します。これにより「この実行のすべてのエージェントでカスタムのモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定のエージェントインスタンスに対してモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使えます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なモデルの多くを簡単に使う方法としては、[LiteLLM 連携](./litellm.md) を利用するのが簡単です。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions の API/モデルを使用しています。もしお使いの LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえばトリアージには小さく高速なモデルを使い、複雑なタスクには大きく高性能なモデルを使うといった形です。[`Agent`][agents.Agent] を設定する際は、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしますが、ワークフローごとに 1 つのモデル形状に統一することを推奨します。両者はサポートする機能やツールのセットが異なるためです。ワークフロー上でモデル形状を混在させる場合は、使用するすべての機能が双方で利用可能であることを確認してください。

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

1. OpenAI モデルの名前を直接指定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

エージェントで使用するモデルをさらに詳細に設定したい場合は、`temperature` などの任意のモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、`user` や `service_tier` などの任意パラメーターを追加で指定できます。これらがトップレベルで指定できない場合は、`extra_args` を使って渡せます。

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

## 他の LLM プロバイダーを使用する際の一般的な問題

### トレーシング クライアント エラー 401

トレーシング関連のエラーが出る場合、トレースは OpenAI のサーバーにアップロードされる一方で、OpenAI の API キーがないことが原因です。解決方法は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK は既定で Responses API を使用しますが、多くの他社 LLM プロバイダーはまだサポートしていません。その結果、404 エラーなどが発生することがあります。解決するには次のいずれかの方法を取ってください。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### Structured outputs サポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生する場合があります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダー側の制約で、JSON 出力自体はサポートしていても、出力に使用する `json_schema` を指定できないためです。現在修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することを推奨します。そうでない場合、JSON の形式が不正でアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデルの混在

モデルプロバイダー間で機能差がある点に注意しないと、エラーに遭遇することがあります。たとえば OpenAI は Structured outputs、マルチモーダル入力、ホスト型の ファイル検索 と Web 検索 をサポートしますが、多くの他プロバイダーはこれらの機能をサポートしていません。次の制約に注意してください。

-   サポートしていない `tools` を理解しないプロバイダーには送らない
-   テキスト専用のモデルを呼び出す前に、マルチモーダル入力を除外する
-   構造化された JSON 出力をサポートしないプロバイダーは、無効な JSON を生成することがある点に注意する