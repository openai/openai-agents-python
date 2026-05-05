# Realtime agents guide

This guide explains how the OpenAI Agents SDK's realtime layer maps onto the OpenAI Realtime API, and what extra behavior the Python SDK adds on top.

!!! warning "Beta feature"

    Realtime agents are in beta. Expect some breaking changes as we improve the implementation.

!!! note "Start here"

    If you want the default Python path, read the [quickstart](quickstart.md) first. If you are deciding whether your app should use server-side WebSocket or SIP, read [Realtime transport](transport.md). Browser WebRTC transport is not part of the Python SDK.

## Overview

Realtime agents keep a long-lived connection open to the Realtime API so the model can process text and audio incrementally, stream audio output, call tools, and handle interruptions without restarting a fresh request on every turn.

The main SDK components are:

-   **RealtimeAgent**: Instructions, tools, output guardrails, and handoffs for one realtime specialist
-   **RealtimeRunner**: Session factory that wires a starting agent to a realtime transport
-   **RealtimeSession**: A live session that sends input, receives events, tracks history, and executes tools
-   **RealtimeModel**: The transport abstraction. The default is OpenAI's server-side WebSocket implementation.

## Session lifecycle

A typical realtime session looks like this:

1. Create one or more `RealtimeAgent`s.
2. Create a `RealtimeRunner` with the starting agent.
3. Call `await runner.run()` to get a `RealtimeSession`.
4. Enter the session with `async with session:` or `await session.enter()`.
5. Send user input with `send_message()` or `send_audio()`.
6. Iterate over session events until the conversation ends.

Unlike text-only runs, `runner.run()` does not produce a final result immediately. It returns a live session object that keeps local history, background tool execution, guardrail state, and the active agent configuration in sync with the transport layer.

By default, `RealtimeRunner` uses `OpenAIRealtimeWebSocketModel`, so the default Python path is a server-side WebSocket connection to the Realtime API. If you pass a different `RealtimeModel`, the same session lifecycle and agent features still apply, while the connection mechanics can change.

## Agent and session configuration

`RealtimeAgent` is intentionally narrower than the regular `Agent` type:

-   Model choice is configured at the session level, not per agent.
-   Structured outputs are not supported.
-   Voice can be configured, but it cannot change after the session has already produced spoken audio.
-   Instructions, function tools, handoffs, hooks, and output guardrails all still work.

`RealtimeSessionModelSettings` supports both a newer nested `audio` config and older flat aliases. Prefer the nested shape for new code, and start with `gpt-realtime-1.5` for new realtime agents:

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-1.5",
            "audio": {
                "input": {
                    "format": "pcm16",
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
                },
                "output": {"format": "pcm16", "voice": "ash"},
            },
            "tool_choice": "auto",
        }
    },
)
```

Useful session-level settings include:

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

Useful run-level settings on `RealtimeRunner(config=...)` include:

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

See [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] and [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] for the full typed surface.

## Inputs and outputs

### Text and structured user messages

Use [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] for plain text or structured realtime messages.

```python
from agents.realtime import RealtimeUserInputMessage

await session.send_message("Summarize what we discussed so far.")

message: RealtimeUserInputMessage = {
    "type": "message",
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Describe this image."},
        {"type": "input_image", "image_url": image_data_url, "detail": "high"},
    ],
}
await session.send_message(message)
```

Structured messages are the main way to include image input in a realtime conversation. The example web demo in [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) forwards `input_image` messages this way.

### Audio input

Use [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] to stream raw audio bytes:

```python
await session.send_audio(audio_bytes)
```

If server-side turn detection is disabled, you are responsible for marking turn boundaries. The high-level convenience is:

```python
await session.send_audio(audio_bytes, commit=True)
```

If you need lower-level control, you can also send raw client events such as `input_audio_buffer.commit` through the underlying model transport.

### Manual response control

`session.send_message()` sends user input using the high-level path and starts a response for you. Raw audio buffering does **not** automatically do the same in every configuration.

At the Realtime API level, manual turn control means clearing `turn_detection` with a raw `session.update`, then sending `input_audio_buffer.commit` and `response.create` yourself.

If you are managing turns manually, you can send raw client events through the model transport:

```python
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

await session.model.send_event(
    RealtimeModelSendRawMessage(
        message={
            "type": "response.create",
        }
    )
)
```

This pattern is useful when:

-   `turn_detection` is disabled and you want to decide when the model should respond
-   you want to inspect or gate user input before triggering a response
-   you need a custom prompt for an out-of-band response

The SIP example in [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) uses a raw `response.create` to force an opening greeting.

## Events, history, and interruptions

`RealtimeSession` emits higher-level SDK events while still forwarding raw model events when you need them.

High-value session events include:

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

The most useful events for UI state are usually `history_added` and `history_updated`. They expose the session's local history as `RealtimeItem` objects, including user messages, assistant messages, and tool calls.

### Interruptions and playback tracking

When the user interrupts the assistant, the session emits `audio_interrupted` and updates history so the server-side conversation stays aligned with what the user actually heard.

In low-latency local playback, the default playback tracker is often enough. In remote or delayed playback scenarios, especially telephony, use [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] so interruption truncation is based on actual playback progress rather than assuming all generated audio has already been heard.

The Twilio example in [`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) shows this pattern.

## Tools, approvals, handoffs, and guardrails

### Function tools

Realtime agents support function tools during live conversations:

```python
from agents import function_tool


@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny, 72F."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You can answer weather questions.",
    tools=[get_weather],
)
```

### Tool approvals

Function tools can require human approval before execution. When that happens, the session emits `tool_approval_required` and pauses the tool run until you call `approve_tool_call()` or `reject_tool_call()`.

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

For a concrete server-side approval loop, see [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py). The human-in-the-loop docs also point back to this flow in [Human in the loop](../human_in_the_loop.md).

### Handoffs

Realtime handoffs let one agent transfer the live conversation to another specialist:

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[realtime_handoff(billing_agent, tool_description="Transfer to billing support")],
)
```

Bare `RealtimeAgent` handoffs are auto-wrapped, and `realtime_handoff(...)` lets you customize names, descriptions, validation, callbacks, and availability. Realtime handoffs do **not** support the regular handoff `input_filter`.

### Guardrails

Only output guardrails are supported for realtime agents. They run on debounced transcript accumulation rather than on every partial token, and they emit `guardrail_tripped` instead of raising an exception.

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail


def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )


agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

## SIP and telephony

The Python SDK includes a first-class SIP attach flow via [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel].

Use it when a call arrives through the Realtime Calls API and you want to attach an agent session to the resulting `call_id`:

```python
from agents.realtime import RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(starting_agent=agent, model=OpenAIRealtimeSIPModel())

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
    }
) as session:
    async for event in session:
        ...
```

If you need to accept the call first and want the accept payload to match the agent-derived session configuration, use `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`. The complete flow is shown in [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py).

## Low-level access and custom endpoints

You can access the underlying transport object through `session.model`.

Use this when you need:

-   custom listeners via `session.model.add_listener(...)`
-   raw client events such as `response.create` or `session.update`
-   custom `url`, `headers`, or `api_key` handling through `model_config`
-   `call_id` attach to an existing realtime call

`RealtimeModelConfig` supports:

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

This repository's shipped `call_id` example is SIP. The broader Realtime API also uses `call_id` for some server-side control flows, but those are not packaged as Python examples here.

When connecting to Azure OpenAI, pass a GA Realtime endpoint URL and explicit headers. For example:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

For token-based authentication, use a bearer token in `headers`:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

If you pass `headers`, the SDK does not add `Authorization` automatically. Avoid the legacy beta path (`/openai/realtime?api-version=...`) with realtime agents.

## Further reading

-   [Realtime transport](transport.md)
-   [Quickstart](quickstart.md)
-   [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)
