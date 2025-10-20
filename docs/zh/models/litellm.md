---
search:
  exclude: true
---
# 通过 LiteLLM 使用任意模型

!!! note

    LiteLLM 集成目前为测试版。你可能会在使用某些模型提供商时遇到问题，特别是较小的提供商。请通过 [GitHub 问题](https://github.com/openai/openai-agents-python/issues)反馈，我们会尽快修复。

[LiteLLM](https://docs.litellm.ai/docs/) 是一个库，允许你通过统一接口使用 100+ 个模型。我们已添加对 LiteLLM 的集成，以便你在 Agents SDK 中使用任意 AI 模型。

## 设置

你需要确保可用 `litellm`。你可以通过安装可选的 `litellm` 依赖组来实现：

```bash
pip install "openai-agents[litellm]"
```

完成后，你可以在任意智能体中使用 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

## 示例

这是一个可直接运行的示例。运行后会提示输入模型名称和 API key。例如，你可以输入：

-   模型使用 `openai/gpt-4.1`，并提供你的 OpenAI API key
-   模型使用 `anthropic/claude-3-5-sonnet-20240620`，并提供你的 Anthropic API key
-   等等

关于 LiteLLM 支持的完整模型列表，请参见 [litellm providers 文档](https://docs.litellm.ai/docs/providers)。

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

## 使用数据追踪

如果你希望 LiteLLM 的响应填充到 Agents SDK 的使用指标中，请在创建智能体时传入 `ModelSettings(include_usage=True)`。

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

启用 `include_usage=True` 后，LiteLLM 请求将通过 `result.context_wrapper.usage` 报告 token 和请求计数，与内置的 OpenAI 模型一致。