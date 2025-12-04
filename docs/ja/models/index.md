---
search:
  exclude: true
---
# モデル

Agents SDK には、2 種類の OpenAI モデルが標準でサポートされています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい Responses API を使って OpenAI API を呼び出します(https://platform.openai.com/docs/api-reference/responses)。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。Chat Completions API を使って OpenAI API を呼び出します(https://platform.openai.com/docs/api-reference/chat)。

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、デフォルトのモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェント型ワークフローの予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) などの他のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### 既定の OpenAI モデル

カスタムモデルを設定していないすべての エージェント に対して特定のモデルを一貫して使用したい場合は、エージェント を実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 の推論モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用する場合、SDK は既定で適切な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` の両方を `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

さらに低レイテンシや特定の要件がある場合は、別のモデルと設定を選択できます。デフォルトモデルの推論強度を調整するには、独自の `ModelSettings` を渡します。

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

特に低レイテンシを重視する場合、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) モデルに `reasoning.effort="minimal"` を指定すると、デフォルト設定よりも高速に応答が返ることがよくあります。ただし、Responses API の一部の内蔵ツール（ファイル検索 や画像生成など）は `"minimal"` の推論強度をサポートしていないため、この Agents SDK では `"low"` をデフォルトとしています。

#### 非 GPT-5 モデル

カスタム `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK はどのモデルでも互換性のある汎用的な `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md)を使って、ほとんどの非 OpenAI モデルを利用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、[サポート対象のモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使うその他の方法

他の LLM プロバイダーはさらに 3 通りの方法で統合できます（コード例 は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に有用です。これは LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能なサンプルは [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルの仕組みです。これにより、「この実行のすべての エージェント に対してカスタムのモデルプロバイダーを使う」と指定できます。設定可能なサンプルは [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できます。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使用できます。設定可能なサンプルは [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどの利用可能なモデルを簡単に使うには、[LiteLLM 連携](./litellm.md) が便利です。

`platform.openai.com` の API キーをお持ちでない場合は、`set_tracing_disabled()` によるトレーシング の無効化、または[別のトレーシング プロセッサー](../tracing.md) の設定をおすすめします。

!!! note

    これらの例では、Responses API をまだサポートしていない LLM プロバイダーが多いため、Chat Completions API / モデルを使用しています。お使いの LLM プロバイダーが Responses をサポートしている場合は、Responses の使用をおすすめします。

## モデルの組み合わせ

単一のワークフロー内で、エージェント ごとに異なるモデルを使いたい場合があります。たとえば、振り分けには小型で高速なモデルを使用し、複雑なタスクには大型で高機能なモデルを使用する、といった使い分けです。[`Agent`][agents.Agent] を構成する際、次のいずれかで特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + それを Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形に対応していますが、ワークフローごとに 1 つのモデル形に統一することをおすすめします。これは両者でサポートする機能やツールのセットが異なるためです。ワークフロー上でモデル形の混在が必要な場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI モデルの名前を直接設定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

エージェント で使用するモデルをさらに詳細に構成したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡してください。これは temperature などのオプションのモデル構成パラメーターを提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、[他にもいくつかのオプション パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで指定できない場合は、`extra_args` を使って渡すことができます。

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

## 他社 LLM プロバイダー使用時の一般的な問題

### トレーシング クライアントのエラー 401

トレーシング に関連するエラーが発生する場合、これはトレースが OpenAI サーバー にアップロードされる一方で、OpenAI API キーをお持ちでないためです。解決策は次の 3 つです。

1. トレーシング を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング 用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[トレーシング ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK は既定で Responses API を使用しますが、他の多くの LLM プロバイダーはまだ対応していません。その結果、404 などの問題が発生する場合があります。解決するには次の 2 通りがあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にコード例 があります。

### Structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約で、JSON 出力には対応していても、出力に使用する `json_schema` を指定できないというものです。現在、この問題の修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーの利用を推奨します。そうでない場合、不正な形式の JSON によってアプリが頻繁に壊れる可能性があります。

## プロバイダー間でのモデル混在

モデルプロバイダー間の機能差に注意しないと、エラーに遭遇する可能性があります。例えば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 および Web 検索 をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制約に注意してください。

-   サポートしていない `tools` を理解しないプロバイダーには送らない
-   テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外する
-   構造化 JSON 出力をサポートしないプロバイダーでは、無効な JSON が出力される場合があることに注意する