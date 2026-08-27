try:
    from agents.voice import StreamedAudioResult
except ImportError:
    pass


async def extract_events(result: StreamedAudioResult) -> tuple[list[str], list[bytes]]:
    """Collapse pipeline stream events to simple labels for ordering assertions."""
    flattened: list[str] = []
    audio_chunks: list[bytes] = []

    async for ev in result.stream():
        if ev.type == "voice_stream_event_audio":
            if ev.data is not None:
                audio_chunks.append(ev.data.tobytes())
            flattened.append("audio")
        elif ev.type == "voice_stream_event_lifecycle":
            flattened.append(ev.event)
        elif ev.type == "voice_stream_event_error":
            flattened.append("error")
    return flattened, audio_chunks
