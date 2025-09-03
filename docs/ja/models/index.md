---
search:
  exclude: true
---
# モデル

Agents SDK には、2 つの形態で OpenAI モデルの即時利用が含まれます。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を用いて OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を用いて OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、デフォルトモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェント的ワークフローにおける予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) など他のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### 既定の OpenAI モデル

カスタムモデルを設定していないすべてのエージェントに対して特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 のいずれかの reasoning モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、[`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用すると、SDK は既定で妥当な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` をともに `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

より低レイテンシや特定の要件がある場合は、別のモデルと設定を選択できます。デフォルトモデルの reasoning 努力度を調整するには、独自の `ModelSettings` を渡してください。

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

特に低レイテンシを狙う場合、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) モデルを `reasoning.effort="minimal"` で使用すると、デフォルト設定より高速に応答が返ることがよくあります。ただし、Responses API の一部の組み込みツール（ファイル検索や画像生成など）は `"minimal"` の reasoning 努力度をサポートしていないため、この Agents SDK は既定で `"low"` に設定しています。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK はあらゆるモデルと互換性のある汎用的な `ModelSettings` に戻します。

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

他の LLM プロバイダーを統合する方法はさらに 3 つあります（code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に該当します。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルにあります。これにより、「この実行のすべてのエージェントにカスタムモデルプロバイダーを使用する」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できるようにします。これにより、エージェントごとに異なるプロバイダーを組み合わせて使用できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。ほとんどの利用可能なモデルを簡単に使う方法は、[LiteLLM 連携](./litellm.md) を利用することです。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` によるトレーシングの無効化、または [別のトレーシング プロセッサー](../tracing.md) の設定を推奨します。

!!! note

    これらの code examples では、Responses API をサポートしていない LLM プロバイダーがほとんどであるため、Chat Completions API/モデルを使用しています。もしお使いの LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、振り分けには小型で高速なモデルを使用し、複雑なタスクには大型で高性能なモデルを使用できます。[`Agent`][agents.Agent] を構成する際には、以下のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名と、それを Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. 直接 [`Model`][agents.models.interface.Model] 実装を提供する。

!!!note

    当社の SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形態をサポートしますが、両者はサポートする機能やツールのセットが異なるため、各ワークフローでは単一のモデル形態の使用を推奨します。ワークフローがモデル形態の混在を必要とする場合は、使用するすべての機能が双方で利用可能であることを確認してください。

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

エージェントで使用するモデルをさらに構成したい場合は、温度などの任意のモデル構成パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡すことができます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、`extra_args` を使ってそれらを渡せます。

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

トレースは OpenAI のサーバーにアップロードされ、OpenAI の API キーを持っていない場合、トレーシング関連のエラーが発生します。解決策は 3 つあります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK は既定で Responses API を使用しますが、ほとんどの他社 LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生することがあります。解決策は 2 つあります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。code examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。その結果、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないというものです。私たちはこれに対する修正に取り組んでいますが、JSON schema 出力をサポートするプロバイダーに依存することをおすすめします。そうでない場合、JSON の不正形式によりアプリがしばしば壊れてしまいます。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差異に注意しないと、エラーに直面する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしますが、多くの他社プロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

-   サポートされていない `tools` を理解しないプロバイダーへ送信しないでください
-   テキストのみのモデルを呼び出す前に、マルチモーダル入力をフィルタリングしてください
-   structured な JSON 出力をサポートしないプロバイダーは、無効な JSON を生成することがあります