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

Usage is aggregated across all model calls during the run (including tool calls and handoffs).

### Enabling usage with LiteLLM models

LiteLLM providers do not report usage metrics by default. When you are using [`LitellmModel`](models/litellm.md), pass `ModelSettings(include_usage=True)` to your agent so that LiteLLM responses populate `result.context_wrapper.usage`.

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

## Per-request usage tracking

The SDK automatically tracks usage for each API request in `request_usage_entries`, useful for detailed cost calculation and monitoring context window consumption.

```python
result = await Runner.run(agent, "What's the weather in Tokyo?")

for request in enumerate(result.context_wrapper.usage.request_usage_entries):
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

Note that while sessions preserve conversation context between runs, the usage metrics returned by each `Runner.run()` call represent only that particular execution. In sessions, previous messages may be re-fed as input to each run, which affects the input token count in consequent turns.

## Using usage in hooks

If you're using `RunHooks`, the `context` object passed to each hook contains `usage`. This lets you log usage at key lifecycle moments.

```python
class MyHooks(RunHooks):
    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        u = context.usage
        print(f"{agent.name} → {u.requests} requests, {u.total_tokens} total tokens")
```

## Modifying chat history in hooks

`RunContextWrapper` also exposes `message_history`, giving hooks a mutable view of the
conversation:

- `get_messages()` returns the full transcript (original input, model outputs, and pending
    injections) as a list of `ResponseInputItem` dictionaries.
- `add_message(agent=..., message=...)` queues custom messages (string, dict, or list of
    `ResponseInputItem`s). Pending messages are appended to the current LLM input immediately and are
    emitted as `InjectedInputItem`s in the run result or stream events.
- `override_next_turn(messages)` replaces the entire input for the upcoming LLM call. Use this to
    rewrite history after a guardrail or external reviewer intervenes.

```python
class BroadcastHooks(RunHooks):
        def __init__(self, reviewer_name: str):
                self.reviewer_name = reviewer_name

        async def on_llm_start(
                self,
                context: RunContextWrapper,
                agent: Agent,
                _instructions: str | None,
                _input_items: list[TResponseInputItem],
        ) -> None:
                context.message_history.add_message(
                        agent=agent,
                        message={
                                "role": "user",
                                "content": f"{self.reviewer_name}: Please cite the appendix before answering.",
                        },
                )
```

> **Note:** When running with `conversation_id` or `previous_response_id`, overrides are managed by
> the server-side conversation thread and `message_history.override_next_turn()` is disabled for
> that run.

## API Reference

For detailed API documentation, see:

-   [`Usage`][agents.usage.Usage] - Usage tracking data structure
-   [`RequestUsage`][agents.usage.RequestUsage] - Per-request usage details
-   [`RunContextWrapper`][agents.run.RunContextWrapper] - Access usage from run context
-   [`MessageHistory`][agents.run_context.MessageHistory] - Inspect or edit the conversation from hooks
-   [`RunHooks`][agents.run.RunHooks] - Hook into usage tracking lifecycle