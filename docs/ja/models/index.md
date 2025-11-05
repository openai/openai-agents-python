---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルに対するすぐに使えるサポートが 2 種類あります:

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい Responses API を使用して OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。Chat Completions API を使用して OpenAI API を呼び出します。

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、既定のモデルが使用されます。現在の既定は [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェント型ワークフローの予測性と低レイテンシのバランスが優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) など他のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### 既定の OpenAI モデル

すべての エージェント でカスタム モデルを設定していない場合に特定のモデルを一貫して使用したいときは、エージェント を実行する前に環境変数 `OPENAI_DEFAULT_MODEL` を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 の reasoning モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用すると、SDK は既定で妥当な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` の両方を `"low"` に設定します。これらの設定を自分で構成したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

レイテンシをさらに下げたい場合や特定の要件がある場合は、別のモデルと設定を選べます。既定モデルの reasoning 努力度を調整するには、独自の `ModelSettings` を渡します:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    model_settings=ModelSettings(reasoning=Reasoning(effort="minimal"), verbosity="low")
    # If OPENAI_DEFAULT_MODEL=gpt-5 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    # model="gpt-5",
)
```

特に低レイテンシ向けには、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) か [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) モデルで `reasoning.effort="minimal"` を使用すると、既定設定より高速に応答が返ることがよくあります。ただし、Responses API の一部の組み込みツール（ファイル検索 や 画像生成 など）は `"minimal"` の reasoning 努力度をサポートしていないため、本 Agents SDK の既定は `"low"` です。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡すと、SDK はあらゆるモデルに互換性のある汎用の `ModelSettings` に戻ります。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を通じて、ほとんどの非 OpenAI モデルを使用できます。まず、litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使用するその他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能なサンプルは [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行中のすべての エージェント にカスタム モデル プロバイダーを使用する」と宣言できます。設定可能なサンプルは [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] を使うと、特定の Agent インスタンスでモデルを指定できます。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使用できます。設定可能なサンプルは [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。最も多くの利用可能なモデルを簡単に使う方法は、[LiteLLM 連携](./litellm.md) 経由です。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` で トレーシング を無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの code examples では Chat Completions API/モデルを使用しています。多くの LLM プロバイダーがまだ Responses API をサポートしていないためです。プロバイダーがサポートしている場合は、Responses の使用をおすすめします。

## モデルの組み合わせ

1 つのワークフローの中で、エージェント ごとに異なるモデルを使用したい場合があります。たとえば、振り分けには小型で高速なモデルを使い、複雑なタスクにはより大きく高性能なモデルを使うといった形です。[`Agent`][agents.Agent] を構成する際、次のいずれかで特定のモデルを選択できます:

1. モデル名を渡す。
2. 任意のモデル名 + それを Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. 直接 [`Model`][agents.models.interface.Model] 実装を提供する。

!!!note

    本 SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしますが、両者はサポートする機能やツールが異なるため、各ワークフローでは単一のモデル形状を使用することをおすすめします。ワークフローでモデル形状を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

```python
from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model="gpt-5-mini", # (1)!
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIChatCompletionsModel( # (2)!
        model="gpt-5-nano",
        openai_client=AsyncOpenAI()
    ),
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model="gpt-5",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  OpenAI のモデル名を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を提供します。

エージェント に使用するモデルをさらに構成したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡してください。これは temperature などの任意のモデル構成パラメーターを提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、`extra_args` を使って渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(
        temperature=0.1,
        extra_args={"service_tier": "flex", "user": "user_12345"},
    ),
)
```

## 他の LLM プロバイダー使用時の一般的な問題

### トレーシング クライアント エラー 401

トレーシング に関連するエラーが発生する場合、トレースは OpenAI サーバー にアップロードされ、OpenAI の API キーがないことが原因です。解決策は 3 つあります:

1. トレーシング を完全に無効化: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング 用の OpenAI キーを設定: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. OpenAI 以外のトレース プロセッサーを使用。[tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK は既定で Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生する場合があります。解決するには、次の 2 つの方法があります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデル プロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデル プロバイダーの制約で、JSON 出力はサポートしているものの、出力に使用する `json_schema` を指定できません。現在この点の改善に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することをおすすめします。そうでないと、JSON の不正形式が原因でアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデルの組み合わせ

モデル プロバイダー間の機能差に留意しないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 と Web 検索 をサポートしますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制約に注意してください:

-   サポートされていない `tools` を理解しないプロバイダーに送らないでください
-   テキストのみのモデルを呼び出す前に、マルチモーダル入力をフィルターしてください
-   structured JSON 出力をサポートしないプロバイダーは、時折無効な JSON を生成する可能性があることに注意してください。