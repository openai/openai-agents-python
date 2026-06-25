# Voice Pipeline Lifecycle

Use this reference for changes to `VoicePipeline`, `AudioInput`, `StreamedAudioInput`, STT sessions, TTS task ordering, voice lifecycle events, PCM framing, result streaming, or voice tracing. Realtime agents use a different live-session architecture; read [Realtime session lifecycle](realtime-session-lifecycle.md) for that path.

## Pipeline Ownership

`VoicePipeline` owns an STT-to-workflow-to-TTS producer task and returns a `StreamedAudioResult` that drives its observable completion.

- Static `AudioInput` produces one transcription and one workflow turn. `StreamedAudioInput` creates a long-lived transcription session and runs one workflow turn for each emitted transcript until the input or session ends.
- The multi-turn pipeline owns the transcription session and closes it in `finally` before marking output complete. Partial setup and workflow failure must not strand the STT connection or producer task.
- `workflow.on_start()` applies only to the streamed multi-turn path. Its failure is logged and skipped so the transcription session can still start; normal per-turn workflow failures are terminal and surface through the result stream.
- The SDK does not provide application-level interruption handling for `StreamedAudioInput`. Lifecycle events expose turn boundaries, but microphone muting, playback interruption, and barge-in policy remain application-owned.

## Text, Audio, and Event Ordering

- A workflow can yield multiple text fragments. The text splitter returns ready-to-synthesize text plus a remainder; synthesize non-empty ready text even when it is shorter than a default sentence threshold, and retain the remainder for the turn's final flush.
- TTS segment tasks may run concurrently, but `_ordered_tasks` and the dispatcher must emit their audio and lifecycle events in workflow text order rather than completion order.
- `turn_started` precedes audio for that turn. `turn_ended` is emitted only after the turn's final text remainder has been synthesized and its audio dispatched. `session_ended` follows all ordered segment queues and all turns.
- A `VoiceStreamEventError` terminates result streaming and the stored exception is raised after task cleanup. `session_ended` is a lifecycle marker, not proof of success; consumers must still observe the terminal exception from `stream()`.
- Consuming `StreamedAudioResult.stream()` is the public completion and error boundary. On normal `session_ended`, let the producer finish before cleanup so session close and trace end are not cancelled by result teardown.

## PCM and Caller Data

- PCM16 samples span two bytes. Preserve a trailing half-sample across TTS chunks, combine it with the next chunk, and pad only the final unmatched byte at end of segment.
- Apply `buffer_size` to TTS source chunks without changing sample order. Convert to float32 only after PCM16 framing is complete, then apply caller-provided `transform_data` to each emitted array.
- `AudioInput.to_base64()` and audio-file conversion must not mutate the caller's NumPy buffer when converting float input to PCM16.
- Empty input and empty text-splitter output are valid boundaries. They must not cause NumPy reduction errors, phantom TTS calls, or missing turn/session lifecycle events.

## Trace Lifetime and Data

- The pipeline trace stays active for the full asynchronous producer lifecycle, not only until `VoicePipeline.run()` returns its result object.
- Each output turn owns a speech-group span and each synthesized segment owns a child speech span. Finish the turn span after ordered audio dispatch and finish the pipeline trace after STT session close and output completion.
- Text and audio sensitivity are independent controls. `trace_include_sensitive_data` governs transcript and TTS text, while `trace_include_sensitive_audio_data` governs encoded audio payloads.
- Error paths must finish active speech spans and the enclosing trace without replacing the original pipeline exception.

## Review Checklist

1. Test static and streamed input, including STT setup failure, workflow failure, TTS failure, and transcription-session close.
2. Verify fragment concurrency never changes audio, turn, or session event order.
3. Test short splitter output, empty output, odd-byte chunks, cross-chunk sample boundaries, int16, and float32 conversion.
4. Consume the public result stream and verify terminal errors, task cleanup, session close, and trace-end order.
5. Confirm sensitive text and audio are independently omitted from trace payloads.

## Sources

- `docs/voice/pipeline.md`
- `docs/voice/tracing.md`
- `src/agents/voice/pipeline.py`
- `src/agents/voice/result.py`
- `src/agents/voice/input.py`
- `src/agents/voice/model.py`
- `src/agents/voice/models/openai_stt.py`
- `tests/voice/test_pipeline.py`
- `tests/voice/test_input.py`
- `tests/voice/test_openai_stt.py`
- `tests/voice/test_openai_tts.py`
