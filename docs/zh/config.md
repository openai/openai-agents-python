---
search:
  exclude: true
---
# 配置

本页介绍通常在应用启动期间只需设置一次的 SDK 全局默认值，例如默认 OpenAI 密钥或客户端、默认 OpenAI API 形态、追踪导出默认值以及日志记录行为。

这些默认值仍适用于基于沙盒的工作流，但沙盒工作区、沙盒客户端和会话复用需要单独配置。

如果你需要改为配置特定智能体或运行，请从以下内容开始：

-   [智能体](agents.md)：了解普通 `Agent` 上的 instructions、tools、输出类型、任务转移和安全防护措施。
-   [运行智能体](running_agents.md)：了解 `RunConfig`、会话和对话状态选项。
-   [沙盒智能体](sandbox/guide.md)：了解 `SandboxRunConfig`、清单、能力以及特定于沙盒客户端的工作区设置。
-   [模型](models/index.md)：了解模型选择和提供方配置。
-   [追踪](tracing.md)：了解每次运行的追踪元数据和自定义追踪处理器。

## API 密钥和客户端

默认情况下，SDK 使用 `OPENAI_API_KEY` 环境变量处理 LLM 请求和追踪。该密钥会在 SDK 首次创建 OpenAI 客户端时解析（惰性初始化），因此请在首次模型调用之前设置环境变量。如果你无法在应用启动前设置该环境变量，可以使用 [set_default_openai_key()][agents.set_default_openai_key] 函数来设置密钥。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

或者，你也可以配置要使用的 OpenAI 客户端。默认情况下，SDK 会创建一个 `AsyncOpenAI` 实例，使用环境变量中的 API 密钥或上面设置的默认密钥。你可以使用 [set_default_openai_client()][agents.set_default_openai_client] 函数更改此行为。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

如果你更偏好基于环境变量的端点配置，默认 OpenAI 提供方也会读取 `OPENAI_BASE_URL`。当你启用 Responses websocket 传输时，它还会读取 `OPENAI_WEBSOCKET_BASE_URL` 作为 websocket `/responses` 端点。

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

最后，你还可以自定义所使用的 OpenAI API。默认情况下，我们使用 OpenAI Responses API。你可以使用 [set_default_openai_api()][agents.set_default_openai_api] 函数将其覆盖为使用 Chat Completions API。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 追踪

追踪默认启用。默认情况下，它使用与你在上一节中的模型请求相同的 OpenAI API 密钥（即环境变量中的密钥或你设置的默认密钥）。你可以使用 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 函数专门设置用于追踪的 API 密钥。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

如果你的模型流量使用一个密钥或客户端，但追踪应使用另一个 OpenAI 密钥，请在设置默认密钥或客户端时传入 `use_for_tracing=False`，然后单独配置追踪。如果你没有使用自定义客户端，同样的模式也适用于 [`set_default_openai_key()`][agents.set_default_openai_key]。

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

如果你在使用默认导出器时需要将追踪归因到特定组织或项目，请在应用启动前设置这些环境变量：

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

你也可以在每次运行时设置追踪 API 密钥，而无需更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

你也可以使用 [`set_tracing_disabled()`][agents.set_tracing_disabled] 函数完全禁用追踪。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

如果你希望保持追踪启用，但从追踪负载中排除可能敏感的输入/输出，请将 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 设置为 `False`：

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

你也可以通过在应用启动前设置此环境变量，在不编写代码的情况下更改默认值：

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

有关完整的追踪控制，请参阅[追踪指南](tracing.md)。

## 调试日志

SDK 定义了两个 Python 日志记录器（`openai.agents` 和 `openai.agents.tracing`），并且默认不附加处理器。日志遵循你的应用的 Python 日志配置。

要启用详细日志，请使用 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 函数。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

或者，你可以通过添加处理器、过滤器、格式化器等来自定义日志。你可以在 [Python 日志指南](https://docs.python.org/3/howto/logging.html)中了解更多信息。

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

### 日志中的敏感数据

某些日志可能包含敏感数据（例如用户数据）。

默认情况下，SDK **不会**记录 LLM 输入/输出或工具输入/输出。这些保护由以下内容控制：

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

如果你需要临时包含这些数据以进行调试，请在应用启动前将任一变量设置为 `0`（或 `false`）：

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```