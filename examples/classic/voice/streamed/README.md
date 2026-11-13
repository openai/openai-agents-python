# Streamed voice demo

This is an interactive demo, where you can talk to an Agent conversationally. It uses the voice pipeline's built in turn detection feature, so if you stop speaking the Agent responds.

Install the matching SDK with its `classic-voice` extra and install `sounddevice` with its PortAudio system library and `textual`. Set `OPENAI_API_KEY` and connect a microphone and an audio output device before running the demo. See [Classic setup](../../../../CLASSIC.md).

From the repository root:

```sh
python -m examples.classic.voice.streamed.main
```

## How it works

1. We create a `VoicePipeline` with `MyWorkflow`, a custom workflow that keeps conversation history, runs an Agent with tools and handoffs, and returns a fixed response if you say the secret word.
2. Press **K** in the terminal UI to start or stop microphone capture. Press **Q** to quit.
3. The pipeline is run with the recorded audio, which causes it to:
    1. Transcribe the audio
    2. Feed the transcription to the workflow, which runs the agent.
    3. Stream the output of the agent to a text-to-speech model.
4. Play the audio.

Some suggested examples to try:

-   Tell me a joke (_the assistant tells you a joke_)
-   What's the weather in Tokyo? (_will call the `get_weather` tool and then speak_)
-   Hola, como estas? (_will handoff to the spanish agent_)
-   Tell me about dogs. (_will respond with the hardcoded "you guessed the secret word" message_)
