# Realtime Weather Voice Agent

This example shows how to build a realtime voice assistant with the OpenAI Agents SDK. The agent speaks with the `gpt-4o-realtime-preview` model, calls a mock `lookup_weather` tool, and streams responses into a Streamlit dashboard so you can watch events as they arrive.

## Prerequisites

- Python 3.9+
- An `OPENAI_API_KEY` environment variable with access to the realtime API
- A microphone and speakers so you can record and listen to audio replies

## Setup

Install the project dependencies from the repository root. All commands use `uv`, matching the rest of the realtime documentation (`docs/realtime`).

```bash
uv sync
uv pip install streamlit sounddevice numpy
```

If you are running the example outside of this repository, install the SDK with voice support alongside Streamlit:

```bash
uv pip install "openai-agents[voice]" streamlit sounddevice numpy
```

## Run the demo

Start the Streamlit UI without any command line arguments:

```bash
cd examples/realtime/weather_voice_agent
export OPENAI_API_KEY=...  # replace with your key
uv run streamlit run app.py
```

## How to use the UI

1. Click **Connect** to open a realtime session using `RealtimeRunner`.
2. When the status turns to `connected`, press **Record and send** to capture a short microphone clip (between two and six seconds) and send it to the agent.
3. Ask for the weather in cities such as New York, San Francisco, Seattle, or Austin. The agent will call the mock `lookup_weather` tool before responding.
4. Watch the **Conversation** pane for user transcripts, tool results, and assistant replies. The **Event log** shows each event that Streamlit receives from the session.
5. Listen to the assistant's audio responses in the **Assistant audio replies** section, or use the optional text form if you do not want to record audio.
6. Click **Disconnect** to end the session once you are finished.

## Project files

- `agent.py` creates the realtime agent and defines the mock weather tool and run configuration.
- `app.py` contains the Streamlit UI that records audio, streams events, and renders the session state.

The example mirrors the configuration described in `docs/realtime/quickstart.md` while remaining self-contained by using a local tool rather than a live weather API.
