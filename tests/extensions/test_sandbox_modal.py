from __future__ import annotations

import importlib
import io
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import File, GCSMount
from agents.sandbox.errors import InvalidManifestPathError, WorkspaceArchiveReadError
from agents.sandbox.manifest import Environment
from agents.sandbox.types import ExecResult


def _load_modal_module(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[dict[str, object]], list[str]]:
    create_calls: list[dict[str, object]] = []
    registry_tags: list[str] = []

    class _FakeImage:
        object_id = "im-123"

        @staticmethod
        def from_registry(_tag: str) -> _FakeImage:
            registry_tags.append(_tag)
            return _FakeImage()

        @staticmethod
        def from_id(_image_id: str) -> _FakeImage:
            return _FakeImage()

    class _FakeSandboxInstance:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.terminate_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def poll(self) -> None:
            return None

    class _FakeSandbox:
        @staticmethod
        def create(**kwargs: object) -> _FakeSandboxInstance:
            create_calls.append(dict(kwargs))
            return _FakeSandboxInstance()

        @staticmethod
        def from_id(_sandbox_id: str) -> _FakeSandboxInstance:
            return _FakeSandboxInstance()

    class _FakeApp:
        @staticmethod
        def lookup(_name: str, *, create_if_missing: bool = False) -> object:
            _ = create_if_missing
            return object()

    fake_modal: Any = types.ModuleType("modal")
    fake_modal.Image = _FakeImage
    fake_modal.App = _FakeApp
    fake_modal.Sandbox = _FakeSandbox

    fake_container_process: Any = types.ModuleType("modal.container_process")
    fake_container_process.ContainerProcess = object

    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    monkeypatch.setitem(sys.modules, "modal.container_process", fake_container_process)
    sys.modules.pop("agents.extensions.sandbox.sandboxes.modal", None)

    module: Any = importlib.import_module("agents.extensions.sandbox.sandboxes.modal")
    return module, create_calls, registry_tags


@pytest.mark.asyncio
async def test_modal_sandbox_create_passes_manifest_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    session = await client.create(
        manifest=Manifest(environment=Environment(value={"SANDBOX_FLAG": "enabled"})),
        options=modal_module.ModalSandboxClientOptions(app_name="sandbox-tests"),
    )

    await session._inner._ensure_sandbox()  # noqa: SLF001

    assert create_calls
    assert create_calls[0]["env"] == {"SANDBOX_FLAG": "enabled"}
    assert registry_tags == ["python:3.11-slim"]


@pytest.mark.asyncio
async def test_modal_stop_is_persistence_only_and_shutdown_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    sandbox = sys.modules["modal"].Sandbox.create()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    session._running = True

    await session.stop()

    assert sandbox.terminate_calls == 0
    assert session.state.sandbox_id == "sb-123"
    assert await session.running() is True

    await session.shutdown()

    assert sandbox.terminate_calls == 1
    assert session.state.sandbox_id is None
    assert await session.running() is False


@pytest.mark.asyncio
async def test_modal_snapshot_failure_restores_ephemeral_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeRestoreProcess:
        def __init__(self, owner: _FakeSnapshotSandbox) -> None:
            self._owner = owner
            self.stderr = io.BytesIO(b"")
            self.stdin = self._FakeStdin(owner)

        class _FakeStdin:
            def __init__(self, owner: _FakeSnapshotSandbox) -> None:
                self._owner = owner
                self._buffer = bytearray()

            def write(self, data: bytes) -> None:
                self._buffer.extend(data)

            def write_eof(self) -> None:
                return

            def drain(self) -> None:
                return

        def wait(self) -> int:
            self._owner.restore_payloads.append(bytes(self.stdin._buffer))
            return 0

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.restore_payloads: list[bytes] = []

        def snapshot_filesystem(self) -> str:
            raise RuntimeError("snapshot failed")

        def exec(self, *command: object, **kwargs: object) -> _FakeRestoreProcess:
            _ = kwargs
            assert command[:3] == ("tar", "xf", "-")
            return _FakeRestoreProcess(self)

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={"tmp.txt": File(content=b"ephemeral", ephemeral=True)},
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"ephemeral-backup", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context["reason"] == "snapshot_filesystem_failed"
    assert sandbox.restore_payloads == [b"ephemeral-backup"]


@pytest.mark.asyncio
async def test_modal_snapshot_filesystem_uses_resolved_mount_paths_for_backup_and_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeRestoreProcess:
        def __init__(self) -> None:
            self.stderr = io.BytesIO(b"")
            self.stdin = self._FakeStdin()

        class _FakeStdin:
            def write(self, data: bytes) -> None:
                _ = data

            def write_eof(self) -> None:
                return

            def drain(self) -> None:
                return

        def wait(self) -> int:
            return 0

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def snapshot_filesystem(self) -> str:
            return "snap-123"

        def exec(self, *command: object, **kwargs: object) -> _FakeRestoreProcess:
            _ = kwargs
            assert command[:3] == ("tar", "xf", "-")
            return _FakeRestoreProcess()

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={"logical": GCSMount(bucket="bucket", mount_path=Path("actual"))},
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"ephemeral-backup", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    archive = await session.persist_workspace()

    assert archive.read() == modal_module._encode_snapshot_filesystem_ref(snapshot_id="snap-123")
    assert commands[0][0:2] == ["sh", "-lc"]
    assert "actual" in commands[0][2]
    assert "logical" in commands[0][2]
    assert commands[1] == ["rm", "-rf", "--", "/workspace/actual", "/workspace/logical"]


@pytest.mark.asyncio
async def test_modal_tar_persist_uses_resolved_mount_paths_for_excludes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={"logical": GCSMount(bucket="bucket", mount_path=Path("actual"))},
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=None)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        return ExecResult(stdout=b"tar-bytes", stderr=b"", exit_code=0)

    monkeypatch.setattr(session, "exec", _fake_exec)

    archive = await session.persist_workspace()

    assert archive.read() == b"tar-bytes"
    assert commands == [
        [
            "tar",
            "cf",
            "-",
            "--exclude",
            "./actual",
            "--exclude",
            "./logical",
            "-C",
            "/workspace",
            ".",
        ]
    ]


@pytest.mark.asyncio
async def test_modal_snapshot_filesystem_rejects_escaping_mount_paths_before_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.snapshot_calls = 0

        def snapshot_filesystem(self) -> str:
            self.snapshot_calls += 1
            return "snap-123"

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={"logical": GCSMount(bucket="bucket", mount_path=Path("/workspace/../../tmp"))},
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        commands.append([str(part) for part in command])
        raise AssertionError("exec() should not run for escaping mount paths")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = (fn, args, call_timeout, kwargs)
        raise AssertionError("snapshot_filesystem() should not run for escaping mount paths")

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.persist_workspace()

    assert commands == []
    assert sandbox.snapshot_calls == 0
