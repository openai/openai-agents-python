# Quickstart

Realtime agents enable voice conversations with your AI agents using OpenAI's Realtime API. This guide walks you through creating your first realtime voice agent.

!!! warning "Beta feature"
Realtime agents are in beta. Expect some breaking changes as we improve the implementation.

## Prerequisites

-   Python 3.9 or higher
-   OpenAI API key
-   Basic familiarity with the OpenAI Agents SDK

## Installation

If you haven't already, install the OpenAI Agents SDK:

```bash
pip install openai-agents
```

## Creating your first realtime agent

### 1. Import required components

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. Create a realtime agent

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. Set up the runner

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime",
            "voice": "ash",
            "modalities": ["audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        }
    }
)
```

### 4. Start a session

```python
# Start the session
session = await runner.run()

async with session:
    print("Session started! The agent will stream audio responses in real-time.")
    # Process events
    async for event in session:
        try:
            if event.type == "agent_start":
                print(f"Agent started: {event.agent.name}")
            elif event.type == "agent_end":
                print(f"Agent ended: {event.agent.name}")
            elif event.type == "handoff":
                print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
            elif event.type == "tool_start":
                print(f"Tool started: {event.tool.name}")
            elif event.type == "tool_end":
                print(f"Tool ended: {event.tool.name}; output: {event.output}")
            elif event.type == "audio_end":
                print("Audio ended")
            elif event.type == "audio":
                # Enqueue audio for callback-based playback with metadata
                # Non-blocking put; queue is unbounded, so drops won’t occur.
                pass
            elif event.type == "audio_interrupted":
                print("Audio interrupted")
                # Begin graceful fade + flush in the audio callback and rebuild jitter buffer.
            elif event.type == "error":
                print(f"Error: {event.error}")
            elif event.type == "history_updated":
                pass  # Skip these frequent events
            elif event.type == "history_added":
                pass  # Skip these frequent events
            elif event.type == "raw_model_event":
                print(f"Raw model event: {_truncate_str(str(event.data), 200)}")
            else:
                print(f"Unknown event type: {event.type}")
        except Exception as e:
            print(f"Error processing event: {_truncate_str(str(e), 200)}")

def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s
```

## Complete example

Here's a complete working example:

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner

async def main():
    # Create the agent
    agent = RealtimeAgent(
        name="Assistant",
        instructions="You are a helpful voice assistant. Keep responses brief and conversational.",
    )
    # Set up the runner with configuration
    runner = RealtimeRunner(
        starting_agent=agent,
        config={
            "model_settings": {
                "model_name": "gpt-realtime",
                "voice": "ash",
                "modalities": ["audio"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
            }
        },
    )
    # Start the session
    session = await runner.run()

    async with session:
        print("Session started! The agent will stream audio responses in real-time.")
        # Process events
        async for event in session:
            try:
                if event.type == "agent_start":
                    print(f"Agent started: {event.agent.name}")
                elif event.type == "agent_end":
                    print(f"Agent ended: {event.agent.name}")
                elif event.type == "handoff":
                    print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
                elif event.type == "tool_start":
                    print(f"Tool started: {event.tool.name}")
                elif event.type == "tool_end":
                    print(f"Tool ended: {event.tool.name}; output: {event.output}")
                elif event.type == "audio_end":
                    print("Audio ended")
                elif event.type == "audio":
                    # Enqueue audio for callback-based playback with metadata
                    # Non-blocking put; queue is unbounded, so drops won’t occur.
                    pass
                elif event.type == "audio_interrupted":
                    print("Audio interrupted")
                    # Begin graceful fade + flush in the audio callback and rebuild jitter buffer.
                elif event.type == "error":
                    print(f"Error: {event.error}")
                elif event.type == "history_updated":
                    pass  # Skip these frequent events
                elif event.type == "history_added":
                    pass  # Skip these frequent events
                elif event.type == "raw_model_event":
                    print(f"Raw model event: {_truncate_str(str(event.data), 200)}")
                else:
                    print(f"Unknown event type: {event.type}")
            except Exception as e:
                print(f"Error processing event: {_truncate_str(str(e), 200)}")

def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s

if __name__ == "__main__":
    # Run the session
    asyncio.run(main())
```

## Configuration options

### Model settings

-   `model_name`: Choose from available realtime models (e.g., `gpt-realtime`)
-   `voice`: Select voice (`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`)
-   `modalities`: Enable text or audio (`["text"]` or `["audio"]`)

### Audio settings

-   `input_audio_format`: Format for input audio (`pcm16`, `g711_ulaw`, `g711_alaw`)
-   `output_audio_format`: Format for output audio
-   `input_audio_transcription`: Transcription configuration

### Turn detection

-   `type`: Detection method (`server_vad`, `semantic_vad`)
-   `threshold`: Voice activity threshold (0.0-1.0)
-   `silence_duration_ms`: Silence duration to detect turn end
-   `prefix_padding_ms`: Audio padding before speech

## Next steps

-   [Learn more about realtime agents](guide.md)
-   Check out working examples in the [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) folder
-   Add tools to your agent
-   Implement handoffs between agents
-   Set up guardrails for safety

## Authentication

Make sure your OpenAI API key is set in your environment:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

Or pass it directly when creating the session:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```
