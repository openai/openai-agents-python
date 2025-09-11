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

## Architecture

-   **Backend**: FastAPI server with WebSocket connections for real-time communication
-   **Session Management**: Each connection gets a unique session with the OpenAI Realtime API
-   **Image Inputs**: The UI uploads images and the server forwards a
    `conversation.item.create` event with `input_image` (plus optional `input_text`),
    followed by `response.create` to start the model response. The messages pane
    renders image bubbles for `input_image` content.
-   **Audio Processing**: 24kHz mono audio capture and playback
-   **Event Handling**: Full event stream processing with transcript generation
-   **Frontend**: Vanilla JavaScript with clean, responsive CSS

The demo showcases the core patterns for building realtime voice applications with the OpenAI Agents SDK.
