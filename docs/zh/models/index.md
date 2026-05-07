---
search:
  exclude: true
---
# 模型

Agents SDK 内置支持两种 OpenAI 模型：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，它使用新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，它使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## 模型设置选择

从适合你设置的最简单路径开始：

| 如果你想要…… | 推荐路径 | 阅读更多 |
| --- | --- | --- |
| 仅使用 OpenAI 模型 | 使用默认 OpenAI 提供方和 Responses 模型路径 | [OpenAI 模型](#openai-models) |
| 通过 websocket 传输使用 OpenAI Responses API | 保持 Responses 模型路径并启用 websocket 传输 | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用一个非 OpenAI 提供方 | 从内置提供方集成点开始 | [非 OpenAI 模型](#non-openai-models) |
| 在智能体之间混用模型或提供方 | 按每次运行或每个智能体选择提供方，并审查功能差异 | [在一个工作流中混用模型](#mixing-models-in-one-workflow) 和 [跨提供方混用模型](#mixing-models-across-providers) |
| 调整高级 OpenAI Responses 请求设置 | 在 OpenAI Responses 路径上使用 `ModelSettings` | [高级 OpenAI Responses 设置](#advanced-openai-responses-settings) |
| 使用第三方适配器进行非 OpenAI 或混合提供方路由 | 比较受支持的 beta 适配器，并验证你计划发布的提供方路径 | [第三方适配器](#third-party-adapters) |

## OpenAI 模型

对于大多数仅使用 OpenAI 的应用，推荐路径是使用字符串模型名称配合默认 OpenAI 提供方，并保持在 Responses 模型路径上。

当你初始化 `Agent` 时未指定模型，将使用默认模型。当前默认值是 [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini)，并为低延迟智能体工作流设置 `reasoning.effort="none"` 和 `verbosity="low"`。如果你有访问权限，我们建议将智能体设置为 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5)，以在保持显式 `model_settings` 的同时获得更高质量。

如果你想切换到其他模型，例如 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5)，有两种方式配置你的智能体。

### 默认模型

首先，如果你想让所有未设置自定义模型的智能体始终使用某个特定模型，请在运行智能体之前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.5
python3 my_awesome_agent.py
```

其次，你可以通过 `RunConfig` 为一次运行设置默认模型。如果某个智能体没有设置模型，将使用本次运行的模型。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.5"),
)
```

#### GPT-5 模型

当你以这种方式使用任何 GPT-5 模型（例如 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5)）时，SDK 会应用默认 `ModelSettings`。它会设置最适合大多数用例的选项。若要调整默认模型的推理力度，请传入你自己的 `ModelSettings`：

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.5 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.5",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

为了降低延迟，建议对 GPT-5 模型使用 `reasoning.effort="none"`。

#### ComputerTool 模型选择

如果智能体包含 [`ComputerTool`][agents.tool.ComputerTool]，实际 Responses 请求上的有效模型会决定 SDK 发送哪种计算机工具负载。显式 `gpt-5.5` 请求使用 GA 内置 `computer` 工具，而显式 `computer-use-preview` 请求会保留较旧的 `computer_use_preview` 负载。

由提示词管理的调用是主要例外。如果提示词模板拥有模型，而 SDK 在请求中省略 `model`，SDK 会默认使用兼容 preview 的计算机负载，以免猜测该提示词固定了哪个模型。要在该流程中保持 GA 路径，可以在请求中显式设置 `model="gpt-5.5"`，或使用 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制选择 GA。

注册了 [`ComputerTool`][agents.tool.ComputerTool] 后，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 会被规范化为与有效请求模型匹配的内置选择器。如果未注册 `ComputerTool`，这些字符串会继续像普通函数名称一样运行。

兼容 preview 的请求必须预先序列化 `environment` 和显示尺寸，因此，使用 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂的提示词管理流程应传入具体的 `Computer` 或 `AsyncComputer` 实例，或在发送请求前强制选择 GA。完整迁移详情请参阅[工具](../tools.md#computertool-and-the-responses-computer-tool)。

#### 非 GPT-5 模型

如果你传入非 GPT-5 模型名称且没有自定义 `model_settings`，SDK 会回退到与任何模型兼容的通用 `ModelSettings`。

### 仅限 Responses 的工具搜索功能

以下工具功能仅支持 OpenAI Responses 模型：

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 以及其他延迟加载的 Responses 工具表面

这些功能会在 Chat Completions 模型和非 Responses 后端上被拒绝。使用延迟加载工具时，请将 `ToolSearchTool()` 添加到智能体，并让模型通过 `auto` 或 `required` 工具选择来加载工具，而不是强制使用裸命名空间名称或仅延迟的函数名称。设置详情和当前限制请参阅[工具](../tools.md#hosted-tool-search)。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。在使用由 OpenAI 支持的模型时，你可以选择启用 websocket 传输。

#### 基本设置

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI 提供方解析的 OpenAI Responses 模型（包括诸如 `"gpt-5.5"` 的字符串模型名称）。

当 SDK 将模型名称解析为模型实例时会发生传输选择。如果你传入具体的 [`Model`][agents.models.interface.Model] 对象，它的传输已经固定：[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 websocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，而 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 保持使用 Chat Completions。如果你传入 `RunConfig(model_provider=...)`，该提供方会控制传输选择，而不是全局默认值。

#### 提供方或运行级设置

你也可以按提供方或按运行配置 websocket 传输：

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

由 OpenAI 支持的提供方还接受可选的智能体注册配置。这是一个高级选项，适用于你的 OpenAI 设置需要提供方级注册元数据（例如 harness ID）的情况。

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

如果你需要基于前缀的模型路由（例如在一次运行中混用 `openai/...` 和 `any-llm/...` 模型名称），请使用 [`MultiProvider`][agents.MultiProvider]，并在其中设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留两个历史默认值：

-   `openai/...` 被视为 OpenAI 提供方的别名，因此 `openai/gpt-4.1` 会作为模型 `gpt-4.1` 进行路由。
-   未知前缀会引发 `UserError`，而不是被透传。

当你将 OpenAI 提供方指向一个期望字面量命名空间模型 ID 的 OpenAI 兼容端点时，请显式选择透传行为。在启用 websocket 的设置中，也要在 `MultiProvider` 上保持 `openai_use_responses_websocket=True`：

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

当后端期望字面量 `openai/...` 字符串时，使用 `openai_prefix_mode="model_id"`。当后端期望其他命名空间模型 ID（例如 `openrouter/openai/gpt-4.1-mini`）时，使用 `unknown_prefix_mode="model_id"`。这些选项也适用于 websocket 传输之外的 `MultiProvider`；本示例保持启用 websocket，因为它是本节所述传输设置的一部分。相同选项也可用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果你在通过 `MultiProvider` 路由时需要相同的提供方级注册元数据，请传入 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`，它会被转发到底层 OpenAI 提供方。

如果你使用自定义 OpenAI 兼容端点或代理，websocket 传输还需要兼容的 websocket `/responses` 端点。在这些设置中，你可能需要显式设置 `websocket_base_url`。

#### 说明

-   这是通过 websocket 传输的 Responses API，而不是 [Realtime API](../realtime/guide.md)。除非它们支持 Responses websocket `/responses` 端点，否则它不适用于 Chat Completions 或非 OpenAI 提供方。
-   如果你的环境中尚未提供 `websockets` 包，请安装它。
-   启用 websocket 传输后，你可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望跨轮次（以及嵌套的 agent-as-tool 调用）复用同一 websocket 连接的多轮工作流，建议使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。请参阅[运行智能体](../running_agents.md)指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。
-   对于较长推理轮次或存在延迟尖峰的网络，请使用 `responses_websocket_options` 自定义 websocket keepalive 行为。增大 `ping_timeout` 以容忍延迟的 pong 帧，或设置 `ping_timeout=None` 以在保持启用 ping 的同时禁用心跳超时。当可靠性比 websocket 延迟更重要时，优先使用 HTTP/SSE 传输。

## 非 OpenAI 模型

如果你需要非 OpenAI 提供方，请从 SDK 的内置提供方集成点开始。在许多设置中，这已经足够，无需添加第三方适配器。每种模式的示例位于 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### 非 OpenAI 提供方集成方式

| 方法 | 使用场景 | 范围 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 一个 OpenAI 兼容端点应作为大多数或所有智能体的默认值 | 全局默认 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 一个自定义提供方应应用于单次运行 | 按运行 |
| [`Agent.model`][agents.agent.Agent.model] | 不同智能体需要不同提供方或具体模型对象 | 按智能体 |
| 第三方适配器 | 你需要适配器管理的提供方覆盖范围或内置路径不提供的路由 | 请参阅[第三方适配器](#third-party-adapters) |

你可以通过这些内置路径集成其他 LLM 提供方：

1. [`set_default_openai_client`][agents.set_default_openai_client] 在你想全局使用一个 `AsyncOpenAI` 实例作为 LLM 客户端时很有用。这适用于 LLM 提供方具有 OpenAI 兼容 API 端点，并且你可以设置 `base_url` 和 `api_key` 的情况。可配置示例见 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 级别。这让你可以表示“本次运行中的所有智能体都使用自定义模型提供方”。可配置示例见 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 让你可以在特定 Agent 实例上指定模型。这使你能够为不同智能体混合搭配不同提供方。可配置示例见 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。

在你没有来自 `platform.openai.com` 的 API 密钥的情况下，我们建议通过 `set_tracing_disabled()` 禁用追踪，或设置[不同的追踪进程](../tracing.md)。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为许多 LLM 提供方仍不支持 Responses API。如果你的 LLM 提供方支持它，我们建议使用 Responses。

## 在一个工作流中混用模型

在单个工作流中，你可能希望为每个智能体使用不同模型。例如，你可以使用更小、更快的模型进行分诊，同时使用更大、能力更强的模型处理复杂任务。配置 [`Agent`][agents.Agent] 时，你可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 一个可将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!! note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形态，但我们建议每个工作流使用单一模型形态，因为两种形态支持的功能和工具集合不同。如果你的工作流需要混合搭配模型形态，请确保你使用的所有功能在两者上都可用。

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
    model="gpt-5.5",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  直接设置 OpenAI 模型的名称。
2.  提供 [`Model`][agents.models.interface.Model] 实现。

当你想进一步配置智能体使用的模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供可选的模型配置参数，例如 temperature。

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

当你在 OpenAI Responses 路径上并需要更多控制时，请从 `ModelSettings` 开始。

### 常用高级 `ModelSettings` 选项

当你使用 OpenAI Responses API 时，已有多个请求字段具备直接的 `ModelSettings` 字段，因此不需要为它们使用 `extra_args`。

- `parallel_tool_calls`：允许或禁止同一轮次中的多个工具调用。
- `truncation`：设置为 `"auto"`，让 Responses API 在上下文将溢出时丢弃最旧的对话项，而不是失败。
- `store`：控制生成的响应是否存储在服务端以供之后检索。这对依赖响应 ID 的后续工作流，以及在 `store=False` 时可能需要回退到本地输入的会话压缩流程很重要。
- `context_management`：配置服务端上下文处理，例如带有 `compact_threshold` 的 Responses 压缩。
- `prompt_cache_retention`：更长时间保留缓存的提示词前缀，例如使用 `"24h"`。
- `response_include`：请求更丰富的响应负载，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。
- `top_logprobs`：请求输出文本的 top-token logprobs。SDK 还会自动添加 `message.output_text.logprobs`。
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

当你设置 `store=False` 时，Responses API 不会保留该响应以供之后服务端检索。这对无状态或零数据保留风格的流程很有用，但也意味着原本会复用响应 ID 的功能需要改为依赖本地管理的状态。例如，当最后一个响应未被存储时，[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 会将其默认 `"auto"` 压缩路径切换为基于输入的压缩。请参阅[会话指南](../sessions/index.md#openai-responses-compaction-sessions)。

服务端压缩不同于 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]。`context_management=[{"type": "compaction", "compact_threshold": ...}]` 会随每个 Responses API 请求发送，并且当渲染后的上下文超过阈值时，API 可以将压缩项作为响应的一部分发出。`OpenAIResponsesCompactionSession` 会在轮次之间调用独立的 `responses.compact` 端点，并重写本地会话历史。

### 传递 `extra_args`

当你需要提供方特定或较新的请求字段，而 SDK 尚未在顶层直接暴露时，请使用 `extra_args`。

此外，当你使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们在顶层不可用，也可以使用 `extra_args` 传递它们。不要同时通过直接的 `ModelSettings` 字段设置同一个请求字段。

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

重试仅在运行时生效，并且需要选择启用。除非你设置 `ModelSettings(retry=...)` 且你的重试策略选择重试，否则 SDK 不会重试通用模型请求。

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.5",
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
| `max_retries` | `int | None` | 初始请求之后允许的重试尝试次数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 当策略在未返回显式延迟的情况下重试时使用的默认延迟策略。 |
| `policy` | `RetryPolicy | None` | 决定是否重试的回调。此字段仅在运行时生效，不会被序列化。 |

</div>

重试策略会收到一个 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]，其中包含：

- `attempt` 和 `max_retries`，以便你根据尝试次数做出决策。
- `stream`，以便你在流式与非流式行为之间分支。
- `error`，用于原始检查。
- `normalized` 事实，例如 `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout` 和 `is_abort`。
- 当底层模型适配器能够提供重试指导时的 `provider_advice`。

策略可以返回：

- `True` / `False`，表示简单的重试决策。
- 当你想覆盖延迟或附加诊断原因时，返回 [`RetryDecision`][agents.retry.RetryDecision]。

SDK 在 `retry_policies` 上导出开箱即用的辅助函数：

| 辅助函数 | 行为 |
| --- | --- |
| `retry_policies.never()` | 始终选择不重试。 |
| `retry_policies.provider_suggested()` | 在可用时遵循提供方重试建议。 |
| `retry_policies.network_error()` | 匹配瞬态传输和超时故障。 |
| `retry_policies.http_status([...])` | 匹配选定的 HTTP 状态码。 |
| `retry_policies.retry_after()` | 仅在存在 retry-after 提示时重试，并使用该延迟。 |
| `retry_policies.any(...)` | 当任一嵌套策略选择重试时重试。 |
| `retry_policies.all(...)` | 仅当每个嵌套策略都选择重试时才重试。 |

组合策略时，`provider_suggested()` 是最安全的第一构建块，因为当提供方能够区分时，它会保留提供方否决和重放安全批准。

##### 安全边界

某些故障永远不会自动重试：

- 中止错误。
- 提供方建议将重放标记为不安全的请求。
- 已经开始输出且以会使重放不安全的方式进行的流式运行。

使用 `previous_response_id` 或 `conversation_id` 的有状态后续请求也会被更保守地处理。对于这些请求，仅靠 `network_error()` 或 `http_status([500])` 等非提供方谓词是不够的。重试策略应包含来自提供方的重放安全批准，通常通过 `retry_policies.provider_suggested()` 实现。

##### 运行器与智能体合并行为

`retry` 会在运行器级和智能体级 `ModelSettings` 之间进行深度合并：

- 智能体可以仅覆盖 `retry.max_retries`，同时仍继承运行器的 `policy`。
- 智能体可以仅覆盖 `retry.backoff` 的一部分，并保留来自运行器的同级 backoff 字段。
- `policy` 仅在运行时生效，因此序列化后的 `ModelSettings` 会保留 `max_retries` 和 `backoff`，但省略回调本身。

更多示例请参阅 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 和[由适配器支持的重试示例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)。

## 非 OpenAI 提供方故障排除

### 追踪客户端错误 401

如果你遇到与追踪相关的错误，这是因为 traces 会上传到 OpenAI 服务，而你没有 OpenAI API 密钥。你有三个选项可解决此问题：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 设置用于追踪的 OpenAI 密钥：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API 密钥仅用于上传 traces，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI trace 进程。请参阅[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但许多其他 LLM 提供方仍不支持它。因此，你可能会看到 404 或类似问题。要解决此问题，你有两个选项：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果你通过环境变量设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，此方法可用。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)有示例。

### Structured outputs 支持

某些模型提供方不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似如下的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型提供方的不足之处——它们支持 JSON 输出，但不允许你指定用于输出的 `json_schema`。我们正在修复此问题，但建议依赖支持 JSON schema 输出的提供方，因为否则你的应用常常会因格式不正确的 JSON 而中断。

## 跨提供方混用模型

你需要了解模型提供方之间的功能差异，否则可能会遇到错误。例如，OpenAI 支持 structured outputs、多模态输入以及托管的文件检索和网络检索，但许多其他提供方不支持这些功能。请注意以下限制：

-   不要向无法理解的提供方发送不受支持的 `tools`
-   在调用仅支持文本的模型之前，过滤掉多模态输入
-   请注意，不支持结构化 JSON 输出的提供方偶尔会生成无效 JSON。

## 第三方适配器

只有当 SDK 的内置提供方集成点不足时，才考虑使用第三方适配器。如果你仅通过此 SDK 使用 OpenAI 模型，请优先使用内置 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 路径，而不是 Any-LLM 或 LiteLLM。第三方适配器适用于你需要将 OpenAI 模型与非 OpenAI 提供方结合，或需要适配器管理的提供方覆盖范围或内置路径不提供的路由的情况。适配器会在 SDK 和上游模型提供方之间增加另一层兼容性，因此功能支持和请求语义可能因提供方而异。SDK 目前包含 Any-LLM 和 LiteLLM，作为尽力而为的 beta 适配器集成。

### Any-LLM

Any-LLM 支持以尽力而为的 beta 形式提供，适用于你需要由 Any-LLM 管理的提供方覆盖范围或路由的情况。

根据上游提供方路径，Any-LLM 可能使用 Responses API、Chat Completions 兼容 API 或提供方特定的兼容层。

如果你需要 Any-LLM，请安装 `openai-agents[any-llm]`，然后从 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 或 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) 开始。你可以将 `any-llm/...` 模型名称与 [`MultiProvider`][agents.MultiProvider] 一起使用，直接实例化 `AnyLLMModel`，或在运行范围使用 `AnyLLMProvider`。如果需要显式固定模型表面，请在构造 `AnyLLMModel` 时传入 `api="responses"` 或 `api="chat_completions"`。

Any-LLM 仍是第三方适配器层，因此提供方依赖项和能力缺口由上游 Any-LLM 而不是 SDK 定义。当上游提供方返回使用指标时，使用指标会自动传播，但流式 Chat Completions 后端可能需要 `ModelSettings(include_usage=True)` 才会发出使用量块。如果你依赖 structured outputs、工具调用、使用量报告或 Responses 特定行为，请验证你计划部署的确切提供方后端。

### LiteLLM

LiteLLM 支持以尽力而为的 beta 形式提供，适用于你需要 LiteLLM 特定提供方覆盖范围或路由的情况。

如果你需要 LiteLLM，请安装 `openai-agents[litellm]`，然后从 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 或 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) 开始。你可以使用 `litellm/...` 模型名称，或直接实例化 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

某些由 LiteLLM 支持的提供方默认不会填充 SDK 使用指标。如果你需要使用量报告，请传入 `ModelSettings(include_usage=True)`，并在依赖 structured outputs、工具调用、使用量报告或适配器特定路由行为时，验证你计划部署的确切提供方后端。