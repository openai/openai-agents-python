---
search:
  exclude: true
---
# 模型

Agents SDK 内置支持两种 OpenAI 模型：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，使用新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## OpenAI 模型

初始化 `Agent` 时如果没有指定模型，将使用默认模型。当前默认模型是 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1)，它在智能体工作流的可预测性和低延迟之间提供了良好的平衡。

如果你想切换到其他模型如 [`gpt-5`](https://platform.openai.com/docs/models/gpt-5)，请按照下一节的步骤操作。

### 默认 OpenAI 模型

如果你想为所有未设置自定义模型的智能体一致地使用特定模型，请在运行智能体前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 模型

当你以这种方式使用 GPT-5 的推理模型（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）时，SDK 默认应用合理的 `ModelSettings`。具体来说，它将 `reasoning.effort` 和 `verbosity` 都设置为 `"low"`。如果你想自己构建这些设置，请调用 `agents.models.get_default_model_settings("gpt-")`。

为了更低延迟或特定需求，你可以选择不同的模型和设置。要调整默认模型的推理强度，请传递你自己的 `ModelSettings`：

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    model_settings=ModelSettings(reasoning=Reasoning(effort="minimal"), verbosity="low")
    # 如果设置了 OPENAI_DEFAULT_MODEL=gpt-5，只传递 model_settings 即可。
    # 显式传递 GPT-5 模型名也是可以的：
    # model="gpt-5",
)
```

特别是对于更低延迟，使用 [`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) 模型配合 `reasoning.effort="minimal"` 通常会比默认设置更快地返回响应。然而，Responses API 中的一些内置工具（如文件搜索和图像生成）不支持 `"minimal"` 推理强度，这就是为什么 Agents SDK 默认使用 `"low"`。

#### 非 GPT-5 模型

如果你没有传递自定义 `model_settings` 而使用了非 GPT-5 模型名称，SDK 将回退到与任何模型兼容的通用 `ModelSettings`。

## 非 OpenAI 模型

你可以通过 [LiteLLM 集成](./litellm.md) 使用大多数其他非 OpenAI 模型。首先，安装 litellm 依赖组：

```bash
pip install "openai-agents[litellm]"
```

然后，使用 `litellm/` 前缀的任何[支持的模型](https://docs.litellm.ai/docs/providers)：

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 使用非 OpenAI 模型的其他方法

你有另外 3 种方式集成其他 LLM 提供商（示例[在此](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）：

1. [`set_default_openai_client`][agents.set_default_openai_client] 在你想全局使用 `AsyncOpenAI` 实例作为 LLM 客户端的情况下很有用。这适用于 LLM 提供商有 OpenAI 兼容 API 端点的情况，你可以设置 `base_url` 和 `api_key`。查看可配置示例 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 在 `Runner.run` 级别。这让你可以指定"此运行中的所有智能体都使用自定义模型提供商"。查看可配置示例 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 让你在特定智能体实例上指定模型。这让你可以为不同智能体混合匹配不同提供商。查看可配置示例 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。使用大多数可用模型的简单方法是通过 [LiteLLM 集成](./litellm.md)。

如果你没有来自 `platform.openai.com` 的 API 密钥，我们建议通过 `set_tracing_disabled()` 禁用追踪，或设置[不同的追踪处理器](../tracing.md)。

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为大多数 LLM 提供商还不支持 Responses API。如果你的 LLM 提供商支持它，我们推荐使用 Responses。

## 混合和匹配模型

在单个工作流中，你可能想为每个智能体使用不同的模型。例如，你可以使用更小、更快的模型进行分类，而对复杂任务使用更大、更有能力的模型。配置 [`Agent`][agents.Agent] 时，你可以通过以下方式选择特定模型：

1. 传递模型名称。
2. 传递任何模型名称 + 可以将该名称映射到模型实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!!note

    虽然我们的 SDK 支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形状，但我们建议为每个工作流使用单一模型形状，因为这两种形状支持不同的功能和工具集。如果你的工作流需要混合匹配模型形状，请确保你使用的所有功能在两者上都可用。

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

1.  直接设置 OpenAI 模型名称。
2.  提供 [`Model`][agents.models.interface.Model] 实现。

当你想进一步配置智能体使用的模型时，你可以传递 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供可选的模型配置参数如 temperature。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

另外，当你使用 OpenAI 的 Responses API 时，[还有其他一些可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们在顶层不可用，你也可以使用 `extra_args` 传递它们。

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

## 使用其他 LLM 提供商时的常见问题

### 追踪客户端错误 401

如果你遇到与追踪相关的错误，这是因为追踪被上传到 OpenAI 服务器，而你没有 OpenAI API 密钥。你有三个选项来解决这个问题：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI 密钥：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API 密钥将仅用于上传追踪，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 追踪处理器。参见 [追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但大多数其他 LLM 提供商还不支持它。你可能会因此看到 404 或类似问题。要解决此问题，你有两个选项：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果你通过环境变量设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，这将会生效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。示例在[这里](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### 结构化输出支持

一些模型提供商不支持[结构化输出](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致如下错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型提供商的缺陷 - 它们支持 JSON 输出，但不允许你指定输出使用的 `json_schema`。我们正在研究解决方案，但建议你依赖支持 JSON 模式输出的提供商，否则你的应用经常会因为格式错误的 JSON 而崩溃。

## 跨提供商混合模型

你需要了解模型提供商之间的功能差异，否则可能会遇到错误。例如，OpenAI 支持结构化输出、多模态输入以及托管文件搜索和网络搜索，但许多其他提供商不支持这些功能。请注意这些限制：

-   不要向不理解它们的提供商发送不受支持的 `tools`
-   在调用仅支持文本的模型前过滤掉多模态输入
-   注意不支持结构化 JSON 输出的提供商偶尔会生成无效的 JSON