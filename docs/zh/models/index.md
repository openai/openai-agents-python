---
search:
  exclude: true
---
# 模型

Agents SDK 开箱即用支持两种 OpenAI 模型形态：

-   **推荐**：[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]，通过新的 [Responses API](https://platform.openai.com/docs/api-reference/responses) 调用 OpenAI API。
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]，通过 [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 调用 OpenAI API。

## 模型设置选择

根据你的设置，请按以下顺序使用本页：

| 目标 | 从这里开始 |
| --- | --- |
| 使用 SDK 默认配置的 OpenAI 托管模型 | [OpenAI 模型](#openai-models) |
| 通过 websocket 传输使用 OpenAI Responses API | [Responses WebSocket 传输](#responses-websocket-transport) |
| 使用非 OpenAI 提供方 | [非 OpenAI 模型](#non-openai-models) |
| 在一个工作流中混用模型/提供方 | [高级模型选择与混用](#advanced-model-selection-and-mixing) 和 [跨提供方混用模型](#mixing-models-across-providers) |
| 调试提供方兼容性问题 | [非 OpenAI 提供方故障排查](#troubleshooting-non-openai-providers) |

## OpenAI 模型

当你在初始化 `Agent` 时未指定模型，将使用默认模型。当前默认值是 [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1)，以兼顾兼容性和低延迟。如果你有权限，我们建议将智能体设置为 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) 以获得更高质量，同时保持显式的 `model_settings`。

如果你想切换到其他模型（如 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4)），有两种方式配置你的智能体。

### 默认模型

首先，如果你希望所有未设置自定义模型的智能体始终使用某个特定模型，请在运行智能体前设置 `OPENAI_DEFAULT_MODEL` 环境变量。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

其次，你可以通过 `RunConfig` 为一次运行设置默认模型。如果未为某个智能体设置模型，将使用该次运行的模型。

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

当你以这种方式使用任意 GPT-5 模型（如 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4)）时，SDK 会应用默认的 `ModelSettings`。它会设置对大多数使用场景效果最佳的配置。若要调整默认模型的推理强度，请传入你自己的 `ModelSettings`：

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

为了更低延迟，建议在 `gpt-5.4` 上使用 `reasoning.effort="none"`。gpt-4.1 系列（包括 mini 和 nano 变体）同样仍是构建交互式智能体应用的可靠选择。

#### ComputerTool 模型选择

如果智能体包含 [`ComputerTool`][agents.tool.ComputerTool]，实际 Responses 请求中生效的模型决定 SDK 发送哪种计算机工具负载。显式 `gpt-5.4` 请求会使用 GA 内置 `computer` 工具，而显式 `computer-use-preview` 请求会保留旧的 `computer_use_preview` 负载。

由提示词管理的调用是主要例外。如果提示模板持有模型且 SDK 在请求中省略 `model`，SDK 会默认使用与 preview 兼容的计算机负载，以避免猜测提示词绑定了哪个模型。要在该流程中保持 GA 路径，可在请求中显式设置 `model="gpt-5.4"`，或通过 `ModelSettings(tool_choice="computer")` 或 `ModelSettings(tool_choice="computer_use")` 强制使用 GA 选择器。

在已注册 [`ComputerTool`][agents.tool.ComputerTool] 的情况下，`tool_choice="computer"`、`"computer_use"` 和 `"computer_use_preview"` 会被规范化为与生效请求模型匹配的内置选择器。如果未注册 `ComputerTool`，这些字符串仍按普通函数名处理。

与 preview 兼容的请求必须预先序列化 `environment` 和显示尺寸，因此在使用 [`ComputerProvider`][agents.tool.ComputerProvider] 工厂的提示词管理流程中，应传入具体的 `Computer` 或 `AsyncComputer` 实例，或在发送请求前强制使用 GA 选择器。完整迁移细节见 [工具](../tools.md#computertool-and-the-responses-computer-tool)。

#### 非 GPT-5 模型

如果你传入非 GPT-5 模型名称且未自定义 `model_settings`，SDK 会回退为兼容任意模型的通用 `ModelSettings`。

### 仅 Responses 的工具检索功能

以下工具功能仅在 OpenAI Responses 模型中受支持：

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 及其他延迟加载的 Responses 工具接口

这些功能在 Chat Completions 模型和非 Responses 后端上会被拒绝。当你使用延迟加载工具时，请将 `ToolSearchTool()` 添加到智能体中，并让模型通过 `auto` 或 `required` 的工具选择来加载工具，而不是强制使用裸命名空间名称或仅延迟加载的函数名。配置细节和当前限制见 [工具](../tools.md#hosted-tool-search)。

### Responses WebSocket 传输

默认情况下，OpenAI Responses API 请求使用 HTTP 传输。使用 OpenAI 支持的模型时，你可以选择启用 websocket 传输。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响由默认 OpenAI 提供方解析的 OpenAI Responses 模型（包括字符串模型名，如 `"gpt-5.4"`）。

传输方式会在 SDK 将模型名解析为模型实例时确定。如果你传入具体的 [`Model`][agents.models.interface.Model] 对象，其传输已固定：[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] 使用 websocket，[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 使用 HTTP，[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 仍使用 Chat Completions。如果你传入 `RunConfig(model_provider=...)`，则由该提供方而非全局默认值控制传输选择。

你也可以按提供方或按运行配置 websocket 传输：

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

如果你需要基于前缀的模型路由（例如在一次运行中混用 `openai/...` 和 `litellm/...` 模型名），请改用 [`MultiProvider`][agents.MultiProvider] 并在其中设置 `openai_use_responses_websocket=True`。

`MultiProvider` 保留两个历史默认行为：

-   `openai/...` 被视为 OpenAI 提供方的别名，因此 `openai/gpt-4.1` 会按模型 `gpt-4.1` 路由。
-   未知前缀会触发 `UserError`，而不是透传。

当你将 OpenAI 提供方指向一个期望字面命名空间模型 ID 的 OpenAI 兼容端点时，请显式启用透传行为。在启用 websocket 的设置中，也要在 `MultiProvider` 上保留 `openai_use_responses_websocket=True`：

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

当后端期望字面量 `openai/...` 字符串时，使用 `openai_prefix_mode="model_id"`。当后端期望其他命名空间模型 ID（如 `openrouter/openai/gpt-4.1-mini`）时，使用 `unknown_prefix_mode="model_id"`。这些选项在非 websocket 传输的 `MultiProvider` 上同样可用；本示例保持启用 websocket，因为它属于本节描述的传输设置。相同选项也可用于 [`responses_websocket_session()`][agents.responses_websocket_session]。

如果你使用自定义 OpenAI 兼容端点或代理，websocket 传输还需要兼容的 websocket `/responses` 端点。在这类设置中，你可能需要显式设置 `websocket_base_url`。

说明：

-   这是基于 websocket 传输的 Responses API，不是 [Realtime API](../realtime/guide.md)。它不适用于 Chat Completions 或非 OpenAI 提供方，除非它们支持 Responses websocket `/responses` 端点。
-   如果环境中尚未安装，请安装 `websockets` 包。
-   启用 websocket 传输后，你可以直接使用 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]。对于希望在多轮工作流（以及嵌套的 Agents-as-tools 调用）中复用同一 websocket 连接的场景，推荐使用 [`responses_websocket_session()`][agents.responses_websocket_session] 辅助函数。参见 [运行智能体](../running_agents.md) 指南和 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)。

## 非 OpenAI 模型

你可以通过 [LiteLLM 集成](./litellm.md) 使用大多数其他非 OpenAI 模型。首先，安装 litellm 依赖组：

```bash
pip install "openai-agents[litellm]"
```

然后，使用任意[受支持模型](https://docs.litellm.ai/docs/providers)，并加上 `litellm/` 前缀：

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 使用非 OpenAI 模型的其他方式

你还可以通过另外 3 种方式集成其他 LLM 提供方（代码示例见[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)）：

1. [`set_default_openai_client`][agents.set_default_openai_client] 适用于你希望全局使用 `AsyncOpenAI` 实例作为 LLM 客户端的情况。这适用于 LLM 提供方拥有 OpenAI 兼容 API 端点，且你可以设置 `base_url` 与 `api_key` 的场景。可配置示例见 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)。
2. [`ModelProvider`][agents.models.interface.ModelProvider] 位于 `Runner.run` 层级。这让你可以指定“本次运行中的所有智能体都使用自定义模型提供方”。可配置示例见 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)。
3. [`Agent.model`][agents.agent.Agent.model] 允许你在特定 Agent 实例上指定模型。这使你能够为不同智能体混用不同提供方。可配置示例见 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)。使用多数可用模型的简单方式是通过 [LiteLLM 集成](./litellm.md)。

在你没有来自 `platform.openai.com` 的 API key 时，我们建议通过 `set_tracing_disabled()` 禁用追踪，或配置[不同的追踪进程](../tracing.md)。

!!! note

    在这些示例中，我们使用 Chat Completions API/模型，因为大多数 LLM 提供方尚不支持 Responses API。如果你的 LLM 提供方支持它，我们建议使用 Responses。

## 高级模型选择与混用

在单个工作流中，你可能希望为每个智能体使用不同模型。例如，你可以为分流使用更小、更快的模型，同时为复杂任务使用更大、能力更强的模型。配置 [`Agent`][agents.Agent] 时，你可以通过以下任一方式选择特定模型：

1. 传入模型名称。
2. 传入任意模型名称 + 可将该名称映射到 Model 实例的 [`ModelProvider`][agents.models.interface.ModelProvider]。
3. 直接提供 [`Model`][agents.models.interface.Model] 实现。

!!!note

    虽然我们的 SDK 同时支持 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 和 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 两种形态，但我们建议每个工作流只使用一种模型形态，因为两者支持的功能和工具集合不同。如果你的工作流需要混用模型形态，请确保你使用的所有功能在两者中都可用。

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
2.  提供一个 [`Model`][agents.models.interface.Model] 实现。

当你想进一步配置智能体使用的模型时，可以传入 [`ModelSettings`][agents.models.interface.ModelSettings]，它提供如 temperature 等可选模型配置参数。

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

当你使用 OpenAI Responses API 时，若干请求字段已在 `ModelSettings` 中有直接对应字段，因此你无需为它们使用 `extra_args`。

| 字段 | 用途 |
| --- | --- |
| `parallel_tool_calls` | 允许或禁止在同一轮中进行多次工具调用。 |
| `truncation` | 设置为 `"auto"`，让 Responses API 在上下文将溢出时丢弃最旧的会话项，而不是失败。 |
| `store` | 控制是否将生成的响应存储在服务端以供后续检索。这会影响依赖 response ID 的后续工作流，以及在 `store=False` 时可能需要回退到本地输入的会话压缩流程。 |
| `prompt_cache_retention` | 更长时间保留缓存的提示前缀，例如 `"24h"`。 |
| `response_include` | 请求更丰富的响应负载，例如 `web_search_call.action.sources`、`file_search_call.results` 或 `reasoning.encrypted_content`。 |
| `top_logprobs` | 为输出文本请求 top-token logprobs。SDK 还会自动添加 `message.output_text.logprobs`。 |
| `retry` | 为模型调用启用由 Runner 管理的重试设置。见[Runner 管理的重试](#runner-managed-retries)。 |

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

当你设置 `store=False` 时，Responses API 不会保留该响应供后续服务端检索。这对无状态或零数据保留风格流程很有用，但也意味着原本可复用 response ID 的功能需要改为依赖本地管理状态。例如，当上一次响应未被存储时，[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 会将其默认 `"auto"` 压缩路径切换为基于输入的压缩。参见[会话指南](../sessions/index.md#openai-responses-compaction-sessions)。

#### Runner 管理的重试

重试是仅运行时功能，并且是选择启用。除非你设置 `ModelSettings(retry=...)` 且重试策略决定重试，否则 SDK 不会重试常规模型请求。

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

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `max_retries` | `int \| None` | 初始请求后允许的重试次数。 |
| `backoff` | `ModelRetryBackoffSettings \| dict \| None` | 当策略决定重试但未返回显式延迟时使用的默认延迟策略。 |
| `policy` | `RetryPolicy \| None` | 决定是否重试的回调。此字段仅运行时有效，不会被序列化。 |

重试策略会收到一个 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]，其中包含：

- `attempt` 和 `max_retries`，便于你做基于尝试次数的决策。
- `stream`，便于区分流式与非流式行为。
- `error`，用于原始检查。
- 规范化信息，如 `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout` 和 `is_abort`。
- 当底层模型适配器可提供重试建议时的 `provider_advice`。

策略可返回：

- `True` / `False`，用于简单重试决策。
- [`RetryDecision`][agents.retry.RetryDecision]，用于覆盖延迟或附加诊断原因。

SDK 在 `retry_policies` 中导出了一组现成辅助函数：

| 辅助函数 | 行为 |
| --- | --- |
| `retry_policies.never()` | 始终不重试。 |
| `retry_policies.provider_suggested()` | 在可用时遵循提供方重试建议。 |
| `retry_policies.network_error()` | 匹配暂时性传输错误和超时失败。 |
| `retry_policies.http_status([...])` | 匹配指定 HTTP 状态码。 |
| `retry_policies.retry_after()` | 仅在存在 retry-after 提示时重试，并使用该延迟。 |
| `retry_policies.any(...)` | 任一嵌套策略选择重试即重试。 |
| `retry_policies.all(...)` | 仅当所有嵌套策略都选择重试才重试。 |

组合策略时，`provider_suggested()` 是最安全的首个构件，因为当提供方能够区分时，它会保留提供方否决和可重放安全批准。

##### 安全边界

某些失败永远不会自动重试：

- 中止错误。
- 提供方建议将重放标记为不安全的请求。
- 流式运行中输出已开始且重放会不安全的情况。

使用 `previous_response_id` 或 `conversation_id` 的有状态后续请求也会被更保守地处理。对于这类请求，仅有 `network_error()` 或 `http_status([500])` 等非提供方谓词并不足够。重试策略应包含来自提供方的可重放安全批准，通常通过 `retry_policies.provider_suggested()`。

##### Runner 与智能体合并行为

`retry` 会在 Runner 级与智能体级 `ModelSettings` 之间做深度合并：

- 智能体可仅覆盖 `retry.max_retries`，并继承 Runner 的 `policy`。
- 智能体可仅覆盖 `retry.backoff` 的一部分，并保留来自 Runner 的同级 backoff 字段。
- `policy` 仅运行时有效，因此序列化后的 `ModelSettings` 会保留 `max_retries` 与 `backoff`，但省略回调本身。

更完整示例见 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 和 [`examples/basic/retry_litellm.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)。

当你需要 SDK 尚未在顶层直接暴露的提供方特定字段或较新请求字段时，请使用 `extra_args`。

另外，当你使用 OpenAI 的 Responses API 时，[还有一些其他可选参数](https://platform.openai.com/docs/api-reference/responses/create)（例如 `user`、`service_tier` 等）。如果它们在顶层不可用，也可以通过 `extra_args` 传递。

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

如果你遇到与追踪相关的错误，这是因为追踪会上传到 OpenAI 服务端，而你没有 OpenAI API key。你有三种解决方式：

1. 完全禁用追踪：[`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. 为追踪设置 OpenAI key：[`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。该 API key 仅用于上传追踪，且必须来自 [platform.openai.com](https://platform.openai.com/)。
3. 使用非 OpenAI 的追踪进程。参见[追踪文档](../tracing.md#custom-tracing-processors)。

### Responses API 支持

SDK 默认使用 Responses API，但多数其他 LLM 提供方尚不支持。因此你可能会看到 404 或类似问题。可通过以下两种方式解决：

1. 调用 [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]。这适用于你通过环境变量设置 `OPENAI_API_KEY` 与 `OPENAI_BASE_URL` 的情况。
2. 使用 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。示例见[此处](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)。

### structured outputs 支持

某些模型提供方不支持 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)。有时会导致类似如下错误：

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

这是某些模型提供方的不足——它们支持 JSON 输出，但不允许你指定输出所用的 `json_schema`。我们正在修复这个问题，但建议依赖支持 JSON schema 输出的提供方，否则你的应用会经常因 JSON 格式错误而中断。

## 跨提供方混用模型

你需要了解不同模型提供方之间的功能差异，否则可能遇到错误。例如，OpenAI 支持 structured outputs、多模态输入、托管文件检索和网络检索，但许多其他提供方不支持这些功能。请注意以下限制：

-   不要向不理解这些 `tools` 的提供方发送不受支持的 `tools`
-   在调用仅支持文本的模型前，过滤掉多模态输入
-   注意：不支持结构化 JSON 输出的提供方会偶尔生成无效 JSON。