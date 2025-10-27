---
search:
  exclude: true
---
# 配置 SDK

## API 密钥与客户端

默认情况下，SDK 在被导入后会立即查找用于 LLM 请求和追踪的 `OPENAI_API_KEY` 环境变量。如果无法在应用启动前设置该环境变量，可以使用 [set_default_openai_key()][agents.set_default_openai_key] 函数来设置密钥。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

或者，你也可以配置要使用的 OpenAI 客户端。默认情况下，SDK 会创建一个 `AsyncOpenAI` 实例，使用来自环境变量的 API 密钥或上面设置的默认密钥。你可以使用 [set_default_openai_client()][agents.set_default_openai_client] 函数进行更改。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最后，你还可以自定义要使用的 OpenAI API。默认情况下，我们使用 OpenAI Responses API。你可以使用 [set_default_openai_api()][agents.set_default_openai_api] 函数将其改为使用 Chat Completions API。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 追踪

追踪默认启用。它默认使用上述部分中的 OpenAI API 密钥（即环境变量或你设置的默认密钥）。你可以使用 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 函数单独设置用于追踪的 API 密钥。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

你也可以使用 [`set_tracing_disabled()`][agents.set_tracing_disabled] 函数完全禁用追踪。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## 调试日志

SDK 有两个未设置任何处理器的 Python 日志记录器。默认情况下，这意味着警告和错误将发送到 `stdout`，但其他日志会被抑制。

要启用详细日志，使用 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 函数。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

或者，你可以通过添加处理器、过滤器、格式化器等来自定义日志。更多信息请参见 [Python logging guide](https://docs.python.org/3/howto/logging.html)。

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

某些日志可能包含敏感数据（例如，用户数据）。如果你想禁用这些数据的记录，请设置以下环境变量。

要禁用记录 LLM 的输入与输出：

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

要禁用记录工具的输入与输出：

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```