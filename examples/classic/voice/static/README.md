# Static voice demo

This demo operates by capturing a recording, then running a voice pipeline on it.

Install the matching SDK with its `classic-voice` extra and install `sounddevice` with its PortAudio system library. Set `OPENAI_API_KEY` and connect a microphone and an audio output device before running the demo. See [Classic setup](../../../../CLASSIC.md).

Run via:

```
python -m examples.classic.voice.static.main
```

## How it works

1. We create a `VoicePipeline` with a `SingleAgentVoiceWorkflow` that runs an Agent with tools and handoffs.
2. When you speak, audio is forwarded to the voice pipeline. When you stop speaking, the agent runs.
3. The pipeline is run with the audio, which causes it to:
    1. Transcribe the audio
    2. Feed the transcription to the workflow, which runs the agent.
    3. Stream the output of the agent to a text-to-speech model.
4. Play the audio.

Some suggested examples to try:

-   Tell me a joke (_the assistant tells you a joke_)
-   What's the weather in Tokyo? (_will call the `get_weather` tool and then speak_)
-   Hola, como estas? (_will handoff to the spanish agent_)
