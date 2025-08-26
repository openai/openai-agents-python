---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルに対する標準サポートが次の 2 つの形で含まれています。

-   ** 推奨 **: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。新しい Responses API（[https://platform.openai.com/docs/api-reference/responses](https://platform.openai.com/docs/api-reference/responses)）を使って OpenAI API を呼び出します。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。Chat Completions API（[https://platform.openai.com/docs/api-reference/chat](https://platform.openai.com/docs/api-reference/chat)）を使って OpenAI API を呼び出します。

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、デフォルトモデルが使用されます。現在のデフォルトは [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) で、エージェントワークフローの予測可能性と低レイテンシのバランスに優れています。

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) などの他モデルに切り替える場合は、次のセクションの手順に従ってください。

### デフォルト OpenAI モデル

すべての エージェント でカスタムモデルを設定していない場合に特定のモデルを一貫して使用したいときは、エージェント を実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 モデル

この方法で GPT-5 の推論モデル（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini)、または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）を使用する場合、SDK は既定で適切な `ModelSettings` を適用します。具体的には、`reasoning.effort` と `verbosity` の両方を `"low"` に設定します。これらの設定を自分で構築したい場合は、`agents.models.get_default_model_settings("gpt-5")` を呼び出してください。

さらなる低レイテンシや特定要件のために、別のモデルと設定を選ぶこともできます。デフォルトモデルの推論負荷を調整するには、独自の `ModelSettings` を渡します。

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

特にレイテンシを下げる目的では、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) または [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) を `reasoning.effort="minimal"` と組み合わせると、デフォルト設定よりも高速に応答が返ることが多いです。ただし、Responses API の一部の組み込みツール（ファイル検索 や 画像生成 など）は `"minimal"` の推論負荷をサポートしていないため、この Agents SDK のデフォルトは `"low"` になっています。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで非 GPT-5 のモデル名を渡した場合、SDK はあらゆるモデルと互換性のある汎用の `ModelSettings` にフォールバックします。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md)を介して、ほとんどの非 OpenAI モデルを使用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、[サポート対象モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルの他の利用方法

他の LLM プロバイダーはさらに 3 つの方法で統合できます（[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) に code examples があります）。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、LLM クライアントとして `AsyncOpenAI` のインスタンスをグローバルに使用したい場合に便利です。これは、LLM プロバイダーが OpenAI 互換の API エンドポイントを持ち、`base_url` と `api_key` を設定できるケース向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより、「この実行のすべての エージェント に対してカスタムのモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] により、特定の Agent インスタンスでモデルを指定できます。これにより、エージェント ごとに異なるプロバイダーを組み合わせて使用できます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。最も多くの利用可能なモデルを簡単に使う方法は、[LiteLLM 連携](./litellm.md) です。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` で トレーシング を無効化するか、[別のトレーシング プロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では、Responses API をまだサポートしていない LLM プロバイダーがほとんどであるため、Chat Completions API/モデルを使用しています。LLM プロバイダーが Responses をサポートしている場合は、Responses の使用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェント ごとに異なるモデルを使いたい場合があります。例えば、トリアージには小さく高速なモデルを使い、複雑なタスクにはより大きく高性能なモデルを使う、といった具合です。[`Agent`][agents.Agent] を構成する際、以下のいずれかで特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接指定する。

!!!note

    当社の SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形状をサポートしていますが、両者はサポートする機能やツールのセットが異なるため、各ワークフローでは 1 つのモデル形状の使用を推奨します。ワークフローでモデル形状を混在させる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

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

エージェント で使用するモデルをさらに構成したい場合は、[`ModelSettings`][agents.models.interface.ModelSettings] を渡すことで、temperature などのオプションのモデル構成 パラメーター を指定できます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使用する際、[他にもいくつかの任意 パラメーター](https://platform.openai.com/docs/api-reference/responses/create)（例: `user`、`service_tier` など）があります。トップレベルで指定できない場合は、`extra_args` を使って渡せます。

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

トレーシング に関連するエラーが発生する場合、トレースは OpenAI の サーバー にアップロードされ、OpenAI API キーを持っていないことが原因です。解決方法は次の 3 つです。

1. トレーシング を完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング 用に OpenAI キーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードのみに使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレース プロセッサーを使用する。詳しくは [tracing ドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだサポートしていません。その結果、404 などの問題が発生することがあります。解決するには、次の 2 つの方法があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出す。これは環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用する。code examples は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。この場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの弱点で、JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できないことがあります。現在これに対する修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することを推奨します。さもないと、不正な JSON によりアプリが頻繁に壊れる可能性があります。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差を把握していないと、エラーに遭遇する可能性があります。例えば、OpenAI は structured outputs、マルチモーダル入力、ホスト型 ファイル検索 と Web 検索 をサポートしますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制約に注意してください。

-   サポートしていない `tools` を理解しないプロバイダーに送らないでください
-   テキスト専用のモデルを呼び出す前に、マルチモーダル入力をフィルタリングしてください
-   structured JSON 出力をサポートしないプロバイダーは、時折無効な JSON を生成することがあります