---
search:
  exclude: true
---
# 通过 LiteLLM 使用任意模型

!!! note

    LiteLLM 集成处于测试阶段。您可能会在某些模型提供商（尤其是较小的提供商）上遇到问题。请通过 [Github issues](https://github.com/openai/openai-agents-python/issues) 报告，我们会尽快修复。

[LiteLLM](https://docs.litellm.ai/docs/) 是一个库，允许您通过统一接口使用 100 多个模型。我们在 Agents SDK 中加入了 LiteLLM 集成，以便您使用任意 AI 模型。

## 设置

您需要确保可用 `litellm`。可以通过安装可选的 `litellm` 依赖组来实现：

```bash
pip install "openai-agents[litellm]"
```

完成后，您可以在任意智能体中使用 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

## 示例

下面是一个可直接运行的示例。运行后，它会提示您输入模型名称和 API key。例如，您可以输入：

- `openai/gpt-4.1` 作为模型，并使用您的 OpenAI API key
- `anthropic/claude-3-5-sonnet-20240620` 作为模型，并使用您的 Anthropic API key
- 等等

有关 LiteLLM 支持的完整模型列表，请参见 [litellm providers docs](https://docs.litellm.ai/docs/providers)。

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

## 追踪用量数据

如果希望将 LiteLLM 的响应计入 Agents SDK 的使用指标，在创建智能体时传入 `ModelSettings(include_usage=True)`。

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

使用 `include_usage=True` 时，LiteLLM 请求会通过 `result.context_wrapper.usage` 报告 token 和请求计数，就像内置的 OpenAI 模型一样。