---
search:
  exclude: true
---
# モデル

Agents SDK は、すぐに使える OpenAI モデルの 2 つの方式をサポートします。

-  **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい Responses API を使用して OpenAI API を呼び出します。
-  [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。Chat Completions API を使用して OpenAI API を呼び出します。

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、デフォルトのモデルが使用されます。現在のデフォルトは互換性と低レイテンシのために [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。アクセス権がある場合は、明示的な `model_settings` を保ったまま品質を高めるために エージェント を [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) に設定することをお勧めします。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような他のモデルへ切り替えたい場合は、次のセクションの手順に従ってください。

### デフォルトの OpenAI モデル

カスタムモデルを設定していないすべての エージェント に対して特定のモデルを一貫して使用したい場合は、エージェント を実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 の推論モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用する場合、SDK はデフォルトで妥当な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` の両方を `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

より低レイテンシや特定の要件のために、別のモデルと設定を選択できます。デフォルトモデルの推論量を調整するには、独自の `ModelSettings` を渡します。

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

特に低レイテンシ向けには、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) を `reasoning.effort="minimal"` と組み合わせると、多くの場合デフォルト設定より高速に応答が返ります。ただし、Responses API の一部の組み込みツール（ファイル検索 や 画像生成 など）は `"minimal"` の推論量をサポートしていないため、本 Agents SDK のデフォルトは `"low"` になっています。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK はあらゆるモデルと互換性のある汎用の `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 統合](./litellm.md) を通じて、ほとんどの他社製モデルを使用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーは、さらに 3 通りの方法で統合できます（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に該当します。設定可能なサンプルは [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行のすべての エージェント にカスタムのモデルプロバイダーを使う」と指定できます。設定可能なサンプルは [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] を使うと、特定の Agent インスタンスでモデルを指定できます。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使用できます。簡単に多くの利用可能なモデルを使う方法としては、[LiteLLM 統合](./litellm.md) の利用が挙げられます。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` で トレーシング を無効にするか、[別の トレーシング プロセッサー](../tracing.md) を設定することをお勧めします。

!!! note

    これらの code examples では、Responses API/モデルを使用しています。これは、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないためです。LLM プロバイダーが Responses をサポートしている場合は、Responses の使用をお勧めします。

## モデルの組み合わせ

1 つのワークフロー内で、エージェント ごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクには大きく高機能なモデルを使う、といった具合です。[`Agent`][agents.Agent] を構成する際、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名と、その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接提供する。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしますが、各ワークフローでは単一のモデル形状の使用を推奨します。これは両者がサポートする機能やツールのセットが異なるためです。ワークフローでモデル形状を混在させる必要がある場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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

エージェント に使用するモデルをさらに構成したい場合は、`temperature` などのオプションのモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する際には、[`user`、`service_tier` など、他にもいくつかのオプション パラメーター](https://platform.openai.com/docs/api-reference/responses/create) があります。トップレベルで利用できない場合は、`extra_args` を使ってそれらを渡せます。

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

## 他社製 LLM プロバイダー利用時の一般的な問題

### Tracing client error 401

トレーシング に関連するエラーが発生する場合、これはトレースが OpenAI サーバー にアップロードされ、あなたが OpenAI の API キーを持っていないためです。解決策は 3 つあります。

1. トレーシング を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング 用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものを使用する必要があります。
3. 非 OpenAI のトレース プロセッサーを使用する。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他社製 LLM プロバイダーはまだ対応していません。その結果、404 などの問題が発生する場合があります。解決するには次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは `OPENAI_API_KEY` と `OPENAI_BASE_URL` を環境変数で設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。このため、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないというものです。現在、これに対する修正に取り組んでいますが、JSON schema 出力をサポートしているプロバイダーに依存することをお勧めします。そうしないと、不正な形式の JSON によってアプリが頻繁に壊れてしまう可能性があります。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差異を把握しておかないと、エラーに遭遇する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 と Web 検索 をサポートしていますが、多くの他社プロバイダーはこれらの機能をサポートしていません。以下の制限に注意してください。

-  サポートされていない `tools` を理解しないプロバイダーに送らないでください
-  テキストのみのモデルを呼び出す前に、マルチモーダル入力をフィルタリングしてください
-  structured JSON 出力をサポートしていないプロバイダーは、無効な JSON を出力することがあります