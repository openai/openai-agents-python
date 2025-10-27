---
search:
  exclude: true
---
# LiteLLM 経由での任意モデルの利用

!!! note

    LiteLLM 連携はベータ版です。特に小規模なモデルプロバイダーでは問題が発生する可能性があります。問題があれば [GitHub issues](https://github.com/openai/openai-agents-python/issues) からご報告ください。迅速に対応します。

[LiteLLM](https://docs.litellm.ai/docs/) は、単一のインターフェースで 100+ のモデルを利用できるライブラリです。Agents SDK に LiteLLM 連携を追加し、任意の AI モデルを利用できるようにしました。

## セットアップ

`litellm` を利用可能にする必要があります。オプションの `litellm` 依存関係グループをインストールしてください。

```bash
pip install "openai-agents[litellm]"
```

完了したら、任意の エージェント で [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を使用できます。

## 例

これは完全に動作する例です。実行すると、モデル名と API キーの入力を求められます。例えば次のように入力できます。

-   モデルに `openai/gpt-4.1`、API キーにあなたの OpenAI API キー
-   モデルに `anthropic/claude-3-5-sonnet-20240620`、API キーにあなたの Anthropic API キー
-   など

LiteLLM でサポートされているモデルの一覧は、[litellm のプロバイダードキュメント](https://docs.litellm.ai/docs/providers) を参照してください。

```python
from __future__ import annotations

import asyncio

from agents import Agent, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

@function_tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main(model: str, api_key: str):
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model=LitellmModel(model=model, api_key=api_key),
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    # First try to get model/api key from args
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, required=False)
    parser.add_argument("--api-key", type=str, required=False)
    args = parser.parse_args()

    model = args.model
    if not model:
        model = input("Enter a model name for Litellm: ")

    api_key = args.api_key
    if not api_key:
        api_key = input("Enter an API key for Litellm: ")

    asyncio.run(main(model, api_key))
```

## 使用状況データのトラッキング

LiteLLM のレスポンスで Agents SDK の使用状況メトリクスを埋めたい場合は、エージェント作成時に `ModelSettings(include_usage=True)` を渡してください。

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

`include_usage=True` の場合、LiteLLM のリクエストは、組み込みの OpenAI モデルと同様に、`result.context_wrapper.usage` を通じてトークン数とリクエスト数を報告します。