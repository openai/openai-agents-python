# Static voice demo

This demo operates by capturing a recording, then running a voice pipeline on it.

Run via:

```
python -m examples.voice.static.main
```

## How it works

1. We create a `VoicePipeline`, setup with a custom workflow. The workflow runs an Agent, but it also has some custom responses if you say the secret word.
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
-   Tell me about dogs. (_will respond with the hardcoded "you guessed the secret word" message_)
