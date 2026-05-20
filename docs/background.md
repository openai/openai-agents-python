# Background mode

OpenAI's [Responses API background mode](https://platform.openai.com/docs/guides/background) lets long-running model calls survive client disconnects: the server keeps processing the request and you poll it to completion. This matters for reasoning-heavy single turns (`gpt-5.2-pro`, deep-research models) that can take minutes and otherwise fall foul of HTTP timeouts on Vercel, Cloudflare Workers, corporate proxies, etc.

The Agents SDK exposes background mode via two new fields on [`ModelSettings`][agents.model_settings.ModelSettings]:

- `background: bool | None` — opt in to background mode.
- `background_poll_interval_seconds: float | None` — optional fixed poll interval. When unset, the SDK honors the `openai-poll-after-ms` response header and falls back to 1.0 second.

## Transparent use through `Runner`

Set the flag on your agent's `ModelSettings` and run as usual. The SDK submits with `background=True`, polls `client.responses.retrieve(id)` adaptively, and returns the terminal response — `Runner.run` and `Runner.run_streamed` need no other changes.

```python
from agents import Agent, ModelSettings, Runner

agent = Agent(
    name="reasoner",
    model="gpt-5.2-pro",
    model_settings=ModelSettings(background=True),
)
result = await Runner.run(agent, "Plan a multi-stage research workflow.")
print(result.final_output)
```

For streaming, `background=True` is passed through to `responses.create(stream=True, background=True)` so the server keeps generating across client disconnects. Client-side auto-resume via `starting_after` is intentionally not part of this MVP — plain `openai-python` doesn't auto-resume either.

```python
async for event in Runner.run_streamed(agent, "Stream me a long answer").stream_events():
    print(event)
```

## Retrieving a response by id

If you captured a `response_id` and want to fetch the latest server state from a different process or worker, call `client.responses.retrieve(response_id)` on the underlying `AsyncOpenAI` client directly — there is no SDK-specific wrapper, deliberately, because that would only add API surface without adding capability.

```python
from openai import AsyncOpenAI

client = AsyncOpenAI()
response = await client.responses.retrieve(response_id)
print(response.status)
```

## Cancellation

If the surrounding task is cancelled (`asyncio.CancelledError`) while the SDK is polling, the SDK schedules a best-effort `client.responses.cancel(response_id)` so the in-flight server-side response is not leaked. The `CancelledError` then propagates to the caller as usual.

## Compatibility

Background mode is **supported only by the HTTP Responses transport** ([`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]). Setting `background=True` on either of these adapters raises [`UserError`][agents.exceptions.UserError] so the durability guarantee you opted into is not silently demoted:

- [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] — the WebSocket transport always streams and cannot decouple submit from poll.
- [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] — the Chat Completions API has no `background` parameter.

If you're on a non-OpenAI provider via LiteLLM / AnyLLM, the field is read on `ModelSettings` but not plumbed by those adapters; whether it does anything depends on the underlying provider.

## Limits

- Background responses are retained server-side for **about 10 minutes**.
- Background mode is **not ZDR-compatible**.
- The `Runner` does not impose its own deadline on a background poll. If you need a hard ceiling, wrap your call (e.g. `asyncio.wait_for(Runner.run(agent, ...), timeout=600)`); on timeout, the SDK's cancel-on-CancelledError logic still fires.
