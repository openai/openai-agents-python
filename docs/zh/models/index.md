---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用地支持两种 OpenAI 模型形态：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，通过新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，通过 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## OpenAI 模型

当你在初始化 `Agent` 时未指定模型，会使用默认模型。出于兼容性与低延迟考虑，当前默认是 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1)。如果你有权限，我们建议在保持显式 `model_settings` 的同时，将你的智能体设置为 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 以获得更高质量。

如果你想切换到诸如 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 等其他模型，有两种方式来配置你的智能体。

### 默认模型

首先，如果你希望对所有未设置自定义模型的智能体始终使用某个特定模型，请在运行智能体之前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

其次，你可以通过 `RunConfig` 为一次运行设置默认模型。如果你没有为某个智能体设置模型，则会使用本次运行的模型。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.2"),
)
```

#### GPT-5.x 模型

当你以这种方式使用任意 GPT-5.x 模型（例如 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2)）时，SDK 会应用默认的 `ModelSettings`，并设置对大多数用例效果最好的选项。要调整默认模型的推理强度，请传入你自己的 `ModelSettings`：

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.2 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5.x model name explicitly:
    model="gpt-5.2",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

为了更低延迟，推荐在 `gpt-5.2` 中使用 `reasoning.effort="none"`。gpt-4.1 系列（包括 mini 与 nano 变体）也仍然是构建交互式智能体应用的稳健选择。

#### 非 GPT-5 模型

如果你传入非 GPT-5 的模型名称且未提供自定义 `model_settings`，SDK 会回退到与任意模型兼容的通用 `ModelSettings`。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用 OpenAI 支持的模型时，你可以选择启用 websocket 传输。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI provider 解析的 OpenAI Responses 模型（包括诸如 `"gpt-5.2"` 这类字符串模型名）。

你也可以按 provider 或按每次运行配置 websocket 传输：

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

如果你需要基于前缀的模型路由（例如在一次运行中混用 `openai/...` 与 `litellm/...` 模型名），请使用 [`MultiProvider`][agents.MultiProvider]，并在其中改为设置 `openai_use_responses_websocket=True`。

注意事项：

-   这是通过 websocket 传输的 Responses API，而不是 [Realtime API](../realtime/guide.md)。
-   如果你的环境中尚未安装 `websockets` 包，请安装它。
-   启用 websocket 传输后，你可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于你希望在多轮流程中复用同一 websocket 连接（以及嵌套的 Agents-as-tools 调用）的场景，推荐使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。参见 [运行智能体](../running_agents.md) 指南以及 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。

## 非 OpenAI 模型

你可以通过 [LiteLLM 集成](./litellm.md) 使用大多数其他非 OpenAI 模型。首先，安装 litellm 依赖组：

```bash
pip install "openai-agents[litellm]"
```

然后，使用带 `litellm/` 前缀的任意[受支持模型](https://docs.litellm.ai/docs/providers)：

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 使用非 OpenAI 模型的其他方式

你还可以通过另外 3 种方式集成其他 LLM provider（示例见[这里](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）：

1. [`set_default_openai_client`][agents.set_default_openai_client]：适用于你希望在全局使用某个 `AsyncOpenAI` 实例作为 LLM client 的场景。用于 LLM provider 提供 OpenAI 兼容 API endpoint 的情况，你可以设置 `base_url` 与 `api_key`。参见可配置示例：[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider]：位于 `Runner.run` 层级。它允许你“为本次运行中的所有智能体使用自定义模型 provider”。参见可配置示例：[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model]：允许你在某个特定 Agent 实例上指定模型。这使你能够为不同智能体混用不同 provider。参见可配置示例：[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。使用大多数可用模型的简单方式是通过 [LiteLLM 集成](./litellm.md)。

在你没有来自 `platform.openai.com` 的 API key 的情况下，我们建议通过 `set_tracing_disabled()` 禁用追踪，或设置一个[不同的追踪进程](../tracing.md)。

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为多数 LLM provider 还不支持 Responses API。如果你的 LLM provider 支持它，我们建议使用 Responses。

## 混用模型

在单个工作流中，你可能希望为每个智能体使用不同模型。例如，你可以用更小、更快的模型做分流（triage），同时用更大、更强的模型处理复杂任务。在配置 [`Agent`][agents.Agent] 时，你可以通过以下方式之一选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 一个能将该名称映射为 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供一个 [`Model`][agents.models.interface.Model] 实现。

!!!note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 与 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 这两种形态，但我们建议在每个工作流中只使用一种模型形态，因为两种形态支持的特性与工具集合不同。如果你的工作流确实需要混用模型形态，请确保你使用的所有特性在两者上都可用。

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

1.  直接设置一个 OpenAI 模型名称。
2.  提供一个 [`Model`][agents.models.interface.Model] 实现。

当你希望进一步配置某个智能体使用的模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供温度（temperature）等可选模型配置参数。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

另外，当你使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们在顶层不可用，你也可以使用 `extra_args` 传入。

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

## 使用其他 LLM provider 的常见问题

### 追踪 client 错误 401

如果你遇到与追踪相关的错误，这是因为 trace 会上传到 OpenAI 服务器，而你没有 OpenAI API key。你有三种解决方式：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI key：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。该 API key 只用于上传 trace，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 的追踪处理器。参见[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但多数其他 LLM provider 还不支持它，因此你可能会看到 404 或类似问题。为解决此问题，你有两种选择：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。这在你通过环境变量设置 `OPENAI_API_KEY` 与 `OPENAI_BASE_URL` 时有效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。示例见[这里](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### structured outputs 支持

一些模型 provider 不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似如下的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型 provider 的不足之处——它们支持 JSON 输出，但不允许你为输出指定要使用的 `json_schema`。我们正在修复这一问题，但我们建议依赖确实支持 JSON schema 输出的 provider，否则你的应用会经常因为 JSON 格式错误而中断。

## 跨 provider 混用模型

你需要了解不同模型 provider 之间的特性差异，否则可能会遇到错误。例如，OpenAI 支持 structured outputs、多模态输入，以及由OpenAI托管的工具中的文件检索与网络检索，但许多其他 provider 不支持这些特性。请注意以下限制：

-   不要向不理解它们的 provider 发送不受支持的 `tools`
-   在调用纯文本模型之前过滤掉多模态输入
-   注意：不支持结构化 JSON 输出的 provider 偶尔会生成无效 JSON。