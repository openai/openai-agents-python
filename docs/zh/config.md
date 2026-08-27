---
search:
  exclude: true
---
# 配置

本页介绍通常在应用启动时一次性设置的 SDK 全局默认配置，例如默认OpenAI密钥或客户端、默认OpenAI API 形式、追踪导出默认配置以及日志行为。

这些默认配置仍适用于基于沙箱的工作流，但沙箱工作区、沙箱客户端和会话复用需要单独配置。

如果需要配置特定智能体或运行，请先参阅：

-   [智能体](agents.md)：了解普通 `Agent` 的指令、工具、输出类型、任务转移和安全防护措施。
-   [运行智能体](running_agents.md)：了解 `RunConfig`、会话和对话状态选项。
-   [沙箱智能体](sandbox/guide.md)：了解 `SandboxRunConfig`、清单、能力以及特定于沙箱客户端的工作区设置。
-   [模型](models/index.md)：了解模型选择和提供商配置。
-   [追踪](tracing.md)：了解每次运行的追踪元数据和自定义追踪处理器。

## 配置对象与字典 {#configuration-objects-and-dictionaries}

SDK 定义的配置参数通常既接受相应的强类型设置对象，也接受包含相同字段的字典。这适用于类型注解中包含字典的智能体、运行、模型、会话、沙箱和语音配置边界。SDK 定义的嵌套设置类型也可以使用字典。

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings={
        "reasoning": {"effort": "high"},
        "verbosity": "low",
    },
)
```

SDK 会将这些字典规范化为相应的设置对象。对于 SDK 定义的 dataclass 配置类型，未知字段会引发 `TypeError`，这有助于尽早发现拼写错误的选项名称。请查看参数的类型注解或 API 参考文档，以确认特定配置边界是否接受字典。

## API 密钥与客户端 {#api-keys-and-clients}

默认情况下，SDK 使用 `OPENAI_API_KEY` 环境变量处理 LLM 请求和追踪。SDK 首次创建OpenAI客户端时才会解析该密钥（延迟初始化），因此请在首次调用模型之前设置该环境变量。如果无法在应用启动前设置该环境变量，可以使用 [set_default_openai_key()][agents.set_default_openai_key] 函数设置密钥。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

或者，也可以配置要使用的OpenAI客户端。默认情况下，SDK 会使用环境变量中的 API 密钥或上述默认密钥创建 `AsyncOpenAI` 实例。可以使用 [set_default_openai_client()][agents.set_default_openai_client] 函数更改此行为。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

向 [`OpenAIProvider`][agents.models.openai_provider.OpenAIProvider] 传入显式客户端后，该客户端将负责管理其连接和账户设置。请勿同时向 `OpenAIProvider` 传入 `api_key`、`base_url`、`websocket_base_url`、`organization` 或 `project`；将 `openai_client` 与其中任何参数结合使用时，会引发 [`UserError`][agents.exceptions.UserError]，而不是静默忽略重复值。请在构造 `AsyncOpenAI` 时设置所需值。

### 使用 `openai` v3 的自定义 HTTP 客户端 {#custom-http-clients-with-openai-v3}

0.21.0 版本要求使用 `openai>=3.0.0,<4`。默认OpenAI提供商使用 HTTPX2，因此大多数应用不需要直接配置 HTTP 客户端。如果应用向 `AsyncOpenAI` 传入 `http_client=`，请为自定义客户端及其传输层相关选项使用 HTTPX2 类型：

```python
import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from agents import set_default_openai_client

http_client = DefaultAsyncHttpx2Client(
    timeout=httpx2.Timeout(30.0, connect=5.0),
)
custom_client = AsyncOpenAI(
    api_key="...",
    http_client=http_client,
)
set_default_openai_client(custom_client)
```

相同的迁移方式也适用于自定义传输、身份验证、事件钩子、模拟传输、URL、请求、响应和传输异常处理。请使用它们对应的 `httpx2`。Agents SDK 不会将任意旧版 `httpx` 对象转换为 HTTPX2。当应用显式安装 `httpx` 时，OpenAI Python SDK 会为旧版客户端提供临时兼容路径，但新增代码和已迁移代码应使用 HTTPX2。

此OpenAI客户端边界与本地 MCP 传输自定义相互独立。MCP Python SDK v1 使用其自身的旧版 `httpx` 依赖项，而 MCP Python SDK v2 使用 `httpx2`；请参阅 [MCP Python SDK v1 和 v2](mcp.md#mcp-python-sdk-v1-and-v2)。

如果倾向于使用基于环境变量的端点配置，默认OpenAI提供商还会读取 `OPENAI_BASE_URL`。启用 Responses WebSocket 传输后，它还会读取 `OPENAI_WEBSOCKET_BASE_URL`，作为 WebSocket 的 `/responses` 端点。

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

此外，还可以自定义所使用的OpenAI API。默认情况下，我们使用OpenAI Responses API。可以使用 [set_default_openai_api()][agents.set_default_openai_api] 函数将其替换为 Chat Completions API。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## OpenAI提供商默认配置 {#openai-provider-defaults}

使用 SDK OpenAI后端的提供商在将模型名称字符串映射到模型时，也会读取 SDK 全局默认配置。使用 [`set_default_openai_responses_transport()`][agents.set_default_openai_responses_transport] 可使OpenAI Responses 模型默认使用 WebSocket 传输：

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

这会影响默认OpenAI提供商解析模型名称时生成的OpenAI Responses 模型。有关提供商级设置、连接复用、保活选项和自定义 WebSocket 端点，请参阅 [Responses WebSocket 传输](models/index.md#responses-websocket-transport)。

如果OpenAI设置需要提供商级智能体注册元数据，请在启动时一次性配置默认 harness ID：

```python
from agents import set_default_openai_harness

set_default_openai_harness("your-harness-id")
```

也可以传入完整的注册对象：

```python
from agents import OpenAIAgentRegistrationConfig, set_default_openai_agent_registration

set_default_openai_agent_registration(
    OpenAIAgentRegistrationConfig(harness_id="your-harness-id")
)
```

如果未设置 SDK 默认值，使用 SDK OpenAI后端的提供商会回退到 `OPENAI_AGENT_HARNESS_ID` 环境变量。配置 harness ID 后，SDK 会将其作为 `agent_harness_id` 添加到追踪元数据中，除非 `RunConfig.trace_metadata` 中已存在该键。

## 追踪 {#tracing}

追踪默认处于启用状态。默认情况下，它使用与上一节中的模型请求相同的OpenAI API 密钥，即环境变量中的密钥或设置的默认密钥。可以使用 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 函数专门设置用于追踪的 API 密钥。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

如果模型流量使用某个密钥或客户端，而追踪应使用另一个OpenAI密钥，请在设置默认密钥或客户端时传入 `use_for_tracing=False`，然后单独配置追踪。如果不使用自定义客户端，同样的方式也适用于 [`set_default_openai_key()`][agents.set_default_openai_key]。

```python
from openai import AsyncOpenAI
from agents import (
    set_default_openai_client,
    set_tracing_export_api_key,
)

custom_client = AsyncOpenAI(base_url="https://your-openai-compatible-endpoint.example/v1", api_key="provider-key")
set_default_openai_client(custom_client, use_for_tracing=False)

set_tracing_export_api_key("sk-tracing")
```

使用默认导出器时，如果需要将追踪归属到特定组织或项目，请在应用启动前设置以下环境变量：

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

也可以为每次运行设置追踪 API 密钥，而无需更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

还可以使用 [`set_tracing_disabled()`][agents.set_tracing_disabled] 函数完全禁用追踪。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

如果希望保持追踪启用，但从追踪负载中排除可能包含敏感信息的输入和输出，请将 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 设置为 `False`：

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

也可以在应用启动前设置以下环境变量，以便在不修改代码的情况下更改默认值：

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

有关完整的追踪控制选项，请参阅[追踪指南](tracing.md)。

## 调试日志 {#debug-logging}

SDK 定义了两个 Python 日志记录器（`openai.agents` 和 `openai.agents.tracing`），默认不附加处理器。日志遵循应用的 Python 日志配置。

如需启用详细日志，请使用 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 函数。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

或者，也可以通过添加处理器、过滤器、格式化器等来自定义日志。有关更多信息，请参阅 [Python 日志指南](https://docs.python.org/3/howto/logging.html)。

```python
import logging

logger = logging.getLogger("openai.agents") # or openai.agents.tracing for the Tracing logger

# To make all logs show up
logger.setLevel(logging.DEBUG)
# To make info and above show up
logger.setLevel(logging.INFO)
# To make warning and above show up
logger.setLevel(logging.WARNING)
# etc

# You can customize this as needed, but this will output to `stderr` by default
logger.addHandler(logging.StreamHandler())
```

### 日志与诊断信息中的敏感数据 {#sensitive-data-in-logs-and-diagnostics}

某些日志和诊断异常可能包含敏感数据，例如模型或工具的输入和输出。

默认情况下，SDK **不会**记录 LLM 输入/输出或工具输入/输出。以下配置控制这些保护措施：

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

如果需要为调试临时包含这些数据，请在应用启动前将任一变量设置为 `0`（或 `false`）：

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```

这些标志还控制相关故障是否保留包含负载的诊断详情。例如，启用工具数据脱敏后，`FunctionTool` 的无效参数会引发通用的 `ModelBehaviorError`，而不会链接底层验证错误。将任一变量设置为 `0`，可能会在日志、异常消息、异常链和其他诊断上下文中暴露原始模型或工具数据，因此请仅在受控的开发环境中启用。