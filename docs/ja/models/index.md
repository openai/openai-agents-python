---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに利用できる 2 種類のサポートが付属しています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## OpenAI 以外のモデル

[LiteLLM 連携](./litellm.md)を利用すると、ほとんどの OpenAI 以外のモデルを使用できます。まずは litellm 依存グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

その後、`litellm/` プレフィックスを付けて、[サポートされているモデル](https://docs.litellm.ai/docs/providers)を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 以外のモデルを使用するその他の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます（[コード例](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)はこちら）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   OpenAI 互換の API エンドポイントを持つ LLM プロバイダーで、`base_url` と `api_key` を設定できる場合に、`AsyncOpenAI` インスタンスをグローバルに LLM クライアントとして利用したいときに便利です。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで使用します。「この実行に含まれるすべての エージェント に対してカスタムモデルプロバイダーを使う」と指定できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model]  
   特定の `Agent` インスタンスでモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) をご覧ください。最も多くのモデルを簡単に利用する方法としては、[LiteLLM 連携](./litellm.md)があります。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md)を設定することを推奨します。

!!! note
    これらの例では、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API/モデルを使用しています。お使いの LLM プロバイダーが Responses API をサポートしている場合は、Responses の利用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、振り分けには小型で高速なモデルを、複雑なタスクには大型で高性能なモデルを使用するといったケースです。[`Agent`][agents.Agent] を設定する際、以下のいずれかでモデルを選択できます。

1. モデル名を直接渡す  
2. いずれかのモデル名と、それをモデルインスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の双方をサポートしていますが、各ワークフローでは 1 つのモデル形状に統一することを推奨します。両者は利用できる機能やツールが異なるためです。異なる形状を混在させる場合は、使用するすべての機能が両モデルで利用可能であることをご確認ください。

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

エージェントで使用するモデルをさらに設定したい場合は、`temperature` などの任意パラメーターを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、`user` や `service_tier` など[追加のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create)があります。トップレベルで指定できない場合は、`extra_args` を使用して渡すことも可能です。

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

### Tracing クライアントエラー 401

トレーシング関連のエラーが発生する場合、トレースは OpenAI サーバーへアップロードされるため、OpenAI API キーが必要です。以下のいずれかの方法で解決できます。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。  
3. OpenAI 以外のトレースプロセッサーを使用する。詳しくは [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの LLM プロバイダーはまだ対応していません。そのため 404 エラーなどが発生する場合があります。解決策は以下のとおりです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   [コード例](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) があります。

### 構造化出力のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないためです。現在修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーを使用することを推奨します。そうでない場合、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差を理解しておかないと、エラーが発生する可能性があります。たとえば、OpenAI は 構造化出力、マルチモーダル入力、ホスト型ファイル検索・Web 検索 をサポートしていますが、多くの他プロバイダーはこれらをサポートしていません。以下の制限に注意してください。

-   対応していないプロバイダーへ `tools` を送信しない  
-   テキストのみのモデルを呼び出す前に、マルチモーダル入力を除外する  
-   構造化 JSON 出力をサポートしないプロバイダーでは、無効な JSON が時折生成される点に注意する