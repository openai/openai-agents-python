"""Capture provider-owned media data without opening files or transports."""

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

ImageDetail: TypeAlias = Literal["auto", "low", "high", "original"]
_DETAILS = {"auto", "low", "high", "original"}


def _record(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("Media content must be a mapping")
    return dict(value)


def _source(value: object, *, file: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError("Media source must be a string")
    if not value or (file and value.isspace()):
        raise ValueError("Media source must be nonempty")
    return value


# Reuse the native model WebSocket ceiling before JSON receipt encoding.
_MAX_AUDIO_INPUT_BYTES = 64 * 1024 * 1024
_AUDIO_PART_OVERHEAD = len(json.dumps([{"type": "audio", "audio_url": ""}]))


def _contains_audio(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("type") in ("audio", "input_audio"):
            return True
        if value.get("type") in ("function_call_output", "custom_tool_call_output"):
            return _contains_audio(value.get("output"))
        return any(
            _contains_audio(value[key])
            for key in ("input", "content", "initial_messages")
            if key in value
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_audio(part) for part in value)
    return False


def _check_audio_json(value: Any, *, contains_audio: bool | None = None) -> None:
    """Bound the complete SDK-owned audio DTO before default ASCII JSON encoding."""
    if not (_contains_audio(value) if contains_audio is None else contains_audio):
        return
    remaining = _MAX_AUDIO_INPUT_BYTES
    active: set[int] = set()

    def consume(size: int) -> None:
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise ValueError("Audio input exceeds the native request capacity")

    def count(item: Any) -> None:
        if isinstance(item, str):
            consume(2)
            length = str.__len__(item)
            if length > remaining:
                raise ValueError("Audio input exceeds the native request capacity")
            for start in range(0, length, 32_768):
                consume(len(json.dumps(str.__getitem__(item, slice(start, start + 32_768)))) - 2)
        elif isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity in active:
                raise ValueError("Circular reference detected")
            active.add(identity)
            try:
                consume(2 + max(0, len(item) - 1) * 2)
                if isinstance(item, dict):
                    consume(len(item) * 2)
                    for key, child in item.items():
                        if not isinstance(key, str):
                            if key is None or isinstance(key, (bool, int, float)):
                                key = json.dumps(key)
                            else:
                                # Retain the existing encoder's unsupported-key error.
                                json.dumps({key: None})
                        count(key)
                        count(child)
                else:
                    for child in item:
                        count(child)
            finally:
                active.remove(identity)
        else:
            consume(len(json.dumps(item)))

    count(value)


def _audio_json(value: Any) -> str:
    _check_audio_json(value)
    return json.dumps(value)


def _capture_audio(value: object, budget: list[int] | None = None) -> dict[str, Any]:
    data = _record(value)
    if set(data) != {"audio_url"}:
        raise ValueError("Audio requires exactly one audio_url")
    audio = _source(data["audio_url"])
    if budget is None:
        budget = [_MAX_AUDIO_INPUT_BYTES]
    size = _AUDIO_PART_OVERHEAD
    length = str.__len__(audio)
    if length > budget[0] - size:
        raise ValueError("Audio input exceeds the native request capacity")
    # Python receipts use ASCII JSON. Bound quoting scratch, including astral escapes.
    for start in range(0, length, 32_768):
        chunk = str.__getitem__(audio, slice(start, start + 32_768))
        size += len(json.dumps(chunk)) - 2
        if size > budget[0]:
            raise ValueError("Audio input exceeds the native request capacity")
    budget[0] -= size
    return {"audio_url": audio}


def _capture_image(value: object) -> dict[str, Any]:
    data = _record(value)
    if set(data) - {"image_url", "file_id", "detail"}:
        raise ValueError("Unsupported image field")
    sources = [key for key in ("image_url", "file_id") if key in data]
    if len(sources) != 1:
        raise ValueError("Image requires exactly one image_url or file_id")
    key = sources[0]
    source = _source(data[key])
    if key == "image_url":
        header, separator, payload = source.partition(",")
        inline = header.startswith("data:image/") and header.endswith(";base64")
        if inline:
            if not separator or not payload:
                raise ValueError("Inline image requires a nonempty base64 data URL")
        else:
            parsed = urlsplit(source)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Image URL must be an absolute HTTP(S) or image data URL")
    result: dict[str, Any] = {key: source}
    detail = data.get("detail")
    if detail is not None:
        if not isinstance(detail, str) or detail not in _DETAILS:
            raise ValueError("Unsupported image detail")
        result["detail"] = detail
    return result


def _capture_file(value: object) -> dict[str, Any]:
    data = _record(value)
    if set(data) - {"file_id", "file_data", "file_url", "filename"}:
        raise ValueError("Unsupported file field")
    sources = [key for key in ("file_id", "file_data", "file_url") if key in data]
    if len(sources) != 1:
        raise ValueError("File requires exactly one file_id, file_data or file_url")
    key = sources[0]
    source = _source(data[key], file=True)
    if key == "file_url":
        parsed = urlsplit(source)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("File URL must be an absolute HTTPS URL")
    elif key == "file_data":
        start = 0
        if source[:5].lower() == "data:":
            comma = source.find(",", 5)
            if comma < 12 or source[comma - 7 : comma].lower() != ";base64":
                raise ValueError("File data must contain base64 data")
            start = comma + 1
        if start == len(source):
            raise ValueError("File data must contain canonical base64 data")
        # Aligned chunks bound validation scratch space without copying the payload.
        chunk_size = 32 * 1024
        for offset in range(start, len(source), chunk_size):
            end = min(offset + chunk_size, len(source))
            payload = source[offset:end]
            if end != len(source) and "=" in payload:
                raise ValueError("File data must contain canonical base64 data")
            try:
                decoded = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
                canonical = base64.b64encode(decoded).decode("ascii")
            except (ValueError, UnicodeError) as error:
                raise ValueError("File data must contain base64 data") from error
            if payload != canonical and payload != canonical.rstrip("="):
                raise ValueError("File data must contain canonical base64 data")
    result: dict[str, Any] = {key: source}
    filename = data.get("filename")
    if filename is not None:
        if not isinstance(filename, str):
            raise TypeError("File filename must be a string")
        result["filename"] = filename
    return result


def _capture_tool_part(value: object) -> dict[str, Any]:
    part = _record(value)
    kind = part.pop("type", None)
    if kind == "inputImage":
        if set(part) - {"imageUrl", "fileId", "detail"}:
            raise ValueError("Unsupported tool image field")
        image = _capture_image(
            {
                "image_url" if key == "imageUrl" else "file_id" if key == "fileId" else key: item
                for key, item in part.items()
            }
        )
        return {
            "type": kind,
            **{
                "imageUrl" if key == "image_url" else "fileId" if key == "file_id" else key: item
                for key, item in image.items()
            },
        }
    if kind == "inputFile":
        return {"type": kind, **_capture_file(part)}
    field = {"inputText": "text", "inputAudio": "audioUrl"}.get(kind)
    if field is None or set(part) != {field} or not isinstance(part[field], str):
        raise ValueError("Unsupported tool content")
    return {"type": kind, field: part[field]}
