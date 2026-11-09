"""Responses-shaped user messages for native sessions."""

from collections.abc import Mapping as Mapping, Sequence as Sequence
from typing import Any as Any, Literal as Literal, TypeAlias as TypeAlias

from typing_extensions import NotRequired as NotRequired, TypedDict as TypedDict

from ._media import ImageDetail as ImageDetail, _capture_file, _capture_image
from ._public import preserve_module_identity as _preserve_module_identity


class InputText(TypedDict):
    type: Literal["input_text"]
    text: str


class InputImage(TypedDict):
    """An image URL/data URL. Core prepares it for the actual current model."""

    type: Literal["input_image"]
    image_url: str
    detail: NotRequired[ImageDetail | None]


class InputImageFile(TypedDict):
    """An opaque provider image ID; never a host path."""

    type: Literal["input_image"]
    file_id: str
    detail: NotRequired[ImageDetail | None]


class InputFile(TypedDict):
    """Exactly one provider ID, inline base64 data or HTTPS URL; validated at capture."""

    type: Literal["input_file"]
    file_id: NotRequired[str]
    file_data: NotRequired[str]
    file_url: NotRequired[str]
    filename: NotRequired[str | None]


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str | Sequence[InputText | InputImage | InputImageFile | InputFile]
    type: NotRequired[Literal["message"]]


UserInput: TypeAlias = str | UserMessage


class AssistantMessage(TypedDict):
    """Settled assistant text; IDs, tools and reasoning are not seed data."""

    role: Literal["assistant"]
    content: str
    type: NotRequired[Literal["message"]]


InitialMessage: TypeAlias = UserMessage | AssistantMessage


def _native_input(input: UserInput) -> list[dict[str, Any]]:
    if isinstance(input, str):
        return [{"type": "text", "text": input}]
    if not isinstance(input, Mapping):
        raise TypeError("input must be a string or a user message")
    captured = dict(input)
    if (
        captured.get("role") != "user"
        or captured.get("type", "message") != "message"
        or set(captured) - {"role", "content", "type"}
    ):
        raise ValueError("Only user messages are supported")
    content = captured.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)) or not content:
        raise TypeError("User message content must be text or a non-empty content sequence")
    parts: list[dict[str, Any]] = []
    for value in content:
        if not isinstance(value, Mapping):
            raise TypeError("User message parts must be content mappings")
        part = dict(value)
        kind = part.pop("type", None)
        if kind == "input_text":
            if set(part) != {"text"} or not isinstance(part["text"], str):
                raise ValueError("input_text requires a text string")
            parts.append({"type": "text", "text": part["text"]})
        elif kind == "input_image":
            parts.append({"type": "image", **_capture_image(part)})
        elif kind == "input_file":
            parts.append({"type": "file", **_capture_file(part)})
        else:
            raise ValueError("Unsupported user content type")
    return parts


def _response_content(parts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project captured/native data only; native admission retains all policy."""
    result: list[dict[str, Any]] = []
    tags = {
        "text": "input_text",
        "image": "input_image",
        "file": "input_file",
        "inputText": "input_text",
        "inputImage": "input_image",
        "inputFile": "input_file",
    }
    for value in parts:
        part = dict(value)
        kind = part.pop("type", None)
        if kind not in tags:
            raise ValueError("Unsupported captured content")
        if kind == "inputImage":
            part = {
                "image_url" if key == "imageUrl" else "file_id" if key == "fileId" else key: item
                for key, item in part.items()
            }
        tag = tags[kind]
        fields = {
            "input_text": {"text"},
            "input_image": {"image_url", "file_id", "detail"},
            "input_file": {"file_id", "file_data", "file_url", "filename"},
        }[tag]
        result.append({"type": tag, **{key: item for key, item in part.items() if key in fields}})
    return result


_preserve_module_identity(globals(), "agents.input")
