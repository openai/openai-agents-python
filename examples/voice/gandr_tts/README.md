# Gandr TTS voice demo

This demo runs the voice pipeline with a third party text-to-speech model. [Gandr](https://gandr.ai) is an OpenAI compatible TTS API, so the swap is an `AsyncOpenAI` client pointed at `https://tts.gandr.ai/v1` plus a small `TTSModel` subclass. Speech-to-text and the agent keep using your OpenAI key.

Run via:

```
python -m examples.voice.gandr_tts.main
```

Set two environment variables first:

```
export OPENAI_API_KEY=...   # transcription and the agent
export GANDR_API_KEY=...    # speech output
```

Keys are at [gandr.ai](https://gandr.ai). The free tier is 50,000 tokens.

## How it works

1. We create a `VoicePipeline` with a `SingleAgentVoiceWorkflow` and pass a custom `tts_model`.
2. `GandrTTSModel` implements the `TTSModel` interface. It calls `POST /v1/audio/speech` through the OpenAI Python client with `response_format="pcm"`. Gandr's pcm output is headerless s16le mono at 24000 Hz, which is what the pipeline streams.
3. When you speak, the audio is transcribed with your OpenAI key, the agent runs, and the reply is spoken through Gandr.

The same shape works for any OpenAI compatible speech endpoint: point the client at a different `base_url` and implement `TTSModel.run` with the fields that endpoint supports.

## Notes

-   Voices: `gandr-mia`, `gandr-ava`, `gandr-jenny`, `gandr-dane`, `gandr-leo`, `gandr-lewis`. 23 languages.
-   Requests are capped at 2000 characters. The pipeline's sentence based splitter keeps each chunk well under that.
-   `TTSModelSettings.instructions` is OpenAI specific and is not sent to Gandr.
-   Every render is watermarked.
