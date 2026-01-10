---
search:
  exclude: true
---
# 使用量

Agents SDK 会自动为每次运行追踪 token 使用情况。你可以从运行上下文中获取它，用于监控成本、执行限制或记录分析数据。

## 追踪内容

- **requests**: 发起的 LLM API 调用次数
- **input_tokens**: 发送的输入 token 总数
- **output_tokens**: 接收的输出 token 总数
- **total_tokens**: 输入 + 输出
- **request_usage_entries**: 每次请求的使用量明细列表
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 从一次运行访问使用量

在 `Runner.run(...)` 之后，通过 `result.context_wrapper.usage` 获取使用量。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量会在运行期间的所有模型调用中聚合（包括工具调用和任务转移）。

### 在 LiteLLM 模型中启用使用量

LiteLLM 提供方默认不报告使用量指标。当你使用 [`LitellmModel`](models/litellm.md) 时，向你的智能体传入 `ModelSettings(include_usage=True)`，以便 LiteLLM 的响应填充 `result.context_wrapper.usage`。

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

## 按请求的使用量追踪

SDK 会在 `request_usage_entries` 中自动追踪每次 API 请求的使用量，便于进行精细的成本计算和监控上下文窗口消耗。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 在会话中访问使用量

当你使用 `Session`（例如 `SQLiteSession`）时，每次调用 `Runner.run(...)` 都会返回该次运行的使用量。会话会为上下文维护对话历史，但每次运行的使用量彼此独立。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

请注意，虽然会话会在运行之间保留对话上下文，但每次 `Runner.run()` 调用返回的使用量指标仅代表该次执行。在会话中，先前消息可能会在每次运行时重新作为输入提供，这会影响后续轮次的输入 token 计数。

## 在钩子中使用使用量

如果你在使用 `RunHooks`，传递给每个钩子的 `context` 对象包含 `usage`。这使你可以在关键生命周期时刻记录使用量。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 参考

如需详细的 API 文档，请参阅：

- [`Usage`][agents.usage.Usage] - 使用量追踪数据结构
- [`RequestUsage`][agents.usage.RequestUsage] - 按请求的使用量详情
- [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文访问使用量
- [`RunHooks`][agents.run.RunHooks] - 挂载到使用量追踪的生命周期