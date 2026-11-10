# Native voice

This guide describes the current source branch. Use a matching source build, or examples from the exact preview release you installed. Do not assume that current branch APIs exist in an older wheel. These examples make live API requests.

## Prepare the SDK

Follow [Build the SDK from source](../../CONTRIBUTING.md#build-the-sdk-from-source) for the supported Python runtime, native build tools, and wheel installation. Use the wheel built from the same checkout as these examples. For the speech pipeline and the custom WebSocket adapter below, install that wheel with its `voice` extra. Replace the path in this command with the actual wheel under `dist/`:

```sh
python -m pip install '/absolute/path/to/your-built-wheel.whl[voice]'
```

The extra installs the speech and WebSocket dependencies. Set `OPENAI_API_KEY` for live requests. The recording examples do not open a microphone or speaker.

## Record a reply

Set `OPENAI_API_KEY`, then run from the matching source archive:

```sh
python examples/codex/native_realtime_voice.py 'Explain a rainbow briefly' answer.wav --seconds 30
```

The example opens `Session.realtime()`, sends one text prompt, and records mono PCM16 audio at 24 kHz. Its fixed window includes delegated work and may cut off a long answer. The deadline is an application limit, not a model completion signal. Disconnects and failures also end the recording.

Use `async with session.realtime(config=RealtimeConfig(...)) as voice` inside the Session's context. Exiting the voice context releases media and joins its delegated native turns. The caller's Session remains usable for text. Session cleanup also closes its voice owner. If `aclose()` fails, keep the owner and retry cleanup rather than dropping it.

If opening voice fails and its cleanup also fails, entering a new `session.realtime(...)` context retries the previous owner's cleanup first. A failed retry preserves that owner for another attempt; a successful retry allows the new voice connection. An already open or still opening voice connection continues to reject another owner.

After normal cleanup, `events()` yields one `RealtimeLifecycle("closed")` and ends. Failed connections raise their original error and discard buffered media.

## Choose a connection

Import the native types from `agents.realtime`.

| Path | Configuration and ownership |
| --- | --- |
| Server WebSocket | Omit `RealtimeConfig.transport`. Use `send_text()`, `send_audio()`, and `events()`. The application records and plays audio. |
| Browser WebRTC | Pass `RealtimeWebRTC(offer_sdp)` as the transport on your server. Return `await voice.answer_sdp()` after native opening succeeds. Keep the Session and native sideband open for the call. |
| SIP | Pass `RealtimeSIP(call_id)` for an already accepted provider call. Closing this owner detaches the sideband; your application still owns hanging up the existing call. |
| Application transport or model | Pass `adapter` in `RealtimeConfig`. It cannot be combined with a built-in call transport. |

Python native execution runs on the server. Your browser handles microphone access, SDP exchange, and playback, using an authenticated application endpoint. Keep long-lived credentials on the server. Browser observations are not permission to execute tools or approve requests. End server ownership when the call ends; closing a browser peer alone does not replace server cleanup.

SIP calls must be accepted and configured before attachment. On your server, call `await session.build_realtime_sip_config(config=RealtimeConfig(...))` to build the call-accept request body from the Session's active Agent. The builder uses the same instructions, tools, handoffs, and settings as voice setup. When `tools` is omitted, it prepares Session-owned MCP tools. It does not accept the call, open voice media, or resolve voice credentials. Send the returned body to the provider's call-accept endpoint, then attach the same Session with the same configuration and a `RealtimeSIP` transport. Regenerate the body if you change the Session or its configuration before accepting the call. The builder preserves supported SIP VAD tuning, including `threshold`, `prefix_padding_ms`, and `silence_duration_ms`.

Repeat the builder's `model`, `voice`, `instructions`, `settings`, and `tools` when attaching. These values describe the already accepted call; attachment checks them without sending an initial configuration update. A mismatch rejects attachment. For example, reuse one configuration inside your asynchronous incoming-call handler:

```python
from dataclasses import replace
from agents.realtime import RealtimeConfig, RealtimeSIP

config = RealtimeConfig(
    model="gpt-realtime",
    voice="alloy",
    instructions="Answer spoken questions briefly.",
    settings={"tool_choice": "auto"},
    tools=(),
)
acceptance = await session.build_realtime_sip_config(config=config)
# Send acceptance to the provider's call-accept endpoint before attaching.
async with session.realtime(config=replace(config, transport=RealtimeSIP(call_id))) as voice:
    await serve_call(voice)
```

Here, `session` is your open Session, `call_id` comes from the verified webhook, and `serve_call` consumes the voice events until the call ends. The empty tool selection excludes the Agent's tools. The accept body controls the tools advertised to the provider. The attachment selection also limits tools available to delegated native work. The builder saves no configuration for a later attachment.

Configure hosted MCP follow-ups on the `RealtimeConfig` used for attachment. The builder rejects `automatically_trigger_response_for_mcp_tool_calls=False`, because the HTTP accept body cannot carry this SDK option.

The [SIP example](native_realtime_sip.py) shows call acceptance, attachment, history retrieval, and hangup. It requires a call ID from an incoming-call webhook your application has verified. Nonempty client audio is rejected; a commit-only call can control audio already buffered by the provider. An SDK-created WebRTC call has different ownership: closing its native owner also hangs up that created call.

WebRTC SDP offers are limited to 1 MiB of UTF-8 text. Oversized offers are rejected before credentials are resolved or a connection is opened.

### Keep server control open

This server recipe publishes an SDP answer without closing its native owner. The application supplies an authenticated `publish_answer` callback and a `serve_call` callback. `serve_call` consumes `voice.events()`, routes any application decisions, and returns when the call ends. Keep this coroutine running after the answer has been sent.

```python
from collections.abc import Awaitable, Callable
from agents import Agent
from agents.realtime import RealtimeConfig, RealtimeSession, RealtimeWebRTC


async def serve_webrtc(
    offer_sdp: str,
    publish_answer: Callable[[str], Awaitable[None]],
    serve_call: Callable[[RealtimeSession], Awaitable[None]],
) -> None:
    agent = Agent(instructions="Answer spoken questions briefly.")
    async with agent.session() as session:
        async with session.realtime(
            config=RealtimeConfig(transport=RealtimeWebRTC(offer_sdp))
        ) as voice:
            await publish_answer(await voice.answer_sdp())
            await serve_call(voice)
```

For an already accepted SIP call, use the same lifetime and event-consumer pattern. Reuse the acceptance configuration and set its transport with `replace(config, transport=RealtimeSIP(call_id))`, as shown above. Ending that context detaches native control; hang up through the application that owns the provider call.

The retained Realtime runtime remains available under `agents.classic.realtime`. The native entrypoint uses the existing caller Session and canonical native history.

## Per-connection credentials and adapters

`RealtimeConfig.api_key` accepts a string or a zero-argument synchronous or asynchronous callback. It resolves once per connection after native source capture. Omit it to inherit the Session provider's credentials. This setting does not change text-turn credentials. Closing during lookup cancels the owned callback; asynchronous resolvers must cooperate with cancellation and clean partial resources.

`NativeRealtimeAdapter.connect(context)` is asynchronous. `RealtimeAdapterContext` supplies a private URL, headers, and model. You can choose another endpoint or translate another model's protocol to canonical Realtime V2 frames. Return a `NativeRealtimeConnection` with an `incoming` asynchronous iterator of strings, an async `send(frame)`, and an async `aclose()`.

Keep incoming and outgoing I/O independent. The factory must clean partial resources if it raises or is cancelled. Connection cleanup must release resources and wake pending sends and incoming iteration. A failed cleanup can be retried. Do not await the owning Session's close from an adapter callback; close the owner from your outer application code. Do not log headers or credential values.

Run the complete [custom adapter example](native_realtime_adapter.py) from the same checkout:

```sh
python examples/codex/native_realtime_adapter.py 'Explain a rainbow briefly' adapter-answer.wav
```

It implements the factory, incoming iterator, send, and cleanup methods with the `websockets` dependency from the `voice` extra. It forwards canonical frames unchanged to the native-selected endpoint and records a fixed window of PCM16 audio. It does not translate another model's protocol.

Call `update()`, `approve_hosted_mcp()`, `delete_conversation_item()`, `send_event()`, and `send_audio(..., commit=True)` from application code outside this owner's adapter callbacks. They need the same ordered transport and reject calls from its active callbacks. An adapter callback can submit nested text when no committed audio call is pending. Once a committed audio call is waiting or running, callbacks also reject nested input behind it. This includes a commit waiting to acquire the input lock.

The native runtime still checks configuration acknowledgements, tools, ordering, and history. A translating adapter must implement the applicable [Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events) and [server events](https://developers.openai.com/api/reference/resources/realtime/server-events), including configuration acknowledgements and actual response/item IDs. Replacing I/O does not bypass these checks.

## Send images and control turn boundaries

`send_text(text)` keeps its existing behavior: append user text and request a response. Use `send_message(..., trigger_response=False)` to add text or images without requesting a response. `request_response()` requests one later. It does not commit buffered audio.

Text and image URLs share a 1 MiB UTF-8 limit per message, with at most 1,024 content parts. Across active and waiting input calls, each voice owner allows 128 operations and 8 MiB of input payload. Excess input raises `ValueError` before it joins the queue. Await input calls and split larger audio inputs into smaller chunks; capacity becomes available as calls finish or are cancelled.

For manual audio boundaries, call `await voice.update({"turn_detection": None})` inside the open voice context. Append audio with `await voice.send_audio(pcm_bytes)`, then commit the buffered input with `await voice.send_audio(b"", commit=True)`. Alternatively, `await voice.send_audio(pcm_bytes, commit=True)` sends those bytes and commits once after their final frame. Then call `await voice.request_response()` to generate an answer. Commit and response creation are separate operations. Calls with `commit=False` finish at native admission; they do not promise that a following text call waits for those audio frames to reach the provider.

Save the following script as `describe_image.py`. It sends a local PNG, prints a text answer, and deletes the completed user item from the current provider conversation when its ID is available. Run it with the matching SDK build and `OPENAI_API_KEY` set:

```sh
python describe_image.py /path/to/image.png
```

```python
import asyncio
import base64
import sys
from pathlib import Path

from agents import Agent
from agents.realtime import (
    RealtimeConfig,
    RealtimeConversationItemObserved,
    RealtimeResponse,
    RealtimeText,
)


async def main(image_path: str) -> None:
    image = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    async with Agent(instructions="Describe the image briefly.").session() as session:
        async with session.realtime(config=RealtimeConfig()) as voice:
            await voice.update({"output_modality": "text", "tool_choice": "none"})
            await voice.send_message(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is in this image?"},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{image}"},
                    ],
                },
                trigger_response=False,
            )
            await voice.request_response()
            provider_item_id: str | None = None
            async for event in voice.events():
                if (
                    isinstance(event, RealtimeConversationItemObserved)
                    and event.role == "user"
                    and event.status == "done"
                ):
                    provider_item_id = event.item_id
                if isinstance(event, RealtimeText) and event.role == "assistant" and event.final:
                    print(event.text)
                if isinstance(event, RealtimeResponse) and event.status == "done":
                    break
            if provider_item_id is not None:
                await voice.delete_conversation_item(provider_item_id)


asyncio.run(main(sys.argv[1]))
```

### Send provider-wire events

Inside an open voice context, `await voice.send_event(event)` sends a supported provider-wire event through the native owner. Core validates the event and preserves native tool and approval authority. This method accepts a JSON-compatible mapping, captures it before waiting, and shares the connection's 128-operation/8 MiB input budget. It rejects calls from the same owner's adapter callbacks. Cancellation waits for native acceptance to settle.

For example, use an open `voice` owner to request a response with provider metadata and an event ID:

```python
await voice.send_event(
    {
        "type": "response.create",
        "event_id": "application-response-1",
        "response": {
            "output_modalities": ["text"],
            "instructions": "Reply briefly.",
            "metadata": {"request": "application-request-1"},
        },
    }
)
```

Core accepts these event names with guarded payloads:

- `session.update` accepts existing sparse session settings and waits for the provider acknowledgement. Raw tool declarations remain unsupported; use the Session's admitted tools.
- `response.create` accepts instructions, output modality, output-token limits, parallel tool calls, reasoning, metadata, and an event ID. Alternate input and out-of-band responses remain unsupported. Admission and coalescing match `request_response()`.
- `response.cancel` retires native work and local buffered output for the exact accepted response IDs. Consume `RealtimePlaybackInterrupted` and discard the indicated unplayed application audio, as with `interrupt()`.
- `conversation.item.create` accepts supported message rows, an optional item ID, and a known `previous_item_id`. Native validation rejects fabricated function, MCP, and approval rows. Creation waits for the matching provider acknowledgement.
- `conversation.item.retrieve` returns the full provider item as a dictionary. `conversation.item.delete` waits for the matching provider acknowledgement.
- `conversation.item.truncate` requires known received assistant audio and a valid playback offset.
- `input_audio_buffer.append`, `input_audio_buffer.clear`, and `input_audio_buffer.commit` use the native media queue. Audio appends use base64 data in the configured input format.
- `output_audio_buffer.clear` applies only to provider-managed media.

Every event can carry an `event_id`. Sends other than item retrieval return `None`. The existing message, response, audio, and history methods remain available for their typed operations.

### Edit provider history

`delete_conversation_item(provider_item_id)` deletes an eligible item from the current provider conversation. Use the provider ID from `RealtimeConversationItemObserved`, whose `status` is `"added"` or `"done"`. Canonical history IDs are separate. Native checks reject unknown or incomplete items and items still needed by unfinished work. The call completes after the matching provider acknowledgement and durable history update; `RealtimeConversationItemDeleted` reports that deletion.

Canonical history keeps prior transcripts and appends a `provider_item_deleted` entry with its target in `provider_item_id`. Shared Session history, tool outcomes, and approval records remain intact. Deletion cannot retract output already generated or change context a response already consumed. A later voice connection can use retained Session context. This operation prunes the current provider conversation rather than erasing Session history.

Use `await voice.retrieve_conversation_item(provider_item_id)` to retrieve a known provider item with its available media, including an item from before SIP attachment. Retrieval rejects IDs still recorded as deleted. Retrieving an ID absent from the observed history does not add it to that history's order. `await voice.retrieve_conversation_history()` retrieves full items for IDs observed during this connection, in observed order. The result can include audio and image data and can be large. It cannot discover items from before SIP attachment. These requests are separate from the synchronous `voice.history` audit view.

Pass an edited list of those provider items to `await voice.update_history(items)` to update the observed portion of the provider conversation. The method supports adding, correcting, removing, and reordering supported system, user, and assistant messages. Keep surviving tool, MCP, and approval items byte-for-byte unchanged and in their relative order. You can remove them only after their native work has settled. Supported messages can move across retained items. Removing a settled item prunes provider context; it does not retract execution or approval authority. Native validation checks the replacement before sending changes and waits for provider acknowledgements. It cannot erase unobserved items or rewrite the Session's audit, background work, or retained context. An interrupted edit or a provider failure after some acknowledgements can leave partial edits. If the connection remains active, retrieve the observed history before retrying.

The connection retains 128 recent deletion receipts. Within this window, you can restore a message removed by an interrupted `update_history()` under its original ID. Supply a supported message after refreshing the observed history. IDs deleted directly with `delete_conversation_item()` or by another controller, and tool or control identities, remain protected while their receipts are retained. Recovery preserves the canonical deletion audit. Deletions observed from another controller also remove items from observed provider history. They do not cancel or approve pending native work.

Once the receipt window evicts any deletion, individual item lookup requires a current live provider observation. An arbitrary or unobserved ID, including one from before SIP attachment, then rejects locally. This restriction lasts until a new voice connection. Further pruning and history editing remain available.

Created or changed messages use the provider's role-specific content types: `input_text` for system messages; `input_text`, `input_audio`, or `input_image` for user messages; and `output_text` for assistant messages. For images, pass a PNG or JPEG base64 data URI. New or recreated images must be no larger than 4,096 pixels per side. Validation sets a 64 MiB decoder allocation allowance; decoder overhead can use more memory. Ordinary 3,840×2,160 RGBA8 images fit these decoding limits if their encoded payload fits the input budget. Existing image items can remain unchanged. Validation and recreation share a 64 MiB budget for decoded pixel data across all rows and image parts; repeated checks count again. Exceeding this budget rejects the whole edit before writes. Base64-encode audio bytes in the session's input audio format. Existing assistant audio can be retained but cannot be created through this API. Keep existing provider item IDs and give new messages unique IDs.

Edits preserve known provider predecessors. An edit that needs an unknown insertion position fails before sending changes. If provider events leave the order incomplete, ordered history retrieval and replacement are unavailable. You can still retrieve a known item by ID. Adding items to an empty observed history appends them to the provider conversation.

Each history operation has an 8 MiB budget for estimated retained JSON storage, including string and container overhead. An update counts its requested replacement and retrieved provider items together. Exceeding this budget rejects the operation before writes and leaves the call active. Queued replacements also count toward the connection's input queue limits.

Validation failures and interruptions caused by concurrent input or responses leave the connection active. A timeout or malformed reply while reading history also leaves the connection active. New history requests stay blocked until a valid matching reply for the outstanding request arrives. If it never arrives, history requests remain unavailable for that connection. Provider errors, transport failures, and timeouts waiting for a write acknowledgement close the native voice connection. A closed connection cannot retrieve history.

Each voice connection retains at most 8 MiB of estimated canonical history. Long, fragmented transcripts can also exhaust a cumulative limit on the work spent extending text snapshots. History and its work accounting last for the connection, regardless of how quickly events are consumed. Guardrail transcripts have a separate work limit shared across retained guardrail items. Exceeding these limits fails the voice connection.

## Settings, tools, and active Agents

`RealtimeConfig.settings` uses `NativeRealtimeSettings` fields such as `input_format`, `output_format`, `transcription`, `turn_detection`, `output_voice`, and `hosted_mcp_tools`. `voice.update()` accepts sparse changes. Omitted fields keep their values. PCM16 uses mono 24 kHz samples; G.711 uses mono 8 kHz bytes. A codec setting does not convert your samples. `RealtimeAudioInput` labels encoded input, and `RealtimeAudio` carries the accepted output format and item identity.

Set `settings={"tool_choice": "auto"}` to let the model choose tools, or select `"none"`, `"required"`, or a named function such as `{"type": "function", "name": "weather"}`. A named function must already be advertised by the active selection. Before an update or handoff removes it, reset the choice with `await voice.update({"tool_choice": "auto"})`. Tool choice does not bypass native policies or approvals.

Omit `RealtimeConfig.tools` to inherit the captured active Agent's function catalog and handoffs. An explicit tuple contains original admitted function objects. An empty tuple excludes the Agent's tools and handoffs from direct calls and delegated backend turns. The reserved `background_agent` and `remain_silent` controls remain available; delegation still obeys the empty backend tool selection. Native policies and application approvals still apply. `background_agent` delegates to the same Session and returns only accepted final string output. Output validation and repair finish before that text is returned. Non-string output fails rather than being serialized for speech.

Declare reachable `Agent.handoffs` before opening the Session. `await session.get_active_agent()` returns the original Agent selected by native state. An idle `session.update(agent=...)` activates a captured Agent and applies its declaration defaults. Idle tool updates change active defaults. Explicit per-operation tools remain a ceiling across handoffs; omitted tools inherit the destination defaults. Graph Agents must share compatible output formats and Session runtime grants. Restore binds fresh caller-supplied objects, not saved callback implementations.

A tool can return `background_result(value)` to publish an accepted result without requesting a model follow-up. Output policies can replace that decision. Provider-hosted MCP tools are separate from native MCP tools. Configure their URL or connector ID in `hosted_mcp_tools`. Pass the exact issued `RealtimeHostedMcpApprovalRequested` object to `await voice.approve_hosted_mcp(request, approve=False)` to deny it, or set `approve=True` after your application approves. Cancellation waits for the admitted decision to settle; an accepted decision cannot be replayed. Clones and cross-session objects are invalid. `automatically_trigger_response_for_mcp_tool_calls` defaults to true and controls hosted MCP follow-ups.

Hosted MCP catalogs are limited to 1,024 tools. Catalog events share an 8 MiB queue budget, including estimated storage for schema structures and strings. Oversized catalogs, deeply nested schemas, or a full queue terminate the voice connection. Keep server tool lists and schemas small, and consume events promptly to release queue capacity.

The native incoming queue also shares an 8 MiB budget across wire text and conservative estimates of decoded structures and derived events. A compact frame with too many JSON values can exceed this budget. Oversized or deeply nested frames terminate the connection before further event conversion. Keep frames small and consume events promptly. This is a queue allocation policy, not a limit on total process memory.

Realtime speech streams before completion. `output_guardrails` can check text and interrupt the exact response, but some audio may already have played. Report actual playback through `report_playback()`. Discard locally queued audio for the IDs in `RealtimePlaybackInterrupted.retired_response_ids`. This differs from accepted-final-text validation on backend delegation.

Report playback progress while audio plays. When playback bookkeeping reaches its limit, it can release older completed content that has finished playing or was interrupted. Playback reports for released content are no longer accepted.

## Transcribe, run, and synthesize

[voice.py](voice.py) reads a WAV file, runs a native Session, and writes spoken output. Install the matching SDK's `voice` extra as shown above. Supply a mono PCM16 WAV file at 24 kHz, then run:

```sh
python examples/codex/voice.py input.wav spoken-answer.wav
```

`agents.voice` exposes `VoicePipeline`, speech model settings, streamed audio input, and `NativeSessionVoiceWorkflow`.

Speech providers and audio delivery reuse the retained speech engine; the workflow executes native turns. It preserves Session history, tools, output validation, and approvals. Synthesis starts only after the native turn accepts final text. Use a custom workflow to render accepted structured output. The application owns audio input, output playback, stream consumption, and the Session lifetime.

Pass `NativeSessionVoiceWorkflow(session, tools=[lookup])` to limit every transcription to selected tools. The workflow captures the sequence when constructed. Omit `tools` to use the Session's current defaults, or pass `tools=[]` to exclude all tools. Selected opening tools use their current acknowledged replacements. Submission fails if a different active Agent does not admit the selection. Realtime background delegation carries the Realtime owner's captured selection into the workflow, including an explicit empty selection.
