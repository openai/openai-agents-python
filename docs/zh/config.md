---
search:
  exclude: true
---
# 配置 SDK

## API 密钥和客户端

默认情况下，SDK 会在导入后立即查找 `OPENAI_API_KEY` 环境变量以进行 LLM 请求和追踪。如果你无法在应用启动前设置该环境变量，可以使用 [set_default_openai_key()][agents.set_default_openai_key] 函数来设置密钥。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

或者，你也可以配置要使用的 OpenAI 客户端。默认情况下，SDK 会创建一个 `AsyncOpenAI` 实例，使用来自环境变量或上面设置的默认密钥的 API 密钥。你可以通过使用 [set_default_openai_client()][agents.set_default_openai_client] 函数来更改此设置。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最后，你还可以自定义使用的 OpenAI API。默认情况下，我们使用 OpenAI Responses API。你可以通过使用 [set_default_openai_api()][agents.set_default_openai_api] 函数来覆盖此设置以使用聊天完成 API。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 追踪

追踪默认启用。它默认使用上面部分中的 OpenAI API 密钥（即环境变量或你设置的默认密钥）。你可以通过使用 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 函数来专门设置用于追踪的 API 密钥。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

你还可以通过使用 [`set_tracing_disabled()`][agents.set_tracing_disabled] 函数来完全禁用追踪。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## 调试日志记录

SDK 有两个 Python 日志记录器，没有设置任何处理程序。默认情况下，这意味着警告和错误会发送到 `stdout`，但其他日志会被抑制。

要启用详细日志记录，请使用 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 函数。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

或者，你可以通过添加处理程序、过滤器、格式化程序等来定制日志。你可以在 [Python 日志记录指南](https://docs.python.org/3/howto/logging.html) 中阅读更多内容。

```python
import logging

logger = logging.getLogger("openai.agents") # 或 openai.agents.tracing 用于追踪日志记录器

# 要使所有日志显示
logger.setLevel(logging.DEBUG)
# 要使信息及以上级别显示
logger.setLevel(logging.INFO)
# 要使警告及以上级别显示
logger.setLevel(logging.WARNING)
# 等等

# 你可以根据需要自定义此设置，但默认情况下这会输出到 `stderr`
logger.addHandler(logging.StreamHandler())
```

### 日志中的敏感数据

某些日志可能包含敏感数据（例如，用户数据）。如果你想禁用记录这些数据，请设置以下环境变量。

要禁用记录 LLM 输入和输出：

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

要禁用记录工具输入和输出：

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```