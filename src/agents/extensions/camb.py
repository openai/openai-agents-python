"""camb.ai toolkit for the OpenAI Agents SDK.

Provides 9 audio/speech tools powered by camb.ai:
- Text-to-Speech (TTS)
- Translation
- Transcription
- Translated TTS
- Voice Cloning
- Voice Listing
- Voice Creation from Description
- Text-to-Sound generation
- Audio Separation

These tools can be used with any Agent by calling ``CambAITools().get_tools()``.
"""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
from os import getenv
from typing import Any

from agents.tool import FunctionTool, function_tool


class CambAITools:
    """Toolkit that exposes camb.ai audio/speech services as agent tools.

    Each enabled service is returned as a :class:`FunctionTool` instance that
    agents can call.  The underlying ``camb`` SDK is imported lazily so that
    installing the extra is only required when the toolkit is actually used.

    Args:
        api_key: camb.ai API key.  Falls back to ``CAMB_API_KEY`` env var.
        timeout: Request timeout in seconds.
        max_poll_attempts: Maximum number of polling attempts for async tasks.
        poll_interval: Seconds between polling attempts.
        enable_tts: Enable the text-to-speech tool.
        enable_translation: Enable the translation tool.
        enable_transcription: Enable the transcription tool.
        enable_translated_tts: Enable the translated TTS tool.
        enable_voice_clone: Enable the voice cloning tool.
        enable_voice_list: Enable the voice listing tool.
        enable_voice_from_description: Enable the voice-from-description tool.
        enable_text_to_sound: Enable the text-to-sound tool.
        enable_audio_separation: Enable the audio separation tool.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_poll_attempts: int = 60,
        poll_interval: float = 2.0,
        enable_tts: bool = True,
        enable_translation: bool = True,
        enable_transcription: bool = True,
        enable_translated_tts: bool = True,
        enable_voice_clone: bool = True,
        enable_voice_list: bool = True,
        enable_voice_from_description: bool = True,
        enable_text_to_sound: bool = True,
        enable_audio_separation: bool = True,
    ) -> None:
        self._api_key = api_key or getenv("CAMB_API_KEY")
        if not self._api_key:
            raise ValueError(
                "CAMB_API_KEY not set. Please set the CAMB_API_KEY environment variable "
                "or pass api_key to CambAITools."
            )

        self._timeout = timeout
        self._max_poll_attempts = max_poll_attempts
        self._poll_interval = poll_interval
        self._client: Any = None  # Lazy AsyncCambAI

        self._enable_tts = enable_tts
        self._enable_translation = enable_translation
        self._enable_transcription = enable_transcription
        self._enable_translated_tts = enable_translated_tts
        self._enable_voice_clone = enable_voice_clone
        self._enable_voice_list = enable_voice_list
        self._enable_voice_from_description = enable_voice_from_description
        self._enable_text_to_sound = enable_text_to_sound
        self._enable_audio_separation = enable_audio_separation

    def _get_client(self) -> Any:
        """Return a lazily-initialised ``AsyncCambAI`` client."""
        if self._client is None:
            try:
                from camb.client import AsyncCambAI
            except ImportError as e:
                raise ImportError(
                    "The 'camb' package is required. Install it with: "
                    "pip install 'openai-agents[camb]'"
                ) from e
            self._client = AsyncCambAI(api_key=self._api_key, timeout=self._timeout)
        return self._client

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _poll_async(self, get_status_fn: Any, task_id: Any, *, run_id: Any = None) -> Any:
        """Poll a camb.ai async task until completion."""
        for _ in range(self._max_poll_attempts):
            status = await get_status_fn(task_id, run_id=run_id)
            if hasattr(status, "status"):
                val = status.status
                if val in ("completed", "SUCCESS"):
                    return status
                if val in ("failed", "FAILED", "error"):
                    raise RuntimeError(f"Task failed: {getattr(status, 'error', 'Unknown error')}")
            await asyncio.sleep(self._poll_interval)
        raise TimeoutError(
            f"Task {task_id} did not complete within "
            f"{self._max_poll_attempts * self._poll_interval}s"
        )

    @staticmethod
    def _detect_audio_format(data: bytes, content_type: str = "") -> str:
        """Detect audio format from raw bytes or content-type header."""
        if data.startswith(b"RIFF"):
            return "wav"
        if data.startswith((b"\xff\xfb", b"\xff\xfa", b"ID3")):
            return "mp3"
        if data.startswith(b"fLaC"):
            return "flac"
        if data.startswith(b"OggS"):
            return "ogg"
        ct = content_type.lower()
        for key, fmt in [
            ("wav", "wav"),
            ("wave", "wav"),
            ("mpeg", "mp3"),
            ("mp3", "mp3"),
            ("flac", "flac"),
            ("ogg", "ogg"),
        ]:
            if key in ct:
                return fmt
        return "pcm"

    @staticmethod
    def _add_wav_header(pcm_data: bytes) -> bytes:
        """Wrap raw PCM data with a WAV header."""
        sr, ch, bps = 24000, 1, 16
        byte_rate = sr * ch * bps // 8
        block_align = ch * bps // 8
        data_size = len(pcm_data)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + data_size,
            b"WAVE",
            b"fmt ",
            16,
            1,
            ch,
            sr,
            byte_rate,
            block_align,
            bps,
            b"data",
            data_size,
        )
        return header + pcm_data

    @staticmethod
    def _save_audio(data: bytes, suffix: str = ".wav") -> str:
        """Save audio bytes to a temp file and return the path."""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            return f.name

    @staticmethod
    def _gender_str(g: int) -> str:
        """Convert numeric gender code to a human-readable string."""
        return {0: "not_specified", 1: "male", 2: "female", 9: "not_applicable"}.get(g, "unknown")

    @staticmethod
    def _format_transcription(transcription: Any) -> str:
        """Format a transcription result as a JSON string."""
        out: dict[str, Any] = {
            "text": getattr(transcription, "text", ""),
            "segments": [],
            "speakers": [],
        }
        if hasattr(transcription, "segments"):
            for seg in transcription.segments:
                out["segments"].append(
                    {
                        "start": getattr(seg, "start", 0),
                        "end": getattr(seg, "end", 0),
                        "text": getattr(seg, "text", ""),
                        "speaker": getattr(seg, "speaker", None),
                    }
                )
        if hasattr(transcription, "speakers"):
            out["speakers"] = list(transcription.speakers)
        elif out["segments"]:
            out["speakers"] = list({s["speaker"] for s in out["segments"] if s.get("speaker")})
        return json.dumps(out, indent=2)

    def _format_voices(self, voices: Any) -> str:
        """Format a list of voice objects as a JSON string."""
        out: list[dict[str, Any]] = []
        for v in voices:
            if isinstance(v, dict):
                out.append(
                    {
                        "id": v.get("id"),
                        "name": v.get("voice_name", v.get("name", "Unknown")),
                        "gender": self._gender_str(v.get("gender", 0)),
                        "age": v.get("age"),
                        "language": v.get("language"),
                    }
                )
            else:
                out.append(
                    {
                        "id": getattr(v, "id", None),
                        "name": getattr(v, "voice_name", getattr(v, "name", "Unknown")),
                        "gender": self._gender_str(getattr(v, "gender", 0)),
                        "age": getattr(v, "age", None),
                        "language": getattr(v, "language", None),
                    }
                )
        return json.dumps(out, indent=2)

    def _format_separation(self, sep: Any) -> str:
        """Format an audio separation result as a JSON string."""
        out: dict[str, Any] = {"vocals": None, "background": None, "status": "completed"}
        for attr, key in [
            ("vocals_url", "vocals"),
            ("vocals", "vocals"),
            ("voice_url", "vocals"),
            ("background_url", "background"),
            ("background", "background"),
            ("instrumental_url", "background"),
        ]:
            val = getattr(sep, attr, None)
            if val and out[key] is None:
                if isinstance(val, bytes):
                    out[key] = self._save_audio(val, f"_{key}.wav")
                else:
                    out[key] = val
        return json.dumps(out, indent=2)

    @staticmethod
    def _extract_translation(result: Any) -> str:
        """Extract translated text from an SDK result."""
        if hasattr(result, "__iter__") and not isinstance(result, (str, bytes)):
            parts: list[str] = []
            for chunk in result:
                if hasattr(chunk, "text"):
                    parts.append(chunk.text)
                elif isinstance(chunk, str):
                    parts.append(chunk)
            return "".join(parts)
        if hasattr(result, "text"):
            return str(result.text)
        return str(result)

    # ------------------------------------------------------------------
    # get_tools — build and return FunctionTool instances
    # ------------------------------------------------------------------

    def get_tools(self) -> list[FunctionTool]:
        """Return the enabled tools as a list of :class:`FunctionTool` instances."""
        tools: list[FunctionTool] = []

        if self._enable_tts:
            tools.append(self._make_tts_tool())
        if self._enable_translation:
            tools.append(self._make_translate_tool())
        if self._enable_transcription:
            tools.append(self._make_transcribe_tool())
        if self._enable_translated_tts:
            tools.append(self._make_translated_tts_tool())
        if self._enable_voice_clone:
            tools.append(self._make_clone_voice_tool())
        if self._enable_voice_list:
            tools.append(self._make_list_voices_tool())
        if self._enable_voice_from_description:
            tools.append(self._make_voice_from_description_tool())
        if self._enable_text_to_sound:
            tools.append(self._make_text_to_sound_tool())
        if self._enable_audio_separation:
            tools.append(self._make_audio_separation_tool())

        return tools

    # ------------------------------------------------------------------
    # Individual tool builders
    # ------------------------------------------------------------------

    def _make_tts_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_tts(
            text: str,
            language: str = "en-us",
            voice_id: int = 147320,
            speech_model: str = "mars-flash",
            user_instructions: str | None = None,
        ) -> str:
            """Convert text to speech using camb.ai.

            Supports 140+ languages and multiple voice models.  The audio is
            saved to a temporary file and the file path is returned.

            Args:
                text: Text to convert to speech (3-3000 characters).
                language: BCP-47 language code (e.g. 'en-us', 'fr-fr').
                voice_id: Voice ID.  Use camb_list_voices to find voices.
                speech_model: Model: 'mars-flash', 'mars-pro', 'mars-instruct'.
                user_instructions: Instructions for mars-instruct model.
            """
            from camb import StreamTtsOutputConfiguration

            client = toolkit._get_client()
            kwargs: dict[str, Any] = {
                "text": text,
                "language": language,
                "voice_id": voice_id,
                "speech_model": speech_model,
                "output_configuration": StreamTtsOutputConfiguration(format="wav"),
            }
            if user_instructions and speech_model == "mars-instruct":
                kwargs["user_instructions"] = user_instructions

            chunks: list[bytes] = []
            async for chunk in client.text_to_speech.tts(**kwargs):
                chunks.append(chunk)
            return toolkit._save_audio(b"".join(chunks), ".wav")

        return function_tool(camb_tts, name_override="camb_tts")

    def _make_translate_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_translate(
            text: str,
            source_language: int,
            target_language: int,
            formality: int | None = None,
        ) -> str:
            """Translate text between 140+ languages using camb.ai.

            Provide integer language codes: 1=English, 2=Spanish, 3=French, 4=German,
            5=Italian, 6=Portuguese, 7=Dutch, 8=Russian, 9=Japanese, 10=Korean,
            11=Chinese.

            Args:
                text: Text to translate.
                source_language: Source language code (integer).
                target_language: Target language code (integer).
                formality: Optional formality level: 1=formal, 2=informal.
            """
            from camb.core.api_error import ApiError

            client = toolkit._get_client()
            kwargs: dict[str, Any] = {
                "text": text,
                "source_language": source_language,
                "target_language": target_language,
            }
            if formality:
                kwargs["formality"] = formality

            try:
                result = await client.translation.translation_stream(**kwargs)
                return toolkit._extract_translation(result)
            except ApiError as e:
                if e.status_code == 200 and e.body:
                    return str(e.body)
                raise

        return function_tool(camb_translate, name_override="camb_translate")

    def _make_transcribe_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_transcribe(
            language: int,
            audio_url: str | None = None,
            audio_file_path: str | None = None,
        ) -> str:
            """Transcribe audio to text with speaker identification using camb.ai.

            Supports audio URLs or local file paths.  Returns JSON with full transcription
            text, timed segments, and speaker labels.

            Args:
                language: Language code (integer).  1=English, 2=Spanish, 3=French, etc.
                audio_url: URL of the audio file to transcribe.
                audio_file_path: Local file path to the audio file.
            """
            client = toolkit._get_client()
            kwargs: dict[str, Any] = {"language": language}

            if audio_url:
                import httpx

                async with httpx.AsyncClient() as http:
                    resp = await http.get(audio_url)
                    resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(resp.content)
                    tmp_path = tmp.name
                with open(tmp_path, "rb") as f:
                    kwargs["media_file"] = f
                    result = await client.transcription.create_transcription(**kwargs)
            elif audio_file_path:
                with open(audio_file_path, "rb") as f:
                    kwargs["media_file"] = f
                    result = await client.transcription.create_transcription(**kwargs)
            else:
                return json.dumps({"error": "Provide either audio_url or audio_file_path"})

            task_id = result.task_id
            status = await toolkit._poll_async(
                client.transcription.get_transcription_task_status, task_id
            )
            transcription = await client.transcription.get_transcription_result(status.run_id)
            return toolkit._format_transcription(transcription)

        return function_tool(camb_transcribe, name_override="camb_transcribe")

    def _make_translated_tts_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_translated_tts(
            text: str,
            source_language: int,
            target_language: int,
            voice_id: int = 147320,
            formality: int | None = None,
        ) -> str:
            """Translate text and convert to speech in one step using camb.ai.

            Returns the file path to the generated audio file.

            Args:
                text: Text to translate and speak.
                source_language: Source language code (integer).
                target_language: Target language code (integer).
                voice_id: Voice ID for TTS output.
                formality: Optional formality: 1=formal, 2=informal.
            """
            import httpx

            client = toolkit._get_client()
            kwargs: dict[str, Any] = {
                "text": text,
                "voice_id": voice_id,
                "source_language": source_language,
                "target_language": target_language,
            }
            if formality:
                kwargs["formality"] = formality

            result = await client.translated_tts.create_translated_tts(**kwargs)
            status = await toolkit._poll_async(
                client.translated_tts.get_translated_tts_task_status, result.task_id
            )

            # Fetch the audio via the run_id result endpoint.
            run_id = getattr(status, "run_id", None)
            audio_data = b""
            fmt = "pcm"
            if run_id:
                client_wrapper = getattr(client, "_client_wrapper", None)
                if client_wrapper and hasattr(client_wrapper, "base_url"):
                    url = f"{client_wrapper.base_url}/tts-result/{run_id}"
                else:
                    url = f"https://client.camb.ai/apis/tts-result/{run_id}"

                async with httpx.AsyncClient() as http:
                    resp = await http.get(url, headers={"x-api-key": toolkit._api_key or ""})
                    if resp.status_code == 200:
                        audio_data = resp.content
                        fmt = toolkit._detect_audio_format(
                            audio_data, resp.headers.get("content-type", "")
                        )

            if fmt == "pcm" and audio_data:
                audio_data = toolkit._add_wav_header(audio_data)
                fmt = "wav"

            ext = {"wav": ".wav", "mp3": ".mp3", "flac": ".flac", "ogg": ".ogg"}.get(fmt, ".wav")
            return toolkit._save_audio(audio_data, ext)

        return function_tool(camb_translated_tts, name_override="camb_translated_tts")

    def _make_clone_voice_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_clone_voice(
            voice_name: str,
            audio_file_path: str,
            gender: int,
            description: str | None = None,
            age: int | None = None,
            language: int | None = None,
        ) -> str:
            """Clone a voice from an audio sample using camb.ai.

            Creates a custom voice from a 2+ second audio sample that can be used with
            camb_tts and camb_translated_tts.

            Args:
                voice_name: Name for the new cloned voice.
                audio_file_path: Path to audio file (minimum 2 seconds).
                gender: Gender: 1=Male, 2=Female, 0=Not Specified, 9=Not Applicable.
                description: Optional description of the voice.
                age: Optional age of the voice.
                language: Optional language code for the voice.
            """
            client = toolkit._get_client()
            with open(audio_file_path, "rb") as f:
                kwargs: dict[str, Any] = {
                    "voice_name": voice_name,
                    "gender": gender,
                    "file": f,
                }
                if description:
                    kwargs["description"] = description
                if age:
                    kwargs["age"] = age
                if language:
                    kwargs["language"] = language
                result = await client.voice_cloning.create_custom_voice(**kwargs)

            out: dict[str, Any] = {
                "voice_id": getattr(result, "voice_id", getattr(result, "id", None)),
                "voice_name": voice_name,
                "status": "created",
            }
            if hasattr(result, "message"):
                out["message"] = result.message
            return json.dumps(out, indent=2)

        return function_tool(camb_clone_voice, name_override="camb_clone_voice")

    def _make_list_voices_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_list_voices() -> str:
            """List all available voices from camb.ai.

            Returns voice IDs, names, genders, ages, and languages.  Use the voice ID
            with camb_tts or camb_translated_tts.
            """
            client = toolkit._get_client()
            voices = await client.voice_cloning.list_voices()
            return toolkit._format_voices(voices)

        return function_tool(camb_list_voices, name_override="camb_list_voices")

    def _make_voice_from_description_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_voice_from_description(
            text: str,
            voice_description: str,
        ) -> str:
            """Generate a synthetic voice from a detailed text description using camb.ai.

            Provide sample text the voice will speak and a detailed description of the
            desired voice (minimum 100 characters / 18+ words).  Include details like
            accent, tone, age, gender, speaking style, etc.

            Args:
                text: Sample text the generated voice will speak.
                voice_description: Detailed description of the desired voice (min 100 chars).
            """
            client = toolkit._get_client()
            result = await client.text_to_voice.create_text_to_voice(
                text=text, voice_description=voice_description
            )
            status = await toolkit._poll_async(
                client.text_to_voice.get_text_to_voice_status, result.task_id
            )
            voice_result = await client.text_to_voice.get_text_to_voice_result(status.run_id)

            out: dict[str, Any] = {
                "previews": getattr(voice_result, "previews", []),
                "status": "completed",
            }
            return json.dumps(out, indent=2)

        return function_tool(
            camb_voice_from_description, name_override="camb_voice_from_description"
        )

    def _make_text_to_sound_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_text_to_sound(
            prompt: str,
            duration: float | None = None,
            audio_type: str | None = None,
        ) -> str:
            """Generate sounds, music, or soundscapes from text descriptions using camb.ai.

            Describe the sound or music you want and the tool will generate it.  Returns
            the file path to the generated audio file.

            Args:
                prompt: Description of the sound or music to generate.
                duration: Optional duration in seconds.
                audio_type: Optional type: 'music' or 'sound'.
            """
            client = toolkit._get_client()
            kwargs: dict[str, Any] = {"prompt": prompt}
            if duration:
                kwargs["duration"] = duration
            if audio_type:
                kwargs["audio_type"] = audio_type

            result = await client.text_to_audio.create_text_to_audio(**kwargs)
            status = await toolkit._poll_async(
                client.text_to_audio.get_text_to_audio_status, result.task_id
            )

            chunks: list[bytes] = []
            async for chunk in client.text_to_audio.get_text_to_audio_result(status.run_id):
                chunks.append(chunk)
            return toolkit._save_audio(b"".join(chunks), ".wav")

        return function_tool(camb_text_to_sound, name_override="camb_text_to_sound")

    def _make_audio_separation_tool(self) -> FunctionTool:
        toolkit = self

        async def camb_audio_separation(
            audio_url: str | None = None,
            audio_file_path: str | None = None,
        ) -> str:
            """Separate vocals/speech from background audio using camb.ai.

            Provide either an audio URL or a local file path.  Returns JSON with paths
            to the separated vocals and background audio files.

            Args:
                audio_url: URL of the audio file to separate.
                audio_file_path: Local file path to the audio file.
            """
            client = toolkit._get_client()
            kwargs: dict[str, Any] = {}

            if audio_url:
                import httpx

                async with httpx.AsyncClient() as http:
                    resp = await http.get(audio_url)
                    resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(resp.content)
                    tmp_path = tmp.name
                with open(tmp_path, "rb") as f:
                    kwargs["media_file"] = f
                    result = await client.audio_separation.create_audio_separation(**kwargs)
            elif audio_file_path:
                with open(audio_file_path, "rb") as f:
                    kwargs["media_file"] = f
                    result = await client.audio_separation.create_audio_separation(**kwargs)
            else:
                return json.dumps({"error": "Provide either audio_url or audio_file_path"})

            status = await toolkit._poll_async(
                client.audio_separation.get_audio_separation_status, result.task_id
            )
            sep = await client.audio_separation.get_audio_separation_run_info(status.run_id)
            return toolkit._format_separation(sep)

        return function_tool(camb_audio_separation, name_override="camb_audio_separation")
