---
search:
  exclude: true
---
# LiteLLM 経由で任意モデルの利用

!!! note

    LiteLLM 統合は現在ベータ版です。特に小規模なモデルプロバイダーでは問題が発生する可能性があります。問題を見つけた場合は [Github issues](https://github.com/openai/openai-agents-python/issues) からご報告ください。迅速に対応します。

[LiteLLM](https://docs.litellm.ai/docs/) は、単一のインターフェースで 100 以上のモデルを利用できるライブラリです。Agents SDK に LiteLLM 統合を追加したことで、任意の AI モデルを使用できるようになりました。

## セットアップ

`litellm` が利用可能であることを確認する必要があります。オプションの `litellm` 依存グループをインストールすることで準備できます。

```bash
pip install "openai-agents[litellm]"
```

インストール後、任意のエージェントで [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を使用できます。

## 例

以下は完全に動作する例です。実行すると、モデル名と API キーの入力を求められます。たとえば、次のように入力できます。

-   モデルに `openai/gpt-4.1`、API キーに OpenAI API キー
-   モデルに `anthropic/claude-3-5-sonnet-20240620`、API キーに Anthropic API キー
-   など

LiteLLM がサポートしているモデルの一覧は、[litellm providers docs](https://docs.litellm.ai/docs/providers) をご覧ください。

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