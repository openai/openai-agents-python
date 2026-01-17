---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用地支持两种 OpenAI 模型用法：

- **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，使用全新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，使用 [Chat Completions API](https://平台.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## OpenAI 模型

当你在初始化一个 `Agent` 时未指定模型，将使用默认模型。当前默认是出于兼容性与低延迟考虑的 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1)。如果你有访问权限，建议将你的智能体设置为 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2)，以在保持显式 `model_settings` 的前提下获得更高质量。

如果你想切换到其他模型，比如 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2)，请按照下一节的步骤进行。

### 默认 OpenAI 模型

如果你希望对未设置自定义模型的所有智能体始终使用特定模型，请在运行智能体之前设置环境变量 `OPENAI_DEFAULT_MODEL`。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 模型

当你以这种方式使用任何 GPT-5 推理模型（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）时，SDK 会默认应用合理的 `ModelSettings`。具体来说，它会将 `reasoning.effort` 和 `verbosity` 都设置为 `"low"`。如果你想自行构建这些设置，请调用 `agents.models.get_default_model_settings("gpt-5")`。

出于更低延迟或特定需求，你可以选择不同的模型与设置。要为默认模型调整推理强度，请传入你自己的 `ModelSettings`：

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    model_settings=ModelSettings(reasoning=Reasoning(effort="minimal"), verbosity="low")
    # If OPENAI_DEFAULT_MODEL=gpt-5 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    # model="gpt-5",
)
```

如果特别追求低延迟，使用 [`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)，并将 `reasoning.effort="minimal"`，通常会比默认设置更快返回结果。但需要注意，Responses API 中的一些内置工具（如文件检索和图像生成）不支持 `"minimal"` 的推理强度，因此本 Agents SDK 默认使用 `"low"`。

#### 非 GPT-5 模型

如果你传入非 GPT-5 的模型名称且未提供自定义 `model_settings`，SDK 会回退到兼容任意模型的通用 `ModelSettings`。

## 非 OpenAI 模型

你可以通过 [LiteLLM 集成](./litellm.md) 使用大多数其他非 OpenAI 模型。首先，安装 litellm 依赖分组：

```bash
pip install "openai-agents[litellm]"
```

然后，使用带有 `litellm/` 前缀的任意[受支持模型](https://docs.litellm.ai/docs/providers)：

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 使用非 OpenAI 模型的其他方式

你还可以通过另外 3 种方式集成其他 LLM 提供商（示例见[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于你希望全局使用一个 `AsyncOpenAI` 实例作为 LLM 客户端的情况。适用于 LLM 提供商具备 OpenAI 兼容 API 端点且你可以设置 `base_url` 和 `api_key` 的场景。参见可配置示例：[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 作用于 `Runner.run` 级别。它允许你在一次运行中指定“为所有智能体使用自定义模型提供商”。参见可配置示例：[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许你在特定的 Agent 实例上指定模型。这样你可以为不同智能体混用不同提供商。参见可配置示例：[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。使用大多数可用模型的简便方式是通过 [LiteLLM 集成](./litellm.md)。

在你没有来自 `platform.openai.com` 的 API key 的情况下，建议通过 `set_tracing_disabled()` 禁用追踪，或设置[不同的追踪进程](../tracing.md)。

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为大多数 LLM 提供商尚未支持 Responses API。如果你的 LLM 提供商支持，建议使用 Responses。

## 混合搭配模型

在单个工作流中，你可能希望为每个智能体使用不同的模型。例如，你可以为分诊使用更小更快的模型，而为复杂任务使用更大更强的模型。配置 [`Agent`][agents.Agent] 时，你可以通过以下方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 一个可将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供一个 [`Model`][agents.models.interface.Model] 实现。

!!!note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形态，但我们建议在每个工作流中使用单一的模型形态，因为两者支持的功能和工具集不同。如果你的工作流需要混用不同模型形态，请确保你所使用的全部功能在两者上都可用。

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

1. 直接设置一个 OpenAI 模型的名称。
2. 提供一个 [`Model`][agents.models.interface.Model] 实现。

当你希望进一步配置智能体所用的模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供温度等可选的模型配置参数。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

此外，当你使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们不在顶层可用，你可以通过 `extra_args` 一并传入。

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

## 使用其他 LLM 提供商的常见问题

### 追踪客户端错误 401

如果你遇到与追踪相关的错误，这是因为追踪数据会上传至 OpenAI 服务，而你没有 OpenAI API key。你有三种解决方案：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置一个 OpenAI key：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API key 仅用于上传追踪数据，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 的追踪进程。参见[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但大多数其他 LLM 提供商尚未支持。因此你可能会看到 404 或类似问题。为解决此问题，你有两种选择：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果你通过环境变量设置了 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，这将有效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。相关示例[在此](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### Structured outputs 支持

有些模型提供商不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似如下的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型提供商的不足之处——它们支持 JSON 输出，但不允许你为输出指定 `json_schema`。我们正在努力修复，但建议优先依赖支持 JSON schema 输出的提供商，否则你的应用可能会因 JSON 格式错误而经常出现问题。

## 跨提供商混合模型

你需要注意不同模型提供商之间的功能差异，否则可能会遇到错误。例如，OpenAI 支持 structured outputs、多模态输入，以及托管的文件检索与网络检索，但许多其他提供商不支持这些功能。请注意以下限制：

- 不要向不理解的提供商发送不支持的 `tools`
- 在调用仅支持文本的模型前，过滤掉多模态输入
- 注意不支持结构化 JSON 输出的提供商可能会偶尔生成无效的 JSON。