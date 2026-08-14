from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import cast

import pytest

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.capabilities.tools import ViewImageTool
from agents.sandbox.errors import InvalidManifestPathError, WorkspaceReadNotFoundError
from agents.sandbox.types import User
from agents.testing import scripted_sandbox_session
from agents.tool import ToolOutputImage
from agents.tool_context import ToolContext

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a84QAAAAASUVORK5CYII="
)
_PNG_BYTES = base64.b64decode(_PNG_BASE64)


class TestViewImageTool:
    def test_view_image_accepts_needs_approval_setting(self) -> None:
        session = scripted_sandbox_session()

        async def needs_approval(_ctx: object, params: dict[str, object], _call_id: str) -> bool:
            return str(params["path"]).startswith("sensitive/")

        tool = ViewImageTool(session=session, needs_approval=needs_approval)

        assert cast(object, tool.needs_approval) is needs_approval

    @pytest.mark.asyncio
    async def test_view_image_returns_tool_output_image_for_png(self) -> None:
        session = scripted_sandbox_session([{"method": "read", "result": io.BytesIO(_PNG_BYTES)}])
        tool = ViewImageTool(session=session)

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"images/dot.png"}',
        )

        assert isinstance(output, ToolOutputImage)
        assert output.image_url == f"data:image/png;base64,{_PNG_BASE64}"
        assert output.detail is None
        session.assert_complete()

    @pytest.mark.asyncio
    async def test_view_image_reads_absolute_extra_path_grant(self) -> None:
        session = scripted_sandbox_session(
            [{"method": "read", "result": io.BytesIO(_PNG_BYTES)}],
            manifest=Manifest(
                root="/workspace",
                extra_path_grants=(SandboxPathGrant(path="/shared", read_only=True),),
            ),
        )
        tool = ViewImageTool(session=session)

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"/shared/dot.png"}',
        )

        assert isinstance(output, ToolOutputImage)
        assert session.calls[0].args[0].as_posix() == "/shared/dot.png"
        session.assert_complete()

    @pytest.mark.asyncio
    async def test_view_image_still_rejects_ungranted_absolute_path(self) -> None:
        session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
        tool = ViewImageTool(session=session)

        with pytest.raises(InvalidManifestPathError):
            await tool.on_invoke_tool(
                cast(ToolContext[object], None),
                '{"path":"/shared/dot.png"}',
            )

        assert session.calls == ()

    @pytest.mark.asyncio
    async def test_view_image_reads_as_bound_user(self) -> None:
        session = scripted_sandbox_session([{"method": "read", "result": io.BytesIO(_PNG_BYTES)}])
        tool = ViewImageTool(session=session, user=User(name="sandbox-user"))

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"images/dot.png"}',
        )

        assert isinstance(output, ToolOutputImage)
        assert session.calls[0].kwargs["user"] == User(name="sandbox-user")
        session.assert_complete()

    @pytest.mark.asyncio
    async def test_view_image_rejects_non_image_files(self) -> None:
        session = scripted_sandbox_session([{"method": "read", "result": io.BytesIO(b"hello\n")}])
        tool = ViewImageTool(session=session)

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"path":"notes.txt"}',
        )

        assert output == "image path `notes.txt` is not a supported image file"
        session.assert_complete()

    @pytest.mark.asyncio
    async def test_view_image_rejects_images_larger_than_10mb(self) -> None:
        session = scripted_sandbox_session(
            [
                {
                    "method": "read",
                    "result": io.BytesIO(b"\x89PNG\r\n\x1a\n" + (b"0" * (_MAX_IMAGE_BYTES + 1))),
                }
            ]
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
        session.assert_complete()

    @pytest.mark.asyncio
    async def test_view_image_rejection_text_does_not_expose_provider_path(self) -> None:
        provider_root = Path("/provider/private/root")
        session = scripted_sandbox_session(
            [
                {
                    "method": "read",
                    "error": WorkspaceReadNotFoundError(path=provider_root / "images/missing.png"),
                },
                {"method": "read", "result": io.BytesIO(b"hello\n")},
                {
                    "method": "read",
                    "result": io.BytesIO(b"\x89PNG\r\n\x1a\n" + (b"0" * (_MAX_IMAGE_BYTES + 1))),
                },
            ],
            manifest=Manifest(root=str(provider_root)),
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
