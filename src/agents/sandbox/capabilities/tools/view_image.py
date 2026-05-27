from __future__ import annotations

import base64
import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....run_context import RunContextWrapper
from ....tool import FunctionTool, ToolOutputImage
from ...errors import WorkspaceReadNotFoundError
from ...session.base_sandbox_session import BaseSandboxSession
from ...types import User

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_SIZE_LABEL = "10MB"
_SVG_SNIFF_BYTES = 2048


def _detect_image_mime_type(path: Path, payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"

    snippet = payload[:_SVG_SNIFF_BYTES].lstrip().lower()
    if snippet.startswith(b"<svg") or (snippet.startswith(b"<?xml") and b"<svg" in snippet):
        return "image/svg+xml"

    guessed_type, _ = mimetypes.guess_type(path.name)
    if isinstance(guessed_type, str) and guessed_type.startswith("image/"):
        return guessed_type
    return None


def _encode_data_url(mime_type: str, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _coerce_payload_bytes(payload: object) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, memoryview):
        return payload.tobytes()
    raise TypeError(f"view_image read an unsupported payload type: {type(payload).__name__}")


class ViewImageArgs(BaseModel):
    path: str = Field(
        description="Path to the image file. Absolute and relative workspace paths are supported.",
        min_length=1,
    )


@dataclass(init=False)
class ViewImageTool(FunctionTool):
    tool_name: ClassVar[str] = "view_image"
    args_model: ClassVar[type[ViewImageArgs]] = ViewImageArgs
    tool_description: ClassVar[str] = (
        "Loads an image from the sandbox workspace and returns it as a structured image output."
    )
    session: BaseSandboxSession = field(init=False, repr=False, compare=False)
    user: str | User | None = field(default=None, init=False, repr=False, compare=False)

    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        user: str | User | None = None,
        needs_approval: (
            bool | Callable[[RunContextWrapper[Any], dict[str, Any], str], Awaitable[bool]]
        ) = False,
    ) -> None:
        self.session = session
        self.user = user
        super().__init__(
            name=self.tool_name,
            description=self.tool_description,
            params_json_schema=self.args_model.model_json_schema(),
            on_invoke_tool=self._invoke,
            strict_json_schema=False,
            needs_approval=needs_approval,
        )

    async def _invoke(self, _: object, raw_input: str) -> ToolOutputImage | str:
        return await self.run(self.args_model.model_validate_json(raw_input))

    async def run(self, args: ViewImageArgs) -> ToolOutputImage | str:
        input_path = Path(args.path)
        path_policy = self.session._workspace_path_policy()
        resolved_path = path_policy.absolute_workspace_path(input_path)
        display_path = path_policy.relative_path(input_path).as_posix()

        try:
            file_obj = await self.session.read(resolved_path, user=self.user)
        except (FileNotFoundError, WorkspaceReadNotFoundError):
            return f"image path `{display_path}` was not found"
        except Exception as exc:
            return f"unable to read image at `{display_path}`: {type(exc).__name__}"

        try:
            payload = file_obj.read(_MAX_IMAGE_BYTES + 1)
        finally:
            try:
                file_obj.close()
            except Exception:
                pass

        try:
            payload = _coerce_payload_bytes(payload)
        except TypeError as exc:
            return f"unable to read image at `{display_path}`: {exc}"
        if len(payload) > _MAX_IMAGE_BYTES:
            return (
                f"image path `{display_path}` exceeded the allowed size of "
                f"{_MAX_IMAGE_SIZE_LABEL}; resize or compress the image and try again"
            )

        mime_type = _detect_image_mime_type(resolved_path, payload)
        if mime_type is None:
            return f"image path `{display_path}` is not a supported image file"

        return ToolOutputImage(image_url=_encode_data_url(mime_type, payload))
