---
search:
  exclude: true
---
# 配置 SDK

本页介绍 SDK 范围内的默认设置，这些通常会在应用启动时设置一次，例如默认 OpenAI key 或客户端、默认 OpenAI API 形态、追踪导出默认值以及日志行为。

如果你需要改为配置某个特定智能体或某次运行，请从以下内容开始：

-   查看[运行智能体](running_agents.md)，了解 `RunConfig`、会话以及对话状态选项。
-   查看[模型](models/index.md)，了解模型选择与提供方配置。
-   查看[追踪](tracing.md)，了解按次运行的追踪元数据和自定义追踪进程。

## API 密钥与客户端

默认情况下，SDK 使用 `OPENAI_API_KEY` 环境变量来处理 LLM 请求和追踪。该密钥会在 SDK 首次创建 OpenAI 客户端时解析（延迟初始化），因此请在首次模型调用前设置好该环境变量。如果你无法在应用启动前设置该环境变量，可以使用 [set_default_openai_key()][agents.set_default_openai_key] 函数来设置密钥。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

或者，你也可以配置要使用的 OpenAI 客户端。默认情况下，SDK 会创建一个 `AsyncOpenAI` 实例，并使用环境变量中的 API 密钥或上面设置的默认密钥。你可以通过 [set_default_openai_client()][agents.set_default_openai_client] 函数更改此行为。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最后，你还可以自定义所使用的 OpenAI API。默认情况下，我们使用 OpenAI Responses API。你可以通过 [set_default_openai_api()][agents.set_default_openai_api] 函数将其覆盖为 Chat Completions API。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 追踪

默认启用追踪。默认情况下，它会使用与你在上文模型请求中相同的 OpenAI API 密钥（即环境变量中的密钥或你设置的默认密钥）。你也可以通过 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 函数专门设置用于追踪的 API 密钥。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

如果在使用默认导出器时，你需要将追踪归属到特定组织或项目，请在应用启动前设置以下环境变量：

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

你还可以为每次运行设置追踪 API 密钥，而无需更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

你也可以通过 [`set_tracing_disabled()`][agents.set_tracing_disabled] 函数完全禁用追踪。

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

你也可以在不修改代码的情况下，通过在应用启动前设置此环境变量来更改默认值：

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

完整的追踪控制请参阅[追踪指南](tracing.md)。

## 调试日志

SDK 定义了两个 Python logger（`openai.agents` 和 `openai.agents.tracing`），并且默认不附加 handlers。日志会遵循你应用的 Python logging 配置。

要启用详细日志，请使用 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 函数。

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

默认情况下，SDK **不会**记录 LLM 输入/输出或工具输入/输出。这些保护由以下项控制：

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

如果你需要为调试临时包含这些数据，请在应用启动前将任一变量设置为 `0`（或 `false`）：

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```