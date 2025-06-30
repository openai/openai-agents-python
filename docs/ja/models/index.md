---
search:
  exclude: true
---
# モデル

Agents SDK は OpenAI モデルを 2 つの形態で即利用できます。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM インテグレーション](./litellm.md) 経由で利用できます。まず、litellm 依存グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` 接頭辞を付けて任意の [サポート対象モデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを利用するその他の方法

他の LLM プロバイダーを統合する方法は、あと 3 つあります（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に例があります）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   `AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使用します。設定例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) にあります。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルでカスタムモデルプロバイダーを指定できます。これにより「この run のすべてのエージェントでカスタムプロバイダーを使う」と宣言できます。設定例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) にあります。
3. [`Agent.model`][agents.agent.Agent.model]  
   特定のエージェントインスタンスにモデルを指定できます。エージェントごとに異なるプロバイダーを組み合わせることが可能です。設定例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) にあります。ほとんどのモデルを簡単に利用する方法として [LiteLLM インテグレーション](./litellm.md) を利用できます。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することをお勧めします。

!!! note
    これらの例では、Responses API をまだサポートしていない LLM プロバイダーが多いため、Chat Completions API/モデルを使用しています。LLM プロバイダーが Responses API をサポートしている場合は、Responses を使用することを推奨します。

## モデルの組み合わせ

1 つのワークフロー内でエージェントごとに異なるモデルを使用したい場合があります。たとえば、振り分けには小さく高速なモデルを、複雑なタスクには大きく高性能なモデルを使用するといったケースです。[`Agent`][agents.Agent] を設定する際、次のいずれかの方法でモデルを選択できます。

1. モデル名を直接指定する  
2. 任意のモデル名と、その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を指定する  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す  

!!!note
    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両形態をサポートしていますが、各ワークフローで 1 つのモデル形態に統一することを推奨します。2 つの形態はサポートする機能とツールが異なるためです。混在させる場合は、使用する機能が双方で利用可能かを必ず確認してください。

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

1. OpenAI のモデル名を直接設定  
2. [`Model`][agents.models.interface.Model] 実装を提供  

エージェントで使用するモデルをさらに構成したい場合は、`temperature` などのオプションパラメーターを指定できる [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

OpenAI の Responses API を使用する場合、`user` や `service_tier` など[その他のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで指定できない場合は、`extra_args` で渡してください。

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

Responses API でトークンの対数確率を取得したい場合は、
`ModelSettings` の `top_logprobs` を設定してください。

```python
from agents import Agent, ModelSettings

agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(top_logprobs=2),
)
```

## 他の LLM プロバイダー使用時の一般的な問題

### Tracing クライアントの 401 エラー

Tracing 関連のエラーが発生する場合、トレースは OpenAI サーバーへアップロードされるため、OpenAI API キーが必要です。対応方法は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレース用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) で取得したものが必要です。  
3. OpenAI 以外のトレースプロセッサーを使用する。詳細は [tracing のドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの LLM プロバイダーはまだ非対応です。その結果、404 などのエラーが発生することがあります。対処方法は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す  
   `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に有効です。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する  
   例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力自体はサポートしていても `json_schema` を指定できないことが原因です。修正に向けて取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーを使用することをお勧めします。そうでないと、不正な JSON が返されてアプリが頻繁に壊れる可能性があります。

## プロバイダーを跨いだモデルの組み合わせ

モデルプロバイダーごとの機能差に注意しないと、エラーが発生します。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型の file search や web search をサポートしていますが、多くの他プロバイダーは非対応です。以下の制限に留意してください。

- 対応していないプロバイダーには未サポートの `tools` を送らない  
- テキストのみのモデルを呼び出す前にマルチモーダル入力を除外する  
- structured JSON 出力をサポートしていないプロバイダーでは、不正な JSON が返ることがある点に注意する