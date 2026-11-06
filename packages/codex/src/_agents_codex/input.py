"""Responses-shaped user messages for native sessions."""

from collections.abc import Mapping as Mapping, Sequence as Sequence
from typing import Any as Any, Literal as Literal, TypeAlias as TypeAlias

from typing_extensions import NotRequired as NotRequired, TypedDict as TypedDict

from ._public import preserve_module_identity as _preserve_module_identity


class InputText(TypedDict):
    """One Responses input_text content part."""

    type: Literal["input_text"]
    text: str


class InputImage(TypedDict):
    """One inline image, using Core's default image preparation.

    Remote URLs, file IDs and local paths are unsupported. Core owns decoding
    and model-specific processing. Only the default detail is supported.
    """

    type: Literal["input_image"]
    image_url: str
    detail: NotRequired[Literal["auto"]]


class UserMessage(TypedDict):
    """A user message with the same role and content shape as Responses."""

    role: Literal["user"]
    content: str | Sequence[InputText | InputImage]
    type: NotRequired[Literal["message"]]


UserInput: TypeAlias = str | UserMessage


def _native_input(input: UserInput) -> list[dict[str, Any]]:
    if isinstance(input, str):
        return [{"type": "text", "text": input}]
    if not isinstance(input, Mapping):
        raise TypeError("input must be a string or a user message")
    if (
        input.get("role") != "user"
        or input.get("type", "message") != "message"
        or set(input) - {"role", "content", "type"}
    ):
        raise ValueError("Only user messages are supported")
    content = input.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)) or not content:
        raise TypeError("User message content must be text or a non-empty content sequence")
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            raise TypeError("User message parts must be input_text or input_image mappings")
        if part.get("type") == "input_text":
            text = part.get("text")
            if set(part) != {"type", "text"} or not isinstance(text, str):
                raise ValueError("input_text requires a text string")
            parts.append({"type": "text", "text": text})
        elif part.get("type") == "input_image":
            if set(part) - {"type", "image_url", "detail"} or part.get("detail", "auto") != "auto":
                raise ValueError("input_image supports only image_url and default detail")
            image = part.get("image_url")
            if not isinstance(image, str):
                raise TypeError("input_image.image_url must be a base64 image data URL")
            header, separator, data = image.partition(",")
            if not (
                header.startswith("data:image/")
                and header.endswith(";base64")
                and separator
                and data
            ):
                raise ValueError(
                    "input_image requires a base64 image data URL; remote URLs, file IDs "
                    "and local paths are unsupported"
                )
            parts.append({"type": "image", "image_url": image})
        else:
            raise ValueError("User message parts must have type input_text or input_image")
    return parts


_preserve_module_identity(globals(), "agents.input")
