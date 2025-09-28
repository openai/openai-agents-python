---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルのサポートが 2 つの形で組み込まれています。

-   **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出します。

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合は、デフォルトのモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェント型ワークフローにおける予測可能性と低レイテンシの強力なバランスを提供します。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) など別のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### デフォルトの OpenAI モデル

カスタムモデルを設定していないすべての エージェント で特定のモデルを継続的に使用したい場合は、エージェント を実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 の reasoning モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用する場合、SDK はデフォルトで適切な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` をどちらも `"low"` に設定します。これらの設定を自分で構成したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

より低レイテンシや特定の要件に合わせるため、別のモデルや設定を選択できます。デフォルトモデルの reasoning effort を調整するには、独自の `ModelSettings` を渡します。

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

特に低レイテンシを狙う場合は、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) モデルで `reasoning.effort="minimal"` を使用すると、デフォルト設定より高速にレスポンスが返ることがよくあります。ただし、Responses API の一部の組み込みツール（ファイル検索 や 画像生成 など）は `"minimal"` の reasoning effort をサポートしていないため、本 Agents SDK ではデフォルトを `"low"` にしています。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK は任意のモデルと互換性のある汎用的な `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を通じて、ほとんどの非 OpenAI モデルを使用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [対応モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使用するその他の方法

他の LLM プロバイダーを連携する方法はさらに 3 つあります（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に有用です。これは LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行のすべての エージェント に対してカスタムモデルプロバイダーを使用する」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] を使用すると、特定の Agent インスタンス上でモデルを指定できます。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使用できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。最も多くの利用可能なモデルを簡単に使う方法は、[LiteLLM 連携](./litellm.md) を使うことです。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` による トレーシング の無効化、または[別の トレーシング プロセッサー](../tracing.md) のセットアップを推奨します。

!!! note

    これらの例では、Responses API をまだサポートしていない LLM プロバイダーが多いため、Chat Completions API/モデルを使用しています。お使いの LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェント ごとに異なるモデルを使用したい場合があります。たとえば、選別には小型で高速なモデルを使い、複雑なタスクには大型で高性能なモデルを使用する、といった形です。[`Agent`][agents.Agent] を構成する際は、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    本 SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしていますが、両者はサポートする機能やツールのセットが異なるため、各ワークフローで使用するモデルの形は 1 種類に統一することを推奨します。もしワークフロー内でモデルの形を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1.  OpenAI モデルの名前を直接設定します。
2.  [`Model`][agents.models.interface.Model] 実装を提供します。

エージェント で使用するモデルをさらに構成したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。これは temperature などの任意のモデル構成パラメーターを提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する際には、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。これらがトップレベルで指定できない場合は、`extra_args` を使って渡せます。

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

### トレーシング クライアントのエラー 401

トレーシング に関連するエラーが発生する場合、これはトレースが OpenAI サーバー にアップロードされる一方で、OpenAI の API キーをお持ちでないことが原因です。次の 3 つの解決策があります。

1. トレーシング を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング 用に OpenAI のキーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[tracing のドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生する場合があります。解決するには、次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### Structured outputs のサポート

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダー側の制約で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないというものです。現在、これについては対処中ですが、JSON schema 出力をサポートするプロバイダーに依存することを推奨します。そうでないと、不正な形式の JSON が原因でアプリが頻繁に壊れてしまいます。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差に注意しないと、エラーが発生する可能性があります。例えば、OpenAI は structured outputs、マルチモーダル入力、ホスト型の ファイル検索 と Web 検索 をサポートしますが、多くの他プロバイダーはこれらの機能をサポートしていません。次の制約に注意してください。

-   サポートしていない `tools` を理解しないプロバイダーへ送信しないでください
-   テキストのみのモデルを呼び出す前に、マルチモーダル入力を除外してください
-   structured JSON 出力をサポートしていないプロバイダーでは、無効な JSON が出力されることがあります