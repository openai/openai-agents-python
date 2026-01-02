---
search:
  exclude: true
---
# モデル

Agents SDK には、2 つの形で OpenAI モデルのサポートが標準で含まれています。

-  **推奨**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出します。
-  [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出します。

## OpenAI モデル

`Agent` の初期化時にモデルを指定しない場合は、デフォルトのモデルが使用されます。現在のデフォルトは互換性と低レイテンシのために [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。アクセス権がある場合は、明示的な `model_settings` を維持しつつ、より高品質な [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) に設定することを推奨します。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) などの他のモデルに切り替えたい場合は、次のセクションの手順に従ってください。

### デフォルトの OpenAI モデル

カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用したい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 のいずれかの推論モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用する場合、SDK はデフォルトで適切な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` の両方を `"low"` に設定します。これらの設定を自分で構成したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

レイテンシを下げたい場合や特定の要件がある場合は、別のモデルと設定を選択できます。デフォルトモデルの推論努力度を調整するには、独自の `ModelSettings` を渡します。

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

とくに低レイテンシを重視する場合、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) を `reasoning.effort="minimal"` で使用すると、デフォルト設定より高速に応答を返すことが多いです。ただし、Responses API の一部の組み込みツール（ファイル検索や画像生成など）は `"minimal"` の推論努力度をサポートしていないため、この Agents SDK ではデフォルトを `"low"` にしています。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで GPT-5 以外のモデル名を渡した場合、SDK はあらゆるモデルと互換性のある汎用的な `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md)を通じて、ほとんどの非 OpenAI モデルを使用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて [対応モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーを、さらに 3 つの方法で統合できます（code examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、グローバルに `AsyncOpenAI` のインスタンスを LLM クライアントとして使用したい場合に便利です。これは LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能な code examples は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` のレベルで指定します。これにより、「この実行内のすべてのエージェントでカスタムのモデルプロバイダーを使用する」と指定できます。設定可能な code examples は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] は、特定の Agent インスタンスでモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使用できます。設定可能な code examples は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。最も多くの利用可能なモデルを簡単に使う方法は、[LiteLLM 連携](./litellm.md)です。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの code examples では、Responses API をまだサポートしていない LLM プロバイダーがほとんどであるため、Chat Completions API/モデルを使用しています。もしお使いの LLM プロバイダーが対応している場合は、Responses の使用を推奨します。

## モデルの組み合わせ

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、振り分けには小型で高速なモデルを、複雑なタスクには大型で高性能なモデルを使うことが考えられます。[`Agent`][agents.Agent] を構成する際、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を直接渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] の実装を直接渡す。

!!!note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしますが、両者はサポートする機能やツールのセットが異なるため、各ワークフローでは単一のモデル形状を使用することを推奨します。ワークフローでモデル形状を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

1. OpenAI のモデル名を直接指定します。
2. [`Model`][agents.models.interface.Model] の実装を提供します。

エージェントで使用するモデルをさらに構成したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡してください。これは temperature などの任意のモデル構成パラメーターを提供します。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する場合、[いくつかの他の任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで指定できない場合は、`extra_args` を使って渡すこともできます。

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

### トレーシングクライアントのエラー 401

トレーシング関連のエラーが発生する場合、トレースは OpenAI のサーバーへアップロードされるため、OpenAI の API キーをお持ちでないことが原因です。解決策は次の 3 つです。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。詳しくは [tracing のドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生する場合があります。解決するには次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。code examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約で、JSON 出力自体はサポートしているものの、出力に使用する `json_schema` を指定できないというものです。現在これに対する改善に取り組んでいますが、JSON schema 出力をサポートしているプロバイダーを利用することを推奨します。そうでない場合、不正な形式の JSON によりアプリが頻繁に動作しなくなる可能性があります。

## 複数プロバイダー間でのモデル混在

モデルプロバイダー間の機能差に注意しないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

-  サポートしていない `tools` を理解しないプロバイダーには送らない
-  テキスト専用のモデルを呼び出す前に、マルチモーダル入力を除外する
-  structured JSON 出力をサポートしないプロバイダーは、無効な JSON を出力する場合がある点に注意する