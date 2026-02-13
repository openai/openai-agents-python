---
search:
  exclude: true
---
# モデル

Agents SDK には、OpenAI モデルを 2 つの形でそのまま使えるサポートが用意されています。

-   **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使って OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使って OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## OpenAI モデル

`Agent` を初期化する際にモデルを指定しない場合、デフォルトのモデルが使用されます。現在のデフォルトは、互換性と低レイテンシのために [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) です。アクセスできる場合は、明示的な `model_settings` を維持したまま品質を高めるために、エージェントを [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) に設定することを推奨します。

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) のような他のモデルに切り替えたい場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使いたい場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

次に、`RunConfig` で実行ごとのデフォルトモデルを設定することもできます。エージェント側でモデルを設定していない場合、この実行のモデルが使用されます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.2"),
)
```

#### GPT-5.x モデル

この方法で [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) などの任意の GPT-5.x モデルを使うと、SDK はデフォルトの `ModelSettings` を適用します。これは多くのユースケースで最適に動く設定になっています。デフォルトモデルの推論努力 (reasoning effort) を調整したい場合は、自分の `ModelSettings` を渡します。

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.2 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5.x model name explicitly:
    model="gpt-5.2",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

低レイテンシのためには、`gpt-5.2` で `reasoning.effort="none"` を使うことが推奨です。gpt-4.1 ファミリー (mini および nano バリアントを含む) も、インタラクティブなエージェントアプリを構築するうえで引き続き堅実な選択肢です。

#### 非 GPT-5 モデル

カスタムの `model_settings` なしで非 GPT-5 のモデル名を渡すと、SDK は任意のモデルと互換性のある汎用 `ModelSettings` に戻します。

## 非 OpenAI モデル

[LiteLLM 連携](./litellm.md) を介して、ほとんどの非 OpenAI モデルを使用できます。まず、litellm の依存関係グループをインストールします。

```bash
pip install "openai-agents[litellm]"
```

次に、`litellm/` プレフィックスを付けて、任意の [対応モデル](https://docs.litellm.ai/docs/providers) を使用します。

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 非 OpenAI モデルを使う他の方法

他の LLM プロバイダーは、さらに 3 つの方法で統合できます (例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) )。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使いたい場合に便利です。これは、LLM プロバイダーに OpenAI 互換の API エンドポイントがあり、`base_url` と `api_key` を設定できる場合向けです。設定可能な例は [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルです。これにより「この実行内のすべてのエージェントに対してカスタムのモデルプロバイダーを使う」と指定できます。設定可能な例は [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] により、特定の `Agent` インスタンスでモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせて使えます。設定可能な例は [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。利用可能なほとんどのモデルを簡単に使う方法は [LiteLLM 連携](./litellm.md) です。

`platform.openai.com` の API キーを持っていない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md) を設定することを推奨します。

!!! note

    これらの例では Chat Completions API/モデルを使用しています。これは、ほとんどの LLM プロバイダーがまだ Responses API をサポートしていないためです。LLM プロバイダーが対応している場合は、Responses の使用を推奨します。

## モデルの組み合わせ

1 つのワークフロー内で、エージェントごとに異なるモデルを使いたい場合があります。たとえば、トリアージには小さく高速なモデルを使い、複雑なタスクには大きく高性能なモデルを使う、といった形です。[`Agent`][agents.Agent] を設定する際、次のいずれかで特定のモデルを選択できます。

1. モデル名を渡す。
2. 任意のモデル名 + その名前を Model インスタンスにマップできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡す。
3. [`Model`][agents.models.interface.Model] 実装を直接渡す。

!!!note

    当社の SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形をサポートしていますが、2 つの形はサポートする機能とツールの集合が異なるため、ワークフローごとに 1 つのモデル形を使うことを推奨します。ワークフローでモデル形を混在させる必要がある場合は、使用しているすべての機能が両方で利用可能であることを確認してください。

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

エージェントで使用するモデルをさらに設定したい場合は、temperature などの任意のモデル設定パラメーターを提供する [`ModelSettings`][agents.models.interface.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

また、OpenAI の Responses API を使う場合、[他にもいくつかの任意パラメーター](https://platform.openai.com/docs/api-reference/responses/create) (例: `user`、`service_tier` など) があります。トップレベルで利用できない場合は、`extra_args` を使ってそれらも渡せます。

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

### トレーシングクライアントエラー 401

トレーシング関連のエラーが出る場合、これはトレースが OpenAI サーバーにアップロードされる一方で、OpenAI API キーがないことが原因です。解決するには 3 つの選択肢があります。

1. トレーシングを完全に無効化する: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. トレーシング用に OpenAI のキーを設定する: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) のものが必要です。
3. 非 OpenAI のトレースプロセッサーを使う。[トレーシングドキュメント](../tracing.md#custom-tracing-processors) を参照してください。

### Responses API のサポート

SDK はデフォルトで Responses API を使用しますが、ほとんどの他の LLM プロバイダーはまだ対応していません。その結果、404 などの問題が発生する場合があります。解決するには 2 つの選択肢があります。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。例は [こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) です。

### structured outputs のサポート

一部のモデルプロバイダーは [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) をサポートしていません。これにより、次のようなエラーになることがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できません。私たちはこの修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーに依存することを推奨します。そうでない場合、不正な形式の JSON によってアプリが頻繁に壊れるためです。

## プロバイダーをまたいだモデルの混在

モデルプロバイダー間の機能差を意識する必要があります。そうしないとエラーに遭遇する可能性があります。たとえば OpenAI は structured outputs、マルチモーダル入力、ホストされた ファイル検索 と Web 検索 をサポートしていますが、多くの他プロバイダーはこれらをサポートしていません。次の制限に注意してください。

-   対応していないプロバイダーに、理解できない `tools` を送らない
-   テキスト専用のモデルを呼び出す前に、マルチモーダル入力をフィルタリングする
-   構造化された JSON 出力をサポートしないプロバイダーは、ときどき無効な JSON を生成することがある点に注意する