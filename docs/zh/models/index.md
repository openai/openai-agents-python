---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用支持两种 OpenAI 模型方式：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，使用新的[Responses API](https://platform.openai.com/docs/api-reference/responses)调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，使用[Chat Completions API](https://platform.openai.com/docs/api-reference/chat)调用 OpenAI API。

## 模型设置选择

从最适合你当前设置的最简单路径开始：

| 如果你想要…… | 推荐路径 | 了解更多 |
| --- | --- | --- |
| 仅使用 OpenAI 模型 | 使用默认 OpenAI provider 和 Responses 模型路径 | [OpenAI 模型](#openai-models) |
| 通过 websocket 传输使用 OpenAI Responses API | 保持 Responses 模型路径并启用 websocket 传输 | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用一个非 OpenAI provider | 从内置 provider 集成点开始 | [非 OpenAI 模型](#non-openai-models) |
| 在多个智能体之间混用模型或 provider | 按每次 run 或每个智能体选择 provider，并检查功能差异 | [在单个工作流中混合模型](#mixing-models-in-one-workflow) 和 [跨 provider 混合模型](#mixing-models-across-providers) |
| 调整高级 OpenAI Responses 请求设置 | 在 OpenAI Responses 路径上使用 `ModelSettings` | [高级 OpenAI Responses 设置](#advanced-openai-responses-settings) |
| 使用第三方适配器进行非 OpenAI 或混合 provider 路由 | 比较受支持的 beta 适配器并验证你计划上线的 provider 路径 | [第三方适配器](#third-party-adapters) |

## OpenAI 模型

对于大多数仅使用 OpenAI 的应用，推荐路径是使用字符串模型名称、默认 OpenAI provider，并保持在 Responses 模型路径上。

当你在初始化 `Agent` 时未指定模型，将使用默认模型。当前默认值是 [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1)，以兼容性和低延迟为优先。如果你有权限，我们建议将智能体设置为 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) 以获得更高质量，同时显式设置 `model_settings`。

如果你想切换到其他模型（如 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4)），有两种方式可配置智能体。

### 默认模型

首先，如果你希望所有未设置自定义模型的智能体都稳定使用某个特定模型，请在运行智能体前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

其次，你可以通过 `RunConfig` 为一次 run 设置默认模型。如果未为智能体设置模型，将使用该 run 的模型。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.4"),
)
```

#### GPT-5 模型

当你以这种方式使用任意 GPT-5 模型（如 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4)）时，SDK 会应用默认 `ModelSettings`。它会设置在大多数用例中表现最佳的项。若要调整默认模型的推理强度，请传入你自己的 `ModelSettings`：

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.4 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.4",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

为了更低延迟，推荐在 `gpt-5.4` 上使用 `reasoning.effort="none"`。gpt-4.1 系列（包括 mini 和 nano 变体）同样是构建交互式智能体应用的可靠选择。

#### ComputerTool 模型选择

如果某个智能体包含 [`ComputerTool`][agents.tool.ComputerTool]，实际 Responses 请求中的有效模型会决定 SDK 发送哪种 computer-tool payload。显式的 `gpt-5.4` 请求会使用 GA 内置 `computer` 工具，而显式的 `computer-use-preview` 请求会保留旧版 `computer_use_preview` payload。

提示词托管调用是主要例外。如果提示词模板控制模型且 SDK 在请求中省略 `model`，SDK 会默认使用与 preview 兼容的 computer payload，以避免猜测提示词绑定了哪个模型。要在该流程中保持 GA 路径，可在请求中显式设置 `model="gpt-5.4"`，或使用 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制 GA 选择器。

在已注册 [`ComputerTool`][agents.tool.ComputerTool] 的情况下，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 会被标准化为与有效请求模型匹配的内置选择器。如果未注册 `ComputerTool`，这些字符串仍按普通函数名处理。

与 preview 兼容的请求必须预先序列化 `environment` 和显示尺寸，因此在使用 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂的提示词托管流程中，应传入具体的 `Computer` 或 `AsyncComputer` 实例，或在发送请求前强制 GA 选择器。完整迁移细节见 [Tools](../tools.md#computertool-and-the-responses-computer-tool)。

#### 非 GPT-5 模型

如果你传入非 GPT-5 模型名且未提供自定义 `model_settings`，SDK 会回退到与任意模型兼容的通用 `ModelSettings`。

### 仅 Responses 的工具检索功能

以下工具功能仅在 OpenAI Responses 模型中受支持：

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 及其他延迟加载的 Responses 工具接口

这些功能在 Chat Completions 模型和非 Responses 后端上会被拒绝。使用延迟加载工具时，请将 `ToolSearchTool()` 添加到智能体，并让模型通过 `auto` 或 `required` 的工具选择来加载工具，而不是强制使用裸命名空间名称或仅延迟加载函数名。设置细节和当前限制见 [Tools](../tools.md#hosted-tool-search)。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用 OpenAI 支持的模型时，你可以选择启用 websocket 传输。

#### 基础设置

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI provider 解析的 OpenAI Responses 模型（包括 `"gpt-5.4"` 这类字符串模型名）。

传输方式选择发生在 SDK 将模型名解析为模型实例时。如果你传入具体的 [`Model`][agents.models.interface.Model] 对象，其传输方式已固定：[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 websocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 保持在 Chat Completions。若你传入 `RunConfig(model_provider=...)`，则由该 provider 控制传输选择，而非全局默认值。

#### provider 或 run 级设置

你也可以按 provider 或每次 run 配置 websocket 传输：

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

OpenAI 支持的 provider 还接受可选的智能体注册配置。这是高级选项，用于你的 OpenAI 设置需要 provider 级注册元数据（例如 harness ID）的场景。

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

如果你需要基于前缀的模型路由（例如在一次 run 中混用 `openai/...` 与 `any-llm/...` 模型名），请使用 [`MultiProvider`][agents.MultiProvider] 并在其中设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留两个历史默认行为：

-   `openai/...` 被视为 OpenAI provider 的别名，因此 `openai/gpt-4.1` 会被路由为模型 `gpt-4.1`。
-   未知前缀会抛出 `UserError`，而不是透传。

当你将 OpenAI provider 指向一个期待字面命名空间模型 ID 的 OpenAI 兼容端点时，请显式启用透传行为。在启用 websocket 的设置中，也请在 `MultiProvider` 上保留 `openai_use_responses_websocket=True`：

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

当后端期望字面 `openai/...` 字符串时，使用 `openai_prefix_mode="model_id"`。当后端期望其他命名空间模型 ID（如 `openrouter/openai/gpt-4.1-mini`）时，使用 `unknown_prefix_mode="model_id"`。这些选项在非 websocket 传输的 `MultiProvider` 上同样可用；此示例保持 websocket 启用，因为本节描述的是传输设置。这些选项同样可用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果你在通过 `MultiProvider` 路由时也需要相同的 provider 级注册元数据，可传入 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`，它会被转发到底层 OpenAI provider。

如果你使用自定义 OpenAI 兼容端点或代理，websocket 传输还要求兼容的 websocket `/responses` 端点。在这些设置中，你可能需要显式设置 `websocket_base_url`。

#### 说明

-   这是基于 websocket 传输的 Responses API，不是 [Realtime API](../realtime/guide.md)。除非它们支持 Responses websocket `/responses` 端点，否则不适用于 Chat Completions 或非 OpenAI provider。
-   如果你的环境中尚未安装，请安装 `websockets` 包。
-   启用 websocket 传输后，你可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望在多轮工作流（及嵌套 Agents-as-tools 调用）中复用同一 websocket 连接的场景，推荐使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。参见[运行智能体](../running_agents.md)指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。

## 非 OpenAI 模型

如果你需要非 OpenAI provider，请先从 SDK 内置的 provider 集成点开始。很多场景下无需引入第三方适配器。每种模式的示例见 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### 集成非 OpenAI provider 的方式

| 方式 | 适用场景 | 范围 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 一个 OpenAI 兼容端点应作为大多数或全部智能体的默认值 | 全局默认 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 一个自定义 provider 应用于单次 run | 每次 run |
| [`Agent.model`][agents.agent.Agent.model] | 不同智能体需要不同 provider 或具体模型对象 | 每个智能体 |
| 第三方适配器 | 你需要内置路径无法提供的适配器托管 provider 覆盖或路由 | 见[第三方适配器](#third-party-adapters) |

你可以通过这些内置路径集成其他 LLM provider：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于你希望全局使用 `AsyncOpenAI` 实例作为 LLM 客户端的情况。适合 LLM provider 提供 OpenAI 兼容 API 端点，且你可设置 `base_url` 与 `api_key`。可配置示例见 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 层级。可用于声明“本次 run 的所有智能体都使用自定义模型 provider”。可配置示例见 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许你在特定 Agent 实例上指定模型。这使你可以为不同智能体混合搭配不同 provider。可配置示例见 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。

在你没有 `platform.openai.com` 的 API key 时，我们建议通过 `set_tracing_disabled()` 禁用追踪，或配置[不同的追踪进程](../tracing.md)。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为许多 LLM provider 仍不支持 Responses API。如果你的 LLM provider 支持，我们建议使用 Responses。

## 在单个工作流中混合模型

在单个工作流中，你可能希望每个智能体使用不同模型。例如，你可以在分流阶段使用更小更快的模型，在复杂任务中使用更大更强的模型。配置 [`Agent`][agents.Agent] 时，你可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 可将该名称映射为 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!! note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 与 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形态，但我们建议每个工作流只使用一种模型形态，因为两者支持的功能和工具集不同。如果你的工作流必须混用模型形态，请确保所用功能在两者上都可用。

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
    model="gpt-5.4",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  直接设置 OpenAI 模型名称。
2.  提供 [`Model`][agents.models.interface.Model] 实现。

当你希望进一步配置智能体所用模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供诸如 temperature 等可选模型配置参数。

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

当你使用 OpenAI Responses 路径且需要更多控制时，请从 `ModelSettings` 开始。

### 常见高级 `ModelSettings` 选项

在使用 OpenAI Responses API 时，若干请求字段在 `ModelSettings` 中已有直接对应字段，因此你无需为其使用 `extra_args`。

- `parallel_tool_calls`：允许或禁止同一轮中的多个工具调用。
- `truncation`：设置为 `"auto"`，让 Responses API 在上下文将溢出时丢弃最旧对话项，而不是直接失败。
- `store`：控制是否将生成的响应存储在服务端以供后续检索。这会影响依赖响应 ID 的后续工作流，以及在 `store=False` 时可能需要回退到本地输入的会话压缩流程。
- `prompt_cache_retention`：更长时间保留缓存的提示词前缀，例如 `"24h"`。
- `response_include`：请求更丰富的响应 payload，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。
- `top_logprobs`：为输出文本请求 top-token logprobs。SDK 还会自动添加 `message.output_text.logprobs`。
- `retry`：为模型调用启用由 runner 管理的重试设置。参见[Runner 管理的重试](#runner-managed-retries)。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.4",
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

当你设置 `store=False` 时，Responses API 不会保留该响应供后续服务端检索。这对无状态或零数据保留风格流程很有用，但也意味着原本可复用响应 ID 的功能需要改为依赖本地管理状态。例如，[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 在最后一次响应未被存储时，会将默认的 `"auto"` 压缩路径切换为基于输入的压缩。参见[Sessions 指南](../sessions/index.md#openai-responses-compaction-sessions)。

### 传入 `extra_args`

当你需要 SDK 尚未在顶层直接暴露的 provider 特定字段或更新请求字段时，请使用 `extra_args`。

另外，使用 OpenAI 的 Responses API 时，[还有一些可选参数](https://platform.openai.com/docs/api-reference/responses/create)（如 `user`、`service_tier` 等）。若它们在顶层不可用，也可通过 `extra_args` 传入。

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

重试仅在运行时生效且为显式启用。除非你设置 `ModelSettings(retry=...)` 且重试策略选择重试，否则 SDK 不会重试一般模型请求。

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.4",
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
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 当策略重试但未返回显式延迟时使用的默认延迟策略。 |
| `policy` | `RetryPolicy | None` | 决定是否重试的回调。此字段仅运行时有效，不会被序列化。 |

</div>

重试策略会收到一个包含以下内容的 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]：

- `attempt` 和 `max_retries`，用于按尝试次数做决策。
- `stream`，用于区分流式与非流式行为分支。
- `error`，用于原始检查。
- `normalized` 事实，如 `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout` 和 `is_abort`。
- 当底层模型适配器可提供重试指导时的 `provider_advice`。

策略可返回：

- `True` / `False`，用于简单重试决策。
- [`RetryDecision`][agents.retry.RetryDecision]，当你想覆盖延迟或附加诊断原因时。

SDK 在 `retry_policies` 中提供现成辅助函数：

| 辅助函数 | 行为 |
| --- | --- |
| `retry_policies.never()` | 始终不重试。 |
| `retry_policies.provider_suggested()` | 在可用时遵循 provider 重试建议。 |
| `retry_policies.network_error()` | 匹配瞬时传输与超时失败。 |
| `retry_policies.http_status([...])` | 匹配选定 HTTP 状态码。 |
| `retry_policies.retry_after()` | 仅在存在 retry-after 提示时重试，并使用该延迟。 |
| `retry_policies.any(...)` | 任一嵌套策略选择重试即重试。 |
| `retry_policies.all(...)` | 仅当所有嵌套策略都选择重试时才重试。 |

组合策略时，`provider_suggested()` 是最安全的首个构件，因为当 provider 可区分时，它会保留 provider 的否决和重放安全批准。

##### 安全边界

某些失败永远不会自动重试：

- Abort 错误。
- provider 建议标记为重放不安全的请求。
- 流式 run 中已开始输出且重放会不安全的情况。

使用 `previous_response_id` 或 `conversation_id` 的有状态后续请求也会被更保守处理。对这类请求，仅使用 `network_error()` 或 `http_status([500])` 等非 provider 条件本身并不足够。重试策略应包含来自 provider 的重放安全批准，通常通过 `retry_policies.provider_suggested()`。

##### Runner 与智能体的合并行为

`retry` 会在 runner 级与智能体级 `ModelSettings` 间进行深度合并：

- 智能体可只覆盖 `retry.max_retries`，同时继承 runner 的 `policy`。
- 智能体可只覆盖 `retry.backoff` 的部分字段，并保留 runner 中同级其他 backoff 字段。
- `policy` 仅运行时有效，因此序列化后的 `ModelSettings` 会保留 `max_retries` 和 `backoff`，但省略回调本身。

更完整示例见 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 和[基于适配器的重试示例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)。

## 非 OpenAI provider 故障排查

### 追踪客户端错误 401

如果你收到与追踪相关的错误，这是因为追踪会上传到 OpenAI 服务端，而你没有 OpenAI API key。你有三种解决方式：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI key：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。该 API key 仅用于上传追踪，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 的追踪进程。见[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但许多其他 LLM provider 仍不支持。因此你可能会遇到 404 或类似问题。可通过两种方式解决：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。当你通过环境变量设置 `OPENAI_API_KEY` 与 `OPENAI_BASE_URL` 时可用。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。示例见[这里](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### structured outputs 支持

某些模型 provider 不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似如下错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型 provider 的不足——它们支持 JSON 输出，但不允许你指定输出使用的 `json_schema`。我们正在修复此问题，但建议依赖支持 JSON schema 输出的 provider，否则你的应用会经常因 JSON 格式错误而中断。

## 跨 provider 混合模型

你需要了解不同模型 provider 的功能差异，否则可能遇到错误。例如，OpenAI 支持 structured outputs、多模态输入，以及托管的文件检索和网络检索，但许多其他 provider 不支持这些功能。请注意以下限制：

-   不要向不支持的 provider 发送它们无法理解的 `tools`
-   在调用纯文本模型前过滤掉多模态输入
-   注意不支持结构化 JSON 输出的 provider 会偶尔产生无效 JSON

## 第三方适配器

仅当 SDK 内置 provider 集成点不足时，才使用第三方适配器。如果你在本 SDK 中只使用 OpenAI 模型，优先选择内置 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 路径，而不是 Any-LLM 或 LiteLLM。第三方适配器适用于需要将 OpenAI 模型与非 OpenAI provider 组合使用，或需要内置路径无法提供的适配器托管 provider 覆盖或路由的场景。适配器在 SDK 与上游模型 provider 之间增加了一层兼容层，因此功能支持与请求语义可能因 provider 而异。SDK 当前以尽力而为的 beta 集成方式包含 Any-LLM 和 LiteLLM。

### Any-LLM

Any-LLM 支持以尽力而为的 beta 形式提供，适用于你需要 Any-LLM 托管的 provider 覆盖或路由的场景。

根据上游 provider 路径，Any-LLM 可能使用 Responses API、Chat Completions 兼容 API，或 provider 特定兼容层。

如果你需要 Any-LLM，请安装 `openai-agents[any-llm]`，然后从 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 或 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) 开始。你可以在 [`MultiProvider`][agents.MultiProvider] 中使用 `any-llm/...` 模型名，直接实例化 `AnyLLMModel`，或在 run 范围使用 `AnyLLMProvider`。如果你需要显式固定模型接口，构造 `AnyLLMModel` 时传入 `api="responses"` 或 `api="chat_completions"`。

Any-LLM 仍是第三方适配器层，因此 provider 依赖与能力缺口由 Any-LLM 上游定义，而非由 SDK 定义。当上游 provider 返回用量指标时会自动透传，但流式 Chat Completions 后端可能需要先设置 `ModelSettings(include_usage=True)` 才会输出 usage 块。如果你依赖 structured outputs、工具调用、用量上报或 Responses 特定行为，请验证计划部署的具体 provider 后端。

### LiteLLM

LiteLLM 支持以尽力而为的 beta 形式提供，适用于你需要 LiteLLM 特定 provider 覆盖或路由的场景。

如果你需要 LiteLLM，请安装 `openai-agents[litellm]`，然后从 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 或 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) 开始。你可以使用 `litellm/...` 模型名，或直接实例化 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

某些 LiteLLM 支持的 provider 默认不会填充 SDK 用量指标。如果你需要用量上报，请传入 `ModelSettings(include_usage=True)`；若你依赖 structured outputs、工具调用、用量上报或适配器特定路由行为，请验证计划部署的具体 provider 后端。