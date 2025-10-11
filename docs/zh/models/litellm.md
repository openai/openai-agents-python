---
search:
  exclude: true
---
# 通过 LiteLLM 使用任意模型

!!! note

    LiteLLM 集成处于测试阶段。特别是小型模型提供商可能会遇到问题。如果有问题，请通过 [GitHub issues](https://github.com/openai/openai-agents-python/issues) 报告，我们会快速修复。

[LiteLLM](https://docs.litellm.ai/docs/) 是一个允许你通过单一接口使用 100+ 模型的库。我们在 Agents SDK 中添加了 LiteLLM 集成，让你可以使用任何 AI 模型。

## 设置

你需要确保 `litellm` 可用。你可以通过安装可选的 `litellm` 依赖组来实现这一点：

```bash
pip install "openai-agents[litellm]"
```

完成后，你可以在任何智能体中使用 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

## 例

这是一个完全工作的示例。当你运行它时，系统会要求你输入模型名称和 API 密钥。例如，你可以输入：

- 模型输入 `openai/gpt-4.1`，以及你的 OpenAI API 密钥
- 模型输入 `anthropic/claude-3-5-sonnet-20240620`，以及你的 Anthropic API 密钥
- 等等

有关 LiteLLM 支持的完整模型列表，请参见 [litellm 提供商文档](https://docs.litellm.ai/docs/providers)。

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

## 跟踪使用数据

如果你想让 LiteLLM 响应填充 Agents SDK 的使用指标，请在创建智能体时传递 `ModelSettings(include_usage=True)`。

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

使用 `include_usage=True` 时，LiteLLM 请求会通过 `result.context_wrapper.usage` 报告令牌和请求计数，就像内置的 OpenAI 模型一样。