---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用地支持两类 OpenAI 模型：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，它使用新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，它使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## 模型设置选择

从最适合你的设置的最简单路径开始：

| 如果你想要... | 推荐路径 | 了解更多 |
| --- | --- | --- |
| 仅使用 OpenAI 模型 | 使用默认 OpenAI provider，并采用 Responses 模型路径 | [OpenAI 模型](#openai-models) |
| 通过 websocket 传输使用 OpenAI Responses API | 保持 Responses 模型路径并启用 websocket 传输 | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用一个非 OpenAI provider | 从内置 provider 集成点开始 | [非 OpenAI 模型](#non-openai-models) |
| 在智能体之间混用模型或 provider | 按每次运行或每个智能体选择 provider，并检查功能差异 | [在一个工作流中混用模型](#mixing-models-in-one-workflow) 和 [跨 provider 混用模型](#mixing-models-across-providers) |
| 调整高级 OpenAI Responses 请求设置 | 在 OpenAI Responses 路径上使用 `ModelSettings` | [高级 OpenAI Responses 设置](#advanced-openai-responses-settings) |
| 为非 OpenAI 或混合 provider 路由使用第三方适配器 | 比较受支持的 beta 适配器，并验证你计划交付的 provider 路径 | [第三方适配器](#third-party-adapters) |

## OpenAI 模型

对于大多数仅使用 OpenAI 的应用，推荐路径是使用字符串模型名称和默认 OpenAI provider，并保持使用 Responses 模型路径。

初始化 `Agent` 时如果未指定模型，将使用默认模型。当前默认值为 [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1)，以确保兼容性和低延迟。如果你有访问权限，我们建议将你的智能体设置为 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5)，以在保持显式 `model_settings` 的同时获得更高质量。

如果你想切换到其他模型，例如 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5)，有两种方式可以配置你的智能体。

### 默认模型

首先，如果你想让所有未设置自定义模型的智能体始终使用某个特定模型，请在运行智能体前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.5
python3 my_awesome_agent.py
```

其次，你可以通过 `RunConfig` 为一次运行设置默认模型。如果没有为某个智能体设置模型，将使用这次运行的模型。

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

当你以这种方式使用任何 GPT-5 模型（例如 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5)）时，SDK 会应用默认 `ModelSettings`。它会设置最适合大多数用例的选项。要调整默认模型的推理强度，请传入你自己的 `ModelSettings`：

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

为降低延迟，建议将 `reasoning.effort="none"` 与 `gpt-5.5` 搭配使用。gpt-4.1 系列（包括 mini 和 nano 变体）仍然是构建交互式智能体应用的可靠选择。

#### ComputerTool 模型选择

如果智能体包含 [`ComputerTool`][agents.tool.ComputerTool]，实际 Responses 请求上的有效模型将决定 SDK 发送哪种 computer-tool 载荷。显式的 `gpt-5.5` 请求会使用 GA 内置 `computer` 工具，而显式的 `computer-use-preview` 请求会继续使用较旧的 `computer_use_preview` 载荷。

由提示词管理的调用是主要例外。如果提示词模板拥有模型，并且 SDK 在请求中省略 `model`，SDK 会默认使用与预览版兼容的计算机载荷，这样它就不会猜测提示词绑定的是哪个模型。要在该流程中保持 GA 路径，请在请求上显式设置 `model="gpt-5.5"`，或使用 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制使用 GA 选择器。

注册了 [`ComputerTool`][agents.tool.ComputerTool] 后，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 会被规范化为与有效请求模型匹配的内置选择器。如果未注册 `ComputerTool`，这些字符串会继续像普通函数名一样运行。

与预览版兼容的请求必须预先序列化 `environment` 和显示尺寸，因此使用 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂的由提示词管理的流程应传入具体的 `Computer` 或 `AsyncComputer` 实例，或在发送请求前强制使用 GA 选择器。完整迁移详情请参阅 [工具](../tools.md#computertool-and-the-responses-computer-tool)。

#### 非 GPT-5 模型

如果你传入非 GPT-5 模型名称且没有自定义 `model_settings`，SDK 会回退到与任何模型兼容的通用 `ModelSettings`。

### 仅 Responses 支持的工具搜索功能

以下工具功能仅受 OpenAI Responses 模型支持：

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 以及其他延迟加载的 Responses 工具接口

这些功能会在 Chat Completions 模型和非 Responses 后端上被拒绝。当使用延迟加载工具时，请向智能体添加 `ToolSearchTool()`，并让模型通过 `auto` 或 `required` 工具选择来加载工具，而不是强制使用裸命名空间名称或仅延迟加载的函数名称。设置详情和当前限制请参阅 [工具](../tools.md#hosted-tool-search)。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用 OpenAI 支持的模型时，你可以选择启用 websocket 传输。

#### 基本设置

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI provider 解析的 OpenAI Responses 模型（包括字符串模型名称，例如 `"gpt-5.5"`）。

传输选择发生在 SDK 将模型名称解析为模型实例时。如果你传入具体的 [`Model`][agents.models.interface.Model] 对象，其传输已经固定：[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 websocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，而 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 保持使用 Chat Completions。如果你传入 `RunConfig(model_provider=...)`，则由该 provider 控制传输选择，而不是全局默认设置。

#### Provider 或运行级设置

你也可以按 provider 或按运行配置 websocket 传输：

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

由 OpenAI 支持的 provider 还接受可选的智能体注册配置。这是一个高级选项，适用于你的 OpenAI 设置需要 provider 级注册元数据（例如 harness ID）的情况。

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

如果你需要基于前缀的模型路由（例如在一次运行中混合 `openai/...` 和 `any-llm/...` 模型名称），请使用 [`MultiProvider`][agents.MultiProvider]，并在那里设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留了两个历史默认行为：

-   `openai/...` 被视为 OpenAI provider 的别名，因此 `openai/gpt-4.1` 会作为模型 `gpt-4.1` 路由。
-   未知前缀会引发 `UserError`，而不是被透传。

当你将 OpenAI provider 指向一个期望字面量命名空间模型 ID 的 OpenAI 兼容端点时，请显式选择透传行为。在启用 websocket 的设置中，也要在 `MultiProvider` 上保持 `openai_use_responses_websocket=True`：

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

当后端期望字面量 `openai/...` 字符串时，使用 `openai_prefix_mode="model_id"`。当后端期望其他命名空间模型 ID（例如 `openrouter/openai/gpt-4.1-mini`）时，使用 `unknown_prefix_mode="model_id"`。这些选项也可在 websocket 传输之外的 `MultiProvider` 上使用；本示例保持启用 websocket，因为它是本节所述传输设置的一部分。相同选项也可用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果你在通过 `MultiProvider` 路由时需要相同的 provider 级注册元数据，请传入 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`，它会被转发给底层 OpenAI provider。

如果你使用自定义 OpenAI 兼容端点或代理，websocket 传输还需要兼容的 websocket `/responses` 端点。在这些设置中，你可能需要显式设置 `websocket_base_url`。

#### 注意事项

-   这是基于 websocket 传输的 Responses API，不是 [Realtime API](../realtime/guide.md)。除非 Chat Completions 或非 OpenAI provider 支持 Responses websocket `/responses` 端点，否则它不适用于它们。
-   如果你的环境中尚未提供 `websockets` 包，请安装它。
-   启用 websocket 传输后，你可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望在多轮之间（以及嵌套的 agent-as-tool 调用之间）复用同一个 websocket 连接的多轮工作流，建议使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。请参阅 [运行智能体](../running_agents.md) 指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。

## 非 OpenAI 模型

如果你需要非 OpenAI provider，请从 SDK 的内置 provider 集成点开始。在许多设置中，这已经足够，无需添加第三方适配器。每种模式的示例位于 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### 集成非 OpenAI provider 的方式

| 方法 | 适用情况 | 范围 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 一个 OpenAI 兼容端点应作为大多数或所有智能体的默认端点 | 全局默认 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 一个自定义 provider 应应用于单次运行 | 每次运行 |
| [`Agent.model`][agents.agent.Agent.model] | 不同智能体需要不同 provider 或具体模型对象 | 每个智能体 |
| 第三方适配器 | 你需要适配器管理的 provider 覆盖或路由，而内置路径无法提供 | 参见 [第三方适配器](#third-party-adapters) |

你可以通过这些内置路径集成其他 LLM provider：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于你希望全局使用某个 `AsyncOpenAI` 实例作为 LLM 客户端的情况。这适用于 LLM provider 拥有 OpenAI 兼容 API 端点，并且你可以设置 `base_url` 和 `api_key` 的情况。请参阅 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) 中的可配置示例。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 级别。这使你可以声明“在这次运行中为所有智能体使用自定义模型 provider”。请参阅 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) 中的可配置示例。
3. [`Agent.model`][agents.agent.Agent.model] 允许你在特定 Agent 实例上指定模型。这使你可以为不同智能体混合搭配不同 provider。请参阅 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) 中的可配置示例。

如果你没有来自 `platform.openai.com` 的 API key，我们建议通过 `set_tracing_disabled()` 禁用追踪，或设置一个[不同的追踪进程](../tracing.md)。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为许多 LLM provider 仍不支持 Responses API。如果你的 LLM provider 支持 Responses API，我们建议使用 Responses。

## 在一个工作流中混用模型

在单个工作流中，你可能希望为每个智能体使用不同模型。例如，你可以使用更小、更快的模型进行分流，同时使用更大、更强的模型处理复杂任务。配置 [`Agent`][agents.Agent] 时，可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 一个可以将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供一个 [`Model`][agents.models.interface.Model] 实现。

!!! note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 形态，但我们建议每个工作流使用单一模型形态，因为这两种形态支持的功能和工具集合不同。如果你的工作流需要混合搭配模型形态，请确保你使用的所有功能在二者上都可用。

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
2.  提供一个 [`Model`][agents.models.interface.Model] 实现。

当你想进一步配置智能体使用的模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供可选模型配置参数，例如 temperature。

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

当你使用 OpenAI Responses 路径并需要更多控制时，请从 `ModelSettings` 开始。

### 常见高级 `ModelSettings` 选项

当你使用 OpenAI Responses API 时，多个请求字段已经有直接的 `ModelSettings` 字段，因此无需为它们使用 `extra_args`。

- `parallel_tool_calls`：允许或禁止在同一轮中进行多个工具调用。
- `truncation`：设置为 `"auto"`，让 Responses API 在上下文将溢出时丢弃最早的对话项，而不是失败。
- `store`：控制生成的响应是否存储在服务端以供稍后检索。这对于依赖 response ID 的后续工作流，以及可能需要在 `store=False` 时回退到本地输入的会话压缩流程很重要。
- `prompt_cache_retention`：让缓存的提示词前缀保留更久，例如使用 `"24h"`。
- `response_include`：请求更丰富的响应载荷，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。
- `top_logprobs`：请求输出文本的 top-token logprobs。SDK 还会自动添加 `message.output_text.logprobs`。
- `retry`：选择启用由 runner 管理的模型调用重试设置。请参阅 [Runner 管理的重试](#runner-managed-retries)。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.5",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

当你设置 `store=False` 时，Responses API 不会保留该响应以供稍后在服务端检索。这对无状态或零数据保留风格的流程很有用，但也意味着原本会复用 response ID 的功能需要改为依赖本地管理的状态。例如，当最后一个响应未被存储时，[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 会将其默认 `"auto"` 压缩路径切换为基于输入的压缩。请参阅 [Sessions 指南](../sessions/index.md#openai-responses-compaction-sessions)。

### 传递 `extra_args`

当你需要 provider 特定的或更新的请求字段，而 SDK 尚未在顶层直接公开时，请使用 `extra_args`。

此外，当你使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们在顶层不可用，也可以使用 `extra_args` 传入。

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

## Runner 管理的重试

重试仅在运行时生效，且需要显式启用。除非你设置 `ModelSettings(retry=...)` 且你的重试策略选择重试，否则 SDK 不会重试一般模型请求。

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
| `max_retries` | `int | None` | 初始请求之后允许的重试次数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 当策略重试且没有返回显式延迟时使用的默认延迟策略。 |
| `policy` | `RetryPolicy | None` | 决定是否重试的回调。此字段仅在运行时有效，不会被序列化。 |

</div>

重试策略会接收一个 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]，其中包含：

- `attempt` 和 `max_retries`，以便你做出感知尝试次数的决策。
- `stream`，以便你在流式和非流式行为之间分支。
- `error`，用于原始检查。
- `normalized` 事实，例如 `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout` 和 `is_abort`。
- 当底层模型适配器可以提供重试指导时的 `provider_advice`。

策略可以返回以下任一项：

- `True` / `False`，用于简单的重试决策。
- 当你想覆盖延迟或附加诊断原因时，返回 [`RetryDecision`][agents.retry.RetryDecision]。

SDK 在 `retry_policies` 上导出开箱即用的辅助函数：

| 辅助函数 | 行为 |
| --- | --- |
| `retry_policies.never()` | 始终选择不重试。 |
| `retry_policies.provider_suggested()` | 在可用时遵循 provider 重试建议。 |
| `retry_policies.network_error()` | 匹配瞬时传输和超时故障。 |
| `retry_policies.http_status([...])` | 匹配所选 HTTP 状态码。 |
| `retry_policies.retry_after()` | 仅当存在 retry-after 提示时重试，并使用该延迟。 |
| `retry_policies.any(...)` | 当任一嵌套策略选择重试时重试。 |
| `retry_policies.all(...)` | 仅当每个嵌套策略都选择重试时才重试。 |

组合策略时，`provider_suggested()` 是最安全的第一个构建块，因为当 provider 能够区分时，它会保留 provider 的否决和重放安全批准。

##### 安全边界

某些失败永远不会自动重试：

- 中止错误。
- provider 建议将重放标记为不安全的请求。
- 在输出已开始且会导致重放不安全的情况下的流式运行。

使用 `previous_response_id` 或 `conversation_id` 的有状态后续请求也会被更保守地处理。对于这些请求，单独使用 `network_error()` 或 `http_status([500])` 等非 provider 谓词是不够的。重试策略应包含来自 provider 的重放安全批准，通常通过 `retry_policies.provider_suggested()` 实现。

##### Runner 与智能体合并行为

`retry` 会在 runner 级和智能体级 `ModelSettings` 之间进行深度合并：

- 智能体可以只覆盖 `retry.max_retries`，同时仍继承 runner 的 `policy`。
- 智能体可以只覆盖 `retry.backoff` 的一部分，并保留来自 runner 的同级 backoff 字段。
- `policy` 仅在运行时有效，因此序列化的 `ModelSettings` 会保留 `max_retries` 和 `backoff`，但省略回调本身。

更多完整示例，请参阅 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 和 [adapter-backed retry 示例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)。

## 非 OpenAI provider 故障排除

### 追踪客户端错误 401

如果你收到与追踪相关的错误，这是因为 trace 会上传到 OpenAI 服务，而你没有 OpenAI API key。你有三个选项可以解决此问题：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI key：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API key 仅用于上传 trace，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI trace 进程。请参阅 [追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但许多其他 LLM provider 仍不支持它。因此你可能会看到 404 或类似问题。要解决此问题，你有两个选项：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果你通过环境变量设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，这会生效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。示例在[这里](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### structured outputs 支持

一些模型 provider 不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似以下的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是一些模型 provider 的不足之处——它们支持 JSON 输出，但不允许你指定用于输出的 `json_schema`。我们正在修复这个问题，但建议依赖支持 JSON schema 输出的 provider，因为否则你的应用经常会因格式错误的 JSON 而中断。

## 跨 provider 混用模型

你需要了解模型 provider 之间的功能差异，否则可能会遇到错误。例如，OpenAI 支持 structured outputs、多模态输入以及托管的文件检索和网络检索，但许多其他 provider 不支持这些功能。请注意以下限制：

-   不要向不理解这些 `tools` 的 provider 发送不受支持的 `tools`
-   在调用仅文本模型前过滤掉多模态输入
-   注意，不支持结构化 JSON 输出的 provider 偶尔会生成无效 JSON。

## 第三方适配器

只有当 SDK 的内置 provider 集成点不够用时，才考虑使用第三方适配器。如果你在此 SDK 中仅使用 OpenAI 模型，请优先使用内置 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 路径，而不是 Any-LLM 或 LiteLLM。第三方适配器适用于你需要将 OpenAI 模型与非 OpenAI provider 组合，或需要适配器管理的 provider 覆盖或路由，而内置路径无法提供的情况。适配器会在 SDK 与上游模型 provider 之间增加另一层兼容层，因此功能支持和请求语义可能因 provider 而异。SDK 目前包含 Any-LLM 和 LiteLLM，作为尽力而为的 beta 适配器集成。

### Any-LLM

Any-LLM 支持以尽力而为的 beta 形式提供，适用于你需要 Any-LLM 管理的 provider 覆盖或路由的情况。

根据上游 provider 路径，Any-LLM 可能使用 Responses API、Chat Completions 兼容 API，或 provider 特定兼容层。

如果你需要 Any-LLM，请安装 `openai-agents[any-llm]`，然后从 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 或 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) 开始。你可以将 `any-llm/...` 模型名称与 [`MultiProvider`][agents.MultiProvider] 搭配使用，直接实例化 `AnyLLMModel`，或在运行范围内使用 `AnyLLMProvider`。如果你需要显式固定模型接口，请在构造 `AnyLLMModel` 时传入 `api="responses"` 或 `api="chat_completions"`。

Any-LLM 仍是第三方适配器层，因此 provider 依赖和能力差距由上游 Any-LLM 定义，而不是由 SDK 定义。当上游 provider 返回使用量指标时，它们会自动传播，但流式 Chat Completions 后端可能需要先设置 `ModelSettings(include_usage=True)` 才会发出使用量数据块。如果你依赖 structured outputs、工具调用、使用量报告或 Responses 特定行为，请验证你计划部署的确切 provider 后端。

### LiteLLM

LiteLLM 支持以尽力而为的 beta 形式提供，适用于你需要 LiteLLM 特定 provider 覆盖或路由的情况。

如果你需要 LiteLLM，请安装 `openai-agents[litellm]`，然后从 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 或 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) 开始。你可以使用 `litellm/...` 模型名称，或直接实例化 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

一些由 LiteLLM 支持的 provider 默认不会填充 SDK 使用量指标。如果你需要使用量报告，请传入 `ModelSettings(include_usage=True)`，并在依赖 structured outputs、工具调用、使用量报告或适配器特定路由行为时，验证你计划部署的确切 provider 后端。