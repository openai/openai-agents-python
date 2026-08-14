from __future__ import annotations

import base64
import io

import pytest

from agents.sandbox import Manifest
from agents.sandbox.capabilities.tools import ViewImageArgs, ViewImageTool
from agents.sandbox.capabilities.tools.shell_tool import _resolve_workdir_command
from agents.testing import scripted_sandbox_session
from agents.tool import ToolOutputImage

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a84QAAAAASUVORK5CYII="
)


def test_shell_workdir_normalizes_backslashes_as_sandbox_separators() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))

    command = _resolve_workdir_command(
        session=session,
        command="pwd",
        workdir=r"src\project",
    )

    assert command == "cd /workspace/src/project && pwd"


@pytest.mark.asyncio
async def test_view_image_normalizes_backslashes_as_sandbox_separators() -> None:
    session = scripted_sandbox_session(
        [{"method": "read", "result": io.BytesIO(_PNG_BYTES)}],
        manifest=Manifest(root="/workspace"),
    )
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path=r"images\plot.png"))

    assert isinstance(output, ToolOutputImage)
    assert session.calls[0].args[0].as_posix() == "/workspace/images/plot.png"
    session.assert_complete()
