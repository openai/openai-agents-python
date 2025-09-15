---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルをすぐに使える形で 2 通りサポートしています。

-  **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-  [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## OpenAI モデル

`Agent` の初期化時にモデルを指定しない場合は、デフォルトのモデルが使われます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェントワークフローにおける予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) など別のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### デフォルトの OpenAI モデル

カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使いたい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 の reasoning モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使う場合、SDK は妥当な `ModelSettings` をデフォルトで適用します。具体的には、`reasoning.effort` と `verbosity` をどちらも `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

レイテンシをさらに下げたい、または特定の要件がある場合は、別のモデルと設定を選べます。デフォルトモデルの reasoning effort を調整するには、独自の `ModelSettings` を渡します。

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

特にレイテンシを下げる目的では、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) に `reasoning.effort="minimal"` を指定すると、デフォルト設定より高速に応答が返ることが多いです。ただし Responses API の一部の組み込みツール（ファイル検索や画像生成など）は `"minimal"` の reasoning effort をサポートしていないため、この Agents SDK ではデフォルトを `"low"` にしています。

#### 非 GPT-5 モデル

カスタムの `model_settings` を指定せずに GPT-5 以外のモデル名を渡した場合、SDK はどのモデルでも互換性のある汎用の `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md)を使って、ほとんどの非 OpenAI モデルを利用できます。まず、litellm の依存関係グループをインストールしてください。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [サポート対象モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルの他の利用方法

他の LLM プロバイダは、さらに 3 通りの方法で統合できます（examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に便利です。これは、LLM プロバイダが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できる場合に使用します。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで指定します。これにより、「この実行のすべてのエージェントでカスタムのモデルプロバイダを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] では、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダを組み合わせて使えます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能な多くのモデルを簡単に使う方法として、[LiteLLM 連携](./litellm.md) があります。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの examples では、Responses API をまだサポートしていない LLM プロバイダが多いため、Chat Completions API/モデルを使用しています。もし利用中の LLM プロバイダが Responses をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、振り分けには小さく高速なモデルを、複雑なタスクにはより大きく高性能なモデルを使うといった具合です。[`Agent`][agents.Agent] の設定時には、次のいずれかの方法で特定のモデルを選べます。

1. モデル名を渡す。
2. 任意のモデル名と、その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] の実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、両者はサポートする機能やツールのセットが異なるため、各ワークフローでは単一のモデル形状を使うことを推奨します。ワークフローでモデル形状を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使用するモデルをさらに細かく設定したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡してください。これは、temperature などのオプションのモデル設定パラメーターを提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使う場合、[他にもいくつかのオプションのパラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで利用できない場合は、`extra_args` を使って渡すことができます。

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

## 他の LLM プロバイダ使用時の一般的な問題

### トレーシングクライアントの 401 エラー

トレーシング関連のエラーが発生する場合、トレースは OpenAI のサーバーにアップロードされ、あなたが OpenAI の API キーを持っていないことが原因です。解決策は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用の OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. OpenAI 以外のトレースプロセッサーを使用する。[tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、多くの他の LLM プロバイダはまだサポートしていません。その結果、404 などの問題が発生する場合があります。解決策は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。examples は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### structured outputs のサポート

一部のモデルプロバイダは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。この場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダ側の制約で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないという問題です。現在、この点については修正に取り組んでいますが、JSON schema 出力をサポートしているプロバイダに依存することを推奨します。そうでない場合、不正な JSON が原因でアプリが頻繁に壊れる可能性があります。

## プロバイダ間でモデルを混在させる

モデルプロバイダ間の機能差を理解していないと、エラーに遭遇する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、多くの他プロバイダはこれらの機能をサポートしていません。以下の制約に注意してください。

-  サポートしていない `tools` を理解しないプロバイダに送らない
-  テキスト専用モデルを呼び出す前にマルチモーダル入力を除去する
-  structured JSON 出力をサポートしないプロバイダでは、無効な JSON が出力される場合がある点に注意する