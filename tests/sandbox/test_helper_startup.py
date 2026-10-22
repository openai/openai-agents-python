from __future__ import annotations

import io
import json
import shlex
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from openai.types.responses import ResponseCustomToolCall

from agents import RunConfig, Runner
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.capabilities.shell import ShellToolSet
from agents.sandbox.session import SandboxSession
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import NoopSnapshotSpec
from agents.testing import ModelStep, ScriptedModel
from tests.test_responses import get_final_output_message, get_function_tool_call

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX helpers")


if TYPE_CHECKING or sys.platform != "win32":
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    class _RecordingClient(UnixLocalSandboxClient):
        def __init__(self) -> None:
            super().__init__(inherit_host_environment=False)
            self.sessions: list[SandboxSession] = []

        async def create(self, **kwargs: Any) -> SandboxSession:
            session = await super().create(**kwargs)
            self.sessions.append(session)
            return session


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [None, False, True], ids=["file-only", "rejected", "approved"])
async def test_runner_snapshot_does_not_execute_workspace_startup_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approval: bool | None
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged")
    payload = f"printf startup > {shlex.quote(str(outside))}\n"
    operations = [
        {"type": "create_file", "path": name, "diff": "+" + payload.rstrip("\n")}
        for name in (".profile", ".bash_profile", ".bashrc", ".zshenv")
    ]
    operations.append({"type": "create_file", "path": "notes.txt", "diff": "+saved content"})
    edit = ResponseCustomToolCall(
        id="edit",
        call_id="edit",
        type="custom_tool_call",
        name="apply_patch",
        input=json.dumps({"operations": operations}),
    )
    steps = [ModelStep(output=[edit])]
    if approval is not None:
        steps.append(
            ModelStep(
                output=[
                    get_function_tool_call(
                        "exec_command",
                        json.dumps(
                            {
                                "cmd": f"printf approved > {shlex.quote(str(outside))}",
                                "shell": "/bin/sh",
                                "login": False,
                            }
                        ),
                    )
                ]
            )
        )
    steps.append(ModelStep(output=[get_final_output_message("saved")]))

    def require_approval(tools: ShellToolSet) -> None:
        tools.exec_command.needs_approval = True

    agent = SandboxAgent(
        name="File task",
        capabilities=[Filesystem()]
        if approval is None
        else [Filesystem(), Shell(configure_tools=require_approval)],
        model=ScriptedModel(steps=steps),
    )
    client = _RecordingClient()
    config = RunConfig(tracing_disabled=True, sandbox=SandboxRunConfig(client=client))
    try:
        result = await Runner.run(agent, "Write the files.", run_config=config)
        assert outside.read_text() == "unchanged"
        if approval is not None:
            assert len(result.interruptions) == 1
            state = result.to_state()
            if approval:
                state.approve(result.interruptions[0])
            else:
                state.reject(result.interruptions[0])
            result = await Runner.run(agent, state, run_config=config)
        assert not result.interruptions
        assert result.final_output == "saved"
        assert outside.read_text() == ("approved" if approval else "unchanged")
        for session in client.sessions:
            assert session.state.snapshot.type == "local"
            assert session.state.snapshot_fingerprint
            with await session.state.snapshot.restore() as archive:
                with tarfile.open(fileobj=archive) as tar:
                    notes = tar.extractfile("./notes.txt")
                    assert notes is not None and notes.read() == b"saved content"
            assert not Path(session.state.manifest.root).exists()
    finally:
        for session in client.sessions:
            await session.shutdown()
            await client.delete(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write", "mkdir", "rm", "same-file"])
async def test_fixed_file_checks_do_not_load_workspace_profile(
    tmp_path: Path, operation: str
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged")
    client = UnixLocalSandboxClient(inherit_host_environment=False)
    session = await client.create(
        manifest=Manifest(root=str(tmp_path / "workspace")), snapshot=NoopSnapshotSpec()
    )
    try:
        await session.write(
            Path(".profile"),
            io.BytesIO(f"printf startup > {shlex.quote(str(outside))}\n".encode()),
        )
        await session.write(Path("notes.txt"), io.BytesIO(b"saved content"))
        inner = session._inner
        if operation == "read":
            await inner._check_read_with_exec("notes.txt")
        elif operation == "write":
            await inner._check_write_with_exec("notes.txt")
        elif operation == "mkdir":
            await inner._check_mkdir_with_exec("nested", parents=True)
            assert not (Path(session.state.manifest.root) / "nested").exists()
        elif operation == "rm":
            await inner._check_rm_with_exec("notes.txt")
        else:
            assert await BaseSandboxSession.same_file(inner, "notes.txt", "notes.txt")
        with await session.read(Path("notes.txt")) as data:
            assert data.read() == b"saved content"
        assert outside.read_text() == "unchanged"
    finally:
        await session.shutdown()
