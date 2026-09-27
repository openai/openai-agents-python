from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import pytest

from agents.sandbox import Manifest
from agents.sandbox.capabilities.tools import ViewImageArgs, ViewImageTool
from agents.sandbox.capabilities.tools.shell_tool import _resolve_workdir_command
from agents.sandbox.config import MemoryLayoutConfig
from agents.sandbox.memory.storage import SandboxMemoryStorage
from agents.sandbox.types import ExecResult
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

    assert command == "cd /workspace/src/project || exit\npwd"


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

    assert command == f"cd {workspace.as_posix()}/src/project || exit\npwd"


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


@pytest.mark.asyncio
async def test_memory_layout_probes_existing_files_with_posix_paths() -> None:
    """The existence probe must be POSIX so an existing memory file is not replaced.

    A Windows host normalizes the sandbox path to a native one, and stringifying it sends
    ``test -f \\workspace\\memories\\MEMORY.md`` into a POSIX sandbox. That probe can never
    succeed, so every layout check overwrites the file with an empty one. ``ensure_layout`` runs on
    each rollout enqueue and on flush, so the loss repeats.
    """
    session = scripted_sandbox_session(
        [
            *({"method": "mkdir", "result": None} for _ in range(5)),
            {"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)},
            {"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)},
        ],
        manifest=Manifest(root="/workspace"),
    )
    storage = SandboxMemoryStorage(session=session, layout=MemoryLayoutConfig())

    await storage.ensure_layout()

    probes = [call for call in session.calls if call.method == "exec"]
    assert [call.args[2] for call in probes] == [
        "/workspace/memories/MEMORY.md",
        "/workspace/memories/memory_summary.md",
    ]
    session.assert_complete()
