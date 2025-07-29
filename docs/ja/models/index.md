---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルを以下 2 種類でサポートしています。

- **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] — 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。  
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] — [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## 非 OpenAI モデル

ほとんどの非 OpenAI モデルは [LiteLLM 連携](./litellm.md) 経由で利用できます。まずは litellm の依存関係グループをインストールしてください。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [対応モデル](https://docs.litellm.ai/docs/providers) を利用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを利用するその他の方法

他社 LLM プロバイダーを連携する方法は 3 つあります（[コード例はこちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client]  
   グローバルに `AsyncOpenAI` インスタンスを LLM クライアントとして利用したい場合に便利です。OpenAI 互換エンドポイントを持つプロバイダーで、`base_url` と `api_key` を設定できるケースに適しています。[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) に設定例があります。
2. [`ModelProvider`][agents.models.interface.ModelProvider]  
   `Runner.run` レベルで指定できます。「この実行内のすべてのエージェントでカスタムモデルプロバイダーを使う」といった場合に利用します。[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) をご覧ください。
3. [`Agent.model`][agents.agent.Agent.model]  
   個々のエージェントごとにモデルを指定できます。エージェントごとに異なるプロバイダーを組み合わせたい場合に便利です。[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) に設定例があります。多数のモデルを簡単に使う方法としては [LiteLLM 連携](./litellm.md) が手軽です。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらのコード例では Chat Completions API／モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。もしご利用のプロバイダーが Responses API をサポートしている場合は、Responses の利用をお勧めします。

## モデルの組み合わせ

単一のワークフロー内でエージェントごとに異なるモデルを使いたい場合があります。たとえば、振り分けには小型で高速なモデルを使い、複雑なタスクには高性能モデルを使う、といったケースです。[`Agent`][agents.Agent] を設定する際は、以下のいずれかでモデルを指定できます。

1. モデル名を直接渡す。  
2. 任意のモデル名と、その名前を [`ModelProvider`][agents.models.interface.ModelProvider] が `Model` インスタンスへマッピングできるようにする。  
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。  

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方をサポートしていますが、1 つのワークフローではどちらか 1 つのモデル形状を使うことを推奨します。両者は利用できる機能やツールが異なるためです。もし混在させる場合は、使用する機能が両モデルでサポートされていることを確認してください。

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

1. OpenAI モデル名を直接指定しています。  
2. [`Model`][agents.models.interface.Model] 実装を渡しています。  

エージェントで利用するモデルをさらに細かく設定したい場合は、`temperature` などのオプションを持つ [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4o",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を利用する場合、`user` や `service_tier` など [追加のオプションパラメーター](https://platform.openai.com/docs/api-reference/responses/create) がいくつかあります。トップレベルで指定できない場合は、`extra_args` に渡してください。

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

## 他社 LLM プロバイダー使用時によくある問題

### トレーシングクライアントの 401 エラー

トレーシングは OpenAI サーバーへアップロードされるため、OpenAI API キーがないとエラーになります。解決策は次の 3 つです。

1. トレーシングを完全に無効にする — [`set_tracing_disabled(True)`][agents.set_tracing_disabled]  
2. トレーシング用の OpenAI キーを設定する — [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]  
   この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のキーである必要があります。  
3. OpenAI 以外のトレースプロセッサーを使用する — 詳細は [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの LLM プロバイダーは未対応です。そのため 404 などのエラーが発生する場合があります。解決策は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。  
   これは環境変数 `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。  
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。  
   [コード例はこちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) を参照してください。  

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部プロバイダーの制限で、JSON 出力はサポートしていても `json_schema` を指定できないためです。現在修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを利用することを推奨します。そうでない場合、JSON が不正でアプリが破損することが頻繁に起こります。

## プロバイダーをまたいだモデルの混在

プロバイダーごとにサポート機能が異なるため、注意が必要です。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型 file search・web search をサポートしていますが、多くの他社プロバイダーは未対応です。以下の点にご注意ください。

- 対応していない `tools` を理解できないプロバイダーに送らない  
- テキスト専用モデルを呼び出す前にマルチモーダル入力を除外する  
- structured JSON outputs をサポートしないプロバイダーでは無効な JSON が生成される可能性があることを理解する  