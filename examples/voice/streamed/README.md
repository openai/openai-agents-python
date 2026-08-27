# Streamed voice demo

This is an interactive demo, where you can talk to an Agent conversationally. It uses the voice pipeline's built in turn detection feature, so if you stop speaking the Agent responds.

Run via:

```
python -m examples.voice.streamed.main
```

## How it works

1. We create a `VoicePipeline`, setup with a `SingleAgentVoiceWorkflow`. This is a workflow that starts at an Assistant agent, has tools and handoffs.
2. Audio input is captured from the terminal.
3. The pipeline is run with the recorded audio, which causes it to:
    1. Transcribe the audio
    2. Feed the transcription to the workflow, which runs the agent.
    3. Stream the output of the agent to a text-to-speech model.
4. Play the audio.

Some suggested examples to try:

-   Tell me a joke (_the assistant tells you a joke_)
-   What's the weather in Tokyo? (_will call the `get_weather` tool and then speak_)
-   Hola, como estas? (_will handoff to the spanish agent_)
