---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用地支持两种 OpenAI 模型调用方式：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，使用全新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## OpenAI 模型

当你在初始化一个 `Agent` 时未指定模型，将使用默认模型。当前默认是为兼容性与低延迟而设定的 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1)。如果你有权限，建议将你的智能体设置为 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 以获得更高质量，同时显式保留 `model_settings`。

如果你想切换到其他模型（如 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2)），请按照下一节的步骤操作。

### 默认 OpenAI 模型

如果你希望对所有未自定义模型的智能体始终使用同一特定模型，请在运行智能体前设置环境变量 `OPENAI_DEFAULT_MODEL`。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 模型

当你以这种方式使用任意 GPT-5 推理模型（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）时，SDK 默认会应用合理的 `ModelSettings`。具体而言，会将 `reasoning.effort` 和 `verbosity` 都设置为 `"low"`。如果你希望自行构建这些设置，可调用 `agents.models.get_default_model_settings("gpt-5")`。

为更低延迟或特定需求，你可以选择不同的模型与设置。要为默认模型调整推理强度，请传入你自己的 `ModelSettings`：

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

特别是为了更低延迟，使用 [`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) 并设置 `reasoning.effort="minimal"`，通常会比默认设置更快返回结果。但需要注意，Responses API 中的一些内置工具（如 文件检索 和 图像生成）不支持 `"minimal"` 推理强度，这也是本 Agents SDK 默认使用 `"low"` 的原因。

#### 非 GPT-5 模型

如果你传入的是非 GPT-5 的模型名称，并且未提供自定义 `model_settings`，SDK 将回退到适用于任意模型的通用 `ModelSettings`。

## 非 OpenAI 模型

你可以通过 [LiteLLM 集成](./litellm.md) 使用多数其他非 OpenAI 模型。首先，安装 litellm 依赖组：

```bash
pip install "openai-agents[litellm]"
```

然后，使用带有 `litellm/` 前缀的任一[受支持模型](https://docs.litellm.ai/docs/providers)：

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 使用非 OpenAI 模型的其他方式

你还可以通过另外 3 种方式集成其他 LLM 提供商（示例见[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于你希望在全局范围内使用 `AsyncOpenAI` 实例作为 LLM 客户端的情况。该方式用于 LLM 提供商具有 OpenAI 兼容 API 端点的情形，你可以设置 `base_url` 和 `api_key`。可参考 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) 中的可配置示例。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 作用于 `Runner.run` 层面。你可以声明“在此运行中为所有智能体使用自定义模型提供商”。可参考 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) 中的可配置示例。
3. [`Agent.model`][agents.agent.Agent.model] 允许你在特定的 Agent 实例上指定模型。这使你可以为不同的智能体混合使用不同的提供商。可参考 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) 中的可配置示例。使用多数可用模型的简便方式是通过 [LiteLLM 集成](./litellm.md)。

在你没有来自 `platform.openai.com` 的 API key 的情况下，建议通过 `set_tracing_disabled()` 禁用 追踪，或设置[不同的追踪进程](../tracing.md)。

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为多数 LLM 提供商尚不支持 Responses API。如果你的 LLM 提供商支持，建议使用 Responses。

## 混合与匹配模型

在单个工作流中，你可能希望为每个智能体使用不同模型。例如，可以为分诊使用更小更快的模型，而为复杂任务使用更大更强的模型。配置 [`Agent`][agents.Agent] 时，你可以通过以下方式之一选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 一个可将该名称映射为 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供一个 [`Model`][agents.models.interface.Model] 实现。

!!!note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形态，但我们建议在每个工作流中仅使用一种形态，因为二者支持的功能与工具集不同。如果你的工作流需要混合使用不同形态，请确保你所用的全部功能在两者中都可用。

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

1.  直接设置一个 OpenAI 模型的名称。
2.  提供一个 [`Model`][agents.models.interface.Model] 实现。

当你希望进一步配置智能体所用的模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供可选的模型配置参数，如 temperature。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

此外，使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（如 `user`、`service_tier` 等）。如果它们不在顶层可用，你可以通过 `extra_args` 传入。

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

### Tracing 客户端错误 401

如果你遇到与 追踪 相关的错误，这是因为追踪数据会上传到 OpenAI 服务，而你没有 OpenAI API key。你有三种解决方案：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置一个 OpenAI key：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。该 API key 只用于上传追踪数据，并且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 的追踪进程。参见[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但多数其他 LLM 提供商尚不支持。因此你可能会看到 404 或类似问题。解决方法有两种：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。当你通过环境变量设置了 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 时有效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。可在[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)找到示例。

### Structured outputs 支持

某些模型提供商不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似如下的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是部分模型提供商的不足——它们支持 JSON 输出，但不允许你为输出指定要使用的 `json_schema`。我们正在努力修复这一点，但建议优先使用支持 JSON schema 输出的提供商，否则你的应用常会因 JSON 不符合格式良好的数据 而出错。

## 跨提供商混合模型

你需要了解不同模型提供商之间的功能差异，否则可能会遇到错误。例如，OpenAI 支持 structured outputs、多模态输入，以及由OpenAI托管的工具中的文件检索与 网络检索，但许多其他提供商并不支持这些功能。请注意以下限制：

-   不要将不受支持的 `tools` 发送给无法理解它们的提供商
-   在调用仅支持文本的模型前过滤掉多模态输入
-   注意不支持结构化 JSON 输出的提供商可能会偶尔生成无效 JSON。