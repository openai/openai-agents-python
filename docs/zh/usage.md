---
search:
  exclude: true
---
# 用量

Agents SDK 会自动跟踪每次运行的 token 用量。你可以从运行上下文中访问它，并用它来监控成本、执行限制或记录分析数据。

## 跟踪内容

- **requests**: 发起的 LLM API 调用次数
- **input_tokens**: 发送的输入 token 总数
- **output_tokens**: 接收的输出 token 总数
- **total_tokens**: 输入 + 输出
- **request_usage_entries**: 按请求列出的用量明细列表
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 运行中的用量访问

在 `Runner.run(...)` 之后，通过 `result.context_wrapper.usage` 访问用量。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

用量会汇总运行期间的所有模型调用（包括工具调用和任务转移）。

### 第三方适配器中的用量启用

不同第三方适配器和提供方后端的用量报告方式各不相同。如果你依赖适配器支持的模型，并且需要准确的 `result.context_wrapper.usage` 值：

- 使用 `AnyLLMModel` 时，只要上游提供方返回用量，用量就会自动传递。对于流式传输的 Chat Completions 后端，你可能需要在发出用量块之前设置 `ModelSettings(include_usage=True)`。
- 使用 `LitellmModel` 时，某些提供方后端默认不报告用量，因此通常需要 `ModelSettings(include_usage=True)`。

请查看 Models 指南中[第三方适配器](models/index.md#third-party-adapters)部分的适配器特定说明，并验证你计划部署的具体提供方后端。

## 按请求的用量跟踪

SDK 会在 `request_usage_entries` 中自动跟踪每个 API 请求的用量，这对详细成本计算和上下文窗口消耗监控很有用。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 会话中的用量访问

使用 `Session`（例如 `SQLiteSession`）时，每次调用 `Runner.run(...)` 都会返回该特定运行的用量。会话会维护对话历史作为上下文，但每次运行的用量都是独立的。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

请注意，虽然会话会在运行之间保留对话上下文，但每次 `Runner.run()` 调用返回的用量指标仅代表该次特定执行。在会话中，先前的消息可能会作为输入重新提供给每次运行，这会影响后续轮次中的输入 token 数量。

## 钩子中的用量使用

如果你使用 `RunHooks`，传递给每个钩子的 `context` 对象会包含 `usage`。这使你可以在关键生命周期时刻记录用量。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 参考

有关详细的 API 文档，请参阅：

-   [`Usage`][agents.usage.Usage] - 用量跟踪数据结构
-   [`RequestUsage`][agents.usage.RequestUsage] - 按请求的用量详情
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文访问用量
-   [`RunHooks`][agents.run.RunHooks] - 接入用量跟踪生命周期