# Realtime agents guide

This guide explains how the OpenAI Agents SDK's realtime layer maps onto the OpenAI Realtime API, and what extra behavior the Python SDK adds on top.

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

When the Realtime API server closes the default WebSocket connection normally, the model transport emits a `disconnected` [`RealtimeModelConnectionStatusEvent`][agents.realtime.model_events.RealtimeModelConnectionStatusEvent] followed by a [`RealtimeModelEndOfStreamEvent`][agents.realtime.model_events.RealtimeModelEndOfStreamEvent]. `RealtimeSession` forwards both inside `raw_model_event`, drains events that are already queued, and then ends asynchronous iteration without raising an exception. A caller-initiated `session.close()` does not synthesize these server-disconnect events. Unexpected WebSocket failures continue through the session's exception path instead of ending iteration as a normal server close.

## Agent and session configuration

`RealtimeAgent` is intentionally narrower than the regular `Agent` type:

-   Model choice is configured at the session level, not per agent.
-   Structured outputs are not supported.
-   Voice can be configured, but it cannot change after the session has already produced spoken audio.
-   Instructions, function tools, handoffs, hooks, and output guardrails all still work.

`RealtimeSessionModelSettings` supports both a newer nested `audio` config and older flat aliases. Prefer the nested shape for new code, and start with `gpt-realtime-2.1` for new realtime agents:

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-2.1",
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

### Input transcription settings

Configure input transcription under `audio.input.transcription`. Use `gpt-live-transcribe` for low-latency incremental transcripts, or use `gpt-transcribe` over WebSocket when transcription should begin after an audio turn is committed or when your application needs detected-language output. The Agents SDK forwards the model-specific GA transcription settings in the nested session configuration:

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-live-transcribe",
                        "prompt": "A support call about the OpenAI Agents SDK.",
                        "keywords": ["RunState", "MCPServerManager"],
                        "languages": ["en", "ja"],
                    },
                    "turn_detection": None,
                }
            }
        }
    },
)
```

For `gpt-live-transcribe`, `prompt` provides free-form recording context, `keywords` lists literal terms that may occur in the audio, and `languages` lists expected input languages. This model uses plural `languages` instead of singular `language`; do not send both fields.

The OpenAI client version pinned by this SDK supports `delay` only with `gpt-realtime-whisper`. Configure that model's latency and accuracy tradeoff as follows:

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-realtime-whisper",
                        "delay": "low",
                    },
                    "turn_detection": None,
                }
            }
        }
    },
)
```

The `delay` setting accepts `minimal`, `low`, `medium`, `high`, or `xhigh`. Lower values can produce earlier partial text, while higher values give the transcription model more audio context and can improve recognition accuracy. Benchmark representative audio instead of assuming fixed timing for any level.

Use `gpt-transcribe` in a Realtime session over WebSocket only when transcription should begin after a committed audio turn or the application needs detected-language output. The model automatically uses earlier transcribed turns as context. The `gpt-transcribe` completion event reports detected languages in its `languages` output field. This output field is different from the `gpt-live-transcribe` expected-language input shown above.

Setting `audio.input.turn_detection` to `None` disables automatic turn detection. The application must then commit audio turns and control response creation as described in [Manual response control](#manual-response-control). See the OpenAI API [Realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription) for model behavior, validation rules, and latency guidance.

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

If you need lower-level control, you can also send Realtime API client events such as `input_audio_buffer.commit` directly through the underlying model transport.

### Manual response control

`session.send_message()` sends user input using the high-level path and starts a response for you. In some configurations, raw audio buffering does **not** automatically do the same.

At the Realtime API level, manual turn control means sending a `session.update` event that sets `turn_detection` to `null`, then sending `input_audio_buffer.commit` and `response.create` yourself.

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

### Usage accounting

When a completed model response includes usage, the SDK's OpenAI `RealtimeModel` transport emits a [`RealtimeModelUsageEvent`][agents.realtime.model_events.RealtimeModelUsageEvent] inside a `raw_model_event`. Its `usage` field contains the token counts for that response, while `input_tokens_details` and `output_tokens_details` provide optional modality breakdowns.

The session also adds each response's usage to the shared [`RunContextWrapper.usage`][agents.run_context.RunContextWrapper.usage]. Read it from `event.info.context.usage` on a subsequent high-level event such as `agent_end` to inspect cumulative usage for the live session.

```python
from agents.realtime import RealtimeModelUsageEvent

async for event in session:
    if event.type == "raw_model_event" and isinstance(
        event.data, RealtimeModelUsageEvent
    ):
        response_usage = event.data.usage
        print("Response tokens:", response_usage.total_tokens)
        print("Input modalities:", event.data.input_tokens_details)
        print("Output modalities:", event.data.output_tokens_details)
    elif event.type == "agent_end":
        session_usage = event.info.context.usage
        print("Session tokens:", session_usage.total_tokens)
```

Usage is reported only when the model provider includes it in the completed response. The cumulative value covers responses received by that `RealtimeSession`; it is not a cross-session total.

### Interruptions and playback tracking

When the user interrupts the assistant, the session emits `audio_interrupted` and updates history so the server-side conversation stays aligned with what the user actually heard.

In low-latency local playback, the default playback tracker is often enough. In remote or delayed playback scenarios, especially telephony, use [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] so the interrupted response is truncated at the actual playback position rather than assuming all generated audio has already been heard.

The Twilio example in [`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) shows this pattern.

## Tools, approvals, handoffs, and guardrails

### Function tools

Realtime agents support function tools during live conversations:

```python
from agents.decorators import tool


@tool
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

If the tool also has input guardrails, those guardrails run immediately before execution after approval. To run them before the approval event is emitted, create the runner with `RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})`. Calls that pass this pre-approval check are still checked again after approval before execution.

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
    handoffs=[
        realtime_handoff(
            billing_agent,
            tool_description_override="Transfer to billing support",
        )
    ],
)
```

`RealtimeAgent` objects used directly as handoffs are auto-wrapped, and `realtime_handoff(...)` lets you customize names, descriptions, validation, callbacks, and availability. Realtime handoffs do **not** support the regular handoff `input_filter`.

### Guardrails

Realtime agents support output guardrails on agent responses and input guardrails on function-tool calls. Output guardrail checks are debounced: each check runs on accumulated output-text and audio-transcript deltas rather than on every partial delta, and emits `guardrail_tripped` instead of raising an exception. A single delta schedules at most one check. If that delta crosses multiple `debounce_text_length` boundaries, the SDK advances the next boundary past all of them instead of scheduling catch-up checks after later small deltas.

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

When a realtime output guardrail trips on an audio transcript, the session interrupts the active response, forces `response.cancel`, emits `guardrail_tripped`, and sends a follow-up user message that names the triggered guardrail so the model can produce a replacement response. Your audio player should still listen for `audio_interrupted` and stop local playback immediately, because some audio may already be buffered when the tripwire fires. With the built-in OpenAI Realtime transports, if the guardrail check finishes after the response it is checking has ended, the session interrupts only that response's buffered playback and does not cancel any response that started later. For text-only output, the session instead sends a response-scoped `response.cancel`; it does not emit `audio_interrupted` because there is no audio playback to stop. The same `guardrail_tripped` event and follow-up user message are emitted for the text-only path when using the built-in OpenAI Realtime models.

Custom `RealtimeModel` transports must honor `RealtimeModelSendInterrupt.response_id` and `playback_only` to provide the same source-scoped audio interruption behavior. They must also override `RealtimeModel.send_event_if()` to support the recovery message for the text-only output path. The implementation must either recheck the supplied condition at the transport's actual event commit boundary or serialize the condition check together with the event commit. The default implementation safely skips the recovery message because, if it checked the condition once and then sent the event separately, another response could start between that check and the event commit; response cancellation and the `guardrail_tripped` event still occur.

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
