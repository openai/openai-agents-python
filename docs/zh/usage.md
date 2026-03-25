---
search:
  exclude: true
---
# 用法

Agents SDK 会自动追踪每次运行的 token 使用情况。你可以从运行上下文中访问这些数据，并用它来监控成本、执行限制或记录分析数据。

## 追踪内容

- **requests**: 发起的 LLM API 调用次数
- **input_tokens**: 发送的输入 token 总数
- **output_tokens**: 接收的输出 token 总数
- **total_tokens**: 输入 + 输出
- **request_usage_entries**: 按请求划分的使用明细列表
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 从一次运行中访问使用情况

在 `Runner.run(...)` 之后，可通过 `result.context_wrapper.usage` 访问使用情况。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量会汇总该次运行期间所有模型调用（包括工具调用和任务转移）。

### 在第三方适配器中启用使用情况追踪

不同第三方适配器和提供方后端的使用情况上报方式有所不同。如果你依赖由适配器支持的模型并且需要准确的 `result.context_wrapper.usage` 值：

- 使用 `AnyLLMModel` 时，如果上游提供方返回了使用数据，则会自动透传。对于流式 Chat Completions 后端，在发出 usage 分块前，你可能需要设置 `ModelSettings(include_usage=True)`。
- 使用 `LitellmModel` 时，某些提供方后端默认不会上报使用数据，因此通常需要 `ModelSettings(include_usage=True)`。

请查看 Models 指南中[第三方适配器](models/index.md#third-party-adapters)章节的适配器说明，并验证你计划部署的具体提供方后端。

## 按请求追踪使用情况

SDK 会自动在 `request_usage_entries` 中追踪每个 API 请求的使用情况，这对精细化成本计算和上下文窗口消耗监控很有帮助。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 在会话中访问使用情况

当你使用 `Session`（例如 `SQLiteSession`）时，每次调用 `Runner.run(...)` 都会返回该次运行对应的使用数据。会话会维护对话历史以提供上下文，但每次运行的使用数据彼此独立。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

请注意，尽管会话会在多次运行之间保留对话上下文，但每次 `Runner.run()` 调用返回的使用指标只代表该次执行。在会话中，先前消息可能会在每次运行时作为输入再次传入，这会影响后续轮次的输入 token 计数。

## 在 hooks 中使用使用情况

如果你使用 `RunHooks`，传递给每个 hook 的 `context` 对象都包含 `usage`。这使你可以在关键生命周期节点记录使用情况。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 参考

详细 API 文档请参见：

-   [`Usage`][agents.usage.Usage] - 使用情况追踪数据结构
-   [`RequestUsage`][agents.usage.RequestUsage] - 按请求划分的使用详情
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文访问使用情况
-   [`RunHooks`][agents.run.RunHooks] - 挂接到使用情况追踪生命周期