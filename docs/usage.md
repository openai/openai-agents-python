# Usage

The Agents SDK automatically tracks token usage for every run. You can access it from the run context and use it to monitor costs, enforce limits, or record analytics.

## What is tracked

- **requests**: number of LLM API calls made
- **input_tokens**: total input tokens sent
- **output_tokens**: total output tokens received
- **total_tokens**: input + output
- **request_usage_entries**: list of per-request usage breakdowns
- **details**:
  - `input_tokens_details.cached_tokens`
  - `input_tokens_details.cache_write_tokens`
  - `output_tokens_details.reasoning_tokens`

## Accessing usage from a run

After `Runner.run(...)`, access usage via `result.context_wrapper.usage`.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")
usage = result.context_wrapper.usage

print("Requests:", usage.requests)
print("Input tokens:", usage.input_tokens)
print("Output tokens:", usage.output_tokens)
print("Total tokens:", usage.total_tokens)
```

Usage is aggregated across all model calls during the run, including model calls that produce tool calls or handoffs.

When an [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] automatically compacts history before the run finishes, usage reported by that `responses.compact` request is also added to the same run totals. A manual `run_compaction()` call made outside a run has no enclosing run context, so it does not update the usage object returned by an earlier run. See [OpenAI Responses compaction sessions](sessions/index.md#openai-responses-compaction-sessions).

### Enabling usage with third-party adapters

Usage reporting varies across third-party adapters and provider backends. If you access models through third-party adapters and need accurate `result.context_wrapper.usage` values:

- With `AnyLLMModel`, usage is propagated automatically when the upstream provider returns it. When streaming responses from a Chat Completions backend, you may need `ModelSettings(include_usage=True)` for usage chunks to be emitted.
- With `LitellmModel`, some provider backends do not report usage by default, so `ModelSettings(include_usage=True)` is often required.

Review the adapter-specific notes in the [Third-party adapters](models/index.md#third-party-adapters) section of the Models guide and validate usage reporting on the exact provider backend you plan to deploy.

## Per-request usage tracking

The SDK automatically tracks usage for each API request in `request_usage_entries`, useful for detailed cost calculation and monitoring context window consumption.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

When the SDK aggregates one [`Usage`][agents.usage.Usage] object into another, it copies the per-request entries and their nested input/output token details. Later mutation of the source usage object cannot change the aggregate's `request_usage_entries`, and mutation of the aggregate cannot change the source entries.

## Preserving provider usage payloads

The Agents SDK normalizes provider usage into [`Usage`][agents.usage.Usage] fields that provide consistent totals across model providers. Set [`ModelSettings.preserve_raw_usage`][agents.model_settings.ModelSettings.preserve_raw_usage] to `True` when an application must retain provider-specific usage fields or distinguish an omitted field from a provider-reported zero:

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

The Agents SDK stores each [`ModelResponse.raw_usage`][agents.items.ModelResponse.raw_usage] value as a detached, JSON-compatible snapshot of the provider payload for that model call. The Agents SDK does not aggregate `raw_usage` across the run. The value remains `None` when preservation is disabled, the provider returns no usage payload, or an upstream adapter has already discarded the original field-presence information.

`preserve_raw_usage` preserves only a usage payload that reaches the model adapter; the setting does not request usage from the provider. When a streaming Chat Completions provider requires an explicit usage request, also set `ModelSettings(include_usage=True)`.

`LitellmModel` does not currently populate `ModelResponse.raw_usage` in either streaming or non-streaming runs, so `preserve_raw_usage=True` has no effect with that adapter. Continue to use the normalized [`Usage`][agents.usage.Usage] fields when using `LitellmModel`, or choose an adapter that supports raw usage preservation when provider-specific field presence is required.

## Accessing usage with sessions

When you use a `Session` (e.g., `SQLiteSession`), each call to `Runner.run(...)` returns usage for that specific run. Sessions maintain conversation history for context, but each run's usage is independent.

```python
session = SQLiteSession("my_conversation")

first = await Runner.run(agent, "Hi!", session=session)
print(first.context_wrapper.usage.total_tokens)  # Usage for first run

second = await Runner.run(agent, "Can you elaborate?", session=session)
print(second.context_wrapper.usage.total_tokens)  # Usage for second run
```

Note that while sessions preserve conversation context between runs, the usage metrics returned by each `Runner.run()` call represent only that particular execution. In sessions, previous messages may be re-fed as input to each run, which affects the input token count in subsequent turns.

## Usage in RunState checkpoints

[`RunResult.to_state()`][agents.result.RunResult.to_state] captures an independent snapshot of the usage accumulated so far. A run resumed from that checkpoint starts with the captured totals and adds usage from its own model calls. The resumed run does not add those new totals to the original `RunResult` or to another checkpoint created from that result.

```python
first = await Runner.run(agent, "First request")
checkpoint_a = first.to_state()
checkpoint_b = first.to_state()

resumed_a = await Runner.run(agent, checkpoint_a)
resumed_b = await Runner.run(agent, checkpoint_b)

assert resumed_a.context_wrapper.usage is not first.context_wrapper.usage
assert resumed_b.context_wrapper.usage is not resumed_a.context_wrapper.usage
```

This isolation also applies to the `request_usage_entries` list inside [`Usage`][agents.usage.Usage]. A resumed nested [`Agent.as_tool()`][agents.agent.Agent.as_tool] run is the exception to independent top-level accounting: its post-resume model usage is deliberately aggregated into the active outer run's usage, just like the nested run's earlier model calls.

## Using usage in hooks

If you're using `RunHooks`, the `context` object passed to each hook contains `usage`. This lets you log usage at key lifecycle moments.

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## API reference

For detailed API documentation, see:

-   [`Usage`][agents.usage.Usage] - Usage tracking data structure
-   [`RequestUsage`][agents.usage.RequestUsage] - Per-request usage details
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - Access usage from run context
-   [`RunHooks`][agents.run.RunHooks] - Hook into usage tracking lifecycle
