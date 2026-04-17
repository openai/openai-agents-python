from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path
from typing import cast

import pytest

from agents.sandbox import Manifest
from agents.sandbox.capabilities.tools import ViewImageTool
from agents.sandbox.errors import WorkspaceReadNotFoundError
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, User
from agents.tool import ToolOutputImage
from agents.tool_context import ToolContext
from tests.utils.factories import TestSessionState

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a84QAAAAASUVORK5CYII="
)
_PNG_BYTES = base64.b64decode(_PNG_BASE64)


class _ImageSession(BaseSandboxSession):
    def __init__(self, manifest: Manifest) -> None:
        self.state = TestSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self.files: dict[Path, bytes] = {}
        self.read_users: list[str | None] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def running(self) -> bool:
        return True

    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        self.read_users.append(user.name if isinstance(user, User) else user)
        normalized = self.normalize_path(path)
        if normalized not in self.files:
            raise FileNotFoundError(normalized)
        return io.BytesIO(self.files[normalized])

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        _ = user
        normalized = self.normalize_path(path)
        payload = data.read()
        if isinstance(payload, str):
            self.files[normalized] = payload.encode("utf-8")
        else:
            self.files[normalized] = bytes(payload)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        raise AssertionError("_exec_internal() should not be called")

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data


class _ProviderNotFoundImageSession(_ImageSession):
    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        self.read_users.append(user.name if isinstance(user, User) else user)
        normalized = self.normalize_path(path)
        if normalized in self.files:
            return io.BytesIO(self.files[normalized])
        raise WorkspaceReadNotFoundError(path=normalized)


class TestViewImageTool:
    def test_view_image_accepts_needs_approval_setting(self) -> None:
        session = _ImageSession(Manifest(root="/workspace"))

        async def needs_approval(_ctx: object, params: dict[str, object], _call_id: str) -> bool:
            return str(params["path"]).startswith("sensitive/")

        tool = ViewImageTool(session=session, needs_approval=needs_approval)

        assert cast(object, tool.needs_approval) is needs_approval

    @pytest.mark.asyncio
    async def test_view_image_returns_tool_output_image_for_png(self) -> None:
        session = _ImageSession(Manifest(root="/workspace"))
        session.files[Path("/workspace/images/dot.png")] = _PNG_BYTES
        tool = ViewImageTool(session=session)

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"images/dot.png"}',
        )

        assert isinstance(output, ToolOutputImage)
        assert output.image_url == f"data:image/png;base64,{_PNG_BASE64}"
        assert output.detail is None

    @pytest.mark.asyncio
    async def test_view_image_reads_as_bound_user(self) -> None:
        session = _ImageSession(Manifest(root="/workspace"))
        session.files[Path("/workspace/images/dot.png")] = _PNG_BYTES
        tool = ViewImageTool(session=session, user=User(name="sandbox-user"))

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"images/dot.png"}',
        )

        assert isinstance(output, ToolOutputImage)
        assert session.read_users == ["sandbox-user"]

    @pytest.mark.asyncio
    async def test_view_image_rejects_non_image_files(self) -> None:
        session = _ImageSession(Manifest(root="/workspace"))
        session.files[Path("/workspace/notes.txt")] = b"hello\n"
        tool = ViewImageTool(session=session)

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"notes.txt"}',
        )

        assert output == "image path `notes.txt` is not a supported image file"

    @pytest.mark.asyncio
    async def test_view_image_rejects_images_larger_than_10mb(self) -> None:
        session = _ImageSession(Manifest(root="/workspace"))
        session.files[Path("/workspace/images/huge.png")] = b"\x89PNG\r\n\x1a\n" + (
            b"0" * (_MAX_IMAGE_BYTES + 1)
        )
        tool = ViewImageTool(session=session)

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"images/huge.png"}',
        )

        assert output == (
            "image path `images/huge.png` exceeded the allowed size of 10MB; "
            "resize or compress the image and try again"
        )

    @pytest.mark.asyncio
    async def test_view_image_rejection_text_does_not_expose_provider_path(self) -> None:
        provider_root = Path("/provider/private/root")
        session = _ProviderNotFoundImageSession(Manifest(root=str(provider_root)))
        session.files[provider_root / "notes.txt"] = b"hello\n"
        session.files[provider_root / "images/huge.png"] = b"\x89PNG\r\n\x1a\n" + (
            b"0" * (_MAX_IMAGE_BYTES + 1)
        )
        tool = ViewImageTool(session=session)

        missing_output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"images/missing.png"}',
        )
        non_image_output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"notes.txt"}',
        )
        huge_output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"images/huge.png"}',
        )

        outputs = [missing_output, non_image_output, huge_output]
        assert outputs == [
            "image path `images/missing.png` was not found",
            "image path `notes.txt` is not a supported image file",
            (
                "image path `images/huge.png` exceeded the allowed size of 10MB; "
                "resize or compress the image and try again"
            ),
        ]
        for output in outputs:
            assert isinstance(output, str)
            assert str(provider_root) not in output
