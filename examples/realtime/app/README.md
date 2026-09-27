# Realtime Demo App

A web-based realtime voice assistant demo with a FastAPI backend and HTML/JS frontend.

## Installation

Install the required dependencies:

```bash
uv add fastapi uvicorn websockets
```

## Usage

Start the application with a single command:

```bash
cd examples/realtime/app && uv run python server.py
```

Then open your browser to: http://localhost:8000

### Local-use boundary and limits

Run this demo only on your own trusted machine. The launch command binds to
`127.0.0.1`. WebSocket connections require a `localhost` or `127.0.0.1` Host and a
matching HTTP Origin, so open the page from this server. Missing or unrelated
browser origins are rejected before the server opens an OpenAI session.

These checks do not authenticate local processes, which can forge HTTP headers.
Do not expose this demo through a public bind address, proxy, or tunnel. A deployed
service needs its own authentication, authorization, rate limits, account quotas,
and transport security before opening sessions with server credentials.

The demo allows four simultaneous sessions, including sessions still connecting.
Each client text message is limited to 1 MiB and each audio message to 24,000 int16
samples (one second at 24 kHz). Each connection can assemble one image at a time,
up to 4 MiB of ASCII data URL content in at most 128 chunks; the UI sends images in 60,000-character
chunks. A direct image message must also fit the text-message limit. These are
demo limits, not OpenAI API limits. Invalid or excessive input closes the socket
and releases its session. Keep the launch command's WebSocket backend and queue
limits when running the example.

### Debugging Realtime usage

Set `LOG_LEVEL=DEBUG` to log the raw `response.done` usage, the typed per-response usage with modality details, and the cumulative session usage:

```bash
cd examples/realtime/app && LOG_LEVEL=DEBUG uv run python server.py
```

The debug logs include concise summaries for server, model, session, history, tool, handoff, error, and usage events. Audio frames and high-volume delta events are omitted, and transcript content is not logged. Uvicorn and WebSocket protocol logging remain at INFO so `LOG_LEVEL=DEBUG` does not dump wire payloads.

## Customization

To use the same UI with your own agents, edit `agent.py` and ensure get_starting_agent() returns the right starting agent for your use case.

## How to Use

1. Click **Connect** to establish a realtime session
2. Audio capture starts automatically - just speak naturally
3. Click the **Mic On/Off** button to mute/unmute your microphone
4. To send an image, enter an optional prompt and click **🖼️ Send Image** (select a file)
5. Watch the conversation unfold in the left pane (image thumbnails are shown)
6. Monitor raw events in the right pane (click to expand/collapse)
7. Click **Disconnect** when done

### Human-in-the-loop approvals

- The seat update tool now requires approval. When the agent wants to run it, the browser shows a `window.confirm` dialog so you can allow or deny the tool call before it executes.

## Architecture

-   **Backend**: FastAPI server with WebSocket connections for real-time communication
-   **Session Management**: Each connection gets a unique session with the OpenAI Realtime API
- **Image Inputs**: The UI uploads images and the server forwards a `conversation.item.create` event with `input_image` (plus optional `input_text`), followed by `response.create` to start the model response. The messages pane renders image bubbles for `input_image` content.
-   **Audio Processing**: 24kHz mono audio capture and playback
-   **Event Handling**: Full event stream processing with transcript generation
-   **Frontend**: Vanilla JavaScript with clean, responsive CSS

The demo showcases the core patterns for building realtime voice applications with the OpenAI Agents SDK.
