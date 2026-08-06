---
search:
  exclude: true
---
# 模型

Agents SDK 原生支持两种 OpenAI 模型：

-   **推荐**：使用新 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API 的 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
-   使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API 的 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## 模型设置选择

请从符合您设置的最简单路径开始：

| 如果您希望…… | 推荐路径 | 更多信息 |
| --- | --- | --- |
| 仅使用 OpenAI模型 | 使用默认 OpenAI提供商和 Responses 模型路径 | [OpenAI模型](#openai-models) |
| 通过 WebSocket 传输使用 OpenAI Responses API | 保持使用 Responses 模型路径并启用 WebSocket 传输 | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用由OpenAI托管的子智能体 | 使用实验性托管式多智能体模型 | [托管式多智能体](#hosted-multi-agent-experimental) |
| 使用一个非 OpenAI提供商 | 从内置提供商集成点开始 | [非 OpenAI模型](#non-openai-models) |
| 在多个智能体之间混用模型或提供商 | 按每次运行或每个智能体选择提供商，并检查功能差异 | [在一个工作流中混用模型](#mixing-models-in-one-workflow)和[跨提供商混用模型](#mixing-models-across-providers) |
| 调整高级 OpenAI Responses 请求设置 | 在 OpenAI Responses 路径上使用 `ModelSettings` | [高级 OpenAI Responses 设置](#advanced-openai-responses-settings) |
| 使用第三方适配器实现非 OpenAI或混合提供商路由 | 比较受支持的测试版适配器，并验证您计划发布的提供商路径 | [第三方适配器](#third-party-adapters) |

## OpenAI模型

对于大多数仅使用 OpenAI的应用，推荐路径是配合默认 OpenAI提供商使用字符串模型名称，并继续使用 Responses 模型路径。

如果初始化 `Agent` 时未指定模型，将使用默认模型。目前的默认模型是 [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini)，并使用 `reasoning.effort="none"` 和 `verbosity="low"`，以适应低延迟智能体工作流。如果您拥有访问权限，我们建议将智能体设置为 `gpt-5.6-sol`，以获得更高质量，同时继续显式设置 `model_settings`。

如果您希望切换到 `gpt-5.6-sol` 等其他模型，可通过两种方式配置智能体。

### 默认模型

首先，如果希望所有未设置自定义模型的智能体始终使用某个特定模型，请在运行智能体前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

其次，您可以通过 `RunConfig` 为一次运行设置默认模型。如果未为某个智能体设置模型，将使用本次运行的模型。

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

以这种方式使用 `gpt-5.6-sol` 等任意 GPT-5 模型时，SDK 会应用默认的 `ModelSettings`，其中包含最适合大多数用例的设置。要调整默认模型的推理强度，请传入您自己的 `ModelSettings`：

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

为降低延迟，建议对 GPT-5 模型使用 `reasoning.effort="none"`。

GPT-5.6 还通过现有的 `reasoning` 设置支持推理模式、持久化推理上下文以及 `"max"` 强度级别。这些控制项可用于 Responses API 路径：

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

agent = Agent(
    name="Deep research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        reasoning=Reasoning(
            mode="pro",
            effort="max",
            context="all_turns",
        ),
    ),
)
```

`reasoning.mode` 和 `reasoning.context` 是仅适用于 Responses 的设置。Chat Completions 仅使用 `reasoning.effort`，受支持的强度级别取决于模型和 API 接口。请使用 Responses API 设置 GPT-5.6 的 `"max"` 强度。Chat Completions 适配器会忽略模式和上下文并发出警告；可在 OpenAI提供商上设置 `strict_feature_validation=True`，将该警告转为错误。

使用 `context="all_turns"` 时，请通过 `previous_response_id`、服务端会话或重放先前的推理项来保留对话。对于 `store=False` 的无状态调用，请在响应中包含 `reasoning.encrypted_content`，并在下一个请求中重放这些推理项。

#### ComputerTool 模型选择

如果智能体包含 [`ComputerTool`][agents.tool.ComputerTool]，实际 Responses 请求中的有效模型将决定 SDK 发送哪种计算机工具载荷。显式的 `gpt-5.5` 请求使用正式发布的内置 `computer` 工具，而显式的 `computer-use-preview` 请求继续使用较旧的 `computer_use_preview` 载荷。

由提示词管理的调用是主要例外。如果提示词模板决定模型，且 SDK 在请求中省略 `model`，SDK 将默认使用兼容预览版的计算机载荷，以避免猜测提示词锁定了哪个模型。要在该流程中继续使用正式发布路径，请在请求中显式设置 `model="gpt-5.5"`，或使用 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制使用正式发布选择器。

注册 [`ComputerTool`][agents.tool.ComputerTool] 后，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 会被规范化为与有效请求模型匹配的内置选择器。如果未注册 `ComputerTool`，这些字符串将继续按普通函数名称处理。

兼容预览版的请求必须预先序列化 `environment` 和显示尺寸，因此，使用 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂且由提示词管理的流程，应传入具体的 `Computer` 或 `AsyncComputer` 实例，或者在发送请求前强制使用正式发布选择器。有关完整迁移详情，请参阅[工具](../tools.md#computertool-and-the-responses-computer-tool)。

#### 非 GPT-5 模型

如果传入非 GPT-5 模型名称且未提供自定义 `model_settings`，SDK 将恢复使用与任意模型兼容的通用 `ModelSettings`。

### Responses 专属工具功能

以下工具功能仅受 OpenAI Responses 模型支持：

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 和其他延迟加载的 Responses 工具接口
-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]、`allowed_callers` 和 `tool_choice="programmatic_tool_calling"`

Chat Completions 模型和非 Responses 后端会拒绝这些功能。使用延迟加载工具时，请向智能体添加 `ToolSearchTool()`，并让模型通过 `auto` 或 `required` 工具选择加载工具，而不是强制指定单独的命名空间名称或仅延迟加载的函数名称。有关设置详情和当前限制，请参阅[托管式工具搜索](../tools.md#hosted-tool-search)和[程序化工具调用](../tools.md#programmatic-tool-calling)。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用由OpenAI支持的模型时，您可以选择启用 WebSocket 传输。

#### 基本设置

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI提供商解析的 OpenAI Responses 模型，包括 `"gpt-5.6-sol"` 等字符串模型名称。

SDK 将模型名称解析为模型实例时，会进行传输方式选择。如果传入具体的 [`Model`][agents.models.interface.Model] 对象，其传输方式已固定：[‌`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 WebSocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，而 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 继续使用 Chat Completions。如果传入 `RunConfig(model_provider=...)`，则由该提供商控制传输方式选择，而不是全局默认设置。

#### 提供商或运行级设置

您还可以按提供商或按运行配置 WebSocket 传输：

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

由OpenAI支持的提供商还接受可选的智能体注册配置。这是一个高级选项，适用于 OpenAI设置需要提供商级注册元数据（例如测试框架 ID）的场景。

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

如果需要基于前缀的模型路由，例如在一次运行中混用 `openai/...` 和 `any-llm/...` 模型名称，请使用 [`MultiProvider`][agents.MultiProvider]，并在其中设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留了两项历史默认行为：

-   `openai/...` 被视为 OpenAI提供商的别名，因此 `openai/gpt-4.1` 会作为模型 `gpt-4.1` 进行路由。
-   未知前缀会引发 `UserError`，而不是直接透传。

当 OpenAI提供商指向要求使用字面命名空间模型 ID 的 OpenAI兼容端点时，请显式启用透传行为。在启用了 WebSocket 的设置中，还应在 `MultiProvider` 上保留 `openai_use_responses_websocket=True`：

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

当后端要求使用字面的 `openai/...` 字符串时，请使用 `openai_prefix_mode="model_id"`。当后端要求使用其他命名空间模型 ID（例如 `openrouter/openai/gpt-4.1-mini`）时，请使用 `unknown_prefix_mode="model_id"`。这些选项也适用于 WebSocket 传输之外的 `MultiProvider`；此示例继续启用 WebSocket，是因为它属于本节介绍的传输设置。相同选项也可用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果通过 `MultiProvider` 进行路由时需要相同的提供商级注册元数据，请传入 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`，它会被转发到底层 OpenAI提供商。

如果使用自定义 OpenAI兼容端点或代理，WebSocket 传输还需要兼容的 WebSocket `/responses` 端点。在这些设置中，您可能需要显式设置 `websocket_base_url`。

#### 注意事项

-   这是通过 WebSocket 传输使用的 Responses API，并非 [Realtime API](../realtime/guide.md)。它不适用于 Chat Completions 或非 OpenAI提供商，除非它们支持 Responses WebSocket `/responses` 端点。
-   如果您的环境中尚未安装 `websockets` 软件包，请安装它。
-   启用 WebSocket 传输后，可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望在多个轮次间复用同一 WebSocket 连接的多轮工作流，包括嵌套的“智能体作为工具”调用，建议使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。请参阅[运行智能体](../running_agents.md)指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。
-   对于耗时较长的推理轮次或延迟偶发突增的网络，可通过 `responses_websocket_options` 自定义 WebSocket 保活行为。增大 `ping_timeout` 以容忍延迟的 pong 帧，或设置 `ping_timeout=None`，在继续启用 ping 的同时禁用心跳超时。当可靠性比 WebSocket 延迟更重要时，优先使用 HTTP/SSE 传输。
-   默认情况下，SDK 会禁用传入消息大小限制（`max_size=None`）。对于位于代理之后或运行在内存受限容器中的长生命周期智能体进程，请设置 `responses_websocket_options={"max_size": 8 * 1024 * 1024}`，以限制每条消息的内存使用量。
-   [Responses API WebSocket 服务](https://developers.openai.com/api/docs/guides/websocket-mode)在每个连接上一次处理一个响应，并将每个连接限制为 60 分钟。达到该限制后请打开新连接；需要并行运行时，请使用多个连接。
-   该服务仅在连接本地内存中保留最近一次响应。失败的 `4xx` 或 `5xx` 轮次会逐出引用的 `previous_response_id`。重新连接后，如果存储的响应仍然可用，依然可以继续该响应；但 `store=False` 和 ZDR 流程没有持久化回退方案。请使用 `previous_response_id=None` 启动新的响应链并发送完整输入上下文，或通过本地管理的会话状态重建该上下文。

### 托管式多智能体（实验性）

OpenAI Responses API 托管式多智能体测试版允许 GPT-5.6 根模型创建并协调由服务托管的子智能体。Agents SDK 可以继续使用常规 `Runner`：托管式编排保留在服务上，而开发者定义的工具调用则在您的应用中执行。

此集成是实验性的，并使用 Responses WebSocket 传输，以便通过 `response.inject` 将本地函数输出返回给活动的托管智能体。它要求使用 `openai[realtime]>=2.45.0`，其中包括公开 `client.beta.responses.connect` 的测试版本。在正式发布前，接口和测试版项目架构可能发生变化。

#### 模型配置

从实验性模块导入模型，并将其分配给 SDK `Agent`：

```python
from agents import Agent
from agents.extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel

agent = Agent(
    name="Research coordinator",
    instructions="Delegate independent research tasks, then synthesize the findings.",
    model=OpenAIHostedMultiAgentModel(model="gpt-5.6-sol", config={"max_concurrent_subagents": 3}),
)
```

构造 `OpenAIHostedMultiAgentModel` 会启用 `multi_agent.enabled`，并发送 `OpenAI-Beta: responses_multi_agent=v1` WebSocket 标头。除非提供 `openai_client`，否则该模型会使用默认 OpenAI客户端。如果省略 `max_concurrent_subagents`，则使用服务默认值。

#### 本地工具调用

所有托管智能体共享为请求配置的模型和工具。Responses API 决定由哪个托管智能体调用函数。常规 SDK Runner 在本地执行函数，并将具有相同调用 ID 的 `function_call_output` 注入活动 WebSocket 响应，使服务能够恢复原始托管调用方。函数执行仍会经过 Runner 的常规安全防护措施、钩子和失败转换。SDK 不支持工具审批中断：任何 `needs_approval` 设置不为 `False` 的工具调用，都会在发送请求前被拒绝。

当工具需要感知调用方的日志记录或授权时，请使用 `get_hosted_agent_metadata()`：

```python
from typing import Any

from agents.decorators import tool
from agents.extensions.experimental.hosted_multi_agent import get_hosted_agent_metadata
from agents.tool_context import ToolContext

@tool
def lookup_document(ctx: ToolContext[Any], section: str) -> str:
    metadata = get_hosted_agent_metadata(ctx)
    caller = metadata.agent_name if metadata else "unknown"
    print(f"tool caller: {caller}; call ID: {ctx.tool_call_id}")
    return f"Contents for {section}"
```

托管智能体名称是观察性元数据，并非本地路由机制。请使用 SDK 提供的调用 ID 路由输出。对于有副作用的工具，请将该调用 ID 用作幂等键，并在执行工具之前或期间，通过应用代码执行所有必要的授权；请勿对该模型使用 `needs_approval`。工具参数和输出会跨越 Responses API 边界。

#### 输出与流式传输行为

只有归属于 `/root` 且阶段为 `final_answer` 的消息才会成为常规最终消息。实验性适配器会从高级 `RunResult` 中过滤掉子智能体消息和托管式编排记录；SDK 绝不会将这些记录作为本地函数执行。

原始流式传输会继续公开测试版 Responses 事件，包括托管输出项和 `response.inject.created` 确认。函数调用就绪时，适配器会将一个活动的提供商响应拆分为 SDK 可见的逻辑模型轮次；Runner 生成输出后，再恢复同一个提供商响应。请将 `get_hosted_agent_metadata()` 与原始托管项或 `ToolContext` 配合使用，以检查归属信息。

#### 与 SDK 编排的关系

托管式多智能体与 SDK 任务转移及 agents-as-tools 不同：

-   托管式多智能体会在 OpenAI服务上创建子智能体。您的应用不会创建或调度这些子智能体。
-   SDK 任务转移会更改当前活动的本地 SDK `Agent`。使用此实验性模型时，任务转移会被拒绝，因为每个托管智能体都会收到相同的任务转移工具，从而造成所有权冲突。
-   Agents-as-tools 仍然可用，但使用它们会创建嵌套的客户端和服务端编排。请审慎评估额外的延迟、成本和工具暴露。

#### 当前限制

实验性模型不接受 `reasoning.summary`、`max_tool_calls` 以及调用方提供的 `multi_agent` 或 `betas` 覆盖值。测试版不支持 Responses `/compact` 端点，但可以使用显式的 `context_management.compact_threshold`，因为服务会自动独立压缩每个托管智能体的上下文。

一个 `OpenAIHostedMultiAgentModel` 实例同一时间最多拥有一个活动的托管响应。如果运行在等待本地函数输出时被放弃，请调用 `await model.close()` 释放其 WebSocket。目前不支持在其他进程或事件循环中恢复正在进行的托管响应。

有关底层 Responses API 测试版行为，请参阅 [OpenAI多智能体指南](https://developers.openai.com/api/docs/guides/tools-multi-agent)。有关非流式和流式 SDK 用法，请参阅 [`examples/agent_patterns/hosted_multi_agent_beta.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/hosted_multi_agent_beta.py)。

## 非 OpenAI模型

如果需要非 OpenAI提供商，请从 SDK 的内置提供商集成点开始。在许多设置中，无需添加第三方适配器即可满足需求。每种模式的代码示例位于 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### 非 OpenAI提供商的集成方式

| 方式 | 适用场景 | 作用域 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 一个 OpenAI兼容端点应作为大多数或所有智能体的默认端点 | 全局默认 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 一个自定义提供商应应用于单次运行 | 每次运行 |
| [`Agent.model`][agents.agent.Agent.model] | 不同智能体需要不同的提供商或具体模型对象 | 每个智能体 |
| 第三方适配器 | 您需要由适配器管理的提供商覆盖或内置路径未提供的路由功能 | 请参阅[第三方适配器](#third-party-adapters) |

您可以通过以下内置路径集成其他 LLM 提供商：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于希望在全局范围内使用 `AsyncOpenAI` 实例作为 LLM 客户端的场景。它适用于 LLM 提供商拥有 OpenAI兼容 API 端点，并且您可以设置 `base_url` 和 `api_key` 的情况。可配置的代码示例请参阅 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 层级。您可以借此指定“本次运行中的所有智能体均使用自定义模型提供商”。可配置的代码示例请参阅 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许您为特定 Agent 实例指定模型，从而为不同智能体灵活混用不同的提供商。可配置的代码示例请参阅 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。

如果您没有来自 `platform.openai.com` 的 API 密钥，建议通过 `set_tracing_disabled()` 禁用追踪，或设置[其他追踪进程](../tracing.md)。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    在这些代码示例中，我们使用 Chat Completions API/模型，因为许多 LLM 提供商仍不支持 Responses API。如果您的 LLM 提供商支持 Responses API，我们建议使用 Responses。

## 在一个工作流中混用模型

在单个工作流中，您可能希望为每个智能体使用不同的模型。例如，可以使用较小、较快的模型进行分流，同时使用较大、能力更强的模型处理复杂任务。配置 [`Agent`][agents.Agent] 时，可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称以及可将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!! note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 形式，但由于这两种形式支持不同的功能和工具集，我们建议每个工作流仅使用一种模型形式。如果您的工作流需要混用模型形式，请确保您使用的所有功能均受二者支持。

```python
import asyncio

from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel

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


if __name__ == "__main__":
    asyncio.run(main())
```

1.  直接设置 OpenAI模型的名称。
2.  提供 [`Model`][agents.models.interface.Model] 实现。

如果希望进一步配置智能体所用的模型，可以传入 [`ModelSettings`][agents.model_settings.ModelSettings]，它提供 temperature 等可选模型配置参数。

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

当您使用 OpenAI Responses 路径并需要更多控制时，请首先使用 `ModelSettings`。

### 常用高级 `ModelSettings` 选项

使用 OpenAI Responses API 时，多个请求字段已经拥有直接对应的 `ModelSettings` 字段，因此无需为它们使用 `extra_args`。

- `parallel_tool_calls`：允许或禁止在同一轮中进行多次工具调用。
- `truncation`：设置为 `"auto"`，让 Responses API 在上下文即将溢出时丢弃最早的对话项，而不是让请求失败。
- `store`：控制生成的响应是否存储在服务端，以便稍后检索。这对于依赖响应 ID 的后续工作流，以及在 `store=False` 时可能需要回退到本地输入的会话压缩流程非常重要。
- `context_management`：配置服务端上下文处理，例如通过 `compact_threshold` 进行 Responses 压缩。
- `prompt_cache_retention`：为较早的模型系列配置延长保留时间，例如
  使用 `"24h"`。
- `prompt_cache_options`：选择隐式或显式提示词缓存，并为 GPT-5.6 配置 `"30m"` 缓存 TTL。
- `response_include`：请求更丰富的响应载荷，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。
- `top_logprobs`：请求输出文本中概率最高的 token 对数概率。SDK 还会自动添加 `message.output_text.logprobs`。
- `retry`：选择启用由 Runner 管理的模型调用重试设置。请参阅 [Runner 管理的重试](#runner-managed-retries)。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_options={"mode": "explicit", "ttl": "30m"},
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

使用显式提示词缓存时，请在结束可复用前缀的内容部分添加断点。同一个 `ModelSettings.prompt_cache_options` 字段会透传给 Responses 和 Chat Completions 请求，且 Chat Completions 转换器会保留文本、图像、音频和文件内容部分上的断点。

```python
from agents import Runner

result = await Runner.run(
    research_agent,
    [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Reusable background material...",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {
                    "type": "input_text",
                    "text": "Analyze the latest question.",
                },
            ],
        }
    ],
)
```

对于使用旧版保留控制的较早模型系列，`prompt_cache_retention` 仍然可用。请勿将直接的 `ModelSettings` 字段与 `extra_args` 中的同名键结合使用。

设置 `store=False` 时，Responses API 不会保留该响应供服务端稍后检索。这对于无状态或零数据保留类型的流程很有用，但也意味着原本会复用响应 ID 的功能必须改为依赖本地管理的状态。例如，当最后一个响应未被存储时，[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 会将默认的 `"auto"` 压缩路径切换为基于输入的压缩。请参阅[会话指南](../sessions/index.md#openai-responses-compaction-sessions)。

服务端压缩不同于 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]。`context_management=[{"type": "compaction", "compact_threshold": ...}]` 会随每个 Responses API 请求一同发送；当渲染后的上下文超过阈值时，API 可以将压缩项作为响应的一部分发出。`OpenAIResponsesCompactionSession` 则会在轮次之间调用独立的 `responses.compact` 端点，并重写本地会话历史记录。

### `extra_args` 传递

当您需要 SDK 尚未在顶层直接公开的提供商专属或较新的请求字段时，请使用 `extra_args`。

使用 OpenAI模型时，`extra_args` 可以向 Responses API 和 Chat Completions API 传递可选参数，例如 `user` 和 `service_tier`。对于受支持的模型，可设置 `extra_args={"service_tier": "fast"}` 以使用[快速模式](https://developers.openai.com/api/docs/guides/fast-mode)；`"priority"` 仍与之等效。请勿同时通过直接的 `ModelSettings` 字段设置同一请求字段。

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

重试仅在运行时生效，并且需要主动启用。除非您设置 `ModelSettings(retry=...)`，且您的重试策略选择进行重试，否则 SDK 不会重试常规模型请求。

在 Responses WebSocket 传输中，`retry_policies.provider_suggested()` 会将响应前的过载帧和无代码的 `server_error` 帧识别为重试建议。这本身不会启用重试：您仍然需要 `ModelRetrySettings`，且常规的重放安全检查仍然适用。如果已经收到任何响应事件，SDK 就不会重放请求。

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

`ModelRetrySettings` 包含三个字段：

<div class="field-table" markdown="1">

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `max_retries` | `int | None` | 初始请求后允许的重试次数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 当策略选择重试但未返回显式延迟时使用的默认延迟策略。`backoff.max_delay` 仅限制该计算得出的退避延迟，不限制策略返回的显式延迟或 retry-after 提示。 |
| `policy` | `RetryPolicy | None` | 决定是否重试的回调。该字段仅在运行时生效，不会被序列化。 |

</div>

重试策略会收到一个 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]，其中包含：

- `attempt` 和 `max_retries`，以便根据尝试次数做出决策。
- `stream`，以便区分流式和非流式行为。
- `error`，用于原始检查。
- `normalized` 信息，例如 `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout` 和 `is_abort`。
- `provider_advice`，用于底层模型适配器能够提供重试指导的情况。

策略可以返回以下任一内容：

- `True` / `False`，表示简单的重试决策。
- 当您希望覆盖延迟或附加诊断原因时，返回 [`RetryDecision`][agents.retry.RetryDecision]。

SDK 在 `retry_policies` 上导出了现成的辅助函数：

| 辅助函数 | 行为 |
| --- | --- |
| `retry_policies.never()` | 始终不重试。 |
| `retry_policies.provider_suggested()` | 在有可用建议时遵循提供商的重试建议。 |
| `retry_policies.network_error()` | 匹配暂时性传输失败和超时失败。 |
| `retry_policies.http_status([...])` | 匹配选定的 HTTP 状态码。 |
| `retry_policies.retry_after()` | 仅在存在 retry-after 提示时重试，并使用该延迟。该辅助函数将 retry-after 值视为显式策略延迟，因此 `backoff.max_delay` 不会限制它。 |
| `retry_policies.any(...)` | 任一嵌套策略选择重试时进行重试。 |
| `retry_policies.all(...)` | 仅当所有嵌套策略均选择重试时进行重试。 |

组合策略时，`provider_suggested()` 是最安全的首选基础组件，因为当提供商能够区分拒绝重试和重放安全批准时，它会保留这些信息。

##### 安全边界

某些失败绝不会自动重试：

- 中止错误。
- 提供商建议将重放标记为不安全的请求。
- 输出已开始，且重放会变得不安全的流式运行。

使用 `previous_response_id` 或 `conversation_id` 的有状态后续请求也会得到更保守的处理。对于这些请求，`network_error()` 或 `http_status([500])` 等非提供商判断条件本身并不足够。重试策略应包含来自提供商的重放安全批准，通常通过 `retry_policies.provider_suggested()` 实现。

##### Runner 与智能体的合并行为

Runner 级与智能体级 `ModelSettings` 之间会深度合并 `retry`：

- 智能体可以仅覆盖 `retry.max_retries`，并继续继承 Runner 的 `policy`。
- 智能体可以仅覆盖 `retry.backoff` 的一部分，并保留来自 Runner 的其他同级退避字段。
- `policy` 仅在运行时生效，因此序列化后的 `ModelSettings` 会保留 `max_retries` 和 `backoff`，但省略回调本身。

有关更完整的代码示例，请参阅 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 和[由适配器支持的重试示例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)。

## 非 OpenAI提供商故障排除

### 追踪客户端 401 错误

如果出现与追踪相关的错误，这是因为追踪数据会上传到 OpenAI服务，而您没有 OpenAI API 密钥。您可以通过以下三种方式解决：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI密钥：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API 密钥仅用于上传追踪数据，并且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI追踪进程。请参阅[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但许多其他 LLM 提供商仍不支持它。因此，您可能会遇到 404 或类似问题。您可以通过以下两种方式解决：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果您通过环境变量设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，此方式有效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)提供了代码示例。

### Chat Completions 兼容性选项

通过 Chat Completions 进行路由时，SDK 会静默丢弃 Chat Completions 无法发送的 Responses 专属字段，以保持兼容性，例如 `previous_response_id`、`conversation_id`、提示词或非纯文本工具输出。如果您希望这些不匹配问题在开发期间快速失败，请在 OpenAI提供商上启用严格功能验证：

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

某些 OpenAI兼容 Chat Completions 提供商会分块流式传输工具调用增量，但这些数据不足以支持可靠的 SDK 增量处理。在这种情况下，请启用流式工具调用缓冲，使 SDK 仅在提供商流结束后发出工具调用：

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

对于 [`MultiProvider`][agents.MultiProvider]，请使用 `openai_buffer_streamed_tool_calls=True`。

### structured outputs 支持

部分模型提供商不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似以下内容的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是部分模型提供商的不足之处——它们支持 JSON 输出，但不允许您指定用于输出的 `json_schema`。我们正在解决此问题，但建议依赖支持 JSON schema 输出的提供商，否则您的应用经常会因 JSON 格式错误而中断。

## 跨提供商混用模型

您需要注意模型提供商之间的功能差异，否则可能遇到错误。例如，OpenAI支持 structured outputs、多模态输入以及托管式文件检索和网络检索，但许多其他提供商并不支持这些功能。请注意以下限制：

-   不要向无法理解的提供商发送不受支持的 `tools`
-   调用纯文本模型前，请过滤掉多模态输入
-   请注意，不支持结构化 JSON 输出的提供商偶尔会生成无效 JSON。

## 第三方适配器

仅当 SDK 的内置提供商集成点无法满足需求时，才应使用第三方适配器。如果您仅通过此 SDK 使用 OpenAI模型，请优先使用内置 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 路径，而不是 Any-LLM 或 LiteLLM。第三方适配器适用于需要将 OpenAI模型与非 OpenAI提供商结合使用，或需要由适配器管理的提供商覆盖或内置路径未提供的路由功能的场景。适配器会在 SDK 与上游模型提供商之间增加一个兼容层，因此功能支持和请求语义可能因提供商而异。SDK 目前以尽力支持的测试版集成形式包含 Any-LLM 和 LiteLLM。

### Any-LLM

对于需要由 Any-LLM 管理提供商覆盖或路由的场景，我们会以尽力支持的测试版形式提供 Any-LLM 支持。

根据上游提供商路径，Any-LLM 可能使用 Responses API、Chat Completions 兼容 API 或提供商专属兼容层。

如果需要 Any-LLM，请安装 `openai-agents[any-llm]`，然后从 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 或 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) 开始。您可以将 `any-llm/...` 模型名称与 [`MultiProvider`][agents.MultiProvider] 配合使用、直接实例化 `AnyLLMModel`，或在运行作用域中使用 `AnyLLMProvider`。如果需要显式锁定模型接口，请在构造 `AnyLLMModel` 时传入 `api="responses"` 或 `api="chat_completions"`。

Any-LLM 仍然是第三方适配层，因此提供商依赖项和能力缺口由上游 Any-LLM 定义，而非 SDK。上游提供商返回使用量指标时，这些指标会自动传播，但流式 Chat Completions 后端可能需要设置 `ModelSettings(include_usage=True)` 才会发出使用量数据块。如果您依赖 structured outputs、工具调用、使用量报告或 Responses 专属行为，请验证计划部署的确切提供商后端。

### LiteLLM

对于需要 LiteLLM 专属提供商覆盖或路由的场景，我们会以尽力支持的测试版形式提供 LiteLLM 支持。

如果需要 LiteLLM，请安装 `openai-agents[litellm]`，然后从 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 或 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) 开始。您可以使用 `litellm/...` 模型名称，也可以直接实例化 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

某些由 LiteLLM 支持的提供商默认不会填充 SDK 使用量指标。如果需要使用量报告，请传入 `ModelSettings(include_usage=True)`；如果您依赖 structured outputs、工具调用、使用量报告或适配器专属路由行为，请验证计划部署的确切提供商后端。

如果 LiteLLM 针对响应对象发出 Pydantic 序列化器警告，您可以在导入 LiteLLM 适配器前选择启用 SDK 的兼容性补丁：

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

该补丁默认禁用，仅在值为 `1` 或 `true` 时启用。它通过包装 LiteLLM 的私有日志辅助函数，抑制一类特定的 LiteLLM 响应序列化警告，因此应将其视为针对性权宜方案，而非通用序列化设置。由于它依赖 LiteLLM 的私有 API，升级 LiteLLM 时请重新验证该补丁；当上游不再出现该警告时，请移除该环境变量。