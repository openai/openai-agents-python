---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用地支持两种 OpenAI 模型：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，通过新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，通过 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## OpenAI 模型

当你在初始化一个 `Agent` 时未指定模型，将使用默认模型。当前默认是 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1)，在智能体工作流的可预测性与低延迟之间提供了良好平衡。

如果你想切换到其他模型（如 [`gpt-5`](https://platform.openai.com/docs/models/gpt-5)），请按照下一节中的步骤操作。

### 默认 OpenAI 模型

如果你希望在所有未设置自定义模型的智能体中始终使用某个特定模型，请在运行智能体之前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 模型

当你以这种方式使用任一 GPT-5 推理模型（[`gpt-5`](https://platform.openai.com/docs/models/gpt-5)、[`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano)）时，SDK 会默认应用合理的 `ModelSettings`。具体来说，它会将 `reasoning.effort` 和 `verbosity` 都设置为 `"low"`。如果你想自行构建这些设置，请调用 `agents.models.get_default_model_settings("gpt-5")`。

出于更低延迟或特定需求的考虑，你可以选择不同的模型和设置。要调整默认模型的推理强度，请传入你自己的 `ModelSettings`：

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

若特别追求低延迟，使用 [`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 或 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) 并设置 `reasoning.effort="minimal"`，通常会比默认设置更快返回结果。但需注意，Responses API 中的一些内置工具（如 文件检索 和 图像生成）不支持 `"minimal"` 推理强度，因此本 Agents SDK 将默认设置为 `"low"`。

#### 非 GPT-5 模型

如果你传入一个非 GPT-5 的模型名且未提供自定义 `model_settings`，SDK 会回退到与任意模型兼容的通用 `ModelSettings`。

## 非 OpenAI 模型

你可以通过 [LiteLLM 集成](./litellm.md)使用大多数其他非 OpenAI 模型。首先，安装 litellm 依赖组：

```bash
pip install "openai-agents[litellm]"
```

然后，使用带有 `litellm/` 前缀的任一[受支持模型](https://docs.litellm.ai/docs/providers)：

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 使用非 OpenAI 模型的其他方式

你还可以通过另外 3 种方式集成其他 LLM 提供商（code examples [在此](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于你希望在全局使用一个 `AsyncOpenAI` 实例作为 LLM 客户端的情况。适用于 LLM 提供商具有与 OpenAI 兼容的 API 端点、并且你可以设置 `base_url` 和 `api_key` 的场景。可参考此可配置示例：[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 层级。它允许你指定“在此次运行中为所有智能体使用自定义模型提供商”。可参考此可配置示例：[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许你在特定的 Agent 实例上指定模型。这使你能够为不同智能体混用不同的提供商。可参考此可配置示例：[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。使用大多数可用模型的简便方式是通过 [LiteLLM 集成](./litellm.md)。

如果你没有来自 `platform.openai.com` 的 API key，建议通过 `set_tracing_disabled()` 禁用追踪，或设置一个[不同的追踪进程](../tracing.md)。

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为大多数 LLM 提供商尚不支持 Responses API。如果你的 LLM 提供商支持，建议使用 Responses。

## 混合与搭配模型

在单个工作流中，你可能希望为每个智能体使用不同的模型。例如，可以为分诊使用更小更快的模型，而为复杂任务使用更大更强的模型。在配置 [`Agent`][agents.Agent] 时，你可以通过以下方式之一选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 一个可将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供一个 [`Model`][agents.models.interface.Model] 实现。

!!!note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形态，但我们建议在每个工作流中使用单一模型形态，因为这两种形态支持的功能和工具集不同。如果你的工作流需要混用不同模型形态，请确保你所使用的所有功能在两种形态上都可用。

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

当你希望进一步配置智能体所用模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，其中提供 temperature 等可选的模型配置参数。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

此外，当你使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们不在顶层可用，你可以使用 `extra_args` 传入。

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

如果你遇到与追踪相关的错误，这是因为追踪会上传到 OpenAI 服务，而你没有 OpenAI API key。你有三种解决方案：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置一个 OpenAI key：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API key 仅用于上传追踪，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 的追踪进程。参见[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但大多数其他 LLM 提供商尚不支持它。因此你可能会看到 404 或类似问题。要解决此问题，你有两个选项：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果你通过环境变量设置了 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，这将有效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。code examples [在此](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### Structured outputs 支持

一些模型提供商不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致如下错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是部分模型提供商的不足——它们支持 JSON 输出，但不允许你为输出指定要使用的 `json_schema`。我们正在努力修复，但建议依赖支持 JSON schema 输出的提供商，否则你的应用经常会因为 JSON 格式不正确而出错。

## 跨提供商混用模型

你需要了解不同模型提供商之间的功能差异，否则可能会遇到错误。比如，OpenAI 支持 structured outputs、多模态输入，以及托管的 文件检索 和 网络检索，但许多其他提供商并不支持这些功能。请注意以下限制：

-   不要向不支持的提供商发送其无法理解的 `tools`
-   在调用仅支持文本的模型之前，过滤掉多模态输入
-   注意不支持结构化 JSON 输出的提供商偶尔会生成无效的 JSON。