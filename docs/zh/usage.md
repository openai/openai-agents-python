---
search:
  exclude: true
---
# 使用量

Agents SDK 会自动为每次运行跟踪令牌使用量。你可以从运行上下文中获取它，用于监控成本、实施限制或记录分析数据。

## 跟踪内容

- **requests**: 发起的 LLM API 调用次数
- **input_tokens**: 发送的输入令牌总数
- **output_tokens**: 接收的输出令牌总数
- **total_tokens**: 输入 + 输出
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 从一次运行中获取使用量

在执行 `Runner.run(...)` 之后，通过 `result.context_wrapper.usage` 访问使用量。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量会在该次运行中的所有模型调用之间聚合（包括工具调用和任务转移）。

### 在 LiteLLM 模型中启用使用量

LiteLLM 提供方默认不报告使用指标。当你使用 [`LitellmModel`](models/litellm.md) 时，将 `ModelSettings(include_usage=True)` 传给你的智能体，以便 LiteLLM 的响应填充 `result.context_wrapper.usage`。

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

## 使用会话获取使用量

当你使用 `Session`（例如 `SQLiteSession`）时，每次调用 `Runner.run(...)` 都会返回该次运行的使用量。会话会维护对话历史用于上下文，但每次运行的使用量彼此独立。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

请注意，尽管会话会在运行之间保留对话上下文，但每次 `Runner.run()` 返回的使用量仅代表该次执行。在会话中，先前消息可能会作为输入重新提供给每次运行，从而影响后续轮次的输入令牌计数。

## 在钩子中使用使用量

如果你在使用 `RunHooks`，传递给每个钩子的 `context` 对象包含 `usage`。这使你可以在关键生命周期时刻记录使用量。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 参考

欲了解详细的 API 文档，请参见：

- [`Usage`][agents.usage.Usage] - 使用量追踪数据结构
- [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文访问使用量
- [`RunHooks`][agents.run.RunHooks] - 钩入使用量追踪生命周期