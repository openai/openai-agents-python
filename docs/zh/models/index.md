---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用地支持两种 OpenAI 模型：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，使用新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，使用 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## 模型配置选择 {#choosing-a-model-setup}

从最符合您配置的最简单方案开始：

| 如果您希望…… | 推荐方案 | 详细信息 |
| --- | --- | --- |
| 仅使用 OpenAI模型 | 使用默认 OpenAI提供商和 Responses 模型路径 | [OpenAI模型](#openai-models) |
| 通过 WebSocket 传输使用 OpenAI Responses API | 保持使用 Responses 模型路径，并启用 WebSocket 传输 | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用由OpenAI托管的子智能体 | 使用实验性的托管多智能体模型 | [托管多智能体](#hosted-multi-agent-experimental) |
| 使用一个非 OpenAI提供商 | 从内置提供商集成点开始 | [非 OpenAI模型](#non-openai-models) |
| 在不同智能体之间混用模型或提供商 | 按运行或智能体选择提供商，并检查功能差异 | [在一个工作流中混用模型](#mixing-models-in-one-workflow)和[跨提供商混用模型](#mixing-models-across-providers) |
| 调整高级 OpenAI Responses 请求设置 | 在 OpenAI Responses 路径上使用 `ModelSettings` | [高级 OpenAI Responses 设置](#advanced-openai-responses-settings) |
| 使用第三方适配器进行非 OpenAI或混合提供商路由 | 比较受支持的 Beta 版适配器，并验证您计划发布的提供商路径 | [第三方适配器](#third-party-adapters) |

## OpenAI模型 {#openai-models}

对于大多数仅使用 OpenAI的应用，推荐使用默认 OpenAI提供商配合字符串模型名称，并保持使用 Responses 模型路径。

当 [`Agent`][agents.agent.Agent] 未指定模型时，对于成本敏感的高吞吐量智能体工作流，Agents SDK 默认使用 [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna)，并搭配 `reasoning.effort="none"` 和 `verbosity="low"`。需要前沿能力的应用可以显式设置 `model="gpt-5.6-sol"`，并选择适合工作负载的 `model_settings`。

如果您想切换到 `gpt-5.6-sol` 等其他模型，可以通过两种方式配置智能体。

### 默认模型 {#default-model}

首先，如果您希望所有未设置自定义模型的智能体始终使用某个特定模型，请在运行智能体之前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

其次，您可以通过 `RunConfig` 为某次运行设置默认模型。如果未为智能体设置模型，则会使用本次运行的模型。

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

#### GPT-5 模型 {#gpt-5-models}

以这种方式使用任何 GPT-5 模型（例如 `gpt-5.6-sol`）时，SDK 会应用默认的 `ModelSettings`，其中设置了适合大多数用例的最佳选项。若要调整默认模型的推理强度，请传入您自己的 `ModelSettings`：

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

为了降低延迟，建议将 GPT-5 模型与 `reasoning.effort="none"` 搭配使用。

GPT-5.6 还通过现有的 `reasoning` 设置支持推理模式、跨对话轮次保留的推理上下文，以及 `"max"` 强度级别。这些控制项可在 Responses API 路径上使用：

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

`reasoning.mode` 和 `reasoning.context` 是仅适用于 Responses 的设置。Chat Completions 仅使用 `reasoning.effort`，且支持的强度级别取决于模型和 API 接口。若要使用 GPT-5.6 的 `"max"` 强度，请使用 Responses API。Chat Completions 适配器会忽略模式和上下文并发出警告；在 OpenAI提供商上设置 `strict_feature_validation=True`，可将该警告转为错误。

使用 `context="all_turns"` 时，请通过 `previous_response_id`、服务端 Responses API 对话，或在下一个请求中包含先前的推理项来保留对话。对于无状态的 `store=False` 调用，请在响应中请求 `reasoning.encrypted_content`，然后在下一个请求中将这些推理项作为输入。

#### ComputerTool 模型选择 {#computertool-model-selection}

如果智能体包含 [`ComputerTool`][agents.tool.ComputerTool]，则实际 Responses 请求上的有效模型将决定 SDK 发送哪种计算机工具载荷。显式的 `gpt-5.5` 请求使用正式发布的内置 `computer` 工具，而显式的 `computer-use-preview` 请求继续使用较旧的 `computer_use_preview` 载荷。

由提示词管理的调用是主要例外。如果提示词模板指定了模型，而 SDK 在请求中省略了 `model`，SDK 将默认使用与预览版兼容的计算机载荷，从而避免猜测提示词固定的是哪个模型。若要在该流程中继续使用正式发布路径，请在请求中显式指定 `model="gpt-5.5"`，或使用 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制选择正式发布版本。

注册 [`ComputerTool`][agents.tool.ComputerTool] 后，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 会规范化为与有效请求模型匹配的内置选择器。如果未注册 `ComputerTool`，这些字符串会继续像普通函数名称一样工作。

与预览版兼容的请求必须预先序列化 `environment` 和显示尺寸，因此，使用 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂、由提示词管理的流程应传入具体的 `Computer` 或 `AsyncComputer` 实例，或者在发送请求前强制使用正式发布版选择器。有关完整迁移详情，请参阅[工具](../tools.md#computertool-and-the-responses-computer-tool)。

#### 非 GPT-5 模型 {#non-gpt-5-models}

如果您传入非 GPT-5 模型名称且未提供自定义 `model_settings`，SDK 将恢复为与任何模型兼容的通用 `ModelSettings`。

### Responses 专属工具功能 {#responses-only-tool-features}

以下工具功能仅受 OpenAI Responses 模型支持：

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 及其他延迟加载的 Responses 工具接口
-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]、`allowed_callers` 和 `tool_choice="programmatic_tool_calling"`

Chat Completions 模型和非 Responses 后端会拒绝这些功能。使用延迟加载工具时，请将 `ToolSearchTool()` 添加到智能体，并让模型通过 `auto` 或 `required` 工具选择来加载工具，而不是强制使用纯命名空间名称或仅限延迟加载的函数名称。有关配置详情和当前限制，请参阅[托管工具搜索](../tools.md#hosted-tool-search)和[程序化工具调用](../tools.md#programmatic-tool-calling)。

### Responses WebSocket 传输 {#responses-websocket-transport}

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用 OpenAI Responses 提供商路径时，您可以选择启用 WebSocket 传输。

#### 基本配置 {#basic-setup}

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响默认 OpenAI提供商解析模型名称后得到的 OpenAI Responses 模型，包括 `"gpt-5.6-sol"` 等字符串模型名称。

传输方式的选择发生在 SDK 将模型名称解析为模型实例时。如果传入具体的 [`Model`][agents.models.interface.Model] 对象，其传输方式已经固定：[ `OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 WebSocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，而 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 仍使用 Chat Completions。如果传入 `RunConfig(model_provider=...)`，则由该提供商而非全局默认设置控制传输方式的选择。

#### 提供商或运行级配置 {#provider-or-run-level-setup}

您也可以按提供商或按运行配置 WebSocket 传输：

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

通过 SDK 的 OpenAI集成进行路由的提供商也接受可选的智能体注册配置。这是一个高级选项，适用于 OpenAI配置需要提供商级注册元数据（例如框架 ID）的情况。

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

#### 使用 `MultiProvider` 的高级路由 {#advanced-routing-with-multiprovider}

如果您需要基于前缀的模型路由，例如在一次运行中混用 `openai/...` 和 `any-llm/...` 模型名称，请使用 [`MultiProvider`][agents.MultiProvider]，并在其中设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留了两个历史默认行为：

-   `openai/...` 被视为 OpenAI提供商的别名，因此 `openai/gpt-4.1` 会以模型 `gpt-4.1` 进行路由。
-   未知前缀会引发 `UserError`，而不是直接透传。

当您将 OpenAI提供商指向要求使用字面命名空间模型 ID 的 OpenAI兼容端点时，请显式启用透传行为。在启用 WebSocket 的配置中，也要在 `MultiProvider` 上保留 `openai_use_responses_websocket=True`：

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

当后端要求使用字面量 `openai/...` 字符串时，请使用 `openai_prefix_mode="model_id"`。当后端要求使用其他命名空间模型 ID（例如 `openrouter/openai/gpt-4.1-mini`）时，请使用 `unknown_prefix_mode="model_id"`。这些选项也适用于 WebSocket 传输之外的 `MultiProvider`；此示例保持启用 WebSocket，因为它属于本节所述的传输配置。这些选项同样适用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果通过 `MultiProvider` 路由时需要相同的提供商级注册元数据，请传入 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`，它将被转发到下层 OpenAI提供商。

如果使用自定义 OpenAI兼容端点或代理，WebSocket 传输还要求提供兼容的 WebSocket `/responses` 端点。在这些配置中，您可能需要显式设置 `websocket_base_url`。

#### 注意事项 {#notes}

-   这是通过 WebSocket 传输的 Responses API，而不是 [Realtime API](../realtime/guide.md)。它不适用于 Chat Completions。仅当非 OpenAI提供商支持 Responses WebSocket `/responses` 端点时，才适用于这些提供商。
-   如果您的环境中尚未提供 `websockets` 包，请安装它。
-   启用 WebSocket 传输后，您可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望跨轮次以及嵌套的智能体即工具调用复用同一 WebSocket 连接的多轮工作流，建议使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助工具。请参阅[运行智能体](../running_agents.md)指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。
-   对于耗时较长的推理轮次或延迟会突增的网络，请使用 `responses_websocket_options` 自定义 WebSocket 保活行为。增大 `ping_timeout` 可容忍延迟的 pong 帧，或将 `ping_timeout=None` 设置为禁用心跳超时，同时保持启用 ping。当可靠性比 WebSocket 延迟更重要时，优先使用 HTTP/SSE 传输。
-   默认情况下，SDK 会禁用传入消息的大小限制（`max_size=None`）。对于位于代理后方或内存受限容器中的长时间运行智能体进程，请设置 `responses_websocket_options={"max_size": 8 * 1024 * 1024}` 以限制每条消息的内存用量。
-   [Responses API WebSocket 服务](https://developers.openai.com/api/docs/guides/websocket-mode)在每个连接上一次处理一个响应，并将每个连接限制为 60 分钟。达到该限制后，请打开新连接；需要并行运行时，请使用多个连接。
-   该服务仅在连接本地内存中保留最近一次响应。失败的 `4xx` 或 `5xx` 轮次会从该内存中逐出 `previous_response_id` 引用的响应。重新连接后，存储的响应在可用时仍可继续，但 `store=False` 和 ZDR 流程没有持久化回退方案。请使用 `previous_response_id=None` 开始新的链并发送完整的输入上下文，或根据本地管理的会话状态重建该上下文。

### 托管多智能体（实验性） {#hosted-multi-agent-experimental}

OpenAI Responses API 的托管多智能体 Beta 版允许 GPT-5.6 根模型创建并协调服务端托管的子智能体。Agents SDK 可以继续使用其常规的 `Runner`：托管编排在服务端进行，而开发者定义的函数工具在您的应用中执行。

此集成为实验性功能，并使用 Responses WebSocket 传输，以便通过 `response.inject` 将本地函数输出返回给活动的托管智能体。它要求使用 `openai[realtime]` 2.45.0 或更高版本的构建，且该构建需公开 `client.beta.responses.connect`。该接口和 Beta 版项目架构可能会在正式发布前发生变化。

#### 模型配置 {#configure-the-model}

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

构造 `OpenAIHostedMultiAgentModel` 会启用 `multi_agent.enabled`，并发送 `OpenAI-Beta: responses_multi_agent=v1` WebSocket 标头。除非提供 `openai_client`，否则模型将使用默认 OpenAI客户端。如果省略 `max_concurrent_subagents`，则使用服务默认值。

#### 本地函数工具 {#local-function-tools}

所有托管智能体共享为该请求配置的模型和工具。Responses API 决定由哪个托管智能体调用函数。常规 SDK Runner 在本地执行函数，并将具有相同调用 ID 的 `function_call_output` 注入活动的 WebSocket 响应，从而让服务恢复原始托管调用方。函数执行仍会经过 Runner 的常规安全防护措施、钩子和失败转换。不支持 SDK 工具审批中断：任何 `needs_approval` 设置不为 `False` 的函数工具，都会在请求发送前被拒绝。

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

托管智能体名称是观察性元数据，而不是本地路由机制。请使用 SDK 提供的调用 ID 路由输出。对于具有副作用的工具，请将该调用 ID 用作幂等键，并在工具执行之前或期间，通过应用代码实施任何必要的授权；不要对此模型使用 `needs_approval`。工具参数和输出会跨越 Responses API 边界。

#### 输出和流式传输行为 {#output-and-streaming-behavior}

只有归属于 `/root` 且阶段为 `final_answer` 的消息才会成为常规最终消息。实验性适配器会从高级 `RunResult` 中过滤掉子智能体消息和托管编排记录；SDK 绝不会将这些记录作为本地函数执行。

原始流式传输会继续公开 Beta 版 Responses 事件，包括托管输出项和 `response.inject.created` 确认。当函数调用就绪时，适配器会将一个活动的提供商响应拆分为 SDK 可见的逻辑模型轮次，然后在 Runner 生成输出后恢复同一个提供商响应。使用原始托管项目或 `ToolContext` 的 `get_hosted_agent_metadata()`，可识别项目或工具调用归属的托管智能体。

#### 与 SDK 编排的关系 {#relationship-to-sdk-orchestration}

托管多智能体独立于 SDK 任务转移和 Agents-as-tools：

-   托管多智能体在 OpenAI服务上创建子智能体。您的应用不会创建或调度这些子智能体。
-   SDK 任务转移会更改活动的本地 SDK `Agent`。使用此实验性模型时会拒绝任务转移，因为每个托管智能体都会收到相同的任务转移工具，这会导致所有权冲突。
-   Agents-as-tools 仍然可用，但使用它们会创建嵌套的客户端和服务端编排。请审慎评估额外的延迟、成本和工具暴露。

#### 当前限制 {#current-limitations}

实验性模型会拒绝 `reasoning.summary`、`max_tool_calls`，以及调用方提供的 `multi_agent` 或 `betas` 覆盖值。Beta 版不支持 Responses `/compact` 端点，但可以使用显式的 `context_management.compact_threshold`，因为服务会自动独立压缩每个托管智能体的上下文。

一个 `OpenAIHostedMultiAgentModel` 实例一次最多拥有一个活动的托管响应。如果某次运行在等待本地函数输出期间被放弃，请调用 `await model.close()` 释放其 WebSocket。目前不支持在其他进程或事件循环中恢复正在进行的托管响应。

有关底层 Responses API Beta 版行为，请参阅 [OpenAI多智能体指南](https://developers.openai.com/api/docs/guides/tools-multi-agent)。有关非流式和流式 SDK 用法，请参阅 [`examples/agent_patterns/hosted_multi_agent_beta.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/hosted_multi_agent_beta.py)。

## 非 OpenAI模型 {#non-openai-models}

如果您需要非 OpenAI提供商，请从 SDK 的内置提供商集成点开始。在许多配置中，无需添加第三方适配器即可满足需求。每种模式的代码示例位于 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### 非 OpenAI提供商集成方式 {#ways-to-integrate-non-openai-providers}

| 方式 | 适用场景 | 作用域 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 应将一个 OpenAI兼容端点作为大多数或所有智能体的默认端点 | 全局默认 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 一个自定义提供商应仅应用于单次运行 | 按运行 |
| [`Agent.model`][agents.agent.Agent.model] | 不同智能体需要不同提供商或具体模型对象 | 按智能体 |
| 第三方适配器 | 由于内置路径无法提供所需功能，因此您需要适配器提供的提供商覆盖范围或路由 | 请参阅[第三方适配器](#third-party-adapters) |

您可以通过以下内置路径集成其他 LLM 提供商：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于希望将 `AsyncOpenAI` 实例全局用作 LLM 客户端的情况。这适用于 LLM 提供商拥有 OpenAI兼容 API 端点，并且您可以设置 `base_url` 和 `api_key` 的情况。可配置的代码示例请参阅 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 级别。借助它，您可以指定“本次运行中的所有智能体都使用自定义模型提供商”。可配置的代码示例请参阅 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许您在特定 Agent 实例上指定模型。借助它，您可以为不同智能体混用不同提供商。可配置的代码示例请参阅 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。

如果您没有 `platform.openai.com` 提供的 API 密钥，建议通过 `set_tracing_disabled()` 禁用追踪，或设置[其他追踪处理器](../tracing.md)。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    在这些代码示例中，我们使用 Chat Completions API/模型，因为许多 LLM 提供商仍不支持 Responses API。如果您的 LLM 提供商支持该 API，我们建议使用 Responses。

## 在一个工作流中混用模型 {#mixing-models-in-one-workflow}

在单个工作流中，您可能希望每个智能体使用不同模型。例如，可以使用更小、更快的模型进行分流，同时使用更大、能力更强的模型处理复杂任务。配置 [`Agent`][agents.Agent] 时，可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称以及可将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!! note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 形式，但我们建议每个工作流仅使用一种模型形式，因为两者支持的功能和工具集合不同。如果您的工作流需要混用模型形式，请确保使用的所有功能都同时受两者支持。

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

如果要进一步配置智能体使用的模型，可以传入 [`ModelSettings`][agents.model_settings.ModelSettings]，它提供 temperature 等可选模型配置参数。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## 高级 OpenAI Responses 设置 {#advanced-openai-responses-settings}

当您使用 OpenAI Responses 路径并需要更多控制时，请从 `ModelSettings` 开始。

### 常用高级 `ModelSettings` 选项 {#common-advanced-modelsettings-options}

使用 OpenAI Responses API 时，若干请求字段已经有直接对应的 `ModelSettings` 字段，因此无需为它们使用 `extra_args`。

- `parallel_tool_calls`：允许或禁止在同一轮中进行多次工具调用。
- `truncation`：设置 `"auto"`，让 Responses API 在上下文即将溢出时丢弃最早的对话项，而不是让请求失败。
- `store`：控制生成的响应是否存储在服务端以供后续检索。这对于依赖响应 ID 的后续工作流，以及在 `store=False` 时可能需要回退到本地输入的会话压缩流程非常重要。
- `context_management`：配置服务端上下文处理，例如使用 `compact_threshold` 进行 Responses 压缩。
- `prompt_cache_retention`：为较早的模型系列配置延长保留，例如
  使用 `"24h"`。
- `prompt_cache_options`：选择隐式或显式提示词缓存，并为 GPT-5.6 配置 `"30m"` 缓存 TTL。
- `response_include`：请求更丰富的响应载荷，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。
- `top_logprobs`：请求输出文本中排名靠前的 token 的 logprob。SDK 还会自动添加 `message.output_text.logprobs`。
- `retry`：选择启用由 Runner 管理的模型调用重试设置。请参阅[由 Runner 管理的重试](#runner-managed-retries)。

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

使用显式提示词缓存时，请在可复用前缀结尾的内容部分添加断点。同一个 `ModelSettings.prompt_cache_options` 字段会透传到 Responses 和 Chat Completions 请求，并且 Chat Completions 转换器会保留文本、图像、音频和文件内容部分上的断点。

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

对于使用旧版保留控制的较早模型系列，`prompt_cache_retention` 仍然可用。不要将直接的 `ModelSettings` 字段与
`extra_args` 中的相同键组合使用。

设置 `store=False` 时，Responses API 不会保留该响应以供日后在服务端检索。这对于无状态或零数据保留风格的流程很有用，但这也意味着原本会复用响应 ID 的功能需要改为依赖本地管理的状态。例如，当最后一次响应未存储时，[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 会将其默认的 `"auto"` 压缩路径切换为基于输入的压缩。请参阅[会话指南](../sessions/index.md#openai-responses-compaction-sessions)。

服务端压缩不同于 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]。`context_management=[{"type": "compaction", "compact_threshold": ...}]` 会随每个 Responses API 请求发送，当渲染后的上下文超过阈值时，API 可以在响应中生成压缩项。`OpenAIResponsesCompactionSession` 会在轮次之间调用独立的 `responses.compact` 端点，并重写本地会话历史记录。

### `extra_args` 的传递 {#passing-extra_args}

如果需要 SDK 尚未直接在顶层公开的提供商特定字段或较新的请求字段，请使用 `extra_args`。

使用 OpenAI模型时，`extra_args` 可以向 Responses API 和 Chat Completions API 传递可选参数，例如 `user` 和 `service_tier`。对于受支持的模型，请设置 `extra_args={"service_tier": "fast"}` 以使用[快速模式](https://developers.openai.com/api/docs/guides/fast-mode)；`"priority"` 仍与之等效。请勿同时通过直接的 `ModelSettings` 字段设置同一个请求字段。

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

## 模型调用超时 {#model-call-timeouts}

将 [`ModelSettings.timeout`][agents.model_settings.ModelSettings.timeout] 设置为正数秒值，以限制每次模型调用尝试。该超时适用于流式和非流式调用，并涵盖完整的调用尝试，包括等待传输的时间。它不会限制完整的智能体运行、函数工具执行或重试退避。

```python
from agents import Agent, ModelSettings

agent = Agent(
    name="Assistant",
    model_settings=ModelSettings(timeout=30.0),
)
```

如果一次尝试超过限制，SDK 会取消该尝试并等待其清理完成，然后引发 [`ModelTimeoutError`][agents.exceptions.ModelTimeoutError]。启用由 Runner 管理的重试时，SDK 会将超时失败传递给重试策略，并将 `context.normalized.is_timeout` 设置为 `True`；例如，`retry_policies.network_error()` 会匹配该分类。每次允许的重试都会获得新的单次尝试超时。重试前，SDK 仍会应用常规的[重放安全规则](#safety-boundaries)。

## 由 Runner 管理的重试 {#runner-managed-retries}

重试仅在运行时生效，并且需要选择启用。除非您设置 `ModelSettings(retry=...)` 且重试策略决定重试，否则 SDK 不会重试常规模型请求。

在 Responses WebSocket 传输中，`retry_policies.provider_suggested()` 会将响应前的过载帧和没有代码的 `server_error` 帧识别为重试建议。这本身不会启用重试：您仍需设置 `ModelRetrySettings`，并且常规的重放安全检查仍然适用。如果已经收到任何响应事件，SDK 不会重放请求。

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
| `max_retries` | `int | None` | 初始请求后允许的重试次数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 策略决定重试但未返回显式延迟时使用的默认延迟策略。`backoff.max_delay` 仅限制由此计算出的退避延迟，不会限制策略返回的显式延迟或 retry-after 提示。 |
| `policy` | `RetryPolicy | None` | 决定是否重试的回调。此字段仅在运行时生效，不会被序列化。 |

</div>

重试策略会收到一个 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]，其中包含：

- `attempt` 和 `max_retries`，便于您根据尝试次数作出决定。
- `stream`，便于您区分流式和非流式行为。
- `error`，用于原始检查。
- `normalized` 事实，例如 `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout` 和 `is_abort`。
- 当底层模型适配器可以提供重试指导时的 `provider_advice`。
- `response_started`、`replay_safety` 和 `stateful_request`，它们是在策略运行前捕获的稳定重放安全事实。`replay_safety` 为 `"safe"`、`"unsafe"` 或 `"unknown"`；当请求使用 `previous_response_id` 或 `conversation_id` 时，`stateful_request` 为 true。

策略可以返回以下任一结果：

- `True` / `False`，用于简单的重试决策。
- 当您希望覆盖延迟、附加诊断原因，或显式批准范围严格受限的不安全重放时，返回 [`RetryDecision`][agents.retry.RetryDecision]。

SDK 在 `retry_policies` 上导出了现成的辅助工具：

| 辅助工具 | 行为 |
| --- | --- |
| `retry_policies.never()` | 始终选择不重试。 |
| `retry_policies.provider_suggested()` | 在提供商提供重试建议时遵循该建议。 |
| `retry_policies.network_error()` | 匹配暂时性传输和超时失败。 |
| `retry_policies.http_status([...])` | 匹配选定的 HTTP 状态码。 |
| `retry_policies.retry_after()` | 仅在提供 retry-after 提示时重试，并使用该延迟。此辅助工具将 retry-after 值视为显式策略延迟，因此 `backoff.max_delay` 不会限制它。 |
| `retry_policies.any(...)` | 当任一嵌套策略选择重试时进行重试。 |
| `retry_policies.all(...)` | 仅当每个嵌套策略都选择重试时才进行重试。 |

组合策略时，`provider_suggested()` 是最安全的第一个基本组件，因为当提供商能够区分否决和重放安全批准时，它会保留这些信息。

##### 安全边界 {#safety-boundaries}

某些失败永远不会重试：

- 中止错误。
- 输出已经以某种方式开始，导致重放不安全的流式运行。
- 存在独立本地副作用重放否决的请求，包括程序化工具调用请求，除非提供商已独立将重放标记为安全。

默认情况下，提供商标记为不安全的失败也会被阻止。对于不存在独立本地副作用否决的非流式请求，应用可以通过返回 `RetryDecision(retry=True, approve_unsafe_replay=True)` 来接受提供商侧的重放风险。在授予此批准之前，请检查 `context.response_started`、`context.replay_safety` 和 `context.stateful_request`，并且仅在可以接受重复执行提供商侧工作时授予批准。普通的 `RetryDecision(retry=True)` 永远无法绕过重放保护，而 `approve_unsafe_replay=True` 无法授权流式重试或本地副作用。

使用 `previous_response_id` 或 `conversation_id` 的有状态后续请求会在重放安全性未知时以关闭方式失败。对于这些请求，`network_error()` 或 `http_status([500])` 等非提供商谓词本身并不足够。请包含提供商提供的重放安全批准（通常通过 `retry_policies.provider_suggested()`），或按照上述方式显式批准提供商标记为不安全的非流式失败。

##### Runner 和智能体合并行为 {#runner-and-agent-merge-behavior}

Runner 级与智能体级 `ModelSettings` 之间会对 `retry` 进行深度合并：

- 智能体可以仅覆盖 `retry.max_retries`，同时继续继承 Runner 的 `policy`。
- 智能体可以仅覆盖 `retry.backoff` 的一部分，同时保留 Runner 中同级的退避字段。
- `policy` 仅在运行时生效，因此序列化后的 `ModelSettings` 会保留 `max_retries` 和 `backoff`，但省略回调本身。

有关更完整的代码示例，请参阅 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 和[基于适配器的重试代码示例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)。

## 非 OpenAI提供商故障排除 {#troubleshooting-non-openai-providers}

### 追踪客户端错误 401 {#tracing-client-error-401}

如果遇到与追踪相关的错误，这是因为追踪数据会上传到 OpenAI服务器，而您没有 OpenAI API 密钥。您有以下三种解决方案：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI密钥：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。此 API 密钥仅用于上传追踪数据，并且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI追踪处理器。请参阅[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持 {#responses-api-support}

SDK 默认使用 Responses API，但许多其他 LLM 提供商仍不支持它。因此，您可能会看到 404 或类似问题。您有以下两个解决方案：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。如果您通过环境变量设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，此方法有效。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)提供了一些代码示例。

### Chat Completions 兼容性选项 {#chat-completions-compatibility-options}

通过 Chat Completions 路由时，SDK 会静默丢弃 Chat Completions 无法发送的 Responses 专属字段，从而保持兼容性，例如 `previous_response_id`、`conversation_id`、Responses API 的 `prompt` 字段，或并非纯文本的工具输出。如果您希望在开发过程中让这些不匹配情况快速失败，请在 OpenAI提供商上启用严格功能验证：

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

OpenAI Chat Completions API 可以返回音频输出，但 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 目前不会将音频输出转换为 Agents SDK 运行项。如果非流式消息或流式增量包含音频输出，适配器会引发 `AgentsException("Audio is not currently supported")`，而不是返回部分结果或空结果。对于由 SDK 管理的音频工作流，请使用[实时智能体](../realtime/guide.md)或[语音智能体](../voice/quickstart.md)。

某些兼容 OpenAI的 Chat Completions 提供商会以分块方式流式传输工具调用增量，而这些分块的可靠性不足以进行 SDK 增量处理。在这种情况下，请启用流式工具调用缓冲，使 SDK 仅在提供商流结束后生成工具调用：

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

对于 [`MultiProvider`][agents.MultiProvider]，请使用 `openai_buffer_streamed_tool_calls=True`。

### structured outputs 支持 {#structured-outputs-support}

某些模型提供商不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。这有时会导致类似以下内容的错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型提供商的不足之处——它们支持 JSON 输出，但不允许您指定输出使用的 `json_schema`。我们正在修复此问题，但建议依赖支持 JSON schema 输出的提供商，否则您的应用会经常因格式错误的 JSON 而中断。

## 跨提供商混用模型 {#mixing-models-across-providers}

您需要注意模型提供商之间的功能差异，否则可能会遇到错误。例如，OpenAI支持 structured outputs、多模态输入，以及托管的文件检索和网络检索，但许多其他提供商并不支持这些功能。请注意以下限制：

-   不要向无法理解不受支持的 `tools` 的提供商发送它们
-   在调用纯文本模型前过滤掉多模态输入
-   请注意，不支持结构化 JSON 输出的提供商偶尔会生成无效 JSON。

## 第三方适配器 {#third-party-adapters}

仅当 SDK 的内置提供商集成点不足以满足需求时，才应使用第三方适配器。如果您在此 SDK 中仅使用 OpenAI模型，请优先使用内置的 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 路径，而不是 Any-LLM 或 LiteLLM。第三方适配器适用于需要将 OpenAI模型与非 OpenAI提供商结合使用，或需要仅由适配器提供的提供商覆盖范围或路由的情况。适配器会在 SDK 与上游模型提供商之间增加另一个兼容层，因此功能支持和请求语义可能因提供商而异。SDK 目前以尽力支持的 Beta 版适配器集成形式包含 Any-LLM 和 LiteLLM。

### Any-LLM {#any-llm}

Any-LLM 支持以尽力支持的 Beta 版形式提供，适用于需要由 Any-LLM 管理提供商覆盖范围或路由的情况。

根据上游提供商路径，Any-LLM 可能使用 Responses API、兼容 Chat Completions 的 API，或提供商特定的兼容层。

如果需要 Any-LLM，请安装 `openai-agents[any-llm]`，然后从 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 或 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) 开始。您可以将 `any-llm/...` 模型名称与 [`MultiProvider`][agents.MultiProvider] 搭配使用、直接实例化 `AnyLLMModel`，或在运行作用域使用 `AnyLLMProvider`。如果需要显式固定模型接口，请在构造 `AnyLLMModel` 时传入 `api="responses"` 或 `api="chat_completions"`。

Any-LLM 仍然是第三方适配器层，因此提供商依赖项和能力缺口由上游 Any-LLM 而非 SDK 定义。当上游提供商返回使用量指标时，这些指标会自动传播，但流式 Chat Completions 后端可能需要设置 `ModelSettings(include_usage=True)` 才会生成使用量分块。如果您依赖 structured outputs、工具调用、使用量报告或 Responses 特定行为，请验证计划部署的具体提供商后端。

### LiteLLM {#litellm}

LiteLLM 支持以尽力支持的 Beta 版形式提供，适用于需要 LiteLLM 特定提供商覆盖范围或路由的情况。

如果需要 LiteLLM，请安装 `openai-agents[litellm]`，然后从 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 或 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) 开始。您可以使用 `litellm/...` 模型名称，或直接实例化 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]。

通过 LiteLLM 适配器访问的某些提供商默认不会填充 SDK 使用量指标。如果需要使用量报告，请传入 `ModelSettings(include_usage=True)`；如果您依赖 structured outputs、工具调用、使用量报告或适配器特定路由行为，请验证计划部署的具体提供商后端。

如果 LiteLLM 为响应对象发出 Pydantic 序列化器警告，您可以在导入 LiteLLM 适配器之前选择启用 SDK 的兼容性补丁：

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

该补丁默认禁用，仅对 `1` 或 `true` 值启用。它通过包装 LiteLLM 的私有日志辅助工具来抑制一类特定的 LiteLLM 响应序列化警告，因此应将其视为针对性解决方案，而不是通用序列化设置。由于它依赖 LiteLLM 的私有 API，升级 LiteLLM 时请重新验证，并在上游警告不再出现后移除该环境变量。