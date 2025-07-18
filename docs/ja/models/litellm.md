---
search:
  exclude: true
---
# LiteLLM 経由の任意モデル利用

!!! note

    LiteLLM との統合は現在 beta 版です。特に小規模なモデルプロバイダーでは問題が発生する場合があります。問題を見つけた際は [Github issues](https://github.com/openai/openai-agents-python/issues) からご報告ください。迅速に対応いたします。

[LiteLLM](https://docs.litellm.ai/docs/) は、1 つのインターフェースで 100+ のモデルを利用できるライブラリです。Agents SDK に LiteLLM との統合を追加したことで、任意の AI モデルを使用できるようになりました。

## Setup

`litellm` が利用可能であることを確認する必要があります。オプションの `litellm` 依存グループをインストールすることで設定できます:

```bash
pip install "openai-agents[litellm]"
```

インストールが完了すると、任意のエージェントで [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を使用できます。

## Example

以下は完全に動作する例です。実行するとモデル名と API キーの入力を求められます。例えば、次のように入力できます。

-   `openai/gpt-4.1` をモデルに指定し、OpenAI API キーを入力
-   `anthropic/claude-3-5-sonnet-20240620` をモデルに指定し、Anthropic API キーを入力
-   など

LiteLLM でサポートされているモデルの全リストは [litellm providers docs](https://docs.litellm.ai/docs/providers) をご覧ください。

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