from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

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


@pytest.mark.skipif(sys.platform == "win32", reason="UnixLocalSandbox is Unix-only")
def test_shell_workdir_normalizes_backslashes_before_unix_local_resolution(
    tmp_path: Path,
) -> None:
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )
    from agents.sandbox.snapshot import NoopSnapshot

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(workspace)),
            snapshot=NoopSnapshot(id="noop"),
        )
    )

    command = _resolve_workdir_command(
        session=session,
        command="pwd",
        workdir=r"src\project",
    )

    assert command == f"cd {workspace.as_posix()}/src/project && pwd"


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
