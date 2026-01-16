---
search:
  exclude: true
---
# 基于 LiteLLM 的通用模型使用

!!! note

    LiteLLM 集成处于测试阶段。你在使用部分模型提供方（尤其是规模较小的）时可能会遇到问题。请通过 [Github issues](https://github.com/openai/openai-agents-python/issues) 报告问题，我们会尽快修复。

[LiteLLM](https://docs.litellm.ai/docs/) 是一个库，可通过统一接口使用 100+ 模型。我们在 Agents SDK 中加入了 LiteLLM 集成，让你可以使用任意 AI 模型。

## 设置

你需要确保可用的 `litellm`。可通过安装可选的 `litellm` 依赖组来完成：

```bash
pip install "openai-agents[litellm]"
```

完成后，你可以在任意智能体中使用 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

## 示例

这是一个可直接运行的示例。运行后会提示你输入模型名称和 API Key。例如，你可以输入：

- `openai/gpt-4.1` 作为模型，并提供你的 OpenAI API Key
- `anthropic/claude-3-5-sonnet-20240620` 作为模型，并提供你的 Anthropic API Key
- 等等

LiteLLM 支持的完整模型列表见 [litellm providers docs](https://docs.litellm.ai/docs/providers)。

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

## 用量数据追踪

如果你希望将 LiteLLM 的响应纳入 Agents SDK 的用量指标，在创建智能体时传入 `ModelSettings(include_usage=True)`。

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

在 `include_usage=True` 的情况下，LiteLLM 请求会通过 `result.context_wrapper.usage` 报告 token 与请求计数，与内置的 OpenAI 模型一致。

## 故障排查

如果你在 LiteLLM 响应中看到来自 Pydantic 的序列化警告，可通过设置以下选项启用一项小的兼容性补丁：

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

该可选标志会抑制已知的 LiteLLM 序列化警告，同时保持正常行为。如果不需要，可将其关闭（不设置或设为 `false`）。