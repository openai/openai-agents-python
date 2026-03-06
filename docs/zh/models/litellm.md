---
search:
  exclude: true
---
# 通过 LiteLLM 使用任意模型

!!! note

    LiteLLM 集成处于测试版。您可能会在某些模型提供商（尤其是较小的提供商）上遇到问题。请通过 [Github issues](https://github.com/openai/openai-agents-python/issues) 报告问题，我们会尽快修复。

[LiteLLM](https://docs.litellm.ai/docs/) 是一个库，允许您通过统一接口使用 100+ 款模型。我们在 Agents SDK 中加入了 LiteLLM 集成，以便您使用任意 AI 模型。

## 设置

您需要确保可用 `litellm`。可以通过安装可选的 `litellm` 依赖组实现：

```bash
pip install "openai-agents[litellm]"
```

完成后，您可以在任意智能体中使用 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

## 示例

这是一个可直接运行的示例。运行时会提示输入模型名称和 API 密钥。例如，您可以输入：

-   模型使用 `openai/gpt-4.1`，并提供您的 OpenAI API 密钥
-   模型使用 `anthropic/claude-3-5-sonnet-20240620`，并提供您的 Anthropic API 密钥
-   等等

有关 LiteLLM 支持的完整模型列表，请参见 [litellm providers 文档](https://docs.litellm.ai/docs/providers)。

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

如果希望 LiteLLM 的响应填充 Agents SDK 的使用指标，请在创建智能体时传入 `ModelSettings(include_usage=True)`。

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

使用 `include_usage=True` 后，LiteLLM 请求会通过 `result.context_wrapper.usage` 报告 token 和请求计数，与内置的 OpenAI 模型一致。

## 疑难解答

如果您看到来自 LiteLLM 响应的 Pydantic 序列化器警告，请通过设置下述选项启用一个小型兼容性补丁：

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

该自选开关会抑制已知的 LiteLLM 序列化器警告，同时保持正常行为。如果不需要，可将其关闭（未设置或 `false`）。