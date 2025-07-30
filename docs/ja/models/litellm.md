---
search:
  exclude: true
---
# LiteLLM 経由で任意のモデルを使用する

!!! note

    LiteLLM 統合は現在ベータ版です。特に小規模なモデルプロバイダーで問題が発生する可能性があります。問題を見つけた場合は [GitHub の issues](https://github.com/openai/openai-agents-python/issues) でご報告ください。迅速に対応します。

[LiteLLM](https://docs.litellm.ai/docs/) は、単一のインターフェースで 100+ のモデルを利用できるライブラリです。 Agents SDK では、あらゆる AI モデルを利用できるように LiteLLM 統合を追加しました。

## セットアップ

`litellm` が利用可能であることを確認してください。オプションの `litellm` 依存関係グループをインストールすることで導入できます：

```bash
pip install "openai-agents[litellm]"
```

インストール後は、どの エージェント でも [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を使用できます。

## 例

これは完全に動作するサンプルです。実行すると、モデル名と API キーの入力を求められます。たとえば次のように入力できます。

-   モデルには `openai/gpt-4.1`、 OpenAI API キー
-   モデルには `anthropic/claude-3-5-sonnet-20240620`、 Anthropic API キー
-   など

LiteLLM がサポートするモデルの全一覧は、[litellm プロバイダー ドキュメント](https://docs.litellm.ai/docs/providers) をご覧ください。

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