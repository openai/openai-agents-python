---
search:
  exclude: true
---
# 用法

Agents SDK 会自动跟踪每次运行的 token 使用情况。你可以从运行上下文中访问它，并用它来监控成本、强制限制或记录分析数据。

## 跟踪内容

- **requests**: 发起的 LLM API 调用次数
- **input_tokens**: 发送的输入 token 总数
- **output_tokens**: 接收的输出 token 总数
- **total_tokens**: 输入 + 输出
- **request_usage_entries**: 按请求划分的使用情况明细列表
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 从运行中访问 usage

在 `Runner.run(...)` 之后，通过 `result.context_wrapper.usage` 访问 usage。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

usage 会聚合该次运行期间的所有模型调用（包括工具调用和任务转移）。

### 使用 LiteLLM 模型启用 usage

LiteLLM 提供方默认不会上报 usage 指标。使用 [`LitellmModel`](models/litellm.md) 时，请向你的智能体传入 `ModelSettings(include_usage=True)`，这样 LiteLLM 的响应才会填充 `result.context_wrapper.usage`。

```python
from agents import Agent, ModelSettings, Runner
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)

result = await Runner.run(agent, "What's the weather in Tokyo?")
print(result.context_wrapper.usage.total_tokens)
```

## 按请求 usage 跟踪

SDK 会自动在 `request_usage_entries` 中跟踪每个 API 请求的 usage，这对于精细化成本计算和监控上下文窗口消耗非常有用。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 在会话中访问 usage

当你使用 `Session`（例如 `SQLiteSession`）时，每次调用 `Runner.run(...)` 都会返回该次运行的 usage。会话会为上下文维护对话历史，但每次运行的 usage 是独立的。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

请注意，虽然会话会在多次运行之间保留对话上下文，但每次 `Runner.run()` 调用返回的 usage 指标仅代表该次执行。在会话中，之前的消息可能会在每次运行时作为输入再次提供，这会影响后续轮次的输入 token 计数。

## 在 hooks 中使用 usage

如果你在使用 `RunHooks`，传递给每个 hook 的 `context` 对象都包含 `usage`。这让你可以在关键生命周期节点记录 usage。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 参考

详细 API 文档请参阅：

-   [`Usage`][agents.usage.Usage] - usage 跟踪数据结构
-   [`RequestUsage`][agents.usage.RequestUsage] - 按请求划分的 usage 详情
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文访问 usage
-   [`RunHooks`][agents.run.RunHooks] - 挂接 usage 跟踪生命周期