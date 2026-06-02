# Realtime transport

Use this page to decide how realtime agents fit into your Python application.

!!! note "Python SDK boundary"

    The Python SDK does **not** include a browser WebRTC transport. This page is only about Python SDK transport choices: server-side WebSockets and SIP attach flows. Browser WebRTC is a separate platform topic, documented in the official [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/) guide.

## Decision guide

| Goal | Start with | Why |
| --- | --- | --- |
| Build a server-managed realtime app | [Quickstart](quickstart.md) | The default Python path is a server-side WebSocket session managed by `RealtimeRunner`. |
| Understand which transport and deployment shape to choose | This page | Use this before you commit to a transport or deployment shape. |
| Attach agents to phone or SIP calls | [Realtime guide](guide.md) and [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | The repo ships a SIP attach flow driven by `call_id`. |

## Server-side WebSocket is the default Python path

`RealtimeRunner` uses `OpenAIRealtimeWebSocketModel` unless you pass a custom `RealtimeModel`.

That means the standard Python topology looks like this:

1. Your Python service creates a `RealtimeRunner`.
2. `await runner.run()` returns a `RealtimeSession`.
3. Enter the session and send text, structured messages, or audio.
4. Consume `RealtimeSessionEvent` items and forward audio or transcripts to your application.

This is the topology used by the core demo app, the CLI example, and the Twilio Media Streams example:

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

Use this path when your server owns the audio pipeline, tool execution, approval flow, and history handling.

## SIP attach is the telephony path

For the telephony flow documented in this repository, the Python SDK attaches to an existing realtime call via `call_id`.

This topology looks like:

1. OpenAI sends your service a webhook such as `realtime.call.incoming`.
2. Your service accepts the call through the Realtime Calls API.
3. Your Python service starts a `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`.
4. The session connects with `model_config={"call_id": ...}` and then processes events like any other realtime session.

This is the topology shown in [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip).

The broader Realtime API also uses `call_id` for some server-side control patterns, but this repository's shipped attach example is SIP.

## Browser WebRTC is outside this SDK

If your app's primary client is a browser using Realtime WebRTC:

-   Treat it as outside the scope of the Python SDK docs in this repository.
-   Use the official [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/) and [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/) docs for the client-side flow and event model.
-   Use the official [Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/) guide if you need a sideband server connection on top of a browser WebRTC client.
-   Do not expect this repository to provide a browser-side `RTCPeerConnection` abstraction or a ready-made browser WebRTC sample.

This repository also does not currently ship a browser WebRTC plus Python sideband example.

## Custom endpoints and attach points

The transport configuration surface in [`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] lets you adapt the default paths:

-   `url`: Override the WebSocket endpoint
-   `headers`: Provide explicit headers such as Azure auth headers
-   `api_key`: Pass an API key directly or via callback
-   `call_id`: Attach to an existing realtime call. In this repository, the documented example is SIP.
-   `playback_tracker`: Report actual playback progress for interruption handling

See the [Realtime agents guide](guide.md) for the detailed lifecycle and capability surface once you've chosen a topology.
