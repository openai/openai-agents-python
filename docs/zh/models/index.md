---
search:
  exclude: true
---
# 模型

Agents SDK 原生支持两种 OpenAI 模型：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，它使用新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，它使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## 模型配置选择

从最符合你配置的简单方案开始：

| 如果你想要…… | 推荐方案 | 更多信息 |
| --- | --- | --- |
| 仅使用 OpenAI 模型 | 使用默认 OpenAI 提供商及 Responses 模型路径 | [OpenAI 模型](#openai-models) |
| 通过 WebSocket 传输使用 OpenAI Responses API | 保持使用 Responses 模型路径并启用 WebSocket 传输 | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用一个非 OpenAI 提供商 | 从内置的提供商集成点开始 | [非 OpenAI 模型](#non-openai-models) |
| 在不同智能体之间混用模型或提供商 | 按运行或按智能体选择提供商，并检查功能差异 | [在一个工作流中混用模型](#mixing-models-in-one-workflow)和[跨提供商混用模型](#mixing-models-across-providers) |
| 调整高级 OpenAI Responses 请求设置 | 在 OpenAI Responses 路径上使用 `ModelSettings` | [高级 OpenAI Responses 设置](#advanced-openai-responses-settings) |
| 使用第三方适配器进行非 OpenAI 或混合提供商路由 | 比较受支持的 Beta 版适配器，并验证你计划发布的提供商路径 | [第三方适配器](#third-party-adapters) |

## OpenAI 模型

对于大多数仅使用 OpenAI 的应用，推荐使用默认 OpenAI 提供商和字符串形式的模型名称，并继续使用 Responses 模型路径。

初始化 `Agent` 时，如果未指定模型，将使用默认模型。当前默认模型为 [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini)，并设置 `reasoning.effort="none"` 和 `verbosity="low"`，适用于低延迟智能体工作流。如果你有权访问，我们建议将智能体设置为 `gpt-5.6-sol` 以获得更高质量，同时保留显式的 `model_settings`。

如果要切换到 `gpt-5.6-sol` 等其他模型，可以通过两种方式配置智能体。

### 默认模型

首先，如果希望所有未设置自定义模型的智能体始终使用特定模型，请在运行智能体之前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

其次，可以通过 `RunConfig` 为一次运行设置默认模型。如果没有为智能体设置模型，则将使用本次运行的模型。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.6-sol"),
)
```

#### GPT-5 模型

以这种方式使用任意 GPT-5 模型（例如 `gpt-5.6-sol`）时，SDK 会应用默认的 `ModelSettings`。这些设置最适合大多数使用场景。要调整默认模型的推理强度，请传入你自己的 `ModelSettings`：

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.6-sol is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.6-sol",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

为了降低延迟，建议对 GPT-5 模型使用 `reasoning.effort="none"`。

#### ComputerTool 模型选择

如果智能体包含 [`ComputerTool`][agents.tool.ComputerTool]，实际 Responses 请求中的有效模型将决定 SDK 发送哪种计算机工具载荷。显式的 `gpt-5.5` 请求会使用正式发布版的内置 `computer` 工具，而显式的 `computer-use-preview` 请求会继续使用旧版 `computer_use_preview` 载荷。

由提示词管理的调用是主要例外。如果模型由提示词模板指定，并且 SDK 在请求中省略 `model`，SDK 将默认使用与预览版兼容的计算机载荷，以免猜测提示词固定的是哪个模型。要在此流程中继续使用正式发布版路径，可以在请求中显式设置 `model="gpt-5.5"`，或使用 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制选择正式发布版。

注册 [`ComputerTool`][agents.tool.ComputerTool] 后，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 会被规范化为与有效请求模型匹配的内置选择器。如果未注册 `ComputerTool`，这些字符串仍会像普通函数名称一样工作。

与预览版兼容的请求必须预先序列化 `environment` 和显示尺寸，因此，使用 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂的提示词管理流程应传入具体的 `Computer` 或 `AsyncComputer` 实例，或者在发送请求前强制使用正式发布版选择器。有关完整的迁移详情，请参阅[工具](../tools.md#computertool-and-the-responses-computer-tool)。

#### 非 GPT-5 模型

如果传入非 GPT-5 模型名称且未提供自定义 `model_settings`，SDK 将恢复使用兼容任意模型的通用 `ModelSettings`。

### 仅限 Responses 的工具搜索功能

以下工具功能仅受 OpenAI Responses 模型支持：

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 及其他延迟加载的 Responses 工具接口

Chat Completions 模型和非 Responses 后端会拒绝这些功能。使用延迟加载工具时，请将 `ToolSearchTool()` 添加到智能体，并让模型通过 `auto` 或 `required` 工具选择来加载工具，而不是强制指定不带限定的命名空间名称或仅限延迟加载的函数名称。有关配置详情和当前限制，请参阅[工具](../tools.md#hosted-tool-search)。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用由 OpenAI 支持的模型时，可以选择启用 WebSocket 传输。

#### 基本配置

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI 提供商解析的 OpenAI Responses 模型，包括 `"gpt-5.6-sol"` 等字符串模型名称。

SDK 将模型名称解析为模型实例时会选择传输方式。如果传入具体的 [`Model`][agents.models.interface.Model] 对象，其传输方式已经固定：[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 WebSocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，而 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 继续使用 Chat Completions。如果传入 `RunConfig(model_provider=...)`，该提供商将取代全局默认设置来控制传输方式的选择。

#### 提供商或运行级配置

还可以按提供商或按运行配置 WebSocket 传输：

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
    # Optional low-level websocket keepalive settings.
    responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

由 OpenAI 支持的提供商还接受可选的智能体注册配置。这是一项高级选项，适用于 OpenAI 配置需要提供商级注册元数据（例如测试框架 ID）的情况。

```python
from agents import (
    Agent,
    OpenAIAgentRegistrationConfig,
    OpenAIProvider,
    RunConfig,
    Runner,
)

provider = OpenAIProvider(
    use_responses_websocket=True,
    agent_registration=OpenAIAgentRegistrationConfig(harness_id="your-harness-id"),
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

#### 使用 `MultiProvider` 的高级路由

如果需要基于前缀进行模型路由，例如在一次运行中混用 `openai/...` 和 `any-llm/...` 模型名称，请使用 [`MultiProvider`][agents.MultiProvider]，并在其中设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留两个历史默认行为：

-   `openai/...` 被视为 OpenAI 提供商的别名，因此 `openai/gpt-4.1` 会作为模型 `gpt-4.1` 进行路由。
-   未知前缀会引发 `UserError`，而不是被直接传递。

当 OpenAI 提供商指向需要原样命名空间模型 ID 的 OpenAI 兼容端点时，请显式启用直通行为。在启用 WebSocket 的配置中，也要在 `MultiProvider` 上保留 `openai_use_responses_websocket=True`：

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

当后端要求使用原样的 `openai/...` 字符串时，请使用 `openai_prefix_mode="model_id"`。当后端要求使用其他命名空间模型 ID（例如 `openrouter/openai/gpt-4.1-mini`）时，请使用 `unknown_prefix_mode="model_id"`。这些选项也适用于 WebSocket 传输之外的 `MultiProvider`；此示例继续启用 WebSocket，因为它属于本节所述的传输配置。相同选项也可用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果通过 `MultiProvider` 进行路由时需要相同的提供商级注册元数据，请传入 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`，该配置将转发给底层 OpenAI 提供商。

如果使用自定义 OpenAI 兼容端点或代理，WebSocket 传输还要求提供兼容的 WebSocket `/responses` 端点。在这些配置中，可能需要显式设置 `websocket_base_url`。

#### 注意事项

-   这是通过 WebSocket 传输使用的 Responses API，而不是 [Realtime API](../realtime/guide.md)。它不适用于 Chat Completions 或非 OpenAI 提供商，除非它们支持 Responses WebSocket `/responses` 端点。
-   如果环境中尚未安装 `websockets` 软件包，请安装该软件包。
-   启用 WebSocket 传输后，可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望在多个轮次之间复用同一 WebSocket 连接的多轮工作流，包括嵌套的智能体作为工具调用，建议使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。请参阅[运行智能体](../running_agents.md)指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。
-   对于耗时较长的推理轮次或延迟会突然升高的网络，可以使用 `responses_websocket_options` 自定义 WebSocket 保活行为。增大 `ping_timeout` 可容忍延迟到达的 pong 帧，也可以设置 `ping_timeout=None`，在继续启用 ping 的同时禁用心跳超时。当可靠性比 WebSocket 延迟更重要时，优先使用 HTTP/SSE 传输。
-   默认情况下，SDK 会禁用传入消息的大小限制（`max_size=None`）。对于位于代理之后的长期运行智能体进程，或内存受限容器中的智能体进程，请设置 `responses_websocket_options={"max_size": 8 * 1024 * 1024}`，以限制单条消息的内存使用量。

## 非 OpenAI 模型

如果需要非 OpenAI 提供商，请从 SDK 的内置提供商集成点开始。在许多配置中，无需添加第三方适配器即可满足需求。每种模式的代码示例位于 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### 非 OpenAI 提供商集成方式

| 方式 | 适用场景 | 作用域 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 对于大多数或所有智能体，应默认使用同一个 OpenAI 兼容端点 | 全局默认 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 单个自定义提供商应应用于一次运行 | 按运行 |
| [`Agent.model`][agents.agent.Agent.model] | 不同智能体需要不同提供商或具体模型对象 | 按智能体 |
| 第三方适配器 | 需要由适配器管理的提供商覆盖范围或内置路径无法提供的路由 | 请参阅[第三方适配器](#third-party-adapters) |

可以通过以下内置路径集成其他 LLM 提供商：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于希望在全局范围内将 `AsyncOpenAI` 实例用作 LLM 客户端的情况。这适用于 LLM 提供商具有 OpenAI 兼容 API 端点，并且可以设置 `base_url` 和 `api_key` 的情况。可配置的代码示例请参阅 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 层级。借助它，你可以指定“对本次运行中的所有智能体使用自定义模型提供商”。可配置的代码示例请参阅 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许为特定 Agent 实例指定模型。这样可以为不同智能体混搭不同提供商。可配置的代码示例请参阅 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。

如果没有来自 `platform.openai.com` 的 API 密钥，建议通过 `set_tracing_disabled()` 禁用追踪，或设置[其他追踪进程](../tracing.md)。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    在这些代码示例中，我们使用 Chat Completions API/模型，因为许多 LLM 提供商仍不支持 Responses API。如果你的 LLM 提供商支持 Responses API，建议使用 Responses。

## 一个工作流中的模型混用

在单个工作流中，你可能希望每个智能体使用不同的模型。例如，可以使用较小、较快的模型进行分流，同时使用较大、能力更强的模型处理复杂任务。配置 [`Agent`][agents.Agent] 时，可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称以及可将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!! note

    虽然 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 形式，但我们建议每个工作流仅使用一种模型形式，因为两种形式支持的功能和工具集不同。如果工作流需要混搭模型形式，请确保你使用的所有功能都同时受两种形式支持。

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
    model="gpt-5.6-sol",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  直接设置 OpenAI 模型的名称。
2.  提供 [`Model`][agents.models.interface.Model] 实现。

如果要进一步配置智能体使用的模型，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供 temperature 等可选模型配置参数。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## 高级 OpenAI Responses 设置

使用 OpenAI Responses 路径并需要更精细的控制时，请从 `ModelSettings` 开始。

### 常用高级 `ModelSettings` 选项

使用 OpenAI Responses API 时，多个请求字段已经有对应的直接 `ModelSettings` 字段，因此无需通过 `extra_args` 设置。

- `parallel_tool_calls`：允许或禁止在同一轮次中进行多个工具调用。
- `truncation`：设置为 `"auto"`，让 Responses API 在上下文即将溢出时丢弃最早的对话项，而不是使请求失败。
- `store`：控制生成的响应是否存储在服务端，以供之后检索。这对于依赖响应 ID 的后续工作流，以及在 `store=False` 时可能需要回退到本地输入的会话压缩流程十分重要。
- `context_management`：配置服务端上下文处理，例如使用 `compact_threshold` 进行 Responses 压缩。
- `prompt_cache_retention`：延长提示词前缀的缓存保留时间，例如使用 `"24h"`。
- `response_include`：请求更丰富的响应载荷，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。
- `top_logprobs`：请求输出文本中概率最高的词元对数概率。SDK 还会自动添加 `message.output_text.logprobs`。
- `retry`：选择启用由运行器管理的模型调用重试设置。请参阅[由运行器管理的重试](#runner-managed-retries)。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.5",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

设置 `store=False` 时，Responses API 不会保留该响应供服务端之后检索。这适用于无状态或零数据保留类型的流程，但也意味着原本会复用响应 ID 的功能必须改为依赖本地管理的状态。例如，如果上一个响应未被存储，[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 会将默认的 `"auto"` 压缩路径切换为基于输入的压缩。请参阅[会话指南](../sessions/index.md#openai-responses-compaction-sessions)。

服务端压缩与 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 不同。`context_management=[{"type": "compaction", "compact_threshold": ...}]` 会随每个 Responses API 请求发送；当渲染后的上下文超过阈值时，API 可以在响应中生成压缩项。`OpenAIResponsesCompactionSession` 则在轮次之间调用独立的 `responses.compact` 端点，并重写本地会话历史记录。

### `extra_args` 传递

当需要 SDK 尚未直接在顶层公开的提供商特定请求字段或较新的请求字段时，请使用 `extra_args`。

此外，使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)，例如 `user`、`service_tier` 等。如果顶层不提供这些参数，也可以使用 `extra_args` 传递。请勿同时通过直接的 `ModelSettings` 字段设置同一个请求字段。

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

## 由运行器管理的重试

重试仅在运行时生效，并且需要选择启用。除非设置 `ModelSettings(retry=...)` 且重试策略决定进行重试，否则 SDK 不会重试常规模型请求。

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        retry=ModelRetrySettings(
            max_retries=4,
            backoff={
                "initial_delay": 0.5,
                "max_delay": 5.0,
                "multiplier": 2.0,
                "jitter": True,
            },
            policy=retry_policies.any(
                retry_policies.provider_suggested(),
                retry_policies.retry_after(),
                retry_policies.network_error(),
                retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
            ),
        )
    ),
)
```

`ModelRetrySettings` 有三个字段：

<div class="field-table" markdown="1">

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `max_retries` | `int | None` | 初始请求之后允许的重试次数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 当策略决定重试但未返回显式延迟时使用的默认延迟策略。`backoff.max_delay` 仅限制计算得出的退避延迟，不限制策略返回的显式延迟或 retry-after 提示。 |
| `policy` | `RetryPolicy | None` | 决定是否重试的回调。此字段仅在运行时生效，不会被序列化。 |

</div>

重试策略接收一个 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]，其中包含：

- `attempt` 和 `max_retries`，以便根据尝试次数作出决定。
- `stream`，以便针对流式和非流式行为采用不同逻辑。
- `error`，用于检查原始错误。
- `normalized` 事实，例如 `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout` 和 `is_abort`。
- 当底层模型适配器能够提供重试指导时，包含 `provider_advice`。

策略可以返回：

- `True` / `False`，表示简单的重试决定。
- 当需要覆盖延迟或附加诊断原因时，返回 [`RetryDecision`][agents.retry.RetryDecision]。

SDK 在 `retry_policies` 中导出了现成的辅助函数：

| 辅助函数 | 行为 |
| --- | --- |
| `retry_policies.never()` | 始终不重试。 |
| `retry_policies.provider_suggested()` | 在提供商给出重试建议时遵循该建议。 |
| `retry_policies.network_error()` | 匹配暂时性传输故障和超时故障。 |
| `retry_policies.http_status([...])` | 匹配选定的 HTTP 状态码。 |
| `retry_policies.retry_after()` | 仅当存在 retry-after 提示时重试，并使用其指定的延迟。此辅助函数将 retry-after 值视为显式策略延迟，因此 `backoff.max_delay` 不会对其进行限制。 |
| `retry_policies.any(...)` | 任意嵌套策略选择重试时进行重试。 |
| `retry_policies.all(...)` | 仅当所有嵌套策略都选择重试时进行重试。 |

组合策略时，`provider_suggested()` 是最安全的第一个基础组件，因为当提供商能够区分这些情况时，它会保留提供商的否决决定和重放安全批准。

##### 安全边界

某些故障绝不会被自动重试：

- 中止错误。
- 提供商建议将重放标记为不安全的请求。
- 已开始产生输出，并且重放会造成不安全后果的流式运行。

使用 `previous_response_id` 或 `conversation_id` 的有状态后续请求也会得到更谨慎的处理。对于这些请求，仅使用 `network_error()` 或 `http_status([500])` 等非提供商谓词并不足够。重试策略应包含来自提供商的重放安全批准，通常通过 `retry_policies.provider_suggested()` 实现。

##### 运行器与智能体合并行为

运行器级和智能体级 `ModelSettings` 之间会对 `retry` 进行深度合并：

- 智能体可以只覆盖 `retry.max_retries`，同时继续继承运行器的 `policy`。
- 智能体可以只覆盖 `retry.backoff` 的一部分，并保留运行器中的其他同级退避字段。
- `policy` 仅在运行时生效，因此序列化后的 `ModelSettings` 会保留 `max_retries` 和 `backoff`，但省略回调本身。

有关更完整的代码示例，请参阅 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 和[由适配器支持的重试示例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)。

## 非 OpenAI 提供商故障排除

### 追踪客户端错误 401

如果遇到与追踪相关的错误，这是因为追踪数据会上传到 OpenAI 服务，而你没有 OpenAI API 密钥。可以通过以下三种方式解决：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI 密钥：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API 密钥仅用于上传追踪数据，并且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 追踪进程。请参阅[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但许多其他 LLM 提供商仍不支持它。因此，你可能会遇到 404 或类似问题。可以通过以下两种方式解决：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果通过环境变量设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，此方式可用。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。相关代码示例请参阅[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### Chat Completions 兼容性选项

通过 Chat Completions 进行路由时，SDK 会静默丢弃 Chat Completions 无法发送的仅限 Responses 字段，例如 `previous_response_id`、`conversation_id`、提示词或不只包含文本的工具输出，从而保持兼容性。如果希望在开发期间因这些不匹配而立即失败，请在 OpenAI 提供商上启用严格功能验证：

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses=False,
    strict_feature_validation=True,
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

如果使用 [`MultiProvider`][agents.MultiProvider]，请改为传入 `openai_strict_feature_validation=True`。

一些与 OpenAI 兼容的 Chat Completions 提供商会分块传输工具调用增量，但这些分块不够可靠，SDK 无法进行增量处理。在这种情况下，请启用流式工具调用缓冲，使 SDK 仅在提供商的流结束后生成工具调用：

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

对于 [`MultiProvider`][agents.MultiProvider]，请使用 `openai_buffer_streamed_tool_calls=True`。

### structured outputs 支持

某些模型提供商不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似以下内容的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型提供商的不足：它们支持 JSON 输出，但不允许指定用于输出的 `json_schema`。我们正在修复此问题，但建议依赖支持 JSON schema 输出的提供商，否则应用经常会因格式错误的 JSON 而中断。

## 跨提供商混用模型

你需要了解模型提供商之间的功能差异，否则可能遇到错误。例如，OpenAI 支持 structured outputs、多模态输入以及托管的文件检索和网络检索，但许多其他提供商不支持这些功能。请注意以下限制：

-   不要向无法理解 `tools` 的提供商发送不受支持的 `tools`
-   在调用仅支持文本的模型之前，过滤掉多模态输入
-   请注意，不支持结构化 JSON 输出的提供商偶尔会生成无效 JSON。

## 第三方适配器

仅当 SDK 的内置提供商集成点无法满足需求时，才应使用第三方适配器。如果仅通过此 SDK 使用 OpenAI 模型，请优先选择内置的 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 路径，而不是 Any-LLM 或 LiteLLM。第三方适配器适用于需要将 OpenAI 模型与非 OpenAI 提供商结合使用，或需要由适配器管理的提供商覆盖范围或内置路径无法提供的路由的情况。适配器会在 SDK 和上游模型提供商之间增加一个兼容层，因此功能支持和请求语义可能因提供商而异。SDK 目前以尽力支持的 Beta 版适配器集成形式提供 Any-LLM 和 LiteLLM。

### Any-LLM

Any-LLM 支持以尽力支持的 Beta 版形式提供，适用于需要由 Any-LLM 管理的提供商覆盖范围或路由的情况。

根据上游提供商路径，Any-LLM 可能使用 Responses API、Chat Completions 兼容 API 或提供商特定的兼容层。

如果需要 Any-LLM，请安装 `openai-agents[any-llm]`，然后从 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 或 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) 开始。可以将 `any-llm/...` 模型名称与 [`MultiProvider`][agents.MultiProvider] 搭配使用、直接实例化 `AnyLLMModel`，或在运行作用域中使用 `AnyLLMProvider`。如果需要显式固定模型接口，请在构造 `AnyLLMModel` 时传入 `api="responses"` 或 `api="chat_completions"`。

Any-LLM 仍然是第三方适配器层，因此提供商依赖项和能力缺口由上游 Any-LLM 而不是 SDK 决定。当上游提供商返回使用量指标时，这些指标会自动传播，但流式 Chat Completions 后端可能需要设置 `ModelSettings(include_usage=True)` 才会生成使用量数据块。如果依赖 structured outputs、工具调用、使用量报告或 Responses 特定行为，请验证计划部署的具体提供商后端。

### LiteLLM

LiteLLM 支持以尽力支持的 Beta 版形式提供，适用于需要 LiteLLM 特定提供商覆盖范围或路由的情况。

如果需要 LiteLLM，请安装 `openai-agents[litellm]`，然后从 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 或 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) 开始。可以使用 `litellm/...` 模型名称，或直接实例化 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

一些由 LiteLLM 支持的提供商默认不会填充 SDK 使用量指标。如果需要使用量报告，请传入 `ModelSettings(include_usage=True)`；如果依赖 structured outputs、工具调用、使用量报告或适配器特定路由行为，请验证计划部署的具体提供商后端。