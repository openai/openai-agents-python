---
search:
  exclude: true
---
# モデル

 Agents SDK には、OpenAI モデルをすぐに利用できる 2 つの形態が用意されています:

-   **推奨**: [`OpenAIResponsesModel`] は、新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`] は、[Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

 ほとんどの非 OpenAI モデルは [LiteLLM 連携](./litellm.md) 経由で使用できます。まず、litellm の依存グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

 次に、`litellm/` プレフィックスを付けて、[サポートされているモデル](https://docs.litellm.ai/docs/providers) を利用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを利用するその他の方法

 他の LLM プロバイダーは、さらに 3 つの方法で統合できます（コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）:

1. [`set_default_openai_client`] は、`AsyncOpenAI` インスタンスを LLM クライアントとしてグローバルに使用したい場合に便利です。LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケースに適しています。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) をご覧ください。
2. [`ModelProvider`] は `Runner.run` レベルで指定します。これにより、「この実行のすべてのエージェントに対してカスタムモデルプロバイダーを使用する」と宣言できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) をご覧ください。
3. [`Agent.model`] では、特定のエージェントインスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使用できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) をご覧ください。多くの利用可能なモデルを簡単に使う方法としては、[LiteLLM 連携](./litellm.md) が便利です。

 `platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの例では、ほとんどの LLM プロバイダーが Responses API をまだサポートしていないため、Chat Completions API/モデルを使用しています。ご使用の LLM プロバイダーが Responses API をサポートしている場合は、Responses を使うことをおすすめします。

## モデルの組み合わせ

 1 つのワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、振り分けには小型で高速なモデルを使用し、複雑なタスクには大型で高性能なモデルを使う、といった形です。[`Agent`] を設定する際、次のいずれかで特定のモデルを選択できます:

1. モデル名を直接渡す。
2. 任意のモデル名と、それをモデルインスタンスにマッピングできる [`ModelProvider`] を渡す。
3. [`Model`] 実装を直接提供する。

!!!note

    SDK は [`OpenAIResponsesModel`] と [`OpenAIChatCompletionsModel`] の両方の形態をサポートしていますが、ワークフローごとに単一のモデル形態を使用することを推奨します。両モデル形態は利用できる機能やツールのセットが異なるためです。ワークフローでモデル形態を混在させる必要がある場合は、使用するすべての機能が両形態で利用可能かをご確認ください。

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

1.  OpenAI のモデル名を直接設定します。  
2.  [`Model`] 実装を提供します。

 エージェントで使用するモデルをさらに詳細に設定したい場合は、`temperature` などのオプションパラメーターを含む [`ModelSettings`] を渡すことができます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

 また、OpenAI の Responses API を使用する際には、`user` や `service_tier` など、[その他のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで利用できない場合は、`extra_args` を使って渡してください。

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

### Tracing クライアント エラー 401

 トレーシングに関連したエラーが発生する場合、トレースが OpenAI サーバーにアップロードされるため、OpenAI API キーが設定されていないことが原因です。解決方法は次の 3 つです:

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`]
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) から取得したものである必要があります。
3. 非 OpenAI のトレースプロセッサーを使用する。詳細は [tracing docs](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

 SDK はデフォルトで Responses API を使用しますが、多くの LLM プロバイダーはまだサポートしていません。その結果、404 エラーなどが発生する場合があります。解決策は次の 2 つです:

1. [`set_default_openai_api("chat_completions")`] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`] を使用する。コード例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

 一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

 これは一部のプロバイダーの制限によるもので、JSON 出力には対応していても、出力に使用する `json_schema` を指定できません。現在修正に取り組んでいますが、JSON スキーマ出力をサポートしているプロバイダーを利用することを推奨します。さもないと、アプリが不正な JSON により頻繁に失敗するおそれがあります。

## プロバイダー間でのモデルの組み合わせ

 モデルプロバイダーごとの機能差に注意しないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型 file search や web search をサポートしていますが、多くの他プロバイダーはこれらをサポートしていません。次の制限に注意してください:

-   サポートしていない `tools` を理解しないプロバイダーには送信しない
-   テキストのみのモデルを呼び出す前にマルチモーダル入力を除外する
-   structured JSON 出力をサポートしないプロバイダーは、無効な JSON を生成することがある点に注意