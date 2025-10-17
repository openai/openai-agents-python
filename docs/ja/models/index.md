---
search:
  exclude: true
---
# モデル

Agents SDK には、2 種類の OpenAI モデルのサポートがあらかじめ組み込まれています。

-  **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-  [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## OpenAI モデル

`Agent` を初期化するときにモデルを指定しない場合は、デフォルトのモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェント型ワークフローにおける予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) など別のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### デフォルトの OpenAI モデル

カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 のいずれかの推論モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用すると、SDK はデフォルトで適切な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` の両方を `"low"` に設定します。これらの設定を自分で構成したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

レイテンシを下げたい場合や特定の要件がある場合は、別のモデルや設定を選べます。デフォルトモデルの推論負荷を調整するには、独自の `ModelSettings` を渡します。

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

特に低レイテンシが必要な場合、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) モデルで `reasoning.effort="minimal"` を使用すると、デフォルト設定よりも高速に応答が返ってくることがよくあります。ただし、Responses API の一部の組み込みツール（ファイル検索や画像生成など）は `"minimal"` の推論負荷をサポートしていないため、この Agents SDK ではデフォルトを `"low"` にしています。

#### 非 GPT-5 モデル

カスタムの `model_settings` を指定せずに GPT-5 以外のモデル名を渡した場合、SDK はあらゆるモデルと互換性のある一般的な `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を通じて、ほとんどの非 OpenAI モデルを使用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポートされているモデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使うその他の方法

他の LLM プロバイダを連携する方法はさらに 3 つあります（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に便利です。これは LLM プロバイダが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能なサンプルは [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行の全エージェントでカスタムモデルプロバイダを使う」と指定できます。設定可能なサンプルは [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスに対してモデルを指定できます。これにより、異なるエージェントごとに異なるプロバイダを組み合わせて使用できます。設定可能なサンプルは [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なモデルの多くを簡単に使う方法として [LiteLLM 連携](./litellm.md) があります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することをおすすめします。

!!! note

    これらの code examples では、Responses API をまだサポートしていないプロバイダが多いため、Chat Completions API/モデルを使用しています。お使いの LLM プロバイダが Responses をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。例えば、振り分けには小型で高速なモデルを、複雑なタスクには大型で高機能なモデルを使う、といった使い分けです。[`Agent`][agents.Agent] を構成する際、次のいずれかで特定のモデルを選べます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしますが、両者はサポートする機能やツールのセットが異なるため、各ワークフローでは単一のモデル形に統一することを推奨します。ワークフロー上どうしても混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI のモデル名を直接設定します。
2. [`Model`][agents.models.interface.Model] 実装を提供します。

エージェントで使用するモデルをさらに構成したい場合は、`temperature` などの任意のモデル構成パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルに該当項目がない場合は、`extra_args` を使って渡せます。

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

## 他の LLM プロバイダを使用する際の一般的な問題

### トレーシング クライアントのエラー 401

トレーシングに関連するエラーが発生する場合、トレースは OpenAI サーバーにアップロードされ、OpenAI の API キーがないことが原因です。次の 3 つの方法で解決できます。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダはまだサポートしていません。その結果、404 などの問題が発生する場合があります。解決方法は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### Structured outputs support

一部のモデルプロバイダは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダの制約で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できません。現在この問題の修正に取り組んでいますが、JSON schema 出力をサポートしているプロバイダに依存することをおすすめします。そうでない場合、不正な形式の JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダをまたいだモデルの混在

モデルプロバイダ間の機能差に注意しないと、エラーに遭遇する可能性があります。例えば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索および Web 検索をサポートしますが、他の多くのプロバイダはこれらの機能をサポートしていません。次の制限に注意してください。

-  サポートしていない `tools` を理解しないプロバイダに送らないでください
-  テキスト専用モデルを呼び出す前に、マルチモーダル入力をフィルタリングしてください
-  structured な JSON 出力をサポートしていないプロバイダは、無効な JSON を出力することがあります