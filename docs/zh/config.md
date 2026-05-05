---
search:
  exclude: true
---
# 配置

本页面介绍通常在应用启动时一次性设置的 SDK 全局默认项，例如默认 OpenAI key 或 client、默认 OpenAI API 形态、追踪导出默认项以及日志行为。

这些默认项同样适用于基于沙箱的工作流，但沙箱工作区、沙箱客户端和会话复用需要单独配置。

如果你需要改为配置特定智能体或运行，请先查看：

-   普通 `Agent` 的 instructions、tools、输出类型、任务转移和安全防护措施，请参阅[智能体](agents.md)。
-   `RunConfig`、会话和对话状态选项，请参阅[运行智能体](running_agents.md)。
-   `SandboxRunConfig`、清单、能力和沙箱客户端专属工作区设置，请参阅[沙箱智能体](sandbox/guide.md)。
-   模型选择和提供方配置，请参阅[模型](models/index.md)。
-   每次运行的追踪元数据和自定义追踪进程，请参阅[追踪](tracing.md)。

## API keys 与 clients

默认情况下，SDK 使用 `OPENAI_API_KEY` 环境变量来处理 LLM 请求和追踪。该 key 会在 SDK 首次创建 OpenAI client 时解析（惰性初始化），因此请在首次模型调用前设置该环境变量。如果你的应用启动前无法设置该环境变量，可以使用 [set_default_openai_key()][agents.set_default_openai_key] 函数设置 key。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

或者，你也可以配置要使用的 OpenAI client。默认情况下，SDK 会创建一个 `AsyncOpenAI` 实例，使用来自环境变量的 API key 或上面设置的默认 key。你可以通过 [set_default_openai_client()][agents.set_default_openai_client] 函数进行修改。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

如果你更偏好基于环境变量的 endpoint 配置，默认 OpenAI provider 也会读取 `OPENAI_BASE_URL`。启用 Responses websocket 传输时，它还会读取 `OPENAI_WEBSOCKET_BASE_URL` 用于 websocket `/responses` endpoint。

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

最后，你还可以自定义所使用的 OpenAI API。默认情况下我们使用 OpenAI Responses API。你可以通过 [set_default_openai_api()][agents.set_default_openai_api] 函数将其覆盖为 Chat Completions API。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 追踪

追踪默认启用。默认情况下，它使用与你在上文模型请求中相同的 OpenAI API key（即环境变量中的 key，或你设置的默认 key）。你可以使用 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 函数专门设置追踪使用的 API key。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

如果你的模型流量使用一个 key 或 client，但追踪应使用另一个 OpenAI key，请在设置默认 key 或 client 时传入 `use_for_tracing=False`，然后单独配置追踪。如果你未使用自定义 client，也可对 [`set_default_openai_key()`][agents.set_default_openai_key] 使用同样模式。

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

如果使用默认导出器时，你需要将 traces 归属到特定 organization 或 project，请在应用启动前设置这些环境变量：

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

你也可以按每次运行设置追踪 API key，而无需更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

你还可以使用 [`set_tracing_disabled()`][agents.set_tracing_disabled] 函数完全禁用追踪。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

如果你希望保持追踪启用，但从追踪负载中排除可能敏感的输入/输出，请将 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 设为 `False`：

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

你也可以不写代码，在应用启动前设置此环境变量来修改默认行为：

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

完整的追踪控制请参阅[追踪指南](tracing.md)。

## 调试日志

SDK 定义了两个 Python logger（`openai.agents` 和 `openai.agents.tracing`），默认不附加 handlers。日志遵循你应用的 Python 日志配置。

如需启用详细日志，请使用 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 函数。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

或者，你也可以通过添加 handlers、filters、formatters 等来自定义日志。更多信息请参阅[Python logging 指南](https://docs.python.org/3/howto/logging.html)。

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

默认情况下，SDK **不会**记录 LLM 输入/输出或 tools 输入/输出。这些保护由以下项控制：

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

如果你需要为调试临时包含这些数据，请在应用启动前将任一变量设为 `0`（或 `false`）：

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```