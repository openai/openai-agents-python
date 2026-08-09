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

### Enabling usage with third-party adapters

Usage reporting varies across third-party adapters and provider backends. If you access models through third-party adapters and need accurate `result.context_wrapper.usage` values:

- With `AnyLLMModel`, usage is propagated automatically when the upstream provider returns it. When streaming responses from a Chat Completions backend, you may need `ModelSettings(include_usage=True)` for usage chunks to be emitted.
- With `LitellmModel`, some provider backends do not report usage by default, so `ModelSettings(include_usage=True)` is often required.

Review the adapter-specific notes in the [Third-party adapters](models/index.md#third-party-adapters) section of the Models guide and validate usage reporting on the exact provider backend you plan to deploy.

### Preserving the raw provider usage payload

`Usage` normalizes missing fields to `0`, so a field the provider never reported and a field the provider explicitly reported as `0` look identical. If you build cost or coverage reporting on top of usage, opt in to [`ModelSettings.preserve_raw_usage`][agents.model_settings.ModelSettings.preserve_raw_usage] and read [`ModelResponse.raw_usage`][agents.items.ModelResponse.raw_usage] from `result.raw_responses`:

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

`raw_usage` is a JSON-compatible snapshot of the usage object as the model adapter received it, taken before the SDK normalizes it. Keys the provider omitted stay absent, while explicit `null`, zero, and provider-specific keys stay present as received. A provider that omitted `cached_tokens` yields `{"prompt_tokens_details": {}}` and one that reported it as zero yields `{"prompt_tokens_details": {"cached_tokens": 0}}`, even though `usage.input_tokens_details.cached_tokens` is `0` in both cases.

Keep these boundaries in mind:

-   It is one snapshot per completed `ModelResponse`, on both streamed and non-streamed calls. It is never aggregated, and `result.context_wrapper.usage` is unaffected.
-   It does not ask the provider for usage. Streaming backends that need `ModelSettings(include_usage=True)` still need it.
-   It is `None` when the adapter received no usage payload, or when an upstream adapter already discarded field-presence information.
-   It is diagnostic metadata that `RunState` serialization does not persist, so it is unavailable after resuming a run from a serialized state.

## Per-request usage tracking

The SDK automatically tracks usage for each API request in `request_usage_entries`, useful for detailed cost calculation and monitoring context window consumption.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for i, request in enumerate(result.context_wrapper.usage.request_usage_entries):
    print(f"Request {i + 1}: {request.input_tokens} in, {request.output_tokens} out")
```

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
-   [`ModelSettings.preserve_raw_usage`][agents.model_settings.ModelSettings.preserve_raw_usage] - Opt in to raw provider usage snapshots
-   [`ModelResponse.raw_usage`][agents.items.ModelResponse.raw_usage] - The preserved provider usage payload
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - Access usage from run context
-   [`RunHooks`][agents.run.RunHooks] - Hook into usage tracking lifecycle
