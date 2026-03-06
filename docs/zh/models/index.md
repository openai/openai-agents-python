---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用地支持两种形式的 OpenAI 模型：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，使用新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## 模型设置选择

请根据你的设置按以下顺序使用本页：

| 目标 | 从这里开始 |
| --- | --- |
| 使用 SDK 默认配置的 OpenAI 托管模型 | [OpenAI 模型](#openai-models) |
| 通过 websocket 传输使用 OpenAI Responses API | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用非 OpenAI 提供方 | [非 OpenAI 模型](#non-openai-models) |
| 在单个工作流中混合模型/提供方 | [高级模型选择与混合](#advanced-model-selection-and-mixing) 和 [跨提供方混合模型](#mixing-models-across-providers) |
| 调试提供方兼容性问题 | [非 OpenAI 提供方故障排查](#troubleshooting-non-openai-providers) |

## OpenAI 模型

当你在初始化 `Agent` 时未指定模型，将使用默认模型。当前默认是 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1)，以获得兼容性和低延迟。如果你有访问权限，我们建议将智能体设置为 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 以获得更高质量，同时显式设置 `model_settings`。

如果你想切换到 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 等其他模型，可以通过两种方式配置智能体。

### 默认模型

首先，如果你希望所有未设置自定义模型的智能体始终使用某个特定模型，请在运行智能体前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

其次，你可以通过 `RunConfig` 为一次运行设置默认模型。如果未为某个智能体设置模型，则使用该次运行的模型。

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

当你以这种方式使用任意 GPT-5.x 模型（例如 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2)）时，SDK 会应用默认 `ModelSettings`。它会设置对大多数场景效果最好的选项。要调整默认模型的推理强度，请传入你自己的 `ModelSettings`：

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

为获得更低延迟，建议在 `gpt-5.2` 上使用 `reasoning.effort="none"`。gpt-4.1 系列（包括 mini 和 nano 变体）仍然是构建交互式智能体应用的可靠选择。

#### 非 GPT-5 模型

如果你传入非 GPT-5 的模型名称且未提供自定义 `model_settings`，SDK 会回退为兼容任意模型的通用 `ModelSettings`。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用 OpenAI 支持的模型时，你可以选择启用 websocket 传输。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI 提供方解析的 OpenAI Responses 模型（包括 `"gpt-5.2"` 这类字符串模型名）。

传输方式的选择发生在 SDK 将模型名解析为模型实例时。如果你传入具体的 [`Model`][agents.models.interface.Model] 对象，其传输方式已固定：[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 websocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 保持使用 Chat Completions。如果你传入 `RunConfig(model_provider=...)`，则由该提供方控制传输方式选择，而非全局默认值。

你也可以按提供方或按单次运行配置 websocket 传输：

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

如果你需要基于前缀的模型路由（例如在一次运行中混用 `openai/...` 与 `litellm/...` 模型名），请改用 [`MultiProvider`][agents.MultiProvider]，并在其中设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留两个历史默认行为：

-   `openai/...` 被视为 OpenAI 提供方别名，因此 `openai/gpt-4.1` 会路由为模型 `gpt-4.1`。
-   未知前缀会抛出 `UserError`，而不是直接透传。

当你将 OpenAI 提供方指向一个期望字面命名空间模型 ID 的 OpenAI 兼容端点时，请显式启用透传行为。在启用 websocket 的设置中，也请在 `MultiProvider` 上保持 `openai_use_responses_websocket=True`：

```python
from agents import Agent, MultiProvider, RunConfig, Runner

provider = MultiProvider(
    openai_base_url="https://openrouter.ai/api/v1",
    openai_api_key="...",
    openai_use_responses_websocket=True,
    openai_prefix_mode="model_id",
    unknown_prefix_mode="model_id",
)

agent = Agent(
    name="Assistant",
    instructions="Be concise.",
    model="openai/gpt-4.1",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

当后端需要字面 `openai/...` 字符串时，使用 `openai_prefix_mode="model_id"`。当后端需要其他命名空间模型 ID（例如 `openrouter/openai/gpt-4.1-mini`）时，使用 `unknown_prefix_mode="model_id"`。这些选项在非 websocket 传输下的 `MultiProvider` 中同样可用；本示例保持 websocket 启用，因为它属于本节描述的传输设置。同样的选项也可用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果你使用自定义 OpenAI 兼容端点或代理，websocket 传输还要求有兼容的 websocket `/responses` 端点。在这些设置中，你可能需要显式设置 `websocket_base_url`。

说明：

-   这是基于 websocket 传输的 Responses API，不是 [Realtime API](../realtime/guide.md)。它不适用于 Chat Completions 或非 OpenAI 提供方，除非它们支持 Responses websocket `/responses` 端点。
-   如果你的环境中尚未安装，请安装 `websockets` 包。
-   启用 websocket 传输后，你可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望在多轮工作流中复用同一 websocket 连接（以及嵌套的 agent-as-tool 调用）的场景，推荐使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。参见 [运行智能体](../running_agents.md) 指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。

## 非 OpenAI 模型

你可以通过 [LiteLLM 集成](./litellm.md) 使用大多数其他非 OpenAI 模型。首先，安装 litellm 依赖组：

```bash
pip install "openai-agents[litellm]"
```

然后，使用任意[支持的模型](https://docs.litellm.ai/docs/providers)，并添加 `litellm/` 前缀：

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 使用非 OpenAI 模型的其他方式

你还可以通过另外 3 种方式集成其他 LLM 提供方（示例见[这里](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于你希望全局使用某个 `AsyncOpenAI` 实例作为 LLM 客户端的情况。该方式适用于 LLM 提供方具有 OpenAI 兼容 API 端点，并且你可以设置 `base_url` 和 `api_key`。可配置示例见 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 层级。这让你可以声明“本次运行中的所有智能体都使用自定义模型提供方”。可配置示例见 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许你在特定 Agent 实例上指定模型。这样你就可以为不同智能体混合使用不同提供方。可配置示例见 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。使用大多数可用模型的简便方式是 [LiteLLM 集成](./litellm.md)。

在你没有 `platform.openai.com` 的 API 密钥时，我们建议通过 `set_tracing_disabled()` 禁用追踪，或配置[其他追踪进程](../tracing.md)。

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为大多数 LLM 提供方尚不支持 Responses API。如果你的 LLM 提供方支持，我们建议使用 Responses。

## 高级模型选择与混合

在单个工作流中，你可能希望为每个智能体使用不同模型。例如，你可以在分流场景使用更小更快的模型，而在复杂任务中使用更大更强的模型。配置 [`Agent`][agents.Agent] 时，你可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名 + 可将该名称映射为 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!!note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形式，但我们建议每个工作流使用单一模型形式，因为两者支持的功能和工具集不同。如果你的工作流必须混用不同模型形式，请确保你使用的所有功能在两者上都可用。

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
2.  提供一个 [`Model`][agents.models.interface.Model] 实现。

当你希望进一步配置某个智能体使用的模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供诸如 temperature 等可选模型配置参数。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

#### 常见高级 `ModelSettings` 选项

当你使用 OpenAI Responses API 时，多个请求字段在 `ModelSettings` 中已有直接对应字段，因此你不需要为它们使用 `extra_args`。

| 字段 | 用途 |
| --- | --- |
| `parallel_tool_calls` | 允许或禁止在同一轮中进行多个工具调用。 |
| `truncation` | 设置为 `"auto"`，让 Responses API 在上下文将溢出时丢弃最旧的对话项，而不是直接失败。 |
| `prompt_cache_retention` | 让已缓存的提示前缀保留更久，例如使用 `"24h"`。 |
| `response_include` | 请求更丰富的响应负载，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。 |
| `top_logprobs` | 请求输出文本的 top-token logprobs。SDK 还会自动添加 `message.output_text.logprobs`。 |

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.2",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

当你需要提供方特定字段，或 SDK 顶层尚未直接暴露的新请求字段时，请使用 `extra_args`。

此外，当你使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们在顶层不可用，也可以通过 `extra_args` 传入。

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

## 非 OpenAI 提供方故障排查

### 追踪客户端错误 401

如果你遇到与追踪相关的错误，这是因为追踪数据会上传到 OpenAI 服务，而你没有 OpenAI API 密钥。你有三种解决方式：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI 密钥：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。该 API 密钥仅用于上传追踪数据，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 的追踪进程。参见[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但大多数其他 LLM 提供方尚不支持它。因此你可能会看到 404 或类似问题。你有两种解决方式：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。当你通过环境变量设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 时，这种方式有效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。示例见[这里](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### structured outputs 支持

一些模型提供方不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似如下错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是部分模型提供方的不足——它们支持 JSON 输出，但不允许你为输出指定要使用的 `json_schema`。我们正在修复这一问题，但建议你依赖支持 JSON schema 输出的提供方，否则你的应用会经常因 JSON 格式错误而中断。

## 跨提供方混合模型

你需要了解不同模型提供方之间的功能差异，否则可能遇到错误。例如，OpenAI 支持 structured outputs、多模态输入，以及托管的文件检索和网络检索，但许多其他提供方不支持这些能力。请注意以下限制：

-   不要向不支持的提供方发送其无法理解的 `tools`
-   在调用仅文本模型前，先过滤掉多模态输入
-   注意不支持结构化 JSON 输出的提供方会偶发生成无效 JSON