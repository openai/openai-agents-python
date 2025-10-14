---
search:
  exclude: true
---
# 使用统计

Agents SDK会自动跟踪每次运行的token使用情况。你可以从运行上下文中访问它，用于监控成本、强制执行限制或记录分析。

## 跟踪内容

- **requests**: 发出的LLM API调用数量
- **input_tokens**: 发送的总输入token数
- **output_tokens**: 接收的总输出token数
- **total_tokens**: 输入 + 输出
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 从运行中访问使用情况

在 `Runner.run(...)` 之后，通过 `result.context_wrapper.usage` 访问使用情况。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用情况在运行期间的所有模型调用（包括工具调用和交接）中进行汇总。

### 启用LiteLLM模型的使用情况

LiteLLM提供程序默认不报告使用指标。当你使用 [`LitellmModel`](models/litellm.md) 时，向你的智能体传递 `ModelSettings(include_usage=True)`，这样LiteLLM响应就会填充到 `result.context_wrapper.usage` 中。

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

## 在会话中获取使用情况

当你使用 `Session`（例如 `SQLiteSession`）时，每次调用 `Runner.run(...)` 都会返回该特定运行的使用情况。会话为上下文维护对话历史，但每次运行的使用情况是独立的。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # 第一次运行的使用情况

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # 第二次运行的使用情况
```

请注意，虽然会话在运行之间保留对话上下文，但每次 `Runner.run()` 调用返回的使用情况指标只代表该特定执行。在会话中，先前的消息可能会作为输入重新提供给每次运行，这会影响后续轮次的输入token计数。

## 在钩子中使用使用情况

如果你正在使用 `RunHooks`，传递给每个钩子的 `context` 对象包含 `usage`。这让你在关键生命周期时刻记录使用情况。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} 次请求, {u.total_tokens} 总token数")
```

## API 参考

有关详细的 API 文档，请参见：

- [`Usage`][agents.usage.Usage] - 使用情况跟踪数据结构
- [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文访问使用情况
- [`RunHooks`][agents.run.RunHooks] - 挂钩到使用情况跟踪生命周期