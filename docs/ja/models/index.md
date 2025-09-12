---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに使える 2 つの方式が用意されています:

-  **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-  [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、デフォルトのモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェント的なワークフローにおける予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) などの他のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### デフォルトの OpenAI モデル

カスタムモデルを設定していないすべての エージェント で特定のモデルを一貫して使いたい場合は、エージェント を実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

GPT-5 の推論モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）をこの方法で使用する場合、SDK はデフォルトで妥当な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` をどちらも `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

より低レイテンシや特定の要件がある場合は、別のモデルや設定を選べます。デフォルトモデルの推論負荷を調整するには、独自の `ModelSettings` を渡します:

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

特に低レイテンシを重視する場合は、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) を `reasoning.effort="minimal"` と組み合わせると、デフォルト設定より速く応答を返すことがよくあります。ただし、Responses API の一部の組み込みツール（ファイル検索や画像生成など）は `"minimal"` の推論負荷をサポートしていないため、この Agents SDK ではデフォルトを `"low"` にしています。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK はあらゆるモデルと互換性のある汎用的な `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM との連携](./litellm.md)により、その他の多くの非 OpenAI モデルを利用できます。まず、litellm の依存関係グループをインストールします:

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、[サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使うその他の方法

他の LLM プロバイダーを統合する方法はさらに 3 つあります（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）:

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に有用です。これは LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケースに該当します。設定可能なサンプルは [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` のレベルにあります。これにより、「この実行でのすべての エージェント に対してカスタムのモデルプロバイダーを使う」と指定できます。設定可能なサンプルは [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できるようにします。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使えます。簡単に多くのモデルを使うには、[LiteLLM との連携](./litellm.md)が便利です。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` で トレーシング を無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの code examples では、Responses API/モデルを LLM プロバイダーの多くがまだサポートしていないため、Chat Completions を使用しています。お使いの LLM プロバイダーがサポートしている場合は、Responses の使用をおすすめします。

## モデルの組み合わせ

1 つのワークフローの中で、エージェント ごとに異なるモデルを使いたい場合があります。たとえば、振り分けには小型で高速なモデルを使い、複雑なタスクには大型で高性能なモデルを使う、といった使い分けです。[`Agent`][agents.Agent] を構成する際は、次のいずれかの方法で特定のモデルを選べます:

1. モデル名を渡す。
2. 任意のモデル名 + それを Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形に対応していますが、両者はサポートする機能やツールのセットが異なるため、各ワークフローでは 1 つのモデル形に統一することをおすすめします。もしワークフロー上でモデル形を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

特定の エージェント で使用するモデルをさらに細かく設定したい場合は、`temperature` などの任意のモデル構成 パラメーター を提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意 パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで指定できない場合は、`extra_args` を使って渡せます。

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

## 他の LLM プロバイダー利用時の一般的な問題

### トレーシング クライアントのエラー 401

トレーシング に関連するエラーが発生する場合、これはトレースが OpenAI の サーバー にアップロードされる一方で、OpenAI の API キーをお持ちでないためです。解決策は次の 3 つです:

1. トレーシング を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング 用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生する場合があります。解決するには、次の 2 つの方法があります:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダー側の制約で、JSON 出力はサポートしているものの、出力に使用する `json_schema` を指定できません。現在この問題に対する修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することをおすすめします。そうしない場合、不正な JSON によりアプリが頻繁に壊れてしまう可能性があります。

## プロバイダーをまたぐモデルの混在

モデルプロバイダー間の機能差異を把握していないと、エラーに遭遇する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 および Web 検索 をサポートしますが、他の多くのプロバイダーはこれらの機能をサポートしていません。以下の制約に注意してください:

-  サポートしていない `tools` を理解しないプロバイダーに送らない
-  テキスト専用のモデルを呼び出す前に、マルチモーダル入力をフィルタリングする
-  structured な JSON 出力をサポートしていないプロバイダーは、無効な JSON を生成することがある点に注意する