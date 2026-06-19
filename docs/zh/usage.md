---
search:
  exclude: true
---
# 使用量

Agents SDK会自动追踪每次运行的token使用量。您可以从运行上下文中访问它，并用它来监控成本、强制执行限制或记录分析数据。

## 追踪内容

- **requests**: 发起的LLM API调用次数
- **input_tokens**: 发送的输入token总数
- **output_tokens**: 接收的输出token总数
- **total_tokens**: 输入 + 输出
- **request_usage_entries**: 每个请求的使用量明细列表
- **details**:
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens`

## 运行中的使用量访问

在`Runner.run(...)`之后，通过`result.context_wrapper.usage`访问使用量。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量会在运行期间的所有模型调用中汇总（包括工具调用和任务转移）。

### 第三方适配器的使用量启用

使用量报告会因第三方适配器和提供商后端而异。如果您依赖由适配器支持的模型，并且需要准确的`result.context_wrapper.usage`值：

- 使用`AnyLLMModel`时，当上游提供商返回使用量数据时，使用量会自动传递。对于流式传输的Chat Completions后端，可能需要设置`ModelSettings(include_usage=True)`后才会发出使用量数据块。
- 使用`LitellmModel`时，某些提供商后端默认不报告使用量，因此通常需要`ModelSettings(include_usage=True)`。

请查看模型指南中[第三方适配器](models/index.md#third-party-adapters)部分的适配器特定说明，并验证您计划部署的具体提供商后端。

## 按请求的使用量追踪

SDK会在`request_usage_entries`中自动追踪每个API请求的使用量，可用于详细的成本计算和监控上下文窗口消耗。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 会话中的使用量访问

当使用`Session`（例如`SQLiteSession`）时，每次调用`Runner.run(...)`都会返回该特定运行的使用量。会话会维护用于上下文的对话历史，但每次运行的使用量都是独立的。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

请注意，尽管会话会在运行之间保留对话上下文，但每次`Runner.run()`调用返回的使用量指标仅代表该特定执行。在会话中，之前的消息可能会作为输入重新提供给每次运行，这会影响后续轮次中的输入token数量。

## 钩子中的使用量访问

如果您使用`RunHooks`，传递给每个钩子的`context`对象都包含`usage`。这使您能够在关键生命周期时刻记录使用量。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API参考

有关详细的API文档，请参阅：

-   [`Usage`][agents.usage.Usage] - 使用量追踪数据结构
-   [`RequestUsage`][agents.usage.RequestUsage] - 每个请求的使用量详情
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文访问使用量
-   [`RunHooks`][agents.run.RunHooks] - 接入使用量追踪生命周期