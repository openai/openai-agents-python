---
search:
  exclude: true
---
# 使用量

Agents SDK会自动追踪每次运行的 token 使用量。你可以从运行上下文中访问这些数据，用于监控成本、强制执行限制或记录分析数据。

## 追踪内容 {#what-is-tracked}

- **requests**：发起的 LLM API 调用次数
- **input_tokens**：发送的输入 token 总数
- **output_tokens**：接收的输出 token 总数
- **total_tokens**：输入 + 输出
- **request_usage_entries**：每个请求的使用量明细列表
- **details**：
  - `input_tokens_details.cached_tokens`
  - `input_tokens_details.cache_write_tokens`
  - `output_tokens_details.reasoning_tokens`

## 从运行中访问使用量 {#accessing-usage-from-a-run}

执行 `Runner.run(...)` 后，通过 `result.context_wrapper.usage` 访问使用量。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

使用量会汇总运行期间的所有模型调用，包括生成工具调用或任务转移的模型调用。

当 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] 在运行结束前自动压缩历史记录时，该 `responses.compact` 请求报告的使用量也会添加到同一次运行的总量中。在运行之外手动调用 `run_compaction()` 时，由于没有包含该调用的运行上下文，因此不会更新先前运行返回的使用量对象。请参阅 [OpenAI Responses 压缩会话](sessions/index.md#openai-responses-compaction-sessions)。

### 使用第三方适配器启用使用量统计 {#enabling-usage-with-third-party-adapters}

不同第三方适配器和提供商后端的使用量报告方式各不相同。如果你通过第三方适配器访问模型，并且需要准确的 `result.context_wrapper.usage` 值：

- 使用 `AnyLLMModel` 时，如果上游提供商返回使用量数据，系统会自动传递这些数据。从 Chat Completions 后端以流式方式获取响应时，可能需要设置 `ModelSettings(include_usage=True)`，才能发出使用量数据块。
- 使用 `LitellmModel` 时，某些提供商后端默认不报告使用量，因此通常需要设置 `ModelSettings(include_usage=True)`。

请查看模型指南中[第三方适配器](models/index.md#third-party-adapters)一节的适配器专属说明，并在计划部署的具体提供商后端上验证使用量报告。

## 按请求追踪使用量 {#per-request-usage-tracking}

SDK 会在 `request_usage_entries` 中自动追踪每个 API 请求的使用量，这有助于详细计算成本和监控上下文窗口消耗。

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

## 提供商使用量有效载荷的保留 {#preserving-provider-usage-payloads}

Agents SDK会将提供商使用量标准化为 [`Usage`][agents.usage.Usage] 字段，从而在不同模型提供商之间提供一致的总量。当应用必须保留提供商特定的使用量字段，或需要区分被省略的字段与提供商报告为零的字段时，请将 [`ModelSettings.preserve_raw_usage`][agents.model_settings.ModelSettings.preserve_raw_usage] 设置为 `True`：

```python
from agents import Agent, ModelSettings, Runner

agent = Agent(
    name="Assistant",
    model_settings=ModelSettings(preserve_raw_usage=True),
)
result = await Runner.run(agent, "What's the weather in Tokyo?")

for response in result.raw_responses:
    print(response.raw_usage)
```

Agents SDK会将每个 [`ModelResponse.raw_usage`][agents.items.ModelResponse.raw_usage] 值存储为该模型调用的提供商有效载荷的独立 JSON 兼容快照。Agents SDK不会在整个运行过程中汇总 `raw_usage`。当禁用保留功能、提供商未返回使用量有效载荷，或上游适配器已丢弃原始字段是否存在的信息时，该值仍为 `None`。

`preserve_raw_usage` 只会保留到达模型适配器的使用量有效载荷；此设置不会向提供商请求使用量数据。当流式 Chat Completions 提供商要求显式请求使用量数据时，还应设置 `ModelSettings(include_usage=True)`。

无论是流式运行还是非流式运行，`LitellmModel` 目前都不会填充 `ModelResponse.raw_usage`，因此 `preserve_raw_usage=True` 对该适配器不起作用。使用 `LitellmModel` 时，请继续使用标准化的 [`Usage`][agents.usage.Usage] 字段；如果需要提供商特定字段是否存在的信息，请选择支持保留原始使用量的适配器。

## 通过会话访问使用量 {#accessing-usage-with-sessions}

使用 `Session`（例如 `SQLiteSession`）时，每次调用 `Runner.run(...)` 都会返回该次特定运行的使用量。会话会保留对话历史记录作为上下文，但每次运行的使用量相互独立。

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

请注意，虽然会话会在不同运行之间保留对话上下文，但每次调用 `Runner.run()` 返回的使用量指标仅代表该次执行。在会话中，先前的消息可能会作为输入重新传入每次运行，从而影响后续轮次的输入 token 数量。

## RunState 检查点中的使用量 {#usage-in-runstate-checkpoints}

[`RunResult.to_state()`][agents.result.RunResult.to_state] 会捕获截至当前已累计使用量的独立快照。从该检查点恢复的运行以捕获的总量为起点，并在此基础上添加自身模型调用的使用量。恢复后的运行不会将这些新增总量添加到原始 `RunResult`，也不会添加到根据该结果创建的其他检查点。

```python
first = await Runner.run(agent, "First request")
checkpoint_a = first.to_state()
checkpoint_b = first.to_state()

resumed_a = await Runner.run(agent, checkpoint_a)
resumed_b = await Runner.run(agent, checkpoint_b)

assert resumed_a.context_wrapper.usage is not first.context_wrapper.usage
assert resumed_b.context_wrapper.usage is not resumed_a.context_wrapper.usage
```

这种隔离也适用于 [`Usage`][agents.usage.Usage] 中的 `request_usage_entries` 列表。恢复后的嵌套 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行是顶层独立计量的例外：该嵌套运行恢复后的模型使用量会被有意汇总到当前外层运行的使用量中，与该嵌套运行先前的模型调用处理方式相同。

## 钩子中的使用量 {#using-usage-in-hooks}

如果你使用 `RunHooks`，传递给每个钩子的 `context` 对象都包含 `usage`。这样便可在生命周期的关键时刻记录使用量。

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API 参考 {#api-reference}

有关详细的 API 文档，请参阅：

-   [`Usage`][agents.usage.Usage] - 使用量追踪数据结构
-   [`RequestUsage`][agents.usage.RequestUsage] - 每个请求的使用量详情
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - 从运行上下文中访问使用量
-   [`RunHooks`][agents.run.RunHooks] - 接入使用量追踪生命周期